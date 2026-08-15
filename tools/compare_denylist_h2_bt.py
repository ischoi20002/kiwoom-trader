# -*- coding: utf-8 -*-
"""
H1 상습 -EV 명단을 H2 포트폴리오 백테에 적용해 검증한다.

  python tools/compare_denylist_h2_bt.py
"""
from __future__ import annotations

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
import strategy_selector as ss

importlib.reload(be)
importlib.reload(ss)
importlib.reload(db)

from backtest_engine import settings_for_backtest  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

OUT = ROOT / "tools" / "bt_compare_out_sel_outside_s1"
H1_STATS = OUT / "h1_name_stats.csv"
START = "2026-01-01"
END = "2026-07-31"


def _banned(min_n: int, max_avg: float) -> List[str]:
    """H1 통계에서 제외 코드를 읽는다."""
    if not H1_STATS.is_file():
        raise RuntimeError(f"H1 통계가 없습니다: {H1_STATS}")
    df = pd.read_csv(H1_STATS, encoding="utf-8-sig")
    df["code"] = df["code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    m = (df["n"] >= int(min_n)) & (df["avg_ret"] <= float(max_avg))
    return sorted(df.loc[m, "code"].astype(str).tolist())


def _filter_sched(sched: Any, banned: Set[str]) -> Any:
    """일자→코드 리스트에서 명단을 뺀다."""
    if not isinstance(sched, dict):
        return sched
    out: Dict[str, List[str]] = {}
    for day, codes in sched.items():
        try:
            kept = [str(c).zfill(6) for c in (codes or []) if str(c).zfill(6) not in banned]
        except Exception:
            kept = list(codes or [])
        out[str(day)] = kept
    return out


def _load_base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["universe_index_choice"] = "KOSPI200"
    st["kospi_top_n"] = 200
    st["bt_high_precision"] = False
    st["bt_rt_parity_intrabar"] = False
    st["enforce_track_signal_match"] = True
    return st


def _trade_metrics(trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if trades is None or trades.empty:
        return {"n_buy1": 0, "n_buy2": 0, "n_sell": 0, "win_rate": 0.0, "avg_trade_ret_pct": 0.0}
    buys1 = trades[trades["Action"] == "BUY1"]
    buys2 = trades[trades["Action"] == "BUY2"]
    sells = trades[trades["Action"] == "SELL"]
    return {
        "n_buy1": int(len(buys1)),
        "n_buy2": int(len(buys2)),
        "n_sell": int(len(sells)),
        "win_rate": float((sells["Return"] > 0).mean() * 100.0) if len(sells) else 0.0,
        "avg_trade_ret_pct": float(sells["Return"].mean()) if len(sells) else 0.0,
    }


def _equity_metrics(daily_equity: List[dict], initial: float) -> Dict[str, Any]:
    df = pd.DataFrame(daily_equity)
    if df.empty:
        return {"ret_total_pct": 0.0, "max_dd_pct": 0.0, "end_equity": float(initial)}
    df["date"] = pd.to_datetime(df["date"], format="mixed", errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")
    col = "equity" if "equity" in df.columns else "Total"
    e0 = float(initial)
    e_end = float(df.iloc[-1][col])
    dd = (df[col] - df[col].cummax()) / df[col].cummax()
    return {
        "ret_total_pct": (e_end / e0 - 1.0) * 100.0,
        "max_dd_pct": float(dd.min() * 100.0),
        "end_equity": e_end,
        "profit_krw": e_end - e0,
    }


def run_one(exp_id: str, title: str, banned: List[str], base: Dict[str, Any]) -> Dict[str, Any]:
    """한 실험을 돌린다. banned 가 있으면 선정 스케줄에서 제거."""
    dest = OUT / f"bt_h2_{exp_id}"
    meta_path = dest / "meta.json"
    if meta_path.is_file():
        try:
            cached = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            if cached.get("start") == START and cached.get("end") == END:
                print(f"[{exp_id}] 캐시 재사용")
                return cached
        except Exception:
            pass

    st = deepcopy(base)
    banned_set = {str(c).zfill(6) for c in banned}
    orig = ss.prepare_dynamic_universe

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        result = orig(*args, **kwargs)
        if not banned_set:
            return result
        try:
            if isinstance(result, tuple) and len(result) >= 3:
                sched = _filter_sched(result[0], banned_set)
                sa = _filter_sched(result[1], banned_set)
                sb = _filter_sched(result[2], banned_set)
                rest = list(result[3:])
                return tuple([sched, sa, sb] + rest)
        except Exception:
            traceback.print_exc()
        return result

    print(f"\n[{exp_id}] {title}  banned={len(banned_set)}  {START} ~ {END}")

    def cb(msg: str, val: Any = None) -> None:
        try:
            if str(msg).startswith("[") and "시계열" in str(msg) and val is not None:
                print(f"\r  {float(val):5.1f}%", end="", flush=True)
        except Exception:
            pass

    t0 = time.time()
    ss.prepare_dynamic_universe = _wrapped  # type: ignore[method-assign]
    try:
        tester = DynamicBacktester(st)
        trades = tester.run(START, END, interval="15m", callback=cb, high_precision=False)
    finally:
        ss.prepare_dynamic_universe = orig  # type: ignore[method-assign]
    elapsed = time.time() - t0
    print(f"\n  done {elapsed:.1f}s")

    dest.mkdir(parents=True, exist_ok=True)
    if isinstance(trades, pd.DataFrame) and not trades.empty:
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    try:
        pd.DataFrame(tester.daily_equity).to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")
    except Exception:
        pass

    row: Dict[str, Any] = {
        "id": exp_id,
        "title": title,
        "start": START,
        "end": END,
        "n_banned": int(len(banned_set)),
        "elapsed_sec": round(elapsed, 1),
        **_equity_metrics(getattr(tester, "daily_equity", []) or [], float(tester.initial_cash)),
        **_trade_metrics(trades if isinstance(trades, pd.DataFrame) else None),
    }
    meta_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"  ret={row.get('ret_total_pct', 0):+.2f}%  "
        f"DD={row.get('max_dd_pct', 0):.1f}%  "
        f"BUY1={row.get('n_buy1', 0)}  "
        f"win={row.get('win_rate', 0):.1f}%"
    )
    return row


def main() -> None:
    base = _load_base()
    rows: List[Dict[str, Any]] = []
    rows.append(run_one("BASE", "명단 없음", [], base))
    rows.append(
        run_one("N8_AVG0", "n>=8 평균<0 제외", _banned(8, 0.0), base)
    )
    rows.append(
        run_one("N8_AVG-05", "n>=8 평균<=-0.5 제외", _banned(8, -0.5), base)
    )
    base_row = next((r for r in rows if r.get("id") == "BASE"), None)
    print("\n" + "=" * 80)
    print(f"H2 PORTFOLIO BT  {START} ~ {END}")
    print("=" * 80)
    print(f"  {'ID':12s} {'수익%':>8s} {'Δ':>7s} {'MDD%':>7s} {'BUY1':>6s} {'승률%':>7s} {'평균%':>7s}")
    for r in rows:
        delta = 0.0
        if base_row is not None:
            delta = float(r.get("ret_total_pct", 0)) - float(base_row.get("ret_total_pct", 0))
        print(
            f"  {str(r.get('id')):12s} "
            f"{float(r.get('ret_total_pct', 0)):+8.2f} "
            f"{delta:+7.2f} "
            f"{float(r.get('max_dd_pct', 0)):7.1f} "
            f"{int(r.get('n_buy1', 0)):6d} "
            f"{float(r.get('win_rate', 0)):7.1f} "
            f"{float(r.get('avg_trade_ret_pct', 0)):+7.2f}"
        )
    (OUT / "denylist_h2_bt.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n저장: {OUT / 'denylist_h2_bt.json'}")


if __name__ == "__main__":
    main()
