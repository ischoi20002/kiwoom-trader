# -*- coding: utf-8 -*-
"""
순위 모드 비교 — 목표 지표를 S1-only( use_s2_master_a/b=False )로 고정.

  python tools/compare_score_modes_s1_only.py
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
import strategy_selector as ss

importlib.reload(be)
importlib.reload(ss)
importlib.reload(db)

from backtest_engine import settings_for_backtest  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

OUT = ROOT / "tools" / "bt_compare_out_score_s1_only"
START = "2024-06-03"
END = "2026-06-30"
OOS_START = "2025-07-01"


def _base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["use_market_breadth_gate"] = False
    st["bt_high_precision"] = False
    st["enforce_track_signal_match"] = True
    st["universe_index_choice"] = "KODEX200"
    # S1-only 목표
    st["use_s2_master_a"] = False
    st["use_s2_master_b"] = False
    return st


def _load_s1_signs() -> Dict[str, float]:
    p = ROOT / "tools" / "bt_compare_out_s1_pnl_rank" / "s1_pnl_signs.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        signs = data.get("signs") or {}
        if isinstance(signs, dict) and "signs" in signs:
            # nested from previous payload
            return {k: float(v) for k, v in (signs.get("signs") or {}).items()}
        return {k: float(v) for k, v in signs.items()} if isinstance(signs, dict) else {}
    except Exception:
        return {}


def run_bt(mode: str, settings: Dict[str, Any], start: str, end: str) -> Dict[str, Any]:
    st = deepcopy(settings)
    st["sel_score_mode"] = mode
    if mode == "s1_pnl_aligned_v1":
        st["sel_s1_pnl_signs"] = _load_s1_signs() or st.get("sel_s1_pnl_signs") or {}
    print(f"\n=== S1-only BT  mode={mode}  {start}~{end} ===")
    print(f"  signs_keys={list((st.get('sel_s1_pnl_signs') or {}).keys())[:8]}…")
    t0 = time.time()
    tester = DynamicBacktester(st)

    def cb(msg: str, val: Any = None) -> None:
        try:
            if str(msg).startswith("[") and "시계열" in str(msg) and val is not None:
                print(f"\r  {float(val):5.1f}%", end="", flush=True)
        except Exception:
            pass

    trades = tester.run(start, end, interval="15m", callback=cb, high_precision=False)
    elapsed = time.time() - t0
    print(f"\n  done {elapsed:.1f}s")

    eq = pd.DataFrame(tester.daily_equity)
    eq["date"] = pd.to_datetime(eq["date"], format="mixed", errors="coerce")
    eq = eq.dropna(subset=["date"]).sort_values("date")
    eq["d"] = eq["date"].dt.normalize()
    daily = eq.groupby("d", as_index=False).last()
    col = "equity" if "equity" in daily.columns else "Total"
    e0 = float(tester.initial_cash)
    e1 = float(daily.iloc[-1][col])
    dd = (daily[col] - daily[col].cummax()) / daily[col].cummax()

    n_buy1 = n_buy2 = 0
    win = avg_ret = 0.0
    if isinstance(trades, pd.DataFrame) and not trades.empty:
        n_buy1 = int((trades["Action"] == "BUY1").sum())
        n_buy2 = int((trades["Action"] == "BUY2").sum())
        sells = trades[trades["Action"] == "SELL"]
        if len(sells):
            win = float((sells["Return"] > 0).mean() * 100.0)
            avg_ret = float(sells["Return"].mean())  # 이미 %
        dest = OUT / f"{mode}_{start}_{end}".replace(":", "")
        dest.mkdir(parents=True, exist_ok=True)
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
        daily.to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")

    return {
        "mode": mode,
        "start": start,
        "end": end,
        "ret_total_pct": (e1 / e0 - 1.0) * 100.0,
        "max_dd_pct": float(dd.min() * 100.0),
        "n_buy1": n_buy1,
        "n_buy2": n_buy2,
        "win_rate": win,
        "avg_trade_ret_pct": avg_ret,
        "elapsed_sec": round(elapsed, 1),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    base = _base()
    # FIT 구간에서 뽑은 부호가 파일에 nested일 수 있어 정규화
    signs_path = ROOT / "tools" / "bt_compare_out_s1_pnl_rank" / "s1_pnl_signs.json"
    if signs_path.is_file():
        raw = json.loads(signs_path.read_text(encoding="utf-8"))
        # file is {"signs": {...}, "rho": {...}} from _derive_signs
        if "signs" in raw and isinstance(raw["signs"], dict):
            # already flat signs dict at top from earlier write
            pass

    modes = ["legacy", "fwd_aligned_v1", "s1_pnl_aligned_v1"]
    full: List[Dict[str, Any]] = []
    oos: List[Dict[str, Any]] = []

    for m in modes:
        try:
            full.append(run_bt(m, base, START, END))
        except Exception as e:
            traceback.print_exc()
            full.append({"mode": m, "error": str(e)})
    for m in modes:
        try:
            oos.append(run_bt(m, base, OOS_START, END))
        except Exception as e:
            oos.append({"mode": m, "error": str(e)})

    payload = {
        "objective": "S1-only (S2 masters OFF)",
        "full": full,
        "oos": oos,
        "note": "총수익이 아니라 S1-only 자산곡선으로 순위 모드를 비교한다.",
    }
    (OUT / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    def _prt(title: str, rows: List[Dict[str, Any]]) -> None:
        print("\n" + "=" * 64)
        print(title)
        base_ret = None
        for r in rows:
            if "error" in r:
                print(f"  {r['mode']}: ERROR {r['error']}")
                continue
            if base_ret is None and r["mode"] == "legacy":
                base_ret = float(r["ret_total_pct"])
            dlt = ""
            if base_ret is not None and r["mode"] != "legacy":
                dlt = f"  Δ={float(r['ret_total_pct']) - base_ret:+.2f}%p"
            print(
                f"  {r['mode']:22s}  {r['ret_total_pct']:+7.2f}%  DD={r['max_dd_pct']:6.1f}%  "
                f"buy1={r['n_buy1']:4d} buy2={r['n_buy2']}  "
                f"win={r['win_rate']:.1f}%  avgTrade={r['avg_trade_ret_pct']:+.2f}%{dlt}"
            )

    _prt(f"S1-only FULL {START}~{END}", full)
    _prt(f"S1-only OOS  {OOS_START}~{END}", oos)
    print(f"\n[저장] {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
