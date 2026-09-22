"""Track C 하단 반등 신호 테스트."""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtest_engine import TradingStrategy
from core.bt_hp_signal_bridge import sel_track_to_bt_code_track
from strategy.signals import get_buy_signals_detailed


def _make_type_c_frame() -> pd.DataFrame:
    """마지막 봉에서 접촉 후 재진입·고가 돌파가 완성되는 지표 프레임입니다."""
    index = pd.date_range("2026-09-21 09:00", periods=30, freq="15min")
    close = np.full(30, 103.0)
    frame = pd.DataFrame(
        {
            "Open": close,
            "High": close + 0.4,
            "Low": close - 0.4,
            "Close": close,
            "Volume": np.full(30, 1_200.0),
            "MA5": np.full(30, 103.0),
            "MA20": np.full(30, 103.0),
            "MA60": np.full(30, 103.0),
            "RSI": np.full(30, 45.0),
            "RSI_Sig": np.full(30, 44.0),
            "MACD": np.zeros(30),
            "MACD_Sig": np.zeros(30),
            "OSC": np.zeros(30),
            "Prev_Day_Vol": np.full(30, 1_000.0),
            "Prev_Day_Vol_B": np.full(30, 1_000.0),
            "Prev_Day_Vol_S1_A": np.full(30, 1_000.0),
            "Prev_Day_Vol_S1_B": np.full(30, 1_000.0),
            "Vol_Ratio": np.full(30, 1.2),
            "ATR": np.ones(30),
            "ATR_C": np.ones(30),
        },
        index=index,
    )
    # 직전 봉에서 박스 하단 접촉 및 과매도
    frame.iloc[-2, frame.columns.get_loc("Open")] = 101.2
    frame.iloc[-2, frame.columns.get_loc("High")] = 101.5
    frame.iloc[-2, frame.columns.get_loc("Low")] = 100.1
    frame.iloc[-2, frame.columns.get_loc("Close")] = 100.8
    frame.iloc[-2, frame.columns.get_loc("RSI")] = 34.0
    # 현재 봉에서 박스 안 재진입, 직전 고가 돌파, 강한 종가
    frame.iloc[-1, frame.columns.get_loc("Open")] = 101.0
    frame.iloc[-1, frame.columns.get_loc("High")] = 102.2
    frame.iloc[-1, frame.columns.get_loc("Low")] = 100.5
    frame.iloc[-1, frame.columns.get_loc("Close")] = 102.0
    frame.iloc[-1, frame.columns.get_loc("RSI")] = 40.0
    return frame


def _settings() -> dict:
    return {
        "use_type_a": False,
        "use_type_b": False,
        "use_type_c": True,
        "use_macd_signal": False,
        "use_bb_block_a_2s": False,
        "use_bb_block_all_3s": False,
        "type_c_touch_lookback_bars": 3,
        "type_c_min_candle_confirmations": 1,
    }


def test_type_c_rebound_signal_requires_box_context() -> None:
    """박스 경계가 있는 C 트랙에서만 하단 반등을 TYPE_C로 확정합니다."""
    frame = _make_type_c_frame()
    context = {"sel_track": "C", "box_lower": 100.0, "box_upper": 110.0}

    detailed = get_buy_signals_detailed(
        frame, _settings(), signal_context=context
    )
    no_context = get_buy_signals_detailed(frame, _settings())

    assert bool(detailed.iloc[-1]["Signal"])
    assert detailed.iloc[-1]["Sig_Type"] == "TYPE_C"
    assert bool(detailed.iloc[-1]["C_Touch"])
    assert bool(detailed.iloc[-1]["C_Reclaim"])
    assert not bool(no_context.iloc[-1]["Signal"])


