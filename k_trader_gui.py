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
from reporting.trade_report import generate_trade_report
from reporting.virtual_report import generate_virtual_narrative_report
from strategy.data_loader import BacktestDataLoader

# Modularized UI Tabs [v4.5.3 Restructured]
from ui.tabs.dashboard_tab import DashboardTab
from ui.tabs.monitor_tab import MonitorTab
from ui.tabs.settings_tab import SettingsTab
from ui.tabs.virtual_tab import VirtualTab
from ui.tabs.backtest_tab import BacktestTab

from ui.gui_utils import GuiUtils
from ui.styles import NordicStyle

# [v3.8] Core Modules extraction
from core.portfolio_manager import PortfolioManager
from core.market_service import MarketDataService
from core.remote_commander import RemoteCommander
from core.trading_controller import TradingController
from core.strategy_engine import StrategyEngine
from core.selection_service import SelectionService
from core.defaults import DEFAULT_SETTINGS, get_allowed_keys

from kiwoom_rest_api.auth.token import TokenManager

# [v3.5] Kiwoom REST API 전역 설정
api_config = None
try:
    import kiwoom_rest_api.config as api_config
    import kiwoom_rest_api.auth.token as api_token
    from config import APP_KEY, APP_SECRET, USE_MOCK
    
    m_key = getattr(config, 'MOCK_APP_KEY', "")
    m_sec = getattr(config, 'MOCK_APP_SECRET', "")
    
    if USE_MOCK and m_key:
        api_config.API_KEY = m_key
        api_config.API_SECRET = m_sec
    else:
        api_config.API_KEY = APP_KEY
        api_config.API_SECRET = APP_SECRET
    
    os.environ['KIWOOM_API_KEY'] = api_config.API_KEY
    os.environ['KIWOOM_API_SECRET'] = api_config.API_SECRET
    api_config.USE_SANDBOX = USE_MOCK
    os.environ['KIWOOM_USE_SANDBOX'] = 'true' if USE_MOCK else 'false'
    
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

class AuthManager:
    def __init__(self, logger_callback=None):
        self.access_token = None
        self.log = logger_callback or print
        
    def get_auth_token(self):
        try:
            from config import APP_KEY, APP_SECRET
            tm = TokenManager()
            self.access_token = tm.get_token()
            if self.access_token:
                self.log("✅ [Auth] REST API 토큰 인증 성공")
                return self.access_token
        except Exception as e:
            self.log(f"❌ [Auth] 인증 실패: {e}")
        return None

    def get_token(self):
        """[v4.0.1] API Client 호환용 인터페이스"""
        return self.get_auth_token()

