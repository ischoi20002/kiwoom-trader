# -*- coding: utf-8 -*-
"""KRX 애프터마켓 분봉 완결 시각."""
from __future__ import annotations

from datetime import date, datetime

from reporting.krx_trading_calendar import (
    aftermarket_last_m1_datetime,
    harvest_blocked_until_local,
    has_krx_aftermarket,
    minute_bar_complete_datetime_for_day,
    minute_harvest_datetime_cap_iso,
    required_eod_bar_datetime_asof_local,
)


def test_aftermarket_starts_2026_09_14() -> None:
    assert not has_krx_aftermarket(date(2026, 9, 13))
    assert has_krx_aftermarket(date(2026, 9, 14))
    assert has_krx_aftermarket("2026-09-15")


def test_minute_complete_before_aftermarket_is_regular_close() -> None:
    dt = minute_bar_complete_datetime_for_day(date(2026, 9, 11))
    assert dt == datetime(2026, 9, 11, 15, 30, 0)


def test_minute_complete_aftermarket_day_is_1959() -> None:
    assert aftermarket_last_m1_datetime(date(2026, 9, 14)) == datetime(
        2026, 9, 14, 19, 59, 0
    )
    assert minute_bar_complete_datetime_for_day(date(2026, 9, 14)) == datetime(
        2026, 9, 14, 19, 59, 0
    )


def test_required_eod_after_regular_close_uses_aftermarket_end() -> None:
    # 정규장 종료 후·애프터 중: 당일 19:59 가 완결 기준
    now = datetime(2026, 9, 14, 19, 10, 0)
    req = required_eod_bar_datetime_asof_local(now)
    assert req == datetime(2026, 9, 14, 19, 59, 0)


def test_required_eod_during_regular_session_prev_day() -> None:
    # 다음 거래일 장중: 직전일 애프터 완결(19:59)
    now = datetime(2026, 9, 15, 10, 0, 0)  # 월요일이면 거래일 가정
    # 2026-09-15 화요일 — 직전 거래일 9/14
    req = required_eod_bar_datetime_asof_local(now)
    assert req == datetime(2026, 9, 14, 19, 59, 0)


def test_intraday_cap_includes_prev_aftermarket() -> None:
    # 장중 캡도 직전일 19:59 까지 허용해야 애프터 증분이 잘리지 않음
    now = datetime(2026, 9, 15, 10, 0, 0)
    cap = minute_harvest_datetime_cap_iso(now)
    assert cap == "2026-09-14 19:59:00"


def test_after_hours_no_cap() -> None:
    now = datetime(2026, 9, 14, 19, 10, 0)
    assert minute_harvest_datetime_cap_iso(now) is None


def test_harvest_blocked_during_aftermarket() -> None:
    until = datetime(2026, 10, 14, 20, 5, 0)
    # 정규장 마감 시각부터 애프터 종료+5분 직전까지 차단
    assert harvest_blocked_until_local(datetime(2026, 10, 14, 15, 30, 0)) == until
    assert harvest_blocked_until_local(datetime(2026, 10, 14, 17, 0, 0)) == until
    assert harvest_blocked_until_local(datetime(2026, 10, 14, 20, 4, 59)) == until


def test_harvest_allowed_outside_aftermarket() -> None:
    # 장전·장중(당일 봉 캡 적용)·애프터 종료+5분 이후·주말·애프터 시행 전은 허용
    assert harvest_blocked_until_local(datetime(2026, 10, 14, 8, 0, 0)) is None
    assert harvest_blocked_until_local(datetime(2026, 10, 14, 15, 29, 0)) is None
    assert harvest_blocked_until_local(datetime(2026, 10, 14, 20, 5, 0)) is None
    assert harvest_blocked_until_local(datetime(2026, 10, 17, 17, 0, 0)) is None
    assert harvest_blocked_until_local(datetime(2026, 9, 11, 17, 0, 0)) is None


if __name__ == "__main__":
    test_aftermarket_starts_2026_09_14()
    test_minute_complete_before_aftermarket_is_regular_close()
    test_minute_complete_aftermarket_day_is_1959()
    test_required_eod_after_regular_close_uses_aftermarket_end()
    test_required_eod_during_regular_session_prev_day()
    test_intraday_cap_includes_prev_aftermarket()
    test_after_hours_no_cap()
    test_harvest_blocked_during_aftermarket()
    test_harvest_allowed_outside_aftermarket()
    print("ok")
