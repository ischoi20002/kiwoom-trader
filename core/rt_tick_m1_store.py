# -*- coding: utf-8 -*-
"""
실시간 **공식 1분**(ka10080) OHLCV 를 종목별로 소량 유지합니다.

- ``RtWatchlistM1ApiPoller`` 가 매 분 받은 완료 1분을 ``apply_official_closed_minute`` 로 반영합니다.
- ``dataframe_for_window`` 는 신호용 15분 형성 행 집계에 사용합니다(진행 중 분은 기본 제외).
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from threading import RLock
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import pandas as pd

from utils.normalization import canonical_stock_code


def _floor_minute(ts: datetime) -> pd.Timestamp:
    """현재 분의 시작 시각(초·마이크로 0)."""
    try:
        t = pd.Timestamp(ts)
        return t.floor("min")
    except (TypeError, ValueError):
        return pd.Timestamp(datetime.now()).floor("min")


class RtTickM1Store:
    """
    공식 1분(REST) 봉을 종목별 deque 로 유지합니다.
    """

    def __init__(
        self,
        *,
        max_completed_per_code: int = 24,
        logger_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._lock = RLock()
        self._max_c = max(8, min(int(max_completed_per_code), 64))
        self._log = logger_callback
        # code -> {"completed": Deque[Tuple[pd.Timestamp, float, float, float, float, float]], ...}
        self._st: Dict[str, Dict[str, Any]] = {}

    def _emit_log(self, msg: str) -> None:
        if self._log is None:
            return
        try:
            self._log(msg)
        except Exception:
            pass

    def feed_tick(
        self,
        code: str,
        price: float,
        cum_volume: float,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        """
        레거시 호환용 빈 구현입니다.
        실시간 1분은 ka10080(``RtWatchlistM1ApiPoller``)만으로 적재합니다.
        """
        return

    def apply_official_closed_minute(
        self,
        code: str,
        open_label: Any,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> None:
        """
        ka10080 등으로 확인한 **완료 1분** 봉을 반영합니다.

        ``open_label`` 은 해당 분의 **시작 시각**(``m1_bar_open_label_for_just_completed_minute`` 결과와 동일).
        ``completed`` 에 동일 분이 있으면 API OHLCV 로 교체하고, 스트림 상태가 없으면 최소 상태를 만듭니다.
        """
        try:
            nk = canonical_stock_code(str(code).strip())
            if not nk:
                return
            ol = pd.Timestamp(open_label).floor("min")
            o = float(open_)
            h = float(high)
            l_ = float(low)
            c = float(close)
            v = max(0.0, float(volume))
        except (TypeError, ValueError):
            return

        with self._lock:
            try:
                st = self._st.get(nk)
                if st is None:
                    dq: Deque[
                        Tuple[pd.Timestamp, float, float, float, float, float]
                    ] = deque(maxlen=self._max_c)
                    dq.append((ol, o, h, l_, c, v))
                    nxt = ol + pd.Timedelta(minutes=1)
                    self._st[nk] = {
                        "bucket": nxt,
                        "O": c,
                        "H": c,
                        "L": c,
                        "C": c,
                        "V": 0.0,
                        "cum_v_start": 0.0,
                        "completed": dq,
                    }
                    return

                dq = st["completed"]
                kept: List[Tuple[pd.Timestamp, float, float, float, float, float]] = [
                    t
                    for t in dq
                    if pd.Timestamp(t[0]).floor("min") != ol
                ]
                kept.append((ol, o, h, l_, c, v))
                kept.sort(key=lambda x: x[0])
                st["completed"] = deque(
                    kept[-self._max_c :], maxlen=self._max_c
                )

                try:
                    bkt = pd.Timestamp(st["bucket"]).floor("min")
                    if bkt == ol:
                        st["O"] = o
                        st["H"] = h
                        st["L"] = l_
                        st["C"] = c
                        st["V"] = v
                except (TypeError, ValueError, AttributeError):
                    pass
            except (TypeError, ValueError, KeyError, AttributeError) as e:
                self._emit_log(f"⚠️ [RtTickM1] apply_official_closed_minute 예외 ({code}): {e!r}")

    def dataframe_for_window(
        self,
        code: str,
        t0: pd.Timestamp,
        t_hi: pd.Timestamp,
        *,
        include_open_bucket: bool = False,
    ) -> pd.DataFrame:
        """
        ``[t0, t_hi]`` 구간의 1분 OHLCV.

        - ``include_open_bucket=False``(기본): **완료 분**만 — 공식 REST로 닫힌 1분만 신호에 사용.
        - ``True``: 현재 진행 중 분 버킷도 포함(레거시·디버그용).
        """
        out_empty = pd.DataFrame()
        try:
            nk = canonical_stock_code(str(code).strip())
            if not nk:
                return out_empty
            t0s = pd.Timestamp(t0)
            this = pd.Timestamp(t_hi)
            if this < t0s:
                return out_empty
        except (TypeError, ValueError):
            return out_empty

        rows: List[Tuple[pd.Timestamp, float, float, float, float, float]] = []
        with self._lock:
            try:
                st = self._st.get(nk)
                if st is None:
                    return out_empty
                dq: Deque[Tuple[pd.Timestamp, float, float, float, float, float]] = st[
                    "completed"
                ]
                for tup in dq:
                    ts = tup[0]
                    if ts < t0s:
                        continue
                    if ts > this:
                        continue
                    rows.append(tup)

                if include_open_bucket:
                    bucket: pd.Timestamp = st["bucket"]
                    if t0s <= bucket <= this:
                        rows.append(
                            (
                                bucket,
                                float(st["O"]),
                                float(st["H"]),
                                float(st["L"]),
                                float(st["C"]),
                                max(0.0, float(st.get("V", 0.0) or 0.0)),
                            )
                        )
            except (TypeError, ValueError, KeyError, AttributeError):
                return out_empty

        if not rows:
            return out_empty
        try:
            rows.sort(key=lambda x: x[0])
            idx = [r[0] for r in rows]
            df = pd.DataFrame(
                {
                    "Open": [r[1] for r in rows],
                    "High": [r[2] for r in rows],
                    "Low": [r[3] for r in rows],
                    "Close": [r[4] for r in rows],
                    "Volume": [r[5] for r in rows],
                },
                index=pd.DatetimeIndex(idx, name="datetime"),
            )
            df = df[~df.index.duplicated(keep="last")]
            try:
                df = df.sort_index()
            except (TypeError, ValueError):
                pass
            return df
        except (TypeError, ValueError, KeyError):
            return out_empty

    def clear_code(self, code: str) -> None:
        """테스트·특수 경로용: 종목 상태 제거."""
        try:
            nk = canonical_stock_code(str(code).strip())
            with self._lock:
                self._st.pop(nk, None)
        except (TypeError, ValueError):
            pass
