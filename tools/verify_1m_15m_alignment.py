# -*- coding: utf-8 -*-
"""1분/15분봉 정합: bars_15m vs KRX 1m→15m(utils.krx_bars) vs 1m 인트라바 창(09:15=T-15, 그 외 T-14)."""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta
from typing import Any, Optional, Tuple

import pandas as pd

# 프로젝트 루트
ABS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ABS not in sys.path:
    sys.path.insert(0, ABS)
from data_manager_v2 import DataManagerV2  # noqa: E402
from utils.krx_bars import m1_window_start_for_15m_end, resample_1m_ohlcv_to_15m_krx
from utils.normalization import normalize_code  # noqa: E402


def _ts(d: str, hm: str) -> pd.Timestamp:
    return pd.Timestamp(f"{d} {hm}")


def verify_one_code_one_day(
    db_path: str, code: str, day: str, verbose: bool = True
) -> Tuple[
    int,
    int,
    int,
    list[Tuple[pd.Timestamp, int, int]],
    int,
    Optional[dict[str, Any]],
]:
    """
    Returns: (n_same, n_higher, n_lower, higher_examples, n_db_vs_resample_mismatch, slot_945)
    - higher: T-14~T 1분 min > 15분 Low (1분이 더 높음 = 창이 저점 분을 누락)
    - lower: T-14~T 1분 min < 15분 Low
    - slot_945: 09:45 행이 있을 때 9:30~9:45, 9:31~9:45, 15m 비교 dict
    """
    code = normalize_code(code)
    t_start = f"{day} 00:00:00"
    t_end = f"{day} 23:59:59"
    with sqlite3.connect(db_path, timeout=120) as conn:
        df1 = pd.read_sql_query(
            "SELECT datetime, open, high, low, close, volume FROM bars_1m "
            "WHERE code = ? AND datetime >= ? AND datetime <= ?",
            conn,
            params=[code, t_start, t_end],
        )
        df15_db = pd.read_sql_query(
            "SELECT datetime, open, high, low, close, volume FROM bars_15m "
            "WHERE code = ? AND datetime >= ? AND datetime <= ?",
            conn,
            params=[code, t_start, t_end],
        )
    if df1.empty or df15_db.empty:
        return 0, 0, 0, [], 0, None
    df1["datetime"] = pd.to_datetime(df1["datetime"], errors="coerce")
    df1 = df1.dropna(subset=["datetime"])
    for c in ("low", "open", "high", "close", "volume"):
        df1[c] = pd.to_numeric(df1[c], errors="coerce")
    df1 = df1.set_index("datetime").sort_index()

    # bar_resample: KRX 1m→15m (r/r + 09:00~09:15 병합)
    df15_k = resample_1m_ohlcv_to_15m_krx(df1, code)
    if df15_k.empty:
        krx_by_ts: dict[pd.Timestamp, int] = {}
    else:
        tcol = pd.to_datetime(df15_k["datetime"], errors="coerce")
        krx_by_ts = {pd.Timestamp(t): int(v) for t, v in zip(tcol, df15_k["low"].values) if pd.notna(t)}

    df15_idx = df15_db.set_index(pd.to_datetime(df15_db["datetime"])).sort_index()
    mismatch_db_vs_rs: list[Tuple[pd.Timestamp, int, int]] = []
    for t, row in df15_idx.iterrows():
        t = pd.Timestamp(t)
        a = int(row["low"])
        b = krx_by_ts.get(t) or krx_by_ts.get(t.floor("s")) or krx_by_ts.get(
            t.replace(microsecond=0, nanosecond=0)
        )
        if b is not None and a != b:
            mismatch_db_vs_rs.append((t, a, b))

    same, higher, lower = 0, 0, 0
    higher_list: list[Tuple[pd.Timestamp, int, int]] = []
    for t in df15_idx.index:
        t = pd.Timestamp(t)
        t0 = m1_window_start_for_15m_end(t)
        m1w = df1[(df1.index >= t0) & (df1.index <= t)]
        if m1w.empty:
            continue
        cmin = int(m1w["low"].min())
        low15 = int(df15_idx.loc[t, "low"])
        if cmin == low15:
            same += 1
        elif cmin > low15:
            higher += 1
            higher_list.append((t, low15, cmin))
        else:
            lower += 1

    slot_945: Optional[dict[str, Any]] = None
    k = _ts(day, "09:45:00")
    if k in df15_idx.index:
        t0k = m1_window_start_for_15m_end(k)
        t_chunk2 = df1[(df1.index >= t0k) & (df1.index <= k)]
        _lk = krx_by_ts.get(k) or krx_by_ts.get(k.replace(microsecond=0, nanosecond=0))
        slot_945 = {
            "day": day,
            "15m_db": int(df15_idx.loc[k, "low"]),
            "15m_krx_from_1m": _lk,
            "1m_m1_start_to_0945": int(t_chunk2["low"].min()) if not t_chunk2.empty else None,
            "n_1m_chunk": len(t_chunk2),
            "m1_t0": str(t0k),
        }

    if verbose and mismatch_db_vs_rs:
        print("  [DB 15m vs 1m→15m resample Low 불일치]", len(mismatch_db_vs_rs), "건, 예시(최대5):")
        for m in mismatch_db_vs_rs[:5]:
            print("   ", m)
    if verbose and higher_list and len(higher_list) <= 10:
        print("  [KRX 1m창] min > 15m DB Low (불가—갱신 필요):")
        for m in higher_list:
            print("   ", m)
    elif verbose and higher_list:
        print("  [KRX 1m창] min > 15m DB Low:", len(higher_list), "건 (일부):")
        for m in higher_list[:8]:
            print("   ", m)

    return same, higher, lower, higher_list, len(mismatch_db_vs_rs), slot_945


