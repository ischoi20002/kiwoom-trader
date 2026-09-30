# -*- coding: utf-8 -*-
"""
KRX 장중 15분봉·1분 창의 시간 정의.

키움 ka10080(tic_scope=15·1) 과 동일하게 **좌측(시작) 라벨**을 쓴다.
일자별 개장·마감은 ``utils.krx_session_hours.SessionHours`` 를 따른다
(기본 09:00~15:30, 수능·신년 등은 ``krx_special_sessions.json``).

1분봉 → 15분봉 합성은 봉 개수가 아니라 타임스탬프 구간으로 한다.

- 일반 슬롯(라벨 ``T``): ``T ≤ ts < T+15분``
- 동시호가 직전 슬롯(``pre_auction``): ``T ≤ ts < continuous_end``
- 종가 단일가 슬롯(``close``): ``ts == close``

신호 평가: 종가 단일가 슬롯만 ``is_krx_no_signal_eval_15m_slot`` 으로 스킵.
직전 슬롯은 동시호가 시장가 → 당일 종가 체결(``is_krx_auction_market_fill_slot``).
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Any, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

from utils.krx_session_hours import SessionHours, get_session_hours
from utils.normalization import normalize_code

__all__ = [
    "m1_window_start_for_15m_end",
    "m1_continuous_end_exclusive_for_15m_end",
    "m1_closing_auction_ts_for_15m_end",
    "m1_index_bounds_for_15m_end",
    "m1_index_ranges_for_15m_end",
    "m1_mask_for_15m_end",
    "slice_m1_for_15m_end",
    "krx_15m_slot_complete_ts",
    "m1_bar_open_label_for_just_completed_minute",
    "m1_bar_end_ts_for_closed_open",
    "m1_ka10080_cntr_tm_for_closed_open",
    "m1_open_label_from_ka10080_cntr_tm",
    "chart_covers_m1_closed_minute",
    "is_last_m1_open_of_15m_slot",
    "is_pre_auction_last_m1_open",
    "krx_15m_label_for_m1_open",
    "is_krx_m1_closed_open_label",
    "is_krx_last_15m_slot",
    "is_krx_aftermarket_last_15m_slot",
    "is_krx_first_15m_slot",
    "is_krx_pre_auction_15m_slot",
    "is_krx_no_signal_eval_15m_slot",
    "is_krx_auction_market_fill_slot",
    "is_krx_next_open_defer_15m_slot",
    "is_krx_after_close_15m_slot",
    "settings_allow_after_close_orders",
    "settings_allow_1530_orders",
    "after_close_orders_blocked_now",
    "krx_same_day_1530_label",
    "iter_krx_15m_slot_ends_for_day",
    "resample_1m_ohlcv_to_15m_krx",
    "KRX_15M_RESAMPLE_KWARGS",
    "is_krx_15m_slot_end",
    "krx_stream_slot_end_datetime",
    "next_krx_15m_slot_end",
    "prev_krx_15m_slot_end",
    "last_completed_krx_15m_slot_end",
    "seconds_until_next_krx_15m_boundary",
]

# 레거시 이름. 공식은 좌측 라벨(시작 시각). pandas label=left, closed=left 에 가깝다.
KRX_15M_RESAMPLE_KWARGS: dict = {"label": "left", "closed": "left"}

# 레거시 상수(기본일). 신규 로직은 SessionHours 를 사용한다.
_KRX_FIRST_15M_END = time(9, 0)
_KRX_LAST_15M_END = time(15, 30)


def _hours_for_ts(ts: Union[pd.Timestamp, datetime, str]) -> SessionHours:
    """타임스탬프 캘린더일의 SessionHours."""
    try:
        d = pd.Timestamp(ts).date()
    except Exception:
        d = datetime.now().date()
    return get_session_hours(d)


def _pd_at(day0: pd.Timestamp, t: time) -> pd.Timestamp:
    return day0.normalize() + pd.Timedelta(hours=int(t.hour), minutes=int(t.minute))


def _krx_day_first_slot_end(day0: pd.Timestamp) -> pd.Timestamp:
    """당일 첫 15분 라벨(개장)."""
    h = get_session_hours(day0.date())
    return _pd_at(day0, h.open)


def _krx_day_last_slot_end(day0: pd.Timestamp) -> pd.Timestamp:
    """당일 마지막 15분 라벨(종가 단일가)."""
    h = get_session_hours(day0.date())
    return _pd_at(day0, h.close)


def _is_krx_session_day(ts: Union[pd.Timestamp, datetime, str]) -> bool:
    """KRX 정규 거래일이면 True. 주말·휴장(대체공휴일 포함)은 False."""
    try:
        d = pd.Timestamp(ts).date()
        from reporting.krx_trading_calendar import is_trading_day

        return bool(is_trading_day(d))
    except Exception:
        try:
            return int(pd.Timestamp(ts).weekday()) < 5
        except Exception:
            return True


def _next_trading_day_first_slot_after(t: pd.Timestamp) -> pd.Timestamp:
    """``t`` 날짜 **다음날**부터 찾은 첫 거래일 개장 라벨 (주말·휴장 스킵)."""
    try:
        d = pd.Timestamp(t).normalize() + pd.Timedelta(days=1)
        for _ in range(16):
            if _is_krx_session_day(d):
                return _krx_day_first_slot_end(d)
            d = d + pd.Timedelta(days=1)
    except Exception:
        pass
    return _krx_day_first_slot_end(pd.Timestamp(t).normalize() + pd.Timedelta(days=1))


def _next_weekday_first_slot_after(t: pd.Timestamp) -> pd.Timestamp:
    """레거시 별칭 — 거래일 개장 라벨."""
    return _next_trading_day_first_slot_after(t)


def _ah_enabled(day0: pd.Timestamp) -> bool:
    try:
        from reporting.krx_trading_calendar import has_krx_aftermarket

        return bool(has_krx_aftermarket(day0.date()))
    except Exception:
        return False


def _ah_open_minutes() -> int:
    return 16 * 60


def _ah_end_minutes() -> int:
    return 20 * 60


def _ah_last_15m_minutes() -> int:
    return 19 * 60 + 45


def _is_aftermarket_15m_label(t: pd.Timestamp) -> bool:
    """애프터 15분 좌측 라벨(정규 close 이후 · 16:00…19:45) 여부."""
    try:
        if not _ah_enabled(t.normalize()):
            return False
        h = _hours_for_ts(t)
        hm = int(t.hour) * 60 + int(t.minute)
        if hm <= h.close_minutes:
            return False
        if hm < _ah_open_minutes() or hm > _ah_last_15m_minutes():
            return False
        return ((hm - _ah_open_minutes()) % 15) == 0
    except Exception:
        return False


def _is_aftermarket_m1_label(t: pd.Timestamp) -> bool:
    """애프터 연속매매 1분 라벨(정규 close 이후 · ~19:59) 여부."""
    try:
        if not _ah_enabled(t.normalize()):
            return False
        h = _hours_for_ts(t)
        hm = int(t.hour) * 60 + int(t.minute)
        if hm <= h.close_minutes:
            return False
        return max(_ah_open_minutes(), h.close_minutes + 1) <= hm <= (
            _ah_end_minutes() - 1
        )
    except Exception:
        return False


def _krx_day_aftermarket_first_slot(day0: pd.Timestamp) -> pd.Timestamp:
    """당일 첫 애프터 15분 라벨(없으면 16:00 — 호출부에서 AH 여부 확인)."""
    try:
        from utils.krx_session_hours import iter_aftermarket_15m_slot_labels

        labs = iter_aftermarket_15m_slot_labels(day0.date())
        if labs:
            return pd.Timestamp(labs[0])
    except Exception:
        pass
    return day0.normalize() + pd.Timedelta(hours=16, minutes=0)


def _krx_day_aftermarket_last_slot(day0: pd.Timestamp) -> pd.Timestamp:
    return day0.normalize() + pd.Timedelta(hours=19, minutes=45)


def _krx_day_last_tradable_slot_end(day0: pd.Timestamp) -> pd.Timestamp:
    """당일 마지막 평가 가능 15분 라벨(애프터일이면 19:45, 아니면 종가)."""
    if _ah_enabled(day0):
        return _krx_day_aftermarket_last_slot(day0)
    return _krx_day_last_slot_end(day0)


def is_krx_15m_slot_end(ts: Union[pd.Timestamp, datetime, str]) -> bool:
    """
    KRX/키움 공식 15분봉 **라벨**(정규장 SessionHours 격자 + 애프터 16슬롯) 여부.

    주말·휴장(대체공휴일 포함)과 그 외 장외·야간 슬롯은 False.
    """
    try:
        t = pd.Timestamp(ts).floor("s")
        if not _is_krx_session_day(t):
            return False
        if t.second != 0 or t.microsecond != 0:
            return False
        if _is_aftermarket_15m_label(t):
            return True
        h = _hours_for_ts(t)
        hm = int(t.hour) * 60 + int(t.minute)
        if hm == h.close_minutes:
            return True
        if hm < h.open_minutes or hm > h.pre_auction_slot_minutes:
            return False
        return ((hm - h.open_minutes) % 15) == 0
    except Exception:
        return False


def is_krx_last_15m_slot(ts: Union[pd.Timestamp, datetime, str]) -> bool:
    """라벨이 당일 마지막 15분봉(종가 단일가)인지."""
    try:
        t = pd.Timestamp(ts)
        if not _is_krx_session_day(t):
            return False
        h = _hours_for_ts(t)
        return h.is_last_slot_time(int(t.hour) * 60 + int(t.minute))
    except Exception:
        return False


def is_krx_aftermarket_last_15m_slot(
    ts: Union[pd.Timestamp, datetime, str],
) -> bool:
    """
    애프터마켓 마지막 15분 라벨(기본 19:45)인지.

    정규 종가 단일가(15:30)와 별개. 완성 시각은 20:00.
    """
    try:
        t = pd.Timestamp(ts)
        if not _is_krx_session_day(t):
            return False
        if not _ah_enabled(t.normalize()):
            return False
        return bool(t == _krx_day_aftermarket_last_slot(t.normalize()))
    except Exception:
        return False


def is_krx_first_15m_slot(ts: Union[pd.Timestamp, datetime, str]) -> bool:
    """라벨이 당일 첫 15분봉(개장)인지."""
    try:
        t = pd.Timestamp(ts)
        if not _is_krx_session_day(t):
            return False
        h = _hours_for_ts(t)
        return int(t.hour) * 60 + int(t.minute) == h.open_minutes
    except Exception:
        return False


def is_krx_pre_auction_15m_slot(ts: Union[pd.Timestamp, datetime, str]) -> bool:
    """라벨이 연속매매 마지막 잔여 구간(기본 15:15)인지."""
    try:
        t = pd.Timestamp(ts)
        h = _hours_for_ts(t)
        return h.is_pre_auction_slot_time(int(t.hour) * 60 + int(t.minute))
    except Exception:
        return False


def is_krx_no_signal_eval_15m_slot(ts: Union[pd.Timestamp, datetime, str]) -> bool:
    """
    완성 15분 신호 평가(S1/S2/청산 콜백)를 하지 않는 슬롯인지.

    종가 단일가 슬롯만 스킵. 직전 슬롯은 평가 후 동시호가 시장가→종가 체결.
    """
    try:
        return bool(is_krx_last_15m_slot(ts))
    except Exception:
        return False


def is_krx_auction_market_fill_slot(ts: Union[pd.Timestamp, datetime, str]) -> bool:
    """
    동시호가 직전 슬롯 주문을 시장가로 내어 당일 종가로 체결할지.
    """
    try:
        return bool(is_krx_pre_auction_15m_slot(ts))
    except Exception:
        return False


def is_krx_next_open_defer_15m_slot(ts: Union[pd.Timestamp, datetime, str]) -> bool:
    """
    완성봉 신호를 익일 시가(동시호가 지정가)로 이월할 슬롯인지.

    정규 종가 단일가(15:30) 또는 애프터 마지막(19:45).
    """
    try:
        return bool(is_krx_last_15m_slot(ts) or is_krx_aftermarket_last_15m_slot(ts))
    except Exception:
        return False


def is_krx_after_close_15m_slot(ts: Union[pd.Timestamp, datetime, str]) -> bool:
    """
    정규장 마감 이후 15분 라벨인지.

    종가 단일가(기본 15:30) 슬롯 + 애프터마켓 슬롯(16:00…19:45) 전부.
    ``order_after_close_enabled`` OFF 시 주문·체결 생략 대상.
    """
    try:
        t = pd.Timestamp(ts)
        return bool(is_krx_last_15m_slot(t) or _is_aftermarket_15m_label(t))
    except Exception:
        return False


def settings_allow_after_close_orders(settings: Optional[dict]) -> bool:
    """
    정규장 마감(15:30) 이후 주문 허용 여부(``order_after_close_enabled``, 기본 True).

    OFF면 15:30 슬롯·애프터 슬롯의 당일 주문·NXT 터치·익일 시초 예약을 모두 생략하고
    신호만 기록한다(BT는 해당 슬롯 체결 생략).
    """
    try:
        st = settings or {}
        raw = st.get("order_after_close_enabled", True)
        if isinstance(raw, str):
            return raw.strip().lower() not in ("0", "false", "off", "no", "")
        return bool(raw)
    except Exception:
        return True


def settings_allow_1530_orders(settings: Optional[dict]) -> bool:
    """
    15:30(종가 단일가) 슬롯 주문 허용 여부 — RT·BT 공통 판정.

    ``order_after_close_enabled``(기본 True) AND ``order_1530_enabled``(기본 False).
    OFF면 RT는 신호만 기록, BT는 15:30 슬롯 체결(익일 시가 이월 포함)을 생략한다.
    """
    try:
        if not settings_allow_after_close_orders(settings):
            return False
        st = settings or {}
        raw = st.get("order_1530_enabled", False)
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "on", "yes")
        return bool(raw)
    except Exception:
        return False


def after_close_orders_blocked_now(
    settings: Optional[dict], now: Optional[datetime] = None
) -> bool:
    """
    ``order_after_close_enabled`` OFF + 거래일 정규장 종가(close) 이후(당일)면 True.

    주문 집행·미체결 정정 루프의 벽시계 최종 방어선.
    """
    try:
        if settings_allow_after_close_orders(settings):
            return False
        t = now if now is not None else datetime.now()
        if t.weekday() >= 5:
            return False
        try:
            from reporting.krx_trading_calendar import is_trading_day

            if not is_trading_day(t.date()):
                return False
        except Exception:
            pass
        return bool(t >= get_session_hours(t.date()).close_datetime())
    except Exception:
        return False


def krx_same_day_1530_label(
    ts: Union[pd.Timestamp, datetime, str],
) -> Optional[pd.Timestamp]:
    """``ts`` 와 같은 캘린더일의 종가 단일가 좌측 라벨(기본 15:30)."""
    try:
        t = pd.Timestamp(ts)
        return _krx_day_last_slot_end(t.normalize())
    except Exception:
        return None


def iter_krx_15m_slot_ends_for_day(day: Union[pd.Timestamp, datetime, str]) -> List[pd.Timestamp]:
    """해당 캘린더일의 KRX/키움 15분 라벨 목록(개장 … 종가 [+ 애프터 16슬롯])."""
    try:
        day0 = pd.Timestamp(day).normalize()
        h = get_session_hours(day0.date())
        out = [pd.Timestamp(x) for x in h.iter_15m_slot_labels()]
        try:
            from utils.krx_session_hours import iter_aftermarket_15m_slot_labels

            for x in iter_aftermarket_15m_slot_labels(day0.date()):
                out.append(pd.Timestamp(x))
        except Exception:
            pass
        return out
    except Exception:
        return []


def krx_15m_slot_complete_ts(
    label_ts: Union[pd.Timestamp, datetime, str],
) -> pd.Timestamp:
    """
    좌측 라벨 ``T`` 봉이 **완성되는 벽시계 시각**.

    - 일반: ``T+15분``
    - 동시호가 직전: ``continuous_end``
    - 종가 단일가: ``close``
    """
    t = pd.Timestamp(label_ts)
    h = _hours_for_ts(t)
    day0 = t.normalize()
    if is_krx_last_15m_slot(t):
        return t.floor("min")
    if is_krx_pre_auction_15m_slot(t):
        return _pd_at(day0, h.continuous_end)
    return t.floor("min") + pd.Timedelta(minutes=15)


def krx_stream_slot_end_datetime(now: Optional[datetime] = None) -> pd.Timestamp:
    """
    벽시계 기준 **현재 형성 중** 15분봉의 **좌측 라벨**.

    정규장 SessionHours + (시행일) 애프터 16:00~19:45.
    """
    try:
        raw = now if now is not None else datetime.now()
        t = pd.Timestamp(raw)
        day0 = t.normalize()
        if not _is_krx_session_day(day0):
            d = day0
            for _ in range(16):
                d = d - pd.Timedelta(days=1)
                if _is_krx_session_day(d):
                    return _krx_day_last_tradable_slot_end(d)
            return _krx_day_last_tradable_slot_end(day0 - pd.Timedelta(days=1))

        h = get_session_hours(day0.date())
        hm = int(t.hour) * 60 + int(t.minute)

        # 애프터 구간(정규 종가 슬롯 이후만)
        if (
            _ah_enabled(day0)
            and hm > h.close_minutes
            and hm >= _ah_open_minutes()
        ):
            if hm >= _ah_end_minutes():
                return _krx_day_aftermarket_last_slot(day0)
            offset = hm - _ah_open_minutes()
            slot_min = _ah_open_minutes() + (offset // 15) * 15
            if slot_min <= h.close_minutes:
                slot_min = (
                    ((h.close_minutes // 15) + 1) * 15
                )
            if slot_min > _ah_last_15m_minutes():
                return _krx_day_aftermarket_last_slot(day0)
            return day0 + pd.Timedelta(minutes=int(slot_min))

        if hm < h.open_minutes:
            return _krx_day_first_slot_end(day0)
        if hm >= h.close_minutes:
            # 종가 후 ~ 애프터 개장 전: 종가 슬롯 유지
            return _krx_day_last_slot_end(day0)
        # 연속매매 종료 후 ~ 종가: 단일가 슬롯 형성 (15:15는 이미 완성)
        if hm >= h.continuous_end_minutes:
            return _krx_day_last_slot_end(day0)
        if hm >= h.pre_auction_slot_minutes:
            return _pd_at(day0, h.pre_auction_slot)

        offset = hm - h.open_minutes
        slot_min = h.open_minutes + (offset // 15) * 15
        return day0 + pd.Timedelta(minutes=int(slot_min))
    except Exception:
        try:
            return pd.Timestamp(now or datetime.now()).floor("15min")
        except Exception:
            return pd.Timestamp.now().floor("15min")


def next_krx_15m_slot_end(
    ts: Union[pd.Timestamp, datetime, str],
) -> Optional[pd.Timestamp]:
    """
    ``ts`` **이후** 첫 KRX 15분봉 라벨.

    종가(15:30) 다음은 애프터일이면 당일 ``16:00``, 아니면 다음 거래일 개장.
    애프터 마지막(19:45) 다음은 다음 거래일 개장.
    """
    try:
        t = pd.Timestamp(ts).floor("s")
        day0 = t.normalize()

        if not _is_krx_session_day(day0):
            return _next_trading_day_first_slot_after(t)

        h = get_session_hours(day0.date())
        hm = int(t.hour) * 60 + int(t.minute)
        last_end = _krx_day_last_slot_end(day0)
        first_end = _krx_day_first_slot_end(day0)
        ah_on = _ah_enabled(day0)
        ah_first = _krx_day_aftermarket_first_slot(day0) if ah_on else None
        ah_last = _krx_day_aftermarket_last_slot(day0) if ah_on else None

        # 애프터 구간 이후 → 익일 개장
        if ah_on and hm >= _ah_end_minutes():
            return _next_trading_day_first_slot_after(t)
        if ah_on and is_krx_15m_slot_end(t) and _is_aftermarket_15m_label(t):
            if t.floor("min") >= ah_last.floor("min"):
                return _next_trading_day_first_slot_after(t)
            nxt = t + pd.Timedelta(minutes=15)
            if _is_aftermarket_15m_label(nxt):
                return nxt
            return _next_trading_day_first_slot_after(t)
        if ah_on and hm >= _ah_open_minutes():
            # 애프터 중간(슬롯 경계가 아닌 시각)
            forming = krx_stream_slot_end_datetime(t.to_pydatetime())
            if forming > t:
                return forming
            return next_krx_15m_slot_end(forming)

        # 종가 슬롯 시각 이상 ~ 애프터 개장 전
        if hm >= h.close_minutes:
            if ah_on and ah_first is not None and t < ah_first:
                return ah_first
            if ah_on and ah_first is not None and t.floor("min") == last_end.floor("min"):
                return ah_first
            return _next_trading_day_first_slot_after(t)

        if hm < h.open_minutes:
            if t < first_end:
                return first_end
            return next_krx_15m_slot_end(first_end)

        if is_krx_15m_slot_end(t):
            # 직전 슬롯 다음은 종가 단일가(15분 격자 밖일 수 있음)
            if is_krx_pre_auction_15m_slot(t):
                return last_end
            if is_krx_last_15m_slot(t):
                if ah_on and ah_first is not None:
                    return ah_first
                return _next_trading_day_first_slot_after(t)
            nxt = t + pd.Timedelta(minutes=15)
            if is_krx_15m_slot_end(nxt) and not is_krx_last_15m_slot(nxt):
                return nxt
            if t < last_end:
                return last_end
            if ah_on and ah_first is not None:
                return ah_first
            return _next_trading_day_first_slot_after(t)

        up = krx_stream_slot_end_datetime(t.to_pydatetime())
        if up > t:
            return up
        return next_krx_15m_slot_end(up)
    except Exception:
        return None


def prev_krx_15m_slot_end(
    label_ts: Union[pd.Timestamp, datetime, str],
) -> Optional[pd.Timestamp]:
    """
    ``label_ts``(15분 좌측 라벨) **직전** KRX 15분봉 라벨.

    당일 첫 슬롯이면 ``None`` (전일 종가 슬롯으로 넘기지 않음).
    애프터 첫 슬롯(16:00)의 직전은 당일 종가(15:30).
    """
    try:
        t = pd.Timestamp(label_ts).floor("min")
        day0 = t.normalize()
        if not _is_krx_session_day(day0):
            return None
        first_end = _krx_day_first_slot_end(day0)
        if t <= first_end:
            return None
        if _is_aftermarket_15m_label(t):
            ah_first = _krx_day_aftermarket_first_slot(day0)
            if t <= ah_first:
                return _krx_day_last_slot_end(day0)
            prev = t - pd.Timedelta(minutes=15)
            if _is_aftermarket_15m_label(prev):
                return prev.floor("min")
            return _krx_day_last_slot_end(day0)
        if is_krx_last_15m_slot(t):
            h = get_session_hours(day0.date())
            return _pd_at(day0, h.pre_auction_slot)
        prev = t - pd.Timedelta(minutes=15)
        if is_krx_15m_slot_end(prev) and prev.normalize() == day0:
            return prev.floor("min")
        return None
    except Exception:
        return None


def last_completed_krx_15m_slot_end(
    now: Optional[Union[datetime, pd.Timestamp]] = None,
) -> Optional[pd.Timestamp]:
    """
    벽시계 기준 **이미 완성된** 가장 최근 당일 15분봉 라벨.

    장중 재시작 시 형성 중 슬롯만 보면 직전 완성봉 평가가 빠지므로
    catch-up 평가 대상 산정에 사용합니다.
    """
    try:
        raw = now if now is not None else datetime.now()
        t = pd.Timestamp(raw)
        day0 = t.normalize()
        if not _is_krx_session_day(day0):
            return None
        forming = pd.Timestamp(
            krx_stream_slot_end_datetime(
                t.to_pydatetime() if hasattr(t, "to_pydatetime") else t
            )
        ).floor("min")
        complete_f = krx_15m_slot_complete_ts(forming)
        if t >= pd.Timestamp(complete_f):
            slot = forming
        else:
            slot = prev_krx_15m_slot_end(forming)
        if slot is None:
            return None
        slot = pd.Timestamp(slot).floor("min")
        if slot.normalize() < day0:
            return None
        if t < pd.Timestamp(krx_15m_slot_complete_ts(slot)):
            return None
        return slot
    except Exception:
        return None


def seconds_until_next_krx_15m_boundary(
    now: Optional[Union[datetime, pd.Timestamp]] = None,
) -> float:
    """
    다음 15분봉 **완성 경계**(또는 다음 라벨 시작)까지 남은 초.

    슬롯 롤 워처의 exact wake 에 사용합니다.
    """
    try:
        raw = now if now is not None else datetime.now()
        t = pd.Timestamp(raw)
        label = krx_stream_slot_end_datetime(
            t.to_pydatetime() if hasattr(t, "to_pydatetime") else t
        )
        complete = krx_15m_slot_complete_ts(label)
        delta = float((pd.Timestamp(complete) - t).total_seconds())
        if delta > 0.001:
            return delta
        nxt = next_krx_15m_slot_end(label)
        if nxt is None:
            return 60.0
        # 다음 라벨의 완성 시각(또는 라벨 시각이 곧 경계인 15:30)
        nxt_complete = krx_15m_slot_complete_ts(nxt)
        return max(0.0, float((pd.Timestamp(nxt_complete) - t).total_seconds()))
    except Exception:
        return 1.0


def m1_window_start_for_15m_end(
    end_ts: Union[pd.Timestamp, datetime, str],
) -> pd.Timestamp:
    """
    15분봉 라벨 ``T`` 에 대응하는 1분봉 창 **시작(포함)**.

    좌측 라벨: 항상 ``T``.
    """
    return pd.Timestamp(end_ts).floor("min")


def m1_continuous_end_exclusive_for_15m_end(
    end_ts: Union[pd.Timestamp, datetime, str],
) -> pd.Timestamp:
    """
    연속매매 1분봉 창의 **끝(미포함)**.

    - 일반: ``T+15``
    - 동시호가 직전: ``continuous_end``
    - 종가 단일가: ``T`` (연속 구간 없음)
    """
    t = pd.Timestamp(end_ts).floor("min")
    if is_krx_last_15m_slot(t):
        return t
    if is_krx_pre_auction_15m_slot(t):
        h = _hours_for_ts(t)
        return _pd_at(t.normalize(), h.continuous_end)
    return t + pd.Timedelta(minutes=15)


def m1_closing_auction_ts_for_15m_end(
    end_ts: Union[pd.Timestamp, datetime, str],
) -> Optional[pd.Timestamp]:
    """종가 단일가 슬롯의 1분봉 시각. 그 외 ``None``."""
    t = pd.Timestamp(end_ts)
    if not is_krx_last_15m_slot(t):
        return None
    h = _hours_for_ts(t)
    return _pd_at(t.normalize(), h.close)


def m1_mask_for_15m_end(
    m1_index: pd.DatetimeIndex,
    end_ts_15m: Union[pd.Timestamp, datetime, str],
    *,
    until: Optional[Union[pd.Timestamp, datetime, str]] = None,
) -> np.ndarray:
    """
    라벨 ``end_ts_15m`` 슬롯에 속하는 1분봉 여부 마스크(공식 좌측 라벨 규칙).

    ``until`` 이 있으면 그 시각(포함)까지만 — 형성 중 슬롯용.
    """
    try:
        idx = pd.DatetimeIndex(pd.to_datetime(m1_index, errors="coerce"))
        t_end = pd.Timestamp(end_ts_15m)
        t0 = m1_window_start_for_15m_end(t_end)
        cont_ex = m1_continuous_end_exclusive_for_15m_end(t_end)
        auction = m1_closing_auction_ts_for_15m_end(t_end)

        mask = (idx >= t0) & (idx < cont_ex)
        if auction is not None:
            a0 = pd.Timestamp(auction).floor("min")
            mask = mask | (idx.floor("min") == a0)
        if until is not None:
            u = pd.Timestamp(until).floor("min")
            mask = mask & (idx.floor("min") <= u)
        return np.asarray(mask, dtype=bool)
    except Exception:
        try:
            return np.zeros(len(m1_index), dtype=bool)
        except Exception:
            return np.asarray([], dtype=bool)
def m1_index_ranges_for_15m_end(
    m1_index: pd.DatetimeIndex,
    end_ts_15m: Union[pd.Timestamp, datetime, str],
) -> List[Tuple[int, int]]:
    """
    슬롯에 속하는 1분 행의 half-open 인덱스 구간 목록.

    일반·갭 없는 15:30 은 보통 1개 구간. 15:20~15:29 잔여 봉이 있으면
    연속 구간과 단일가 구간이 분리된다.

    정렬된 인덱스에서는 ``searchsorted`` 로 O(log n) 구간을 잡고,
    비정렬/이상치는 마스크 폴백.
    """
    try:
        t_end = pd.Timestamp(end_ts_15m)
        t0 = m1_window_start_for_15m_end(t_end)
        cont_ex = m1_continuous_end_exclusive_for_15m_end(t_end)
        auction = m1_closing_auction_ts_for_15m_end(t_end)

        idx = pd.DatetimeIndex(pd.to_datetime(m1_index, errors="coerce"))
        if len(idx) == 0:
            return []

        # 단조 증가면 searchsorted 고속 경로
        try:
            vals = idx.asi8
            mono = bool(np.all(vals[1:] >= vals[:-1]))
        except Exception:
            mono = False

        ranges: List[Tuple[int, int]] = []
        if mono:
            i0 = int(np.searchsorted(vals, np.int64(pd.Timestamp(t0).value), side="left"))
            i1 = int(
                np.searchsorted(vals, np.int64(pd.Timestamp(cont_ex).value), side="left")
            )
            if i1 > i0:
                ranges.append((i0, i1))
            if auction is not None:
                a0 = pd.Timestamp(auction).floor("min")
                a_ns = np.int64(a0.value)
                j = int(np.searchsorted(vals, a_ns, side="left"))
                if j < len(vals) and int(vals[j]) == int(a_ns):
                    # 연속 구간에 이미 포함되면 추가하지 않음
                    if not (i0 <= j < i1):
                        ranges.append((j, j + 1))
            return ranges

        # 폴백: 마스크 → 연속 구간 압축
        mask = m1_mask_for_15m_end(idx, end_ts_15m)
        if mask.size == 0 or not bool(np.any(mask)):
            return []
        flat = np.flatnonzero(mask)
        start = int(flat[0])
        prev = start
        for j in flat[1:]:
            j = int(j)
            if j == prev + 1:
                prev = j
                continue
            ranges.append((start, prev + 1))
            start = j
            prev = j
        ranges.append((start, prev + 1))
        return ranges
    except Exception:
        return []


def m1_index_bounds_for_15m_end(
    m1_index: pd.DatetimeIndex,
    end_ts_15m: Union[pd.Timestamp, datetime, str],
) -> Tuple[int, int]:
    """
    슬롯 1분 행의 **첫 연속 구간** ``[s, e)``.

    15:30에서 갭 잔여봉이 있으면 단일가는 제외될 수 있으므로,
    전체 포함이 필요하면 ``m1_index_ranges_for_15m_end`` / ``m1_mask_for_15m_end`` 사용.
    갭이 비어 있으면 단일가까지 한 구간에 포함된다.
    """
    try:
        ranges = m1_index_ranges_for_15m_end(m1_index, end_ts_15m)
        if not ranges:
            return 0, 0
        if len(ranges) == 1:
            return ranges[0]
        # 연속+단일가: 전체가 희소하면 첫~마지막을 덮는 단일 구간은 갭을 포함하므로
        # 호출부(HP)는 갭이 비었다는 전제에서 min_s~max_e 사용 가능
        s0 = ranges[0][0]
        e1 = ranges[-1][1]
        # 중간이 모두 마스크에 없으면 contiguous OK
        mask = m1_mask_for_15m_end(m1_index, end_ts_15m)
        if s0 < e1 and bool(np.all(mask[s0:e1])):
            return s0, e1
        return ranges[0]
    except Exception:
        return 0, 0


def slice_m1_for_15m_end(
    m1_df: Any,
    end_ts_15m: Union[pd.Timestamp, datetime, str],
    *,
    until: Optional[Union[pd.Timestamp, datetime, str]] = None,
) -> Optional[pd.DataFrame]:
    """
    15분 슬롯에 속하는 1분봉 DataFrame 슬라이스(타임스탬프 마스크).

    ``until``: 형성 중일 때 상한(포함, 분 단위).
    """
    try:
        if m1_df is None or getattr(m1_df, "empty", True):
            return None
        mask = m1_mask_for_15m_end(m1_df.index, end_ts_15m, until=until)
        if mask.size != len(m1_df) or not bool(np.any(mask)):
            return m1_df.iloc[0:0].copy() if hasattr(m1_df, "iloc") else None
        return m1_df.loc[mask].copy()
    except Exception:
        return None


def m1_bar_open_label_for_just_completed_minute(
    wall: Union[pd.Timestamp, datetime, str],
) -> pd.Timestamp:
    """
    벽시계(또는 틱 ``ts_wall``) 시각 ``wall`` 기준, **방금 끝난** 1분봉의 시작 라벨.

    ``bars_1m`` 인덱스(분 시작 시각)와 맞추기 위해 ``floor(분) - 1분`` 으로 둡니다.
    예: ``wall=13:02:30`` → ``13:01:00`` (13:01~13:02 구간이 닫힘).
    """
    t = pd.Timestamp(wall)
    floored = t.floor("min")
    return floored - pd.Timedelta(minutes=1)


def is_last_m1_open_of_15m_slot(
    open_ts: Union[pd.Timestamp, datetime, str],
) -> bool:
    """
    닫힌 1분봉 open 라벨이 소속 15분 슬롯의 **마지막 연속매매 분**(또는 종가 단일가)인지.

    - 일반: ``minute % 15 == 14`` (예: 09:14, 09:29)
    - 동시호가 직전 슬롯: ``continuous_end - 1분``
    - 종가 단일가 슬롯: ``close``
    - 애프터: ``16:00~19:59`` 중 ``minute % 15 == 14`` (예: 16:14, 19:59)
    """
    try:
        t = pd.Timestamp(open_ts).floor("min")
        if t.weekday() >= 5:
            return False
        if _is_aftermarket_m1_label(t):
            return (int(t.minute) % 15) == 14
        h = _hours_for_ts(t)
        hm = int(t.hour) * 60 + int(t.minute)
        if hm == h.continuous_end_minutes - 1:
            return True
        if hm == h.close_minutes:
            return True
        if hm < h.open_minutes or hm > h.continuous_end_minutes - 1:
            return False
        # 동시호가 직전 슬롯 분은 위에서 처리; 일반 격자만
        if hm >= h.pre_auction_slot_minutes:
            return False
        return (int(t.minute) % 15) == 14
    except Exception:
        return False


def is_pre_auction_last_m1_open(
    open_ts: Union[pd.Timestamp, datetime, str],
) -> bool:
    """
    동시호가 직전 **연속매매 마지막 1분**인지 (기본 ``15:19``).

    이 분은 ka10080 반영이 늦은 경우가 많아, 실시간 경로는 틱말분 즉시 평가 후
    공식봉으로 15분을 대체하는 쪽이 안전하다.
    """
    try:
        t = pd.Timestamp(open_ts).floor("min")
        if t.weekday() >= 5:
            return False
        h = _hours_for_ts(t)
        hm = int(t.hour) * 60 + int(t.minute)
        return hm == int(h.continuous_end_minutes) - 1
    except Exception:
        return False


def is_slot_head_complete_m1_open(
    open_ts: Union[pd.Timestamp, datetime, str],
) -> bool:
    """
    닫힌 1분봉이 슬롯 **head catchup** 을 시작할 시각인지.

    벽시계 :00 직후 head soft catchup (슬롯 완성 **1분 전**).
    - 일반: ``minute % 15 == 13`` (09:13 닫힘 → 09:14:00)
    - 동시호가 직전(15:15 등): ``continuous_end - 2분`` (15:18 닫힘 → **15:19:00**)
    - 종가 단일가: False
    - 애프터: ``minute % 15 == 13`` (16:13, 19:58 등)
    """
    try:
        t = pd.Timestamp(open_ts).floor("min")
        if t.weekday() >= 5:
            return False
        if _is_aftermarket_m1_label(t):
            return (int(t.minute) % 15) == 13
        h = _hours_for_ts(t)
        hm = int(t.hour) * 60 + int(t.minute)
        if hm == h.close_minutes:
            return False
        if hm < h.open_minutes or hm > h.continuous_end_minutes - 1:
            return False
        # 동시호가 직전 잔여봉: 15:18 닫힘(벽 15:19)에 head — 말분(15:19)과 분리
        if hm == h.continuous_end_minutes - 2 and hm >= h.pre_auction_slot_minutes:
            return True
        if hm >= h.pre_auction_slot_minutes:
            return False
        return (int(t.minute) % 15) == 13
    except Exception:
        return False


def slot_head_m1_range(
    slot_label: Union[pd.Timestamp, datetime, str],
) -> Optional[Tuple[pd.Timestamp, pd.Timestamp]]:
    """
    15분 슬롯의 공식 head 구간 ``[start, end_excl)`` (1분 open 라벨 기준).

    - 일반 ``T``: ``[T, T+14분)``
    - 동시호가 직전: ``[pre_auction, continuous_end - 1분)``
    - 종가 단일가: None
    """
    try:
        slot = pd.Timestamp(slot_label).floor("min")
        if is_krx_last_15m_slot(slot):
            return None
        h = _hours_for_ts(slot)
        if is_krx_pre_auction_15m_slot(slot):
            start = slot
            end_excl = _pd_at(slot.normalize(), h.continuous_end) - pd.Timedelta(
                minutes=1
            )
            return start, end_excl
        if not is_krx_15m_slot_end(slot):
            return None
        start = slot
        end_excl = slot + pd.Timedelta(minutes=14)
        return start, end_excl
    except Exception:
        return None


def krx_15m_label_for_m1_open(
    open_ts: Union[pd.Timestamp, datetime, str],
) -> Optional[pd.Timestamp]:
    """
    1분봉 open 라벨이 속한 **좌측 라벨** 15분 슬롯 시각.
    """
    try:
        t = pd.Timestamp(open_ts).floor("min")
        day0 = t.normalize()
        hm = int(t.hour) * 60 + int(t.minute)
        if _is_aftermarket_m1_label(t):
            offset = hm - _ah_open_minutes()
            slot_min = _ah_open_minutes() + (offset // 15) * 15
            if slot_min > _ah_last_15m_minutes():
                return None
            lab = day0 + pd.Timedelta(minutes=int(slot_min))
            return lab if is_krx_15m_slot_end(lab) else None
        h = _hours_for_ts(t)
        if hm == h.continuous_end_minutes - 1:
            return _pd_at(day0, h.pre_auction_slot)
        if hm == h.close_minutes:
            return _pd_at(day0, h.close)
        if hm < h.open_minutes or hm > h.continuous_end_minutes - 1:
            return None
        if hm >= h.pre_auction_slot_minutes:
            return _pd_at(day0, h.pre_auction_slot)
        offset = hm - h.open_minutes
        slot_min = h.open_minutes + (offset // 15) * 15
        lab = day0 + pd.Timedelta(minutes=int(slot_min))
        if not is_krx_15m_slot_end(lab):
            return None
        return lab
    except Exception:
        return None


def m1_bar_end_ts_for_closed_open(
    closed_open: Union[pd.Timestamp, datetime, str],
) -> pd.Timestamp:
    """
    닫힌 1분봉 시작 라벨 → ka10080 1분 차트 인덱스(cntr_tm).

    ``m1_ka10080_cntr_tm_for_closed_open`` 과 동일. 이름은 과거 종가시각(+1)
    오인에서 왔지만, tic=1 ``cntr_tm`` 은 봉 시작 시각이다.
    """
    return m1_ka10080_cntr_tm_for_closed_open(closed_open)


def m1_ka10080_cntr_tm_for_closed_open(
    closed_open: Union[pd.Timestamp, datetime, str],
) -> pd.Timestamp:
    """
    닫힌 1분 open 라벨 → ka10080 1분 차트에서 찾을 cntr_tm.

    harvest ``bars_1m`` 과 같이 **좌측 라벨**(cntr_tm = 봉 시작).
    ``09:14`` 조회는 ``09:14`` 행이다. ``+1분`` 하면 다음 1분이 말분에 들어간다.
    종가 단일가(15:30)도 cntr_tm=15:30.
    """
    return pd.Timestamp(closed_open).floor("min")


def m1_open_label_from_ka10080_cntr_tm(
    cntr_tm: Union[pd.Timestamp, datetime, str],
) -> pd.Timestamp:
    """
    ka10080 1분 차트 인덱스(cntr_tm) → RtTickM1Store open 라벨.

    tic=1 은 좌측 라벨이므로 변환 없이 ``floor('min')``.
    """
    return pd.Timestamp(cntr_tm).floor("min")


def chart_covers_m1_closed_minute(
    chart_df: pd.DataFrame,
    closed_open: Union[pd.Timestamp, datetime, str],
) -> bool:
    """
    ka10080 1분 차트가 목표 닫힌 분까지 데이터를 포함하는지.

    ``max(cntr_tm) < 목표 cntr_tm`` 이면 아직 해당 분봉이 생성되지 않은 것.
    """
    try:
        if chart_df is None or getattr(chart_df, "empty", True):
            return False
        want = m1_ka10080_cntr_tm_for_closed_open(closed_open)
        return pd.Timestamp(chart_df.index.max()).floor("s") >= want.floor("s")
    except Exception:
        return False


def is_krx_m1_closed_open_label(
    closed_open: Union[pd.Timestamp, datetime, str],
) -> bool:
    """
    ``m1_bar_open_label_for_just_completed_minute`` 로 구한 **닫힌 1분봉 시작 라벨**이
    KRX 연속매매 1분 격자(개장~continuous_end-1)·종가 단일가·애프터(16:00~19:59)에 속하는지.
    """
    try:
        t = pd.Timestamp(closed_open).floor("min")
        if t.weekday() >= 5:
            return False
        if _is_aftermarket_m1_label(t):
            return True
        h = _hours_for_ts(t)
        hm = int(t.hour) * 60 + int(t.minute)
        if hm == h.close_minutes:
            return True
        return h.open_minutes <= hm <= (h.continuous_end_minutes - 1)
    except Exception:
        return False


def _agg_ohlcv_from_m1(seg: pd.DataFrame) -> Optional[dict]:
    """1분 OHLCV 구간 → 단일 봉 dict. 실패 시 None."""
    try:
        if seg is None or seg.empty:
            return None
        return {
            "open": float(seg["open"].iloc[0]),
            "high": float(seg["high"].max()),
            "low": float(seg["low"].min()),
            "close": float(seg["close"].iloc[-1]),
            "volume": float(seg["volume"].sum()),
        }
    except (TypeError, ValueError, KeyError):
        return None


def _agg_ohlcv_from_ranges(
    o: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,
    c: np.ndarray,
    v: np.ndarray,
    ranges: List[Tuple[int, int]],
) -> Optional[dict]:
    """numpy OHLCV + half-open 구간 목록 → 단일 봉. 실패 시 None."""
    try:
        if not ranges:
            return None
        first_i = int(ranges[0][0])
        last_e = int(ranges[-1][1])
        if first_i < 0 or last_e <= first_i:
            return None
        hi: Optional[float] = None
        lo: Optional[float] = None
        vol = 0.0
        for a, b in ranges:
            a_i, b_i = int(a), int(b)
            if b_i <= a_i:
                continue
            seg_h = float(np.max(h[a_i:b_i]))
            seg_l = float(np.min(l[a_i:b_i]))
            hi = seg_h if hi is None else max(hi, seg_h)
            lo = seg_l if lo is None else min(lo, seg_l)
            vol += float(np.sum(v[a_i:b_i]))
        if hi is None or lo is None:
            return None
        return {
            "open": float(o[first_i]),
            "high": float(hi),
            "low": float(lo),
            "close": float(c[last_e - 1]),
            "volume": float(vol),
        }
    except (TypeError, ValueError, IndexError):
        return None


def _slot_ranges_on_sorted_ns(
    vals: np.ndarray,
    end_ts_15m: Union[pd.Timestamp, datetime, str],
) -> List[Tuple[int, int]]:
    """정렬된 index.asi8 배열에서 슬롯 half-open 구간(searchsorted)."""
    t_end = pd.Timestamp(end_ts_15m)
    t0 = m1_window_start_for_15m_end(t_end)
    cont_ex = m1_continuous_end_exclusive_for_15m_end(t_end)
    auction = m1_closing_auction_ts_for_15m_end(t_end)
    i0 = int(np.searchsorted(vals, np.int64(pd.Timestamp(t0).value), side="left"))
    i1 = int(np.searchsorted(vals, np.int64(pd.Timestamp(cont_ex).value), side="left"))
    ranges: List[Tuple[int, int]] = []
    if i1 > i0:
        ranges.append((i0, i1))
    if auction is not None:
        a_ns = np.int64(pd.Timestamp(auction).floor("min").value)
        j = int(np.searchsorted(vals, a_ns, side="left"))
        if j < len(vals) and int(vals[j]) == int(a_ns):
            if not (i0 <= j < i1):
                ranges.append((j, j + 1))
    return ranges


def resample_1m_ohlcv_to_15m_krx(
    df_1m: pd.DataFrame, code: str
) -> pd.DataFrame:
    """
    1m OHLCV(열: open, high, low, close, volume, index=datetime)을
    KRX 타임스탬프 규칙으로 15m DataFrame(열 datetime+OHLCV+code)로 집계한다.
    """
    ncode = normalize_code(code)
    _empty = pd.DataFrame(
        columns=["datetime", "open", "high", "low", "close", "volume", "code"]
    )
    if df_1m is None or len(df_1m) == 0:
        return _empty

    try:
        s = df_1m.copy()
        s.index = pd.to_datetime(s.index, errors="coerce")
        s = s[s.index.notna()]
        for col in ("open", "high", "low", "close", "volume"):
            if col not in s.columns:
                return _empty
            s[col] = pd.to_numeric(s[col], errors="coerce")
        s = s.dropna(subset=["open", "high", "low", "close", "volume"])
        if s.empty:
            return _empty
        if not s.index.is_monotonic_increasing:
            s = s.sort_index()
    except (TypeError, ValueError, KeyError) as e:
        print(f"[krx_bars] 1m 전처리 실패: {e!r}", flush=True)
        return _empty

    rows: List[dict] = []
    try:
        o = s["open"].to_numpy(dtype=np.float64, copy=False)
        h = s["high"].to_numpy(dtype=np.float64, copy=False)
        l = s["low"].to_numpy(dtype=np.float64, copy=False)
        c = s["close"].to_numpy(dtype=np.float64, copy=False)
        v = s["volume"].to_numpy(dtype=np.float64, copy=False)
        vals = s.index.asi8
        days = sorted({pd.Timestamp(ts).normalize() for ts in s.index})
        for day0 in days:
            for slot_end in iter_krx_15m_slot_ends_for_day(day0):
                try:
                    ranges = _slot_ranges_on_sorted_ns(vals, slot_end)
                    if not ranges:
                        continue
                    agg = _agg_ohlcv_from_ranges(o, h, l, c, v, ranges)
                    if agg is None:
                        continue
                    agg["datetime"] = slot_end
                    rows.append(agg)
                except (TypeError, ValueError, KeyError) as e:
                    try:
                        print(
                            f"[krx_bars] 슬롯 집계 스킵 {slot_end}: {e!r}",
                            flush=True,
                        )
                    except Exception:
                        pass
    except Exception as e:  # noqa: BLE001
        try:
            print(f"[krx_bars] 일자별 집계 실패: {e!r}", flush=True)
        except Exception:
            pass
        return _empty

    if not rows:
        return _empty

    try:
        out = pd.DataFrame(rows)
        out = out.sort_values("datetime")
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        out["code"] = ncode
        return out[["datetime", "open", "high", "low", "close", "volume", "code"]]
    except (KeyError, AttributeError, ValueError) as e:
        print(f"[krx_bars] 결과 프레임 실패: {e!r}", flush=True)
        return _empty
