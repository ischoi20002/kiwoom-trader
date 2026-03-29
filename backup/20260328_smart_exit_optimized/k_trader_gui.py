# -*- coding: utf-8 -*-
import tkinter as tk
import typing as tp
from tkinter import ttk, scrolledtext, messagebox, simpledialog
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
import subprocess

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
    import sys
    sys.path.append(os.path.join(os.getcwd(), "gui"))
    from dashboard_tab import DashboardTab
    from monitor_tab import MonitorTab
    from settings_tab import SettingsTab
    from virtual_tab import VirtualTab
    from backtest_tab import BacktestTab

from strategy import indicators, signals
from strategy.simulation_models import SimulatedRealtimeMetrics
from core.auth_manager import AuthManager
from core.config_handler import ConfigHandler
from core.order_manager import OrderManager
from core.stream_processor import StreamProcessor
from core.signal_engine import SignalEngine
from gui.gui_utils import GuiUtils
from gui.styles import NordicStyle

# [v3.8] Core Modules extraction
from core.portfolio_manager import PortfolioManager
from core.market_service import MarketDataService
from core.remote_commander import RemoteCommander

from kiwoom_rest_api.auth.token import TokenManager

# [v3.5] Kiwoom REST API 전역 설정 (모든 임포트보다 최우선)
api_config = None
try:
    import kiwoom_rest_api.config as api_config
    import kiwoom_rest_api.auth.token as api_token
    from config import APP_KEY, APP_SECRET, USE_MOCK
    
    # 1. API 키 설정 (모의투자 시 모의 전용 키 우선 사용)
    m_key = getattr(config, 'MOCK_APP_KEY', "")
    m_sec = getattr(config, 'MOCK_APP_SECRET', "")
    
    if USE_MOCK and m_key:
        api_config.API_KEY = m_key
        api_config.API_SECRET = m_sec
    else:
        api_config.API_KEY = APP_KEY
        api_config.API_SECRET = APP_SECRET
    
    # [v3.2] 환경변수도 동일하게 업데이트 (타 모듈 호환성)
    os.environ['KIWOOM_API_KEY'] = api_config.API_KEY
    os.environ['KIWOOM_API_SECRET'] = api_config.API_SECRET
    
    # 2. 샌드박스(모의투자) 모드 적용
    api_config.USE_SANDBOX = USE_MOCK
    os.environ['KIWOOM_USE_SANDBOX'] = 'true' if USE_MOCK else 'false'
    
    # 3. 토큰 주소 및 베이스 URL 강제 고정 (중요)
    if USE_MOCK:
        fixed_token_url = "https://mockapi.kiwoom.com/oauth2/token"
        api_config.DEFAULT_BASE_URL = "https://mockapi.kiwoom.com"
    else:
        fixed_token_url = "https://api.kiwoom.com/oauth2/token"
        api_config.DEFAULT_BASE_URL = "https://api.kiwoom.com"
        
    api_config.TOKEN_URL = fixed_token_url
    api_token.TOKEN_URL = fixed_token_url 
except:
    pass

DEFAULT_SETTINGS = {
    "min_trde_val": 50.0,
    "sel_rsi_min": 30.0,
    "sel_rsi_max": 70.0,
    "buy_rsi_min": 30.0,
    "buy_rsi_max_b": 70.0,
    "buy_rsi_max_a": 65.0,
    "use_type_a": True,
    "use_type_b": True,
    "vol_surge": 1.5,
    "use_vol_surge": True,
    "vol_surge_b": 2.0,
    "use_type_b_vol": True,
    "use_macd_gc_b": True,
    "use_trend_aligned_b": True,
    "total_capital": 10000000,
    "max_slots": 30,
    "vol_ratio": 1.5,
    "vol_lookback": 10.0,
    "ma_convergence": 5.0,
    "use_gc": True,
    "gc_window": 75,
    "use_rsi_signal": True,
    "rsi_window": 45,
    "use_ma60_safe": True,
    "ma60_min": 100.0,
    "ma60_max": 110.0,
    "use_market_filter": True,
    "use_macd_signal": True,
    "macd_fast": 12.0,
    "macd_slow": 26.0,
    "macd_sig": 9.0,
    "rsi_sig_period": 9.0,
    "use_auto_virtual": True,
    "gap_down_limit": -5.0,
    "stage1_amt": 20.0,
    "stage2_amt": 30.0,
    "use_s2_ma5": True,
    "s2_ma5_gap": 0.0,
    "use_s2_profit": True,
    "s2_profit_limit": 1.0,
    "use_s2_delay": True,
    "s2_delay_hours": 24.0,
    "use_s2_rsi_sig": True,
    "s2_rsi_gap": 0.0,
    "use_s2_macd_gc": True,
    "s2_macd_gap": 0.0,
    "use_s2_vol_surge": True,
    "s2_vol_ratio": 1.5,
    "use_s2_ma20": True,
    "s2_ma20_gap": 0.0,
    "s1_a_flash_exit_pct": 3.0,
    "s1_b_flash_exit_pct": 3.0,
    "s1_a_stop_loss": 3.0,
    "s1_b_stop_loss": 3.0,
    "s1_a_take_profit": 5.0,
    "s1_b_take_profit": 4.0,
    "use_s1_flash_exit": False,
    "use_s2_flash_exit": False,
    "s2_flash_exit_pct": 3.0,
    "use_s1_stop_loss": False,
    "use_s2_stop_loss": False,
    "s2_stop_loss": 3.0,
    "s2_take_profit": 6.0,
    "use_s1_tp": False,
    "use_rsi_min": True,
    "use_s2_tp": False,
    "use_rsi_sig_filter": True,
    "use_gap_grace": True,
    "use_atr_exit": True,
    "use_atr_stop_loss": True,
    "use_atr_trailing_stop": True,
    "atr_period": 100,
    "atr_multiplier_sl": 2.5,
    "atr_multiplier_trail": 5.0,
    "s1_equity_pct": 20.0,
    "s2_equity_pct": 30.0,
    "s1_sizing_unit": "%",
    "s2_sizing_unit": "%",
    "use_smart_buy_a": False,
    "use_smart_buy_b": True,
    "use_smart_buy_s2": True,
    "smart_buy_vp": 125.0,
    "smart_buy_ba_ratio": 1.2,
    "use_smart_exit": True,
    "smart_exit_vp": 90.0,
    "smart_exit_ba_ratio": 1.3,
    "smart_exit_price_threshold": -2.0,
    "use_smart_buy": True,
    "use_true_breakout": True,
    "min_vol_power_entry": 120.0,
    "min_ask_bid_ratio_entry": 1.5,
    "power_sustained_min": 2,
    "smart_exit_bid_ratio": 1.5
}