def test_type_c_signal_does_not_use_future_bar() -> None:
    """미래 봉을 붙여도 이미 확정된 C 신호는 달라지지 않아야 합니다."""
    frame = _make_type_c_frame()
    context = {"sel_track": "C", "box_lower": 100.0, "box_upper": 110.0}
    before = get_buy_signals_detailed(
        frame, _settings(), signal_context=context
    ).iloc[-1]

    future = frame.iloc[[-1]].copy()
    future.index = pd.DatetimeIndex([frame.index[-1] + pd.Timedelta(minutes=15)])
    future.loc[:, ["Open", "High", "Low", "Close"]] = [95.0, 96.0, 94.0, 94.5]
    extended = pd.concat([frame, future])
    after = get_buy_signals_detailed(
        extended, _settings(), signal_context=context
    ).loc[frame.index[-1]]

    assert bool(before["Signal"]) == bool(after["Signal"])
    assert before["Sig_Type"] == after["Sig_Type"] == "TYPE_C"


def test_type_c_prev_high_break_can_be_disabled() -> None:
    """전고 돌파 토글 OFF면 직전 고가를 넘지 않아도 TYPE_C 신호가 납니다."""
    frame = _make_type_c_frame()
    # 종가가 직전 고가(101.5)를 넘지 않도록 조정
    frame.iloc[-1, frame.columns.get_loc("High")] = 101.4
    frame.iloc[-1, frame.columns.get_loc("Close")] = 101.3
    context = {"sel_track": "C", "box_lower": 100.0, "box_upper": 110.0}

    required = _settings()
    required["use_type_c_prev_high_break"] = True
    with_req = get_buy_signals_detailed(
        frame, required, signal_context=context
    )
    assert not bool(with_req.iloc[-1]["Signal"])
    assert not bool(with_req.iloc[-1]["C_PrevHighBreak"])

    optional = _settings()
    optional["use_type_c_prev_high_break"] = False
    without_req = get_buy_signals_detailed(
        frame, optional, signal_context=context
    )
    assert bool(without_req.iloc[-1]["Signal"])
    assert without_req.iloc[-1]["Sig_Type"] == "TYPE_C"
    assert not bool(without_req.iloc[-1]["C_PrevHighBreak"])


def test_type_c_s2_is_always_blocked() -> None:
    """TYPE_C 보유에는 기존 A/B 수익권 추매를 적용하지 않습니다."""
    position = {
        "sig_type": "TYPE_C",
        "entry_date": "2026-09-20 09:00:00",
        "avg_price": 101.0,
    }
    row = pd.Series(
        {"Close": 103.0},
        name=pd.Timestamp("2026-09-21 10:00:00"),
    )

    assert not TradingStrategy._check_s2_approval(position, row, {})
    assert (
        TradingStrategy.explain_s2_approval_failure(position, row, {})
        == "s2_type_c_disabled"
    )


def test_type_c_is_not_counted_as_type_a_and_track_is_preserved() -> None:
    """C 보유가 A/B 균형 계산이나 고정밀 트랙에서 A로 오인되면 안 됩니다."""
    holdings = {
        "005930": {"sig_type": "TYPE_A"},
        "000660": {"sig_type": "TYPE_B"},
        "035420": {"sig_type": "TYPE_C"},
    }

    assert TradingStrategy._count_holdings_ab(holdings) == (1, 1)
    assert sel_track_to_bt_code_track("AC") == "AC"
    assert sel_track_to_bt_code_track("C") == "C"


def _type_c_position() -> dict:
    return {
        "sig_type": "TYPE_C",
        "avg_price": 102.0,
        "entry_time": "2026-09-21 10:00:00",
        "box_lower": 100.0,
        "box_upper": 110.0,
        "box_atr_entry": 1.0,
        "box_breakdown_closes": 0,
        "box_last_breakdown_bar": "",
    }


def test_type_c_take_profit_and_hard_stop() -> None:
    """상단 ATR 버퍼 목표와 하단 ATR 이탈 손절을 사용해야 합니다."""
    take_profit = TradingStrategy.evaluate_position(
        "005930",
        109.8,
        _type_c_position(),
        {},
        indicator_row=pd.Series({"Low": 109.0, "Close": 109.8}),
        current_time=pd.Timestamp("2026-09-22 10:00:00"),
    )
    stop = TradingStrategy.evaluate_position(
        "005930",
        99.4,
        _type_c_position(),
        {},
        indicator_row=pd.Series({"Low": 99.4, "Close": 99.4}),
        current_time=pd.Timestamp("2026-09-22 10:00:00"),
    )

    assert take_profit["reason"] == "BOX_C_TAKE_PROFIT"
    assert stop["reason"] == "BOX_C_STOP"


