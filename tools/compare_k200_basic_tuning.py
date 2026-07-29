# -*- coding: utf-8 -*-
"""
현재 settings.json + KOSPI200 기준 기본 튜닝 비교.

목표:
- 기존 K200 baseline 대비 과도한 탐색 없이
  점수모드 / 전환게이트 / B 갭다운의 보수적 후보만 비교

  python tools/compare_k200_basic_tuning.py
  python tools/compare_k200_basic_tuning.py --only BASE,SCORE_ALL_ALIGN
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

OUT_DIR = ROOT / "tools" / "bt_compare_out_k200_basic_tuning"
HIGH_PRECISION = False
OOS_START = "2025-07-01"


def _load_b_signs() -> Dict[str, float]:
    path = ROOT / "tools" / "bt_compare_out_s1_pnl_rank" / "s1_pnl_signs.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        signs = data.get("signs") or {}
        return {str(k): float(v) for k, v in signs.items()}
    except Exception:
        return {}


def _load_base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["universe_index_choice"] = "KOSPI200"
    st["kospi_top_n"] = 200
    st["bt_high_precision"] = False
    st["bt_rt_parity_intrabar"] = False
    st["enforce_track_signal_match"] = True
    return st


def _experiments(base: Dict[str, Any]) -> List[Dict[str, Any]]:
    signs = _load_b_signs()
    return [
        {
            "id": "BASE",
            "title": "현재 설정 baseline",
            "overrides": {},
        },
        {
            "id": "SCORE_B_ALIGN",
            "title": "Type B만 pnl aligned score",
            "overrides": {
                "sel_score_mode": "b_s1_pnl_aligned_v1",
                "sel_s1_pnl_signs": signs,
            },
        },
        {
            "id": "SCORE_ALL_ALIGN",
            "title": "A/B 공통 pnl aligned score",
            "overrides": {
                "sel_score_mode": "s1_pnl_aligned_v1",
                "sel_s1_pnl_signs": signs,
            },
        },
        {
            "id": "B_GAP3",
            "title": "Type B gap down -3.0",
            "overrides": {
                "gap_down_limit_b": -3.0,
            },
        },
        {
            "id": "TRANS_SOFT8_NS2",
            "title": "soft8 유지, soft 구간 S2 허용",
            "overrides": {
                "use_transition_gate": True,
                "transition_prior_share_min": 50.0,
                "transition_share_delta5_max": -20.0,
                "transition_share_delta3_max": -10.0,
                "transition_rvol_min": 35.0,
                "transition_xs_ret_std_min": 3.5,
                "transition_soft_slots_a": 8,
                "transition_soft_slots_b": 8,
                "transition_block_s1_on_soft": False,
                "transition_block_s2_on_soft": False,
                "transition_ignore_soft": False,
            },
        },
        {
            "id": "ALIGN_B_GAP3",
            "title": "A/B aligned score + Type B gap -3.0",
            "overrides": {
                "sel_score_mode": "s1_pnl_aligned_v1",
                "sel_s1_pnl_signs": signs,
                "gap_down_limit_b": -3.0,
            },
        },
    ]


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
        "profit_krw": e_end - e0,
        "max_dd_pct": float(dd.min() * 100),
    }


def _trade_metrics(trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if trades is None or trades.empty:
        return {
            "n_sell": 0,
            "n_buy1": 0,
            "n_buy2": 0,
            "win_rate": 0.0,
            "avg_trade_ret_pct": 0.0,
        }
    df = trades.copy()
    buys1 = df[df["Action"] == "BUY1"]
    buys2 = df[df["Action"] == "BUY2"]
    sells = df[df["Action"] == "SELL"]
    return {
        "n_buy1": int(len(buys1)),
        "n_buy2": int(len(buys2)),
        "n_sell": int(len(sells)),
        "win_rate": float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0,
        "avg_trade_ret_pct": float(sells["Return"].mean()) if len(sells) else 0.0,
        "s2_rate_pct": float(len(buys2) / len(buys1) * 100) if len(buys1) else 0.0,
    }


def run_one(exp: Dict[str, Any], base: Dict[str, Any], start: str, end: str) -> Dict[str, Any]:
    rid = exp["id"]
    period = "full" if start == str(base.get("bt_start_date")) else "oos"
    run_key = f"{rid}_{period}"
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
    print(f"[{run_key}] {exp['title']}  {start} ~ {end}")
    print("=" * 72)

    st = deepcopy(base)
    st.update(exp["overrides"])

    def cb(msg: str, val: Any = None) -> None:
        try:
            if str(msg).startswith("[") and "시계열" in str(msg) and val is not None:
                print(
                    f"\r  {str(msg).split(']', 1)[0]}] {float(val):5.1f}%",
                    end="",
                    flush=True,
                )
        except Exception:
            pass

    t0 = time.time()
    try:
        tester = DynamicBacktester(st)
        trades = tester.run(
            start,
            end,
            interval="15m",
            callback=cb,
            high_precision=HIGH_PRECISION,
        )
    except Exception:
        traceback.print_exc()
        return {"id": rid, "period": period, "start": start, "end": end, "error": True}

    elapsed = time.time() - t0
    print(f"\n  done {elapsed:.1f}s")

    dest.mkdir(parents=True, exist_ok=True)
    if isinstance(trades, pd.DataFrame) and not trades.empty:
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(tester.daily_equity).to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")

    eq = _equity_metrics(tester.daily_equity, tester.initial_cash)
    tm = _trade_metrics(trades if isinstance(trades, pd.DataFrame) else None)
    row: Dict[str, Any] = {
        "id": rid,
        "title": exp["title"],
        "period": period,
        "start": start,
        "end": end,
        "elapsed_sec": round(elapsed, 1),
        **eq,
        **tm,
    }
    meta_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"  total={row.get('ret_total_pct', float('nan')):+.2f}% "
        f"({row.get('profit_krw', 0) / 1e4:+.0f}만) "
        f"DD={row.get('max_dd_pct', float('nan')):.1f}% "
        f"BUY1={row.get('n_buy1', 0)} "
        f"win={row.get('win_rate', 0):.1f}%"
    )
    return row


def _print_summary(rows: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 100)
    print("K200 기본 튜닝 비교 요약")
    print("=" * 100)
    for period in ("full", "oos"):
        sub = [r for r in rows if r.get("period") == period and "ret_total_pct" in r]
        if not sub:
            continue
        base = next((r for r in sub if r.get("id") == "BASE"), None)
        print(f"\n--- {period.upper()} ---")
        print(
            f"  {'ID':18s} {'수익%':>9s} {'ΔBASE':>8s} {'만원':>8s} "
            f"{'MDD%':>8s} {'BUY1':>6s} {'BUY2':>6s} {'승률%':>7s}"
        )
        for r in sorted(sub, key=lambda x: float(x.get("ret_total_pct", -999)), reverse=True):
            delta = 0.0
            if base is not None:
                delta = float(r.get("ret_total_pct", 0)) - float(base.get("ret_total_pct", 0))
            print(
                f"  {str(r.get('id')):18s} "
                f"{float(r.get('ret_total_pct', 0)):+9.2f} "
                f"{delta:+8.2f} "
                f"{float(r.get('profit_krw', 0)) / 1e4:+8.0f} "
                f"{float(r.get('max_dd_pct', 0)):8.1f} "
                f"{int(r.get('n_buy1', 0)):6d} "
                f"{int(r.get('n_buy2', 0)):6d} "
                f"{float(r.get('win_rate', 0)):7.1f}"
            )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="쉼표 구분 id")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    experiments = _experiments(base)
    if args.only.strip():
        allow = {x.strip().upper() for x in args.only.split(",") if x.strip()}
        experiments = [e for e in experiments if e["id"] in allow]

    start_full = str(base.get("bt_start_date", "2024-06-03"))
    end_full = str(base.get("bt_end_date", "2026-07-27"))
    rows: List[Dict[str, Any]] = []
    for exp in experiments:
        rows.append(run_one(exp, base, start_full, end_full))
    for exp in experiments:
        rows.append(run_one(exp, base, OOS_START, end_full))

    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_summary(rows)
    print(f"\n저장: {summary_path}")


if __name__ == "__main__":
    main()
