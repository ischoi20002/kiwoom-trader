# -*- coding: utf-8 -*-
"""장중 재시작 후 틱만으로 완성 15분을 만들지 않는지."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Optional

import pandas as pd

from core.realtime_official_bars_sync import compose_local_15m_ohlcv
from core.rt_tick_m1_store import RtTickM1Store


def _app(
    store: RtTickM1Store,
    *,
    require_head: bool = True,
    stream_ready: Optional[datetime] = None,
    wait_incomplete: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        settings={
            "realtime_15m_last_minute_from_ticks": True,
            "realtime_15m_compose_tick_fill_on_api_miss": True,
            "realtime_15m_compose_require_official_head": require_head,
            "realtime_15m_wait_official_last_if_tick_incomplete": wait_incomplete,
        },
        rt_tick_m1_store=store,
        rt_tick_stream_ready_at=stream_ready,
    )


def _live_app(
    store: RtTickM1Store,
    *,
    require_head: bool = True,
) -> SimpleNamespace:
    """말분 시작 전부터 틱 스트림이 살아 있는 정상 장중."""
    return _app(
        store,
        require_head=require_head,
        stream_ready=datetime(2026, 8, 18, 10, 0, 1),
    )


def test_tick_only_compose_is_rejected() -> None:
    store = RtTickM1Store(max_completed_per_code=24)
    code = "005930"
    # 10:13~10:14 틱만 (재시작 직후)
    store.feed_tick(
        code, 70000.0, 1000.0, now=datetime(2026, 8, 18, 10, 13, 10)
    )
    store.feed_tick(
        code, 70100.0, 1100.0, now=datetime(2026, 8, 18, 10, 14, 5)
    )
    store.seal_tick_tail_minute(code, pd.Timestamp("2026-08-18 10:14:00"))

    slot = pd.Timestamp("2026-08-18 10:00:00")
    got = compose_local_15m_ohlcv(_app(store), code, slot)
    assert got is None, f"틱전용 합성은 거부되어야 함: {got!r}"


def test_official_head_plus_tick_tail_is_allowed() -> None:
    store = RtTickM1Store(max_completed_per_code=24)
    code = "005930"
    store.apply_official_closed_minute(
        code,
        pd.Timestamp("2026-08-18 10:00:00"),
        69000.0,
        69500.0,
        68900.0,
        69400.0,
        5000.0,
    )
    store.feed_tick(
        code, 70000.0, 1000.0, now=datetime(2026, 8, 18, 10, 14, 5)
    )
    store.seal_tick_tail_minute(code, pd.Timestamp("2026-08-18 10:14:00"))

    slot = pd.Timestamp("2026-08-18 10:00:00")
    got = compose_local_15m_ohlcv(_live_app(store), code, slot)
    assert got is not None
    o, h, l_, c, v = got
    assert o == 69000.0
    assert c == 70000.0
    assert h >= 70000.0
    assert v >= 5000.0


def test_require_head_off_allows_tick_only() -> None:
    store = RtTickM1Store(max_completed_per_code=24)
    code = "005930"
    store.feed_tick(
        code, 70000.0, 1000.0, now=datetime(2026, 8, 18, 10, 14, 5)
    )
    store.seal_tick_tail_minute(code, pd.Timestamp("2026-08-18 10:14:00"))
    slot = pd.Timestamp("2026-08-18 10:00:00")
    got = compose_local_15m_ohlcv(
        _live_app(store, require_head=False), code, slot
    )
    assert got is not None
    assert got[3] == 70000.0


def test_incomplete_tail_waits_without_official_last() -> None:
    """10:14:15 재시작 — 부분 틱 말분만 있으면 합성하지 않는다."""
    store = RtTickM1Store(max_completed_per_code=24)
    code = "005930"
    store.apply_official_closed_minute(
        code,
        pd.Timestamp("2026-08-18 10:00:00"),
        69000.0,
        69500.0,
        68900.0,
        69400.0,
        5000.0,
    )
    store.feed_tick(
        code, 99999.0, 100.0, now=datetime(2026, 8, 18, 10, 14, 15)
    )
    store.seal_tick_tail_minute(code, pd.Timestamp("2026-08-18 10:14:00"))
    app = _app(store, stream_ready=datetime(2026, 8, 18, 10, 14, 15))
    got = compose_local_15m_ohlcv(app, code, pd.Timestamp("2026-08-18 10:00:00"))
    assert got is None, f"부분 틱 말분은 대기해야 함: {got!r}"


def test_incomplete_tail_uses_official_last_not_partial_tick() -> None:
    """공식 10:14가 오면 부분 틱 종가가 아니라 공식 종가로 평가한다."""
    store = RtTickM1Store(max_completed_per_code=24)
    code = "005930"
    store.apply_official_closed_minute(
        code,
        pd.Timestamp("2026-08-18 10:00:00"),
        69000.0,
        69500.0,
        68900.0,
        69400.0,
        5000.0,
    )
    store.apply_official_closed_minute(
        code,
        pd.Timestamp("2026-08-18 10:14:00"),
        69600.0,
        69800.0,
        69500.0,
        69700.0,
        800.0,
    )
    store.feed_tick(
        code, 99999.0, 100.0, now=datetime(2026, 8, 18, 10, 14, 15)
    )
    store.seal_tick_tail_minute(code, pd.Timestamp("2026-08-18 10:14:00"))
    app = _app(store, stream_ready=datetime(2026, 8, 18, 10, 14, 15))
    got = compose_local_15m_ohlcv(app, code, pd.Timestamp("2026-08-18 10:00:00"))
    assert got is not None
    o, h, l_, c, v = got
    assert o == 69000.0
    assert c == 69700.0
    assert c != 99999.0
    assert v >= 5800.0


if __name__ == "__main__":
    test_tick_only_compose_is_rejected()
    test_official_head_plus_tick_tail_is_allowed()
    test_require_head_off_allows_tick_only()
    test_incomplete_tail_waits_without_official_last()
    test_incomplete_tail_uses_official_last_not_partial_tick()
    print("ok")
