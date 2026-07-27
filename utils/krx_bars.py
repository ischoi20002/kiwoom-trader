# -*- coding: utf-8 -*-
"""
KRX 장중 15분봉·1분 창의 시간 정의.

pandas 기본 ``resample('15min')`` 은 label=left, closed=left 로
「09:45」 행이 [09:45, 10:00) 1분봉을 집계해, (09:30,09:45] 및 인트라바(09:31~09:45)와
저가/체결 정합이 깨진다. DB ``bars_15m`` 는 ``label=right, closed=right`` 집계 뒤,
첫 09:00~09:15 는 1m [09:00, 09:15] 로 병합해 보정한다.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Optional, Tuple, Union

import numpy as np
import pandas as pd

from utils.normalization import normalize_code

__all__ = [
    "m1_window_start_for_15m_end",
    "m1_index_bounds_for_15m_end",
    "m1_bar_open_label_for_just_completed_minute",
    "m1_bar_end_ts_for_closed_open",
    "chart_covers_m1_closed_minute",
    "is_krx_m1_closed_open_label",
    "resample_1m_ohlcv_to_15m_krx",
    "KRX_15M_RESAMPLE_KWARGS",
    "is_krx_15m_slot_end",
    "krx_stream_slot_end_datetime",
    "next_krx_15m_slot_end",
    "seconds_until_next_krx_15m_boundary",
]

# DB·백텐·키움 REST resample(r/r)과 동일
KRX_15M_RESAMPLE_KWARGS: dict = {"label": "right", "closed": "right"}

# KRX 정규장 15분봉(right-label) 첫·마지막 슬롯 종가 시각
_KRX_FIRST_15M_END = time(9, 15)
_KRX_LAST_15M_END = time(15, 30)


def _krx_day_first_slot_end(day0: pd.Timestamp) -> pd.Timestamp:
    return day0.normalize() + pd.Timedelta(hours=9, minutes=15)


def _krx_day_last_slot_end(day0: pd.Timestamp) -> pd.Timestamp:
    return day0.normalize() + pd.Timedelta(hours=15, minutes=30)


def _next_weekday_first_slot_after(t: pd.Timestamp) -> pd.Timestamp:
    """``t`` 날짜 **다음날**부터 찾은 첫 평일 09:15."""
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
    KRX 정규장 15분봉 종가 라벨(09:15~15:30, 15분 간격, 평일) 여부.

    공휴일은 미반영(평일만). 장외·야간 슬롯은 False.
    """
    try:
        t = pd.Timestamp(ts).floor("s")
        if t.weekday() >= 5:
            return False
        if t.second != 0 or t.microsecond != 0:
            return False
        hm = t.hour * 60 + t.minute
        if hm < 9 * 60 + 15 or hm > 15 * 60 + 30:
            return False
        return (t.minute % 15) == 0
    except Exception:
        return False


def krx_stream_slot_end_datetime(now: Optional[datetime] = None) -> pd.Timestamp:
    """
    벽시계 기준 **현재 형성 중** 15분봉의 종가 라벨(KRX 장중만).

    - 09:15 이전: 당일 09:15
    - 09:15~15:30: 해당 구간의 right-label 종가 시각
    - 15:30 이후·주말: 당일(또는 직전 평일) 15:30 — 장외에는 새 슬롯을 만들지 않음
    """
    try:
        raw = now if now is not None else datetime.now()
        t = pd.Timestamp(raw)
        day0 = t.normalize()
        if t.weekday() >= 5:
            # 주말: 직전 금요일 15:30 고정(새 슬롯 전진 없음)
            d = day0
            while d.weekday() >= 5:
                d = d - pd.Timedelta(days=1)
            return _krx_day_last_slot_end(d)

        hm = t.hour * 60 + t.minute
        if hm < 9 * 60 + 15:
            return _krx_day_first_slot_end(day0)
        if hm >= 15 * 60 + 30:
            return _krx_day_last_slot_end(day0)

        # 장중: 09:15 기준 15분 단위로 올림한 종가 라벨
        base_min = 9 * 60 + 15
        offset = hm - base_min
        slot_idx = offset // 15
        end_min = base_min + (slot_idx + 1) * 15
        if end_min > 15 * 60 + 30:
            end_min = 15 * 60 + 30
        return day0 + pd.Timedelta(minutes=end_min)
    except Exception:
        try:
            return pd.Timestamp(now or datetime.now()).floor("15min")
        except Exception:
            return pd.Timestamp.now().floor("15min")


