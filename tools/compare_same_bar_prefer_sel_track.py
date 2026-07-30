# -*- coding: utf-8 -*-
"""
KOSPI200 · 동시신호(A+B) 봉에서 **선정 트랙 우선 확정** 효과 비교.

배경:
  ``S1_Simultaneous_AB`` 봉은 Type A·B 신호가 모두 성립한 봉이다. 그런데
  ``effective_s1_sig_type_for_buy`` 는 트랙을 모른 채 우선순위(same_bar_sig_priority)와
  보유 동수 타이브레이크(n_a == n_b → TYPE_B)로 타입을 하나 확정하고,
  그 뒤 ``track_signal_match_allows`` 가 트랙과 다르면 진입을 폐기한다.
  → 트랙의 신호가 실제로 켜졌는데도 버려지는 기회 손실이 발생한다.

축:
  - same_bar_sig_prefer_sel_track (OFF=현행 / ON=트랙 우선)
  - same_bar_sig_priority (prefer_a / confluence / prefer_b) 와의 상호작용

  python tools/compare_same_bar_prefer_sel_track.py
"""
from __future__ import annotations

import importlib
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

import backtest_engine as be
import dynamic_backtest as db

importlib.reload(be)
importlib.reload(db)

from backtest_engine import settings_for_backtest  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

OUT_DIR = ROOT / "tools" / "bt_compare_out_same_bar_track"
OOS_START = "2025-07-01"

EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "id": "BASE",
        "title": "현행 (트랙 우선 OFF · priority=현재값)",
        "overrides": {"same_bar_sig_prefer_sel_track": False},
    },
    {
        "id": "TRACK_PREF",
        "title": "트랙 우선 ON (priority=현재값)",
        "overrides": {"same_bar_sig_prefer_sel_track": True},
    },
    {
        "id": "BASE_CONF",
        "title": "트랙 우선 OFF · priority=confluence",
        "overrides": {
            "same_bar_sig_prefer_sel_track": False,
            "same_bar_sig_priority": "confluence",
        },
    },
    {
        "id": "TRACK_PREF_CONF",
        "title": "트랙 우선 ON · priority=confluence",
        "overrides": {
            "same_bar_sig_prefer_sel_track": True,
            "same_bar_sig_priority": "confluence",
        },
    },
    {
        "id": "BASE_PREF_B",
        "title": "트랙 우선 OFF · priority=prefer_b",
        "overrides": {
            "same_bar_sig_prefer_sel_track": False,
            "same_bar_sig_priority": "prefer_b",
        },
    },
    {
        "id": "TRACK_PREF_B",
        "title": "트랙 우선 ON · priority=prefer_b",
        "overrides": {
            "same_bar_sig_prefer_sel_track": True,
            "same_bar_sig_priority": "prefer_b",
        },
    },
    {
        "id": "MATCH_OFF",
        "title": "참조: 트랙 교차검증 자체를 끔",
        "overrides": {
            "same_bar_sig_prefer_sel_track": False,
            "enforce_track_signal_match": False,
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
        return {"n_buy1": 0, "n_buy2": 0, "win_rate": 0.0, "n_buy1_a": 0, "n_buy1_b": 0}
    buys1 = trades[trades["Action"] == "BUY1"]
    buys2 = trades[trades["Action"] == "BUY2"]
    sells = trades[trades["Action"] == "SELL"]
    n_a = 0
    n_b = 0
    try:
        if "Sig_Type" in buys1.columns:
            st_up = buys1["Sig_Type"].astype(str).str.upper()
            n_b = int(st_up.str.contains("TYPE_B").sum())
            n_a = int(len(buys1) - n_b)
    except Exception:
        n_a = 0
        n_b = 0
    return {
        "n_buy1": int(len(buys1)),
        "n_buy1_a": n_a,
        "n_buy1_b": n_b,
        "n_buy2": int(len(buys2)),
        "win_rate": float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0,
    }


def run_one(
    exp: Dict[str, Any], base: Dict[str, Any], start: str, end: str
) -> Dict[str, Any]:
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
    pd.DataFrame(tester.daily_equity).to_csv(
        dest / "equity.csv", index=False, encoding="utf-8-sig"
    )

    row: Dict[str, Any] = {
        "id": exp["id"],
        "title": exp["title"],
        "period": period,
        "start": start,
        "end": end,
        "elapsed_sec": round(elapsed, 1),
        "overrides": exp.get("overrides") or {},
        **_equity_metrics(tester.daily_equity, tester.initial_cash),
        **_trade_metrics(trades if isinstance(trades, pd.DataFrame) else None),
    }
    meta_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"  total={row.get('ret_total_pct', float('nan')):+.2f}% "
        f"DD={row.get('max_dd_pct', float('nan')):.1f}% "
        f"BUY1={row.get('n_buy1', 0)}(A{row.get('n_buy1_a', 0)}/B{row.get('n_buy1_b', 0)}) "
        f"BUY2={row.get('n_buy2', 0)}"
    )
    return row


def _print_summary(rows: List[Dict[str, Any]]) -> None:
    for period in ("full", "oos"):
        sub = [r for r in rows if r.get("period") == period and "ret_total_pct" in r]
        if not sub:
            continue
        base = next((r for r in sub if r.get("id") == "BASE"), None)
        print("\n" + "=" * 108)
        print(f"K200 SAME-BAR TRACK PREFERENCE {period.upper()}")
        print("=" * 108)
        print(
            f"  {'ID':18s} {'수익%':>9s} {'ΔBASE':>8s} {'만원':>8s} "
            f"{'MDD%':>8s} {'BUY1':>6s} {'A':>5s} {'B':>5s} {'BUY2':>6s} {'승률%':>7s}"
        )
        for r in sorted(
            sub, key=lambda x: float(x.get("ret_total_pct", -999)), reverse=True
        ):
            delta = 0.0
            if base is not None:
                delta = float(r.get("ret_total_pct", 0)) - float(
                    base.get("ret_total_pct", 0)
                )
            print(
                f"  {str(r.get('id')):18s} "
                f"{float(r.get('ret_total_pct', 0)):+9.2f} "
                f"{delta:+8.2f} "
                f"{float(r.get('profit_krw', 0)) / 1e4:+8.0f} "
                f"{float(r.get('max_dd_pct', 0)):8.1f} "
                f"{int(r.get('n_buy1', 0)):6d} "
                f"{int(r.get('n_buy1_a', 0)):5d} "
                f"{int(r.get('n_buy1_b', 0)):5d} "
                f"{int(r.get('n_buy2', 0)):6d} "
                f"{float(r.get('win_rate', 0)):7.1f}"
            )


def _recommend(rows: List[Dict[str, Any]]) -> str:
    full = {r["id"]: r for r in rows if r.get("period") == "full" and "ret_total_pct" in r}
    oos = {r["id"]: r for r in rows if r.get("period") == "oos" and "ret_total_pct" in r}
    if "BASE" not in full or "BASE" not in oos:
        return "권고: 데이터 부족 — BASE 결과 확인 필요"

    bf = float(full["BASE"]["ret_total_pct"])
    bo = float(oos["BASE"]["ret_total_pct"])
    bd_o = float(oos["BASE"]["max_dd_pct"])
    nb_f = int(full["BASE"].get("n_buy1", 0))
    nb_o = int(oos["BASE"].get("n_buy1", 0))

    candidates: List[tuple[str, float, float, float, float]] = []
    for eid, rf in full.items():
        if eid in ("BASE", "MATCH_OFF") or eid not in oos:
            continue
        ro = oos[eid]
        df_ = float(rf["ret_total_pct"]) - bf
        do = float(ro["ret_total_pct"]) - bo
        dd_improve = float(ro["max_dd_pct"]) - bd_o
        if df_ < -5.0:
            continue
        if do > 0.5 or dd_improve > 0.5:
            score = do + 0.3 * dd_improve + 0.2 * df_
            candidates.append((eid, score, do, dd_improve, df_))

    note = f" (BASE 진입수 FULL {nb_f} / OOS {nb_o})"
    if not candidates:
        return f"권고: BASE 유지 — 트랙 우선 확정의 유의미한 개선 없음{note}"

    candidates.sort(key=lambda x: x[1], reverse=True)
    best_id, score, do, dd_i, df_ = candidates[0]
    title = next((e["title"] for e in EXPERIMENTS if e["id"] == best_id), best_id)
    return (
        f"권고: {best_id} ({title}) — "
        f"OOS Δ{do:+.1f}%p, OOS MDD Δ{dd_i:+.1f}%p, FULL Δ{df_:+.1f}%p, "
        f"score={score:.2f}{note}"
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    full_start = str(base.get("bt_start_date", "2024-06-03"))
    full_end = str(base.get("bt_end_date", "2026-07-27"))

    print(
        f"[same-bar track pref] K200  "
        f"FULL {full_start}~{full_end} / OOS {OOS_START}~{full_end}"
    )
    print(
        f"  base priority={base.get('same_bar_sig_priority')} "
        f"prefer_sel_track={base.get('same_bar_sig_prefer_sel_track')} "
        f"enforce_match={base.get('enforce_track_signal_match')}"
    )

    rows: List[Dict[str, Any]] = []
    for exp in EXPERIMENTS:
        rows.append(run_one(exp, base, full_start, full_end))
    for exp in EXPERIMENTS:
        rows.append(run_one(exp, base, OOS_START, full_end))

    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _print_summary(rows)
    rec = _recommend(rows)
    print("\n" + rec)
    (OUT_DIR / "recommend.txt").write_text(rec + "\n", encoding="utf-8")
    print(f"\n저장: {summary_path}")


if __name__ == "__main__":
    main()
