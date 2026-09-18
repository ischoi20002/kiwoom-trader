# -*- coding: utf-8 -*-
"""공식1분 도착 프로브 라벨 판별 단위 테스트."""

from __future__ import annotations

import pandas as pd


def test_is_official_arrival_probe_label() -> None:
    from core.rt_watchlist_m1_api_poller import RtWatchlistM1ApiPoller

    assert RtWatchlistM1ApiPoller._is_official_arrival_probe_label(
        pd.Timestamp("2026-09-18 15:14:00")
    )
    assert RtWatchlistM1ApiPoller._is_official_arrival_probe_label(
        pd.Timestamp("2026-09-18 15:19:00")
    )
    assert RtWatchlistM1ApiPoller._is_official_arrival_probe_label(
        pd.Timestamp("2026-09-18 15:30:00")
    )
    assert not RtWatchlistM1ApiPoller._is_official_arrival_probe_label(
        pd.Timestamp("2026-09-18 15:13:00")
    )
    assert not RtWatchlistM1ApiPoller._is_official_arrival_probe_label(
        pd.Timestamp("2026-09-18 14:14:00")
    )


if __name__ == "__main__":
    test_is_official_arrival_probe_label()
    print("ok")
