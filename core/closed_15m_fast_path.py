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
from typing import Any, Callable, Optional, Set, Tuple

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
    """
    로컬 15분 확정 후 ``eval_on_completed_15m`` 전용 워커 풀.

    FIFO 가 아니라 **보유 전용 워커 1 + 나머지 우선순위 큐**.
    청산(ATR_TRAIL 등)이 미보유 S1 평가 뒤에 수 분씩 밀리지 않게 한다.
    같은 (종목, 슬롯) 은 큐/실행 중 중복 제출하지 않는다.
    """

    def __init__(self, app: Any, log: Optional[Callable[[str], None]] = None) -> None:
        self._app = app
        self._log = log
        self._lock = threading.Lock()
        self._workers = 0
        self._pq: "queue.PriorityQueue[Tuple[int, int, str, Any, str]]" = (
            queue.PriorityQueue()
        )
        self._held_pq: "queue.PriorityQueue[Tuple[int, int, str, Any, str]]" = (
            queue.PriorityQueue()
        )
        self._inflight: Set[Tuple[str, str]] = set()
        self._seq = 0
        self._stop = threading.Event()
        self._worker_threads: list = []
        self._held_thread: Optional[threading.Thread] = None

    def _desired_workers(self) -> int:
        try:
            st = getattr(self._app, "settings", None) or {}
            return clamp_closed_15m_eval_workers(
                st.get("realtime_closed_15m_eval_workers", 2)
            )
        except Exception:
            return clamp_closed_15m_eval_workers(2)

    @staticmethod
    def _slot_key(bar_ts: Any) -> str:
        try:
            import pandas as pd

            return pd.Timestamp(bar_ts).floor("min").strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(bar_ts or "")

    def _eval_priority(self, code: Any) -> int:
        """낮을수록 먼저. 보유=0, 그 외는 (1-합치도)*1000."""
        try:
            from core.rt_closed_15m_official_replace import (
                official_replace_priority_score,
            )

            sc = float(official_replace_priority_score(self._app, code))
        except Exception:
            sc = 0.0
        if sc >= 0.999:
            return 0
        sc = max(0.0, min(1.0, sc))
        # 고합치도(0.9) → 100, 저합치도(0.1) → 900 — 매수 임박을 앞으로
        return max(1, int(round((1.0 - sc) * 1000.0)))

    def pending_approx(self) -> int:
        """평가 대기·실행 중 대략 건수(공식 정렬 양보 판단용)."""
        try:
            with self._lock:
                inflight = len(self._inflight)
            try:
                qn = int(self._pq.qsize())
            except Exception:
                qn = 0
            try:
                hn = int(self._held_pq.qsize())
            except Exception:
                hn = 0
            return max(0, inflight + qn + hn)
        except Exception:
            return 0

    def _ensure_workers(self) -> None:
        with self._lock:
            want = self._desired_workers()
            alive = [t for t in self._worker_threads if t.is_alive()]
            self._worker_threads = alive
            while len(self._worker_threads) < want:
                t = threading.Thread(
                    target=self._worker_loop,
                    args=(self._pq,),
                    name=f"closed15m_eval_{len(self._worker_threads)}",
                    daemon=True,
                )
                try:
                    t.start()
                except Exception:
                    break
                self._worker_threads.append(t)
            self._workers = len(self._worker_threads)
            ht = self._held_thread
            if ht is None or (not ht.is_alive()):
                try:
                    ht = threading.Thread(
                        target=self._worker_loop,
                        args=(self._held_pq,),
                        name="closed15m_eval_held",
                        daemon=True,
                    )
                    ht.start()
                    self._held_thread = ht
                except Exception:
                    self._held_thread = None

    def _worker_loop(
        self,
        pq: "queue.PriorityQueue[Tuple[int, int, str, Any, str]]",
    ) -> None:
        while not self._stop.is_set():
            try:
                item = pq.get(timeout=0.25)
            except queue.Empty:
                continue
            nk = ""
            slot_key = ""
            bar_ts: Any = None
            try:
                _pri, _seq, nk, bar_ts, slot_key = item
            except Exception:
                try:
                    pq.task_done()
                except Exception:
                    pass
                continue
            try:
                se = getattr(self._app, "signal_engine", None)
                if se is not None:
                    se.eval_on_completed_15m(nk, bar_ts)
            except Exception:
                pass
            finally:
                try:
                    with self._lock:
                        self._inflight.discard((str(nk), str(slot_key)))
                except Exception:
                    pass
                yield_gil_for_ui()
                try:
                    pq.task_done()
                except Exception:
                    pass

    def submit_eval(self, code: Any, bar_ts: Any) -> None:
        """종목별 확정 평가를 풀에 제출합니다(보유 전용 워커, 슬롯 중복 생략)."""
        try:
            # 평가 폭주 동안 공식 정렬·지표 재계산이 GIL을 뺏지 않게 quiet 연장
            try:
                pending_n = max(1, int(self.pending_approx()) + 1)
                quiet = min(45.0, 8.0 + float(pending_n) * 1.2)
                mark_rt15_official_quiet(self._app, quiet)
            except Exception:
                try:
                    mark_rt15_official_quiet(self._app)
                except Exception:
                    pass
            try:
                from utils.normalization import canonical_stock_code

                nk = canonical_stock_code(str(code).strip()) or str(code).strip()
            except Exception:
                nk = str(code or "").strip()
            if not nk:
                return
            slot_key = self._slot_key(bar_ts)
            job_key = (str(nk), str(slot_key))
            pri = int(self._eval_priority(nk))
            try:
                with self._lock:
                    if job_key in self._inflight:
                        return
                    self._inflight.add(job_key)
                    self._seq += 1
                    seq = int(self._seq)
            except Exception:
                seq = 0
            target = self._held_pq if pri <= 0 else self._pq
            try:
                target.put((pri, seq, str(nk), bar_ts, slot_key))
            except Exception:
                try:
                    with self._lock:
                        self._inflight.discard(job_key)
                except Exception:
                    pass
                raise
            self._ensure_workers()
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
        try:
            self._stop.set()
        except Exception:
            pass
        with self._lock:
            self._worker_threads = []
            self._workers = 0
            try:
                self._inflight.clear()
            except Exception:
                pass


