# -*- coding: utf-8 -*-
"""
KRX 주식 정규장(평일 09:00~15:30, PC 로컬 시계 기준) 판별.
공휴일·조기종료는 미반영 — 실시간 UI에서 ‘장전/장후’ 표시용.
"""
from __future__ import annotations

import math
from datetime import datetime


def is_krx_regular_session(now: datetime | None = None) -> bool:
    """
    정규 매매 시간이면 True. 장 시작 전·마감 후·주말은 False.

    사용자 PC가 한국 표준시로 맞춰져 있다고 가정한다.
    """
    try:
        t = now if now is not None else datetime.now()
        if t.weekday() >= 5:
            return False
        hm = t.hour * 60 + t.minute
        # 09:00 ~ 15:30 (포함)
        return (9 * 60) <= hm <= (15 * 60 + 30)
    except Exception:
        return True
