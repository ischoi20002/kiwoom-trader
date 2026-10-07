# -*- coding: utf-8 -*-
"""애프터(16:00~19:45) 15분봉 사용 정책 — 슬롯 격자·워밍업·백테 로딩 공통."""
from __future__ import annotations

import contextlib
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, Iterator

import pandas as pd

from utils.aftermarket_policy import (
    aftermarket_bars_enabled_for_settings,
    drop_aftermarket_rows,
    force_aftermarket_grid,
    register_live_settings_provider,
)
from utils.krx_bars import (
    is_krx_15m_slot_end,
    iter_krx_15m_slot_ends_for_day,
    krx_15m_label_for_m1_open,
    krx_stream_slot_end_datetime,
    next_krx_15m_slot_end,
    resample_1m_ohlcv_to_15m_krx,
)

_OFF: Dict[str, Any] = {"order_after_close_enabled": False}
_ON: Dict[str, Any] = {"order_after_close_enabled": True}


@contextlib.contextmanager
def _live_off() -> Iterator[None]:
    """실시간 설정 = 장마감 후 주문 OFF(애프터 봉 미사용)."""
    register_live_settings_provider(lambda: _OFF)
    try:
        yield
    finally:
        register_live_settings_provider(None)


def _day_15m_frame(day: str) -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        [
            f"{day} 15:15:00",
            f"{day} 15:30:00",
            f"{day} 16:00:00",
            f"{day} 19:45:00",
        ]
    )
    return pd.DataFrame(
        {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0, "Volume": 1.0},
        index=idx,
    )


def test_policy_resolution() -> None:
    assert aftermarket_bars_enabled_for_settings(_OFF) is False
    assert aftermarket_bars_enabled_for_settings(_ON) is True
    assert aftermarket_bars_enabled_for_settings({}) is True
    assert aftermarket_bars_enabled_for_settings(None) is True
    assert aftermarket_bars_enabled_for_settings({**_OFF, "use_aftermarket_bars": True}) is True
    assert aftermarket_bars_enabled_for_settings({**_ON, "use_aftermarket_bars": "false"}) is False
    assert aftermarket_bars_enabled_for_settings({**_OFF, "use_aftermarket_bars": "auto"}) is False


def test_drop_rows_keeps_1530_and_removes_aftermarket() -> None:
    df = _day_15m_frame("2026-09-14")
    out = drop_aftermarket_rows(df, _OFF)
    assert list(out.index.strftime("%H:%M")) == ["15:15", "15:30"]
    assert drop_aftermarket_rows(df, _ON) is df


def test_drop_rows_time_column_frame() -> None:
    df = _day_15m_frame("2026-09-14").reset_index().rename(columns={"index": "time"})
    out = drop_aftermarket_rows(df, _OFF)
    assert len(out) == 2


def test_grid_default_includes_aftermarket() -> None:
    register_live_settings_provider(None)
    assert is_krx_15m_slot_end("2026-09-14 16:00:00")
    assert next_krx_15m_slot_end("2026-09-14 15:30:00") == pd.Timestamp("2026-09-14 16:00:00")


def test_grid_off_skips_aftermarket() -> None:
    with _live_off():
        assert not is_krx_15m_slot_end("2026-09-14 16:00:00")
        assert len(iter_krx_15m_slot_ends_for_day("2026-09-14")) == 27
        assert next_krx_15m_slot_end("2026-09-14 15:30:00") == pd.Timestamp(
            "2026-09-15 09:00:00"
        )
        # 애프터 시각에도 형성 슬롯은 종가(15:30) 유지 → 새 봉을 만들지 않음
        assert krx_stream_slot_end_datetime(datetime(2026, 9, 14, 17, 7)) == pd.Timestamp(
            "2026-09-14 15:30:00"
        )
        assert krx_15m_label_for_m1_open("2026-09-14 17:07:00") is None


def test_force_grid_overrides_live() -> None:
    with _live_off():
        with force_aftermarket_grid(True):
            assert is_krx_15m_slot_end("2026-09-14 16:00:00")
        assert not is_krx_15m_slot_end("2026-09-14 16:00:00")


def test_harvest_resample_keeps_aftermarket() -> None:
    idx = pd.date_range("2026-09-14 16:00:00", periods=15, freq="1min")
    m1 = pd.DataFrame(
        {"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5, "volume": 100.0},
        index=idx,
    )
    with _live_off():
        out = resample_1m_ohlcv_to_15m_krx(m1, "005930")
    assert list(out["datetime"]) == ["2026-09-14 16:00:00"]


def test_warmup_merge_drops_aftermarket() -> None:
    from core.warmup_service import WarmupService

    svc = WarmupService.__new__(WarmupService)
    svc.app = SimpleNamespace(settings=dict(_OFF))
    svc.log = lambda *_a, **_k: None
    df = _day_15m_frame("2026-09-14")
    out = svc._merge_15m_ohlcv(pd.DataFrame(), df)
    assert list(out.index.strftime("%H:%M")) == ["15:15", "15:30"]

    svc.app = SimpleNamespace(settings=dict(_ON))
    out_on = svc._merge_15m_ohlcv(pd.DataFrame(), df)
    assert len(out_on) == 4


def test_bt_ohlcv_fingerprint_depends_on_policy() -> None:
    from core.bt_prep_cache import ohlcv_fingerprint

    a = ohlcv_fingerprint("u", _OFF, start_date="2026-09-01", end_date="2026-09-30")
    b = ohlcv_fingerprint("u", _ON, start_date="2026-09-01", end_date="2026-09-30")
    assert a != b


if __name__ == "__main__":
    test_policy_resolution()
    test_drop_rows_keeps_1530_and_removes_aftermarket()
    test_drop_rows_time_column_frame()
    test_grid_default_includes_aftermarket()
    test_grid_off_skips_aftermarket()
    test_force_grid_overrides_live()
    test_harvest_resample_keeps_aftermarket()
    test_warmup_merge_drops_aftermarket()
    test_bt_ohlcv_fingerprint_depends_on_policy()
    print("ok")
