# -*- coding: utf-8 -*-
"""볼린저 Z·과열 차단 필터 단위 테스트."""
from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from strategy.indicators import attach_bollinger_z
from strategy.signals import get_buy_signals_detailed


def _flat_then_spike() -> pd.DataFrame:
    """20봉 횡보 후 급등 → Z가 +2 이상으로 나가게 만든다."""
    n = 30
    close = np.full(n, 10000.0)
    close[-3:] = [10400.0, 10800.0, 11200.0]
    idx = pd.date_range("2026-03-10 09:00", periods=n, freq="15min")
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + 10,
            "Low": close - 10,
            "Close": close,
            "Volume": np.full(n, 1000.0),
        },
        index=idx,
    )


class BollingerZTest(unittest.TestCase):
    def test_z_near_zero_on_flat(self) -> None:
        df = _flat_then_spike()
        # 완전 평탄이면 σ=0 → Z는 NaN (0으로 나누지 않음)
        attach_bollinger_z(df, 20)
        self.assertTrue(np.isnan(float(df["BB_Z"].iloc[19])))
        # 작은 흔들림이 있으면 횡보 구간의 |Z|는 작다
        df2 = df.copy()
        df2.loc[df2.index[:20], "Close"] = np.linspace(9990, 10010, 20)
        attach_bollinger_z(df2, 20)
        z_flat = float(df2["BB_Z"].iloc[19])
        self.assertTrue(np.isfinite(z_flat))
        self.assertLess(abs(z_flat), 2.0)

    def test_z_exceeds_two_on_spike(self) -> None:
        df = _flat_then_spike()
        attach_bollinger_z(df, 20)
        z_last = float(df["BB_Z"].iloc[-1])
        self.assertGreater(z_last, 2.0)


class BbBlockSignalTest(unittest.TestCase):
    def _base_df(self) -> pd.DataFrame:
        n = 80
        idx = pd.date_range("2026-03-10 09:00", periods=n, freq="15min")
        close = np.linspace(10000, 10100, n)
        df = pd.DataFrame(
            {
                "Open": close,
                "High": close + 20,
                "Low": close - 20,
                "Close": close,
                "Volume": np.full(n, 1_000_000.0),
                "MA5": close,
                "MA20": close - 50,
                "MA60": close - 80,
                "RSI": np.full(n, 48.0),
                "RSI_Sig": np.full(n, 45.0),
                "MACD": np.linspace(-1, 1, n),
                "MACD_Sig": np.linspace(-1.2, 0.8, n),
                "OSC": np.full(n, 0.2),
                "Prev_Day_Vol": np.full(n, 100.0),
                "Prev_Day_Vol_B": np.full(n, 100.0),
                "BB_Z": np.full(n, 0.5),
            },
            index=idx,
        )
        return df

    def test_a_2s_blocks_only_hot_a(self) -> None:
        df = self._base_df()
        df.loc[df.index[-1], "BB_Z"] = 2.4
        # 필터 OFF면 신호가 있을 수도/없을 수도 있음 — ON일 때 마지막 봉 Type A는 꺼져야 함
        st_on = {
            "use_type_a": True,
            "use_type_b": False,
            "use_gc": False,
            "use_vol_surge": False,
            "use_ma60_safe": False,
            "use_rsi_signal": False,
            "use_rsi_sig_filter": False,
            "use_macd_signal": False,
            "use_rsi_min": False,
            "buy_rsi_max_a": 80.0,
            "use_bb_block_a_2s": True,
            "bb_block_a_z": 2.0,
        }
        out = get_buy_signals_detailed(df, st_on)
        last = out.iloc[-1]
        if bool(last.get("Signal")):
            self.assertNotEqual(str(last.get("Sig_Type")), "TYPE_A")


if __name__ == "__main__":
    unittest.main()
