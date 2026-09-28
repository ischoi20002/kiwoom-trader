# -*- coding: utf-8 -*-
"""감시 행 등락색 판정 — 근거 없을 때 보합(흰색)으로 떨어지지 않는지 검증."""
from __future__ import annotations

from core.monitor_tab_helpers import (
    monitor_direction_from_info,
    resolve_monitor_row_direction,
    row_direction_from_tags,
)
from core.signal_monitor_tree_ui import SignalMonitorTreeUiMixin


def test_row_direction_from_tags_exact_match() -> None:
    assert row_direction_from_tags(("up_even",)) == "up"
    assert row_direction_from_tags(("active_down_odd",)) == "down"
    assert row_direction_from_tags(("stable_even",)) == "stable"
    assert row_direction_from_tags(()) is None
    # 'warmup' 에 'up' 부분문자열이 있어도 방향으로 오인하지 않음
    assert row_direction_from_tags(("warmup_ok_row",)) is None
    assert row_direction_from_tags(("status_low",)) is None


def test_direction_from_info_diff_priority() -> None:
    assert monitor_direction_from_info({"diff": "+1500", "ratio": "+2.5"}) == "up"
    assert monitor_direction_from_info({"diff": "-800", "ratio": "-1.1"}) == "down"
    assert monitor_direction_from_info({"diff": "▲300"}) == "up"
    assert monitor_direction_from_info({"ratio": "-0.42%"}) == "down"


def test_direction_from_info_zero_diff_uses_ratio() -> None:
    assert monitor_direction_from_info({"diff": "0", "ratio": "+0.10"}) == "up"
    assert monitor_direction_from_info({"diff": "0", "ratio": "0.00"}) == "stable"


def test_direction_from_info_unknown_returns_none() -> None:
    assert monitor_direction_from_info({}) is None
    assert monitor_direction_from_info({"diff": "—", "ratio": ""}) is None
    assert monitor_direction_from_info(None) is None


def test_resolve_keeps_previous_color_without_evidence() -> None:
    # 전일대비 미수신 + 큐 태그 보합 → 기존 상승색 유지
    assert resolve_monitor_row_direction({}, "stable", "up") == "up"
    assert resolve_monitor_row_direction(None, "stable", "down") == "down"
    # 큐 태그에 방향이 있으면 그것을 사용
    assert resolve_monitor_row_direction({}, "down", "up") == "down"
    # 새 행이고 근거가 전혀 없으면 보합
    assert resolve_monitor_row_direction({}, "stable", None) == "stable"


def test_resolve_prefers_live_info_over_stale_tag() -> None:
    assert resolve_monitor_row_direction({"diff": "+100"}, "stable", "down") == "up"
    # 실제 보합 수신은 보합으로 표시
    assert resolve_monitor_row_direction({"diff": "0", "ratio": "0"}, "up", "up") == "stable"


class _Mixin(SignalMonitorTreeUiMixin):
    pass


def test_ratio_direction_keeps_sign_when_price_unknown() -> None:
    m = _Mixin()
    cell, d = m._monitor_tree_ratio_and_direction({"diff": "+500"}, 0.0, 0.0, 0.0)
    assert d > 0
    cell, d = m._monitor_tree_ratio_and_direction(
        {"diff": "+500", "ratio": "+1.20"}, 0.0, 0.0, 0.0
    )
    assert d > 0
    assert cell.startswith("▲")


def test_ratio_direction_normal_case_unchanged() -> None:
    m = _Mixin()
    cell, d = m._monitor_tree_ratio_and_direction({"diff": "-1000"}, 49000.0, 0.0, 0.0)
    assert d < 0
    assert cell == "▼ 2.00%"
    cell, d = m._monitor_tree_ratio_and_direction({"diff": "0"}, 50000.0, 0.0, 0.0)
    assert d == 0.0
