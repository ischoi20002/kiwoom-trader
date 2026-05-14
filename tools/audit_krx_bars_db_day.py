# -*- coding: utf-8 -*-
"""
``trading_data.db`` 의 1분·15분봉 시각이 KRX 정규장 격자와 맞는지 점검한다.

- 1분: 당일 **09:00~15:19** (연속 매매). **15:20~15:30 동시호가** 구간은 DB에 없어도 정상으로 본다.
- 15분: DB 행 시각이 ``09:15`` 시작 ``15분`` 간격(라벨=구간 끝) 집합에 속하는지·누락 요약

**단일 분 누락**(예: 09:03, 11:44)은 DB만으로는 원인 확정이 불가하다.
(증권사 API 일시 오류·레이트리밋, 네트워크, **거래정지/휴장**, 해당 분 무거래 등)

실시간 API vs 백테 **직접 대조**는 API 응답을 파일로 남기지 않은 이상 불가하지만,
**백테가 읽는 DB 자체**가 격자에서 얼마나 벗어나는지는 이 스크립트로 즉시 확인 가능하다.

사용 예::

    python tools/audit_krx_bars_db_day.py --day 2026-05-14 --codes 139480,011780,042700
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Iterable, List, Set, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TRADING_DB = ROOT / "trading_data.db"


def _parse_codes(s: str) -> List[str]:
    return [p.strip().zfill(6) for p in str(s).split(",") if p.strip()]


def expected_1m_opens(day: str) -> pd.DatetimeIndex:
    """
    연속 매매 구간 분 시작 라벨.

    장 마감 **동시호가(대개 15:20~15:30)** 구간은 1분 OHLC가 비거나 의미가
    달라질 수 있어, 백테·DB 정합 점검에서는 **15:19분까지**만 기대 집합에 넣는다.
    """
    d0 = f"{day} 09:00:00"
    d1 = f"{day} 15:19:00"
    return pd.date_range(d0, d1, freq="1min")


def expected_15m_end_labels(day: str) -> pd.DatetimeIndex:
    """DB 15m 라벨 = 구간 끝(09:15, 09:30, …, 15:30) 가정."""
    d0 = f"{day} 09:15:00"
    d1 = f"{day} 15:30:00"
    return pd.date_range(d0, d1, freq="15min")


def load_db_1m_datetimes(con: sqlite3.Connection, code: str, day: str) -> List[pd.Timestamp]:
    d0 = f"{day} 00:00:00"
    d1 = f"{day} 23:59:59"
    cur = con.execute(
        "SELECT datetime FROM bars_1m WHERE code = ? AND datetime >= ? AND datetime <= ? ORDER BY datetime",
        (code, d0, d1),
    )
    out: list[pd.Timestamp] = []
    for (dt_str,) in cur.fetchall():
        try:
            out.append(pd.Timestamp(str(dt_str)))
        except Exception:
            continue
    return out


def load_db_15m_datetimes(con: sqlite3.Connection, code: str, day: str) -> List[pd.Timestamp]:
    d0 = f"{day} 00:00:00"
    d1 = f"{day} 23:59:59"
    cur = con.execute(
        "SELECT datetime FROM bars_15m WHERE code = ? AND datetime >= ? AND datetime <= ? ORDER BY datetime",
        (code, d0, d1),
    )
    out: list[pd.Timestamp] = []
    for (dt_str,) in cur.fetchall():
        try:
            out.append(pd.Timestamp(str(dt_str)))
        except Exception:
            continue
    return out


def _fmt_ts_set(ts: Iterable[pd.Timestamp], limit: int = 8) -> str:
    xs = sorted({pd.Timestamp(t).floor("min") for t in ts})
    if len(xs) <= limit:
        return ", ".join(x.strftime("%H:%M") for x in xs)
    head = ", ".join(x.strftime("%H:%M") for x in xs[: limit // 2])
    tail = ", ".join(x.strftime("%H:%M") for x in xs[-(limit // 2) :])
    return f"{head} ... ({len(xs)}건) ... {tail}"


def _is_closing_auction_minute(t: pd.Timestamp, day: str) -> bool:
    """15:20~15:30 동시호가 구간(1분 봉은 없거나 있어도 기대 격자와 별도)."""
    try:
        tt = pd.Timestamp(t).floor("min")
        return tt >= pd.Timestamp(f"{day} 15:20:00")
    except Exception:
        return False


def audit_code(con: sqlite3.Connection, day: str, code: str) -> Tuple[str, ...]:
    exp1 = set(expected_1m_opens(day))
    exp15 = set(expected_15m_end_labels(day))

    m1 = load_db_1m_datetimes(con, code, day)
    m15 = load_db_15m_datetimes(con, code, day)

    s1 = {pd.Timestamp(t).floor("min") for t in m1}
    s15 = {pd.Timestamp(t).floor("min") for t in m15}

    miss1 = sorted(exp1 - s1)
    raw_extra = sorted(s1 - exp1)
    extra1 = [t for t in raw_extra if not _is_closing_auction_minute(t, day)]
    dup1 = len(m1) - len(s1)

    miss15 = sorted(exp15 - s15)
    extra15 = sorted(s15 - exp15)

    lines = [
        f"[{code}] 1m DB={len(m1)}행 고유분={len(s1)} | 격자 대비 누락={len(miss1)} 장외·이상={len(extra1)} 중복분={dup1}",
        f"       15m DB={len(m15)}행 고유슬롯={len(s15)} | 격자 대비 누락={len(miss15)} 장외·이상={len(extra15)}",
    ]
    if miss1:
        lines.append(f"       1m 누락(최대12): {_fmt_ts_set(miss1[:12], 12)}")
    if extra1:
        lines.append(f"       1m 장외/격자밖(최대12): {_fmt_ts_set(extra1[:12], 12)}")
    if miss15:
        lines.append(f"       15m 누락(최대12): {_fmt_ts_set(miss15[:12], 12)}")
    if extra15:
        lines.append(f"       15m 장외/격자밖(최대12): {_fmt_ts_set(extra15[:12], 12)}")
    return tuple(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="DB 1m/15m 시각이 KRX 격자와 맞는지 점검")
    ap.add_argument("--day", type=str, required=True, help="YYYY-MM-DD")
    ap.add_argument(
        "--codes",
        type=str,
        default="139480,011780,042700,028260,007070,000880,023530",
        help="콤마 구분 6자리 종목",
    )
    args = ap.parse_args()
    day = str(args.day).strip()[:10]
    codes = _parse_codes(args.codes)

    if not TRADING_DB.is_file():
        raise SystemExit(f"DB 없음: {TRADING_DB}")

    print(f"=== KRX 격자 vs DB ({day}) ===", flush=True)
    print(f"DB: {TRADING_DB}", flush=True)
    print(
        f"기대 1m 분 수(09:00~15:19, 동시호가 제외): {len(expected_1m_opens(day))} | "
        f"기대 15m 슬롯 수(09:15~15:30): {len(expected_15m_end_labels(day))}",
        flush=True,
    )
    print("", flush=True)

    try:
        con = sqlite3.connect(str(TRADING_DB))
    except OSError as e:
        raise SystemExit(f"DB 연결 실패: {e}") from e
    try:
        for c in codes:
            for line in audit_code(con, day, c):
                print(line, flush=True)
            print("", flush=True)
    finally:
        con.close()


if __name__ == "__main__":
    main()
