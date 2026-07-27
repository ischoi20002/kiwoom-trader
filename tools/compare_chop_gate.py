# -*- coding: utf-8 -*-
"""
Chop 게이트 vs Baseline vs Breadth 게이트 백테스트 비교.

Chop: valid_a 과밀 + valid_b 고갈 시 A 슬롯만 축소 (B 유지).

  python tools/compare_chop_gate.py
  python tools/compare_chop_gate.py --only BASE,CHOP8
"""
from __future__ import annotations

import argparse
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

START = "2024-06-03"
END = "2026-07-24"
OUT_DIR = ROOT / "tools" / "bt_compare_out_chop_gate"
HIGH_PRECISION = False

EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "id": "BASE",
        "title": "Baseline (게이트 없음)",
        "overrides": {
            "use_market_breadth_gate": False,
            "use_chop_gate": False,
        },
    },
    {
        "id": "CHOP8",
        "title": "Chop gate: A슬롯→8 (a_min=15)",
        "overrides": {
            "use_market_breadth_gate": False,
            "use_chop_gate": True,
            "chop_slots_a": 8,
            "chop_valid_a_min": 15,
            "chop_valid_b_max": 3,
            "chop_valid_b_share_max_pct": 15.0,
            "chop_valid_a_floor": 10,
        },
    },
    {
        "id": "CHOP8_A18",
        "title": "Chop gate: A슬롯→8 (a_min=18)",
        "overrides": {
            "use_market_breadth_gate": False,
            "use_chop_gate": True,
            "chop_slots_a": 8,
            "chop_valid_a_min": 18,
            "chop_valid_b_max": 3,
            "chop_valid_b_share_max_pct": 15.0,
            "chop_valid_a_floor": 18,
        },
    },
    {
        "id": "CHOP8_A20",
        "title": "Chop gate: A슬롯→8 (a_min=20)",
        "overrides": {
            "use_market_breadth_gate": False,
            "use_chop_gate": True,
            "chop_slots_a": 8,
            "chop_valid_a_min": 20,
            "chop_valid_b_max": 3,
            "chop_valid_b_share_max_pct": 15.0,
            "chop_valid_a_floor": 20,
        },
    },
    {
        "id": "STRESS_TIER",
        "title": "Stress gate: SOFT 4/4, HARD 신규차단",
        "overrides": {
            "use_market_breadth_gate": False,
            "use_chop_gate": False,
            "use_transition_gate": False,
            "use_stress_gate": True,
            "stress_soft_slots_a": 4,
            "stress_soft_slots_b": 4,
            "stress_rvol_soft": 35.0,
            "stress_rvol5_soft": 42.0,
            "stress_rvol_hard": 50.0,
            "stress_valid_a_hard": 12,
            "stress_valid_b_share_hard": 15.0,
            "stress_xs_ret_std_hard": 4.0,
        },
    },
    {
        "id": "TRANSITION",
        "title": "Transition: B-share 5d↓+rvol, S1차단+S2중단",
        "overrides": {
            "use_market_breadth_gate": False,
            "use_chop_gate": False,
            "use_stress_gate": False,
            "use_transition_gate": True,
            "transition_share_delta5_max": -15.0,
            "transition_share_delta3_max": -10.0,
            "transition_prior_share_min": 45.0,
            "transition_rvol_min": 35.0,
            "transition_xs_ret_std_min": 3.5,
            "chop_valid_a_min": 15,
            "chop_valid_b_max": 3,
            "chop_valid_b_share_max_pct": 15.0,
            "chop_valid_a_floor": 10,
        },
    },
]


def _load_base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["use_market_breadth_gate"] = False
    st["use_chop_gate"] = False
    st["use_type_a"] = True
    st["use_type_b"] = True
    st["use_sel_track_a"] = True
    st["use_sel_track_b"] = True
    st["bt_high_precision"] = False
    st["bt_rt_parity_intrabar"] = False
    st["enforce_track_signal_match"] = True
    st["use_s2_master_a"] = True
    st["use_s2_master_b"] = True
    return st


def _exit_tag(reason: str) -> str:
    r = str(reason or "").split("|", 1)[0].strip().upper()
    for key in ("TAKE_PROFIT", "ATR_SL", "ATR_TRAIL", "FLASH_EXIT", "GAP_DOWN"):
        if key in r:
            return key
    return r[:20] if r else "?"


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
    e24 = _equity_at(daily, col, "2024-12-31", e0)
    e25 = _equity_at(daily, col, "2025-12-31", e0)
    e_end = float(daily.iloc[-1][col])
    dd = (daily[col] - daily[col].cummax()) / daily[col].cummax()
    out: Dict[str, Any] = {
        "ret_2024_pct": (e24 / e0 - 1) * 100,
        "ret_2025_pct": (e25 / e24 - 1) * 100 if e24 else 0.0,
        "ret_2026_ytd_pct": (e_end / e25 - 1) * 100 if e25 else 0.0,
        "ret_total_pct": (e_end / e0 - 1) * 100,
        "end_equity": e_end,
        "max_dd_pct": float(dd.min() * 100),
    }
    for ym_prefix, label in [("2026-05", "ret_2026_05"), ("2026-06", "ret_2026_06"), ("2026-07", "ret_2026_07")]:
        sub = daily[daily["d"].dt.strftime("%Y-%m").eq(ym_prefix)]
        if len(sub) >= 2:
            out[label] = (float(sub.iloc[-1][col]) / float(sub.iloc[0][col]) - 1) * 100
        elif len(sub) == 1:
            out[label] = 0.0
        else:
            out[label] = None
    return out


