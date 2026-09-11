# -*- coding: utf-8 -*-
"""유휴 최대절전 억제 여부 판정 — 장중·장전은 막고, 장마감 선정 후에는 허용 옵션.

설정 키 ``prevent_sleep_*`` 는 호환용 이름이며, 실제 대상은 최대절전(하이버네이트)이다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Tuple


def _truthy_setting(st: Any, key: str, default: bool) -> bool:
    """settings 값이 bool/문자 혼재여도 안전하게 해석한다."""
    try:
        v = st.get(key, default) if st is not None else default
    except Exception:
        return bool(default)
    if isinstance(v, bool):
        return v
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on", "y"):
        return True
    if s in ("0", "false", "no", "off", "n", ""):
        return False
    return bool(default)


def sleep_inhibit_decision(
    app: Any, now: Optional[datetime] = None
) -> Tuple[bool, str]:
    """
    (억제여부, 사유코드) 를 반환한다.

    사유코드는 로그·진단용 짧은 영문 토큰.
    """
    try:
        st = getattr(app, "settings", None) or {}
        if not _truthy_setting(st, "prevent_sleep_while_running", True):
            return False, "master_off"
        if not _truthy_setting(st, "prevent_sleep_allow_after_eod", False):
            return True, "always_while_running"

        t = now if now is not None else datetime.now()

        cms = getattr(app, "continuous_monitor_service", None)
        if cms is not None:
            try:
                # 장전 자동시작 윈도우: 억제 유지
                if bool(getattr(cms, "armed", False)) and cms._should_morning_start(t):
                    return True, "morning_window"

                # 장마감+버퍼 이후: 감시 플래그와 무관하게 허용
                # (EOD 후 is_monitoring 잔존·해제 API 누락 시에도 최대절전이 막히지 않게)
                today_d = t.date()
                if cms._is_trading_day(today_d):
                    due = cms._eod_due_datetime(today_d)
                    if t >= due:
                        return False, "after_eod"
            except Exception:
                pass

        tc = getattr(app, "trading_controller", None)
        if tc is not None and bool(getattr(tc, "is_monitoring", False)):
            return True, "monitoring"

        # app 쪽 잔존 플래그도 보조 확인(컨트롤러와 불일치 대비)
        try:
            if bool(getattr(app, "is_monitoring", False)):
                return True, "app_monitoring"
        except Exception:
            pass

        return False, "idle_allow"
    except Exception:
        return True, "error_failsafe"


def should_prevent_sleep(app: Any, now: Optional[datetime] = None) -> bool:
    """
    Windows 유휴 최대절전을 막아야 하면 True.

    - ``prevent_sleep_while_running`` 이 꺼져 있으면 항상 False.
    - ``prevent_sleep_allow_after_eod`` 가 꺼져 있으면 GUI 실행 중 항상 True.
    - 켜져 있으면: 장전 자동시작 구간·실시간 감시 중만 True,
      거래일 장마감+버퍼(기본 16:00) 이후에는 감시 잔존과 무관하게 False.
    """
    want, _reason = sleep_inhibit_decision(app, now=now)
    return want
