# -*- coding: utf-8 -*-
"""
S1 품질 필터(A: RSI갭+MA20/MA60, B: ATR%+RSI하한) BASE vs NEW 비교.

  python tools/compare_s1_quality_filters.py
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
import strategy.signals as sigmod

importlib.reload(be)
importlib.reload(db)
importlib.reload(ss)
importlib.reload(sigmod)

from backtest_engine import settings_for_backtest  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

OUT_DIR = ROOT / "tools" / "bt_compare_out_s1_quality"
OOS_START = "2025-07-01"
FULL_START = "2024-06-03"
FULL_END = "2026-07-31"

QUALITY_ON: Dict[str, Any] = {
    "use_s1_quality_a": True,
    "s1_a_rsi_sig_gap_max": 16.0,
    "s1_a_ma20_ma60_min": 0.997,
    "use_s1_quality_b": True,
    "s1_b_atr_pct_max": 0.96,
    "s1_b_rsi_floor": 66.5,
}

EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "id": "BASE",
        "title": "품질필터 OFF",
        "overrides": {
            "use_s1_quality_a": False,
            "use_s1_quality_b": False,
        },
    },
    {
        "id": "QUAL",
        "title": "품질필터 A+B ON",
        "overrides": dict(QUALITY_ON),
    },
    {
        "id": "QUAL_A",
        "title": "품질필터 A만",
        "overrides": {
            "use_s1_quality_a": True,
            "s1_a_rsi_sig_gap_max": 16.0,
            "s1_a_ma20_ma60_min": 0.997,
            "use_s1_quality_b": False,
        },
    },
    {
        "id": "QUAL_B",
        "title": "품질필터 B만",
        "overrides": {
            "use_s1_quality_a": False,
            "use_s1_quality_b": True,
            "s1_b_atr_pct_max": 0.96,
            "s1_b_rsi_floor": 66.5,
        },
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
    st["use_transition_gate"] = True
    st["use_s1_quality_a"] = False
    st["use_s1_quality_b"] = False
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
    e0 = float(initial)
    e_end = float(daily.iloc[-1][col])
    dd = (daily[col] - daily[col].cummax()) / daily[col].cummax()
    return {
        "ret_total_pct": (e_end / e0 - 1) * 100,
        "end_equity": e_end,
        "profit_krw": e_end - e0,
        "max_dd_pct": float(dd.min() * 100),
    }


def _pair_s1(trades: pd.DataFrame) -> pd.DataFrame:
    df = trades.copy()
    df["Date"] = pd.to_datetime(df["Date"], format="mixed", errors="coerce")
    df["Code"] = df["Code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    df = df.dropna(subset=["Date"]).sort_values(["Code", "Date"])
    rows: List[dict] = []
    for code, g in df.groupby("Code", sort=False):
        open_buy = None
        for _, r in g.iterrows():
            act = str(r.get("Action", ""))
            if act == "BUY1":
                open_buy = r
            elif act == "SELL" and open_buy is not None:
                try:
                    ret = float(r.get("Return", float("nan")))
                except Exception:
                    ret = float("nan")
                rows.append(
                    {
                        "sig_type": str(open_buy.get("Sig_Type", "")).upper(),
                        "ret_pct": ret,
                    }
                )
                open_buy = None
    return pd.DataFrame(rows)


def _trade_metrics(trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if trades is None or trades.empty:
        return {"n_buy1": 0, "n_buy2": 0, "win_rate": 0.0, "s1_avg_ret": 0.0, "s1_by_type": {}}
    buys1 = trades[trades["Action"] == "BUY1"]
    buys2 = trades[trades["Action"] == "BUY2"]
    sells = trades[trades["Action"] == "SELL"]
    out: Dict[str, Any] = {
        "n_buy1": int(len(buys1)),
        "n_buy2": int(len(buys2)),
        "win_rate": float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0,
        "s1_avg_ret": 0.0,
        "s1_by_type": {},
    }
    try:
        rounds = _pair_s1(trades)
        if not rounds.empty:
            out["s1_avg_ret"] = float(rounds["ret_pct"].mean())
            out["s1_win_rate"] = float((rounds["ret_pct"] > 0).mean() * 100)
            for sig, g in rounds.groupby("sig_type"):
                out["s1_by_type"][str(sig)] = {
                    "n": int(len(g)),
                    "avg_ret": float(g["ret_pct"].mean()),
                    "win_rate": float((g["ret_pct"] > 0).mean() * 100),
                }
    except Exception as e:
        out["s1_err"] = str(e)
    return out


def run_one(exp: Dict[str, Any], base: Dict[str, Any], start: str, end: str) -> Dict[str, Any]:
    period = "full" if start == FULL_START else "oos"
    run_id = f"{exp['id']}_{period}"
    dest = OUT_DIR / run_id
    meta_path = dest / "meta.json"
    if meta_path.is_file():
        try:
            cached = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            if cached.get("start") == start and cached.get("end") == end and not cached.get("error"):
                print(f"[{run_id}] 캐시 재사용")
                return cached
        except Exception:
            pass

    print(f"\n[{run_id}] {exp['title']}  {start} ~ {end}")
    st = deepcopy(base)
    st.update(exp["overrides"])
    st["bt_start_date"] = start
    st["bt_end_date"] = end

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
    initial = float(
        st.get("backtest_initial_capital", st.get("initial_capital", 10_000_000))
    )
    eq_m: Dict[str, Any] = {}
    try:
        if hasattr(tester, "daily_equity") and tester.daily_equity:
            eq_m = _equity_metrics(tester.daily_equity, initial)
            pd.DataFrame(tester.daily_equity).to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")
    except Exception as e:
        print(f"  [WARN] equity: {e}")

    tm = _trade_metrics(trades if isinstance(trades, pd.DataFrame) else None)
    if isinstance(trades, pd.DataFrame) and not trades.empty:
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")

    meta = {
        "id": exp["id"],
        "title": exp["title"],
        "period": period,
        "start": start,
        "end": end,
        "elapsed_s": round(elapsed, 1),
        "overrides": exp["overrides"],
        **eq_m,
        **tm,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"  ret={meta.get('ret_total_pct', float('nan')):+.2f}% "
        f"maxDD={meta.get('max_dd_pct', float('nan')):.2f}% "
        f"BUY1={meta.get('n_buy1')} S1avg={meta.get('s1_avg_ret', 0):+.2f}% "
        f"S1wr={meta.get('s1_win_rate', 0):.1f}%"
    )
    return meta


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    rows: List[Dict[str, Any]] = []
    for exp in EXPERIMENTS:
        for start, end in ((FULL_START, FULL_END), (OOS_START, FULL_END)):
            rows.append(run_one(exp, base, start, end))

    (OUT_DIR / "summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    print("\n" + "=" * 78)
    print(f"{'ID':<10} {'Period':<5} {'Ret%':>8} {'MaxDD%':>8} {'BUY1':>6} {'S1avg':>7} {'S1wr%':>6}")
    print("-" * 78)
    for r in rows:
        if r.get("error"):
            print(f"{r.get('id'):<10} {r.get('period'):<5} ERROR")
            continue
        print(
            f"{r.get('id',''):<10} {r.get('period',''):<5} "
            f"{r.get('ret_total_pct', float('nan')):+8.2f} "
            f"{r.get('max_dd_pct', float('nan')):8.2f} "
            f"{r.get('n_buy1', 0):6d} "
            f"{r.get('s1_avg_ret', 0):+7.2f} "
            f"{r.get('s1_win_rate', 0):6.1f}"
        )

    # 간단 판정: OOS 수익·S1평균 기준
    by = {(r["id"], r["period"]): r for r in rows if not r.get("error")}
    base_oos = by.get(("BASE", "oos"), {})
    lines = ["# S1 quality filter judgment", ""]
    for eid in ("QUAL", "QUAL_A", "QUAL_B"):
        oos = by.get((eid, "oos"), {})
        full = by.get((eid, "full"), {})
        if not oos or not base_oos:
            continue
        d_ret = float(oos.get("ret_total_pct", 0)) - float(base_oos.get("ret_total_pct", 0))
        d_s1 = float(oos.get("s1_avg_ret", 0)) - float(base_oos.get("s1_avg_ret", 0))
        d_dd = float(oos.get("max_dd_pct", 0)) - float(base_oos.get("max_dd_pct", 0))
        ok = d_ret > 0 or d_s1 > 0.1
        lines.append(
            f"- **{eid}** OOS Δret={d_ret:+.2f}%p ΔS1avg={d_s1:+.2f}%p ΔmaxDD={d_dd:+.2f}%p "
            f"| full ret={full.get('ret_total_pct', float('nan')):+.2f}% → "
            f"{'PASS' if ok else 'WEAK'}"
        )
    judge = OUT_DIR / "judgment.md"
    judge.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[완료] {OUT_DIR / 'summary.json'}")
    print(f"[판정] {judge}")
    print(judge.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
