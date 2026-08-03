# -*- coding: utf-8 -*-
"""
틱 → 1분봉 **검증용** 누적·저장 (매매 경로와 완전 격리).

격리 원칙:
- 호출측 ``offer_tick`` 은 무제한 큐 put 만 하고 즉시 반환 (OHLC/DB 없음).
- 누적·분봉 seal·SQLite 쓰기는 **전용 데몬 스레드**에서만 수행.
- DB 파일은 공식/워밍업 ``realtime_15m_*.db``·``trading_data.db`` 와 **별도**
  (``realtime_1m_tick_*.db``) → 락·I/O 경합 없음.
- ``bars_data`` / SignalEngine / M1 폴러 / 주문 경로에 개입하지 않음.

빈 시점 배치:
- 분 경계 직후 ``realtime_tick_m1_seal_defer_sec``(기본 4초) 동안은
  확정봉 DB flush 를 미룸 (프로브·공식 1분 조회·신호평가와 겹치지 않게).
"""
from __future__ import annotations

import queue
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from utils.normalization import canonical_stock_code
from utils.server_mode_paths import realtime_tick_1m_db_path


def tick_m1_accumulate_save_only_enabled(settings: Optional[Dict[str, Any]]) -> bool:
    try:
        st = settings or {}
        return bool(st.get("realtime_tick_m1_accumulate_save_only", True))
    except Exception:
        return True


