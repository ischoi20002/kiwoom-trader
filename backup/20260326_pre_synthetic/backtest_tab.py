import tkinter as tk
from tkinter import ttk, messagebox
import threading
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
        self.last_opt_df = None  # [v25.2] 최적합 파라미터 저장용
        self._init_vars()
        self._setup_ui()

    def _init_vars(self):
        """[v25.0] 최적화 대상 파라미터 선택 변수 초기화"""
        self.opt_params_vars = {
            'buy_rsi_min': tk.BooleanVar(value=True),
            'buy_rsi_max_a': tk.BooleanVar(value=True),
            'buy_rsi_max_b': tk.BooleanVar(value=True),
            'vol_surge': tk.BooleanVar(value=True),
            'vol_surge_b': tk.BooleanVar(value=True),
            'gc_window': tk.BooleanVar(value=True),
            'rsi_window': tk.BooleanVar(value=True),
            's1_a_take_profit': tk.BooleanVar(value=True),
            's1_a_stop_loss': tk.BooleanVar(value=True),
            's1_b_take_profit': tk.BooleanVar(value=True),
            's1_b_stop_loss': tk.BooleanVar(value=True),
            's2_take_profit': tk.BooleanVar(value=True),
            's2_stop_loss': tk.BooleanVar(value=True),
            's2_vol_ratio': tk.BooleanVar(value=True),
            'atr_period': tk.BooleanVar(value=True),
            'atr_multiplier_sl': tk.BooleanVar(value=True),
            'atr_multiplier_trail': tk.BooleanVar(value=True),
            's1_equity_pct': tk.BooleanVar(value=False),
            's2_equity_pct': tk.BooleanVar(value=False)
        }
        self.opt_high_precision_var = tk.BooleanVar(value=False)
        self.col_to_params = {
            "vol_a": ["vol_surge"], "vol_b": ["vol_surge_b"],
            "rsi_a": ["buy_rsi_max_a"], "rsi_b": ["buy_rsi_max_b"], "rsi_min": ["buy_rsi_min"],
            "gc_w": ["gc_window"], "rsi_w": ["rsi_window"],
            "tp_a": ["s1_a_take_profit"], "sl_a": ["s1_a_stop_loss"],
            "tp_b": ["s1_b_take_profit"], "sl_b": ["s1_b_stop_loss"],
            "tp_s2": ["s2_take_profit"], "sl_s2": ["s2_stop_loss"],
            "s2_vol": ["s2_vol_ratio"],
            "atr_p": ["atr_period"], "atr_sl": ["atr_multiplier_sl"], "atr_tr": ["atr_multiplier_trail"],
            "s1_%": ["s1_equity_pct"], "s2_%": ["s2_equity_pct"]
        }
        # [v15.7] 헤더 텍스트 매핑 (아이콘 업데이트용)
        self.param_to_col_id = {
            "vol_surge": "#6", "vol_surge_b": "#7", "buy_rsi_max_a": "#8",
            "buy_rsi_max_b": "#9", "buy_rsi_min": "#10", "gc_window": "#11",
            "rsi_window": "#12",
            "s1_a_take_profit": "#13", "s1_a_stop_loss": "#14",
            "s1_b_take_profit": "#15", "s1_b_stop_loss": "#16",
            "s2_take_profit": "#17", "s2_stop_loss": "#18",
            "s2_vol_ratio": "#19",
            "atr_period": "#20", "atr_multiplier_sl": "#21", "atr_multiplier_trail": "#22",
            "s1_equity_pct": "#23", "s2_equity_pct": "#1"  # Adjusted dynamically later
        }
        # 초기 동기화 수행
        self._sync_opt_toggles_with_settings()

    def _sync_opt_toggles_with_settings(self):
        """[v15.7] 전략 설정 탭의 활성화 여부에 따라 최적화 체크박스 자동 동기화"""
        s = self.app.settings
        # 연동 맵 정의 (Settings Key -> Opt Params)
        sync_map = {
            "use_type_a": ["vol_surge", "buy_rsi_max_a", "s1_a_take_profit", "s1_a_stop_loss"],
            "use_type_b": ["vol_surge_b", "buy_rsi_max_b", "s1_b_take_profit", "s1_b_stop_loss"],
            "use_rsi_min": ["buy_rsi_min"],
            "use_s1_tp": ["s1_a_take_profit", "s1_b_take_profit"],
            "use_s1_stop_loss": ["s1_a_stop_loss", "s1_b_stop_loss"],
            "use_s2_tp": ["s2_take_profit"],
            "use_s2_stop_loss": ["s2_stop_loss"],
            "use_atr_exit": ["atr_period"],
            "use_atr_stop_loss": ["atr_multiplier_sl"],
            "use_atr_trailing_stop": ["atr_multiplier_trail"]
        }
        
        for settings_key, opt_keys in sync_map.items():
            is_enabled = s.get(settings_key, True)
            for k in opt_keys:
                if k in self.opt_params_vars:
                    self.opt_params_vars[k].set(is_enabled)
        
        # UI 업데이트 (헤더 아이콘 반영) - UI가 생성된 후라면 실행
        if hasattr(self.app, 'tree_backtest') and self.app.tree_backtest is not None:
            self._refresh_header_icons()

    def _refresh_header_icons(self):
        """[v15.7] 현재 opt_params_vars 상태에 맞춰 헤더 아이콘(☑️/⬜) 일괄 업데이트"""
        if not hasattr(self.app, 'tree_backtest') or self.app.tree_backtest is None:
            return
            
        for col_name, params in self.col_to_params.items():
            # 묶음 파라미터 중 하나라도 켜져 있으면 ON (일관성을 위해 첫 번째 기준)
            is_active = self.opt_params_vars[params[0]].get()
            icon = "☑️" if is_active else "⬜"
            
            # 컬럼 인덱스 찾기
            cols = list(self.app.tree_backtest["columns"])
            if col_name in cols:
                col_id = f"#{cols.index(col_name) + 1}"
                current_text = self.app.tree_backtest.heading(col_id, "text")
                base_text = current_text.replace("☑️", "").replace("⬜", "").strip()
                self.app.tree_backtest.heading(col_id, text=f"{icon} {base_text}")

    def _setup_ui(self):
        """백테스팅 탭 레이아웃 구성 (v16.8: 직관적인 그룹화 레이아웃 적용)"""
        # ---상단: 가로 배치 섹션 (설정 / 최적화 / 백테스트)---
        top_grid_frame = ttk.Frame(self.parent)
        top_grid_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=5)
        
        # 1. 기간 설정 (Date Settings)
        date_lf = ttk.LabelFrame(top_grid_frame, text=" 📅 1. 기본 기간 설정 ", padding="15")
        date_lf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        
        ttk.Label(date_lf, text="시작:").grid(row=0, column=0, sticky=tk.W)
        self.app.backtest_entries['start_date'] = ttk.Entry(date_lf, width=12)
        self.app.backtest_entries['start_date'].insert(0, self.app.settings.get('bt_start_date', "2025-03-04"))
        self.app.backtest_entries['start_date'].grid(row=0, column=1, padx=(5, 2), pady=2)
        ttk.Button(date_lf, text="📅", width=3, 
                   command=lambda: self._show_picker('start_date')).grid(row=0, column=2, padx=(0, 5))

        ttk.Label(date_lf, text="종료:").grid(row=1, column=0, sticky=tk.W)
        self.app.backtest_entries['end_date'] = ttk.Entry(date_lf, width=12)
        self.app.backtest_entries['end_date'].insert(0, self.app.settings.get('bt_end_date', yesterday))
        self.app.backtest_entries['end_date'].grid(row=1, column=1, padx=(5, 2), pady=2)
        ttk.Button(date_lf, text="📅", width=3, 
                   command=lambda: self._show_picker('end_date')).grid(row=1, column=2, padx=(0, 5))

        # 2. 전략 최적화 (Optimization)
        opt_lf = ttk.LabelFrame(top_grid_frame, text=" 🚀 2. 전략 최적화 ", padding="15")
        opt_lf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        
        btn_row_opt = ttk.Frame(opt_lf)
        btn_row_opt.pack(expand=True)
        self.app.btn_start_optimize = ttk.Button(btn_row_opt, text="최적화 시작", width=15, command=self._on_btn_start_optimize)
        self.app.btn_start_optimize.pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_row_opt, text="📊 최적화결과 리포트", width=18, command=self._open_opt_trade_report).pack(side=tk.LEFT, padx=2)

        # [v25.0] 최적화 대상 종목 안내 (2025 대표주 10선)
        target_info = "🔍 대상: 삼성전자, SK하이닉스 등 10개 대표주"
        lbl_targets = ttk.Label(opt_lf, text=target_info, font=("Malgun Gothic", 8), foreground=self.app.colors['secondary'], cursor="hand2")
        lbl_targets.pack(pady=(5, 0))
        
        # [v30.0] 고정밀 최적화 옵션 (1분봉 체크)
        hp_opt_cb = ttk.Checkbutton(opt_lf, text="고정밀 최적화(1분분)", variable=self.opt_high_precision_var)
        hp_opt_cb.pack(pady=(2, 0))
        
        # 툴팁용 텍스트
        from gui.gui_utils import ToolTip
        full_stock_list = "최적화 대상 (2025 대표 10선):\n" + \
                         "삼성전자, SK하이닉스, 현대차, 신한지주, LG화학, " + \
                         "한화에어로스페이스, NAVER, 한국전력, 삼성물산, 삼성전기"
        ToolTip(lbl_targets, lambda e: full_stock_list)


        # 3. 성과 검증 (Portfolio Backtest)
        bt_lf = ttk.LabelFrame(top_grid_frame, text=" 📈 3. 성과 검증 ", padding="15")
        bt_lf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        
        btn_row_bt = ttk.Frame(bt_lf)
        btn_row_bt.pack(pady=5)
        self.app.btn_start_dynamic = ttk.Button(btn_row_bt, text="백테스트 시작", width=15, command=self._on_btn_start_dynamic_backtest)
        self.app.btn_start_dynamic.pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_row_bt, text="📊 백테스트결과 리포트", width=18, command=self._open_dynamic_trade_report).pack(side=tk.LEFT, padx=2)
        
        # 고정밀 옵션 전용 배치
        hp_cb = ttk.Checkbutton(bt_lf, text="고정밀 체결(1분봉)", variable=self.app.backtest_high_precision_var)
        hp_cb.pack()

        # ---중단: 진행 상태 (Full-Width Notification Bar)---
        status_lf = ttk.LabelFrame(self.parent, text=" 🔔 실시간 진행 상태 ", padding="10")
        status_lf.pack(side=tk.TOP, fill=tk.X, padx=15, pady=5)
        self.app.lbl_bt_status = ttk.Label(status_lf, text="시스템 대기 중...", font=("Malgun Gothic", 10, "bold"), 
                                         foreground=self.app.colors['primary'], anchor=tk.CENTER)
        self.app.lbl_bt_status.pack(fill=tk.X, expand=True)



        # 하단: 결과 테이블 영역
        res_frame = ttk.LabelFrame(self.parent, text=" 💎 최적화 결과 랭킹 (수익률 순) ")
        res_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=5)

        # [v18.0] 테이블 어셈블리 (중앙 정렬을 위한 컨테이너)
        self.table_assembly = ttk.Frame(res_frame)
        self.table_assembly.pack(fill=tk.BOTH, expand=True, pady=10) # [v33.9] Maximize Available Space
        
        # [v25.2] 선택 조합 백테스트 버튼 (누락된 버튼 복구)
        btn_apply_opt = ttk.Button(res_frame, text="🛠️ 선택한 조합으로 포트폴리오 백테스트 시작", 
                                  command=self._on_btn_backtest_with_selected)
        btn_apply_opt.pack(pady=5)

        # --- [v33.9] Unified Horizontal Scrolling Container ---
        # All table elements (Color Bar + Treeview) will be grouped in this scrollable frame
        self.canvas_bt = tk.Canvas(self.table_assembly, bd=0, highlightthickness=0, bg="#2E3440")
        h_scroll = ttk.Scrollbar(self.table_assembly, orient=tk.HORIZONTAL, command=self.canvas_bt.xview)
        
        self.canvas_bt.configure(xscrollcommand=h_scroll.set)
        
        # Inner Frame to hold everything
        self.scroll_frame_bt = tk.Frame(self.canvas_bt, bg="#2E3440")
        self.canvas_window = self.canvas_bt.create_window((0, 0), window=self.scroll_frame_bt, anchor="nw")
        
        # Structure the container with grid
        self.canvas_bt.grid(row=0, column=0, sticky="nsew")
        h_scroll.grid(row=1, column=0, sticky="ew")
        self.table_assembly.grid_columnconfigure(0, weight=1)
        self.table_assembly.grid_rowconfigure(0, weight=1)

        # Now create children with scroll_frame_bt as parent
        # [v5.0] 노르딕 프로페셔널 카테고리 헤더
        cat_frame = tk.Frame(self.scroll_frame_bt, height=30, bg="#3B4252")
        cat_frame.pack(side=tk.TOP, fill=tk.X) 

        # 카테고리 컬러 (Nord Palette 기반 저채도 구성)
        # [v16.9] 청산/추매 섹션 세분화로 가로 너비 확장 (275 + 455 + 130 + 650 = 1510px) [v33.8]
        categories = [
            ("📊 성과", 275, "#434C5E"),
            ("🚀 매수 진입", 455, "#4C566A"),
            ("💰 비중(%)", 130, "#3B4252"),
            ("🎯 청산 및 추매", 650, "#2E3440")
        ]
        
        for name, width, color in categories:
            f = tk.Frame(cat_frame, width=width, height=28, bg=color)
            f.pack_propagate(0)
            f.pack(side=tk.LEFT)
            lbl = tk.Label(f, text=name, font=("Malgun Gothic", 9, "bold"), 
                         bg=color, fg="#D8DEE9", relief=tk.FLAT)
            lbl.pack(fill=tk.BOTH, expand=True)

        # [v3.5] 컬럼 설명 정의 (툴팁용)
        self.header_tooltips = {
            "rank": "순위: 수익률 기준 최적화 결과 순위",
            "avg_ret": "수익률: 백테스트 대상 종목들의 평균 수익률 (%)",
            "avg_mdd": "MDD: 평균 최대 낙폭 (Risk 지표, 낮을수록 안정적)",
            "win_rate": "승률: 전체 매매 중 익절 마감 비율 (%)",
            "trades": "매매수: 백테스트 기간 중 발생한 총 매매 횟수",
            "vol_a": "vol_surge: Type A 거래량 급증 기준 (배)",
            "vol_b": "vol_surge_b: Type B 거래량 급증 기준 (배)",
            "rsi_b": "buy_rsi_max_b: 매수 조건 - RSI 상한선 (Type B)",
            "rsi_a": "buy_rsi_max_a: 매수 조건 - Type A 전용 RSI 상한선",
            "rsi_min": "buy_rsi_min: 매수 조건 - 전략 공통 RSI 하한선",
            "macd_f": "macd_fast: MACD 단기 이동평균 기간",
            "macd_s": "macd_slow: MACD 장기 이동평균 기간",
            "macd_sig": "macd_sig: MACD 시그널 주기",
            "gc_w": "gc_window: 이평선 골든크로스 유지시간: 5/20 이평선 골든크로스가 유지된 캔들 갯수(분)",
            "rsi_w": "rsi_window: RSI 50 상향돌파 유지시간: RSI 50을 넘어선 상태로 유지된 캔들 갯수(분)",
            "tp_a": "1단계(A) 익절: 목표 수익률 (%)",
            "sl_a": "1단계(A) 손절: 손절 기준 (%)",
            "tp_b": "1단계(B) 익절: 목표 수익률 (%)",
            "sl_b": "1단계(B) 손절: 손절 기준 (%)",
            "tp_s2": "2단계(S2) 익절: 목표 수익률 (%)",
            "sl_s2": "2단계(S2) 손절: 손절 기준 (%)",
            "s2_vol": "s2_vol_ratio: 2차 매수 시 필요한 최소 거래량 배수 (배)",
            "atr_p": "atr_period: ATR 계산 기간 (Candles)",
            "atr_sl": "atr_multiplier_sl: ATR 손절 배수 (Multiplier)",
            "atr_tr": "atr_multiplier_trail: ATR 추격 매도시점 배수 (Multiplier)",
            "s1_%": "s1_equity_pct: 1단계 투입 비중 (%)",
            "s2_%": "s2_equity_pct: 2단계 투입 비중 (%)"
        }

        # 카테고리별 컬럼 구성 [v16.9]
        cols = ["rank", "avg_ret", "avg_mdd", "win_rate", "trades", 
                "vol_a", "vol_b", "rsi_a", "rsi_b", "rsi_min", "gc_w", "rsi_w",
                "s1_%", "s2_%",
                "tp_a", "sl_a", "tp_b", "sl_b", "tp_s2", "sl_s2", "s2_vol",
                "atr_p", "atr_sl", "atr_tr"] 
        self.app.tree_backtest = ttk.Treeview(self.scroll_frame_bt, columns=cols, show="headings", height=15)
        
        # 헤더 이름 설정 (카테고리 없이 깔끔하게)
        headers = {
            "rank": "순위", "avg_ret": "수익률", "avg_mdd": "MDD", "win_rate": "승률", "trades": "횟수",
            "vol_a": "☑️ V_A", "vol_b": "☑️ V_B", "rsi_a": "☑️ R_A", "rsi_b": "☑️ R_B", "rsi_min": "☑️ R_Min",
            "gc_w": "☑️ G_Dur", "rsi_w": "☑️ R_Dur",
            "tp_a": "☑️ T_A", "sl_a": "☑️ L_A", "tp_b": "☑️ T_B", "sl_b": "☑️ L_B",
            "tp_s2": "☑️ T_S2", "sl_s2": "☑️ L_S2",
            "s2_vol": "☑️ Vol", "atr_p": "☑️ ATR_P", "atr_sl": "☑️ ATR_SL", "atr_tr": "☑️ ATR_TR",
            "s1_%": "☑️ S1%", "s2_%": "☑️ S2%"
        }
        
        for col in cols:
            self.app.tree_backtest.heading(col, text=headers.get(col, col))
            
        # 정확한 픽셀 매칭 (위 카테고리 바와 일치시키기 위함, v3.7)
        # [v16.9] 청산 섹션 너비 재조정 (80*5 = 400)
        # [v25.3] 지표 섹션 제거에 따른 너비 재조정
        col_widths = {
            "rank": 40, "avg_ret": 60, "avg_mdd": 60, "win_rate": 60, "trades": 55,
            "vol_a": 65, "vol_b": 65, "rsi_b": 65, "rsi_a": 65, "rsi_min": 65, "gc_w": 65, "rsi_w": 65, # [v25.3]
            "tp_a": 65, "sl_a": 65, "tp_b": 65, "sl_b": 65, "tp_s2": 65, "sl_s2": 65,
            "s2_vol": 65, "atr_p": 65, "atr_sl": 65, "atr_tr": 65,
            "s1_%": 65, "s2_%": 65
        }
        for col in cols:
            self.app.tree_backtest.column(col, width=col_widths.get(col, 60), anchor=tk.CENTER, stretch=False)
        
        # [v25.0] 헤더 클릭 바인딩
        self.app.tree_backtest.bind("<Button-1>", self._on_header_click)
        # [v25.2] 더블 클릭 시 백테스트 실행 바인딩
        self.app.tree_backtest.bind("<Double-1>", lambda e: self._on_btn_backtest_with_selected())
        
        # v_scroll은 tree_backtest와 같은 부모(scroll_frame_bt) 또는 table_assembly에 둘 수 있습니다.
        # table_assembly에 두면 스크롤바가 우측에 고정되어 더 편리합니다.
        v_scroll = ttk.Scrollbar(self.table_assembly, orient=tk.VERTICAL, command=self.app.tree_backtest.yview)
        v_scroll.grid(row=0, column=1, sticky="ns")
        self.app.tree_backtest.configure(yscrollcommand=v_scroll.set)
        
        self.app.tree_backtest.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # Dynamic Scroll Region Update
        def _update_scroll_region(e):
            self.canvas_bt.configure(scrollregion=self.canvas_bt.bbox("all"))
            # [v33.9] Remove height clamping to allow full expansion with window
            # req_h = self.scroll_frame_bt.winfo_reqheight()
            # self.canvas_bt.config(height=min(req_h, 450)) 

        self.scroll_frame_bt.bind("<Configure>", _update_scroll_region)

        # [v3.8] Treeview 스타일 세부 조정 (줄무늬 및 가독성)
        style = ttk.Style()
        style.configure("Treeview", rowheight=25)
        self.app.tree_backtest.tag_configure('odd', background='#FDFDFD')
        self.app.tree_backtest.tag_configure('even', background='#F7F9FB')

        # [v3.5] 툴팁 클래스 적용 (헤더 영역 전용)
        from gui.gui_utils import ToolTip
        def get_header_tip(event):
            region = self.app.tree_backtest.identify_region(event.x, event.y)
            if region == "heading":
                col_id = self.app.tree_backtest.identify_column(event.x)
                try:
                    col_index = int(col_id.replace("#", "")) - 1
                    cols = self.app.tree_backtest["columns"]
                    col_name = cols[col_index]
                    
                    tip = f"[{col_name}]\n{self.header_tooltips[col_name]}"
                    
                    # [v15.7] 동적 탐색 범위 추가 표시
                    if col_name in self.col_to_params:
                        params = self.col_to_params[col_name]
                        space = self._get_dynamic_search_space(self.app.settings)
                        
                        range_strs = []
                        for p in params:
                            is_active = self.opt_params_vars[p].get()
                            if is_active:
                                range_strs.append(f"🔍 탐색: {p} {space[p]}")
                            else:
                                range_strs.append(f"🔒 고정: {p} [{self.app.settings.get(p)}]")
                        
                        tip += "\n" + "-"*20 + "\n" + "\n".join(range_strs)
                    
                    return tip
                except: return None
            return None
            
        self.header_tip = ToolTip(self.app.tree_backtest, get_header_tip)
        
        # [v15.7] 초기 UI 생성 후 아이콘 동기화
        self.app.root.after(200, self._refresh_header_icons)

    def _get_dynamic_search_space(self, settings):
        """[v15.7] 현재 설정값을 기준으로 주변 탐색 범위를 동적으로 생성"""
        s = settings
        def range_for(val, step, count=3):
            # 정수형인 경우 정수 리스트, 소수인 경우 소수 리스트 반환
            if isinstance(val, int):
                return [int(val + (i - count//2) * step) for i in range(count)]
            else:
                return [round(float(val) + (i - count//2) * step, 2) for i in range(count)]

        return {
            'buy_rsi_min': range_for(s.get('buy_rsi_min', 40.0), 5.0),
            'buy_rsi_max_b': range_for(s.get('buy_rsi_max_b', 65.0), 5.0),
            'buy_rsi_max_a': range_for(s.get('buy_rsi_max_a', 58.0), 5.0),
            'vol_surge': range_for(s.get('vol_surge', 1.5), 0.3),
            'vol_surge_b': range_for(s.get('vol_surge_b', 1.5), 0.3),
            's1_a_take_profit': range_for(s.get('s1_a_take_profit', 5.0), 2.0),
            's1_a_stop_loss': range_for(s.get('s1_a_stop_loss', 2.0), 1.0),
            's1_b_take_profit': range_for(s.get('s1_b_take_profit', 4.0), 2.0),
            's1_b_stop_loss': range_for(s.get('s1_b_stop_loss', 2.0), 1.0),
            's2_take_profit': range_for(s.get('s2_take_profit', 5.0), 2.0),
            's2_stop_loss': range_for(s.get('s2_stop_loss', 2.0), 0.5),
            's2_vol_ratio': range_for(s.get('s2_vol_ratio', 1.0), 0.2),
            'gc_window': range_for(s.get('gc_window', 90), 10),
            'rsi_window': range_for(s.get('rsi_window', 45), 5),
            'atr_period': range_for(s.get('atr_period', 14), 5),
            'atr_multiplier_sl': range_for(s.get('atr_multiplier_sl', 2.0), 0.5),
            'atr_multiplier_trail': range_for(s.get('atr_multiplier_trail', 3.0), 1.0),
            's1_equity_pct': range_for(s.get('s1_equity_pct', 10.0), 5.0),
            's2_equity_pct': range_for(s.get('s2_equity_pct', 20.0), 10.0)
        }

    def _on_header_click(self, event):
        """[v25.0] 헤더 클릭 시 최적화 여부 토글"""
        region = self.app.tree_backtest.identify_region(event.x, event.y)
        if region != "heading":
            return
            
        col_id = self.app.tree_backtest.identify_column(event.x)
        try:
            col_index = int(col_id.replace("#", "")) - 1
            cols = self.app.tree_backtest["columns"]
            col_name = cols[col_index]
            
            if col_name in self.col_to_params:
                params = self.col_to_params[col_name]
                # 첫 번째 파라미터 기준으로 토글 (묶음 처리)
                is_active = not self.opt_params_vars[params[0]].get()
                for p in params:
                    self.opt_params_vars[p].set(is_active)
                
                # 헤더 텍스트 업데이트 (아이콘 교체)
                icon = "☑️" if is_active else "⬜"
                current_text = self.app.tree_backtest.heading(col_id, "text")
                # 기존 아이콘 제거 후 새 아이콘 결합
                base_text = current_text.replace("☑️", "").replace("⬜", "").strip()
                self.app.tree_backtest.heading(col_id, text=f"{icon} {base_text}")
                
                # 상태바 안내
                status = "활성화" if is_active else "고정(Settings 기준)"
                self.app.lbl_bt_status.config(text=f"파라미터 [{base_text}] 최적화 {status}", foreground="blue")
        except Exception as e:
            print(f"Header Toggle Error: {e}")

    def _on_btn_start_optimize(self):
        """최적화 버튼 클릭 핸들러"""
        if self.app.is_monitoring:
            messagebox.showwarning("경고", "실시간 감시가 중단된 상태에서만 백테스팅이 가능합니다.")
            return
            
        self.app.btn_start_optimize.config(state=tk.DISABLED)
        self.app.lbl_bt_status.config(text="최적화 중... (수 분 소요될 수 있음)", foreground="red")
        
        # [v26.3] 설정 탭의 최신 입력값을 동기화 (사용자가 '저장'을 누르지 않았을 때를 대비)
        if hasattr(self.app, 'settings_tab'):
            try:
                # [v33.9] Disable periodic sync to backtest to preserve manual toggles
                self.app.settings_tab.save_settings(sync_to_backtest=False)
                self.app.log("ℹ️ 백테스트 시작 전 설정 탭의 값을 자동으로 동기화했습니다. (토글 유지)")
            except Exception as e:
                print(f"[DEBUG] Settings sync error: {e}")
        
        # 입력값 파싱 (v18.8: Date Normalization)
        try:
            raw_start = self.app.backtest_entries['start_date'].get().strip()
            raw_end = self.app.backtest_entries['end_date'].get().strip()
            start_date = pd.to_datetime(raw_start).strftime('%Y-%m-%d')
            end_date = pd.to_datetime(raw_end).strftime('%Y-%m-%d')
            
            # [Persistence] 날짜 설정 즉시 저장
            self.app.settings['bt_start_date'] = start_date
            self.app.settings['bt_end_date'] = end_date
            self.app.save_settings()
            
            # [v15.7] 최적화 시작 전에는 다시 동기화하지 않음 (사용자 수동 선택 존중)
            # self._sync_opt_toggles_with_settings() 
            
            # [v8.6] 전략 설정(self.settings) 탭의 값을 기준으로 백테스트 수행
            s = self.app.settings
            
            # [v15.7] 현재 설정값을 기반으로 다이나믹 탐색 범위 생성
            full_search_space = self._get_dynamic_search_space(s)
            
            # [v25.0] 선택된 파라미터만 search_space에 포함
            search_space = {}
            for key, var in self.opt_params_vars.items():
                if var.get():
                    search_space[key] = full_search_space[key]
            
            if not search_space:
                messagebox.showwarning("주의", "최소 하나 이상의 파라미터를 선택해야 합니다.")
                self.app.btn_start_optimize.config(state=tk.NORMAL)
                return
            
            # v6.3: UI에서 제거되었으므로 기본값 False 사용
            force_refresh = False 
            
            # [v33.8] Sizing Unit Auto-Sync
            if self.opt_params_vars['s1_equity_pct'].get():
                self.app.settings['s1_sizing_unit'] = '%'
            if self.opt_params_vars['s2_equity_pct'].get():
                self.app.settings['s2_sizing_unit'] = '%'
                
            # [v30.0] 고정밀 최적화 옵션 (1분봉 체크)
            high_precision = self.opt_high_precision_var.get()
            
            # 쓰레드 실행
            threading.Thread(target=self._run_optimize_thread, args=(start_date, end_date, search_space, force_refresh, high_precision), daemon=True).start()
            
        except Exception as e:
            messagebox.showerror("입력 오류", f"입력 형식을 확인해 주세요: {e}")
            self.app.btn_start_optimize.config(state=tk.NORMAL)

    def _render_optimization_results(self, df):
        """최적화 결과 랭킹 렌더링 [v13.0]"""
        # 기존 내용 삭제
        for item in self.app.tree_backtest.get_children():
            self.app.tree_backtest.delete(item)
            
        if df is None or df.empty:
            return
        
        self.last_opt_df = df.head(20)  # [v25.2] 상위 결과물 캐시
            
        # 상위 20개 표시 (v30.0: 확장된 파라미터 매칭)
        for i, row in enumerate(df.head(20).itertuples(), 1):
            tag = 'plus' if row.avg_ret > 0 else 'minus'
            res = row._asdict() # 편의를 위해 dict 변환
            
            # [v25.1] 최적화 대상 여부와 무관하게 원래대로 깔끔하게 출력 (사용자 요청)
            def fmt(key, val, decimal=1):
                if isinstance(val, (float, int)):
                    return f"{val:.{decimal}f}"
                return str(val)

            tp_a = fmt('s1_a_take_profit', res.get('s1_a_take_profit', 0))
            sl_a = fmt('s1_a_stop_loss', res.get('s1_a_stop_loss', 0))
            tp_b = fmt('s1_b_take_profit', res.get('s1_b_take_profit', 0))
            sl_b = fmt('s1_b_stop_loss', res.get('s1_b_stop_loss', 0))
            tp_s2 = fmt('s2_take_profit', res.get('s2_take_profit', 0))
            sl_s2 = fmt('s2_stop_loss', res.get('s2_stop_loss', 0))

            vol_a = fmt('vol_surge', res.get('vol_surge', 0))
            vol_b = fmt('vol_surge_b', res.get('vol_surge_b', 0))
            rsi_b = fmt('buy_rsi_max_b', res.get('buy_rsi_max_b', 0))
            rsi_a = fmt('buy_rsi_max_a', res.get('buy_rsi_max_a', 0))
            rsi_min = fmt('buy_rsi_min', res.get('buy_rsi_min', 0))
            rsi_w = fmt('rsi_window', res.get('rsi_window', 0), 0)
            gc_w = fmt('gc_window', res.get('gc_window', 0), 0)
            s2_vol = fmt('s2_vol_ratio', res.get('s2_vol_ratio', 1.0))
            
            # [v31.0] ATR Parameters
            atr_p = fmt('atr_period', res.get('atr_period', 14), 0)
            atr_sl = fmt('atr_multiplier_sl', res.get('atr_multiplier_sl', 2.0))
            atr_tr = fmt('atr_multiplier_trail', res.get('atr_multiplier_trail', 3.0))
            
            s1_pct = fmt('s1_equity_pct', res.get('s1_equity_pct', 10.0))
            s2_pct = fmt('s2_equity_pct', res.get('s2_equity_pct', 20.0))
            
            row_tag = 'even' if i % 2 == 0 else 'odd'
            perf_tag = 'plus' if row.avg_ret > 0 else 'minus'
            
            self.app.tree_backtest.insert("", tk.END, values=(
                i, f"{row.avg_ret:+.2f}", f"{row.avg_mdd:.2f}", f"{row.avg_win:.2f}", 
                int(row.total_trades), vol_a, vol_b, rsi_a, rsi_b, rsi_min, gc_w, rsi_w, 
                s1_pct, s2_pct,
                tp_a, sl_a, tp_b, sl_b, tp_s2, sl_s2, s2_vol, atr_p, atr_sl, atr_tr
            ), tags=(perf_tag, row_tag))
            
        # 태그 색상 설정 (노르딕 테마 고정밀 - 저채도)
        self.app.tree_backtest.tag_configure('plus', foreground='#A3BE8C') # Moss Green
        self.app.tree_backtest.tag_configure('minus', foreground='#BF616A') # Aurora Red
        self.app.tree_backtest.tag_configure('odd', background='#242933')
        self.app.tree_backtest.tag_configure('even', background='#2E3440')
        self.app.tree_backtest.tag_configure('selected', background='#434C5E', foreground='#ECEFF4')

    def _run_optimize_thread(self, start, end, space, force_refresh=False, high_precision=False):
        """최적화 메인 쓰레드 [v13.0]"""
        try:
            import importlib.util
            import backtest_engine as bt_mod
            import parameter_optimizer as opt_mod
            import importlib
            importlib.reload(bt_mod)
            importlib.reload(opt_mod)
            
            optimizer = None
            
            # 테스트 종목군
            sample_codes = [
                '005930', '000660', '005380', '055550', '051910', 
                '012450', '035420', '015760', '028260', '009150'
            ]
            
            # v6.3: base_settings(self.app.settings)를 전달하여 최종 패스 시 활용
            optimizer = opt_mod.ParameterOptimizer(sample_codes, space, self.app.settings)
            
            # UI 상태 업데이트 함수
            def update_progress(msg, value):
                self.app.root.after(0, lambda: self.app.lbl_bt_status.config(text=msg))
                self.app.root.after(0, lambda: self.app.progress_var.set(value))

            # UI 상태 초기화
            self.app.root.after(0, lambda: self.app.lbl_bt_status.config(text="최적화 시작...", foreground="orange"))
            
            # 고정밀 15분봉 백테스팅 수행 (v30.0: Bayesian Optimization 적용)
            df_results = optimizer.find_best_params(start, end, interval='15m', callback=update_progress, 
                                                    force_refresh=force_refresh, high_precision=high_precision,
                                                    method='bayesian', n_trials=150, seed=42)
            
            # UI 업데이트 (메인 쓰레드에서 실행)
            if df_results is not None and not df_results.empty:
                self.app.root.after(0, lambda: self._render_optimization_results(df_results))
            else:
                self.app.root.after(0, lambda: messagebox.showwarning("결과 없음", "선택한 기간 동안 매매 신호가 발생하지 않았거나 데이터를 불러올 수 없습니다.\n기간을 조정하거나 RSI 범위를 넓혀보세요."))
            
        except Exception as e:
            err_msg = str(e)
            print(f"Error in optimization thread: {err_msg}")
            self.app.root.after(0, lambda: messagebox.showerror("최적화 오류", f"최적화 중 오류가 발생했습니다: {err_msg}"))
        finally:
            # [v8.5] 최종 결과 데이터 저장 (온디맨드 차트용)
            try:
                if os.path.exists("backtest_trades.csv"):
                    self.app.last_backtest_trades = pd.read_csv("backtest_trades.csv")
                    # 날짜 형식 클리닝
                    self.app.last_backtest_trades = self.app.last_backtest_trades[self.app.last_backtest_trades['Date'] != 'Date'].copy()
                    self.app.last_backtest_trades['Date'] = pd.to_datetime(self.app.last_backtest_trades['Date'], errors='coerce')
                    self.app.last_backtest_trades = self.app.last_backtest_trades.dropna(subset=['Date'])
                    
                    # 콤보박스 업데이트
                    trade_codes = sorted(self.app.last_backtest_trades['Code'].unique())
                    codes_with_names = []
                    name_map = bt_mod.TradingStrategy.get_stock_name_map()
                    for c in trade_codes:
                        c_str = str(c).zfill(6)
                        name = name_map.get(c_str, "Unknown")
                        codes_with_names.append(f"{name} ({c_str})")
                    
                    self.app.root.after(0, lambda: self.app.lbl_bt_status.config(text="데이터 처리 중...", foreground="orange"))
                
                # 시장 데이터 보관 (ParameterOptimizer 멤버로 접근 가능하다고 가정)
                if optimizer is not None:
                    if hasattr(optimizer, 'market_df'): self.app.last_backtest_market = optimizer.market_df
                    if hasattr(optimizer, 'vix_df'): self.app.last_backtest_vix = optimizer.vix_df
                    if hasattr(optimizer, 'best_settings'): self.app.last_backtest_settings = optimizer.best_settings
                self.app.last_backtest_interval = "15m"
                self.app.last_backtest_start = start # [v11.8]
                self.app.last_backtest_end = end     # [v11.8]
            except Exception as ex:
                print(f"Error saving backtest context: {ex}")

            self.app.root.after(0, lambda: self.app.btn_start_optimize.config(state=tk.NORMAL))
            self.app.root.after(0, lambda: self.app.lbl_bt_status.config(text="최적화 완료!", foreground="green"))


    def _open_signal_report(self):
        """신호 분석 리포트 열기"""
        path = os.path.abspath("signal_report.html")
        if os.path.exists(path):
            webbrowser.open(f"file://{path}")
        else:
            messagebox.showinfo("정보", "신호 리포트 파일이 없습니다. 백테스트를 먼저 실행해 주세요.")

    def _open_opt_trade_report(self):
        """최적화용 매매 리포트 열기 (trade_report.html)"""
        import os, webbrowser
        path = os.path.abspath("trade_report.html")
        if os.path.exists(path):
            webbrowser.open(f"file://{path}")
        else:
            messagebox.showinfo("정보", "최적화 매매 리포트 파일이 없습니다. 최적화를 먼저 수행해 주세요.")

    def _open_dynamic_trade_report(self):
        """포트폴리오 백테스트용 매매 리포트 열기 (dynamic_trade_report.html)"""
        import os, webbrowser
        path = os.path.abspath("dynamic_trade_report.html")
        if os.path.exists(path):
            webbrowser.open(f"file://{path}")
        else:
            messagebox.showinfo("정보", "포트폴리오 백테스트 매매 리포트 파일이 없습니다. 백테스트를 먼저 수행해 주세요.")

    def _on_btn_backtest_with_selected(self):
        """[v25.2] 선택된 행의 파라미터로 다이나믹 백테스트 팝업 띄우기"""
        selected = self.app.tree_backtest.selection()
        if not selected:
            messagebox.showwarning("알림", "백테스트에 적용할 조합을 테이블에서 선택해 주세요.")
            return

        item = self.app.tree_backtest.item(selected[0])
        rank = item['values'][0]
        
        if self.last_opt_df is not None:
            # rank는 1부터 시작하므로 머리속으로 생각한 인덱스와 맞춤
            try:
                # head(20) 기준이므로 rank-1로 접근
                params = self.last_opt_df.iloc[int(rank)-1].to_dict()
                self._show_param_edit_popup(params)
            except Exception as e:
                messagebox.showerror("오류", f"데이터를 가져오는데 실패했습니다: {e}")

    def _show_param_edit_popup(self, params):
        """[v25.5] 파라미터 확인 및 수정용 팝업 다이얼로그 (Nord-Styled Grouped Layout)"""
        popup = tk.Toplevel(self.app.root)
        popup.title("⚙️ 백테스트 반영 파라미터 확인")
        
        # [v25.6] 화면 중앙 배치 로직 (부모 창 기준)
        w, h = 580, 600
        self.app.root.update_idletasks() # 부모 창 최신 정보 반영
        root_w = self.app.root.winfo_width()
        root_h = self.app.root.winfo_height()
        root_x = self.app.root.winfo_x()
        root_y = self.app.root.winfo_y()
        
        # 부모 창의 중앙 좌표 계산
        pos_x = root_x + (root_w // 2) - (w // 2)
        pos_y = root_y + (root_h // 2) - (h // 2)
        
        popup.geometry(f"{w}x{h}+{pos_x}+{pos_y}")
        popup.configure(bg="#2E3440")
        popup.transient(self.app.root)
        popup.grab_set()

        # [Header] Rank & Score Info
        header_frame = tk.Frame(popup, bg="#3B4252", height=50)
        header_frame.pack(fill=tk.X)
        rank_val = params.get('rank', '-')
        ret_val = params.get('avg_ret', 0)
        mdd_val = params.get('avg_mdd', 0)
        win_val = params.get('avg_win', 0)
        trades_val = int(params.get('total_trades', 0))
        
        info_text = f"🏆 Rank #{rank_val}  |  Ret: {ret_val:+.2f}%  |  MDD: {mdd_val:.2f}%  |  Win: {win_val:.1f}%  |  Trades: {trades_val}"
        ttk.Label(header_frame, text=info_text, font=("Malgun Gothic", 10, "bold"), 
                  foreground="#81A1C1", background="#3B4252").pack(pady=10)

        main_container = tk.Frame(popup, bg="#2E3440", padx=15, pady=10)
        main_container.pack(fill=tk.BOTH, expand=True)

        # Style Configuration
        style = ttk.Style()
        style.configure("Nord.TLabelframe", background="#2E3440", foreground="#D8DEE9")
        style.configure("Nord.TLabelframe.Label", font=("Malgun Gothic", 9, "bold"), foreground="#88C0D0", background="#2E3440")

        # [v26.6] Aligned with exactly 12 Parameter Columns (Treeview Headers)
        # Using a single-column layout with grouped inputs for TP/SL to match column counts.
        param_rows = [
            ('vol_surge', 'V_A (거래량급증A)'),
            ('vol_surge_b', 'V_B (거래량급증B)'),
            ('buy_rsi_max_a', 'R_A (RSI상한A)'),
            ('buy_rsi_max_b', 'R_B (RSI상한B)'),
            ('buy_rsi_min', 'R_Min (RSI하한)'),
            ('gc_window', 'G_Dur (MA유지)'),
            ('rsi_window', 'R_Dur (RSI유지)'),
            ('s1_a_take_profit', 'TP_A (정찰 익절)'),
            ('s1_a_stop_loss', 'SL_A (정찰 손절)'),
            ('s1_b_take_profit', 'TP_B (돌파 익절)'),
            ('s1_b_stop_loss', 'SL_B (돌파 손절)'),
            ('s2_take_profit', 'TP_S2 (추매 익절)'),
            ('s2_stop_loss', 'SL_S2 (추매 손절)'),
            ('s2_vol_ratio', 'Vol (추매 거래량)'),
            ('atr_period', 'ATR_P (ATR기간)'),
            ('atr_multiplier_sl', 'ATR_SL (손절배수)'),
            ('atr_multiplier_trail', 'ATR_TR (추격배수)'),
            ('s1_equity_pct', 'S1_% (1차비중)'),
            ('s2_equity_pct', 'S2_% (추매비중)')
        ]

        entries = {}
        for idx, (key_or_keys, label) in enumerate(param_rows):
            row_frame = tk.Frame(main_container, bg="#2E3440", pady=3)
            row_frame.pack(fill=tk.X)
            
            # Label
            tk.Label(row_frame, text=f"{idx+1}. {label}", font=("Malgun Gothic", 10), 
                     bg="#2E3440", fg="#E5E9F0", width=25, anchor="w").pack(side=tk.LEFT)
            
            if isinstance(key_or_keys, list):
                # Combined row (e.g. TP/SL)
                for k in key_or_keys:
                    is_opt = k in self.opt_params_vars and self.opt_params_vars[k].get()
                    val = params.get(k, self.app.settings.get(k, 0))
                    ent = tk.Entry(row_frame, width=8, justify='center', 
                                   bg="#3B4252", fg="#ECEFF4", insertbackground="white", relief=tk.FLAT)
                    ent.insert(0, f"{val:.1f}" if isinstance(val, (float, np.float64)) else str(val))
                    if not is_opt: 
                        ent.config(state='disabled', disabledbackground="#2E3440", disabledforeground="#4C566A")
                    ent.pack(side=tk.LEFT, padx=5)
                    entries[k] = ent
                    
                    unit = "TP" if "profit" in k else "SL"
                    tk.Label(row_frame, text=unit, font=("Malgun Gothic", 8), bg="#2E3440", fg="#81A1C1").pack(side=tk.LEFT, padx=(0, 10))
            else:
                # Single row
                k = key_or_keys
                is_opt = k in self.opt_params_vars and self.opt_params_vars[k].get()
                val = params.get(k, self.app.settings.get(k, 0))
                ent = tk.Entry(row_frame, width=8, justify='center', 
                               bg="#3B4252", fg="#ECEFF4", insertbackground="white", relief=tk.FLAT)
                ent.insert(0, f"{val:.1f}" if isinstance(val, (float, np.float64)) else str(val))
                if not is_opt: 
                    ent.config(state='disabled', disabledbackground="#2E3440", disabledforeground="#4C566A")
                ent.pack(side=tk.LEFT, padx=5)
                entries[k] = ent

        def run_confirmed():
            try:
                new_params = {}
                for key, entry in entries.items():
                    # Disabled 상태는 무시하거나 현재 설정값 유지
                    if entry.cget('state') == 'disabled':
                        continue
                    raw_val = entry.get().strip()
                    new_params[key] = float(raw_val) if '.' in raw_val else int(raw_val)
                
                popup.destroy()
                self._on_btn_start_dynamic_backtest(override_params=new_params)
            except Exception as e:
                messagebox.showerror("입력 오류", f"수치를 다시 확인해 주세요: {e}")

        # [Footer] Action Buttons
        footer = tk.Frame(popup, bg="#2E3440", pady=15)
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        
        btn_run = tk.Button(footer, text="🚀 이 설정으로 포트폴리오 백테스트 시작", width=40, height=2,
                            bg="#A3BE8C", fg="#2E3440", font=("Malgun Gothic", 10, "bold"), 
                            activebackground="#8FBCBB", relief=tk.FLAT, command=run_confirmed)
        btn_run.pack()
        
    def _on_btn_start_dynamic_backtest(self, override_params=None):
        """포트폴리오 백테스트(단일) 버튼 클릭 핸들러"""
        if self.app.is_monitoring:
            messagebox.showwarning("경고", "실시간 감시가 중단된 상태에서만 백테스팅이 가능합니다.")
            return

        # [v18.8] Date Normalization
        try:
            raw_start = self.app.backtest_entries['start_date'].get().strip()
            raw_end = self.app.backtest_entries['end_date'].get().strip()
            start = pd.to_datetime(raw_start).strftime('%Y-%m-%d')
            end = pd.to_datetime(raw_end).strftime('%Y-%m-%d')
        except Exception as e:
            messagebox.showerror("입력 오류", f"날짜 형식이 올바르지 않습니다: {e}")
            return
            
        # [Persistence] 날짜 설정 즉시 저장
        self.app.settings['bt_start_date'] = start
        self.app.settings['bt_end_date'] = end
        self.app.save_settings()
            
        self.app.btn_start_dynamic.config(state=tk.DISABLED)
        self.app.btn_start_optimize.config(state=tk.DISABLED)
        self.app.lbl_bt_status.config(text="백테스트 준비 중...", foreground="orange")
        
        threading.Thread(target=self._run_dynamic_backtest_thread, args=(start, end, override_params), daemon=True).start()

    def _run_dynamic_backtest_thread(self, start_date, end_date, override_params=None):
        """별도 스레드에서 고정밀 백테스트 시뮬레이션 실행"""
        try:
            import dynamic_backtest as bt
            importlib.reload(bt)
            
            # UI 콜백 함수 정의
            def update_progress(msg, value=None):
                self.app.root.after(0, lambda: self.app.lbl_bt_status.config(text=msg))
                # progress_var가 있다면 업데이트 (v16.0 호환성)
                if hasattr(self.app, 'progress_var') and value is not None:
                    self.app.root.after(0, lambda: self.app.progress_var.set(value))

            # [v20.0] 고정밀 모드 여부 가져오기
            hp_enabled = self.app.backtest_high_precision_var.get()

            # [v25.2] 파라미터 준비 (Override vs Settings)
            active_settings = self.app.settings.copy()
            if override_params:
                active_settings.update(override_params)
                # [v33.8] Sizing Unit Sync for Override (Popup Backtest)
                if 's1_equity_pct' in override_params:
                    active_settings['s1_sizing_unit'] = '%'
                if 's2_equity_pct' in override_params:
                    active_settings['s2_sizing_unit'] = '%'
                update_progress(f"사용자 정의 파라미터 적용 중...", 5)

            # [Engine Call] 고정밀 백테스터 인스턴스화 및 실행
            tester = bt.DynamicBacktester(active_settings)
            # interval은 설정값 혹은 기본 15m 사용
            trades_df = tester.run(start_date=start_date, end_date=end_date, interval='15m', callback=update_progress, high_precision=hp_enabled)

            if trades_df is not None and not trades_df.empty:
                self.app.root.after(0, lambda: self.app.lbl_bt_status.config(text="백테스트 완료! 리포트 생성됨", foreground="green"))
                
                # 리포트 자동 열기
                report_path = os.path.abspath(os.path.join(os.getcwd(), 'dynamic_trade_report.html'))
                if os.path.exists(report_path):
                    self.app.root.after(0, lambda: webbrowser.open(f"file://{report_path}"))
            else:
                self.app.root.after(0, lambda: messagebox.showwarning("결과 없음", "선택된 기간에 매매된 내역이 없습니다. (시그널 또는 예산 부족)"))
                self.app.root.after(0, lambda: self.app.lbl_bt_status.config(text="완료 (매매 내역 없음)", foreground="blue"))

        except Exception as e:
            err_msg = str(e)
            print(f"Error in dynamic backtest thread: {err_msg}")
            traceback.print_exc()
            self.app.root.after(0, lambda: messagebox.showerror("오류", f"백테스트 중 오류가 발생했습니다.\n{err_msg}"))
            self.app.root.after(0, lambda: self.app.lbl_bt_status.config(text="오류 발생", foreground="red"))
        finally:
            self.app.root.after(0, lambda: self.app.btn_start_dynamic.config(state=tk.NORMAL))
            self.app.root.after(0, lambda: self.app.btn_start_optimize.config(state=tk.NORMAL))

    def _show_picker(self, key):
        """[v3.1] 커스텀 날짜 선택기 팝업 호출"""
        from gui.gui_utils import CustomDatePicker
        entry = self.app.backtest_entries.get(key)
        if entry:
            CustomDatePicker(self.parent, entry, initial_date=entry.get(), colors=self.app.colors)
