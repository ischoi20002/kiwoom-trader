# -*- coding: utf-8 -*-
"""
초단기 ATR_SL 유예 vs prefer_b+A슬롯 축소 비교 (브레드스 OFF 기준).

실험:
  CUR  — 현재 settings.json (C조합 + breadth OFF)
  E1   — 진입 24h ATR_SL 배수 4.0 유예
  E2   — same_bar prefer_b + max_slots_a=12
  E3   — E1 + E2

실행:
  python tools/compare_early_sl_vs_prefer_b.py
"""
from __future__ import annotations

import json
import os
import shutil
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

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from backtest_engine import settings_for_backtest  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

START = "2024-06-03"
END = "2025-12-31"
OUT_DIR = ROOT / "tools" / "bt_compare_out2"
HIGH_PRECISION = False

EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "id": "CUR",
        "title": "CUR: 현재(C조합·breadth OFF)",
        "overrides": {},
    },
    {
        "id": "E1",
        "title": "E1: 진입24h ATR_SL×4.0 유예",
        "overrides": {
            "atr_sl_entry_grace_hours": 24.0,
            "atr_sl_entry_grace_mult": 4.0,
        },
    },
    {
        "id": "E2",
        "title": "E2: prefer_b + max_slots_a=12",
        "overrides": {
            "same_bar_sig_priority": "prefer_b",
            "max_slots_a": 12,
        },
    },
    {
        "id": "E3",
        "title": "E3: E1+E2",
        "overrides": {
            "atr_sl_entry_grace_hours": 24.0,
            "atr_sl_entry_grace_mult": 4.0,
            "same_bar_sig_priority": "prefer_b",
            "max_slots_a": 12,
        },
    },
]


def _load_base_settings() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    # 분석 시점 스택 고정
    st["use_market_breadth_gate"] = False
    st["use_type_a"] = True
    st["use_type_b"] = True
    st["use_sel_track_a"] = True
    st["use_sel_track_b"] = True
    st["atr_multiplier_sl_a"] = float(st.get("atr_multiplier_sl_a", 2.8))
    st["s1_a_take_profit"] = float(st.get("s1_a_take_profit", 6.0))
    st["bt_high_precision"] = False
    st["bt_rt_parity_intrabar"] = False
    # 유예 기본 OFF (실험에서만 ON)
    st["atr_sl_entry_grace_hours"] = 0.0
    st["atr_sl_entry_grace_mult"] = 0.0
    return st


def _exit_tag(reason: str) -> str:
    r = str(reason or "").split("|", 1)[0].strip().upper()
    for key in ("TAKE_PROFIT", "ATR_SL", "ATR_TRAIL", "FLASH_EXIT", "GAP_DOWN", "STOP_LOSS"):
        if key in r:
            return key
    return r[:24] if r else "UNKNOWN"


