# -*- coding: utf-8 -*-
"""
KRX 장중 15분봉·1분 창의 시간 정의.

키움 ka10080(tic_scope=15) 과 동일하게 **좌측(시작) 라벨**을 쓴다.

1분봉 → 15분봉 합성은 봉 개수가 아니라 타임스탬프 구간으로 한다.

- 일반 슬롯(라벨 ``T``): ``T ≤ ts < T+15분``
  (예: 09:00 → 09:00~09:14, 09:15 → 09:15~09:29)
- ``15:15``: ``15:15 ≤ ts < 15:20`` (연속매매 잔여분만)
- ``15:30``: ``ts == 15:30`` (단일가)

라벨 목록: 09:00, 09:15, …, 15:00, 15:15, 15:30.
구간에 실제 존재하는 1분봉만 집계한다.

신호 평가: ``15:30`` 단일가 슬롯만 ``is_krx_no_signal_eval_15m_slot`` 으로 스킵.
``15:15`` 은 평가하되 체결은 동시호가 시장가 → 당일 ``15:30`` 종가
(``is_krx_auction_market_fill_slot``). 봉 자체는 DB·차트에 유지.
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Any, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

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
    "chart_covers_m1_closed_minute",
    "is_last_m1_open_of_15m_slot",
    "krx_15m_label_for_m1_open",
    "is_krx_m1_closed_open_label",
    "is_krx_last_15m_slot",
    "is_krx_pre_auction_15m_slot",
    "is_krx_no_signal_eval_15m_slot",
    "is_krx_auction_market_fill_slot",
    "is_krx_next_open_defer_15m_slot",
    "krx_same_day_1530_label",
    "iter_krx_15m_slot_ends_for_day",
    "resample_1m_ohlcv_to_15m_krx",
    "KRX_15M_RESAMPLE_KWARGS",
    "is_krx_15m_slot_end",
    "krx_stream_slot_end_datetime",
    "next_krx_15m_slot_end",
    "seconds_until_next_krx_15m_boundary",
]

# 레거시 이름. 공식은 좌측 라벨(시작 시각). pandas label=left, closed=left 에 가깝다.
KRX_15M_RESAMPLE_KWARGS: dict = {"label": "left", "closed": "left"}

# KRX/키움 공식 15분 라벨 첫·마지막
_KRX_FIRST_15M_END = time(9, 0)
_KRX_LAST_15M_END = time(15, 30)


def _krx_day_first_slot_end(day0: pd.Timestamp) -> pd.Timestamp:
    """당일 첫 15분 라벨(09:00)."""
    return day0.normalize() + pd.Timedelta(hours=9, minutes=0)


def _krx_day_last_slot_end(day0: pd.Timestamp) -> pd.Timestamp:
    return day0.normalize() + pd.Timedelta(hours=15, minutes=30)


def _next_weekday_first_slot_after(t: pd.Timestamp) -> pd.Timestamp:
    """``t`` 날짜 **다음날**부터 찾은 첫 평일 09:00."""
    try:
        d = pd.Timestamp(t).normalize() + pd.Timedelta(days=1)
        for _ in range(8):
            if d.weekday() < 5:
                return _krx_day_first_slot_end(d)
            d = d + pd.Timedelta(days=1)
    except Exception:
        pass
    return _krx_day_first_slot_end(pd.Timestamp(t).normalize() + pd.Timedelta(days=1))


def is_krx_15m_slot_end(ts: Union[pd.Timestamp, datetime, str]) -> bool:
    """
    KRX/키움 공식 15분봉 **라벨**(09:00~15:30, 15분 간격, 평일) 여부.

    공휴일은 미반영(평일만). 장외·야간 슬롯은 False.
    """
    try:
        t = pd.Timestamp(ts).floor("s")
        if t.weekday() >= 5:
            return False
        if t.second != 0 or t.microsecond != 0:
            return False
        hm = t.hour * 60 + t.minute
        if hm < 9 * 60 or hm > 15 * 60 + 30:
            return False
        return (t.minute % 15) == 0
    except Exception:
        return False


def is_krx_last_15m_slot(ts: Union[pd.Timestamp, datetime, str]) -> bool:
    """라벨이 당일 마지막 15분봉(15:30, 단일가)인지."""
    try:
        t = pd.Timestamp(ts)
        return int(t.hour) == 15 and int(t.minute) == 30
    except Exception:
        return False


def is_krx_pre_auction_15m_slot(ts: Union[pd.Timestamp, datetime, str]) -> bool:
    """라벨이 연속매매 마지막 잔여 구간(15:15)인지."""
    try:
        t = pd.Timestamp(ts)
        return int(t.hour) == 15 and int(t.minute) == 15
    except Exception:
        return False


def is_krx_no_signal_eval_15m_slot(ts: Union[pd.Timestamp, datetime, str]) -> bool:
    """
    완성 15분 신호 평가(S1/S2/청산 콜백)를 하지 않는 슬롯인지.

    ``15:30``(종가 단일가)만 스킵. ``15:15`` 은 평가 후 동시호가 시장가→15:30 종가 체결.
    """
    try:
        return bool(is_krx_last_15m_slot(ts))
    except Exception:
        return False


def is_krx_auction_market_fill_slot(ts: Union[pd.Timestamp, datetime, str]) -> bool:
    """
    완성 15:15 슬롯 주문을 동시호가 시장가로 내어 당일 15:30 종가로 체결할지.
    """
    try:
        return bool(is_krx_pre_auction_15m_slot(ts))
    except Exception:
        return False


def is_krx_next_open_defer_15m_slot(ts: Union[pd.Timestamp, datetime, str]) -> bool:
    """
    15:30 완성봉 신호를 익일 09:00 시가 체결 판정으로 이월할 슬롯인지.
    """
    try:
        return bool(is_krx_last_15m_slot(ts))
    except Exception:
        return False


def krx_same_day_1530_label(
    ts: Union[pd.Timestamp, datetime, str],
) -> Optional[pd.Timestamp]:
    """``ts`` 와 같은 캘린더일의 15:30 좌측 라벨."""
    try:
        t = pd.Timestamp(ts)
        return t.normalize() + pd.Timedelta(hours=15, minutes=30)
    except Exception:
        return None


def iter_krx_15m_slot_ends_for_day(day: Union[pd.Timestamp, datetime, str]) -> List[pd.Timestamp]:
    """해당 캘린더일의 KRX/키움 15분 라벨 목록(09:00 … 15:30)."""
    try:
        day0 = pd.Timestamp(day).normalize()
        out: List[pd.Timestamp] = []
        t = _krx_day_first_slot_end(day0)
        last = _krx_day_last_slot_end(day0)
        while t <= last:
            out.append(t)
            t = t + pd.Timedelta(minutes=15)
        return out
    except Exception:
        return []


def krx_15m_slot_complete_ts(
    label_ts: Union[pd.Timestamp, datetime, str],
) -> pd.Timestamp:
    """
    좌측 라벨 ``T`` 봉이 **완성되는 벽시계 시각**.

    - 일반: ``T+15분``
    - 15:15: ``15:20`` (연속 잔여 종료)
    - 15:30: ``15:30``
    """
    t = pd.Timestamp(label_ts)
    if is_krx_last_15m_slot(t):
        return t.floor("min")
    if is_krx_pre_auction_15m_slot(t):
        return t.normalize() + pd.Timedelta(hours=15, minutes=20)
    return t.floor("min") + pd.Timedelta(minutes=15)


def krx_stream_slot_end_datetime(now: Optional[datetime] = None) -> pd.Timestamp:
    """
    벽시계 기준 **현재 형성 중** 15분봉의 **좌측 라벨**(KRX 장중만).

    - 09:00 이전: 당일 09:00
    - 09:00~15:14: 해당 15분 구간의 시작 라벨
    - 15:15~15:29: 15:15
    - 15:30 이후·주말: 당일(또는 직전 평일) 15:30
    """
    try:
        raw = now if now is not None else datetime.now()
        t = pd.Timestamp(raw)
        day0 = t.normalize()
        if t.weekday() >= 5:
            d = day0
            while d.weekday() >= 5:
                d = d - pd.Timedelta(days=1)
            return _krx_day_last_slot_end(d)

        hm = t.hour * 60 + t.minute
        if hm < 9 * 60:
            return _krx_day_first_slot_end(day0)
        if hm >= 15 * 60 + 30:
            return _krx_day_last_slot_end(day0)
        if hm >= 15 * 60 + 15:
            return day0 + pd.Timedelta(hours=15, minutes=15)

        base_min = 9 * 60
        offset = hm - base_min
        slot_min = base_min + (offset // 15) * 15
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

    장마감(15:30) 다음은 **다음 평일 09:00** 으로 점프합니다.
    """
    try:
        t = pd.Timestamp(ts).floor("s")
        day0 = t.normalize()

        if t.weekday() >= 5:
            return _next_weekday_first_slot_after(t)

        hm = t.hour * 60 + t.minute
        last_end = _krx_day_last_slot_end(day0)
        first_end = _krx_day_first_slot_end(day0)

        if hm >= 15 * 60 + 30:
            return _next_weekday_first_slot_after(t)

        if hm < 9 * 60:
            if t < first_end:
                return first_end
            return next_krx_15m_slot_end(first_end)

        if is_krx_15m_slot_end(t):
            nxt = t + pd.Timedelta(minutes=15)
            if is_krx_15m_slot_end(nxt):
                return nxt
            if t < last_end:
                return last_end
            return _next_weekday_first_slot_after(t)

        up = krx_stream_slot_end_datetime(t.to_pydatetime())
        # 형성 중 라벨이 t 이하면 그 다음 라벨
        if up > t:
            return up
        return next_krx_15m_slot_end(up)
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
    - 15:15: ``15:20``
    - 15:30: ``T`` (연속 구간 없음 — 단일가만)
    """
    t = pd.Timestamp(end_ts).floor("min")
    if is_krx_last_15m_slot(t):
        return t
    if is_krx_pre_auction_15m_slot(t):
        return t.normalize() + pd.Timedelta(hours=15, minutes=20)
    return t + pd.Timedelta(minutes=15)


def m1_closing_auction_ts_for_15m_end(
    end_ts: Union[pd.Timestamp, datetime, str],
) -> Optional[pd.Timestamp]:
    """마지막 15:30 슬롯의 단일가 1분봉 시각. 그 외 ``None``."""
    t = pd.Timestamp(end_ts)
    if not is_krx_last_15m_slot(t):
        return None
    return t.normalize() + pd.Timedelta(hours=15, minutes=30)


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
    닫힌 1분봉 open 라벨이 소속 15분 슬롯의 **마지막 연속매매 분**(또는 15:30 단일가)인지.

    - 일반: ``minute % 15 == 14`` (예: 09:14, 09:29)
    - ``15:15`` 슬롯: ``15:19``
    - ``15:30`` 슬롯: ``15:30``
    """
    try:
        t = pd.Timestamp(open_ts).floor("min")
        if t.weekday() >= 5:
            return False
        hm = t.hour * 60 + t.minute
        if hm == 15 * 60 + 19:
            return True
        if hm == 15 * 60 + 30:
            return True
        if hm < 9 * 60 or hm > 15 * 60 + 14:
            return False
        return (t.minute % 15) == 14
    except Exception:
        return False


