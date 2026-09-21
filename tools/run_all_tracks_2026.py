# -*- coding: utf-8 -*-
"""2026년 Track A/B/C 통합 백테스트와 동일 조건 A/B 기준선을 비교합니다."""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from backtest_engine import settings_for_backtest  # noqa: E402
from core.defaults import DEFAULT_SETTINGS  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

START = "2026-01-01"
END = "2026-09-18"
OUT = ROOT / "tools" / "bt_all_tracks_2026"


def _base_settings() -> Dict[str, Any]:
    """현재 사용자 설정을 기본값과 병합해 백테스트 설정으로 변환합니다."""
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    merged = dict(DEFAULT_SETTINGS)
    merged.update(raw)
    settings = settings_for_backtest(merged)
    settings.update(
        {
            "bt_high_precision": False,
            "use_type_a": True,
            "use_type_b": True,
            "use_type_c": True,
            "use_sel_track_a": True,
            "use_sel_track_b": True,
            "use_sel_track_c": True,
            "max_slots_c": int(settings.get("max_slots_c", 15) or 15),
            "enforce_track_signal_match": True,
        }
    )
    return settings


def _equity_metrics(
    tester: DynamicBacktester,
    trades: Optional[pd.DataFrame],
) -> Dict[str, Any]:
    """비용 반영 포트폴리오 수익률·MDD·월별 수익률을 계산합니다."""
    equity = pd.DataFrame(tester.daily_equity)
    if equity.empty:
        return {
            "return_pct": 0.0,
            "max_dd_pct": 0.0,
            "end_equity": float(tester.initial_cash),
            "monthly_returns": {},
        }
    equity["date"] = pd.to_datetime(equity["date"], format="mixed", errors="coerce")
    equity["equity"] = pd.to_numeric(equity["equity"], errors="coerce")
    equity.dropna(subset=["date", "equity"], inplace=True)
    equity.sort_values("date", inplace=True)
    initial = float(tester.initial_cash)
    end_equity = float(equity.iloc[-1]["equity"])
    drawdown = equity["equity"] / equity["equity"].cummax() - 1.0

    month_end = equity.set_index("date")["equity"].resample("ME").last().dropna()
    month_base = pd.concat(
        [
            pd.Series(
                [initial],
                index=[pd.Timestamp(equity.iloc[0]["date"]) - pd.Timedelta(days=1)],
            ),
            month_end,
        ]
    )
    monthly = month_base.pct_change().dropna() * 100.0
    frame = trades if isinstance(trades, pd.DataFrame) else pd.DataFrame()
    return {
        "initial_equity": round(initial, 0),
        "end_equity": round(end_equity, 0),
        "profit_krw": round(end_equity - initial, 0),
        "return_pct": round((end_equity / initial - 1.0) * 100.0, 3),
        "max_dd_pct": round(float(drawdown.min()) * 100.0, 3),
        "n_buy1": int((frame.get("Action") == "BUY1").sum()) if not frame.empty else 0,
        "n_buy2": int((frame.get("Action") == "BUY2").sum()) if not frame.empty else 0,
        "n_exits": int(
            frame.get("Action", pd.Series(dtype=str)).isin(["SELL", "HOLD"]).sum()
        )
        if not frame.empty
        else 0,
        "monthly_returns": {
            index.strftime("%Y-%m"): round(float(value), 3)
            for index, value in monthly.items()
        },
    }


def _normalized_reason(value: Any) -> str:
    """예약·단일가 체결 접두사와 무관하게 실제 청산 원인을 반환합니다."""
    text = str(value or "").upper()
    for reason in (
        "BOX_C_TAKE_PROFIT",
        "BOX_C_GAP_DOWN",
        "BOX_C_BREAKDOWN",
        "BOX_C_TIME_EXIT",
        "BOX_C_STOP",
    ):
        if reason in text:
            return reason
    return text.split("|", 1)[0] or "UNKNOWN"


