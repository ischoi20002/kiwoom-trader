# -*- coding: utf-8 -*-
"""
BT 전용 09:00 조기 패스 가능성 타진 (헤드리스).

Pass1: 전일 완성 15분 + 당일 첫 1분봉 OHLC/Volume, 거래량은 **전일 첫1분 대비** vol_surge
Pass2: 기존 09:00 완성 15분 Close 평가 (Pass1에서 이미 보유면 S1 중복 차단)

  python tools/compare_bt_open_auction_pass.py
  python tools/compare_bt_open_auction_pass.py --start 2024-06-03 --end 2024-07-31
  python tools/compare_bt_open_auction_pass.py --only BASE,OPEN_AUCTION
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

OUT_DIR = ROOT / "tools" / "bt_compare_out_open_auction_pass"
HIGH_PRECISION = False


def _load_base() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["universe_index_choice"] = "KOSPI200"
    st["kospi_top_n"] = 200
    st["bt_high_precision"] = False
    st["bt_rt_parity_intrabar"] = False
    st["enforce_track_signal_match"] = True
    st["bt_open_auction_pass_enabled"] = False
    return st


def _experiments() -> List[Dict[str, Any]]:
    return [
        {
            "id": "BASE",
            "title": "시초 패스 OFF (기존)",
            "overrides": {"bt_open_auction_pass_enabled": False},
        },
        {
            "id": "OPEN_AUCTION",
            "title": "09:00 첫1분 패스 ON + 완성15분 Pass2",
            "overrides": {"bt_open_auction_pass_enabled": True},
        },
    ]


def _equity_at(daily: pd.DataFrame, col: str, day: str, initial: float) -> float:
    sub = daily[daily["d"] <= pd.Timestamp(day)]
    if sub.empty:
        return initial
    return float(sub.iloc[-1][col])


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
        "ret_mid_pct": (
            (_equity_at(daily, col, str(daily["d"].iloc[len(daily) // 2].date()), e0) / e0 - 1)
            * 100
            if len(daily)
            else 0.0
        ),
    }


def _trade_metrics(trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    empty = {
        "n_sell": 0,
        "n_buy1": 0,
        "n_buy2": 0,
        "win_rate": 0.0,
        "avg_trade_ret_pct": 0.0,
        "n_open_auction_buy1": 0,
        "n_open_auction_sell": 0,
        "open_auction_buy1_pct": 0.0,
    }
    if trades is None or trades.empty:
        return empty
    df = trades.copy()
    buys1 = df[df["Action"] == "BUY1"]
    buys2 = df[df["Action"] == "BUY2"]
    sells = df[df["Action"] == "SELL"]
    if "Reason" in df.columns:
        oa_buy1 = buys1[
            buys1["Reason"].astype(str).str.contains("OPEN_AUCTION", na=False)
        ]
        oa_sell = sells[
            sells["Reason"].astype(str).str.contains("OPEN_AUCTION", na=False)
        ]
    else:
        oa_buy1 = buys1.iloc[0:0]
        oa_sell = sells.iloc[0:0]
    return {
        "n_buy1": int(len(buys1)),
        "n_buy2": int(len(buys2)),
        "n_sell": int(len(sells)),
        "win_rate": float((sells["Return"] > 0).mean() * 100) if len(sells) else 0.0,
        "avg_trade_ret_pct": float(sells["Return"].mean()) if len(sells) else 0.0,
        "s2_rate_pct": float(len(buys2) / len(buys1) * 100) if len(buys1) else 0.0,
        "n_open_auction_buy1": int(len(oa_buy1)),
        "n_open_auction_sell": int(len(oa_sell)),
        "open_auction_buy1_pct": (
            float(len(oa_buy1) / len(buys1) * 100) if len(buys1) else 0.0
        ),
    }


def run_one(
    exp: Dict[str, Any],
    base: Dict[str, Any],
    start: str,
    end: str,
    *,
    force: bool = False,
) -> Dict[str, Any]:
    rid = exp["id"]
    run_key = f"{rid}_{start}_{end}".replace("-", "")
    dest = OUT_DIR / run_key
    meta_path = dest / "meta.json"
    if (not force) and meta_path.is_file():
        try:
            cached = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            if cached.get("start") == start and cached.get("end") == end:
                print(f"[{run_key}] 캐시 재사용")
                return cached
        except Exception:
            pass

    print("\n" + "=" * 72)
    print(f"[{run_key}] {exp['title']}  {start} ~ {end}")
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
    try:
        tester = DynamicBacktester(st)
        trades = tester.run(
            start,
            end,
            interval="15m",
            callback=cb,
            high_precision=HIGH_PRECISION,
        )
    except Exception:
        traceback.print_exc()
        return {"id": rid, "start": start, "end": end, "error": True}

    elapsed = time.time() - t0
    print(f"\n  done {elapsed:.1f}s")

    dest.mkdir(parents=True, exist_ok=True)
    trades_df = trades if isinstance(trades, pd.DataFrame) else None
    if trades_df is not None and not trades_df.empty:
        trades_df.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(tester.daily_equity).to_csv(
        dest / "equity.csv", index=False, encoding="utf-8-sig"
    )

    eq = _equity_metrics(tester.daily_equity, tester.initial_cash)
    tm = _trade_metrics(trades_df)
    row: Dict[str, Any] = {
        "id": rid,
        "title": exp["title"],
        "start": start,
        "end": end,
        "elapsed_sec": round(elapsed, 1),
        **eq,
        **tm,
    }
    meta_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"  total={row.get('ret_total_pct', float('nan')):+.2f}% "
        f"DD={row.get('max_dd_pct', float('nan')):.1f}% "
        f"BUY1={row.get('n_buy1', 0)} "
        f"OA_BUY1={row.get('n_open_auction_buy1', 0)} "
        f"({row.get('open_auction_buy1_pct', 0):.1f}%) "
        f"win={row.get('win_rate', 0):.1f}%"
    )
    return row


def _print_summary(rows: List[Dict[str, Any]]) -> None:
    print("\n" + "=" * 100)
    print("09:00 첫1분 패스 비교 요약")
    print("=" * 100)
    hdr = (
        f"{'id':<14} {'ret%':>8} {'DD%':>7} {'BUY1':>6} {'OA_B1':>6} "
        f"{'OA%':>6} {'OA_S':>5} {'win%':>6} {'sec':>7}"
    )
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        if r.get("error"):
            print(f"{r.get('id', '?'):<14} ERROR")
            continue
        print(
            f"{r.get('id', '?'):<14} "
            f"{r.get('ret_total_pct', 0):+8.2f} "
            f"{r.get('max_dd_pct', 0):7.1f} "
            f"{r.get('n_buy1', 0):6d} "
            f"{r.get('n_open_auction_buy1', 0):6d} "
            f"{r.get('open_auction_buy1_pct', 0):6.1f} "
            f"{r.get('n_open_auction_sell', 0):5d} "
            f"{r.get('win_rate', 0):6.1f} "
            f"{r.get('elapsed_sec', 0):7.1f}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="BT 09:00 첫1분 패스 비교")
    ap.add_argument(
        "--start",
        default="2024-06-03",
        help="시작일 (기본: 짧은 스모크 구간)",
    )
    ap.add_argument(
        "--end",
        default="2024-07-31",
        help="종료일 (기본: 약 2개월)",
    )
    ap.add_argument("--only", default="", help="쉼표 구분 실험 id (예: BASE,OPEN_AUCTION)")
    ap.add_argument("--force", action="store_true", help="캐시 무시하고 재실행")
    args = ap.parse_args()

    base = _load_base()
    exps = _experiments()
    if args.only.strip():
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        exps = [e for e in exps if e["id"] in want]
    if not exps:
        print("실험이 비었습니다.")
        return 1

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows: List[Dict[str, Any]] = []
    for exp in exps:
        rows.append(
            run_one(
                exp,
                base,
                str(args.start),
                str(args.end),
                force=bool(args.force),
            )
        )

    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(
        json.dumps(
            {"start": args.start, "end": args.end, "rows": rows},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _print_summary(rows)
    print(f"\n요약: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
