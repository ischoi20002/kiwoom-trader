# -*- coding: utf-8 -*-
"""
KOSPI200 + 현재 설정 기준 5차 리스크/포지션 관리 미세튜닝.

축:
- DD sizing (trigger / scale / slot scale)
- ATR trail B (Type B 추세 추적 폭)

  python tools/compare_k200_risk_tuning.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import importlib
import backtest_engine as be
import dynamic_backtest as db

importlib.reload(be)
importlib.reload(db)

from backtest_engine import settings_for_backtest  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

OUT_DIR = ROOT / "tools" / "bt_compare_out_k200_risk_tuning"
OOS_START = "2025-07-01"

EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "id": "BASE",
        "title": "현재 설정 (DD15×0.5, trail B6)",
        "overrides": {},
    },
    {
        "id": "DD_OFF",
        "title": "DD sizing OFF",
        "overrides": {"use_dd_sizing": False},
    },
    {
        "id": "DD10_S50",
        "title": "DD≥10% → 금액×0.5",
        "overrides": {
            "use_dd_sizing": True,
            "dd_sizing_trigger_pct": 10.0,
            "dd_sizing_scale": 0.5,
            "dd_slots_scale": 1.0,
        },
    },
    {
        "id": "DD10_S70",
        "title": "DD≥10% → 금액×0.7",
        "overrides": {
            "use_dd_sizing": True,
            "dd_sizing_trigger_pct": 10.0,
            "dd_sizing_scale": 0.7,
            "dd_slots_scale": 1.0,
        },
    },
    {
        "id": "DD15_S70",
        "title": "DD≥15% → 금액×0.7",
        "overrides": {
            "use_dd_sizing": True,
            "dd_sizing_trigger_pct": 15.0,
            "dd_sizing_scale": 0.7,
            "dd_slots_scale": 1.0,
        },
    },
    {
        "id": "DD10_SLOT",
        "title": "DD≥10% → 금액×0.5 + 슬롯×0.6",
        "overrides": {
            "use_dd_sizing": True,
            "dd_sizing_trigger_pct": 10.0,
            "dd_sizing_scale": 0.5,
            "dd_slots_scale": 0.6,
        },
    },
    {
        "id": "TRAIL_B5",
        "title": "ATR trail B 5.0 (현재 6.0)",
        "overrides": {"atr_multiplier_trail_b": 5.0},
    },
    {
        "id": "TRAIL_B7",
        "title": "ATR trail B 7.0",
        "overrides": {"atr_multiplier_trail_b": 7.0},
    },
]


def _load_base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["universe_index_choice"] = "KOSPI200"
    st["kospi_top_n"] = 200
    st["bt_high_precision"] = False
    st["bt_rt_parity_intrabar"] = False
    st["enforce_track_signal_match"] = True
    return st


def _equity_at(daily: pd.DataFrame, col: str, day: str, initial: float) -> float:
    sub = daily[daily["d"] <= pd.Timestamp(day)]
    if sub.empty:
        return initial
    return float(sub.iloc[-1][col])


def _equity_metrics(daily_equity: List[dict], initial: float) -> Dict[str, Any]:
    df = pd.DataFrame(daily_equity)
    if df.empty:
        return {}
    df["date"] = pd.to_datetime(df["date"], format="mixed", errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")
    df["d"] = df["date"].dt.normalize()
    daily = df.groupby("d", as_index=False).last()
    col = "equity" if "equity" in daily.columns else "Total"
    e0 = float(initial)
    e_end = float(daily.iloc[-1][col])
    dd = (daily[col] - daily[col].cummax()) / daily[col].cummax()
    return {
        "ret_total_pct": (e_end / e0 - 1) * 100,
        "end_equity": e_end,
        "profit_krw": e_end - e0,
        "max_dd_pct": float(dd.min() * 100),
    }


def _trade_metrics(trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if trades is None or trades.empty:
        return {"n_buy1": 0, "n_buy2": 0, "win_rate": 0.0}
    buys1 = trades[trades["Action"] == "BUY1"]
    buys2 = trades[trades["Action"] == "BUY2"]
    sells = trades[trades["Action"] == "SELL"]
    return {
        "n_buy1": int(len(buys1)),
        "n_buy2": int(len(buys2)),
        "win_rate": float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0,
    }


def run_one(exp: Dict[str, Any], base: Dict[str, Any], start: str, end: str) -> Dict[str, Any]:
    period = "full" if start == str(base.get("bt_start_date")) else "oos"
    run_id = f"{exp['id']}_{period}"
    dest = OUT_DIR / run_id
    meta_path = dest / "meta.json"
    if meta_path.is_file():
        try:
            cached = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            if cached.get("start") == start and cached.get("end") == end:
                print(f"[{run_id}] 캐시 재사용")
                return cached
        except Exception:
            pass

    print(f"\n[{run_id}] {exp['title']}  {start} ~ {end}")
    st = deepcopy(base)
    st.update(exp["overrides"])

    def cb(msg: str, val: Any = None) -> None:
        try:
            if str(msg).startswith("[") and "시계열" in str(msg) and val is not None:
                print(f"\r  {float(val):5.1f}%", end="", flush=True)
        except Exception:
            pass

    t0 = time.time()
    try:
        tester = DynamicBacktester(st)
        trades = tester.run(start, end, interval="15m", callback=cb, high_precision=False)
    except Exception:
        traceback.print_exc()
        return {"id": exp["id"], "period": period, "error": True}
    elapsed = time.time() - t0
    print(f"\n  done {elapsed:.1f}s")

    dest.mkdir(parents=True, exist_ok=True)
    if isinstance(trades, pd.DataFrame) and not trades.empty:
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(tester.daily_equity).to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")

    row: Dict[str, Any] = {
        "id": exp["id"],
        "title": exp["title"],
        "period": period,
        "start": start,
        "end": end,
        "elapsed_sec": round(elapsed, 1),
        **_equity_metrics(tester.daily_equity, tester.initial_cash),
        **_trade_metrics(trades if isinstance(trades, pd.DataFrame) else None),
    }
    meta_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"  total={row.get('ret_total_pct', float('nan')):+.2f}% "
        f"DD={row.get('max_dd_pct', float('nan')):.1f}% "
        f"BUY1={row.get('n_buy1', 0)}"
    )
    return row


def _print_summary(rows: List[Dict[str, Any]]) -> None:
    for period in ("full", "oos"):
        sub = [r for r in rows if r.get("period") == period and "ret_total_pct" in r]
        if not sub:
            continue
        base = next((r for r in sub if r.get("id") == "BASE"), None)
        print("\n" + "=" * 92)
        print(f"K200 RISK TUNING {period.upper()}")
        print("=" * 92)
        print(
            f"  {'ID':14s} {'수익%':>9s} {'ΔBASE':>8s} {'만원':>8s} "
            f"{'MDD%':>8s} {'BUY1':>6s} {'승률%':>7s}"
        )
        for r in sorted(sub, key=lambda x: float(x.get("ret_total_pct", -999)), reverse=True):
            delta = 0.0
            if base is not None:
                delta = float(r.get("ret_total_pct", 0)) - float(base.get("ret_total_pct", 0))
            print(
                f"  {str(r.get('id')):14s} "
                f"{float(r.get('ret_total_pct', 0)):+9.2f} "
                f"{delta:+8.2f} "
                f"{float(r.get('profit_krw', 0))/1e4:+8.0f} "
                f"{float(r.get('max_dd_pct', 0)):8.1f} "
                f"{int(r.get('n_buy1', 0)):6d} "
                f"{float(r.get('win_rate', 0)):7.1f}"
            )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    full_start = str(base.get("bt_start_date", "2024-06-03"))
    full_end = str(base.get("bt_end_date", "2026-07-27"))

    rows: List[Dict[str, Any]] = []
    for exp in EXPERIMENTS:
        rows.append(run_one(exp, base, full_start, full_end))
    for exp in EXPERIMENTS:
        rows.append(run_one(exp, base, OOS_START, full_end))

    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_summary(rows)
    print(f"\n저장: {summary_path}")


if __name__ == "__main__":
    main()
