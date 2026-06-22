# -*- coding: utf-8 -*-
"""
보유 직후 monitor_info 가격 스텁(vp_price_stub) 유예.

첫 틱 직후 메인 스레드 REST 폴백·UI 멈춤을 줄이기 위해 스텁을 N초 유지합니다.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional


def vp_price_stub_grace_sec(settings: Optional[Dict[str, Any]] = None) -> float:
    """설정: 보유 스텁 유지 시간(초)."""
    try:
        st = settings or {}
        v = float(st.get("vp_price_stub_grace_sec", 45.0))
        return max(5.0, min(v, 300.0))
    except (TypeError, ValueError):
        return 45.0


def stamp_vp_price_stub(
    row: Dict[str, Any],
    *,
    settings: Optional[Dict[str, Any]] = None,
    grace_sec: Optional[float] = None,
) -> None:
    """monitor_info 행에 스텁 플래그·만료 시각을 기록합니다."""
    try:
        sec = float(grace_sec) if grace_sec is not None else vp_price_stub_grace_sec(settings)
        sec = max(5.0, min(sec, 300.0))
        row["vp_price_stub"] = True
        row["vp_stub_until_ts"] = float(time.time()) + sec
    except Exception:
        try:
            row["vp_price_stub"] = True
        except Exception:
            pass


def is_vp_price_stub_active(info: Optional[Dict[str, Any]]) -> bool:
    """
    스텁이 유효한지(유예 중인지) 판별.

    ``vp_stub_until_ts`` 가 없거나 0 이하이면 유예 정보가 없는 것으로 보고 False 를 반환합니다.
    (만료 시각이 없는 스텁을 '영구 활성'으로 처리하면 실시간 가격이 영영 막혀 평단이 현재가처럼
    표시되는 고착이 생기고, ``clear_vp_price_stub_if_expired`` 의 즉시 제거 동작과도 모순됨)
    만료 시각이 지났으면 False — 호출 측에서 플래그 정리 권장.
    """
    if not isinstance(info, dict):
        return False
    if not bool(info.get("vp_price_stub")):
        return False
    try:
        until_ts = float(info.get("vp_stub_until_ts", 0) or 0)
    except (TypeError, ValueError):
        until_ts = 0.0
    if until_ts <= 0:
        return False
    return float(time.time()) < until_ts


def clear_vp_price_stub_if_expired(info: Dict[str, Any]) -> None:
    """유예 만료 후 스텁 키를 제거합니다."""
    if not isinstance(info, dict):
        return
    if not info.get("vp_price_stub"):
        return
    try:
        until_ts = float(info.get("vp_stub_until_ts", 0) or 0)
    except (TypeError, ValueError):
        until_ts = 0.0
    if until_ts > 0 and float(time.time()) < until_ts:
        return
    try:
        info.pop("vp_price_stub", None)
        info.pop("vp_stub_until_ts", None)
    except Exception:
        pass
