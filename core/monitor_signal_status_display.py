# -*- coding: utf-8 -*-
"""
감시 트리 **신호상태** 열에 넣을 표시 문자열.

기존 ``status_text or confluence`` 순서는 ``status_text`` 가 ``"대기"`` 처럼
**항상 참인 문자열**이면 틱마다 갱신되는 ``confluence``(합치도·S2 문구)가
영구히 가려져 화면이 멈춘 것처럼 보이는 버그가 있었다.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def _is_status_placeholder(text: object) -> bool:
    try:
        t = str(text).strip()
    except Exception:
        return True
    return t in ("", "-", "—")


def monitor_signal_status_display_text(info: Optional[Dict[str, Any]]) -> str:
    """
    ``monitor_info`` 한 행에서 신호상태 열 문자열을 결정한다.

    - ``confluence`` 가 플레이스홀더가 아니면 **우선**(틱·슬롯마다 갱신되는 합치도·세션 문구).
    - 그렇지 않으면 ``status_text`` (워밍업·보조 안내 등).
    """
    if not isinstance(info, dict):
        return "대기"
    try:
        cf = info.get("confluence", "")
        if not _is_status_placeholder(cf):
            return str(cf).strip()
        st = info.get("status_text", "")
        if not _is_status_placeholder(st):
            return str(st).strip()
        return "대기"
    except Exception:
        return "대기"
