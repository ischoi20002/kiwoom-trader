# -*- coding: utf-8 -*-
"""SessionHours / krx_bars 특수 개장일 단위 테스트."""
from __future__ import annotations

import json
import tempfile
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

from utils import krx_session_hours as sh
from utils.krx_bars import (
    is_krx_15m_slot_end,
    is_krx_auction_market_fill_slot,
    is_krx_first_15m_slot,
    is_krx_last_15m_slot,
    is_krx_next_open_defer_15m_slot,
    is_krx_pre_auction_15m_slot,
    iter_krx_15m_slot_ends_for_day,
    krx_15m_slot_complete_ts,
)
from utils.krx_session import is_krx_regular_session


def _with_special(mapping: dict, fn) -> None:
    """임시 JSON으로 특수일/메타 오버라이드 후 캐시 리로드."""
    orig_path = sh._SPECIAL_PATH
    try:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "krx_special_sessions.json"
            p.write_text(json.dumps(mapping), encoding="utf-8")
            sh._SPECIAL_PATH = p  # type: ignore[misc]
            sh.reload_special_sessions()
            fn()
    finally:
        sh._SPECIAL_PATH = orig_path  # type: ignore[misc]
        sh.reload_special_sessions()


def test_default_session_hours() -> None:
    h = sh.get_session_hours(date(2026, 3, 10))  # 화요일, JSON 미등록
    assert h.open == time(9, 0)
    assert h.continuous_end == time(15, 20)
    assert h.close == time(15, 30)
    assert h.pre_auction_slot == time(15, 15)


def test_auto_new_year_without_day_entry() -> None:
    """신년 첫 거래일은 JSON 일자 없이도 자동 적용."""

    def _run() -> None:
        # 2026-01-01 목 휴일 → 첫 거래일은 보통 1/2
        first = sh.first_trading_day_of_year(2026)
        assert first is not None
        h = sh.get_session_hours(first)
        assert h.open == time(10, 0)
        assert h.close == time(15, 30)
        assert "신년" in (h.note or "")
        # 그 다음날(거래일)은 기본
        nxt = first.toordinal() + 1
        from datetime import date as _date

        d2 = _date.fromordinal(nxt)
        while d2.weekday() >= 5:
            d2 = _date.fromordinal(d2.toordinal() + 1)
        h2 = sh.get_session_hours(d2)
        assert h2.open == time(9, 0)

    _with_special({"_auto_rules": {"new_year_first_trading_day": True, "csat": True}}, _run)


def test_auto_csat_from_meta_list() -> None:
    mapping = {
        "_auto_rules": {"new_year_first_trading_day": True, "csat": True},
        "_csat_dates": ["2026-11-12"],
    }

    def _run() -> None:
        h = sh.get_session_hours("2026-11-12")
        assert h.open == time(10, 0)
        assert h.close == time(16, 30)
        assert "수능" in (h.note or "")

    _with_special(mapping, _run)


def test_use_default_overrides_auto() -> None:
    mapping = {
        "_csat_dates": ["2026-11-12"],
        "2026-11-12": {"use_default": True, "note": "휴리스틱 해제"},
    }

    def _run() -> None:
        h = sh.get_session_hours("2026-11-12")
        assert h.open == time(9, 0)
        assert h.close == time(15, 30)

    _with_special(mapping, _run)


def test_special_csat_and_new_year() -> None:
    mapping = {
        "2026-01-02": {
            "open": "10:00",
            "continuous_end": "15:20",
            "close": "15:30",
            "note": "신년",
        },
        "2026-11-12": {
            "open": "10:00",
            "continuous_end": "16:20",
            "close": "16:30",
            "note": "수능",
        },
    }

    def _run() -> None:
        ny = sh.get_session_hours("2026-01-02")
        assert ny.open == time(10, 0)
        assert ny.close == time(15, 30)
        assert ny.pre_auction_slot == time(15, 15)
        assert sh.is_in_regular_session(datetime(2026, 1, 2, 9, 30)) is False
        assert sh.is_in_regular_session(datetime(2026, 1, 2, 10, 0)) is True

        cs = sh.get_session_hours("2026-11-12")
        assert cs.open == time(10, 0)
        assert cs.continuous_end == time(16, 20)
        assert cs.close == time(16, 30)
        assert cs.pre_auction_slot == time(16, 15)
        assert sh.is_in_regular_session(datetime(2026, 11, 12, 9, 30)) is False
        assert sh.is_in_regular_session(datetime(2026, 11, 12, 16, 0)) is True
        assert sh.is_in_regular_session(datetime(2026, 11, 12, 16, 45)) is False

    _with_special(mapping, _run)


