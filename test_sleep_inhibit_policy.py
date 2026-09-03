# -*- coding: utf-8 -*-
"""최대절전 억제 정책 단위 테스트."""
from __future__ import annotations

import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from utils.sleep_inhibit_policy import should_prevent_sleep


class _FakeCms:
    def __init__(
        self,
        *,
        armed: bool = True,
        morning_start: bool = False,
        trading_day: bool = True,
        eod_due: datetime | None = None,
    ) -> None:
        self.armed = armed
        self._morning_start = morning_start
        self._trading_day = trading_day
        self._eod_due = eod_due or datetime(2026, 9, 2, 16, 0, 0)

    def _should_morning_start(self, t: datetime) -> bool:
        return self._morning_start

    def _is_trading_day(self, day_d: date) -> bool:
        return self._trading_day

    def _eod_due_datetime(self, day_d: date) -> datetime:
        return self._eod_due


def _app(
    *,
    prevent: bool = True,
    allow_after_eod: bool = True,
    monitoring: bool = False,
    cms: _FakeCms | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        settings={
            "prevent_sleep_while_running": prevent,
            "prevent_sleep_allow_after_eod": allow_after_eod,
        },
        trading_controller=SimpleNamespace(is_monitoring=monitoring),
        continuous_monitor_service=cms if cms is not None else _FakeCms(),
    )


class TestSleepInhibitPolicy(unittest.TestCase):
    def test_master_off(self) -> None:
        app = _app(prevent=False, allow_after_eod=True)
        self.assertFalse(should_prevent_sleep(app))

    def test_always_on_without_eod_option(self) -> None:
        app = _app(allow_after_eod=False, monitoring=False)
        self.assertTrue(
            should_prevent_sleep(app, now=datetime(2026, 9, 2, 20, 0, 0))
        )

    def test_monitoring_on(self) -> None:
        app = _app(monitoring=True)
        self.assertTrue(should_prevent_sleep(app, now=datetime(2026, 9, 2, 20, 0, 0)))

    def test_after_eod_when_idle(self) -> None:
        cms = _FakeCms(eod_due=datetime(2026, 9, 2, 16, 0, 0))
        app = _app(cms=cms)
        self.assertFalse(
            should_prevent_sleep(app, now=datetime(2026, 9, 2, 16, 5, 0))
        )

    def test_before_eod_when_idle(self) -> None:
        cms = _FakeCms(eod_due=datetime(2026, 9, 2, 16, 0, 0))
        app = _app(cms=cms)
        self.assertFalse(
            should_prevent_sleep(app, now=datetime(2026, 9, 2, 14, 0, 0))
        )

    def test_morning_window(self) -> None:
        cms = _FakeCms(morning_start=True)
        app = _app(cms=cms)
        self.assertTrue(
            should_prevent_sleep(app, now=datetime(2026, 9, 3, 8, 36, 0))
        )

    def test_weekend_idle(self) -> None:
        cms = _FakeCms(trading_day=False)
        app = _app(cms=cms)
        with patch.object(cms, "_is_trading_day", return_value=False):
            self.assertFalse(
                should_prevent_sleep(app, now=datetime(2026, 9, 5, 12, 0, 0))
            )


if __name__ == "__main__":
    unittest.main()
