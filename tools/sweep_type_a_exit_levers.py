# -*- coding: utf-8 -*-
"""
Type A 청산 레버 소폭 스윕 (진단 후속).

축:
  1) atr_multiplier_sl_a: 2.2 / 2.8(baseline) / 3.2
  2) gap_down_limit_a: -3.0 / -4.0(baseline) / -5.0

조건: Type A + S2(A), Type B OFF
판정: 총수익 ≥ baseline*0.95  AND  MDD 개선(덜 나쁨)

  python tools/sweep_type_a_exit_levers.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

OUT = ROOT / "tools" / "bt_compare_out_type_a_exit_sweep"
START = "2024-06-03"
END = "2026-06-30"
OOS_START = "2025-07-01"
RET_FLOOR = 0.95  # baseline 대비 최소 수익 유지율


def _base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["use_market_breadth_gate"] = False
    st["bt_high_precision"] = False
    st["enforce_track_signal_match"] = True
    st["universe_index_choice"] = "KODEX200"
    st["sel_score_mode"] = "legacy"
    st["use_type_a"] = True
    st["use_type_b"] = False
    st["use_s2_master_a"] = True
    st["use_s2_master_b"] = False
    return st


def _slug(overrides: Dict[str, Any]) -> str:
    parts = []
    for k in sorted(overrides.keys()):
        v = overrides[k]
        parts.append(f"{k}={v}".replace(".", "p"))
    return "__".join(parts) if parts else "baseline"


def run_bt(
    name: str,
    overrides: Dict[str, Any],
    start: str,
    end: str,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    dest = OUT / f"{name}_{start}_{end}".replace(":", "")
    trades_p = dest / "trades.csv"
    equity_p = dest / "equity.csv"
    meta_p = dest / "meta.json"

    if not force and trades_p.is_file() and equity_p.is_file() and meta_p.is_file():
        print(f"[캐시] {dest.name}")
        return json.loads(meta_p.read_text(encoding="utf-8-sig"))

    st = _base()
    for k, v in overrides.items():
        st[k] = v

    print(f"\n=== BT {name} {start}~{end} overrides={overrides} ===")
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

    n_buy1 = int((trades["Action"] == "BUY1").sum()) if isinstance(trades, pd.DataFrame) else 0
    n_buy2 = int((trades["Action"] == "BUY2").sum()) if isinstance(trades, pd.DataFrame) else 0
    sells = trades[trades["Action"] == "SELL"] if isinstance(trades, pd.DataFrame) else pd.DataFrame()
    win = float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0

    meta = {
        "name": name,
        "overrides": overrides,
        "start": start,
        "end": end,
        "ret_total_pct": (e1 / e0 - 1.0) * 100.0,
        "max_dd_pct": float(dd.min() * 100),
        "n_buy1": n_buy1,
        "n_buy2": n_buy2,
        "win_rate": win,
        "elapsed_sec": round(elapsed, 1),
    }
    meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"  ret={meta['ret_total_pct']:+.2f}% DD={meta['max_dd_pct']:.1f}% "
        f"buy1={n_buy1} buy2={n_buy2}"
    )
    return meta


def _judge(row: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    """수익 ≥ 95% baseline AND MDD 개선(절대값 감소)."""
    br = float(base["ret_total_pct"])
    bd = float(base["max_dd_pct"])  # 음수
    r = float(row["ret_total_pct"])
    d = float(row["max_dd_pct"])
    ret_ok = r >= br * RET_FLOOR
    # MDD 개선: d가 bd보다 덜 나쁨 (예: -7 > -9)
    mdd_ok = d > bd + 1e-9  # strictly better (less drawdown)
    mdd_tie = abs(d - bd) < 1e-6
    return {
        "ret_ok": ret_ok,
        "mdd_improved": mdd_ok,
        "mdd_same": mdd_tie,
        "delta_ret_pp": r - br,
        "delta_mdd_pp": d - bd,  # +면 MDD 개선(덜 나쁨)
        "pass": bool(ret_ok and mdd_ok),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    atr_vals = [2.2, 2.8, 3.2]
    gap_vals = [-3.0, -4.0, -5.0]

    cases: List[Tuple[str, Dict[str, Any]]] = []
    # baseline
    cases.append(("baseline", {}))
    for v in atr_vals:
        if abs(v - 2.8) < 1e-9:
            continue
        cases.append((f"atr_sl_{v}", {"atr_multiplier_sl_a": float(v)}))
    for v in gap_vals:
        if abs(v - (-4.0)) < 1e-9:
            continue
        cases.append((f"gap_{v}", {"gap_down_limit_a": float(v)}))

    periods = [("full", START, END), ("oos", OOS_START, END)]
    results: Dict[str, List[Dict[str, Any]]] = {"full": [], "oos": []}

    try:
        for label, start, end in periods:
            for name, ov in cases:
                try:
                    meta = run_bt(name, ov, start, end)
                    results[label].append(meta)
                except Exception as e:
                    traceback.print_exc()
                    results[label].append({"name": name, "overrides": ov, "error": str(e)})

        # baseline 찾기
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
            # 후보: full 또는 oos에서 pass, 다른 쪽은 수익 유지
            candidate = (jf["pass"] and jo["ret_ok"]) or (jo["pass"] and jf["ret_ok"])
            both_pass = jf["pass"] and jo["pass"]
            row = {
                "name": name,
                "overrides": ov,
                "full": {**{k: rf[k] for k in ("ret_total_pct", "max_dd_pct", "n_buy1", "n_buy2", "win_rate")}, **jf},
                "oos": {**{k: ro[k] for k in ("ret_total_pct", "max_dd_pct", "n_buy1", "n_buy2", "win_rate")}, **jo},
                "candidate": candidate,
                "both_pass": both_pass,
            }
            judged.append(row)
            tag = "BOTH" if both_pass else ("CAND" if candidate else ("keep$" if jf["ret_ok"] and jo["ret_ok"] else "fail"))
            print(
                f"{name:22s} {rf['ret_total_pct']:+8.2f}% {rf['max_dd_pct']:7.1f}% "
                f"{ro['ret_total_pct']:+8.2f}% {ro['max_dd_pct']:7.1f}%  {tag}"
            )

        candidates = [j for j in judged if j["candidate"] and j["name"] != "baseline"]
        payload = {
            "objective": "Type A exit lever sweep (ATR_SL, GapDown)",
            "ret_floor": RET_FLOOR,
            "baseline": {"full": base_full, "oos": base_oos},
            "results": results,
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

        print("\n=== 후보 (수익≥95% + MDD 개선) ===")
        if not candidates:
            print("  (없음) — Type A 청산 파라미터는 현행 유지 권장")
        else:
            for c in candidates:
                print(
                    f"  {c['name']}: full {c['full']['ret_total_pct']:+.2f}% "
                    f"DD {c['full']['max_dd_pct']:.1f}% "
                    f"(Δret {c['full']['delta_ret_pp']:+.2f}pp Δmdd {c['full']['delta_mdd_pp']:+.2f}pp) | "
                    f"oos {c['oos']['ret_total_pct']:+.2f}% DD {c['oos']['max_dd_pct']:.1f}%"
                )
        print(f"\n[저장] {OUT / 'summary.json'}")
        print("※ settings.json 은 변경하지 않음 — 반영은 별도 승인")
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