def test_default_15m_labels_regression() -> None:
    labels = iter_krx_15m_slot_ends_for_day("2026-03-10")
    assert labels[0] == pd.Timestamp("2026-03-10 09:00:00")
    assert labels[-1] == pd.Timestamp("2026-03-10 15:30:00")
    assert pd.Timestamp("2026-03-10 15:15:00") in labels
    assert is_krx_last_15m_slot(labels[-1])
    assert is_krx_pre_auction_15m_slot(pd.Timestamp("2026-03-10 15:15:00"))
    assert is_krx_auction_market_fill_slot(pd.Timestamp("2026-03-10 15:15:00"))
    assert is_krx_next_open_defer_15m_slot(labels[-1])
    from utils.krx_bars import (
        is_last_m1_open_of_15m_slot,
        is_pre_auction_last_m1_open,
        is_slot_head_complete_m1_open,
        slot_head_m1_range,
    )

    assert is_pre_auction_last_m1_open(pd.Timestamp("2026-03-10 15:19:00"))
    assert is_last_m1_open_of_15m_slot(pd.Timestamp("2026-03-10 15:19:00"))
    assert not is_pre_auction_last_m1_open(pd.Timestamp("2026-03-10 15:14:00"))
    assert krx_15m_slot_complete_ts(pd.Timestamp("2026-03-10 15:15:00")) == pd.Timestamp(
        "2026-03-10 15:20:00"
    )
    # head catchup: 15:18 닫힘 → 벽 15:19:00 (말분 15:19는 15:20:00)
    assert is_slot_head_complete_m1_open(pd.Timestamp("2026-03-10 15:18:00"))
    assert not is_slot_head_complete_m1_open(pd.Timestamp("2026-03-10 15:19:00"))
    assert is_slot_head_complete_m1_open(pd.Timestamp("2026-03-10 09:13:00"))
    assert not is_slot_head_complete_m1_open(pd.Timestamp("2026-03-10 09:14:00"))
    pa_rng = slot_head_m1_range(pd.Timestamp("2026-03-10 15:15:00"))
    assert pa_rng is not None
    assert pa_rng[0] == pd.Timestamp("2026-03-10 15:15:00")
    assert pa_rng[1] == pd.Timestamp("2026-03-10 15:19:00")


def test_stream_slot_rolls_to_close_after_continuous_end() -> None:
    """
    15:15 잔여봉은 continuous_end(15:20)에 완성 → 그 시각부터 want=15:30.
    (이전: 15:30까지 want=15:15 유지 → 정렬교체가 +860s로 밀림)
    """
    from datetime import datetime

    from utils.krx_bars import (
        krx_stream_slot_end_datetime,
        seconds_until_next_krx_15m_boundary,
    )

    assert krx_stream_slot_end_datetime(datetime(2026, 3, 10, 15, 15, 0)) == pd.Timestamp(
        "2026-03-10 15:15:00"
    )
    assert krx_stream_slot_end_datetime(datetime(2026, 3, 10, 15, 19, 59)) == pd.Timestamp(
        "2026-03-10 15:15:00"
    )
    assert krx_stream_slot_end_datetime(datetime(2026, 3, 10, 15, 20, 0)) == pd.Timestamp(
        "2026-03-10 15:30:00"
    )
    assert krx_stream_slot_end_datetime(datetime(2026, 3, 10, 15, 25, 0)) == pd.Timestamp(
        "2026-03-10 15:30:00"
    )
    # 15:15 구간 중에는 완성 경계(15:20)까지 대기
    rem = seconds_until_next_krx_15m_boundary(datetime(2026, 3, 10, 15, 15, 0))
    assert 299.0 <= rem <= 300.0


