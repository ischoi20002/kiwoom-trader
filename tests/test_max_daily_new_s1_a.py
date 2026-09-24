# -*- coding: utf-8 -*-
"""max_daily_new_s1_a 카운터 단위 테스트."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_daily_new_s1_a_counter() -> None:
    from dynamic_backtest import DynamicBacktester

    bt = object.__new__(DynamicBacktester)
    bt.settings = {"max_daily_new_s1_a": 2}
    bt._daily_new_s1_a_date = ""
    bt._daily_new_s1_a_count = 0
    bt._daily_new_s1_a_by_day = {}

    assert bt._max_daily_new_s1_a() == 2
    bt._reset_daily_new_s1_a("2026-09-11")
    assert bt._can_open_new_s1_a("2026-09-11") is True
    bt._note_new_s1_a("2026-09-11")
    bt._note_new_s1_a("2026-09-11")
    assert bt._can_open_new_s1_a("2026-09-11") is False

    # NEXT_OPEN: 전일 루프에서 익일 체결일을 카운트
    bt._note_new_s1_a("2026-09-12 09:00:00")
    bt._reset_daily_new_s1_a("2026-09-12")  # 익일 개장 — 기존 카운트 보존
    assert bt._daily_new_s1_a_by_day.get("2026-09-12") == 1
    assert bt._can_open_new_s1_a("2026-09-12") is True  # lim=2, used=1
    bt._note_new_s1_a("2026-09-12")
    assert bt._can_open_new_s1_a("2026-09-12") is False

    # 0 = 무제한
    bt.settings["max_daily_new_s1_a"] = 0
    assert bt._can_open_new_s1_a("2026-09-12") is True


if __name__ == "__main__":
    test_daily_new_s1_a_counter()
    print("ok")
