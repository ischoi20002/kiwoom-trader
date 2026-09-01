# -*- coding: utf-8 -*-
"""Transition 레짐 디스크 캐시 단위 테스트."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from utils import regime_gate_cache as rgc


class TestRegimeGateCache(unittest.TestCase):
  def test_fingerprint_changes_when_settings_change(self) -> None:
    fp1 = rgc.transition_gate_settings_fingerprint(
      {"use_transition_gate": True, "transition_rvol_min": 35.0}
    )
    fp2 = rgc.transition_gate_settings_fingerprint(
      {"use_transition_gate": True, "transition_rvol_min": 40.0}
    )
    self.assertNotEqual(fp1, fp2)

  def test_save_and_load_roundtrip(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      path = os.path.join(tmp, "regime_gate_cache.json")
      settings = {
        "use_transition_gate": True,
        "transition_rvol_min": 35.0,
      }
      payload = {
        "regime": "OFF",
        "slots_a": 20,
        "slots_b": 15,
        "block_s1": False,
        "block_s2": False,
        "as_of": "2026-08-29",
      }
      with patch.object(rgc, "cache_file_path", return_value=path):
        rgc.save_regime_gate_cache(
          as_of="2026-08-29",
          settings=settings,
          payload=payload,
        )
        loaded = rgc.load_regime_gate_cache(as_of="2026-08-29", settings=settings)
      self.assertIsNotNone(loaded)
      assert loaded is not None
      self.assertEqual(loaded["regime"], "OFF")
      self.assertEqual(loaded["slots_a"], 20)

  def test_load_miss_on_as_of_mismatch(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      path = os.path.join(tmp, "regime_gate_cache.json")
      settings = {"use_transition_gate": True}
      payload = {"regime": "HARD", "slots_a": 0, "slots_b": 0, "as_of": "2026-08-29"}
      with patch.object(rgc, "cache_file_path", return_value=path):
        rgc.save_regime_gate_cache(
          as_of="2026-08-29",
          settings=settings,
          payload=payload,
        )
        loaded = rgc.load_regime_gate_cache(as_of="2026-08-30", settings=settings)
      self.assertIsNone(loaded)

  def test_corrupt_cache_returns_none(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      path = os.path.join(tmp, "regime_gate_cache.json")
      with open(path, "w", encoding="utf-8") as f:
        f.write("{not-json")
      with patch.object(rgc, "cache_file_path", return_value=path):
        loaded = rgc.load_regime_gate_cache(
          as_of="2026-08-29",
          settings={"use_transition_gate": True},
        )
      self.assertIsNone(loaded)


if __name__ == "__main__":
  unittest.main()