def test_csat_15m_labels() -> None:
    mapping = {
        "2026-11-12": {
            "open": "10:00",
            "continuous_end": "16:20",
            "close": "16:30",
        }
    }

    def _run() -> None:
        labels = iter_krx_15m_slot_ends_for_day("2026-11-12")
        assert labels[0] == pd.Timestamp("2026-11-12 10:00:00")
        assert pd.Timestamp("2026-11-12 16:30:00") in labels
        # 애프터 시행일: 종가(16:30) 이후 애프터 슬롯이 이어짐
        assert labels[-1] == pd.Timestamp("2026-11-12 19:45:00")
        assert pd.Timestamp("2026-11-12 16:15:00") in labels
        assert is_krx_15m_slot_end(pd.Timestamp("2026-11-12 10:00:00"))
        assert is_krx_first_15m_slot(pd.Timestamp("2026-11-12 10:00:00"))
        assert not is_krx_first_15m_slot(pd.Timestamp("2026-11-12 09:00:00"))
        assert not is_krx_15m_slot_end(pd.Timestamp("2026-11-12 09:00:00"))
        assert is_krx_pre_auction_15m_slot(pd.Timestamp("2026-11-12 16:15:00"))
        assert is_krx_last_15m_slot(pd.Timestamp("2026-11-12 16:30:00"))
        assert is_krx_auction_market_fill_slot(pd.Timestamp("2026-11-12 16:15:00"))
        assert is_krx_next_open_defer_15m_slot(pd.Timestamp("2026-11-12 16:30:00"))
        assert krx_15m_slot_complete_ts(pd.Timestamp("2026-11-12 16:15:00")) == pd.Timestamp(
            "2026-11-12 16:20:00"
        )
        # 정규 격자 27 + 종가 이후 애프터(16:45…19:45)
        base = len(iter_krx_15m_slot_ends_for_day("2026-03-10"))
        assert len(labels) > base
        assert pd.Timestamp("2026-11-12 16:45:00") in labels

    _with_special(mapping, _run)


def test_is_krx_regular_session_uses_hours() -> None:
    mapping = {
        "2026-01-02": {
            "open": "10:00",
            "continuous_end": "15:20",
            "close": "15:30",
        }
    }

    def _run() -> None:
        assert is_krx_regular_session(datetime(2026, 1, 2, 9, 45)) is False
        assert is_krx_regular_session(datetime(2026, 1, 2, 10, 5)) is True
        assert is_krx_regular_session(datetime(2026, 1, 2, 15, 30)) is True
        assert is_krx_regular_session(datetime(2026, 1, 2, 15, 31)) is False

    _with_special(mapping, _run)


def test_bad_json_falls_back() -> None:
    mapping = {
        "2026-06-01": {
            "open": "99:00",
            "continuous_end": "15:20",
            "close": "15:30",
        }
    }

    def _run() -> None:
        h = sh.get_session_hours("2026-06-01")
        assert h.open == time(9, 0)
        assert h.close == time(15, 30)

    _with_special(mapping, _run)


def test_last_completed_15m_slot_mid_restart() -> None:
    """장중 재시작(예: 13:48) → 직전 완성 슬롯(13:30)을 잡는다."""
    from datetime import datetime

    from utils.krx_bars import (
        last_completed_krx_15m_slot_end,
        prev_krx_15m_slot_end,
    )

    assert prev_krx_15m_slot_end(pd.Timestamp("2026-03-10 13:45:00")) == pd.Timestamp(
        "2026-03-10 13:30:00"
    )
    assert last_completed_krx_15m_slot_end(
        datetime(2026, 3, 10, 13, 48, 0)
    ) == pd.Timestamp("2026-03-10 13:30:00")
    # 슬롯 완성 직후(13:45:01)에도 13:30
    assert last_completed_krx_15m_slot_end(
        datetime(2026, 3, 10, 13, 45, 1)
    ) == pd.Timestamp("2026-03-10 13:30:00")
    # 당일 첫 슬롯 형성 중(09:05) → None (전일 종가 평가 금지)
    assert last_completed_krx_15m_slot_end(datetime(2026, 3, 10, 9, 5, 0)) is None


if __name__ == "__main__":
    test_default_session_hours()
    test_auto_new_year_without_day_entry()
    test_auto_csat_from_meta_list()
    test_use_default_overrides_auto()
    test_special_csat_and_new_year()
    test_default_15m_labels_regression()
    test_csat_15m_labels()
    test_is_krx_regular_session_uses_hours()
    test_bad_json_falls_back()
    test_last_completed_15m_slot_mid_restart()
    print("OK: all krx session hours tests passed")