def test_type_c_two_confirmed_breakdown_bars_exit_once_per_bar() -> None:
    """같은 봉의 반복 평가로 카운트가 증가하지 않고 두 번째 확정 봉에서 청산합니다."""
    position = _type_c_position()
    row1 = pd.Series(
        {"Low": 99.7, "Close": 99.8},
        name=pd.Timestamp("2026-09-22 10:00:00"),
    )
    first = TradingStrategy.evaluate_position(
        "005930",
        99.8,
        position,
        {},
        indicator_row=row1,
        current_time=row1.name,
        time_is_bar_label=True,
    )
    repeated = TradingStrategy.evaluate_position(
        "005930",
        99.8,
        position,
        {},
        indicator_row=row1,
        current_time=row1.name,
        time_is_bar_label=True,
    )
    row2 = pd.Series(
        {"Low": 99.7, "Close": 99.8},
        name=pd.Timestamp("2026-09-22 10:15:00"),
    )
    second = TradingStrategy.evaluate_position(
        "005930",
        99.8,
        position,
        {},
        indicator_row=row2,
        current_time=row2.name,
        time_is_bar_label=True,
    )

    assert first["action"] == "HOLD"
    assert repeated["action"] == "HOLD"
    assert position["box_breakdown_closes"] == 2
    assert second["reason"] == "BOX_C_BREAKDOWN"


def test_type_c_time_exit_on_fifth_holding_day() -> None:
    """목표와 손절에 닿지 않아도 5번째 보유 평일에는 시간 청산합니다."""
    result = TradingStrategy.evaluate_position(
        "005930",
        105.0,
        _type_c_position(),
        {},
        indicator_row=pd.Series({"Low": 104.5, "Close": 105.0}),
        current_time=pd.Timestamp("2026-09-25 14:00:00"),
    )

    assert result["reason"] == "BOX_C_TIME_EXIT"


def test_type_c_gap_grace_holds_gap_down_at_open() -> None:
    """개장 유예 중 갭하락은 HOLD, 유예 밖·OFF면 EXIT."""
    gap_settings = {
        "use_type_c_gap_down_exit": True,
        "type_c_gap_down_limit": -4.0,
        "use_type_c_gap_grace": True,
        "type_c_gap_grace_mins": 45,
    }
    held = TradingStrategy.evaluate_position(
        "005930",
        97.0,
        _type_c_position(),
        gap_settings,
        indicator_row=pd.Series({"Low": 97.0, "Close": 97.0}),
        current_time=pd.Timestamp("2026-09-22 09:15:00"),
    )
    exited = TradingStrategy.evaluate_position(
        "005930",
        97.0,
        _type_c_position(),
        gap_settings,
        indicator_row=pd.Series({"Low": 97.0, "Close": 97.0}),
        current_time=pd.Timestamp("2026-09-22 10:00:00"),
    )
    no_grace = TradingStrategy.evaluate_position(
        "005930",
        97.0,
        _type_c_position(),
        {**gap_settings, "use_type_c_gap_grace": False},
        indicator_row=pd.Series({"Low": 97.0, "Close": 97.0}),
        current_time=pd.Timestamp("2026-09-22 09:15:00"),
    )
    take_profit = TradingStrategy.evaluate_position(
        "005930",
        109.8,
        _type_c_position(),
        gap_settings,
        indicator_row=pd.Series({"Low": 109.0, "Close": 109.8}),
        current_time=pd.Timestamp("2026-09-22 09:15:00"),
    )

    assert held["action"] == "HOLD"
    assert held["reason"] == "GRACE(BOX_C_GAP_DOWN)"
    assert exited["reason"] == "BOX_C_GAP_DOWN"
    assert no_grace["reason"] == "BOX_C_GAP_DOWN"
    assert take_profit["reason"] == "BOX_C_TAKE_PROFIT"
