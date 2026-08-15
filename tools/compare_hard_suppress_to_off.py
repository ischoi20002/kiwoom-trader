# -*- coding: utf-8 -*-
"""
HARD 상승일 억제 + OFF 전용(TRANSITION 폴백 금지) FULL+OOS 비교.

BASE vs SUP2_OFF(≥2%) vs SUP3_OFF(≥3%).

  python tools/compare_hard_suppress_to_off.py
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
import strategy_selector as ss

importlib.reload(be)
importlib.reload(db)
importlib.reload(ss)

from backtest_engine import settings_for_backtest  # noqa: E402
from data_manager_v2 import DataManagerV2  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

OUT_DIR = ROOT / "tools" / "bt_compare_out_hard_suppress_to_off"
OOS_START = "2025-07-01"
FULL_START = "2024-06-03"
FULL_END = "2026-07-31"
PROBE_DAY = "2026-07-31"

EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "id": "BASE",
        "title": "현행 (suppress OFF)",
        "overrides": {
            "hard_suppress_on_index_up": False,
            "hard_suppress_to_off_only": False,
        },
    },
    {
        "id": "SUP2_OFF",
        "title": "억제≥2% + OFF만",
        "overrides": {
            "hard_suppress_on_index_up": True,
            "hard_suppress_index_up_pct": 2.0,
            "hard_suppress_to_off_only": True,
        },
    },
    {
        "id": "SUP3_OFF",
        "title": "억제≥3% + OFF만",
        "overrides": {
            "hard_suppress_on_index_up": True,
            "hard_suppress_index_up_pct": 3.0,
            "hard_suppress_to_off_only": True,
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
        return {"n_buy1": 0, "n_buy2": 0, "win_rate": 0.0}
    buys1 = trades[trades["Action"] == "BUY1"]
    buys2 = trades[trades["Action"] == "BUY2"]
    sells = trades[trades["Action"] == "SELL"]
    return {
        "n_buy1": int(len(buys1)),
        "n_buy2": int(len(buys2)),
        "win_rate": float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0,
    }


def _probe_regime_day(base: Dict[str, Any]) -> Dict[str, Any]:
    dm = DataManagerV2(DataManagerV2.DEFAULT_DB_PATH)
    out: Dict[str, Any] = {"probe_day": PROBE_DAY, "by_exp": {}, "flip_days": {}}
    maps: Dict[str, Dict[str, str]] = {}
    for exp in EXPERIMENTS:
        st = deepcopy(base)
        st.update(exp["overrides"])
        rmap = ss._compute_transition_regime_map_fdr(
            st, dm, FULL_START, FULL_END, logger_callback=lambda *_a, **_k: None
        )
        maps[exp["id"]] = rmap
        reg = str(rmap.get(PROBE_DAY, "MISSING"))
        out["by_exp"][exp["id"]] = {
            "regime": reg,
            "hard_days": sum(1 for v in rmap.values() if v == "HARD"),
            "transition_days": sum(1 for v in rmap.values() if v == "TRANSITION"),
            "n_days": len(rmap),
        }
        print(
            f"[probe] {exp['id']} {PROBE_DAY}={reg} "
            f"HARD={out['by_exp'][exp['id']]['hard_days']} "
            f"TRANS={out['by_exp'][exp['id']]['transition_days']}/{len(rmap)}"
        )

    base_map = maps.get("BASE", {})
    for eid, rmap in maps.items():
        if eid == "BASE":
            continue
        flips = sorted(
            d
            for d, r in base_map.items()
            if r == "HARD" and rmap.get(d, "HARD") != "HARD"
        )
        out["flip_days"][eid] = {
            "n": len(flips),
            "sample": flips[:20],
            "includes_probe": PROBE_DAY in flips,
            "probe_to": rmap.get(PROBE_DAY),
        }
        print(
            f"[probe] {eid} HARD→비HARD {len(flips)}일 "
            f"probe→{rmap.get(PROBE_DAY)}"
        )
    return out


def run_one(exp: Dict[str, Any], base: Dict[str, Any], start: str, end: str) -> Dict[str, Any]:
    period = "full" if start == FULL_START else "oos"
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
    st["bt_start_date"] = start
    st["bt_end_date"] = end

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
        print(f"HARD SUPPRESS→OFF {period.upper()}")
        print("=" * 96)
        print(
            f"  {'ID':10s} {'수익%':>9s} {'ΔBASE':>8s} {'만원':>8s} "
            f"{'MDD%':>8s} {'ΔMDD':>8s} {'BUY1':>6s}"
        )
        for r in sorted(sub, key=lambda x: float(x.get("ret_total_pct", -999)), reverse=True):
            delta = 0.0
            d_mdd = 0.0
            if base is not None:
                delta = float(r.get("ret_total_pct", 0)) - float(base.get("ret_total_pct", 0))
                d_mdd = float(r.get("max_dd_pct", 0)) - float(base.get("max_dd_pct", 0))
            print(
                f"  {str(r.get('id')):10s} "
                f"{float(r.get('ret_total_pct', 0)):+9.2f} "
                f"{delta:+8.2f} "
                f"{float(r.get('profit_krw', 0))/1e4:+8.0f} "
                f"{float(r.get('max_dd_pct', 0)):8.1f} "
                f"{d_mdd:+8.1f} "
                f"{int(r.get('n_buy1', 0)):6d}"
            )


def _judge(rows: List[Dict[str, Any]], probe: Dict[str, Any]) -> Dict[str, Any]:
    """
    채택: OOS Δret≥−0.5%p, OOS Δmdd≥−1%p, FULL Δret≥−5%p,
    probe_day 가 OFF (HARD/TRANSITION 아님).
    """
    oos = {r["id"]: r for r in rows if r.get("period") == "oos" and "ret_total_pct" in r}
    full = {r["id"]: r for r in rows if r.get("period") == "full" and "ret_total_pct" in r}
    result: Dict[str, Any] = {
        "adopt": False,
        "winner": None,
        "reason": "",
        "candidates": [],
    }
    if "BASE" not in oos:
        result["reason"] = "OOS BASE 없음"
        return result

    bo = float(oos["BASE"]["ret_total_pct"])
    bd = float(oos["BASE"]["max_dd_pct"])
    bf = float(full["BASE"]["ret_total_pct"]) if "BASE" in full else None
    probe_by = probe.get("by_exp") or {}
    passed: List[tuple[str, float]] = []
    details: List[Dict[str, Any]] = []

    for eid in ("SUP2_OFF", "SUP3_OFF"):
        if eid not in oos:
            details.append({"id": eid, "pass": False, "why": "OOS 없음"})
            continue
        ro = oos[eid]
        do = float(ro["ret_total_pct"]) - bo
        d_mdd = float(ro["max_dd_pct"]) - bd
        df_full = (
            float(full[eid]["ret_total_pct"]) - bf
            if eid in full and bf is not None
            else None
        )
        reg = str((probe_by.get(eid) or {}).get("regime", "MISSING"))
        ok_ret = do >= -0.5
        ok_mdd = d_mdd >= -1.0
        ok_full = True if df_full is None else df_full >= -5.0
        ok_probe = reg == "OFF"
        ok = ok_ret and ok_mdd and ok_full and ok_probe
        details.append(
            {
                "id": eid,
                "pass": ok,
                "oos_delta_ret": round(do, 3),
                "oos_delta_mdd": round(d_mdd, 3),
                "full_delta_ret": None if df_full is None else round(df_full, 3),
                "probe_regime": reg,
                "ok_ret": ok_ret,
                "ok_mdd": ok_mdd,
                "ok_full": ok_full,
                "ok_probe": ok_probe,
            }
        )
        if ok:
            passed.append((eid, do + 0.3 * d_mdd))

    result["candidates"] = details
    if not passed:
        result["reason"] = "미통과: " + "; ".join(
            f"{d['id']}(probe={d.get('probe_regime')}, "
            f"Δret={d.get('oos_delta_ret')}, Δmdd={d.get('oos_delta_mdd')}, "
            f"FULLΔ={d.get('full_delta_ret')})"
            for d in details
            if not d.get("pass")
        )
        return result

    passed.sort(key=lambda x: x[1], reverse=True)
    winner = passed[0][0]
    w = next(d for d in details if d["id"] == winner)
    result["adopt"] = True
    result["winner"] = winner
    result["reason"] = (
        f"채택 {winner}: probe={w['probe_regime']}, "
        f"OOS Δret={w['oos_delta_ret']:+.2f}%p, "
        f"OOS Δmdd={w['oos_delta_mdd']:+.2f}%p"
    )
    return result


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    print(
        f"[hard suppress→OFF] FULL {FULL_START}~{FULL_END} / "
        f"OOS {OOS_START}~{FULL_END}"
    )
    print("\n--- 레짐 프로브 ---")
    probe = _probe_regime_day(base)
    (OUT_DIR / "probe.json").write_text(
        json.dumps(probe, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    rows: List[Dict[str, Any]] = []
    for exp in EXPERIMENTS:
        rows.append(run_one(exp, base, FULL_START, FULL_END))
    for exp in EXPERIMENTS:
        rows.append(run_one(exp, base, OOS_START, FULL_END))

    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_summary(rows)
    judgment = _judge(rows, probe)
    (OUT_DIR / "judgment.json").write_text(
        json.dumps(judgment, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n" + judgment.get("reason", ""))
    print(f"adopt={judgment.get('adopt')} winner={judgment.get('winner')}")
    print(f"\n저장: {summary_path}")


if __name__ == "__main__":
    main()
