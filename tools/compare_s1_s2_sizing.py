# -*- coding: utf-8 -*-
"""
S1/S2 자금 비중(%) 그리드 비교.

기준: 현재 settings(저정밀, breadth OFF, 재진입 3일, trail A5/B6, DD15 등)
축: s1_equity_pct × s2_equity_pct (sizing unit=% 고정)

기간: settings bt 기간 또는 2024-06-03 ~ 2026-06-30
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from copy import deepcopy
from itertools import product
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import importlib
import backtest_engine as be
import dynamic_backtest as db

importlib.reload(be)
importlib.reload(db)

from backtest_engine import settings_for_backtest  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

START = "2024-06-03"
END = "2026-06-30"
OUT_DIR = ROOT / "tools" / "bt_compare_out8"
HIGH_PRECISION = False

# 현재 20/25 주변 + 약간 넓게
S1_PCTS = (12.0, 15.0, 20.0, 25.0)
S2_PCTS = (15.0, 20.0, 25.0, 30.0)


def _build_experiments() -> List[Dict[str, Any]]:
    exps: List[Dict[str, Any]] = []
    for s1, s2 in product(S1_PCTS, S2_PCTS):
        rid = f"S1_{int(s1)}_S2_{int(s2)}"
        exps.append(
            {
                "id": rid,
                "title": f"S1 {s1:.0f}% / S2 {s2:.0f}%",
                "s1": float(s1),
                "s2": float(s2),
                "overrides": {
                    "s1_sizing_unit": "%",
                    "s2_sizing_unit": "%",
                    "s1_equity_pct": float(s1),
                    "s2_equity_pct": float(s2),
                    # UI 금액 키도 동기화(혼동 방지)
                    "stage1_amt": float(s1),
                    "stage2_amt": float(s2),
                },
            }
        )
    return exps


def _load_base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["use_market_breadth_gate"] = False
    st["bt_high_precision"] = False
    st["bt_rt_parity_intrabar"] = False
    st["s1_sizing_unit"] = "%"
    st["s2_sizing_unit"] = "%"
    return st


def _equity_metrics(daily_equity: List[dict], initial: float) -> Dict[str, Any]:
    df = pd.DataFrame(daily_equity)
    if df.empty:
        return {}
    df["date"] = pd.to_datetime(df["date"], format="mixed", errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")
    df["d"] = df["date"].dt.normalize()
    daily = df.groupby("d", as_index=False).last()
    col = "equity" if "equity" in daily.columns else "Total"

    def at(day: str) -> float:
        sub = daily[daily["d"] <= pd.Timestamp(day)]
        return float(sub.iloc[-1][col])

    e0 = float(initial)
    e24 = at("2024-12-31")
    e25 = at("2025-12-31")
    e_end = float(daily.iloc[-1][col])
    dd = (daily[col] - daily[col].cummax()) / daily[col].cummax()
    return {
        "ret_2024_pct": (e24 / e0 - 1) * 100,
        "ret_2025_pct": (e25 / e24 - 1) * 100,
        "ret_2026h1_pct": (e_end / e25 - 1) * 100,
        "ret_total_pct": (e_end / e0 - 1) * 100,
        "end": e_end,
        "max_dd_pct": float(dd.min() * 100),
    }


def _trade_metrics(trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if trades is None or trades.empty:
        return {"n_sell": 0, "n_buy1": 0, "n_buy2": 0}
    df = trades.copy()
    buys1 = df[df["Action"] == "BUY1"]
    buys2 = df[df["Action"] == "BUY2"]
    sells = df[df["Action"] == "SELL"]
    return {
        "n_buy1": int(len(buys1)),
        "n_buy2": int(len(buys2)),
        "n_sell": int(len(sells)),
        "win_rate": float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0,
        "s2_rate": float(len(buys2) / len(buys1) * 100) if len(buys1) else 0.0,
    }


def run_one(exp: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    rid = exp["id"]
    print("\n" + "=" * 72)
    print(f"[{rid}] {exp['title']}")
    print("=" * 72)
    st = deepcopy(base)
    st.update(exp["overrides"])

    def cb(msg: str, val: Any = None) -> None:
        try:
            if str(msg).startswith("[") and "시계열" in str(msg) and val is not None:
                print(f"\r  {str(msg).split(']', 1)[0]}] {float(val):5.1f}%", end="", flush=True)
        except Exception:
            pass

    t0 = time.time()
    tester = DynamicBacktester(st)
    trades = tester.run(START, END, interval="15m", callback=cb, high_precision=HIGH_PRECISION)
    elapsed = time.time() - t0
    print(f"\n  done {elapsed:.1f}s")

    dest = OUT_DIR / rid
    dest.mkdir(parents=True, exist_ok=True)
    if trades is not None and not trades.empty:
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(tester.daily_equity).to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")

    eq = _equity_metrics(tester.daily_equity, tester.initial_cash)
    tm = _trade_metrics(trades if isinstance(trades, pd.DataFrame) else None)
    r = {
        "id": rid,
        "title": exp["title"],
        "s1_pct": exp["s1"],
        "s2_pct": exp["s2"],
        "elapsed_sec": elapsed,
        **eq,
        **tm,
    }
    print(
        f"  total={r.get('ret_total_pct', float('nan')):+.2f}% "
        f"26H1={r.get('ret_2026h1_pct', float('nan')):+.1f}% "
        f"DD={r.get('max_dd_pct', float('nan')):.1f}% "
        f"buy1={r.get('n_buy1', 0)} buy2={r.get('n_buy2', 0)}"
    )
    return r


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    experiments = _build_experiments()
    base = _load_base()
    print(f"{START} ~ {END} | S1×S2 sizing grid | {len(experiments)} runs")
    print(f"S1={list(S1_PCTS)}  S2={list(S2_PCTS)}")
    results: List[Dict[str, Any]] = []
    for exp in experiments:
        try:
            results.append(run_one(exp, base))
        except Exception as e:
            print(f"[FAIL] {exp['id']}: {e}")
            traceback.print_exc()
            results.append({"id": exp["id"], "error": str(e)})

    ok = [r for r in results if "error" not in r]
    print("\n" + "=" * 72)
    print("SUMMARY S1/S2 SIZING")
    print("=" * 72)
    hdr = ["ID", "S1", "S2", "Total%", "26H1%", "MaxDD", "Buy1", "Buy2", "S2%"]
    print("  ".join(f"{h:>9}" for h in hdr))
    for r in ok:
        print(
            "  ".join(
                f"{v:>9}"
                for v in [
                    r["id"],
                    f"{r.get('s1_pct', 0):.0f}",
                    f"{r.get('s2_pct', 0):.0f}",
                    f"{r.get('ret_total_pct', float('nan')):+.1f}",
                    f"{r.get('ret_2026h1_pct', float('nan')):+.1f}",
                    f"{r.get('max_dd_pct', float('nan')):.1f}",
                    str(r.get("n_buy1", 0)),
                    str(r.get("n_buy2", 0)),
                    f"{r.get('s2_rate', 0):.0f}",
                ]
            )
        )

    print("\n[전체 수익 순위]")
    for i, r in enumerate(sorted(ok, key=lambda x: x.get("ret_total_pct", -999), reverse=True), 1):
        print(
            f"  {i}. {r['id']}: total={r.get('ret_total_pct', float('nan')):+.2f}% "
            f"DD={r.get('max_dd_pct', float('nan')):.1f}% 26H1={r.get('ret_2026h1_pct', float('nan')):+.1f}% — {r['title']}"
        )
    print("\n[MaxDD 순위(덜 나쁨)]")
    for i, r in enumerate(sorted(ok, key=lambda x: x.get("max_dd_pct", -999), reverse=True), 1):
        print(
            f"  {i}. {r['id']}: DD={r.get('max_dd_pct', float('nan')):.1f}% "
            f"total={r.get('ret_total_pct', float('nan')):+.2f}%"
        )

    # 종합 점수: 수익 - 0.5*|DD| (간단한 리스크조정)
    if ok:
        for r in ok:
            r["score"] = float(r.get("ret_total_pct", 0)) + 0.5 * float(r.get("max_dd_pct", 0))
        print("\n[리스크조정 점수 = Total + 0.5*MaxDD]")
        for i, r in enumerate(sorted(ok, key=lambda x: x.get("score", -999), reverse=True), 1):
            print(
                f"  {i}. {r['id']}: score={r['score']:+.2f} "
                f"(total={r.get('ret_total_pct', float('nan')):+.1f}, DD={r.get('max_dd_pct', float('nan')):.1f})"
            )
        try:
            pdf = pd.DataFrame(ok)
            piv = pdf.pivot(index="s1_pct", columns="s2_pct", values="ret_total_pct")
            print("\n[피벗 Total%]")
            print(piv.round(2).to_string())
            piv_dd = pdf.pivot(index="s1_pct", columns="s2_pct", values="max_dd_pct")
            print("\n[피벗 MaxDD%]")
            print(piv_dd.round(2).to_string())
            piv.to_csv(OUT_DIR / "pivot_total.csv", encoding="utf-8-sig")
            piv_dd.to_csv(OUT_DIR / "pivot_maxdd.csv", encoding="utf-8-sig")
        except Exception as e:
            print(f"[피벗 실패] {e}")

    (OUT_DIR / "summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    if ok:
        pd.DataFrame(ok).to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    print(f"\n저장: {OUT_DIR / 'summary.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
