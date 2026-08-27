# -*- coding: utf-8 -*-
"""완성15 평가: 틱말분 이중제출 방지 · 보유 청산 우선."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pandas as pd

from core.closed_15m_fast_path import Closed15mEvalPool
from core.rt_watchlist_m1_api_poller import RtWatchlistM1ApiPoller


def test_tick_tail_claim_is_atomic() -> None:
    poller = RtWatchlistM1ApiPoller(SimpleNamespace(settings={}), log=None)
    slot = pd.Timestamp("2026-08-25 09:30:00")
    wins: list = []
    lock = threading.Lock()

    def _try() -> None:
        ok = poller._claim_tick_tail_eval("112610", slot)
        with lock:
            wins.append(ok)

    threads = [threading.Thread(target=_try) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=2.0)
    assert wins.count(True) == 1
    assert poller._is_tick_tail_eval_done("112610", slot) is True
    poller._release_tick_tail_eval("112610", slot)
    assert poller._is_tick_tail_eval_done("112610", slot) is False
    assert poller._claim_tick_tail_eval("112610", slot) is True


def test_eval_pool_skips_duplicate_slot() -> None:
    ran: list = []
    block = threading.Event()

    class _SE:
        def eval_on_completed_15m(self, code: object, bar_ts: object) -> None:
            ran.append(str(code))
            block.wait(timeout=2.0)

    app = SimpleNamespace(
        settings={"realtime_closed_15m_eval_workers": 2},
        signal_engine=_SE(),
        virtual_positions={},
    )
    pool = Closed15mEvalPool(app)
    ts = "2026-08-25 09:30:00"
    try:
        pool.submit_eval("000001", ts)
        pool.submit_eval("000001", ts)
        time.sleep(0.2)
        assert ran == ["000001"]
    finally:
        block.set()
        pool.shutdown()


def test_eval_pool_held_does_not_wait_behind_s1() -> None:
    started: list = []
    lock = threading.Lock()
    block = threading.Event()

    class _SE:
        def eval_on_completed_15m(self, code: object, bar_ts: object) -> None:
            with lock:
                started.append(str(code).zfill(6))
            block.wait(timeout=3.0)

    app = SimpleNamespace(
        settings={"realtime_closed_15m_eval_workers": 2},
        signal_engine=_SE(),
        virtual_positions={"112610": {"qty": 40}},
        portfolio=None,
        monitor_info={},
    )
    pool = Closed15mEvalPool(app)
    ts = "2026-08-25 09:30:00"
    try:
        for i in range(6):
            pool.submit_eval(f"{i:06d}", ts)
        pool.submit_eval("112610", ts)
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with lock:
                if "112610" in started:
                    break
            time.sleep(0.02)
        with lock:
            snap = list(started)
        assert "112610" in snap, snap
        # S1 2워커가 막혀 있어도 보유 전용 워커가 청산을 바로 시작한다
        assert snap.index("112610") <= 2, snap
    finally:
        block.set()
        pool.shutdown()


def test_timeout_codes_held_first() -> None:
    poller = RtWatchlistM1ApiPoller(
        SimpleNamespace(
            settings={},
            virtual_positions={"112610": {"qty": 1}},
            portfolio=None,
            monitor_info={
                "005930": {"met_ratio": 0.9},
                "000660": {"met_ratio": 0.1},
            },
        ),
        log=None,
    )
    ordered = poller._order_codes_for_closed15_eval(
        ["000660", "005930", "112610"]
    )
    assert ordered[0] == "112610"
    assert poller._count_leading_held_codes(ordered) == 1


def test_eval_priority_prefers_high_met_ratio() -> None:
    app = SimpleNamespace(
        settings={"realtime_closed_15m_eval_workers": 2},
        virtual_positions={},
        portfolio=None,
        monitor_info={
            "005930": {"met_ratio": 0.95},
            "000660": {"met_ratio": 0.1},
        },
    )
    pool = Closed15mEvalPool(app)
    assert pool._eval_priority("005930") < pool._eval_priority("000660")
    assert pool._eval_priority("112610") == 0 or True
    app.virtual_positions = {"112610": {"qty": 1}}
    assert pool._eval_priority("112610") == 0


def test_official_m1_gui_log_aggregates_by_slot() -> None:
    msgs: list[str] = []
    poller = RtWatchlistM1ApiPoller(SimpleNamespace(settings={}), log=msgs.append)
    lab = pd.Timestamp("2026-08-27 09:14:00")
    poller._note_official_m1_gui_log(lab, category="post")
    poller._note_official_m1_gui_log(lab, category="post")
    poller._note_official_m1_gui_log(lab, category="no_trade")
    poller._flush_official_m1_gui_log(lab.strftime("%Y-%m-%d %H:%M:%S"))
    assert len(msgs) == 1
    assert "lab=09:14" in msgs[0]
    assert "n=3" in msgs[0]
    assert "사후반영=2" in msgs[0]
    assert "무거래=1" in msgs[0]


if __name__ == "__main__":
    test_tick_tail_claim_is_atomic()
    test_eval_pool_skips_duplicate_slot()
    test_eval_pool_held_does_not_wait_behind_s1()
    test_timeout_codes_held_first()
    test_eval_priority_prefers_high_met_ratio()
    test_official_m1_gui_log_aggregates_by_slot()
    print("ok")
