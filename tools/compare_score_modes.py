# -*- coding: utf-8 -*-
"""
sel_score_mode: legacy vs fwd_aligned_v1 비교.

1) Valid 안 Score↔선행수익 정렬(Spearman·상하20% 스프레드)
2) 동일 슬롯·KODEX·교차거절 ON 동적 백테

  python tools/compare_score_modes.py
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

import numpy as np
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
from core.defaults import DEFAULT_SETTINGS  # noqa: E402
from data_manager_v2 import DataManagerV2  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

OUT = ROOT / "tools" / "bt_compare_out_score_mode"
START = "2024-06-03"
END = "2026-06-30"
HORIZONS = (1, 5, 20)


def _base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["use_market_breadth_gate"] = False
    st["bt_high_precision"] = False
    st["enforce_track_signal_match"] = True
    st["universe_index_choice"] = "KODEX200"
    return st


def _spearman(a: pd.Series, b: pd.Series) -> Optional[float]:
    d = pd.DataFrame({"a": a, "b": b}).replace([np.inf, -np.inf], np.nan).dropna()
    if len(d) < 80:
        return None
    try:
        return float(d["a"].rank().corr(d["b"].rank()))
    except Exception:
        return None


def _top_bot(score: pd.Series, ret: pd.Series) -> Optional[float]:
    d = pd.DataFrame({"s": score, "r": ret}).replace([np.inf, -np.inf], np.nan).dropna()
    if len(d) < 50:
        return None
    hi = d["s"] >= d["s"].quantile(0.8)
    lo = d["s"] <= d["s"].quantile(0.2)
    try:
        return float(d.loc[hi, "r"].mean() - d.loc[lo, "r"].mean())
    except Exception:
        return None


def diagnose_mode(mode: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    import importlib.util as ilu

    spec = ilu.spec_from_file_location(
        "diag_fwd", str(ROOT / "tools" / "diagnose_selection_forward_returns.py")
    )
    mod = ilu.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)

    st = deepcopy(settings)
    st["sel_score_mode"] = mode
    dm = DataManagerV2()
    df, _, _ = mod.load_scored_panel(st, START, END, dm)
    df = mod.add_forward_returns(df, list(HORIZONS))
    out: Dict[str, Any] = {"mode": mode, "tracks": {}}
    for track, vcol, scol in (
        ("A", "Valid_A", "Score_A"),
        ("B", "Valid_B", "Score_B"),
    ):
        mask = df[vcol].astype(bool)
        block: Dict[str, Any] = {"n_valid_rows": int(mask.sum())}
        for h in HORIZONS:
            rc = f"fwd_ret_{h}d"
            sub = df.loc[mask & df[rc].notna()]
            block[f"rho_{h}d"] = _spearman(sub[scol], sub[rc])
            block[f"topbot_{h}d"] = _top_bot(sub[scol], sub[rc])
            block[f"mean_{h}d"] = float(sub[rc].mean()) if len(sub) else None
        # 스케줄 상위만 5d
        # schedule rebuild
        from strategy_selector import _df_top_by_score_slot_band

        max_sa = int(st.get("max_slots_a", 20))
        max_sb = int(st.get("max_slots_b", 15))
        max_n = max_sa if track == "A" else max_sb
        sched_rets: List[float] = []
        for d_str, day in df.groupby(df["time"].dt.strftime("%Y-%m-%d")):
            day_v = day.loc[day[vcol].astype(bool)]
            if day_v.empty:
                continue
            top = _df_top_by_score_slot_band(
                day_v,
                score_col=scol,
                code_col="code",
                max_n=max_n,
                settings=st,
                min_score_floor=0.0,
            )
            if top is None or top.empty:
                continue
            codes = set(top["code"].astype(str).str.zfill(6))
            m = day["code"].astype(str).str.zfill(6).isin(codes) & day["fwd_ret_5d"].notna()
            if m.any():
                sched_rets.append(float(day.loc[m, "fwd_ret_5d"].mean()))
        block["sched_mean_5d"] = float(np.mean(sched_rets)) if sched_rets else None
        out["tracks"][track] = block
    return out


def run_bt(mode: str, settings: Dict[str, Any]) -> Dict[str, Any]:
    st = deepcopy(settings)
    st["sel_score_mode"] = mode
    print(f"\n=== BT mode={mode} ===")
    t0 = time.time()
    tester = DynamicBacktester(st)

    def cb(msg: str, val: Any = None) -> None:
        try:
            if str(msg).startswith("[") and "시계열" in str(msg) and val is not None:
                print(f"\r  {float(val):5.1f}%", end="", flush=True)
        except Exception:
            pass

    trades = tester.run(START, END, interval="15m", callback=cb, high_precision=False)
    elapsed = time.time() - t0
    print(f"\n  done {elapsed:.1f}s")
    eq = pd.DataFrame(tester.daily_equity)
    eq["date"] = pd.to_datetime(eq["date"], format="mixed", errors="coerce")
    eq = eq.dropna(subset=["date"]).sort_values("date")
    eq["d"] = eq["date"].dt.normalize()
    daily = eq.groupby("d", as_index=False).last()
    col = "equity" if "equity" in daily.columns else "Total"
    e0 = float(tester.initial_cash)
    e1 = float(daily.iloc[-1][col])
    dd = (daily[col] - daily[col].cummax()) / daily[col].cummax()
    n_buy1 = 0
    win = 0.0
    if isinstance(trades, pd.DataFrame) and not trades.empty:
        n_buy1 = int((trades["Action"] == "BUY1").sum())
        sells = trades[trades["Action"] == "SELL"]
        win = float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0
        dest = OUT / f"bt_{mode}"
        dest.mkdir(parents=True, exist_ok=True)
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
        daily.to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")
    return {
        "mode": mode,
        "ret_total_pct": (e1 / e0 - 1.0) * 100.0,
        "max_dd_pct": float(dd.min() * 100),
        "n_buy1": n_buy1,
        "win_rate": win,
        "elapsed_sec": round(elapsed, 1),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    base = _base()
    modes = ["legacy", "fwd_aligned_v1"]
    diags = []
    print("[1/2] Score↔선행수익 정렬 진단")
    for m in modes:
        d = diagnose_mode(m, base)
        diags.append(d)
        print(f"\n  mode={m}")
        for tr, b in d["tracks"].items():
            print(
                f"    {tr}: rho5={b.get('rho_5d')} topbot5={b.get('topbot_5d')} "
                f"sched5d={b.get('sched_mean_5d')} rho1={b.get('rho_1d')} rho20={b.get('rho_20d')}"
            )

    print("\n[2/2] 동적 백테")
    bts = []
    for m in modes:
        try:
            bts.append(run_bt(m, base))
        except Exception as e:
            traceback.print_exc()
            bts.append({"mode": m, "error": str(e)})

    payload = {"diagnose": diags, "backtest": bts, "start": START, "end": END}
    (OUT / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print("\n" + "=" * 64)
    print("백테 요약")
    for r in bts:
        if "error" in r:
            print(f"  {r['mode']}: ERROR {r['error']}")
            continue
        print(
            f"  {r['mode']}: total={r['ret_total_pct']:+.2f}% DD={r['max_dd_pct']:.1f}% "
            f"buy1={r['n_buy1']} win={r['win_rate']:.1f}%"
        )
    if len(bts) >= 2 and "ret_total_pct" in bts[0] and "ret_total_pct" in bts[1]:
        dlt = bts[1]["ret_total_pct"] - bts[0]["ret_total_pct"]
        print(f"  Δ(aligned−legacy) = {dlt:+.2f}%p")
    print(f"\n[저장] {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
