# -*- coding: utf-8 -*-
"""
A) S1 체결 후 손익에 맞춘 순위 기준 검증.

1) legacy 동적 백테 S1(BUY1→SELL) 손익과, 진입 전일(ref_day) 선정 피처 상관
2) 부호에 맞춘 ``sel_score_mode=s1_pnl_aligned_v1`` 식 도출·적용
3) legacy vs s1_pnl_aligned_v1 전체기간 BT + (선택) 후반 OOS 감각용 분할 리포트

  python tools/compare_s1_pnl_rank.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

OUT = ROOT / "tools" / "bt_compare_out_s1_pnl_rank"
START = "2024-06-03"
END = "2026-06-30"
# 피처 학습(부호)용 — 전반 / 백테 평가는 전체 + 후반만 따로 표기
FIT_END = "2025-06-30"


def _base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["use_market_breadth_gate"] = False
    st["bt_high_precision"] = False
    st["enforce_track_signal_match"] = True
    st["universe_index_choice"] = "KODEX200"
    st["sel_score_mode"] = "legacy"
    return st


def _spearman(a: pd.Series, b: pd.Series) -> Optional[float]:
    d = pd.DataFrame({"a": a, "b": b}).replace([np.inf, -np.inf], np.nan).dropna()
    if len(d) < 40:
        return None
    try:
        return float(d["a"].rank().corr(d["b"].rank()))
    except Exception:
        return None


def _pair_s1_rounds(trades: pd.DataFrame) -> pd.DataFrame:
    """BUY1 직후 같은 종목 첫 SELL을 라운드로 묶음(S2 포함 포지션의 최종 청산)."""
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
                    ret = float(r.get("Return", np.nan))
                except Exception:
                    ret = float("nan")
                # dynamic_backtest SELL Return 은 이미 % 단위
                ret_pct = ret
                rows.append(
                    {
                        "code": code,
                        "entry_dt": open_buy["Date"],
                        "exit_dt": r["Date"],
                        "sig_type": str(open_buy.get("Sig_Type", "")),
                        "entry_price": float(open_buy.get("Price", np.nan)),
                        "exit_price": float(r.get("Price", np.nan)),
                        "ret_pct": ret_pct,
                        "exit_reason": str(r.get("Reason", "")),
                    }
                )
                open_buy = None
            elif act == "BUY2":
                continue
    return pd.DataFrame(rows)


def _run_legacy_trades(settings: Dict[str, Any]) -> pd.DataFrame:
    print("[BT] legacy 트레이드 생성 중…")
    st = deepcopy(settings)
    st["sel_score_mode"] = "legacy"
    t0 = time.time()
    tester = DynamicBacktester(st)

    def cb(msg: str, val: Any = None) -> None:
        try:
            if str(msg).startswith("[") and "시계열" in str(msg) and val is not None:
                print(f"\r  {float(val):5.1f}%", end="", flush=True)
        except Exception:
            pass

    trades = tester.run(START, END, interval="15m", callback=cb, high_precision=False)
    print(f"\n  done {time.time() - t0:.1f}s")
    if not isinstance(trades, pd.DataFrame) or trades.empty:
        raise RuntimeError("legacy 트레이드 비어 있음")
    dest = OUT / "bt_legacy"
    dest.mkdir(parents=True, exist_ok=True)
    trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    return trades


def _load_panel(settings: Dict[str, Any]) -> pd.DataFrame:
    import importlib.util as ilu

    spec = ilu.spec_from_file_location(
        "diag_fwd", str(ROOT / "tools" / "diagnose_selection_forward_returns.py")
    )
    mod = ilu.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    dm = DataManagerV2()
    # legacy 스코어로 피처 패널 로드(Valid·보조열)
    st = deepcopy(settings)
    st["sel_score_mode"] = "legacy"
    df, _, _ = mod.load_scored_panel(st, START, END, dm)
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["d"] = pd.to_datetime(df["time"]).dt.normalize()
    return df


def _attach_ref_features(
    rounds: pd.DataFrame, panel: pd.DataFrame, trading_days: List[pd.Timestamp]
) -> pd.DataFrame:
    """진입일 직전 영업일 선정 피처 조인."""
    days = sorted(pd.to_datetime(trading_days))
    day_set = {pd.Timestamp(d).normalize() for d in days}
    # map entry_date -> prev trading day
    prev_map: Dict[pd.Timestamp, pd.Timestamp] = {}
    for i, d in enumerate(days):
        dn = pd.Timestamp(d).normalize()
        if i > 0:
            prev_map[dn] = pd.Timestamp(days[i - 1]).normalize()

    feat_cols = [
        "Score_A",
        "Score_B",
        "Valid_A",
        "Valid_B",
        "_sel_rs_pct",
        "_sel_pb_pct",
        "_sel_atr_pct",
        "_sel_b_rs_pct",
        "_sel_b_hi_pct",
        "_sel_b_atr_pct",
        "_sel_b_ma20_slope",
        "RSI",
        "OSC",
        "AvgVal5",
        "MA_Diff",
        "close",
        "MA20",
    ]
    use_cols = ["code", "d"] + [c for c in feat_cols if c in panel.columns]
    p = panel[use_cols].copy()
    # 빠른 조인: (code,d) 인덱스
    p = p.drop_duplicates(["code", "d"], keep="last")
    p = p.set_index(["code", "d"])

    out_rows: List[dict[str, Any]] = []
    for _, r in rounds.iterrows():
        entry_d = pd.Timestamp(r["entry_dt"]).normalize()
        ref = prev_map.get(entry_d)
        if ref is None:
            # entry가 캘린더에 없으면 날짜만으로 전일 탐색
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
        # 트랙별 사용 점수
        st = str(r.get("sig_type", "")).upper()
        if "TYPE_A" in st:
            row["score_used"] = row.get("Score_A", np.nan)
            row["track"] = "A"
        elif "TYPE_B" in st:
            row["score_used"] = row.get("Score_B", np.nan)
            row["track"] = "B"
        else:
            row["score_used"] = np.nan
            row["track"] = "?"
        try:
            row["ext_ma20"] = float(row["close"]) / float(row["MA20"]) - 1.0 if row.get("MA20") else np.nan
        except Exception:
            row["ext_ma20"] = np.nan
        out_rows.append(row)
    return pd.DataFrame(out_rows)


def _corr_report(df: pd.DataFrame, label: str) -> Dict[str, Any]:
    print(f"\n=== {label} n={len(df)} mean_ret={df['ret_pct'].mean():+.2f}% ===")
    feats_a = [
        "score_used",
        "Score_A",
        "_sel_rs_pct",
        "_sel_pb_pct",
        "_sel_atr_pct",
        "MA_Diff",
        "RSI",
        "OSC",
        "AvgVal5",
    ]
    feats_b = [
        "score_used",
        "Score_B",
        "_sel_b_rs_pct",
        "_sel_b_hi_pct",
        "_sel_b_atr_pct",
        "_sel_b_ma20_slope",
        "ext_ma20",
        "RSI",
        "OSC",
        "AvgVal5",
    ]
    out: Dict[str, Any] = {"n": len(df), "mean_ret": float(df["ret_pct"].mean()) if len(df) else None, "rho": {}}
    for track, feats in (("A", feats_a), ("B", feats_b), ("ALL", ["score_used", "AvgVal5", "RSI", "OSC"])):
        sub = df if track == "ALL" else df[df["track"] == track]
        if len(sub) < 30:
            continue
        print(f"  [{track}] n={len(sub)}")
        for f in feats:
            if f not in sub.columns:
                continue
            rho = _spearman(sub[f], sub["ret_pct"])
            out["rho"][f"{track}:{f}"] = rho
            if rho is not None:
                print(f"    {f:20s} rho={rho:+.4f}")
    return out


def _derive_signs(fit_df: pd.DataFrame) -> Dict[str, float]:
    """
    전반기 S1 손익 상관 부호 → s1_pnl_aligned_v1 가중.
    값: +1 높을수록 유리, -1 낮을수록 유리. |rho|<0.03 이면 0(미사용).
    """
    signs: Dict[str, float] = {}
    mapping = {
        "A:_sel_pb_pct": "a_pb",
        "A:_sel_atr_pct": "a_atr",
        "A:MA_Diff": "a_ma_diff",
        "A:AvgVal5": "a_liq",
        "A:RSI": "a_rsi",
        "A:_sel_rs_pct": "a_rs",
        "B:_sel_b_rs_pct": "b_rs",
        "B:_sel_b_hi_pct": "b_hi",
        "B:_sel_b_atr_pct": "b_atr",
        "B:AvgVal5": "b_liq",
        "B:OSC": "b_osc",
        "B:ext_ma20": "b_ext",
        "B:RSI": "b_rsi",
    }
    # compute
    raw_rho: Dict[str, Optional[float]] = {}
    for track, col, key in [
        ("A", "_sel_pb_pct", "a_pb"),
        ("A", "_sel_atr_pct", "a_atr"),
        ("A", "MA_Diff", "a_ma_diff"),
        ("A", "AvgVal5", "a_liq"),
        ("A", "RSI", "a_rsi"),
        ("A", "_sel_rs_pct", "a_rs"),
        ("B", "_sel_b_rs_pct", "b_rs"),
        ("B", "_sel_b_hi_pct", "b_hi"),
        ("B", "_sel_b_atr_pct", "b_atr"),
        ("B", "AvgVal5", "b_liq"),
        ("B", "OSC", "b_osc"),
        ("B", "ext_ma20", "b_ext"),
        ("B", "RSI", "b_rsi"),
    ]:
        sub = fit_df[fit_df["track"] == track]
        rho = _spearman(sub[col], sub["ret_pct"]) if col in sub.columns else None
        raw_rho[key] = rho
        if rho is None or abs(rho) < 0.03:
            signs[key] = 0.0
        else:
            signs[key] = 1.0 if rho > 0 else -1.0
    print("\n[부호 도출] FIT 구간 S1 ret 상관 → sign")
    for k, v in signs.items():
        print(f"  {k:12s} rho={raw_rho.get(k)} → sign={v:+.0f}")
    return {"signs": signs, "rho": raw_rho}


def run_bt(mode: str, settings: Dict[str, Any], start: str, end: str) -> Dict[str, Any]:
    st = deepcopy(settings)
    st["sel_score_mode"] = mode
    print(f"\n=== BT {mode} {start}~{end} ===")
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
    n_buy1 = int((trades["Action"] == "BUY1").sum()) if isinstance(trades, pd.DataFrame) else 0
    sells = trades[trades["Action"] == "SELL"] if isinstance(trades, pd.DataFrame) else pd.DataFrame()
    win = float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0
    dest = OUT / f"bt_{mode}_{start}_{end}".replace(":", "")
    dest.mkdir(parents=True, exist_ok=True)
    if isinstance(trades, pd.DataFrame):
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    daily.to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")
    return {
        "mode": mode,
        "start": start,
        "end": end,
        "ret_total_pct": (e1 / e0 - 1.0) * 100.0,
        "max_dd_pct": float(dd.min() * 100),
        "n_buy1": n_buy1,
        "win_rate": win,
        "elapsed_sec": round(elapsed, 1),
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    base = _base()

    # 1) legacy trades
    cache = OUT / "bt_legacy" / "trades.csv"
    if cache.is_file():
        print(f"[로드] {cache}")
        trades = pd.read_csv(cache)
    else:
        trades = _run_legacy_trades(base)

    rounds = _pair_s1_rounds(trades)
    print(f"[S1 라운드] {len(rounds)}건  mean_ret={rounds['ret_pct'].mean():+.2f}%")
    rounds.to_csv(OUT / "s1_rounds.csv", index=False, encoding="utf-8-sig")

    # 2) panel + join
    print("[패널] 선정 피처 로드…")
    panel = _load_panel(base)
    dm = DataManagerV2()
    tdays = [pd.to_datetime(d) for d in (dm.get_trading_days(START, END) or [])]
    joined = _attach_ref_features(rounds, panel, tdays)
    print(f"[조인] {len(joined)}/{len(rounds)} 라운드에 ref_day 피처")
    joined.to_csv(OUT / "s1_rounds_with_sel_features.csv", index=False, encoding="utf-8-sig")

    rep_all = _corr_report(joined, "전체기간 S1손익 ↔ ref_day 피처")
    fit = joined[joined["entry_dt"] <= pd.Timestamp(FIT_END)]
    oos = joined[joined["entry_dt"] > pd.Timestamp(FIT_END)]
    rep_fit = _corr_report(fit, f"FIT(~{FIT_END}) S1손익")
    if len(oos) >= 30:
        rep_oos = _corr_report(oos, f"OOS(>{FIT_END}) S1손익 (같은 피처, 참고)")
    else:
        rep_oos = {}

    derived = _derive_signs(fit)
    # 설정을 JSON으로 넘겨 strategy_selector가 읽게 함
    signs = derived["signs"]
    (OUT / "s1_pnl_signs.json").write_text(
        json.dumps(derived, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    # 3) strategy_selector에 이미 s1_pnl_aligned_v1 가 있으면 BT
    #    signs는 settings에 실어 전달
    base_s1 = deepcopy(base)
    base_s1["sel_score_mode"] = "s1_pnl_aligned_v1"
    base_s1["sel_s1_pnl_signs"] = signs

    # 구현 여부 확인
    from strategy_selector import _sel_score_mode

    # monkey: ensure mode recognized — we'll implement in strategy_selector before BT
    bts: List[Dict[str, Any]] = []
    for mode, st in (
        ("legacy", base),
        ("s1_pnl_aligned_v1", base_s1),
    ):
        try:
            bts.append(run_bt(mode, st, START, END))
        except Exception as e:
            traceback.print_exc()
            bts.append({"mode": mode, "error": str(e)})

    # OOS 구간만 재BT (동일 초기자금 — 절대수익 비교용 참고)
    bts_oos: List[Dict[str, Any]] = []
    oos_start = "2025-07-01"
    for mode, st in (
        ("legacy", base),
        ("s1_pnl_aligned_v1", base_s1),
    ):
        try:
            bts_oos.append(run_bt(mode, st, oos_start, END))
        except Exception as e:
            bts_oos.append({"mode": mode, "error": str(e)})

    payload = {
        "corr_all": rep_all,
        "corr_fit": rep_fit,
        "corr_oos_note": rep_oos,
        "signs": derived,
        "backtest_full": bts,
        "backtest_oos": bts_oos,
    }
    (OUT / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    print("\n" + "=" * 64)
    print("BT 전체기간")
    for r in bts:
        if "error" in r:
            print(f"  {r['mode']}: ERROR {r['error']}")
        else:
            print(
                f"  {r['mode']}: {r['ret_total_pct']:+.2f}% DD={r['max_dd_pct']:.1f}% "
                f"buy1={r['n_buy1']} win={r['win_rate']:.1f}%"
            )
    print(f"BT OOS {oos_start}~{END}")
    for r in bts_oos:
        if "error" in r:
            print(f"  {r['mode']}: ERROR {r['error']}")
        else:
            print(
                f"  {r['mode']}: {r['ret_total_pct']:+.2f}% DD={r['max_dd_pct']:.1f}% "
                f"buy1={r['n_buy1']} win={r['win_rate']:.1f}%"
            )
    print(f"\n[저장] {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
