# -*- coding: utf-8 -*-
"""
실시간 체결(누적거래량·가격)으로 1분 OHLCV 를 구성합니다.

- BT-HP 브리지용: HTTP(ka10080) 지연 없이 동일 틱 경로에서만 갱신.
- ``RtWatchlistM1ApiPoller`` 가 매 분 ka10080 로 받은 **완료 1분** 은
  ``apply_official_closed_minute`` 로 동일 분 틱 집계를 **덮어써** 계산에 반영합니다.
- 종목당 완료 1분봉은 소량만 유지(deque)하여 메모리·스냅샷 비용을 제한합니다.
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
    틱 1회당 O(1) 갱신으로 1분 봉을 유지합니다.

    ``StreamProcessor.update_realtime_bar`` 와 동일한 틱·``datetime.now`` 기준을 씁니다
    (15분 슬롯 라벨과 별개로, 벽시계 분 단위 버킷).
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
        체결 1틱 반영. ``cum_volume`` 은 키움 0B 누적거래량(``StreamProcessor`` 와 동일 가정).
        """
        try:
            nk = canonical_stock_code(str(code).strip())
            if not nk:
                return
            px = float(price)
            cv = float(cum_volume)
            dt = now if now is not None else datetime.now()
            floor = _floor_minute(dt)
        except (TypeError, ValueError):
            return

        with self._lock:
            try:
                st = self._st.get(nk)
                if st is None:
                    dq: Deque[Tuple[pd.Timestamp, float, float, float, float, float]] = deque(
                        maxlen=self._max_c
                    )
                    self._st[nk] = {
                        "bucket": floor,
                        "O": px,
                        "H": px,
                        "L": px,
                        "C": px,
                        "V": 0.0,
                        "cum_v_start": cv,
                        "completed": dq,
                    }
                    return

                bucket: pd.Timestamp = st["bucket"]
                # 장중 재시작·시계 튐: 버킷이 한 시간 이상 어긋나면 상태 초기화
                try:
                    if abs((floor - bucket).total_seconds()) > 3600:
                        st["completed"].clear()
                        st["bucket"] = floor
                        st["O"] = st["H"] = st["L"] = st["C"] = px
                        st["V"] = 0.0
                        st["cum_v_start"] = cv
                        return
                except (TypeError, ValueError, AttributeError):
                    pass

                if floor > bucket:
                    # 직전 분 확정
                    try:
                        st["completed"].append(
                            (
                                bucket,
                                float(st["O"]),
                                float(st["H"]),
                                float(st["L"]),
                                float(st["C"]),
                                max(0.0, float(st["V"])),
                            )
                        )
                    except (TypeError, ValueError):
                        pass
                    st["bucket"] = floor
                    st["O"] = st["H"] = st["L"] = st["C"] = px
                    st["V"] = 0.0
                    st["cum_v_start"] = cv
                    return

                if floor < bucket:
                    # 비정상(시계 역행): 현재 분으로 덮어쓰기
                    st["bucket"] = floor
                    st["O"] = st["H"] = st["L"] = st["C"] = px
                    st["V"] = 0.0
                    st["cum_v_start"] = cv
                    return

                # 동일 분 갱신
                st["C"] = px
                if px > float(st["H"]):
                    st["H"] = px
                if px < float(st["L"]):
                    st["L"] = px

                start_v = float(st.get("cum_v_start", cv) or 0.0)
                if start_v <= 0.0 and cv > 0.0:
                    cur_v = float(st.get("V", 0.0) or 0.0)
                    st["cum_v_start"] = max(0.0, cv - cur_v)
                else:
                    st["V"] = max(0.0, cv - start_v)
            except (TypeError, ValueError, KeyError, AttributeError) as e:
                self._emit_log(f"⚠️ [RtTickM1] feed_tick 예외 ({code}): {e!r}")

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
    ) -> pd.DataFrame:
        """
        ``[t0, t_hi]`` 구간의 1분 OHLCV (진행 중 분 포함).
        열: Open, High, Low, Close, Volume — 인덱스 분 시작.
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
