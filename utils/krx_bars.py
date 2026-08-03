# -*- coding: utf-8 -*-
"""
KRX 장중 15분봉·1분 창의 시간 정의.

1분봉 → 15분봉 합성은 **봉 개수가 아니라 타임스탬프 구간**으로 한다.

- 일반 슬롯(종가 라벨 ``T``): ``T-15분 ≤ ts < T``
  (예: 09:15 → 09:00~09:14, 09:30 → 09:15~09:29 — 09:15는 두 번째 봉)
- 마지막 슬롯(15:30): ``15:15 ≤ ts < 15:20`` 또는 ``ts == 15:30``(단일가)

구간에 실제 존재하는 1분봉만 집계한다(결측·서킷 등으로 개수가 줄어도 됨).
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
    "next_m1_ts_after_15m_end",
    "next_m1_close_after_15m_end",
    "m1_bar_open_label_for_just_completed_minute",
    "m1_bar_end_ts_for_closed_open",
    "chart_covers_m1_closed_minute",
    "is_krx_m1_closed_open_label",
    "is_krx_last_15m_slot",
    "iter_krx_15m_slot_ends_for_day",
    "resample_1m_ohlcv_to_15m_krx",
    "KRX_15M_RESAMPLE_KWARGS",
    "is_krx_15m_slot_end",
    "krx_stream_slot_end_datetime",
    "next_krx_15m_slot_end",
    "seconds_until_next_krx_15m_boundary",
]

# 레거시 호환 이름. 신규 합성은 타임스탬프 마스크를 쓰며 pandas resample 에 의존하지 않음.
# closed=left, label=right 는 일반 슬롯 [T-15, T) 와 동일한 의미.
KRX_15M_RESAMPLE_KWARGS: dict = {"label": "right", "closed": "left"}

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


def is_krx_last_15m_slot(ts: Union[pd.Timestamp, datetime, str]) -> bool:
    """종가 라벨이 당일 마지막 15분봉(15:30)인지."""
    try:
        t = pd.Timestamp(ts)
        return int(t.hour) == 15 and int(t.minute) == 30
    except Exception:
        return False


def iter_krx_15m_slot_ends_for_day(day: Union[pd.Timestamp, datetime, str]) -> List[pd.Timestamp]:
    """해당 캘린더일의 KRX 15분 종가 라벨 목록(09:15 … 15:30)."""
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
    15분봉 종가 라벨 ``T`` 에 대응하는 1분봉 창 **시작(포함)**.

    항상 ``T - 15분`` (타임스탬프 기준, 개수 고정 아님).
    """
    return pd.Timestamp(end_ts) - pd.Timedelta(minutes=15)


def m1_continuous_end_exclusive_for_15m_end(
    end_ts: Union[pd.Timestamp, datetime, str],
) -> pd.Timestamp:
    """
    연속매매 1분봉 창의 **끝(미포함)**.

    - 일반: ``T``
    - 15:30: ``15:20`` (15:15~15:19만 연속 구간; 단일가는 별도)
    """
    t = pd.Timestamp(end_ts)
    if is_krx_last_15m_slot(t):
        return t.normalize() + pd.Timedelta(hours=15, minutes=20)
    return t


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
    ``end_ts_15m`` 슬롯에 속하는 1분봉 여부 마스크(타임스탬프 규칙).

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
    """
    try:
        mask = m1_mask_for_15m_end(m1_index, end_ts_15m)
        if mask.size == 0 or not bool(np.any(mask)):
            return []
        idx = np.flatnonzero(mask)
        ranges: List[Tuple[int, int]] = []
        start = int(idx[0])
        prev = start
        for j in idx[1:]:
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


def next_m1_ts_after_15m_end(
    end_ts_15m: Union[pd.Timestamp, datetime, str],
) -> Optional[pd.Timestamp]:
    """
    15분 슬롯 종가 라벨 ``T`` 다음 1분봉의 DB close-label 시각 ``T+1분``.

    정규장 연속 1분이 없으면 ``None`` (예: 15:30 슬롯 이후).
    """
    try:
        t = pd.Timestamp(end_ts_15m)
        nxt = t + pd.Timedelta(minutes=1)
        # 정규장 연속분: 09:01~15:20 close-label (마지막 연속분은 15:20)
        if nxt.hour > 15 or (nxt.hour == 15 and nxt.minute > 20):
            return None
        if nxt.hour < 9 or (nxt.hour == 9 and nxt.minute < 1):
            return None
        return nxt
    except Exception:
        return None


def next_m1_close_after_15m_end(
    m1_df: Any,
    end_ts_15m: Union[pd.Timestamp, datetime, str],
) -> Optional[float]:
    """
    DB ``bars_1m``(close-label)에서 15분 슬롯 ``T`` 다음 1분 ``T+1`` 의 Close.

    정확한 시각 행만 사용하며, 이후 봉으로 대체하지 않습니다.
    """
    try:
        want = next_m1_ts_after_15m_end(end_ts_15m)
        if want is None:
            return None
        if m1_df is None or getattr(m1_df, "empty", True):
            return None
        want_ns = pd.Timestamp(want).floor("min")
        try:
            # 인덱스가 이미 Timestamp 인 경우 직접 조회
            if want_ns in m1_df.index:
                val = m1_df.loc[want_ns, "Close"]
            else:
                # 문자열/다른 dtype 인덱스 폴백
                idx = pd.to_datetime(m1_df.index).floor("min")
                mask = idx == want_ns
                if not bool(np.any(mask)):
                    return None
                val = m1_df.loc[mask, "Close"].iloc[0]
        except Exception:
            return None
        try:
            # Series(중복 인덱스)면 첫 값
            if hasattr(val, "iloc"):
                px = float(val.iloc[0])
            else:
                px = float(val)
        except (TypeError, ValueError):
            return None
        return px if px > 0 else None
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


def resample_1m_ohlcv_to_15m_krx(
    df_1m: pd.DataFrame, code: str
) -> pd.DataFrame:
    """
    1m OHLCV(열: open, high, low, close, volume, index=datetime)을
    KRX 타임스탬프 규칙의 15m DataFrame(열 datetime+OHLCV+code)로 집계한다.
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

    rows: List[dict] = []
    try:
        days = sorted({pd.Timestamp(ts).normalize() for ts in s.index})
        for day0 in days:
            for slot_end in iter_krx_15m_slot_ends_for_day(day0):
                try:
                    mask = m1_mask_for_15m_end(s.index, slot_end)
                    if not bool(np.any(mask)):
                        continue
                    agg = _agg_ohlcv_from_m1(s.loc[mask])
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
