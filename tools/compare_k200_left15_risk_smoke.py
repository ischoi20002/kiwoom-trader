# -*- coding: utf-8 -*-
"""
좌측라벨 15분봉 기준 리스크/청산 + Type B 가벼운 스모크.

축:
- DD sizing 재활성 (현 settings에 키 없음 = OFF)
- ATR trail A/B 미세
- Type B gap down / 선정 필터

  python tools/compare_k200_left15_risk_smoke.py
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

from backtest_engine import settings_for_backtest  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

OUT_DIR = ROOT / "tools" / "bt_compare_out_left15_risk_smoke"
OOS_START = "2025-07-01"
CMP_END = "2026-07-27"  # left15 smoke / 구 baseline과 동일

EXPERIMENTS: List[Dict[str, Any]] = [
    {
        "id": "BASE",
        "title": "현행 (DD OFF, trail A5/B6, B gap -2.5)",
        "overrides": {},
    },
    {
        "id": "DD15_S50",
        "title": "DD≥15% → 금액×0.5 (구 baseline)",
        "overrides": {
            "use_dd_sizing": True,
            "dd_sizing_trigger_pct": 15.0,
            "dd_sizing_scale": 0.5,
            "dd_slots_scale": 1.0,
        },
    },
    {
        "id": "DD12_S70",
        "title": "DD≥12% → 금액×0.7",
        "overrides": {
            "use_dd_sizing": True,
            "dd_sizing_trigger_pct": 12.0,
            "dd_sizing_scale": 0.7,
            "dd_slots_scale": 1.0,
        },
    },
    {
        "id": "TRAIL_B55",
        "title": "ATR trail B 5.5 (현재 6.0)",
        "overrides": {"atr_multiplier_trail_b": 5.5},
    },
    {
        "id": "TRAIL_A45",
        "title": "ATR trail A 4.5 (현재 5.0)",
        "overrides": {"atr_multiplier_trail_a": 4.5},
    },
    {
        "id": "B_GAP20",
        "title": "Type B gap -2.0 (더 빡센 갭청산)",
        "overrides": {"gap_down_limit_b": -2.0},
    },
    {
        "id": "B_GAP30",
        "title": "Type B gap -3.0 (완화)",
        "overrides": {"gap_down_limit_b": -3.0},
    },
    {
        "id": "B_HI94",
        "title": "Type B hi_min 94% (현재 92)",
        "overrides": {"sel_b_hi_min_pct": 94.0},
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


def _trade_metrics(trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "n_buy1": 0,
        "n_buy2": 0,
        "n_sell": 0,
        "win_rate": 0.0,
        "avg_trade_ret_pct": 0.0,
        "sell_A_n": 0,
        "sell_A_avg": 0.0,
        "sell_B_n": 0,
        "sell_B_avg": 0.0,
    }
    if trades is None or trades.empty:
        return out
    buys1 = trades[trades["Action"] == "BUY1"]
    buys2 = trades[trades["Action"] == "BUY2"]
    sells = trades[trades["Action"] == "SELL"]
    out["n_buy1"] = int(len(buys1))
    out["n_buy2"] = int(len(buys2))
    out["n_sell"] = int(len(sells))
    if len(sells):
        out["win_rate"] = float((sells["Return"] > 0).mean() * 100)
        out["avg_trade_ret_pct"] = float(sells["Return"].mean())
    col = "Sig_Type" if "Sig_Type" in sells.columns else None
    if col and len(sells):
        for key, label in (("TYPE_A", "A"), ("TYPE_B", "B")):
            sub = sells[sells[col].astype(str).str.contains(key, na=False)]
            out[f"sell_{label}_n"] = int(len(sub))
            out[f"sell_{label}_avg"] = float(sub["Return"].mean()) if len(sub) else 0.0
    return out


def run_one(exp: Dict[str, Any], base: Dict[str, Any], start: str, end: str) -> Dict[str, Any]:
    period = "full" if start == str(base.get("bt_start_date", "2024-06-03")) else "oos"
    # bt_start_date가 다를 수 있어 명시 비교
    if start == OOS_START:
        period = "oos"
    elif start == str(base.get("bt_start_date", "2024-06-03")):
        period = "full"
    else:
        period = "full" if start < OOS_START else "oos"

    run_id = f"{exp['id']}_{period}"
    dest = OUT_DIR / run_id
    meta_path = dest / "meta.json"
    if meta_path.is_file():
        try:
            cached = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            if cached.get("start") == start and cached.get("end") == end:
                print(f"[{run_id}] 캐시 재사용")
                return cached
        except Exception:
            pass

    print(f"\n[{run_id}] {exp['title']}  {start} ~ {end}")
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
        trades = tester.run(start, end, interval="15m", callback=cb, high_precision=False)
    except Exception:
        traceback.print_exc()
        return {"id": exp["id"], "period": period, "error": True}
    elapsed = time.time() - t0
    print(f"\n  done {elapsed:.1f}s")

    dest.mkdir(parents=True, exist_ok=True)
    if isinstance(trades, pd.DataFrame) and not trades.empty:
        trades.to_csv(dest / "trades.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(tester.daily_equity).to_csv(dest / "equity.csv", index=False, encoding="utf-8-sig")

    row: Dict[str, Any] = {
        "id": exp["id"],
        "title": exp["title"],
        "period": period,
        "start": start,
        "end": end,
        "elapsed_sec": round(elapsed, 1),
        **_equity_metrics(tester.daily_equity, tester.initial_cash),
        **_trade_metrics(trades if isinstance(trades, pd.DataFrame) else None),
    }
    meta_path.write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"  ret={row.get('ret_total_pct', float('nan')):+.2f}% "
        f"MDD={row.get('max_dd_pct', float('nan')):.1f}% "
        f"BUY1={row.get('n_buy1', 0)} "
        f"win={row.get('win_rate', 0):.1f}% "
        f"A={row.get('sell_A_avg', 0):+.2f}% B={row.get('sell_B_avg', 0):+.2f}%"
    )
    return row


def _print_summary(rows: List[Dict[str, Any]]) -> None:
    for period in ("full", "oos"):
        sub = [r for r in rows if r.get("period") == period and "ret_total_pct" in r]
        if not sub:
            continue
        base = next((r for r in sub if r.get("id") == "BASE"), None)
        print("\n" + "=" * 100)
        print(f"LEFT15 RISK SMOKE {period.upper()}")
        print("=" * 100)
        print(
            f"  {'ID':12s} {'수익%':>9s} {'Δret':>7s} {'MDD%':>8s} {'ΔMDD':>7s} "
            f"{'BUY1':>5s} {'승률':>6s} {'Aavg':>7s} {'Bavg':>7s}"
        )
        for r in sorted(sub, key=lambda x: float(x.get("ret_total_pct", -999)), reverse=True):
            d_ret = 0.0
            d_mdd = 0.0
            if base is not None:
                d_ret = float(r.get("ret_total_pct", 0)) - float(base.get("ret_total_pct", 0))
                d_mdd = float(r.get("max_dd_pct", 0)) - float(base.get("max_dd_pct", 0))
            print(
                f"  {str(r.get('id')):12s} "
                f"{float(r.get('ret_total_pct', 0)):+9.2f} "
                f"{d_ret:+7.2f} "
                f"{float(r.get('max_dd_pct', 0)):8.1f} "
                f"{d_mdd:+7.1f} "
                f"{int(r.get('n_buy1', 0)):5d} "
                f"{float(r.get('win_rate', 0)):6.1f} "
                f"{float(r.get('sell_A_avg', 0)):+7.2f} "
                f"{float(r.get('sell_B_avg', 0)):+7.2f}"
            )


def _judge(rows: List[Dict[str, Any]]) -> List[str]:
    """OOS 우선: MDD 개선 + 수익 하락 제한, FULL 붕괴 금지."""
    notes: List[str] = []
    oos = {r["id"]: r for r in rows if r.get("period") == "oos" and "ret_total_pct" in r}
    full = {r["id"]: r for r in rows if r.get("period") == "full" and "ret_total_pct" in r}
    if "BASE" not in oos:
        return ["BASE OOS 없음 — 판정 불가"]
    b = oos["BASE"]
    b_ret = float(b["ret_total_pct"])
    b_mdd = float(b["max_dd_pct"])  # more negative = worse
    candidates: List[tuple] = []
    for eid, r in oos.items():
        if eid == "BASE":
            continue
        d_ret = float(r["ret_total_pct"]) - b_ret
        # MDD: less negative is better → positive Δ means improvement
        d_mdd = float(r["max_dd_pct"]) - b_mdd
        f = full.get(eid)
        f_ok = True
        f_note = ""
        if f:
            f_d = float(f["ret_total_pct"]) - float(full["BASE"]["ret_total_pct"])
            if f_d < -15.0:
                f_ok = False
                f_note = f" FULLΔ={f_d:+.1f}pp 과다하락"
        # 합격: OOS MDD 개선(≥0.3pp) and 수익 하락 ≤5pp, FULL OK
        score = d_mdd * 2.0 + d_ret  # MDD 개선 가중
        ok = d_mdd >= 0.3 and d_ret >= -5.0 and f_ok
        soft = d_ret > 1.0 and d_mdd >= -0.5 and f_ok  # 수익↑ + MDD 유지
        tag = "PASS" if ok else ("SOFT" if soft else "NO")
        candidates.append((score, tag, eid, d_ret, d_mdd, f_note))
        notes.append(
            f"  {eid:12s} [{tag}] OOS Δret={d_ret:+.2f} ΔMDD={d_mdd:+.2f}{f_note}"
        )
    candidates.sort(reverse=True)
    notes.insert(0, "판정 (PASS=MDD≥+0.3pp & ret≥-5pp & FULL 붕괴X / SOFT=수익↑·MDD유지):")
    picks = [c for c in candidates if c[1] in ("PASS", "SOFT")]
    if picks:
        best = picks[0]
        notes.append(f"→ 추천 후보: {best[2]} ({best[1]}, score={best[0]:+.2f})")
    else:
        notes.append("→ 추천: BASE 유지 (개선 후보 없음)")
    return notes


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    base = _load_base()
    full_start = str(base.get("bt_start_date", "2024-06-03"))
    full_end = CMP_END

    print("=" * 72)
    print("LEFT15 risk/exit/B smoke")
    print(f"FULL {full_start}~{full_end} / OOS {OOS_START}~{full_end}")
    print(f"실험 {len(EXPERIMENTS)}개 × 2기간")
    print("=" * 72)

    # OOS 먼저 (빠른 방향), 이후 FULL
    rows: List[Dict[str, Any]] = []
    for exp in EXPERIMENTS:
        rows.append(run_one(exp, base, OOS_START, full_end))
    for exp in EXPERIMENTS:
        rows.append(run_one(exp, base, full_start, full_end))

    summary_path = OUT_DIR / "summary.json"
    summary_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_summary(rows)
    print("\n" + "=" * 72)
    for line in _judge(rows):
        print(line)
    print(f"\n저장: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
