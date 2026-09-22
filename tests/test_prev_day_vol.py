"""전일 동시간 거래량(Prev_Day_Vol) 계산 정합·쿼텟 피벗 1회화 검증."""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategy.indicators import (
    attach_prev_day_same_time_volume,
    attach_prev_day_volume_quartet,
)


def _legacy_prev_day_same_time_volume(
    df: pd.DataFrame,
    vol_lookback_days: int,
    out_column: str,
) -> None:
    """리팩터 이전 pivot_table 경로(정합 비교용)."""
    vol_lookback = max(1, int(vol_lookback_days))
    df_tmp = df[["Volume"]].copy()
    df_tmp["date_only"] = df_tmp.index.normalize()
    df_tmp["time_only"] = df_tmp.index.time
    pivoted = df_tmp.pivot_table(index="date_only", columns="time_only", values="Volume")
    pivoted_rolled = pivoted.shift(1).rolling(window=vol_lookback, min_periods=1).mean()
    stacked = pivoted_rolled.stack(future_stack=True)
    lookup_keys = pd.MultiIndex.from_arrays([df_tmp.index.normalize(), df_tmp.index.time])
    df[out_column] = stacked.reindex(lookup_keys).values
    df[out_column] = df[out_column].ffill().fillna(0)


def _make_15m_ohlcv(n_days: int = 12, seed: int = 0) -> pd.DataFrame:
    """영업일 09:00~15:15 15분봉 가짜 OHLCV."""
    rng = np.random.default_rng(seed)
    times: list[pd.Timestamp] = []
    start = pd.Timestamp("2024-06-03 09:00:00")
    day = start.normalize()
    while len({t.normalize() for t in times}) < n_days:
        if day.weekday() < 5:
            for m in range(0, 6 * 60 + 15, 15):  # 09:00 ~ 15:15
                times.append(day + pd.Timedelta(hours=9, minutes=m))
        day += pd.Timedelta(days=1)
    n = len(times)
    close = 10000 + np.cumsum(rng.normal(0, 50, size=n))
    vol = rng.integers(1000, 50000, size=n).astype(float)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + 10,
            "Low": close - 10,
            "Close": close,
            "Volume": vol,
        },
        index=pd.DatetimeIndex(times),
    )


def test_attach_prev_day_matches_legacy_pivot_table() -> None:
    df = _make_15m_ohlcv()
    df_new = df.copy()
    df_old = df.copy()
    attach_prev_day_same_time_volume(df_new, 10, "Prev_Day_Vol")
    _legacy_prev_day_same_time_volume(df_old, 10, "Prev_Day_Vol")
    np.testing.assert_allclose(
        df_new["Prev_Day_Vol"].to_numpy(dtype=float),
        df_old["Prev_Day_Vol"].to_numpy(dtype=float),
        rtol=0,
        atol=1e-9,
    )


def test_quartet_dedupes_identical_lookbacks() -> None:
    df = _make_15m_ohlcv()
    settings = {
        "prev_vol_lookback_s1_a": 8,
        "prev_vol_lookback_s1_b": 8,
        "prev_vol_lookback_s2_a": 5,
        "prev_vol_lookback_s2_b": 5,
    }
    attach_prev_day_volume_quartet(df, settings)
    np.testing.assert_allclose(
        df["Prev_Day_Vol_S1_A"].to_numpy(dtype=float),
        df["Prev_Day_Vol_S1_B"].to_numpy(dtype=float),
        rtol=0,
        atol=1e-9,
    )
    np.testing.assert_allclose(
        df["Prev_Day_Vol_S2_A"].to_numpy(dtype=float),
        df["Prev_Day_Vol_S2_B"].to_numpy(dtype=float),
        rtol=0,
        atol=1e-9,
    )
    np.testing.assert_allclose(
        df["Prev_Day_Vol"].to_numpy(dtype=float),
        df["Prev_Day_Vol_S1_A"].to_numpy(dtype=float),
        rtol=0,
        atol=1e-9,
    )
    ref = _make_15m_ohlcv()
    attach_prev_day_same_time_volume(ref, 8, "L8")
    attach_prev_day_same_time_volume(ref, 5, "L5")
    np.testing.assert_allclose(
        df["Prev_Day_Vol_S1_A"].to_numpy(dtype=float),
        ref["L8"].to_numpy(dtype=float),
        rtol=0,
        atol=1e-9,
    )
    np.testing.assert_allclose(
        df["Prev_Day_Vol_S2_A"].to_numpy(dtype=float),
        ref["L5"].to_numpy(dtype=float),
        rtol=0,
        atol=1e-9,
    )
