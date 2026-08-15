# -*- coding: utf-8 -*-
"""
트리플 볼린저 과열 필터 짧은 비교.

  BASE     : 필터 OFF
  A_2S     : Type A만 +2σ 이상 진입 차단
  ALL_3S   : A/B 모두 +3σ 이상 진입 차단

기간은 기존 비교 스크립트의 OOS 창(2025-07-01 ~ 2026-07-31) 한 구간만.

  python tools/compare_bb_heat_filter.py
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
import strategy.signals as sigmod
import strategy.indicators as indmod

importlib.reload(be)
importlib.reload(db)
importlib.reload(ss)
importlib.reload(indmod)
importlib.reload(sigmod)

from backtest_engine import settings_for_backtest  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

OUT_DIR = ROOT / "tools" / "bt_compare_out_bb_heat"
START = "2025-07-01"
END = "2026-07-31"

EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "id": "BASE",
        "title": "필터 OFF",
        "overrides": {
            "use_bb_block_a_2s": False,
            "use_bb_block_all_3s": False,
        },
    },
    {
        "id": "A_2S",
        "title": "Type A +2σ 차단",
        "overrides": {
            "use_bb_block_a_2s": True,
            "bb_block_a_z": 2.0,
            "use_bb_block_all_3s": False,
        },
    },
    {
        "id": "ALL_3S",
        "title": "전 타입 +3σ 차단",
        "overrides": {
            "use_bb_block_a_2s": False,
            "use_bb_block_all_3s": True,
            "bb_block_all_z": 3.0,
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
    st["use_bb_block_a_2s"] = False
    st["use_bb_block_all_3s"] = False
    st["bb_period"] = 20
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
            if act in ("BUY1", "BUY_1ST"):
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
    act = trades["Action"].astype(str)
    buys1 = trades[act.isin(["BUY1", "BUY_1ST"])]
    buys2 = trades[act.isin(["BUY2", "BUY_2ND"])]
    sells = trades[act == "SELL"]
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


def run_one(exp: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    dest = OUT_DIR / exp["id"]
    meta_path = dest / "meta.json"
    if meta_path.is_file():
        try:
            cached = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            if cached.get("start") == START and cached.get("end") == END and not cached.get("error"):
                print(f"[{exp['id']}] 캐시 재사용")
                return cached
        except Exception:
            pass

    print(f"\n[{exp['id']}] {exp['title']}  {START} ~ {END}")
    st = deepcopy(base)
    st.update(exp["overrides"])
    st["bt_start_date"] = START
    st["bt_end_date"] = END

    def cb(msg: str, val: Any = None) -> None:
        try:
            if str(msg).startswith("[") and "시계열" in str(msg) and val is not None:
                print(f"\r  {float(val):5.1f}%", end="", flush=True)
        except Exception:
            pass

    t0 = time.time()
    try:
        tester = DynamicBacktester(st)
        trades = tester.run(START, END, interval="15m", callback=cb, high_precision=False)
    except Exception:
        traceback.print_exc()
        return {"id": exp["id"], "error": True}
    elapsed = time.time() - t0
    print(f"\n  done {elapsed:.1f}s")

    dest.mkdir(parents=True, exist_ok=True)
    initial = float(
        st.get("backtest_initial_capital", st.get("initial_capital", 10_000_000))
    )
    eq_m: Dict[str, Any] = {}
    try:
        if hasattr(tester, "daily_equity") and tester.daily_equity:
            eq_m = _equity_metrics(tester.daily_equity, initial)
            pd.DataFrame(tester.daily_equity).to_csv(
                dest / "equity.csv", index=False, encoding="utf-8-sig"
            )
    except Exception as e:
        print(f"  [WARN] equity: {e}")

    tm = _trade_metrics(trades if isinstance(trades, pd.DataFrame) else None)
    if isinstance(trades, pd.DataFrame) and not trades.empty:
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")

    meta = {
        "id": exp["id"],
        "title": exp["title"],
        "start": START,
        "end": END,
        "elapsed_s": round(elapsed, 1),
        "overrides": exp["overrides"],
        **eq_m,
        **tm,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"  ret={meta.get('ret_total_pct', float('nan')):+.2f}% "
        f"maxDD={meta.get('max_dd_pct', float('nan')):.2f}% "
        f"BUY1={meta.get('n_buy1')} S1avg={meta.get('s1_avg_ret', 0):+.2f}%"
    )
    return meta


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    rows: List[Dict[str, Any]] = []
    for exp in EXPERIMENTS:
        rows.append(run_one(exp, base))

    (OUT_DIR / "summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )

    print("\n" + "=" * 78)
    print(f"{'ID':<8} {'Ret%':>8} {'MaxDD%':>8} {'BUY1':>6} {'S1avg':>7} {'S1wr%':>6}")
    print("-" * 78)
    by: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if r.get("error"):
            print(f"{r.get('id'):<8} ERROR")
            continue
        by[str(r.get("id"))] = r
        print(
            f"{r.get('id',''):<8} "
            f"{r.get('ret_total_pct', float('nan')):+8.2f} "
            f"{r.get('max_dd_pct', float('nan')):8.2f} "
            f"{r.get('n_buy1', 0):6d} "
            f"{r.get('s1_avg_ret', 0):+7.2f} "
            f"{r.get('s1_win_rate', 0):6.1f}"
        )

    base_r = by.get("BASE", {})
    lines = [
        "# 트리플 볼린저 과열 필터 판정",
        "",
        f"기간: {START} ~ {END} (기존 비교 OOS 창, 고정밀 OFF)",
        "",
    ]
    for eid in ("A_2S", "ALL_3S"):
        cur = by.get(eid, {})
        if not cur or not base_r:
            continue
        d_ret = float(cur.get("ret_total_pct", 0)) - float(base_r.get("ret_total_pct", 0))
        d_s1 = float(cur.get("s1_avg_ret", 0)) - float(base_r.get("s1_avg_ret", 0))
        d_dd = float(cur.get("max_dd_pct", 0)) - float(base_r.get("max_dd_pct", 0))
        d_n = int(cur.get("n_buy1", 0)) - int(base_r.get("n_buy1", 0))
        better = d_ret > 0.3
        lines.append(
            f"- **{eid}** Δret={d_ret:+.2f}%p ΔS1avg={d_s1:+.2f}%p "
            f"ΔmaxDD={d_dd:+.2f}%p ΔBUY1={d_n:+d} → "
            f"{'수익 개선' if better else '수익 개선 없음'}"
        )
    judge = OUT_DIR / "judgment.md"
    judge.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[완료] {OUT_DIR / 'summary.json'}")
    print(judge.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
