# -*- coding: utf-8 -*-
"""장시작 기준 총자산이 재시작·장중 EOD 덮어쓰기로 바뀌지 않는지."""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from core.eod_equity_snapshot_service import resolve_eod_snapshot_date
from utils.session_open_day_pnl import (
    load_day_pnl_baseline,
    on_monitoring_started,
    persist_day_pnl_baseline,
)


def _is_td(d: date) -> bool:
    return d.weekday() < 5


def _prev_td(d: date) -> date:
    x = d - timedelta(days=1)
    while x.weekday() >= 5:
        x -= timedelta(days=1)
    return x


def _due(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, 15, 40)


def test_intraday_does_not_pick_previous_day() -> None:
    now = datetime(2026, 8, 20, 10, 6, 50)
    got = resolve_eod_snapshot_date(
        now,
        is_trading_day_fn=_is_td,
        previous_trading_day_fn=_prev_td,
        end_of_day_due_fn=_due,
    )
    assert got is None


def test_after_close_picks_today() -> None:
    now = datetime(2026, 8, 20, 16, 0, 0)
    got = resolve_eod_snapshot_date(
        now,
        is_trading_day_fn=_is_td,
        previous_trading_day_fn=_prev_td,
        end_of_day_due_fn=_due,
    )
    assert got == date(2026, 8, 20)


def test_baseline_file_roundtrip_same_day_only() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "_day_pnl_baseline.json")
        st = {
            "day": "2026-08-20",
            "baseline": 9517885.0,
            "baseline_locked": True,
            "baseline_source": "pre_open",
            "captured_at": "2026-08-20T08:59:00",
        }
        assert persist_day_pnl_baseline(st, path=path) is True
        rec = load_day_pnl_baseline("2026-08-20", path=path)
        assert rec is not None
        assert float(rec["baseline"]) == 9517885.0
        assert load_day_pnl_baseline("2026-08-19", path=path) is None


def test_monitoring_restart_keeps_persisted_baseline() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "_day_pnl_baseline.json")
        persist_day_pnl_baseline(
            {
                "day": "2026-08-20",
                "baseline": 9123456.0,
                "baseline_locked": True,
                "baseline_source": "pre_open",
                "captured_at": "2026-08-20T08:59:12",
            },
            path=path,
        )
        logs: list = []
        app = SimpleNamespace(
            is_monitoring=True,
            log=logs.append,
            root=None,
            virtual_tab=None,
            portfolio=None,
        )
        with patch(
            "utils.session_open_day_pnl.day_pnl_baseline_path",
            return_value=path,
        ):
            on_monitoring_started(app, now=datetime(2026, 8, 20, 10, 12, 0))
        st = getattr(app, "_session_open_day_pnl", {})
        assert float(st.get("baseline")) == 9123456.0
        assert bool(st.get("baseline_locked")) is True
        assert any("유지(재시작)" in str(x) for x in logs)


if __name__ == "__main__":
    test_intraday_does_not_pick_previous_day()
    test_after_close_picks_today()
    test_baseline_file_roundtrip_same_day_only()
    test_monitoring_restart_keeps_persisted_baseline()
    print("ok")
