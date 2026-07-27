# -*- coding: utf-8 -*-
"""
Type B 청산 레버 소폭 스윕.

축 (baseline 대비 ±20%):
  1) atr_multiplier_sl_b: 1.6 / 2.0 / 2.4
  2) gap_down_limit_b: -3.0 / -4.0 / -5.0
  3) s1_b_stop_loss: 2.5 / 3.0 / 3.5 (진단에서 PCT_SL 기여 시)

  python tools/sweep_type_b_exit_levers.py
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Tuple

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

OUT = ROOT / "tools" / "bt_compare_out_type_b_exit_sweep"
START = "2024-06-03"
END = "2026-06-30"
OOS_START = "2025-07-01"
RET_FLOOR = 0.95
BASE_ATR = 2.0
BASE_GAP = -4.0
BASE_PCT_SL = 3.0


def _base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["use_market_breadth_gate"] = False
    st["bt_high_precision"] = False
    st["enforce_track_signal_match"] = True
    st["universe_index_choice"] = "KODEX200"
    st["sel_score_mode"] = "legacy"
    st["use_type_a"] = False
    st["use_type_b"] = True
    st["use_s2_master_a"] = False
    st["use_s2_master_b"] = True
    return st


def run_bt(name: str, overrides: Dict[str, Any], start: str, end: str) -> Dict[str, Any]:
    dest = OUT / f"{name}_{start}_{end}".replace(":", "")
    trades_p = dest / "trades.csv"
    equity_p = dest / "equity.csv"
    meta_p = dest / "meta.json"
    if trades_p.is_file() and equity_p.is_file() and meta_p.is_file():
        print(f"[캐시] {dest.name}")
        return json.loads(meta_p.read_text(encoding="utf-8-sig"))

    st = _base()
    for k, v in overrides.items():
        st[k] = v
    print(f"\n=== BT {name} {start}~{end} overrides={overrides} ===")
    import time

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
    dest.mkdir(parents=True, exist_ok=True)
    if isinstance(trades, pd.DataFrame):
        trades.to_csv(trades_p, index=False, encoding="utf-8-sig")
    daily.to_csv(equity_p, index=False, encoding="utf-8-sig")
    meta = {
        "name": name,
        "overrides": overrides,
        "start": start,
        "end": end,
        "ret_total_pct": (e1 / e0 - 1.0) * 100.0,
        "max_dd_pct": float(dd.min() * 100),
        "n_buy1": int((trades["Action"] == "BUY1").sum()) if isinstance(trades, pd.DataFrame) else 0,
        "n_buy2": int((trades["Action"] == "BUY2").sum()) if isinstance(trades, pd.DataFrame) else 0,
        "elapsed_sec": round(elapsed, 1),
    }
    meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ret={meta['ret_total_pct']:+.2f}% DD={meta['max_dd_pct']:.1f}%")
    return meta


def _judge(row: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    br = float(base["ret_total_pct"])
    bd = float(base["max_dd_pct"])
    r = float(row["ret_total_pct"])
    d = float(row["max_dd_pct"])
    ret_ok = r >= br * RET_FLOOR
    mdd_ok = d > bd + 1e-9
    return {
        "ret_ok": ret_ok,
        "mdd_improved": mdd_ok,
        "delta_ret_pp": r - br,
        "delta_mdd_pp": d - bd,
        "pass": bool(ret_ok and mdd_ok),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    diag = ROOT / "tools/bt_compare_out_type_b_exit_diag/summary.json"
    include_pct_sl = False
    if diag.is_file():
        try:
            data = json.loads(diag.read_text(encoding="utf-8"))
            for period in ("full", "oos"):
                for r in (data.get("reason_breakdown", {}).get(period, {}).get("by_reason") or []):
                    if r.get("reason") == "PCT_SL" and float(r.get("loss_sum_pct", 0)) < -10:
                        include_pct_sl = True
        except Exception:
            pass

    cases: List[Tuple[str, Dict[str, Any]]] = [("baseline", {})]
    for v in (1.6, 2.4):
        cases.append((f"atr_sl_{v}", {"atr_multiplier_sl_b": float(v)}))
    for v in (-3.0, -5.0):
        cases.append((f"gap_{v}", {"gap_down_limit_b": float(v)}))
    if include_pct_sl:
        for v in (2.5, 3.5):
            cases.append((f"pct_sl_{v}", {"s1_b_stop_loss": float(v)}))

    results: Dict[str, List[Dict[str, Any]]] = {"full": [], "oos": []}
    try:
        for label, start, end in (("full", START, END), ("oos", OOS_START, END)):
            for name, ov in cases:
                try:
                    results[label].append(run_bt(name, ov, start, end))
                except Exception as e:
                    traceback.print_exc()
                    results[label].append({"name": name, "error": str(e)})

        base_full = next(r for r in results["full"] if r.get("name") == "baseline")
        base_oos = next(r for r in results["oos"] if r.get("name") == "baseline")
        judged: List[Dict[str, Any]] = []
        print("\n" + "=" * 72)
        print(f"{'case':22s} {'full ret':>9} {'full DD':>8} {'oos ret':>9} {'oos DD':>8} 판정")
        for name, ov in cases:
            rf = next((r for r in results["full"] if r.get("name") == name), None)
            ro = next((r for r in results["oos"] if r.get("name") == name), None)
            if not rf or "error" in rf or not ro or "error" in ro:
                print(f"{name:22s} ERROR")
                continue
            jf = _judge(rf, base_full)
            jo = _judge(ro, base_oos)
            both_pass = jf["pass"] and jo["pass"]
            candidate = (jf["pass"] and jo["ret_ok"]) or (jo["pass"] and jf["ret_ok"])
            judged.append(
                {
                    "name": name,
                    "overrides": ov,
                    "full": {**{k: rf[k] for k in ("ret_total_pct", "max_dd_pct", "n_buy1", "n_buy2")}, **jf},
                    "oos": {**{k: ro[k] for k in ("ret_total_pct", "max_dd_pct", "n_buy1", "n_buy2")}, **jo},
                    "candidate": candidate,
                    "both_pass": both_pass,
                }
            )
            tag = "BOTH" if both_pass else ("CAND" if candidate else "fail")
            print(
                f"{name:22s} {rf['ret_total_pct']:+8.2f}% {rf['max_dd_pct']:7.1f}% "
                f"{ro['ret_total_pct']:+8.2f}% {ro['max_dd_pct']:7.1f}%  {tag}"
            )
        candidates = [j for j in judged if j.get("both_pass") and j["name"] != "baseline"]
        payload = {
            "objective": "Type B exit lever sweep",
            "ret_floor": RET_FLOOR,
            "baseline": {"full": base_full, "oos": base_oos},
            "judged": judged,
            "candidates": candidates,
            "recommendation": (
                candidates[0]["name"]
                if candidates
                else "기준선을 넘는 MDD 개선 후보 없음 — 설정 미변경 권장"
            ),
        }
        (OUT / "summary.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"\n[저장] {OUT / 'summary.json'}")
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