def _equity_metrics(daily_equity: List[dict], initial: float) -> Dict[str, Any]:
    if not daily_equity:
        return {"error": "empty equity"}
    df = pd.DataFrame(daily_equity)
    df["date"] = pd.to_datetime(df["date"], format="mixed", errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")
    df["d"] = df["date"].dt.normalize()
    daily = df.groupby("d", as_index=False).last()
    col = "equity" if "equity" in daily.columns else "Total"

    def at(day: str) -> float:
        sub = daily[daily["d"] <= pd.Timestamp(day)]
        return float(sub.iloc[-1][col]) if len(sub) else float("nan")

    e0 = float(initial)
    e24 = at("2024-12-31")
    e_end = float(daily.iloc[-1][col])
    peak = float(daily[col].max())
    peak_series = daily[col].cummax()
    dd = (daily[col] - peak_series) / peak_series
    return {
        "start": e0,
        "end": e_end,
        "end_2024": e24,
        "ret_2024_pct": (e24 / e0 - 1) * 100,
        "ret_2025_pct": (e_end / e24 - 1) * 100 if e24 else float("nan"),
        "ret_total_pct": (e_end / e0 - 1) * 100,
        "peak": peak,
        "max_dd_pct": float(dd.min() * 100),
    }


def _trade_metrics(trades_df: pd.DataFrame) -> Dict[str, Any]:
    if trades_df is None or trades_df.empty:
        return {"n_trades": 0, "n_sell": 0}
    df = trades_df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    sells = df[df["Action"] == "SELL"].copy()
    buys1 = df[df["Action"] == "BUY1"]
    if sells.empty:
        return {"n_trades": len(df), "n_buy1": len(buys1), "n_sell": 0}
    sells["year"] = sells["Date"].dt.year
    sells["tag"] = sells["Reason"].map(_exit_tag)
    # hold approx: not FIFO here — just exit mix
    out: Dict[str, Any] = {
        "n_trades": len(df),
        "n_buy1": int(len(buys1)),
        "n_sell": int(len(sells)),
        "win_rate": float((sells["Return"] > 0).mean() * 100),
        "avg_ret": float(sells["Return"].mean()),
    }
    if "Sig_Type" in df.columns:
        buy_sig = df[df["Action"] == "BUY1"]["Sig_Type"].astype(str)
        out["buy_type_a"] = int((buy_sig == "TYPE_A").sum())
        out["buy_type_b"] = int((buy_sig == "TYPE_B").sum())
    for y in (2024, 2025):
        s = sells[sells["year"] == y]
        out[f"n_sell_{y}"] = int(len(s))
        out[f"win_{y}"] = float((s["Return"] > 0).mean() * 100) if len(s) else 0.0
        out[f"avg_ret_{y}"] = float(s["Return"].mean()) if len(s) else 0.0
        out[f"atr_sl_{y}"] = int((s["tag"] == "ATR_SL").sum())
        out[f"trail_{y}"] = int((s["tag"] == "ATR_TRAIL").sum())
        out[f"tp_{y}"] = int((s["tag"] == "TAKE_PROFIT").sum())
        if "Sig_Type" in s.columns:
            for sig in ("TYPE_A", "TYPE_B"):
                g = s[s["Sig_Type"] == sig]
                out[f"pnlpp_{sig[-1]}_{y}"] = float(g["Return"].sum()) if len(g) else 0.0
    return out


def _save_run(run_id: str, tester: DynamicBacktester, trades: Optional[pd.DataFrame]) -> Path:
    dest = OUT_DIR / run_id
    dest.mkdir(parents=True, exist_ok=True)
    eq = pd.DataFrame(tester.daily_equity)
    if not eq.empty:
        eq.to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")
    if trades is not None and not trades.empty:
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    for name in ("backtest_portfolio_curve.csv", "backtest_trades.csv"):
        src = ROOT / name
        if src.is_file():
            try:
                shutil.copy2(src, dest / name)
            except Exception:
                pass
    return dest


def run_one(exp: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    run_id = str(exp["id"])
    print("\n" + "=" * 72)
    print(f"[{run_id}] {exp['title']}")
    print("=" * 72)
    st = deepcopy(base)
    st.update(exp["overrides"])
    print(
        f"  grace_h={st.get('atr_sl_entry_grace_hours')} grace_mult={st.get('atr_sl_entry_grace_mult')} "
        f"priority={st.get('same_bar_sig_priority')} slots_a={st.get('max_slots_a')} "
        f"sl_a={st.get('atr_multiplier_sl_a')} tp_a={st.get('s1_a_take_profit')}"
    )

    def cb(msg: str, val: Any = None) -> None:
        try:
            s = str(msg)
            if s.startswith("[") and "시계열" in s and val is not None:
                print(f"\r  {s.split(']',1)[0]}] {float(val):5.1f}%", end="", flush=True)
        except Exception:
            pass

    t0 = time.time()
    tester = DynamicBacktester(st)
    trades = tester.run(START, END, interval="15m", callback=cb, high_precision=HIGH_PRECISION)
    elapsed = time.time() - t0
    print(f"\n  done in {elapsed:.1f}s")
    _save_run(run_id, tester, trades)
    eq_m = _equity_metrics(tester.daily_equity, tester.initial_cash)
    tr_m = _trade_metrics(trades if trades is not None else pd.DataFrame())
    result = {"id": run_id, "title": exp["title"], "elapsed_sec": elapsed, **eq_m, **tr_m}
    print(
        f"  total={result.get('ret_total_pct', float('nan')):+.2f}%  "
        f"2024={result.get('ret_2024_pct', float('nan')):+.2f}%  "
        f"2025={result.get('ret_2025_pct', float('nan')):+.2f}%  "
        f"MaxDD={result.get('max_dd_pct', float('nan')):.2f}%  "
        f"SL25={result.get('atr_sl_2025', 0)} TR25={result.get('trail_2025', 0)} "
        f"BuyA/B={result.get('buy_type_a', 0)}/{result.get('buy_type_b', 0)}"
    )
    return result


def _print_summary(rows: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    hdr = [
        "ID", "Total%", "2024%", "2025%", "MaxDD%", "WR%", "SL25", "TR25", "BuyA", "BuyB", "sec",
    ]
    print("  ".join(f"{h:>8}" for h in hdr))
    for r in rows:
        vals = [
            r["id"],
            f"{r.get('ret_total_pct', float('nan')):+.1f}",
            f"{r.get('ret_2024_pct', float('nan')):+.1f}",
            f"{r.get('ret_2025_pct', float('nan')):+.1f}",
            f"{r.get('max_dd_pct', float('nan')):.1f}",
            f"{r.get('win_rate', 0):.1f}",
            str(r.get("atr_sl_2025", 0)),
            str(r.get("trail_2025", 0)),
            str(r.get("buy_type_a", 0)),
            str(r.get("buy_type_b", 0)),
            f"{r.get('elapsed_sec', 0):.0f}",
        ]
        print("  ".join(f"{v:>8}" for v in vals))
    ranked = sorted(rows, key=lambda x: (x.get("ret_total_pct", -999), x.get("ret_2025_pct", -999)), reverse=True)
    print("\n[전체 수익률 순위]")
    for i, r in enumerate(ranked, 1):
        print(
            f"  {i}. {r['id']}: total {r.get('ret_total_pct', float('nan')):+.2f}% "
            f"2025 {r.get('ret_2025_pct', float('nan')):+.2f}% — {r['title']}"
        )


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base_settings()
    print(f"기간: {START} ~ {END} | 저정밀 | breadth OFF")
    print(f"출력: {OUT_DIR}")
    results: List[Dict[str, Any]] = []
    for exp in EXPERIMENTS:
        try:
            results.append(run_one(exp, base))
        except Exception as e:
            print(f"\n[FAIL] {exp['id']}: {e}")
            traceback.print_exc()
            results.append({"id": exp["id"], "title": exp["title"], "error": str(e)})
    ok = [r for r in results if "error" not in r]
    _print_summary(ok)
    (OUT_DIR / "summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    if ok:
        pd.DataFrame(ok).to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    print(f"\n저장: {OUT_DIR / 'summary.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