def next_krx_15m_slot_end(
    ts: Union[pd.Timestamp, datetime, str],
) -> Optional[pd.Timestamp]:
    """
    ``ts`` **이후** 첫 KRX 15분봉 종가 시각.

    장마감(15:30) 다음은 **다음 평일 09:15** 로 점프합니다(야간 00:00·03:00 등 생성 안 함).
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

        if hm < 9 * 60 + 15:
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

        # 슬롯 경계 사이 → 올림한 종가 라벨
        up = krx_stream_slot_end_datetime(t.to_pydatetime())
        if up > t:
            return up
        return next_krx_15m_slot_end(up)
    except Exception:
        return None


def seconds_until_next_krx_15m_boundary(
    now: Optional[Union[datetime, pd.Timestamp]] = None,
) -> float:
    """
    다음 KRX 15분 슬롯 **벽시계 경계**까지 남은 초.

    ``krx_stream_slot_end_datetime`` 이 분 단위로 점프하는 시각(09:15, 09:30, …)
    까지의 대기시간. 슬롯 롤 워처의 exact wake 에 사용합니다.
    """
    try:
        raw = now if now is not None else datetime.now()
        t = pd.Timestamp(raw)
        want = krx_stream_slot_end_datetime(
            t.to_pydatetime() if hasattr(t, "to_pydatetime") else t
        )
        want_ts = pd.Timestamp(want)
        delta = float((want_ts - t).total_seconds())
        if delta > 0.001:
            return delta
        nxt = next_krx_15m_slot_end(want_ts)
        if nxt is None:
            return 60.0
        return max(0.0, float((pd.Timestamp(nxt) - t).total_seconds()))
    except Exception:
        return 1.0


def m1_window_start_for_15m_end(
    end_ts: Union[pd.Timestamp, datetime, str],
) -> pd.Timestamp:
    """
    15분봉 행 시각(구간 **종가 시각**에 해당)일 때, 동일 15m 을 구성하는 1분봉 **시작(포함)**.

    - [09:00, 09:15] (첫 봉) → 시작 09:00 (T-15분, 최대 16개 1m)
    - 이후 15m → 시작 end-14분 (T-14, 15개 1m; 예: 09:45봉 → 09:31~09:45)
    """
    t = pd.Timestamp(end_ts)
    if t.hour == 9 and t.minute == 15:
        return t - pd.Timedelta(minutes=15)
    return t - pd.Timedelta(minutes=14)


def m1_index_bounds_for_15m_end(
    m1_index: pd.DatetimeIndex,
    end_ts_15m: Union[pd.Timestamp, datetime, str],
) -> Tuple[int, int]:
    """
    ``backtest_engine.SimpleBacktester.run`` 과 동일한 규칙으로,
    15분봉 **라벨(종가 시각)** ``end_ts_15m`` 에 대응하는 ``bars_1m`` 행 구간 ``[s, e)`` 인덱스.

    - 시작: ``m1_window_start_for_15m_end(end_ts_15m)`` (첫 09:15 봉만 T-15, 그 외 T-14)
    - 끝: ``numpy.searchsorted(..., end_ts_15m, side='right')`` (15m 라벨 시각 **미만** 1분봉까지)
    """
    try:
        t_end = pd.Timestamp(end_ts_15m)
        t_start = m1_window_start_for_15m_end(t_end)
        m1a = np.asarray(m1_index.values, dtype="datetime64[ns]")
        s = int(np.searchsorted(m1a, np.datetime64(t_start), side="left"))
        e = int(np.searchsorted(m1a, np.datetime64(t_end), side="right"))
        return s, e
    except Exception:
        return 0, 0


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
    KRX 연속매매 1분 격자(평일 09:00~15:19)에 속하는지.

    (레거시·보조) 실시간 M1 폴러는 ``chart_covers_m1_closed_minute`` 를 우선 사용한다.
    """
    try:
        t = pd.Timestamp(closed_open).floor("min")
        if t.weekday() >= 5:
            return False
        hm = t.hour * 60 + t.minute
        return (9 * 60) <= hm <= (15 * 60 + 19)
    except Exception:
        return False


