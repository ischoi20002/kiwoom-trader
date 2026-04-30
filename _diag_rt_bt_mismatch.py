# -*- coding: utf-8 -*-
"""
2026-04-30 실시간 매수 vs 백테 신호 불일치 진단 (로컬 trading_data.db).
실행: 프로젝트 루트에서  python _diag_rt_bt_mismatch.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

# 프로젝트 루트
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

from backtest_engine import TradingStrategy  # noqa: E402
from data_manager_v2 import DataManagerV2  # noqa: E402
import strategy_selector  # noqa: E402

TARGET_DAY = "2026-04-30"
CODES = ["352820", "001430", "051900", "259960"]
# 실시간 체결(세션 로그) 시각 근처 15분 봉 끝시각 후보
REALTIME_HINTS: Dict[str, List[str]] = {
    "352820": ["2026-04-30 09:15:00"],  # 09:03 장중 → 해당 슬롯 09:15 라벨
    "001430": ["2026-04-30 09:45:00"],
    "051900": ["2026-04-30 10:45:00"],
    "259960": ["2026-04-30 10:45:00", "2026-04-30 11:00:00"],
}


def _load_settings() -> dict[str, Any]:
    p = os.path.join(_ROOT, "settings.json")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _noop_log(msg: str) -> None:
    pass


def _ref_day_for_target(dm: DataManagerV2, target: str, range_start: str) -> Tuple[str, List[str]]:
    tdays = dm.get_trading_days(range_start, target)
    if not tdays:
        return "", tdays
    norm = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in tdays]
    t = pd.Timestamp(target).strftime("%Y-%m-%d")
    if t not in norm:
        return "", norm
    i = norm.index(t)
    if i == 0:
        return t, norm
    return norm[i - 1], norm


def _load_15m_group(
    dm: DataManagerV2, code: str, fetch_start: str, fetch_end: str
) -> Optional[pd.DataFrame]:
    q = """
        SELECT code, datetime as time, open, high, low, close, volume
        FROM bars_15m
        WHERE code = ? AND datetime BETWEEN ? AND ?
        ORDER BY datetime
    """
    try:
        with dm._get_connection() as conn:
            df = pd.read_sql_query(q, conn, params=[code, fetch_start, fetch_end + " 23:59:59"])
    except Exception as e:
        print(f"  [SQL 오류] {code}: {e}")
        return None
    if df is None or df.empty:
        return None
    df.columns = [c.capitalize() if c.lower() != "time" else "time" for c in df.columns]
    df["time"] = pd.to_datetime(df["time"])
    df = df.rename(columns={"time": "Datetime"})
    df = df.drop(columns=["Code"], errors="ignore")
    df = df.set_index("Datetime").sort_index()
    return df


def _dynamic_style_signals(
    group: pd.DataFrame, settings: dict[str, Any]
) -> pd.DataFrame:
    """dynamic_backtest._prepare_15m_cached_data 와 동일한 전처리·신호 파이프."""
    g = group.copy()
    if settings.get("use_synthetic_realtime", True):
        import numpy as np
        from strategy.synthetic_realtime_fill import (
            synthesize_bid_ask_ratio_vectorized,
            synthesize_vol_power_vectorized,
        )

        for col in ["Vol_power", "Total_ask", "Total_bid"]:
            if col in g.columns:
                g[col] = pd.to_numeric(g[col], errors="coerce")
        p_changes = g["Close"].pct_change().fillna(0).values
        vol_mas = g["Volume"].rolling(20).mean().bfill().values
        vol_ratios = (g["Volume"] / (vol_mas + 1e-10)).values
        vps = g["Vol_power"].values if "Vol_power" in g.columns else np.full(len(g), np.nan)
        tasks = g["Total_ask"].values if "Total_ask" in g.columns else np.full(len(g), np.nan)
        tbids = g["Total_bid"].values if "Total_bid" in g.columns else np.full(len(g), np.nan)
        nan_vp = np.isnan(vps)
        if np.any(nan_vp):
            vps[nan_vp] = synthesize_vol_power_vectorized(
                p_changes[nan_vp], vol_ratios[nan_vp]
            )
        nan_ba = np.isnan(tasks) | np.isnan(tbids)
        if np.any(nan_ba):
            r_vector = synthesize_bid_ask_ratio_vectorized(
                p_changes[nan_ba], vol_ratios[nan_ba], vps[nan_ba]
            )
            tasks = tasks.copy()
            tbids = tbids.copy()
            tasks[nan_ba] = 100000
            tbids[nan_ba] = (100000 * r_vector).astype(int)
        g = g.copy()
        g["Vol_power"] = vps
        g["Total_ask"] = tasks
        g["Total_bid"] = tbids
        g["bid_ask_ratio"] = g["Total_bid"] / (g["Total_ask"] + 1e-10)
        g["vol_power"] = vps

    df_ind = TradingStrategy.calculate_indicators(g, settings)
    is_ma_aligned = df_ind["MA5"] > df_ind["MA20"]
    ma_group_id = (~is_ma_aligned).cumsum()
    df_ind["MA_Maint_Count"] = is_ma_aligned.groupby(ma_group_id).cumsum()
    is_rsi_above_50 = df_ind["RSI"] >= 50.0
    rsi_group_id = (df_ind["RSI"] < 50.0).cumsum()
    df_ind["RSI_Pulse_Count"] = is_rsi_above_50.groupby(rsi_group_id).cumsum()

    full_sigs = TradingStrategy.get_buy_signals_detailed(df_ind, settings, market_trend="UP")
    df_ind = df_ind.join(full_sigs.drop(columns="Signal", errors="ignore"))
    df_ind["Signal"] = full_sigs["Signal"]
    df_ind["Ready"] = df_ind.reset_index().index >= 19
    return df_ind


def main() -> None:
    settings = _load_settings()
    dm = DataManagerV2(os.path.join(_ROOT, "trading_data.db"))

    bt_start = "2026-04-01"
    bt_end = TARGET_DAY

    print("=== 1) 동적 유니버스: TARGET 영업일 직전 거래일(ref_day) 스케줄에 4종목 포함 여부 ===")
    ref_day, tdays = _ref_day_for_target(dm, TARGET_DAY, bt_start)
    print(f"  trading_days[{bt_start}~{bt_end}] 마지막 포함 TARGET={TARGET_DAY}, ref_day={ref_day}")

    sch_a: Dict[str, List[str]] = {}
    sch_b: Dict[str, List[str]] = {}
    sch_union: Dict[str, List[str]] = {}
    try:
        sch_union, sch_a, sch_b = strategy_selector.prepare_dynamic_universe(
            settings, bt_start, bt_end, dm, _noop_log
        )
    except Exception as e:
        print(f"  prepare_dynamic_universe 실패: {e}")
        sch_union, sch_a, sch_b = {}, {}, {}

    all_sched_codes: set[str] = set()
    for _v in sch_union.values():
        all_sched_codes.update(_v)
    print(f"  기간 내 schedule 합집합 종목 수: {len(all_sched_codes)}")
    for c in CODES:
        print(f"    {c} any_day={c in all_sched_codes}")

    if ref_day:
        ra = sch_a.get(ref_day, [])
        rb = sch_b.get(ref_day, [])
        print(f"  schedule_a[{ref_day}] 종목 수: {len(ra)}")
        print(f"  schedule_b[{ref_day}] 종목 수: {len(rb)}")
        for c in CODES:
            in_a = c in ra
            in_b = c in rb
            tag = []
            if in_a:
                tag.append("A")
            if in_b:
                tag.append("B")
            print(f"    {c}: 트랙 {('/'.join(tag) if tag else '없음')}")

    print("\n=== 2) bars_15m 행 수 (넓은 구간 vs 동적과 동일 21일 패드) ===")
    narrow_pad = (
        datetime.strptime(bt_start, "%Y-%m-%d") - timedelta(days=21)
    ).strftime("%Y-%m-%d")
    wide_pad = (
        datetime.strptime(TARGET_DAY, "%Y-%m-%d") - timedelta(days=120)
    ).strftime("%Y-%m-%d")

    for code in CODES:
        df_w = _load_15m_group(dm, code, wide_pad, TARGET_DAY)
        df_n = _load_15m_group(dm, code, narrow_pad, TARGET_DAY)
        n_w = len(df_w) if df_w is not None else 0
        n_n = len(df_n) if df_n is not None else 0
        sub_w = (
            len(df_w.loc[TARGET_DAY : TARGET_DAY + " 23:59:59"]) if df_w is not None else 0
        )
        print(f"  {code}: wide_rows={n_w}, narrow_pad_rows={n_n}, TARGET_DAY_bar수={sub_w}")

    print("\n=== 3) 신호 재현 (wide 패드 = 동적 신호 안정화에 유리) ===")
    for code in CODES:
        df_w = _load_15m_group(dm, code, wide_pad, TARGET_DAY)
        if df_w is None or df_w.empty:
            print(f"  {code}: 데이터 없음")
            continue
        try:
            df_ind = _dynamic_style_signals(df_w, settings)
        except Exception as e:
            print(f"  {code}: 지표/신호 계산 예외: {e}")
            continue
        day = df_ind.loc[TARGET_DAY : TARGET_DAY + " 23:59:59"].copy()
        fired = day[day["Signal"] == True]  # noqa: E712
        fired_ready = day[(day["Signal"] == True) & (day["Ready"] == True)]  # noqa: E712

        print(f"\n  [{code}] TARGET_DAY Signal=True 행 수: {len(fired)} / Ready까지: {len(fired_ready)}")
        if not fired.empty:
            cols = [
                "Open",
                "High",
                "Low",
                "Close",
                "Volume",
                "RSI",
                "MACD",
                "MACD_Sig",
                "OSC",
                "Sig_Type",
                "Signal",
                "Ready",
            ]
            cols = [c for c in cols if c in fired.columns]
            print(fired[cols].to_string())

        # 실시간 체결 시각과 가장 가까운 봉
        for hint in REALTIME_HINTS.get(code, []):
            ts = pd.Timestamp(hint)
            if ts in df_ind.index:
                row = df_ind.loc[ts]
                print(
                    f"    행 {hint}: Signal={bool(row.get('Signal'))}, Ready={bool(row.get('Ready'))}, "
                    f"Sig_Type={row.get('Sig_Type')}, RSI={float(row.get('RSI', 0)):.2f}, "
                    f"MACD={float(row.get('MACD', 0)):.2f}, OSC={float(row.get('OSC', 0)):.2f}"
                )
            else:
                # 근접 인덱스
                ix = df_ind.index.asof(ts)
                print(f"    행 {hint}: 인덱스 미존재 (asof={ix})")

    print("\n=== 4) 좁은 패드(동적 백테 기본 pad_start=시작일-21일) 기준 신호 (비교용) ===")
    for code in CODES:
        df_n = _load_15m_group(dm, code, narrow_pad, TARGET_DAY)
        if df_n is None or df_n.empty:
            print(f"  {code}: narrow 데이터 없음")
            continue
        try:
            df_ind = _dynamic_style_signals(df_n, settings)
        except Exception as e:
            print(f"  {code}: narrow 계산 예외: {e}")
            continue
        day = df_ind.loc[TARGET_DAY : TARGET_DAY + " 23:59:59"]
        n_sig = int((day["Signal"] == True).sum())  # noqa: E712
        print(f"  {code}: TARGET_DAY Signal=True 개수 = {n_sig}")

    print("\n완료.")


if __name__ == "__main__":
    main()
