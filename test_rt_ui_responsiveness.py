# -*- coding: utf-8 -*-
"""15분봉 마감 시 Tk 메인을 굶기지 않는 워커 상한·UI 큐 예산."""
from __future__ import annotations

import queue
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


if __name__ == "__main__":
    test_eval_workers_capped_at_two()
    test_roll_workers_capped_at_two()
    test_eval_pool_ignores_settings_eight()
    test_slot_roll_ignores_settings_eight()
    test_drain_respects_max_n()
    test_drain_respects_time_budget()
    test_delayed_callbacks_are_scheduled()
    print("ok")
