# -*- coding: utf-8 -*-
"""
실시간 **공식 1분**(ka10080) OHLCV 와 **틱 1분**을 종목별로 소량 유지합니다.

- ``RtWatchlistM1ApiPoller`` 가 매 분 받은 완료 1분을 ``apply_official_closed_minute`` 로 반영합니다.
- ``dataframe_for_window`` 는 공식 REST로 닫힌 1분만 반환합니다(신호 합성의 1순위).
- ``feed_tick`` 은 **모든 1분**을 틱으로 누적·seal 하며, API miss 시 합성 폴백에 사용합니다.
- 말분(``:14`` / ``15:19`` / ``15:30``) seal 시에만 ``:00`` 즉시 평가 트리거용 라벨을 반환합니다.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime
from threading import RLock
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import pandas as pd

from utils.normalization import canonical_stock_code

# 종목당 틱 seal 분 상한(15분 슬롯 수개 + 여유)
_MAX_TICK_SEALED_PER_CODE = 64

# sealed tuple: (open_lab, O, H, L, C, V, last_trade_dt, cum_v_start, cum_v_max)
_SealedTup = Tuple[Any, ...]


def _floor_minute(ts: datetime) -> pd.Timestamp:
    """현재 분의 시작 시각(초·마이크로 0)."""
    try:
        t = pd.Timestamp(ts)
        return t.floor("min")
    except (TypeError, ValueError):
        return pd.Timestamp(datetime.now()).floor("min")


class RtTickM1Store:
    """
    공식 1분(REST) + 틱 1분 봉을 종목별로 유지합니다.
    """

    def __init__(
        self,
        *,
        max_completed_per_code: int = 24,
        logger_callback: Optional[Callable[[str], None]] = None,
        max_tick_sealed_per_code: int = _MAX_TICK_SEALED_PER_CODE,
    ) -> None:
        self._lock = RLock()
        self._max_c = max(8, min(int(max_completed_per_code), 64))
        self._max_tick_sealed = max(16, min(int(max_tick_sealed_per_code), 128))
        self._log = logger_callback
        # code -> {"completed": Deque[Tuple[pd.Timestamp, float, float, float, float, float]], ...}
        self._st: Dict[str, Dict[str, Any]] = {}
        # 공식 1분 반영 시마다 증가 — 실시간 S1 풀계산을 시간 스로틀 대신 M1 갱신에 맞출 때 사용
        self._revision: Dict[str, int] = {}
        # 틱으로 쌓는 진행 중 1분(공식 completed 와 분리)
        self._tick_open: Dict[str, Dict[str, Any]] = {}
        # 분별 seal 맵 — code -> {open_lab_floor: sealed_tuple}
        self._tick_sealed_by_min: Dict[str, Dict[pd.Timestamp, _SealedTup]] = {}
        # 호환: 종목별 가장 최근 seal (말분 amend·구 경로)
        self._tick_sealed: Dict[str, _SealedTup] = {}
        # 종목별 이번 감시 세션 첫 틱 시각 — 말분 틱 완전성 판별용
        self._first_tick_at: Dict[str, datetime] = {}

    def _emit_log(self, msg: str) -> None:
        if self._log is None:
            return
        try:
            self._log(msg)
        except Exception:
            pass

    @staticmethod
    def _is_slot_tail_minute(open_label: pd.Timestamp) -> bool:
        """
        15분 슬롯의 마지막 1분(일반 :14, 15:19) 또는 **15:30 단일가**인지.

        말분 seal 시에만 ``:00`` 즉시 평가 트리거를 반환합니다.
        """
        try:
            from utils.krx_bars import is_last_m1_open_of_15m_slot

            return bool(is_last_m1_open_of_15m_slot(open_label))
        except Exception:
            try:
                t = pd.Timestamp(open_label)
                if int(t.hour) == 15 and int(t.minute) == 30:
                    return True
                if int(t.hour) == 15 and int(t.minute) == 19:
                    return True
                return int(t.minute) % 15 == 14
            except Exception:
                return False

    @staticmethod
    def _new_tick_forming(
        bucket: pd.Timestamp,
        px: float,
        cum_v: float,
        trade_dt: datetime,
    ) -> Dict[str, Any]:
        return {
            "bucket": bucket,
            "O": px,
            "H": px,
            "L": px,
            "C": px,
            "V": 0.0,
            "cum_v_start": cum_v,
            "cum_v_max": cum_v,
            "last_trade_dt": trade_dt,
        }

    @staticmethod
    def _update_tick_ohlcv(
        st: Dict[str, Any],
        px: float,
        cum_v: float,
        trade_dt: datetime,
    ) -> None:
        """동일 분 틱 반영. 종가는 체결시각이 더 늦은 쪽 우선."""
        try:
            prev_td = st.get("last_trade_dt")
            newer = True
            if isinstance(prev_td, datetime):
                newer = trade_dt >= prev_td
            if newer:
                st["C"] = px
                st["last_trade_dt"] = trade_dt
        except Exception:
            st["C"] = px
            st["last_trade_dt"] = trade_dt
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
                start_v = float(st.get("cum_v_start", 0) or 0)
            prev_max = float(st.get("cum_v_max", 0) or 0)
            if cum_v > prev_max:
                st["cum_v_max"] = cum_v
            if start_v > 0:
                st["V"] = max(
                    float(st.get("V", 0) or 0),
                    max(0.0, float(st.get("cum_v_max", cum_v) or cum_v) - start_v),
                )
        except Exception:
            pass

    def _prune_tick_sealed_unlocked(self, nk: str) -> None:
        """종목당 seal 분 상한 유지(오래된 open_lab 부터 제거)."""
        try:
            m = self._tick_sealed_by_min.get(nk)
            if not m or len(m) <= self._max_tick_sealed:
                return
            keys = sorted(m.keys())
            drop_n = len(keys) - self._max_tick_sealed
            for k in keys[:drop_n]:
                m.pop(k, None)
        except Exception:
            pass

    def _store_sealed_unlocked(self, nk: str, sealed: _SealedTup) -> None:
        """분별 맵 + 최근 seal 캐시에 저장."""
        try:
            bkt = pd.Timestamp(sealed[0]).floor("min")
            by = self._tick_sealed_by_min.get(nk)
            if by is None:
                by = {}
                self._tick_sealed_by_min[nk] = by
            by[bkt] = sealed
            self._tick_sealed[nk] = sealed
            self._prune_tick_sealed_unlocked(nk)
        except Exception:
            pass

    def _get_sealed_for_minute_unlocked(
        self, nk: str, open_lab: pd.Timestamp
    ) -> Optional[_SealedTup]:
        want = pd.Timestamp(open_lab).floor("min")
        by = self._tick_sealed_by_min.get(nk)
        if by is not None:
            hit = by.get(want)
            if hit is not None and float(hit[4]) > 0:
                return hit
        # 호환: 단일 최근 seal
        sealed = self._tick_sealed.get(nk)
        if sealed is not None:
            try:
                if pd.Timestamp(sealed[0]).floor("min") == want and float(sealed[4]) > 0:
                    return sealed
            except Exception:
                pass
        return None

    def _seal_tick_state_unlocked(self, nk: str, st: Dict[str, Any]) -> bool:
        """진행 중 틱 봉을 sealed 스냅샷으로 확정. 성공 시 True."""
        try:
            bkt = pd.Timestamp(st.get("bucket")).floor("min")
            c = float(st.get("C", 0) or 0)
            if c <= 0:
                return False
            o = float(st.get("O", 0) or 0)
            h = float(st.get("H", 0) or 0)
            l_ = float(st.get("L", 0) or 0)
            if l_ <= 0:
                l_ = c
            v = max(0.0, float(st.get("V", 0) or 0))
            sealed: _SealedTup = (
                bkt,
                o,
                h,
                l_,
                c,
                v,
                st.get("last_trade_dt"),
                float(st.get("cum_v_start", 0) or 0),
                float(st.get("cum_v_max", 0) or 0),
            )
            self._store_sealed_unlocked(nk, sealed)
            return True
        except Exception:
            return False

    def _amend_sealed_unlocked(
        self,
        nk: str,
        open_lab: pd.Timestamp,
        px: float,
        cum_v: float,
        trade_dt: datetime,
    ) -> bool:
        """이미 seal된 분에 늦은 체결을 반영."""
        try:
            sealed = self._get_sealed_for_minute_unlocked(nk, open_lab)
            if sealed is None:
                return False
            sb = pd.Timestamp(sealed[0]).floor("min")
            st = {
                "bucket": sb,
                "O": float(sealed[1]),
                "H": float(sealed[2]),
                "L": float(sealed[3]),
                "C": float(sealed[4]),
                "V": float(sealed[5]),
                "last_trade_dt": sealed[6] if len(sealed) > 6 else None,
                "cum_v_start": float(sealed[7]) if len(sealed) > 7 else 0.0,
                "cum_v_max": float(sealed[8]) if len(sealed) > 8 else float(sealed[5]),
            }
            self._update_tick_ohlcv(st, px, cum_v, trade_dt)
            return self._seal_tick_state_unlocked(nk, st)
        except Exception:
            return False

    def feed_tick(
        self,
        code: str,
        price: float,
        cum_volume: float,
        *,
        now: Optional[datetime] = None,
    ) -> Optional[pd.Timestamp]:
        """
        틱으로 **모든 1분** OHLCV를 갱신합니다(공식 ``completed`` 와 분리).

        ``now`` 에는 **체결시각**(0B fid 20)을 넘기는 것을 권장.
        분 라벨은 체결시각 ``floor(min)`` 이며, seal 이후 늦은 체결은 sealed를 amend합니다.

        Returns:
            다음 분 체결로 **말분이 새로 seal** 된 경우 그 open 라벨.
            (``:00`` 틱 즉시 평가 트리거용). 그 외는 None.
        """
        sealed_on_next_min: Optional[pd.Timestamp] = None
        try:
            nk = canonical_stock_code(str(code).strip())
            if not nk:
                return None
            px = float(price)
            if px <= 0:
                return None
            cum_v = max(0.0, float(cum_volume))
            trade_dt = now if isinstance(now, datetime) else datetime.now()
            cur = _floor_minute(trade_dt)
        except (TypeError, ValueError):
            return None

        with self._lock:
            try:
                if nk not in self._first_tick_at:
                    self._first_tick_at[nk] = trade_dt
                # 이미 seal된 분에 대한 늦은 체결
                if self._amend_sealed_unlocked(nk, cur, px, cum_v, trade_dt):
                    return None

                st = self._tick_open.get(nk)
                if st is None:
                    self._tick_open[nk] = self._new_tick_forming(
                        cur, px, cum_v, trade_dt
                    )
                    return None

                bkt = pd.Timestamp(st.get("bucket")).floor("min")
                if bkt == cur:
                    self._update_tick_ohlcv(st, px, cum_v, trade_dt)
                    return None

                if cur < bkt:
                    # forming이 다음 분인데 직전 분 체결이 늦게 온 경우
                    if float(st.get("C", 0) or 0) > 0:
                        self._seal_tick_state_unlocked(nk, st)
                        self._tick_open.pop(nk, None)
                    self._amend_sealed_unlocked(nk, cur, px, cum_v, trade_dt)
                    return None

                # cur > bkt: 다음 분 체결 → 직전 분 seal
                try:
                    if float(st.get("C", 0) or 0) > 0:
                        if self._seal_tick_state_unlocked(nk, st):
                            # 즉시 평가 트리거는 말분만
                            if self._is_slot_tail_minute(bkt):
                                sealed_on_next_min = bkt
                except Exception:
                    pass
                self._tick_open[nk] = self._new_tick_forming(cur, px, cum_v, trade_dt)
                return sealed_on_next_min
            except Exception as e:
                self._emit_log(f"⚠️ [RtTickM1] feed_tick 예외 ({code}): {e!r}")
                return None
        return sealed_on_next_min

    def seal_tick_tail_minute(self, code: str, open_label: Any) -> bool:
        """
        지정 분 open_label 의 진행 중 틱 봉을 강제 seal.

        말분 타임아웃·15:30 경로에서도 사용. 이미 sealed 이고 동일 분이면 True.
        """
        try:
            nk = canonical_stock_code(str(code).strip())
            if not nk:
                return False
            want = pd.Timestamp(open_label).floor("min")
        except (TypeError, ValueError):
            return False

        with self._lock:
            try:
                existing = self._get_sealed_for_minute_unlocked(nk, want)
                if existing is not None:
                    return True
                st = self._tick_open.get(nk)
                if st is None:
                    return False
                bkt = pd.Timestamp(st.get("bucket")).floor("min")
                if bkt != want:
                    return False
                ok = self._seal_tick_state_unlocked(nk, st)
                if ok:
                    self._tick_open.pop(nk, None)
                return ok
            except Exception as e:
                self._emit_log(f"⚠️ [RtTickM1] seal_tick_tail 예외 ({code}): {e!r}")
                return False

    def tick_minute_ohlcv(
        self, code: str, open_label: Any
    ) -> Optional[Tuple[float, float, float, float, float]]:
        """
        틱으로 만든 해당 분(open_label) OHLCV.
        진행 중 버킷 또는 seal 맵에서 조회합니다.
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
                sealed = self._get_sealed_for_minute_unlocked(nk, want)
                if sealed is not None:
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
        ka10080 등으로 확인한 **완료 1분** 봉을 반영합니다(틱 선적재 위 공식 soft replace).

        ``open_label`` 은 해당 분의 **시작 시각**(``m1_bar_open_label_for_just_completed_minute`` 결과와 동일).
        ``completed`` 에 동일 분이 있으면 API OHLCV 로 교체하고, 스트림 상태가 없으면 최소 상태를 만듭니다.
        compose 시 동일 분은 이 공식 completed가 틱 seal보다 우선합니다.
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

    def first_tick_at(self, code: str) -> Optional[datetime]:
        """이번 감시 세션에서 해당 종목의 첫 틱 시각. 없으면 None."""
        try:
            nk = canonical_stock_code(str(code).strip())
            if not nk:
                return None
            with self._lock:
                v = self._first_tick_at.get(nk)
            return v if isinstance(v, datetime) else None
        except Exception:
            return None

    def reset_tick_session(self) -> None:
        """감시 시작/중단 시 틱 세션 시각만 비웁니다(공식 completed는 유지)."""
        try:
            with self._lock:
                self._first_tick_at.clear()
        except Exception:
            pass

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
                self._tick_open.pop(nk, None)
                self._tick_sealed.pop(nk, None)
                self._tick_sealed_by_min.pop(nk, None)
        except (TypeError, ValueError):
            pass
