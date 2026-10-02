# -*- coding: utf-8 -*-
"""분봉 수정주가 변경 감지·재수신 교체 테스트."""
from __future__ import annotations

import gc
import os
import sqlite3
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Tuple
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import data_manager.minute_adjust_refetch as mar  # noqa: E402

CODE = "010120"


def _bar(t: str, c: float) -> Dict[str, Any]:
    return {"time": t, "open": c, "high": c, "low": c, "close": c, "volume": 10}


def _times(n: int, day: str = "2026-04-10") -> List[str]:
    return [f"{day} 09:{i:02d}:00" for i in range(n)]


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = os.path.join(self._tmp.name, "t.db")
        with sqlite3.connect(self.db) as conn:
            for t in ("bars_1m", "bars_15m"):
                conn.execute(
                    f"CREATE TABLE {t} (code TEXT, datetime TEXT, open INTEGER, high INTEGER, low INTEGER, "
                    "close INTEGER, volume INTEGER, PRIMARY KEY (code, datetime))"
                )
            conn.execute(
                "CREATE TABLE sync_meta (code TEXT PRIMARY KEY, last_sync_dt TEXT, earliest_sync_dt TEXT, status TEXT)"
            )

    def tearDown(self) -> None:
        gc.collect()
        self._tmp.cleanup()

    def _put(self, table: str, bars: List[Dict[str, Any]]) -> None:
        conn = sqlite3.connect(self.db)
        try:
            conn.executemany(
                f"INSERT OR REPLACE INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(CODE, b["time"], b["open"], b["high"], b["low"], b["close"], b["volume"]) for b in bars],
            )
            conn.commit()
        finally:
            conn.close()

    def _closes(self, table: str, db: str = "") -> Dict[str, int]:
        conn = sqlite3.connect(db or self.db)
        try:
            return dict(conn.execute(
                f"SELECT datetime, close FROM {table} WHERE code=? ORDER BY datetime", (CODE,)
            ).fetchall())
        finally:
            conn.close()


class TestDetectOverlap(_Base):
    def test_split_detected(self) -> None:
        ts = _times(10)
        self._put("bars_1m", [_bar(t, 880000) for t in ts])
        api = [_bar(t, 176000) for t in ts]
        mm = mar.detect_minute_overlap_mismatch(self.db, "bars_1m", CODE, api, ts[-1])
        self.assertIsNotNone(mm)
        self.assertEqual(mm["n_mismatch"], 10)

    def test_identical_is_none(self) -> None:
        ts = _times(10)
        self._put("bars_1m", [_bar(t, 176000) for t in ts])
        self.assertIsNone(mar.detect_minute_overlap_mismatch(self.db, "bars_1m", CODE, [_bar(t, 176000) for t in ts]))

    def test_few_corrected_bars_ignored(self) -> None:
        ts = _times(10)
        self._put("bars_1m", [_bar(t, 176000) for t in ts])
        api = [_bar(t, 176000 if i > 1 else 180000) for i, t in enumerate(ts)]
        self.assertIsNone(mar.detect_minute_overlap_mismatch(self.db, "bars_1m", CODE, api))

    def test_upto_excludes_new_bars(self) -> None:
        ts = _times(10)
        self._put("bars_1m", [_bar(t, 176000) for t in ts[:3]])
        api = [_bar(t, 999) for t in ts]
        # DB 와 겹치는 봉이 3개뿐이라 최소 비교 개수 미달
        self.assertIsNone(mar.detect_minute_overlap_mismatch(self.db, "bars_1m", CODE, api, ts[2]))


