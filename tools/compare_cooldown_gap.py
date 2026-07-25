# -*- coding: utf-8 -*-
"""
쿨다운 일수 × 갭다운 한도 2축 비교.

기준 스택: 현행 settings(Type A/B, SL×2.8, TP6%, breadth OFF, 저정밀)
축1 atr_sl_reentry_cooldown_days: 2 / 3 / 5
축2 gap_down_limit_a/b: -3.0 / -4.0(현재) / -5.0

기간: 2024-06-03 ~ 2026-06-30
평가: 총수익 + 2026H1 + MaxDD (+ 갭 청산 건수)

실행:
  python tools/compare_cooldown_gap.py
"""
from __future__ import annotations

import json
import os
import shutil
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
OUT_DIR = ROOT / "tools" / "bt_compare_out4"
HIGH_PRECISION = False

COOLDOWN_DAYS = (2, 3, 5)
GAP_LIMITS = (-3.0, -4.0, -5.0)


def _build_experiments() -> List[Dict[str, Any]]:
    exps: List[Dict[str, Any]] = []
    for cd, gap in product(COOLDOWN_DAYS, GAP_LIMITS):
        gtag = str(int(abs(gap)))  # 3 / 4 / 5
        rid = f"CD{cd}_G{gtag}"
        exps.append(
            {
                "id": rid,
                "title": f"쿨다운 {cd}일 · 갭 {gap:.0f}%",
                "cooldown_days": int(cd),
                "gap_limit": float(gap),
                "overrides": {
                    "atr_sl_reentry_cooldown_days": int(cd),
                    "gap_down_limit": float(gap),
                    "gap_down_limit_a": float(gap),
                    "gap_down_limit_b": float(gap),
                },
            }
        )
    return exps


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
    # 유예는 이번 축에서 제외(이전 비교에서 손해)
    st["atr_sl_entry_grace_hours"] = 0.0
    st["atr_sl_entry_grace_mult"] = 0.0
    st["atr_sl_entry_grace_same_day"] = False
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
        "gap_all": int((sells["tag"] == "GAP_DOWN").sum()),
        "sl_all": int((sells["tag"] == "ATR_SL").sum()),
        "tr_all": int((sells["tag"] == "ATR_TRAIL").sum()),
    }
    if "Sig_Type" in buys1.columns:
        out["buy_a"] = int((buys1["Sig_Type"] == "TYPE_A").sum())
        out["buy_b"] = int((buys1["Sig_Type"] == "TYPE_B").sum())
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
        f"  cooldown={st.get('atr_sl_reentry_cooldown_days')} "
        f"gap_a={st.get('gap_down_limit_a')} gap_b={st.get('gap_down_limit_b')}"
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
    r: Dict[str, Any] = {
        "id": rid,
        "title": exp["title"],
        "cooldown_days": exp["cooldown_days"],
        "gap_limit": exp["gap_limit"],
        "elapsed_sec": elapsed,
        **eq,
        **tm,
    }
    print(
        f"  total={r.get('ret_total_pct', float('nan')):+.2f}% "
        f"24={r.get('ret_2024_pct', float('nan')):+.1f}% "
        f"25={r.get('ret_2025_pct', float('nan')):+.1f}% "
        f"26H1={r.get('ret_2026h1_pct', float('nan')):+.1f}% "
        f"DD={r.get('max_dd_pct', float('nan')):.1f}% "
        f"gap26={r.get('gap_2026', 0)} gap_all={r.get('gap_all', 0)}"
    )
    return r


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    experiments = _build_experiments()
    base = _load_base()
    print(f"{START} ~ {END} | 저정밀 | breadth OFF | {len(experiments)} runs")
    print(f"cooldown={list(COOLDOWN_DAYS)}  gap={list(GAP_LIMITS)}")
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
    print("SUMMARY (쿨다운 × 갭)")
    print("=" * 72)
    hdr = [
        "ID",
        "CD",
        "Gap",
        "Total%",
        "24%",
        "25%",
        "26H1%",
        "MaxDD",
        "Gap26",
        "GapAll",
        "Sells",
        "WR%",
    ]
    print("  ".join(f"{h:>8}" for h in hdr))
    for r in ok:
        print(
            "  ".join(
                f"{v:>8}"
                for v in [
                    r["id"],
                    str(r.get("cooldown_days", "")),
                    f"{r.get('gap_limit', float('nan')):.0f}",
                    f"{r.get('ret_total_pct', float('nan')):+.1f}",
                    f"{r.get('ret_2024_pct', float('nan')):+.1f}",
                    f"{r.get('ret_2025_pct', float('nan')):+.1f}",
                    f"{r.get('ret_2026h1_pct', float('nan')):+.1f}",
                    f"{r.get('max_dd_pct', float('nan')):.1f}",
                    str(r.get("gap_2026", 0)),
                    str(r.get("gap_all", 0)),
                    str(r.get("n_sell", 0)),
                    f"{r.get('win_rate', 0):.1f}",
                ]
            )
        )

    print("\n[전체 수익 순위]")
    ranked_total = sorted(ok, key=lambda x: x.get("ret_total_pct", -999), reverse=True)
    for i, r in enumerate(ranked_total, 1):
        print(
            f"  {i}. {r['id']}: total={r.get('ret_total_pct', float('nan')):+.2f}% "
            f"26H1={r.get('ret_2026h1_pct', float('nan')):+.2f}% "
            f"DD={r.get('max_dd_pct', float('nan')):.1f}% — {r['title']}"
        )

    print("\n[2026H1 순위]")
    ranked_26 = sorted(ok, key=lambda x: x.get("ret_2026h1_pct", -999), reverse=True)
    for i, r in enumerate(ranked_26, 1):
        print(
            f"  {i}. {r['id']}: 26H1={r.get('ret_2026h1_pct', float('nan')):+.2f}% "
            f"total={r.get('ret_total_pct', float('nan')):+.2f}% "
            f"gap26={r.get('gap_2026', 0)}"
        )

    print("\n[MaxDD 순위(덜 나쁨)]")
    ranked_dd = sorted(ok, key=lambda x: x.get("max_dd_pct", -999), reverse=True)
    for i, r in enumerate(ranked_dd, 1):
        print(
            f"  {i}. {r['id']}: DD={r.get('max_dd_pct', float('nan')):.1f}% "
            f"total={r.get('ret_total_pct', float('nan')):+.2f}%"
        )

    # 피벗: Total% (행=쿨다운, 열=갭)
    if ok:
        try:
            pdf = pd.DataFrame(ok)
            piv_t = pdf.pivot(index="cooldown_days", columns="gap_limit", values="ret_total_pct")
            piv_26 = pdf.pivot(index="cooldown_days", columns="gap_limit", values="ret_2026h1_pct")
            piv_dd = pdf.pivot(index="cooldown_days", columns="gap_limit", values="max_dd_pct")
            print("\n[피벗 Total%]")
            print(piv_t.round(2).to_string())
            print("\n[피벗 2026H1%]")
            print(piv_26.round(2).to_string())
            print("\n[피벗 MaxDD%]")
            print(piv_dd.round(2).to_string())
            piv_t.to_csv(OUT_DIR / "pivot_total.csv", encoding="utf-8-sig")
            piv_26.to_csv(OUT_DIR / "pivot_2026h1.csv", encoding="utf-8-sig")
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
