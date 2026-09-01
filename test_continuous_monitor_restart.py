# -*- coding: utf-8 -*-
"""연속감시 EOD 재실행 방지 단위 테스트."""
from __future__ import annotations

import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import patch

from core.continuous_monitor_service import ContinuousMonitorService


class TestContinuousMonitorEodGuard(unittest.TestCase):
    def test_mark_eod_cycle_consumed_blocks_tick(self) -> None:
        app = SimpleNamespace(
            trading_controller=SimpleNamespace(is_monitoring=True),
        )
        cms = ContinuousMonitorService(app, log_func=lambda *_a, **_k: None)
        cms.armed = True
        trade_day = date(2026, 9, 1)
        with patch.object(cms, "_resolve_eod_trading_day", return_value=trade_day):
            with patch.object(cms, "_begin_eod_cycle") as mock_begin:
                cms.tick()
                self.assertTrue(mock_begin.called)
                cms.mark_eod_cycle_consumed(trade_day)
                mock_begin.reset_mock()
                cms.tick()
                mock_begin.assert_not_called()


if __name__ == "__main__":
    unittest.main()