def _trade_metrics(trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if trades is None or trades.empty:
        return {"n_sell": 0, "n_buy1": 0, "n_buy2": 0}
    df = trades.copy()
    buys1 = df[df["Action"] == "BUY1"]
    buys2 = df[df["Action"] == "BUY2"]
    sells = df[df["Action"] == "SELL"].copy()
    sells["Date"] = pd.to_datetime(sells["Date"], errors="coerce")
    sells["year"] = sells["Date"].dt.year
    sells["ym"] = sells["Date"].dt.to_period("M").astype(str)
    sells["tag"] = sells["Reason"].map(_exit_tag)
    out: Dict[str, Any] = {
        "n_buy1": int(len(buys1)),
        "n_buy2": int(len(buys2)),
        "n_sell": int(len(sells)),
        "win_rate": float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0,
        "sl_all": int((sells["tag"] == "ATR_SL").sum()),
    }
    for y in (2024, 2025, 2026):
        s = sells[sells["year"] == y]
        out[f"pnl_{y}"] = float(s["Return"].sum()) if len(s) else 0.0
        out[f"sl_{y}"] = int((s["tag"] == "ATR_SL").sum())
    for ym in ("2026-05", "2026-06", "2026-07"):
        s = sells[sells["ym"] == ym]
        out[f"pnl_{ym.replace('-', '_')}"] = float(s["Return"].sum()) if len(s) else 0.0
    return out


def run_one(exp: Dict[str, Any], base: Dict[str, Any], start: str, end: str) -> Dict[str, Any]:
    rid = exp["id"]
    dest = OUT_DIR / rid
    meta_path = dest / "meta.json"
    if meta_path.is_file():
        try:
            cached = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            if cached.get("start") == start and cached.get("end") == end:
                print(f"[{rid}] 캐시 재사용")
                return cached
        except Exception:
            pass

    print("\n" + "=" * 72)
    print(f"[{rid}] {exp['title']}")
    print("=" * 72)
    st = deepcopy(base)
    st.update(exp["overrides"])
    print(
        f"  breadth={st.get('use_market_breadth_gate')} chop={st.get('use_chop_gate')} "
        f"chop_a={st.get('chop_slots_a', '-')}"
    )

    def cb(msg: str, val: Any = None) -> None:
        try:
            if str(msg).startswith("[") and "시계열" in str(msg) and val is not None:
                print(f"\r  {str(msg).split(']', 1)[0]}] {float(val):5.1f}%", end="", flush=True)
        except Exception:
            pass

    t0 = time.time()
    try:
        tester = DynamicBacktester(st)
        trades = tester.run(start, end, interval="15m", callback=cb, high_precision=HIGH_PRECISION)
    except Exception:
        traceback.print_exc()
        return {"id": rid, "error": True}
    elapsed = time.time() - t0
    print(f"\n  done {elapsed:.1f}s")

    dest.mkdir(parents=True, exist_ok=True)
    if trades is not None and not trades.empty:
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(tester.daily_equity).to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")

    eq = _equity_metrics(tester.daily_equity, tester.initial_cash)
    tm = _trade_metrics(trades if isinstance(trades, pd.DataFrame) else None)
    r: Dict[str, Any] = {
        "id": rid,
        "title": exp["title"],
        "start": start,
        "end": end,
        "elapsed_sec": elapsed,
        **eq,
        **tm,
    }
    meta_path.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"  total={r.get('ret_total_pct', float('nan')):+.2f}% "
        f"24={r.get('ret_2024_pct', float('nan')):+.1f}% "
        f"25={r.get('ret_2025_pct', float('nan')):+.1f}% "
        f"26YTD={r.get('ret_2026_ytd_pct', float('nan')):+.1f}% "
        f"DD={r.get('max_dd_pct', float('nan')):.1f}%"
    )
    return r


def _print_summary(rows: List[Dict[str, Any]]) -> None:
    cols = [
        "id",
        "ret_total_pct",
        "ret_2024_pct",
        "ret_2025_pct",
        "ret_2026_ytd_pct",
        "ret_2026_05",
        "ret_2026_06",
        "ret_2026_07",
        "max_dd_pct",
        "pnl_2026_05",
        "pnl_2026_06",
        "pnl_2026_07",
        "sl_2026",
        "win_rate",
    ]
    df = pd.DataFrame(rows)
    show = [c for c in cols if c in df.columns]
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(df[show].to_string(index=False, float_format=lambda x: f"{x:+.2f}" if pd.notna(x) else "nan"))

    base = next((r for r in rows if r.get("id") == "BASE"), None)
    if base:
        print("\n--- BASE 대비 delta (total / 26YTD / MDD) ---")
        for r in rows:
            if r.get("id") == "BASE":
                continue
            dt = (r.get("ret_total_pct") or 0) - (base.get("ret_total_pct") or 0)
            d26 = (r.get("ret_2026_ytd_pct") or 0) - (base.get("ret_2026_ytd_pct") or 0)
            ddd = (r.get("max_dd_pct") or 0) - (base.get("max_dd_pct") or 0)
            print(f"  {r.get('id'):12s}  Δtotal={dt:+.2f}%p  Δ26YTD={d26:+.2f}%p  ΔMDD={ddd:+.2f}%p")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=START)
    ap.add_argument("--end", default=END)
    ap.add_argument("--only", default="", help="쉼표 구분 id (예 BASE,CHOP8)")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    exps = EXPERIMENTS
    if args.only.strip():
        allow = {x.strip().upper() for x in args.only.split(",") if x.strip()}
        exps = [e for e in EXPERIMENTS if e["id"] in allow]

    rows: List[Dict[str, Any]] = []
    for exp in exps:
        rows.append(run_one(exp, base, args.start, args.end))

    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_summary(rows)
    print(f"\n[저장] {summary_path}")


if __name__ == "__main__":
    main()
