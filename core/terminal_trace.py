# -*- coding: utf-8 -*-
"""
GUI 로그(GuiUtils)와 별개로 **터미널(stderr)** 에 즉시 찍는 진단용 트레이스.

- `settings.json` 의 `debug_terminal_trace`: true 이면 활성
- 환경변수 `K_TRADER_TERMINAL_TRACE` 가 있으면 설정보다 우선
  - 1 / true / yes / on → 강제 ON
  - 0 / false / no / off → 강제 OFF (설정이 true여도 끔)

막힘 구간(워밍업·토큰·ka10080 페이지·WS recv·틱 flush)을 잡기 위한 용도입니다.
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime
from typing import Any, Mapping, Optional


def terminal_trace_enabled(
    app: Any = None,
    settings: Optional[Mapping[str, Any]] = None,
) -> bool:
    """터미널 트레이스 출력 여부."""
    try:
        ev = os.environ.get("K_TRADER_TERMINAL_TRACE", "").strip()
        if ev:
            el = ev.strip().lower()
            if el in ("1", "true", "yes", "on"):
                return True
            if el in ("0", "false", "no", "off"):
                return False
        if settings is None and app is not None:
            try:
                settings = getattr(app, "settings", None) or {}
            except Exception:
                settings = {}
        if isinstance(settings, Mapping):
            return bool(settings.get("debug_terminal_trace", False))
    except Exception:
        pass
    return False


def terminal_trace(
    message: str,
    *,
    app: Any = None,
    settings: Optional[Mapping[str, Any]] = None,
    component: str = "",
) -> None:
    """활성화된 경우에만 stderr 에 한 줄 출력(버퍼 flush)."""
    try:
        if not terminal_trace_enabled(app=app, settings=settings):
            return
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        th = threading.current_thread().name
        prefix = f"[{component}] " if (component or "").strip() else ""
        line = f"{ts} {prefix}[{th}] {message}\n"
        sys.stderr.write(line)
        sys.stderr.flush()
    except Exception:
        pass
