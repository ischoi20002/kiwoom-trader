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
            self.assertEqual(si._mode, "exec")
            si.refresh()
            si.release()
        self.assertFalse(si.active)
        self.assertTrue(any("억제 시작" in m for m in logs))
        self.assertTrue(any("해제" in m for m in logs))
        self.assertGreaterEqual(fake_k32.SetThreadExecutionState.call_count, 2)

    def test_power_request_preferred_no_dual_hold(self) -> None:
        """PowerRequest 성공 시 ExecutionState 를 동시에 걸지 않는다."""
        si = SleepInhibitor(logger=lambda _m: None)
        fake_k32 = MagicMock()
        fake_k32.PowerCreateRequest.return_value = 0xD0
        fake_k32.PowerSetRequest.return_value = True
        fake_k32.PowerClearRequest.return_value = True
        fake_k32.CloseHandle.return_value = True
        fake_k32.SetThreadExecutionState.return_value = 1
        with patch.object(sys, "platform", "win32"), patch(
            "ctypes.WinDLL", return_value=fake_k32
        ):
            self.assertTrue(si.acquire(keep_display=False))
            self.assertEqual(si._mode, "power")
            # acquire 경로에서는 ExecutionState 를 걸지 않음
            fake_k32.SetThreadExecutionState.assert_not_called()
            si.refresh()  # power 모드 refresh 는 no-op
            fake_k32.SetThreadExecutionState.assert_not_called()
            si.release()
            # release 에서 CONTINUOUS 정리만
            self.assertGreaterEqual(fake_k32.SetThreadExecutionState.call_count, 1)
            fake_k32.PowerClearRequest.assert_called()
        self.assertFalse(si.active)
        self.assertIsNone(si._power_handle)

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
