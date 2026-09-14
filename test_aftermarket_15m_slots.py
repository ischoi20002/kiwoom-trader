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


def test_aftermarket_last_slot_helper() -> None:
    from utils.krx_bars import (
        is_krx_aftermarket_last_15m_slot,
        is_krx_next_open_defer_15m_slot,
        krx_15m_slot_complete_ts,
    )

    assert is_krx_aftermarket_last_15m_slot("2026-09-14 19:45:00")
    assert not is_krx_aftermarket_last_15m_slot("2026-09-14 15:30:00")
    assert not is_krx_aftermarket_last_15m_slot("2026-09-14 19:30:00")
    assert is_krx_next_open_defer_15m_slot("2026-09-14 19:45:00")
    assert krx_15m_slot_complete_ts("2026-09-14 19:45:00") == pd.Timestamp(
        "2026-09-14 20:00:00"
    )


def test_1945_wall_grace_and_next_open_route() -> None:
    """20:00~20:10 에 19:45 평가 허용, S2/EXIT 는 next_open queued."""
    from unittest.mock import patch

    from core.signal_engine import SignalEngine

    queued: list[str] = []

    class _Nop:
        def queue_buy_signal(self, **kwargs):  # type: ignore[no-untyped-def]
            queued.append("buy")
            return True

        def queue_strategy_action(self, **kwargs):  # type: ignore[no-untyped-def]
            queued.append(str(kwargs.get("action") or ""))
            return True

    app = SimpleNamespace(
        settings={"next_open_1945_enabled": True, "order_1530_enabled": False},
        next_open_pending_action_service=_Nop(),
        order_execution_service=None,
        log=lambda *_: None,
    )
    se = SignalEngine(app)
    slot = datetime(2026, 9, 14, 19, 45, 0)
    assert se._is_1945_bar_slot(slot) is True

    with patch(
        "core.signal_engine.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = datetime(2026, 9, 14, 20, 0, 5)
        mock_dt.side_effect = lambda *a, **k: datetime(*a, **k)
        with patch(
            "core.signal_engine.is_krx_tradable_session", return_value=False
        ):
            assert se._allow_closed_15m_eval_wall_now(slot) is True

    assert (
        se._route_1945_strategy_execution(
            "005930", "EXIT", reason="TP", limit_price=70000.0, bar_ts=slot
        )
        == "queued"
    )
    assert "EXIT" in queued


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
    from reporting.krx_trading_calendar import (
        completed_trading_day_asof_local,
        monitor_eod_due_datetime,
    )

    app = SimpleNamespace(settings={"continuous_monitor_eod_buffer_minutes": 30})
    cms = ContinuousMonitorService(app)
    due = cms._eod_due_datetime(date(2026, 9, 14))
    assert due == datetime(2026, 9, 14, 20, 30, 0)
    assert monitor_eod_due_datetime(date(2026, 9, 14), eod_buffer_minutes=30) == due
    # 애프터 이전일(시행 전)
    due2 = cms._eod_due_datetime(date(2026, 9, 11))
    assert due2 == datetime(2026, 9, 11, 16, 0, 0)

    # 레짐 as_of: 20:30 전에는 전일, 이후면 당일
    assert completed_trading_day_asof_local(
        datetime(2026, 9, 14, 16, 0), eod_buffer_minutes=30
    ) == date(2026, 9, 11)  # 금요일이면 직전 거래일 — 9/14 월이면 9/11
    assert completed_trading_day_asof_local(
        datetime(2026, 9, 14, 20, 30), eod_buffer_minutes=30
    ) == date(2026, 9, 14)


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
    test_aftermarket_last_slot_helper()
    test_1945_wall_grace_and_next_open_route()
    test_tradable_session_covers_aftermarket()
    test_cms_eod_due_uses_aftermarket_end()
    test_aftermarket_forces_limit_order()
    print("ok")
