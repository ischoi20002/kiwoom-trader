# -*- coding: utf-8 -*-
"""bars_1d 수정주가 혼재 감지·전체 재저장 테스트."""
from __future__ import annotations

import gc
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from typing import Dict, List, Tuple

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_manager.fdr_daily_sync import FdrDailySyncMixin  # noqa: E402

DAYS = ["2026-09-22", "2026-09-23", "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"]
RAW = [1392000, 1373000, 1390000, 1384000, 1383268, 1417007]  # 09-30 이전 수정 전 주가
ADJ = [1381284, 1362430, 1379299, 1373345, 1383268, 1417007]  # FDR 소급 수정주가


def _frame(days: List[str], closes: List[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {"datetime": days, "open": closes, "high": closes, "low": closes, "close": closes, "volume": 100}
    )


class _FakeDM(FdrDailySyncMixin):
    """DataManagerV2 의 bars_1d 저장부만 흉내 낸 테스트용 클래스."""

    def __init__(self, db_path: str, fdr: pd.DataFrame) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self.fdr = fdr
        self.fetch_calls: List[Tuple[str, str]] = []
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "CREATE TABLE bars_1d (code TEXT, datetime TEXT, open REAL, high REAL, low REAL, "
                "close REAL, volume INTEGER, PRIMARY KEY (code, datetime))"
            )
            conn.execute("CREATE TABLE sync_meta (code TEXT PRIMARY KEY, last_daily_sync TEXT)")

    def _get_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def fetch_fdr_daily_dataframe(self, code: str, start_day: str, end_day: str) -> pd.DataFrame:
        self.fetch_calls.append((start_day, end_day))
        d = self.fdr
        return d[(d["datetime"] >= start_day) & (d["datetime"] <= end_day)].copy()

    def save_daily_bars(self, code: str, df: pd.DataFrame) -> None:
        rows = [
            (code, r.datetime, r.open, r.high, r.low, r.close, int(r.volume))
            for r in df.itertuples(index=False)
        ]
        with self._get_connection() as conn:
            conn.executemany("INSERT OR REPLACE INTO bars_1d VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    def closes(self, code: str) -> Dict[str, float]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT datetime, close FROM bars_1d WHERE code = ? ORDER BY datetime", (code,)
            ).fetchall()
        return {d: c for d, c in rows}


class TestFdrDailyAdjustment(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = os.path.join(self._tmp.name, "t.db")

    def tearDown(self) -> None:
        # 실제 DataManagerV2 처럼 연결을 명시적으로 닫지 않으므로 GC 후 정리 (Windows 파일 잠금)
        gc.collect()
        self._tmp.cleanup()

    def _dm_with_db(self, db_days: List[str], db_closes: List[float], fdr: pd.DataFrame) -> _FakeDM:
        dm = _FakeDM(self.db, fdr)
        dm.save_daily_bars("207940", _frame(db_days, db_closes))
        return dm

    def test_detect_mismatch_on_overlap(self) -> None:
        dm = self._dm_with_db(DAYS[:5], RAW[:5], _frame(DAYS, ADJ))
        mm = dm.detect_daily_adjustment_mismatch("207940", _frame(DAYS, ADJ))
        self.assertIsNotNone(mm)
        self.assertEqual(mm["n_mismatch"], 4)
        self.assertLess(mm["date"], "2026-09-30")

    def test_no_mismatch_when_same(self) -> None:
        dm = self._dm_with_db(DAYS[:5], ADJ[:5], _frame(DAYS, ADJ))
        self.assertIsNone(dm.detect_daily_adjustment_mismatch("207940", _frame(DAYS, ADJ)))

    def test_one_won_rounding_is_not_mismatch(self) -> None:
        days = DAYS[:3]
        dm = self._dm_with_db(days, [1212, 1180, 1201], _frame(days, [1211, 1180, 1200]))
        self.assertIsNone(dm.detect_daily_adjustment_mismatch("207940", _frame(days, [1211, 1180, 1200])))

    def test_sync_range_resaves_full_history(self) -> None:
        # DB 는 09-22~09-30 (수정 전), 증분 동기화는 09-29 부터 → 겹친 09-29 불일치로 전체 재저장
        dm = self._dm_with_db(DAYS[:5], RAW[:5], _frame(DAYS, ADJ))
        self.assertTrue(dm.sync_daily_bars_from_fdr_range("207940", "2026-09-29", "2026-10-01"))
        self.assertEqual(list(dm.closes("207940").values()), [float(x) for x in ADJ])
        self.assertIn(("2026-09-22", "2026-10-01"), dm.fetch_calls)

    def test_sync_range_without_mismatch_is_incremental(self) -> None:
        dm = self._dm_with_db(DAYS[:5], ADJ[:5], _frame(DAYS, ADJ))
        self.assertTrue(dm.sync_daily_bars_from_fdr_range("207940", "2026-09-29", "2026-10-01"))
        self.assertEqual(dm.fetch_calls, [("2026-09-29", "2026-10-01")])
        self.assertEqual(dm.closes("207940")["2026-10-01"], 1417007.0)

    def test_full_resave_skipped_when_fdr_history_short(self) -> None:
        # FDR 이 DB 최초일까지 못 내려주면 겹친 구간은 덮어쓰지 않고 새 날짜만 저장 → 다음에 재감지
        dm = self._dm_with_db(DAYS[:5], RAW[:5], _frame(DAYS[3:], ADJ[3:]))
        dm.save_daily_bars("207940", _frame(["2026-09-01"], [1400000]))
        self.assertTrue(dm.sync_daily_bars_from_fdr_range("207940", "2026-09-29", "2026-10-01"))
        c = dm.closes("207940")
        self.assertEqual(c["2026-09-29"], 1384000.0)
        self.assertEqual(c["2026-10-01"], 1417007.0)

    def test_repair_dry_run_and_apply(self) -> None:
        dm = self._dm_with_db(DAYS, RAW, _frame(DAYS, ADJ))
        r = dm.repair_daily_adjustment_for_code("207940", dry_run=True)
        self.assertEqual(r["status"], "mismatch")
        self.assertEqual(dm.closes("207940")["2026-09-22"], 1392000.0)
        r = dm.repair_daily_adjustment_for_code("207940")
        self.assertEqual(r["status"], "repaired")
        self.assertEqual(dm.closes("207940")["2026-09-22"], 1381284.0)
        self.assertEqual(dm.repair_daily_adjustment_for_code("207940")["status"], "ok")


if __name__ == "__main__":
    unittest.main()
