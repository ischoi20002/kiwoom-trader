# -*- coding: utf-8 -*-
"""
정규장 일봉(키움 15분봉) 백그라운드 사전조회.

애프터 정책 OFF 일 때 실시간 선정·레짐이 쓰는 정규장 일봉 캐시를 장전·장마감 후(ka10080 유량을
감시 폴링과 나눠 쓰지 않는 시간)에 미리 채운다. 장중에 선정·레짐을 실행하면 이미 채워진 캐시만 쓴다.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from data_manager.krx_regular_daily import (
    ensure_kiwoom_regular,
    is_prefetch_quiet_time,
    latest_complete_day,
    regular_daily_overlay_enabled,
)


class RegularDailyPrefetcher:
    """GUI 메인 루프에서 주기적으로 확인하고, 필요하면 백그라운드 스레드로 조회한다."""

    CHECK_INTERVAL_MS: int = 10 * 60 * 1000
    FIRST_DELAY_MS: int = 60 * 1000

    def __init__(self, app: Any) -> None:
        self.app = app
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._done_until: Optional[str] = None

    def start(self) -> None:
        self._schedule(self.FIRST_DELAY_MS)

    def stop(self) -> None:
        self._stop.set()

    def _schedule(self, delay_ms: int) -> None:
        try:
            self.app.root.after(int(delay_ms), self._tick)
        except Exception:
            pass

    def _log(self, msg: str) -> None:
        try:
            self.app.log(msg)
        except Exception:
            pass

    def _tick(self) -> None:
        try:
            self._maybe_run()
        except Exception as e:
            self._log(f"⚠️ [정규장일봉] 사전조회 확인 실패: {e}")
        finally:
            if not self._stop.is_set():
                self._schedule(self.CHECK_INTERVAL_MS)

    def _maybe_run(self) -> None:
        settings = getattr(self.app, "settings", None)
        if not isinstance(settings, dict) or not regular_daily_overlay_enabled(settings):
            return
        if not is_prefetch_quiet_time():
            return
        until = latest_complete_day()
        if not until or until == self._done_until:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run, args=(dict(settings), until), name="RegularDailyPrefetch", daemon=True
        )
        self._thread.start()

    def _run(self, settings: dict, until: str) -> None:
        try:
            from strategy.universe_codes import resolve_universe_codes

            codes = resolve_universe_codes(settings)
            if not codes:
                return
            res = ensure_kiwoom_regular(
                codes,
                until=until,
                data_manager=getattr(self.app, "data_manager", None),
                log=self._log,
                stop_event=self._stop,
                progress_every=50,
            )
            if not res.get("failed"):
                self._done_until = until
        except Exception as e:
            self._log(f"⚠️ [정규장일봉] 사전조회 실패(다음 확인 때 재시도): {e}")
