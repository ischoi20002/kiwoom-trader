# -*- coding: utf-8 -*-
"""
Type A 백테스트 개선안 A/B/C 비교 (동일 기간·저정밀).

실험:
  BASE — 현재 settings (Type A only, SL×2.0, TP 3.5%)
  A    — use_type_b + use_sel_track_b ON
  B    — atr_multiplier_sl_a=2.8, s1_a_take_profit=6.0
  C    — A + B 동시

실행:
  python tools/compare_type_a_fixes.py
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

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from backtest_engine import settings_for_backtest  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

START = "2024-06-03"
END = "2025-12-31"
OUT_DIR = ROOT / "tools" / "bt_compare_out"
HIGH_PRECISION = False

EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "id": "BASE",
        "title": "BASE (Type A only, SL×2.0, TP 3.5%)",
        "overrides": {},
    },
    {
        "id": "A",
        "title": "A: Type B + Track B ON",
        "overrides": {
            "use_type_b": True,
            "use_sel_track_b": True,
        },
    },
    {
        "id": "B",
        "title": "B: SL×2.8 + TP 6.0%",
        "overrides": {
            "atr_multiplier_sl_a": 2.8,
            "s1_a_take_profit": 6.0,
        },
    },
    {
        "id": "C",
        "title": "C: A+B (Type B + SL/TP)",
        "overrides": {
            "use_type_b": True,
            "use_sel_track_b": True,
            "atr_multiplier_sl_a": 2.8,
            "s1_a_take_profit": 6.0,
        },
    },
]


def _load_base_settings() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    # GUI와 동일: Type A only 기준 상태 고정(파일에 이미 반영돼 있어도 명시)
    st["use_type_a"] = True
    st["use_type_b"] = False
    st["use_sel_track_a"] = True
    st["use_sel_track_b"] = False
    st["bt_high_precision"] = False
    st["bt_rt_parity_intrabar"] = False
    return st


def _exit_tag(reason: str) -> str:
    r = str(reason or "").split("|", 1)[0].strip().upper()
    for key in ("TAKE_PROFIT", "ATR_SL", "ATR_TRAIL", "FLASH_EXIT", "GAP_DOWN", "STOP_LOSS", "SMART_EXIT"):
        if key in r:
            return key
    return r[:24] if r else "UNKNOWN"


def _equity_metrics(daily_equity: List[dict], initial: float) -> Dict[str, Any]:
    if not daily_equity:
        return {"error": "empty equity"}
    df = pd.DataFrame(daily_equity)
    df["date"] = pd.to_datetime(df["date"], format="mixed", errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date")
    df["d"] = df["date"].dt.normalize()
    daily = df.groupby("d", as_index=False).last()

    def at(day: str) -> float:
        sub = daily[daily["d"] <= pd.Timestamp(day)]
        return float(sub.iloc[-1]["equity"]) if len(sub) else float("nan")

    e0 = float(initial)
    e24 = at("2024-12-31")
    e_end = float(daily.iloc[-1]["equity"])
    peak = float(daily["equity"].max())
    peak_d = daily.loc[daily["equity"].idxmax(), "d"]

    # MaxDD
    peak_series = daily["equity"].cummax()
    dd = (daily["equity"] - peak_series) / peak_series
    max_dd = float(dd.min() * 100)
    max_dd_d = daily.loc[dd.idxmin(), "d"]

    return {
        "start": e0,
        "end": e_end,
        "end_2024": e24,
        "ret_2024_pct": (e24 / e0 - 1) * 100,
        "ret_2025_pct": (e_end / e24 - 1) * 100 if e24 else float("nan"),
        "ret_total_pct": (e_end / e0 - 1) * 100,
        "peak": peak,
        "peak_date": str(peak_d.date()),
        "peak_to_end_pct": (e_end / peak - 1) * 100,
        "max_dd_pct": max_dd,
        "max_dd_date": str(max_dd_d.date()),
    }


def _trade_metrics(trades_df: pd.DataFrame) -> Dict[str, Any]:
    if trades_df is None or trades_df.empty:
        return {"n_trades": 0, "n_sell": 0}
    df = trades_df.copy()
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    sells = df[df["Action"] == "SELL"].copy()
    buys1 = df[df["Action"] == "BUY1"]
    buys2 = df[df["Action"] == "BUY2"]
    if sells.empty:
        return {
            "n_trades": len(df),
            "n_buy1": len(buys1),
            "n_buy2": len(buys2),
            "n_sell": 0,
        }
    sells["year"] = sells["Date"].dt.year
    sells["tag"] = sells["Reason"].map(_exit_tag)
    out: Dict[str, Any] = {
        "n_trades": len(df),
        "n_buy1": int(len(buys1)),
        "n_buy2": int(len(buys2)),
        "n_sell": int(len(sells)),
        "win_rate": float((sells["Return"] > 0).mean() * 100),
        "avg_ret": float(sells["Return"].mean()),
        "sum_ret_pp": float(sells["Return"].sum()),
    }
    for y in (2024, 2025):
        s = sells[sells["year"] == y]
        if s.empty:
            out[f"win_{y}"] = 0.0
            out[f"avg_ret_{y}"] = 0.0
            out[f"n_sell_{y}"] = 0
            out[f"atr_sl_{y}"] = 0
            out[f"trail_{y}"] = 0
            out[f"tp_{y}"] = 0
            continue
        out[f"win_{y}"] = float((s["Return"] > 0).mean() * 100)
        out[f"avg_ret_{y}"] = float(s["Return"].mean())
        out[f"n_sell_{y}"] = int(len(s))
        out[f"atr_sl_{y}"] = int((s["tag"] == "ATR_SL").sum())
        out[f"trail_{y}"] = int((s["tag"] == "ATR_TRAIL").sum())
        out[f"tp_{y}"] = int((s["tag"] == "TAKE_PROFIT").sum())
    # sig type mix
    if "Sig_Type" in df.columns:
        buy_sig = df[df["Action"].isin(["BUY1", "BUY2"])]["Sig_Type"].astype(str)
        out["buy_type_a"] = int((buy_sig == "TYPE_A").sum())
        out["buy_type_b"] = int((buy_sig == "TYPE_B").sum())
    return out


def _save_run_artifacts(run_id: str, tester: DynamicBacktester, trades: Optional[pd.DataFrame]) -> Path:
    dest = OUT_DIR / run_id
    dest.mkdir(parents=True, exist_ok=True)
    # equity
    eq = pd.DataFrame(tester.daily_equity)
    if not eq.empty:
        eq.to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")
    if trades is not None and not trades.empty:
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    # copy global artifacts if present
    for name in ("backtest_portfolio_curve.csv", "backtest_trades.csv", "dynamic_trade_report.html"):
        src = ROOT / name
        if src.is_file():
            try:
                shutil.copy2(src, dest / name)
            except Exception as e:
                print(f"  [warn] copy {name}: {e}")
    meta = {
        "id": run_id,
        "start": START,
        "end": END,
        "high_precision": HIGH_PRECISION,
        "elapsed_sec": getattr(tester, "last_run_elapsed_sec", None),
        "settings_overrides": EXPERIMENTS[[e["id"] for e in EXPERIMENTS].index(run_id)]["overrides"],
    }
    (dest / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def run_one(exp: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    run_id = str(exp["id"])
    print("\n" + "=" * 72)
    print(f"[{run_id}] {exp['title']}")
    print("=" * 72)
    st = deepcopy(base)
    st.update(exp["overrides"])
    # 적용 확인 로그
    print(
        f"  use_type_b={st.get('use_type_b')}  use_sel_track_b={st.get('use_sel_track_b')}  "
        f"sl_a={st.get('atr_multiplier_sl_a')}  tp_a={st.get('s1_a_take_profit')}"
    )

    def cb(msg: str, val: Any = None) -> None:
        # 너무 시끄러우면 일자만
        try:
            s = str(msg)
            if s.startswith("[") and "시계열" in s:
                day = s.split("]", 1)[0] + "]"
                if val is not None:
                    print(f"\r  {day} {float(val):5.1f}%", end="", flush=True)
        except Exception:
            pass

    t0 = time.time()
    tester = DynamicBacktester(st)
    trades = tester.run(START, END, interval="15m", callback=cb, high_precision=HIGH_PRECISION)
    elapsed = time.time() - t0
    print(f"\n  done in {elapsed:.1f}s (engine={getattr(tester, 'last_run_elapsed_sec', 0):.1f}s)")

    dest = _save_run_artifacts(run_id, tester, trades)
    eq_m = _equity_metrics(tester.daily_equity, tester.initial_cash)
    tr_m = _trade_metrics(trades if trades is not None else pd.DataFrame())
    result = {
        "id": run_id,
        "title": exp["title"],
        "elapsed_sec": elapsed,
        "out_dir": str(dest),
        **eq_m,
        **tr_m,
    }
    print(
        f"  total={result.get('ret_total_pct', float('nan')):+.2f}%  "
        f"2024={result.get('ret_2024_pct', float('nan')):+.2f}%  "
        f"2025={result.get('ret_2025_pct', float('nan')):+.2f}%  "
        f"MaxDD={result.get('max_dd_pct', float('nan')):.2f}%  "
        f"sells={result.get('n_sell', 0)}  wr={result.get('win_rate', 0):.1f}%"
    )
    return result


def _print_summary(rows: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 72)
    print("COMPARISON SUMMARY")
    print("=" * 72)
    headers = [
        "ID",
        "Total%",
        "2024%",
        "2025%",
        "MaxDD%",
        "WR%",
        "Sell",
        "SL25",
        "TR25",
        "TP25",
        "BuyA",
        "BuyB",
        "sec",
    ]
    print("  ".join(f"{h:>8}" for h in headers))
    for r in rows:
        vals = [
            r["id"],
            f"{r.get('ret_total_pct', float('nan')):+.1f}",
            f"{r.get('ret_2024_pct', float('nan')):+.1f}",
            f"{r.get('ret_2025_pct', float('nan')):+.1f}",
            f"{r.get('max_dd_pct', float('nan')):.1f}",
            f"{r.get('win_rate', 0):.1f}",
            str(r.get("n_sell", 0)),
            str(r.get("atr_sl_2025", 0)),
            str(r.get("trail_2025", 0)),
            str(r.get("tp_2025", 0)),
            str(r.get("buy_type_a", 0)),
            str(r.get("buy_type_b", 0)),
            f"{r.get('elapsed_sec', 0):.0f}",
        ]
        print("  ".join(f"{v:>8}" for v in vals))

    # ranking by 2025 then total
    ranked = sorted(rows, key=lambda x: (x.get("ret_2025_pct", -999), x.get("ret_total_pct", -999)), reverse=True)
    print("\n[2025 수익률 순위]")
    for i, r in enumerate(ranked, 1):
        print(
            f"  {i}. {r['id']}: 2025 {r.get('ret_2025_pct', float('nan')):+.2f}%  "
            f"total {r.get('ret_total_pct', float('nan')):+.2f}%  "
            f"({r['title']})"
        )


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base_settings()
    print(f"기간: {START} ~ {END} | 저정밀={not HIGH_PRECISION}")
    print(f"출력: {OUT_DIR}")

    results: List[Dict[str, Any]] = []
    for exp in EXPERIMENTS:
        try:
            results.append(run_one(exp, base))
        except Exception as e:
            print(f"\n[FAIL] {exp['id']}: {e}")
            traceback.print_exc()
            results.append({"id": exp["id"], "title": exp["title"], "error": str(e)})

    _print_summary([r for r in results if "error" not in r])

    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    # CSV summary
    flat = []
    for r in results:
        if "error" in r:
            continue
        flat.append(
            {
                "id": r["id"],
                "title": r["title"],
                "ret_total_pct": r.get("ret_total_pct"),
                "ret_2024_pct": r.get("ret_2024_pct"),
                "ret_2025_pct": r.get("ret_2025_pct"),
                "max_dd_pct": r.get("max_dd_pct"),
                "win_rate": r.get("win_rate"),
                "n_sell": r.get("n_sell"),
                "atr_sl_2025": r.get("atr_sl_2025"),
                "trail_2025": r.get("trail_2025"),
                "tp_2025": r.get("tp_2025"),
                "buy_type_a": r.get("buy_type_a"),
                "buy_type_b": r.get("buy_type_b"),
                "elapsed_sec": r.get("elapsed_sec"),
                "end_equity": r.get("end"),
            }
        )
    if flat:
        pd.DataFrame(flat).to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    print(f"\n저장: {summary_path}")
    print(f"저장: {OUT_DIR / 'summary.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
