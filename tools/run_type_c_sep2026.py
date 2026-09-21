# -*- coding: utf-8 -*-
"""지정 구간의 TYPE_C 단독 동적 백테스트."""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from backtest_engine import settings_for_backtest  # noqa: E402
from core.defaults import DEFAULT_SETTINGS  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

START = os.environ.get("TYPE_C_BT_START", "2026-09-01")
END = os.environ.get("TYPE_C_BT_END", "2026-09-18")
OUT_NAME = os.environ.get("TYPE_C_BT_OUT", "bt_type_c_sep2026")
OUT = ROOT / "tools" / OUT_NAME


def _equity_metrics(
    equity: pd.DataFrame,
    initial_cash: float,
) -> Dict[str, Any]:
    """자산곡선의 기간 수익률과 최대 낙폭을 계산합니다."""
    if equity.empty:
        return {
            "initial_equity": initial_cash,
            "end_equity": initial_cash,
            "return_pct": 0.0,
            "max_dd_pct": 0.0,
        }
    date_column = "date" if "date" in equity.columns else equity.columns[0]
    value_column = (
        "equity"
        if "equity" in equity.columns
        else ("Total" if "Total" in equity.columns else equity.columns[-1])
    )
    normalized = equity.copy()
    normalized[date_column] = pd.to_datetime(
        normalized[date_column], format="mixed", errors="coerce"
    )
    normalized[value_column] = pd.to_numeric(
        normalized[value_column], errors="coerce"
    )
    normalized.dropna(subset=[date_column, value_column], inplace=True)
    normalized.sort_values(date_column, inplace=True)
    if normalized.empty:
        return _equity_metrics(pd.DataFrame(), initial_cash)

    end_equity = float(normalized.iloc[-1][value_column])
    drawdown = (
        normalized[value_column] / normalized[value_column].cummax() - 1.0
    )
    return {
        "initial_equity": round(float(initial_cash), 0),
        "end_equity": round(end_equity, 0),
        "profit_krw": round(end_equity - float(initial_cash), 0),
        "return_pct": round(
            (end_equity / float(initial_cash) - 1.0) * 100.0, 3
        ),
        "max_dd_pct": round(float(drawdown.min()) * 100.0, 3),
    }


