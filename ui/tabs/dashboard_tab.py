import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
from typing import Any, Optional

import config

class DashboardTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self.lbl_mode_badge: Optional[tk.Label] = None
        self.btn_sync: Optional[ttk.Button] = None
        self.btn_narrative_report: Optional[ttk.Button] = None
        self.btn_daily_settlement: Optional[ttk.Button] = None
        self.btn_watchlist: Optional[ttk.Button] = None
        self.btn_server_switch: Optional[ttk.Button] = None
        self._setup_ui()

    def _call_virtual_tab(self, method_name: str) -> None:
        """포지션 관리 탭에 구현된 동작을 그대로 호출합니다."""
        try:
            vt = getattr(self.app, "virtual_tab", None)
            if vt is None:
                messagebox.showerror(
                    "오류",
                    "포지션 관리 탭이 아직 초기화되지 않았습니다.",
                    parent=self.parent,
                )
                return
            fn = getattr(vt, method_name, None)
            if not callable(fn):
                messagebox.showerror("오류", f"기능을 찾을 수 없습니다: {method_name}")
                return
            fn()
        except Exception as e:
            try:
                messagebox.showerror("오류", str(e), parent=self.parent)
            except Exception:
                pass

    def _setup_ui(self):
        """대시보드: 운영 메뉴 + 데이터 수집 타일 + 로그"""
        db_main_frame = ttk.Frame(self.parent, padding="20")
        db_main_frame.pack(fill=tk.BOTH, expand=True)

        # --- [0] 운영 · 계좌 (기존 포지션 탭 상단 버튼 이전) ---
        ops = ttk.LabelFrame(
            db_main_frame,
            text=" ⚡ 운영 · 계좌 ",
            padding=(14, 12),
        )
        ops.pack(fill=tk.X, pady=(0, 16))

        ops_top = ttk.Frame(ops)
        ops_top.pack(fill=tk.X)

        use_mock = bool(getattr(config, "USE_MOCK", False))
        mode_text = " 모의투자 " if use_mock else " 실전 "
        mode_bg = "#5E81AC" if use_mock else "#BF616A"
        self.lbl_mode_badge = tk.Label(
            ops_top,
            text=mode_text,
            font=("Malgun Gothic", 9, "bold"),
            foreground="white",
            background=mode_bg,
            padx=12,
            pady=4,
        )
        self.lbl_mode_badge.pack(side=tk.LEFT)

        ttk.Label(
            ops_top,
            text="동기화 · 종합리포트 · 일일결산 · 관심목록 · 서버(모의/실전) 전환",
            font=("Malgun Gothic", 9),
            foreground=self.app.colors.get("secondary", "#787c99"),
        ).pack(side=tk.LEFT, padx=(14, 8))

        ops_btns = ttk.Frame(ops_top)
        ops_btns.pack(side=tk.RIGHT)

        self.btn_sync = ttk.Button(
            ops_btns,
            text="동기화",
            width=11,
            command=self.app._sync_account_deposit,
        )
        self.btn_sync.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_narrative_report = ttk.Button(
            ops_btns,
            text="종합리포트",
            width=11,
            command=lambda: self._call_virtual_tab("generate_report"),
        )
        self.btn_narrative_report.pack(side=tk.LEFT, padx=4)

        self.btn_daily_settlement = ttk.Button(
            ops_btns,
            text="일일결산",
            width=11,
            command=lambda: self._call_virtual_tab("generate_daily_settlement"),
        )
        self.btn_daily_settlement.pack(side=tk.LEFT, padx=4)

        self.btn_watchlist = ttk.Button(
            ops_btns,
            text="관심목록",
            width=11,
            command=lambda: self._call_virtual_tab("_open_watchlist_dialog"),
        )
        self.btn_watchlist.pack(side=tk.LEFT, padx=4)

        self.btn_server_switch = ttk.Button(
            ops_btns,
            text="서버 전환",
            width=11,
            command=lambda: self._call_virtual_tab("_toggle_environment"),
        )
        self.btn_server_switch.pack(side=tk.LEFT, padx=(4, 0))

        # 중앙 핵심 컨트롤 센터 (Grid: 두 타일 동일 너비·동일 높이 셀)
        tiles_frame = ttk.Frame(db_main_frame)
        tiles_frame.pack(fill=tk.X, pady=(0, 18))
        tiles_frame.columnconfigure(0, weight=1, uniform="dashboard_tile")
        tiles_frame.columnconfigure(1, weight=1, uniform="dashboard_tile")
        tiles_frame.rowconfigure(0, minsize=140, weight=0)

        # [타일] 데이터 수집 (Harvest) — 종목 일일 선정은 실시간 감시 탭으로 이전됨
        harvest_tile = ttk.LabelFrame(tiles_frame, text=" 🚜 데이터 수집 ", padding="15")
        harvest_tile.grid(row=0, column=0, columnspan=2, sticky="nsew", padx=0)
        ttk.Label(
            harvest_tile,
            text="최신 시장 데이터를\n로컬 DB로 저장합니다.",
            foreground="#EBCB8B",
        ).pack(expand=True)
        self.btn_harvest = ttk.Button(harvest_tile, text="수집 시작", style="Action.TButton", command=self._on_btn_harvest)
        self.btn_harvest.pack(side=tk.BOTTOM, fill=tk.X, pady=(15,0))

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
        self.app.log_area.tag_configure("warning", foreground="#D08770") # Aurora Orange
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
