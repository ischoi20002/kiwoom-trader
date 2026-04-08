


# -*- coding: utf-8 -*-
import tkinter as tk
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
from typing import Any, Optional, Set, Tuple

from backtest_engine import TradingStrategy
from data_manager_v2 import DataManagerV2 as DataManager
import strategy_selector
from reporting.trade_report import generate_trade_report
from reporting.virtual_report import generate_virtual_narrative_report
from reporting.realtime_session_logger import RealtimeSessionLogger
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
        # 실시간 감시 일별 JSONL (신호·주문·체결 추적)
        self.session_logger = RealtimeSessionLogger()
        self.config = config
        self.nordic_style = NordicStyle(self.root)

        self.market_trend = "NEUTRAL"
        self._last_m_trend = None
        self.is_monitoring = False

        self.notebook = None
        self.status_label = None
        self.pending_tree_updates = {} # [v4.5.3] UI 지연 업데이트용 큐
        # REST 선체결 vs 웹소켓 CHE 중복 on_fill 방지 (주문번호 기준)
        self._exchange_fill_order_ids: Set[str] = set()
        # 계좌 REST 주기 동기화(웹소켓 미제공 구간 보완)
        self._account_sync_job: Optional[Any] = None
        self._account_sync_job_running: bool = False
        self._virtual_tab_render_pending: bool = False
        self._virtual_tab_render_last_ts: float = 0.0
        # 웹소켓 틱 → 메인 스레드에서 배치 처리 (종목당 마지막 틱만 유지)
        self._rt_tick_latest: dict = {}
        self._tick_flush_after_id: Optional[Any] = None

        self.settings = self.config_handler.load_settings(DEFAULT_SETTINGS)
        
        self.bars_data = self.config_handler.load_bars_cache()

        # [v4.5.7] Essential attributes for SignalEngine
        from core.stream_processor import StreamProcessor
        self.stream_processor = StreamProcessor(self.settings, logger_callback=self.log)
        self.streaam_processor = self.stream_processor # [Typo Alias Safety]
        self.rsi_signals = {}
        self._update_timer_active = False # [v4.5.7] UI 업데이트 타이머 상태
        self._updaate_timer_active = False # [Typo Alias Safety]
        
        from core.order_manager import OrderManager
        from core.watchlist_store import WatchlistStore
        from core.signal_engine import SignalEngine
        self.watchlist_store = WatchlistStore(logger_callback=self.log)
        self.watchlist_synth_state = {}
        self._active_stream_codes = []
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
        # 최적화 대상(○/●): tools/optimization_params_audit.xlsx 사용자 표시(2026-04) 기준 72키
        opt_keys = [
            'stage1_amt', 's1_equity_pct', 'stage2_amt', 's2_equity_pct', 'type_balance_a_pct',
            'buy_rsi_min', 'buy_rsi_max_a', 'gc_window', 'rsi_window', 'ma60_min', 'ma60_max',
            'vol_surge', 'prev_vol_lookback_s1_a',
            'type_a_phase1_up_vol_surge_add', 'type_a_phase1_neutral_vol_surge_add',
            'type_a_phase1_up_ma60_shrink_pct', 'type_a_phase1_neutral_ma60_shrink_pct',
            'buy_rsi_max_b', 'vol_surge_b', 'prev_vol_lookback_s1_b',
            'type_b_phase1_up_vol_surge_add', 'type_b_phase1_neutral_vol_surge_add',
            's2_ma20_gap', 's2_ma5_gap', 's2_rsi_gap', 's2_macd_gap', 's2_vol_ratio',
            'prev_vol_lookback_s2_a', 's2_profit_limit', 's2_delay_hours',
            's2_b_ma20_gap', 's2_b_rsi_gap', 's2_b_macd_gap', 's2_b_vol_ratio',
            'prev_vol_lookback_s2_b', 's2_b_profit_limit', 's2_b_delay_hours',
            'smart_buy_vp', 'smart_buy_ba_ratio', 'smart_exit_vp', 'smart_exit_ba_ratio', 'smart_exit_price_threshold',
            'msi_down_threshold', 'msi_up_threshold', 'adx_min_threshold', 'vix_down_threshold',
            'gap_down_limit_a', 'gap_grace_mins_a', 'atr_period_a',
            's1_a_take_profit', 's1_a_stop_loss', 's1_a_flash_exit_pct',
            'atr_multiplier_sl_a', 'atr_multiplier_trail_a',
            's2_take_profit', 's2_stop_loss', 's2_flash_exit_pct',
            'atr_multiplier_sl_s2', 'atr_multiplier_trail_s2',
            'gap_down_limit_b', 'gap_grace_mins_b', 'atr_period_b',
            's1_b_take_profit', 's1_b_stop_loss', 's1_b_flash_exit_pct',
            'atr_multiplier_sl_b', 'atr_multiplier_trail_b',
            's2_b_take_profit', 's2_b_stop_loss', 's2_b_flash_exit_pct',
            'atr_multiplier_sl_s2_b', 'atr_multiplier_trail_s2_b',
        ]
        for key in opt_keys:
            # 설정 파일에 저장된 값이 있으면 그것을 사용, 없으면 False 초기화
            saved_val = self.settings.get(f"opt_{key}", False)
            self.opt_params_vars[key] = tk.BooleanVar(value=saved_val)
        self.market_service = MarketDataService(
            self.data_manager,
            log_func=self.log,
            gui_callback=self._on_market_data_update,
            settings_provider=lambda: getattr(self, "settings", {}) or {},
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
            stock_prep_callback=self._on_warmup_stock_prep,
        )
        self.strategy_engine = StrategyEngine(self)
        self.selection_service = SelectionService(self, logger_callback=self.log)

        self.portfolio.load_all()
        # VirtualTab·콜백이 TARGETS_LOADED 전에 참조할 수 있음 — 미초기화 시 AttributeError로 즉시 종료
        try:
            self.monitor_codes = list(self.trading_controller.monitor_codes or [])
        except Exception:
            self.monitor_codes = []
        try:
            self.monitor_info = dict(self.trading_controller.monitor_info or {})
        except Exception:
            self.monitor_info = {}

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
        try:
            self._last_vp_render_sig: Optional[Tuple[Any, ...]] = (
                self._vp_structure_signature()
            )
        except Exception:
            self._last_vp_render_sig = None

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
        # after(0) 콜백은 기본인자로 값을 고정해 클로저 지연 바인딩으로 라벨이 꼬이지 않게 함
        if trend is not None and str(trend).strip() != "":
            tr = str(trend).strip().upper()
            if self.market_trend != tr:
                self.market_trend = tr
                self.root.after(0, lambda t=tr: self._update_market_visuals(t))
        if display_text and self.lbl_market_indices:
            dt = str(display_text)
            self.root.after(0, lambda s=dt: self.lbl_market_indices.config(text=s))

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
            self.root.after(2000, lambda: self.portfolio.sync_with_actual_account(silent=True, force=True))
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
        """
        웹소켓 콜백은 비메인 스레드일 수 있음.
        SignalEngine·monitor_info 갱신은 메인 루프에서만 수행하고, 동일 종목은 한 프레임당 마지막 틱만 반영.
        """
        try:
            kw = dict(kwargs)
            r = getattr(self, "root", None)
            if r is None:
                self.signal_engine.on_tick_received(code, now_price, volume, trde_val, **kw)
                return
            key = str(code).zfill(6)
            self._rt_tick_latest[key] = (code, now_price, volume, trde_val, kw)
            if self._tick_flush_after_id is None:
                self._tick_flush_after_id = r.after(16, self._flush_realtime_tick_batch_on_main)
        except Exception:
            try:
                self.signal_engine.on_tick_received(
                    code, now_price, volume, trde_val, **dict(kwargs)
                )
            except Exception:
                pass

    def _flush_realtime_tick_batch_on_main(self) -> None:
        """누적된 실시간 틱을 메인 스레드에서 일괄 처리."""
        try:
            batch = dict(self._rt_tick_latest)
            self._rt_tick_latest.clear()
            for _k, tup in batch.items():
                try:
                    c, np0, vol, tv, kw = tup
                    self.signal_engine.on_tick_received(c, np0, vol, tv, **kw)
                except Exception as e:
                    try:
                        self.log(f"⚠️ [Tick] 처리 예외: {e}")
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            try:
                self._tick_flush_after_id = None
                if self._rt_tick_latest:
                    r2 = getattr(self, "root", None)
                    if r2 is not None:
                        self._tick_flush_after_id = r2.after(
                            16, self._flush_realtime_tick_batch_on_main
                        )
            except Exception:
                self._tick_flush_after_id = None

    def reconcile_exchange_fill_after_order_accept(
        self,
        code: str,
        order_no: str,
        side: str,
        qty: int,
        fill_price: float,
        name: str = "",
        reason: str = "",
    ) -> None:
        """
        REST 주문 접수 성공 직후 웹소켓 CHE가 오지 않을 때도 DB 체결가·가상포지션을 맞춥니다.
        CHE가 나중에 오면 동일 주문번호로 on_fill은 생략하고 체결가만 보정합니다.
        """
        try:
            oid = str(order_no or "").strip()
            # CHE가 먼저 처리된 경우 참고가로 덮어쓰지 않음
            if oid and oid in self._exchange_fill_order_ids:
                return
            if oid:
                self._exchange_fill_order_ids.add(oid)
            try:
                self.data_manager.update_order_status(
                    code, order_no, "FILLED", filled_price=int(fill_price), filled_qty=int(qty)
                )
            except Exception as ue:
                self.log(f"⚠️ [체결 동기화] DB 갱신 실패: {ue}")
            snapshot = self.portfolio.capture_snapshot(code, fill_price, reason=f"REST_{side.upper()}")
            self.portfolio.on_fill(
                code,
                side,
                fill_price,
                qty,
                name=name or None,
                reason=reason or None,
                snapshot=snapshot,
                fill_channel="exchange",
            )
            self.log(
                f"✅ [체결 동기화] REST 접수 직후 가상체결 {code} "
                f"{'매수' if side == 'buy' else '매도'} {qty}주 ({int(fill_price):,}원)"
            )
            try:
                tc = getattr(self, "trading_controller", None)
                if tc and oid:
                    tc.clear_limit_order_watch(oid)
            except Exception:
                pass
            self._schedule_account_sync_after_fill()
        except Exception as e:
            self.log(f"⚠️ [체결 동기화] 실패: {e}")

    def _schedule_account_sync_after_fill(self) -> None:
        """체결 후 키움 계좌 REST(총자산·예수금)를 한 번 더 맞춥니다."""
        try:
            r = getattr(self, "root", None)
            if r is not None:
                r.after(2500, lambda: self.portfolio.sync_with_actual_account(silent=True, force=True))
        except Exception:
            pass

    def _on_execution_received(self, code, order_no, qty, price, side):
        try:
            oid = str(order_no or "").strip()
            if oid and oid in self._exchange_fill_order_ids:
                try:
                    self.data_manager.update_order_status(
                        code, order_no, "FILLED", filled_price=price, filled_qty=qty
                    )
                except Exception as ue:
                    self.log(f"⚠️ [체결 보정] DB 갱신 실패: {ue}")
                self.log(f"ℹ️ [체결 보정] CHE 체결가 반영 {code} {price:,}원 (주문번호 {oid})")
                self._schedule_account_sync_after_fill()
                return
            try:
                self.data_manager.update_order_status(code, order_no, "FILLED", filled_price=price, filled_qty=qty)
            except Exception as ue:
                self.log(f"⚠️ [체결] DB 갱신 실패: {ue}")
            snapshot = self.portfolio.capture_snapshot(code, price, reason=f"EXEC_{side.upper()}")
            self.portfolio.on_fill(
                code, side, price, qty, snapshot=snapshot, fill_channel="exchange"
            )
            self.log(f"✅ [체결] {code} {'매수' if side=='buy' else '매도'} {qty}주 완료 ({price:,}원)")
            if oid:
                self._exchange_fill_order_ids.add(oid)
            try:
                tc = getattr(self, "trading_controller", None)
                if tc and oid:
                    tc.clear_limit_order_watch(oid)
            except Exception:
                pass
            self._schedule_account_sync_after_fill()
        except Exception as e:
            self.log(f"⚠️ [체결] 처리 실패: {e}")

    def _update_market_visuals(self, trend):
        c = self.nordic_style.colors
        theme = {
            "UP": (c["bg"], c["primary"]),
            "AGGRESSIVE": (c["bg"], "#A3BE8C"),
            "NEUTRAL": ("#3B4252", "#E5E9F0"),
            "DOWN": ("#3B2E2E", "#BF616A"),
        }
        bg_col, fg_col = theme.get(str(trend).strip().upper(), (c["bg"], c["primary"]))
        style = ttk.Style()
        style.configure("Header.TFrame", background=bg_col)
        if hasattr(self, 'header_frame'): self.header_frame.config(style="Header.TFrame")
        if hasattr(self, 'lbl_market_indices'): self.lbl_market_indices.config(background=bg_col, foreground=fg_col)
        if hasattr(self, 'monitor_tab'): self.monitor_tab._update_visual_theme(trend)

    def _render_tree_updates(self):
        if not self.monitor_tab or not self.is_monitoring:
            return
        self.monitor_tab.update_view(self.pending_tree_updates, self.portfolio.virtual_positions)
        try:
            self.monitor_tab.maybe_reconcile_monitor_trees(
                getattr(self, "monitor_info", None) or {},
                self.portfolio.virtual_positions,
            )
        except Exception:
            pass
        self.root.after(200, self._render_tree_updates)

    def stream_subscription_codes(self):
        """웹소켓 등록 후보: 감시 종목 ∪ 관심목록(중복 제거). 워밍업 직후에는 _active_stream_codes 우선."""
        ac = getattr(self, "_active_stream_codes", None)
        if isinstance(ac, list) and ac:
            return list(ac)
        try:
            mc = list(getattr(self.trading_controller, "monitor_codes", []) or [])
        except Exception:
            mc = []
        try:
            mc = mc or list(self.monitor_codes or [])
        except Exception:
            pass
        try:
            wl = self.watchlist_store.get_codes()
        except Exception:
            wl = []
        base = [str(c).zfill(6) for c in list(mc) + list(wl)]
        try:
            for c in (getattr(self.portfolio, "virtual_positions", None) or {}).keys():
                base.append(str(c).zfill(6))
        except Exception:
            pass
        seen = set()
        out = []
        for c in base:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def _maybe_start_account_sync_timer(self) -> None:
        """감시 중이거나 보유가 있으면 REST 계좌 주기 갱신을 시작합니다."""
        try:
            if self._account_sync_job_running:
                return
            if not (self.is_monitoring or len(self.portfolio.virtual_positions) > 0):
                return
            self._account_sync_job_running = True
            self._periodic_account_refresh_loop()
        except Exception:
            pass

    def _maybe_stop_account_sync_timer(self) -> None:
        """감시 중단 후 보유도 없으면 주기 갱신을 중지합니다."""
        try:
            if self.is_monitoring or len(self.portfolio.virtual_positions) > 0:
                return
            if self._account_sync_job is not None:
                try:
                    self.root.after_cancel(self._account_sync_job)
                except Exception:
                    pass
                self._account_sync_job = None
            self._account_sync_job_running = False
        except Exception:
            pass

    def _periodic_account_refresh_loop(self) -> None:
        """약 30초마다 REST로 총자산·예수금·보유를 동기화합니다 (키움 계좌 WS 스트림 없음)."""
        try:
            if not self.root.winfo_exists():
                return
            if self.is_monitoring or len(self.portfolio.virtual_positions) > 0:
                self.portfolio.sync_with_actual_account(silent=True, force=True)
        except Exception:
            pass
        try:
            if self.is_monitoring or len(self.portfolio.virtual_positions) > 0:
                self._account_sync_job = self.root.after(30000, self._periodic_account_refresh_loop)
            else:
                self._account_sync_job = None
                self._account_sync_job_running = False
        except Exception:
            self._account_sync_job_running = False

    def request_virtual_tab_realtime_render(self) -> None:
        """
        보유 탭 실시간 수익·평가 요약을 스로틀하여 갱신합니다.
        웹소켓(asyncio) 스레드에서 호출되므로 Treeview/StringVar는 메인 스레드로만 위임합니다.
        """
        try:
            if not hasattr(self, "virtual_tab"):
                return
            r = getattr(self, "root", None)
            if r is None:
                return
            r.after(0, self._virtual_tab_render_tick_on_main)
        except Exception:
            pass

    def _virtual_tab_render_tick_on_main(self) -> None:
        """메인 스레드 전용: 스로틀 후 _render_real_data 호출."""
        import time
        try:
            if not hasattr(self, "virtual_tab"):
                return
            now = time.time()
            if now - self._virtual_tab_render_last_ts < 0.12:
                if not self._virtual_tab_render_pending:
                    self._virtual_tab_render_pending = True
                    self.root.after(120, self._flush_virtual_tab_realtime_render)
                return
            self._virtual_tab_render_last_ts = now
            self.virtual_tab._render_real_data()
        except Exception:
            pass

    def _flush_virtual_tab_realtime_render(self) -> None:
        """스로틀 대기 후 한 번만 렌더합니다 (이미 메인 스레드에서 호출)."""
        import time
        try:
            self._virtual_tab_render_pending = False
            self._virtual_tab_render_last_ts = time.time()
            if hasattr(self, "virtual_tab"):
                self.virtual_tab._render_real_data()
        except Exception:
            pass

    def _ensure_vp_monitor_stub(self, code: str) -> None:
        """보유만 있고 감시에 없던 종목에 monitor_info 스텁을 넣고 스트림 재등록을 요청합니다."""
        try:
            nk = str(code).split("_")[0].zfill(6)
            if nk in self.monitor_info:
                return
            vp = getattr(self.portfolio, "virtual_positions", None) or {}
            pos = vp.get(nk) or vp.get(code)
            if not pos:
                return
            nm = pos.get("name") or nk
            ap = float(pos.get("avg_price", 0) or 0)
            stub = {
                "name": nm,
                "ma20": 0.0,
                "ma60": 0.0,
                "prev_vol": 0.0,
                "score": 0.0,
                "ma_buffer_scale": 1.0,
                "open": ap,
                "last_price": ap,
                "sel_track": "AB",
            }
            self.monitor_info[nk] = stub
            try:
                self.trading_controller.monitor_info[nk] = stub
            except Exception:
                pass
            if hasattr(self, "monitor_tab"):
                self.monitor_tab._pending_reg_refresh = True
        except Exception:
            pass

    def toggle_monitoring(self): return self.trading_controller.toggle()

    def stop_monitoring(self) -> None:
        """
        웹소켓 워커 종료(finally) 등에서 호출되는 감시 중단 훅.
        (기존 monitor_tab → app.stop_monitoring 경로와 호환)
        """
        try:
            if getattr(self.trading_controller, "is_monitoring", False):
                self.trading_controller.stop()
        except Exception as e:
            try:
                self.log(f"⚠️ [System] stop_monitoring: {e}")
            except Exception:
                pass

    def _on_trading_status_changed(self, status, data):
        if status == "SHOW_WARNING": messagebox.showwarning("경고", data)
        elif status == "STATUS_TEXT": 
            if hasattr(self, 'lbl_monitor_status'): self.lbl_monitor_status.config(text=data, foreground=self.colors['primary'])
        elif status == "TARGETS_LOADED":
            self.monitor_codes = self.trading_controller.monitor_codes
            self.monitor_info = self.trading_controller.monitor_info
            if hasattr(self, "monitor_tab"):
                self.monitor_tab.update_watchlist(self.monitor_info)
        elif status == "WARMUP_BEGIN":
            codes = data if isinstance(data, list) else []
            if hasattr(self, "monitor_tab"):
                self.monitor_tab.reset_warmup_row_states(codes)
        elif status == "WARMUP_FAILED":
            results = (data or {}).get("results", {})
            failed = (data or {}).get("failed", [])
            lines = []
            for c in failed:
                r = results.get(c, {})
                mi = getattr(self, "monitor_info", None) or {}
                nm = mi.get(c, {}).get("name", c)
                lines.append(f"• {nm} ({c}): {r.get('reason', '알 수 없음')}")
            body = (
                "다음 종목의 15분봉·장중 구간을 API·DB로 채우지 못해 감시를 시작할 수 없습니다.\n\n"
                + "\n".join(lines)
            )
            self.root.after(
                0, lambda msg=body: messagebox.showerror("워밍업 실패", msg)
            )
            if self.lbl_monitor_status:
                self.lbl_monitor_status.config(
                    text="상태: 워밍업 실패 — 감시 미시작",
                    foreground=self.colors["danger"],
                )
        elif status == "MONITOR_CODES_FILTERED":
            self.monitor_codes = list((data or {}).get("codes", []))
            self.trading_controller.monitor_codes = self.monitor_codes
            if hasattr(self, "monitor_tab"):
                self.monitor_tab.update_watchlist(self.monitor_info)
        elif status == "STARTED":
            self.is_monitoring = True
            try:
                self.monitor_info = dict(self.trading_controller.monitor_info or {})
            except Exception:
                pass
            self._maybe_start_account_sync_timer()
            if self.btn_monitor_toggle: self.btn_monitor_toggle.config(text="⏹️ 감시 중단", style="Warning.TButton")
            if self.lbl_monitor_status: self.lbl_monitor_status.config(text=f"상태: 감시 중 ({data}종목)", foreground=self.colors['success'])
            if self.status_label:
                self.status_label.config(
                    text=f"상태: 감시 중 ({data}종목)",
                    foreground=self.colors["success"],
                )
            self._render_tree_updates()
        elif status == "STOPPED":
            self.is_monitoring = False
            self._active_stream_codes = []
            self._maybe_stop_account_sync_timer()
            if self.btn_monitor_toggle: self.btn_monitor_toggle.config(text="▶️ 실시간 감시 시작", style="Accent.TButton")
            if self.lbl_monitor_status: self.lbl_monitor_status.config(text="상태: 대기 중", foreground=self.colors['primary'])
            if self.status_label:
                self.status_label.config(text="상태: 대기 중", foreground=self.colors["primary"])

    def _on_warmup_stock_prep(
        self, event: str, cur: int, total: int, name: str, code: str, reason: str = ""
    ) -> None:
        """워커 스레드 → 메인 스레드: 종목별 워밍업 상태(트리·라벨)."""

        def update() -> None:
            try:
                if hasattr(self, "monitor_tab"):
                    self.monitor_tab.update_warmup_stock_prep(
                        event, cur, total, name, code, reason
                    )
                if hasattr(self, "lbl_monitor_status"):
                    self.lbl_monitor_status.config(
                        text=f"상태: 데이터 준비 중 ({cur}/{total}) {name}",
                        foreground=self.colors["primary"],
                    )
                if self.status_label:
                    self.status_label.config(
                        text=f"상태: 데이터 준비 ({cur}/{total}) {name}"
                    )
            except Exception as e:
                self.log(f"⚠️ [UI] 워밍업 표시 갱신 실패: {e}")

        self.root.after(0, update)

    def on_close(self):
        self.log("⏹️ [System] 프로그램을 종료합니다.")
        if hasattr(self, 'trading_controller'): self.trading_controller.stop()
        self.root.destroy()

    def _calculate_buy_amt(self, stage=1, is_simulation=False):
        return self.portfolio.calculate_order_quantity(stage, is_simulation)
        
    def _virtual_buy_stage_1(self, code, name, price, reason="TYPE_A"):
        return self.trading_controller.execute_buy_signal(code, name, price, reason)

    def _on_buy_signal_received(
        self,
        code: str,
        now_price: float,
        rsi_sig: float,
        macd_val: float,
        osc_val: float,
        ma60_ratio: float,
    ) -> bool:
        """
        SignalEngine 1차 매수 포착 콜백.
        DynamicBacktester 와 동일하게: 확정 Sig_Type(동시신호·동점 B우선), TypeBalanceGate,
        (선택) 선정 트랙(sel_track) 교차 검증 후 주문.
        True: 주문 접수·가상체결 성공 — SignalEngine 매수 쿨타임 갱신에만 사용.
        """
        try:
            code = str(code).zfill(6)
            mi = getattr(self, "monitor_info", None) or {}
            info = mi.get(code, {})
            name = str(info.get("name", "Unknown"))
            detailed = info.get("last_detailed_sig")
            if detailed is None or getattr(detailed, "empty", True):
                self.log(f"⚠️ [Skip] {code} 신호 캐시 없음")
                try:
                    self.session_logger.log(
                        "signal_buy_skipped",
                        {"code": code, "name": name, "reason": "no_signal_cache"},
                    )
                except Exception:
                    pass
                return False
            last_sig = detailed.iloc[-1]
            row = last_sig.to_dict() if hasattr(last_sig, "to_dict") else dict(last_sig)
            sig_type = TradingStrategy.effective_s1_sig_type_for_buy(
                self.settings, row, self.portfolio.virtual_positions
            )
            # CSV/선정 단계에서 SelTrack(A/B/AB)을 넣은 경우에만 백테 트랙 검증과 동일
            tr = str(
                info.get("sel_track", info.get("SelTrack", info.get("seltrack", "AB")))
            ).strip().upper()
            if tr == "A" and sig_type == "TYPE_B":
                self.log(f"⚠️ [Skip] {name}({code}) Track A 종목에 Type B 신호")
                try:
                    self.session_logger.log(
                        "signal_buy_skipped",
                        {
                            "code": code,
                            "name": name,
                            "reason": "track_mismatch",
                            "detail": "track_A_vs_type_B",
                            "sig_type": sig_type,
                        },
                    )
                except Exception:
                    pass
                return False
            if tr == "B" and sig_type == "TYPE_A":
                self.log(f"⚠️ [Skip] {name}({code}) Track B 종목에 Type A 신호")
                try:
                    self.session_logger.log(
                        "signal_buy_skipped",
                        {
                            "code": code,
                            "name": name,
                            "reason": "track_mismatch",
                            "detail": "track_B_vs_type_A",
                            "sig_type": sig_type,
                        },
                    )
                except Exception:
                    pass
                return False
            if not TradingStrategy.type_balance_allows_new_s1(
                self.settings, sig_type, self.portfolio.virtual_positions
            ):
                self.log(f"⚠️ [Skip] {name}({code}) TypeBalanceGate (A/B 비중)")
                try:
                    self.session_logger.log(
                        "signal_buy_skipped",
                        {
                            "code": code,
                            "name": name,
                            "reason": "type_balance_gate",
                            "sig_type": sig_type,
                        },
                    )
                except Exception:
                    pass
                return False
            try:
                self.session_logger.log(
                    "signal_buy_accepted",
                    {
                        "code": code,
                        "name": name,
                        "price": float(now_price),
                        "sig_type": sig_type,
                        "sel_track": tr,
                        "rsi_sig": float(rsi_sig),
                        "macd": float(macd_val),
                        "osc": float(osc_val),
                        "ma60_ratio": float(ma60_ratio),
                    },
                )
            except Exception:
                pass
            return self.trading_controller.execute_buy_signal(
                code, name, now_price, reason=sig_type
            )
        except Exception as e:
            self.log(f"❌ [BuySignal] {code} 처리 실패: {e}")
            try:
                self.session_logger.log(
                    "signal_buy_error", {"code": code, "error": str(e)}
                )
            except Exception:
                pass
            try:
                traceback.print_exc()
            except Exception:
                pass
            return False

    def _on_strategy_action_needed(self, code, action, reason, **kwargs):
        return self.trading_controller.handle_strategy_action(code, action, reason, **kwargs)

    def _recalculate_all_indicators(self, code):
        return self.strategy_engine.recalculate_indicators(code, self.bars_data, self.settings)

    def send_telegram_message(self, message: str, parse_mode: Optional[str] = "HTML") -> None:
        try:
            ok = self.data_manager.send_telegram_message(message, parse_mode=parse_mode)
            if not ok:
                self.log("⚠️ [Telegram] 전송 실패 또는 미설정 — config.py 의 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 를 확인하세요.")
        except Exception as e:
            self.log(f"⚠️ [Telegram] 예외: {e}")
    def _vp_structure_signature(self) -> Tuple[Any, ...]:
        """
        virtual_positions 구조 + 종료 이력 건수.
        동일하면 예수금·총자산만 바뀐 동기화로 보고 전체 트리 리빌드를 생략한다.
        lock으로 스냅샷을 맞춰 동기화 중간 상태를 읽지 않는다.
        """
        try:
            with self.portfolio.lock:
                vp = dict(getattr(self.portfolio, "virtual_positions", None) or {})
                hlen = len(getattr(self.portfolio, "virtual_history", None) or [])
            parts: list = []
            for k, p in vp.items():
                nk = str(k).split("_")[0].zfill(6)
                qty = int(p.get("qty", 0) or 0)
                parts.append((nk, qty))
            return (tuple(sorted(parts)), int(hlen))
        except Exception:
            return (tuple(), 0)

    def _render_virtual_positions(self) -> None:
        """
        PortfolioManager gui_callback 진입점.
        포지션 구조가 이전과 같으면 히스토리·주문 트리 전체 삭제 없이 보유 셀·요약만 갱신한다.
        """
        try:
            self._maybe_start_account_sync_timer()
            try:
                self.trading_controller._ensure_virtual_positions_monitor_info()
            except Exception:
                pass
            try:
                self.monitor_info = dict(self.trading_controller.monitor_info or {})
            except Exception:
                pass
        except Exception:
            pass
        if not hasattr(self, "virtual_tab"):
            return
        try:
            sig = self._vp_structure_signature()
            prev = getattr(self, "_last_vp_render_sig", None)
            if prev is not None and sig == prev:
                self.virtual_tab.refresh_portfolio_light()
                return
        except Exception:
            pass
        try:
            self.virtual_tab._render_virtual_positions()
            self._last_vp_render_sig = self._vp_structure_signature()
        except Exception:
            pass

    def refresh_virtual_tab_full(self) -> None:
        """주문 로그 반영 등 히스토리·주문 트리까지 전부 다시 그릴 때 사용."""
        try:
            self._maybe_start_account_sync_timer()
            try:
                self.trading_controller._ensure_virtual_positions_monitor_info()
            except Exception:
                pass
            try:
                self.monitor_info = dict(self.trading_controller.monitor_info or {})
            except Exception:
                pass
        except Exception:
            pass
        if hasattr(self, "virtual_tab"):
            try:
                self.virtual_tab._render_virtual_positions()
            except Exception:
                pass
        try:
            self._last_vp_render_sig = self._vp_structure_signature()
        except Exception:
            pass
    def sync_with_actual_account(self, silent: bool = False, force: bool = False) -> None:
        self.portfolio.sync_with_actual_account(silent=silent, force=force)

    def _sync_account_deposit(self) -> None:
        # [v4.9.0] 전체 계좌 동기화 — 버튼 클릭 시 API 스로틀을 우회(force)
        self.portfolio.sync_with_actual_account(silent=False, force=True)
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
    import sys

    # Run Python File / 터미널에서 곧바로 출력이 보이도록 (버퍼링 완화·실행 여부 확인)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        print("K-Trader: 시작 중… (이 메시지가 보이면 터미널 연결은 정상)", flush=True)
    except Exception:
        pass

    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except: pass
    app = KiwoomApp(root, auto_start="--auto-start" in sys.argv)
    root.mainloop()
