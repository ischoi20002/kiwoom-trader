# -*- coding: utf-8 -*-
"""무거래 t0·말분 평봉 합성."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from core.realtime_official_bars_sync import compose_local_15m_ohlcv
from core.rt_tick_m1_store import RtTickM1Store


def _app_with_store(store: RtTickM1Store) -> SimpleNamespace:
    return SimpleNamespace(
        settings={
            "realtime_15m_compose_flat_no_trade": True,
            "realtime_15m_compose_tick_fill_on_api_miss": True,
            "realtime_15m_compose_require_official_head": True,
            "realtime_15m_eval_on_official_last_m1": True,
        },
        rt_tick_m1_store=store,
        rt_tick_stream_ready_at=datetime(2026, 8, 18, 9, 0, 0),
        bars_data={},
        _bars_data_lock=__import__("threading").Lock(),
    )


def test_flat_t0_when_later_minutes_exist() -> None:
    code = "005930"
    slot = pd.Timestamp("2026-08-18 10:00:00")  # 좌측 라벨 [10:00, 10:15)
    t0 = pd.Timestamp("2026-08-18 10:00:00")
    prev_close = 69990.0
    store = RtTickM1Store(max_completed_per_code=24)
    store.apply_official_closed_minute(
        code,
        t0 - pd.Timedelta(minutes=1),
        prev_close,
        prev_close,
        prev_close,
        prev_close,
        10.0,
    )
    # t0 무거래 — 10:01~10:14만 적재
    for i in range(1, 15):
        lab = t0 + pd.Timedelta(minutes=i)
        px = 70000.0 + i * 10
        store.apply_official_closed_minute(code, lab, px, px + 5, px - 5, px, 100.0)
    app = _app_with_store(store)
    out = compose_local_15m_ohlcv(app, code, slot)
    assert out is not None, "t0 무거래 평봉 후 합성 실패"
    o, _, _, c, _ = out
    assert float(o) == prev_close
    assert float(c) == 70000.0 + 14 * 10


def test_flat_last_when_slot_closed() -> None:
    code = "005930"
    slot = pd.Timestamp("2026-08-18 10:00:00")
    t0 = pd.Timestamp("2026-08-18 10:00:00")
    store = RtTickM1Store(max_completed_per_code=24)
    for i in range(14):
        lab = t0 + pd.Timedelta(minutes=i)
        px = 50000.0 + i * 10
        store.apply_official_closed_minute(code, lab, px, px, px, px, 50.0)
    # 말분(10:14) 무거래 생략
    app = _app_with_store(store)
    out = compose_local_15m_ohlcv(app, code, slot)
    assert out is not None, "말분 무거래 평봉 후 합성 실패"
    _, _, _, c, v = out
    assert float(c) == 50000.0 + 13 * 10
    assert float(v) == 14 * 50.0


if __name__ == "__main__":
    test_flat_t0_when_later_minutes_exist()
    test_flat_last_when_slot_closed()
    print("ok")
