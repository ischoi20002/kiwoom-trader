# -*- coding: utf-8 -*-
"""marcap 연도 parquet 지연 시 자동 재다운로드 (백테스트 경로) 테스트."""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, List
from unittest import mock

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import core.marcap_universe as mu  # noqa: E402


def _write_year(path: Path, last_day: str) -> None:
    """최신 Date 가 last_day 인 최소 marcap parquet 작성."""
    days = pd.bdate_range(end=last_day, periods=3)
    rows = [
        {"Date": d, "Code": "005930", "Name": "삼성전자", "Marcap": 1e15, "MarketId": "STK"}
        for d in days
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)


def _set_age_hours(path: Path, hours: float) -> None:
    ts = time.time() - hours * 3600.0
    os.utime(path, (ts, ts))


class MarcapLagRefreshTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.year = 2026
        self.path = self.dir / f"marcap-{self.year}.parquet"
        self._p_dir = mock.patch.object(mu, "_MARCAP_CACHE_DIR", self.dir)
        self._p_dir.start()
        mu._year_df_cache.clear()
        mu._lag_refresh_attempted.clear()
        self.download_calls: List[Any] = []

    def tearDown(self) -> None:
        self._p_dir.stop()
        mu._year_df_cache.clear()
        mu._lag_refresh_attempted.clear()
        self._tmp.cleanup()

    def _fake_ensure(self, new_last_day: str):
        """force 다운로드를 흉내: 파일을 새 최신일로 덮어쓴다."""
        real = mu.ensure_marcap_year

        def _impl(year: int, **kw: Any):
            if kw.get("force"):
                self.download_calls.append((year, kw))
                _write_year(self.path, new_last_day)
                return self.path
            return real(year, **{k: v for k, v in kw.items() if k != "force"})

        return mock.patch.object(mu, "ensure_marcap_year", side_effect=_impl)

    def test_lagging_old_file_redownloads(self) -> None:
        """로컬 09-23 / as-of 10-01 / 파일 4일 경과 → 재다운로드 후 09-29 반영."""
        _write_year(self.path, "2026-09-23")
        _set_age_hours(self.path, 96)
        with self._fake_ensure("2026-09-29"):
            refreshed = mu.refresh_marcap_if_lagging("2026-10-01")
        self.assertTrue(refreshed)
        self.assertEqual(len(self.download_calls), 1)
        df = mu._load_marcap_year_df(self.year)
        self.assertEqual(pd.Timestamp(df["Date"].max()), pd.Timestamp("2026-09-29"))

    def test_load_marcap_day_finds_snapshot_after_refresh(self) -> None:
        """분기 첫 영업일 as-of 조회가 갱신 덕분에 빈 결과가 아니게 된다."""
        _write_year(self.path, "2026-09-23")
        _set_age_hours(self.path, 96)
        with self._fake_ensure("2026-09-29"):
            day, actual = mu.load_marcap_day("2026-10-01")
        self.assertFalse(day.empty)
        self.assertEqual(actual, pd.Timestamp("2026-09-29"))

    def test_recent_file_skips(self) -> None:
        """파일이 6시간 이내면 업스트림 지연으로 보고 재다운로드하지 않는다."""
        _write_year(self.path, "2026-09-23")
        _set_age_hours(self.path, 1)
        with self._fake_ensure("2026-09-29"):
            self.assertFalse(mu.refresh_marcap_if_lagging("2026-10-01"))
        self.assertEqual(self.download_calls, [])

    def test_covered_skips(self) -> None:
        """as-of 를 이미 덮으면 다운로드하지 않는다."""
        _write_year(self.path, "2026-09-29")
        _set_age_hours(self.path, 96)
        with self._fake_ensure("2026-09-30"):
            self.assertFalse(mu.refresh_marcap_if_lagging("2026-09-29"))
        self.assertEqual(self.download_calls, [])

    def test_once_per_process_per_year(self) -> None:
        """여전히 뒤처져도 같은 연도는 프로세스당 1회만 시도."""
        _write_year(self.path, "2026-09-23")
        _set_age_hours(self.path, 96)
        with self._fake_ensure("2026-09-24"):
            mu.refresh_marcap_if_lagging("2026-10-01")
            _set_age_hours(self.path, 96)
            self.assertFalse(mu.refresh_marcap_if_lagging("2026-10-02"))
        self.assertEqual(len(self.download_calls), 1)

    def test_force_redownload_keeps_file_on_failure(self) -> None:
        """force 다운로드 실패 시 기존 파일을 유지한다."""
        import urllib.error

        _write_year(self.path, "2026-09-23")
        before = self.path.read_bytes()
        with mock.patch.object(
            mu.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")
        ):
            self.assertIsNone(mu.ensure_marcap_year(self.year, force=True))
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
