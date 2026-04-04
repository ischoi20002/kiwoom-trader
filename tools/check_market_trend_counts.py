# -*- coding: utf-8 -*-
"""
일별 m_trend(UP/DOWN/NEUTRAL/AGGRESSIVE) 집계 — dynamic_backtest와 동일 규칙·전일 지표 참조.

  python tools/check_market_trend_counts.py 2024-01-01 2024-12-31
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backtest_engine import TradingStrategy  # noqa: E402
from data_manager_v2 import DataManagerV2  # noqa: E402


def run_period(start_date: str, end_date: str) -> tuple[dict[str, int], list[str], int]:
    os.chdir(_ROOT)
    with open("settings.json", "r", encoding="utf-8") as f:
        settings = json.load(f)

    s = settings
    msi_down_thr = float(s.get("msi_down_threshold", -0.10))
    msi_up_thr = float(s.get("msi_up_threshold", 0.10))
    adx_min_thr = float(s.get("adx_min_threshold", 15))
    adx_down_thr = float(s.get("adx_down_threshold", 20))
    vix_down_thr = float(s.get("vix_down_threshold", 30))

    warmup_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=280)).strftime(
        "%Y-%m-%d"
    )
    db = DataManagerV2("trading_data.db")
    market_df_raw = db.get_daily_bars("001", warmup_start, end_date)
    vix_df = db.get_daily_bars("^VIX", warmup_start, end_date)

    if market_df_raw is None or market_df_raw.empty:
        raise RuntimeError("시장(001) 데이터 없음")

    market_df_raw = market_df_raw.copy()
    market_df_raw["time"] = pd.to_datetime(market_df_raw["time"])
    market_df_raw.set_index("time", inplace=True)
    market_df_full = TradingStrategy.calculate_indicators(market_df_raw, settings)
    market_df = market_df_full.loc[start_date:end_date]
    if market_df.empty:
        raise RuntimeError("기간 슬라이스 비어 있음")

    trading_days = market_df.index.strftime("%Y-%m-%d").tolist()
    vix_series: pd.Series | None = None
    if vix_df is not None and not vix_df.empty:
        try:
            _vix = vix_df.copy()
            if "time" in _vix.columns:
                _vix["time"] = pd.to_datetime(_vix["time"])
                _vix = _vix.set_index("time")
            _vix.index = pd.to_datetime(_vix.index)
            td_idx = pd.to_datetime(trading_days)
            vix_series = _vix["Close"].reindex(td_idx, method="ffill")
        except Exception:
            vix_series = None

    def classify(prev_day: str) -> str:
        m_trend = "UP"
        try:
            m_data = market_df.loc[prev_day]
            msi = float(m_data.get("MSI", 0) or 0)
            adx = float(m_data.get("ADX", 20) or 20)
            v_val = 15.0
            if vix_series is not None:
                try:
                    looked_up = vix_series.get(pd.to_datetime(prev_day))
                    if looked_up is not None and not pd.isna(looked_up):
                        v_val = float(looked_up)
                except Exception:
                    pass
            if (
                msi < msi_down_thr
                or v_val > vix_down_thr
                or (msi < 0 and adx > adx_down_thr)
            ):
                m_trend = "DOWN"
            elif adx < adx_min_thr and msi <= msi_up_thr:
                m_trend = "NEUTRAL"
            elif msi > msi_up_thr and adx > adx_down_thr:
                m_trend = "AGGRESSIVE"
            else:
                m_trend = "UP"
        except Exception:
            m_trend = "UP"
        return m_trend

    neutral_days: list[str] = []
    counts: dict[str, int] = {"UP": 0, "DOWN": 0, "NEUTRAL": 0, "AGGRESSIVE": 0}
    for idx, current_day in enumerate(trading_days):
        prev_day_idx = idx - 1
        if prev_day_idx < 0:
            mt = "UP"
        else:
            mt = classify(trading_days[prev_day_idx])
        counts[mt] = counts.get(mt, 0) + 1
        if mt == "NEUTRAL":
            neutral_days.append(current_day)

    return counts, neutral_days, len(trading_days)


def main() -> None:
    p = argparse.ArgumentParser(description="m_trend 일별 집계 (dynamic_backtest 동일)")
    p.add_argument("start_date", help="YYYY-MM-DD")
    p.add_argument("end_date", help="YYYY-MM-DD")
    p.add_argument("--list-neutral", action="store_true", help="NEUTRAL인 당일 날짜 나열")
    args = p.parse_args()

    try:
        counts, neutral_days, n_days = run_period(args.start_date, args.end_date)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print(f"기간: {args.start_date} ~ {args.end_date}")
    print(f"영업일: {n_days}")
    print(f"m_trend: {counts}")
    print(f"NEUTRAL 일수: {len(neutral_days)}")
    if args.list_neutral and neutral_days:
        for d in neutral_days:
            print(d)


if __name__ == "__main__":
    main()
