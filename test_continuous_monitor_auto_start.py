# -*- coding: utf-8 -*-
"""장전 자동 감시 시작 시각(개장 N분 전) 단위 테스트."""
from __future__ import annotations

from datetime import date, datetime, time
from types import SimpleNamespace

from core.continuous_monitor_service import ContinuousMonitorService
from utils import krx_session_hours as sh


def _svc(minutes: int = 25) -> ContinuousMonitorService:
    app = SimpleNamespace(
        settings={
            "realtime_monitor_auto_start_minutes_before_open": minutes,
            "continuous_monitor_eod_buffer_minutes": 30,
        },
        root=None,
        trading_controller=None,
    )
    return ContinuousMonitorService(app, log_func=lambda _m: None)


def test_auto_start_regular_open_minus_25() -> None:
    """정규장 09:00 → 08:35."""
    svc = _svc(25)
    day = date(2026, 3, 10)  # 화요일
    assert sh.get_session_hours(day).open == time(9, 0)
    assert svc._auto_start_hm(day) == (8, 35)
    assert svc._auto_start_hhmm_str(day) == "08:35"


def test_auto_start_new_year_open_minus_25() -> None:
    """신년 첫 거래일 10:00 → 09:35."""
    first = sh.first_trading_day_of_year(2026)
    assert first is not None
    h = sh.get_session_hours(first)
    assert h.open == time(10, 0)
    svc = _svc(25)
    assert svc._auto_start_hm(first) == (9, 35)


def test_should_morning_start_window() -> None:
    """장전 윈도우(08:35~09:00)에서만 자동 시작."""
    svc = _svc(25)
    day = date(2026, 3, 10)
    assert svc._should_morning_start(datetime(2026, 3, 10, 8, 34, 59)) is False
    assert svc._should_morning_start(datetime(2026, 3, 10, 8, 35, 0)) is True
    assert svc._should_morning_start(datetime(2026, 3, 10, 8, 50, 0)) is True
    assert svc._should_morning_start(datetime(2026, 3, 10, 8, 59, 59)) is True
    assert svc._should_morning_start(datetime(2026, 3, 10, 9, 0, 0)) is False
    assert svc._should_morning_start(datetime(2026, 3, 10, 10, 0, 0)) is False
    # 주말
    assert svc._should_morning_start(datetime(2026, 3, 14, 8, 40, 0)) is False
    _ = day


def test_ms_until_before_and_inside_window() -> None:
    svc = _svc(25)
    before = datetime(2026, 3, 10, 8, 30, 0)
    ms = svc.ms_until_auto_start(before)
    assert 4 * 60 * 1000 <= ms <= 5 * 60 * 1000

    inside = datetime(2026, 3, 10, 8, 40, 0)
    assert svc.ms_until_auto_start(inside) == 0


def test_ms_until_after_open_goes_to_next_day() -> None:
    svc = _svc(25)
    after_open = datetime(2026, 3, 10, 10, 0, 0)
    ms = svc.ms_until_auto_start(after_open)
    # 다음날(수) 08:35
    nxt = datetime(2026, 3, 11, 8, 35, 0)
    expect = int((nxt - after_open).total_seconds() * 1000)
    assert abs(ms - expect) < 2000


def test_mark_morning_done_blocks_same_day() -> None:
    svc = _svc(25)
    now = datetime(2026, 3, 10, 8, 40, 0)
    assert svc._should_morning_start(now) is True
    svc.mark_morning_start_done(date(2026, 3, 10))
    assert svc._should_morning_start(now) is False
    # 다음날 장전까지 대기
    assert svc.ms_until_auto_start(now) > 0


if __name__ == "__main__":
    test_auto_start_regular_open_minus_25()
    test_auto_start_new_year_open_minus_25()
    test_should_morning_start_window()
    test_ms_until_before_and_inside_window()
    test_ms_until_after_open_goes_to_next_day()
    test_mark_morning_done_blocks_same_day()
    print("ok")
