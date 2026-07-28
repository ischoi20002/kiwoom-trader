# -*- coding: utf-8 -*-
"""
KOSPI200 유니버스 갱신 주기 비교.

기존 MONTHLY/WEEKLY/DAILY meta.json이 있으면 재사용하고,
신규 모드만 실행하려면:
  python tools/compare_k200_univ_refresh.py --only BIWEEKLY,BIMONTHLY,QUARTERLY

전체(캐시 포함 병합):
  python tools/compare_k200_univ_refresh.py
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
from typing import Any, Dict, List, Optional, Set

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

OUT_DIR = ROOT / "tools" / "bt_compare_out_k200_univ_refresh"
OOS_START = "2025-07-01"

EXPERIMENTS = [
    {"id": "MONTHLY", "title": "월별 marcap as-of (현행)", "overrides": {"kospi_universe_refresh": "monthly"}},
    {"id": "BIWEEKLY", "title": "격주 marcap as-of", "overrides": {"kospi_universe_refresh": "biweekly"}},
    {"id": "WEEKLY", "title": "주별 marcap as-of (주 첫 영업일)", "overrides": {"kospi_universe_refresh": "weekly"}},
    {"id": "DAILY", "title": "일별 marcap as-of (전일)", "overrides": {"kospi_universe_refresh": "daily"}},
    {"id": "BIMONTHLY", "title": "2개월 marcap as-of", "overrides": {"kospi_universe_refresh": "bimonthly"}},
    {"id": "QUARTERLY", "title": "분기 marcap as-of", "overrides": {"kospi_universe_refresh": "quarterly"}},
]


def _load_base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["universe_index_choice"] = "KOSPI200"
    st["kospi_top_n"] = 200
    st["bt_high_precision"] = False
    st["bt_rt_parity_intrabar"] = False
    st["enforce_track_signal_match"] = True
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


def _trade_metrics(trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if trades is None or trades.empty:
        return {"n_buy1": 0, "n_buy2": 0, "win_rate": 0.0}
    buys1 = trades[trades["Action"] == "BUY1"]
    buys2 = trades[trades["Action"] == "BUY2"]
    sells = trades[trades["Action"] == "SELL"]
    return {
        "n_buy1": int(len(buys1)),
        "n_buy2": int(len(buys2)),
        "win_rate": float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0,
    }


def run_one(exp: Dict[str, Any], base: Dict[str, Any], start: str, end: str) -> Dict[str, Any]:
    period = "full" if start == str(base.get("bt_start_date")) else "oos"
    run_id = f"{exp['id']}_{period}"
    dest = OUT_DIR / run_id
    meta_path = dest / "meta.json"
    if meta_path.is_file():
        try:
            cached = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            if cached.get("start") == start and cached.get("end") == end:
                print(f"[{run_id}] 캐시 재사용")
                return cached
        except Exception:
            pass

    print(f"\n[{run_id}] {exp['title']}  {start} ~ {end}")
    st = deepcopy(base)
    st.update(exp["overrides"])

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
    if isinstance(trades, pd.DataFrame) and not trades.empty:
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(tester.daily_equity).to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")

    row = {
        "id": exp["id"],
        "title": exp["title"],
        "period": period,
        "start": start,
        "end": end,
        "refresh": exp["overrides"].get("kospi_universe_refresh"),
        "elapsed_sec": round(elapsed, 1),
        **_equity_metrics(tester.daily_equity, tester.initial_cash),
        **_trade_metrics(trades if isinstance(trades, pd.DataFrame) else None),
    }
    meta_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"  total={row.get('ret_total_pct', float('nan')):+.2f}% "
        f"DD={row.get('max_dd_pct', float('nan')):.1f}% "
        f"BUY1={row.get('n_buy1', 0)}"
    )
    return row


def _parse_only(raw: Optional[str]) -> Optional[Set[str]]:
    if not raw:
        return None
    ids = {x.strip().upper() for x in str(raw).split(",") if x.strip()}
    return ids or None


def main() -> None:
    ap = argparse.ArgumentParser(description="KOSPI200 유니버스 갱신 주기 BT 비교")
    ap.add_argument(
        "--only",
        default=None,
        help="실행할 실험 ID만 (쉼표 구분). 예: BIWEEKLY,BIMONTHLY,QUARTERLY",
    )
    args = ap.parse_args()
    only = _parse_only(args.only)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    start = str(base.get("bt_start_date", "2024-06-03"))
    end = str(base.get("bt_end_date", "2026-07-27"))

    exps = [e for e in EXPERIMENTS if only is None or e["id"] in only]
    if only is not None:
        unknown = only - {e["id"] for e in EXPERIMENTS}
        if unknown:
            print(f"[warn] 알 수 없는 id: {sorted(unknown)}")
        print(f"[only] 실행 대상: {[e['id'] for e in exps]}")

    rows: List[Dict[str, Any]] = []
    for exp in exps:
        rows.append(run_one(exp, base, start, end))
    for exp in exps:
        rows.append(run_one(exp, base, OOS_START, end))

    # --only 시에도 기존 meta를 모아 전체 summary 유지
    if only is not None:
        by_key: Dict[tuple, Dict[str, Any]] = {}
        for exp in EXPERIMENTS:
            for period, s in (("full", start), ("oos", OOS_START)):
                meta_path = OUT_DIR / f"{exp['id']}_{period}" / "meta.json"
                if not meta_path.is_file():
                    continue
                try:
                    cached = json.loads(meta_path.read_text(encoding="utf-8-sig"))
                    if cached.get("start") == s and cached.get("end") == end:
                        by_key[(exp["id"], period)] = cached
                except Exception:
                    pass
        for r in rows:
            if r and not r.get("error"):
                by_key[(r["id"], r["period"])] = r
        rows = list(by_key.values())
        # EXPERIMENTS 순서 유지
        order = {(e["id"], p): i for i, e in enumerate(EXPERIMENTS) for p in ("full", "oos")}
        rows.sort(key=lambda r: order.get((r.get("id"), r.get("period")), 999))

    (OUT_DIR / "summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n=== SUMMARY ===")
    for period in ("full", "oos"):
        print(f"\n-- {period.upper()} --")
        sub = [r for r in rows if r.get("period") == period and "ret_total_pct" in r]
        for r in sub:
            print(
                f"  {r['id']:10s} {float(r['ret_total_pct']):+8.2f}% "
                f"DD={float(r['max_dd_pct']):6.1f}% "
                f"BUY1={int(r['n_buy1']):4d} "
                f"({r.get('elapsed_sec', 0):.0f}s)"
            )
    print(f"\n저장: {OUT_DIR / 'summary.json'}")


if __name__ == "__main__":
    main()
