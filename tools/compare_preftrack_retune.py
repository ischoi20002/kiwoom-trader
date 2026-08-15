# -*- coding: utf-8 -*-
"""
prefer_track 고정 BASE 기준 K200 재튜닝 오케스트레이터.

Phase 순 스윕 → OOS Δ>+0.5%p 이고 FULL Δ>-5%p 이면 채택·누적.

  python tools/compare_preftrack_retune.py
  python tools/compare_preftrack_retune.py --phase seed
  python tools/compare_preftrack_retune.py --phase 1
  python tools/compare_preftrack_retune.py --phase all
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

OUT_ROOT = ROOT / "tools" / "bt_compare_out_preftrack"
OOS_START = "2025-07-01"
ADOPT_OOS_MIN = 0.5
ADOPT_FULL_FLOOR = -5.0


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


def _load_raw_base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["universe_index_choice"] = "KOSPI200"
    st["kospi_top_n"] = 200
    st["bt_high_precision"] = False
    st["bt_rt_parity_intrabar"] = False
    st["enforce_track_signal_match"] = True
    st["same_bar_sig_priority"] = "prefer_track"
    st["same_bar_sig_prefer_sel_track"] = False
    # 기본 always (설정값 존중, 없으면 always)
    if not str(st.get("s1_simul_balance_tie_mode", "")).strip():
        st["s1_simul_balance_tie_mode"] = "always"
    st["s1_sizing_unit"] = "%"
    st["s2_sizing_unit"] = "%"
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
        pass
    return {
        "n_buy1": int(len(buys1)),
        "n_buy1_a": n_a,
        "n_buy1_b": n_b,
        "n_buy2": int(len(buys2)),
        "win_rate": float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0,
    }


def run_one(
    out_dir: Path,
    exp: Dict[str, Any],
    base: Dict[str, Any],
    start: str,
    end: str,
    full_start: str,
) -> Dict[str, Any]:
    period = "full" if start == full_start else "oos"
    run_id = f"{exp['id']}_{period}"
    dest = out_dir / run_id
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
    st.update(exp.get("overrides") or {})

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
        f"({float(row.get('profit_krw', 0)) / 1e4:+.0f}만) "
        f"DD={row.get('max_dd_pct', float('nan')):.1f}% "
        f"BUY1={row.get('n_buy1', 0)}"
        f"(A{row.get('n_buy1_a', 0)}/B{row.get('n_buy1_b', 0)}) "
        f"BUY2={row.get('n_buy2', 0)}"
    )
    return row


def _print_summary(phase: str, rows: List[Dict[str, Any]]) -> None:
    for period in ("full", "oos"):
        sub = [r for r in rows if r.get("period") == period and "ret_total_pct" in r]
        if not sub:
            continue
        base = next((r for r in sub if r.get("id") == "BASE"), None)
        print("\n" + "=" * 96)
        print(f"PREFTRACK {phase.upper()} {period.upper()}")
        print("=" * 96)
        print(
            "  {:16s}{:>10s}{:>9s}{:>8s}{:>7s}{:>5s}{:>5s}{:>7s}".format(
                "ID", "수익%", "dBASE", "MDD%", "BUY1", "A", "B", "BUY2"
            )
        )
        for r in sorted(sub, key=lambda x: float(x.get("ret_total_pct", -999)), reverse=True):
            delta = 0.0
            if base is not None:
                delta = float(r.get("ret_total_pct", 0)) - float(
                    base.get("ret_total_pct", 0)
                )
            print(
                "  {:16s}{:+10.2f}{:+9.2f}{:8.1f}{:7d}{:5d}{:5d}{:7d}".format(
                    str(r.get("id")),
                    float(r.get("ret_total_pct", 0)),
                    delta,
                    float(r.get("max_dd_pct", 0)),
                    int(r.get("n_buy1", 0)),
                    int(r.get("n_buy1_a", 0)),
                    int(r.get("n_buy1_b", 0)),
                    int(r.get("n_buy2", 0)),
                )
            )


def _pick_adopt(rows: List[Dict[str, Any]]) -> Tuple[Optional[str], str]:
    full = {r["id"]: r for r in rows if r.get("period") == "full" and "ret_total_pct" in r}
    oos = {r["id"]: r for r in rows if r.get("period") == "oos" and "ret_total_pct" in r}
    if "BASE" not in full or "BASE" not in oos:
        return None, "권고: BASE 데이터 부족 → 유지"
    bf = float(full["BASE"]["ret_total_pct"])
    bo = float(oos["BASE"]["ret_total_pct"])
    cands: List[Tuple[float, str, float, float]] = []
    for rid, orow in oos.items():
        if rid == "BASE" or rid not in full:
            continue
        do = float(orow["ret_total_pct"]) - bo
        df_ = float(full[rid]["ret_total_pct"]) - bf
        if do > ADOPT_OOS_MIN and df_ > ADOPT_FULL_FLOOR:
            cands.append((do, rid, df_, do))
    if not cands:
        return None, (
            f"권고: BASE 유지 (OOS>{ADOPT_OOS_MIN}%p & FULL>{ADOPT_FULL_FLOOR}%p 충족 후보 없음)"
        )
    cands.sort(reverse=True)
    best = cands[0]
    rid = best[1]
    return rid, (
        f"권고: {rid} 채택 — OOS Δ{best[3]:+.1f}%p, FULL Δ{best[2]:+.1f}%p"
    )


def _phase_experiments(phase: str) -> List[Dict[str, Any]]:
    signs = _load_b_signs()
    if phase == "seed":
        return [{"id": "BASE", "title": "prefer_track baseline", "overrides": {}}]
    if phase == "1":
        return [
            {"id": "BASE", "title": "prefer_track baseline", "overrides": {}},
            {
                "id": "TIE_OFF",
                "title": "동수 덮기 off (prefer_track 순수)",
                "overrides": {"s1_simul_balance_tie_mode": "off"},
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
                "overrides": {"gap_down_limit_b": -3.0},
            },
            {
                "id": "ALIGN_B_GAP3",
                "title": "aligned score + B gap -3.0",
                "overrides": {
                    "sel_score_mode": "s1_pnl_aligned_v1",
                    "sel_s1_pnl_signs": signs,
                    "gap_down_limit_b": -3.0,
                },
            },
        ]
    if phase == "2":
        return [
            {"id": "BASE", "title": "phase BASE", "overrides": {}},
            {
                "id": "A_RELAX",
                "title": "A 완화 (rs_min 0.30, pb_max 18)",
                "overrides": {"sel_a_rs_rank_min": 0.30, "sel_a_pb_max_pct": 18.0},
            },
            {
                "id": "A_STRICT",
                "title": "A 엄격 (rs_min 0.40, pb_max 14)",
                "overrides": {"sel_a_rs_rank_min": 0.40, "sel_a_pb_max_pct": 14.0},
            },
            {
                "id": "B_RELAX",
                "title": "B 완화 (hi_min 90, rs_min 0.40)",
                "overrides": {"sel_b_hi_min_pct": 90.0, "sel_b_rs_rank_min": 0.40},
            },
            {
                "id": "B_STRICT",
                "title": "B 엄격 (hi_min 94, rs_min 0.50)",
                "overrides": {"sel_b_hi_min_pct": 94.0, "sel_b_rs_rank_min": 0.50},
            },
            {
                "id": "AB_RELAX",
                "title": "A/B 동시 소폭 완화",
                "overrides": {
                    "sel_a_rs_rank_min": 0.30,
                    "sel_a_pb_max_pct": 18.0,
                    "sel_b_hi_min_pct": 90.0,
                    "sel_b_rs_rank_min": 0.40,
                },
            },
        ]
    if phase == "3":
        return [
            {"id": "BASE", "title": "phase BASE", "overrides": {}},
            {
                "id": "S2_PROFIT_10",
                "title": "S2 profit 1.0",
                "overrides": {"s2_profit_limit": 1.0, "s2_b_profit_limit": 1.0},
            },
            {
                "id": "S2_PROFIT_20",
                "title": "S2 profit 2.0",
                "overrides": {"s2_profit_limit": 2.0, "s2_b_profit_limit": 2.0},
            },
            {
                "id": "S2_DELAY_12",
                "title": "S2 delay 12h",
                "overrides": {"s2_delay_hours": 12.0, "s2_b_delay_hours": 12.0},
            },
            {
                "id": "S2_DELAY_36",
                "title": "S2 delay 36h",
                "overrides": {"s2_delay_hours": 36.0, "s2_b_delay_hours": 36.0},
            },
            {
                "id": "S2_LOOSE",
                "title": "S2 느슨 (profit 1.0 + 12h)",
                "overrides": {
                    "s2_profit_limit": 1.0,
                    "s2_b_profit_limit": 1.0,
                    "s2_delay_hours": 12.0,
                    "s2_b_delay_hours": 12.0,
                },
            },
            {
                "id": "S2_STRICT",
                "title": "S2 엄격 (profit 2.0 + 36h)",
                "overrides": {
                    "s2_profit_limit": 2.0,
                    "s2_b_profit_limit": 2.0,
                    "s2_delay_hours": 36.0,
                    "s2_b_delay_hours": 36.0,
                },
            },
        ]
    if phase == "4":
        return [
            {"id": "BASE", "title": "S1 20 / S2 25", "overrides": {}},
            {
                "id": "S1_15_S2_25",
                "title": "S1 15 / S2 25",
                "overrides": {"s1_equity_pct": 15.0, "s2_equity_pct": 25.0},
            },
            {
                "id": "S1_25_S2_25",
                "title": "S1 25 / S2 25",
                "overrides": {"s1_equity_pct": 25.0, "s2_equity_pct": 25.0},
            },
            {
                "id": "S1_20_S2_20",
                "title": "S1 20 / S2 20",
                "overrides": {"s1_equity_pct": 20.0, "s2_equity_pct": 20.0},
            },
            {
                "id": "S1_20_S2_30",
                "title": "S1 20 / S2 30",
                "overrides": {"s1_equity_pct": 20.0, "s2_equity_pct": 30.0},
            },
            {
                "id": "S1_15_S2_20",
                "title": "S1 15 / S2 20",
                "overrides": {"s1_equity_pct": 15.0, "s2_equity_pct": 20.0},
            },
            {
                "id": "S1_25_S2_30",
                "title": "S1 25 / S2 30",
                "overrides": {"s1_equity_pct": 25.0, "s2_equity_pct": 30.0},
            },
        ]
    if phase == "5":
        return [
            {"id": "BASE", "title": "phase BASE", "overrides": {}},
            {
                "id": "GATE_OFF",
                "title": "게이트 OFF",
                "overrides": {"use_transition_gate": False},
            },
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
                "id": "IGNORE_SOFT",
                "title": "TRANSITION 무시",
                "overrides": {"transition_ignore_soft": True},
            },
            {
                "id": "DET_STRICT",
                "title": "감지 엄격 prior55 d5=-25",
                "overrides": {
                    "transition_prior_share_min": 55.0,
                    "transition_share_delta5_max": -25.0,
                },
            },
            {
                "id": "DET_LOOSE",
                "title": "감지 완화 prior45 d5=-15",
                "overrides": {
                    "transition_prior_share_min": 45.0,
                    "transition_share_delta5_max": -15.0,
                },
            },
        ]
    if phase == "6":
        return [
            {"id": "BASE", "title": "phase BASE", "overrides": {}},
            {
                "id": "TRAIL_B5",
                "title": "ATR trail B 5.0",
                "overrides": {"atr_multiplier_trail_b": 5.0},
            },
            {
                "id": "TRAIL_B7",
                "title": "ATR trail B 7.0",
                "overrides": {"atr_multiplier_trail_b": 7.0},
            },
            {
                "id": "TRAIL_A5",
                "title": "ATR trail A 5.0",
                "overrides": {"atr_multiplier_trail_a": 5.0},
            },
            {
                "id": "TRAIL_A7",
                "title": "ATR trail A 7.0",
                "overrides": {"atr_multiplier_trail_a": 7.0},
            },
        ]
    raise ValueError(f"unknown phase: {phase}")


def _load_state() -> Dict[str, Any]:
    path = OUT_ROOT / "retune_state.json"
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {"adopted": {}, "history": []}


def _save_state(state: Dict[str, Any]) -> None:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    (OUT_ROOT / "retune_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def run_phase(phase: str, accumulated: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[str], str]:
    out_dir = OUT_ROOT / f"phase_{phase}"
    out_dir.mkdir(parents=True, exist_ok=True)
    base = _load_raw_base()
    base.update(accumulated)
    experiments = _phase_experiments(phase)
    full_start = str(base.get("bt_start_date", "2024-06-03"))
    full_end = str(base.get("bt_end_date", "2026-07-27"))
    print(
        f"\n######## PHASE {phase}  accumulated={json.dumps(accumulated, ensure_ascii=False)} ########"
    )
    rows: List[Dict[str, Any]] = []
    for exp in experiments:
        rows.append(run_one(out_dir, exp, base, full_start, full_end, full_start))
    for exp in experiments:
        rows.append(run_one(out_dir, exp, base, OOS_START, full_end, full_start))
    (out_dir / "summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _print_summary(phase, rows)
    adopt_id, msg = _pick_adopt(rows)
    print("\n" + msg)
    (out_dir / "recommend.txt").write_text(msg + "\n", encoding="utf-8")
    return rows, adopt_id, msg


def _crosscheck_gui(seed_rows: List[Dict[str, Any]]) -> str:
    lines = ["## GUI 교차검증"]
    full = next(
        (r for r in seed_rows if r.get("id") == "BASE" and r.get("period") == "full"),
        None,
    )
    if not full:
        return "GUI 교차검증: BASE full 없음"
    profit = float(full.get("profit_krw", 0))
    gui_net = 15_751_684.0  # dynamic_trade_report Net Profit
    lines.append(
        f"- 스크립트 FULL profit={profit:,.0f}원 ({float(full.get('ret_total_pct', 0)):+.2f}%)"
    )
    lines.append(f"- GUI report Net Profit≈{gui_net:,.0f}원")
    lines.append(
        f"- 차이={profit - gui_net:,.0f}원 "
        f"(BUY1={full.get('n_buy1')} A{full.get('n_buy1_a')}/B{full.get('n_buy1_b')})"
    )
    # portfolio curve if present
    curve = ROOT / "backtest_portfolio_curve.csv"
    if curve.is_file():
        try:
            cdf = pd.read_csv(curve)
            col = "Total" if "Total" in cdf.columns else "equity"
            e0 = float(cdf.iloc[0][col])
            e1 = float(cdf.iloc[-1][col])
            lines.append(
                f"- backtest_portfolio_curve.csv end={e1:,.0f} "
                f"ret={(e1 / e0 - 1) * 100:+.2f}%"
            )
        except Exception as e:
            lines.append(f"- curve 읽기 실패: {e}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--phase",
        default="all",
        help="seed|1|2|3|4|5|6|all",
    )
    args = ap.parse_args()

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    state = _load_state()
    accumulated: Dict[str, Any] = dict(state.get("adopted") or {})

    phases: List[str]
    if args.phase.strip().lower() == "all":
        phases = ["seed", "1", "2", "3", "4", "5", "6"]
    else:
        phases = [args.phase.strip().lower()]

    report_lines: List[str] = [
        "# prefer_track K200 재튜닝 결과",
        "",
        f"- 고정: same_bar_sig_priority=prefer_track, tie={_load_raw_base().get('s1_simul_balance_tie_mode')}",
        "",
    ]

    for ph in phases:
        rows, adopt_id, msg = run_phase(ph, accumulated)
        if ph == "seed":
            report_lines.append(_crosscheck_gui(rows))
            report_lines.append("")
            full = next(
                (r for r in rows if r.get("id") == "BASE" and r.get("period") == "full"),
                {},
            )
            oos = next(
                (r for r in rows if r.get("id") == "BASE" and r.get("period") == "oos"),
                {},
            )
            report_lines.append(
                f"## Seed BASE\n"
                f"- FULL {float(full.get('ret_total_pct', 0)):+.2f}% "
                f"MDD {float(full.get('max_dd_pct', 0)):.1f}% "
                f"BUY1 {full.get('n_buy1')}\n"
                f"- OOS  {float(oos.get('ret_total_pct', 0)):+.2f}% "
                f"MDD {float(oos.get('max_dd_pct', 0)):.1f}% "
                f"BUY1 {oos.get('n_buy1')}\n"
            )
            state.setdefault("history", []).append(
                {"phase": "seed", "adopt": None, "msg": "BASE seeded"}
            )
            _save_state(state)
            continue

        experiments = {e["id"]: e for e in _phase_experiments(ph)}
        if adopt_id and adopt_id in experiments:
            ov = experiments[adopt_id].get("overrides") or {}
            accumulated.update(ov)
            state["adopted"] = accumulated
            state.setdefault("history", []).append(
                {"phase": ph, "adopt": adopt_id, "overrides": ov, "msg": msg}
            )
            report_lines.append(f"## Phase {ph}\n- {msg}\n- overrides: `{ov}`\n")
        else:
            state.setdefault("history", []).append(
                {"phase": ph, "adopt": None, "msg": msg}
            )
            report_lines.append(f"## Phase {ph}\n- {msg}\n")
        _save_state(state)

    report_lines.append("## 최종 누적 채택")
    report_lines.append(f"```json\n{json.dumps(accumulated, ensure_ascii=False, indent=2)}\n```")
    report_path = OUT_ROOT / "FINAL_REPORT.md"
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(report_lines))
    print(f"\n저장: {report_path}")


if __name__ == "__main__":
    main()
