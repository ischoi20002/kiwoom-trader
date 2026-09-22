# -*- coding: utf-8 -*-
"""
실시간 UI가 15분봉 마감 폭주에서 멈추지 않게 하는 한도.

pandas/지표 계산 워커가 많으면 CPython GIL 때문에 Tk 메인이
수십 초 동안 after() 를 실행하지 못합니다. 워커 수 상한과
짧은 sleep 으로 GIL을 양보하고, UI 콜백 큐는 한 틱 예산을 둡니다.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional, TypeVar

_T = TypeVar("_T")

# 공식 15분 메모리 반영을 한 번에 1종목만 (40종 동시 pandas가 Tk를 굶기지 않게)
_official_15m_apply_lock = threading.Lock()

# 8워커가 09:15 슬롯에서 UI를 ~60초 멈춘 이력. 코드에서 강제 상한.
CLOSED_15M_EVAL_WORKERS_CAP: int = 2
SLOT_ROLL_WORKERS_CAP: int = 2
GIL_YIELD_SEC: float = 0.003
UI_CALL_QUEUE_DRAIN_BUDGET_SEC: float = 0.012
UI_CALL_QUEUE_DRAIN_MAX_N: int = 8


def _clamp_int(raw: Any, default: int, lo: int, hi: int) -> int:
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        v = int(default)
    return max(int(lo), min(int(hi), v))


def clamp_closed_15m_eval_workers(raw: Any, default: int = 2) -> int:
    """확정 15분 평가 워커 수. 설정값이 커도 상한 2."""
    return _clamp_int(raw, default, 1, CLOSED_15M_EVAL_WORKERS_CAP)


def clamp_slot_roll_workers(raw: Any, default: int = 2) -> int:
    """15분 슬롯 롤 워커 수. 설정값이 커도 상한 2."""
    return _clamp_int(raw, default, 1, SLOT_ROLL_WORKERS_CAP)


def yield_gil_for_ui(sec: float = GIL_YIELD_SEC) -> None:
    """지표 계산 직후 Tk 메인이 after/렌더를 돌릴 틈을 줍니다."""
    try:
        time.sleep(max(0.0, min(float(sec), 0.05)))
    except Exception:
        pass


def run_official_15m_memory_apply(apply_fn: Callable[[], _T]) -> _T:
    """
    공식 15분 OHLCV·지표 반영은 호출 스레드에서 직렬 실행합니다.

    Tk ``after(0)`` 로 넘기면 슬롯당 수십 종목이 메인 한 틱에 몰려
    09:47·10:02처럼 UI가 1분 가까이 멈춥니다.
    """
    with _official_15m_apply_lock:
        try:
            return apply_fn()
        finally:
            yield_gil_for_ui()


def drain_ui_call_queue_once(
    q: Any,
    *,
    run_now: Callable[[Any], None],
    schedule_delayed: Callable[[int, Any], None],
    budget_sec: float = UI_CALL_QUEUE_DRAIN_BUDGET_SEC,
    max_n: int = UI_CALL_QUEUE_DRAIN_MAX_N,
    now: Optional[Callable[[], float]] = None,
) -> bool:
    """
    UI 콜백 큐를 시간·건수 한도까지 비웁니다.

    Args:
        q: ``queue.Queue`` — 항목은 ``(delay_ms, callback)``.
        run_now: delay<=0 콜백 실행.
        schedule_delayed: delay>0 예약.
        budget_sec: 한 틱 최대 점유 시간.
        max_n: 한 틱 최대 건수.
        now: 테스트용 시계. 기본 ``time.perf_counter``.

    Returns:
        큐에 남은 작업이 있으면 True (다음 틱을 바로 예약해야 함).
    """
    clock = now if callable(now) else time.perf_counter
    try:
        limit_sec = float(budget_sec)
    except (TypeError, ValueError):
        limit_sec = UI_CALL_QUEUE_DRAIN_BUDGET_SEC
    limit_sec = max(0.004, min(limit_sec, 0.050))
    try:
        limit_n = int(max_n)
    except (TypeError, ValueError):
        limit_n = UI_CALL_QUEUE_DRAIN_MAX_N
    limit_n = max(2, min(limit_n, 32))

    t0 = clock()
    n = 0
    while n < limit_n:
        try:
            if n > 0 and (clock() - t0) >= limit_sec:
                break
        except Exception:
            break
        try:
            delay, callback = q.get_nowait()
        except Exception:
            break
        n += 1
        try:
            d = max(0, int(delay or 0))
        except (TypeError, ValueError):
            d = 0
        try:
            if d <= 0:
                run_now(callback)
            else:
                schedule_delayed(d, callback)
        except Exception:
            pass

    try:
        return not q.empty()
    except Exception:
        return False
