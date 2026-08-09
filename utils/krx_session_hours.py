# -*- coding: utf-8 -*-
"""
KRX 일자별 정규장 세션 시각(특수 개장일 포함).

기본: 09:00 개장 / 15:20 동시호가 시작 / 15:30 종가.

우선순위:
1. ``krx_special_sessions.json`` 일자 오버라이드 (예외·정정)
2. 내장 자동 규칙 (신년 첫 거래일, 수능일)
3. 기본 정규장

거래일 여부(휴장)는 ``reporting.krx_trading_calendar`` 를 사용한다.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)

# 기본 정규장
DEFAULT_OPEN = time(9, 0)
DEFAULT_CONTINUOUS_END = time(15, 20)
DEFAULT_CLOSE = time(15, 30)

# 신년 첫 거래일: 개장만 1시간 순연
NEW_YEAR_OPEN = time(10, 0)
NEW_YEAR_CONTINUOUS_END = time(15, 20)
NEW_YEAR_CLOSE = time(15, 30)

# 수능일: 정규장 전체 1시간 순연
CSAT_OPEN = time(10, 0)
CSAT_CONTINUOUS_END = time(16, 20)
CSAT_CLOSE = time(16, 30)

# 알려진 수능일(거래일). JSON ``_csat_dates`` 로 추가·덮어씀.
_BUILTIN_CSAT_DATES: Set[str] = {
    "2022-11-17",
    "2023-11-16",
    "2024-11-14",
    "2025-11-13",
}

_SPECIAL_PATH = Path(__file__).resolve().parents[1] / "krx_special_sessions.json"
_special_cache: Optional[Dict[str, Dict[str, Any]]] = None
_special_meta: Dict[str, Any] = {}
_special_mtime: Optional[float] = None
_load_warned: bool = False
_first_trading_day_cache: Dict[int, Optional[date]] = {}
# 일자별 SessionHours 결과 캐시 (BT 슬롯 판정 핫패스) — 값은 SessionHours
_session_hours_cache: Dict[str, Any] = {}


def _parse_day(d: Union[str, date, datetime]) -> date:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()


def _parse_hhmm(raw: Any, *, field: str) -> time:
    """'HH:MM' 또는 'HH:MM:SS' → time."""
    s = str(raw or "").strip()
    if not s:
        raise ValueError(f"{field} 비어 있음")
    parts = s.split(":")
    if len(parts) < 2:
        raise ValueError(f"{field} 형식 오류: {raw!r}")
    h = int(parts[0])
    m = int(parts[1])
    sec = int(parts[2]) if len(parts) >= 3 else 0
    return time(h, m, sec)


def _time_to_minutes(t: time) -> int:
    return int(t.hour) * 60 + int(t.minute)


def _combine(day: date, t: time) -> datetime:
    return datetime.combine(day, t)


@dataclass(frozen=True)
class SessionHours:
    """하루 정규장 시각."""

    day: date
    open: time
    continuous_end: time
    close: time
    note: str = ""

    @property
    def pre_auction_slot(self) -> time:
        """연속매매 잔여 15분 라벨(기본 15:15 = close - 15분)."""
        close_dt = _combine(self.day, self.close)
        return (close_dt - timedelta(minutes=15)).time()

    @property
    def open_minutes(self) -> int:
        return _time_to_minutes(self.open)

    @property
    def continuous_end_minutes(self) -> int:
        return _time_to_minutes(self.continuous_end)

    @property
    def close_minutes(self) -> int:
        return _time_to_minutes(self.close)

    @property
    def pre_auction_slot_minutes(self) -> int:
        return _time_to_minutes(self.pre_auction_slot)

    def open_datetime(self) -> datetime:
        return _combine(self.day, self.open)

    def continuous_end_datetime(self) -> datetime:
        return _combine(self.day, self.continuous_end)

    def close_datetime(self) -> datetime:
        return _combine(self.day, self.close)

    def pre_auction_slot_datetime(self) -> datetime:
        return _combine(self.day, self.pre_auction_slot)

    def iter_15m_slot_labels(self) -> List[datetime]:
        """
        당일 15분 좌측 라벨 목록.

        open … pre_auction_slot (15분 간격) + close(단일가).
        """
        out: List[datetime] = []
        cur = self.open_datetime()
        pre = self.pre_auction_slot_datetime()
        close_dt = self.close_datetime()
        # open → pre_auction 까지 15분 간격
        while cur <= pre:
            out.append(cur)
            cur = cur + timedelta(minutes=15)
        if not out or out[-1] != close_dt:
            # close 가 pre+15 이 아닌 경우(정상: close = pre+15)에도 마지막 추가
            if not out or out[-1] < close_dt:
                out.append(close_dt)
        return out

    def is_last_slot_time(self, hm_or_t: Union[time, int]) -> bool:
        if isinstance(hm_or_t, int):
            return hm_or_t == self.close_minutes
        return _time_to_minutes(hm_or_t) == self.close_minutes

    def is_pre_auction_slot_time(self, hm_or_t: Union[time, int]) -> bool:
        if isinstance(hm_or_t, int):
            return hm_or_t == self.pre_auction_slot_minutes
        return _time_to_minutes(hm_or_t) == self.pre_auction_slot_minutes


def default_session_hours(day: Union[str, date, datetime]) -> SessionHours:
    """기본 정규장(09:00~15:30)."""
    d = _parse_day(day)
    return SessionHours(
        day=d,
        open=DEFAULT_OPEN,
        continuous_end=DEFAULT_CONTINUOUS_END,
        close=DEFAULT_CLOSE,
        note="",
    )


def _validate_hours(open_t: time, cont_t: time, close_t: time) -> None:
    om = _time_to_minutes(open_t)
    cm = _time_to_minutes(cont_t)
    xm = _time_to_minutes(close_t)
    if not (0 <= om < cm < xm <= 23 * 60 + 59):
        raise ValueError(
            f"세션 시각 순서 오류: open={open_t} continuous_end={cont_t} close={close_t}"
        )
    # 종가 단일가는 동시호가 시작 + 10분(관례) — 엄격하진 않되 close > continuous_end
    if xm - cm < 5:
        raise ValueError(
            f"continuous_end~close 간격이 너무 짧음: {cont_t} ~ {close_t}"
        )


def _load_special_map(*, force: bool = False) -> Dict[str, Dict[str, Any]]:
    """특수일 JSON 로드(mtime 캐시). 실패 시 {}."""
    global _special_cache, _special_meta, _special_mtime, _load_warned
    try:
        path = _SPECIAL_PATH
        if not path.is_file():
            _special_cache = {}
            _special_meta = {}
            _special_mtime = None
            return _special_cache
        mtime = float(path.stat().st_mtime)
        if (
            not force
            and _special_cache is not None
            and _special_mtime is not None
            and mtime == _special_mtime
        ):
            return _special_cache
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError("루트는 object 여야 함")
        cleaned: Dict[str, Dict[str, Any]] = {}
        meta: Dict[str, Any] = {}
        for k, v in raw.items():
            ks = str(k)
            if ks.startswith("_"):
                meta[ks] = v
                continue
            if not isinstance(v, dict):
                continue
            cleaned[ks[:10]] = v
        _special_cache = cleaned
        _special_meta = meta
        _special_mtime = mtime
        return _special_cache
    except Exception as e:
        if not _load_warned:
            _load_warned = True
            try:
                logger.warning("krx_special_sessions.json 로드 실패 — 기본/자동 규칙 사용: %s", e)
            except Exception:
                pass
            try:
                print(f"[Warn] krx_special_sessions.json 로드 실패 — 기본/자동 규칙 사용: {e}")
            except Exception:
                pass
        _special_cache = {}
        _special_meta = {}
        _special_mtime = None
        return _special_cache


def _get_special_meta() -> Dict[str, Any]:
    _load_special_map()
    return _special_meta


def reload_special_sessions() -> None:
    """테스트·수동 갱신용 캐시 무효화."""
    global _special_cache, _special_meta, _special_mtime, _load_warned, _first_trading_day_cache
    global _session_hours_cache
    _special_cache = None
    _special_meta = {}
    _special_mtime = None
    _load_warned = False
    _first_trading_day_cache = {}
    _session_hours_cache = {}
    _load_special_map(force=True)


def _auto_rules_enabled() -> Tuple[bool, bool]:
    """
    JSON ``_auto_rules`` 플래그.

    반환: (신년 첫 거래일 자동, 수능 자동)
    """
    try:
        meta = _get_special_meta().get("_auto_rules")
        if not isinstance(meta, dict):
            return True, True
        ny = bool(meta.get("new_year_first_trading_day", True))
        csat = bool(meta.get("csat", True))
        return ny, csat
    except Exception:
        return True, True


def _csat_date_set() -> Set[str]:
    """내장 + JSON ``_csat_dates`` 합집합."""
    out = set(_BUILTIN_CSAT_DATES)
    try:
        raw = _get_special_meta().get("_csat_dates")
        if isinstance(raw, list):
            for item in raw:
                s = str(item or "").strip()[:10]
                if len(s) == 10:
                    out.add(s)
        elif isinstance(raw, dict):
            # {"2026": "2026-11-12"} 형태도 허용
            for v in raw.values():
                s = str(v or "").strip()[:10]
                if len(s) == 10:
                    out.add(s)
    except Exception:
        pass
    return out


def third_thursday_of_november(year: int) -> date:
    """11월 셋째 목요일(휴리스틱)."""
    d = date(year, 11, 1)
    # 월요일=0 … 목요일=3
    offset = (3 - d.weekday()) % 7
    first_thu = d + timedelta(days=offset)
    return first_thu + timedelta(weeks=2)


def first_trading_day_of_year(year: int) -> Optional[date]:
    """해당 연도의 첫 KRX 거래일."""
    if year in _first_trading_day_cache:
        return _first_trading_day_cache[year]
    result: Optional[date] = None
    try:
        from reporting.krx_trading_calendar import is_trading_day, next_trading_day

        jan1 = date(year, 1, 1)
        if is_trading_day(jan1):
            result = jan1
        else:
            result = next_trading_day(jan1, max_lookahead=15)
    except Exception:
        # 폴백: 1/1 이후 첫 평일
        try:
            d = date(year, 1, 1)
            for _ in range(15):
                if d.weekday() < 5:
                    result = d
                    break
                d += timedelta(days=1)
        except Exception:
            result = None
    _first_trading_day_cache[year] = result
    return result


def _hours_from_mapping(
    d: date,
    mapping: Dict[str, Any],
    *,
    default_note: str = "",
) -> SessionHours:
    open_t = _parse_hhmm(mapping.get("open"), field="open")
    cont_t = _parse_hhmm(
        mapping.get("continuous_end", mapping.get("auction_start")),
        field="continuous_end",
    )
    close_t = _parse_hhmm(mapping.get("close"), field="close")
    _validate_hours(open_t, cont_t, close_t)
    note = str(mapping.get("note") or default_note or "")
    return SessionHours(
        day=d,
        open=open_t,
        continuous_end=cont_t,
        close=close_t,
        note=note,
    )


def _auto_rule_session_hours(d: date) -> Optional[SessionHours]:
    """
    내장 자동 규칙. 해당 없으면 None.

    - 신년 첫 거래일 → 10:00~15:30
    - 수능일(확정 목록 또는 11월 셋째 목 휴리스틱) → 10:00~16:30
    """
    ny_on, csat_on = _auto_rules_enabled()
    key = d.strftime("%Y-%m-%d")

    if ny_on:
        try:
            first = first_trading_day_of_year(d.year)
            if first is not None and d == first:
                return SessionHours(
                    day=d,
                    open=NEW_YEAR_OPEN,
                    continuous_end=NEW_YEAR_CONTINUOUS_END,
                    close=NEW_YEAR_CLOSE,
                    note="자동:신년 첫 거래일(개장 1시간 순연)",
                )
        except Exception:
            pass

    if csat_on:
        try:
            known = _csat_date_set()
            if key in known:
                return SessionHours(
                    day=d,
                    open=CSAT_OPEN,
                    continuous_end=CSAT_CONTINUOUS_END,
                    close=CSAT_CLOSE,
                    note="자동:수능일(정규장 1시간 순연)",
                )
            # 확정 목록에 없는 해만 휴리스틱(오탐 가능 → JSON으로 정정)
            years_covered = {int(s[:4]) for s in known if len(s) >= 4 and s[:4].isdigit()}
            if d.year not in years_covered and d.month == 11:
                heur = third_thursday_of_november(d.year)
                if d == heur:
                    return SessionHours(
                        day=d,
                        open=CSAT_OPEN,
                        continuous_end=CSAT_CONTINUOUS_END,
                        close=CSAT_CLOSE,
                        note="자동:수능일(11월 셋째 목 휴리스틱·확인 권장)",
                    )
        except Exception:
            pass

    return None


def get_session_hours(day: Union[str, date, datetime]) -> SessionHours:
    """
    해당일의 SessionHours.

    우선순위: JSON 일자 오버라이드 → 자동 규칙(신년/수능) → 기본값.
    JSON에서 ``use_default: true`` 이면 자동 규칙을 끄고 기본 정규장을 강제한다.
    """
    d = _parse_day(day)
    key = d.strftime("%Y-%m-%d")
    try:
        cached = _session_hours_cache.get(key)
        if cached is not None:
            return cached
    except Exception:
        pass

    special = _load_special_map().get(key)
    result: Optional[SessionHours] = None

    # 1) JSON 일자 오버라이드
    if isinstance(special, dict):
        try:
            if bool(special.get("use_default") or special.get("disabled")):
                h = default_session_hours(d)
                note = str(special.get("note") or "JSON:기본 정규장 강제")
                result = SessionHours(
                    day=h.day,
                    open=h.open,
                    continuous_end=h.continuous_end,
                    close=h.close,
                    note=note,
                )
            else:
                result = _hours_from_mapping(d, special)
        except Exception as e:
            try:
                logger.warning("특수 세션 %s 파싱 실패 — 자동/기본값 시도: %s", key, e)
            except Exception:
                pass
            try:
                print(f"[Warn] 특수 세션 {key} 파싱 실패 — 자동/기본값 시도: {e}")
            except Exception:
                pass

    # 2) 자동 규칙
    if result is None:
        try:
            auto = _auto_rule_session_hours(d)
            if auto is not None:
                result = auto
        except Exception:
            pass

    # 3) 기본
    if result is None:
        result = default_session_hours(d)

    try:
        _session_hours_cache[key] = result
    except Exception:
        pass
    return result


def is_in_regular_session(
    now: Optional[datetime] = None,
    *,
    hours: Optional[SessionHours] = None,
    require_trading_day: bool = False,
) -> bool:
    """
    정규장 구간이면 True (open ≤ now ≤ close, 분 단위).

    ``require_trading_day=True`` 이면 KRX 거래일만 True.
    주말은 기본적으로 False(거래일 검사 없이도 weekday>=5 차단).
    """
    try:
        t = now if now is not None else datetime.now()
        if t.weekday() >= 5:
            return False
        if require_trading_day:
            try:
                from reporting.krx_trading_calendar import is_trading_day

                if not is_trading_day(t.date()):
                    return False
            except Exception:
                pass
        h = hours if hours is not None else get_session_hours(t.date())
        hm = t.hour * 60 + t.minute
        return h.open_minutes <= hm <= h.close_minutes
    except Exception:
        return True


def session_close_datetime(day: Union[str, date, datetime]) -> datetime:
    """해당일 정규장 종가(단일가) datetime."""
    return get_session_hours(day).close_datetime()


def session_open_datetime(day: Union[str, date, datetime]) -> datetime:
    """해당일 정규장 개장 datetime."""
    return get_session_hours(day).open_datetime()
