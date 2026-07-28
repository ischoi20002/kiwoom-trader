# -*- coding: utf-8 -*-
"""
현재 settings.json 기준 KOSPI 시총 상위 N (150/200/250/300) 유니버스 백테 비교.

  python tools/compare_kospi_univ_ladder.py
  python tools/compare_kospi_univ_ladder.py --only K150,K200
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

OUT_DIR = ROOT / "tools" / "bt_compare_out_kospi_univ_ladder"
HIGH_PRECISION = False

UNIVERSE_SIZES = (150, 200, 250, 300)

PERIODS: List[Dict[str, str]] = [
    {"id": "full", "start": "", "end": ""},  # settings bt_start/end
    {"id": "oos", "start": "2025-07-01", "end": ""},
]


def _load_base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["universe_index_choice"] = "KOSPI200"
    st["bt_high_precision"] = False
    st["bt_rt_parity_intrabar"] = False
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
    return {
        "ret_2024_pct": (e24 / e0 - 1) * 100,
        "ret_2025_pct": (e25 / e24 - 1) * 100 if e24 else 0.0,
        "ret_2026_ytd_pct": (e_end / e25 - 1) * 100 if e25 else 0.0,
        "ret_total_pct": (e_end / e0 - 1) * 100,
        "end_equity": e_end,
        "max_dd_pct": float(dd.min() * 100),
        "profit_krw": e_end - e0,
    }


def _trade_metrics(trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if trades is None or trades.empty:
        return {
            "n_sell": 0,
            "n_buy1": 0,
            "n_buy2": 0,
            "win_rate": 0.0,
            "avg_trade_ret_pct": 0.0,
            "sl_all": 0,
        }
    df = trades.copy()
    buys1 = df[df["Action"] == "BUY1"]
    buys2 = df[df["Action"] == "BUY2"]
    sells = df[df["Action"] == "SELL"].copy()
    sells["Date"] = pd.to_datetime(sells["Date"], errors="coerce")
    sells["tag"] = sells["Reason"].map(_exit_tag)
    return {
        "n_buy1": int(len(buys1)),
        "n_buy2": int(len(buys2)),
        "n_sell": int(len(sells)),
        "win_rate": float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0,
        "avg_trade_ret_pct": float(sells["Return"].mean()) if len(sells) else 0.0,
        "sl_all": int((sells["tag"] == "ATR_SL").sum()),
        "s2_rate_pct": float(len(buys2) / len(buys1) * 100) if len(buys1) else 0.0,
    }


def run_one(
    top_n: int,
    base: Dict[str, Any],
    period: Dict[str, str],
) -> Dict[str, Any]:
    rid = f"K{int(top_n)}"
    start = period["start"] or str(base.get("bt_start_date", "2024-06-03"))
    end = period["end"] or str(base.get("bt_end_date", "2026-06-30"))
    period_id = period["id"]
    run_key = f"{rid}_{period_id}"
    dest = OUT_DIR / run_key
    meta_path = dest / "meta.json"
    if meta_path.is_file():
        try:
            cached = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            if cached.get("start") == start and cached.get("end") == end:
                print(f"[{run_key}] 캐시 재사용")
                return cached
        except Exception:
            pass

    print("\n" + "=" * 72)
    print(f"[{run_key}] KOSPI top{top_n}  {start} ~ {end}")
    print("=" * 72)
    st = deepcopy(base)
    st["kospi_top_n"] = int(top_n)

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
        return {"id": run_key, "kospi_top_n": top_n, "period": period_id, "error": True}
    elapsed = time.time() - t0
    print(f"\n  done {elapsed:.1f}s")

    dest.mkdir(parents=True, exist_ok=True)
    if trades is not None and not trades.empty:
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(tester.daily_equity).to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")

    eq = _equity_metrics(tester.daily_equity, tester.initial_cash)
    tm = _trade_metrics(trades if isinstance(trades, pd.DataFrame) else None)
    r: Dict[str, Any] = {
        "id": run_key,
        "kospi_top_n": int(top_n),
        "period": period_id,
        "start": start,
        "end": end,
        "elapsed_sec": round(elapsed, 1),
        "transition_gate": bool(st.get("use_transition_gate")),
        **eq,
        **tm,
    }
    meta_path.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"  total={r.get('ret_total_pct', float('nan')):+.2f}% "
        f"({r.get('profit_krw', 0)/1e4:+.0f}만) "
        f"DD={r.get('max_dd_pct', float('nan')):.1f}% "
        f"BUY1={r.get('n_buy1', 0)} win={r.get('win_rate', 0):.1f}%"
    )
    return r


def _print_summary(rows: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 88)
    print("KOSPI 유니버스 크기 비교 요약")
    print("=" * 88)
    for period_id in ("full", "oos"):
        sub = [r for r in rows if r.get("period") == period_id and "ret_total_pct" in r]
        if not sub:
            continue
        sub.sort(key=lambda x: int(x.get("kospi_top_n", 0)))
        base_n = 150
        base_ret = next(
            (float(r["ret_total_pct"]) for r in sub if int(r.get("kospi_top_n", 0)) == base_n),
            None,
        )
        start = sub[0].get("start", "")
        end = sub[0].get("end", "")
        print(f"\n--- {period_id.upper()} ({start} ~ {end}) ---")
        print(
            f"  {'N':>5s} {'수익%':>9s} {'Δ150':>8s} {'만원':>8s} {'MDD%':>8s} "
            f"{'BUY1':>6s} {'BUY2':>6s} {'승률%':>7s} {'평균거래%':>9s} {'SL':>5s}"
        )
        for r in sub:
            n = int(r.get("kospi_top_n", 0))
            ret = float(r.get("ret_total_pct", 0))
            delta = ret - base_ret if base_ret is not None else 0.0
            print(
                f"  {n:5d} {ret:+9.2f} {delta:+8.2f} "
                f"{float(r.get('profit_krw', 0))/1e4:+8.0f} "
                f"{float(r.get('max_dd_pct', 0)):8.1f} "
                f"{int(r.get('n_buy1', 0)):6d} {int(r.get('n_buy2', 0)):6d} "
                f"{float(r.get('win_rate', 0)):7.1f} "
                f"{float(r.get('avg_trade_ret_pct', 0)):9.3f} "
                f"{int(r.get('sl_all', 0)):5d}"
            )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="K150,K200,... 쉼표 구분")
    args = ap.parse_args()

    base = _load_base()
    only: Optional[set[int]] = None
    if args.only.strip():
        only = {int(x.strip().lstrip("Kk")) for x in args.only.split(",") if x.strip()}

    sizes = [n for n in UNIVERSE_SIZES if only is None or n in only]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(
        f"기준 설정: transition_gate={base.get('use_transition_gate')} "
        f"kospi_top_n(현재)={base.get('kospi_top_n')} "
        f"S2={base.get('use_s2_master_a')}/{base.get('use_s2_master_b')}"
    )

    rows: List[Dict[str, Any]] = []
    for period in PERIODS:
        for n in sizes:
            rows.append(run_one(n, base, period))

    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_summary(rows)
    print(f"\n저장: {summary_path}")


if __name__ == "__main__":
    main()
