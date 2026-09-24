# -*- coding: utf-8 -*-
"""use_sel_track OFF여도 Valid 스코어는 계산되고, 감시 병합만 제외되는지 검증."""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from strategy.selection_dynamic_score import _score_dynamic_universe_daily_ohlcv
from strategy.selection_live_aux import _merge_dual_track_picks


def _toy_ohlcv(n_days: int = 80, n_codes: int = 8) -> pd.DataFrame:
    rows: list[dict] = []
    rng = np.random.default_rng(42)
    base = pd.Timestamp("2024-01-02")
    for ci in range(n_codes):
        code = f"{ci + 1:06d}"
        px = 10000.0 + ci * 500.0
        for d in range(n_days):
            ret = float(rng.normal(0.001, 0.015))
            px = max(1000.0, px * (1.0 + ret))
            hi = px * 1.01
            lo = px * 0.99
            vol = float(rng.integers(50_000, 200_000))
            rows.append(
                {
                    "code": code,
                    "time": base + pd.Timedelta(days=d),
                    "open": px,
                    "high": hi,
                    "low": lo,
                    "close": px,
                    "volume": vol,
                }
            )
    return pd.DataFrame(rows)


class TestSelTrackScoreAlways(unittest.TestCase):
    def test_score_computes_valid_b_when_track_b_monitor_off(self) -> None:
        """Track B 감시 OFF여도 Valid_B 열이 실제 조건으로 채워진다."""
        settings = {
            "min_trde_val": 0.01,
            "ma_convergence": 50.0,
            "vol_lookback": 5,
            "vol_ratio": 0.5,
            "sel_b_vol_ratio": 0.5,
            "sel_b_min_trde_val": 0.01,
            "use_sel_track_a": True,
            "use_sel_track_b": False,
            "use_sel_track_c": False,
            "macd_fast_daily": 12,
            "macd_slow_daily": 26,
            "macd_sig_daily": 9,
        }
        df = _toy_ohlcv()
        _score_dynamic_universe_daily_ohlcv(df, settings)
        self.assertIn("Valid_B", df.columns)
        self.assertIn("Score_B", df.columns)
        self.assertEqual(int(df["Valid_B"].notna().sum()), len(df))

    def test_merge_excludes_disabled_track_from_watchlist(self) -> None:
        """감시 OFF 트랙은 최종 감시 목록에서 빠진다."""
        rows = [
            {
                "Code": "000001",
                "Name": "A1",
                "_valid_a": True,
                "_score_a": 90.0,
                "_valid_b": True,
                "_score_b": 80.0,
                "_valid_c": False,
                "_score_c": 0.0,
            },
            {
                "Code": "000002",
                "Name": "B1",
                "_valid_a": False,
                "_score_a": 0.0,
                "_valid_b": True,
                "_score_b": 95.0,
                "_valid_c": False,
                "_score_c": 0.0,
            },
        ]
        settings = {
            "use_sel_track_a": True,
            "use_sel_track_b": False,
            "use_sel_track_c": False,
            "use_type_c": False,
            "max_slots_a": 20,
            "max_slots_b": 10,
            "max_slots_c": 15,
        }
        out = _merge_dual_track_picks(rows, settings, lambda _m: None)
        codes = {str(r["Code"]).zfill(6) for r in out}
        self.assertIn("000001", codes)
        self.assertNotIn("000002", codes)
        self.assertTrue(all("B" not in str(r.get("SelTrack", "")) for r in out))


if __name__ == "__main__":
    unittest.main()
