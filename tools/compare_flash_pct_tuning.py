# -*- coding: utf-8 -*-
"""
Flash % 튜닝 (고가 max_p 기준) — 2026-01-01 ~ 2026-08-13.

실험:
  BASE       — 현재 Flash (A4 / B2 / S2A2.5 / S2B2), 봉 High 고점
  A5~A8      — Type A Flash만 완화
  SCALE_*    — A/B/S2 Flash를 동일 배수로 완화
  FLASH_OFF  — Flash 전부 OFF
  CLOSE_PEAK — Flash%는 BASE, 고점은 종가만(옵션3 참고선)

  python tools/compare_flash_pct_tuning.py
"""
from __future__ import annotations

import json
import os
import re
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

importlib.reload(be)
importlib.reload(db)

from backtest_engine import settings_for_backtest  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

OUT_DIR = ROOT / "tools" / "bt_compare_out_flash_pct_2026"
START = "2026-01-01"
END = "2026-08-13"

BASE_FLASH = {
    "s1_a_flash_exit_pct": 4.0,
    "s1_b_flash_exit_pct": 2.0,
    "s2_flash_exit_pct": 2.5,
    "s2_b_flash_exit_pct": 2.0,
}


def _scale_flash(mult: float) -> Dict[str, Any]:
    return {
        "s1_a_flash_exit_pct": round(BASE_FLASH["s1_a_flash_exit_pct"] * mult, 2),
        "s1_b_flash_exit_pct": round(BASE_FLASH["s1_b_flash_exit_pct"] * mult, 2),
        "s2_flash_exit_pct": round(BASE_FLASH["s2_flash_exit_pct"] * mult, 2),
        "s2_b_flash_exit_pct": round(BASE_FLASH["s2_b_flash_exit_pct"] * mult, 2),
        "use_flash_peak_bar_high": True,
    }


EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "id": "BASE",
        "title": "High peak + Flash A4/B2/S2A2.5/S2B2",
        "overrides": {"use_flash_peak_bar_high": True, **BASE_FLASH},
    },
    {
        "id": "A5",
        "title": "Type A Flash 5%",
        "overrides": {"use_flash_peak_bar_high": True, "s1_a_flash_exit_pct": 5.0},
    },
    {
        "id": "A6",
        "title": "Type A Flash 6%",
        "overrides": {"use_flash_peak_bar_high": True, "s1_a_flash_exit_pct": 6.0},
    },
    {
        "id": "A7",
        "title": "Type A Flash 7%",
        "overrides": {"use_flash_peak_bar_high": True, "s1_a_flash_exit_pct": 7.0},
    },
    {
        "id": "A8",
        "title": "Type A Flash 8%",
        "overrides": {"use_flash_peak_bar_high": True, "s1_a_flash_exit_pct": 8.0},
    },
    {
        "id": "SCALE_1p25",
        "title": "전 Flash ×1.25",
        "overrides": _scale_flash(1.25),
    },
    {
        "id": "SCALE_1p5",
        "title": "전 Flash ×1.5",
        "overrides": _scale_flash(1.5),
    },
    {
        "id": "SCALE_2p0",
        "title": "전 Flash ×2.0",
        "overrides": _scale_flash(2.0),
    },
    {
        "id": "FLASH_OFF",
        "title": "Flash 전부 OFF",
        "overrides": {
            "use_flash_peak_bar_high": True,
            "use_s1_a_flash_exit": False,
            "use_s1_b_flash_exit": False,
            "use_s2_flash_exit": False,
            "use_s2_b_flash_exit": False,
        },
    },
    {
        "id": "CLOSE_PEAK",
        "title": "종가 고점 + Flash% BASE (옵션3 참고)",
        "overrides": {"use_flash_peak_bar_high": False, **BASE_FLASH},
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


def _norm_exit(reason: Any) -> str:
    s = str(reason or "").upper()
    if "FLASH" in s:
        return "FLASH"
    if "ATR" in s and ("TRL" in s or "TRAIL" in s):
        return "ATR_TRAIL"
    if "ATR" in s and "SL" in s:
        return "ATR_SL"
    if "TAKE_PROFIT" in s or s.strip() == "TP":
        return "TAKE_PROFIT"
    if "GAP" in s:
        return "GAP_DOWN"
    if "SMART" in s:
        return "SMART_EXIT"
    if "STOP" in s or "SL%" in s:
        return "PCT_SL"
    return "OTHER"


def _trade_metrics(trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if trades is None or trades.empty:
        return {"n_buy1": 0, "n_buy2": 0, "n_sell": 0, "win_rate": 0.0, "n_flash": 0, "flash_mean_ret": 0.0}
    buys1 = trades[trades["Action"] == "BUY1"]
    buys2 = trades[trades["Action"] == "BUY2"]
    sells = trades[trades["Action"] == "SELL"].copy()
    if "Reason" in sells.columns:
        sells["_exit"] = sells["Reason"].map(_norm_exit)
    else:
        sells["_exit"] = "OTHER"
    flash = sells[sells["_exit"] == "FLASH"]
    return {
        "n_buy1": int(len(buys1)),
        "n_buy2": int(len(buys2)),
        "n_sell": int(len(sells)),
        "win_rate": float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0,
        "n_flash": int(len(flash)),
        "flash_mean_ret": float(flash["Return"].mean() * 100) if len(flash) and "Return" in flash.columns else 0.0,
        "flash_share_pct": float(len(flash) / len(sells) * 100) if len(sells) else 0.0,
    }


def run_one(exp: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    run_id = str(exp["id"])
    dest = OUT_DIR / run_id
    meta_path = dest / "meta.json"
    if meta_path.is_file():
        try:
            cached = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            if cached.get("start") == START and cached.get("end") == END and not cached.get("error"):
                print(f"[{run_id}] 캐시 재사용")
                return cached
        except Exception:
            pass

    print(f"\n[{run_id}] {exp['title']}  {START} ~ {END}")
    st = deepcopy(base)
    st.update(exp["overrides"])

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
        return {"id": run_id, "error": True, "start": START, "end": END}
    elapsed = time.time() - t0
    print(f"\n  done {elapsed:.1f}s")

    dest.mkdir(parents=True, exist_ok=True)
    if isinstance(trades, pd.DataFrame) and not trades.empty:
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    try:
        pd.DataFrame(tester.daily_equity).to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")
    except Exception:
        pass

    row: Dict[str, Any] = {
        "id": run_id,
        "title": exp["title"],
        "start": START,
        "end": END,
        "elapsed_sec": round(elapsed, 1),
        "overrides": exp["overrides"],
        **_equity_metrics(tester.daily_equity, tester.initial_cash),
        **_trade_metrics(trades if isinstance(trades, pd.DataFrame) else None),
    }
    meta_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"  total={row.get('ret_total_pct', float('nan')):+.2f}% "
        f"DD={row.get('max_dd_pct', float('nan')):.1f}% "
        f"FLASH={row.get('n_flash', 0)} "
        f"BUY1={row.get('n_buy1', 0)}"
    )
    return row


def _print_summary(rows: List[Dict[str, Any]]) -> None:
    ok = [r for r in rows if "ret_total_pct" in r and not r.get("error")]
    if not ok:
        print("결과 없음")
        return
    base = next((r for r in ok if r.get("id") == "BASE"), None)
    close = next((r for r in ok if r.get("id") == "CLOSE_PEAK"), None)
    print("\n" + "=" * 108)
    print(f"FLASH % TUNING  {START} ~ {END}  (High peak unless CLOSE_PEAK)")
    print("=" * 108)
    print(
        f"  {'ID':12s} {'수익%':>9s} {'ΔBASE':>8s} {'ΔCLOSE':>8s} "
        f"{'MDD%':>8s} {'FLASH':>6s} {'Fl%':>6s} {'Flμ%':>7s} {'BUY1':>6s} {'승률':>6s}"
    )
    for r in sorted(ok, key=lambda x: float(x.get("ret_total_pct", -999)), reverse=True):
        d_base = 0.0
        d_close = 0.0
        if base is not None:
            d_base = float(r.get("ret_total_pct", 0)) - float(base.get("ret_total_pct", 0))
        if close is not None:
            d_close = float(r.get("ret_total_pct", 0)) - float(close.get("ret_total_pct", 0))
        print(
            f"  {str(r.get('id')):12s} "
            f"{float(r.get('ret_total_pct', 0)):+9.2f} "
            f"{d_base:+8.2f} "
            f"{d_close:+8.2f} "
            f"{float(r.get('max_dd_pct', 0)):8.1f} "
            f"{int(r.get('n_flash', 0)):6d} "
            f"{float(r.get('flash_share_pct', 0)):6.1f} "
            f"{float(r.get('flash_mean_ret', 0)):7.2f} "
            f"{int(r.get('n_buy1', 0)):6d} "
            f"{float(r.get('win_rate', 0)):6.1f}"
        )


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    rows: List[Dict[str, Any]] = []
    for exp in EXPERIMENTS:
        rows.append(run_one(exp, base))
    summary = {
        "objective": "Flash % tuning with High peak (option3 CLOSE_PEAK reference)",
        "start": START,
        "end": END,
        "base_flash": BASE_FLASH,
        "rows": rows,
    }
    (OUT_DIR / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _print_summary(rows)
    print(f"\n결과: {OUT_DIR}")


if __name__ == "__main__":
    main()
