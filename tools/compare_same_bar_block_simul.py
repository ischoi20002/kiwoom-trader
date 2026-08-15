# -*- coding: utf-8 -*-
"""
동시신호(A+B) S1 매수 전면 차단 효과 비교.

``same_bar_block_simul=True``: 동시신호 봉이면 TYPE_A·B 모두 매수 차단.
BASE는 이전 ``bt_compare_out_same_bar_block_a`` 캐시를 재사용(동일 설정).

  python tools/compare_same_bar_block_simul.py
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
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

OUT_DIR = ROOT / "tools" / "bt_compare_out_same_bar_block_simul"
BASE_CACHE_DIR = ROOT / "tools" / "bt_compare_out_same_bar_block_a"
OOS_START = "2025-07-01"

EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "id": "BASE",
        "title": "현행 (동시신호 매수 허용)",
        "overrides": {
            "same_bar_block_simul": False,
            "same_bar_block_type_a": False,
        },
    },
    {
        "id": "BLOCK_ALL",
        "title": "동시신호 A+B 전면 차단",
        "overrides": {
            "same_bar_block_simul": True,
            "same_bar_block_type_a": False,
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
    st["same_bar_sig_prefer_sel_track"] = False
    st["same_bar_sig_allow_track_b"] = False
    st["same_bar_block_type_a"] = False
    st["same_bar_block_simul"] = False
    st["s1_simul_balance_tie_mode"] = "always"
    return st


def _seed_base_from_cache(full_start: str, full_end: str) -> None:
    """이전 BLOCK_A 비교의 BASE 결과를 이 OUT_DIR 로 복사."""
    for period, start in (("full", full_start), ("oos", OOS_START)):
        src = BASE_CACHE_DIR / f"BASE_{period}"
        dst = OUT_DIR / f"BASE_{period}"
        meta_src = src / "meta.json"
        if not meta_src.is_file():
            print(f"[BASE_{period}] 캐시 없음 → 재실행 예정 ({src})")
            continue
        try:
            cached = json.loads(meta_src.read_text(encoding="utf-8-sig"))
        except Exception as e:
            print(f"[BASE_{period}] 캐시 읽기 실패: {e}")
            continue
        if cached.get("start") != start or cached.get("end") != full_end:
            print(
                f"[BASE_{period}] 기간 불일치 "
                f"(cache {cached.get('start')}~{cached.get('end')} "
                f"vs {start}~{full_end}) → 재실행"
            )
            continue
        try:
            dst.mkdir(parents=True, exist_ok=True)
            for name in ("meta.json", "trades.csv", "equity.csv"):
                sp = src / name
                if sp.is_file():
                    shutil.copy2(sp, dst / name)
            # overrides 표기를 이번 실험 키에 맞춤(수치 동일)
            cached["overrides"] = {
                "same_bar_block_simul": False,
                "same_bar_block_type_a": False,
            }
            cached["title"] = "현행 (동시신호 매수 허용)"
            (dst / "meta.json").write_text(
                json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                f"[BASE_{period}] 캐시 재사용 "
                f"(from {BASE_CACHE_DIR.name}, "
                f"ret={float(cached.get('ret_total_pct', 0)):.2f}%)"
            )
        except Exception as e:
            print(f"[BASE_{period}] 캐시 복사 실패: {e}")


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
        print("\n" + "=" * 96)
        print(f"K200 SAME-BAR BLOCK SIMUL (ALL) {period.upper()}")
        print("=" * 96)
        print(
            "  {:12s}{:>10s}{:>9s}{:>8s}{:>7s}{:>5s}{:>5s}{:>7s}{:>8s}".format(
                "ID", "수익%", "dBASE", "MDD%", "BUY1", "A", "B", "BUY2", "승률%"
            )
        )
        for r in sorted(sub, key=lambda x: float(x.get("ret_total_pct", -999)), reverse=True):
            delta = 0.0
            if base is not None:
                delta = float(r.get("ret_total_pct", 0)) - float(
                    base.get("ret_total_pct", 0)
                )
            print(
                "  {:12s}{:+10.2f}{:+9.2f}{:8.1f}{:7d}{:5d}{:5d}{:7d}{:8.1f}".format(
                    str(r.get("id")),
                    float(r.get("ret_total_pct", 0)),
                    delta,
                    float(r.get("max_dd_pct", 0)),
                    int(r.get("n_buy1", 0)),
                    int(r.get("n_buy1_a", 0)),
                    int(r.get("n_buy1_b", 0)),
                    int(r.get("n_buy2", 0)),
                    float(r.get("win_rate", 0)),
                )
            )


def _recommend(rows: List[Dict[str, Any]]) -> str:
    full = {r["id"]: r for r in rows if r.get("period") == "full" and "ret_total_pct" in r}
    oos = {r["id"]: r for r in rows if r.get("period") == "oos" and "ret_total_pct" in r}
    if (
        "BASE" not in full
        or "BASE" not in oos
        or "BLOCK_ALL" not in full
        or "BLOCK_ALL" not in oos
    ):
        return "권고: 데이터 부족"
    bf = float(full["BASE"]["ret_total_pct"])
    bo = float(oos["BASE"]["ret_total_pct"])
    af = float(full["BLOCK_ALL"]["ret_total_pct"])
    ao = float(oos["BLOCK_ALL"]["ret_total_pct"])
    df_ = af - bf
    do = ao - bo
    dd_o = float(oos["BLOCK_ALL"]["max_dd_pct"]) - float(oos["BASE"]["max_dd_pct"])
    nb = int(full["BLOCK_ALL"]["n_buy1"]) - int(full["BASE"]["n_buy1"])
    na = int(full["BLOCK_ALL"].get("n_buy1_a", 0)) - int(full["BASE"].get("n_buy1_a", 0))
    nbb = int(full["BLOCK_ALL"].get("n_buy1_b", 0)) - int(full["BASE"].get("n_buy1_b", 0))
    if do > 0.5 and df_ > -5.0:
        return (
            f"권고: BLOCK_ALL 채택 검토 — OOS Δ{do:+.1f}%p, FULL Δ{df_:+.1f}%p, "
            f"OOS MDD Δ{dd_o:+.1f}%p, BUY1 {nb:+d} (A {na:+d}/B {nbb:+d})"
        )
    return (
        f"권고: BASE 유지 — BLOCK_ALL OOS Δ{do:+.1f}%p, FULL Δ{df_:+.1f}%p, "
        f"OOS MDD Δ{dd_o:+.1f}%p, BUY1 {nb:+d} (A {na:+d}/B {nbb:+d})"
    )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    full_start = str(base.get("bt_start_date", "2024-06-03"))
    full_end = str(base.get("bt_end_date", "2026-07-27"))
    print(
        f"[block simul all] K200  FULL {full_start}~{full_end} / "
        f"OOS {OOS_START}~{full_end}"
    )
    _seed_base_from_cache(full_start, full_end)
    rows: List[Dict[str, Any]] = []
    for exp in EXPERIMENTS:
        rows.append(run_one(exp, base, full_start, full_end))
    for exp in EXPERIMENTS:
        rows.append(run_one(exp, base, OOS_START, full_end))
    (OUT_DIR / "summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _print_summary(rows)
    rec = _recommend(rows)
    print("\n" + rec)
    (OUT_DIR / "recommend.txt").write_text(rec + "\n", encoding="utf-8")
    print(f"\n저장: {OUT_DIR / 'summary.json'}")


if __name__ == "__main__":
    main()
