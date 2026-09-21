# -*- coding: utf-8 -*-
"""2026 Track C: 현재 설정 vs 1순위 신호 완화 비교."""

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

START = "2026-01-01"
END = "2026-09-18"
OUT = ROOT / "tools" / "bt_type_c_2026_signal_loosen"


def _equity_metrics(equity: pd.DataFrame, initial_cash: float) -> Dict[str, Any]:
    if equity is None or equity.empty:
        return {
            "initial_equity": initial_cash,
            "end_equity": initial_cash,
            "profit_krw": 0.0,
            "return_pct": 0.0,
            "max_dd_pct": 0.0,
        }
    date_column = "date" if "date" in equity.columns else equity.columns[0]
    value_column = (
        "equity"
        if "equity" in equity.columns
        else ("Total" if "Total" in equity.columns else equity.columns[-1])
    )
    frame = equity.copy()
    frame[date_column] = pd.to_datetime(
        frame[date_column], format="mixed", errors="coerce"
    )
    frame[value_column] = pd.to_numeric(frame[value_column], errors="coerce")
    frame.dropna(subset=[date_column, value_column], inplace=True)
    frame.sort_values(date_column, inplace=True)
    if frame.empty:
        return _equity_metrics(pd.DataFrame(), initial_cash)
    end_equity = float(frame.iloc[-1][value_column])
    drawdown = frame[value_column] / frame[value_column].cummax() - 1.0
    return {
        "initial_equity": round(float(initial_cash), 0),
        "end_equity": round(end_equity, 0),
        "profit_krw": round(end_equity - float(initial_cash), 0),
        "return_pct": round((end_equity / float(initial_cash) - 1.0) * 100.0, 3),
        "max_dd_pct": round(float(drawdown.min()) * 100.0, 3),
    }


def _trade_metrics(trades: Optional[pd.DataFrame]) -> Dict[str, Any]:
    if trades is None or trades.empty:
        return {
            "n_buy1": 0,
            "n_buy2": 0,
            "n_exits": 0,
            "win_rate_pct": 0.0,
            "avg_exit_return_pct": 0.0,
            "median_exit_return_pct": 0.0,
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
            float(exit_returns.mean()) if len(exit_returns) else 0.0, 3
        ),
        "median_exit_return_pct": round(
            float(exit_returns.median()) if len(exit_returns) else 0.0, 3
        ),
        "exit_reasons": {str(k): int(v) for k, v in reasons.items()},
    }


def _base_settings() -> Dict[str, Any]:
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    merged = dict(DEFAULT_SETTINGS)
    merged.update(raw)
    settings = settings_for_backtest(merged)
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
            "max_slots_c": int(settings.get("max_slots_c", 15) or 15),
            "use_s2_master_a": False,
            "use_s2_master_b": False,
            "enforce_track_signal_match": True,
        }
    )
    return settings


# 1순위 신호 완화 패치
LOOSEN_SIGNAL = {
    "type_c_max_box_position_pct": 55.0,
    "type_c_rsi_min": 30.0,
    "type_c_rsi_max": 62.0,
    "type_c_touch_atr_tolerance": 0.6,
    "type_c_touch_lookback_bars": 5,
    "type_c_min_volume_ratio": 0.5,
    "type_c_min_candle_confirmations": 0,
}

SIGNAL_KEYS = list(LOOSEN_SIGNAL.keys())


def _extract_context(tester: DynamicBacktester) -> Dict[str, Any]:
    return {
        "start_date": START,
        "end_date": END,
        "schedule_a": getattr(tester, "schedule_a", {}) or {},
        "schedule_b": getattr(tester, "schedule_b", {}) or {},
        "schedule_c": getattr(tester, "schedule_c", {}) or {},
        "schedule_c_meta": getattr(tester, "schedule_c_meta", {}) or {},
        "transition_regime_by_day": getattr(tester, "transition_regime_by_day", {})
        or {},
        "index_dd_block_by_day": getattr(tester, "index_dd_block_by_day", {}) or {},
        "cached_15m_data": getattr(tester, "cached_15m_data", {}) or {},
    }


def _run_one(
    label: str,
    settings: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    print(f"\n[{label}] {START} ~ {END} 시작")
    started = time.time()
    tester = DynamicBacktester(settings)
    tester._quiet_backtest = True
    tester._suppress_backtest_artifacts = True
    tester._type_c_signal_cache = {}
    if context is not None:
        tester._precomputed_bt_context = context
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
    run_dir = OUT / label
    run_dir.mkdir(parents=True, exist_ok=True)
    trades_frame.to_csv(run_dir / "trades.csv", index=False, encoding="utf-8-sig")
    equity_frame.to_csv(run_dir / "equity.csv", index=False, encoding="utf-8-sig")
    metrics = {
        "id": label,
        "period": f"{START} ~ {END}",
        "elapsed_sec": round(elapsed, 1),
        **_equity_metrics(equity_frame, float(tester.initial_cash)),
        **_trade_metrics(trades_frame),
        "open_positions_at_end": int(len(tester.holdings)),
        "signal_settings": {k: settings.get(k) for k in SIGNAL_KEYS},
        "selection_settings": {
            "box_sel_lookback": settings.get("box_sel_lookback"),
            "box_sel_ma_period": settings.get("box_sel_ma_period"),
            "box_sel_max_adx": settings.get("box_sel_max_adx"),
        },
    }
    (run_dir / "summary.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"[{label}] buy1={metrics['n_buy1']} ret={metrics['return_pct']}% "
        f"mdd={metrics['max_dd_pct']}% wr={metrics['win_rate_pct']}% "
        f"({elapsed:.1f}s)"
    )
    return {"metrics": metrics, "context": _extract_context(tester)}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        base = _base_settings()
        current = dict(base)
        loosened = dict(base)
        loosened.update(LOOSEN_SIGNAL)

        current_run = _run_one("CURRENT", current, context=None)
        context = current_run["context"]
        loose_run = _run_one("LOOSEN_SIGNAL", loosened, context=context)

        cur = current_run["metrics"]
        loose = loose_run["metrics"]
        comparison = {
            "period": f"{START} ~ {END}",
            "current": cur,
            "loosen_signal": loose,
            "delta": {
                "n_buy1": int(loose["n_buy1"] - cur["n_buy1"]),
                "return_pct": round(
                    float(loose["return_pct"]) - float(cur["return_pct"]), 3
                ),
                "max_dd_pct": round(
                    float(loose["max_dd_pct"]) - float(cur["max_dd_pct"]), 3
                ),
                "win_rate_pct": round(
                    float(loose["win_rate_pct"]) - float(cur["win_rate_pct"]), 2
                ),
                "avg_exit_return_pct": round(
                    float(loose["avg_exit_return_pct"])
                    - float(cur["avg_exit_return_pct"]),
                    3,
                ),
            },
            "patched_keys": LOOSEN_SIGNAL,
        }
        (OUT / "comparison.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print("\n=== 비교 요약 ===")
        print(json.dumps(comparison["delta"], ensure_ascii=False, indent=2))
        print(f"[완료] {OUT}")
    except Exception:
        traceback.print_exc()
        raise


if __name__ == "__main__":
    main()
