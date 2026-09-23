# -*- coding: utf-8 -*-
"""
G1 파라미터 그리드 스윕(사후) → 상위 후보 풀 BT 검증 → settings 반영.

스윕:
  a_share_min ∈ {0.50,0.55,0.60,0.65}
  idx5_min    ∈ {-1.0, 0.0, 0.5, 1.0}
  xs_pct      ∈ {50,55,60,65,70}

점수: OOS·AUGSEP Δ 우선, FULL이 크게 깎이지 않는 후보만.
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from itertools import product
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
sys.stdout.reconfigure(encoding="utf-8")

OUT = ROOT / "tools" / "bt_g1_param_opt"
OUT.mkdir(parents=True, exist_ok=True)
STATS_PATH = ROOT / "tools" / "bt_widen_soft_probe" / "daily_transition_stats.json"
TRADES = ROOT / "tools" / "bt_rs_filter_long_20240603_20260922" / "trades.parquet"
START = "2024-06-03"
END = "2026-09-22"


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
            print(f"[warn] settings: {e}")
    return s


def build_day_stats(settings: dict) -> Tuple[Dict[str, dict], List[float]]:
    import FinanceDataReader as fdr
    from strategy.selection_regime import classify_transition_regime

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
    xs_vals = [float(r["xs"]) for r in by_d.values() if r.get("xs") is not None]
    return by_d, xs_vals


def prev_stat(by_d: Dict[str, dict], d: str) -> Optional[dict]:
    prior = [x for x in sorted(by_d.keys()) if x < d]
    return by_d[prior[-1]] if prior else None


def load_a_roundtrips() -> List[dict]:
    trades = pd.read_parquet(TRADES)
    trades["Date"] = pd.to_datetime(trades["Date"], errors="coerce")
    trades = trades.dropna(subset=["Date"]).sort_values(["Code", "Date"])
    rts: List[dict] = []
    for code, g in trades.groupby("Code", sort=False):
        open_buys: List[pd.Series] = []
        for _, row in g.iterrows():
            act = str(row["Action"])
            if act.startswith("BUY"):
                open_buys.append(row)
            elif act in ("SELL", "HOLD") and open_buys:
                buys = open_buys
                open_buys = []
                b0 = buys[0]
                # S1 타입만 (BUY1)
                st = str(b0.get("Sig_Type") or "").upper()
                if "TYPE_A" not in st:
                    continue
                cost = sum(float(b["Amount"]) + float(b.get("Fee") or 0) for b in buys)
                proceeds = float(row["Amount"]) - float(row.get("Fee") or 0)
                buy_d = pd.Timestamp(buys[0]["Date"]).strftime("%Y-%m-%d")
                rts.append(
                    {
                        "pnl": proceeds - cost,
                        "buy_d": buy_d,
                    }
                )
    return rts


def in_slice(buy_d: str, sl: str) -> bool:
    if sl == "FULL":
        return True
    if sl == "TRAIN":
        return buy_d < "2026-01-01"
    if sl == "OOS":
        return buy_d >= "2026-01-01"
    if sl == "AUGSEP":
        return "2026-08-01" <= buy_d <= "2026-09-30"
    return True


def eval_params(
    by_d: Dict[str, dict],
    xs_vals: List[float],
    rts: List[dict],
    a_min: float,
    idx5_min: float,
    xs_pct: float,
) -> dict:
    from strategy.selection_regime import is_rotation_a_guard_g1_day

    xs_thr = float(np.nanpercentile(xs_vals, xs_pct))
    g1_ref: Dict[str, bool] = {}
    for d, s in by_d.items():
        g1_ref[d] = bool(
            is_rotation_a_guard_g1_day(
                regime=str(s.get("regime", "OFF")),
                idx5=s.get("idx5"),
                xs_ret_std=s.get("xs"),
                valid_a=int(s.get("va", 0) or 0),
                valid_b=int(s.get("vb", 0) or 0),
                xs_threshold=xs_thr,
                a_share_min=a_min,
                idx5_min=idx5_min,
            )
        )
    n_on = sum(1 for v in g1_ref.values() if v)
    pct_on = 100.0 * n_on / max(1, len(g1_ref))

    out: Dict[str, Any] = {
        "a_share_min": a_min,
        "idx5_min": idx5_min,
        "xs_pct": xs_pct,
        "xs_thr": round(xs_thr, 4),
        "n_g1_on": n_on,
        "pct_on": round(pct_on, 2),
    }
    sorted_days = sorted(by_d.keys())
    for sl in ("FULL", "TRAIN", "OOS", "AUGSEP"):
        rows = [r for r in rts if in_slice(r["buy_d"], sl)]
        base = sum(r["pnl"] for r in rows)
        cut_pnl = 0.0
        n_cut = 0
        for r in rows:
            prior = [x for x in sorted_days if x < r["buy_d"]]
            if not prior:
                continue
            ref = prior[-1]
            if not g1_ref.get(ref, False):
                continue
            n_cut += 1
            cut_pnl += r["pnl"]
        # 스킵 시 해당 PnL 제거 → delta = -cut_pnl
        delta = -cut_pnl
        out[f"{sl}_base"] = round(base)
        out[f"{sl}_delta"] = round(delta)
        out[f"{sl}_n_cut"] = n_cut
    # 점수: OOS+AUGSEP 중심, FULL 패널티, 발동률 2~12% 선호
    score = (
        float(out["OOS_delta"])
        + float(out["AUGSEP_delta"])
        + 0.25 * float(out["FULL_delta"])
    )
    if pct_on < 1.0 or pct_on > 12.0:
        score -= 2_000_000
    if out["FULL_delta"] < -3_000_000:
        score -= 3_000_000
    if out["OOS_delta"] < 0:
        score -= 1_000_000
    out["score"] = round(score)
    return out


def run_full_bt(label: str, params: dict) -> Dict[str, Any]:
    from backtest_engine import settings_for_backtest
    from dynamic_backtest import DynamicBacktester
    import reporting.trade_report as tr

    def _noop(*_a: Any, **_k: Any) -> None:
        print(f"[Report] skipped ({label})")

    tr.generate_trade_report = _noop  # type: ignore[assignment]

    base = _load_settings()
    base["bt_start_date"] = START
    base["bt_end_date"] = END
    base["use_rotation_a_guard_g1"] = True
    base["max_daily_new_s1_a"] = 0
    base["rotation_a_guard_a_share_min"] = float(params["a_share_min"])
    base["rotation_a_guard_idx5_min"] = float(params["idx5_min"])
    base["rotation_a_guard_xs_pct"] = float(params["xs_pct"])
    s = settings_for_backtest(base)
    s.update(
        {
            "use_rotation_a_guard_g1": True,
            "rotation_a_guard_a_share_min": float(params["a_share_min"]),
            "rotation_a_guard_idx5_min": float(params["idx5_min"]),
            "rotation_a_guard_xs_pct": float(params["xs_pct"]),
        }
    )
    cash = float(s.get("backtest_initial_capital", 10_000_000) or 10_000_000)
    dest = OUT / label
    dest.mkdir(parents=True, exist_ok=True)
    print(
        f"\n===== FULL BT {label} a={params['a_share_min']} "
        f"idx5={params['idx5_min']} xs_pct={params['xs_pct']} =====",
        flush=True,
    )
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
        # metrics
        df = pd.DataFrame(equity)
        dcol = "date" if "date" in df.columns else df.columns[0]
        df[dcol] = pd.to_datetime(df[dcol], errors="coerce")
        df = df.dropna(subset=[dcol]).sort_values(dcol)
        df["d"] = df[dcol].dt.normalize()
        vcol = "equity" if "equity" in df.columns else df.columns[-1]
        daily = df.groupby("d", as_index=False).last()

        def _at(day: str) -> float:
            sub = daily[daily["d"] <= pd.Timestamp(day)]
            return float(sub.iloc[-1][vcol]) if not sub.empty else cash

        e_end = float(daily.iloc[-1][vcol])
        e_2025e = _at("2025-12-31")
        e_aug0 = _at("2026-07-31")
        dd = (daily[vcol] - daily[vcol].cummax()) / daily[vcol].cummax()
        metrics = {
            "full_ret_pct": round((e_end / cash - 1) * 100, 2),
            "oos_2026_ret_pct": round((e_end / e_2025e - 1) * 100, 2),
            "augsep_ret_pct": round((e_end / e_aug0 - 1) * 100, 2),
            "max_dd_pct": round(float(dd.min() * 100), 2),
            "end_equity": round(e_end, 0),
            "n_buy1_a": int(
                (
                    (trades["Action"] == "BUY1")
                    & (trades["Sig_Type"].astype(str).str.upper() == "TYPE_A")
                ).sum()
            )
            if not trades.empty and "Sig_Type" in trades.columns
            else 0,
        }
        meta = {
            "label": label,
            "params": params,
            "elapsed_sec": round(elapsed, 1),
            "metrics": metrics,
            "n_g1_on": int(
                sum(
                    1
                    for v in (
                        getattr(bt, "rotation_a_guard_g1_by_ref_day", {}) or {}
                    ).values()
                    if v
                )
            ),
        }
        (dest / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"[{label}] done {elapsed:.1f}s full={metrics['full_ret_pct']}% "
            f"oos={metrics['oos_2026_ret_pct']}% augsep={metrics['augsep_ret_pct']}% "
            f"dd={metrics['max_dd_pct']}%",
            flush=True,
        )
        return meta
    except Exception as e:
        err = f"{e}\n{traceback.format_exc()}"
        (dest / "error.txt").write_text(err, encoding="utf-8")
        print(f"[{label}] FAILED: {e}", flush=True)
        return {"label": label, "error": str(e), "params": params}


def main() -> None:
    settings = _load_settings()
    print("[1/3] 일별 통계·라운드트립 로드...", flush=True)
    by_d, xs_vals = build_day_stats(settings)
    rts = load_a_roundtrips()
    print(f"  days={len(by_d)} A_rts={len(rts)}", flush=True)

    grid_a = [0.50, 0.55, 0.60, 0.65]
    grid_i = [-1.0, 0.0, 0.5, 1.0]
    grid_x = [50.0, 55.0, 60.0, 65.0, 70.0]

    print("[2/3] 사후 그리드 스윕...", flush=True)
    rows: List[dict] = []
    for a, i5, xp in product(grid_a, grid_i, grid_x):
        rows.append(eval_params(by_d, xs_vals, rts, a, i5, xp))
    rows.sort(key=lambda r: r["score"], reverse=True)
    (OUT / "sweep_posthoc.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("  Top 10 (posthoc):", flush=True)
    for r in rows[:10]:
        print(
            f"    a={r['a_share_min']} idx5={r['idx5_min']} xs={r['xs_pct']} "
            f"on={r['pct_on']}% FULLΔ={r['FULL_delta']:+,} OOSΔ={r['OOS_delta']:+,} "
            f"AUGΔ={r['AUGSEP_delta']:+,} score={r['score']:+,}",
            flush=True,
        )

    # 현재 설정(베이스라인) 위치
    cur = next(
        (
            r
            for r in rows
            if r["a_share_min"] == 0.55
            and r["idx5_min"] == 0.0
            and r["xs_pct"] == 60.0
        ),
        None,
    )
    print(f"  current(0.55/0/60): {cur}", flush=True)

    # 풀 BT 후보: top1 + current + (top에서 current와 다른 것 1~2)
    candidates: List[dict] = []
    seen = set()
    for r in rows:
        key = (r["a_share_min"], r["idx5_min"], r["xs_pct"])
        if key in seen:
            continue
        # 필터: OOS 비음수, FULL 크게 안 깎임, 발동 1~12%
        if r["OOS_delta"] < 0:
            continue
        if r["FULL_delta"] < -2_000_000:
            continue
        if r["pct_on"] < 1.0 or r["pct_on"] > 12.0:
            continue
        seen.add(key)
        candidates.append(r)
        if len(candidates) >= 3:
            break
    # current 항상 포함
    if cur and (cur["a_share_min"], cur["idx5_min"], cur["xs_pct"]) not in {
        (c["a_share_min"], c["idx5_min"], c["xs_pct"]) for c in candidates
    }:
        candidates.append(cur)

    print(f"\n[3/3] 풀 BT 검증 후보 {len(candidates)}개...", flush=True)
    bt_results = []
    for i, c in enumerate(candidates):
        label = f"C{i}_{c['a_share_min']}_{c['idx5_min']}_{int(c['xs_pct'])}".replace(
            ".", "p"
        )
        meta = run_full_bt(
            label,
            {
                "a_share_min": c["a_share_min"],
                "idx5_min": c["idx5_min"],
                "xs_pct": c["xs_pct"],
            },
        )
        meta["posthoc"] = {
            k: c[k]
            for k in (
                "FULL_delta",
                "OOS_delta",
                "AUGSEP_delta",
                "pct_on",
                "score",
            )
        }
        bt_results.append(meta)

    # BASE (G1 off) from long bt for reference
    base_eq = ROOT / "tools" / "bt_rs_filter_long_20240603_20260922" / "equity.json"
    base_metrics = None
    if base_eq.exists():
        equity = json.loads(base_eq.read_text(encoding="utf-8"))
        df = pd.DataFrame(equity)
        dcol = "date" if "date" in df.columns else df.columns[0]
        df[dcol] = pd.to_datetime(df[dcol], errors="coerce")
        df = df.dropna(subset=[dcol]).sort_values(dcol)
        df["d"] = df[dcol].dt.normalize()
        vcol = "equity" if "equity" in df.columns else df.columns[-1]
        daily = df.groupby("d", as_index=False).last()
        cash = 10_000_000.0

        def _at(day: str) -> float:
            sub = daily[daily["d"] <= pd.Timestamp(day)]
            return float(sub.iloc[-1][vcol]) if not sub.empty else cash

        e_end = float(daily.iloc[-1][vcol])
        base_metrics = {
            "full_ret_pct": round((e_end / cash - 1) * 100, 2),
            "oos_2026_ret_pct": round((e_end / _at("2025-12-31") - 1) * 100, 2),
            "augsep_ret_pct": round((e_end / _at("2026-07-31") - 1) * 100, 2),
        }

    # 승자: OOS 우선, AUGSEP, FULL (vs BASE)
    def bt_score(m: dict) -> float:
        met = m.get("metrics") or {}
        if not met or "error" in m:
            return -1e18
        b = base_metrics or {"full_ret_pct": 0, "oos_2026_ret_pct": 0, "augsep_ret_pct": 0}
        return (
            2.0 * (float(met["oos_2026_ret_pct"]) - float(b["oos_2026_ret_pct"]))
            + 2.0 * (float(met["augsep_ret_pct"]) - float(b["augsep_ret_pct"]))
            + 0.5 * (float(met["full_ret_pct"]) - float(b["full_ret_pct"]))
            - 0.3 * abs(float(met.get("max_dd_pct", 0)))  # dd는 음수, abs로 패널티
        )

    valid = [m for m in bt_results if "metrics" in m and "error" not in m]
    valid.sort(key=bt_score, reverse=True)
    winner = valid[0] if valid else None

    summary = {
        "base_no_g1": base_metrics,
        "sweep_top10": rows[:10],
        "bt_candidates": bt_results,
        "winner": winner,
    }
    (OUT / "opt_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    if winner and "params" in winner:
        wp = winner["params"]
        # settings.json 반영
        sp = ROOT / "settings.json"
        raw = json.loads(sp.read_text(encoding="utf-8"))
        raw["use_rotation_a_guard_g1"] = True
        raw["rotation_a_guard_a_share_min"] = float(wp["a_share_min"])
        raw["rotation_a_guard_idx5_min"] = float(wp["idx5_min"])
        raw["rotation_a_guard_xs_pct"] = float(wp["xs_pct"])
        sp.write_text(json.dumps(raw, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
        # defaults도 맞춤
        # (defaults는 코드 수정)
        print("\n===== WINNER =====", flush=True)
        print(f"  params={wp}", flush=True)
        print(f"  metrics={winner.get('metrics')}", flush=True)
        print(f"  vs BASE(no G1)={base_metrics}", flush=True)
        print(f"  → settings.json 반영 완료", flush=True)
    else:
        print("\n[warn] 승자 없음 — settings 미변경", flush=True)

    print(f"\nsaved {OUT / 'opt_summary.json'}", flush=True)


if __name__ == "__main__":
    main()
