# -*- coding: utf-8 -*-
"""
Type B 시장 레짐별 1차 보정 (vol_surge_b만).

Type A 레짐·파라미터와 완전히 독립 — `use_type_b`·`use_type_b_regime_phase1`·`type_b_phase1_*` 만 본다.

- AGGRESSIVE / UP: `type_b_phase1_up_vol_surge_add` 가산(동일 티어 — 둘 다 ‘진입 허용’ 장세).
- NEUTRAL: `type_b_phase1_neutral_vol_surge_add` 가산(더 보수적; 단, 시장필터 ON이면 B 진입은 보통 차단됨).
- DOWN: 수치 보정 없음(상위에서 진입 차단).
"""

from __future__ import annotations

from typing import Any, Dict, Mapping


def apply_type_b_phase1_overlay(
    base_settings: Mapping[str, Any],
    working: Mapping[str, Any],
    regime: str,
) -> Dict[str, Any]:
    """
    이미 Type A 보정 등이 반영된 working dict 위에 vol_surge_b만 조정한다.

    - base_settings: 사용자 원본 설정 — vol_surge_b 기준값은 항상 여기서 읽음.
    - working: 신호 계산에 쓸 설정 사본.
    - regime: AGGRESSIVE / UP / NEUTRAL / DOWN 등 (동적 백테 m_trend와 동일; B 보정만 적용).
    """
    out: Dict[str, Any] = dict(working)
    try:
        if not bool(out.get("use_type_b_regime_phase1", False)):
            return out
        if not bool(out.get("use_type_b", False)):
            return out
    except (TypeError, ValueError):
        return out

    r = str(regime or "UP").strip().upper()
    if r in ("DOWN", "", "NONE"):
        return out

    try:
        vol_add_up = float(out.get("type_b_phase1_up_vol_surge_add", 0.2))
        vol_add_neu = float(out.get("type_b_phase1_neutral_vol_surge_add", 0.45))
    except (TypeError, ValueError):
        return out

    # A 레짐과 무관: 진입 가능한 강세(AGG/UP)는 동일 UP-티어, 중립만 NEUTRAL-티어
    if r in ("AGGRESSIVE", "UP"):
        v_add = vol_add_up
    elif r == "NEUTRAL":
        v_add = vol_add_neu
    else:
        v_add = vol_add_up

    try:
        if bool(out.get("use_type_b_vol", True)) and v_add != 0.0:
            base_v = float(base_settings.get("vol_surge_b", 2.0))
            out["vol_surge_b"] = max(0.01, base_v + v_add)
    except (TypeError, ValueError):
        pass

    return out
