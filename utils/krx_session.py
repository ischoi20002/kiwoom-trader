# -*- coding: utf-8 -*-
"""
KRX 주식 세션 판별 (PC 로컬 시계 + 일자별 SessionHours / 애프터마켓).

공휴일은 ``require_trading_day`` 또는 호출부에서 별도 검사.
특수 개장일(신년·수능 등)은 ``utils.krx_session_hours`` / ``krx_special_sessions.json``.
"""
from __future__ import annotations

from datetime import datetime

from utils.krx_session_hours import (
    is_in_aftermarket_session,
    is_in_regular_session,
    is_in_tradable_session,
)


def is_krx_regular_session(now: datetime | None = None) -> bool:
    """
    정규 매매 시간이면 True. 장 시작 전·마감 후·주말은 False.

    사용자 PC가 한국 표준시로 맞춰져 있다고 가정한다.
    당일 ``get_session_hours`` 의 open~close 구간을 사용한다.
    """
    try:
        t = now if now is not None else datetime.now()
        return bool(is_in_regular_session(t))
    except Exception:
        return True


def is_krx_aftermarket_session(now: datetime | None = None) -> bool:
    """애프터마켓(16:00~20:00, 시행일 이후)이면 True."""
    try:
        t = now if now is not None else datetime.now()
        return bool(is_in_aftermarket_session(t))
    except Exception:
        return False


def is_krx_tradable_session(now: datetime | None = None) -> bool:
    """
    정규장 또는 애프터마켓이면 True.

    실시간 틱·완성15 평가·주문 게이트에 사용한다.
    """
    try:
        t = now if now is not None else datetime.now()
        return bool(is_in_tradable_session(t))
    except Exception:
        return True