def main() -> int:
    dm = DataManagerV2()
    db_path = str(dm.db_path)
    code = "011170"
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            d = conn.execute("SELECT MAX(datetime) FROM bars_1m WHERE code = ?", (normalize_code(code),)).fetchone()[0]
    except (sqlite3.Error, OSError) as e:
        print(f"DB 읽기 실패: {e}")
        return 1
    if d is None:
        print("bars_1m에 해당 종목이 없습니다.")
        return 1
    day = d[:10]
    print("DB:", db_path)
    print(f"종목: {code}  기준일(말일 1m): {day}  (last_1m={d})")
    a, b, c, hlist, mcnt, s945 = verify_one_code_one_day(db_path, code, day, verbose=True)
    print("---")
    print(f"15m 행별 KRX 1m창(09:15=T-15, 이후 T-14) Low min vs 15m DB Low: 같음={a}, 1m>15m={b}, 1m<15m={c}")
    print(f"bars_15m Low vs KRX 1m→15m(resample+09:0병합) Low 불일치: {mcnt}건 (0이면 DB=엔진일치; 갱신 후 0 권장)")

    if s945 is not None:
        print("--- 09:45 봉(해당일) ---")
        for k_, v_ in s945.items():
            print(f"  {k_}: {v_}")
        cmin = s945.get("1m_m1_start_to_0945")
        l15 = s945.get("15m_db")
        krl = s945.get("15m_krx_from_1m")
        if cmin is not None and l15 is not None and cmin == l15 and krl == l15:
            print("  [OK] 1m창(KRX) == 15m DB; bars_15m 는 refresh_resampled_data 갱신 기준과 정합")
        elif cmin is not None and l15 is not None and cmin != l15:
            print(
                f"  [권장] 15m DB 를 갱신: cmin(1m) {cmin} vs 15m {l15} — `DataManagerV2.refresh_resampled_data({code!r})`"
            )

    # 전체 랜덤/최근 며칠: 불일치 건수 훑기
    print("---")
    print("최근 5거래일(1m이 있는 날) 동일 점검(요약):")
    with sqlite3.connect(db_path, timeout=60) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT date(datetime) AS d FROM bars_1m
            WHERE code = ? AND date(datetime) <= date(?)
            ORDER BY d DESC
            LIMIT 5
            """,
            (normalize_code(code), day + " 23:59:59"),
        ).fetchall()
    days_ = [r[0] for r in rows]
    for d_ in days_:
        sa, hb, lc, _hl, mc, _s = verify_one_code_one_day(db_path, code, d_, verbose=False)
        if hb > 0 or lc > 0 or mc > 0:
            print(f"  {d_}: same={sa} high={hb} low={lc} db_vs_resample_mismatch={mc}")
        else:
            print(f"  {d_}: 전 15m행 1m창(KRX)==DB, DB=1m KRX 집계와 일치")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"검증 실패: {e!r}", file=sys.stderr)
        raise
