# -*- coding: utf-8 -*-
import threading
import time
from typing import Any, Optional


class RestAuthRetryService:
    """
    키움 REST 인증/재시도 워커를 담당하는 서비스.

    - 백그라운드 thread에서 토큰 발급을 시도
    - 성공 시 UI 상태 라벨/부가 초기화 수행
    - 실패 시 UI 상태를 인증 실패로 전환
    """

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print

        self._lock = threading.Lock()
        self._running: bool = False

    def is_running(self) -> bool:
        """인증 재시도 워커가 돌고 있는지."""
        try:
            with self._lock:
                return bool(self._running)
        except Exception:
            return False

    def start(self) -> None:
        """인증 워커를 시작합니다. 이미 실행 중이면 스킵합니다."""
        acquired = False
        try:
            with self._lock:
                if self._running:
                    acquired = True
                    self._safe_log("ℹ️ [Auth] 인증·연결 갱신이 이미 진행 중입니다.")
                    return
                self._running = True
                acquired = True
        finally:
            # acquired는 사용하지 않지만, 안전하게 유지
            _ = acquired

        def worker() -> None:
            try:
                self._run_auth_token_with_retries()
            finally:
                with self._lock:
                    self._running = False

        try:
            threading.Thread(target=worker, daemon=True).start()
        except Exception as e:
            self._safe_log(f"❌ [Auth] 인증 스레드 시작 실패: {e}")
            with self._lock:
                self._running = False

    def _safe_log(self, msg: str) -> None:
        try:
            self.log(msg)
        except Exception:
            pass

    def _run_auth_token_with_retries(self) -> None:
        """백그라운드 전용: 토큰 발급 재시도 후 성공 시 부가 초기화 수행."""
        max_attempts = 12
        delay_sec = 3.0
        max_delay = 45.0
        backoff_mult = 1.25
        token: Optional[str] = None

        try:
            err_fg = self.app.colors.get("error", "#BF616A")
            ok_fg = self.app.colors.get("success", "#A3BE8C")
        except Exception:
            err_fg, ok_fg = "#BF616A", "#A3BE8C"

        try:
            self.app._boot_progress_set_message("키움 REST 인증 연결 중…")
        except Exception:
            pass

        for attempt in range(1, max_attempts + 1):
            try:
                am = getattr(self.app, "auth_manager", None)
                if am is not None and getattr(am, "is_fatal_auth_error", lambda: False)():
                    break

                wait_sec = 0.0
                if attempt > 1:
                    wait_sec = float(delay_sec)
                    try:
                        if am is not None and hasattr(am, "oauth_cooldown_remaining_sec"):
                            wait_sec = max(
                                wait_sec, float(am.oauth_cooldown_remaining_sec())
                            )
                    except Exception:
                        pass
                    try:
                        if wait_sec >= 5.0:
                            self._safe_log(
                                f"⏳ [Auth] {int(wait_sec)}초 대기 후 "
                                f"{attempt}/{max_attempts}차 인증 시도…"
                            )
                        else:
                            self._safe_log(
                                f"🔁 [Auth] {attempt}/{max_attempts}차 인증 시도 "
                                f"({wait_sec:.1f}초 대기 후)…"
                            )
                    except Exception:
                        pass
                    if wait_sec > 0:
                        time.sleep(wait_sec)
                    delay_sec = min(delay_sec * backoff_mult, max_delay)
                    try:
                        if am is not None and not am.is_fatal_auth_error():
                            am.invalidate_cached_token()
                    except Exception:
                        pass

                try:
                    token = (
                        am.get_auth_token()
                        if am is not None
                        else None
                    )
                except Exception as ex:
                    token = None
                    self._safe_log(f"⚠️ [Auth] {attempt}/{max_attempts}차 예외: {ex}")

                if token:
                    if attempt > 1:
                        self._safe_log(f"✅ [Auth] {attempt}회차에서 인증 성공")
                    break

                if am is not None and am.is_fatal_auth_error():
                    break
            except Exception as loop_ex:
                self._safe_log(f"⚠️ [Auth] 재시도 루프 오류: {loop_ex}")

        if not token:
            # UI 갱신은 Tk 메인 스레드로 위임
            try:
                am = getattr(self.app, "auth_manager", None)
                if am is not None and am.is_fatal_auth_error():
                    self._safe_log(
                        "❌ [Auth] API 키가 올바르지 않아 인증을 중단했습니다. "
                        "config.py 키를 수정한 뒤 앱을 다시 시작하세요."
                    )
                else:
                    self._safe_log(
                        f"❌ [Auth] {max_attempts}회 시도 후에도 인증에 실패했습니다. "
                        "네트워크·키움 API 상태를 확인하거나 잠시 뒤 앱을 다시 시작해 주세요."
                    )
            except Exception:
                pass

            def _fail_status() -> None:
                try:
                    if self.app.status_label:
                        self.app.status_label.config(
                            text="상태: 인증 실패 — 연결 확인", foreground=err_fg
                        )
                except Exception:
                    pass

            try:
                run_ui = getattr(self.app, "run_on_ui_thread", None)
                if callable(run_ui):
                    run_ui(_fail_status, 0)
                else:
                    self.app.root.after(0, _fail_status)
            except Exception:
                pass
            try:
                self.app._boot_progress_finish(ok=False)
            except Exception:
                pass
            return

        # 로그인 완료 상태 라벨
        def _ok_status() -> None:
            try:
                if self.app.status_label:
                    self.app.status_label.config(
                        text="상태: 로그인 완료", foreground=ok_fg
                    )
            except Exception:
                pass

        try:
            run_ui = getattr(self.app, "run_on_ui_thread", None)
            if callable(run_ui):
                run_ui(_ok_status, 0)
            else:
                self.app.root.after(0, _ok_status)
        except Exception:
            pass

        try:
            self.app._boot_progress_set_message("계좌·시장 데이터 준비 중…")
        except Exception:
            pass

        # 상세 요약은 첫 계좌 동기화(REST)가 끝난 뒤 PortfolioManager 에서 한 번만 출력
        try:
            self._safe_log(
                "[기동] 인증 완료 → 약 2초 후 첫 계좌 동기화(백그라운드). "
                "그동안 탭 전환 가능. 로그는 동기화가 끝나는 대로 올라옵니다."
            )
        except Exception:
            pass

        try:
            # 포지션/이력은 GUI 초기화의 portfolio.load_all()에서 1회만 복구.
            # 인증 후 재로드는 동일 로그·DB 덮어쓰기만 반복하므로 생략하고,
            # 계좌와의 정합은 아래 첫 동기화에서 맞춘다.
            try:
                api = getattr(self.app, "status_api_server", None)
                if api is not None:
                    api.start()
            except Exception:
                try:
                    self._safe_log("[MobileAPI] 인증 후 기동 실패")
                except Exception:
                    pass

            try:
                self.app._schedule_vp_stub_price_refresh_after_auth()
            except Exception:
                pass

            def _schedule_first_sync() -> None:
                try:
                    self.app._run_portfolio_sync_in_daemon_thread(
                        silent=True, force=True
                    )
                except Exception:
                    pass

            def _resume_monitoring_after_restart() -> None:
                try:
                    fn = getattr(self.app, "try_resume_monitoring_after_restart", None)
                    if callable(fn):
                        fn()
                except Exception:
                    pass

            try:
                run_ui = getattr(self.app, "run_on_ui_thread", None)
                if callable(run_ui):
                    run_ui(_schedule_first_sync, 2000)
                    if bool(getattr(self.app, "_restart_resume_monitoring", False)):
                        run_ui(_resume_monitoring_after_restart, 3500)
                else:
                    self.app.root.after(2000, _schedule_first_sync)
                    if bool(getattr(self.app, "_restart_resume_monitoring", False)):
                        self.app.root.after(3500, _resume_monitoring_after_restart)
            except Exception:
                pass

            threading.Thread(
                target=lambda: self.app.get_kospi_200(force=True), daemon=True
            ).start()

            self.app._boot_progress_finish(ok=True)
        except Exception:
            try:
                self._safe_log("❌ [Auth] 인증 후 초기화 중 오류")
            except Exception:
                pass
            try:
                self.app._boot_progress_finish(ok=False)
            except Exception:
                pass

