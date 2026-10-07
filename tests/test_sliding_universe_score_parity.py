# -*- coding: utf-8 -*-
"""슬라이딩 유니버스 스코어 정합: as-of [D-N,D] · 당일 resync · 기간 불변."""

from __future__ import annotations

import unittest
from datetime import timedelta
from typing import Dict, List, Set

import numpy as np
import pandas as pd

from strategy.selection_dynamic_score import _score_dynamic_universe_daily_ohlcv
from strategy.selection_dynamic_universe import _score_one_sliding_day
from strategy.selection_track_batch import _resync_batch_valid_to_period_universe


def _synth_ohlcv(
    codes: List[str],
    start: str,
    end: str,
    *,
    seed: int = 7,
) -> pd.DataFrame:
    """영업일 근사(BDay) 합성 일봉."""
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start=start, end=end)
    rows: List[dict] = []
    for code in codes:
        px = 10000.0 + float(rng.integers(0, 5000))
        for i, d in enumerate(days):
            shock = float(rng.normal(0.0, 0.015))
            # 종목·일자별 결정적 드리프트
            shock += 0.0002 * (hash(code) % 7 - 3)
            o = px
            c = max(100.0, px * (1.0 + shock))
            h = max(o, c) * (1.0 + abs(float(rng.normal(0, 0.005))))
            l = min(o, c) * (1.0 - abs(float(rng.normal(0, 0.005))))
            vol = float(rng.integers(50_000, 500_000))
            rows.append(
                {
                    "code": code,
                    "time": pd.Timestamp(d),
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": vol,
                }
            )
            px = c
    df = pd.DataFrame(rows)
    df.sort_values(["time", "code"], kind="mergesort", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def _legacy_score_ohlcv(df_all: pd.DataFrame, settings: dict) -> None:
    """리팩터 이전 groupby 반복 경로(정합 비교용)."""
    df_all["close"] = df_all["close"].astype(float)
    df_all["volume"] = df_all["volume"].astype(float)
    df_all["trde_val"] = df_all["close"] * df_all["volume"]
    df_all["MA5"] = (
        df_all.groupby("code")["close"].rolling(5).mean().reset_index(0, drop=True)
    )
    df_all["MA20"] = (
        df_all.groupby("code")["close"].rolling(20).mean().reset_index(0, drop=True)
    )
    df_all["MA60"] = (
        df_all.groupby("code")["close"].rolling(60).mean().reset_index(0, drop=True)
    )
    delta = df_all.groupby("code")["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ema_up = (
        gain.groupby(df_all["code"]).ewm(com=13, adjust=False).mean().reset_index(0, drop=True)
    )
    ema_down = (
        loss.groupby(df_all["code"])
        .ewm(com=13, adjust=False)
        .mean()
        .reset_index(0, drop=True)
    )
    df_all["RSI"] = 100 - (100 / (1 + ema_up / (ema_down + 1e-10)))
    m_f, m_sl, m_sg = (
        int(settings.get("macd_fast_daily", 12)),
        int(settings.get("macd_slow_daily", 26)),
        int(settings.get("macd_sig_daily", 9)),
    )
    ema_f = (
        df_all.groupby("code")["close"]
        .ewm(span=m_f, adjust=False)
        .mean()
        .reset_index(0, drop=True)
    )
    ema_s = (
        df_all.groupby("code")["close"]
        .ewm(span=m_sl, adjust=False)
        .mean()
        .reset_index(0, drop=True)
    )
    df_all["MACD"] = ema_f - ema_s
    macd_s = (
        df_all.groupby("code")["MACD"]
        .ewm(span=m_sg, adjust=False)
        .mean()
        .reset_index(0, drop=True)
    )
    df_all["OSC"] = df_all["MACD"] - macd_s
    vol_lookback = int(settings.get("vol_lookback", 10))
    df_all["AvgVal5"] = (
        df_all.groupby("code")["trde_val"].rolling(5).mean().reset_index(0, drop=True)
    )
    df_all["MA_Diff"] = (df_all["MA5"] - df_all["MA20"]).abs() / df_all["MA20"]
    df_all["Pos_Diff"] = (df_all["close"] - df_all["MA20"]).abs() / df_all["MA20"]
    df_all["VolRatio"] = df_all["volume"] / df_all.groupby("code")["volume"].shift(1)
    df_all["OSC_Prev"] = df_all.groupby("code")["OSC"].shift(1)
    vol_ratio_a = float(settings.get("vol_ratio", 1.5))
    df_all["VolBurst_A"] = (df_all["VolRatio"] >= vol_ratio_a) & (
        df_all["close"] > df_all.groupby("code")["close"].shift(1)
    )
    df_all["VolBurstActive_A"] = (
        df_all.groupby("code")["VolBurst_A"]
        .rolling(vol_lookback)
        .max()
        .reset_index(0, drop=True)
    )
    vol_ratio_b = float(settings.get("sel_b_vol_ratio", vol_ratio_a))
    vol_lookback_b = int(settings.get("sel_b_vol_lookback", vol_lookback))
    df_all["VolBurst_B"] = (df_all["VolRatio"] >= vol_ratio_b) & (
        df_all["close"] > df_all.groupby("code")["close"].shift(1)
    )
    df_all["VolBurstActive_B"] = (
        df_all.groupby("code")["VolBurst_B"]
        .rolling(vol_lookback_b)
        .max()
        .reset_index(0, drop=True)
    )
    from strategy.selection_track_batch import (
        _apply_sel_track_a_batch_v2,
        _apply_sel_track_b_batch_v2,
        _apply_sel_track_c_batch_latest,
    )

    min_억 = float(settings.get("min_trde_val", 40.0))
    conv = float(settings.get("ma_convergence", 3.0)) / 100.0
    # 프로덕션과 동일: Valid 는 항상 계산 (감시 토글과 무관)
    _apply_sel_track_a_batch_v2(df_all, settings, min_억, conv)
    _apply_sel_track_b_batch_v2(df_all, settings, with_b_debug=False)
    _apply_sel_track_c_batch_latest(df_all, settings)
    df_all["Valid"] = df_all[["Valid_A", "Valid_B", "Valid_C"]].fillna(False).any(axis=1)
    df_all["Score"] = df_all[["Score_A", "Score_B", "Score_C"]].fillna(0.0).max(axis=1)


def _sel_settings() -> dict:
    return {
        "min_trde_val": 1.0,
        "ma_convergence": 3.0,
        "vol_lookback": 5,
        "vol_ratio": 1.2,
        "macd_fast_daily": 12,
        "macd_slow_daily": 26,
        "macd_sig_daily": 9,
        "use_sel_track_a": True,
        "use_sel_track_b": True,
        "use_sel_track_c": False,
        "sel_a_require_vol_burst": False,
        "sel_b_require_vol_burst": False,
        "sel_a_require_sel_rsi_band": False,
        "sel_b_require_sel_rsi_band": False,
        "sel_a_rs_rank_min": 0.0,
        "sel_a_rs_rank_max": 1.0,
        "sel_b_rs_rank_min": 0.0,
        "sel_b_rs_rank_max": 1.0,
        "sel_a_pb_min_pct": 0.0,
        "sel_a_pb_max_pct": 100.0,
        "sel_a_atr_pct_min": 0.0,
        "sel_a_atr_pct_max": 1.0,
        "sel_b_hi_min_pct": 0.0,
        "sel_b_hi_max_pct": 100.0,
        "sel_b_atr_pct_min": 0.0,
        "sel_b_atr_pct_max": 1.0,
        "sel_b_min_trde_val": 1.0,
        "bt_universe_indicator_lookback_days": 120,
    }


class TestSlidingUniverseScoreParity(unittest.TestCase):
    def test_score_matches_legacy_groupby_path(self) -> None:
        settings = _sel_settings()
        df = _synth_ohlcv(
            [f"{i:06d}" for i in range(1, 9)],
            "2025-01-02",
            "2025-08-29",
        )
        a = df.copy()
        b = df.copy()
        _legacy_score_ohlcv(a, settings)
        _score_dynamic_universe_daily_ohlcv(b, settings, with_b_debug=False)
        for col in ("RSI", "OSC", "MACD", "MA20", "MA60", "AvgVal5", "VolRatio"):
            s1 = a[col].astype(float)
            s2 = b[col].astype(float)
            both = s1.notna() & s2.notna()
            self.assertTrue(bool(both.any()), msg=col)
            np.testing.assert_allclose(
                s1.loc[both].to_numpy(),
                s2.loc[both].to_numpy(),
                rtol=1e-10,
                atol=1e-9,
                err_msg=col,
            )
        for col in ("Valid_A", "Valid_B", "Score_A", "Score_B"):
            pd.testing.assert_series_equal(
                a[col].reset_index(drop=True),
                b[col].reset_index(drop=True),
                check_names=False,
                obj=col,
            )

    def test_day_only_resync_matches_full_window(self) -> None:
        """당일만 resync ≡ 전체창 resync 후 당일 슬라이스."""
        settings = _sel_settings()
        codes = [f"{i:06d}" for i in range(1, 21)]
        univ: Set[str] = set(codes[:15])
        df = _synth_ohlcv(codes, "2025-01-02", "2025-08-29")
        d_str = "2025-08-29"
        d_ts = pd.Timestamp(d_str)
        lookback = 120
        left = d_ts - timedelta(days=lookback)
        win = df.loc[(df["time"] >= left) & (df["time"] <= d_ts + timedelta(hours=23))].copy()
        win.sort_values(["code", "time"], kind="mergesort", inplace=True)
        win.reset_index(drop=True, inplace=True)

        full = win.copy()
        _score_dynamic_universe_daily_ohlcv(full, settings, with_b_debug=False)

        def _gp(_dt: object) -> List[str]:
            return list(univ)

        _resync_batch_valid_to_period_universe(full, _gp, settings)
        day_full = full.loc[full["time"].dt.normalize() == d_ts].copy()
        day_full = day_full.loc[
            day_full["code"].astype(str).str.zfill(6).isin(univ)
        ].sort_values("code")

        day_only = win.loc[win["time"].dt.normalize() == d_ts].copy()
        # 지표는 창 전체 스코어 결과를 당일에 복사한 것과 같아야 하므로
        # 동일 win 스코어 후 당일 추출·resync
        scored = win.copy()
        _score_dynamic_universe_daily_ohlcv(scored, settings, with_b_debug=False)
        day_only = scored.loc[scored["time"].dt.normalize() == d_ts].copy()
        _resync_batch_valid_to_period_universe(day_only, _gp, settings)
        day_only = day_only.loc[
            day_only["code"].astype(str).str.zfill(6).isin(univ)
        ].sort_values("code")

        for col in ("Valid_A", "Valid_B", "Score_A", "Score_B", "RSI", "OSC"):
            pd.testing.assert_series_equal(
                day_full[col].reset_index(drop=True),
                day_only[col].reset_index(drop=True),
                check_names=False,
                obj=col,
            )

    def test_same_eval_day_invariant_to_panel_length(self) -> None:
        """같은 D·[D-N,D] 이면 패널이 길어도 당일 Valid/Score 동일."""
        settings = _sel_settings()
        codes = [f"{i:06d}" for i in range(1, 12)]
        univ = set(codes)
        d_str = "2025-06-30"
        d_ts = pd.Timestamp(d_str)
        lookback = 120
        left = d_ts - timedelta(days=lookback)

        short_start = "2024-12-01"
        long = _synth_ohlcv(codes, "2024-01-02", "2025-06-30", seed=3)
        # 동일 시세의 짧은 패널 (lookback 창 안 OHLCV 는 long 과 일치)
        short = long.loc[long["time"] >= pd.Timestamp(short_start)].copy()
        short.reset_index(drop=True, inplace=True)

        def _day_snap(df: pd.DataFrame) -> pd.DataFrame:
            ordered = df.sort_values("time", kind="mergesort").reset_index(drop=True)
            ts = pd.DatetimeIndex(pd.to_datetime(ordered["time"])).asi8
            li = int(np.searchsorted(ts, pd.Timestamp(left).value, side="left"))
            ri = int(
                np.searchsorted(
                    ts,
                    (d_ts + timedelta(hours=23, minutes=59, seconds=59)).value,
                    side="right",
                )
            )
            got = _score_one_sliding_day(
                ordered,
                li=li,
                ri=ri,
                d_str=d_str,
                d_ts=d_ts,
                settings=settings,
                univ_set=univ,
            )
            assert got is not None
            return got[1].sort_values("code").reset_index(drop=True)

        a = _day_snap(short)
        b = _day_snap(long)
        self.assertEqual(list(a["code"]), list(b["code"]))
        for col in ("Valid_A", "Valid_B", "Score_A", "Score_B"):
            pd.testing.assert_series_equal(a[col], b[col], check_names=False, obj=col)

    def test_oneshot_full_series_breaks_asof_parity(self) -> None:
        """전체 시계열 1회 EWM → 일자 슬라이스는 [D-N,D] as-of 와 다를 수 있음(회귀 고정)."""
        settings = _sel_settings()
        codes = [f"{i:06d}" for i in range(1, 8)]
        d_str = "2025-06-30"
        d_ts = pd.Timestamp(d_str)
        lookback = 120
        left = d_ts - timedelta(days=lookback)
        df = _synth_ohlcv(codes, "2024-01-02", "2025-06-30", seed=11)

        win = df.loc[(df["time"] >= left) & (df["time"] <= d_ts)].copy()
        asof = win.copy()
        _score_dynamic_universe_daily_ohlcv(asof, settings, with_b_debug=False)
        asof_day = asof.loc[asof["time"].dt.normalize() == d_ts].sort_values("code")

        oneshot = df.copy()
        _score_dynamic_universe_daily_ohlcv(oneshot, settings, with_b_debug=False)
        one_day = oneshot.loc[oneshot["time"].dt.normalize() == d_ts].sort_values("code")

        # EWM as-of 창이 lookback보다 길면 oneshot≠슬라이딩이 되는 것이 정상
        osc_diff = float(
            np.nanmax(
                np.abs(
                    asof_day["OSC"].to_numpy(dtype=float)
                    - one_day["OSC"].to_numpy(dtype=float)
                )
            )
        )
        rsi_diff = float(
            np.nanmax(
                np.abs(
                    asof_day["RSI"].to_numpy(dtype=float)
                    - one_day["RSI"].to_numpy(dtype=float)
                )
            )
        )
        self.assertTrue(
            osc_diff > 1e-8 or rsi_diff > 1e-8,
            msg=f"oneshot이 as-of와 같으면 안 됨 (osc={osc_diff}, rsi={rsi_diff})",
        )


if __name__ == "__main__":
    unittest.main()
