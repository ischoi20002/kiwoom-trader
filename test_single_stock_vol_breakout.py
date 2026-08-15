# -*- coding: utf-8 -*-
"""단일 종목 시뮬 — 변동성 돌파 단위 테스트."""
from __future__ import annotations

import unittest
from datetime import datetime

import pandas as pd

from single_stock_sim import (
    DEFAULT_VOL_BREAKOUT_K,
    STRATEGY_S1_S2,
    STRATEGY_VOL_BREAKOUT,
    SingleStockSimError,
    _first_breakout_fill,
    _normalize_strategy,
    _parse_vol_k,
    simulate_vol_breakout_trades,
)


def _bars_for_day(
    day: str,
    ohlc: list[tuple[int, int, float, float, float, float]],
) -> list[dict]:
    """하루치 15분봉 행을 만든다. ohlc=(hour, minute, open, high, low, close)."""
    rows: list[dict] = []
    for hh, mm, o, h, l, c in ohlc:
        ts = datetime.strptime(f"{day} {hh:02d}:{mm:02d}", "%Y-%m-%d %H:%M")
        rows.append({"Date": ts, "Open": o, "High": h, "Low": l, "Close": c})
    return rows


def _df_from_days(*day_rows: list[dict]) -> pd.DataFrame:
    rows: list[dict] = []
    for part in day_rows:
        rows.extend(part)
    df = pd.DataFrame(rows)
    df = df.set_index(pd.to_datetime(df["Date"])).drop(columns=["Date"]).sort_index()
    return df


class VolBreakoutHelpersTest(unittest.TestCase):
    def test_normalize_strategy_aliases(self) -> None:
        self.assertEqual(_normalize_strategy("s1_s2"), STRATEGY_S1_S2)
        self.assertEqual(_normalize_strategy("vol_breakout"), STRATEGY_VOL_BREAKOUT)
        self.assertEqual(_normalize_strategy("변동성 돌파"), STRATEGY_VOL_BREAKOUT)
        self.assertEqual(_normalize_strategy(None), STRATEGY_S1_S2)

    def test_parse_vol_k_range(self) -> None:
        self.assertAlmostEqual(_parse_vol_k("0.5"), 0.5)
        self.assertAlmostEqual(_parse_vol_k(DEFAULT_VOL_BREAKOUT_K), 0.5)
        with self.assertRaises(SingleStockSimError):
            _parse_vol_k("abc")
        with self.assertRaises(SingleStockSimError):
            _parse_vol_k(0.0)
        with self.assertRaises(SingleStockSimError):
            _parse_vol_k(3.0)

    def test_first_breakout_fill_target_and_gap(self) -> None:
        day = _df_from_days(
            _bars_for_day(
                "2026-03-11",
                [
                    (9, 0, 10000, 10100, 9990, 10050),
                    (9, 15, 10050, 10500, 10040, 10480),
                ],
            )
        )
        hit = _first_breakout_fill(day, 10450.0)
        self.assertIsNotNone(hit)
        assert hit is not None
        ts, px = hit
        self.assertEqual(ts.hour, 9)
        self.assertEqual(ts.minute, 15)
        self.assertAlmostEqual(px, 10450.0)

        gap = _first_breakout_fill(day, 9900.0)
        self.assertIsNotNone(gap)
        assert gap is not None
        self.assertAlmostEqual(gap[1], 10000.0)


class VolBreakoutSimTest(unittest.TestCase):
    def test_simulate_touch_same_day_exit(self) -> None:
        """전일 레인지 500, K=0.5 → 목표가 시가+250. 터치 후 종가 청산."""
        d1 = _bars_for_day(
            "2026-03-10",
            [
                (9, 0, 10000, 10500, 10000, 10200),
                (15, 30, 10200, 10300, 10150, 10200),
            ],
        )
        d2 = _bars_for_day(
            "2026-03-11",
            [
                (9, 0, 10200, 10300, 10180, 10250),
                (10, 0, 10250, 10500, 10240, 10480),
                (15, 30, 10480, 10620, 10450, 10600),
            ],
        )
        df = _df_from_days(d1, d2)
        trades, summary = simulate_vol_breakout_trades(
            df,
            "2026-03-11",
            "2026-03-11",
            0.5,
            initial_capital=10_000_000,
            equity_pct=100.0,
            buy_fee_rate=0.0,
            sell_fee_rate=0.0,
        )
        self.assertEqual(summary["trades"], 1)
        self.assertEqual(len(trades), 2)
        buy, sell = trades
        self.assertEqual(buy["Action"], "BUY_1ST")
        self.assertAlmostEqual(buy["Price"], 10450.0)
        self.assertEqual(buy["Sig_Type"], "VOL_BO")
        self.assertEqual(sell["Action"], "SELL")
        self.assertAlmostEqual(sell["Price"], 10600.0)
        self.assertAlmostEqual(sell["Return"], (10600.0 / 10450.0 - 1.0) * 100.0)
        self.assertAlmostEqual(summary["win_rate"], 100.0)

    def test_simulate_no_touch_skips(self) -> None:
        d1 = _bars_for_day(
            "2026-03-10",
            [(9, 0, 10000, 10500, 10000, 10200), (15, 30, 10200, 10300, 10150, 10200)],
        )
        d2 = _bars_for_day(
            "2026-03-11",
            [(9, 0, 10200, 10300, 10100, 10220), (15, 30, 10220, 10350, 10200, 10300)],
        )
        df = _df_from_days(d1, d2)
        trades, summary = simulate_vol_breakout_trades(
            df, "2026-03-11", "2026-03-11", 0.5,
            buy_fee_rate=0.0, sell_fee_rate=0.0,
        )
        self.assertEqual(trades, [])
        self.assertEqual(summary["trades"], 0)

    def test_simulate_uses_previous_trading_day_not_calendar(self) -> None:
        """주말 등으로 달력상 전날이 없어도 직전 거래일 레인지를 쓴다."""
        d1 = _bars_for_day(
            "2026-03-10",
            [(9, 0, 10000, 11000, 10000, 10500), (15, 30, 10500, 10600, 10400, 10500)],
        )
        d2 = _bars_for_day(
            "2026-03-12",
            [
                (9, 0, 10500, 10600, 10480, 10520),
                (11, 0, 10520, 11200, 10500, 11100),
                (15, 30, 11100, 11150, 11050, 11120),
            ],
        )
        df = _df_from_days(d1, d2)
        trades, summary = simulate_vol_breakout_trades(
            df, "2026-03-12", "2026-03-12", 0.5,
            buy_fee_rate=0.0, sell_fee_rate=0.0, equity_pct=100.0,
        )
        self.assertEqual(summary["trades"], 1)
        self.assertAlmostEqual(trades[0]["Price"], 11000.0)


if __name__ == "__main__":
    unittest.main()
