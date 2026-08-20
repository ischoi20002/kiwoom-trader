# -*- coding: utf-8 -*-
"""
로컬 15분 확정 직후 지연 최소화용 인프라.

- 확정봉 DB upsert 비동기 큐
- S1/청산 평가 ThreadPoolExecutor (종목별 무제한 스레드 방지)
- 슬롯 경계 직후 공식 M1/15분 REST 조용 구간(신호·주문 우선)
"""
from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, Tuple

from core.rt_ui_responsiveness import (
    clamp_closed_15m_eval_workers,
    yield_gil_for_ui,
)

_official_quiet_lock = threading.Lock()
_official_quiet_until_mono: float = 0.0


def closed_15m_defer_official_sec(app: Any) -> float:
    """로컬 15분 확정 직후 공식 REST/동기화를 미룰 초(신호·주문 우선)."""
    try:
        st = getattr(app, "settings", None) or {}
        v = float(st.get("realtime_closed_15m_defer_official_sec", 8.0))
        return max(0.0, min(45.0, v))
    except Exception:
        return 8.0


def mark_rt15_official_quiet(app: Any, duration_sec: Optional[float] = None) -> float:
    """
    공식 M1 폴링·형성봉 sync·15분 교체를 잠시 후순위로 돌립니다.

    Returns:
        적용된 조용 구간 길이(초).
    """
    global _official_quiet_until_mono
    try:
        dur = (
            float(duration_sec)
            if duration_sec is not None
            else closed_15m_defer_official_sec(app)
        )
    except Exception:
        dur = 8.0
    dur = max(0.0, min(45.0, float(dur)))
    if dur <= 0.0:
        return 0.0
    try:
        until = time.monotonic() + dur
        with _official_quiet_lock:
            if until > float(_official_quiet_until_mono):
                _official_quiet_until_mono = until
            # app 속성에도 미러(디버그·외부 조회용)
            try:
                setattr(app, "_rt15_official_quiet_until", float(_official_quiet_until_mono))
            except Exception:
                pass
        return dur
    except Exception:
        return 0.0


def rt15_official_quiet_remaining_sec(app: Any = None) -> float:
    """공식 작업 조용 구간 남은 초(0이면 즉시 가능)."""
    try:
        with _official_quiet_lock:
            until = float(_official_quiet_until_mono)
        rem = until - time.monotonic()
        if rem > 0.0:
            return float(rem)
    except Exception:
        pass
    # app 미러 폴백
    if app is not None:
        try:
            until2 = float(getattr(app, "_rt15_official_quiet_until", 0.0) or 0.0)
            rem2 = until2 - time.monotonic()
            if rem2 > 0.0:
                return float(rem2)
        except Exception:
            pass
    return 0.0


def schedule_after_rt15_official_quiet(
    app: Any,
    fn: Callable[[], None],
    *,
    name: str = "Rt15mOfficialDeferred",
) -> None:
    """조용 구간이 끝난 뒤 ``fn`` 을 한 번 실행합니다(즉시면 바로)."""
    try:
        rem = rt15_official_quiet_remaining_sec(app)
    except Exception:
        rem = 0.0

    def _run() -> None:
        try:
            # 경계가 연달아 갱신됐을 수 있어 한 번 더 확인
            extra = rt15_official_quiet_remaining_sec(app)
            if extra > 0.05:
                time.sleep(extra)
            fn()
        except Exception:
            pass

    if rem <= 0.05:
        try:
            threading.Thread(target=_run, name=name, daemon=True).start()
        except Exception:
            try:
                fn()
            except Exception:
                pass
        return
    try:
        t = threading.Timer(rem, _run)
        t.daemon = True
        t.name = name
        t.start()
    except Exception:
        try:
            threading.Thread(target=_run, name=name, daemon=True).start()
        except Exception:
            pass


class Closed15mDbUpsertQueue:
    """확정 15분봉 SQLite upsert를 전용 스레드에서 처리합니다."""

    def __init__(self, store: Any, log: Optional[Callable[[str], None]] = None) -> None:
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
                target=self._loop, name="Closed15mDbUpsert", daemon=True
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
        """upsert를 큐에 넣고 즉시 반환(평가 경로 블로킹 방지)."""
        try:
            self.start()
            self._q.put((code, ts, o, h, l, c, v))
        except Exception:
            # 폴백: 동기 upsert
            try:
                self._store.upsert_completed_bar(code, ts, o, h, l, c, v)
            except Exception as ex:
                try:
                    if self._log is not None:
                        self._log(f"⚠️ [RT15mDB] 확정봉 저장 실패: {ex}")
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
                        self._log(f"⚠️ [RT15mDB] 확정봉 저장 실패: {ex}")
                except Exception:
                    pass


class Closed15mEvalPool:
    """로컬 15분 확정 후 ``eval_on_completed_15m`` 전용 워커 풀."""

    def __init__(self, app: Any, log: Optional[Callable[[str], None]] = None) -> None:
        self._app = app
        self._log = log
        self._executor: Optional[ThreadPoolExecutor] = None
        self._lock = threading.Lock()
        self._workers = 0

    def _desired_workers(self) -> int:
        try:
            st = getattr(self._app, "settings", None) or {}
            return clamp_closed_15m_eval_workers(
                st.get("realtime_closed_15m_eval_workers", 2)
            )
        except Exception:
            return clamp_closed_15m_eval_workers(2)

    def _ensure(self) -> ThreadPoolExecutor:
        with self._lock:
            want = self._desired_workers()
            if self._executor is not None and self._workers == want:
                return self._executor
            old = self._executor
            self._executor = ThreadPoolExecutor(
                max_workers=want, thread_name_prefix="closed15m_eval"
            )
            self._workers = want
            if old is not None:
                try:
                    old.shutdown(wait=False, cancel_futures=False)
                except TypeError:
                    try:
                        old.shutdown(wait=False)
                    except Exception:
                        pass
                except Exception:
                    pass
            return self._executor

    def submit_eval(self, code: Any, bar_ts: Any) -> None:
        """종목별 확정 평가를 풀에 제출합니다."""
        try:
            # 평가·주문 동안 공식 M1/15분 REST 양보
            try:
                mark_rt15_official_quiet(self._app)
            except Exception:
                pass
            ex = self._ensure()

            def _run() -> None:
                try:
                    se = getattr(self._app, "signal_engine", None)
                    if se is not None:
                        se.eval_on_completed_15m(code, bar_ts)
                except Exception:
                    pass
                finally:
                    # pandas 평가 후 GIL을 놓아 Tk after/렌더가 돌게 함
                    yield_gil_for_ui()

            ex.submit(_run)
        except Exception:
            # 폴백: 단일 데몬 스레드
            try:
                def _fallback() -> None:
                    try:
                        se = getattr(self._app, "signal_engine", None)
                        if se is not None:
                            se.eval_on_completed_15m(code, bar_ts)
                    except Exception:
                        pass
                    finally:
                        yield_gil_for_ui()

                threading.Thread(
                    target=_fallback,
                    name=f"closed15m_{code}",
                    daemon=True,
                ).start()
            except Exception:
                pass

    def shutdown(self) -> None:
        with self._lock:
            if self._executor is not None:
                try:
                    self._executor.shutdown(wait=False, cancel_futures=False)
                except TypeError:
                    try:
                        self._executor.shutdown(wait=False)
                    except Exception:
                        pass
                except Exception:
                    pass
                self._executor = None
                self._workers = 0
