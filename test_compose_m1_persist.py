# -*- coding: utf-8 -*-
"""15분 합성에 쓰인 1분봉·출처(official/tick/flat) 저장."""
from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from core.realtime_monitor_bars_store import (
    COMPOSE_M1_SOURCE_FLAT,
    COMPOSE_M1_SOURCE_OFFICIAL,
    COMPOSE_M1_SOURCE_TICK,
    RealtimeMonitorBarsStore,
)
from core.realtime_official_bars_sync import compose_local_15m_ohlcv
from core.rt_tick_m1_store import RtTickM1Store


def test_upsert_compose_m1_bars_with_source() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = str(Path(td) / "rt15.db")
        store = RealtimeMonitorBarsStore(db_path=db, retention_calendar_days=30)
        slot = pd.Timestamp("2026-09-11 09:00:00")
        rows = [
            (
                slot + pd.Timedelta(minutes=int(i)),
                100.0 + i,
                101.0,
                99.0,
                100.5,
                10.0 + i,
                src,
            )
            for i, src in enumerate(
                [COMPOSE_M1_SOURCE_TICK] * 2
                + [COMPOSE_M1_SOURCE_OFFICIAL] * 12
                + [COMPOSE_M1_SOURCE_FLAT]
            )
        ]
        n = store.upsert_compose_m1_bars("005930", slot, rows)
        assert n == 15
        with sqlite3.connect(db) as conn:
            cur = conn.execute(
                "SELECT source, COUNT(*) FROM bars_1m_compose_rt "
                "WHERE code=? AND slot_datetime=? GROUP BY source",
                ("005930", "2026-09-11 09:00:00"),
            )
            got = {str(r[0]): int(r[1]) for r in cur.fetchall()}
        assert got.get(COMPOSE_M1_SOURCE_TICK) == 2
        assert got.get(COMPOSE_M1_SOURCE_OFFICIAL) == 12
        assert got.get(COMPOSE_M1_SOURCE_FLAT) == 1
        del store


def test_compose_persists_official_and_tick_sources() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        db = str(Path(td) / "rt15.db")
        store = RealtimeMonitorBarsStore(db_path=db, retention_calendar_days=30)
        m1s = RtTickM1Store(max_completed_per_code=64)
        code = "005930"
        slot = pd.Timestamp("2026-08-18 10:00:00")
        # 공식: 10:00~10:13, 말분 10:14는 틱만
        for i in range(14):
            lab = slot + pd.Timedelta(minutes=int(i))
            px = 70000.0 + float(i)
            m1s.apply_official_closed_minute(code, lab, px, px + 10, px - 10, px, 100.0)
        m1s.feed_tick(code, 70140.0, 1000.0, now=datetime(2026, 8, 18, 10, 14, 5))
        m1s.seal_tick_tail_minute(code, pd.Timestamp("2026-08-18 10:14:00"))

        app = SimpleNamespace(
            settings={
                "realtime_15m_last_minute_from_ticks": False,
                "realtime_15m_compose_tick_fill_on_api_miss": True,
                "realtime_15m_compose_require_official_head": True,
                "realtime_15m_eval_on_official_last_m1": True,
                "realtime_compose_m1_persist_enabled": True,
                "realtime_15m_compose_flat_no_trade": True,
            },
            rt_tick_m1_store=m1s,
            realtime_monitor_bars_store=store,
            rt_tick_stream_ready_at=datetime(2026, 8, 18, 10, 0, 1),
        )
        got = compose_local_15m_ohlcv(app, code, slot)
        assert got is not None
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT datetime, source FROM bars_1m_compose_rt "
                "WHERE code=? AND slot_datetime=? ORDER BY datetime",
                ("005930", "2026-08-18 10:00:00"),
            ).fetchall()
        assert len(rows) == 15
        assert str(rows[-1][1]) == COMPOSE_M1_SOURCE_TICK
        assert all(str(r[1]) == COMPOSE_M1_SOURCE_OFFICIAL for r in rows[:-1])
        del store


if __name__ == "__main__":
    test_upsert_compose_m1_bars_with_source()
    test_compose_persists_official_and_tick_sources()
    print("ok")
