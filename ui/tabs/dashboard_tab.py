import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import os
import strategy_selector

class DashboardTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self._setup_ui()

    def _setup_ui(self):
        """대시보드 탭 레이아웃 고도화 (Midnight Control Room 스타일)"""
        db_main_frame = ttk.Frame(self.parent, padding="25")
        db_main_frame.pack(fill=tk.BOTH, expand=True)

        # 중앙 핵심 컨트롤 센터 (Grid 레이아웃)
        tiles_frame = ttk.Frame(db_main_frame)
        tiles_frame.pack(fill=tk.X, pady=(0, 25))
        
        # [타일 1] 전략 종목 스캔 (Scanner) - 가장 왼쪽, 큰 비중
        scan_tile = ttk.LabelFrame(tiles_frame, text=" 🎯 종목 스캐너 ", padding="15")
        scan_tile.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        ttk.Label(scan_tile, text="KOSPI 200 종목 중\n최적 전략 종목을 찾습니다.", foreground="#81A1C1").pack(expand=True)
        self.btn_select = ttk.Button(scan_tile, text="종목 선정 실행", style="Action.TButton", 
                                        command=self.start_selection_thread, state=tk.DISABLED)
        self.btn_select.pack(side=tk.BOTTOM, fill=tk.X, pady=(15,0))

        # [타일 2] 데이터 수집 (Harvest) - 중간, 큰 비중
        harvest_tile = ttk.LabelFrame(tiles_frame, text=" 🚜 데이터 수집 ", padding="15")
        harvest_tile.grid(row=0, column=1, sticky="nsew", padx=12)
        ttk.Label(harvest_tile, text="최신 시장 데이터를\n로컬 DB로 저장합니다.", foreground="#81A1C1").pack(expand=True)
        self.btn_harvest = ttk.Button(harvest_tile, text="수집 시작", style="Action.TButton", command=self._on_btn_harvest)
        self.btn_harvest.pack(side=tk.BOTTOM, fill=tk.X, pady=(15,0))

        # [타일 3] 인증 및 연결 (Auth) - 가장 오른쪽, 작은 비중
        auth_tile = ttk.LabelFrame(tiles_frame, text=" 🔑 계정 연결 ", padding="15")
        auth_tile.grid(row=0, column=2, sticky="nsew", padx=(12, 0))
        self.btn_auth = ttk.Button(auth_tile, text="인증 재시도", style="Subtle.TButton", command=self.app.get_auth_token)
        self.btn_auth.pack(side=tk.BOTTOM, fill=tk.X, pady=(15,0))

        # 컬럼 가중치 설정 (2:2:1 비율)
        tiles_frame.columnconfigure(0, weight=2)
        tiles_frame.columnconfigure(1, weight=2)
        tiles_frame.columnconfigure(2, weight=1)

        # 상태 요약 라인 (아이콘 추가)
        status_info_frame = ttk.Frame(db_main_frame)
        status_info_frame.pack(fill=tk.X, pady=(0, 12))
        self.app.status_label = ttk.Label(status_info_frame, text="⚡ 시스템 엔진 가동 중...", 
                                        font=("Malgun Gothic", 10), foreground=self.app.colors['primary'])
        self.app.status_label.pack(side=tk.LEFT)

        # [v5.0] 노르딕 스타일 로그 영역 (Soft Slate-Blue)
        self.app.log_area = scrolledtext.ScrolledText(db_main_frame, height=12, 
                                                     font=("Consolas", 10), 
                                                     bg="#242933", fg="#D8DEE9",
                                                     insertbackground="#88C0D0",
                                                     borderwidth=0, padx=10, pady=10)
        self.app.log_area.pack(fill=tk.BOTH, expand=True)
        
        # [v4.5.4] GuiUtils에 로그 위젯 연결
        self.app.gui_utils.log_area = self.app.log_area
        
        # 로그 태그 설정 (노르딕 팔레트 기반)
        self.app.log_area.tag_configure("info", foreground="#88C0D0") # Frost Blue
        self.app.log_area.tag_configure("success", foreground="#A3BE8C") # Moss Green
        self.app.log_area.tag_configure("error", foreground="#BF616A") # Aurora Red
        self.app.log_area.tag_configure("warning", foreground="#EBCB8B") # Yellow
        self.app.log_area.tag_configure("header", foreground="#B48EAD", font=("Consolas", 10, "bold")) # Purple

    def _on_btn_harvest(self):
        """데이터 하베스팅 서비스 실행 (독립 프로세스)"""
        try:
            import subprocess
            # 별도 콘솔 창에서 실행하여 GUI 종료와 무관하게 동작하도록 함
            subprocess.Popen(["python", "harvest_service.py"], creationflags=subprocess.CREATE_NEW_CONSOLE)
            self.app.log("🚀 [하베스팅] 전 종목 수집 프로세스가 별도 창에서 시작되었습니다.")
            messagebox.showinfo("알림", "데이터 하베스팅이 별도 창에서 시작되었습니다.\n이 프로그램(GUI)을 종료해도 수집은 계속됩니다.")
        except Exception as e:
            self.app.log(f"❌ [하베스팅] 실행 실패: {e}")
            messagebox.showerror("오류", f"수집 프로세스 실행 중 오류 발생: {e}")

    def start_selection_thread(self, auto_confirm=False):
        if self.btn_select: self.btn_select.config(state=tk.DISABLED)
        threading.Thread(target=self.select_monitoring_targets, args=(auto_confirm,), daemon=True).start()

    def select_monitoring_targets(self, auto_confirm=False):
        """[v2.9/v4.5.7] 선정 로직을 실행하고 결과에 따라 팝업 또는 자동 확정 처리"""
        try:
            df_result = strategy_selector.select_monitoring_targets(
                settings=self.app.settings,
                logger_callback=self.app.log,
                data_manager=self.app.data_manager
            )
            
            if df_result is not None and not df_result.empty:
                if auto_confirm:
                    # [v4.5.7] 원격 명령(/select) 대응: 팝업 없이 즉시 기동
                    self.app.trading_controller.set_monitor_targets(df_result)
                    self.app.monitor_codes = self.app.trading_controller.monitor_codes
                    self.app.monitor_info = self.app.trading_controller.monitor_info
                    
                    if hasattr(self.app, 'monitor_tab'):
                        self.app.monitor_tab.update_watchlist(self.app.monitor_info)
                    
                    self.app.log(f"✅ [Remote] {len(self.app.monitor_codes)}개 종목이 자동으로 실시간 감시 대상으로 등록되었습니다.")
                    
                    # 텔레그램 결과 전송
                    msg = f"<b>🎯 [원격 종목 선정 완료]</b>\n"
                    msg += f"총 {len(self.app.monitor_codes)}개 종목이 감시 대상으로 등록되었습니다.\n"
                    top_5 = df_result.head(5)
                    for i, row in top_5.iterrows():
                        msg += f"- {row.get('Name','')} ({row.get('Code','')})\n"
                    
                    import threading
                    threading.Thread(target=self.app.send_telegram_message, args=(msg,), daemon=True).start()
                else:
                    self.app._display_selected_targets(df_result)
            
            # 버튼 상태 복구
            self.app.root.after(0, lambda: self.btn_select.config(state=tk.NORMAL) if self.btn_select else None)
            
        except Exception as e:
            import traceback
            self.app.log(f"❌ 종목 선정 중 예외 발생: {e}")
            self.app.log(f"DEBUG Traceback:\n{traceback.format_exc()}")
            self.app.root.after(0, lambda: self.btn_select.config(state=tk.NORMAL) if self.btn_select else None)
