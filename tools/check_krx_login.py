# -*- coding: utf-8 -*-
"""
KRX 회원 로그인(pykrx) 동작 확인 + 정규장 공식 종가 비교.

KRX_ID / KRX_PW 가 현재 프로세스 환경에 없으면 Windows 사용자 환경변수(HKCU\\Environment),
그다음 config.py 의 KRX_ID / KRX_PW 순으로 읽어 옵니다.

확인 항목:
  1. 전종목 일괄 시세 (KRX, 하루 1회 호출)
  2. 개별 종목 수정 전 주가 (KRX, adjusted=False)
  3. 로컬 bars_15m 의 15:30 정규 종가 / 애프터 마지막 종가와 비교

사용 예::

    python tools/check_krx_login.py
    python tools/check_krx_login.py --code 000660 --date 20261002
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Optional

import pandas as pd

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _read_user_env(name: str) -> str:
    """Windows 사용자 환경변수 값을 레지스트리에서 직접 읽습니다. 실패 시 빈 문자열."""
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            val, _ = winreg.QueryValueEx(key, name)
            return str(val or "").strip()
    except Exception:
        return ""


def _ensure_krx_env() -> bool:
    """KRX_ID/KRX_PW 를 프로세스 환경에 채웁니다. (사용자 환경변수 → config.py) 둘 다 있으면 True."""
    for name in ("KRX_ID", "KRX_PW"):
        if not os.environ.get(name, "").strip():
            v = _read_user_env(name)
            if v:
                os.environ[name] = v
    try:
        if _ROOT not in sys.path:
            sys.path.insert(0, _ROOT)
        from utils.krx_credentials import ensure_krx_env

        return ensure_krx_env()
    except Exception as e:
        print(f"[경고] config.py 계정 로드 실패: {e}")
        return bool(os.environ.get("KRX_ID", "").strip() and os.environ.get("KRX_PW", "").strip())


def _local_closes(code: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """bars_15m 에서 날짜별 15:30 정규 종가와 마지막 봉 종가."""
    db = os.path.join(_ROOT, "trading_data.db")
    if not os.path.exists(db):
        return None
    try:
        with sqlite3.connect(db) as con:
            q = pd.read_sql(
                "SELECT datetime, close FROM bars_15m WHERE code=? AND datetime>=? AND datetime<?",
                con,
                params=(code, f"{start[:4]}-{start[4:6]}-{start[6:]}",
                        (pd.Timestamp(end) + pd.DateOffset(days=1)).strftime("%Y-%m-%d")),
            )
    except Exception as e:
        print(f"[경고] bars_15m 조회 실패: {e}")
        return None
    if q.empty:
        return None
    q["datetime"] = pd.to_datetime(q["datetime"])
    q["date"] = q["datetime"].dt.normalize()
    hm = q["datetime"].dt.strftime("%H:%M")
    reg = q[hm <= "15:30"].groupby("date")["close"].last().rename("정규종가(15:30)")
    last = q.groupby("date")["close"].last().rename("마지막봉종가")
    return pd.concat([reg, last], axis=1)


def main() -> int:
    ap = argparse.ArgumentParser(description="KRX 로그인 및 공식 종가 확인")
    ap.add_argument("--code", default="005930")
    ap.add_argument("--date", default="20261002", help="전종목 일괄 조회 일자 (YYYYMMDD)")
    ap.add_argument("--start", default="20260925")
    ap.add_argument("--end", default="20261002")
    args = ap.parse_args()

    if not _ensure_krx_env():
        print("[실패] KRX_ID / KRX_PW 환경변수를 찾지 못했습니다. (사용자 환경변수 등록 여부 확인)")
        return 1

    # pykrx 는 import 시점에 환경변수로 로그인하므로 환경 설정 이후에 import
    try:
        from pykrx import stock
    except Exception as e:
        print(f"[실패] pykrx import 실패: {e}")
        return 1

    ok = True

    print(f"\n=== 1. 전종목 일괄 시세 (KOSPI, {args.date}) ===")
    try:
        t0 = time.time()
        bulk = stock.get_market_ohlcv(args.date, market="KOSPI")
        print(f"{time.time() - t0:.2f}초, {len(bulk)}종목")
        if args.code in bulk.index:
            print(bulk.loc[[args.code]])
        else:
            print(bulk.head(3))
    except Exception as e:
        ok = False
        print(f"[실패] 실패: {type(e).__name__}: {e}")

    print(f"\n=== 2. {args.code} 수정 전 주가 (KRX, {args.start}~{args.end}) ===")
    krx_df: Optional[pd.DataFrame] = None
    try:
        t0 = time.time()
        krx_df = stock.get_market_ohlcv(args.start, args.end, args.code, adjusted=False)
        print(f"{time.time() - t0:.2f}초, {len(krx_df)}행")
        if krx_df is None or krx_df.empty:
            ok = False
            print("[실패] 빈 응답 (로그인 실패 가능성)")
    except Exception as e:
        ok = False
        print(f"[실패] 실패: {type(e).__name__}: {e}")

    print("\n=== 3. 종가 비교 (KRX vs 네이버 vs 로컬 15분봉) ===")
    try:
        nav = stock.get_market_ohlcv(args.start, args.end, args.code, adjusted=True)
        parts = []
        if krx_df is not None and not krx_df.empty and "종가" in krx_df.columns:
            parts.append(krx_df["종가"].rename("KRX종가"))
        if nav is not None and not nav.empty and "종가" in nav.columns:
            parts.append(nav["종가"].rename("네이버종가"))
        loc = _local_closes(args.code, args.start, args.end)
        if loc is not None:
            parts.append(loc)
        if parts:
            cmp_df = pd.concat(parts, axis=1)
            cmp_df.index = pd.to_datetime(cmp_df.index).strftime("%Y-%m-%d")
            print(cmp_df.to_string())
    except Exception as e:
        print(f"[경고] 비교 실패: {type(e).__name__}: {e}")

    print("\n[OK] KRX 로그인 조회 정상" if ok else "\n[실패] KRX 조회 실패 — ID/PW 또는 계정 상태 확인")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
