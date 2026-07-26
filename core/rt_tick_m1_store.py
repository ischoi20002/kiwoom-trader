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
        # 공식 1분 반영 시마다 증가 — 실시간 S1 풀계산을 시간 스로틀 대신 M1 갱신에 맞출 때 사용
        self._revision: Dict[str, int] = {}
        # 틱으로만 쌓는 진행 중 1분(공식 completed 와 분리) — 15분 슬롯 마지막 1분 보정용
        self._tick_open: Dict[str, Dict[str, Any]] = {}
        # 방금 막힌 틱 1분(슬롯 꼬리분) 스냅샷 — 벽시계 롤 직후 조회용
        self._tick_sealed: Dict[str, Tuple[pd.Timestamp, float, float, float, float, float]] = {}

    def _emit_log(self, msg: str) -> None:
        if self._log is None:
            return
        try:
            self._log(msg)
        except Exception:
            pass

    @staticmethod
    def _is_slot_tail_minute(open_label: pd.Timestamp) -> bool:
        """15분 슬롯의 마지막 1분(open 시각의 minute%15==14)인지."""
        try:
            return int(pd.Timestamp(open_label).minute) % 15 == 14
        except Exception:
            return False

    def feed_tick(
        self,
        code: str,
        price: float,
        cum_volume: float,
        *,
        now: Optional[datetime] = None,
    ) -> None:
        """
        틱으로 **진행 중 1분** OHLCV를 갱신합니다(공식 ``completed`` 와 분리).

        15분 슬롯의 마지막 1분(및 그 직후 seal)에 쓰며, 공식 1분 경로를 덮어쓰지 않습니다.
        ``cum_volume`` 은 당일 누적 거래량(키움 0B 등)을 가정합니다.
        """
        try:
            nk = canonical_stock_code(str(code).strip())
            if not nk:
                return
            px = float(price)
            if px <= 0:
                return
            cum_v = max(0.0, float(cum_volume))
            wall = now if now is not None else datetime.now()
            cur = _floor_minute(wall)
        except (TypeError, ValueError):
            return

        with self._lock:
            try:
                st = self._tick_open.get(nk)
                if st is None:
                    # 슬롯 꼬리분이 아니면 시작하지 않음(불필요 부하 방지)
                    if not self._is_slot_tail_minute(cur):
                        return
                    self._tick_open[nk] = {
                        "bucket": cur,
                        "O": px,
                        "H": px,
                        "L": px,
                        "C": px,
                        "V": 0.0,
                        "cum_v_start": cum_v,
                    }
                    return

                bkt = pd.Timestamp(st.get("bucket")).floor("min")
                if bkt != cur:
                    # 분이 바뀌면 직전 버킷이 슬롯 꼬리분이면 seal
                    try:
                        if self._is_slot_tail_minute(bkt) and float(st.get("C", 0) or 0) > 0:
                            self._tick_sealed[nk] = (
                                bkt,
                                float(st["O"]),
                                float(st["H"]),
                                float(st["L"]),
                                float(st["C"]),
                                max(0.0, float(st.get("V", 0) or 0)),
                            )
                    except Exception:
                        pass
                    if not self._is_slot_tail_minute(cur):
                        self._tick_open.pop(nk, None)
                        return
                    self._tick_open[nk] = {
                        "bucket": cur,
                        "O": px,
                        "H": px,
                        "L": px,
                        "C": px,
                        "V": 0.0,
                        "cum_v_start": cum_v,
                    }
                    return

                # 동일 분: OHLC + 누계 차분 거래량
                st["C"] = px
                try:
                    st["H"] = max(float(st.get("H", px) or px), px)
                    st["L"] = min(float(st.get("L", px) or px), px)
                    if float(st.get("L", 0) or 0) <= 0:
                        st["L"] = px
                except Exception:
                    st["H"] = px
                    st["L"] = px
                try:
                    start_v = float(st.get("cum_v_start", 0) or 0)
                    if start_v <= 0 and cum_v > 0:
                        cur_v = float(st.get("V", 0) or 0)
                        st["cum_v_start"] = max(0.0, cum_v - cur_v)
                    else:
                        st["V"] = max(0.0, cum_v - start_v)
                except Exception:
                    pass
            except Exception as e:
                self._emit_log(f"⚠️ [RtTickM1] feed_tick 예외 ({code}): {e!r}")

    def tick_minute_ohlcv(
        self, code: str, open_label: Any
    ) -> Optional[Tuple[float, float, float, float, float]]:
        """
        틱으로 만든 해당 분(open_label) OHLCV.
        진행 중 버킷 또는 seal 스냅샷에서 조회합니다.
        """
        try:
            nk = canonical_stock_code(str(code).strip())
            if not nk:
                return None
            want = pd.Timestamp(open_label).floor("min")
        except (TypeError, ValueError):
            return None
        with self._lock:
            try:
                st = self._tick_open.get(nk)
                if st is not None:
                    bkt = pd.Timestamp(st.get("bucket")).floor("min")
                    if bkt == want and float(st.get("C", 0) or 0) > 0:
                        return (
                            float(st["O"]),
                            float(st["H"]),
                            float(st["L"]),
                            float(st["C"]),
                            max(0.0, float(st.get("V", 0) or 0)),
                        )
                sealed = self._tick_sealed.get(nk)
                if sealed is not None:
                    sb = pd.Timestamp(sealed[0]).floor("min")
                    if sb == want and float(sealed[4]) > 0:
                        return (
                            float(sealed[1]),
                            float(sealed[2]),
                            float(sealed[3]),
                            float(sealed[4]),
                            max(0.0, float(sealed[5])),
                        )
            except Exception:
                return None
        return None

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
                    self._revision[nk] = int(self._revision.get(nk, 0)) + 1
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
                self._revision[nk] = int(self._revision.get(nk, 0)) + 1
            except (TypeError, ValueError, KeyError, AttributeError) as e:
                self._emit_log(f"⚠️ [RtTickM1] apply_official_closed_minute 예외 ({code}): {e!r}")

    def revision_for_code(self, code: str) -> int:
        """``apply_official_closed_minute`` 성공 반영 횟수(종목별). 실패·미적재 시 0."""
        try:
            nk = canonical_stock_code(str(code).strip())
            if not nk:
                return 0
            with self._lock:
                return int(self._revision.get(nk, 0))
        except (TypeError, ValueError):
            return 0

    def last_completed_close(self, code: str) -> float:
        """완료 공식 1분 중 가장 최근 종가. 없으면 0."""
        try:
            nk = canonical_stock_code(str(code).strip())
            if not nk:
                return 0.0
            with self._lock:
                st = self._st.get(nk)
                if not st:
                    return 0.0
                dq = st.get("completed")
                if not dq:
                    return 0.0
                _ol, _o, _h, _l, c, _v = dq[-1]
                return float(c) if float(c) > 0 else 0.0
        except (TypeError, ValueError, KeyError, IndexError):
            return 0.0

    def forming_minute_ohlc(
        self, code: str
    ) -> Tuple[float, float, float, float]:
        """진행 중 1분 (O, H, L, C). 없으면 0."""
        try:
            nk = canonical_stock_code(str(code).strip())
            if not nk:
                return 0.0, 0.0, 0.0, 0.0
            with self._lock:
                st = self._st.get(nk)
                if not st:
                    return 0.0, 0.0, 0.0, 0.0
                o = float(st.get("O", 0) or 0)
                h = float(st.get("H", 0) or 0)
                l_ = float(st.get("L", 0) or 0)
                c = float(st.get("C", 0) or 0)
                if c <= 0:
                    return 0.0, 0.0, 0.0, 0.0
                if l_ <= 0:
                    l_ = c
                return o, h, l_, c
        except (TypeError, ValueError, KeyError):
            return 0.0, 0.0, 0.0, 0.0

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