class ConfigHandler:
    def __init__(self, logger_callback=None):
        self.log = logger_callback or print
        self.settings_file = "settings.json"
        self.cache_file = "bars_cache.json"

    def load_settings(self, default_settings):
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    return {**default_settings, **json.load(f)}
            except Exception as e:
                self.log(f"Error loading settings: {e}")
        return default_settings.copy()

    def save_settings(self, settings):
        try:
            with open(self.settings_file, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=4, ensure_ascii=False)
            return True
        except Exception as e:
            self.log(f"Error saving settings: {e}")
            return False

    def load_bars_cache(self):
        if os.path.exists(self.cache_file):
            try:
                import io
                with open(self.cache_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return {c: pd.read_json(io.StringIO(j)) for c, j in data.items()}
            except: pass
        return {}

    def save_bars_cache(self, bars_data):
        try:
            data = {c: df.to_json() for c, df in bars_data.items() if not df.empty}
            with open(self.cache_file, 'w', encoding='utf-8') as f:
                json.dump(data, f)
            return True
        except: return False

class KiwoomApp:
    @property
    def access_token(self): return self.auth_manager.access_token
    @access_token.setter
    def access_token(self, value): self.auth_manager.access_token = value
        
    @property
    def virtual_positions(self): return self.portfolio.virtual_positions
    
    def __init__(self, root, auto_start=False):
        self.root = root
        self.auto_start = auto_start
        self.root.title("K-Trader v4.0.0 [Remote Monitoring]")
        self.root.geometry("1400x900")
        
        self.gui_utils = GuiUtils(root=self.root)
        self.auth_manager = AuthManager(logger_callback=self.log)
        self.config_handler = ConfigHandler(logger_callback=self.log)
        self.data_manager = DataManager()
        self.config = config
        self.nordic_style = NordicStyle(self.root)

        self.market_trend = "NEUTRAL"
        self._last_m_trend = None
        self.is_monitoring = False

        self.notebook = None
        self.status_label = None
        self.pending_tree_updates = {} # [v4.5.3] UI 지연 업데이트용 큐
        
        self.settings = self.config_handler.load_settings(DEFAULT_SETTINGS)
        
        self.bars_data = self.config_handler.load_bars_cache() 
        self.volume_profiles = {} # [v4.0] 수급 분석 프로파일 (일일)
        self.min_volume_profiles = {} # [v4.0] 수급 분석 프로파일 (분봉)

        # [v4.5.7] Essential attributes for SignalEngine
        from core.stream_processor import StreamProcessor
        self.stream_processor = StreamProcessor(self.settings, logger_callback=self.log)
        self.streaam_processor = self.stream_processor # [Typo Alias Safety]
        self.rsi_signals = {}
        self._update_timer_active = False # [v4.5.7] UI 업데이트 타이머 상태
        self._updaate_timer_active = False # [Typo Alias Safety]
        
        from core.order_manager import OrderManager
        from core.signal_engine import SignalEngine
        self.order_manager = OrderManager(self) 
        self.signal_engine = SignalEngine(self)

        # [v3.8] Core 매니저 통합 (Service Layer 중심)
        self.portfolio = PortfolioManager(
            self,
            self.data_manager, 
            self.order_manager, 
            settings=self.settings,
            log_func=self.log,
            gui_callback=self._render_virtual_positions,
            telegram_callback=self.send_telegram_message
        )
        
        # [v16.50] 최적화 상태 데이터 중앙 집중화 (Singleton State Management)
        # 탭 전환 시 데이터 유실 방지를 위해 메인 앱 인스턴스에서 직접 관리
        self.opt_params_vars = {}
        opt_keys = [
            # 자금관리
            's1_equity_pct', 's2_equity_pct',
            # 종목선정필터 (Track A)
            'min_trde_val', 'vol_ratio', 'vol_lookback', 
            'ma_convergence', 'sel_rsi_max', 'sel_rsi_min',
            # 종목선정필터 (Track B)
            'sel_b_rsi_min', 'sel_b_rsi_max', 'sel_b_min_trde_val',
            # 단계별 매수 (A)
            'buy_rsi_max_a', 'gc_window', 'rsi_window', 'vol_surge', 'atr_multiplier_sl_a', 'atr_multiplier_trail_a',
            # 단계별 매수 (B)
            'buy_rsi_max_b', 'vol_surge_b', 'atr_multiplier_sl_b', 'atr_multiplier_trail_b',
            # 2차 추매 (S2)
            's2_vol_ratio', 's2_profit_limit', 's2_delay_hours', 'atr_multiplier_sl_s2', 'atr_multiplier_trail_s2',
            # 전술적 수급
            'smart_buy_vp', 'smart_buy_ba_ratio', 'smart_exit_vp', 'smart_exit_ba_ratio', 'smart_exit_price_threshold',
            # 공통/리스크
            'gap_down_limit', 'gap_grace_mins', 'atr_period', 
            's1_a_take_profit', 's1_a_stop_loss', 's1_b_take_profit', 's1_b_stop_loss',
            's2_take_profit', 's2_stop_loss', 
            's1_a_flash_exit_pct', 's1_b_flash_exit_pct', 's2_flash_exit_pct',
            # 시장 트렌드 필터 임계값 [v4.6]
            'msi_down_threshold', 'msi_up_threshold', 'adx_min_threshold', 'vix_down_threshold',
        ]
        for key in opt_keys:
            # 설정 파일에 저장된 값이 있으면 그것을 사용, 없으면 False 초기화
            saved_val = self.settings.get(f"opt_{key}", False)
            self.opt_params_vars[key] = tk.BooleanVar(value=saved_val)
        self.market_service = MarketDataService(
            self.data_manager,
            log_func=self.log,
            gui_callback=self._on_market_data_update
        )
        self.remote_commander = RemoteCommander(
            self,
            self.data_manager,
            log_func=self.log
        )
        self.trading_controller = TradingController(
            self, 
            status_callback=self._on_trading_status_changed,
            log_func=self.log
        )
        from core.warmup_service import WarmupService
        self.warmup_service = WarmupService(
            self, 
            logger_callback=self.log,
            progress_callback=self._on_warmup_progress
        )
        self.strategy_engine = StrategyEngine(self)
        self.selection_service = SelectionService(self, logger_callback=self.log)

        self.portfolio.load_all()
        self._setup_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        
        self.root.after(5000, self.remote_commander.start)
        if self.auto_start:
            self.log("[Auto] 8초 후 실시간 감시를 자동으로 시작합니다...")
            self.root.after(8000, self.toggle_monitoring)
        
    def _final_save_settings(self):
        if self.config_handler.save_settings(self.settings):
            self.log("💿 설정 파일 저장 및 데이터 동기화 완료")
            return True
        return False

    def reset_ui_tabs(self):
        self.settings = self.config_handler.load_settings(DEFAULT_SETTINGS)
        if hasattr(self, 'notebook'):
            curr_tab = self.notebook.index("current")
            for child in self.notebook.winfo_children(): child.destroy()
            self.notebook = None 
            self._setup_ui()
            self.notebook.select(curr_tab)
            self.log("모든 UI 탭이 초기화 데이터로 리로드되었습니다.")

    def _setup_ui(self):
        self.colors = self.nordic_style.colors

        if not hasattr(self, 'notebook') or self.notebook is None or not self.notebook.winfo_exists():
            self.header_frame = ttk.Frame(self.root, padding=(15, 8))
            self.header_frame.pack(side=tk.TOP, fill=tk.X)
            
            self.lbl_market_indices = ttk.Label(self.header_frame, text="시장 지수: 데이터 로딩 중...", 
                                              font=("Malgun Gothic", 10, "bold"), foreground=self.colors['primary'])
            self.lbl_market_indices.pack(side=tk.LEFT)

            status_frame = ttk.Frame(self.root)
            status_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=2)
            
            self.learn_status_frame = ttk.Frame(status_frame)
            self.learn_status_frame.pack(side=tk.LEFT, padx=30)
            
            self.lbl_iq_status = ttk.Label(self.learn_status_frame, text="🧠 IQ: 0 steps", font=("Malgun Gothic", 8), foreground="#8FBCBB")
            self.lbl_iq_status.pack(side=tk.LEFT, padx=10)
            
            self.lbl_vp_calib = ttk.Label(self.learn_status_frame, text="⚡ VP: -", font=("Malgun Gothic", 8), foreground="#88C0D0")
            self.lbl_vp_calib.pack(side=tk.LEFT, padx=10)
            
            self.lbl_ba_calib = ttk.Label(self.learn_status_frame, text="⚖️ BA: -", font=("Malgun Gothic", 8), foreground="#81A1C1")
            self.lbl_ba_calib.pack(side=tk.LEFT, padx=10)
            
            self.status_label = ttk.Label(status_frame, text="상태: 대기 중", font=("Malgun Gothic", 9), foreground=self.colors['primary'])
            self.status_label.pack(side=tk.RIGHT, padx=5)
            
            self.lbl_remote_status = ttk.Label(status_frame, text="원격: 비활성", font=("Malgun Gothic", 9), foreground=self.colors['border'])
            self.lbl_remote_status.pack(side=tk.LEFT, padx=10)
            
            self.notebook = ttk.Notebook(self.root)
            self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=0)
        
        self._tab_frames = {
            'dashboard': ttk.Frame(self.notebook),
            'monitor': ttk.Frame(self.notebook),
            'virtual': ttk.Frame(self.notebook),
            'backtest': ttk.Frame(self.notebook),
            'settings': ttk.Frame(self.notebook)
        }
        
        self.notebook.add(self._tab_frames['dashboard'], text=" 대시보드 ")
        self.notebook.add(self._tab_frames['monitor'], text=" 실시간 감시 ")
        self.notebook.add(self._tab_frames['virtual'], text=" 가상 포지션 ") 
        self.notebook.add(self._tab_frames['backtest'], text=" 백테스팅 ")
        self.notebook.add(self._tab_frames['settings'], text=" 전략 설정 ")

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        self.dashboard_tab = DashboardTab(self._tab_frames['dashboard'], self)
        self.monitor_tab = MonitorTab(self._tab_frames['monitor'], self)
        self.backtest_tab = BacktestTab(self._tab_frames['backtest'], self)
        self.settings_tab = SettingsTab(self._tab_frames['settings'], self, DEFAULT_SETTINGS)
        self.virtual_tab = VirtualTab(self._tab_frames['virtual'], self)
        
        # Compatibility links
        self.tree_monitor = self.monitor_tab.tree_monitor
        self.btn_monitor_toggle = self.monitor_tab.btn_monitor_toggle
        self.lbl_monitor_status = self.monitor_tab.lbl_monitor_status
        
        self.log("[System] 화면 구성 및 GUI 모듈 초기화 완료")
        if not hasattr(self, '_initial_threads_started'):
            self._initial_threads_started = True
            threading.Thread(target=self.get_auth_token, daemon=True).start()
        self._update_learning_status()

    def _update_learning_status(self):
        try:
            from strategy.simulation_models import SimulatedRealtimeMetrics
            if not getattr(SimulatedRealtimeMetrics, '_initialized', False): SimulatedRealtimeMetrics.load_params()
            p = SimulatedRealtimeMetrics.params
            self.lbl_iq_status.config(text=f"🧠 IQ: {p['total_calibrations']:,} steps")
            self.lbl_vp_calib.config(text=f"⚡ VP: x{p['vp_multiplier']:.1f}")
            self.lbl_ba_calib.config(text=f"⚖️ BA: x{p['ba_multiplier']:.1f}")
        except: pass

    def _on_market_data_update(self, indices=None, trend=None, display_text=None):
        if trend and self.market_trend != trend:
            self.market_trend = trend
            self.root.after(0, lambda: self._update_market_visuals(trend))
        if display_text and self.lbl_market_indices:
            self.root.after(0, lambda: self.lbl_market_indices.config(text=display_text))

    def _on_tab_changed(self, event):
        tab_text = self.notebook.tab(self.notebook.select(), "text")
        if "백테스팅" in tab_text and hasattr(self, 'backtest_tab'):
            # [v26.1] 탭 전환 시 현재 선택된 최적화 항목만 컬럼으로 표시하도록 즉시 리프레시
            self.backtest_tab._refresh_columns_from_settings()

    def log(self, message): self.gui_utils.log(message)

    def get_auth_token(self):
        token = self.auth_manager.get_auth_token()
        if token:
            self.root.after(0, lambda: self.status_label.config(text="상태: 로그인 완료", foreground=self.colors['success']))
            self.portfolio.load_all()
            self.market_service.start()
            self.remote_commander.start()
            self.root.after(2000, lambda: self.portfolio.sync_with_actual_account(silent=True))
            threading.Thread(target=lambda: self.get_kospi_200(force=True), daemon=True).start()

    def get_kospi_200(self, force=False):
        self.log("[Data] KOSPI 종목 리스트 수집 중...")
        df_200 = self.data_manager.get_kospi_list(count=200, force=force)
        if df_200 is not None: 
            self.log(f"[Data] 감시 대상 {len(df_200)}개 종목 로드 완료")
            # [v4.5.2] 종목 선정 버튼 활성화
            if hasattr(self, 'dashboard_tab') and hasattr(self.dashboard_tab, 'btn_select'):
                self.root.after(0, lambda: self.dashboard_tab.btn_select.config(state=tk.NORMAL))

    def _on_realtime_data_received(self, code, now_price, volume, trde_val, **kwargs):
        # [v4.5.45] 모든 실시간 연산 및 UI 업데이트 권한을 SignalEngine으로 일원화
        self.signal_engine.on_tick_received(code, now_price, volume, trde_val, **kwargs)

    def _on_execution_received(self, code, order_no, qty, price, side):
        self.data_manager.update_order_status(code, order_no, "FILLED", filled_price=price, filled_qty=qty)
        snapshot = self.portfolio.capture_snapshot(code, price, reason=f"EXEC_{side.upper()}")
        self.portfolio.on_fill(code, side, price, qty, snapshot=snapshot)
        self.log(f"✅ [체결] {code} {'매수' if side=='buy' else '매도'} {qty}주 완료 ({price:,}원)")

    def _update_market_visuals(self, trend):
        c = self.nordic_style.colors
        bg_col, fg_col = {"UP":(c['bg'], c['primary']), "NEUTRAL":("#3B4252","#E5E9F0"), "DOWN":("#3B2E2E","#BF616A")}.get(trend, (c['bg'], c['primary']))
        style = ttk.Style()
        style.configure("Header.TFrame", background=bg_col)
        if hasattr(self, 'header_frame'): self.header_frame.config(style="Header.TFrame")
        if hasattr(self, 'lbl_market_indices'): self.lbl_market_indices.config(background=bg_col, foreground=fg_col)
        if hasattr(self, 'monitor_tab'): self.monitor_tab._update_visual_theme(trend)
        
    def _display_selected_targets(self, df):
        """[v2.9] 선정된 종목 리스트를 팝업으로 보여주고 확정"""
        popup = tk.Toplevel(self.root)
        popup.title("🎯 선정 종목 확인")
        popup.geometry("600x450")
        popup.configure(bg="#2E3440")
        
        ttk.Label(popup, text=f"검색된 후보: {len(df)}개 (전략 점수순)", font=("Malgun Gothic", 10, "bold")).pack(pady=10)
        
        tree_f = ttk.Frame(popup)
        tree_f.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)
        
        cols = ("Rank", "Code", "Name", "Score", "Reason")
        tree = ttk.Treeview(tree_f, columns=cols, show="headings", height=10)
        for c in cols: tree.heading(c, text=c); tree.column(c, width=80, anchor=tk.CENTER)
        tree.column("Name", width=120, anchor=tk.W)
        tree.column("Reason", width=180, anchor=tk.W)
        
        sb = ttk.Scrollbar(tree_f, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        
        # [v4.5.6] Normalize ALL columns for subsequent DataFrame access (confirm function etc)
        df.columns = [c.lower() for c in df.columns]
        
        # [v4.5.5] Robust Key Mapping (Loop)
        def safe_get(row, key, default=""):
            for k in [key, key.upper(), 'stk_cd']:
                if k in row: return row[k]
            return default

        for i, row in df.iterrows():
            c_code = str(safe_get(row, 'code', '000000')).zfill(6)
            c_name = safe_get(row, 'name', 'Unknown')
            c_score = safe_get(row, 'score', 0.0)
            c_reason = safe_get(row, 'reason', '전략 조건 수렴')
            
            try: f_score = float(c_score)
            except: f_score = 0.0
            tree.insert("", tk.END, values=(i+1, c_code, c_name, f"{f_score:.1f}", c_reason))
            
        def confirm():
            # [v4.5.6] Service Layer를 통해 종목 등록 일원화
            self.trading_controller.set_monitor_targets(df)
            self.monitor_codes = self.trading_controller.monitor_codes
            self.monitor_info = self.trading_controller.monitor_info
            
            if hasattr(self, 'monitor_tab'):
                self.monitor_tab.update_watchlist(self.monitor_info)
                
            self.log(f"✅ {len(self.monitor_codes)}개 종목이 실시간 감시 대상으로 확정되었습니다.")
            popup.destroy()
            
        ttk.Button(popup, text="확정 및 감시 등록", command=confirm, style="Accent.TButton").pack(pady=15)

    def _render_tree_updates(self):
        if not self.monitor_tab or not self.is_monitoring: return
        self.monitor_tab.update_view(self.pending_tree_updates, self.portfolio.virtual_positions)
        self.root.after(200, self._render_tree_updates)

    def toggle_monitoring(self): return self.trading_controller.toggle()

    def _on_trading_status_changed(self, status, data):
        if status == "SHOW_WARNING": messagebox.showwarning("경고", data)
        elif status == "STATUS_TEXT": 
            if hasattr(self, 'lbl_monitor_status'): self.lbl_monitor_status.config(text=data, foreground=self.colors['primary'])
        elif status == "TARGETS_LOADED":
            self.monitor_codes = self.trading_controller.monitor_codes
            self.monitor_info = self.trading_controller.monitor_info
            if hasattr(self, 'monitor_tab'): self.monitor_tab.load_targets(data)
        elif status == "STARTED":
            self.is_monitoring = True
            if self.btn_monitor_toggle: self.btn_monitor_toggle.config(text="⏹️ 감시 중단", style="Warning.TButton")
            if self.lbl_monitor_status: self.lbl_monitor_status.config(text=f"상태: 감시 중 ({data}종목)", foreground=self.colors['success'])
            self._render_tree_updates()
        elif status == "STOPPED":
            self.is_monitoring = False
            if self.btn_monitor_toggle: self.btn_monitor_toggle.config(text="▶️ 실시간 감시 시작", style="Accent.TButton")
            if self.lbl_monitor_status: self.lbl_monitor_status.config(text="상태: 대기 중", foreground=self.colors['primary'])

    def _on_warmup_progress(self, cur, total, name, code):
        def update():
            self.status_label.config(text=f"상태: 분석 중 ({cur}/{total})")
            if hasattr(self, 'monitor_tab'): self.monitor_tab.update_warmup(cur, total, name, code)
        self.root.after(0, update)

    def on_close(self):
        self.log("⏹️ [System] 프로그램을 종료합니다.")
        if hasattr(self, 'trading_controller'): self.trading_controller.stop()
        self.root.destroy()

    def _calculate_buy_amt(self, stage=1, is_simulation=False):
        return self.portfolio.calculate_order_quantity(stage, is_simulation)
        
    def _virtual_buy_stage_1(self, code, name, price, reason="TYPE_A"):
        return self.trading_controller.execute_buy_signal(code, name, price, reason)
        
    def _on_strategy_action_needed(self, code, action, reason, **kwargs):
        return self.trading_controller.handle_strategy_action(code, action, reason, **kwargs)

    def _recalculate_all_indicators(self, code):
        return self.strategy_engine.recalculate_indicators(code, self.bars_data, self.settings)

    def send_telegram_message(self, message): self.data_manager.send_telegram_message(message)
    def _render_virtual_positions(self): 
        if hasattr(self, 'virtual_tab'): self.virtual_tab._render_virtual_positions()
    def sync_with_actual_account(self, silent=False): self.portfolio.sync_with_actual_account(silent=silent)
    def _sync_account_deposit(self): 
        # [v4.9.0] 단순 예수금이 아닌 전체 계좌 종목 및 자산 동기화(Full Sync)로 업그레이드
        self.portfolio.sync_with_actual_account(silent=False)
    def _sell_all_virtual(self):
        if messagebox.askyesno("🚨 비상 청산", "모든 보유 종목을 즉시 시장가로 매도하시겠습니까?"):
            self.portfolio.sell_all()

    def _handle_remote_update(self):
        self.send_telegram_message("📦 [원격 배포 시작]")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        update_script = os.path.join(script_dir, "remote_update.ps1")
        if os.path.exists(update_script):
            subprocess.Popen(["powershell.exe", "-ExecutionPolicy", "Bypass", "-File", update_script], cwd=script_dir, creationflags=0x10)
            self.root.after(1000, self.on_close)

if __name__ == "__main__":
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except: pass
    import sys
    app = KiwoomApp(root, auto_start="--auto-start" in sys.argv)
    root.mainloop()
