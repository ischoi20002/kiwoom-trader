# -*- coding: utf-8 -*-
"""
Track↔Sig_Type 교차 거절(enforce_track_signal_match) ON vs OFF 비교.

선정(Track A/B·슬롯 N)은 동일하고, 감시목록 안에서 타입 불일치 매수만 허용할지 검증한다.

  python tools/compare_track_mismatch_gate.py
  python tools/compare_track_mismatch_gate.py --start 2025-01-01 --end 2025-06-30
"""

from __future__ import annotations

import argparse
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

importlib.reload(be)
importlib.reload(db)

from backtest_engine import settings_for_backtest  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

OUT_DIR = ROOT / "tools" / "bt_compare_out_track_mismatch"
HIGH_PRECISION = False


def _load_base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["use_market_breadth_gate"] = False
    st["bt_high_precision"] = False
    st["bt_rt_parity_intrabar"] = False
    # 기본값 명시 (구 settings.json에 키 없어도 True)
    st.setdefault("enforce_track_signal_match", True)
    return st


def _experiments() -> List[Dict[str, Any]]:
    return [
        {
            "id": "match_ON_s2on",
            "title": "교차거절 ON · S2 ON (현행)",
            "overrides": {
                "enforce_track_signal_match": True,
                "use_s2_master_a": True,
                "use_s2_master_b": True,
            },
        },
        {
            "id": "match_OFF_s2on",
            "title": "교차거절 OFF · S2 ON",
            "overrides": {
                "enforce_track_signal_match": False,
                "use_s2_master_a": True,
                "use_s2_master_b": True,
            },
        },
        {
            "id": "match_ON_s1only",
            "title": "교차거절 ON · S1 only",
            "overrides": {
                "enforce_track_signal_match": True,
                "use_s2_master_a": False,
                "use_s2_master_b": False,
            },
        },
        {
            "id": "match_OFF_s1only",
            "title": "교차거절 OFF · S1 only",
            "overrides": {
                "enforce_track_signal_match": False,
                "use_s2_master_a": False,
                "use_s2_master_b": False,
            },
        },
    ]


def _equity_metrics(
    daily_equity: List[dict], initial: float, start: str, end: str
) -> Dict[str, Any]:
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
    dd = (daily[col] - daily[col].cummax()) / daily[col].cummax().replace(0, pd.NA)
    return {
        "start": start,
        "end": end,
        "ret_total_pct": (e_end / e0 - 1.0) * 100.0 if e0 else 0.0,
        "end_equity": e_end,
        "max_dd_pct": float(dd.min() * 100.0) if dd.notna().any() else 0.0,
    }


