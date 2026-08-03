# -*- coding: utf-8 -*-
"""KRX 1분 완성 라벨 헬퍼 스모크 검증."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    from utils.krx_bars import (
        m1_bar_open_label_for_just_completed_minute,
        m1_window_start_for_15m_end,
    )

    lab = m1_bar_open_label_for_just_completed_minute("2026-05-12 13:02:30")
    assert str(lab) == "2026-05-12 13:01:00", lab
    t0 = m1_window_start_for_15m_end("2026-05-12 13:15:00")
    assert str(t0) == "2026-05-12 13:00:00", t0
    print("OK: m1_bar_open_label_for_just_completed_minute / m1_window_start_for_15m_end")


if __name__ == "__main__":
    main()
