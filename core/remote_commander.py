import threading
import time
import subprocess
import os
import sys

class RemoteCommander:
    """
    [v3.8] 텔레그램을 통한 원격 업데이트 및 재시작 명령을 전담하는 모듈.
    """
    def __init__(self, app, data_manager, log_func=None):
        self.app = app
        self.data_manager = data_manager
        self.log = log_func or print
        self.is_running = False
        
    def start(self):
        """명령어 폴링 스레드를 시작합니다."""
        if self.is_running: return
        self.is_running = True
        threading.Thread(target=self._polling_loop, daemon=True).start()
        self.log("[Remote] 원격 명령 대기 엔진 가동 시작")

    def stop(self):
        self.is_running = False

    def _polling_loop(self):
        """주기적으로 DB의 원격 명령 테이블을 체크합니다."""
        while self.is_running:
            try:
                # [v3.0.4] DataManagerV2의 get_remote_command() 활용
                command = self.data_manager.get_remote_command()
                if command:
                    self.log(f"[Info] [Remote] 원격 명령 수신: {command}")
                    if command == "/update":
                        self._handle_remote_update()
                    elif command == "/restart":
                        self._handle_remote_restart()
                    elif command == "/select":
                        self._handle_remote_select()
                    elif command == "/start":
                        ens = getattr(
                            self.app, "ensure_fresh_selection_then_start", None
                        )
                        if callable(ens):
                            self.app.root.after(0, ens)
                        else:
                            self.app.root.after(
                                0, self.app.trading_controller.start
                            )
                    # 처리된 명령은 DB에서 삭제됨
            except Exception as e:
                self.log(f"[Error] [Remote] 명령 폴링 오류: {e}")
            time.sleep(5)

    def _resolve_remote_update_script(self) -> str | None:
        """원격 업데이트 배치 (개발 루트 c:\\vibe 우선)."""
        try:
            candidates = (
                os.path.join(r"c:\vibe", "remote_update.bat"),
                os.path.join(os.getcwd(), "remote_update.bat"),
            )
            for path in candidates:
                if os.path.isfile(path):
                    return path
        except Exception:
            pass
        return None

    def _resolve_restart_script(self) -> str | None:
        """운영 재시작 배치 (c:\\k-trader 우선)."""
        try:
            candidates = (
                os.path.join(r"c:\k-trader", "run_k_trader.bat"),
                os.path.join(os.getcwd(), "run_k_trader.bat"),
            )
            for path in candidates:
                if os.path.isfile(path):
                    return path
        except Exception:
            pass
        return None

    def _handle_remote_update(self):
        """원격 업데이트 실행 (Git Pull & Re-launch)"""
        self.log("[Remote] 원격 업데이트 시퀀스 시작...")
        try:
            update_script = self._resolve_remote_update_script()
            if update_script:
                subprocess.Popen(["cmd", "/c", update_script], shell=True)
                self.log("[Ok] [Remote] 업데이트 스크립트 실행 완료 (프로그램 재시작 예정)")
            else:
                self.log("[Error] [Remote] remote_update.bat(c:\\vibe)를 찾을 수 없습니다.")
        except Exception as e:
            self.log(f"[Error] [Remote] 업데이트 중 치명적 오류: {e}")

    def _handle_remote_restart(self):
        """단순 재시작 실행"""
        self.log("[Remote] 프로그램 재시작 시퀀스 시작...")
        try:
            restart_script = self._resolve_restart_script()
            if restart_script:
                subprocess.Popen(
                    ["cmd", "/c", restart_script, "--auto-start"], shell=True
                )
                sys.exit(0)
            else:
                self.log(
                    "[Error] [Remote] run_k_trader.bat(c:\\k-trader)를 찾을 수 없습니다."
                )
        except Exception as e:
            self.log(f"[Error] [Remote] 재시작 중 오류: {e}")

    def _handle_remote_select(self):
        """원격 종목 선정 트리거 (Auto-Confirm 모드)"""
        self.log("[Remote] 원격 종목 선정 시작...")
        if hasattr(self.app, "monitor_tab") and hasattr(
            self.app.monitor_tab, "start_selection_thread"
        ):
            # GUI 스레드 세이프하게 호출
            self.app.root.after(
                0,
                lambda: self.app.monitor_tab.start_selection_thread(auto_confirm=True),
            )
        else:
            self.log("[Error] [Remote] MonitorTab이 로드되지 않아 명령을 수행할 수 없습니다.")