class TestFetchPages(unittest.TestCase):
    def _fetcher(self, pages: List[Any]) -> Tuple[Any, List[str]]:
        calls: List[str] = []

        def fetch(code: str, token: str, nk: str, tic_scope: str = "1") -> Tuple[Any, str]:
            calls.append(nk)
            idx = int(nk or 0)
            raw = pages[idx]
            return raw, (str(idx + 1) if idx + 1 < len(pages) and raw else "")

        return fetch, calls

    def test_paginates_and_sorts(self) -> None:
        fetch, _ = self._fetcher([[_bar("2026-04-10 09:02:00", 3)], [_bar("2026-04-10 09:01:00", 2)]])
        with mock.patch.object(mar.time, "sleep"):
            got = mar.fetch_all_minute_pages(fetch, lambda r: r, CODE, "tk", "1")
        self.assertEqual([b["time"] for b in got], ["2026-04-10 09:01:00", "2026-04-10 09:02:00"])

    def test_first_page_empty_is_no_data(self) -> None:
        fetch, _ = self._fetcher([[]])
        self.assertEqual(mar.fetch_all_minute_pages(fetch, lambda r: r, CODE, "tk", "1"), [])

    def test_mid_failure_returns_none(self) -> None:
        def fetch(code: str, token: str, nk: str, tic_scope: str = "1") -> Tuple[Any, str]:
            return ([_bar("2026-04-10 09:02:00", 3)], "1") if not nk else (None, "")

        with mock.patch.object(mar.time, "sleep"):
            self.assertIsNone(mar.fetch_all_minute_pages(fetch, lambda r: r, CODE, "tk", "1"))

    def test_cap_filters_future_bars(self) -> None:
        fetch, _ = self._fetcher([[_bar("2026-04-10 10:00:00", 3), _bar("2026-04-10 09:00:00", 2)]])
        got = mar.fetch_all_minute_pages(fetch, lambda r: r, CODE, "tk", "1", cap="2026-04-10 09:30:00")
        self.assertEqual(len(got), 1)


class TestReplaceRange(_Base):
    def test_replace_with_backup_and_meta(self) -> None:
        ts = _times(6)
        self._put("bars_1m", [_bar(t, 880000) for t in ts] + [_bar("2026-04-09 09:00:00", 870000)])
        new = [_bar(t, 176000) for t in ts[1:]]  # 09:00 은 API 에 없음 → 범위 밖이라 유지
        bk = os.path.join(self._tmp.name, "bk.db")
        deleted, saved, changed = mar.replace_minute_range(self.db, "bars_1m", CODE, new, backup_db=bk)
        self.assertEqual((deleted, saved, changed), (5, 5, 5))
        c = self._closes("bars_1m")
        self.assertEqual(c[ts[0]], 880000)
        self.assertEqual(c[ts[-1]], 176000)
        self.assertEqual(c["2026-04-09 09:00:00"], 870000)
        self.assertEqual(len(self._closes("bars_1m", bk)), 5)
        conn = sqlite3.connect(self.db)
        try:
            meta = conn.execute("SELECT last_sync_dt FROM sync_meta WHERE code=?", (CODE,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(meta[0], ts[-1])

    def test_rejects_unknown_table(self) -> None:
        with self.assertRaises(ValueError):
            mar.replace_minute_range(self.db, "bars_1d", CODE, [_bar("2026-04-10 09:00:00", 1)])


class TestHarvestHook(_Base):
    def _hs(self) -> Any:
        from harvest_service import HarvestService

        hs = object.__new__(HarvestService)
        hs.db = mock.Mock(db_path=self.db)
        hs.running = True
        hs._minute_adj_enabled = True
        hs.log = lambda m: None
        return hs

    def test_refetch_when_overlap_changed(self) -> None:
        ts = _times(10)
        self._put("bars_1m", [_bar(t, 880000) for t in ts])
        hs = self._hs()
        rec = {"bars_1m": {"status": "replaced", "n": 900, "changed": 10}, "bars_15m": {"status": "replaced"}}
        with mock.patch("harvest_service.refetch_and_replace_code", return_value=rec) as m:
            n = hs._minute_adj_refetch_if_changed(CODE, "tk", [_bar(t, 176000) for t in ts], ts[-1], None)
        self.assertEqual(n, 900)
        m.assert_called_once()

    def test_no_refetch_when_same_or_disabled(self) -> None:
        ts = _times(10)
        self._put("bars_1m", [_bar(t, 176000) for t in ts])
        hs = self._hs()
        with mock.patch("harvest_service.refetch_and_replace_code") as m:
            self.assertIsNone(hs._minute_adj_refetch_if_changed(CODE, "tk", [_bar(t, 176000) for t in ts], ts[-1], None))
            hs._minute_adj_enabled = False
            self.assertIsNone(hs._minute_adj_refetch_if_changed(CODE, "tk", [_bar(t, 1) for t in ts], ts[-1], None))
        m.assert_not_called()

    def test_failed_refetch_falls_back(self) -> None:
        ts = _times(10)
        self._put("bars_1m", [_bar(t, 880000) for t in ts])
        hs = self._hs()
        rec = {"bars_1m": {"status": "fetch_failed"}, "bars_15m": {"status": "fetch_failed"}}
        with mock.patch("harvest_service.refetch_and_replace_code", return_value=rec):
            self.assertIsNone(hs._minute_adj_refetch_if_changed(CODE, "tk", [_bar(t, 176000) for t in ts], ts[-1], None))


if __name__ == "__main__":
    unittest.main()
