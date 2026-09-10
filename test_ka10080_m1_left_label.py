# -*- coding: utf-8 -*-
"""ka10080 1분 cntr_tm 은 좌측 라벨 — 조회·catchup이 +1/−1 하면 안 된다."""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from core.realtime_m1_fallback import (
    apply_chart_catchup_to_m1_store,
    chart_1m_open_labels_in_range,
)
from core.rt_tick_m1_store import RtTickM1Store
from core.rt_watchlist_m1_api_poller import RtWatchlistM1ApiPoller
from utils.krx_bars import (
    m1_ka10080_cntr_tm_for_closed_open,
    m1_open_label_from_ka10080_cntr_tm,
)


def test_cntr_tm_lookup_is_open_label() -> None:
    t = pd.Timestamp("2026-09-10 09:14:00")
    assert m1_ka10080_cntr_tm_for_closed_open(t) == t
    auc = pd.Timestamp("2026-09-10 15:30:00")
    assert m1_ka10080_cntr_tm_for_closed_open(auc) == auc
    assert m1_open_label_from_ka10080_cntr_tm(t) == t


def test_pick_row_does_not_take_next_minute() -> None:
    day = pd.Timestamp("2026-09-10")
    idx = pd.DatetimeIndex(
        [
            day + pd.Timedelta(hours=9, minutes=14),
            day + pd.Timedelta(hours=9, minutes=15),
        ]
    )
    chart = pd.DataFrame(
        {
            "Open": [100.0, 200.0],
            "High": [101.0, 201.0],
            "Low": [99.0, 199.0],
            "Close": [100.0, 200.0],
            "Volume": [10.0, 20.0],
        },
        index=idx,
    )
    poller = RtWatchlistM1ApiPoller(SimpleNamespace(settings={}), log=None)
    lab = day + pd.Timedelta(hours=9, minutes=14)
    row = poller._pick_row_for_closed_open(chart, lab)
    assert row is not None
    assert float(row["Close"]) == 100.0


def test_chart_open_labels_are_index_times() -> None:
    day = pd.Timestamp("2026-09-10")
    idx = pd.DatetimeIndex(
        [
            day + pd.Timedelta(hours=9, minutes=14),
            day + pd.Timedelta(hours=9, minutes=15),
        ]
    )
    chart = pd.DataFrame({"Open": [1.0, 2.0]}, index=idx)
    t0 = day + pd.Timedelta(hours=9, minutes=0)
    last = day + pd.Timedelta(hours=9, minutes=14)
    found = chart_1m_open_labels_in_range(chart, t0, last)
    assert (day + pd.Timedelta(hours=9, minutes=14)) in found
    assert (day + pd.Timedelta(hours=9, minutes=13)) not in found
    assert (day + pd.Timedelta(hours=9, minutes=15)) not in found


def test_catchup_stores_same_timestamp() -> None:
    store = RtTickM1Store(max_completed_per_code=32)
    app = SimpleNamespace(rt_tick_m1_store=store)
    day = pd.Timestamp("2026-09-10")
    idx = pd.DatetimeIndex(
        [
            day + pd.Timedelta(hours=9, minutes=13),
            day + pd.Timedelta(hours=9, minutes=14),
            day + pd.Timedelta(hours=9, minutes=15),
        ]
    )
    chart = pd.DataFrame(
        {
            "Open": [10.0, 20.0, 30.0],
            "High": [11.0, 21.0, 31.0],
            "Low": [9.0, 19.0, 29.0],
            "Close": [10.0, 20.0, 30.0],
            "Volume": [1.0, 2.0, 3.0],
        },
        index=idx,
    )
    upto = day + pd.Timedelta(hours=9, minutes=14)
    applied = apply_chart_catchup_to_m1_store(
        app,
        "005930",
        chart,
        upto,
        from_open=day + pd.Timedelta(hours=9, minutes=13),
    )
    assert applied >= 2
    df = store.dataframe_for_window(
        "005930",
        day + pd.Timedelta(hours=9, minutes=13),
        upto,
        include_open_bucket=False,
    )
    assert df is not None and not df.empty
    assert float(df.loc[upto, "Close"]) == 20.0
    nxt = day + pd.Timedelta(hours=9, minutes=15)
    assert nxt not in df.index


if __name__ == "__main__":
    test_cntr_tm_lookup_is_open_label()
    test_pick_row_does_not_take_next_minute()
    test_chart_open_labels_are_index_times()
    test_catchup_stores_same_timestamp()
    print("ok")
