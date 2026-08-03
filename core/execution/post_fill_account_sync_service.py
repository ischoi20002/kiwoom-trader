# -*- coding: utf-8 -*-
import threading
from typing import Any, Optional


class PostFillAccountSyncService:
    """
    체결(매수/매도) 후 계좌 동기화 스케줄러.

    - 연속 체결 시 마지막 체결 기준으로 1회만 예약(디바운스)
    - UI 스레드 부하를 줄이기 위해 동기화 횟수·GUI 갱신을 최소화
    """

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print
        self._lock = threading.RLock()

    def _get_root(self) -> Optional[Any]:
        try:
            return getattr(self.app, "root", None)
        except Exception:
            return None

    def _cancel_post_fill_sync_jobs(self) -> None:
        """기존에 예약된 after를 취소합니다."""
        r = self._get_root()
        if r is None:
            return
        for attr in ("_post_fill_sync_job", "_post_fill_sync_followup"):
            try:
                jid = getattr(self.app, attr, None)
                if jid is not None:
                    try:
                        r.after_cancel(jid)
                    except Exception:
                        pass
                    setattr(self.app, attr, None)
            except Exception:
                pass

    def cancel_jobs(self) -> None:
        try:
            with self._lock:
                self._cancel_post_fill_sync_jobs()
        except Exception:
            pass

    def _first_delay_ms(self) -> int:
        try:
            st = getattr(self.app, "settings", None) or {}
            ms = int(float(st.get("post_fill_sync_first_delay_ms", 8500)))
            return max(3000, min(ms, 60000))
        except Exception:
            return 8500

    def _follow_delay_ms(self) -> int:
        try:
            st = getattr(self.app, "settings", None) or {}
            ms = int(float(st.get("post_fill_sync_follow_delay_ms", 28000)))
            return max(8000, min(ms, 120000))
        except Exception:
            return 28000

    def _run_sync(self, *, refresh_cash: bool, refresh_gui: bool) -> None:
        sync_ctx = "post_fill_full" if refresh_cash else "post_fill_holdings"
        try:
            self.app._run_portfolio_sync_in_daemon_thread(
                silent=False,
                force=True,
                refresh_cash_deposit=bool(refresh_cash),
                wait_for_lock=False,
                schedule_gui_refresh=bool(refresh_gui),
                schedule_orders_refresh=bool(refresh_gui),
                sync_context=sync_ctx,
            )
        except TypeError:
            # 구버전 시그니처 호환
            try:
                self.app._run_portfolio_sync_in_daemon_thread(
                    silent=True,
                    force=True,
                    refresh_cash_deposit=bool(refresh_cash),
                    wait_for_lock=False,
                )
            except Exception as e:
                try:
                    self.log(f"⚠️ [체결후] 동기화 예외: {e}")
                except Exception:
                    pass
        except Exception as e:
            try:
                self.log(f"⚠️ [체결후] 동기화 예외: {e}")
            except Exception:
                pass

    def schedule_after_fill(self) -> None:
        """
        체결 직후 계좌 동기화를 예약합니다.
        - 1차: 보유(ka10085) 위주, GUI 갱신 없음
        - 2차(선택): 예수·D+2 포함 + GUI 1회(마지막 체결 기준 디바운스)
        """
        try:
            r = self._get_root()
            if r is None:
                return

            with self._lock:
                self._cancel_post_fill_sync_jobs()
                try:
                    self.log("ℹ️ [체결후] 잔고·예수 동기화 예약")
                except Exception:
                    pass

                follow_ms = self._follow_delay_ms()
                try:
                    st = getattr(self.app, "settings", None) or {}
                    enable_follow = bool(st.get("post_fill_sync_enable_followup", True))
                except Exception:
                    enable_follow = True

                def _first() -> None:
                    try:
                        self.app._post_fill_sync_job = None
                        self._run_sync(refresh_cash=False, refresh_gui=False)
                    except Exception:
                        pass
                    # 첫 sync는 GUI 전체 갱신 없음 — 보유 유령 UI만 light purge
                    try:
                        fn = getattr(
                            self.app, "request_virtual_tab_sync_light_refresh", None
                        )
                        if callable(fn):
                            fn()
                    except Exception:
                        pass
                    if not enable_follow:
                        return
                    try:
                        r2 = self._get_root()
                        if r2 is None:
                            return

                        def _second() -> None:
                            try:
                                self.app._post_fill_sync_followup = None
                                self._run_sync(refresh_cash=True, refresh_gui=True)
                            except Exception:
                                pass

                        self.app._post_fill_sync_followup = r2.after(
                            follow_ms, _second
                        )
                    except Exception:
                        pass

                self.app._post_fill_sync_job = r.after(
                    self._first_delay_ms(), _first
                )
        except Exception as e0:
            try:
                self.log(f"⚠️ [체결 동기화] 예약 실패: {e0}")
            except Exception:
                pass