def krx_15m_label_for_m1_open(
    open_ts: Union[pd.Timestamp, datetime, str],
) -> Optional[pd.Timestamp]:
    """
    1분봉 open 라벨이 속한 **좌측 라벨** 15분 슬롯 시각.

    - ``15:19`` → ``15:15``
    - ``15:30`` → ``15:30``
    - 그 외: ``floor('15min')`` (예: 09:14 → 09:00)
    """
    try:
        t = pd.Timestamp(open_ts).floor("min")
        hm = t.hour * 60 + t.minute
        if hm == 15 * 60 + 19:
            return t.normalize() + pd.Timedelta(hours=15, minutes=15)
        if hm == 15 * 60 + 30:
            return t.normalize() + pd.Timedelta(hours=15, minutes=30)
        lab = t.floor("15min")
        if not is_krx_15m_slot_end(lab):
            return None
        return lab
    except Exception:
        return None


def m1_bar_end_ts_for_closed_open(
    closed_open: Union[pd.Timestamp, datetime, str],
) -> pd.Timestamp:
    """
    닫힌 1분봉 시작 라벨 → ka10080 인덱스(cntr_tm = **봉 종가 시각**).
    """
    return pd.Timestamp(closed_open).floor("min") + pd.Timedelta(minutes=1)


