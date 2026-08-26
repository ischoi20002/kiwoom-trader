# -*- coding: utf-8 -*-
"""
ka10080 REST 호출 분산 — 감시 종목 수(3~40+)에 따라 M1·15분 확정 창을 자동 스케일.

고정 40초/360초는 16종목 기준으로는 적절하나, 40종목이면 M1이 분당 창을 넘치거나
15분 REST가 겹칠 수 있어 ``종목 수 × 초/종목`` 으로 계산합니다.
"""
from __future__ import annotations

from typing import Any, Dict


def _active_stream_code_count(app: Any) -> int:
    """현재 구독·감시 종목 수(정규 6자리)."""
    try:
        ac = getattr(app, "_active_stream_codes", None)
        if isinstance(ac, list):
            return max(1, len(ac))
    except Exception:
        pass
    return 1


def effective_m1_poll_spread_sec(settings: Dict[str, Any], n_codes: int) -> float:
    """
    M1 폴러: 매 분 ``[0, spread)`` 초 구간에 ``n_codes`` 종목 분산.

    ``spread = clamp(n × per_code, min, max)`` (기본 per_code=2.5 → 16종목≈40s, 40종목=55s cap).
    """
    try:
        n = max(1, int(n_codes))
    except (TypeError, ValueError):
        n = 1
    try:
        per = float(settings.get("realtime_watchlist_m1_api_spread_per_code_sec", 2.5))
    except (TypeError, ValueError):
        per = 2.5
    per = max(0.8, min(per, 8.0))
    try:
        lo = float(settings.get("realtime_watchlist_m1_api_spread_min_sec", 12.0))
    except (TypeError, ValueError):
        lo = 12.0
    try:
        hi = float(settings.get("realtime_watchlist_m1_api_spread_max_sec", 55.0))
    except (TypeError, ValueError):
        hi = 55.0
    lo = max(8.0, min(lo, 58.0))
    hi = max(lo, min(hi, 58.0))
    scaled = float(n) * per
    # 레거시 고정값(16종목 튜닝 40s)이 더 크면 상향만 반영
    try:
        legacy = float(settings.get("realtime_watchlist_m1_api_spread_sec", 0.0) or 0.0)
    except (TypeError, ValueError):
        legacy = 0.0
    if legacy > 0:
        scaled = max(scaled, legacy)
    return max(lo, min(hi, scaled))


def effective_m1_head_catchup_spread_sec(
    settings: Dict[str, Any], n_codes: int
) -> float:
    """
    슬롯 **헤드 soft catchup** 전용 분산 창(초).

    ``:13`` 라벨 마감(~ ``:14``) 이후 ~1분 안에 말분 평가(``:00``)가 오므로,
    레거시 M1 40초 바닥을 쓰지 않고 전역 ka10080 간격(~1.05s)에 맞춰
    ``n × per_code`` 로 잡되 상한 50초로 자른다.
    """
    try:
        n = max(1, int(n_codes))
    except (TypeError, ValueError):
        n = 1
    try:
        per = float(
            settings.get(
                "realtime_m1_head_catchup_per_code_sec",
                settings.get("realtime_ka10080_global_min_interval_sec", 1.1),
            )
        )
    except (TypeError, ValueError):
        per = 1.1
    per = max(1.0, min(float(per), 2.0))
    try:
        lo = float(settings.get("realtime_m1_head_catchup_spread_min_sec", 3.0))
    except (TypeError, ValueError):
        lo = 3.0
    try:
        hi = float(settings.get("realtime_m1_head_catchup_spread_max_sec", 50.0))
    except (TypeError, ValueError):
        hi = 50.0
    lo = max(1.0, min(lo, 55.0))
    hi = max(lo, min(hi, 55.0))
    return max(lo, min(hi, float(n) * per))


def effective_m1_last_official_spread_sec(
    settings: Dict[str, Any], n_codes: int
) -> float:
    """
    슬롯 **말분 공식 1분** 조회 분산 창(초).

    공식 우선 평가용 — 헤드 catchup과 같이 ``n × ~1.1s`` 로 조이고
    레거시 M1 40초 바닥은 쓰지 않는다. 키가 없으면 헤드 설정을 재사용.
    """
    try:
        n = max(1, int(n_codes))
    except (TypeError, ValueError):
        n = 1
    try:
        per = float(
            settings.get(
                "realtime_m1_last_official_per_code_sec",
                settings.get(
                    "realtime_m1_head_catchup_per_code_sec",
                    settings.get("realtime_ka10080_global_min_interval_sec", 1.1),
                ),
            )
        )
    except (TypeError, ValueError):
        per = 1.1
    per = max(1.0, min(float(per), 2.0))
    try:
        lo = float(
            settings.get(
                "realtime_m1_last_official_spread_min_sec",
                settings.get("realtime_m1_head_catchup_spread_min_sec", 3.0),
            )
        )
    except (TypeError, ValueError):
        lo = 3.0
    try:
        hi = float(
            settings.get(
                "realtime_m1_last_official_spread_max_sec",
                settings.get("realtime_m1_head_catchup_spread_max_sec", 50.0),
            )
        )
    except (TypeError, ValueError):
        hi = 50.0
    lo = max(1.0, min(lo, 55.0))
    hi = max(lo, min(hi, 55.0))
    return max(lo, min(hi, float(n) * per))


def effective_rt15m_stagger_spread_sec(settings: Dict[str, Any], n_codes: int) -> float:
    """
    15분 확정 REST: ``min_delay`` 이후 ``[0, spread)`` 에 종목 분산.

    ``spread = clamp(n × per_code, min, max)`` (기본 per_code=22.5 → 16≈360s, 40≈840s cap).
    """
    try:
        n = max(1, int(n_codes))
    except (TypeError, ValueError):
        n = 1
    try:
        per = float(
            settings.get(
                "realtime_closed_15m_official_replace_spread_per_code_sec", 22.5
            )
        )
    except (TypeError, ValueError):
        per = 22.5
    per = max(5.0, min(per, 60.0))
    try:
        lo = float(
            settings.get("realtime_closed_15m_official_replace_spread_min_sec", 90.0)
        )
    except (TypeError, ValueError):
        lo = 90.0
    try:
        hi = float(
            settings.get("realtime_closed_15m_official_replace_spread_max_sec", 840.0)
        )
    except (TypeError, ValueError):
        hi = 840.0
    lo = max(60.0, min(lo, 870.0))
    hi = max(lo, min(hi, 870.0))
    scaled = float(n) * per
    try:
        legacy = float(
            settings.get("realtime_closed_15m_official_replace_spread_sec", 0.0) or 0.0
        )
    except (TypeError, ValueError):
        legacy = 0.0
    if legacy > 0:
        scaled = max(scaled, legacy)
    return max(lo, min(hi, scaled))


def effective_m1_poll_spread_for_app(app: Any) -> float:
    """``app.settings`` + ``_active_stream_codes`` 기준 M1 spread."""
    try:
        st = getattr(app, "settings", None) or {}
    except Exception:
        st = {}
    return effective_m1_poll_spread_sec(st, _active_stream_code_count(app))