class KiwoomApp:
    @property
    def access_token(self):
        return self.auth_manager.access_token

    @access_token.setter
    def access_token(self, value):
        self.auth_manager.access_token = value
        
    def __init__(self, root, auto_start=False):
        self.root = root
        self.auto_start = auto_start
        self.root.title("K-Trader v4.0.0 [Remote Monitoring]")
        self.root.geometry("1400x900")
        
        # [v1.3] 로깅 및 기본 매니저들을 최우선으로 초기화 (다른 초기화 중 log 호출 대응)
        self.gui_utils = GuiUtils(root=self.root)
        self.auth_manager = AuthManager(logger_callback=self.log)
        self.config_handler = ConfigHandler(logger_callback=self.log)
        self.data_manager = DataManager()
        self.config = config
        self.nordic_style = NordicStyle(self.root)

        # 나머지 속성 초기화
        self.settings_file = "settings.json"
        self.realtime_data = {} 
        self.monitor_thread = None
        self.is_monitoring = False
        self.executor = None 
        self.monitor_codes = []
        self.monitor_info = {}
        self.volume_profiles = {} 
        self.min_volume_profiles = {} 
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
        self.market_trend = "NEUTRAL"
        self._last_m_trend = None

        self.notebook = None
        self.tab_dashboard = None
        self.tab_settings = None
        self.tab_monitor = None
        self.status_label = None
        self.update_kospi_var = None
        self.update_theme_var = None
        self.btn_auth = None
        
        
        # 기존 가상 포지션 호환성 (Tab 참조용 - 초기화 필요)
        self.virtual_positions = {}
        self.virtual_history = []
        self.virtual_db_file = "virtual_db.json"
        
        
        self.backtest_entries = {}
        self.backtest_force_refresh_var = tk.BooleanVar(value=False)
        self.base_backtest_settings = DEFAULT_SETTINGS.copy()
        self.last_backtest_trades = None
        self.last_backtest_market = None
        self.last_backtest_vix = None
        self.last_backtest_settings = None
        self.last_backtest_interval = "15m"
        self.last_backtest_start = None  
        self.last_backtest_end = None    

        self.tree_virtual = None     
        self.tab_backtest = None
        self.tree_backtest = None
        self.btn_basics_update = None
        self.btn_select = None
        self.log_area = None
        self.lbl_monitor_status = None
        self.lbl_criteria = None
        self.tree_monitor = None
        self.btn_monitor_toggle = None
        self.settings_status_label = None
        self.lbl_remote_status = None 
        self.tree_active_monitor = None
        self.lbl_heartbeat = None
        self.lbl_headwind_caution = None
        self.virtual_tab = None
        self.header_frame: tp.Any = None
        self.tab_virtual: tp.Any = None
        self.monitor_tab: tp.Any = None
        self.lbl_market_status: tp.Any = None
        self.lbl_market_indices: tp.Any = None

        self.settings = self.config_handler.load_settings(DEFAULT_SETTINGS)
        self.bars_data = self.config_handler.load_bars_cache() 
        
        self.stream_processor = StreamProcessor(self.settings, logger_callback=self.log)
        self.order_manager = OrderManager(self) 
        self.signal_engine = SignalEngine(self)

        # [v3.8] Core 매니저 통합 (종속성 확보 후 초기화)
        # 1. 포트폴리오 매니저 (가상 포지션/동기화 전담)
        self.portfolio = PortfolioManager(
            self.data_manager, 
            self.order_manager, 
            settings=self.settings,
            log_func=self.log,
            gui_callback=self._render_virtual_positions,
            telegram_callback=self.send_telegram_message
        )
        # 2. 시장 데이터 서비스 (지수/트렌드 루프 전담)
        self.market_service = MarketDataService(
            self.data_manager,
            log_func=self.log,
            gui_callback=self._on_market_data_update
        )
        # 3. 원격 명령 서비스 (텔레그램 업데이트/재시작 전담)
        self.remote_commander = RemoteCommander(
            self.data_manager,
            log_func=self.log
        )

        # 가상 포지션 데이터 바인딩
        self.virtual_positions = self.portfolio.virtual_positions
        self.virtual_history = self.portfolio.virtual_history
        self.virtual_db_file = self.portfolio.virtual_db_file

        self.portfolio.load_all()
        self._setup_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        # [v1.0] 원격 업데이트 리스너 시작 (5초 주기)
        self.last_update_id = 0
        self.is_updating = False
        self.root.after(5000, self.remote_commander.start) # 로그 출력 제거 (gui_utils 준비성 보장)
        
        # [v3.8] 자동 시작 옵션이 있는 경우 8초 후 실시간 감시 시작
        if self.auto_start:
            self.log("🚀 [Auto] 8초 후 실시간 감시를 자동으로 시작합니다...")
            self.root.after(8000, self.toggle_monitoring)
        
    def _final_save_settings(self):
        """[v3.5] 설정 데이터를 실제로 파일에 저장하고 엔진들에 통보 (Zombie 파라미터 필터링)"""
        # 1. 유효한 키 리스트 구성 (DEFAULT_SETTINGS 기준 + 동적 보존 키)
        valid_keys = set(DEFAULT_SETTINGS.keys())
        extra_keys = {'bt_start_date', 'bt_end_date', 's1_sizing_unit', 's2_sizing_unit', 'mock_account_id'}
        allowed = valid_keys.union(extra_keys)
        
        # 2. 필터링 수행 (더 이상 사용하지 않는 키는 제외)
        filtered_settings = {k: v for k, v in self.settings.items() if k in allowed}
        self.settings = filtered_settings 
        
        if self.config_handler.save_settings(self.settings):
            self.stream_processor.settings = self.settings
            self.log("💿 설정 파일(settings.json) 저장 완료 (유효 파라미터 동기화)")
            return True
        return False

    def reset_ui_tabs(self):
        """설정 초기화 시 UI 탭들을 다시 로드하여 반영"""
        self.settings = self.config_handler.load_settings(DEFAULT_SETTINGS)
        # SettingsTab 등 각 탭이 재생성되거나 내부 변수를 갱신하게 함
        if hasattr(self, 'notebook'):
            # 현재 탭 인덱스 저장
            curr_tab = self.notebook.index("current")
            # 탭 초기화 및 재배치 (가장 확실한 방법)
            for child in self.notebook.winfo_children():
                child.destroy()
            self._setup_ui()
            self.notebook.select(curr_tab)
            self.log("🔄 모든 UI 탭이 초기화된 설정으로 리로드되었습니다.")

    def _setup_ui(self):
        """전체 UI 초기화 및 메인 탭 구성"""
        self.colors = self.nordic_style.colors

        # [v33.3] Notebook 재사용 로직 (reset_ui_tabs 대응)
        if not hasattr(self, 'notebook') or self.notebook is None or not self.notebook.winfo_exists():
            # [v27.1] 상단 전광판 (Market Ticker Header) 추가
            self.header_frame = ttk.Frame(self.root, padding=(15, 8))
            self.header_frame.pack(side=tk.TOP, fill=tk.X)
            
            self.lbl_market_indices = ttk.Label(self.header_frame, text="📊 시장 지수: 데이터 로딩 중...", 
                                              font=("Helvetica", 10, "bold"), foreground=self.colors['primary'])
            self.lbl_market_indices.pack(side=tk.LEFT)
            
            self.notebook = ttk.Notebook(self.root)
            self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=0)
        
        self.tab_dashboard = ttk.Frame(self.notebook)
        self.tab_monitor = ttk.Frame(self.notebook)  
        self.tab_virtual = ttk.Frame(self.notebook) 
        self.tab_backtest = ttk.Frame(self.notebook)
        self.tab_settings = ttk.Frame(self.notebook)
        
        self.notebook.add(self.tab_dashboard, text=" 📊 대시보드 ")
        self.notebook.add(self.tab_monitor, text=" 👁️ 실시간 감시 ")
        self.notebook.add(self.tab_virtual, text=" 💰 가상 포지션 ") 
        self.notebook.add(self.tab_backtest, text=" 📈 백테스팅 ")
        self.notebook.add(self.tab_settings, text=" ⚙️ 전략 설정 ")

        # [v25.8] 탭 전환 시 동기화 연동 바인딩
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.dashboard_tab = DashboardTab(self.tab_dashboard, self)
        if hasattr(self, 'log_area'):
            self.gui_utils.log_area = self.log_area
        
        self.monitor_tab = MonitorTab(self.tab_monitor, self)
        self.settings_tab = SettingsTab(self.tab_settings, self, DEFAULT_SETTINGS)
        self.virtual_tab = VirtualTab(self.tab_virtual, self)
        self.backtest_tab = BacktestTab(self.tab_backtest, self)
        
        status_frame = ttk.Frame(self.root)
        status_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=2)
        
        self.status_label = ttk.Label(status_frame, text="상태: 대기 중", foreground="blue")
        self.status_label.pack(side=tk.RIGHT, padx=5)
        
        self.lbl_remote_status = ttk.Label(status_frame, text="📡 원격: 비활성", foreground="gray")
        self.lbl_remote_status.pack(side=tk.LEFT, padx=10)
        
        self.gui_utils.log_area = self.log_area
        self.log("[System] 화면 구성 및 GUI 모듈 초기화 완료")
        
        # [v33.4] 초기화 시에만 실행되는 스레드들 (재입방지)
        if not hasattr(self, '_initial_threads_started'):
            self._initial_threads_started = True
            # [v27.4] 듀얼 트랙 지수 업데이트 시작
            threading.Thread(target=self._fast_index_price_loop, daemon=True).start()
            threading.Thread(target=self._heavy_index_trend_loop, daemon=True).start()
            threading.Thread(target=self.get_auth_token, daemon=True).start()

    def _on_tab_changed(self, event):
        """[v25.8] 탭 전환 시 호출되는 핸들러"""
        selected_tab = self.notebook.select()
        tab_text = self.notebook.tab(selected_tab, "text")
        
        # 백테스팅 탭으로 전환된 경우 동기화 수행
        if "백테스팅" in tab_text:
            if hasattr(self, 'backtest_tab'):
                self.backtest_tab._sync_opt_toggles_with_settings()

    def _final_save_settings(self):
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
        # [v4.2] UI에서 라벨이 제거되어 더 이상 사용하지 않음 (Stub)
        pass

    def _get_criteria_text(self, format_type="ui"):
        return self.gui_utils.get_criteria_text(self.settings, format_type)
        
    def log(self, message):
        self.gui_utils.log(message)

    def get_auth_token(self):
        token = self.auth_manager.get_auth_token()
        if token:
            if self.status_label:
                self.root.after(0, lambda: self.status_label.config(text="상태: 로그인 완료", foreground="green"))
            # [v3.8] 모듈화된 서비스 시작
            self.portfolio.load_all()      # 기존 데이터 복구
            self.market_service.start()    # 시장 지수 모니터링 시작
            self.remote_commander.start()  # 원격 명령 폴링 시작
            
            # 초기 동기화 (2초 후)
            self.root.after(2000, lambda: self.portfolio.sync_with_actual_account(silent=True))
            
            threading.Thread(target=lambda: self.get_kospi_200(force=True), daemon=True).start()

    def get_kospi_200(self, force=False):
        self.log("[Data] KOSPI 종목 리스트 수집 및 검증 중...")
        # 1. 실시간 감시용 (200종목)
        df_200 = self.data_manager.get_kospi_list(count=200, force=force)
        # 2. 전체 백업/하베스팅용 (500종목)
        self.data_manager.get_kospi_list(count=500, force=force)
        
        if df_200 is not None:
            self.log(f"[Data] 감시 대상 {len(df_200)}개 종목 로드 완료")
            if self.btn_select:
                self.root.after(0, lambda: self.btn_select.config(state=tk.NORMAL))
        self.log("-" * 60)

    def start_selection_thread(self):
        if self.btn_select: self.btn_select.config(state=tk.DISABLED)
        threading.Thread(target=self.select_monitoring_targets, daemon=True).start()

    def select_monitoring_targets(self):
        try:
            df_result = strategy_selector.select_monitoring_targets(
                settings=self.settings,
                logger_callback=self.log
            )
            if df_result is not None and not df_result.empty:
                self._display_selected_targets(df_result)
            self.root.after(0, lambda: self.btn_select.config(state=tk.NORMAL) if self.btn_select else None)
        except Exception as e:
            self.log(f"[Error] 종목 선정 중 예외 발생: {e}")
            self.root.after(0, lambda: self.btn_select.config(state=tk.NORMAL) if self.btn_select else None)

    # [v27.4] 듀얼 트랙 지수 업데이트 로직
    def _fast_index_price_loop(self):
        """10초 주기로 지수 현재가만 빠르게 업데이트"""
        while True:
            try:
                self._fast_update_market_prices()
            except Exception as e:
                print(f"[ERROR] Fast Index Price Loop Error: {e}")
            time.sleep(10)

    def _heavy_index_trend_loop(self):
        """2분 주기로 MSI/ADX/VIX 포함 정밀 추세 분석 수행"""
        while True:
            try:
                self._update_market_indices()
            except Exception as e:
                print(f"[ERROR] Heavy Index Trend Loop Error: {e}")
            time.sleep(120)

    def _fast_update_market_prices(self):
        """네이버 폴링 API 기반 초실시간 가격 반영"""
        res_dict = self.data_manager.get_realtime_indices()
        if not res_dict: return
        
        for name, data in res_dict.items():
            if name in self.market_status:
                self.market_status[name].update(data)
            else:
                self.market_status[name] = data
        
        self._refresh_market_ticker_ui()

    def _update_market_indices(self):
        """Triple-Lock 기반 정밀 장세 판정 및 시각 테마 동기화"""
        res_dict = self.data_manager.get_market_indices()
        if not res_dict: return

        for name, data in res_dict.items():
            self.market_status[name] = data
        
        # [v4.2] Triple-Lock 기반 통합 추세 판단 (KOSPI 중심으로 고정)
        kospi_data = self.market_status.get("KOSPI", {})
        self.market_trend = kospi_data.get("trend", "NEUTRAL")
        
        m_display = {"UP": "🟢순풍", "NEUTRAL": "⚪중립", "DOWN": "⚠️역풍"}[self.market_trend]
        
        if self._last_m_trend != m_display:
            self._last_m_trend = m_display
            self.log(f"[System] 현재 시장 지수 추세: {m_display}")
            self.root.after(0, lambda: self._update_market_visuals(self.market_trend))
        
        self._refresh_market_ticker_ui()

    def _refresh_market_ticker_ui(self):
        """상단 티커 라벨의 텍스트를 최신 정보로 갱신"""
        if not self.lbl_market_indices: return
        
        results = []
        vix = 15.0
        for name, data in self.market_status.items():
            price = data.get('val', 0)
            ratio = data.get('ratio', 0)
            msi = data.get('msi', 0)
            adx = data.get('adx', 0)
            vix = data.get('vix', vix)
            
            sign = "▲" if ratio > 0 else "▼"
            # [v4.0] MSI와 ADX 정보를 명확하게 표시
            results.append(f"{name}: {price:,.2f}({sign}{abs(ratio):.2f}%, MSI:{msi:+.2f}, ADX:{adx:.1f})")
        
        if results:
            m_tag = {"UP": "[순풍]", "NEUTRAL": "[중립]", "DOWN": "[역풍]"}.get(self.market_trend, "[확인중]")
            status_text = " | ".join(results)
            full_text = f"📊 {m_tag} 시장 지수: {status_text} | VIX: {vix:.1f}"
            self.root.after(0, lambda: self.lbl_market_indices.config(text=full_text))

    def _on_realtime_data_received(self, code, now_price, volume, trde_val, **kwargs):
        """[v25.7] SignalEngine을 통해 실시간 데이터 처리 및 DB 로깅"""
        self.signal_engine.on_tick_received(code, now_price, volume, trde_val, **kwargs)

    def _on_execution_received(self, code, order_no, qty, price, side):
        """[v3.8] 실시간 체결(Execution/CHE) 이벤트 처리 - 모듈 위임"""
        side_kr = "매수" if side == "buy" else "매도"
        
        # 1. DB 상태 업데이트
        self.data_manager.update_order_status(code, order_no, "FILLED", filled_price=price, filled_qty=qty)
        
        # 2. 포트폴리오 매니저 위임 (가상 포지션 관리 및 텔레그램 알림)
        self.portfolio.on_fill(code, side, price, qty)
        
        # 3. UI 업데이트 (3판넬 통합 갱신)
        self._log_order_ui(code, side, qty, price, "SYSTEM", "SUCCESS")
        
        self.log(f"✅ [체결] {code} {side_kr} {qty}주 완료 ({price:,}원)")

    def _update_market_visuals(self, trend):
        """[v27.3] 시장 지수 추세에 따른 실시간 감시창 '온몸' 테마 변경"""
        # 1. 상단 마켓 티커 배경색 및 포인트 컬러 설정
        # UP: 기본 배경, NEUTRAL: Slate, DOWN: Crimson
        c = self.nordic_style.colors
        theme_colors = {"UP": (c['bg'], c['primary']), 
                        "NEUTRAL": ("#3B4252", "#E5E9F0"), 
                        "DOWN": ("#3B2E2E", "#BF616A")}
        bg_col, fg_col = theme_colors.get(trend, (c['bg'], c['primary']))
        
        style = ttk.Style()
        style.configure("Header.TFrame", background=bg_col)
        if hasattr(self, 'header_frame'):
            self.header_frame.config(style="Header.TFrame")
        if hasattr(self, 'lbl_market_indices'):
            self.lbl_market_indices.config(background=bg_col, foreground=fg_col)
            
        # 2. 실시간 감시 탭 테마 동기화 (MonitorTab 위임)
        if hasattr(self, 'monitor_tab'):
            self.monitor_tab._update_visual_theme(trend)

    async def _ws_client_loop(self):
        await self.monitor_tab._ws_client_loop()

    def _ws_worker(self):
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

    def _on_market_data_update(self, indices=None, trend=None):
        """[v3.8] MarketDataService로부터 데이터를 전달받아 UI 갱신"""
        if indices:
            self.market_status = indices
            self._refresh_market_ticker_ui()
        if trend:
            self.market_trend = trend
            self._update_market_visuals(trend)

    def _render_tree_updates(self):
        if not self.tree_monitor or not self.is_monitoring: 
            self._update_timer_active = False
            return
        try:
            # 1. 대기 중인 트레이 업데이트 처리
            if self.pending_tree_updates:
                items = list(self.pending_tree_updates.items())
                self.pending_tree_updates.clear()
                
                for iid, (values, tag, _) in items:
                    # [v2.2] 보유 여부에 따라 타겟 트리 결정
                    is_active = iid in self.virtual_positions
                    target_tree = self.tree_active_monitor if is_active else self.tree_monitor
                    other_tree = self.tree_monitor if is_active else self.tree_active_monitor
                    
                    # 반대편 트리에 있으면 삭제 (이동 효과)
                    if other_tree.exists(iid):
                        other_tree.delete(iid)
                        
                    # 타겟 트리에 없으면 생성, 있으면 업데이트
                    if not target_tree.exists(iid):
                        target_tree.insert("", tk.END, iid=iid, values=values)
                    
                    idx = target_tree.index(iid)
                    row_tag = "evenrow" if idx % 2 == 0 else "oddrow"
                    base_tags = [row_tag]
                    if is_active: base_tags.append("active_pos")
                    
                    if tag:
                        if isinstance(tag, (list, tuple)): base_tags.extend(tag)
                        else: base_tags.append(tag)
                    
                    new_tags = tuple(base_tags)
                    curr = target_tree.item(iid)
                    if curr['values'] != values or tuple(curr['tags']) != new_tags:
                        target_tree.item(iid, values=values, tags=new_tags)

            # [v2.2] 가상 포지션 탭 실시간 수익률 동기화 (1초 주기)
            self._virtual_refresh_cnt = getattr(self, '_virtual_refresh_cnt', 0) + 1
            if self._virtual_refresh_cnt % 5 == 0:
                if hasattr(self, 'virtual_tab'):
                    self.virtual_tab._render_real_data()
                # [v4.0] 자가 학습 모델 상태 업데이트 (약 1초 주기)
                if hasattr(self, 'monitor_tab'):
                    self.monitor_tab._update_learning_status()

        except Exception as e:
            print(f"[DEBUG] Render error: {e}")
        finally:
            self.root.after(200, self._render_tree_updates)

    def _calculate_stage2_confluence(self, code, now_price, ind, vol_power, ask_bid_ratio):
        """[v2.2] 2단계 추매 조건 합치도 계산 (MonitorTab 표시용)"""
        if code not in self.virtual_positions: return "N/A", 0.0
        pos = self.virtual_positions[code]
        if pos.get('stage', 1) != 1: return "🔥 완료", 1.0
        
        s = self.settings
        met, total = 0, 0
        
        # 1. 시간 지연
        if s.get('use_s2_delay', True):
            total += 1
            min_h = float(s.get('s2_delay_hours', 24.0))
            if pos.get('entry_time') and (datetime.now() - datetime.fromisoformat(pos['entry_time'])).total_seconds() / 3600 >= min_h:
                met += 1
        
        # 2. 수익률 트리거
        total += 1
        profit = (now_price / pos['avg_price'] - 1) * 100
        if profit >= float(s.get('stage2_trigger', 1.0)): met += 1
        
        # 3. 기술적 지표 (MA20, MA5, RSI, MACD)
        if ind is not None:
            if s.get('use_s2_ma20', True):
                total += 1
                if now_price >= ind.get('MA20', 0) * (1 + float(s.get('s2_ma20_gap', 0.0))/100.0): met += 1
            if s.get('use_s2_ma5', True):
                total += 1
                if now_price >= ind.get('MA5', 0) * (1 + float(s.get('s2_ma5_gap', -0.5))/100.0): met += 1
            if s.get('use_s2_vol_surge', True):
                total += 1
                if ind.get('Volume', 0) >= ind.get('Vol_MA20', 0) * float(s.get('s2_vol_ratio', 1.0)): met += 1
                
        # 4. 수급 (Smart Buy)
        if s.get('use_smart_buy_s2', True):
            total += 1
            min_p = float(s.get('smart_buy_vp', 120.0))
            min_r = float(s.get('smart_buy_ba_ratio', 1.5))
            if vol_power >= min_p and ask_bid_ratio >= min_r: met += 1
            
        return f"추매 ({met}/{total})", (met/total if total > 0 else 0)

    def _sync_mock_account_balance(self):
        """[v2.2] 모의투자 계좌의 실시간 예수금 조회 (kt00001 API 활용)"""
        try:
            # 1. 계좌번호 확인
            acc_id = self.settings.get('mock_account_id', "")
            if not acc_id:
                acc_id = simpledialog.askstring("입력", "모의투자 계좌번호를 입력하세요:", parent=self.root)
                if not acc_id: return
                self.settings['mock_account_id'] = acc_id
                self._final_save_settings()

            # 2. 모의투자 전용 토큰 직접 발급 시도 (V3.6)
            mock_token = None
            try:
                mock_token_url = "https://mockapi.kiwoom.com/oauth2/token"
                self.log(f"[Auth] 모의투자 전용 토큰 발급 시도: {mock_token_url}")
                
                # 모의투자 전용 키가 설정되어 있는지 확인
                mock_app_key = getattr(config, 'MOCK_APP_KEY', "")
                mock_app_secret = getattr(config, 'MOCK_APP_SECRET', "")
                
                # 공백이거나 설정되지 않은 경우 실전 키 사용 (단, 8030 에러 가능성 높음)
                use_key = mock_app_key if mock_app_key else config.APP_KEY
                use_secret = mock_app_secret if mock_app_secret else config.APP_SECRET
                
                if not mock_app_key:
                    self.log("⚠️ [Auth] 모의투자 전용 AppKey가 config.py에 설정되지 않았습니다. 실전 키로 시도합니다.")

                res = requests.post(mock_token_url, json={
                    "grant_type": "client_credentials",
                    "appkey": use_key,
                    "secretkey": use_secret
                }, timeout=10)
                
                if res.status_code == 200:
                    data = res.json()
                    mock_token = data.get('access_token') or data.get('token')
                    if mock_token:
                        self.log("✅ [Auth] 모의투자 토큰 발급 성공!")
                    else:
                        msg = data.get('return_msg', '응답 메시지 없음')
                        self.log(f"⚠️ [Auth] 모의투자 토큰 발급 거절 ({data.get('return_code')}): {msg}")
                else:
                    self.log(f"❌ [Auth] 모의투자 토큰 발급 실패 ({res.status_code}): {res.text}")
                    # [v4.1] TokenManager 대신 자체 AuthManager 사용 (302 방지)
                    mock_token = self.auth_manager.get_auth_token()
            except Exception as te:
                self.log(f"❌ [Auth] 모의투자 토큰 요청 중 예외: {te}")
                mock_token = self.auth_manager.get_auth_token()

            token = mock_token
            if not token:
                self.virtual_tab.lbl_mock_result.config(text="토큰 발급 실패", foreground="#BF616A")
                return

            # 3. API 요청 (kt00001 - 모의투자 예수금상세현황)
            host = getattr(config, 'MOCK_URL', 'https://mockapi.kiwoom.com')
            url = f"{host}/api/dostk/acnt"
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {token}',
                'api-id': 'kt00001',
            }
            params = {
                'qry_tp': '3', # 추정조회
                'acc_no': acc_id
            }

            self.virtual_tab.lbl_mock_result.config(text="조회 중...", foreground="#EBCB8B")
            
            def run_inquiry():
                try:
                    # 1차 시도: qry_tp='3' (추정조회)
                    payload = {'qry_tp': '3'}
                    res = requests.post(url, headers=headers, json=payload, timeout=10)
                    data = res.json()
                    
                    # 만약 잔고가 0이거나 조회가 안 되면 qry_tp='2' (일반조회) 시도
                    deposit_str = data.get('entr', '0')
                    if not deposit_str or float(str(deposit_str).replace(',', '')) == 0:
                        payload = {'qry_tp': '2'}
                        res = requests.post(url, headers=headers, json=payload, timeout=10)
                        data = res.json()
                        deposit_str = data.get('entr', '0')

                    dps_val = int(float(str(deposit_str).replace(',', '')))

                    # 결과 반영
                    def update_ui():
                        self.virtual_tab.lbl_mock_result.config(text=f"연결됨: {dps_val:,}원", foreground="#A3BE8C")
                        if dps_val <= 0:
                            self.log(f"⚠️ [모의계좌] 잔고가 0원입니다. (응답: {data})")
                        else:
                            self.log(f"✅ [모의계좌] 잔고 조회 성공: {dps_val:,}원")
                    
                    self.root.after(0, update_ui)
                except Exception as e:
                    self.root.after(0, lambda: self.virtual_tab.lbl_mock_result.config(
                        text="오류 발생", foreground="#BF616A"
                    ))
                    print(f"[DEBUG] Mock Inquiry Error: {e}")

            threading.Thread(target=run_inquiry, daemon=True).start()

        except Exception as e:
            messagebox.showerror("에러", f"모의 잔고 조회 중 오류 발생: {e}")

    def toggle_monitoring(self):
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
        required_cols = ['MA20', 'MA60', 'PrevVol', 'Price', 'Ratio', 'MA60Ratio']
        missing_cols = [c for c in required_cols if c not in df_targets.columns]
        if missing_cols:
            self.log(f"⚠️ [주의] 선정 파일에 지표({', '.join(missing_cols)})가 없습니다.")
            return

        self.monitor_codes = df_targets['Code'].tolist()
        self.monitor_info = {}
        
        if self.tree_monitor:
            for item in self.tree_monitor.get_children(): self.tree_monitor.delete(item)

        for i, (_, row) in enumerate(df_targets.iterrows()):
            c = str(row['Code']).zfill(6)
            name = row.get('Name', 'Unknown')
            self.monitor_info[c] = {
                'name': name, 'ma20': float(row.get('MA20', 0)), 'ma60': float(row.get('MA60', 0)),
                'prev_vol': float(row.get('PrevVol', 0)),
                'score': row.get('Score', 0.0), 'ma_buffer_scale': float(row.get('MABufScale', 1.0)),
                'open': float(row.get('Open', 0)), 'last_price': float(row.get('Price', 0)) if row.get('Price') else 0
            }
            if self.tree_monitor:
                ratio = row.get('Ratio', 0)
                sign = "▲" if ratio > 0 else "▼" if ratio < 0 else ""
                row_tag = "evenrow" if i % 2 == 0 else "oddrow"
                color_tag = "price_up" if ratio > 0 else "price_down" if ratio < 0 else ""
                
                tags = [row_tag]
                if color_tag: tags.append(color_tag)
                
                vals = (
                    f"{row.get('Score', 0.0):.1f}", c, name,
                    f"{int(row.get('Price', 0)):,}" if row.get('Price', 0) else "",
                    f"{sign} {abs(ratio):.2f}%" if ratio != 0 else "0.00%",
                    "", "-", # VolPower, Status(Confluence) [v3.5]
                    "", "", # TotalAsk, TotalBid placeholders
                    f"{int(row.get('Volume', 0)):,}" if row.get('Volume', 0) else "",
                    f"{int(row.get('AvgTrdeVal', 0)):,}"
                )
                self.tree_monitor.insert("", tk.END, iid=c, values=vals, tags=tuple(tags))
        
        if self.lbl_monitor_status: 
            self.root.after(0, lambda: self.lbl_monitor_status.config(text=f"상태: 분석 중 ({len(self.monitor_codes)}종목)", foreground="blue"))
        
        def start_sequence():
            try:
                self.log("[Analysis] 4단계: 분봉 데이터 분석 및 프로필 최적화 시작...")
                # [v3.5] 1분봉 워밍업 (Cold Start 제거)
                self._warmup_historical_data()
                self._prepare_volume_profiles()
                self.is_monitoring = True
                if self.btn_monitor_toggle: self.root.after(0, self.btn_monitor_toggle.config, {"text": "⏹️ 감시 중단", "style": "Toggle.TButton"})
                if self.lbl_monitor_status: self.root.after(0, self.lbl_monitor_status.config, {"text": f"상태: 연결 중 ({len(self.monitor_codes)}종목)", "foreground": "blue"})
                self.monitor_thread = threading.Thread(target=self._ws_worker, daemon=True)
                self.monitor_thread.start()
                
                # [v35.7] 자가 학습형 모델 주기적 실력 보고 및 저장 스케줄링
                self.root.after(300000, self._auto_save_sim_params) # 5분마다 저장
                self.root.after(1800000, self._report_sim_calibration) # 30분마다 보고
            except Exception as e:
                self.log(f"❌ [Error] 감시 시작 시퀀스 중 오류 발생: {e}")
                self.stop_monitoring()
        threading.Thread(target=start_sequence, daemon=True).start()

    def _auto_save_sim_params(self):
        """[v35.7] 학습된 파라미터를 주기적으로 파일에 저장"""
        if not self.is_monitoring: return
        SimulatedRealtimeMetrics.save_params()
        self.root.after(300000, self._auto_save_sim_params)

    def _report_sim_calibration(self):
        """[v35.7] 현재까지의 모델 학습 성과를 로그에 보고"""
        if not self.is_monitoring: return
        p = SimulatedRealtimeMetrics.params
        total = p.get('total_calibrations', 0)
        if total > 100:
            err_vp = p.get('avg_error_vp', 0)
            err_ba = p.get('avg_error_ba', 0)
            msg = f"📈 [Model Report] 합성 모델 자가 학습 진행 중... (학습 횟수: {total})\n"
            msg += f"   - 체결강도 평균 오차: {err_vp:.2f} | 호가잔량 평균 오차: {err_ba:.2f}"
            self.log(msg)
        self.root.after(1800000, self._report_sim_calibration)

    def _warmup_historical_data(self):
        """감시 대상 전 종목의 15시간치 데이터를 DB+API에서 가져와 즉시 지표 계산 가능하게 함"""
        token = self.auth_manager.get_auth_token() # Could be None (DB-only)
        
        self.log(f"📦 [System] {len(self.monitor_codes)}개 종목 딥 워밍업(15H) 진행 중...")
        if self.status_label:
            self.root.after(0, lambda: self.status_label.config(text=f"상태: 분석 중 (0/{len(self.monitor_codes)})", foreground="blue"))
        
        cnt_lock = threading.Lock()
        success_count = [0]
        total = len(self.monitor_codes)
        
        def process(code):
            df_15m = self.data_manager.fetch_and_parse_15m_bars(code, token)
            if not df_15m.empty:
                self.bars_data[code] = df_15m
                with cnt_lock:
                    success_count[0] += 1
                    cur = success_count[0]
                
                # 전역 상태 라벨 업데이트 (화면 최하단)
                if cur % 5 == 0 or cur == total:
                    if self.status_label:
                        self.root.after(0, lambda c=cur: self.status_label.config(text=f"상태: 분석 중 ({c}/{total})"))

        # [v3.5.2] API 호출 제한(429) 방지를 위해 병렬 허용치를 5 -> 2로 축소
        self.executor = ThreadPoolExecutor(max_workers=2)
        try:
            list(self.executor.map(process, self.monitor_codes))
        finally:
            if self.executor:
                self.executor.shutdown(wait=False)
                self.executor = None
        
        if self.status_label:
            self.root.after(0, lambda: self.status_label.config(text="상태: 분석 완료", foreground="green"))
        
        msg = f"✅ [System] 워밍업 완료 ({success_count[0]}/{total} 성공). 지표가 즉시 활성화됩니다."
        if success_count[0] < total:
            msg = f"⚠️ [System] 워밍업 일부 실패 ({success_count[0]}/{total} 성공). API 제한이나 DB 부재로 지표가 지연될 수 있습니다."
            if success_count[0] == 0:
                msg = f"❌ [System] 워밍업 전원 실패 (0/{total}). API 호출 제한(429) 또는 DB 파일 유실을 확인하세요."
        
        self.log(msg)

    def on_close(self):
        """창 닫기 시 안전하게 종료 처리"""
        self.log("⏹️ [System] 프로그램을 종료합니다. 리소스를 정리 중...")
        
        # [v35.7] 종료 시 학습된 파라미터 강제 저장
        try:
            from strategy.simulation_models import SimulatedRealtimeMetrics
            SimulatedRealtimeMetrics.save_params()
            self.log("✅ [Learning] 자가 학습 파라미터가 안전하게 저장되었습니다.")
        except: pass
        
        self.is_monitoring = False
        if self.executor:
            try:
                self.executor.shutdown(wait=False)
            except: pass
        self.root.destroy()

    def _prepare_volume_profiles(self):
        # [v4.1] TokenManager 대신 자체 AuthManager 사용 (302/Saturday Maintenance Crash 방지)
        token = self.auth_manager.get_auth_token()
        if not token: return
        self.data_manager.prepare_volume_profiles(self.monitor_codes, token, self.bars_data, self.volume_profiles, self.min_volume_profiles)
        self.log(f"✅ {len(self.volume_profiles)}/{len(self.monitor_codes)}개 종목 분석 완료")
        self.config_handler.save_bars_cache(self.bars_data) 

    def _recalculate_all_indicators(self, code):
        df = self.bars_data.get(code)
        if df is None or df.empty: return
        try:
            # [v31.0] 전체 지표 통합 계산 (ATR 포함)
            self.bars_data[code] = TradingStrategy.calculate_indicators(df, self.settings)
        except Exception as e:
            print(f"[DEBUG] Indicator Recalc Error ({code}): {e}")

    def stop_monitoring(self):
        self.is_monitoring = False
        self._save_bars_cache()
        if hasattr(self, 'btn_monitor_toggle') and self.btn_monitor_toggle: 
            self.btn_monitor_toggle.config(text="▶️ 실시간 감시 시작", style="Accent.TButton")
        if hasattr(self, 'lbl_monitor_status') and self.lbl_monitor_status: self.lbl_monitor_status.config(text="상태: 중단됨", foreground="red")

    def send_telegram_message(self, message):
        self.data_manager.send_telegram_message(message)

    def _display_selected_targets(self, df):
        if df.empty: return
        self.log("\n" + "="*125)
        self.log(f"{'No':<3} | {'가점':>5} | {'코드':<6} | {'종목명':<16} | {'현재가':>8} | {'등락률':>7} | {'대금':>4}억 | {'RSI':>5}")
        self.log("-" * 125)
        criteria_text = self._get_criteria_text(format_type="telegram")
        msg = f"🚀 <b>오늘의 스윙 매수 랭킹 (v2.1)</b>\n\n<b>[선정 기준]</b>\n{criteria_text}\n\n"
        for i, (_, row) in enumerate(df.iterrows(), 1):
            c = str(row.get('Code', '')).zfill(6)
            self.log(f"{i:3d} | {row.get('Score', 0.0):5.1f} | {c:6s} | {row.get('Name', 'Unknown'):16s} | {int(row.get('Price', 0)):8,d} | {row.get('Ratio', 0):>6.1f}% | {row.get('AvgTrdeVal', 0):>4d}억 | {row.get('RSI', 0):>5.1f}")
            msg += f"<b>{i}. {row.get('Name', 'Unknown')}</b> ({c}) - <b>{row.get('Score', 0.0)}점</b>\n📊 현재가: {int(row.get('Price', 0)):,}원 ({row.get('Ratio', 0):+.2f}%)\n📈 RSI: {row.get('RSI', 0):.1f} | 💰 대금: {row.get('AvgTrdeVal', 0)}억\n\n"
        threading.Thread(target=self.send_telegram_message, args=(msg,), daemon=True).start()

    def _render_virtual_positions(self):
        if hasattr(self, 'virtual_tab'):
            self.virtual_tab._render_virtual_positions()

    def sync_with_actual_account(self, silent=False):
        """UI에서 호출하는 계좌 동기화 프록시"""
        self.portfolio.sync_with_actual_account(silent=silent)

    def _clear_virtual_positions(self):
        """UI에서 호출하는 포지션 초기화 프록시"""
        if messagebox.askyesno("초기화", "모든 가상 포지션과 히스토리를 초기화하시겠습니까?"):
            self.portfolio.clear_all()

    def _sell_all_virtual(self):
        """UI에서 호출하는 비상 매도 프록시"""
        if messagebox.askyesno("🚨 비상 청산", "모든 보유 종목을 지금 즉시 시장가로 매도하시겠습니까?"):
            self.portfolio.sell_all()

    def _calculate_buy_amt(self, stage=1):
        """[v2.2] 설정창의 비중 설정(원/%)에 따른 실제 투자금액 산출"""
        unit = self.settings.get(f's{stage}_sizing_unit', '원')
        if unit == '원':
            # 콤마가 포함된 문자열일 가능성을 배제하기 위해 float 변환 후 int
            return int(float(str(self.settings.get(f'stage{stage}_amt', 1000000)).replace(',', '')))
        else:
            pct = float(self.settings.get(f's{stage}_equity_pct', 10.0))
            total_cap = float(str(self.settings.get('total_capital', 10000000)).replace(',', ''))
            return int(total_cap * pct / 100.0)

    def _virtual_buy_stage_1(self, code, name, price, reason="TYPE_A"):
        if code in self.virtual_positions: return
        # [v2.2] 동적 비중 계산 적용
        invest_amt = self._calculate_buy_amt(stage=1)
        
        # [v3.8] 공격적 체결을 위한 +1틱 보정가 산출
        adj_price = self.order_manager.get_adjusted_price(price, "buy")
        qty = max(1, invest_amt // adj_price)
        actual_invested = qty * adj_price
        
        # [v3.8] 실전/모의투자 연동 (Stage 1) - 이제 주문만 발송하고 체결(CHE) 시점에 가상포지션 등록
        use_mock = getattr(config, 'USE_MOCK', False)
        allow_order = getattr(config, 'ALLOW_MOCK_ORDER', False) if use_mock else getattr(config, 'ALLOW_REAL_ORDER', False)
        if allow_order:
            self.order_manager.place_order(code, qty, price, order_type="03", side="buy", name=name, reason=reason)
        
        self.log(f"🚀 [주문발송] {name}({code}) 1차 진입 주문! ({price:,}원, {qty}주). 체결 대기 중...")
        msg = f"<b>💰 [가상매수/1차]</b>\n종목: {name}({code})\n가: {price:,}원 / 수: {qty}주\n시간: {datetime.now().strftime('%H:%M:%S')}"
        threading.Thread(target=self.send_telegram_message, args=(msg,), daemon=True).start()
        
        # [v3.7.8] DB 영구 저장 및 UI 로그
        self.data_manager.save_active_position(code, name, price, qty, 1, actual_invested, datetime.now().isoformat(), reason)
        self._log_order_ui(code, "buy", qty, price, "03", "SUCCESS")

        self.notebook.select(self.tab_virtual)
        self.portfolio.save_db()
        self._render_virtual_positions()

    def _process_virtual_scenarios(self, code, now_price, rsi_val, macd_val, osc_val, vol_power, is_vol_spike, ind=None, **kwargs):
        """[v22.0] 설정창의 7가지 조건과 동기화된 가상 매매 시나리오 처리"""
        if now_price <= 0: return # [v2.2] 유효하지 않은 가격 데이터 진입 차단
        if code not in self.virtual_positions: return
        pos = self.virtual_positions[code]
        if pos.get('exit_pending', False): return # [v3.8] 이미 매도 주문 발송됨
        stage = pos['stage']
        avg_p = pos['avg_price']
        profit_ratio = (now_price / avg_p - 1) * 100
        pos['current_price'] = now_price
        
        # [Phase 5] 수격 지표 추출
        is_power_exhausted = kwargs.get('is_power_exhausted', False)
        bid_ask_ratio = kwargs.get('bid_ask_ratio', 0)
        ask_bid_ratio = kwargs.get('ask_bid_ratio', 0)

        if now_price > pos.get('max_now', 0): pos['max_now'] = now_price

        # 지표 데이터 확보 (ind가 없으면 bars_data 활용)
        if ind is None and code in self.bars_data and not self.bars_data[code].empty:
            ind = self.bars_data[code].iloc[-1]

        is_exit = False
        exit_reason = ""
        max_now = pos.get('max_now', now_price)
        
        # 1. 익절/손절/Flash Exit 체크
        flash_pct = float(self.settings.get(f's{stage}_flash_exit_pct', 3.0))
        if self.settings.get(f'use_s{stage}_flash_exit', True) and now_price <= max_now * (1.0 - flash_pct / 100.0):
            is_exit, exit_reason = True, "FLASH_EXIT"

        if not is_exit and self.settings.get(f'use_s{stage}_tp', True):
            tp_pct = float(self.settings.get('s1_a_take_profit' if stage == 1 else 's2_take_profit', 5.0))
            if stage == 1:
                is_b = "TYPE_B" in pos.get('reason', '')
                tp_pct = float(self.settings.get('s1_b_take_profit' if is_b else 's1_a_take_profit', 5.0))
            
            if now_price >= avg_p * (1.0 + tp_pct / 100.0):
                is_exit, exit_reason = True, "TAKE_PROFIT"

        # [Phase 5] Smart Exit (Supply/Demand Trap)
        if not is_exit and self.settings.get('use_smart_exit', True):
            # [v31.4] Simplified threshold check (High - Drop)
            threshold = float(self.settings.get('smart_exit_price_threshold', -0.5))
            if now_price <= max_now * (1.0 + threshold / 100.0):
                if is_power_exhausted:
                    min_ba = float(self.settings.get('smart_exit_ba_ratio', 1.3))
                    # [Fix] bid_ask_ratio is (Bid/Ask). Seller dominance is (Ask > Bid), i.e., ask_bid_ratio >= min_ba
                    if ask_bid_ratio >= min_ba:
                        is_exit, exit_reason = True, "SMART_EXIT"

        if not is_exit and self.settings.get(f'use_s{stage}_stop_loss', True):
            sl_pct = float(self.settings.get('s1_a_stop_loss' if stage == 1 else 's2_stop_loss', 3.0))
            if stage == 1:
                is_b = "TYPE_B" in pos.get('reason', '')
                sl_pct = float(self.settings.get('s1_b_stop_loss' if is_b else 's1_a_stop_loss', 3.0))
            
            if now_price <= avg_p * (1.0 - sl_pct / 100.0):
                is_exit, exit_reason = True, "STOP_LOSS"

        # [v31.0] ATR-based Exit Check (Real-time)
        if not is_exit and self.settings.get('use_atr_exit', False):
            curr_atr = ind.get('ATR', 0) if ind is not None else 0
            entry_atr = pos.get('atr_at_entry', curr_atr)
            if curr_atr > 0:
                sl_mult = float(self.settings.get('atr_multiplier_sl', 2.0))
                trail_mult = float(self.settings.get('atr_multiplier_trail', 3.0))
                if self.settings.get('use_atr_stop_loss', True) and now_price <= avg_p - (sl_mult * entry_atr):
                    is_exit, exit_reason = True, "ATR_SL"
                elif self.settings.get('use_atr_trailing_stop', True) and now_price <= max_now - (trail_mult * curr_atr):
                    is_exit, exit_reason = True, "ATR_TRAIL"

        if is_exit:
            # [v3.8] 공격적 체결(매도)을 위한 -1틱 보정가 산출
            adj_price = self.order_manager.get_adjusted_price(now_price, "sell")
            pnl_amt = (adj_price - avg_p) * pos['qty']
            
            # [v3.7.8] 실전/모의투자 연동 (Exit)
            use_mock = getattr(config, 'USE_MOCK', False)
            allow_order = getattr(config, 'ALLOW_MOCK_ORDER', False) if use_mock else getattr(config, 'ALLOW_REAL_ORDER', False)
            if allow_order:
                # [v3.8] name, reason 파라미터 추가
                self.order_manager.place_order(code, pos['qty'], now_price, order_type="03", side="sell", name=pos['name'], reason=f"EXIT_{exit_reason}")

            self.log(f"🚨 [매도발송/{exit_reason}] {pos['name']}({code}) 탈출 주문! ({profit_ratio:+.2f}%)")
            # [v3.8] 즉시 삭제 대신 '매도 대기' 상태로 전환
            pos['exit_pending'] = True
            pos['exit_reason'] = exit_reason
            pos['exit_adj_price'] = adj_price # 체결 시점 pnl 계산용 임시 저장
            
            # UI 로그에는 주문 단계 노출
            self._log_order_ui(code, "sell", pos.get('qty', 0), now_price, "03", "SELL_PENDING")
            
            msg = f"<b>🚀 [매도발송/{exit_reason}]</b>\n종목: {pos['name']}({code})\n예상수익: {profit_ratio:+.1f}%\n시간: {datetime.now().strftime('%H:%M:%S')}"
            threading.Thread(target=self.send_telegram_message, args=(msg,), daemon=True).start()
            
            self.portfolio.save_db()
            self._render_virtual_positions()

            return

        # 2. 2단계 매수(추매) 승인 체크
        if stage == 1:
            s = self.settings
            if s.get('use_s2_delay', True):
                min_hours = float(s.get('s2_delay_hours', 24.0))
                if pos.get('entry_time') and (datetime.now() - datetime.fromisoformat(pos['entry_time'])).total_seconds() / 3600 < min_hours:
                    return
            
            can_addon = True
            if ind is not None:
                rsi_val = ind.get('RSI', 0)
                macd_val = ind.get('MACD', 0)
                osc_val = ind.get('OSC', 0)
                if s.get('use_s2_ma20', True):
                    gap = float(s.get('s2_ma20_gap', 0.0))
                    if now_price < ind.get('MA20', 0) * (1 + gap/100.0): can_addon = False
                if can_addon and s.get('use_s2_ma5', True):
                    gap = float(s.get('s2_ma5_gap', -0.5))
                    if now_price < ind.get('MA5', 0) * (1 + gap/100.0): can_addon = False
                if can_addon and s.get('use_s2_vol_surge', True):
                    v_ma20 = ind.get('Vol_MA20', ind.get('Volume', 0))
                    if ind.get('Volume', 0) < v_ma20 * float(s.get('s2_vol_ratio', 1.0)): can_addon = False
                if can_addon and s.get('use_s2_rsi_sig', True):
                    r_sig = ind.get('RSI_Sig', 50)
                    if rsi_val < r_sig * (1 + float(s.get('s2_rsi_gap', 0.0))/100.0): can_addon = False
                if can_addon and s.get('use_s2_macd_gc', True):
                    m_sig = ind.get('MACD_Sig', 0)
                    if macd_val < m_sig + float(s.get('s2_macd_gap', 0.0)): can_addon = False

            if can_addon:
                if self.settings.get('use_smart_buy_s2', True):
                    min_p = float(self.settings.get('smart_buy_vp', 120.0))
                    min_r = float(self.settings.get('smart_buy_ba_ratio', 1.5))
                    if vol_power < min_p or ask_bid_ratio < min_r:
                        can_addon = False
                
            if can_addon:
                s2_trigger = float(s.get('stage2_trigger', 1.0))
                if profit_ratio >= s2_trigger:
                    # [v2.2] 동적 비중 계산 적용
                    add_amt = self._calculate_buy_amt(stage=2)
                    # [v3.8] 공격적 체결을 위한 +1틱 보정가 산출
                    adj_price = self.order_manager.get_adjusted_price(now_price, "buy")
                    add_qty = add_amt // adj_price
                    if add_qty > 0:
                        # [v3.7.8] 실전/모의투자 연동 (Stage 2)
                        use_mock = getattr(config, 'USE_MOCK', False)
                        allow_order = getattr(config, 'ALLOW_MOCK_ORDER', False) if use_mock else getattr(config, 'ALLOW_REAL_ORDER', False)
                        if allow_order:
                            self.order_manager.place_order(code, add_qty, now_price, order_type="03", side="buy", name=pos['name'], reason="Stage 2 Scaling")
                        
                        self.log(f"🔥 [추매발송] {pos['name']}({code}) 2차 진입 주문! ({adj_price:,}원, {add_qty}주). 체결 대기 중...")
                        # [v4.2] 2차 추매 발송 텔레그램 알림 추가
                        msg = f"<b>🔥 [추매발송/2차]</b>\n종목: {pos['name']}({code})\n가: {adj_price:,}원 / 수: {add_qty}주\n수익률: {profit_ratio:+.2f}%\n시간: {datetime.now().strftime('%H:%M:%S')}"
                        threading.Thread(target=self.send_telegram_message, args=(msg,), daemon=True).start()
                        
                        # DB에 임시 기록 (체결 시 정식 반영)
                        self._log_order_ui(code, "buy", add_qty, adj_price, "03", "ADD_PENDING")

    def _get_system_snapshot(self, code, price, reason="N/A"):
        """현재 시점의 시장지수 및 보조지표 상황 캡처"""
        info = self.monitor_info.get(code, {})
        # [v2.2] market_trend 가시성 개선
        m_trend = getattr(self, 'market_trend', 'NEUTRAL')
        m_tag = {"UP": "🟢순풍", "NEUTRAL": "⚪중립", "DOWN": "⚠️역풍"}.get(m_trend, m_trend)
        
        snapshot = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "price": f"{price:,}",
            "market": m_tag,
            "rsi": "-", "ma": "확인중",
            "power": f"{info.get('vol_power', 100.0):.1f}%",
            "reason": reason
        }
        if code in self.bars_data and not self.bars_data[code].empty:
            ind = self.bars_data[code].iloc[-1]
            snapshot["rsi"] = f"{ind.get('RSI', 0):.1f}"
            snapshot["ma"] = "정배열" if ind.get('MA5', 0) > ind.get('MA20', 0) else "데드크로스"
            # 호가 잔량 정보 추가
            total_ask = info.get('total_ask', 0)
            total_bid = info.get('total_bid', 1)
            snapshot["ba_ratio"] = f"{(total_bid / total_ask):.2f}" if total_ask > 0 else "-"
        return snapshot

    def _load_virtual_db(self):
        if os.path.exists(self.virtual_db_file):
            try:
                with open(self.virtual_db_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.virtual_positions = data.get('active', {})
                    self.virtual_history = data.get('history', [])
            except Exception as e:
                print(f"Virtual DB Load Error: {e}")

    def _log_order_ui(self, code, side, qty, price, order_type, result):
        """[v3.9] DB 기록 및 신규 3판넬 UI 갱신"""
        self.data_manager.log_order(code, side, qty, price, order_type, "MOCK_ID", result)
        if hasattr(self, 'virtual_tab'):
            self.virtual_tab._render_orders()
            self.virtual_tab._render_virtual_positions()

    def _sell_all_virtual(self):
        """[v3.7.8] 비상 상황: 모든 가상/실제 포지션 즉시 시장가 매도 청산"""
        if not self.virtual_positions:
            messagebox.showinfo("알림", "보유 중인 포지션이 없습니다.")
            return
        
        if not messagebox.askyesno("🚨 비상 청산", "모든 보유 종목을 지금 즉시 시장가로 매도하시겠습니까?"):
            return
            
        codes = list(self.virtual_positions.keys())
        for code in codes:
            pos = self.virtual_positions[code]
            info = self.monitor_info.get(code, {})
            curr_p = info.get('price', pos['avg_price'])
            
            # [v3.8] 공격적 체결(매도)을 위한 -1틱 보정가 산출
            adj_p = self.order_manager.get_adjusted_price(curr_p, "sell")
            
            # 실제 주문 발송 (허용된 경우)
            use_mock = getattr(config, 'USE_MOCK', False)
            allow_order = getattr(config, 'ALLOW_MOCK_ORDER', False) if use_mock else getattr(config, 'ALLOW_REAL_ORDER', False)
            if allow_order:
                self.order_manager.place_order(code, pos['qty'], curr_p, order_type="03", side="sell")
            
            # [v3.8] 즉시 삭제 대신 '매도 대기' 상태로 전환
            pos['exit_pending'] = True
            pos['exit_reason'] = "EMERGENCY"
            # self.data_manager.delete_active_position(code) # CHE에서 삭제하도록 유도
            
            # DB 및 UI 로그 기록 (보정가 반영 [v3.8])
            self._log_order_ui(code, "sell", pos['qty'], adj_p, "03", "SELL_PENDING")
            
        self.portfolio.save_db()
        self._render_virtual_positions()
        messagebox.showinfo("완료", f"{len(codes)}개 종목의 청산 주문이 발송되었습니다.")

    # ==========================================
    # 원격 배포 및 자동 업데이트 (Handover 방식)
    # ==========================================
    def _handle_remote_update(self):
        """[Handover] 대리인 스크립트 실행 후 즉시 자기 자신을 종료합니다."""
        self.is_updating = True
        self.send_telegram_message("📦 <b>[원격 배포 시작]</b>\nGIT Pull 및 재배포를 시작합니다.\n10~20초 후 프로그램이 자동 재시작됩니다.")
        
        try:
            # [v1.7] CWD 의존성을 없애기 위해 실행 파일 경로 기준으로 탐색
            script_dir = os.path.dirname(os.path.abspath(__file__))
            update_script = os.path.normpath(os.path.join(script_dir, "remote_update.ps1"))
            
            msg_start = f"📡 [Remote] 업데이트 스크립트 실행 시도: {update_script}"
            self.log(msg_start)
            print(msg_start)
            
            if os.path.exists(update_script):
                # [v2.2] 영문 경로 대응 및 자동 재시작 최적화
                ps_exe = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
                subprocess.Popen([ps_exe, "-ExecutionPolicy", "Bypass", "-File", update_script], 
                                 cwd=script_dir, 
                                 creationflags=0x10) # CREATE_NEW_CONSOLE
                
                self.log("✅ [Remote] 업데이트 배포 시작. 2초 후 자동 재시작됩니다.")
                self.root.after(2000, self.on_close)
            else:
                msg = f"❌ [Remote] 업데이트 스크립트 미탐지: {update_script}"
                self.log(msg)
                print(msg)
                self.is_updating = False
        except Exception as e:
            msg_err = f"⚠️ [Error] 업데이트 프로세스 시작 실패: {e}"
            self.log(msg_err)
            print(msg_err)
            self.is_updating = False

    def _handle_remote_restart(self):
        """단순 재시작 처리"""
        self.send_telegram_message("🔄 <b>[원격 재시작]</b>\n프로그램을 다시 시작합니다.")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        restart_script = os.path.join(script_dir, "run_app.bat")
        subprocess.Popen(["cmd.exe", "/c", restart_script], cwd=script_dir, creationflags=0x10)
        self.root.after(500, self.on_close)


if __name__ == "__main__":
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except: pass
    
    import sys
    auto_start = "--auto-start" in sys.argv
    
    try:
        app = KiwoomApp(root, auto_start=auto_start)
        root.mainloop()
    except Exception as e:
        import traceback
        err_details = traceback.format_exc()
        try:
            print(err_details)
        except:
            # CP949 인코딩 에러 방지 (특수문자 제거)
            safe_details = err_details.encode('ascii', errors='replace').decode('ascii')
            print(safe_details)
        # 팝업으로 에러 상세 표시 (깜빡임 종료 방지)
        messagebox.showerror("Fatal Error", f"프로그램 시작 중 오류가 발생했습니다:\n\n{e}\n\n상세 정보:\n{err_details[:500]}...")
