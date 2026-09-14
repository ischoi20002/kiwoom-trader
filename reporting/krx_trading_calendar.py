# -*- coding: utf-8 -*-
"""
KRX(XKRX) 거래일 판정 — pandas_market_calendars + 로컬 휴장 보정.

휴장일·주말은 스킵, 라이브러리 미설치 시 폴백(주말만 제외).
XKRX가 놓친 대체공휴일은 양력 법정공휴일 규칙과
``krx_closed_day_overrides.json`` 으로 보정한다.
"""
from __future__ import annotations

import json
import warnings
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    import pandas_market_calendars as mcal

    _HAS_MCAL = True
except Exception:
    _HAS_MCAL = False

# XKRX 캘린더는 생성 비용·경고가 있어 프로세스당 1회만 로드합니다.
_xkrx_cal_cache: Optional[Any] = None

# 양력 법정공휴일 — 토·일에 걸리면 다음 월요일(평일)이 대체공휴일
# (삼일절·어린이날·광복절·개천절·한글날, 2023년 개정 기준)
_SOLAR_PUBLIC_HOLIDAYS: tuple[tuple[int, int], ...] = (
    (3, 1),
    (5, 5),
    (8, 15),
    (10, 3),
    (10, 9),
)

_OVERRIDE_PATH = Path(__file__).resolve().parents[1] / "krx_closed_day_overrides.json"
_override_closed: Set[date] = set()
_override_open: Set[date] = set()
_override_mtime: Optional[float] = None
_solar_sub_cache: Dict[int, Set[date]] = {}


def _parse_day(d: str | date) -> date:
    if isinstance(d, datetime):
        return d.date()
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


def _solar_substitute_days(year: int) -> Set[date]:
    """해당 연도 양력 공휴일이 주말에 걸린 경우의 대체공휴일."""
    cached = _solar_sub_cache.get(year)
    if cached is not None:
        return cached
    out: Set[date] = set()
    try:
        for month, day in _SOLAR_PUBLIC_HOLIDAYS:
            try:
                hol = date(year, month, day)
            except ValueError:
                continue
            if hol.weekday() == 5:
                sub = hol + timedelta(days=2)
            elif hol.weekday() == 6:
                sub = hol + timedelta(days=1)
            else:
                continue
            while sub.weekday() >= 5:
                sub += timedelta(days=1)
            out.add(sub)
    except Exception:
        out = set()
    _solar_sub_cache[year] = out
    return out


