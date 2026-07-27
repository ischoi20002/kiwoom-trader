# -*- coding: utf-8 -*-
"""
Type B 스코어링 검증.

1) legacy Score_B ↔ S1(Type B) 손익 상관·분위수 분석
2) S1-only BT: legacy vs b_s1_pnl_aligned_v1 (B만 교체) vs s1_pnl_aligned_v1 (참고)

  python tools/compare_type_b_score.py
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
from data_manager_v2 import DataManagerV2  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

OUT = ROOT / "tools" / "bt_compare_out_type_b_score"
START = "2024-06-03"
END = "2026-06-30"
OOS_START = "2025-07-01"
FIT_END = "2025-06-30"


def _base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["use_market_breadth_gate"] = False
    st["bt_high_precision"] = False
    st["enforce_track_signal_match"] = True
    st["universe_index_choice"] = "KODEX200"
    st["use_s2_master_a"] = False
    st["use_s2_master_b"] = False
    return st


def _load_b_signs() -> Dict[str, float]:
    p = ROOT / "tools" / "bt_compare_out_s1_pnl_rank" / "s1_pnl_signs.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        signs = data.get("signs") or {}
        return {k: float(v) for k, v in signs.items() if str(k).startswith("b_")}
    except Exception:
        return {}


def _spearman(a: pd.Series, b: pd.Series) -> Optional[float]:
    d = pd.DataFrame({"a": a, "b": b}).replace([np.inf, -np.inf], np.nan).dropna()
    if len(d) < 20:
        return None
    try:
        return float(d["a"].rank().corr(d["b"].rank()))
    except Exception:
        return None


def _pair_s1_rounds(trades: pd.DataFrame) -> pd.DataFrame:
    df = trades.copy()
    df["Date"] = pd.to_datetime(df["Date"], format="mixed", errors="coerce")
    df["Code"] = df["Code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    df = df.dropna(subset=["Date"]).sort_values(["Code", "Date"])
    rows: List[dict[str, Any]] = []
    for code, g in df.groupby("Code", sort=False):
        open_buy: Optional[pd.Series] = None
        for _, r in g.iterrows():
            act = str(r.get("Action", ""))
            if act == "BUY1":
                open_buy = r
            elif act == "SELL" and open_buy is not None:
                try:
                    ret_pct = float(r.get("Return", np.nan))
                except Exception:
                    ret_pct = float("nan")
                rows.append(
                    {
                        "code": code,
                        "entry_dt": open_buy["Date"],
                        "exit_dt": r["Date"],
                        "sig_type": str(open_buy.get("Sig_Type", "")),
                        "ret_pct": ret_pct,
                        "exit_reason": str(r.get("Reason", "")),
                    }
                )
                open_buy = None
    return pd.DataFrame(rows)


def _load_panel(settings: Dict[str, Any]) -> pd.DataFrame:
    import importlib.util as ilu

    spec = ilu.spec_from_file_location(
        "diag_fwd", str(ROOT / "tools" / "diagnose_selection_forward_returns.py")
    )
    mod = ilu.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    dm = DataManagerV2()
    st = deepcopy(settings)
    st["sel_score_mode"] = "legacy"
    df, _, _ = mod.load_scored_panel(st, START, END, dm)
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["d"] = pd.to_datetime(df["time"]).dt.normalize()
    return df


def _attach_ref_features(
    rounds: pd.DataFrame, panel: pd.DataFrame, trading_days: List[pd.Timestamp]
) -> pd.DataFrame:
    days = sorted(pd.to_datetime(trading_days))
    prev_map: Dict[pd.Timestamp, pd.Timestamp] = {}
    for i, d in enumerate(days):
        dn = pd.Timestamp(d).normalize()
        if i > 0:
            prev_map[dn] = pd.Timestamp(days[i - 1]).normalize()

    feat_cols = [
        "Score_B",
        "_sel_b_rs_pct",
        "_sel_b_hi_pct",
        "_sel_b_atr_pct",
        "_sel_b_ma20_slope",
        "RSI",
        "OSC",
        "AvgVal5",
        "close",
        "MA20",
    ]
    use_cols = ["code", "d"] + [c for c in feat_cols if c in panel.columns]
    p = panel[use_cols].drop_duplicates(["code", "d"], keep="last").set_index(["code", "d"])

    out_rows: List[dict[str, Any]] = []
    for _, r in rounds.iterrows():
        entry_d = pd.Timestamp(r["entry_dt"]).normalize()
        ref = prev_map.get(entry_d)
        if ref is None:
            earlier = [d for d in days if pd.Timestamp(d).normalize() < entry_d]
            ref = pd.Timestamp(earlier[-1]).normalize() if earlier else None
        if ref is None:
            continue
        key = (str(r["code"]).zfill(6), ref)
        if key not in p.index:
            continue
        feat = p.loc[key]
        if isinstance(feat, pd.DataFrame):
            feat = feat.iloc[-1]
        row = dict(r)
        row["ref_day"] = ref
        for c in use_cols:
            if c in ("code", "d"):
                continue
            try:
                row[c] = feat[c]
            except Exception:
                row[c] = np.nan
        try:
            row["ext_ma20"] = float(row["close"]) / float(row["MA20"]) - 1.0 if row.get("MA20") else np.nan
        except Exception:
            row["ext_ma20"] = np.nan
        out_rows.append(row)
    return pd.DataFrame(out_rows)


def _quintile_report(df: pd.DataFrame, score_col: str, label: str) -> Dict[str, Any]:
    sub = df.replace([np.inf, -np.inf], np.nan).dropna(subset=[score_col, "ret_pct"]).copy()
    if len(sub) < 25:
        return {"label": label, "n": len(sub), "quintiles": []}
    try:
        sub["q"] = pd.qcut(sub[score_col].rank(method="first"), 5, labels=["Q1(low)", "Q2", "Q3", "Q4", "Q5(high)"])
    except Exception:
        return {"label": label, "n": len(sub), "quintiles": []}
    rows = []
    print(f"\n=== {label} Score 분위수 vs S1 ret (n={len(sub)}) ===")
    for q, g in sub.groupby("q", observed=True):
        mean_ret = float(g["ret_pct"].mean())
        win = float((g["ret_pct"] > 0).mean() * 100.0)
        rows.append({"quintile": str(q), "n": len(g), "mean_ret_pct": mean_ret, "win_pct": win})
        print(f"  {q}: n={len(g):3d}  mean={mean_ret:+.2f}%  win={win:.1f}%")
    rho = _spearman(sub[score_col], sub["ret_pct"])
    print(f"  Spearman rho({score_col}, ret) = {rho:+.4f}" if rho is not None else "  rho=NA")
    return {"label": label, "n": len(sub), "rho": rho, "quintiles": rows}


def _add_aligned_b_score(df: pd.DataFrame, settings: Dict[str, Any]) -> pd.DataFrame:
    """패널 행에 s1_pnl_aligned B 스코어를 붙여 상관 비교."""
    try:
        one = df.copy()
        one["_sel_b_rs_pct"] = pd.to_numeric(one.get("_sel_b_rs_pct"), errors="coerce")
        one["_sel_b_hi_pct"] = pd.to_numeric(one.get("_sel_b_hi_pct"), errors="coerce")
        one["_sel_b_atr_pct"] = pd.to_numeric(one.get("_sel_b_atr_pct"), errors="coerce")
        one["OSC"] = pd.to_numeric(one.get("OSC"), errors="coerce")
        one["AvgVal5"] = pd.to_numeric(one.get("AvgVal5"), errors="coerce")
        sc = ss._score_b_s1_pnl_aligned_series(one, settings)
        out = df.copy()
        out["Score_B_aligned"] = sc.values
        return out
    except Exception:
        return df


def _trade_type_b_stats(trades: pd.DataFrame) -> Dict[str, Any]:
    if not isinstance(trades, pd.DataFrame) or trades.empty:
        return {"n_type_b": 0}
    buy1 = trades[trades["Action"] == "BUY1"].copy()
    buy1["sig"] = buy1["Sig_Type"].astype(str).str.upper()
    b_codes = set(buy1.loc[buy1["sig"].str.contains("TYPE_B"), "Code"].astype(str))
    if not b_codes:
        return {"n_type_b": 0}
    # Type B BUY1 이후 첫 SELL
    df = trades.copy()
    df["Date"] = pd.to_datetime(df["Date"], format="mixed", errors="coerce")
    df["Code"] = df["Code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    df = df.dropna(subset=["Date"]).sort_values(["Code", "Date"])
    rets: List[float] = []
    for code in b_codes:
        g = df[df["Code"] == code]
        open_buy = False
        for _, r in g.iterrows():
            act = str(r.get("Action", ""))
            sig = str(r.get("Sig_Type", "")).upper()
            if act == "BUY1" and "TYPE_B" in sig:
                open_buy = True
            elif act == "SELL" and open_buy:
                try:
                    rets.append(float(r.get("Return", np.nan)))
                except Exception:
                    pass
                open_buy = False
    if not rets:
        return {"n_type_b": len(b_codes), "n_rounds": 0}
    s = pd.Series(rets)
    return {
        "n_type_b_buy1": int((buy1["sig"].str.contains("TYPE_B")).sum()),
        "n_type_b_rounds": len(s),
        "type_b_mean_ret_pct": float(s.mean()),
        "type_b_win_pct": float((s > 0).mean() * 100.0),
        "type_b_sum_ret_pct": float(s.sum()),
    }


def run_bt(mode: str, settings: Dict[str, Any], start: str, end: str) -> Dict[str, Any]:
    st = deepcopy(settings)
    st["sel_score_mode"] = mode
    if mode in ("b_s1_pnl_aligned_v1", "s1_pnl_aligned_v1"):
        b_only = _load_b_signs()
        all_signs = _load_b_signs()
        # A 부호도 필요하면 전체 signs 로드
        p = ROOT / "tools" / "bt_compare_out_s1_pnl_rank" / "s1_pnl_signs.json"
        if p.is_file():
            try:
                all_signs = json.loads(p.read_text(encoding="utf-8")).get("signs") or {}
                all_signs = {k: float(v) for k, v in all_signs.items()}
            except Exception:
                all_signs = b_only
        st["sel_s1_pnl_signs"] = all_signs

    print(f"\n=== S1-only BT mode={mode} {start}~{end} ===")
    t0 = time.time()
    tester = DynamicBacktester(st)

    def cb(msg: str, val: Any = None) -> None:
        try:
            if str(msg).startswith("[") and "시계열" in str(msg) and val is not None:
                print(f"\r  {float(val):5.1f}%", end="", flush=True)
        except Exception:
            pass

    trades = tester.run(start, end, interval="15m", callback=cb, high_precision=False)
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

    dest = OUT / f"{mode}_{start}_{end}".replace(":", "")
    dest.mkdir(parents=True, exist_ok=True)
    if isinstance(trades, pd.DataFrame):
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    daily.to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")

    tb = _trade_type_b_stats(trades if isinstance(trades, pd.DataFrame) else pd.DataFrame())
    n_buy1 = int((trades["Action"] == "BUY1").sum()) if isinstance(trades, pd.DataFrame) else 0
    sells = trades[trades["Action"] == "SELL"] if isinstance(trades, pd.DataFrame) else pd.DataFrame()
    win = float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0

    return {
        "mode": mode,
        "start": start,
        "end": end,
        "ret_total_pct": (e1 / e0 - 1.0) * 100.0,
        "max_dd_pct": float(dd.min() * 100),
        "n_buy1": n_buy1,
        "win_rate": win,
        "elapsed_sec": round(elapsed, 1),
        **tb,
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    base = _base()
    b_signs = _load_b_signs()
    base_aligned = deepcopy(base)
    base_aligned["sel_s1_pnl_signs"] = b_signs

    # --- Part A: 상관·분위수 (legacy trades 필요) ---
    legacy_cache = OUT / "legacy_corr" / "trades.csv"
    if legacy_cache.is_file():
        print(f"[로드] {legacy_cache}")
        trades = pd.read_csv(legacy_cache)
    else:
        print("[BT] legacy 상관용 트레이드 생성…")
        run_bt("legacy", base, START, END)
        src = OUT / f"legacy_{START}_{END}".replace(":", "") / "trades.csv"
        legacy_cache.parent.mkdir(parents=True, exist_ok=True)
        trades = pd.read_csv(src)
        trades.to_csv(legacy_cache, index=False, encoding="utf-8-sig")

    rounds = _pair_s1_rounds(trades)
    rounds_b = rounds[rounds["sig_type"].astype(str).str.upper().str.contains("TYPE_B")].copy()
    print(f"\n[Type B S1 라운드] {len(rounds_b)}건  mean={rounds_b['ret_pct'].mean():+.2f}%")

    panel = _load_panel(base)
    dm = DataManagerV2()
    tdays = [pd.to_datetime(d) for d in (dm.get_trading_days(START, END) or [])]
    joined_b = _attach_ref_features(rounds_b, panel, tdays)
    joined_b.to_csv(OUT / "type_b_rounds_with_features.csv", index=False, encoding="utf-8-sig")

    corr_feats = [
        "Score_B",
        "_sel_b_rs_pct",
        "_sel_b_hi_pct",
        "_sel_b_atr_pct",
        "_sel_b_ma20_slope",
        "ext_ma20",
        "OSC",
        "AvgVal5",
    ]
    print("\n=== Type B 피처 ↔ S1 ret Spearman ===")
    corr_out: Dict[str, Optional[float]] = {}
    for f in corr_feats:
        if f not in joined_b.columns:
            continue
        rho = _spearman(joined_b[f], joined_b["ret_pct"])
        corr_out[f] = rho
        if rho is not None:
            print(f"  {f:22s} rho={rho:+.4f}")

    fit = joined_b[joined_b["entry_dt"] <= pd.Timestamp(FIT_END)]
    oos = joined_b[joined_b["entry_dt"] > pd.Timestamp(FIT_END)]
    q_legacy_all = _quintile_report(joined_b, "Score_B", "legacy 전체")
    q_legacy_fit = _quintile_report(fit, "Score_B", f"legacy FIT(~{FIT_END})")
    if len(oos) >= 25:
        q_legacy_oos = _quintile_report(oos, "Score_B", f"legacy OOS(>{FIT_END})")
    else:
        q_legacy_oos = {"n": len(oos)}

    joined_aligned = _add_aligned_b_score(joined_b, base_aligned)
    if "Score_B_aligned" in joined_aligned.columns:
        rho_aligned = _spearman(joined_aligned["Score_B_aligned"], joined_aligned["ret_pct"])
        print(f"\n[참고] aligned Score_B ↔ ret rho = {rho_aligned:+.4f}" if rho_aligned else "")
        q_aligned = _quintile_report(joined_aligned, "Score_B_aligned", "aligned (오프라인 시뮬)")
    else:
        rho_aligned = None
        q_aligned = {}

    # --- Part B: BT 비교 ---
    modes = ("legacy", "b_s1_pnl_aligned_v1", "s1_pnl_aligned_v1")
    bts_full: List[Dict[str, Any]] = []
    bts_oos: List[Dict[str, Any]] = []
    for mode in modes:
        try:
            bts_full.append(run_bt(mode, base, START, END))
        except Exception as e:
            traceback.print_exc()
            bts_full.append({"mode": mode, "error": str(e)})
        try:
            bts_oos.append(run_bt(mode, base, OOS_START, END))
        except Exception as e:
            bts_oos.append({"mode": mode, "error": str(e)})

    payload = {
        "type_b_rounds": len(rounds_b),
        "corr_all": corr_out,
        "quintile_legacy_all": q_legacy_all,
        "quintile_legacy_fit": q_legacy_fit,
        "quintile_legacy_oos": q_legacy_oos,
        "aligned_offline_rho": rho_aligned,
        "quintile_aligned": q_aligned,
        "backtest_full": bts_full,
        "backtest_oos": bts_oos,
        "b_signs_used": b_signs,
    }
    (OUT / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    print("\n" + "=" * 68)
    print("S1-only BT 전체기간")
    for r in bts_full:
        if "error" in r:
            print(f"  {r['mode']}: ERROR {r['error']}")
        else:
            print(
                f"  {r['mode']:22s} total={r['ret_total_pct']:+.2f}%  DD={r['max_dd_pct']:.1f}%  "
                f"buy1={r['n_buy1']}  TypeB mean={r.get('type_b_mean_ret_pct', 0):+.2f}%  "
                f"TypeB n={r.get('n_type_b_rounds', 0)}"
            )
    print(f"S1-only BT OOS {OOS_START}~{END}")
    for r in bts_oos:
        if "error" in r:
            print(f"  {r['mode']}: ERROR {r['error']}")
        else:
            print(
                f"  {r['mode']:22s} total={r['ret_total_pct']:+.2f}%  DD={r['max_dd_pct']:.1f}%  "
                f"buy1={r['n_buy1']}  TypeB mean={r.get('type_b_mean_ret_pct', 0):+.2f}%  "
                f"TypeB n={r.get('n_type_b_rounds', 0)}"
            )
    print(f"\n[저장] {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