_official_recalc_lock = threading.Lock()
_official_recalc_pending: Set[str] = set()
_official_recalc_q: "queue.SimpleQueue[Optional[Tuple[Any, str]]]" = queue.SimpleQueue()
_official_recalc_thread: Optional[threading.Thread] = None


def _official_recalc_loop() -> None:
    """공식 15분 교체 후 지표 재계산 — 1워커, 조용/평가 구간에는 양보."""
    while True:
        try:
            item = _official_recalc_q.get()
        except Exception:
            continue
        if item is None:
            continue
        app, code = item
        try:
            # 신호·주문 우선: quiet 동안은 재계산을 미룸
            for _ in range(180):
                try:
                    rem = float(rt15_official_quiet_remaining_sec(app))
                except Exception:
                    rem = 0.0
                busy = 0
                try:
                    pool = getattr(app, "_closed_15m_eval_pool", None)
                    if pool is not None and hasattr(pool, "pending_approx"):
                        busy = int(pool.pending_approx())
                except Exception:
                    busy = 0
                if rem <= 0.05 and busy <= 0:
                    break
                time.sleep(max(0.2, min(rem if rem > 0 else 0.5, 2.0)))
            se = getattr(app, "strategy_engine", None)
            bd = getattr(app, "bars_data", None)
            st = getattr(app, "settings", None) or {}
            if se is not None and isinstance(bd, dict):
                se.recalculate_indicators(code, bd, st)
        except Exception:
            pass
        finally:
            try:
                with _official_recalc_lock:
                    _official_recalc_pending.discard(str(code))
            except Exception:
                pass
            yield_gil_for_ui()


def enqueue_official_indicator_recalc(app: Any, code: Any) -> None:
    """
    공식봉 OHLCV 반영 후 전체 지표 재계산을 후순위로 넣습니다.

    (공식 15분 정렬교체는 비활성. M1 누락채움 등에서만 호출될 수 있음.)
    """
    global _official_recalc_thread
    try:
        nk = str(code or "").strip()
        if not nk:
            return
        with _official_recalc_lock:
            if nk in _official_recalc_pending:
                return
            _official_recalc_pending.add(nk)
            th = _official_recalc_thread
            if th is None or (not th.is_alive()):
                th = threading.Thread(
                    target=_official_recalc_loop,
                    name="rt15m-ind-recalc",
                    daemon=True,
                )
                th.start()
                _official_recalc_thread = th
        try:
            _official_recalc_q.put((app, nk))
        except Exception:
            with _official_recalc_lock:
                _official_recalc_pending.discard(nk)
    except Exception:
        pass


def wait_out_signal_eval_window(app: Any, *, max_wait_sec: float = 45.0) -> None:
    """
    공식 15분 메모리 반영 직전: quiet·평가 큐가 비울 때까지 양보.

    이전 슬롯 정렬이 다음 슬롯 틱말분 평가와 GIL을 다투지 않게 한다.
    """
    try:
        deadline = time.monotonic() + max(1.0, min(float(max_wait_sec), 90.0))
    except Exception:
        deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        try:
            rem = float(rt15_official_quiet_remaining_sec(app))
        except Exception:
            rem = 0.0
        busy = 0
        try:
            pool = getattr(app, "_closed_15m_eval_pool", None)
            if pool is not None and hasattr(pool, "pending_approx"):
                busy = int(pool.pending_approx())
        except Exception:
            busy = 0
        if rem <= 0.05 and busy <= 0:
            return
        try:
            time.sleep(max(0.2, min(rem if rem > 0 else 0.4, 1.5)))
        except Exception:
            return
