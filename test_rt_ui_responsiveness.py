# -*- coding: utf-8 -*-
"""15분봉 마감 시 Tk 메인을 굶기지 않는 워커 상한·UI 큐 예산."""
from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

from core.closed_15m_fast_path import Closed15mEvalPool
from core.realtime_official_bars_sync import _slot_roll_parallel_workers
from core.rt_ui_responsiveness import (
    clamp_closed_15m_eval_workers,
    clamp_slot_roll_workers,
    drain_ui_call_queue_once,
)


def test_eval_workers_capped_at_two() -> None:
    assert clamp_closed_15m_eval_workers(8) == 2
    assert clamp_closed_15m_eval_workers(16) == 2
    assert clamp_closed_15m_eval_workers(1) == 1
    assert clamp_closed_15m_eval_workers("bad") == 2


def test_roll_workers_capped_at_two() -> None:
    assert clamp_slot_roll_workers(16) == 2
    assert clamp_slot_roll_workers(0) == 1


def test_eval_pool_ignores_settings_eight() -> None:
    app = SimpleNamespace(settings={"realtime_closed_15m_eval_workers": 8})
    pool = Closed15mEvalPool(app)
    assert pool._desired_workers() == 2


def test_slot_roll_ignores_settings_eight() -> None:
    app = SimpleNamespace(settings={"realtime_15m_slot_roll_workers": 8})
    assert _slot_roll_parallel_workers(app) == 2


def test_drain_respects_max_n() -> None:
    q: queue.Queue = queue.Queue()
    ran: list = []
    for i in range(20):
        q.put((0, i))

    remaining = drain_ui_call_queue_once(
        q,
        run_now=ran.append,
        schedule_delayed=lambda d, c: None,
        budget_sec=1.0,
        max_n=8,
    )
    assert ran == list(range(8))
    assert remaining is True
    assert q.qsize() == 12


def test_drain_respects_time_budget() -> None:
    q: queue.Queue = queue.Queue()
    times = [0.0]
    ran: list = []
    for i in range(10):
        q.put((0, i))

    def fake_now() -> float:
        return times[0]

    def run_now(cb: object) -> None:
        ran.append(cb)
        times[0] += 0.01

    remaining = drain_ui_call_queue_once(
        q,
        run_now=run_now,
        schedule_delayed=lambda d, c: None,
        budget_sec=0.012,
        max_n=32,
        now=fake_now,
    )
    assert ran == [0, 1]
    assert remaining is True


def test_official_apply_runs_on_calling_thread() -> None:
    from core.rt_ui_responsiveness import run_official_15m_memory_apply

    ran: list = []

    def _fn() -> str:
        ran.append(threading.get_ident())
        return "ok"

    assert run_official_15m_memory_apply(_fn) == "ok"
    assert ran == [threading.get_ident()]


def test_delayed_callbacks_are_scheduled() -> None:
    q: queue.Queue = queue.Queue()
    scheduled: list = []
    q.put((100, "later"))
    remaining = drain_ui_call_queue_once(
        q,
        run_now=lambda c: None,
        schedule_delayed=lambda d, c: scheduled.append((d, c)),
        budget_sec=1.0,
        max_n=8,
    )
    assert scheduled == [(100, "later")]
    assert remaining is False


def test_official_indicator_recalc_does_not_block_enqueue() -> None:
    import time

    from core.closed_15m_fast_path import enqueue_official_indicator_recalc

    started = threading.Event()
    block = threading.Event()
    ran: list = []

    class _SE:
        def recalculate_indicators(self, code: object, bars_data: object, settings: object) -> None:
            started.set()
            ran.append(str(code))
            block.wait(timeout=2.0)

    # 정렬 모드가 아니면 재계산 큐에 들어간다
    app = SimpleNamespace(
        strategy_engine=_SE(),
        bars_data={"A": object()},
        settings={
            "realtime_watchlist_m1_api_poller_enabled": False,
            "realtime_closed_15m_eval_after_official": False,
        },
    )
    t0 = time.monotonic()
    enqueue_official_indicator_recalc(app, "A")
    enqueue_official_indicator_recalc(app, "A")  # 중복 생략
    elapsed = time.monotonic() - t0
    assert elapsed < 0.2, elapsed
    assert started.wait(timeout=1.0)
    block.set()
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and ran != ["A"]:
        time.sleep(0.01)
    assert ran == ["A"]


def test_alignment_mode_skips_official_indicator_recalc() -> None:
    import time

    from core.closed_15m_fast_path import enqueue_official_indicator_recalc

    ran: list = []

    class _SE:
        def recalculate_indicators(self, code: object, bars_data: object, settings: object) -> None:
            ran.append(str(code))

    app = SimpleNamespace(
        strategy_engine=_SE(),
        bars_data={"A": object()},
        settings={
            "realtime_watchlist_m1_api_poller_enabled": True,
            "realtime_closed_15m_eval_after_official": False,
            "realtime_s1_eval_on_local_15m_close": True,
        },
    )
    enqueue_official_indicator_recalc(app, "A")
    time.sleep(0.15)
    assert ran == []


def test_alignment_batch_coalesces_one_worker() -> None:
    """정렬 schedule N회 → 배치 워커 1회만 기동."""
    from core.rt_closed_15m_official_replace import RtClosed15mOfficialReplaceScheduler

    sch = RtClosed15mOfficialReplaceScheduler(log=None)
    started: list[str] = []

    def _fake_worker(app: object, bar_ts: object) -> None:
        started.append("one")

    sch._align_batch_worker = _fake_worker  # type: ignore[method-assign]
    app = SimpleNamespace(settings={})
    bar_ts = "2026-08-25 13:15:00"
    for code in ("097230", "028670", "020150"):
        sch._align_batch_offer(app, code, bar_ts)
    import time

    time.sleep(0.05)
    assert started == ["one"]
    with sch._lock:
        assert sch._align_batch_codes["2026-08-25 13:15:00"] == {
            "097230",
            "028670",
            "020150",
        }


if __name__ == "__main__":
    test_eval_workers_capped_at_two()
    test_roll_workers_capped_at_two()
    test_eval_pool_ignores_settings_eight()
    test_slot_roll_ignores_settings_eight()
    test_drain_respects_max_n()
    test_drain_respects_time_budget()
    test_official_apply_runs_on_calling_thread()
    test_delayed_callbacks_are_scheduled()
    test_official_indicator_recalc_does_not_block_enqueue()
    test_alignment_mode_skips_official_indicator_recalc()
    test_alignment_batch_coalesces_one_worker()
    print("ok")