def chart_covers_m1_closed_minute(
    chart_df: pd.DataFrame,
    closed_open: Union[pd.Timestamp, datetime, str],
) -> bool:
    """
    ka10080 1분 차트가 목표 닫힌 분(종료 시각)까지 데이터를 포함하는지.

    ``max(cntr_tm) < 종료 시각`` 이면 아직 해당 분봉이 생성되지 않은 것(장전·개장 직후 지연).
    """
    try:
        if chart_df is None or getattr(chart_df, "empty", True):
            return False
        end_ts = m1_bar_end_ts_for_closed_open(closed_open)
        return pd.Timestamp(chart_df.index.max()).floor("s") >= end_ts.floor("s")
    except Exception:
        return False


def is_krx_m1_closed_open_label(
    closed_open: Union[pd.Timestamp, datetime, str],
) -> bool:
    """
    ``m1_bar_open_label_for_just_completed_minute`` 로 구한 **닫힌 1분봉 시작 라벨**이
    KRX 연속매매 1분 격자(평일 09:00~15:19) 또는 15:30 단일가에 속하는지.
    """
    try:
        t = pd.Timestamp(closed_open).floor("min")
        if t.weekday() >= 5:
            return False
        hm = t.hour * 60 + t.minute
        if hm == 15 * 60 + 30:
            return True
        return (9 * 60) <= hm <= (15 * 60 + 19)
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
