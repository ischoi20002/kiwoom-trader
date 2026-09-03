# -*- coding: utf-8 -*-
"""Windows 최대절전 억제 유틸 단위 테스트."""
from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

from utils.sleep_inhibit import SleepInhibitor


class TestSleepInhibitor(unittest.TestCase):
    def test_non_windows_acquire_is_noop(self) -> None:
        logs: list[str] = []
        si = SleepInhibitor(logger=logs.append)
        with patch.object(sys, "platform", "linux"):
            self.assertFalse(si.acquire())
        self.assertFalse(si.active)
        self.assertTrue(any("Windows" in m for m in logs))

    def test_acquire_release_on_windows(self) -> None:
        logs: list[str] = []
        si = SleepInhibitor(logger=logs.append)
        fake_k32 = MagicMock()
        fake_k32.SetThreadExecutionState.return_value = 1
        fake_k32.PowerCreateRequest.return_value = 0  # 실패 → ExecutionState 만
        with patch.object(sys, "platform", "win32"), patch(
            "ctypes.windll", create=True
        ) as windll, patch("ctypes.WinDLL", return_value=fake_k32):
            windll.kernel32 = fake_k32
            self.assertTrue(si.acquire(keep_display=False))
            self.assertTrue(si.active)
            si.refresh()
            si.release()
        self.assertFalse(si.active)
        self.assertTrue(any("억제 시작" in m for m in logs))
        self.assertTrue(any("해제" in m for m in logs))
        self.assertGreaterEqual(fake_k32.SetThreadExecutionState.call_count, 2)

    def test_reacquire_when_keep_display_changes(self) -> None:
        si = SleepInhibitor(logger=lambda _m: None)
        fake_k32 = MagicMock()
        fake_k32.SetThreadExecutionState.return_value = 1
        with patch.object(sys, "platform", "win32"), patch(
            "ctypes.windll", create=True
        ) as windll, patch("ctypes.WinDLL", return_value=fake_k32):
            windll.kernel32 = fake_k32
            self.assertTrue(si.acquire(keep_display=False))
            self.assertFalse(si._keep_display)
            self.assertTrue(si.acquire(keep_display=True))
            self.assertTrue(si._keep_display)
            si.release()

    def test_release_when_inactive_is_safe(self) -> None:
        si = SleepInhibitor()
        si.release()
        self.assertFalse(si.active)


if __name__ == "__main__":
    unittest.main()
