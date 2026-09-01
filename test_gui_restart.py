# -*- coding: utf-8 -*-
"""GUI 재시작 상태 유틸 단위 테스트."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from utils import gui_restart as gr


class TestGuiRestart(unittest.TestCase):
    def test_is_monitoring_active(self) -> None:
        app = SimpleNamespace(
            is_monitoring=False,
            trading_controller=SimpleNamespace(is_monitoring=True),
        )
        self.assertTrue(gr.is_monitoring_active(app))

        app2 = SimpleNamespace(is_monitoring=False, trading_controller=None)
        self.assertFalse(gr.is_monitoring_active(app2))

    def test_restart_state_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gui_restart_state.json")
            with patch.object(gr, "state_file_path", return_value=path), patch.object(
                gr, "_state_file_candidates", return_value=(path,)
            ):
                gr.write_restart_state(resume_monitoring=True)
                data = gr.read_and_clear_restart_state()
            self.assertIsNotNone(data)
            assert data is not None
            self.assertTrue(data["resume_monitoring"])
            self.assertFalse(os.path.isfile(path))

    def test_read_and_clear_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "missing.json")
            with patch.object(gr, "_state_file_candidates", return_value=(path,)):
                self.assertIsNone(gr.read_and_clear_restart_state())


if __name__ == "__main__":
    unittest.main()