class _Tick1mDbStore:
    """틱 합성 1분봉 전용 SQLite."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        logger_callback: Optional[Callable[[str], None]] = None,
        retention_calendar_days: int = 60,
    ) -> None:
        self._db_path = db_path or realtime_tick_1m_db_path()
        self._log = logger_callback
        self._lock = threading.RLock()
        try:
            self._retention_days = max(1, min(int(retention_calendar_days), 3650))
        except (TypeError, ValueError):
            self._retention_days = 60
        self._ensure_schema()

    def _emit(self, msg: str) -> None:
        if self._log is None:
            return
        try:
            self._log(msg)
        except Exception:
            pass

    def _ensure_schema(self) -> None:
        try:
            with self._lock:
                with sqlite3.connect(self._db_path) as conn:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS bars_1m_tick_rt (
                            code TEXT NOT NULL,
                            datetime TEXT NOT NULL,
                            open INTEGER NOT NULL,
                            high INTEGER NOT NULL,
                            low INTEGER NOT NULL,
                            close INTEGER NOT NULL,
                            volume INTEGER NOT NULL,
                            PRIMARY KEY (code, datetime)
                        )
                        """
                    )
                    conn.commit()
        except Exception as e:
            self._emit(f"⚠️ [Tick1mDB] 스키마 실패: {e!r}")

    def upsert_completed_bar(
        self,
        code: str,
        bar_ts: Any,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> None:
        nk = canonical_stock_code(str(code).strip())
        if not nk:
            return
        try:
            ts = pd.Timestamp(bar_ts).floor("min")
            dt_str = ts.strftime("%Y-%m-%d %H:%M:%S")
            o = int(round(float(open_)))
            h = int(round(float(high)))
            l_ = int(round(float(low)))
            c = int(round(float(close)))
            v = int(max(0, round(float(volume))))
            if c <= 0:
                return
            cut = (datetime.now() - timedelta(days=self._retention_days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            with self._lock:
                with sqlite3.connect(self._db_path) as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO bars_1m_tick_rt
                        (code, datetime, open, high, low, close, volume)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (nk, dt_str, o, h, l_, c, v),
                    )
                    conn.execute(
                        "DELETE FROM bars_1m_tick_rt WHERE code = ? AND datetime < ?",
                        (nk, cut),
                    )
                    conn.commit()
        except Exception as e:
            self._emit(f"⚠️ [Tick1mDB] upsert 실패 ({code}): {e!r}")


class _Tick1mDbUpsertQueue:
    def __init__(
        self,
        store: _Tick1mDbStore,
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
                target=self._loop, name="Tick1mDbUpsert", daemon=True
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
                self._store.upsert_completed_bar(code, ts, o, h, l, c, v)
            except Exception as ex:
                try:
                    if self._log is not None:
                        self._log(f"⚠️ [Tick1mDB] 확정봉 저장 실패: {ex}")
                except Exception:
                    pass


class RtTickM1Accumulator:
    """
    종목별 진행 중 1분 OHLCV를 틱으로 누적하고, 분이 바뀌면 검증용 DB에만 저장.
    """

    def __init__(
        self,
        *,
        settings: Optional[Dict[str, Any]] = None,
        logger_callback: Optional[Callable[[str], None]] = None,
        get_settings: Optional[Callable[[], Dict[str, Any]]] = None,
        seal_interval_sec: float = 0.5,
    ) -> None:
        self._settings = settings or {}
        self._get_settings = get_settings
        self._log = logger_callback
        self._forming_lock = threading.RLock()
        self._forming: Dict[str, Dict[str, Any]] = {}
        # 분 경계 직후 DB flush 대기열 (프로브 구간 회피)
        self._pending_db: List[Tuple[str, pd.Timestamp, float, float, float, float, float]] = []
        self._pending_lock = threading.Lock()

        try:
            ret = int(
                float(
                    (settings or {}).get("realtime_tick_m1_calendar_retention_days", 60)
                )
            )
        except (TypeError, ValueError):
            ret = 60
        self._store = _Tick1mDbStore(
            logger_callback=logger_callback,
            retention_calendar_days=ret,
        )
        self._db_q = _Tick1mDbUpsertQueue(self._store, log=logger_callback)
        self._ingest_q: "queue.SimpleQueue[Optional[Tuple[Any, ...]]]" = (
            queue.SimpleQueue()
        )
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        try:
            self._seal_interval = max(0.2, min(5.0, float(seal_interval_sec)))
        except (TypeError, ValueError):
            self._seal_interval = 0.5
        self._last_seal_mono = 0.0
        try:
            self._db_q.start()
        except Exception:
            pass
        self._worker = threading.Thread(
            target=self._ingest_loop,
            name="Tick1mAccumulate",
            daemon=True,
        )
        self._worker.start()

    def enabled(self) -> bool:
        try:
            st = self._get_settings() if self._get_settings else self._settings
            return tick_m1_accumulate_save_only_enabled(st)
        except Exception:
            return True

    def _seal_defer_sec(self) -> float:
        try:
            st = self._get_settings() if self._get_settings else self._settings
            v = float(st.get("realtime_tick_m1_seal_defer_sec", 4.0))
            return max(0.0, min(v, 30.0))
        except Exception:
            return 4.0

    def _emit(self, msg: str) -> None:
        if self._log is None:
            return
        try:
            self._log(msg)
        except Exception:
            pass

    def shutdown(self) -> None:
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
        # 종료 시 대기 중인 seal 강제 flush
        try:
            self._flush_pending_db(force=True)
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
        """비차단 인제스트. 매매 틱 경로에서 안전하게 호출."""
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

    def _ingest_loop(self) -> None:
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

            if not isinstance(item, tuple):
                if self._stop.is_set():
                    break
                continue

            try:
                self._apply_tick_safe(item)
            except Exception as e:
                self._emit(f"⚠️ [Tick1m] ingest 예외: {e!r}")

            drained = 0
            while drained < 512 and not self._stop.is_set():
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
                    self._apply_tick_safe(nxt)
                except Exception as e:
                    self._emit(f"⚠️ [Tick1m] ingest 예외: {e!r}")
                drained += 1

            self._maybe_seal_periodic()

    def _maybe_seal_periodic(self) -> None:
        try:
            now_m = time.monotonic()
            if (now_m - self._last_seal_mono) < self._seal_interval:
                return
            self._last_seal_mono = now_m
            self.maybe_seal_on_clock()
            self._flush_pending_db(force=False)
        except Exception:
            pass

    def _in_seal_quiet_window(self, wall: datetime) -> bool:
        """분 경계 직후 defer 초 동안 True — DB flush 보류."""
        try:
            defer = self._seal_defer_sec()
            if defer <= 0:
                return False
            return float(wall.second) + wall.microsecond / 1e6 < defer
        except Exception:
            return False

    def _flush_pending_db(self, *, force: bool = False) -> None:
        wall = datetime.now()
        if not force and self._in_seal_quiet_window(wall):
            return
        batch: List[Tuple[str, pd.Timestamp, float, float, float, float, float]] = []
        try:
            with self._pending_lock:
                if not self._pending_db:
                    return
                batch = list(self._pending_db)
                self._pending_db.clear()
        except Exception:
            return
        for nk, ts, o, h, l_, c, v in batch:
            try:
                self._db_q.enqueue(nk, ts, o, h, l_, c, v)
            except Exception:
                pass

    def _enqueue_sealed_bar(
        self,
        nk: str,
        open_lab: pd.Timestamp,
        o: float,
        h: float,
        l_: float,
        c: float,
        v: float,
    ) -> None:
        try:
            with self._pending_lock:
                self._pending_db.append((nk, open_lab, o, h, l_, c, v))
        except Exception:
            pass
        # 조용 구간이 아니면 바로 flush 시도
        try:
            self._flush_pending_db(force=False)
        except Exception:
            pass

    def _apply_tick_safe(self, item: Tuple[Any, ...]) -> None:
        try:
            code, px, cum_v, wall = item[0], item[1], item[2], item[3]
        except (TypeError, ValueError, IndexError):
            return
        self._apply_tick(
            code,
            float(px),
            float(cum_v),
            wall if isinstance(wall, datetime) else datetime.now(),
        )

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
            open_lab = pd.Timestamp(wall).floor("min")
        except (TypeError, ValueError):
            return

        with self._forming_lock:
            try:
                st = self._forming.get(nk)
                if st is None:
                    self._forming[nk] = {
                        "open_lab": open_lab,
                        "O": px,
                        "H": px,
                        "L": px,
                        "C": px,
                        "V": 0.0,
                        "cum_v_start": cum_v,
                    }
                    return

                prev = pd.Timestamp(st["open_lab"]).floor("min")
                if prev == open_lab:
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

                self._seal_forming_unlocked(nk, st, seal_cum_v=cum_v)
                self._forming[nk] = {
                    "open_lab": open_lab,
                    "O": px,
                    "H": px,
                    "L": px,
                    "C": px,
                    "V": 0.0,
                    "cum_v_start": cum_v,
                }
            except Exception as e:
                self._emit(f"⚠️ [Tick1m] apply_tick 예외 ({code}): {e!r}")

    def maybe_seal_on_clock(self, now: Optional[datetime] = None) -> int:
        """벽시계 기준 지난 진행 중 1분을 확정(메모리→pending DB)."""
        if not self.enabled():
            return 0
        wall = now if now is not None else datetime.now()
        sealed = 0
        with self._forming_lock:
            try:
                cur = pd.Timestamp(wall).floor("min")
            except Exception:
                return 0
            for nk in list(self._forming.keys()):
                try:
                    st = self._forming.get(nk)
                    if st is None:
                        continue
                    prev = pd.Timestamp(st["open_lab"]).floor("min")
                    if prev >= cur:
                        continue
                    self._seal_forming_unlocked(nk, st, seal_cum_v=None)
                    self._forming.pop(nk, None)
                    sealed += 1
                except Exception as e:
                    self._emit(f"⚠️ [Tick1m] maybe_seal 예외 ({nk}): {e!r}")
        return sealed

    def _seal_forming_unlocked(
        self,
        nk: str,
        st: Dict[str, Any],
        *,
        seal_cum_v: Optional[float],
    ) -> None:
        try:
            open_lab = pd.Timestamp(st["open_lab"]).floor("min")
            o = float(st.get("O", 0) or 0)
            h = float(st.get("H", 0) or 0)
            l_ = float(st.get("L", 0) or 0)
            c = float(st.get("C", 0) or 0)
            if c <= 0:
                return
            if l_ <= 0:
                l_ = c
            v = float(st.get("V", 0) or 0)
            if seal_cum_v is not None:
                try:
                    start_v = float(st.get("cum_v_start", 0) or 0)
                    if start_v > 0:
                        v = max(0.0, float(seal_cum_v) - start_v)
                except Exception:
                    pass
            self._enqueue_sealed_bar(nk, open_lab, o, h, l_, c, max(0.0, v))
        except Exception as e:
            self._emit(f"⚠️ [Tick1m] seal 예외 ({nk}): {e!r}")
