import threading
import time
import subprocess
import os
import sys

class RemoteCommander:
    """
    [v3.8] 텔레그램을 통한 원격 업데이트 및 재시작 명령을 전담하는 모듈.
    """
    def __init__(self, data_manager, log_func=None):
        self.data_manager = data_manager
        self.log = log_func or print
        self.is_running = False
        
    def start(self):
        """명령어 폴링 스레드를 시작합니다."""
        if self.is_running: return
        self.is_running = True
        threading.Thread(target=self._polling_loop, daemon=True).start()
        self.log("📡 [Remote] 원격 명령 대기 엔진 가동 시작")

    def stop(self):
        self.is_running = False

    def _polling_loop(self):
        """주기적으로 DB의 원격 명령 테이블을 체크합니다."""
        while self.is_running:
            try:
                # [v3.0.4] DataManagerV2의 get_remote_command() 활용
                command = self.data_manager.get_remote_command()
                if command:
                    self.log(f"🔔 [Remote] 원격 명령 수신: {command}")
                    if command == "/update":
                        self._handle_remote_update()
                    elif command == "/restart":
                        self._handle_remote_restart()
                    # 처리된 명령은 DB에서 삭제 (DataManagerV2에서 담당한다고 가정)
            except Exception as e:
                self.log(f"⚠️ [Remote] 명령 폴링 오류: {e}")
            time.sleep(5)

    def _handle_remote_update(self):
        """원격 업데이트 실행 (Git Pull & Re-launch)"""
        self.log("🚀 [Remote] 원격 업데이트 시퀀스 시작...")
        try:
            # 1. deploy.bat 또는 update.ps1 실행
            # Windows 환경이므로 .bat 파일 호출
            update_script = os.path.join(os.getcwd(), "remote_update.bat")
            if os.path.exists(update_script):
                subprocess.Popen(["cmd", "/c", update_script], shell=True)
                self.log("✅ [Remote] 업데이트 스크립트 실행 완료 (프로그램 재시작 예정)")
                # 프로그램 종료 시퀀스는 스크립트가 담당하거나 여기서 sys.exit()
            else:
                self.log("❌ [Remote] 업데이트 스크립트(remote_update.bat)를 찾을 수 없습니다.")
        except Exception as e:
            self.log(f"❌ [Remote] 업데이트 중 치명적 오류: {e}")

    def _handle_remote_restart(self):
        """단순 재시작 실행"""
        self.log("🔄 [Remote] 프로그램 재시작 시퀀스 시작...")
        try:
            restart_script = os.path.join(os.getcwd(), "run_app.bat")
            if os.path.exists(restart_script):
                subprocess.Popen(["cmd", "/c", restart_script, "--auto-start"], shell=True)
                sys.exit(0)
            else:
                self.log("❌ [Remote] 실행 스크립트(run_app.bat)를 찾을 수 없습니다.")
        except Exception as e:
            self.log(f"❌ [Remote] 재시작 중 오류: {e}")
