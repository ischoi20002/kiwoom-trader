# -*- coding: utf-8 -*-
import threading
from typing import Any, Optional


class PostFillAccountSyncService:
    """
    체결(매수/매도) 후 계좌 동기화 스케줄러(1/2/3차 디바운스).

    - 기존 구현을 그대로 옮기되, KiwoomApp의 오케스트레이션 크기를 줄이기 위한 분리 목적
    - UI thread(Tk)에서 after 예약/취소를 수행
    """

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print

        # 스케줄 중복 방지(예약 체인 자체는 last-call 기준 디바운스로 처리되므로, 이 락은 보조 목적)
        self._lock = threading.RLock()

    def _get_root(self) -> Optional[Any]:
        try:
            return getattr(self.app, "root", None)
        except Exception:
            return None

    def _cancel_post_fill_sync_jobs(self) -> None:
        """기존에 예약된 1/2/3차 after를 취소합니다."""
        r = self._get_root()
        if r is None:
            return

        # KiwoomApp에 이미 존재하는 예약 id들을 그대로 사용/정리
        for attr in (
            "_post_fill_sync_job",
            "_post_fill_sync_followup",
            "_post_fill_sync_followup2",
        ):
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
        """외부 호출용 취소 메서드."""
        try:
            with self._lock:
                self._cancel_post_fill_sync_jobs()
        except Exception:
            pass

    def schedule_after_fill(self) -> None:
        """
        체결 직후 after 체인을 예약합니다.
        - 1차: 약 4.2초
        - 2차: 약 7초(1차 후가 아니라 "마지막 체결 시점" 기준)
        - 3차: 약 22초(2차 기준 추가 대기)
        """
        try:
            r = self._get_root()
            if r is None:
                return

            with self._lock:
                self._cancel_post_fill_sync_jobs()

                first_ms = 4200
                follow_ms = 7000
                third_after_second_ms = 22000

                def _first() -> None:
                    try:
                        self.app._post_fill_sync_job = None
                        self.app._run_portfolio_sync_in_daemon_thread(
                            silent=True,
                            force=True,
                            refresh_cash_deposit=True,
                            wait_for_lock=True,
                        )
                    except Exception as e1:
                        try:
                            self.log(f"⚠️ [체결 동기화] 1차 실행 예외: {e1}")
                        except Exception:
                            pass

                    try:
                        r2 = self._get_root()
                        if r2 is None:
                            return

                        def _second() -> None:
                            try:
                                self.app._post_fill_sync_followup = None
                                self.app._run_portfolio_sync_in_daemon_thread(
                                    silent=True,
                                    force=True,
                                    refresh_cash_deposit=True,
                                    wait_for_lock=True,
                                )
                            except Exception as e2:
                                try:
                                    self.log(f"⚠️ [체결 동기화] 2차 실행 예외: {e2}")
                                except Exception:
                                    pass

                            try:
                                r3 = self._get_root()
                                if r3 is None:
                                    return

                                def _third() -> None:
                                    try:
                                        self.app._post_fill_sync_followup2 = None
                                        self.app._run_portfolio_sync_in_daemon_thread(
                                            silent=True,
                                            force=True,
                                            refresh_cash_deposit=True,
                                            wait_for_lock=True,
                                        )
                                    except Exception as e3b:
                                        try:
                                            self.log(
                                                f"⚠️ [체결 동기화] 3차 실행 예외: {e3b}"
                                            )
                                        except Exception:
                                            pass

                                self.app._post_fill_sync_followup2 = r3.after(
                                    third_after_second_ms, _third
                                )
                            except Exception as e3a:
                                try:
                                    self.log(f"⚠️ [체결 동기화] 3차 예약 실패: {e3a}")
                                except Exception:
                                    pass

                        self.app._post_fill_sync_followup = r2.after(
                            follow_ms, _second
                        )
                    except Exception as e3:
                        try:
                            self.log(f"⚠️ [체결 동기화] 2차 예약 실패: {e3}")
                        except Exception:
                            pass

                self.app._post_fill_sync_job = r.after(first_ms, _first)
        except Exception as e0:
            try:
                self.log(f"⚠️ [체결 동기화] 예약 실패: {e0}")
            except Exception:
                pass

