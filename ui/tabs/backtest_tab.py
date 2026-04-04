import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import os
import pandas as pd
import numpy as np
import importlib.util
import webbrowser
from datetime import datetime, timedelta
import traceback

class BacktestTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self.gui_queue = queue.Queue() # [v35.2] Thread-safe GUI update queue
        self.last_opt_df = None  # [v25.2] 최적합 파라미터 저장용
        self._init_vars()
        self._setup_ui()
        self._poll_queue()

    def _init_vars(self):
        """[v26.0] 동적 테이블 렌더링을 위한 설정 초기화 (38개 파라미터 매핑)"""
        self.backtest_entries = {}
        self.backtest_force_refresh_var = tk.BooleanVar(value=False)
        from core.defaults import DEFAULT_SETTINGS
        self.base_backtest_settings = DEFAULT_SETTINGS.copy()
        
        # [v16.50] 최적화 상태 변수를 메인 앱의 중앙 저장소와 연동
        self.opt_params_vars = self.app.opt_params_vars
        
        # [v26.0] 최적화 파라미터 상세 정보 사전 (표시명, 카테고리, 설명)
        # [주의] 종목선정 파라미터(min_trde_val, vol_ratio, vol_lookback, ma_convergence, sel_rsi_max/min 등)는
        # SimpleBacktester 기반 최적화와 무관 (DynamicBacktester에서만 유효) → 의도적으로 제외
        # 엑셀(optimization_params_audit) 확정 순서 우선 → 이후 레거시 키(미선택 시에도 툴팁용)
        self.PARAM_INFO = {
            'stage1_amt': ('S1_AmtWon', '자금관리', '1단계 정찰 금액(원)'),
            's1_equity_pct': ('S1_Equity%', '자금관리', '1단계 비중 (%)'),
            'stage2_amt': ('S2_AmtWon', '자금관리', '2단계 본대 금액(원)'),
            's2_equity_pct': ('S2_Equity%', '자금관리', '2단계 비중 (%)'),
            'type_balance_a_pct': ('Bal_A_Pct', '자금관리', '동적백테 A/B 균형 목표 A%'),
            'buy_rsi_min': ('BuyRSI_Min', 'S1 공통', 'S1 진입 RSI 하한'),
            'buy_rsi_max_a': ('A_RSI_High', 'S1 Type A', '눌림(A) RSI 상한선'),
            'gc_window': ('A_GC_Window', 'S1 Type A', '골든크로스 기간(분)'),
            'rsi_window': ('RSI_Window', 'S1 Type A', 'RSI 상향돌파 윈도(분)'),
            'ma60_min': ('MA60_Min%', 'S1 Type A', 'MA60 이격 하한(%)'),
            'ma60_max': ('MA60_Max%', 'S1 Type A', 'MA60 이격 상한(%)'),
            'vol_surge': ('A_Vol_Surge', 'S1 Type A', '거래량 급증 배수'),
            'prev_vol_lookback_s1_a': ('PrevVol_S1A', 'S1 Type A', '전일동시간 평균 일수 S1·A'),
            'type_a_phase1_up_vol_surge_add': ('A_RegUp_Vol', 'S1 Type A', '레짐 UP 거래량 +Δ'),
            'type_a_phase1_neutral_vol_surge_add': ('A_RegNeu_Vol', 'S1 Type A', '레짐 NEUTRAL 거래량 +Δ'),
            'type_a_phase1_up_ma60_shrink_pct': ('A_RegUp_M60', 'S1 Type A', '레짐 UP MA60 폭 축소'),
            'type_a_phase1_neutral_ma60_shrink_pct': ('A_RegNe_M60', 'S1 Type A', '레짐 NEUTRAL MA60 폭 축소'),
            'buy_rsi_max_b': ('B_RSI_High', 'S1 Type B', '돌파(B) RSI 상한선'),
            'vol_surge_b': ('B_Vol_Surge', 'S1 Type B', '돌파 거래량 배수'),
            'prev_vol_lookback_s1_b': ('PrevVol_S1B', 'S1 Type B', '전일동시간 평균 일수 S1·B'),
            'type_b_phase1_up_vol_surge_add': ('B_RegUp_Vol', 'S1 Type B', '레짐 UP vol +Δ(B)'),
            'type_b_phase1_neutral_vol_surge_add': ('B_RegNeu_Vol', 'S1 Type B', '레짐 NEUTRAL vol +Δ(B)'),
            's2_ma20_gap': ('S2_MA20', 'S2 Type A', '20일선 지지 갭(%)'),
            's2_ma5_gap': ('S2_MA5', 'S2 Type A', '5일선 갭(%)'),
            's2_rsi_gap': ('S2_RSI_Gap', 'S2 Type A', 'RSI 시그널 갭(%)'),
            's2_macd_gap': ('S2_MACD', 'S2 Type A', 'MACD GC 갭'),
            's2_vol_ratio': ('S2_Vol_Ratio', 'S2 Type A', '거래량 급증 배수'),
            'prev_vol_lookback_s2_a': ('PrevVol_S2A', 'S2 Type A', '전일동시간 평균 일수 S2·A'),
            's2_profit_limit': ('S2_Min_Profit', 'S2 Type A', '최소 수익률(%)'),
            's2_delay_hours': ('S2_Delay_Hrs', 'S2 Type A', '진입 후 대기(시간)'),
            's2_b_ma20_gap': ('S2B_MA20', 'S2 Type B', '20일선 지지 갭(B)%'),
            's2_b_rsi_gap': ('S2B_RSI', 'S2 Type B', 'RSI 시그널 갭(B)%'),
            's2_b_macd_gap': ('S2B_MACD', 'S2 Type B', 'MACD GC(B)'),
            's2_b_vol_ratio': ('S2B_Vol', 'S2 Type B', '거래량 급증(B) 배수'),
            'prev_vol_lookback_s2_b': ('PrevVol_S2B', 'S2 Type B', '전일동시간 평균 일수 S2·B'),
            's2_b_profit_limit': ('S2B_Profit', 'S2 Type B', '최소 수익률(B)%'),
            's2_b_delay_hours': ('S2B_Delay', 'S2 Type B', '대기(B) 시간'),
            'smart_buy_vp': ('SB_Min_VP', '전술적 수급', 'Smart Buy 최소 체결강도 (%)'),
            'smart_buy_ba_ratio': ('SB_BA_Ratio', '전술적 수급', 'Smart Buy 매도벽 비율 (배)'),
            'smart_exit_vp': ('SX_Min_VP', '전술적 수급', 'Smart Exit 체결강도 하한 (%)'),
            'smart_exit_ba_ratio': ('SX_BA_Ratio', '전술적 수급', 'Smart Exit 매수함정 비율 (배)'),
            'smart_exit_price_threshold': ('SX_Price_Thr', '전술적 수급', 'Smart Exit 가격 하락 임계 (%)'),
            'msi_down_threshold': ('MSI_Down', '시장 트렌드 필터', 'DOWN 판정 MSI 하한선'),
            'msi_up_threshold': ('MSI_Up', '시장 트렌드 필터', 'UP 판정 MSI 최소값'),
            'adx_min_threshold': ('ADX_Min', '시장 트렌드 필터', 'NEUTRAL 판정 ADX 하한선'),
            'vix_down_threshold': ('VIX_Down', '시장 트렌드 필터', 'DOWN 판정 VIX 상한선'),
            'gap_down_limit_a': ('Gap_A', 'Type A 청산', '갭하락 즉시 손절 (A) (%)'),
            'gap_grace_mins_a': ('Grace_A', 'Type A 청산', '개장 매도 유예 (A) (분)'),
            'atr_period_a': ('ATR_P_A', 'Type A 청산', 'ATR 분석 주기 (A) (봉)'),
            's1_a_take_profit': ('A_Take_Profit', 'Type A 청산', '눌림(A) 목표 익절률 (%)'),
            's1_a_stop_loss': ('A_Stop_Loss', 'Type A 청산', '눌림(A) 최대 손절률 (%)'),
            's1_a_flash_exit_pct': ('A_Flash_Exit', 'Type A 청산', '눌림(A) Flash Exit (%)'),
            'atr_multiplier_sl_a': ('A_ATR_Stop', 'Type A 청산', 'S1(A) ATR 손절 배수'),
            'atr_multiplier_trail_a': ('A_ATR_Trail', 'Type A 청산', 'S1(A) ATR 추격 익절 배수'),
            's2_take_profit': ('S2_Take_Profit', 'Type A 청산', 'S2(A) 목표 익절률 (%)'),
            's2_stop_loss': ('S2_Stop_Loss', 'Type A 청산', 'S2(A) 최대 손절률 (%)'),
            's2_flash_exit_pct': ('S2_Flash_Exit', 'Type A 청산', 'S2(A) Flash Exit (%)'),
            'atr_multiplier_sl_s2': ('S2_ATR_Stop', 'Type A 청산', 'S2(A) ATR 손절 배수'),
            'atr_multiplier_trail_s2': ('S2_ATR_Trail', 'Type A 청산', 'S2(A) ATR 추격 익절 배수'),
            'gap_down_limit_b': ('Gap_B', 'Type B 청산', '갭하락 즉시 손절 (B) (%)'),
            'gap_grace_mins_b': ('Grace_B', 'Type B 청산', '개장 매도 유예 (B) (분)'),
            'atr_period_b': ('ATR_P_B', 'Type B 청산', 'ATR 분석 주기 (B) (봉)'),
            's1_b_take_profit': ('B_Take_Profit', 'Type B 청산', '돌파(B) 목표 익절률 (%)'),
            's1_b_stop_loss': ('B_Stop_Loss', 'Type B 청산', '돌파(B) 최대 손절률 (%)'),
            's1_b_flash_exit_pct': ('B_Flash_Exit', 'Type B 청산', '돌파(B) Flash Exit (%)'),
            'atr_multiplier_sl_b': ('B_ATR_Stop', 'Type B 청산', 'S1(B) ATR 손절 배수'),
            'atr_multiplier_trail_b': ('B_ATR_Trail', 'Type B 청산', 'S1(B) ATR 추격 익절 배수'),
            's2_b_take_profit': ('S2B_Take_Profit', 'Type B 청산', 'S2(B) 목표 익절률 (%)'),
            's2_b_stop_loss': ('S2B_Stop_Loss', 'Type B 청산', 'S2(B) 최대 손절률 (%)'),
            's2_b_flash_exit_pct': ('S2B_Flash_Exit', 'Type B 청산', 'S2(B) Flash Exit (%)'),
            'atr_multiplier_sl_s2_b': ('S2B_ATR_Stop', 'Type B 청산', 'S2(B) ATR 손절 배수'),
            'atr_multiplier_trail_s2_b': ('S2B_ATR_Trail', 'Type B 청산', 'S2(B) ATR 추격 익절 배수'),
            # 레거시(엑셀 미선택 시에도 툴팁·재활성 대비)
            'use_s2_master_a': ('S2_Master_A', '2차 추매(S2)', 'Type A 추매(S2) 전체 ON/OFF'),
            'use_s2_master_b': ('S2_Master_B', '2차 추매(S2)', 'Type B 추매(S2) 전체 ON/OFF'),
            'min_trde_val': ('MinTrde', '유니버스(A)', '최소 거래대금(억)'),
            'vol_ratio': ('VolRatio', '유니버스(A)', '거래량 분출 배수'),
            'vol_lookback': ('VolLookback', '유니버스(A)', '분출 확인 기간(일)'),
            'ma_convergence': ('MA_Conv', '유니버스(A)', '이평 수렴도(%)'),
            'sel_rsi_min': ('SelRSI_Min', '유니버스(A)', '선정 RSI 하한'),
            'sel_rsi_max': ('SelRSI_Max', '유니버스(A)', '선정 RSI 상한'),
            'sel_b_rsi_min': ('SelB_RSI_Min', '유니버스(B)', '선정 RSI 하한(B)'),
            'sel_b_rsi_max': ('SelB_RSI_Max', '유니버스(B)', '선정 RSI 상한(B)'),
            'sel_b_min_trde_val': ('SelB_Trde', '유니버스(B)', '최소 거래대금(B)억'),
        }

    def _show_copyable_error(self, title, message):
        """[v4.10.10] 마우스 선택 및 복사가 가능한 커스텀 에러 다이얼로그"""
        top = tk.Toplevel(self.app.root)
        top.title(title)
        top.geometry("600x400")
        top.configure(bg="#2E3440")
        top.transient(self.app.root)
        top.grab_set()
        
        # Heading
        header = tk.Frame(top, bg="#3B4252", height=40)
        header.pack(fill=tk.X)
        tk.Label(header, text=f"⚠️ {title}", bg="#3B4252", fg="#BF616A", font=("Segoe UI", 11, "bold")).pack(pady=8)
        
        # Guide
        tk.Label(top, text="아래 에러 내용을 마우스로 드래그하여 복사(Ctrl+C)할 수 있습니다.", 
                 bg="#2E3440", fg="#D8DEE9", font=("Segoe UI", 9)).pack(pady=(10, 5))
        
        # Text area
        container = tk.Frame(top, bg="#2E3440")
        container.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        txt = tk.Text(container, wrap=tk.WORD, bg="#1E222A", fg="#ECEFF4", 
                      font=("Consolas", 10), padx=15, pady=15, relief=tk.FLAT)
        txt.insert(tk.END, message)
        txt.config(state=tk.DISABLED) # Read-only but selectable
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Close button
        btn_close = tk.Button(top, text="닫기", command=top.destroy, bg="#4C566A", fg="white", 
                             font=("Segoe UI", 10, "bold"), width=15, relief=tk.FLAT, cursor="hand2")
        btn_close.pack(pady=15)

    def _poll_queue(self):
        """[v35.2] GUI 큐 모니터링 및 업데이트 실행 (Thread-safe)"""
        try:
            while not self.gui_queue.empty():
                task = self.gui_queue.get_nowait()
                if callable(task):
                    task()
        except queue.Empty:
            pass
        finally:
            self.parent.after(100, self._poll_queue)

    def _setup_ui(self):
        """백테스팅 탭 레이아웃 구성 (v16.8: 직관적인 그룹화 레이아웃 적용)"""
        # ---상단: 가로 배치 섹션 (설정 / 최적화 / 백테스트)---
        top_grid_frame = ttk.Frame(self.parent)
        top_grid_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=5)
        
        # 1. 기간 설정 (Date Settings)
        date_lf = ttk.LabelFrame(top_grid_frame, text=" 📅 1. 기본 기간 설정 ", padding="15")
        date_lf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        
        ttk.Label(date_lf, text="시작:", foreground="#EBCB8B").grid(row=0, column=0, sticky=tk.W)
        self.backtest_entries['start_date'] = ttk.Entry(date_lf, width=12)
        self.backtest_entries['start_date'].insert(0, self.app.settings.get('bt_start_date', "2025-03-04"))
        self.backtest_entries['start_date'].grid(row=0, column=1, padx=(5, 2), pady=2)
        ttk.Button(date_lf, text="📅", width=3, 
                   command=lambda: self._show_picker('start_date')).grid(row=0, column=2, padx=(0, 5))

        ttk.Label(date_lf, text="종료:", foreground="#EBCB8B").grid(row=1, column=0, sticky=tk.W)
        self.backtest_entries['end_date'] = ttk.Entry(date_lf, width=12)
        self.backtest_entries['end_date'].insert(0, self.app.settings.get('bt_end_date', yesterday))
        self.backtest_entries['end_date'].grid(row=1, column=1, padx=(5, 2), pady=2)
        ttk.Button(date_lf, text="📅", width=3, 
                   command=lambda: self._show_picker('end_date')).grid(row=1, column=2, padx=(0, 5))

        # 2. 전략 최적화 (Optimization)
        opt_lf = ttk.LabelFrame(top_grid_frame, text=" 🚀 2. 전략 최적화 ", padding="15")
        opt_lf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        
        btn_row_opt = ttk.Frame(opt_lf)
        btn_row_opt.pack(expand=True)
        self.btn_start_optimize = ttk.Button(btn_row_opt, text="최적화 시작", width=15, command=self._on_btn_start_optimize)
        self.btn_start_optimize.pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_row_opt, text="📊 최적화결과 리포트", width=18, command=self._open_opt_trade_report).pack(side=tk.LEFT, padx=2)

        # [Perf] 최적화 옵션 행 (trials 수 + 고정밀 토글)
        opt_cfg_row = ttk.Frame(opt_lf)
        opt_cfg_row.pack(pady=(6, 0))
        ttk.Label(opt_cfg_row, text="Trials:", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 2))
        self.opt_n_trials_var = tk.StringVar(value="50")
        ttk.Spinbox(opt_cfg_row, from_=10, to=300, increment=10, textvariable=self.opt_n_trials_var,
                    width=5, font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 10))
        self.opt_high_precision_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_cfg_row, text="고정밀(1m)", variable=self.opt_high_precision_var,
                        style="TCheckbutton").pack(side=tk.LEFT)

        # [v25.0] 최적화 대상 종목 안내 (2025 대표주 10선)
        target_info = "🔍 대상: 삼성전자, SK하이닉스 등 10개 대표주"
        lbl_targets = ttk.Label(opt_lf, text=target_info, font=("Segoe UI", 8), foreground="#EBCB8B", cursor="hand2")
        lbl_targets.pack(pady=(5, 0))
        
        # ToolTip helper
        from ui.gui_utils import ToolTip
        full_stock_list = "최적화 대상 (2025 대표 10선):\n" + \
                         "삼성전자, SK하이닉스, 현대차, 신한지주, LG화학, " + \
                         "한화에어로스페이스, NAVER, 한국전력, 삼성물산, 삼성전기"
        ToolTip(lbl_targets, lambda e: full_stock_list)


        # 3. 성과 검증 (Portfolio Backtest)
        bt_lf = ttk.LabelFrame(top_grid_frame, text=" 📈 3. 성과 검증 ", padding="15")
        bt_lf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        
        btn_row_bt = ttk.Frame(bt_lf)
        btn_row_bt.pack(pady=5)
        self.btn_start_dynamic = ttk.Button(btn_row_bt, text="백테스트 시작", width=15, command=self._on_btn_start_dynamic_backtest)
        self.btn_start_dynamic.pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_row_bt, text="📊 백테스트결과 리포트", width=18, command=self._open_dynamic_trade_report).pack(side=tk.LEFT, padx=2)
        
        # [v31.4] Portfolio Simulation (High Precision & Synthetic Indicators Enabled by Default)
        lbl_info = ttk.Label(bt_lf, text="✨ 1분봉 고정밀 및 가상 실시간 지표 상시 적용", 
                             font=("Segoe UI", 8), foreground="#EBCB8B")
        lbl_info.pack(pady=(5, 0))

        # ---중단: 진행 상태 (Full-Width Notification Bar)---
        status_lf = ttk.LabelFrame(self.parent, text=" 🔔 실시간 진행 상태 ", padding="10")
        status_lf.pack(side=tk.TOP, fill=tk.X, padx=15, pady=5)
        self.lbl_bt_status = ttk.Label(status_lf, text="시스템 대기 중...", font=("Segoe UI", 10, "bold"), 
                                         foreground=self.app.colors['primary'], anchor=tk.CENTER)
        self.lbl_bt_status.pack(fill=tk.X, expand=True)



        # 하단: 결과 테이블 영역
        res_frame = ttk.LabelFrame(self.parent, text=" 💎 최적화 결과 랭킹 (수익률 순) ")
        res_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=5)

        # [v18.0] 테이블 어셈블리 (중앙 정렬을 위한 컨테이너)
        self.table_assembly = ttk.Frame(res_frame)
        self.table_assembly.grid_columnconfigure(0, weight=1)
        self.table_assembly.grid_rowconfigure(0, weight=1)
        self.table_assembly.pack(fill=tk.BOTH, expand=True, pady=10) # [v33.9] Maximize Available Space
        
        # [v25.2] 선택 조합 백테스트 버튼 (누락된 버튼 복구)
        btn_apply_opt = ttk.Button(res_frame, text="🛠️ 선택한 조합으로 포트폴리오 백테스트 시작", 
                                  command=self._on_btn_backtest_with_selected)
        btn_apply_opt.pack(pady=5)

        # --- [v33.9] Unified Horizontal Scrolling Container ---
        self.canvas_bt = tk.Canvas(self.table_assembly, bd=0, highlightthickness=0, bg="#2E3440")
        h_scroll = ttk.Scrollbar(self.table_assembly, orient=tk.HORIZONTAL, command=self.canvas_bt.xview)
        
        self.canvas_bt.configure(xscrollcommand=h_scroll.set)
        
        # Inner Frame to hold everything
        self.scroll_frame_bt = tk.Frame(self.canvas_bt, bg="#2E3440")
        self.canvas_window = self.canvas_bt.create_window((0, 0), window=self.scroll_frame_bt, anchor="nw")
        
        # Structure the container with grid
        self.canvas_bt.grid(row=0, column=0, sticky="nsew")
        h_scroll.grid(row=1, column=0, sticky="ew")

        # Now create children with scroll_frame_bt as parent
        # [v16.15] 상단 카테고리 프레임 제거 (UI 정예화를 위해 삭제)
        pass

        # 대청소: 하드코딩된 트리뷰 셋업과 각종 헤더/툴팁 맵핑 완전히 삭제
        # 최소한의 기본 결과용 뼈대만 남기고, 모든 옵션 컬럼은 결과값 기반 렌더링에서 동적 생성할 예정입니다.
        cols = ["rank", "avg_ret", "avg_mdd", "win_rate", "trades"]
        self.tree_backtest = ttk.Treeview(self.scroll_frame_bt, columns=cols, show="headings", height=15)
        
        headers = {
            "rank": "순위", "avg_ret": "수익률(%)", "avg_mdd": "MDD", "win_rate": "승률(%)", "trades": "매매수"
        }
        for col in cols:
            self.tree_backtest.heading(col, text=headers.get(col, col))
            self.tree_backtest.column(col, width=65, anchor=tk.CENTER, stretch=False)
        
        self.tree_backtest.bind("<Double-1>", lambda e: self._on_btn_backtest_with_selected())
        
        v_scroll = ttk.Scrollbar(self.table_assembly, orient=tk.VERTICAL, command=self.tree_backtest.yview)
        v_scroll.grid(row=0, column=1, sticky="ns")
        self.tree_backtest.configure(yscrollcommand=v_scroll.set)
        
        self.tree_backtest.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        def _update_scroll_region(e):
            self.canvas_bt.configure(scrollregion=self.canvas_bt.bbox("all"))

        self.scroll_frame_bt.bind("<Configure>", _update_scroll_region)

        style = ttk.Style()
        style.configure("Treeview", rowheight=25)
        # 다크 테마 기반 행 색상: 홀수/짝수 배경 + 텍스트 대비 확보
        self.tree_backtest.tag_configure('odd',  background='#2E3440', foreground='#D8DEE9')
        self.tree_backtest.tag_configure('even', background='#3B4252', foreground='#D8DEE9')
        # 수익/손실 행은 foreground만 덮어씀 (배경은 odd/even 태그에서 결정)
        self.tree_backtest.tag_configure('plus',  foreground='#A3BE8C')
        self.tree_backtest.tag_configure('minus', foreground='#BF616A')
        
        # [v26.0] 지능형 툴팁 장착 (카테고리 정보 포함)
        from ui.gui_utils import ToolTip
        def get_header_tip(event):
            region = self.tree_backtest.identify_region(event.x, event.y)
            if region == "heading":
                col_id = self.tree_backtest.identify_column(event.x)
                try:
                    col_index = int(col_id.replace("#", "")) - 1
                    cols = self.tree_backtest["columns"]
                    col_name = cols[col_index]
                    
                    if col_name in self.PARAM_INFO:
                        alias, category, desc = self.PARAM_INFO[col_name]
                        # [v16.90] 최적화 탐색 범위 정보 추가
                        space = self._get_dynamic_search_space(self.app.settings)
                        range_info = f"\n🔍 최적화 범위: {space[col_name]}" if col_name in space else ""
                        return f"📂 {category}\n✨ {desc}{range_info}\n(Key: {col_name})"
                    return None
                except: return None
            return None
        ToolTip(self.tree_backtest, get_header_tip)
        
    def _get_dynamic_search_space(self, settings):
        """[v15.7] 현재 설정값을 기준으로 주변 탐색 범위를 동적으로 생성"""
        s = settings
        def range_for(val, step, count=3):
            if isinstance(val, int):
                return [int(val + (i - count//2) * step) for i in range(count)]
            else:
                return [round(float(val) + (i - count//2) * step, 2) for i in range(count)]

        return {
            'stage1_amt': range_for(float(s.get('stage1_amt', 20_000_000)), 5_000_000),
            'stage2_amt': range_for(float(s.get('stage2_amt', 30_000_000)), 5_000_000),
            's1_equity_pct': range_for(s.get('s1_equity_pct', 10.0), 5.0),
            's2_equity_pct': range_for(s.get('s2_equity_pct', 20.0), 10.0),
            'type_balance_a_pct': range_for(s.get('type_balance_a_pct', 50.0), 10.0),
            'buy_rsi_min': range_for(s.get('buy_rsi_min', 40.0), 5.0),
            'buy_rsi_max_b': range_for(s.get('buy_rsi_max_b', 65.0), 5.0),
            'buy_rsi_max_a': range_for(s.get('buy_rsi_max_a', 58.0), 5.0),
            'ma60_min': range_for(s.get('ma60_min', 100.0), 2.0),
            'ma60_max': range_for(s.get('ma60_max', 110.0), 2.0),
            'vol_surge': range_for(s.get('vol_surge', 1.5), 0.3),
            'vol_surge_b': range_for(s.get('vol_surge_b', 1.5), 0.3),
            'prev_vol_lookback_s1_a': range_for(int(float(s.get('prev_vol_lookback_s1_a', s.get('prev_vol_lookback', 10)))), 2),
            'prev_vol_lookback_s1_b': range_for(int(float(s.get('prev_vol_lookback_s1_b', s.get('prev_vol_lookback_b', 10)))), 2),
            'prev_vol_lookback_s2_a': range_for(int(float(s.get('prev_vol_lookback_s2_a', s.get('prev_vol_lookback', 10)))), 2),
            'prev_vol_lookback_s2_b': range_for(int(float(s.get('prev_vol_lookback_s2_b', s.get('prev_vol_lookback_b', 10)))), 2),
            'type_a_phase1_up_vol_surge_add': range_for(s.get('type_a_phase1_up_vol_surge_add', 0.2), 0.1),
            'type_a_phase1_neutral_vol_surge_add': range_for(s.get('type_a_phase1_neutral_vol_surge_add', 0.45), 0.15),
            'type_a_phase1_up_ma60_shrink_pct': range_for(s.get('type_a_phase1_up_ma60_shrink_pct', 1.0), 0.5),
            'type_a_phase1_neutral_ma60_shrink_pct': range_for(s.get('type_a_phase1_neutral_ma60_shrink_pct', 2.0), 0.5),
            'type_b_phase1_up_vol_surge_add': range_for(s.get('type_b_phase1_up_vol_surge_add', 0.2), 0.1),
            'type_b_phase1_neutral_vol_surge_add': range_for(s.get('type_b_phase1_neutral_vol_surge_add', 0.45), 0.15),
            's2_ma20_gap': range_for(s.get('s2_ma20_gap', 0.0), 0.5),
            's2_ma5_gap': range_for(s.get('s2_ma5_gap', -0.5), 0.25),
            's2_rsi_gap': range_for(s.get('s2_rsi_gap', 0.0), 0.5),
            's2_macd_gap': range_for(s.get('s2_macd_gap', 0.0), 0.5),
            's2_b_ma20_gap': range_for(s.get('s2_b_ma20_gap', 0.0), 0.5),
            's2_b_ma5_gap': range_for(s.get('s2_b_ma5_gap', -0.5), 0.25),
            's2_b_rsi_gap': range_for(s.get('s2_b_rsi_gap', 0.0), 0.5),
            's2_b_macd_gap': range_for(s.get('s2_b_macd_gap', 0.0), 0.5),
            's2_b_vol_ratio': range_for(s.get('s2_b_vol_ratio', 1.5), 0.2),
            's2_b_profit_limit': range_for(s.get('s2_b_profit_limit', 1.0), 0.5),
            's2_b_delay_hours': range_for(s.get('s2_b_delay_hours', 24.0), 6.0),
            's1_a_take_profit': range_for(s.get('s1_a_take_profit', 5.0), 2.0),
            's1_a_stop_loss': range_for(s.get('s1_a_stop_loss', 2.0), 1.0),
            's1_b_take_profit': range_for(s.get('s1_b_take_profit', 4.0), 2.0),
            's1_b_stop_loss': range_for(s.get('s1_b_stop_loss', 2.0), 1.0),
            's2_take_profit': range_for(s.get('s2_take_profit', 5.0), 2.0),
            's2_stop_loss': range_for(s.get('s2_stop_loss', 2.0), 0.5),
            's2_vol_ratio': range_for(s.get('s2_vol_ratio', 1.0), 0.2),
            's2_profit_limit': range_for(s.get('s2_profit_limit', 1.5), 0.5),
            's2_delay_hours': range_for(s.get('s2_delay_hours', 2.0), 1.0),
            'use_s2_master_a': [True, False],
            'use_s2_master_b': [True, False],
            'gc_window': range_for(s.get('gc_window', 90), 10),
            'rsi_window': range_for(s.get('rsi_window', 45), 15),
            'atr_period_a': range_for(s.get('atr_period_a', s.get('atr_period', 14)), 4),
            'atr_period_b': range_for(s.get('atr_period_b', s.get('atr_period', 14)), 4),
            'atr_multiplier_sl_a': range_for(s.get('atr_multiplier_sl_a', 2.5), 0.5),
            'atr_multiplier_trail_a': range_for(s.get('atr_multiplier_trail_a', 3.0), 0.5),
            'atr_multiplier_sl_b': range_for(s.get('atr_multiplier_sl_b', 2.5), 0.5),
            'atr_multiplier_trail_b': range_for(s.get('atr_multiplier_trail_b', 3.0), 0.5),
            'atr_multiplier_sl_s2': range_for(s.get('atr_multiplier_sl_s2', 2.5), 0.5),
            'atr_multiplier_trail_s2': range_for(s.get('atr_multiplier_trail_s2', 3.0), 0.5),
            'atr_multiplier_sl_s2_b': range_for(s.get('atr_multiplier_sl_s2_b', 2.5), 0.5),
            'atr_multiplier_trail_s2_b': range_for(s.get('atr_multiplier_trail_s2_b', 3.0), 0.5),
            'smart_buy_vp': range_for(s.get('smart_buy_vp', 120.0), 10.0),
            'smart_buy_ba_ratio': range_for(s.get('smart_buy_ba_ratio', 1.5), 0.3),
            'smart_exit_vp': range_for(s.get('smart_exit_vp', 100.0), 10.0),
            'smart_exit_ba_ratio': range_for(s.get('smart_exit_ba_ratio', 1.5), 0.3),
            'smart_exit_price_threshold': range_for(s.get('smart_exit_price_threshold', -2.0), 1.0),
            'gap_down_limit_a': range_for(s.get('gap_down_limit_a', s.get('gap_down_limit', -3.0)), 1.0),
            'gap_down_limit_b': range_for(s.get('gap_down_limit_b', s.get('gap_down_limit', -3.0)), 1.0),
            'gap_grace_mins_a': range_for(s.get('gap_grace_mins_a', s.get('gap_grace_mins', 15)), 5),
            'gap_grace_mins_b': range_for(s.get('gap_grace_mins_b', s.get('gap_grace_mins', 15)), 5),
            's1_a_flash_exit_pct': range_for(s.get('s1_a_flash_exit_pct', -5.0), 1.0),
            's1_b_flash_exit_pct': range_for(s.get('s1_b_flash_exit_pct', -5.0), 1.0),
            's2_flash_exit_pct': range_for(s.get('s2_flash_exit_pct', -5.0), 1.0),
            'min_trde_val': range_for(s.get('min_trde_val', 10), 5),
            'vol_ratio': range_for(s.get('vol_ratio', 1.0), 0.5),
            'vol_lookback': range_for(s.get('vol_lookback', 20), 5),
            'ma_convergence': range_for(s.get('ma_convergence', 5.0), 1.0),
            'sel_rsi_min': range_for(s.get('sel_rsi_min', 35.0), 5.0),
            'sel_rsi_max': range_for(s.get('sel_rsi_max', 70.0), 5.0),
            # Track B 종목선정 탐색 범위
            'sel_b_rsi_min': range_for(s.get('sel_b_rsi_min', 45.0), 5.0),
            'sel_b_rsi_max': range_for(s.get('sel_b_rsi_max', 75.0), 5.0),
            'sel_b_min_trde_val': range_for(s.get('sel_b_min_trde_val', 60.0), 10),
            # [v4.6] 시장 트렌드 필터 임계값 탐색 범위
            # msi_down: 역배열 강도 (음수 깊을수록 엄격), step=0.05
            'msi_down_threshold': range_for(s.get('msi_down_threshold', -0.10), 0.05),
            # msi_up: 정배열 최소 기준 (0.05~0.20 탐색), step=0.05
            'msi_up_threshold':   range_for(s.get('msi_up_threshold', 0.10), 0.05),
            # adx_min: 추세 강도 하한 (10~20 탐색), step=5, int
            'adx_min_threshold':  [10, 15, 20],
            # vix_down: 공포지수 임계 (25~35 탐색), step=5, int
            'vix_down_threshold': [25, 30, 35],
        }

    def _refresh_columns_from_settings(self, re_render=True):
        """[v26.1] 설정 탭의 파라미터 선택 상태를 즉시 트리뷰 컬럼에 반영 (선택된 항목만!)"""
        try:
            if not hasattr(self, 'tree_backtest'): return
            
            # 1. 기존 데이터 초기화 (기둥 구조 변경 시 데이터 정합성을 위해 일괄 삭제)
            for item in self.tree_backtest.get_children():
                self.tree_backtest.delete(item)
            
            # 2. 기본 결과 컬럼
            base_cols = ["rank", "avg_ret", "avg_mdd", "win_rate", "trades"]
            
            # 3. 현재 선택된 최적화 대상 파라미터 감지 (순서 보장)
            active_params = []
            for k in self.PARAM_INFO.keys():
                if k in self.opt_params_vars and self.opt_params_vars[k].get():
                    active_params.append(k)
            
            # 4. 트리뷰 컬럼 명단 갱신 (선택되지 않은 파라미터는 리스트에서 제외됨)
            new_cols = base_cols + active_params
            
            # [Core Fix] displaycolumns를 먼저 비워서 이전 기둥을 완전히 걷어냄
            self.tree_backtest["displaycolumns"] = [] 
            self.tree_backtest["columns"] = new_cols
            self.tree_backtest["displaycolumns"] = new_cols
            
            # 5. 헤더 및 너비 설정
            for col in new_cols:
                if col in base_cols:
                    header_text = {"rank": "순위", "avg_ret": "수익률(%)", "avg_mdd": "MDD", "win_rate": "승률(%)", "trades": "매매수"}[col]
                    width = 75
                else:
                    alias, category, desc = self.PARAM_INFO[col]
                    header_text = alias
                    width = 110
                
                self.tree_backtest.heading(col, text=header_text)
                self.tree_backtest.column(col, width=width, anchor=tk.CENTER, stretch=False)

            # 6. 스크롤 영역 갱신 (테이블 너비 확장)
            if hasattr(self, 'canvas_bt'):
                self.canvas_bt.itemconfig(self.canvas_window, width=5000)
                self.parent.update_idletasks()
                req_w = self.scroll_frame_bt.winfo_reqwidth()
                can_w = self.canvas_bt.winfo_width()
                self.canvas_bt.itemconfig(self.canvas_window, width=max(can_w, req_w))
                self.canvas_bt.configure(scrollregion=self.canvas_bt.bbox("all"))

            # 7. 데이터 재랜더링 (탭 전환 시 기존 결과 유지)
            if re_render and self.last_opt_df is not None:
                self._render_optimization_results(self.last_opt_df, refresh_cols=False)

        except Exception as _err:
            print(f"[ERROR] Column Refresh Fail: {_err}")
            pass

    def _on_btn_start_optimize(self):
        """최적화 버튼 클릭 핸들러"""
        if self.app.is_monitoring:
            messagebox.showwarning("경고", "실시간 감시가 중단된 상태에서만 백테스팅이 가능합니다.")
            return
            
        self.btn_start_optimize.config(state=tk.DISABLED)
        self.lbl_bt_status.config(text="최적화 중... (수 분 소요될 수 있음)", foreground="#BF616A")
        
        if hasattr(self.app, 'settings_tab'):
            try:
                self.app.settings_tab.save_settings(sync_to_backtest=False)
                self.app.log("ℹ️ 백테스트 시작 전 설정 탭의 값을 자동으로 동기화했습니다.")
            except Exception as _err:
                print(f"[DEBUG] Settings sync error: {_err}")
        
        try:
            raw_start = self.backtest_entries['start_date'].get().strip()
            raw_end = self.backtest_entries['end_date'].get().strip()
            start_date = pd.to_datetime(raw_start).strftime('%Y-%m-%d')
            end_date = pd.to_datetime(raw_end).strftime('%Y-%m-%d')
            
            self.app.settings['bt_start_date'] = start_date
            self.app.settings['bt_end_date'] = end_date
            self.app._final_save_settings()
            
            s = self.app.settings
            full_search_space = self._get_dynamic_search_space(s)
            search_space = {k: v for k, v in full_search_space.items() if self.opt_params_vars.get(k, tk.BooleanVar(value=False)).get()}
            
            if not search_space:
                messagebox.showwarning("주의", "최소 하나 이상의 파라미터를 선택해야 합니다.")
                self.btn_start_optimize.config(state=tk.NORMAL)
                return
            
            # [Perf] 최적화는 기본적으로 15분봉만 사용 (고정밀 모드는 최종 백테스트에서만 활성화)
            high_precision = getattr(self, 'opt_high_precision_var', None)
            high_precision = high_precision.get() if high_precision else False
            n_trials = getattr(self, 'opt_n_trials_var', None)
            try:
                n_trials = int(n_trials.get()) if n_trials else 50
            except Exception:
                n_trials = 50
            threading.Thread(target=self._run_optimize_thread, args=(start_date, end_date, search_space, high_precision, n_trials), daemon=True).start()
        except Exception as _err:
            self._show_copyable_error("입력 오류", f"입력 형식을 확인해 주세요:\n{str(_err)}\n\n{traceback.format_exc()}")
            self.btn_start_optimize.config(state=tk.NORMAL)

    def _render_optimization_results(self, df, refresh_cols=True):
        """[v26.1] 최적화 결과 랭킹 렌더링 (결과 데이터 삽입)"""
        if df is None or df.empty: return
        
        # 0. 데이터 캐싱
        self.last_opt_df = df.head(20)

        # 1. 화면의 트리뷰 데이터 초기화 (구조는 유지)
        if refresh_cols:
            # 컬럼 구조 강제 갱신과 초기화 동시 수행
            self._refresh_columns_from_settings(re_render=False)
        else:
            for item in self.tree_backtest.get_children():
                self.tree_backtest.delete(item)

        # (삭제 항목: 기존의 중복된 _refresh_columns_from_settings 호출 제거)
        
        # 3. 현재 트리뷰에 설정된 컬럼 순서대로 데이터 추출 및 삽입
        current_cols = self.tree_backtest["columns"]
        
        for i, row in enumerate(df.head(20).itertuples(), 1):
            res_dict = row._asdict()
            values = []
            for col in current_cols:
                val = res_dict.get(col, 0)
                if col == "rank": val = i
                elif col == "trades": val = int(val)
                elif isinstance(val, (float, np.float64, np.float32)): val = f"{val:+.2f}"
                values.append(val)
            
            row_tag = 'even' if i % 2 == 0 else 'odd'
            ret_val = float(res_dict.get('avg_ret', 0))
            perf_tag = 'plus' if ret_val > 0 else 'minus'
            
            self.tree_backtest.insert("", tk.END, values=values, tags=(perf_tag, row_tag))

        # 6. 지오메트리 잠금 해제 (가로 스크롤 영역 갱신)
        # 캔버스 너비를 임시로 크게 벌려 내부 프레임이 제약 없이 펴지게 유도
        self.canvas_bt.itemconfig(self.canvas_window, width=5000)
        self.parent.update_idletasks()
        
        req_w = self.scroll_frame_bt.winfo_reqwidth()
        can_w = self.canvas_bt.winfo_width()
        # 최종적으로 실제 필요한 너비와 캔버스 창 너비 중 큰 값으로 고정
        self.canvas_bt.itemconfig(self.canvas_window, width=max(can_w, req_w))
        self.canvas_bt.configure(scrollregion=self.canvas_bt.bbox("all"))

    def _run_optimize_thread(self, start, end, space, high_precision=False, n_trials=50):
        try:
            import parameter_optimizer as opt_mod
            # [v31.0] Derive optimization targets from KODEX 200 universe
            from backtest_engine import TradingStrategy
            name_map = TradingStrategy.get_stock_name_map()
            opt_codes = list(name_map.keys())[:10] if name_map else ['005930', '000660', '005380', '055550', '051910', '012450', '035420', '015760', '028260', '009150']
            
            optimizer = opt_mod.ParameterOptimizer(opt_codes, space, self.app.settings)
            
            def update_progress(msg, value):
                self.gui_queue.put(lambda: self.lbl_bt_status.config(text=msg))
                if hasattr(self.app, 'progress_var'):
                    self.gui_queue.put(lambda: self.app.progress_var.set(value))

            self.gui_queue.put(lambda: self.lbl_bt_status.config(text="최적화 시작...", foreground="orange"))
            df_results = optimizer.find_best_params(start, end, interval='15m', callback=update_progress, high_precision=high_precision, method='bayesian', n_trials=n_trials)
            
            if df_results is not None and not df_results.empty:
                self.gui_queue.put(lambda: self._render_optimization_results(df_results))
            else:
                self.gui_queue.put(lambda: messagebox.showwarning("결과 없음", "매매 신호가 발생하지 않았습니다."))
        except Exception as _err:
            # [v4.10.5] Absolute isolation of error message
            _msg = str(_err)
            self.gui_queue.put(lambda error_msg=_msg: messagebox.showerror("최적화 오류", error_msg))
        finally:
            self.gui_queue.put(lambda: self.btn_start_optimize.config(state=tk.NORMAL))
            self.gui_queue.put(lambda: self.lbl_bt_status.config(text="최적화 완료!", foreground="#A3BE8C"))

    def _open_signal_report(self):
        path = os.path.abspath("signal_report.html")
        if os.path.exists(path): webbrowser.open(f"file://{path}")

    def _open_opt_trade_report(self):
        path = os.path.abspath("trade_report.html")
        if os.path.exists(path): webbrowser.open(f"file://{path}")

    def _open_dynamic_trade_report(self):
        path = os.path.abspath("dynamic_trade_report.html")
        if os.path.exists(path): webbrowser.open(f"file://{path}")

    def _on_btn_backtest_with_selected(self):
        selected = self.tree_backtest.selection()
        if not selected:
            messagebox.showwarning("알림", "백테스트에 적용할 조합을 선택해 주세요.")
            return
        item = self.tree_backtest.item(selected[0])
        rank = item['values'][0]
        if self.last_opt_df is not None:
            try:
                params = self.last_opt_df.iloc[int(rank)-1].to_dict()
                self._show_param_edit_popup(params)
            except Exception as _err:
                messagebox.showerror("오류", str(_err))

    def _show_param_edit_popup(self, params):
        popup = tk.Toplevel(self.app.root)
        popup.title("⚙️ 백테스트 반영 파라미터 확인")
        w, h = 580, 850
        self.app.root.update_idletasks()
        pos_x = self.app.root.winfo_x() + (self.app.root.winfo_width() // 2) - (w // 2)
        pos_y = self.app.root.winfo_y() + (self.app.root.winfo_height() // 2) - (h // 2)
        popup.geometry(f"{w}x{h}+{pos_x}+{pos_y}")
        popup.configure(bg="#2E3440")
        popup.transient(self.app.root)
        popup.grab_set()

        header_frame = tk.Frame(popup, bg="#3B4252", height=50)
        header_frame.pack(fill=tk.X)
        info_text = f"🏆 Rank #{params.get('rank', '-')} | Ret: {params.get('avg_ret', 0):+.2f}% | MDD: {params.get('avg_mdd', 0):.2f}%"
        ttk.Label(header_frame, text=info_text, font=("Malgun Gothic", 10, "bold"), foreground="#81A1C1", background="#3B4252").pack(pady=10)

        entries = {}
        footer = tk.Frame(popup, bg="#2E3440", pady=15)
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        
        def run_confirmed():
            new_params = {}
            for k, _ent in entries.items():
                if isinstance(_ent, tk.BooleanVar): new_params[k] = _ent.get()
                else: 
                    try: 
                        _val_str = _ent.get()
                        new_params[k] = float(_val_str) if '.' in _val_str else int(_val_str)
                    except: pass
            popup.destroy()
            self._on_btn_start_dynamic_backtest(override_params=new_params)

        tk.Button(footer, text="🚀 이 설정으로 백테스트 시작", width=40, height=2, bg="#A3BE8C", fg="#2E3440", font=("Malgun Gothic", 10, "bold"), relief=tk.FLAT, command=run_confirmed).pack()

        container = tk.Frame(popup, bg="#2E3440")
        container.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(container, bg="#2E3440", highlightthickness=0)
        scroll = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        main = tk.Frame(canvas, bg="#2E3440", padx=15, pady=10)
        main.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0,0), window=main, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        for k, v in params.items():
            if k in ['rank', 'avg_ret', 'avg_mdd', 'avg_win', 'total_trades']: continue
            row = tk.Frame(main, bg="#2E3440", pady=2)
            row.pack(fill=tk.X)
            tk.Label(row, text=k, bg="#2E3440", fg="#E5E9F0", width=20, anchor="w").pack(side=tk.LEFT)
            ent = tk.Entry(row, width=10, bg="#3B4252", fg="#ECEFF4", relief=tk.FLAT)
            ent.insert(0, str(v))
            ent.pack(side=tk.LEFT, padx=5)
            entries[k] = ent

    def _on_btn_start_dynamic_backtest(self, override_params=None):
        if self.app.is_monitoring:
            messagebox.showwarning("경고", "모니터링 중에는 백테스트가 불가합니다.")
            return
        try:
            raw_start = self.backtest_entries['start_date'].get().strip()
            raw_end = self.backtest_entries['end_date'].get().strip()
            start = pd.to_datetime(raw_start).strftime('%Y-%m-%d')
            end = pd.to_datetime(raw_end).strftime('%Y-%m-%d')
            
            # [v4.10.8] Persist dates for future sessions
            self.app.settings['bt_start_date'] = start
            self.app.settings['bt_end_date'] = end
            self.app._final_save_settings()
            
            self.btn_start_dynamic.config(state=tk.DISABLED)
            self.lbl_bt_status.config(text="백테스트 실행 중...", foreground="orange")
            threading.Thread(target=self._run_dynamic_backtest_thread, args=(start, end, override_params), daemon=True).start()
        except Exception as _err: 
            self._show_copyable_error("오류", f"날짜 형식을 확인하세요:\n{str(_err)}\n\n{traceback.format_exc()}")

    def _run_dynamic_backtest_thread(self, start, end, override_params=None):
        try:
            import dynamic_backtest as db
            def update(msg, val=None):
                self.gui_queue.put(lambda text=msg: self.lbl_bt_status.config(text=text))
            active = self.app.settings.copy()
            if override_params: active.update(override_params)
            tester = db.DynamicBacktester(active)
            trades = tester.run(start, end, interval='15m', callback=update, high_precision=True)
            if trades is not None and not trades.empty:
                self.gui_queue.put(lambda: self.lbl_bt_status.config(text="백테스트 완료!", foreground="#A3BE8C"))
                report = os.path.abspath("dynamic_trade_report.html")
                if os.path.exists(report): webbrowser.open(f"file://{report}")
        except Exception as _err:
            # [v4.10.5] Absolute closure isolation
            _msg_str = str(_err)
            self.gui_queue.put(lambda error_msg=_msg_str: messagebox.showerror("오류", error_msg))
        finally:
            self.gui_queue.put(lambda: self.btn_start_dynamic.config(state=tk.NORMAL))

    def _show_picker(self, key):
        from ui.gui_utils import CustomDatePicker
        entry = self.backtest_entries.get(key)
        if entry: CustomDatePicker(self.parent, entry, initial_date=entry.get(), colors=self.app.colors)
