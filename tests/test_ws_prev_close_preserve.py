# -*- coding: utf-8 -*-
"""WS fid 11/12 미수신 시 전일대비 덮어쓰기 방지 (감시 행 보합색 깜빡임)."""
from __future__ import annotations

from core.monitor_tab_helpers import (
    apply_ws_prev_close_fields,
    normalize_ws_num_token,
    ws_fid_raw,
)


def test_normalize_placeholder_returns_none() -> None:
    assert normalize_ws_num_token(None) is None
    assert normalize_ws_num_token("") is None
    assert normalize_ws_num_token("-") is None
    assert normalize_ws_num_token("—") is None
    assert normalize_ws_num_token("  ") is None


def test_normalize_real_zero_and_signed() -> None:
    assert normalize_ws_num_token("0") == "0"
    assert normalize_ws_num_token("0.00") == "0.00"
    assert normalize_ws_num_token("+1.23") == "+1.23"
    assert normalize_ws_num_token("▲1.5") == "+1.5"
    assert normalize_ws_num_token("▼2,500") == "-2500"


def test_ws_fid_raw_int_and_str_keys() -> None:
    assert ws_fid_raw({11: "100"}, 11) == (True, "100")
    assert ws_fid_raw({"12": "-1.2"}, 12) == (True, "-1.2")
    assert ws_fid_raw({10: "50000"}, 11) == (False, None)


def test_apply_preserves_when_fid_missing() -> None:
    info = {"diff": "1500", "ratio": "2.5", "price": 60000}
    apply_ws_prev_close_fields(info, {10: "60100", 13: "1000"})
    assert info["diff"] == "1500"
    assert info["ratio"] == "2.5"


def test_apply_preserves_when_placeholder() -> None:
    info = {"diff": "-800", "ratio": "-1.1"}
    apply_ws_prev_close_fields(info, {11: "—", 12: "", 10: "50000"})
    assert info["diff"] == "-800"
    assert info["ratio"] == "-1.1"


def test_apply_updates_when_valid() -> None:
    info = {"diff": "100", "ratio": "0.5"}
    apply_ws_prev_close_fields(info, {11: "-200", "12": "-0.8"})
    assert info["diff"] == "-200"
    assert info["ratio"] == "-0.8"


def test_apply_allows_real_flat_zero() -> None:
    info = {"diff": "100", "ratio": "0.5"}
    apply_ws_prev_close_fields(info, {11: "0", 12: "0.00"})
    assert info["diff"] == "0"
    assert info["ratio"] == "0.00"
