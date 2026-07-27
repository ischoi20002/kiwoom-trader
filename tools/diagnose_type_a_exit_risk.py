# -*- coding: utf-8 -*-
"""
Type A 청산·포지션 리스크 진단 (스윕 없음).

1) Type A + S2(A) 기준 BT (Type B OFF)
2) SELL Reason 별 건수·평균손익·손실합·승률
3) MDD 구간과 청산사유 매칭
4) 스윕 후보 1~2개 제안만 (실행하지 않음)

  python tools/diagnose_type_a_exit_risk.py
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
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
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

OUT = ROOT / "tools" / "bt_compare_out_type_a_exit_diag"
START = "2024-06-03"
END = "2026-06-30"
OOS_START = "2025-07-01"


def _base() -> Dict[str, Any]:
    """계획 기준: Type A + S2(A), Type B / S2 B OFF."""
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    st = settings_for_backtest(raw)
    st["use_market_breadth_gate"] = False
    st["bt_high_precision"] = False
    st["enforce_track_signal_match"] = True
    st["universe_index_choice"] = "KODEX200"
    st["sel_score_mode"] = "legacy"
    st["use_type_a"] = True
    st["use_type_b"] = False
    st["use_s2_master_a"] = True
    st["use_s2_master_b"] = False
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
        # %SL (고정 손절) — ATR_SL 과 구분
        if "ATR" not in s:
            return "PCT_SL"
    if "TIME" in s or "FORCE" in s or "EOD" in s:
        return "OTHER"
    if not s or s == "NAN":
        return "OTHER"
    return "OTHER"


def _is_type_a(sig: Any) -> bool:
    return "TYPE_A" in str(sig or "").upper()


def run_bt(label: str, start: str, end: str) -> Dict[str, Any]:
    dest = OUT / f"baseline_{label}_{start}_{end}".replace(":", "")
    trades_p = dest / "trades.csv"
    equity_p = dest / "equity.csv"
    if trades_p.is_file() and equity_p.is_file():
        print(f"[캐시] {dest}")
        trades = pd.read_csv(trades_p)
        daily = pd.read_csv(equity_p)
        meta = {
            "label": label,
            "start": start,
            "end": end,
            "cached": True,
        }
        return {"meta": meta, "trades": trades, "daily": daily, "dest": dest}

    st = _base()
    print(f"\n=== BT TypeA+S2A  {label} {start}~{end} ===")
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
    return {
        "ret_total_pct": (e1 / e0 - 1.0) * 100.0,
        "max_dd_pct": float(dd.min() * 100),
        "equity_col": col,
    }


def _pair_rounds(trades: pd.DataFrame) -> pd.DataFrame:
    """BUY1→(BUY2*)→SELL 라운드. Type A만."""
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
                if not _is_type_a(sig):
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
    by_reason.sort(key=lambda x: x["loss_sum_pct"])  # 가장 큰 손실합이 앞

    by_stage: Dict[str, Any] = {}
    for stage, g in rounds.groupby("stage"):
        by_stage[str(stage)] = {
            "n": int(len(g)),
            "mean_ret_pct": float(g["ret_pct"].mean()),
            "sum_ret_pct": float(g["ret_pct"].sum()),
            "win_pct": float((g["ret_pct"] > 0).mean() * 100),
            "reasons": {
                str(r): int(n) for r, n in g["reason"].value_counts().items()
            },
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
        "peak_equity": float(eq.iloc[peak_i]),
        "trough_equity": float(eq.iloc[trough_i]),
        "mdd_pct": float(dd.iloc[trough_i] * 100),
        "peak_i": peak_i,
        "trough_i": trough_i,
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
        # 최악 개별 청산
        worst = in_dd.nsmallest(min(8, len(in_dd)), "ret_pct")[
            ["code", "exit_dt", "reason", "ret_pct", "had_s2"]
        ]
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
        worst_list = []
        print("  (구간 내 Type A 청산 없음)")

    # 전체 기간 손실합 상위 사유 vs MDD 구간 손실합 상위 비교용
    return {
        "label": label,
        "window": win,
        "exits_in_window": int(len(in_dd)),
        "by_reason": reason_rows,
        "worst_exits": worst_list,
        "mean_ret_in_window": float(in_dd["ret_pct"].mean()) if len(in_dd) else None,
        "sum_ret_in_window": float(in_dd["ret_pct"].sum()) if len(in_dd) else None,
        "s2_share_in_window": float(in_dd["had_s2"].mean()) if len(in_dd) else None,
    }


def _suggest_levers(
    reason_full: Dict[str, Any],
    mdd_full: Dict[str, Any],
    reason_oos: Dict[str, Any],
    mdd_oos: Dict[str, Any],
) -> List[Dict[str, str]]:
    """스윕은 하지 않고 후보만 제안. 순익절(TP/FLASH)보다 손실·MDD 기여를 우선."""
    suggestions: List[Dict[str, str]] = []

    # 사유별 점수: 전체 손실합 + MDD구간 손실합(가중) — 음수일수록 우선
    scores: Dict[str, float] = {}
    for src, w in (
        (reason_full.get("by_reason") or [], 1.0),
        (reason_oos.get("by_reason") or [], 0.8),
        (mdd_full.get("by_reason") or [], 2.0),
        (mdd_oos.get("by_reason") or [], 2.5),
    ):
        for r in src:
            name = str(r.get("reason", ""))
            # 순수익 청산(mean>0 & loss_sum≈0)은 MDD 레버 후보에서 제외
            mean_ret = float(r.get("mean_ret_pct", 0) or 0)
            loss_sum = float(r.get("loss_sum_pct", 0) or 0)
            if mean_ret > 0.5 and loss_sum > -5.0:
                continue
            if loss_sum >= -1.0 and name not in ("ATR_SL", "GAP_DOWN"):
                continue
            scores[name] = scores.get(name, 0.0) + loss_sum * w

    priority = sorted(scores.keys(), key=lambda k: scores[k])  # 더 음수 우선

    lever_map = {
        "ATR_SL": {
            "lever": "atr_multiplier_sl_a (및 S2 atr_multiplier_sl_s2)",
            "why": "ATR_SL이 건수·손실합·MDD 구간의 주 손실원 → 배수 소폭 스윕",
            "sweep": "2.8 → 2.2 / 3.2 (S2: 2.0 → 1.6 / 2.4)",
        },
        "FLASH": {
            "lever": "s1_a_flash_exit_pct (및 s2_flash_exit_pct)",
            "why": "FLASH는 승률 높으나 이익 절단 여부 확인용",
            "sweep": "S1 4.0 → 3.0 / 5.0",
        },
        "ATR_TRAIL": {
            "lever": "atr_multiplier_trail_a / use_atr_trail_only_after_tp_s1_a",
            "why": "트레일 손실 꼬리가 MDD에 기여할 때만",
            "sweep": "trail 5.0 → 4.0 / 6.0 또는 S1 trail-after-TP ON",
        },
        "GAP_DOWN": {
            "lever": "gap_down_limit_a",
            "why": "건수는 적지만 건당 손실 최대(~-5%)·OOS MDD 주원인",
            "sweep": "-4.0 → -3.0 / -5.0",
        },
        "PCT_SL": {
            "lever": "use_s1_a_stop_loss / s1_a_stop_loss",
            "why": "현재 S1 A %SL OFF — ATR_SL 대안 비교용",
            "sweep": "OFF 유지 vs ON 3.0% 재도입 비교",
        },
    }

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
                    "note": "이번 단계에서는 스윕 미실행 — 반영 전 별도 승인",
                }
            )

    if not suggestions:
        suggestions.append(
            {
                "reason": "ATR_SL",
                "lever": "atr_multiplier_sl_a",
                "why": "기본 손실 레버",
                "suggested_sweep": "2.8 → 2.2 / 3.2",
                "note": "스윕 미실행",
            }
        )

    print("\n=== 스윕 후보 (실행하지 않음) ===")
    for i, s in enumerate(suggestions, 1):
        print(f"  {i}. [{s['reason']}] {s['lever']}")
        print(f"     why: {s['why']}")
        print(f"     sweep: {s['suggested_sweep']}")
    return suggestions


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        full = run_bt("full", START, END)
        oos = run_bt("oos", OOS_START, END)

        # equity stats (캐시 포함)
        for pack in (full, oos):
            st = _equity_stats(pack["daily"], pack["meta"].get("initial_cash"))
            pack["meta"].update({k: v for k, v in st.items() if k != "equity_col"})

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
            "objective": "Type A exit/risk diagnose (no sweep)",
            "settings_snapshot": {
                "use_type_a": True,
                "use_type_b": False,
                "use_s2_master_a": True,
                "use_s2_master_b": False,
                "s1_a_take_profit": 6.0,
                "use_s1_a_stop_loss": False,
                "s1_a_flash_exit_pct": 4.0,
                "atr_multiplier_sl_a": 2.8,
                "atr_multiplier_trail_a": 5.0,
                "gap_down_limit_a": -4.0,
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
            f"기준선 full: {full['meta'].get('ret_total_pct', float('nan')):+.2f}% "
            f"DD={full['meta'].get('max_dd_pct', float('nan')):.1f}%"
        )
        print(
            f"기준선 oos:  {oos['meta'].get('ret_total_pct', float('nan')):+.2f}% "
            f"DD={oos['meta'].get('max_dd_pct', float('nan')):.1f}%"
        )
        print(f"[저장] {OUT / 'summary.json'}")
        return 0
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
