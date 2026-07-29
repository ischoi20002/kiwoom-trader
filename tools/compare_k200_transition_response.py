# -*- coding: utf-8 -*-
"""
KOSPI200 · 전환 게이트 **응답축** 튜닝.

축:
  - soft slots (TRANSITION 슬롯 축소)
  - S1 / S2 차단 (TRANSITION)
  - TRANSITION 무시 (HARD만)

감지(prior/d5/rvol/chop)는 현재 settings 고정.
  python tools/compare_k200_transition_response.py
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

importlib.reload(be)
importlib.reload(db)

from backtest_engine import settings_for_backtest  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

OUT_DIR = ROOT / "tools" / "bt_compare_out_k200_transition_response"
OOS_START = "2025-07-01"

# 현행: soft 8/8 · S1차단 OFF · S2차단 OFF · ignore OFF
EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "id": "BASE",
        "title": "현행 soft8/8 · 차단없음",
        "overrides": {},
    },
    {
        "id": "GATE_OFF",
        "title": "게이트 OFF (참조)",
        "overrides": {"use_transition_gate": False},
    },
    # ── 슬롯 축소 축 ──
    {
        "id": "SLOT4",
        "title": "soft slots 4/4",
        "overrides": {
            "transition_soft_slots_a": 4,
            "transition_soft_slots_b": 4,
        },
    },
    {
        "id": "SLOT6",
        "title": "soft slots 6/6",
        "overrides": {
            "transition_soft_slots_a": 6,
            "transition_soft_slots_b": 6,
        },
    },
    {
        "id": "SLOT12",
        "title": "soft slots 12/12",
        "overrides": {
            "transition_soft_slots_a": 12,
            "transition_soft_slots_b": 12,
        },
    },
    {
        "id": "SLOT_NOSHRINK",
        "title": "soft=max (TRANSITION 슬롯축소 없음)",
        "overrides": {
            "transition_soft_slots_a": 20,
            "transition_soft_slots_b": 15,
        },
    },
    # ── 차단 축 (soft8 유지) ──
    {
        "id": "BLOCK_S1",
        "title": "TRANSITION S1 차단",
        "overrides": {"transition_block_s1_on_soft": True},
    },
    {
        "id": "BLOCK_S2",
        "title": "TRANSITION S2 차단",
        "overrides": {"transition_block_s2_on_soft": True},
    },
    {
        "id": "BLOCK_S1S2",
        "title": "TRANSITION S1+S2 차단",
        "overrides": {
            "transition_block_s1_on_soft": True,
            "transition_block_s2_on_soft": True,
        },
    },
    # ── 무시 / 조합 ──
    {
        "id": "IGNORE_SOFT",
        "title": "TRANSITION 무시 (HARD만)",
        "overrides": {"transition_ignore_soft": True},
    },
    {
        "id": "SLOT4_BLOCK_S1",
        "title": "soft4 + S1 차단",
        "overrides": {
            "transition_soft_slots_a": 4,
            "transition_soft_slots_b": 4,
            "transition_block_s1_on_soft": True,
        },
    },
    {
        "id": "SLOT6_BLOCK_S2",
        "title": "soft6 + S2 차단",
        "overrides": {
            "transition_soft_slots_a": 6,
            "transition_soft_slots_b": 6,
            "transition_block_s2_on_soft": True,
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
    # 게이트 ON 고정 (GATE_OFF 실험만 덮어씀)
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
        f"BUY1={row.get('n_buy1', 0)} BUY2={row.get('n_buy2', 0)}"
    )
    return row


def _print_summary(rows: List[Dict[str, Any]]) -> None:
    for period in ("full", "oos"):
        sub = [r for r in rows if r.get("period") == period and "ret_total_pct" in r]
        if not sub:
            continue
        base = next((r for r in sub if r.get("id") == "BASE"), None)
        print("\n" + "=" * 100)
        print(f"K200 TRANSITION RESPONSE {period.upper()}")
        print("=" * 100)
        print(
            f"  {'ID':16s} {'수익%':>9s} {'ΔBASE':>8s} {'만원':>8s} "
            f"{'MDD%':>8s} {'BUY1':>6s} {'BUY2':>6s} {'승률%':>7s}"
        )
        for r in sorted(sub, key=lambda x: float(x.get("ret_total_pct", -999)), reverse=True):
            delta = 0.0
            if base is not None:
                delta = float(r.get("ret_total_pct", 0)) - float(base.get("ret_total_pct", 0))
            print(
                f"  {str(r.get('id')):16s} "
                f"{float(r.get('ret_total_pct', 0)):+9.2f} "
                f"{delta:+8.2f} "
                f"{float(r.get('profit_krw', 0)) / 1e4:+8.0f} "
                f"{float(r.get('max_dd_pct', 0)):8.1f} "
                f"{int(r.get('n_buy1', 0)):6d} "
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

    candidates: List[tuple[str, float, float, float, float]] = []
    for eid, rf in full.items():
        if eid in ("BASE", "GATE_OFF") or eid not in oos:
            continue
        ro = oos[eid]
        df = float(rf["ret_total_pct"]) - bf
        do = float(ro["ret_total_pct"]) - bo
        dd_improve = float(ro["max_dd_pct"]) - bd_o
        if df < -5.0:
            continue
        if do > 0.5 or dd_improve > 0.5:
            score = do + 0.3 * dd_improve + 0.2 * df
            candidates.append((eid, score, do, dd_improve, df))

    # GATE_OFF는 참조만 — HARD 보호 포기라 기본 채택 비추천, 다만 수치 언급
    gate_note = ""
    if "GATE_OFF" in full and "GATE_OFF" in oos:
        gf = float(full["GATE_OFF"]["ret_total_pct"]) - bf
        go = float(oos["GATE_OFF"]["ret_total_pct"]) - bo
        gate_note = f" (참조 GATE_OFF FULLΔ{gf:+.1f} OOSΔ{go:+.1f})"

    if not candidates:
        return f"권고: BASE 유지 — 유의미한 개선안 없음{gate_note}"

    candidates.sort(key=lambda x: x[1], reverse=True)
    best_id, score, do, dd_i, df = candidates[0]
    title = next((e["title"] for e in EXPERIMENTS if e["id"] == best_id), best_id)
    return (
        f"권고: {best_id} ({title}) — "
        f"OOS Δ{do:+.1f}%p, OOS MDD Δ{dd_i:+.1f}%p, FULL Δ{df:+.1f}%p, "
        f"score={score:.2f}{gate_note}"
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    full_start = str(base.get("bt_start_date", "2024-06-03"))
    full_end = str(base.get("bt_end_date", "2026-07-27"))

    print(
        f"[transition response] K200  "
        f"FULL {full_start}~{full_end} / OOS {OOS_START}~{full_end}"
    )
    print(
        f"  base soft={base.get('transition_soft_slots_a')}/"
        f"{base.get('transition_soft_slots_b')} "
        f"block_s1={base.get('transition_block_s1_on_soft')} "
        f"block_s2={base.get('transition_block_s2_on_soft')} "
        f"ignore={base.get('transition_ignore_soft')}"
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
