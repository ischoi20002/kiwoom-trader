# -*- coding: utf-8 -*-
"""같은 15분 슬롯 S1은 슬롯 시작 보유로 타입을 확정하는지."""
from __future__ import annotations

from backtest_engine import TradingStrategy
from dynamic_backtest import snapshot_holdings_for_same_slot_s1


def _settings() -> dict:
    return {
        "use_type_a": True,
        "use_type_b": True,
        "same_bar_sig_priority": "prefer_a",
        "same_bar_sig_prefer_sel_track": False,
        "same_bar_sig_allow_track_b": False,
        "s1_simul_balance_tie_mode": "always",
        "enforce_track_signal_match": True,
    }


def _simul_row() -> dict:
    return {
        "Sig_Type": "TYPE_A",
        "S1_Simultaneous_AB": True,
        "Signal": True,
        "Ready": True,
    }


def test_snapshot_does_not_see_later_fill() -> None:
    live = {"005830": {"sig_type": "TYPE_B"}}
    snap = snapshot_holdings_for_same_slot_s1(live)
    live["006400"] = {"sig_type": "TYPE_A"}
    assert "006400" not in snap
    assert "005830" in snap


def test_unequal_holdings_keeps_type_a_on_simul() -> None:
    """A0:B1 이면 동시신호는 prefer_a 로 TYPE_A. 트랙 B는 매수 불가."""
    st = _settings()
    row = _simul_row()
    holdings = {"005830": {"sig_type": "TYPE_B"}}
    eff = TradingStrategy.effective_s1_sig_type_for_buy(st, row, holdings, "B")
    assert eff == "TYPE_A"
    assert not TradingStrategy.track_signal_match_allows(st, eff, "B")


def test_equal_holdings_flips_to_type_b() -> None:
    """같은 슬롯에서 트랙 A를 먼저 반영하면 1:1 → TYPE_B 가 되던 기존 동작."""
    st = _settings()
    row = _simul_row()
    holdings = {
        "005830": {"sig_type": "TYPE_B"},
        "006400": {"sig_type": "TYPE_A"},
    }
    eff = TradingStrategy.effective_s1_sig_type_for_buy(st, row, holdings, "B")
    assert eff == "TYPE_B"
    assert TradingStrategy.track_signal_match_allows(st, eff, "B")


def test_slot_snapshot_prevents_same_bar_tie_flip() -> None:
    """슬롯 시작 스냅샷(0:1)을 쓰면 같은 봉 트랙 A 체결 후에도 TYPE_A 유지."""
    st = _settings()
    row = _simul_row()
    live = {"005830": {"sig_type": "TYPE_B"}}
    snap = snapshot_holdings_for_same_slot_s1(live)
    live["006400"] = {"sig_type": "TYPE_A"}
    eff_live = TradingStrategy.effective_s1_sig_type_for_buy(st, row, live, "B")
    eff_snap = TradingStrategy.effective_s1_sig_type_for_buy(st, row, snap, "B")
    assert eff_live == "TYPE_B"
    assert eff_snap == "TYPE_A"
    assert not TradingStrategy.track_signal_match_allows(st, eff_snap, "B")


if __name__ == "__main__":
    test_snapshot_does_not_see_later_fill()
    test_unequal_holdings_keeps_type_a_on_simul()
    test_equal_holdings_flips_to_type_b()
    test_slot_snapshot_prevents_same_bar_tie_flip()
    print("ok")
