# -*- coding: utf-8 -*-
"""
Type B 청산·포지션 리스크 진단 (스윕 없음).

1) Type B + S2(B) 기준 BT (Type A OFF)
2) SELL Reason 별 집계 + MDD 구간 매칭
3) 스윕 후보 1~2개 제안만

  python tools/diagnose_type_b_exit_risk.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
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

OUT = ROOT / "tools" / "bt_compare_out_type_b_exit_diag"
START = "2024-06-03"
END = "2026-06-30"
OOS_START = "2025-07-01"


def _base() -> Dict[str, Any]:
    """Type B + S2(B), Type A / S2 A OFF."""
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["use_market_breadth_gate"] = False
    st["bt_high_precision"] = False
    st["enforce_track_signal_match"] = True
    st["universe_index_choice"] = "KODEX200"
    st["sel_score_mode"] = "legacy"
    st["use_type_a"] = False
    st["use_type_b"] = True
    st["use_s2_master_a"] = False
    st["use_s2_master_b"] = True
    return st


def _norm_reason(raw: Any) -> str:
    s = str(raw or "").upper()
    if "GAP" in s:
        return "GAP_DOWN"
    if "FLASH" in s:
        return "FLASH"
    if "TAKE_PROFIT" in s or s.strip() == "TP" or "TAKE PROFIT" in s:
        return "TAKE_PROFIT"
    if "ATR" in s and ("TRL" in s or "TRAIL" in s):
        return "ATR_TRAIL"
    if "ATR" in s and "SL" in s:
        return "ATR_SL"
    if "SL%" in s or re.search(r"\bSL\b", s) or "STOP" in s:
        if "ATR" not in s:
            return "PCT_SL"
    if not s or s == "NAN":
        return "OTHER"
    return "OTHER"


def _is_type_b(sig: Any) -> bool:
    return "TYPE_B" in str(sig or "").upper()


def run_bt(label: str, start: str, end: str) -> Dict[str, Any]:
    dest = OUT / f"baseline_{label}_{start}_{end}".replace(":", "")
    trades_p = dest / "trades.csv"
    equity_p = dest / "equity.csv"
    if trades_p.is_file() and equity_p.is_file():
        print(f"[캐시] {dest}")
        trades = pd.read_csv(trades_p)
        daily = pd.read_csv(equity_p)
        meta = {"label": label, "start": start, "end": end, "cached": True}
        return {"meta": meta, "trades": trades, "daily": daily, "dest": dest}

    st = _base()
    print(f"\n=== BT TypeB+S2B  {label} {start}~{end} ===")
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

    dest.mkdir(parents=True, exist_ok=True)
    if isinstance(trades, pd.DataFrame):
        trades.to_csv(trades_p, index=False, encoding="utf-8-sig")
    daily.to_csv(equity_p, index=False, encoding="utf-8-sig")

    meta = {
        "label": label,
        "start": start,
        "end": end,
        "cached": False,
        "ret_total_pct": (e1 / e0 - 1.0) * 100.0,
        "max_dd_pct": float(dd.min() * 100),
        "n_buy1": int((trades["Action"] == "BUY1").sum()) if isinstance(trades, pd.DataFrame) else 0,
        "n_buy2": int((trades["Action"] == "BUY2").sum()) if isinstance(trades, pd.DataFrame) else 0,
        "elapsed_sec": round(elapsed, 1),
        "initial_cash": e0,
    }
    (dest / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ret={meta['ret_total_pct']:+.2f}% DD={meta['max_dd_pct']:.1f}%")
    return {"meta": meta, "trades": trades, "daily": daily, "dest": dest}


def _equity_stats(daily: pd.DataFrame, initial_cash: Optional[float] = None) -> Dict[str, float]:
    d = daily.copy()
    if "d" not in d.columns:
        d["date"] = pd.to_datetime(d.get("date", d.iloc[:, 0]), format="mixed", errors="coerce")
        d["d"] = d["date"].dt.normalize()
    else:
        d["d"] = pd.to_datetime(d["d"], format="mixed", errors="coerce")
    col = "equity" if "equity" in d.columns else ("Total" if "Total" in d.columns else None)
    if col is None:
        return {}
    d = d.dropna(subset=["d"]).sort_values("d")
    e0 = float(initial_cash) if initial_cash is not None else float(d.iloc[0][col])
    e1 = float(d.iloc[-1][col])
    dd = (d[col] - d[col].cummax()) / d[col].cummax()
    return {"ret_total_pct": (e1 / e0 - 1.0) * 100.0, "max_dd_pct": float(dd.min() * 100)}


def _pair_rounds(trades: pd.DataFrame) -> pd.DataFrame:
    df = trades.copy()
    df["Date"] = pd.to_datetime(df["Date"], format="mixed", errors="coerce")
    df["Code"] = df["Code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    df = df.dropna(subset=["Date"]).sort_values(["Code", "Date"])
    rows: List[dict[str, Any]] = []
    for code, g in df.groupby("Code", sort=False):
        open_buy: Optional[pd.Series] = None
        had_s2 = False
        for _, r in g.iterrows():
            act = str(r.get("Action", ""))
            if act == "BUY1":
                open_buy = r
                had_s2 = False
            elif act == "BUY2" and open_buy is not None:
                had_s2 = True
            elif act == "SELL" and open_buy is not None:
                sig = open_buy.get("Sig_Type", r.get("Sig_Type", ""))
                if not _is_type_b(sig):
                    open_buy = None
                    had_s2 = False
                    continue
                try:
                    ret = float(r.get("Return", np.nan))
                except Exception:
                    ret = float("nan")
                rows.append(
                    {
                        "code": code,
                        "entry_dt": open_buy["Date"],
                        "exit_dt": r["Date"],
                        "sig_type": str(sig),
                        "ret_pct": ret,
                        "reason_raw": str(r.get("Reason", "")),
                        "reason": _norm_reason(r.get("Reason", "")),
                        "had_s2": had_s2,
                        "stage": "S2" if had_s2 else "S1",
                    }
                )
                open_buy = None
                had_s2 = False
    return pd.DataFrame(rows)


def _reason_breakdown(rounds: pd.DataFrame, label: str) -> Dict[str, Any]:
    if rounds.empty:
        return {"label": label, "n": 0, "by_reason": [], "by_stage": {}}
    print(f"\n=== 청산사유 분해 [{label}] n={len(rounds)} mean={rounds['ret_pct'].mean():+.2f}% ===")
    by_reason: List[dict[str, Any]] = []
    for reason, g in rounds.groupby("reason"):
        loss = g.loc[g["ret_pct"] < 0, "ret_pct"]
        row = {
            "reason": str(reason),
            "n": int(len(g)),
            "pct_of_trades": float(len(g) / len(rounds) * 100),
            "mean_ret_pct": float(g["ret_pct"].mean()),
            "sum_ret_pct": float(g["ret_pct"].sum()),
            "win_pct": float((g["ret_pct"] > 0).mean() * 100),
            "loss_n": int((g["ret_pct"] < 0).sum()),
            "loss_sum_pct": float(loss.sum()) if len(loss) else 0.0,
            "avg_loss_pct": float(loss.mean()) if len(loss) else 0.0,
        }
        by_reason.append(row)
        print(
            f"  {reason:12s} n={row['n']:3d} ({row['pct_of_trades']:4.1f}%)  "
            f"mean={row['mean_ret_pct']:+.2f}%  sum={row['sum_ret_pct']:+.1f}%  "
            f"win={row['win_pct']:.0f}%  loss_sum={row['loss_sum_pct']:+.1f}%"
        )
    by_reason.sort(key=lambda x: x["loss_sum_pct"])
    by_stage: Dict[str, Any] = {}
    for stage, g in rounds.groupby("stage"):
        by_stage[str(stage)] = {
            "n": int(len(g)),
            "mean_ret_pct": float(g["ret_pct"].mean()),
            "sum_ret_pct": float(g["ret_pct"].sum()),
            "win_pct": float((g["ret_pct"] > 0).mean() * 100),
            "reasons": {str(r): int(n) for r, n in g["reason"].value_counts().items()},
        }
        print(
            f"  [{stage}] n={len(g)} mean={g['ret_pct'].mean():+.2f}% "
            f"win={(g['ret_pct']>0).mean()*100:.0f}% reasons={by_stage[str(stage)]['reasons']}"
        )
    return {"label": label, "n": len(rounds), "by_reason": by_reason, "by_stage": by_stage}


def _find_mdd_window(daily: pd.DataFrame) -> Optional[Dict[str, Any]]:
    d = daily.copy()
    if "d" not in d.columns:
        d["date"] = pd.to_datetime(d.get("date", d.iloc[:, 0]), format="mixed", errors="coerce")
        d["d"] = d["date"].dt.normalize()
    else:
        d["d"] = pd.to_datetime(d["d"], format="mixed", errors="coerce")
    col = "equity" if "equity" in d.columns else "Total"
    d = d.dropna(subset=["d"]).sort_values("d").reset_index(drop=True)
    eq = d[col].astype(float)
    peak = eq.cummax()
    dd = (eq - peak) / peak
    trough_i = int(dd.idxmin())
    peak_i = int(eq.iloc[: trough_i + 1].idxmax()) if trough_i >= 0 else 0
    return {
        "peak_date": str(pd.Timestamp(d.loc[peak_i, "d"]).date()),
        "trough_date": str(pd.Timestamp(d.loc[trough_i, "d"]).date()),
        "mdd_pct": float(dd.iloc[trough_i] * 100),
    }


def _mdd_attribution(rounds: pd.DataFrame, daily: pd.DataFrame, label: str) -> Dict[str, Any]:
    win = _find_mdd_window(daily)
    if win is None or rounds.empty:
        return {"label": label, "window": None}
    peak = pd.Timestamp(win["peak_date"])
    trough = pd.Timestamp(win["trough_date"])
    in_dd = rounds[
        (pd.to_datetime(rounds["exit_dt"]).dt.normalize() >= peak)
        & (pd.to_datetime(rounds["exit_dt"]).dt.normalize() <= trough)
    ].copy()
    print(
        f"\n=== MDD 구간 [{label}] {win['peak_date']} → {win['trough_date']} "
        f"MDD={win['mdd_pct']:.1f}%  exits_in_window={len(in_dd)} ==="
    )
    reason_rows: List[dict[str, Any]] = []
    worst_list: List[dict[str, Any]] = []
    if len(in_dd):
        for reason, g in in_dd.groupby("reason"):
            loss = g.loc[g["ret_pct"] < 0, "ret_pct"]
            row = {
                "reason": str(reason),
                "n": int(len(g)),
                "mean_ret_pct": float(g["ret_pct"].mean()),
                "sum_ret_pct": float(g["ret_pct"].sum()),
                "loss_sum_pct": float(loss.sum()) if len(loss) else 0.0,
                "n_s2": int(g["had_s2"].sum()),
            }
            reason_rows.append(row)
            print(
                f"  {reason:12s} n={row['n']:3d} mean={row['mean_ret_pct']:+.2f}% "
                f"sum={row['sum_ret_pct']:+.1f}% loss_sum={row['loss_sum_pct']:+.1f}% s2={row['n_s2']}"
            )
        reason_rows.sort(key=lambda x: x["loss_sum_pct"])
        worst = in_dd.nsmallest(min(8, len(in_dd)), "ret_pct")
        print("  최악 청산:")
        for _, r in worst.iterrows():
            print(
                f"    {r['code']} {pd.Timestamp(r['exit_dt']).date()} "
                f"{r['reason']} {r['ret_pct']:+.2f}% s2={bool(r['had_s2'])}"
            )
        worst_list = worst.assign(
            exit_dt=lambda x: pd.to_datetime(x["exit_dt"]).dt.strftime("%Y-%m-%d")
        ).to_dict("records")
    else:
        print("  (구간 내 Type B 청산 없음)")
    return {
        "label": label,
        "window": win,
        "exits_in_window": int(len(in_dd)),
        "by_reason": reason_rows,
        "worst_exits": worst_list,
        "sum_ret_in_window": float(in_dd["ret_pct"].sum()) if len(in_dd) else None,
    }


def _suggest_levers(
    reason_full: Dict[str, Any],
    mdd_full: Dict[str, Any],
    reason_oos: Dict[str, Any],
    mdd_oos: Dict[str, Any],
) -> List[Dict[str, str]]:
    scores: Dict[str, float] = {}
    for src, w in (
        (reason_full.get("by_reason") or [], 1.0),
        (reason_oos.get("by_reason") or [], 0.8),
        (mdd_full.get("by_reason") or [], 2.0),
        (mdd_oos.get("by_reason") or [], 2.5),
    ):
        for r in src:
            name = str(r.get("reason", ""))
            mean_ret = float(r.get("mean_ret_pct", 0) or 0)
            loss_sum = float(r.get("loss_sum_pct", 0) or 0)
            if mean_ret > 0.5 and loss_sum > -5.0:
                continue
            if loss_sum >= -1.0 and name not in ("ATR_SL", "GAP_DOWN", "PCT_SL"):
                continue
            scores[name] = scores.get(name, 0.0) + loss_sum * w
    priority = sorted(scores.keys(), key=lambda k: scores[k])
    lever_map = {
        "ATR_SL": {
            "lever": "atr_multiplier_sl_b (및 S2 atr_multiplier_sl_s2_b)",
            "why": "ATR_SL 손실·MDD 기여",
            "sweep": "2.0 → 1.6 / 2.4 (S2: 1.5 → 1.2 / 1.8)",
        },
        "PCT_SL": {
            "lever": "use_s1_b_stop_loss / s1_b_stop_loss",
            "why": "S1 B %SL ON(3%) — 고정 손절 기여",
            "sweep": "3.0 → 2.5 / 3.5 또는 OFF 비교",
        },
        "GAP_DOWN": {
            "lever": "gap_down_limit_b",
            "why": "갭다운 꼬리손실",
            "sweep": "-4.0 → -3.0 / -5.0",
        },
        "ATR_TRAIL": {
            "lever": "atr_multiplier_trail_b / use_atr_trail_only_after_tp_s1_b",
            "why": "트레일 손실 꼬리",
            "sweep": "6.0 → 5.0 / 7.0",
        },
        "FLASH": {
            "lever": "s1_b_flash_exit_pct",
            "why": "조기 익절 절단 여부",
            "sweep": "2.0 → 1.5 / 2.5",
        },
    }
    suggestions: List[Dict[str, str]] = []
    for reason in priority:
        if reason in lever_map and len(suggestions) < 2:
            info = lever_map[reason]
            suggestions.append(
                {
                    "reason": reason,
                    "lever": info["lever"],
                    "why": info["why"],
                    "suggested_sweep": info["sweep"],
                    "score": round(float(scores.get(reason, 0)), 2),
                    "note": "스윕은 별도 스크립트에서 실행",
                }
            )
    if not suggestions:
        suggestions.append(
            {
                "reason": "ATR_SL",
                "lever": "atr_multiplier_sl_b",
                "why": "기본 손실 레버",
                "suggested_sweep": "2.0 → 1.6 / 2.4",
                "note": "",
            }
        )
    print("\n=== 스윕 후보 ===")
    for i, s in enumerate(suggestions, 1):
        print(f"  {i}. [{s['reason']}] {s['lever']} — {s['suggested_sweep']}")
    return suggestions


# numpy for nan in pair_rounds
import numpy as np  # noqa: E402


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        full = run_bt("full", START, END)
        oos = run_bt("oos", OOS_START, END)
        for pack in (full, oos):
            pack["meta"].update(_equity_stats(pack["daily"], pack["meta"].get("initial_cash")))
        rounds_full = _pair_rounds(full["trades"])
        rounds_oos = _pair_rounds(oos["trades"])
        rounds_full.to_csv(OUT / "rounds_full.csv", index=False, encoding="utf-8-sig")
        rounds_oos.to_csv(OUT / "rounds_oos.csv", index=False, encoding="utf-8-sig")
        reason_full = _reason_breakdown(rounds_full, "full")
        reason_oos = _reason_breakdown(rounds_oos, "oos")
        mdd_full = _mdd_attribution(rounds_full, full["daily"], "full")
        mdd_oos = _mdd_attribution(rounds_oos, oos["daily"], "oos")
        suggestions = _suggest_levers(reason_full, mdd_full, reason_oos, mdd_oos)
        payload = {
            "objective": "Type B exit/risk diagnose",
            "settings_snapshot": {
                "use_type_b": True,
                "use_s2_master_b": True,
                "s1_b_take_profit": 4.0,
                "use_s1_b_stop_loss": True,
                "s1_b_stop_loss": 3.0,
                "s1_b_flash_exit_pct": 2.0,
                "atr_multiplier_sl_b": 2.0,
                "atr_multiplier_trail_b": 6.0,
                "gap_down_limit_b": -4.0,
            },
            "baseline": {"full": full["meta"], "oos": oos["meta"]},
            "reason_breakdown": {"full": reason_full, "oos": reason_oos},
            "mdd_attribution": {"full": mdd_full, "oos": mdd_oos},
            "suggested_levers": suggestions,
        }
        (OUT / "summary.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print("\n" + "=" * 64)
        print(
            f"기준선 full: {full['meta'].get('ret_total_pct'):+.2f}% "
            f"DD={full['meta'].get('max_dd_pct'):.1f}%"
        )
        print(
            f"기준선 oos:  {oos['meta'].get('ret_total_pct'):+.2f}% "
            f"DD={oos['meta'].get('max_dd_pct'):.1f}%"
        )
        print(f"[저장] {OUT / 'summary.json'}")
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
