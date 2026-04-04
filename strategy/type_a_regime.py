# -*- coding: utf-8 -*-
"""
Type A 시장 레짐별 1차 보정 (vol_surge, MA60 밴드만).
RSI 상·하한(buy_rsi_min / buy_rsi_max_a)은 보정하지 않음.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping


def apply_type_a_phase1_settings(
    base_settings: Mapping[str, Any],
    regime: str,
) -> Dict[str, Any]:
    """
    AGGRESSIVE: 기준 설정과 동일(보정 없음).
    UP: 거래량 배수 소폭 상향, MA60 허용 구간 소폭 축소.
    NEUTRAL: UP보다 한 단계 더 보수적.
    DOWN: 수치 보정 없음(상위에서 신호 차단).
    실시간 감시 등 trend 문자열이 UP/NEUTRAL/DOWN만 올 때: UP→UP 보정, NEUTRAL→NEUTRAL 보정.
    """
    s: Dict[str, Any] = dict(base_settings)
    try:
        if not bool(s.get("use_type_a_regime_phase1", False)):
            return s
        # Type A 미사용 시 레짐 보정 비적용 — B 전용 백테에서 A 레짐 토글이 결과를 오염시키지 않음
        if not bool(s.get("use_type_a", False)):
            return s
    except (TypeError, ValueError):
        return s

    r = str(regime or "UP").strip().upper()
    if r in ("AGGRESSIVE", "DOWN", "", "NONE"):
        return s
    if r not in ("UP", "NEUTRAL"):
        # 알 수 없는 값은 보수 쪽(NEUTRAL)에 가깝게 처리하지 않고 안전하게 UP 보정만
        r = "UP"

    try:
        vol_add_up = float(s.get("type_a_phase1_up_vol_surge_add", 0.2))
        vol_add_neu = float(s.get("type_a_phase1_neutral_vol_surge_add", 0.45))
        ma_shrink_up = float(s.get("type_a_phase1_up_ma60_shrink_pct", 1.0))
        ma_shrink_neu = float(s.get("type_a_phase1_neutral_ma60_shrink_pct", 2.0))
    except (TypeError, ValueError):
        return s

    if r == "UP":
        v_add, m_shrink = vol_add_up, ma_shrink_up
    else:
        v_add, m_shrink = vol_add_neu, ma_shrink_neu

    # Type A 거래량 서지 (기준값은 항상 base_settings 원본)
    try:
        if bool(s.get("use_vol_surge", True)) and v_add != 0.0:
            base_v = float(base_settings.get("vol_surge", 1.5))
            s["vol_surge"] = max(0.01, base_v + v_add)
    except (TypeError, ValueError):
        pass

    # MA60 안전구역 폭 축소 (하한↑, 상한↓)
    try:
        if bool(s.get("use_ma60_safe", True)) and m_shrink > 0.0:
            lo = float(base_settings.get("ma60_min", 100.0))
            hi = float(base_settings.get("ma60_max", 110.0))
            nlo = lo + m_shrink
            nhi = hi - m_shrink
            if nlo < nhi - 0.01:
                s["ma60_min"] = nlo
                s["ma60_max"] = nhi
    except (TypeError, ValueError):
        pass

    return s