def _track_metrics(trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    """TYPE_A/B/C별 진입·청산 품질과 청산 사유를 집계합니다."""
    if trades is None or trades.empty:
        return {}
    frame = trades.copy()
    frame["Sig_Type"] = frame.get("Sig_Type", "").astype(str).str.upper()
    frame["Return"] = pd.to_numeric(frame.get("Return"), errors="coerce")
    frame["Fee"] = pd.to_numeric(frame.get("Fee"), errors="coerce").fillna(0.0)
    result: Dict[str, Any] = {}
    for sig_type in ("TYPE_A", "TYPE_B", "TYPE_C"):
        group = frame[frame["Sig_Type"] == sig_type]
        exits = group[group["Action"].isin(["SELL", "HOLD"])].copy()
        returns = exits["Return"].dropna()
        reasons = (
            exits.get("Reason", pd.Series(dtype=str))
            .map(_normalized_reason)
            .value_counts()
            .to_dict()
        )
        positive = float(returns[returns > 0].sum())
        negative = abs(float(returns[returns < 0].sum()))
        result[sig_type] = {
            "n_buy1": int((group["Action"] == "BUY1").sum()),
            "n_buy2": int((group["Action"] == "BUY2").sum()),
            "n_exits": int(len(exits)),
            "win_rate_pct": round(
                float((returns > 0).mean() * 100.0) if len(returns) else 0.0,
                2,
            ),
            "avg_exit_return_pct": round(
                float(returns.mean()) if len(returns) else 0.0,
                3,
            ),
            "median_exit_return_pct": round(
                float(returns.median()) if len(returns) else 0.0,
                3,
            ),
            "return_profit_factor": round(
                positive / negative
                if negative > 0
                else (999.0 if positive > 0 else 0.0),
                3,
            ),
            "fees_krw": round(float(group["Fee"].sum()), 0),
            "exit_reasons": {
                str(reason): int(count) for reason, count in reasons.items()
            },
        }
    return result


def _precomputed_context(tester: DynamicBacktester) -> Dict[str, Any]:
    """A/B 기준선이 동일 스케줄·15분 지표를 재사용할 컨텍스트를 만듭니다."""
    return {
        "start_date": START,
        "end_date": END,
        "schedule_a": tester.schedule_a,
        "schedule_b": tester.schedule_b,
        "schedule_c": tester.schedule_c,
        "schedule_c_meta": tester.schedule_c_meta,
        "transition_regime_by_day": tester.transition_regime_by_day,
        "index_dd_block_by_day": tester.index_dd_block_by_day,
        "cached_15m_data": tester.cached_15m_data,
    }


def _save_run(
    run_id: str,
    tester: DynamicBacktester,
    trades: Optional[pd.DataFrame],
) -> Dict[str, Any]:
    """실험별 거래·자산곡선과 요약을 저장합니다."""
    destination = OUT / run_id
    destination.mkdir(parents=True, exist_ok=True)
    frame = trades if isinstance(trades, pd.DataFrame) else pd.DataFrame()
    equity = pd.DataFrame(tester.daily_equity)
    frame.to_csv(destination / "trades.csv", index=False, encoding="utf-8-sig")
    equity.to_csv(destination / "equity.csv", index=False, encoding="utf-8-sig")
    summary = {
        "id": run_id,
        "period": f"{START} ~ {END}",
        **_equity_metrics(tester, frame),
        "tracks": _track_metrics(frame),
        "open_positions_at_end": int(len(tester.holdings)),
    }
    (destination / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def main() -> None:
    """A/B/C 통합 실행 후 동일 사전 계산 컨텍스트로 A/B 기준선을 실행합니다."""
    OUT.mkdir(parents=True, exist_ok=True)
    settings_abc = _base_settings()
    print(f"[ABC] {START} ~ {END} 통합 백테스트 시작")
    started = time.time()
    tester_abc = DynamicBacktester(deepcopy(settings_abc))
    trades_abc = tester_abc.run(
        START,
        END,
        interval="15m",
        callback=None,
        high_precision=False,
    )
    abc_elapsed = time.time() - started
    abc_summary = _save_run(
        "ABC",
        tester_abc,
        trades_abc if isinstance(trades_abc, pd.DataFrame) else None,
    )
    abc_summary["elapsed_sec"] = round(abc_elapsed, 1)

    print("[AB] 동일 스케줄·지표 컨텍스트 기준선 시작")
    settings_ab = deepcopy(settings_abc)
    settings_ab.update({"use_type_c": False, "use_sel_track_c": False})
    tester_ab = DynamicBacktester(settings_ab)
    tester_ab._precomputed_bt_context = _precomputed_context(tester_abc)
    tester_ab._suppress_backtest_artifacts = True
    tester_ab._quiet_backtest = True
    started = time.time()
    trades_ab = tester_ab.run(
        START,
        END,
        interval="15m",
        callback=None,
        high_precision=False,
    )
    ab_elapsed = time.time() - started
    ab_summary = _save_run(
        "AB_BASELINE",
        tester_ab,
        trades_ab if isinstance(trades_ab, pd.DataFrame) else None,
    )
    ab_summary["elapsed_sec"] = round(ab_elapsed, 1)

    comparison = {
        "ABC": abc_summary,
        "AB_BASELINE": ab_summary,
        "delta_abc_minus_ab": {
            "return_pct": round(
                abc_summary["return_pct"] - ab_summary["return_pct"], 3
            ),
            "max_dd_pct": round(
                abc_summary["max_dd_pct"] - ab_summary["max_dd_pct"], 3
            ),
            "profit_krw": round(
                abc_summary["profit_krw"] - ab_summary["profit_krw"], 0
            ),
            "n_buy1": int(abc_summary["n_buy1"] - ab_summary["n_buy1"]),
            "n_buy2": int(abc_summary["n_buy2"] - ab_summary["n_buy2"]),
        },
        "settings": {
            key: settings_abc.get(key)
            for key in (
                "max_slots_a",
                "max_slots_b",
                "max_slots_c",
                "use_s2_master_a",
                "use_s2_master_b",
                "use_transition_gate",
                "use_index_dd_gate",
            )
        },
    }
    (OUT / "comparison.json").write_text(
        json.dumps(comparison, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(comparison, ensure_ascii=False, indent=2))
    print(f"[완료] ABC {abc_elapsed:.1f}초 + AB {ab_elapsed:.1f}초")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
