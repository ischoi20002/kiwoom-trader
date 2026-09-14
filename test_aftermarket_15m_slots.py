# -*- coding: utf-8 -*-
"""KRX 애프터마켓 15분 슬롯·tradable·주문 강제 지정가."""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd

from utils.krx_bars import (
    is_krx_15m_slot_end,
    is_krx_m1_closed_open_label,
    is_krx_no_signal_eval_15m_slot,
    is_last_m1_open_of_15m_slot,
    is_slot_head_complete_m1_open,
    iter_krx_15m_slot_ends_for_day,
    krx_15m_label_for_m1_open,
    krx_stream_slot_end_datetime,
    next_krx_15m_slot_end,
    prev_krx_15m_slot_end,
)
from utils.krx_session import (
    is_krx_aftermarket_session,
    is_krx_regular_session,
    is_krx_tradable_session,
)
from utils.krx_session_hours import iter_aftermarket_15m_slot_labels


def test_aftermarket_slot_count_and_range() -> None:
    day = date(2026, 9, 14)
    ah = iter_aftermarket_15m_slot_labels(day)
    assert len(ah) == 16
    assert ah[0] == datetime(2026, 9, 14, 16, 0)
    assert ah[-1] == datetime(2026, 9, 14, 19, 45)

    all_slots = iter_krx_15m_slot_ends_for_day(day)
    assert len(all_slots) == 27 + 16
    assert all_slots[-1] == pd.Timestamp("2026-09-14 19:45:00")
    assert is_krx_15m_slot_end("2026-09-14 17:00:00")
    assert not is_krx_15m_slot_end("2026-09-14 15:45:00")  # 시간외종가 공백


def test_next_prev_jumps_around_aftermarket() -> None:
    assert next_krx_15m_slot_end("2026-09-14 15:30:00") == pd.Timestamp(
        "2026-09-14 16:00:00"
    )
    assert next_krx_15m_slot_end("2026-09-14 19:45:00") == pd.Timestamp(
        "2026-09-15 09:00:00"
    )
    assert prev_krx_15m_slot_end("2026-09-14 16:00:00") == pd.Timestamp(
        "2026-09-14 15:30:00"
    )
    assert prev_krx_15m_slot_end("2026-09-14 16:15:00") == pd.Timestamp(
        "2026-09-14 16:00:00"
    )


def test_aftermarket_head_and_last_m1() -> None:
    assert is_slot_head_complete_m1_open("2026-09-14 16:13:00")
    assert is_last_m1_open_of_15m_slot("2026-09-14 16:14:00")
    assert is_slot_head_complete_m1_open("2026-09-14 19:58:00")
    assert is_last_m1_open_of_15m_slot("2026-09-14 19:59:00")
    assert is_krx_m1_closed_open_label("2026-09-14 17:07:00")
    assert krx_15m_label_for_m1_open("2026-09-14 17:07:00") == pd.Timestamp(
        "2026-09-14 17:00:00"
    )
    assert krx_stream_slot_end_datetime(datetime(2026, 9, 14, 17, 7)) == pd.Timestamp(
        "2026-09-14 17:00:00"
    )


def test_1530_still_no_signal_eval_but_1945_is_eval() -> None:
    assert is_krx_no_signal_eval_15m_slot("2026-09-14 15:30:00")
    assert not is_krx_no_signal_eval_15m_slot("2026-09-14 19:45:00")


def test_tradable_session_covers_aftermarket() -> None:
    assert is_krx_regular_session(datetime(2026, 9, 14, 14, 0))
    assert not is_krx_aftermarket_session(datetime(2026, 9, 14, 14, 0))
    assert is_krx_tradable_session(datetime(2026, 9, 14, 14, 0))

    assert is_krx_aftermarket_session(datetime(2026, 9, 14, 17, 0))
    assert is_krx_tradable_session(datetime(2026, 9, 14, 17, 0))
    assert not is_krx_regular_session(datetime(2026, 9, 14, 17, 0))

    # 시간외 종가 공백
    assert not is_krx_tradable_session(datetime(2026, 9, 14, 15, 50))
    assert not is_krx_tradable_session(datetime(2026, 9, 14, 20, 0))


def test_cms_eod_due_uses_aftermarket_end() -> None:
    from core.continuous_monitor_service import ContinuousMonitorService

    app = SimpleNamespace(settings={"continuous_monitor_eod_buffer_minutes": 30})
    cms = ContinuousMonitorService(app)
    due = cms._eod_due_datetime(date(2026, 9, 14))
    assert due == datetime(2026, 9, 14, 20, 30, 0)
    # 애프터 이전일(시행 전)
    due2 = cms._eod_due_datetime(date(2026, 9, 11))
    assert due2 == datetime(2026, 9, 11, 16, 0, 0)


def test_aftermarket_forces_limit_order() -> None:
    from core.execution.order_execution_service import OrderExecutionService

    svc = OrderExecutionService(SimpleNamespace(), log_func=lambda *_a, **_k: None)
    # 강제 패치: 애프터로 간주
    svc._is_aftermarket_now = lambda: True  # type: ignore[method-assign]
    ot, style, auction = svc._coerce_aftermarket_limit("03", "market", auction=True)
    assert ot == "00"
    assert style == "limit"
    assert auction is False


if __name__ == "__main__":
    test_aftermarket_slot_count_and_range()
    test_next_prev_jumps_around_aftermarket()
    test_aftermarket_head_and_last_m1()
    test_1530_still_no_signal_eval_but_1945_is_eval()
    test_tradable_session_covers_aftermarket()
    test_cms_eod_due_uses_aftermarket_end()
    test_aftermarket_forces_limit_order()
    print("ok")
