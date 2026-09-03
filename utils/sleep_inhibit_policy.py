# -*- coding: utf-8 -*-
"""유휴 최대절전 억제 여부 판정 — 장중·장전은 막고, 장마감 선정 후에는 허용 옵션.

설정 키 ``prevent_sleep_*`` 는 호환용 이름이며, 실제 대상은 최대절전(하이버네이트)이다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


def should_prevent_sleep(app: Any, now: Optional[datetime] = None) -> bool:
    """
    Windows 유휴 최대절전을 막아야 하면 True.

    - ``prevent_sleep_while_running`` 이 꺼져 있으면 항상 False.
    - ``prevent_sleep_allow_after_eod`` 가 꺼져 있으면 GUI 실행 중 항상 True.
    - 켜져 있으면: 실시간 감시 중·장전 자동시작 대기 구간만 True,
      거래일 장마감+버퍼(기본 16:00) 이후·감시 꺼짐이면 False.
    """
    try:
        st = getattr(app, "settings", None) or {}
        if not bool(st.get("prevent_sleep_while_running", True)):
            return False
        if not bool(st.get("prevent_sleep_allow_after_eod", False)):
            return True

        t = now if now is not None else datetime.now()

        tc = getattr(app, "trading_controller", None)
        if tc is not None and bool(getattr(tc, "is_monitoring", False)):
            return True

        cms = getattr(app, "continuous_monitor_service", None)
        if cms is not None:
            try:
                if bool(getattr(cms, "armed", False)):
                    today_d = t.date()
                    if cms._should_morning_start(t):
                        return True
                    if cms._is_trading_day(today_d):
                        due = cms._eod_due_datetime(today_d)
                        if t >= due:
                            return False
            except Exception:
                pass

        return False
    except Exception:
        return True
