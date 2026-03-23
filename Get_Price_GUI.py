# -*- coding: utf-8 -*-
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import requests
import pandas as pd
import os
import json
from datetime import datetime, date, timedelta
import threading
import time
import FinanceDataReader as fdr
import config
import importlib
from concurrent.futures import ThreadPoolExecutor
import asyncio
import websockets
import traceback
import itertools
import importlib.util

import webbrowser
from backtest_engine import TradingStrategy
from data_manager_v2 import DataManagerV2 as DataManager
import strategy_selector
import reporting
from strategy.data_loader import BacktestDataLoader

# Modularized GUI Tabs [v3.0]
try:
    from gui.dashboard_tab import DashboardTab
    from gui.monitor_tab import MonitorTab
    from gui.settings_tab import SettingsTab
    from gui.virtual_tab import VirtualTab
    from gui.backtest_tab import BacktestTab
except ImportError:
    # Handle development environment where gui/ might not be in path
    import sys
    sys.path.append(os.path.join(os.getcwd(), "gui"))
    from dashboard_tab import DashboardTab
    from monitor_tab import MonitorTab
    from settings_tab import SettingsTab
    from virtual_tab import VirtualTab
    from backtest_tab import BacktestTab
from strategy import indicators, signals
from core.auth_manager import AuthManager
from core.config_handler import ConfigHandler
from core.stream_processor import StreamProcessor
from gui.gui_utils import GuiUtils

# Kiwoom REST API 설정
os.environ['KIWOOM_API_KEY'] = config.APP_KEY
os.environ['KIWOOM_API_SECRET'] = config.APP_SECRET

# 중요: kiwoom_rest_api.config 설정
try:
    from kiwoom_rest_api import config as kiwoom_config
    kiwoom_config.API_KEY = config.APP_KEY
    kiwoom_config.API_SECRET = config.APP_SECRET
except:
    pass

from kiwoom_rest_api.auth.token import TokenManager

# 기본 전략 설정
DEFAULT_SETTINGS = {
    "min_trde_val": 10.0,    # 최소 평균 거래대금(억)
    "sel_rsi_min": 40.0,     # 종목 선정 RSI 하한
    "sel_rsi_max": 70.0,     # 종목 선정 RSI 상한
    "buy_rsi_min": 40.0,     # 매수 전략 공통 RSI 하한
    "buy_rsi_max_b": 65.0,   # Type B 전용 RSI 상한
    "buy_rsi_max_a": 58.0,   # Type A 전용 RSI 상한
    "use_type_a": True,     # Type A 전략 사용 여부
    "use_type_b": True,     # Type B 전략 사용 여부
    "vol_surge": 1.5,       # Type A 전일 동시간 대비 거래량 급증 기준
    "use_vol_surge": True,  # Type A 거래량 급증 사용 여부
    "vol_surge_b": 1.5,     # Type B 전일 동시간 대비 거래량 급증 기준
    "use_type_b_vol": True,
    "use_macd_gc_b": True,
    "use_trend_aligned_b": True,
    'total_capital': 10000000.0, 
    'max_slots': 20,              
    'time_buffer_bars': 4,        
    "vol_ratio": 1.5,       
    "vol_lookback": 10,     
    "ma_convergence": 0.07,   
    "use_gc": True,           
    "use_rsi_signal": True,   
    "rsi_window": 45,         
    "use_vol_power": True,    
    "vol_power_lvl": 120.0,   
    "use_ma60_safe": True,    
    "ma60_min": 100.0,        
    "ma60_max": 120.0,        
    "use_market_filter": True, 
    "use_macd_signal": True,   
    "macd_fast": 12,           
    "macd_slow": 26,           
    "macd_sig": 9,             
    "rsi_sig_period": 9,       
    "max_mon_targets": 20,     
    "use_auto_virtual": True,  # 매수 타점 포착 시 자동 가상 매수 여부
    "flash_exit_pct": 3.0,     
    "gap_down_limit": -5.0,    
    "stage1_amt": 1000000,      
    "stage2_amt": 2000000,     
    "use_s2_ma5": True,        
    "s2_ma5_gap": 0.0,         
    "use_s2_profit": True,     
    "s2_profit_limit": 1.0,    
    "use_s2_osc": True,        
    "s2_osc_gap": 0.0,         
    "use_s2_rsi_sig": True,   
    "s2_rsi_gap": 0.0,         
    "use_s2_macd_gc": True,   
    "s2_macd_gap": 0.0,        
    "use_s2_vol_surge": True, 
    "s2_vol_ratio": 1.2,       
    "use_s2_ma20": True,      
    "s2_ma20_gap": 0.0,        
    "s1_a_flash_exit_pct": 3.0,
    "s1_b_flash_exit_pct": 1.5,
    "s1_a_ma_break_buffer": 1.0,
    "s1_b_ma_break_buffer": 0.5,
    "s1_a_stop_loss": 3.0,
    "s1_b_stop_loss": 2.0,
    "s1_a_take_profit": 5.0,
    "s1_b_take_profit": 4.0,
    "use_s1_flash_exit": True,
    "use_s2_flash_exit": True,
    "s2_flash_exit_pct": 3.0,
    "use_s2_rsi_tp": True,
    "s2_rsi_tp_limit": 70.0,   
    "s2_rsi_tp_drop": 1.0,     
    "use_s2_rsi_exit": True,
    "rsi_exit_limit": 50.0,
    "use_s1_ma_break": True,
    "s1_ma_break_buffer": 1.0,
    "use_s2_ma_break": True,
    "s2_ma_break_buffer": 1.0,
    "use_s1_stop_loss": True,
    "use_s2_stop_loss": True,
    "use_stop_loss": True,
    "stop_loss": 3.0,
    "s2_stop_loss": 2.0,
    "s2_min_days": 1,
    "stage2_trigger": 1.0,
    "take_profit": 5.0, # Legacy for simple backtester
    "s2_take_profit": 5.0,
    "use_s1_tp": True,
    "use_trailing_stop": True  
}

