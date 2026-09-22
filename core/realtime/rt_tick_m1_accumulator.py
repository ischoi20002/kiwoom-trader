# -*- coding: utf-8 -*-
"""
틱 → 1분봉 **검증용** 누적·저장 (매매 경로와 완전 격리).

격리 원칙:
- 호출측 ``offer_tick`` 은 무제한 큐 put 만 하고 즉시 반환 (OHLC/DB 없음).
- 누적·분봉 seal·SQLite 쓰기는 **전용 데몬 스레드**에서만 수행.
- DB 파일은 공식/워밍업 ``realtime_15m_*.db``·``trading_data.db`` 와 **별도**
  (``realtime_1m_tick_*.db``) → 락·I/O 경합 없음.
- ``bars_data`` / SignalEngine / M1 폴러 / 주문 경로에 개입하지 않음.

분 라벨:
- **0B fid 20(체결시간 HHMMSS)** 기준으로 1분 open 라벨을 잡는다.
  수신 벽시계만 쓰면 분 경계 지연 체결이 다음 분 시가로 밀려 종가가 어긋난다.
- fid 20 이 없으면 ``now``/벽시계로 폴백.
- 이미 seal 된 직전 분에 늦은 체결이 오면 recent 버퍼에서 OHLC를 보정 후 재 upsert.

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
from utils.krx_bars import is_krx_m1_closed_open_label


def parse_0b_trade_datetime(
    values: Any,
    *,
    ref: Optional[datetime] = None,
) -> Optional[datetime]:
    """
    키움 실시간 0B ``fid 20``(체결시간) → datetime.

    - 값 예: ``\"101809\"`` / ``101809`` → 당일 10:18:09
    - 날짜는 ``ref``(기본 지금)의 연·월·일 사용 (정규장 중 자정 넘김 없음)
    """
    try:
        vals = values if isinstance(values, dict) else {}
        raw = vals.get("20")
        if raw is None or raw == "":
            raw = vals.get(20)
        if raw is None or raw == "":
            return None
        s = str(raw).strip().replace(":", "")
        # 부호·소수점 잔여 제거
        s = s.replace("+", "").replace("-", "").split(".")[0]
        if not s.isdigit():
            return None
        if len(s) > 6:
            s = s[-6:]
        s = s.zfill(6)
        hh = int(s[0:2])
        mm = int(s[2:4])
        ss = int(s[4:6])
        if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
            return None
        base = ref if isinstance(ref, datetime) else datetime.now()
        return base.replace(hour=hh, minute=mm, second=ss, microsecond=0)
    except Exception:
        return None


def tick_m1_accumulate_save_only_enabled(settings: Optional[Dict[str, Any]]) -> bool:
    try:
        st = settings or {}
        return bool(st.get("realtime_tick_m1_accumulate_save_only", False))
    except Exception:
        return False


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
        # seal 직후 늦은 체결(직전 분) 보정용 — 종목별 최근 open_lab → 봉 상태
        self._recent_sealed: Dict[str, Dict[str, Dict[str, Any]]] = {}
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
        """
        비차단 인제스트. 매매 틱 경로에서 안전하게 호출.

        ``now`` 에는 **체결시각**(0B fid 20)을 넘기는 것을 권장.
        없으면 수신 벽시계로 폴백한다.
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
            trade_dt = now if isinstance(now, datetime) else datetime.now()
            self._ingest_q.put((code, px, cum_v, trade_dt))
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
            code, px, cum_v, trade_dt = item[0], item[1], item[2], item[3]
        except (TypeError, ValueError, IndexError):
            return
        self._apply_tick(
            code,
            float(px),
            float(cum_v),
            trade_dt if isinstance(trade_dt, datetime) else datetime.now(),
        )

    @staticmethod
    def _update_forming_ohlcv(
        st: Dict[str, Any],
        px: float,
        cum_v: float,
        trade_dt: datetime,
    ) -> None:
        """동일 분 봉에 틱 반영. 종가는 체결시각이 더 늦은 쪽을 우선."""
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

    def _remember_sealed(self, nk: str, st: Dict[str, Any]) -> None:
        """seal 직후 늦은 체결 보정을 위해 최근 봉 스냅샷 보관."""
        try:
            open_lab = pd.Timestamp(st["open_lab"]).floor("min")
            key = open_lab.strftime("%Y-%m-%d %H:%M:%S")
            bucket = self._recent_sealed.setdefault(nk, {})
            bucket[key] = {
                "open_lab": open_lab,
                "O": float(st.get("O", 0) or 0),
                "H": float(st.get("H", 0) or 0),
                "L": float(st.get("L", 0) or 0),
                "C": float(st.get("C", 0) or 0),
                "V": float(st.get("V", 0) or 0),
                "cum_v_start": float(st.get("cum_v_start", 0) or 0),
                "cum_v_max": float(
                    st.get("cum_v_max", st.get("cum_v_start", 0)) or 0
                ),
                "last_trade_dt": st.get("last_trade_dt"),
            }
            if len(bucket) > 3:
                for old_k in sorted(bucket.keys())[:-3]:
                    bucket.pop(old_k, None)
        except Exception:
            pass

    def _amend_recent_sealed(
        self,
        nk: str,
        open_lab: pd.Timestamp,
        px: float,
        cum_v: float,
        trade_dt: datetime,
    ) -> bool:
        """이미 seal된 직전 분 봉에 늦은 체결을 반영하고 DB 재 upsert."""
        try:
            key = pd.Timestamp(open_lab).floor("min").strftime("%Y-%m-%d %H:%M:%S")
            bucket = self._recent_sealed.get(nk) or {}
            st = bucket.get(key)
            if st is None:
                return False
            self._update_forming_ohlcv(st, px, cum_v, trade_dt)
            o = float(st.get("O", 0) or 0)
            h = float(st.get("H", 0) or 0)
            l_ = float(st.get("L", 0) or 0)
            c = float(st.get("C", 0) or 0)
            v = float(st.get("V", 0) or 0)
            if c <= 0:
                return False
            if l_ <= 0:
                l_ = c
            self._enqueue_sealed_bar(
                nk, pd.Timestamp(open_lab).floor("min"), o, h, l_, c, max(0.0, v)
            )
            return True
        except Exception as e:
            self._emit(f"⚠️ [Tick1m] amend 예외 ({nk}): {e!r}")
            return False

    def _new_forming(
        self,
        open_lab: pd.Timestamp,
        px: float,
        cum_v: float,
        trade_dt: datetime,
    ) -> Dict[str, Any]:
        return {
            "open_lab": open_lab,
            "O": px,
            "H": px,
            "L": px,
            "C": px,
            "V": 0.0,
            "cum_v_start": cum_v,
            "cum_v_max": cum_v,
            "last_trade_dt": trade_dt,
        }

    def _apply_tick(
        self,
        code: Any,
        px: float,
        cum_v: float,
        trade_dt: datetime,
    ) -> None:
        try:
            nk = canonical_stock_code(str(code).strip())
            if not nk or px <= 0:
                return
            # 분 라벨 = 체결시각 floor (수신 벽시계 아님)
            open_lab = pd.Timestamp(trade_dt).floor("min")
            if not is_krx_m1_closed_open_label(open_lab):
                return
        except (TypeError, ValueError):
            return

        with self._forming_lock:
            try:
                st = self._forming.get(nk)
                if st is None:
                    if self._amend_recent_sealed(nk, open_lab, px, cum_v, trade_dt):
                        return
                    self._forming[nk] = self._new_forming(
                        open_lab, px, cum_v, trade_dt
                    )
                    return

                prev = pd.Timestamp(st["open_lab"]).floor("min")
                if prev == open_lab:
                    self._update_forming_ohlcv(st, px, cum_v, trade_dt)
                    return

                if open_lab < prev:
                    # 분 경계 지연: 다음 분 forming 중 도착한 직전 분 체결
                    self._amend_recent_sealed(nk, open_lab, px, cum_v, trade_dt)
                    return

                # open_lab > prev: 새 분 → 직전 분 seal
                # 새 분 첫 틱의 cum_v 를 넣으면 직전 봉 거래량이 과대됨 → 기존 V/cum_v_max 사용
                self._seal_forming_unlocked(nk, st, seal_cum_v=None)
                self._forming[nk] = self._new_forming(open_lab, px, cum_v, trade_dt)
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
            if not is_krx_m1_closed_open_label(open_lab):
                return
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
            # 늦은 체결 보정을 위해 seal 스냅샷 보관 후 upsert
            try:
                st["V"] = max(0.0, v)
                if seal_cum_v is not None:
                    try:
                        st["cum_v_max"] = max(
                            float(st.get("cum_v_max", 0) or 0), float(seal_cum_v)
                        )
                    except Exception:
                        pass
                self._remember_sealed(nk, st)
            except Exception:
                pass
            self._enqueue_sealed_bar(nk, open_lab, o, h, l_, c, max(0.0, v))
        except Exception as e:
            self._emit(f"⚠️ [Tick1m] seal 예외 ({nk}): {e!r}")
