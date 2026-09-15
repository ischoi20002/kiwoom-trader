# -*- coding: utf-8 -*-
"""유휴 최대절전 억제 여부 판정 — 장중·장전은 막고, 장마감 선정 후에는 허용 옵션.

설정 키 ``prevent_sleep_*`` 는 호환용 이름이며, 실제 대상은 최대절전(하이버네이트)이다.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
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


def _eod_buffer_minutes(app: Any) -> int:
    """연속감시 EOD 버퍼(분). CMS·settings 순으로 읽고 5~120으로 클램프."""
    try:
        cms = getattr(app, "continuous_monitor_service", None)
        if cms is not None and hasattr(cms, "_eod_buffer_minutes"):
            return int(cms._eod_buffer_minutes())
    except Exception:
        pass
    try:
        st = getattr(app, "settings", None) or {}
        v = int(float(st.get("continuous_monitor_eod_buffer_minutes", 30)))
        return max(5, min(v, 120))
    except Exception:
        return 30


def _monitor_eod_due(app: Any, day_d: date) -> datetime:
    """
    감시 EOD due 시각.

    CMS 메서드에만 의존하지 않는다 — CMS 부재·예외 시에도 달력으로 산출해
    after_eod 판정이 monitoring 잔존에 가로채이지 않게 한다.
    """
    buf = _eod_buffer_minutes(app)
    cms = getattr(app, "continuous_monitor_service", None)
    if cms is not None:
        try:
            due = cms._eod_due_datetime(day_d)
            if isinstance(due, datetime):
                return due
        except Exception:
            pass
    try:
        from reporting.krx_trading_calendar import monitor_eod_due_datetime

        return monitor_eod_due_datetime(day_d, eod_buffer_minutes=buf)
    except Exception:
        return datetime.combine(day_d, time(15, 30, 0)) + timedelta(minutes=buf)


def _is_trading_day(app: Any, day_d: date) -> bool:
    """거래일 여부 — CMS 우선, 실패 시 달력/평일 폴백."""
    cms = getattr(app, "continuous_monitor_service", None)
    if cms is not None:
        try:
            return bool(cms._is_trading_day(day_d))
        except Exception:
            pass
    try:
        from reporting.krx_trading_calendar import is_trading_day

        return bool(is_trading_day(day_d))
    except Exception:
        return day_d.weekday() < 5


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
        today_d = t.date()

        cms = getattr(app, "continuous_monitor_service", None)
        if cms is not None:
            try:
                # 장전 자동시작 윈도우: 억제 유지
                if bool(getattr(cms, "armed", False)) and cms._should_morning_start(t):
                    return True, "morning_window"
            except Exception:
                pass

        # 장마감+버퍼 이후: 감시 플래그·CMS 유무와 무관하게 허용
        try:
            if _is_trading_day(app, today_d):
                due = _monitor_eod_due(app, today_d)
                if t >= due:
                    return False, f"after_eod@{due.strftime('%H:%M')}"
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
      거래일 EOD(애프터 시행일=20:00+버퍼, 그 외=정규 close+버퍼) 이후에는
      감시 잔존과 무관하게 False.
    """
    want, _reason = sleep_inhibit_decision(app, now=now)
    return want
