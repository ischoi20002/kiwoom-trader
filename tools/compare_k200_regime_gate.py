# -*- coding: utf-8 -*-
"""
KOSPI200 + 현재 설정 기준 레짐 게이트 계단식 튜닝.

단계: OFF → 감지 → 응답 → chop
  python tools/compare_k200_regime_gate.py
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

OUT_DIR = ROOT / "tools" / "bt_compare_out_k200_regime_gate"
OOS_START = "2025-07-01"

EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "id": "BASE",
        "title": "현재 (gate ON · prior50 · soft8 · chop A15/B3)",
        "overrides": {},
    },
    {
        "id": "GATE_OFF",
        "title": "게이트 OFF",
        "overrides": {"use_transition_gate": False},
    },
    {
        "id": "DET_STRICT",
        "title": "감지 엄격: prior55 · d5=-25",
        "overrides": {
            "transition_prior_share_min": 55.0,
            "transition_share_delta5_max": -25.0,
        },
    },
    {
        "id": "DET_LOOSE",
        "title": "감지 완화: prior45 · d5=-15",
        "overrides": {
            "transition_prior_share_min": 45.0,
            "transition_share_delta5_max": -15.0,
        },
    },
    {
        "id": "DET_RVOL40",
        "title": "감지 rvol≥40",
        "overrides": {"transition_rvol_min": 40.0},
    },
    {
        "id": "RESP_SOFT4",
        "title": "응답 soft slots 4/4",
        "overrides": {
            "transition_soft_slots_a": 4,
            "transition_soft_slots_b": 4,
        },
    },
    {
        "id": "RESP_SOFT12",
        "title": "응답 soft slots 12/12",
        "overrides": {
            "transition_soft_slots_a": 12,
            "transition_soft_slots_b": 12,
        },
    },
    {
        "id": "RESP_BLOCK_S2",
        "title": "응답 TRANSITION 시 S2 차단",
        "overrides": {"transition_block_s2_on_soft": True},
    },
    {
        "id": "RESP_HARDONLY",
        "title": "응답 HARD만 (TRANSITION 무시)",
        "overrides": {"transition_ignore_soft": True},
    },
    {
        "id": "CHOP_STRICT",
        "title": "chop 엄격 A≥18 B≤2 share≤12 floor12",
        "overrides": {
            "chop_valid_a_min": 18,
            "chop_valid_b_max": 2,
            "chop_valid_b_share_max_pct": 12.0,
            "chop_valid_a_floor": 12,
        },
    },
    {
        "id": "CHOP_LOOSE",
        "title": "chop 완화 A≥12 B≤5 share≤20 floor8",
        "overrides": {
            "chop_valid_a_min": 12,
            "chop_valid_b_max": 5,
            "chop_valid_b_share_max_pct": 20.0,
            "chop_valid_a_floor": 8,
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

    row: Dict[str, Any] = {
        "id": exp["id"],
        "title": exp["title"],
        "period": period,
        "start": start,
        "end": end,
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


def _print_summary(rows: List[Dict[str, Any]]) -> None:
    for period in ("full", "oos"):
        sub = [r for r in rows if r.get("period") == period and "ret_total_pct" in r]
        if not sub:
            continue
        base = next((r for r in sub if r.get("id") == "BASE"), None)
        print("\n" + "=" * 96)
        print(f"K200 REGIME GATE {period.upper()}")
        print("=" * 96)
        print(
            f"  {'ID':14s} {'수익%':>9s} {'ΔBASE':>8s} {'만원':>8s} "
            f"{'MDD%':>8s} {'BUY1':>6s} {'승률%':>7s}"
        )
        for r in sorted(sub, key=lambda x: float(x.get("ret_total_pct", -999)), reverse=True):
            delta = 0.0
            if base is not None:
                delta = float(r.get("ret_total_pct", 0)) - float(base.get("ret_total_pct", 0))
            print(
                f"  {str(r.get('id')):14s} "
                f"{float(r.get('ret_total_pct', 0)):+9.2f} "
                f"{delta:+8.2f} "
                f"{float(r.get('profit_krw', 0))/1e4:+8.0f} "
                f"{float(r.get('max_dd_pct', 0)):8.1f} "
                f"{int(r.get('n_buy1', 0)):6d} "
                f"{float(r.get('win_rate', 0)):7.1f}"
            )


def _recommend(rows: List[Dict[str, Any]]) -> str:
    """판정 규칙에 따른 권고 문구."""
    full = {r["id"]: r for r in rows if r.get("period") == "full" and "ret_total_pct" in r}
    oos = {r["id"]: r for r in rows if r.get("period") == "oos" and "ret_total_pct" in r}
    if "BASE" not in full or "BASE" not in oos:
        return "권고: 데이터 부족 — BASE 결과 확인 필요"

    b_f = full["BASE"]
    b_o = oos["BASE"]
    bf = float(b_f["ret_total_pct"])
    bo = float(b_o["ret_total_pct"])
    bd_o = float(b_o["max_dd_pct"])

    # 1) GATE_OFF가 FULL·OOS 모두 명확히 우월
    if "GATE_OFF" in full and "GATE_OFF" in oos:
        g_f = float(full["GATE_OFF"]["ret_total_pct"])
        g_o = float(oos["GATE_OFF"]["ret_total_pct"])
        if g_f > bf + 2.0 and g_o > bo + 2.0:
            return "권고: GATE_OFF — FULL·OOS 모두 BASE 대비 +2%p 이상"

    candidates: List[tuple[str, float, float, float]] = []
    for eid, rf in full.items():
        if eid == "BASE" or eid not in oos:
            continue
        ro = oos[eid]
        df = float(rf["ret_total_pct"]) - bf
        do = float(ro["ret_total_pct"]) - bo
        dd_improve = float(ro["max_dd_pct"]) - bd_o  # MDD는 음수, 덜 음수면 개선(+)
        # FULL이 크게 깎이지 않음 (≤ -5%p) + (OOS 수익↑ 또는 OOS MDD 개선)
        if df < -5.0:
            continue
        if do > 0.5 or dd_improve > 0.5:
            # 점수: OOS 수익 개선 우선, MDD 개선 보조, FULL 희생 페널티
            score = do + 0.3 * dd_improve + 0.2 * df
            candidates.append((eid, score, do, dd_improve))

    if not candidates:
        return "권고: BASE 유지 — 유의미한 개선안 없음 (과적합 방지)"

    candidates.sort(key=lambda x: x[1], reverse=True)
    best_id, score, do, dd_i = candidates[0]
    title = next((e["title"] for e in EXPERIMENTS if e["id"] == best_id), best_id)
    return (
        f"권고: {best_id} ({title}) — "
        f"OOS Δ{do:+.1f}%p, OOS MDD Δ{dd_i:+.1f}%p, score={score:.2f}"
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    full_start = str(base.get("bt_start_date", "2024-06-03"))
    full_end = str(base.get("bt_end_date", "2026-07-27"))

    print(
        f"[regime gate] K200 계단식 튜닝  "
        f"FULL {full_start}~{full_end} / OOS {OOS_START}~{full_end}"
    )
    print(
        f"  base gate={base.get('use_transition_gate')} "
        f"prior={base.get('transition_prior_share_min')} "
        f"d5={base.get('transition_share_delta5_max')} "
        f"soft={base.get('transition_soft_slots_a')}/"
        f"{base.get('transition_soft_slots_b')}"
    )

    rows: List[Dict[str, Any]] = []
    for exp in EXPERIMENTS:
        rows.append(run_one(exp, base, full_start, full_end))
    for exp in EXPERIMENTS:
        rows.append(run_one(exp, base, OOS_START, full_end))

    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_summary(rows)
    rec = _recommend(rows)
    print(f"\n{rec}")
    print(f"\n저장: {summary_path}")


if __name__ == "__main__":
    main()
