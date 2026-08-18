# -*- coding: utf-8 -*-
"""RT 15분 DB에서 비거래일 가짜봉을 삭제한다."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.realtime_monitor_bars_store import RealtimeMonitorBarsStore

PATHS = [
    Path(r"c:\k-trader\realtime_15m_mock.db"),
    Path(r"c:\k-trader\realtime_15m_live.db"),
    Path(r"c:\vibe\realtime_15m_mock.db"),
    Path(r"c:\vibe\realtime_15m_live.db"),
]


def main() -> None:
    for p in PATHS:
        if not p.is_file():
            print(f"skip missing {p}")
            continue
        print(f"purge {p}")
        store = RealtimeMonitorBarsStore(db_path=str(p), logger_callback=print)
        n = store.purge_non_trading_day_bars()
        print(f"  deleted {n}")


if __name__ == "__main__":
    main()