def _trade_metrics(trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    """체결 수와 청산 성과·사유를 집계합니다."""
    if trades is None or trades.empty:
        return {
            "n_buy1": 0,
            "n_buy2": 0,
            "n_exits": 0,
            "win_rate_pct": 0.0,
            "avg_exit_return_pct": 0.0,
            "exit_reasons": {},
        }
    frame = trades.copy()
    exits = frame[frame["Action"].astype(str).isin(["SELL", "HOLD"])].copy()
    exit_returns = pd.to_numeric(exits.get("Return"), errors="coerce").dropna()
    reasons = (
        exits.get("Reason", pd.Series(dtype=str))
        .astype(str)
        .str.split("|", regex=False)
        .str[0]
        .value_counts()
        .to_dict()
    )
    return {
        "n_buy1": int((frame["Action"] == "BUY1").sum()),
        "n_buy2": int((frame["Action"] == "BUY2").sum()),
        "n_exits": int(len(exits)),
        "win_rate_pct": round(
            float((exit_returns > 0).mean() * 100.0) if len(exit_returns) else 0.0,
            2,
        ),
        "avg_exit_return_pct": round(
            float(exit_returns.mean()) if len(exit_returns) else 0.0,
            3,
        ),
        "median_exit_return_pct": round(
            float(exit_returns.median()) if len(exit_returns) else 0.0,
            3,
        ),
        "exit_reasons": {str(key): int(value) for key, value in reasons.items()},
    }


def _existing_baseline_sep_metrics() -> Dict[str, Any]:
    """직전 전체 백테스트 산출물에서 같은 9월 구간을 추출합니다."""
    equity_path = ROOT / "backtest_portfolio_curve.csv"
    trades_path = ROOT / "backtest_trades.csv"
    if not equity_path.is_file():
        return {}
    try:
        equity = pd.read_csv(equity_path)
        date_column = equity.columns[0]
        equity[date_column] = pd.to_datetime(
            equity[date_column], format="mixed", errors="coerce"
        )
        period = equity[
            (equity[date_column] >= pd.Timestamp(START))
            & (equity[date_column] <= pd.Timestamp(END + " 23:59:59"))
        ].copy()
        if period.empty:
            return {}
        value_column = "Total" if "Total" in period.columns else period.columns[-1]
        start_equity = float(period.iloc[0][value_column])
        metrics = _equity_metrics(period, start_equity)
        if trades_path.is_file():
            trades = pd.read_csv(trades_path)
            trades["Date"] = pd.to_datetime(
                trades["Date"], format="mixed", errors="coerce"
            )
            period_trades = trades[
                (trades["Date"] >= pd.Timestamp(START))
                & (trades["Date"] <= pd.Timestamp(END + " 23:59:59"))
            ]
            metrics.update(_trade_metrics(period_trades))
        return metrics
    except (OSError, ValueError, KeyError, IndexError):
        traceback.print_exc()
        return {}


def main() -> None:
    """TYPE_C 단독 백테스트를 실행하고 재현 가능한 결과를 저장합니다."""
    OUT.mkdir(parents=True, exist_ok=True)
    is_sep2026_run = START == "2026-09-01" and END == "2026-09-18"
    baseline = _existing_baseline_sep_metrics() if is_sep2026_run else {}
    previous_result = OUT / "result.json"
    if is_sep2026_run and previous_result.is_file():
        try:
            previous = json.loads(previous_result.read_text(encoding="utf-8"))
            saved_baseline = previous.get("existing_baseline_sep")
            if isinstance(saved_baseline, dict) and saved_baseline.get("n_buy1", 0) > 0:
                baseline = saved_baseline
        except (OSError, ValueError, TypeError):
            pass

    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    merged_settings = dict(DEFAULT_SETTINGS)
    merged_settings.update(raw)
    settings = settings_for_backtest(merged_settings)
    settings.update(
        {
            "bt_high_precision": False,
            "use_type_a": False,
            "use_type_b": False,
            "use_type_c": True,
            "use_sel_track_a": False,
            "use_sel_track_b": False,
            "use_sel_track_c": True,
            "max_slots_a": 0,
            "max_slots_b": 0,
            "max_slots_c": 15,
            "use_s2_master_a": False,
            "use_s2_master_b": False,
            "enforce_track_signal_match": True,
        }
    )

    print(f"[TYPE_C_ONLY] {START} ~ {END} 실행 시작")
    started = time.time()
    tester = DynamicBacktester(settings)
    trades = tester.run(
        START,
        END,
        interval="15m",
        callback=None,
        high_precision=False,
    )
    elapsed = time.time() - started

    trades_frame = trades if isinstance(trades, pd.DataFrame) else pd.DataFrame()
    equity_frame = pd.DataFrame(tester.daily_equity)
    trades_frame.to_csv(OUT / "trades.csv", index=False, encoding="utf-8-sig")
    equity_frame.to_csv(OUT / "equity.csv", index=False, encoding="utf-8-sig")

    metrics = {
        "id": "TYPE_C_ONLY",
        "period": f"{START} ~ {END}",
        "elapsed_sec": round(elapsed, 1),
        **_equity_metrics(equity_frame, float(tester.initial_cash)),
        **_trade_metrics(trades_frame),
        "open_positions_at_end": int(len(tester.holdings)),
        "settings": {
            key: settings.get(key)
            for key in (
                "max_slots_c",
                "box_sel_lookback",
                "box_sel_min_box_width_pct",
                "box_sel_max_box_width_pct",
                "box_sel_max_adx",
                "type_c_touch_lookback_bars",
                "type_c_target_atr_buffer",
                "type_c_stop_atr_tolerance",
                "type_c_breakdown_confirm_bars",
                "type_c_max_holding_days",
            )
        },
    }
    result = {"type_c_only": metrics, "existing_baseline_sep": baseline}
    (OUT / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"[완료] {elapsed:.1f}초 → {OUT}")


if __name__ == "__main__":
    main()
