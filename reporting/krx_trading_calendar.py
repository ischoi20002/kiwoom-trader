# -*- coding: utf-8 -*-
"""
KRX(XKRX) 거래일 판정 — pandas_market_calendars 고정 사용.
휴장일·주말은 스킵, 라이브러리 미설치 시 폴백(주말만 제외).
"""
from __future__ import annotations

import warnings
from datetime import date, datetime, timedelta
from typing import Any, List, Optional

try:
    import pandas_market_calendars as mcal

    _HAS_MCAL = True
except Exception:
    _HAS_MCAL = False

# XKRX 캘린더는 생성 비용·경고가 있어 프로세스당 1회만 로드합니다.
_xkrx_cal_cache: Optional[Any] = None


def _parse_day(d: str | date) -> date:
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()


def get_xkrx_calendar():
    """
    XKRX 캘린더 인스턴스. 미설치 시 None.

    pandas_market_calendars 최신판은 XKRX의 break_start/break_end 폐지로
    MarketCalendar.__init__ 시 UserWarning을 냅니다. 하베스팅·거래일 판정 로직과
    무관하므로, 캘린더 1회 생성 구간에서만 해당 경고를 숨깁니다.
    """
    global _xkrx_cal_cache
    if not _HAS_MCAL:
        return None
    if _xkrx_cal_cache is not None:
        return _xkrx_cal_cache
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                category=UserWarning,
                module=r"pandas_market_calendars\.market_calendar",
            )
            _xkrx_cal_cache = mcal.get_calendar("XKRX")
        return _xkrx_cal_cache
    except Exception:
        return None


def is_trading_day(day: str | date) -> bool:
    """해당 일자가 KRX 거래일이면 True."""
    d = _parse_day(day)
    cal = get_xkrx_calendar()
    if cal is not None:
        try:
            import pandas as pd

            vd = cal.valid_days(start_date=pd.Timestamp(d), end_date=pd.Timestamp(d))
            return len(vd) > 0
        except Exception:
            pass
    # 폴백: 월~금
    return d.weekday() < 5


def trading_days_inclusive(start: str | date, end: str | date) -> List[date]:
    """시작~종료(포함) 사이의 KRX 거래일 목록(정렬)."""
    s = _parse_day(start)
    e = _parse_day(end)
    if s > e:
        return []
    cal = get_xkrx_calendar()
    if cal is not None:
        try:
            import pandas as pd

            vd = cal.valid_days(start_date=pd.Timestamp(s), end_date=pd.Timestamp(e))
            return [t.date() for t in vd]
        except Exception:
            pass
    # 폴백: 주말 제외
    out: List[date] = []
    cur = s
    while cur <= e:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def previous_trading_day(day: str | date, max_lookback: int = 10) -> Optional[date]:
    """직전 거래일 (없으면 None)."""
    d = _parse_day(day)
    for _ in range(max_lookback):
        d -= timedelta(days=1)
        if is_trading_day(d):
            return d
    return None


def next_trading_day(day: str | date, max_lookahead: int = 10) -> Optional[date]:
    """다음 거래일 (없으면 None)."""
    d = _parse_day(day)
    for _ in range(max_lookahead):
        d += timedelta(days=1)
        if is_trading_day(d):
            return d
    return None


def required_eod_bar_date_asof_local(now: Optional[datetime] = None) -> date:
    """
    종목 선정용: 로컬 DB 일봉에 반드시 포함되어야 하는 '마지막 완결 일봉'의 영업일.
    - 비거래일: 직전 거래일
    - 거래일 장마감(15:30) 이후: 당일
    - 거래일 장전·장중: 직전 거래일(당일 일봉은 미완)
    """
    t = now or datetime.now()
    d = t.date()
    try:
        if not is_trading_day(d):
            prev = previous_trading_day(d)
            return prev if prev is not None else d
        mins = t.hour * 60 + t.minute
        close_end = 15 * 60 + 30
        if mins > close_end:
            return d
        prev = previous_trading_day(d)
        return prev if prev is not None else d
    except Exception:
        return d


def required_eod_bar_datetime_asof_local(now: Optional[datetime] = None) -> Optional[datetime]:
    """
    하베스팅·DB 완결성 판정용: 반드시 포함되어야 할 마지막 **1분봉** 시각(해당 영업일 15:30:00).
    """
    try:
        req_d = required_eod_bar_date_asof_local(now)
        return datetime(req_d.year, req_d.month, req_d.day, 15, 30, 0)
    except Exception:
        return None


def is_krx_regular_session_local(now: Optional[datetime] = None) -> bool:
    """
    PC 로컬 시각을 한국장 기준으로 본다고 가정할 때, KRX 정규장(09:00~15:30) 구간 여부.
    (해외 타임존이면 설정 맞춤 필요)
    """
    try:
        t = now or datetime.now()
        mins = t.hour * 60 + t.minute
        return (9 * 60) <= mins <= (15 * 60 + 30)
    except Exception:
        return False


def minute_harvest_datetime_cap_iso(now: Optional[datetime] = None) -> Optional[str]:
    """
    하베스팅용: 장중(정규장)이고 오늘이 거래일이면, DB에 넣을 수 있는 1분봉의 최대 시각(포함).
    직전 거래일 15:30:00 — 당일 장중 봉은 저장하지 않기 위함. 그 외 None(제한 없음).
    """
    try:
        t = now or datetime.now()
        today_d = t.date()
        if not is_trading_day(today_d):
            return None
        if not is_krx_regular_session_local(t):
            return None
        prev = previous_trading_day(today_d)
        if prev is None:
            return None
        return f"{prev.strftime('%Y-%m-%d')} 15:30:00"
    except Exception:
        return None


def clamp_batch_range(start: str | date, end: str | date, max_days: int = 31) -> tuple[date, date, Optional[str]]:
    """
    일괄 생성 허용 구간을 [start, end]로 제한. 최대 max_days 일.
    오류 시 (today, today, error_msg) 형태는 호출부에서 처리.
    """
    s = _parse_day(start)
    e = _parse_day(end)
    if s > e:
        return s, e, "시작일이 종료일보다 늦습니다."
    if (e - s).days > max_days - 1:
        return s, e, f"일괄 생성은 최대 {max_days}일 범위만 허용됩니다."
    return s, e, None
