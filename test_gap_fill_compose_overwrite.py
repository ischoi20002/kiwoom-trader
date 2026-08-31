# -*- coding: utf-8 -*-
"""M1 gap-fill: 기존 15m 행 compose 덮어쓰기."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pandas as pd

from core.realtime_official_bars_sync import (
    fill_missing_closed_15m_from_official_m1,
    _row_ohlcv_differs_from_composed,
)
from core.rt_tick_m1_store import RtTickM1Store


def test_row_ohlcv_differs_detects_open_change() -> None:
    row = pd.Series({"Open": 100.0, "High": 110.0, "Low": 90.0, "Close": 105.0, "Volume": 10.0})
    assert _row_ohlcv_differs_from_composed(row, 101.0, 110.0, 90.0, 105.0, 10.0)
    assert not _row_ohlcv_differs_from_composed(row, 100.0, 110.0, 90.0, 105.0, 10.0)


def test_gap_fill_overwrites_wrong_existing_bar() -> None:
    code = "005930"
    slot = pd.Timestamp("2026-08-18 10:00:00")
    store = RtTickM1Store(max_completed_per_code=24)
    for i, o in enumerate((69000.0, 69100.0, 69200.0)):
        lab = slot + pd.Timedelta(minutes=i)
        store.apply_official_closed_minute(code, lab, o, o + 50, o - 50, o + 10, 100.0 + i)
    for i in range(3, 15):
        lab = slot + pd.Timedelta(minutes=i)
        px = 69200.0 + i
        store.apply_official_closed_minute(code, lab, px, px, px, px, 10.0)

    wrong_o = 69100.0  # skip-first 오염 가정
    df = pd.DataFrame(
        [
            {
                "datetime": slot.strftime("%Y-%m-%d %H:%M:%S"),
                "time": "10:00",
                "Open": wrong_o,
                "High": 69300.0,
                "Low": 68900.0,
                "Close": 69310.0,
                "Volume": 500.0,
            }
        ]
    )
    df["datetime"] = pd.to_datetime(df["datetime"])
    df.set_index("datetime", inplace=True)

    app = SimpleNamespace(
        settings={
            "realtime_m1_head_fill_missing_15m_enabled": True,
            "realtime_m1_compose_overwrite_existing_15m": True,
            "realtime_15m_compose_tick_fill_on_api_miss": True,
            "realtime_15m_compose_require_official_head": True,
            "realtime_15m_compose_flat_middle_no_trade": True,
            "realtime_15m_eval_on_official_last_m1": True,
        },
        rt_tick_m1_store=store,
        rt_tick_stream_ready_at=datetime(2026, 8, 18, 9, 0, 0),
        bars_data={code: df},
        _bars_data_lock=__import__("threading").Lock(),
    )

    rep = fill_missing_closed_15m_from_official_m1(
        app,
        code,
        max_bars=1,
        as_of=datetime(2026, 8, 18, 10, 20, 0),
        include_slots=[slot],
    )
    assert rep.get("filled_overwrite"), rep
    out = app.bars_data[code].loc[slot]
    assert float(out["Open"]) == 69000.0


if __name__ == "__main__":
    test_row_ohlcv_differs_detects_open_change()
    test_gap_fill_overwrites_wrong_existing_bar()
    print("ok")
