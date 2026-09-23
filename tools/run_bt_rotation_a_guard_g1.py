# -*- coding: utf-8 -*-
"""
G1 로테이션 A 가드 풀 Dynamic BT 비교.

G1 (전일 기준): OFF · idx5≥-1 · xs≥p50 · A비중≥60% → 신규 TYPE_A S1 스킵
맵은 사후 타진과 동일 통계(daily_transition_stats + KS11)로 사전계산.

  python -u tools/run_bt_rotation_a_guard_g1.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

START = "2024-06-03"
END = "2026-09-22"
OUT = ROOT / "tools" / "bt_compare_out_rotation_a_guard_g1"
OUT.mkdir(parents=True, exist_ok=True)
BASE_DIR = ROOT / "tools" / "bt_rs_filter_long_20240603_20260922"
STATS_PATH = ROOT / "tools" / "bt_widen_soft_probe" / "daily_transition_stats.json"


def _load_settings() -> dict:
    from core.defaults import DEFAULT_SETTINGS

    s = dict(DEFAULT_SETTINGS)
    sp = ROOT / "settings.json"
    if sp.exists():
        try:
            raw = json.loads(sp.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                s.update(raw)
        except Exception as e:
            print(f"[warn] settings.json: {e}")
    return s


def build_g1_map(settings: dict) -> Tuple[Dict[str, bool], Dict[str, Any]]:
    """프로브와 동일 G1 맵. 키=ref_day(전일), 값=그날 통계로 G1 발동."""
    import FinanceDataReader as fdr
    from strategy.selection_regime import (
        classify_transition_regime,
        is_rotation_a_guard_g1_day,
    )

    stats = json.loads(STATS_PATH.read_text(encoding="utf-8"))
    ks = fdr.DataReader("KS11", "2024-01-01", "2026-09-30")
    ks.index = pd.to_datetime(ks.index).strftime("%Y-%m-%d")
    ks_c = ks["Close"].astype(float).sort_index()
    ks_days = list(ks_c.index)

    def n_ret(end: str, n: int) -> Optional[float]:
        days = [x for x in ks_days if x <= end]
        if len(days) < n + 1:
            return None
        return float(ks_c.loc[days[-1]] / ks_c.loc[days[-(n + 1)]] - 1.0) * 100.0

    by_d: Dict[str, dict] = {}
    for r in stats:
        d = r["d"]
        reg = classify_transition_regime(
            int(r["va"]),
            int(r["vb"]),
            float(r["share"]),
            r.get("d5"),
            r.get("d3"),
            settings,
            index_rvol_20=r.get("rvol20"),
            xs_ret_std=r.get("xs"),
            index_day_ret=r.get("day_ret"),
        )
        by_d[d] = {**r, "regime": reg, "idx5": n_ret(d, 5)}

    xs_vals = [r["xs"] for r in by_d.values() if r.get("xs") is not None]
    xs_pct = float(settings.get("rotation_a_guard_xs_pct", 50.0) or 50.0)
    xs_thr = float(np.nanpercentile(xs_vals, xs_pct))
    a_min = float(settings.get("rotation_a_guard_a_share_min", 0.60) or 0.60)
    _i5_raw = settings.get("rotation_a_guard_idx5_min", -1.0)
    idx5_min = float(-1.0 if _i5_raw is None else _i5_raw)

    g1_map: Dict[str, bool] = {}
    n_on = 0
    for d, s in by_d.items():
        on = is_rotation_a_guard_g1_day(
            regime=str(s.get("regime", "OFF")),
            idx5=s.get("idx5"),
            xs_ret_std=s.get("xs"),
            valid_a=int(s.get("va", 0) or 0),
            valid_b=int(s.get("vb", 0) or 0),
            xs_threshold=xs_thr,
            a_share_min=a_min,
            idx5_min=idx5_min,
        )
        g1_map[d] = bool(on)
        if on:
            n_on += 1

    meta = {
        "xs_thr": xs_thr,
        "xs_pct": xs_pct,
        "a_share_min": a_min,
        "idx5_min": idx5_min,
        "n_ref_days": len(g1_map),
        "n_g1_on": n_on,
        "pct": round(100.0 * n_on / max(1, len(g1_map)), 1),
        "sep_on": sorted(
            d for d, v in g1_map.items() if v and d.startswith("2026-09")
        ),
        "augsep_on": sorted(
            d
            for d, v in g1_map.items()
            if v and "2026-08-01" <= d <= "2026-09-30"
        ),
    }
    return g1_map, meta


def _equity_metrics(
    equity: List[dict],
    trades: pd.DataFrame,
    initial: float,
) -> Dict[str, Any]:
    if not equity:
        return {"error": "no equity"}
    df = pd.DataFrame(equity)
    dcol = None
    for c in ("date", "Date", "datetime", "time"):
        if c in df.columns:
            dcol = c
            break
    if dcol is None:
        dcol = df.columns[0]
    df[dcol] = pd.to_datetime(df[dcol], errors="coerce")
    df = df.dropna(subset=[dcol]).sort_values(dcol)
    df["d"] = df[dcol].dt.normalize()
    vcol = "equity" if "equity" in df.columns else (
        "Total" if "Total" in df.columns else df.columns[-1]
    )
    daily = df.groupby("d", as_index=False).last()
    e0 = float(initial)

    def _at(day: str) -> float:
        sub = daily[daily["d"] <= pd.Timestamp(day)]
        return float(sub.iloc[-1][vcol]) if not sub.empty else e0

    e_end = float(daily.iloc[-1][vcol])
    dd = (daily[vcol] - daily[vcol].cummax()) / daily[vcol].cummax()
    max_dd = float(dd.min() * 100) if len(dd) else 0.0
    e_2025e = _at("2025-12-31")
    e_aug0 = _at("2026-07-31")
    full_ret = (e_end / e0 - 1.0) * 100
    oos_ret = (e_end / e_2025e - 1.0) * 100 if e_2025e > 0 else None
    augsep_ret = (e_end / e_aug0 - 1.0) * 100 if e_aug0 > 0 else None

    n_buy1 = n_buy1_a = n_buy2 = 0
    if trades is not None and not trades.empty:
        n_buy1 = int((trades["Action"] == "BUY1").sum())
        if "Sig_Type" in trades.columns:
            n_buy1_a = int(
                (
                    (trades["Action"] == "BUY1")
                    & (trades["Sig_Type"].astype(str).str.upper() == "TYPE_A")
                ).sum()
            )
        n_buy2 = int((trades["Action"] == "BUY2").sum())

    return {
        "full_ret_pct": round(full_ret, 2),
        "oos_2026_ret_pct": round(oos_ret, 2) if oos_ret is not None else None,
        "augsep_ret_pct": round(augsep_ret, 2) if augsep_ret is not None else None,
        "end_equity": round(e_end, 0),
        "max_dd_pct": round(max_dd, 2),
        "n_buy1": n_buy1,
        "n_buy1_a": n_buy1_a,
        "n_buy2": n_buy2,
        "e_2025e": round(e_2025e, 0),
        "e_aug0": round(e_aug0, 0),
    }


def _load_base() -> Tuple[Optional[pd.DataFrame], List[dict], Dict[str, Any]]:
    trades = None
    pq = BASE_DIR / "trades.parquet"
    if pq.exists():
        trades = pd.read_parquet(pq)
    equity: List[dict] = []
    eqp = BASE_DIR / "equity.json"
    if eqp.exists():
        equity = json.loads(eqp.read_text(encoding="utf-8"))
    meta: Dict[str, Any] = {}
    mp = BASE_DIR / "meta.json"
    if mp.exists():
        meta = json.loads(mp.read_text(encoding="utf-8"))
    return trades, equity, meta


def run_g1(g1_map: Dict[str, bool]) -> Dict[str, Any]:
    from backtest_engine import settings_for_backtest
    from dynamic_backtest import DynamicBacktester
    import reporting.trade_report as tr

    def _noop(*_a: Any, **_k: Any) -> None:
        print("[Report] skipped (G1)")

    tr.generate_trade_report = _noop  # type: ignore[assignment]

    base = _load_settings()
    base["bt_start_date"] = START
    base["bt_end_date"] = END
    base["use_rotation_a_guard_g1"] = True
    base["max_daily_new_s1_a"] = 0
    base["_precomputed_rotation_a_guard_g1"] = g1_map
    s = settings_for_backtest(base)
    # settings_for_backtest copies dict — re-inject after copy
    s["use_rotation_a_guard_g1"] = True
    s["_precomputed_rotation_a_guard_g1"] = g1_map
    cash = float(s.get("backtest_initial_capital", 10_000_000) or 10_000_000)

    dest = OUT / "G1"
    dest.mkdir(parents=True, exist_ok=True)
    print(f"\n===== G1 ON {START}~{END} =====", flush=True)
    t0 = time.time()
    try:
        bt = DynamicBacktester(s, total_budget=cash)
        trades = bt.run(START, END, interval="15m", callback=None, high_precision=False)
        elapsed = time.time() - t0
        equity = list(getattr(bt, "daily_equity", []) or [])
        if trades is None:
            trades = pd.DataFrame()
        try:
            trades.to_parquet(dest / "trades.parquet", index=False)
        except Exception:
            trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
        (dest / "equity.json").write_text(
            json.dumps(equity, ensure_ascii=False, default=str), encoding="utf-8"
        )
        metrics = _equity_metrics(equity, trades, cash)
        meta = {
            "label": "G1",
            "use_rotation_a_guard_g1": True,
            "start": START,
            "end": END,
            "elapsed_sec": round(elapsed, 1),
            "initial_cash": cash,
            "n_g1_ref_days_on": int(
                sum(
                    1
                    for v in (
                        getattr(bt, "rotation_a_guard_g1_by_ref_day", {}) or {}
                    ).values()
                    if v
                )
            ),
            "metrics": metrics,
        }
        (dest / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"[G1] done {elapsed:.1f}s full={metrics.get('full_ret_pct')}% "
            f"oos={metrics.get('oos_2026_ret_pct')}% "
            f"augsep={metrics.get('augsep_ret_pct')}% "
            f"A_BUY1={metrics.get('n_buy1_a')}",
            flush=True,
        )
        return meta
    except Exception as e:
        err = f"{e}\n{traceback.format_exc()}"
        (dest / "error.txt").write_text(err, encoding="utf-8")
        print(f"[G1] FAILED: {e}", flush=True)
        return {"label": "G1", "error": str(e)}


def main() -> None:
    settings = _load_settings()
    g1_map, g1_meta = build_g1_map(settings)
    (OUT / "g1_map_meta.json").write_text(
        json.dumps(g1_meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"[G1 map] on={g1_meta['n_g1_on']}/{g1_meta['n_ref_days']} "
        f"({g1_meta['pct']}%) xs_thr={g1_meta['xs_thr']:.3f} "
        f"augsep={g1_meta['augsep_on']} sep={g1_meta['sep_on']}",
        flush=True,
    )

    results: Dict[str, Any] = {}
    trades, equity, base_meta = _load_base()
    cash = float(base_meta.get("initial_cash", 10_000_000) or 10_000_000)
    if trades is not None and equity:
        m = _equity_metrics(equity, trades, cash)
        results["BASE"] = {
            "label": "BASE",
            "use_rotation_a_guard_g1": False,
            "source": str(BASE_DIR),
            "metrics": m,
        }
        print(
            f"[BASE] full={m.get('full_ret_pct')}% oos={m.get('oos_2026_ret_pct')}% "
            f"augsep={m.get('augsep_ret_pct')}% A_BUY1={m.get('n_buy1_a')}",
            flush=True,
        )
    else:
        print("[BASE] missing", flush=True)

    results["G1"] = run_g1(g1_map)

    compare = {}
    base_m = (results.get("BASE") or {}).get("metrics") or {}
    for k, v in results.items():
        m = v.get("metrics") or {}
        if not m or "error" in v:
            compare[k] = v
            continue
        compare[k] = {
            **m,
            "d_full_pp": round(
                float(m.get("full_ret_pct") or 0) - float(base_m.get("full_ret_pct") or 0),
                2,
            ),
            "d_oos_pp": round(
                float(m.get("oos_2026_ret_pct") or 0)
                - float(base_m.get("oos_2026_ret_pct") or 0),
                2,
            ),
            "d_augsep_pp": round(
                float(m.get("augsep_ret_pct") or 0)
                - float(base_m.get("augsep_ret_pct") or 0),
                2,
            ),
        }

    (OUT / "compare.json").write_text(
        json.dumps(
            {
                "start": START,
                "end": END,
                "g1_meta": g1_meta,
                "results": results,
                "compare": compare,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print("\n===== COMPARE vs BASE =====", flush=True)
    for k in ("BASE", "G1"):
        c = compare.get(k) or {}
        if "full_ret_pct" not in c:
            print(f"  {k}: {c}", flush=True)
            continue
        print(
            f"  {k}: full={c['full_ret_pct']}% (Δ{c.get('d_full_pp'):+}) "
            f"oos={c['oos_2026_ret_pct']}% (Δ{c.get('d_oos_pp'):+}) "
            f"augsep={c['augsep_ret_pct']}% (Δ{c.get('d_augsep_pp'):+}) "
            f"A_BUY1={c.get('n_buy1_a')} MaxDD={c.get('max_dd_pct')}%",
            flush=True,
        )
    print(f"\nsaved {OUT / 'compare.json'}", flush=True)


if __name__ == "__main__":
    main()
