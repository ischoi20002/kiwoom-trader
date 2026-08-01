# -*- coding: utf-8 -*-
"""
틱 → 15분봉 **전체 슬롯** 누적기 (저장 전용 · 매매 경로와 격리).

격리 원칙
- 호출측(``offer_tick``)은 무제한 큐 put 만 하고 즉시 반환 (락/DB/OHLC 없음).
- 누적·슬롯 seal·SQLite 쓰기는 **전용 데몬 스레드**에서만 수행.
- DB 파일은 공식/워밍업 ``realtime_15m_*.db`` 와 **별도**
  (``realtime_15m_tick_*.db``) → 락·I/O 경합 없음.
- ``bars_data`` / SignalEngine / 공식 슬롯 watcher / 주문 경로에 훅하지 않음.
"""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

import pandas as pd

from utils.krx_bars import is_krx_15m_slot_end, krx_stream_slot_end_datetime
from utils.normalization import canonical_stock_code


def tick_15m_accumulate_save_only_enabled(settings: Optional[Dict[str, Any]]) -> bool:
    """틱 15분 전체 누적·저장 전용 모드 ON 여부."""
    try:
        st = settings or {}
        return bool(st.get("realtime_tick_15m_accumulate_save_only", True))
    except Exception:
        return True


class _Tick15mDbUpsertQueue:
    """틱 합성 확정 15분봉 SQLite upsert 전용 큐(누적 워커와 별도 스레드)."""

    def __init__(
        self,
        store: Any,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._store = store
        self._log = log
        self._q: "queue.SimpleQueue[Optional[Tuple[Any, ...]]]" = queue.SimpleQueue()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="Tick15mDbUpsert", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._q.put(None)
        except Exception:
            pass
        try:
            th = self._thread
            if th is not None and th.is_alive():
                th.join(timeout=2.0)
        except Exception:
            pass

    def enqueue(
        self,
        code: Any,
        ts: Any,
        o: float,
        h: float,
        l: float,
        c: float,
        v: float,
    ) -> None:
        try:
            self.start()
            self._q.put((code, ts, o, h, l, c, v))
        except Exception:
            # 폴백도 호출 스레드를 오래 막지 않도록 재시도 없이 로그만
            try:
                if self._log is not None:
                    self._log("⚠️ [Tick15mDB] 확정봉 큐잉 실패(드롭)")
            except Exception:
                pass

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._q.get()
            except Exception:
                continue
            if item is None:
                if self._stop.is_set():
                    break
                continue
            try:
                code, ts, o, h, l, c, v = item
                self._store.upsert_tick_completed_bar(code, ts, o, h, l, c, v)
            except Exception as ex:
                try:
                    if self._log is not None:
                        self._log(f"⚠️ [Tick15mDB] 확정봉 저장 실패: {ex}")
                except Exception:
                    pass


class RtTick15mAccumulator:
    """
    종목별 진행 중 15분 슬롯 OHLCV를 틱으로 누적하고, 슬롯 전환 시 저장만 합니다.

    공개 API는 ``offer_tick`` / ``shutdown`` 만 매매 경로에서 호출합니다.
    """

    def __init__(
        self,
        *,
        store: Any,
        settings: Optional[Dict[str, Any]] = None,
        logger_callback: Optional[Callable[[str], None]] = None,
        get_settings: Optional[Callable[[], Dict[str, Any]]] = None,
        seal_interval_sec: float = 1.0,
    ) -> None:
        self._store = store
        self._settings = settings or {}
        self._get_settings = get_settings
        self._log = logger_callback
        self._forming_lock = threading.RLock()
        self._forming: Dict[str, Dict[str, Any]] = {}
        self._db_q = _Tick15mDbUpsertQueue(store, log=logger_callback)
        # 인제스트: 호출 스레드는 put 만
        self._ingest_q: "queue.SimpleQueue[Optional[Tuple[Any, ...]]]" = (
            queue.SimpleQueue()
        )
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        try:
            self._seal_interval = max(0.25, min(5.0, float(seal_interval_sec)))
        except (TypeError, ValueError):
            self._seal_interval = 1.0
        self._last_seal_mono = 0.0
        try:
            self._db_q.start()
        except Exception:
            pass
        self._start_worker()

    def _emit(self, msg: str) -> None:
        if self._log is None:
            return
        try:
            self._log(msg)
        except Exception:
            pass

    def _settings_now(self) -> Dict[str, Any]:
        try:
            if self._get_settings is not None:
                st = self._get_settings()
                if isinstance(st, dict):
                    return st
        except Exception:
            pass
        return self._settings if isinstance(self._settings, dict) else {}

    def enabled(self) -> bool:
        return tick_15m_accumulate_save_only_enabled(self._settings_now())

    def _start_worker(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._ingest_loop,
            name="Tick15mAccumulate",
            daemon=True,
        )
        self._worker.start()

    def shutdown(self) -> None:
        """감시 종료 시 인제스트·DB 큐 정리."""
        self._stop.set()
        try:
            self._ingest_q.put(None)
        except Exception:
            pass
        try:
            th = self._worker
            if th is not None and th.is_alive():
                th.join(timeout=2.0)
        except Exception:
            pass
        try:
            self._db_q.stop()
        except Exception:
            pass

    def offer_tick(
        self,
        code: Any,
        price: float,
        cum_volume: float,
        *,
        skip: bool = False,
        now: Optional[datetime] = None,
    ) -> None:
        """
        비차단 인제스트. OHLC/DB 작업 없음 — 매매 틱 경로에서 안전하게 호출.

        ``skip=True``(호가 전용·공식동기화 합성 틱 등)이면 무시합니다.
        """
        if skip:
            return
        try:
            if not self.enabled():
                return
        except Exception:
            return
        try:
            px = float(price) if price is not None else 0.0
            if px <= 0:
                return
            cum_v = max(0.0, float(cum_volume) if cum_volume is not None else 0.0)
            wall = now if now is not None else datetime.now()
            self._ingest_q.put((code, px, cum_v, wall))
        except Exception:
            pass

    # 레거시 이름 호환(내부·테스트)
    def feed_tick(
        self,
        code: Any,
        price: float,
        cum_volume: float,
        *,
        now: Optional[datetime] = None,
        skip: bool = False,
    ) -> None:
        self.offer_tick(code, price, cum_volume, skip=skip, now=now)

    def _ingest_loop(self) -> None:
        """틱 누적 + 주기적 벽시계 seal (공식봉 watcher와 독립)."""
        while not self._stop.is_set():
            item: Optional[Tuple[Any, ...]] = None
            try:
                item = self._ingest_q.get(timeout=self._seal_interval)
            except queue.Empty:
                item = None
            except Exception:
                item = None

            if item is None:
                if self._stop.is_set():
                    break
                self._maybe_seal_periodic()
                continue

            # None 센티널 = shutdown
            if not isinstance(item, tuple):
                if self._stop.is_set():
                    break
                continue

            try:
                self._apply_tick_unlocked_safe(item)
            except Exception as e:
                self._emit(f"⚠️ [Tick15m] ingest 예외: {e!r}")

            # 같은 배치로 대기 틱을 비워 지연을 줄임(여전히 전용 스레드)
            drained = 0
            while drained < 256 and not self._stop.is_set():
                try:
                    nxt = self._ingest_q.get_nowait()
                except queue.Empty:
                    break
                except Exception:
                    break
                if nxt is None:
                    self._stop.set()
                    break
                if not isinstance(nxt, tuple):
                    continue
                try:
                    self._apply_tick_unlocked_safe(nxt)
                except Exception as e:
                    self._emit(f"⚠️ [Tick15m] ingest 예외: {e!r}")
                drained += 1

            self._maybe_seal_periodic()

    def _maybe_seal_periodic(self) -> None:
        try:
            now_m = time.monotonic()
            if (now_m - self._last_seal_mono) < self._seal_interval:
                return
            self._last_seal_mono = now_m
            self.maybe_seal_on_clock()
        except Exception:
            pass

    def _apply_tick_unlocked_safe(self, item: Tuple[Any, ...]) -> None:
        try:
            code, px, cum_v, wall = item[0], item[1], item[2], item[3]
        except (TypeError, ValueError, IndexError):
            return
        self._apply_tick(code, float(px), float(cum_v), wall if isinstance(wall, datetime) else datetime.now())

    def _apply_tick(
        self,
        code: Any,
        px: float,
        cum_v: float,
        wall: datetime,
    ) -> None:
        try:
            nk = canonical_stock_code(str(code).strip())
            if not nk or px <= 0:
                return
            slot_ts = pd.Timestamp(krx_stream_slot_end_datetime(wall))
        except (TypeError, ValueError):
            return

        with self._forming_lock:
            try:
                st = self._forming.get(nk)
                if st is None:
                    self._forming[nk] = {
                        "slot_end": slot_ts,
                        "O": px,
                        "H": px,
                        "L": px,
                        "C": px,
                        "V": 0.0,
                        "cum_v_start": cum_v,
                    }
                    return

                prev_end = pd.Timestamp(st["slot_end"])
                if prev_end == slot_ts:
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
                    return

                self._seal_forming_unlocked(nk, st, wall=wall, seal_cum_v=cum_v)
                self._forming[nk] = {
                    "slot_end": slot_ts,
                    "O": px,
                    "H": px,
                    "L": px,
                    "C": px,
                    "V": 0.0,
                    "cum_v_start": cum_v,
                }
            except Exception as e:
                self._emit(f"⚠️ [Tick15m] apply_tick 예외 ({code}): {e!r}")

    def maybe_seal_on_clock(self, now: Optional[datetime] = None) -> int:
        """
        벽시계 기준 슬롯이 지난 진행 중 봉을 확정 저장합니다.
        전용 인제스트 스레드에서만 호출합니다(외부 매매 경로에서 호출하지 말 것).
        """
        if not self.enabled():
            return 0
        wall = now if now is not None else datetime.now()
        sealed = 0
        with self._forming_lock:
            try:
                cur_end = pd.Timestamp(krx_stream_slot_end_datetime(wall))
            except Exception:
                return 0
            codes = list(self._forming.keys())
            for nk in codes:
                try:
                    st = self._forming.get(nk)
                    if st is None:
                        continue
                    prev_end = pd.Timestamp(st["slot_end"])
                    if prev_end >= cur_end:
                        continue
                    self._seal_forming_unlocked(nk, st, wall=wall, seal_cum_v=None)
                    self._forming.pop(nk, None)
                    sealed += 1
                except Exception as e:
                    self._emit(f"⚠️ [Tick15m] maybe_seal 예외 ({nk}): {e!r}")
        return sealed

    def _seal_forming_unlocked(
        self,
        nk: str,
        st: Dict[str, Any],
        *,
        wall: datetime,
        seal_cum_v: Optional[float],
    ) -> None:
        """락 보유 상태에서 진행 중 봉 1개를 저장 큐에 넣습니다."""
        try:
            bar_ts = pd.Timestamp(st["slot_end"])
            if not is_krx_15m_slot_end(bar_ts):
                return
            o = float(st.get("O", 0) or 0)
            h = float(st.get("H", 0) or 0)
            l_ = float(st.get("L", 0) or 0)
            c = float(st.get("C", 0) or 0)
            if c <= 0:
                return
            if l_ <= 0:
                l_ = c
            if h < max(o, c, l_):
                h = max(o, c, l_)
            v = float(st.get("V", 0) or 0)
            try:
                if seal_cum_v is not None:
                    start_v = float(st.get("cum_v_start", 0) or 0)
                    same_day = (
                        bar_ts.year == wall.year
                        and bar_ts.month == wall.month
                        and bar_ts.day == wall.day
                    )
                    if same_day and start_v > 0:
                        v = max(0.0, float(seal_cum_v) - start_v)
            except Exception:
                pass
            self._db_q.enqueue(nk, bar_ts, o, h, l_, c, max(0.0, v))
        except Exception as e:
            self._emit(f"⚠️ [Tick15m] seal 예외 ({nk}): {e!r}")
