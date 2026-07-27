# -*- coding: utf-8 -*-
"""
두 방향 비교 (슬롯 N·교차거절 유지):

1) 유니버스: KODEX200 vs KOSPI top300 (선정 로직 동일)
2) 선정: 현행 vs Soft(Valid 완화) — 유니버스별 2×2

  python tools/compare_universe_and_selection.py
  python tools/compare_universe_and_selection.py --start 2025-01-01 --end 2025-06-30
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

import importlib
import backtest_engine as be
import dynamic_backtest as db

importlib.reload(be)
importlib.reload(db)

from backtest_engine import settings_for_backtest  # noqa: E402
from data_manager_v2 import DataManagerV2  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

OUT_DIR = ROOT / "tools" / "bt_compare_out_univ_sel"
HIGH_PRECISION = False

# Soft: Valid 고갈 완화가 목적 (Score 재설계가 아님). OSC/RSI·되돌림·고점 밴드 완화.
SOFT_SELECTION: Dict[str, Any] = {
    # Track A
    "sel_a_pb_min_pct": 3.0,
    "sel_a_pb_max_pct": 22.0,
    "sel_a_rs_rank_min": 0.25,
    "sel_a_rs_rank_max": 0.90,
    "sel_a_require_osc_positive": False,
    "sel_a_require_sel_rsi_band": False,
    # Track B
    "sel_b_hi_min_pct": 85.0,
    "sel_b_hi_max_pct": 99.7,
    "sel_b_rs_rank_min": 0.30,
    "sel_b_require_osc_rising": False,
    "sel_b_require_vol_burst": False,
}


def _load_base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["use_market_breadth_gate"] = False
    st["bt_high_precision"] = False
    st["bt_rt_parity_intrabar"] = False
    st["enforce_track_signal_match"] = True
    st["use_s2_master_a"] = True
    st["use_s2_master_b"] = True
    return st


def _experiments() -> List[Dict[str, Any]]:
    """2×2 + KOSPI500 현행선정(참고)."""
    return [
        {
            "id": "kodex_sel_base",
            "title": "U=KODEX · Sel=현행",
            "overrides": {
                "universe_index_choice": "KODEX200",
            },
        },
        {
            "id": "kodex_sel_soft",
            "title": "U=KODEX · Sel=Soft",
            "overrides": {
                "universe_index_choice": "KODEX200",
                **SOFT_SELECTION,
            },
        },
        {
            "id": "kospi300_sel_base",
            "title": "U=KOSPI300 · Sel=현행",
            "overrides": {
                "universe_index_choice": "KOSPI200",
                "kospi_top_n": 300,
            },
        },
        {
            "id": "kospi300_sel_soft",
            "title": "U=KOSPI300 · Sel=Soft",
            "overrides": {
                "universe_index_choice": "KOSPI200",
                "kospi_top_n": 300,
                **SOFT_SELECTION,
            },
        },
        {
            "id": "kospi500_sel_base",
            "title": "U=KOSPI500 · Sel=현행 (참고)",
            "overrides": {
                "universe_index_choice": "KOSPI200",
                "kospi_top_n": 500,
            },
        },
    ]


def _census_valid(
    settings: dict[str, Any], start: str, end: str
) -> Dict[str, float]:
    """기간 평균 Valid_A/B·스케줄 크기 (일봉 패널)."""
    try:
        import importlib.util as ilu

        spec = ilu.spec_from_file_location(
            "diag_fwd",
            str(ROOT / "tools" / "diagnose_selection_forward_returns.py"),
        )
        if spec is None or spec.loader is None:
            return {}
        mod = ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        dm = DataManagerV2()
        df_sim, all_codes, duration_days = mod.load_scored_panel(
            settings, start, end, dm
        )
        # 간단 스케줄 (duration<180이면 all, 아니면 월 유니버스는 load가 이미 반영)
        max_sa = int(settings.get("max_slots_a", 20))
        max_sb = int(settings.get("max_slots_b", 15))
        from strategy_selector import _df_top_by_score_slot_band

        n_va: List[int] = []
        n_vb: List[int] = []
        n_sa: List[int] = []
        n_sb: List[int] = []
        for d_str in df_sim["time"].dt.strftime("%Y-%m-%d").drop_duplicates():
            day = df_sim.loc[df_sim["time"] == pd.to_datetime(d_str)]
            va = int(day["Valid_A"].astype(bool).sum()) if "Valid_A" in day else 0
            vb = int(day["Valid_B"].astype(bool).sum()) if "Valid_B" in day else 0
            n_va.append(va)
            n_vb.append(vb)
            day_a = day.loc[day["Valid_A"].astype(bool)] if va else day.iloc[0:0]
            day_b = day.loc[day["Valid_B"].astype(bool)] if vb else day.iloc[0:0]
            sa = 0
            sb = 0
            if not day_a.empty:
                sa = len(
                    _df_top_by_score_slot_band(
                        day_a,
                        score_col="Score_A",
                        code_col="code",
                        max_n=max_sa,
                        settings=settings,
                        min_score_floor=0.0,
                    )
                )
            if not day_b.empty:
                sb = len(
                    _df_top_by_score_slot_band(
                        day_b,
                        score_col="Score_B",
                        code_col="code",
                        max_n=max_sb,
                        settings=settings,
                        min_score_floor=0.0,
                    )
                )
            n_sa.append(sa)
            n_sb.append(sb)
        return {
            "avg_valid_a": float(sum(n_va) / len(n_va)) if n_va else 0.0,
            "avg_valid_b": float(sum(n_vb) / len(n_vb)) if n_vb else 0.0,
            "avg_sched_a": float(sum(n_sa) / len(n_sa)) if n_sa else 0.0,
            "avg_sched_b": float(sum(n_sb) / len(n_sb)) if n_sb else 0.0,
            "pct_days_valid_a_lt_slots": (
                float(sum(1 for x in n_va if x < max_sa) / len(n_va) * 100.0)
                if n_va
                else 0.0
            ),
            "pct_days_valid_b_lt_slots": (
                float(sum(1 for x in n_vb if x < max_sb) / len(n_vb) * 100.0)
                if n_vb
                else 0.0
            ),
        }
    except Exception as e:
        try:
            print(f"  [census 경고] {e}")
        except Exception:
            pass
        return {}


def _equity_metrics(
    daily_equity: List[dict], initial: float
) -> Dict[str, Any]:
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
    dd = (daily[col] - daily[col].cummax()) / daily[col].cummax().replace(0, pd.NA)
    return {
        "ret_total_pct": (e_end / e0 - 1.0) * 100.0 if e0 else 0.0,
        "end_equity": e_end,
        "max_dd_pct": float(dd.min() * 100.0) if dd.notna().any() else 0.0,
    }


def _trade_metrics(trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if trades is None or (isinstance(trades, pd.DataFrame) and trades.empty):
        return {"n_buy1": 0, "n_buy2": 0, "n_sell": 0, "win_rate": 0.0}
    df = trades
    buys1 = df[df["Action"] == "BUY1"]
    buys2 = df[df["Action"] == "BUY2"]
    sells = df[df["Action"] == "SELL"]
    return {
        "n_buy1": int(len(buys1)),
        "n_buy2": int(len(buys2)),
        "n_sell": int(len(sells)),
        "win_rate": float((sells["Return"] > 0).mean() * 100.0) if len(sells) else 0.0,
        "s2_rate": float(len(buys2) / len(buys1) * 100.0) if len(buys1) else 0.0,
    }


def run_one(
    exp: Dict[str, Any],
    base: Dict[str, Any],
    start: str,
    end: str,
    *,
    do_census: bool,
) -> Dict[str, Any]:
    rid = exp["id"]
    print("\n" + "=" * 72)
    print(f"[{rid}] {exp['title']}")
    print("=" * 72)
    st = deepcopy(base)
    st.update(exp["overrides"])

    census: Dict[str, float] = {}
    if do_census:
        print("  Valid 센서스(일봉)…")
        census = _census_valid(st, start, end)
        if census:
            print(
                f"  Valid≈ A {_fmt(census.get('avg_valid_a'),1)} / "
                f"B {_fmt(census.get('avg_valid_b'),1)}  "
                f"sched A {_fmt(census.get('avg_sched_a'),1)} / "
                f"B {_fmt(census.get('avg_sched_b'),1)}  "
                f"(A<슬롯일 {_fmt(census.get('pct_days_valid_a_lt_slots'),0)}%)"
            )

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
    tester = DynamicBacktester(st)
    trades = tester.run(
        start, end, interval="15m", callback=cb, high_precision=HIGH_PRECISION
    )
    elapsed = time.time() - t0
    print(f"\n  done {elapsed:.1f}s")

    dest = OUT_DIR / rid
    dest.mkdir(parents=True, exist_ok=True)
    if isinstance(trades, pd.DataFrame) and not trades.empty:
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(tester.daily_equity).to_csv(
        dest / "equity.csv", index=False, encoding="utf-8-sig"
    )

    eq = _equity_metrics(tester.daily_equity, tester.initial_cash)
    tm = _trade_metrics(trades if isinstance(trades, pd.DataFrame) else None)
    univ = str(st.get("universe_index_choice", ""))
    top_n = st.get("kospi_top_n", "")
    soft = rid.endswith("_soft")
    r = {
        "id": rid,
        "title": exp["title"],
        "universe": univ,
        "kospi_top_n": top_n,
        "sel_soft": soft,
        "elapsed_sec": round(elapsed, 1),
        **census,
        **eq,
        **tm,
    }
    print(
        f"  total={r.get('ret_total_pct', float('nan')):+.2f}% "
        f"DD={r.get('max_dd_pct', float('nan')):.1f}% "
        f"buy1={r.get('n_buy1', 0)} buy2={r.get('n_buy2', 0)} "
        f"win={r.get('win_rate', 0):.1f}%"
    )
    return r


def _fmt(v: Any, digits: int = 2) -> str:
    try:
        if v is None:
            return "n/a"
        return f"{float(v):.{digits}f}"
    except Exception:
        return str(v)


def _print_matrix(rows: List[Dict[str, Any]]) -> None:
    by = {r["id"]: r for r in rows}
    print("\n" + "=" * 72)
    print("요약 매트릭스 (기준= kodex_sel_base)")
    print("=" * 72)
    base = by.get("kodex_sel_base")
    if not base:
        return
    br = float(base.get("ret_total_pct", 0))
    print(
        f"  {'id':28s} {'수익%':>9s} {'Δ%p':>8s} {'DD%':>8s} "
        f"{'BUY1':>6s} {'ValidA':>7s} {'ValidB':>7s}"
    )
    for rid in (
        "kodex_sel_base",
        "kodex_sel_soft",
        "kospi300_sel_base",
        "kospi300_sel_soft",
        "kospi500_sel_base",
    ):
        r = by.get(rid)
        if not r or "ret_total_pct" not in r:
            continue
        rt = float(r["ret_total_pct"])
        print(
            f"  {rid:28s} {rt:+9.2f} {rt - br:+8.2f} "
            f"{float(r.get('max_dd_pct', 0)):8.1f} "
            f"{int(r.get('n_buy1', 0)):6d} "
            f"{_fmt(r.get('avg_valid_a'), 1):>7s} "
            f"{_fmt(r.get('avg_valid_b'), 1):>7s}"
        )

    # 방향별 효과
    print("\n  [방향1] 유니버스만 (Sel=현행)")
    if "kospi300_sel_base" in by:
        d = float(by["kospi300_sel_base"]["ret_total_pct"]) - br
        print(f"    KODEX → KOSPI300: {d:+.2f}%p")
    if "kospi500_sel_base" in by:
        d = float(by["kospi500_sel_base"]["ret_total_pct"]) - br
        print(f"    KODEX → KOSPI500: {d:+.2f}%p")

    print("  [방향2] 선정 Soft만 (U=KODEX)")
    if "kodex_sel_soft" in by:
        d = float(by["kodex_sel_soft"]["ret_total_pct"]) - br
        print(f"    현행 → Soft: {d:+.2f}%p")

    print("  [결합] KOSPI300 × Soft")
    if "kospi300_sel_soft" in by:
        d = float(by["kospi300_sel_soft"]["ret_total_pct"]) - br
        print(f"    vs KODEX 현행: {d:+.2f}%p")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-06-03")
    ap.add_argument("--end", default="2026-06-30")
    ap.add_argument(
        "--no-census",
        action="store_true",
        help="일봉 Valid 센서스 생략(약간 빠름)",
    )
    ap.add_argument(
        "--only",
        default="",
        help="쉼표구분 id만 실행 (예: kodex_sel_base,kospi300_sel_base)",
    )
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    exps = _experiments()
    if args.only.strip():
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        exps = [e for e in exps if e["id"] in want]

    print(
        f"[compare] {args.start} ~ {args.end}  "
        f"slots A={base.get('max_slots_a')} B={base.get('max_slots_b')}  "
        f"match=ON S2=ON  n_exp={len(exps)}"
    )
    print(f"Soft keys: {list(SOFT_SELECTION.keys())}")

    rows: List[Dict[str, Any]] = []
    for exp in exps:
        try:
            rows.append(
                run_one(
                    exp,
                    base,
                    args.start,
                    args.end,
                    do_census=not args.no_census,
                )
            )
        except Exception as e:
            print(f"[Error] {exp['id']}: {e}")
            traceback.print_exc()
            rows.append({"id": exp["id"], "title": exp["title"], "error": str(e)})

    df = pd.DataFrame(rows)
    df.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    (OUT_DIR / "soft_selection.json").write_text(
        json.dumps(SOFT_SELECTION, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _print_matrix([r for r in rows if "error" not in r])
    print(f"\n[저장] {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
