# -*- coding: utf-8 -*-
"""
QUAL_B(품질) + 직교 완화(거래수 회복) 하이브리드 비교.

가설: B 품질필터로 나쁜 진입을 걸러둔 채, vol/슬롯을 풀어
      품질을 통과하는 거래 수를 BASE 근처로 회복하면 총수익이 개선될 수 있다.

실험:
  BASE      — 현행
  QUAL_B    — B 품질만
  VOLB      — vol_surge_b 2.5→2.0 (완화만)
  QB_VOL    — QUAL_B + vol 완화
  QB_SLOT   — QUAL_B + B 슬롯/transition soft 확대
  QB_COMBO  — QUAL_B + vol + 슬롯

  python tools/compare_s1_quality_hybrid.py
"""
from __future__ import annotations

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

import importlib
import backtest_engine as be
import dynamic_backtest as db
import strategy.signals as sigmod

importlib.reload(be)
importlib.reload(db)
importlib.reload(sigmod)

from backtest_engine import settings_for_backtest  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

OUT_DIR = ROOT / "tools" / "bt_compare_out_s1_quality_hybrid"
SEED_DIR = ROOT / "tools" / "bt_compare_out_s1_quality"
OOS_START = "2025-07-01"
FULL_START = "2024-06-03"
FULL_END = "2026-07-31"
INIT_CAPITAL = 10_000_000.0

QUAL_B: Dict[str, Any] = {
    "use_s1_quality_a": False,
    "use_s1_quality_b": True,
    "s1_b_atr_pct_max": 0.96,
    "s1_b_rsi_floor": 66.5,
}

VOL_RELAX: Dict[str, Any] = {
    "vol_surge_b": 2.0,  # 기본 2.5 → 완화 (품질 ATR/RSI와 직교)
}

SLOT_RELAX: Dict[str, Any] = {
    "max_slots_b": 20,  # 기본 15
    "transition_soft_slots_b": 12,  # 기본 8
    "transition_soft_slots_a": 12,  # soft 구간 전체 여유
}

EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "id": "BASE",
        "title": "현행",
        "overrides": {"use_s1_quality_a": False, "use_s1_quality_b": False},
        "seed_from": "BASE",
    },
    {
        "id": "QUAL_B",
        "title": "B 품질만",
        "overrides": dict(QUAL_B),
        "seed_from": "QUAL_B",
    },
    {
        "id": "VOLB",
        "title": "vol_b=2.0 완화만",
        "overrides": {
            "use_s1_quality_a": False,
            "use_s1_quality_b": False,
            **VOL_RELAX,
        },
    },
    {
        "id": "QB_VOL",
        "title": "QUAL_B + vol_b=2.0",
        "overrides": {**QUAL_B, **VOL_RELAX},
    },
    {
        "id": "QB_SLOT",
        "title": "QUAL_B + B슬롯/soft 확대",
        "overrides": {**QUAL_B, **SLOT_RELAX},
    },
    {
        "id": "QB_COMBO",
        "title": "QUAL_B + vol + 슬롯",
        "overrides": {**QUAL_B, **VOL_RELAX, **SLOT_RELAX},
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
    st["use_s1_quality_a"] = False
    st["use_s1_quality_b"] = False
    st["backtest_initial_capital"] = INIT_CAPITAL
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


def _pair_s1(trades: pd.DataFrame) -> pd.DataFrame:
    df = trades.copy()
    df["Date"] = pd.to_datetime(df["Date"], format="mixed", errors="coerce")
    df["Code"] = df["Code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    df = df.dropna(subset=["Date"]).sort_values(["Code", "Date"])
    rows: List[dict] = []
    for code, g in df.groupby("Code", sort=False):
        open_buy = None
        for _, r in g.iterrows():
            act = str(r.get("Action", ""))
            if act == "BUY1":
                open_buy = r
            elif act == "SELL" and open_buy is not None:
                try:
                    ret = float(r.get("Return", float("nan")))
                except Exception:
                    ret = float("nan")
                rows.append(
                    {
                        "sig_type": str(open_buy.get("Sig_Type", "")).upper(),
                        "ret_pct": ret,
                    }
                )
                open_buy = None
    return pd.DataFrame(rows)


def _trade_metrics(trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if trades is None or trades.empty:
        return {"n_buy1": 0, "n_buy2": 0, "win_rate": 0.0, "s1_avg_ret": 0.0, "s1_by_type": {}}
    buys1 = trades[trades["Action"] == "BUY1"]
    buys2 = trades[trades["Action"] == "BUY2"]
    sells = trades[trades["Action"] == "SELL"]
    out: Dict[str, Any] = {
        "n_buy1": int(len(buys1)),
        "n_buy2": int(len(buys2)),
        "win_rate": float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0,
        "s1_avg_ret": 0.0,
        "s1_by_type": {},
    }
    try:
        rounds = _pair_s1(trades)
        if not rounds.empty:
            out["s1_avg_ret"] = float(rounds["ret_pct"].mean())
            out["s1_win_rate"] = float((rounds["ret_pct"] > 0).mean() * 100)
            for sig, g in rounds.groupby("sig_type"):
                out["s1_by_type"][str(sig)] = {
                    "n": int(len(g)),
                    "avg_ret": float(g["ret_pct"].mean()),
                    "win_rate": float((g["ret_pct"] > 0).mean() * 100),
                }
    except Exception as e:
        out["s1_err"] = str(e)
    return out


def _fix_seed(exp_id: str, period: str, start: str, end: str) -> Optional[Dict[str, Any]]:
    """이전 s1_quality 결과에서 BASE/QUAL_B 캐시 이식(동일 기간·초기자본 재산출)."""
    src = SEED_DIR / f"{exp_id}_{period}"
    meta_path = src / "meta.json"
    if not meta_path.is_file():
        return None
    try:
        cached = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        if cached.get("start") != start or cached.get("end") != end or cached.get("error"):
            return None
        end_eq = float(cached.get("end_equity", 0))
        if end_eq <= 0:
            return None
        # 수익률만 올바른 초기자본으로 재산출
        cached = dict(cached)
        cached["ret_total_pct"] = (end_eq / INIT_CAPITAL - 1) * 100
        cached["profit_krw"] = end_eq - INIT_CAPITAL
        dest = OUT_DIR / f"{exp_id}_{period}"
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("meta.json", "trades.csv", "equity.csv"):
            sp = src / name
            if sp.is_file():
                shutil.copy2(sp, dest / name)
        # 재산출 meta 덮어쓰기
        (dest / "meta.json").write_text(
            json.dumps(cached, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"[{exp_id}_{period}] seed 재사용 (ret 재산출 {cached['ret_total_pct']:+.2f}%)")
        return cached
    except Exception as e:
        print(f"[WARN] seed {exp_id}_{period}: {e}")
        return None


def run_one(exp: Dict[str, Any], base: Dict[str, Any], start: str, end: str) -> Dict[str, Any]:
    period = "full" if start == FULL_START else "oos"
    run_id = f"{exp['id']}_{period}"
    dest = OUT_DIR / run_id
    meta_path = dest / "meta.json"

    if meta_path.is_file():
        try:
            cached = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            if cached.get("start") == start and cached.get("end") == end and not cached.get("error"):
                # 과거 잘못된 1억 기준이면 재산출
                end_eq = float(cached.get("end_equity", 0))
                if end_eq > 0:
                    cached["ret_total_pct"] = (end_eq / INIT_CAPITAL - 1) * 100
                    cached["profit_krw"] = end_eq - INIT_CAPITAL
                print(f"[{run_id}] 캐시 재사용")
                return cached
        except Exception:
            pass

    seed_id = exp.get("seed_from")
    if seed_id:
        seeded = _fix_seed(seed_id, period, start, end)
        if seeded is not None:
            return seeded

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
    initial = float(st.get("backtest_initial_capital", INIT_CAPITAL))
    eq_m: Dict[str, Any] = {}
    try:
        if hasattr(tester, "daily_equity") and tester.daily_equity:
            eq_m = _equity_metrics(tester.daily_equity, initial)
            pd.DataFrame(tester.daily_equity).to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")
    except Exception as e:
        print(f"  [WARN] equity: {e}")

    tm = _trade_metrics(trades if isinstance(trades, pd.DataFrame) else None)
    if isinstance(trades, pd.DataFrame) and not trades.empty:
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")

    meta = {
        "id": exp["id"],
        "title": exp["title"],
        "period": period,
        "start": start,
        "end": end,
        "elapsed_s": round(elapsed, 1),
        "overrides": exp["overrides"],
        **eq_m,
        **tm,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(
        f"  ret={meta.get('ret_total_pct', float('nan')):+.2f}% "
        f"maxDD={meta.get('max_dd_pct', float('nan')):.2f}% "
        f"BUY1={meta.get('n_buy1')} S1avg={meta.get('s1_avg_ret', 0):+.2f}% "
        f"S1wr={meta.get('s1_win_rate', 0):.1f}%"
    )
    return meta


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    rows: List[Dict[str, Any]] = []
    for exp in EXPERIMENTS:
        for start, end in ((FULL_START, FULL_END), (OOS_START, FULL_END)):
            rows.append(run_one(exp, base, start, end))

    (OUT_DIR / "summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    print("\n" + "=" * 88)
    print(f"{'ID':<10} {'Per':<5} {'Ret%':>8} {'Profit':>12} {'MaxDD':>8} {'BUY1':>6} {'S1avg':>7} {'S1wr':>6}")
    print("-" * 88)
    by: Dict[tuple, Dict[str, Any]] = {}
    for r in rows:
        if r.get("error"):
            print(f"{r.get('id'):<10} {r.get('period'):<5} ERROR")
            continue
        by[(r["id"], r["period"])] = r
        print(
            f"{r.get('id',''):<10} {r.get('period',''):<5} "
            f"{r.get('ret_total_pct', float('nan')):+8.2f} "
            f"{r.get('profit_krw', 0):12,.0f} "
            f"{r.get('max_dd_pct', float('nan')):8.2f} "
            f"{r.get('n_buy1', 0):6d} "
            f"{r.get('s1_avg_ret', 0):+7.2f} "
            f"{r.get('s1_win_rate', 0):6.1f}"
        )

    lines = [
        "# QUAL_B + 직교 완화 하이브리드 판정",
        "",
        "초기자본 1,000만원. BASE 대비 Δ.",
        "",
    ]
    for period in ("oos", "full"):
        base_r = by.get(("BASE", period), {})
        if not base_r:
            continue
        lines.append(f"## {period.upper()}")
        lines.append("| ID | Δret | Δprofit | BUY1 | S1avg | 판정 |")
        lines.append("|---|---:|---:|---:|---:|---|")
        for eid in ("QUAL_B", "VOLB", "QB_VOL", "QB_SLOT", "QB_COMBO"):
            r = by.get((eid, period), {})
            if not r:
                continue
            d_ret = float(r.get("ret_total_pct", 0)) - float(base_r.get("ret_total_pct", 0))
            d_pf = float(r.get("profit_krw", 0)) - float(base_r.get("profit_krw", 0))
            b1 = f"{base_r.get('n_buy1')}→{r.get('n_buy1')}"
            s1 = f"{base_r.get('s1_avg_ret', 0):.2f}→{r.get('s1_avg_ret', 0):.2f}"
            # 성공: 수익≥BASE 이고 BUY1이 QUAL_B보다 회복
            qb = by.get(("QUAL_B", period), {})
            buy_recovered = int(r.get("n_buy1", 0)) >= int(qb.get("n_buy1", 0))
            better_pnl = d_pf > 0
            if better_pnl and buy_recovered:
                judge = "PASS"
            elif better_pnl:
                judge = "PNL↑"
            elif buy_recovered and d_ret > -2:
                judge = "WEAK"
            else:
                judge = "FAIL"
            lines.append(
                f"| {eid} | {d_ret:+.2f}%p | {d_pf:+,.0f} | {b1} | {s1} | {judge} |"
            )
        lines.append("")

    judge_path = OUT_DIR / "judgment.md"
    judge_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[완료] {OUT_DIR / 'summary.json'}")
    print(f"[판정] {judge_path}")
    print(judge_path.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