def _reload_closed_day_overrides(*, force: bool = False) -> None:
    """``krx_closed_day_overrides.json`` 을 mtime 기준으로 로드."""
    global _override_closed, _override_open, _override_mtime
    try:
        p = _OVERRIDE_PATH
        mt: Optional[float] = p.stat().st_mtime if p.is_file() else None
    except Exception:
        mt = None
    if not force and mt == _override_mtime:
        return
    closed: Set[date] = set()
    opened: Set[date] = set()
    try:
        if mt is not None:
            raw = json.loads(_OVERRIDE_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key, dest in (("closed", closed), ("open", opened)):
                    arr = raw.get(key, [])
                    if not isinstance(arr, list):
                        continue
                    for item in arr:
                        try:
                            dest.add(_parse_day(item))
                        except Exception:
                            continue
    except Exception:
        closed, opened = set(), set()
    _override_closed = closed
    _override_open = opened
    _override_mtime = mt


def is_trading_day(day: str | date) -> bool:
    """해당 일자가 KRX 거래일이면 True."""
    d = _parse_day(day)
    if d.weekday() >= 5:
        return False
    try:
        _reload_closed_day_overrides()
        if d in _override_open:
            return True
        if d in _override_closed:
            return False
        if d in _solar_substitute_days(d.year):
            return False
    except Exception:
        pass
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


def filter_ohlcv_index_to_trading_days(df: Any) -> Any:
    """
    DatetimeIndex OHLCV에서 비거래일(주말·휴장) 행을 제거.

    슬롯 롤이 넣은 휴장 가짜봉·API 플랫봉이 MA60 등을 오염시키지 않게 한다.
    """
    try:
        if df is None or getattr(df, "empty", True):
            return df
        idx = getattr(df, "index", None)
        if idx is None:
            return df
        import pandas as pd

        ts_idx = pd.DatetimeIndex(pd.to_datetime(idx, errors="coerce"))
        uniq_days = {t.date() for t in ts_idx if t is not None and not pd.isna(t)}
        ok_days = {d for d in uniq_days if is_trading_day(d)}
        if not ok_days:
            return df.iloc[0:0].copy() if hasattr(df, "iloc") else df
        mask = [(t.date() in ok_days) if t is not None and not pd.isna(t) else False for t in ts_idx]
        return df.loc[mask]
    except Exception:
        return df


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
            return [t.date() for t in vd if is_trading_day(t.date())]
        except Exception:
            pass
    # 폴백: is_trading_day (주말·대체공휴일 제외)
    out: List[date] = []
    cur = s
    while cur <= e:
        if is_trading_day(cur):
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
    - 거래일 장마감(당일 SessionHours.close) 이후: 당일
    - 거래일 장전·장중: 직전 거래일(당일 일봉은 미완)
    """
    t = now or datetime.now()
    d = t.date()
    try:
        if not is_trading_day(d):
            prev = previous_trading_day(d)
            return prev if prev is not None else d
        from utils.krx_session_hours import get_session_hours

        close_end = get_session_hours(d).close_minutes
        mins = t.hour * 60 + t.minute
        if mins > close_end:
            return d
        prev = previous_trading_day(d)
        return prev if prev is not None else d
    except Exception:
        return d


def completed_trading_day_asof_local(
    now: Optional[datetime] = None,
    *,
    eod_buffer_minutes: int = 30,
) -> date:
    """
    레짐·전환 게이트·연속감시 EOD용: 마지막으로 **완료된** 거래일.

    - 비거래일: 직전 거래일
    - 거래일 · 모니터 EOD(애프터 시행일=20:00+버퍼, 그 외=정규 close+버퍼) 이전:
      직전 거래일
    - 거래일 · 모니터 EOD 이후: 당일
    """
    t = now or datetime.now()
    d = t.date()
    try:
        buf = max(0, min(int(eod_buffer_minutes), 180))
    except (TypeError, ValueError):
        buf = 30
    try:
        if not is_trading_day(d):
            prev = previous_trading_day(d)
            return prev if prev is not None else d
        due = monitor_eod_due_datetime(d, eod_buffer_minutes=buf)
        if t >= due:
            return d
        prev = previous_trading_day(d)
        return prev if prev is not None else d
    except Exception:
        try:
            prev = previous_trading_day(d)
            return prev if prev is not None else d
        except Exception:
            return d


def monitor_eod_due_datetime(
    day: date | datetime | str,
    *,
    eod_buffer_minutes: int = 30,
) -> datetime:
    """
    연속감시 중단·일일 선정·레짐 EOD 공통 due 시각.

    애프터마켓 시행일: 애프터 종료(20:00)+버퍼(기본 20:30).
    그 외: 정규장 close+버퍼(기본 16:00).
    """
    try:
        buf = max(0, min(int(eod_buffer_minutes), 180))
    except (TypeError, ValueError):
        buf = 30
    if isinstance(day, datetime):
        d = day.date()
    elif isinstance(day, date):
        d = day
    else:
        d = datetime.strptime(str(day)[:10], "%Y-%m-%d").date()
    try:
        if has_krx_aftermarket(d):
            return aftermarket_end_datetime(d) + timedelta(minutes=buf)
        from utils.krx_session_hours import get_session_hours

        return get_session_hours(d).close_datetime() + timedelta(minutes=buf)
    except Exception:
        return datetime.combine(d, time(15, 30, 0)) + timedelta(minutes=buf)


def required_eod_bar_datetime_asof_local(now: Optional[datetime] = None) -> Optional[datetime]:
    """
    하베스팅·DB 완결성 판정용: 반드시 포함되어야 할 마지막 **1분봉** 시각.

    - 애프터마켓 시행일(2026-09-14~): 해당일 애프터 종료 직전 1분(19:59)
    - 그 이전: 정규장 SessionHours.close(기본 15:30)
    """
    try:
        req_d = required_eod_bar_date_asof_local(now)
        return minute_bar_complete_datetime_for_day(req_d)
    except Exception:
        return None


# KRX 애프터마켓(접속매매) 시행일 — 정규장 후 16:00~20:00
KRX_AFTERMARKET_START_DATE: date = date(2026, 9, 14)
# 애프터 종료 시각(벽시계). 1분봉 좌측 라벨 마지막은 직전 분(19:59).
KRX_AFTERMARKET_END: time = time(20, 0)
KRX_AFTERMARKET_LAST_M1: time = time(19, 59)
KRX_AFTERMARKET_OPEN: time = time(16, 0)
KRX_AFTERMARKET_LAST_15M: time = time(19, 45)


def has_krx_aftermarket(day: date | datetime | str) -> bool:
    """해당 영업일에 KRX 애프터마켓(20:00까지)이 적용되는지."""
    try:
        if isinstance(day, datetime):
            d = day.date()
        elif isinstance(day, date):
            d = day
        else:
            d = datetime.strptime(str(day)[:10], "%Y-%m-%d").date()
        return d >= KRX_AFTERMARKET_START_DATE
    except Exception:
        return False


def aftermarket_end_datetime(day: date | datetime | str) -> datetime:
    """애프터마켓 종료 벽시계(당일 20:00)."""
    if isinstance(day, datetime):
        d = day.date()
    elif isinstance(day, date):
        d = day
    else:
        d = datetime.strptime(str(day)[:10], "%Y-%m-%d").date()
    return datetime.combine(d, KRX_AFTERMARKET_END)


def aftermarket_last_m1_datetime(day: date | datetime | str) -> datetime:
    """
    애프터마켓 구간 마지막 1분봉 **좌측 라벨**(당일 19:59).

    20:00 종료이므로 마지막 분은 19:59~20:00.
    """
    if isinstance(day, datetime):
        d = day.date()
    elif isinstance(day, date):
        d = day
    else:
        d = datetime.strptime(str(day)[:10], "%Y-%m-%d").date()
    return datetime.combine(d, KRX_AFTERMARKET_LAST_M1)


def minute_bar_complete_datetime_for_day(day: date | datetime | str) -> datetime:
    """
    해당 거래일 분봉 하베스트 완결에 필요한 마지막 1분봉 시각.

    애프터마켓일: 19:59 / 그 외: 정규장 close(15:30 등).
    """
    from utils.krx_session_hours import get_session_hours

    if isinstance(day, datetime):
        d = day.date()
    elif isinstance(day, date):
        d = day
    else:
        d = datetime.strptime(str(day)[:10], "%Y-%m-%d").date()
    if has_krx_aftermarket(d):
        return aftermarket_last_m1_datetime(d)
    return get_session_hours(d).close_datetime()


def is_krx_regular_session_local(now: Optional[datetime] = None) -> bool:
    """
    PC 로컬 시각을 한국장 기준으로 본다고 가정할 때, KRX 정규장 구간 여부.
    일자별 SessionHours(open~close)를 사용한다.
    """
    try:
        from utils.krx_session_hours import is_in_regular_session

        return bool(is_in_regular_session(now or datetime.now()))
    except Exception:
        return False


def minute_harvest_datetime_cap_iso(now: Optional[datetime] = None) -> Optional[str]:
    """
    하베스팅용: 장중(정규장)이고 오늘이 거래일이면, DB에 넣을 수 있는 1분봉의 최대 시각(포함).
    직전 거래일 분봉 완결 시각(애프터마켓일이면 19:59) — 당일 장중 봉은 저장하지 않기 위함.
    그 외 None(제한 없음).
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
        close_dt = minute_bar_complete_datetime_for_day(prev)
        return close_dt.strftime("%Y-%m-%d %H:%M:%S")
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