def _trade_metrics(trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if trades is None or (isinstance(trades, pd.DataFrame) and trades.empty):
        return {"n_buy1": 0, "n_buy2": 0, "n_sell": 0, "win_rate": 0.0, "avg_ret": 0.0}
    df = trades.copy()
    buys1 = df[df["Action"] == "BUY1"]
    buys2 = df[df["Action"] == "BUY2"]
    sells = df[df["Action"] == "SELL"]
    avg_ret = float(sells["Return"].mean() * 100.0) if len(sells) and "Return" in sells.columns else 0.0
    # Return이 이미 %인지 비율인지 — 기존 compare는 (sells["Return"] > 0)만 봄.
    # dynamic_backtest는 보통 비율(0.05=5%). mean*100이 맞는지 확인 위해 절대값 스케일 휴리스틱
    try:
        if len(sells) and "Return" in sells.columns:
            m = float(sells["Return"].abs().median())
            if m > 1.5:  # 이미 % 단위로 보임
                avg_ret = float(sells["Return"].mean())
            else:
                avg_ret = float(sells["Return"].mean() * 100.0)
    except Exception:
        avg_ret = 0.0
    return {
        "n_buy1": int(len(buys1)),
        "n_buy2": int(len(buys2)),
        "n_sell": int(len(sells)),
        "win_rate": float((sells["Return"] > 0).mean() * 100.0) if len(sells) else 0.0,
        "avg_ret": avg_ret,
        "s2_rate": float(len(buys2) / len(buys1) * 100.0) if len(buys1) else 0.0,
    }


def run_one(
    exp: Dict[str, Any], base: Dict[str, Any], start: str, end: str
) -> Dict[str, Any]:
    rid = exp["id"]
    print("\n" + "=" * 72)
    print(f"[{rid}] {exp['title']}")
    print("=" * 72)
    st = deepcopy(base)
    st.update(exp["overrides"])

    def cb(msg: str, val: Any = None) -> None:
        try:
            if str(msg).startswith("[") and "시계열" in str(msg) and val is not None:
                print(
                    f"\r  {str(msg).split(']', 1)[0]}] {float(val):5.1f}%",
                    end="",
                    flush=True,
                )
        except Exception:
            pass

    t0 = time.time()
    tester = DynamicBacktester(st)
    trades = tester.run(start, end, interval="15m", callback=cb, high_precision=HIGH_PRECISION)
    elapsed = time.time() - t0
    print(f"\n  done {elapsed:.1f}s")

    dest = OUT_DIR / rid
    dest.mkdir(parents=True, exist_ok=True)
    if isinstance(trades, pd.DataFrame) and not trades.empty:
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(tester.daily_equity).to_csv(
        dest / "equity.csv", index=False, encoding="utf-8-sig"
    )

    eq = _equity_metrics(tester.daily_equity, tester.initial_cash, start, end)
    tm = _trade_metrics(trades if isinstance(trades, pd.DataFrame) else None)
    r = {
        "id": rid,
        "title": exp["title"],
        "enforce_match": bool(exp["overrides"].get("enforce_track_signal_match", True)),
        "s2_on": bool(exp["overrides"].get("use_s2_master_a", True)),
        "elapsed_sec": round(elapsed, 1),
        **eq,
        **tm,
    }
    print(
        f"  total={r.get('ret_total_pct', float('nan')):+.2f}% "
        f"DD={r.get('max_dd_pct', float('nan')):.1f}% "
        f"buy1={r.get('n_buy1', 0)} buy2={r.get('n_buy2', 0)} "
        f"win={r.get('win_rate', 0):.1f}% avgRet={r.get('avg_ret', 0):+.2f}%"
    )
    return r


def _print_delta(rows: List[Dict[str, Any]]) -> None:
    by_id = {r["id"]: r for r in rows}
    pairs = [
        ("S2 ON", "match_ON_s2on", "match_OFF_s2on"),
        ("S1 only", "match_ON_s1only", "match_OFF_s1only"),
    ]
    print("\n" + "=" * 72)
    print("교차거절 OFF − ON 차이 (허용 효과)")
    print("=" * 72)
    for label, a, b in pairs:
        if a not in by_id or b not in by_id:
            continue
        on, off = by_id[a], by_id[b]
        d_ret = float(off.get("ret_total_pct", 0)) - float(on.get("ret_total_pct", 0))
        d_dd = float(off.get("max_dd_pct", 0)) - float(on.get("max_dd_pct", 0))
        d_b1 = int(off.get("n_buy1", 0)) - int(on.get("n_buy1", 0))
        d_wr = float(off.get("win_rate", 0)) - float(on.get("win_rate", 0))
        print(
            f"  [{label}]  Δ수익 {d_ret:+.2f}%p  ΔDD {d_dd:+.2f}%p  "
            f"ΔBUY1 {d_b1:+d}  Δ승률 {d_wr:+.1f}%p"
        )
        print(
            f"           ON  {on.get('ret_total_pct'):+.2f}% / buy1={on.get('n_buy1')} / "
            f"win={on.get('win_rate'):.1f}%"
        )
        print(
            f"           OFF {off.get('ret_total_pct'):+.2f}% / buy1={off.get('n_buy1')} / "
            f"win={off.get('win_rate'):.1f}%"
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2025-06-30")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    print(
        f"[compare] {args.start} ~ {args.end}  "
        f"slots A={base.get('max_slots_a')} B={base.get('max_slots_b')}  LP"
    )

    rows: List[Dict[str, Any]] = []
    for exp in _experiments():
        try:
            rows.append(run_one(exp, base, args.start, args.end))
        except Exception as e:
            print(f"[Error] {exp['id']}: {e}")
            traceback.print_exc()
            rows.append({"id": exp["id"], "title": exp["title"], "error": str(e)})

    df = pd.DataFrame(rows)
    df.to_csv(OUT_DIR / "summary.csv", index=False, encoding="utf-8-sig")
    (OUT_DIR / "summary.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _print_delta([r for r in rows if "error" not in r])
    print(f"\n[저장] {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
