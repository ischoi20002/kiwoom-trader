# -*- coding: utf-8 -*-
"""보유 평가 HOLD hold_reason / S2·EXIT explain 진단."""
from __future__ import annotations

from backtest_engine import TradingStrategy


def test_explain_s2_approval_failure_profit() -> None:
    settings = {
        "use_s2_master_b": True,
        "use_s2_b_delay": False,
        "use_s2_b_ma20": False,
        "use_s2_b_ma5": False,
        "use_s2_b_vol_surge": False,
        "use_s2_b_rsi_sig": False,
        "use_s2_b_macd_gc": False,
        "use_s2_b_profit": True,
        "s2_b_profit_limit": 1.0,
    }
    pos = {
        "sig_type": "TYPE_B",
        "entry_date": "2026-08-25 11:30:00",
        "avg_p": 45800.0,
        "avg_price": 45800.0,
    }
    ind = {"Close": 45800.0}  # 수익 0%
    reason = TradingStrategy.explain_s2_approval_failure(
        pos, None, settings, indicator_row=ind, curr_dt="2026-08-27 09:30:00"
    )
    assert reason is not None
    assert reason.startswith("s2_profit:"), reason


def test_explain_s2_approval_failure_pass() -> None:
    settings = {
        "use_s2_master_b": True,
        "use_s2_b_delay": False,
        "use_s2_b_ma20": False,
        "use_s2_b_ma5": False,
        "use_s2_b_vol_surge": False,
        "use_s2_b_rsi_sig": False,
        "use_s2_b_macd_gc": False,
        "use_s2_b_profit": True,
        "s2_b_profit_limit": 1.0,
    }
    pos = {
        "sig_type": "TYPE_B",
        "entry_date": "2026-08-25 11:30:00",
        "avg_p": 45800.0,
        "avg_price": 45800.0,
    }
    ind = {"Close": 48550.0}  # +6%
    reason = TradingStrategy.explain_s2_approval_failure(
        pos, None, settings, indicator_row=ind, curr_dt="2026-08-27 09:30:00"
    )
    assert reason is None


def test_explain_exit_hold_flash_suppressed() -> None:
    settings = {
        "use_atr_trail_only_after_tp_s1_b": True,
        "use_atr_trailing_stop_b": True,
        "use_atr_stop_loss_b": True,
        "use_s1_b_flash_exit": True,
        "s1_b_flash_exit_pct": 2.0,
        "s1_b_take_profit": 4.0,
        "use_s1_b_tp": True,
        "atr_multiplier_sl_b": 2.5,
        "atr_multiplier_trail_b": 4.0,
        "use_gap_down_exit_b": False,
    }
    pos = {
        "sig_type": "TYPE_B",
        "entry_n": 1,
        "stage": 1,
        "avg_p": 45800.0,
        "avg_price": 45800.0,
        "max_p": 48900.0,
        "max_price": 48900.0,
        "max_p_atr": 48900.0,
    }
    ind = {"Close": 47600.0, "ATR_B": 800.0, "ATR": 800.0}
    msg = TradingStrategy.explain_exit_hold(
        47600.0, pos, settings, indicator_row=ind
    )
    assert "flash:suppressed" in msg or "flash:" in msg, msg
    assert "exit:S1B" in msg, msg


if __name__ == "__main__":
    test_explain_s2_approval_failure_profit()
    test_explain_s2_approval_failure_pass()
    test_explain_exit_hold_flash_suppressed()
    print("ok")
