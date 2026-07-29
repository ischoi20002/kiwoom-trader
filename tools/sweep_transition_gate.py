# -*- coding: utf-8 -*-
"""
Transition gate 임계값·tiered 응답 스윕.

tiered: TRANSITION → S2만 중단 + 슬롯 6/6, HARD → S1+S2 차단 + 슬롯 0

  python tools/sweep_transition_gate.py
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

START = "2024-06-03"
END = "2026-07-24"
OUT_DIR = ROOT / "tools" / "bt_compare_out_transition_sweep"

_COMMON: Dict[str, Any] = {
    "use_market_breadth_gate": False,
    "use_chop_gate": False,
    "use_stress_gate": False,
    "use_transition_gate": True,
    "transition_soft_slots_a": 6,
    "transition_soft_slots_b": 6,
    "transition_share_delta3_max": -10.0,
    "transition_xs_ret_std_min": 3.5,
    "chop_valid_a_min": 15,
    "chop_valid_b_max": 3,
    "chop_valid_b_share_max_pct": 15.0,
    "chop_valid_a_floor": 10,
}

EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "id": "TRANS_TIER",
        "title": "tiered · prior45 · d5-15 · rvol35",
        "overrides": {
            **_COMMON,
            "transition_prior_share_min": 45.0,
            "transition_share_delta5_max": -15.0,
            "transition_rvol_min": 35.0,
        },
    },
    {
        "id": "TRANS_T50_D20",
        "title": "tiered · prior50 · d5-20 · rvol35",
        "overrides": {
            **_COMMON,
            "transition_prior_share_min": 50.0,
            "transition_share_delta5_max": -20.0,
            "transition_rvol_min": 35.0,
        },
    },
    {
        "id": "TRANS_T55_D20",
        "title": "tiered · prior55 · d5-20 · rvol35",
        "overrides": {
            **_COMMON,
            "transition_prior_share_min": 55.0,
            "transition_share_delta5_max": -20.0,
            "transition_rvol_min": 35.0,
        },
    },
    {
        "id": "TRANS_T50_R40",
        "title": "tiered · prior50 · d5-20 · rvol40",
        "overrides": {
            **_COMMON,
            "transition_prior_share_min": 50.0,
            "transition_share_delta5_max": -20.0,
            "transition_rvol_min": 40.0,
        },
    },
    {
        "id": "TRANS_T50_S4",
        "title": "tiered · prior50 · d5-20 · soft4",
        "overrides": {
            **_COMMON,
            "transition_prior_share_min": 50.0,
            "transition_share_delta5_max": -20.0,
            "transition_rvol_min": 35.0,
            "transition_soft_slots_a": 4,
            "transition_soft_slots_b": 4,
        },
    },
    {
        "id": "TRANS_HARDONLY",
        "title": "tiered · prior50 · d5-20 · HARD만 S1차단",
        "overrides": {
            **_COMMON,
            "transition_prior_share_min": 50.0,
            "transition_share_delta5_max": -20.0,
            "transition_rvol_min": 35.0,
            "transition_block_s1_on_soft": False,
        },
    },
    {
        "id": "TRANS_S8",
        "title": "tiered · prior50 · d5-20 · soft8",
        "overrides": {
            **_COMMON,
            "transition_prior_share_min": 50.0,
            "transition_share_delta5_max": -20.0,
            "transition_rvol_min": 35.0,
            "transition_soft_slots_a": 8,
            "transition_soft_slots_b": 8,
        },
    },
    {
        "id": "TRANS_S8_NS2",
        "title": "soft8 · TRANSITION은 S2유지 · HARD만 S2차단",
        "overrides": {
            **_COMMON,
            "transition_prior_share_min": 50.0,
            "transition_share_delta5_max": -20.0,
            "transition_rvol_min": 35.0,
            "transition_soft_slots_a": 8,
            "transition_soft_slots_b": 8,
            "transition_block_s2_on_soft": False,
        },
    },
    {
        "id": "TRANS_HARD_ONLY",
        "title": "HARD(chop)만 반응 · TRANSITION 무시",
        "overrides": {
            **_COMMON,
            "transition_prior_share_min": 50.0,
            "transition_share_delta5_max": -20.0,
            "transition_rvol_min": 35.0,
            "transition_ignore_soft": True,
        },
    },
]


def _load_base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["use_market_breadth_gate"] = False
    st["bt_high_precision"] = False
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


def _metrics(tester: DynamicBacktester, trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    eq = pd.DataFrame(tester.daily_equity)
    eq["date"] = pd.to_datetime(eq["date"], format="mixed", errors="coerce")
    eq = eq.dropna(subset=["date"]).sort_values("date")
    eq["d"] = eq["date"].dt.normalize()
    daily = eq.groupby("d", as_index=False).last()
    col = "equity" if "equity" in daily.columns else "Total"
    e0 = float(tester.initial_cash)

    def _at(day: str) -> float:
        sub = daily[daily["d"] <= pd.Timestamp(day)]
        return float(sub.iloc[-1][col]) if not sub.empty else e0

    e24, e25 = _at("2024-12-31"), _at("2025-12-31")
    e_end = float(daily.iloc[-1][col])
    dd = (daily[col] - daily[col].cummax()) / daily[col].cummax()
    out: Dict[str, Any] = {
        "ret_total_pct": (e_end / e0 - 1) * 100,
        "ret_2024_pct": (e24 / e0 - 1) * 100,
        "ret_2025_pct": (e25 / e24 - 1) * 100 if e24 else 0.0,
        "ret_2026_ytd_pct": (e_end / e25 - 1) * 100 if e25 else 0.0,
        "max_dd_pct": float(dd.min() * 100),
    }
    for ym, lbl in [("2026-06", "ret_2026_06"), ("2026-07", "ret_2026_07")]:
        sub = daily[daily["d"].dt.strftime("%Y-%m") == ym]
        if len(sub) >= 2:
            out[lbl] = (float(sub.iloc[-1][col]) / float(sub.iloc[0][col]) - 1) * 100
    if trades is not None and not trades.empty:
        t = trades.copy()
        out["n_buy1"] = int((t["Action"] == "BUY1").sum())
        out["n_buy2"] = int((t["Action"] == "BUY2").sum())
        sells = t[t["Action"] == "SELL"].copy()
        sells["Date"] = pd.to_datetime(sells["Date"], errors="coerce")
        sells["tag"] = sells["Reason"].map(_exit_tag)
        out["n_sell"] = int(len(sells))
        out["win_rate"] = float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0
        s26 = sells[sells["Date"].dt.year == 2026]
        out["sl_2026"] = int((s26["tag"] == "ATR_SL").sum())
    return out


def run_one(exp: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    rid = exp["id"]
    dest = OUT_DIR / rid
    meta_path = dest / "meta.json"
    if meta_path.is_file():
        try:
            cached = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            if cached.get("start") == START and cached.get("end") == END:
                print(f"[{rid}] 캐시 재사용")
                return cached
        except Exception:
            pass

    print(f"\n[{rid}] {exp['title']}")
    st = deepcopy(base)
    st.update(exp["overrides"])
    t0 = time.time()
    try:
        tester = DynamicBacktester(st)
        trades = tester.run(START, END, interval="15m", high_precision=False)
    except Exception:
        traceback.print_exc()
        return {"id": rid, "error": True}
    elapsed = time.time() - t0
    dest.mkdir(parents=True, exist_ok=True)
    if trades is not None and not trades.empty:
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(tester.daily_equity).to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")
    r = {"id": rid, "title": exp["title"], "start": START, "end": END, "elapsed_sec": elapsed, ** _metrics(tester, trades if isinstance(trades, pd.DataFrame) else None)}
    meta_path.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"  total={r.get('ret_total_pct', 0):+.1f}% 25={r.get('ret_2025_pct', 0):+.1f}% "
        f"26={r.get('ret_2026_ytd_pct', 0):+.1f}% MDD={r.get('max_dd_pct', 0):+.1f}% "
        f"B2={r.get('n_buy2', 0)} ({elapsed:.0f}s)"
    )
    return r


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="쉼표 구분 id")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    exps = EXPERIMENTS
    if args.only.strip():
        allow = {x.strip().upper() for x in args.only.split(",") if x.strip()}
        exps = [e for e in EXPERIMENTS if e["id"] in allow]
    rows = [run_one(e, base) for e in exps]

    base_meta = ROOT / "tools/bt_compare_out_chop_gate/BASE/meta.json"
    if base_meta.is_file():
        b = json.loads(base_meta.read_text(encoding="utf-8-sig"))
        b["id"] = "BASE"
        rows.insert(0, b)

    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    cols = ["id", "ret_total_pct", "ret_2025_pct", "ret_2026_ytd_pct", "ret_2026_06", "ret_2026_07", "max_dd_pct", "n_buy2", "sl_2026"]
    df = pd.DataFrame(rows)
    show = [c for c in cols if c in df.columns]
    print("\n=== SUMMARY ===")
    print(df[show].to_string(index=False, float_format=lambda x: f"{x:+.2f}"))

    b = next((r for r in rows if r.get("id") == "BASE"), None)
    if b:
        print("\n--- BASE 대비 (total / 2025 / 26YTD / MDD) ---")
        for r in rows:
            if r.get("id") == "BASE":
                continue
            dt = (r.get("ret_total_pct") or 0) - (b.get("ret_total_pct") or 0)
            d25 = (r.get("ret_2025_pct") or 0) - (b.get("ret_2025_pct") or 0)
            d26 = (r.get("ret_2026_ytd_pct") or 0) - (b.get("ret_2026_ytd_pct") or 0)
            ddd = (r.get("max_dd_pct") or 0) - (b.get("max_dd_pct") or 0)
            print(f"  {r.get('id'):16s} Δtot={dt:+.1f}p Δ25={d25:+.1f}p Δ26={d26:+.1f}p ΔMDD={ddd:+.1f}p")

    # adoptable: 2025 >= BASE-10%p, 26YTD >= BASE-2%p, MDD >= BASE+3%p
    if b:
        print("\n--- adoptable 후보 (25≥BASE-10p, 26≥BASE-2p, MDD≥BASE+3p) ---")
        for r in rows:
            if r.get("id") == "BASE":
                continue
            ok25 = (r.get("ret_2025_pct") or 0) >= (b.get("ret_2025_pct") or 0) - 10.0
            ok26 = (r.get("ret_2026_ytd_pct") or 0) >= (b.get("ret_2026_ytd_pct") or 0) - 2.0
            okdd = (r.get("max_dd_pct") or 0) >= (b.get("max_dd_pct") or 0) + 3.0
            mark = "✓" if (ok25 and ok26 and okdd) else " "
            print(f"  [{mark}] {r.get('id')}")
    print(f"\n[저장] {summary_path}")


if __name__ == "__main__":
    main()
