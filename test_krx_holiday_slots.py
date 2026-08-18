# -*- coding: utf-8 -*-
"""휴장·대체공휴일 15분 슬롯 롤이 가짜봉을 만들지 않는지."""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from reporting.krx_trading_calendar import (
    filter_ohlcv_index_to_trading_days,
    is_trading_day,
)
from utils.krx_bars import (
    is_krx_15m_slot_end,
    krx_stream_slot_end_datetime,
    next_krx_15m_slot_end,
)


def test_gwangbok_substitute_2026() -> None:
    """2026-08-15 토요 광복절 → 08-17 월 대체공휴일."""
    assert is_trading_day(date(2026, 8, 14)) is True
    assert is_trading_day(date(2026, 8, 15)) is False
    assert is_trading_day(date(2026, 8, 17)) is False
    assert is_trading_day(date(2026, 8, 18)) is True


def test_samiljeol_substitute_2026() -> None:
    """2026-03-01 일 삼일절 → 03-02 월 대체공휴일."""
    assert is_trading_day(date(2026, 3, 2)) is False
    assert is_trading_day(date(2026, 3, 3)) is True


def test_next_slot_skips_holiday() -> None:
    nxt = next_krx_15m_slot_end(pd.Timestamp("2026-08-14 15:30:00"))
    assert nxt == pd.Timestamp("2026-08-18 09:00:00")


def test_holiday_is_not_15m_slot() -> None:
    assert is_krx_15m_slot_end(pd.Timestamp("2026-08-17 09:00:00")) is False
    assert is_krx_15m_slot_end(pd.Timestamp("2026-08-17 15:30:00")) is False
    assert is_krx_15m_slot_end(pd.Timestamp("2026-08-18 09:00:00")) is True
    assert is_krx_15m_slot_end(pd.Timestamp("2026-08-14 15:30:00")) is True


def test_stream_slot_on_holiday_stays_on_prev_close() -> None:
    lab = krx_stream_slot_end_datetime(datetime(2026, 8, 17, 10, 0, 0))
    assert lab == pd.Timestamp("2026-08-14 15:30:00")


def test_filter_drops_holiday_rows() -> None:
    idx = pd.to_datetime(
        [
            "2026-08-14 15:30:00",
            "2026-08-17 09:00:00",
            "2026-08-17 15:30:00",
            "2026-08-18 09:00:00",
        ]
    )
    df = pd.DataFrame({"Close": [120000, 120000, 120000, 118300]}, index=idx)
    out = filter_ohlcv_index_to_trading_days(df)
    assert list(out.index) == [
        pd.Timestamp("2026-08-14 15:30:00"),
        pd.Timestamp("2026-08-18 09:00:00"),
    ]


if __name__ == "__main__":
    test_gwangbok_substitute_2026()
    test_samiljeol_substitute_2026()
    test_next_slot_skips_holiday()
    test_holiday_is_not_15m_slot()
    test_stream_slot_on_holiday_stays_on_prev_close()
    test_filter_drops_holiday_rows()
    print("OK: krx holiday slot tests passed")
