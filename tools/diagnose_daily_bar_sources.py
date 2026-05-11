# -*- coding: utf-8 -*-
"""
선정 불일치 등으로 의심되는 종목에 대해 **일봉 소스 3갈래**를 날짜별로 맞춰 비교합니다.

1. ``bars_1d`` (SQLite 저장 일봉)
2. ``bars_1m`` 전체를 읽어 **동일 규칙**으로 집계한 일봉 (`aggregate_daily_ohlcv_from_1m_db`)
3. (선택) 키움 REST ``ka10081`` 일봉 — ``--token`` 또는 환경변수 ``KIWOOM_ACCESS_TOKEN``

기본 비교 종목: 선정 CSV 불일치 사례로 쓰이던 ``161890``, ``032830``.

예::

    python tools/diagnose_daily_bar_sources.py
    python tools/diagnose_daily_bar_sources.py --codes 005930 000660
    set KIWOOM_ACCESS_TOKEN=...
    python tools/diagnose_daily_bar_sources.py --codes 161890 032830
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple


def _root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _load_settings(root: str) -> Dict[str, Any]:
    sys.path.insert(0, root)
    from core.defaults import DEFAULT_SETTINGS

    merged: Dict[str, Any] = dict(DEFAULT_SETTINGS)
    cfg = os.path.join(root, "settings.json")
    if os.path.isfile(cfg):
        try:
            with open(cfg, encoding="utf-8") as f:
                u = json.load(f)
            if isinstance(u, dict):
                merged.update(u)
        except Exception:
            pass
    return merged


def _norm_daily_from_bars_1d(df: Any) -> Any:
    """get_daily_bars 결과 → date, o, h, l, c, v."""
    import pandas as pd

    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "o", "h", "l", "c", "v"])
    d = df.copy()
    d["date"] = pd.to_datetime(d["time"], errors="coerce").dt.strftime("%Y-%m-%d")
    d = d.dropna(subset=["date"])
    out = pd.DataFrame(
        {
            "date": d["date"],
            "o": pd.to_numeric(d["Open"], errors="coerce"),
            "h": pd.to_numeric(d["High"], errors="coerce"),
            "l": pd.to_numeric(d["Low"], errors="coerce"),
            "c": pd.to_numeric(d["Close"], errors="coerce"),
            "v": pd.to_numeric(d["Volume"], errors="coerce"),
        }
    )
    return out.dropna(subset=["o", "h", "l", "c", "v"])


def _norm_daily_from_agg1m(df: Any) -> Any:
    """aggregate_daily_ohlcv_from_1m_db 결과(datetime, open,...) → date, o,h,l,c,v."""
    import pandas as pd

    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "o", "h", "l", "c", "v"])
    d = df.copy()
    d["date"] = d["datetime"].astype(str).str[:10]
    return pd.DataFrame(
        {
            "date": d["date"],
            "o": pd.to_numeric(d["open"], errors="coerce"),
            "h": pd.to_numeric(d["high"], errors="coerce"),
            "l": pd.to_numeric(d["low"], errors="coerce"),
            "c": pd.to_numeric(d["close"], errors="coerce"),
            "v": pd.to_numeric(d["volume"], errors="coerce"),
        }
    )


def _norm_daily_from_ka(df: Any) -> Any:
    import pandas as pd

    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "o", "h", "l", "c", "v"])
    d = df.copy()
    d["date"] = pd.to_datetime(d["time"], errors="coerce").dt.strftime("%Y-%m-%d")
    d = d.dropna(subset=["date"])
    return pd.DataFrame(
        {
            "date": d["date"],
            "o": pd.to_numeric(d["Open"], errors="coerce"),
            "h": pd.to_numeric(d["High"], errors="coerce"),
            "l": pd.to_numeric(d["Low"], errors="coerce"),
            "c": pd.to_numeric(d["Close"], errors="coerce"),
            "v": pd.to_numeric(d["Volume"], errors="coerce"),
        }
    )


def _1m_meta(db_path: str, code: str) -> Tuple[str, str, int]:
    """(min datetime, max datetime, row count) — 실패 시 '', '', 0."""
    try:
        with sqlite3.connect(db_path, timeout=60) as conn:
            row = conn.execute(
                """
                SELECT MIN(datetime), MAX(datetime), COUNT(1)
                FROM bars_1m WHERE code = ?
                """,
                (code,),
            ).fetchone()
        if not row or row[2] is None:
            return "", "", 0
        return str(row[0] or ""), str(row[1] or ""), int(row[2])
    except Exception:
        return "", "", 0


def _compare_pair(
    a: Any,
    b: Any,
    name_a: str,
    name_b: str,
    *,
    price_eps: float,
    vol_eps: float,
    max_print: int,
) -> Tuple[int, Any]:
    """
    두 표준 일봉 프레임 비교. 반환: (불일치 일수, 불일치 상위 max_print 행 DataFrame).
    """
    import pandas as pd

    if a.empty and b.empty:
        return 0, pd.DataFrame()
    m = a.merge(b, on="date", how="outer", suffixes=("_a", "_b"), indicator=True)
    diffs: List[Dict[str, Any]] = []

    for _, r in m.iterrows():
        tag = str(r.get("_merge", ""))
        if tag == "left_only":
            diffs.append({"date": r["date"], "issue": f"{name_a}만 존재"})
            continue
        if tag == "right_only":
            diffs.append({"date": r["date"], "issue": f"{name_b}만 존재"})
            continue
        issues: List[str] = []
        for col, lab in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close")):
            va = r[f"{col}_a"]
            vb = r[f"{col}_b"]
            if pd.isna(va) or pd.isna(vb):
                issues.append(f"{lab}:NaN")
                continue
            fa, fb = float(va), float(vb)
            if abs(fa - fb) > price_eps:
                issues.append(f"{lab}:{fa:.0f}!={fb:.0f}")
        va, vb = r["v_a"], r["v_b"]
        if pd.isna(va) or pd.isna(vb):
            issues.append("vol:NaN")
        elif abs(float(va) - float(vb)) > vol_eps:
            issues.append(f"vol:{float(va):.0f}!={float(vb):.0f}")
        if issues:
            diffs.append({"date": r["date"], "issue": "; ".join(issues)})

    out_df = pd.DataFrame(diffs)
    return len(diffs), out_df.head(max_print)


def _run_one_code(
    code: str,
    *,
    db_path: str,
    end_day: str,
    start_day: str,
    token: str,
    price_eps: float,
    vol_eps: float,
    max_print: int,
    use_api: bool,
) -> int:
    import pandas as pd

    from data_manager_v2 import DataManagerV2
    from utils.bar_resample import aggregate_daily_ohlcv_from_1m_db
    from utils.normalization import normalize_code

    try:
        ncode = normalize_code(code)
    except Exception:
        print(f"  [Skip] 코드 정규화 실패: {code}")
        return 1

    print(f"\n======== 종목 {ncode} ========")
    m1, m2, cnt = _1m_meta(db_path, ncode)
    print(f"  bars_1m: rows={cnt}  MIN={m1 or '(없음)'}  MAX={m2 or '(없음)'}")

    try:
        dm = DataManagerV2(db_path=db_path)
    except Exception as e:
        print(f"  [Err] DataManagerV2 실패: {e}")
        return 1

    try:
        df_1d = dm.get_daily_bars(ncode, start_dt=start_day, end_dt=end_day)
    except Exception as e:
        print(f"  [Err] get_daily_bars: {e}")
        df_1d = pd.DataFrame()

    try:
        df_agg_full = aggregate_daily_ohlcv_from_1m_db(db_path, ncode)
    except Exception as e:
        print(f"  [Err] aggregate_daily_ohlcv_from_1m_db: {e}")
        df_agg_full = pd.DataFrame()

    std_1d = _norm_daily_from_bars_1d(df_1d)
    std_agg = _norm_daily_from_agg1m(df_agg_full)
    std_agg = std_agg[(std_agg["date"] >= start_day) & (std_agg["date"] <= end_day)]

    n_mismatch, sample = _compare_pair(
        std_1d,
        std_agg,
        "bars_1d",
        "1m집계",
        price_eps=price_eps,
        vol_eps=vol_eps,
        max_print=max_print,
    )
    print(f"  bars_1d 행수={len(std_1d)}, 1m집계(구간필터) 행수={len(std_agg)}")
    print(f"  [bars_1d vs 1m집계] 날짜 불일치·OHLCV 차이 건수: {n_mismatch}")
    if not sample.empty:
        print(sample.to_string(index=False))

    std_api = pd.DataFrame(columns=["date", "o", "h", "l", "c", "v"])
    if use_api and token:
        try:
            from core.kiwoom_daily_bars import fetch_kiwoom_daily_ohlcv_dataframe

            df_api = fetch_kiwoom_daily_ohlcv_dataframe(
                ncode,
                token,
                min_date=start_day,
                max_date=end_day,
                min_request_interval_sec=1.2,
                page_delay_sec=0.35,
                max_pages=40,
                logger_callback=lambda msg: print(f"    {msg}"),
            )
            std_api = _norm_daily_from_ka(df_api)
            print(f"  ka10081 행수={len(std_api)}")
        except Exception as e:
            print(f"  [Err] ka10081 조회 실패: {e}")
    elif use_api:
        print("  ka10081: 토큰 없음 — 생략 (--token 또는 KIWOOM_ACCESS_TOKEN)")

    if not std_api.empty:
        n2, s2 = _compare_pair(
            std_agg,
            std_api,
            "1m집계",
            "ka10081",
            price_eps=price_eps,
            vol_eps=vol_eps,
            max_print=max_print,
        )
        print(f"  [1m집계 vs ka10081] 불일치 건수: {n2}")
        if not s2.empty:
            print(s2.to_string(index=False))
        n3, s3 = _compare_pair(
            std_1d,
            std_api,
            "bars_1d",
            "ka10081",
            price_eps=price_eps,
            vol_eps=vol_eps,
            max_print=max_print,
        )
        print(f"  [bars_1d vs ka10081] 불일치 건수: {n3}")
        if not s3.empty:
            print(s3.to_string(index=False))

    return 0


def main() -> int:
    root = _root()
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)
    # Windows 콘솔에서 한글 로그 깨짐 완화
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="일봉 소스 진단 (bars_1d / 1m집계 / ka10081)")
    ap.add_argument(
        "--codes",
        nargs="+",
        default=["161890", "032830"],
        help="6자리 종목코드 목록 (기본: 선정 불일치 예시 161890 032830)",
    )
    ap.add_argument(
        "--db",
        default="",
        help="trading_data.db 경로 (미지정 시 DataManagerV2 기본)",
    )
    ap.add_argument("--days", type=int, default=400, help="오늘 기준 역산 비교 일수(캘린더)")
    ap.add_argument(
        "--price-eps",
        type=float,
        default=1.0,
        help="가격 비교 허용 오차(원 단위, 기본 1)",
    )
    ap.add_argument(
        "--vol-eps",
        type=float,
        default=0.0,
        help="거래량 비교 허용 오차(기본 0)",
    )
    ap.add_argument(
        "--max-print",
        type=int,
        default=25,
        help="불일치 샘플 최대 출력 행 수",
    )
    ap.add_argument(
        "--token",
        default=os.environ.get("KIWOOM_ACCESS_TOKEN", "").strip(),
        help="키움 Bearer (미입력 시 환경변수 KIWOOM_ACCESS_TOKEN)",
    )
    ap.add_argument(
        "--no-api",
        action="store_true",
        help="ka10081 호출 생략",
    )
    args = ap.parse_args()

    try:
        end_dt = datetime.now().date()
        start_dt = end_dt - timedelta(days=max(1, int(args.days)))
        end_day = end_dt.strftime("%Y-%m-%d")
        start_day = start_dt.strftime("%Y-%m-%d")
    except Exception:
        end_day = datetime.now().strftime("%Y-%m-%d")
        start_day = "2000-01-01"

    db_path = (args.db or "").strip()
    if not db_path:
        _ = _load_settings(root)
        from data_manager_v2 import DataManagerV2

        db_path = DataManagerV2.DEFAULT_DB_PATH
    db_path = os.path.abspath(db_path)
    print(f"DB: {db_path}")
    print(f"비교 구간: {start_day} ~ {end_day} (캘린더 약 {args.days}일)")

    if not os.path.isfile(db_path):
        print(f"[Err] DB 파일이 없습니다: {db_path}", file=sys.stderr)
        return 2

    use_api = not args.no_api
    token = (args.token or "").strip()
    rc = 0
    for raw in args.codes:
        try:
            rc |= _run_one_code(
                str(raw).strip(),
                db_path=db_path,
                end_day=end_day,
                start_day=start_day,
                token=token,
                price_eps=float(args.price_eps),
                vol_eps=float(args.vol_eps),
                max_print=int(args.max_print),
                use_api=use_api,
            )
        except Exception as e:
            print(f"[Err] {raw}: {e}", file=sys.stderr)
            rc |= 1
    return min(rc, 255)


if __name__ == "__main__":
    raise SystemExit(main())
