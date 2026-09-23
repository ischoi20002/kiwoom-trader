# -*- coding: utf-8 -*-
"""동적 백테스트 준비 캐시 지문·라운드트립."""

from __future__ import annotations

import os
import tempfile
import unittest

from core.bt_prep_cache import (
    clear_memory_cache,
    get_ohlcv,
    get_signals,
    get_universe,
    ohlcv_fingerprint,
    put_ohlcv,
    put_signals,
    put_universe,
    signal_fingerprint,
    universe_fingerprint,
)


def _base_settings() -> dict:
    return {
        "sel_rsi_min": 35.0,
        "sel_rsi_max": 70.0,
        "max_slots_a": 20,
        "max_slots_b": 10,
        "max_slots_c": 15,
        "universe_index_choice": "KODEX200",
        "s1_a_take_profit": 5.0,
        "s1_a_stop_loss": 3.0,
        "buy_rsi_min": 30.0,
        "vol_surge": 2.0,
        "use_type_a": True,
        "use_type_c": False,
        "box_sel_max_adx": 22.0,
        "type_c_rsi_min": 35.0,
        "atr_period_a": 14,
        "use_synthetic_realtime": True,
        "bt_universe_indicator_lookback_days": 280,
    }


class TestBtPrepCache(unittest.TestCase):
    def setUp(self) -> None:
        clear_memory_cache()

    def tearDown(self) -> None:
        clear_memory_cache()

    def test_universe_fp_ignores_exit_params(self) -> None:
        a = _base_settings()
        b = dict(a)
        b["s1_a_take_profit"] = 9.5
        b["s2_stop_loss"] = 4.0
        fp1 = universe_fingerprint(
            a, sched_start="2024-06-03", end_date="2026-09-18", db_path="__missing__.db"
        )
        fp2 = universe_fingerprint(
            b, sched_start="2024-06-03", end_date="2026-09-18", db_path="__missing__.db"
        )
        self.assertEqual(fp1, fp2)

    def test_universe_fp_changes_with_selection(self) -> None:
        a = _base_settings()
        b = dict(a)
        b["sel_rsi_min"] = 40.0
        fp1 = universe_fingerprint(
            a, sched_start="2024-06-03", end_date="2026-09-18", db_path="__missing__.db"
        )
        fp2 = universe_fingerprint(
            b, sched_start="2024-06-03", end_date="2026-09-18", db_path="__missing__.db"
        )
        self.assertNotEqual(fp1, fp2)

    def test_universe_fp_changes_with_track_c(self) -> None:
        a = _base_settings()
        b = dict(a)
        b["use_type_c"] = True
        c = dict(a)
        c["box_sel_max_adx"] = 18.0
        kw = dict(sched_start="2024-06-03", end_date="2026-09-18", db_path="__missing__.db")
        fp_a = universe_fingerprint(a, **kw)
        self.assertNotEqual(fp_a, universe_fingerprint(b, **kw))
        self.assertNotEqual(fp_a, universe_fingerprint(c, **kw))

    def test_signal_fp_changes_with_buy_filter_not_tp(self) -> None:
        a = _base_settings()
        ohlcv_fp = ohlcv_fingerprint(
            "univ1", a, start_date="2024-06-03", end_date="2026-09-18"
        )
        sig1 = signal_fingerprint(ohlcv_fp, a)
        b = dict(a)
        b["s1_a_take_profit"] = 12.0
        self.assertEqual(sig1, signal_fingerprint(ohlcv_fp, b))
        c = dict(a)
        c["buy_rsi_min"] = 40.0
        self.assertNotEqual(sig1, signal_fingerprint(ohlcv_fp, c))
        d = dict(a)
        d["type_c_rsi_min"] = 40.0
        self.assertNotEqual(sig1, signal_fingerprint(ohlcv_fp, d))

    def test_universe_disk_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "schedule": {"2024-06-03": ["005930"]},
                "schedule_a": {"2024-06-03": ["005930"]},
                "schedule_b": {},
                "schedule_c": {"2024-06-03": ["000660"]},
                "schedule_c_meta": {"2024-06-03": {"000660": {"box_lower": 1.0}}},
                "transition_regime_by_day": {},
                "index_dd_block_by_day": {},
            }
            put_universe("abc123", payload, base_dir=tmp)
            clear_memory_cache()
            got = get_universe("abc123", base_dir=tmp)
            self.assertIsNotNone(got)
            assert got is not None
            self.assertEqual(got["schedule_c"]["2024-06-03"], ["000660"])
            cache_path = os.path.join(tmp, "_local_bt_prep_cache", "univ_abc123.pkl")
            self.assertTrue(os.path.isfile(cache_path))

    def test_ohlcv_and_signal_disk_roundtrip(self) -> None:
        """OHLCV·신호도 디스크에 남아 GUI 재시작 후에도 HIT 가능해야 한다."""
        import pandas as pd

        with tempfile.TemporaryDirectory() as tmp:
            ohlcv = {
                "005930": pd.DataFrame(
                    {"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5], "Volume": [100]}
                )
            }
            sig = {
                "005930": pd.DataFrame(
                    {"Close": [1.5], "Signal": [0], "Ready": [True]}
                )
            }
            put_ohlcv("ohlcv_fp1", ohlcv, base_dir=tmp)
            put_signals("sig_fp1", sig, base_dir=tmp)
            clear_memory_cache()
            got_o = get_ohlcv("ohlcv_fp1", base_dir=tmp)
            got_s = get_signals("sig_fp1", base_dir=tmp)
            self.assertIsNotNone(got_o)
            self.assertIsNotNone(got_s)
            assert got_o is not None and got_s is not None
            self.assertIn("005930", got_o)
            self.assertEqual(int(got_s["005930"]["Signal"].iloc[0]), 0)
            self.assertTrue(
                os.path.isfile(
                    os.path.join(tmp, "_local_bt_prep_cache", "ohlcv_ohlcv_fp1.pkl")
                )
            )
            self.assertTrue(
                os.path.isfile(
                    os.path.join(tmp, "_local_bt_prep_cache", "sig_sig_fp1.pkl")
                )
            )


if __name__ == "__main__":
    unittest.main()