class KiwoomApp:
    @property
    def access_token(self):
        return self.auth_manager.access_token

    @access_token.setter
    def access_token(self, value):
        self.auth_manager.access_token = value
        
    def __init__(self, root):
        self.root = root
        self.root.title("Antigravity 스윙매매 전략 플랫폼 v3.0 (Kiwoom REST)")
        self.root.geometry("1400x900")
        
        # [v3.0] 핵심 매니저 초기화
        self.auth_manager = AuthManager(logger_callback=self.log)
        self.config_handler = ConfigHandler(logger_callback=self.log)
        self.data_manager = DataManager()
        
        # 설정 로드 (다른 매니저들이 참조할 수 있도록 우선 로드)
        self.settings = self.config_handler.load_settings(DEFAULT_SETTINGS)
        
        self.stream_processor = StreamProcessor(self.settings, logger_callback=self.log)
        self.gui_utils = GuiUtils(root=self.root)
        
        # UI 요소 선언 호출
        self._init_attributes()
        
        # 설정 로드 및 캐시 로드
        self.bars_data = self.config_handler.load_bars_cache() 
        
        # UI 레이아웃
        self._setup_ui()
        
    def _init_attributes(self):
        # 속성 초기화
        self.settings_file = "settings.json"
        self.realtime_data = {} 
        self.monitor_thread = None
        self.is_monitoring = False
        self.monitor_codes = []
        self.monitor_info = {}
        self.volume_profiles = {} 
        self.min_volume_profiles = {} 
        self.bars_data = {}       
        self.cache_file = "bars_cache.json"
        self.debug_packet_logged = False
        self._last_parse_error_log = 0.0
        self.pending_tree_updates = {} 
        self._update_timer_active = False
        self._total_ticks = 0
        self._diag_logged = False
        self.rsi_signals = {}     
        self.buy_signals = {}     
        self.minute_volume = {}   
        self.progress_var = tk.DoubleVar()
        self.lbl_market_indices = None
        self.market_status = {}
        self.active_blinks = {} # [v3.1] 현재 깜빡이는 중인 행 관리

        # UI 요소 선언 (Linter 대응)
        self.notebook = None
        self.tab_dashboard = None
        self.tab_settings = None
        self.tab_monitor = None
        self.status_label = None
        self.update_kospi_var = None
        self.update_theme_var = None
        self.btn_auth = None
        # 가상 포지션 관리 (v3.0)
        self.virtual_positions = {} 
        self.virtual_history = []    
        self.tree_virtual = None     
        # 백테스팅 (v5.0)
        self.tab_backtest = None
        self.tree_backtest = None
        self.backtest_entries = {}
        self.backtest_force_refresh_var = tk.BooleanVar(value=False)
        self.backtest_high_precision_var = tk.BooleanVar(value=False)
        self.base_backtest_settings = DEFAULT_SETTINGS.copy()
        
        # [v8.5] 온디맨드 리포트용 데이터 보관
        self.last_backtest_trades = None
        self.last_backtest_market = None
        self.last_backtest_vix = None
        self.last_backtest_settings = None
        self.last_backtest_interval = "15m"
        self.last_backtest_start = None  
        self.last_backtest_end = None    
        
        self.btn_basics_update = None
        self.btn_select = None
        self.log_area = None
        self.lbl_monitor_status = None
        self.lbl_criteria = None
        self.tree_monitor = None
        self.btn_monitor_toggle = None
        self.params_entries = {}
        self.settings_status_label = None
        
    def _setup_ui(self):
        """전체 UI 초기화 및 메인 탭 구성"""
        # [v5.1] 'Nordic Professional' 대비 강화 및 버튼 가시성 최적화
        self.colors = {
            'bg': '#2E3440',      # 전체 배경 (Polar Night)
            'card': '#3B4252',    # 카드/탭 배경 (Lighter Slate)
            'fg': '#ECEFF4',      # 기본 텍스트 (Snow Storm - 고대비 강화)
            'primary': '#88C0D0', # 포인트 컬러 (Frost Blue)
            'secondary': '#81A1C1', # 보조 컬러 (Slate Blue)
            'success': '#A3BE8C', # 상승/성공 (Moss Green)
            'danger': '#BF616A',  # 하락/위험 (Aurora Red)
            'border': '#4C566A',  # 경계선
            'white': '#FFFFFF'    # 완벽한 백색
        }

        style = ttk.Style()
        style.theme_use('clam')
        
        # 메인 배경 및 전연 폰트 (고급스러운 Outfit/Inter 느낌의 Malgun Gothic)
        self.root.configure(bg=self.colors['bg'])
        style.configure(".", font=("Malgun Gothic", 9), background=self.colors['bg'], foreground=self.colors['fg'])
        
        # Notebook 스타일 - 탭 구분 확실히 강화
        style.configure("TNotebook", background=self.colors['bg'], borderwidth=0)
        style.configure("TNotebook.Tab", padding=[22, 12], font=("Malgun Gothic", 10, "bold"), 
                        background="#434C5E", foreground="#81A1C1", borderwidth=1, bordercolor=self.colors['bg'])
        style.map("TNotebook.Tab", 
                  background=[("selected", self.colors['card'])],
                  foreground=[("selected", self.colors['primary'])])

        # LabelFrame 스타일 (Thin Border & Soft Accent)
        style.configure("TLabelframe", background=self.colors['card'], bordercolor=self.colors['border'], 
                        relief=tk.FLAT, borderwidth=1)
        style.configure("TLabelframe.Label", font=("Malgun Gothic", 10, "bold"), 
                        foreground=self.colors['primary'], background=self.colors['card'])

        # 버튼 스타일 - 좀 더 버튼답게 (보더 및 입체감 강화)
        style.configure("TButton", padding=[14, 8], font=("Malgun Gothic", 9, "bold"),
                        background="#4C566A", foreground=self.colors['fg'], borderwidth=1, relief=tk.RAISED)
        style.map("TButton", 
                  background=[("active", "#5E81AC"), ("pressed", "#434C5E")],
                  relief=[("pressed", tk.FLAT)])
        
        style.configure("Action.TButton", foreground=self.colors['bg'], background=self.colors['primary'], borderwidth=1)
        style.map("Action.TButton", 
                  background=[("active", "#B3D9E6"), ("disabled", "#4C566A")])
        
        style.configure("Success.TButton", foreground=self.colors['bg'], background=self.colors['success'], borderwidth=1)
        style.map("Success.TButton", 
                  background=[("active", "#B4CF9E")])
        
        style.configure("Accent.TButton", foreground=self.colors['bg'], background=self.colors['primary'], borderwidth=1)
        style.map("Accent.TButton", 
                  background=[("active", "#B3D9E6"), ("disabled", "#4C566A")])
                  
        style.configure("Toggle.TButton", foreground=self.colors['fg'], background=self.colors['danger'], borderwidth=1)
        style.map("Toggle.TButton", 
                  background=[("active", "#D08770")])
                  
        style.configure("Subtle.TButton", foreground=self.colors['secondary'], background=self.colors['card'], borderwidth=1)
        style.map("Subtle.TButton", 
                  background=[("active", self.colors['bg'])],
                  foreground=[("active", self.colors['primary'])])

        # 트리뷰 스타일 - 가독성 강화
        style.configure("Treeview", background="#242933", foreground=self.colors['fg'], 
                        fieldbackground="#242933", rowheight=32, borderwidth=0)
        style.configure("Treeview.Heading", background="#434C5E", foreground=self.colors['primary'], 
                        font=("Malgun Gothic", 9, "bold"), borderwidth=1, relief=tk.FLAT)
        style.map("Treeview", 
                  background=[("selected", "#88C0D0")], 
                  foreground=[("selected", "#2E3440")])

        # [v5.2] 입력 위젯 스타일 (Entry, Spinbox, Combobox) - 노르딕 스타일
        style.configure("TEntry", fieldbackground="#242933", foreground=self.colors['fg'], 
                        insertcolor=self.colors['primary'], borderwidth=1, relief=tk.FLAT)
        style.map("TEntry", fieldbackground=[("readonly", "#2E3440")], foreground=[("readonly", "#81A1C1")])
        
        style.configure("TCheckbutton", background=self.colors['bg'], foreground=self.colors['fg'], font=("Malgun Gothic", 10))
        style.map("TCheckbutton", background=[("active", self.colors['bg'])], foreground=[("active", self.colors['primary'])])

        # 스크롤바 스타일링 (Nord 테마 일관성 유지)
        style.layout("Vertical.TScrollbar", 
                     [('Vertical.Scrollbar.trough', {'children': [('Vertical.Scrollbar.thumb', {'expand': '1', 'sticky': 'nswe'})], 'sticky': 'ns'})])
        style.configure("Vertical.TScrollbar", gripcount=0, background="#434C5E", 
                        troughcolor=self.colors['bg'], bordercolor=self.colors['bg'], arrowcolor="#81A1C1", thickness=12)
        style.map("Vertical.TScrollbar", background=[("active", "#4C566A")])
        
        # 메인 탭 구성 (Notebook)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.tab_dashboard = ttk.Frame(self.notebook)
        self.tab_settings = ttk.Frame(self.notebook)
        self.tab_monitor = ttk.Frame(self.notebook)  
        self.tab_virtual = ttk.Frame(self.notebook) 
        self.tab_backtest = ttk.Frame(self.notebook) # v5.0 New Tab
        
        self.notebook.add(self.tab_dashboard, text=" 📊 대시보드 ")
        self.notebook.add(self.tab_monitor, text=" 👁️ 실시간 감시 ")
        self.notebook.add(self.tab_virtual, text=" 💰 가상 포지션 ") 
        self.notebook.add(self.tab_backtest, text=" 📈 백테스팅 ") # v5.0
        self.notebook.add(self.tab_settings, text=" ⚙️ 전략 설정 ")

        # --- [1] 대시보드 탭 구성 ---
        self.dashboard_tab = DashboardTab(self.tab_dashboard, self)
        # [v3.0] 로그 유틸리티 영역 연결 (DashboardTab에서 이미 self.log_area를 설정함)
        if hasattr(self, 'log_area'):
            self.gui_utils.log_area = self.log_area
        
        # --- [2] 실시간 감시 탭 구성 ---
        self.monitor_tab = MonitorTab(self.tab_monitor, self)

        # --- [3] 전략 설정 탭 구성 ---
        self.settings_tab = SettingsTab(self.tab_settings, self, DEFAULT_SETTINGS)

        # --- [4] 가상 포지션 탭 구성 ---
        self.virtual_tab = VirtualTab(self.tab_virtual, self)

        # --- [5] 백테스팅 탭 구성 ---
        self.backtest_tab = BacktestTab(self.tab_backtest, self)
        
        # 상태바 (시장 지수)
        status_frame = ttk.Frame(self.root)
        status_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=2)
        
        self.lbl_market_indices = ttk.Label(status_frame, text="📊 시장 지수: 로딩 중...", font=("Helvetica", 10))
        self.lbl_market_indices.pack(side=tk.LEFT, padx=5)
        
        self.status_label = ttk.Label(status_frame, text="상태: 대기 중", foreground="blue")
        self.status_label.pack(side=tk.RIGHT, padx=5)
        
        # [v3.0] 로그 유틸리티 영역 연결 (실제 위젯 연결)
        self.gui_utils.log_area = self.log_area
        
        self.log("🚀 프로그램이 시작되었습니다.")
        
        # 시작 시 시장 지수 업데이트 및 루프 시작
        threading.Thread(target=self._fetch_indices_loop, daemon=True).start()
        
        # 실행 시 자동으로 토큰 발급 시도
        threading.Thread(target=self.get_auth_token, daemon=True).start()


    def _final_save_settings(self):
        """설정 파일에 최종 저장"""
        self.config_handler.save_settings(self.settings)

    def load_settings(self):
        self.settings = self.config_handler.load_settings(DEFAULT_SETTINGS)

    def save_settings(self):
        return self.config_handler.save_settings(self.settings)

    def _load_bars_cache(self):
        self.bars_data = self.config_handler.load_bars_cache()

    def _save_bars_cache(self):
        return self.config_handler.save_bars_cache(self.bars_data or {})

    def _update_criteria_label(self):
        if self.lbl_criteria:
            self.lbl_criteria.config(text=self._get_criteria_text(format_type="ui"))

    def _get_criteria_text(self, format_type="ui"):
        """[v3.0] GuiUtils를 통해 조건 텍스트 생성"""
        return self.gui_utils.get_criteria_text(self.settings, format_type)
    def log(self, message):
        """[v3.0] GuiUtils를 통해 고성능 로깅 수행"""
        self.gui_utils.log(message)

    def get_auth_token(self):
        """[v3.0] AuthManager를 통해 토큰 발급"""
        token = self.auth_manager.get_auth_token()
        if token:
            if self.status_label:
                self.root.after(0, lambda: self.status_label.config(text="상태: 로그인 완료", foreground="green"))
            # 로그인 성공 시 자동으로 KOSPI 200 종목 리스트 갱신 시작
            threading.Thread(target=lambda: self.get_kospi_200(force=True), daemon=True).start()

    # [v4.0] 대시보드 상단 카드 제거로 인한 관련 업데이트 메서드 삭제

    def get_kospi_200(self, force=False):
        self.log(f"\n[기초] KOSPI 200 리스트 수집/확인 중...")
        df_sorted = self.data_manager.get_kospi_200_list(force=force)
        if df_sorted is not None:
            self.log(f"✅ {len(df_sorted)}개 종목을 로드했습니다.")
            # [v4.0] 데이터 로드 완료 후 종목 선정 버튼 활성화
            if self.btn_select:
                self.root.after(0, lambda: self.btn_select.config(state=tk.NORMAL))
        self.log("=========================================================\n")

    def start_selection_thread(self):
        if self.btn_select: self.btn_select.config(state=tk.DISABLED)
        threading.Thread(target=self.select_monitoring_targets, daemon=True).start()

    def select_monitoring_targets(self):
        """[v2.9] 선정 로직을 strategy_selector 모듈로 이관하여 호출"""
        try:
            df_result = strategy_selector.select_monitoring_targets(
                settings=self.settings,
                logger_callback=self.log
            )
            
            if df_result is not None and not df_result.empty:
                self._display_selected_targets(df_result)
            
            # 버튼 상태 복구
            self.root.after(0, lambda: self.btn_select.config(state=tk.NORMAL) if self.btn_select else None)
            
        except Exception as e:
            self.log(f"❌ 종목 선정 중 예외 발생: {e}")
            self.root.after(0, lambda: self.btn_select.config(state=tk.NORMAL) if self.btn_select else None)

    def _fetch_indices_loop(self):
        """배경에서 2분마다 코스피/코스닥 지수 정보를 갱신하는 루프"""
        while True:
            try:
                self._update_market_indices()
            except Exception as e:
                print(f"[ERROR] Market Index Fetch Error: {e}")
            time.sleep(120) # 2분 주기로 갱신

    def _update_market_indices(self):
        """FinanceDataReader를 사용하여 시장 지수(KOSPI, KOSDAQ)를 조회하고 UI를 업데이트"""
        res_dict = self.data_manager.get_market_indices()
        results = []
        for name, data in res_dict.items():
            self.market_status[name] = data
            price, ratio, trend = data['val'], data['ratio'], data['trend']
            sign = "▲" if ratio > 0 else "▼"
            results.append(f"{name}: {price:,.2f} ({sign}{abs(ratio):.2f}%)")
                
        if results:
            status_text = " | ".join(results)
            # [v4.0] 하단 상태바 지수 업데이트
            if self.lbl_market_indices:
                self.root.after(0, lambda: self.lbl_market_indices.config(text=f"📊 시장 지수: {status_text}"))
                
            m_trend = "🟢순풍" if all(v['trend']=="UP" for v in self.market_status.values()) else "⚠️역풍"
            if not hasattr(self, '_last_m_trend') or self._last_m_trend != m_trend:
                self._last_m_trend = m_trend
                self.log(f"ℹ️ [시장] 현재 지수 추세: {m_trend}")

    def _on_realtime_data_received(self, code, now_price, volume, trde_val):
        """MonitorTab에서 호출되는 실시간 데이터 처리 본체"""
        try:
            if code not in self.monitor_info: return
            info = self.monitor_info[code]
            
            # 1. 지표 재계산 및 봉 데이터 업데이트
            self._update_realtime_bar(code, now_price, volume)
            
            # 2. 실시간 지표 추출 (RSI, MACD 등)
            rsi_val = self._calculate_realtime_rsi(code)
            
            # 3. 추가 데이터 (로그 등에서 필요한 값들)
            open_price = info.get('open', 0)
            if open_price == 0: 
                open_price = now_price
                info['open'] = open_price # 첫 체결가를 시가로 고정
            
            diff_val = now_price - open_price
            ratio_val = f"{(diff_val / open_price * 100):+.2f}" if open_price else "0.00"
            
            # 4. 시그널 상태 및 태그 결정
            tag = ""
            ma60_ratio = 0.0
            
            # RSI 신호 로직
            s_state_raw = self.rsi_signals.get(code, {'last_rsi': 0.0, 'signal_time': None})
            last_rsi_f = float(s_state_raw.get('last_rsi', 0.0))
            now_dt = datetime.now()
            
            if last_rsi_f > 0 and last_rsi_f < 50.0 and rsi_val >= 50.0:
                s_state_raw['signal_time'] = now_dt
                # self.log(f"🚀 [SIGNAL ON] {info['name']}({code}) RSI 50 상향 돌파! (Now: {rsi_val:.1f})") # 삭제: 지표별 알림 최소화
            
            if s_state_raw['signal_time']:
                if rsi_val < 50.0:
                    s_state_raw['signal_time'] = None
                elif (now_dt - s_state_raw['signal_time']).total_seconds() > self.settings.get('rsi_window', 45) * 60:
                    s_state_raw['signal_time'] = None
                else:
                    tag = "rsi_signal"
            
            s_state_raw['last_rsi'] = float(rsi_val)
            self.rsi_signals[code] = s_state_raw

            # 5. 통합 전략 기반 매수 신호 포착
            is_buy = False
            rsi_sig, macd_val, osc_val = 0.0, 0.0, 0.0
            if code in self.bars_data and len(self.bars_data[code]) >= 60:
                df_with_ind = TradingStrategy.calculate_indicators(self.bars_data[code], self.settings)
                market_trend = 'UP' if all(v.get('trend') == "UP" for v in self.market_status.values()) else 'DOWN'
                all_signals = TradingStrategy.get_buy_signals(df_with_ind, self.settings, market_trend)
                is_buy = all_signals.iloc[-1]
                
                last_row = df_with_ind.iloc[-1]
                rsi_sig = last_row.get('RSI_Sig', 0)
                macd_val = last_row.get('MACD', 0)
                osc_val = last_row.get('OSC', 0)
                ma60_ratio = (now_price / last_row['MA60']) * 100 if 'MA60' in last_row and last_row['MA60'] > 0 else 0.0
                
                if is_buy:
                    tag = "buy_signal"
                    self.log(f"🎯 [BUY SIGNAL] {info['name']}({code}) 통합 전략 타점 포착!")
                    
                    # [v6.0] 자동 가상 매수 집행
                    if self.settings.get('use_auto_virtual', True) and code not in self.virtual_positions:
                        self._virtual_buy_stage_1(code, info['name'], now_price)
                elif rsi_val > rsi_sig:
                    tag = "rsi_signal"
                elif (100.0 <= ma60_ratio <= 112.0) or (last_row['MA20'] > last_row['MA60'] and now_price > last_row['MA20']):
                    # Type A(Safe Zone) 또는 Type B(정배열 상승) 대기태그 설정
                    tag = "highlight_green"

            # 6. 볼륨 스파이크 체크 및 블링크(깜빡임) 효과 설정 [v3.1]
            is_vol_spike = self._check_volume_spike(code, volume - info.get('last_vol', volume))
            info['last_vol'] = volume
            
            is_price_changed = False
            last_p = info.get('last_price', 0)
            if now_price != last_p:
                is_price_changed = True
                info['last_price'] = now_price

            # 7. 트리뷰 값 업데이트 준비
            gc_status = "🟢" if (code in self.bars_data and len(self.bars_data[code]) >= 20 and self.bars_data[code]['ma5'].iloc[-1] >= self.bars_data[code]['ma20'].iloc[-1]) else "⚪"
            pwr_status = f"{info.get('vol_power', 100):.0f}"
            spk_status = "🚀급증" if is_vol_spike else "-"
            ma60_status = f"✅{ma60_ratio:.1f}%" if 100 <= ma60_ratio <= 112 else f"{ma60_ratio:.1f}%"
            
            rsi_display = f"{rsi_val:.1f} ({rsi_sig:.1f})"
            macd_display = f"{macd_val:.1f}({osc_val:+.1f})"
            
            if not tag:
                if diff_val > 0: tag = "price_up"
                elif diff_val < 0: tag = "price_down"
                
            full_values = (
                f"{info.get('score', 0.0)}", code, info['name'],
                f"{int(open_price):,}", f"{int(now_price):,}", f"{int(diff_val):+d}", f"{ratio_val}%",
                f"{int(volume):,}", f"{trde_val:,}억"
            )
            
            self.pending_tree_updates[code] = (full_values, tag, is_price_changed)
            if not self._update_timer_active:
                self._update_timer_active = True
                self.root.after(200, self._render_tree_updates)

        except Exception as e:
            if not getattr(self, '_last_realtime_err', None) == str(e):
                print(f"[DEBUG] Realtime Process Error ({code}): {e}")
                self._last_realtime_err = str(e)

    async def _ws_client_loop(self):
        """MonitorTab으로 위임된 웹소켓 루프"""
        await self.monitor_tab._ws_client_loop()

    def _ws_worker(self):
        """웹소켓 비동기 루프를 실행하기 위한 헬퍼 스레드 함수"""
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(self._ws_client_loop())
        except Exception as e:
            print(f"[DEBUG] WS Worker Error: {e}")

    def _get_time_ratio(self):
        now = datetime.now()
        start = now.replace(hour=9, minute=0, second=0, microsecond=0)
        end = now.replace(hour=15, minute=30, second=0, microsecond=0)
        if now < start: return 0.001
        if now > end: return 1.0
        return min(1.0, (now - start).total_seconds() / 23400.0)

    def _render_tree_updates(self):
        """대기 중인 모든 트리뷰 업데이트를 한 번에 처리 (성능 최적화)"""
        if not self.tree_monitor or not self.is_monitoring: 
            self._update_timer_active = False
            return
        
        try:
            if self.pending_tree_updates:
                items = list(self.pending_tree_updates.items())
                self.pending_tree_updates.clear()
                
                for iid, (values, tag, is_blink) in items:
                    if self.tree_monitor.exists(iid):
                        # 블링크 효과 적용 [v3.1]
                        if is_blink:
                            current_tags = list(self.tree_monitor.item(iid, 'tags'))
                            if "blink" not in current_tags:
                                current_tags.append("blink")
                                self.tree_monitor.item(iid, values=values, tags=tuple(current_tags))
                                # 400ms 후 블링크 제거
                                self.root.after(400, lambda i=iid: self._remove_blink(i))
                            else:
                                # 이미 블링크 중이면 값만 업데이트
                                self.tree_monitor.item(iid, values=values)
                        else:
                            self.tree_monitor.item(iid, values=values, tags=(tag,))
                
        except Exception as e:
            print(f"[DEBUG] Render error: {e}")
        finally:
            self.root.after(300, self._render_tree_updates)

    def _remove_blink(self, iid):
        """[v3.1] 특정 행의 블링크(하이라이트) 태그 제거"""
        try:
            if self.tree_monitor.exists(iid):
                current_tags = list(self.tree_monitor.item(iid, 'tags'))
                if "blink" in current_tags:
                    current_tags.remove("blink")
                    self.tree_monitor.item(iid, tags=tuple(current_tags))
        except: pass

    def _update_tree_item(self, iid, values, tag):
        pass # 더 이상 개별 업데이트를 직접 호출하지 않음

    def toggle_monitoring(self):
        """실시간 감시 시작/중단을 하나의 버튼으로 제어 (Toggle)"""
        if not getattr(self, 'is_monitoring', False):
            self.start_monitoring()
        else:
            self.stop_monitoring()

    def start_monitoring(self):
        if self.monitor_thread and self.monitor_thread.is_alive(): return
        target_cache = "selected_targets.csv"
        if not os.path.exists(target_cache):
            messagebox.showwarning("경고", "먼저 종목 선정을 수행해주세요.")
            return
        df_targets = pd.read_csv(target_cache, dtype={'Code': str})
        
        # 필수 지표 컬럼 확인 (v1.9 강화)
        required_cols = ['MA20', 'MA60', 'BB_Upper', 'PrevVol', 'Price', 'Ratio', 'MA60Ratio']
        missing_cols = [c for c in required_cols if c not in df_targets.columns]
        if missing_cols:
            self.log(f"⚠️ [주의] 선정 파일에 지표({', '.join(missing_cols)})가 없습니다.")
            messagebox.showwarning("필수 지표 누락", "v1.9 버전에 필요한 데이터가 선정 파일에 없습니다.\n\n[3. 전략 종목 선정] 버튼을 눌러 먼저 데이터를 갱신해 주세요.")
            return

        self.monitor_codes = df_targets['Code'].tolist()
        self.monitor_info = {}
        # t_map = self._load_theme_map() # Removed
        
        if self.tree_monitor:
            for item in self.tree_monitor.get_children(): self.tree_monitor.delete(item)

        for _, row in df_targets.iterrows():
            c = str(row['Code']).zfill(6)
            name = row.get('Name', 'Unknown')
            # 기본 정보 저장 (실시간 업데이트용)
            self.monitor_info[c] = {
                'name': name, 
                'ma20': float(row.get('MA20', 0)), 
                'ma60': float(row.get('MA60', 0)),
                'bb_upper': float(row.get('BB_Upper', 0)), 
                'prev_vol': float(row.get('PrevVol', 0)),
                # 'themes': ", ".join(list(t_map.get(c, []))[:2]), # Removed
                'score': row.get('Score', 0.0),
                'ma_buffer_scale': float(row.get('MABufScale', 1.0)),
                'open': float(row.get('Open', 0)),
                'last_price': float(row.get('Price', 0)) if row.get('Price') else 0
            }
            
            # 트리뷰 즉시 채우기 (시장 종료 시에도 숫자가 보이도록)
            if self.tree_monitor:
                price = row.get('Price', 0)
                open_p = row.get('Open', 0)
                diff = row.get('Diff', 0)
                ratio = row.get('Ratio', 0)
                rsi = row.get('RSI', 0)
                ma20_d = row.get('MA20Dist', 0)
                ma60_r = row.get('MA60Ratio', 0)
                v_max = row.get('VolMax', 0)
                vol = row.get('Volume', 0)
                trde_v = row.get('AvgTrdeVal', 0)
                
                bb_status = "🔥돌파" if price and price >= float(row.get('BB_Upper', 0)) else "-"
                tag = "highlight_green" if 100 <= ma60_r <= 112 else ""
                
                # 5대 신호 초기화 (Initial view)
                gc_i = "⚪"
                rsi_i = f"🔥{rsi:.1f}" if rsi >= 45 else f"{rsi:.1f}" # RSI는 45만 넘어도 경계주황? (임시)
                pwr_i = "-"
                spk_i = "-"
                ma_i = f"✅{ma60_r:.1f}%" if 100 <= ma60_r <= 112 else f"{ma60_r:.1f}%"

                # 컬럼 순서: Score, Code, Name, GC, RSI, Power, Spike, MA60, NXT, Open, Price, Diff, Ratio, Volume, TrdeVal
                vals = (
                    f"{row.get('Score', 0.0)}", c, name,
                    f"{int(open_p):,}" if open_p else "", f"{int(price):,}" if price else "",
                    f"{int(diff):+d}" if diff else "", f"{ratio:+.2f}%" if ratio else "",
                    f"{int(vol):,}" if vol else "", f"{trde_v}억"
                )
                self.tree_monitor.insert("", tk.END, iid=c, values=vals, tags=(tag,))
        
        if self.lbl_monitor_status: 
            self.root.after(0, lambda: self.lbl_monitor_status.config(text=f"상태: 분석 중 ({len(self.monitor_codes)}종목)", foreground="blue") if self.lbl_monitor_status else None)
        
        # 분봉 데이터 기반 프로필 사전 계산 및 실시간 감시 시작
        def start_sequence():
            self.log("\n📊 [4단계] 분봉 데이터 분석 및 프로필 최적화 시작...")
            self._prepare_volume_profiles()
            
            self.is_monitoring = True
            if self.btn_monitor_toggle: 
                self.root.after(0, self.btn_monitor_toggle.config, {"text": "⏹️ 감시 중단", "style": "Toggle.TButton"})
            if self.lbl_monitor_status: self.root.after(0, self.lbl_monitor_status.config, {"text": f"상태: 연결 중 ({len(self.monitor_codes)}종목)", "foreground": "blue"})
            
            self.monitor_thread = threading.Thread(target=self._ws_worker, daemon=True)
            self.monitor_thread.start()
            
        threading.Thread(target=start_sequence, daemon=True).start()
    def _prepare_volume_profiles(self):
        """캐시 로드 및 fresh API 데이터를 병합하여 프로필 빌드 (v2.4)"""
        token_manager = TokenManager()
        token = token_manager.get_token()
        if not token:
            self.log("❌ [ERROR] 토큰 획득 실패. 캐시 데이터만 사용합니다.")
            return
        
        self.data_manager.prepare_volume_profiles(self.monitor_codes, token, self.bars_data, self.volume_profiles, self.min_volume_profiles)
        
        self.log(f"✅ {len(self.volume_profiles)}/{len(self.monitor_codes)}개 종목 분석 완료 (캐시 병합됨)")
        self.config_handler.save_bars_cache(self.bars_data) 



    def _update_realtime_bar(self, code, price, volume):
        self.bars_data = self.stream_processor.update_realtime_bar(code, price, volume, self.bars_data or {})

    def _check_volume_spike(self, code, current_vol):
        """[v2.4] 실시간 거래량 급증 탐지 (1분 평균 대비 3배 이상 등)"""
        if not hasattr(self, 'min_volume_profiles') or code not in self.min_volume_profiles:
            return False
        
        # 현재 시간(HHMM) 기준 1분 평균 거래량 확인
        now_hm = datetime.now().strftime('%H%M')
        avg_min_vol = self.min_volume_profiles[code].get(now_hm, 0)
        
        if avg_min_vol <= 0: return False
        
        # 1분 평균의 3배 이상 대량 거래 발생 시 True
        threshold = avg_min_vol * 3.0
        return current_vol >= threshold


    def _calculate_realtime_rsi_from_df(self, df, period=14):
        return self.stream_processor.calculate_indicators(df, period)

    def _calculate_realtime_rsi(self, code, period=14):
        """이력 저장 로직을 StreamProcessor의 결과를 바탕으로 수행"""
        df = self.bars_data.get(code)
        rsi_val, rsi_sig, macd_val, osc_val = self.stream_processor.calculate_indicators(df, period)
        
        if df is not None and not df.empty:
            idx = df.index[-1]
            if 'rsi_history' not in df.columns: df['rsi_history'] = 0.0
            if 'macd' not in df.columns: df['macd'] = 0.0
            if 'osc' not in df.columns: df['osc'] = 0.0
            
            df.at[idx, 'rsi_history'] = rsi_val
            df.at[idx, 'macd'] = macd_val
            df.at[idx, 'osc'] = osc_val
            
        return rsi_val

    def _recalculate_all_indicators(self, code):
        """전략 셀렉터 유틸 대신 StreamProcessor나 Strategy 클래스 활용 고려 가능하나 현재는 로직 유지"""
        df = self.bars_data.get(code)
        if df is None or df.empty: return
        
        try:
            # (기존 재계산 로직 유지)
            rsi_series = strategy_selector.calculate_rsi(df['c'], 14)
            df['rsi_history'] = rsi_series
            macd_fast, macd_slow, macd_sig = int(self.settings.get('macd_fast', 12)), int(self.settings.get('macd_slow', 26)), int(self.settings.get('macd_sig', 9))
            df['ema_f'] = df['c'].ewm(span=macd_fast, adjust=False).mean()
            df['ema_s'] = df['c'].ewm(span=macd_slow, adjust=False).mean()
            df['macd'] = df['ema_f'] - df['ema_s']
            df['macd_sig'] = df['macd'].ewm(span=macd_sig, adjust=False).mean()
            df['osc'] = df['macd'] - df['macd_sig']
            self.bars_data[code] = df
        except Exception as e:
            print(f"[DEBUG] Indicator Recalc Error ({code}): {e}")

    def stop_monitoring(self):
        self.is_monitoring = False
        self._save_bars_cache() # 중단 시점 데이터 저장
        if hasattr(self, 'btn_monitor_toggle') and self.btn_monitor_toggle: 
            self.btn_monitor_toggle.config(text="▶️ 실시간 감시 시작", style="Accent.TButton")
        if hasattr(self, 'lbl_monitor_status') and self.lbl_monitor_status: self.lbl_monitor_status.config(text="상태: 중단됨", foreground="red")

    def send_telegram_message(self, message):
        self.data_manager.send_telegram_message(message)

    def _display_selected_targets(self, df): # theme_map 인자 제거
        if df.empty: return
        self.log("\n" + "="*125)
        self.log(f"{'No':<3} | {'가점':>5} | {'코드':<6} | {'종목명':<16} | {'현재가':>8} | {'등락률':>7} | {'대금':>4}억 | {'RSI':>5}")
        self.log("-" * 125)
        
        # 텔레그램 메시지 구성
        criteria_text = self._get_criteria_text(format_type="telegram")
        msg = f"🚀 <b>오늘의 스윙 매수 랭킹 (v2.1)</b>\n\n"
        msg += f"<b>[선정 기준]</b>\n{criteria_text}\n\n"
        for i, (_, row) in enumerate(df.iterrows(), 1):
            c = str(row['Code']).zfill(6)
            self.log(f"{i:3d} | {row['Score']:5.1f} | {c:6s} | {row['Name']:16s} | {int(row['Price']):8,d} | {row['Ratio']:>6.1f}% | {row['AvgTrdeVal']:>4d}억 | {row['RSI']:>5.1f}")
            
            # 종목별 상세 스코어/지표 추가
            msg += f"<b>{i}. {row['Name']}</b> ({c}) - <b>{row['Score']}점</b>\n"
            msg += f"📊 현재가: {int(row['Price']):,}원 ({row['Ratio']:+.2f}%)\n"
            msg += f"📈 RSI: {row['RSI']:.1f} | 💰 대금: {row['AvgTrdeVal']}억\n\n"
            
        self.log(f"📤 [텔레그램] {len(df)}개 종목 분석 결과 전송 중...")
        threading.Thread(target=self.send_telegram_message, args=(msg,), daemon=True).start()

    def _render_virtual_positions(self):
        """가상 포지션 화면 갱신 위임 [v3.0]"""
        self.virtual_tab._render_virtual_positions()

    # =========================================================================
    # [v3.0] 가상 포지션 관리 (Virtual Position Monitoring)
    # =========================================================================

    # =========================================================================

    def _virtual_buy_stage_1(self, code, name, price):
        """가상 매수 1차 (20%) 집행"""
        if code in self.virtual_positions:
            messagebox.showwarning("경고", "이미 보유 중인 종목입니다.")
            return

        invest_amt = int(self.settings.get('stage1_amt', 900000))
        qty = invest_amt // price
        if qty == 0: qty = 1
        
        actual_invest = qty * price
        
        self.virtual_positions[code] = {
            "name": name,
            "avg_price": price,
            "qty": qty,
            "stage": 1,
            "invested": actual_invest,
            "entry_time": datetime.now(),
            "max_now": price, # 최고가 추적
            "rsi_part_sold_s2": False,
            "ma_buffer_scale": self.monitor_info.get(code, {}).get('ma_buffer_scale', 1.0)
        }
        
        self.log(f"💰 [가상매수] {name}({code}) 1차 진입 완료. (매수가: {price:,}원, {qty}주)")
        
        # [v6.1] 텔레그램 알림 발송
        msg = f"<b>💰 [가상매수/1차]</b>\n종목: {name}({code})\n가: {price:,}원 / 수: {qty}주\n시간: {datetime.now().strftime('%H:%M:%S')}"
        if hasattr(self, 'data_manager'):
            self.data_manager.send_telegram_message(msg)
            
        self.notebook.select(self.tab_virtual)
        self._render_virtual_positions()

    def _clear_virtual_positions(self):
        if not self.virtual_positions: return
        if messagebox.askyesno("확인", "모든 가상 포지션을 삭제하시겠습니까?"):
            self.virtual_positions.clear()
            self._render_virtual_positions()
            self.log("🗑️ 모든 가상 포지션을 초기화했습니다.")

    def _process_virtual_scenarios(self, code, now_price, rsi_val, macd_val, osc_val, vol_power, is_vol_spike):
        """가상 포지션의 추매/익절/손절 시나리오 자동 체크 (v3.0)"""
        if code not in self.virtual_positions: return
        
        pos = self.virtual_positions[code]
        stage = pos['stage']
        avg_p = pos['avg_price']
        profit_ratio = (now_price / avg_p - 1) * 100
        pos['current_price'] = now_price
        
        # [v3.7] Profit Zone (Big Win) 모드 판단
        is_profit_zone = (stage == 3) or (profit_ratio >= 0.5)
        
        # 0. 최고가 갱신 (Flash Exit/Trailing Stop용)
        if now_price > pos.get('max_now', 0):
            pos['max_now'] = now_price

        # 1. 공통 해지/손절 조건 (Exit) - v4.5 단계별 독립 조건 적용
        is_exit = False
        exit_reason = ""
        
        # [Flash Exit] - 단계별 분리
        use_flash = self.settings.get(f'use_s{stage}_flash_exit', True)
        flash_pct = float(self.settings.get(f's{stage}_flash_exit_pct', 3.0))
        max_now = pos.get('max_now', now_price)
        if use_flash and now_price <= max_now * (1.0 - flash_pct / 100.0):
            is_exit = True
            exit_reason = "FLASH_EXIT"

        # [Gap Down Exit] - 공통 토글 및 파라미터화
        if not is_exit and self.settings.get('use_gap_down', True):
            # 실시간은 시가 변동을 직접 보기는 어려우므로 수익률 기반 갭하락 마지노선 적용
            gap_limit = float(self.settings.get('gap_down_limit', -5.0))
            if profit_ratio <= gap_limit:
                is_exit = True
                exit_reason = "GAP_DOWN_LIMIT"

        # [RSI Exit] - 단계별 분리
        if not is_exit:
            # 1단계 RSI 익절 전량매매 (v4.5)
            if stage == 1 and self.settings.get('use_s1_rsi_win', True):
                rsi_tp_limit = float(self.settings.get('rsi_tp_limit', 70.0))
                if rsi_val >= rsi_tp_limit:
                    is_exit = True
                    exit_reason = "RSI_WIN_FULL"
            
            if not is_exit:
                use_rsi_exit = self.settings.get(f'use_s{stage}_rsi_exit', (stage == 2))
                rsi_limit = float(self.settings.get('rsi_exit_limit' if stage == 2 else f's{stage}_rsi_exit_limit', 50.0))
                if use_rsi_exit and rsi_val < rsi_limit:
                    # 2단계의 경우 수익권(Profit Zone)에서는 무시 옵션 유지
                    if not (stage == 2 and is_profit_zone):
                        is_exit = True
                        exit_reason = "RSI_EXIT"

        # [MA Break Exit] - 단계별 분리
        if not is_exit:
            use_ma_break = self.settings.get(f'use_s{stage}_ma_break', (stage == 2))
            # [v4.4] MA 돌파 완충지대 적용
            ma_base_buffer = float(self.settings.get(f's{stage}_ma_break_buffer' if stage == 1 else 'ma_break_buffer', 1.0)) / 100.0
            ma_buffer = ma_base_buffer * pos.get('ma_buffer_scale', 1.0)
            
            df_bars = self.bars_data.get(code)
            if use_ma_break and df_bars is not None:
                ma20_last = df_bars['ma20'].iloc[-1] if 'ma20' in df_bars.columns else 0
                ma60_last = df_bars['ma60'].iloc[-1] if 'ma60' in df_bars.columns else 0
                
                target_ma = ma20_last if stage <= 2 else ma60_last
                if now_price < target_ma * (1.0 - ma_buffer):
                    # 수익권(Stage 2)은 무시 옵션 유지
                    if not (stage == 2 and is_profit_zone):
                        is_exit = True
                        exit_reason = "MA_BREAK"

        # [Trailing Stop] - 공통 토글 및 단계별 비율 적용
        if not is_exit and self.settings.get('use_trailing_stop', True):
            ts_trigger_pct = 3.0
            ts_drop_pct = 0.96 if stage <= 2 else 0.93
            if profit_ratio >= ts_trigger_pct:
                pos['ts_active'] = True
            if pos.get('ts_active') and now_price <= max_now * ts_drop_pct:
                is_exit = True
                exit_reason = "TRAILING_STOP"

        # [Basic Stop Loss] - 단계별 분리
        if not is_exit and self.settings.get('use_stop_loss', True):
            sl_pct_key = 'stop_loss' if stage == 1 else 's2_stop_loss'
            sl_pct = float(self.settings.get(sl_pct_key, 3.0))
            
            # 2단계에서 RSI 부분익절 후라면 더 타이트하게 (본절가 대응)
            if stage == 2 and pos.get('rsi_part_sold_s2'):
                sl_pct = 1.0 
            
            stop_p = avg_p * (1.0 - sl_pct / 100.0)
            if now_price <= stop_p:
                is_exit = True
                exit_reason = "STOP_LOSS"

        if is_exit:
            self.log(f"🚨 [가상종료/{exit_reason}] {pos['name']}({code}) 탈출 (수익률: {profit_ratio:+.1f}%)")
            
            # [v6.1] 텔레그램 알림 발송
            msg = f"<b>🚨 [가상종료/{exit_reason}]</b>\n종목: {pos['name']}({code})\n수익률: {profit_ratio:+.1f}%\n현재가: {now_price:,}원\n시간: {datetime.now().strftime('%H:%M:%S')}"
            if hasattr(self, 'data_manager'):
                self.data_manager.send_telegram_message(msg)
                
            del self.virtual_positions[code]
            self._render_virtual_positions()
            return

        # 2. 익절 조건 (Take-profit)
        # [v4.5] 2단계: RSI 도달 시 50% 부분 익절
        if stage == 2 and self.settings.get('use_s2_rsi_tp', True):
            s2_rsi_tp_limit = float(self.settings.get('s2_rsi_tp_limit', 75.0))
            if rsi_val >= s2_rsi_tp_limit and not pos.get('rsi_part_sold_s2'):
                sell_qty = pos['qty'] // 2
                if sell_qty > 0:
                    pos['qty'] -= sell_qty
                    pos['invested'] = pos['qty'] * avg_p 
                    pos['rsi_part_sold_s2'] = True
                    self.log(f"✂️ [가상부분익절] {pos['name']}({code}) 2단계 RSI {s2_rsi_tp_limit} 도달 (50% 익절, 나머지는 본절가 대응)")
                    
                    # [v6.1] 텔레그램 알림 발송
                    msg = f"<b>✂️ [가상부분익절]</b>\n종목: {pos['name']}({code})\n대상: 2단계 RSI {s2_rsi_tp_limit} 도달\n내용: 50% 익절 완료 (본절가 대응 시작)"
                    if hasattr(self, 'data_manager'):
                        self.data_manager.send_telegram_message(msg)
                        
                    self._render_virtual_positions()

        # [v4.6] 고정 익절 수익률 체크 (RSI 외 보조 장치)
        if not is_exit:
            tp_pct = float(self.settings.get('take_profit', 5.0))
            if profit_ratio >= tp_pct:
                is_exit = True
                exit_reason = "TAKE_PROFIT_LIMIT"
                self.log(f"💰 [가상종료/익절] {pos['name']}({code}) 탈출 (수익률: {profit_ratio:+.1f}%)")
                del self.virtual_positions[code]
                self._render_virtual_positions()
                return

        if stage == 1:
            # (0) [v5.1] 최소 대기 기간 체크 (만 24시간 기준)
            min_days = int(self.settings.get('s2_min_days', 1))
            if min_days > 0:
                entry_time = pos.get('entry_time')
                if entry_time and (datetime.now() - entry_time) < timedelta(days=min_days):
                    return # 아직 대기 기간 미달
            
            cond_met = True
            
            # (1) 5일선 지지 확인
            if self.settings.get('use_s2_ma5', True) and now_price < pos.get('ma5', 0):
                cond_met = False
            # (2) 수익권 확인 (설정값 기준)
            s2_trigger = float(self.settings.get('stage2_trigger', 1.0))
            if self.settings.get('use_s2_profit', True) and profit_ratio < s2_trigger:
                cond_met = False
            # (3) OSC 확장 확인
            df = self.bars_data.get(code)
            if df is not None and len(df) > 1:
                prev_osc = df['osc'].iloc[-2]
                osc_expanding = osc_val > prev_osc
                if self.settings.get('use_s2_osc', True) and not osc_expanding:
                    cond_met = False
                
                # (4) RSI 강세 확인 (RSI > RSI_Sig)
                rsi_val = df['rsi'].iloc[-1]
                rsi_sig = df['rsi_sig'].iloc[-1] if 'rsi_sig' in df.columns else rsi_val
                if self.settings.get('use_s2_rsi_sig', False) and rsi_val <= rsi_sig:
                    cond_met = False
                
                # (5) MACD 정배열 확인 (MACD > MACD_Sig)
                macd_val = df['macd'].iloc[-1]
                macd_sig = df['macd_sig'].iloc[-1] if 'macd_sig' in df.columns else macd_val
                if self.settings.get('use_s2_macd_gc', False) and macd_val <= macd_sig:
                    cond_met = False
                    
                # (6) [v5.1] 체결강도 강화 조건 (실시간 전용)
                # s2_vol_ratio가 설정되어 있다면 이를 사용, 없으면 기본 120% 적용
                vol_limit = float(self.settings.get('s2_vol_ratio', 1.2)) * 100.0 
                if self.settings.get('use_s2_vol_surge', False) and vol_power < vol_limit:
                    cond_met = False
                    
                # (7) 20일선 위 위치
                ma20 = df['ma20'].iloc[-1] if 'ma20' in df.columns else now_price
                if self.settings.get('use_s2_ma20', False) and now_price <= ma20:
                    cond_met = False

            if cond_met:
                add_amt = int(self.settings.get('stage2_amt', 2100000))
                add_qty = add_amt // now_price
                if add_qty > 0:
                    new_total_qty = pos['qty'] + add_qty
                    new_invested = pos['invested'] + (add_qty * now_price)
                    pos['avg_price'] = new_invested / new_total_qty
                    pos['qty'] = new_total_qty
                    pos['invested'] = new_invested
                    pos['stage'] = 2
                    pos['rsi_part_sold_s2'] = False
                    self.log(f"🔥 [가상추매] {pos['name']}({code}) 2차(본대) 진입! (평단: {pos['avg_price']:.0f}, 모멘텀 확인)")
                    
                    # [v6.1] 텔레그램 알림 발송
                    msg = f"<b>🔥 [가상추매/2차]</b>\n종목: {pos['name']}({code})\n평단: {pos['avg_price']:.0f}원\n수량: {pos['qty']}주\n내용: 모멘텀 포착 본대 진입"
                    if hasattr(self, 'data_manager'):
                        self.data_manager.send_telegram_message(msg)
                        
                    self._render_virtual_positions()

    # =========================================================================
    # [v5.0] 백테스팅 및 파라미터 최적화 (Backtest GUI)
    # =========================================================================

    # Backtest logic moved to gui/backtest_tab.py


if __name__ == "__main__":
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except: pass
    KiwoomApp(root)
    root.mainloop()
