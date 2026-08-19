# -*- coding: utf-8 -*-
"""공식 15분 페이지에서 현재 슬롯 이하 완성봉만 남기는지."""
from __future__ import annotations

import pandas as pd

from core.rt_closed_15m_official_replace import _completed_bars_upto


def test_completed_bars_upto_excludes_forming() -> None:
    idx = pd.to_datetime(
        [
            "2026-08-19 11:30:00",
            "2026-08-19 11:45:00",
            "2026-08-19 12:00:00",
        ]
    )
    df = pd.DataFrame(
        {
            "Open": [1.0, 2.0, 3.0],
            "High": [1.0, 2.0, 3.0],
            "Low": [1.0, 2.0, 3.0],
            "Close": [1.0, 2.0, 3.0],
            "Volume": [10.0, 20.0, 30.0],
        },
        index=idx,
    )
    want = pd.Timestamp("2026-08-19 11:45:00")
    rows = _completed_bars_upto(df, want)
    got = [pd.Timestamp(t) for t, *_rest in rows]
    assert pd.Timestamp("2026-08-19 11:30:00") in got
    assert pd.Timestamp("2026-08-19 11:45:00") in got
    assert pd.Timestamp("2026-08-19 12:00:00") not in got


if __name__ == "__main__":
    test_completed_bars_upto_excludes_forming()
    print("ok")
