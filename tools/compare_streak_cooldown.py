# -*- coding: utf-8 -*-
"""
연속 ATR_SL 쿨다운 연장 비교.

기준: 재진입 3일 + 갭 -4% + 현행 스택(breadth OFF, 저정밀)
실험:
  CUR   — 연속 연장 OFF (streak_cooldown=0)
  S5_L10 — lookback 10일, 2회째부터 5일
  S7_L10 — lookback 10일, 2회째부터 7일
  S5_L14 — lookback 14일, 2회째부터 5일
  S7_L14 — lookback 14일, 2회째부터 7일

기간: 2024-06-03 ~ 2026-06-30
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

import importlib
import backtest_engine as be
import dynamic_backtest as db

importlib.reload(be)
importlib.reload(db)

from backtest_engine import settings_for_backtest  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

START = "2024-06-03"
END = "2026-06-30"
OUT_DIR = ROOT / "tools" / "bt_compare_out5"
HIGH_PRECISION = False

EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "id": "CUR",
        "title": "CUR: 기본 3일만",
        "overrides": {
            "atr_sl_reentry_cooldown_days": 3,
            "atr_sl_streak_lookback_days": 10,
            "atr_sl_streak_cooldown_days": 0,
            "atr_sl_streak_min_count": 2,
        },
    },
    {
        "id": "S5_L10",
        "title": "연속SL→5일 (조회10)",
        "overrides": {
            "atr_sl_reentry_cooldown_days": 3,
            "atr_sl_streak_lookback_days": 10,
            "atr_sl_streak_cooldown_days": 5,
            "atr_sl_streak_min_count": 2,
        },
    },
    {
        "id": "S7_L10",
        "title": "연속SL→7일 (조회10)",
        "overrides": {
            "atr_sl_reentry_cooldown_days": 3,
            "atr_sl_streak_lookback_days": 10,
            "atr_sl_streak_cooldown_days": 7,
            "atr_sl_streak_min_count": 2,
        },
    },
    {
        "id": "S5_L14",
        "title": "연속SL→5일 (조회14)",
        "overrides": {
            "atr_sl_reentry_cooldown_days": 3,
            "atr_sl_streak_lookback_days": 14,
            "atr_sl_streak_cooldown_days": 5,
            "atr_sl_streak_min_count": 2,
        },
    },
    {
        "id": "S7_L14",
        "title": "연속SL→7일 (조회14)",
        "overrides": {
            "atr_sl_reentry_cooldown_days": 3,
            "atr_sl_streak_lookback_days": 14,
            "atr_sl_streak_cooldown_days": 7,
            "atr_sl_streak_min_count": 2,
        },
    },
]


def _load_base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["use_market_breadth_gate"] = False
    st["use_type_a"] = True
    st["use_type_b"] = True
    st["use_sel_track_a"] = True
    st["use_sel_track_b"] = True
    st["bt_high_precision"] = False
    st["bt_rt_parity_intrabar"] = False
    st["atr_sl_entry_grace_hours"] = 0.0
    st["atr_sl_entry_grace_mult"] = 0.0
    st["atr_sl_entry_grace_same_day"] = False
    st["gap_down_limit"] = -4.0
    st["gap_down_limit_a"] = -4.0
    st["gap_down_limit_b"] = -4.0
    return st


def _exit_tag(reason: str) -> str:
    r = str(reason or "").split("|", 1)[0].strip().upper()
    for key in ("TAKE_PROFIT", "ATR_SL", "ATR_TRAIL", "FLASH_EXIT", "GAP_DOWN"):
        if key in r:
            return key
    return r[:20] if r else "?"


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
        return {"n_sell": 0}
    df = trades.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    sells = df[df["Action"] == "SELL"].copy()
    buys1 = df[df["Action"] == "BUY1"]
    sells["year"] = sells["Date"].dt.year
    sells["tag"] = sells["Reason"].map(_exit_tag)
    out: Dict[str, Any] = {
        "n_buy1": int(len(buys1)),
        "n_sell": int(len(sells)),
        "win_rate": float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0,
        "sl_all": int((sells["tag"] == "ATR_SL").sum()),
        "tr_all": int((sells["tag"] == "ATR_TRAIL").sum()),
        "gap_all": int((sells["tag"] == "GAP_DOWN").sum()),
    }
    for y in (2024, 2025, 2026):
        s = sells[sells["year"] == y]
        out[f"sell_{y}"] = int(len(s))
        out[f"sl_{y}"] = int((s["tag"] == "ATR_SL").sum())
        out[f"tr_{y}"] = int((s["tag"] == "ATR_TRAIL").sum())
        out[f"gap_{y}"] = int((s["tag"] == "GAP_DOWN").sum())
        out[f"wr_{y}"] = float((s["Return"] > 0).mean() * 100) if len(s) else 0.0
    return out


def run_one(exp: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    rid = exp["id"]
    print("\n" + "=" * 72)
    print(f"[{rid}] {exp['title']}")
    print("=" * 72)
    st = deepcopy(base)
    st.update(exp["overrides"])
    print(
        f"  base_cd={st.get('atr_sl_reentry_cooldown_days')} "
        f"streak={st.get('atr_sl_streak_cooldown_days')} "
        f"lookback={st.get('atr_sl_streak_lookback_days')}"
    )

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
    for name in ("backtest_trades.csv", "backtest_portfolio_curve.csv"):
        src = ROOT / name
        if src.is_file():
            try:
                shutil.copy2(src, dest / name)
            except Exception:
                pass

    eq = _equity_metrics(tester.daily_equity, tester.initial_cash)
    tm = _trade_metrics(trades if isinstance(trades, pd.DataFrame) else None)
    r = {"id": rid, "title": exp["title"], "elapsed_sec": elapsed, **eq, **tm}
    print(
        f"  total={r.get('ret_total_pct', float('nan')):+.2f}% "
        f"24={r.get('ret_2024_pct', float('nan')):+.1f}% "
        f"25={r.get('ret_2025_pct', float('nan')):+.1f}% "
        f"26H1={r.get('ret_2026h1_pct', float('nan')):+.1f}% "
        f"DD={r.get('max_dd_pct', float('nan')):.1f}% sells={r.get('n_sell', 0)}"
    )
    return r


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    print(f"{START} ~ {END} | 저정밀 | breadth OFF | 연속 ATR_SL 쿨다운")
    results: List[Dict[str, Any]] = []
    for exp in EXPERIMENTS:
        try:
            results.append(run_one(exp, base))
        except Exception as e:
            print(f"[FAIL] {exp['id']}: {e}")
            traceback.print_exc()
            results.append({"id": exp["id"], "error": str(e)})

    ok = [r for r in results if "error" not in r]
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    hdr = ["ID", "Total%", "24%", "25%", "26H1%", "MaxDD", "Sells", "SL", "WR%"]
    print("  ".join(f"{h:>8}" for h in hdr))
    for r in ok:
        print(
            "  ".join(
                f"{v:>8}"
                for v in [
                    r["id"],
                    f"{r.get('ret_total_pct', float('nan')):+.1f}",
                    f"{r.get('ret_2024_pct', float('nan')):+.1f}",
                    f"{r.get('ret_2025_pct', float('nan')):+.1f}",
                    f"{r.get('ret_2026h1_pct', float('nan')):+.1f}",
                    f"{r.get('max_dd_pct', float('nan')):.1f}",
                    str(r.get("n_sell", 0)),
                    str(r.get("sl_all", 0)),
                    f"{r.get('win_rate', 0):.1f}",
                ]
            )
        )
    print("\n[전체 수익 순위]")
    for i, r in enumerate(sorted(ok, key=lambda x: x.get("ret_total_pct", -999), reverse=True), 1):
        print(f"  {i}. {r['id']}: {r.get('ret_total_pct', float('nan')):+.2f}% — {r['title']}")
    print("\n[2026H1 순위]")
    for i, r in enumerate(sorted(ok, key=lambda x: x.get("ret_2026h1_pct", -999), reverse=True), 1):
        print(
            f"  {i}. {r['id']}: 26H1={r.get('ret_2026h1_pct', float('nan')):+.2f}% "
            f"total={r.get('ret_total_pct', float('nan')):+.2f}%"
        )
    print("\n[MaxDD 순위(덜 나쁨)]")
    for i, r in enumerate(sorted(ok, key=lambda x: x.get("max_dd_pct", -999), reverse=True), 1):
        print(f"  {i}. {r['id']}: DD={r.get('max_dd_pct', float('nan')):.1f}%")

    (OUT_DIR / "summary.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    if ok:
        pd.DataFrame(ok).to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    print(f"\n저장: {OUT_DIR / 'summary.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
