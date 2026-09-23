# -*- coding: utf-8 -*-
"""
로테이션·보합+장 Type A 가드 — 가능성 타진 (사후, 풀 BT 없음).

아이디어: 기존 SOFT(B비중 급락)와 직교.
  - 지수는 보합~상승
  - 종목 분산(xs) 높음 (로테이션)
  - B비중 붕괴(SOFT)는 아님
  → 그날 신규 Type A만 스킵(가정)

판정은 진입일 D의 전일(D-1) 통계만 사용 (장중 미래정보 없음).
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

import FinanceDataReader as fdr

ROOT = Path(r"c:/vibe")
sys.path.insert(0, str(ROOT))
import os

os.chdir(ROOT)
OUT = ROOT / "tools" / "bt_rotation_a_guard_probe"
OUT.mkdir(parents=True, exist_ok=True)

STATS = json.loads(
    (ROOT / "tools" / "bt_widen_soft_probe" / "daily_transition_stats.json").read_text(
        encoding="utf-8"
    )
)
TRADES = ROOT / "tools" / "bt_rs_filter_long_20240603_20260922" / "trades.parquet"

# --- index returns ---
ks = fdr.DataReader("KS11", "2024-01-01", "2026-09-30")
ks.index = pd.to_datetime(ks.index).strftime("%Y-%m-%d")
ks_c = ks["Close"].astype(float).sort_index()
ks_days = list(ks_c.index)


def n_ret(end: str, n: int) -> Optional[float]:
    if end not in ks_c.index:
        prior = [x for x in ks_days if x <= end]
        if not prior:
            return None
        end = prior[-1]
    days = [x for x in ks_days if x <= end]
    if len(days) < n + 1:
        return None
    return float(ks_c.loc[days[-1]] / ks_c.loc[days[-(n + 1)]] - 1.0) * 100.0


# enrich stats with index rets + regime label under CUR settings
from copy import deepcopy
from core.defaults import DEFAULT_SETTINGS
from strategy.selection_regime import classify_transition_regime

base = dict(DEFAULT_SETTINGS)
raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
base.update(raw)

by_d: Dict[str, dict] = {}
for r in STATS:
    d = r["d"]
    reg = classify_transition_regime(
        int(r["va"]),
        int(r["vb"]),
        float(r["share"]),
        r.get("d5"),
        r.get("d3"),
        base,
        index_rvol_20=r.get("rvol20"),
        xs_ret_std=r.get("xs"),
        index_day_ret=r.get("day_ret"),
    )
    by_d[d] = {
        **r,
        "regime": reg,
        "idx5": n_ret(d, 5),
        "idx10": n_ret(d, 10),
        "idx20": n_ret(d, 20),
    }

# xs percentiles (for relative thresholds)
xs_vals = [r["xs"] for r in by_d.values() if r.get("xs") is not None]
xs_p50 = float(np.nanpercentile(xs_vals, 50))
xs_p60 = float(np.nanpercentile(xs_vals, 60))
xs_p70 = float(np.nanpercentile(xs_vals, 70))
print(f"xs p50={xs_p50:.2f} p60={xs_p60:.2f} p70={xs_p70:.2f}")


def prev_stat(d: str) -> Optional[dict]:
    """진입일 d에 쓸 전일 통계."""
    prior = [x for x in sorted(by_d.keys()) if x < d]
    return by_d[prior[-1]] if prior else None


GuardFn = Callable[[dict], bool]


def G1(s: dict) -> bool:
    """보합+ · 분산↑ · OFF · A우세."""
    if s.get("regime") != "OFF":
        return False
    if s.get("idx5") is None or s.get("xs") is None:
        return False
    if s["idx5"] < 0:
        return False
    if s["xs"] < xs_p60:
        return False
    tot = s["va"] + s["vb"]
    if tot <= 0 or s["va"] / tot < 0.55:
        return False
    return True


def G2(s: dict) -> bool:
    """지수 20일≥0 · xs≥p60 · SOFT 아님(B붕괴 아님)."""
    if s.get("regime") == "HARD":
        return False
    if s.get("regime") == "TRANSITION":
        return False  # 기존 soft와 분리
    if s.get("idx20") is None or s.get("xs") is None:
        return False
    return s["idx20"] >= 0 and s["xs"] >= xs_p60


def G3(s: dict) -> bool:
    """로테이션: xs≥p70 & idx5≥-1 & d5>-15 (B급락 없음)."""
    if s.get("xs") is None or s.get("idx5") is None:
        return False
    d5 = s.get("d5")
    if d5 is not None and d5 <= -15:
        return False
    return s["xs"] >= xs_p70 and s["idx5"] >= -1.0


def G4(s: dict) -> bool:
    """타이트: idx5>1 & xs≥p70 & OFF & va≥12."""
    if s.get("regime") != "OFF":
        return False
    if s.get("idx5") is None or s.get("xs") is None:
        return False
    return s["idx5"] > 1.0 and s["xs"] >= xs_p70 and s["va"] >= 12


GUARDS: Dict[str, GuardFn] = {
    "G1_up_xs_Aheavy": G1,
    "G2_idx20_xs_notSoft": G2,
    "G3_hiXS_noBcrash": G3,
    "G4_tight_up_xs": G4,
}


def guard_on_buy_day(buy_d: str, gfn: GuardFn) -> bool:
    s = prev_stat(buy_d)
    if s is None:
        return False
    try:
        return bool(gfn(s))
    except Exception:
        return False


# --- rebuild roundtrips ---
trades = pd.read_parquet(TRADES)
trades["Date"] = pd.to_datetime(trades["Date"], errors="coerce")
trades = trades.dropna(subset=["Date"]).sort_values(["Code", "Date"])

rts: List[dict] = []
for code, g in trades.groupby("Code", sort=False):
    open_buys: List[pd.Series] = []
    name = str(g.iloc[0]["Name"])
    for _, row in g.iterrows():
        act = str(row["Action"])
        if act.startswith("BUY"):
            open_buys.append(row)
        elif act in ("SELL", "HOLD") and open_buys:
            buys = open_buys
            open_buys = []
            types: List[str] = []
            for b in buys:
                blob = (str(b.get("Sig_Type") or "") + " " + str(b.get("Reason") or "")).upper()
                if "TYPE_C" in blob:
                    types.append("C")
                elif "TYPE_B" in blob:
                    types.append("B")
                elif "TYPE_A" in blob:
                    types.append("A")
                elif "BUY2" in str(b["Action"]).upper():
                    types.append("S2")
                else:
                    types.append("?")
            cost = sum(float(b["Amount"]) + float(b.get("Fee") or 0) for b in buys)
            proceeds = float(row["Amount"]) - float(row.get("Fee") or 0)
            buy_d = pd.Timestamp(buys[0]["Date"]).strftime("%Y-%m-%d")
            rts.append(
                {
                    "code": str(code).zfill(6) if str(code).isdigit() else str(code),
                    "name": name,
                    "entry": "+".join(types),
                    "is_a": str(types[0] if types else "").startswith("A")
                    or (types and types[0] == "A"),
                    "pnl": proceeds - cost,
                    "buy_d": buy_d,
                    "sell_d": pd.Timestamp(row["Date"]).strftime("%Y-%m-%d"),
                    "year": int(pd.Timestamp(row["Date"]).year),
                }
            )

# fix is_a
for r in rts:
    r["is_a"] = str(r["entry"]).startswith("A")

print(f"roundtrips={len(rts)} A={sum(1 for r in rts if r['is_a'])}")


def simulate(label: str, rows: List[dict], gfn: GuardFn) -> dict:
    kept, cut = [], []
    for r in rows:
        if r["is_a"] and guard_on_buy_day(r["buy_d"], gfn):
            cut.append(r)
        else:
            kept.append(r)
    base = sum(r["pnl"] for r in rows)
    kept_pnl = sum(r["pnl"] for r in kept)
    avoided = -sum(r["pnl"] for r in cut if r["pnl"] < 0)
    missed = sum(r["pnl"] for r in cut if r["pnl"] > 0)
    delta = avoided - missed
    return {
        "label": label,
        "n": len(rows),
        "cut": len(cut),
        "cut_a": sum(1 for r in cut if r["is_a"]),
        "base": round(base),
        "kept": round(kept_pnl),
        "delta": round(delta),
        "avoided": round(avoided),
        "missed": round(missed),
    }


# day coverage
print("\n===== SOFT(가드 ON) 일수 =====")
day_cov = {}
for name, gfn in GUARDS.items():
    on_days = []
    for d in sorted(by_d.keys()):
        s = prev_stat(d)
        # count calendar days where *that day's* own stat would flag (for coverage)
        # better: days where if you enter on d, guard fires = prev_stat(d)
        if s and gfn(s):
            on_days.append(d)
    sep = [d for d in on_days if "2026-09-01" <= d <= "2026-09-22"]
    augsep = [d for d in on_days if "2026-08-03" <= d <= "2026-09-22"]
    day_cov[name] = {
        "n": len(on_days),
        "pct": round(100 * len(on_days) / max(1, len(by_d)), 1),
        "sep": sep,
        "augsep_n": len(augsep),
    }
    print(
        f"{name}: {len(on_days)}일 ({day_cov[name]['pct']}%) | "
        f"AugSep진입기준 {len(augsep)}일 | Sep {sep}"
    )

# known loss cluster buy days from earlier analysis
pain_buys = [
    "2026-09-01",
    "2026-09-04",
    "2026-09-09",
    "2026-09-10",
    "2026-09-11",
    "2026-09-16",
    "2026-09-17",
    "2026-09-18",
]
print("\n===== 고통 매수일 커버 (가드가 그 진입일을 막는지) =====")
for name, gfn in GUARDS.items():
    hit = [d for d in pain_buys if guard_on_buy_day(d, gfn)]
    print(f"{name}: {len(hit)}/{len(pain_buys)} → {hit}")

results = []
print("\n===== FULL =====")
for name, gfn in GUARDS.items():
    rec = simulate(f"FULL {name}", rts, gfn)
    results.append(rec)
    print(
        f"{name}: cut={rec['cut']} Δ={rec['delta']:+,} "
        f"(avoid {rec['avoided']:+,} / miss {rec['missed']:+,})"
    )

print("\n===== TRAIN(~2025) vs OOS(2026) =====")
train = [r for r in rts if r["sell_d"] < "2026-01-01"]
oos = [r for r in rts if r["sell_d"] >= "2026-01-01"]
for name, gfn in GUARDS.items():
    t = simulate(f"TRAIN {name}", train, gfn)
    o = simulate(f"OOS {name}", oos, gfn)
    results.extend([t, o])
    print(
        f"{name}: TRAIN Δ={t['delta']:+,} (cut {t['cut']}) | "
        f"OOS Δ={o['delta']:+,} (cut {o['cut']})"
    )

print("\n===== AUGSEP 2026 =====")
augsep = [
    r
    for r in rts
    if r["sell_d"] >= "2026-08-03" and r["sell_d"] <= "2026-09-22"
]
print(f"augsep n={len(augsep)} base={sum(r['pnl'] for r in augsep):+,.0f}")
for name, gfn in GUARDS.items():
    rec = simulate(f"AUGSEP {name}", augsep, gfn)
    results.append(rec)
    print(
        f"{name}: cut={rec['cut']} base={rec['base']:+,} → kept={rec['kept']:+,} "
        f"Δ={rec['delta']:+,}"
    )

out = {
    "xs_percentiles": {"p50": xs_p50, "p60": xs_p60, "p70": xs_p70},
    "day_coverage": day_cov,
    "results": results,
}
(OUT / "rotation_a_guard_probe.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
)
print(f"\nsaved {OUT / 'rotation_a_guard_probe.json'}")