def resample_1m_ohlcv_to_15m_krx(
    df_1m: pd.DataFrame, code: str
) -> pd.DataFrame:
    """
    1m OHLCV(열: open, high, low, close, volume, index=datetime)을
    KRX 정의에 맞는 15m DataFrame(열 datetime+OHLCV+code)로 집계한다.
    """
    ncode = normalize_code(code)
    _empty = pd.DataFrame(
        columns=["datetime", "open", "high", "low", "close", "volume", "code"]
    )
    if df_1m is None or len(df_1m) == 0:
        return _empty

    try:
        s = df_1m.copy()
        s = s.sort_index()
        s.index = pd.to_datetime(s.index, errors="coerce")
        s = s[s.index.notna()]
        for c in ("open", "high", "low", "close", "volume"):
            if c not in s.columns:
                return _empty
            s[c] = pd.to_numeric(s[c], errors="coerce")
        s = s.dropna(subset=["open", "high", "low", "close", "volume"])
        if s.empty:
            return _empty
    except (TypeError, ValueError, KeyError) as e:
        print(f"[krx_bars] 1m 전처리 실패: {e!r}", flush=True)
        return _empty

    try:
        ohlc = s.resample("15min", **KRX_15M_RESAMPLE_KWARGS).agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        ohlc = ohlc.dropna()
    except (TypeError, ValueError) as e:
        print(f"[krx_bars] resample 실패: {e!r}", flush=True)
        return _empty
    except (KeyboardInterrupt, SystemExit) as e:
        raise
    except Exception as e:  # noqa: BLE001
        try:
            print(f"[krx_bars] resample 예기치 않은 실패: {e!r}", flush=True)
        except Exception:  # noqa: BLE001
            pass
        return _empty

    # 날짜마다: (8:45,9:0] 를 달리 쓰는 09:00 15m 행 제거, 09:15 는 [9:0,9:15] 1m 로 재집계
    for day, _ in s.groupby(s.index.date):
        d0 = pd.Timestamp(day)
        t00 = d0 + pd.Timedelta(hours=9)
        t915 = d0 + pd.Timedelta(hours=9, minutes=15)
        mseg = s.loc[(s.index >= t00) & (s.index <= t915)]
        if t00 in ohlc.index:
            ohlc = ohlc.drop(t00)
        if mseg.empty or t915 not in ohlc.index:
            continue
        try:
            ohlc.loc[t915, "open"] = float(mseg["open"].iloc[0])
            ohlc.loc[t915, "high"] = float(mseg["high"].max())
            ohlc.loc[t915, "low"] = float(mseg["low"].min())
            ohlc.loc[t915, "close"] = float(mseg["close"].iloc[-1])
            ohlc.loc[t915, "volume"] = float(mseg["volume"].sum())
        except (TypeError, ValueError, KeyError) as e:
            try:
                print(f"[krx_bars] 09:15 병합 경고(해당일 스킵): {e!r}", flush=True)
            except (OSError, TypeError) as e2:  # noqa: BLE001
                pass

    try:
        out = ohlc.reset_index()
        if out.columns[0] != "datetime":
            out = out.rename(columns={out.columns[0]: "datetime"})
    except (KeyError, AttributeError, ValueError) as e:
        print(f"[krx_bars] reset_index 실패: {e!r}", flush=True)
        return _empty

    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    out["code"] = ncode
    return out
