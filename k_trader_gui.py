


# -*- coding: utf-8 -*-
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog
import requests
import pandas as pd
import os
import json
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
import queue
from typing import Any, Dict, List, Optional, Set, Tuple
from datetime import datetime, timedelta, time as dtime, date

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
from core.remote_commander import RemoteCommander
from core.trading_controller import TradingController, virtual_position_reference_price_for_monitor
from core.strategy_engine import StrategyEngine
from core.selection_service import SelectionService
from core.post_fill_account_sync_service import PostFillAccountSyncService
from core.realtime_tick_dispatch_service import RealtimeTickDispatchService
from core.eod_equity_snapshot_service import EodEquitySnapshotService
from core.rest_auth_retry_service import RestAuthRetryService
from core.fill_reconciliation_service import FillReconciliationService
from core.defaults import DEFAULT_SETTINGS, get_allowed_keys
from core.version import APP_VERSION
from core.supply_display import capture_supply_snapshot
from core.signal_callbacks_service import SignalCallbacksService
from core.ui_trading_status_service import UiTradingStatusService
from core.warmup_stock_prep_ui_service import WarmupStockPrepUiService

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

# 실시간 감시 탭 트리 UI 갱신 주기(ms). 초당 약 2.9회 — UI 부하 완화 시 320~450 권장.
REALTIME_TREE_UI_REFRESH_MS: int = 340

# 실시간 틱(감시 대상) 배치 flush 주기(ms).
# Tk 메인스레드에서 틱 배치 처리(SignalEngine)가 실행되므로,
# 너무 촘촘하면 탭 전환/화면 갱신이 멈춘 것처럼 느려질 수 있습니다.
REALTIME_TICK_DISPATCH_FLUSH_MS: int = 150


class AuthManager:
    """
    키움 REST 액세스 토큰 관리.
    동일 프로세스 내 반복 호출 시 OAuth 재요청·로그 스팸을 막기 위해 TTL 캐시를 둡니다.
    """

    # 키움 토큰은 통상 24h 내외 — 만료 전 갱신 여유를 두고 23시간 캐시
    _TOKEN_TTL_SEC: float = 23 * 3600

    def __init__(self, logger_callback=None):
        self.access_token: Optional[str] = None
        self.log = logger_callback or print
        self._token_lock = threading.Lock()
        self._token_issued_monotonic: float = 0.0

    def invalidate_cached_token(self) -> None:
        """401 등으로 토큰이 무효일 때 캐시를 비웁니다."""
        try:
            with self._token_lock:
                self.access_token = None
                self._token_issued_monotonic = 0.0
        except Exception:
            pass

    def get_auth_token(self) -> Optional[str]:
        try:
            now_m = time.monotonic()
            with self._token_lock:
                if (
                    self.access_token
                    and (now_m - self._token_issued_monotonic) < self._TOKEN_TTL_SEC
                ):
                    return self.access_token

                tm = TokenManager()
                token = tm.get_token()
                if token:
                    self.access_token = token
                    self._token_issued_monotonic = time.monotonic()
                    self.log("✅ [Auth] REST API 토큰 인증 성공")
                    return self.access_token
                self.access_token = None
                self._token_issued_monotonic = 0.0
        except Exception as e:
            self.log(f"❌ [Auth] 인증 실패: {e}")
        return None

    def get_token(self) -> Optional[str]:
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
        self._main_thread_id = threading.get_ident()
        self.auto_start = auto_start
        self.root.title(f"K-Trader v{APP_VERSION} [Remote Monitoring]")
        self.root.geometry("1400x900")
        
        self.gui_utils = GuiUtils(root=self.root)
        self.auth_manager = AuthManager(logger_callback=self.log)
        self.config_handler = ConfigHandler(logger_callback=self.log)
        # UI 상태 JSON 경로 고정: cwd 가 달라져도 k_trader_gui.py 와 같은 폴더를 쓴다
        # (예: 바로가기 실행 시 cwd 가 달라 monitor_column_widths.json 이 다른 위치에 생기던 문제 완화)
        try:
            self._app_root_dir = os.path.dirname(os.path.abspath(__file__))
        except Exception:
            self._app_root_dir = os.getcwd()
        self.data_manager = DataManager()
        # 실시간 감시 일별 JSONL (신호·주문·체결 추적)
        self.session_logger = RealtimeSessionLogger()
        self.config = config
        self.nordic_style = NordicStyle(self.root)

        self.is_monitoring = False
        # [EOD Equity Snapshot] 스냅샷 스케줄러(장중 토글과 무관하게 마감+버퍼 시점 기준 1회 저장)
        self._equity_snapshot_after_id: Optional[Any] = None
        self._equity_snapshot_pending_date: Optional[str] = None  # job 스폰용
        self._equity_snapshot_last_saved_date: Optional[str] = None  # 성공 저장된 날짜(YYYY-MM-DD)
        self._equity_snapshot_job_in_progress: bool = False
        self._equity_snapshot_cancelled: bool = False

        # [EOD Equity Snapshot] 스케줄러/저장 서비스 추출
        self.eod_equity_snapshot_service = EodEquitySnapshotService(
            self, log_func=self.log
        )

        self.notebook = None
        self.status_label = None
        self.pending_tree_updates = {} # [v4.5.3] UI 지연 업데이트용 큐
        # pending 경로가 누락되어도 숫자 열이 멎지 않도록 monitor_info 기반 최소 행 갱신 캐시
        self._ui_force_row_last_price: Dict[str, float] = {}
        self._ui_force_row_last_push_ts: Dict[str, float] = {}
        self._ui_force_row_rr_index: int = 0
        # REST 선체결 vs 웹소켓 CHE 중복 on_fill 방지 (주문번호 기준)
        self._exchange_fill_order_ids: Set[str] = set()
        # (구) 주기 계좌 동기화 타이머 취소용. 자동 동기화는 기동 후 1회만 수행한다.
        self._account_sync_job: Optional[Any] = None
        self._virtual_tab_render_pending: bool = False
        self._virtual_tab_render_last_ts: float = 0.0

        # 실시간 감시/계좌정보 텔레그램 스로틀
        self._telegram_sent_on_monitor_start: bool = False
        self._account_summary_after_trade_job_id: Optional[Any] = None
        self._account_summary_after_trade_seq: int = 0
        # (실시간 틱 배치 디스패치는 RealtimeTickDispatchService로 위임)
        # REST 인증 자동 재시도: 동시에 하나의 워커만 실행
        self._auth_worker_lock = threading.Lock()
        self._auth_worker_running = False
        # 포지션 탭 무거운 렌더 중 사용자에게 피드백(하단 라벨·커서)
        self._ui_busy: bool = False
        self.lbl_ui_reactivity: Optional[ttk.Label] = None
        # 대시보드 기동 프로그레스바: 첫 인증 워커 완료 후 숨김
        self._boot_progress_finished: bool = False
        # sync_with_actual_account 는 REST·DB·정합으로 수 초 걸릴 수 있음 — 백그라운드에서 한 번에 하나만
        self._portfolio_sync_thread_lock: threading.Lock = threading.Lock()
        # 체결 후 계좌 동기화: 연속 체결 시 after 예약이 겹치지 않도록 디바운스 + 2·3차 추격
        self._post_fill_sync_job: Optional[Any] = None
        self._post_fill_sync_followup: Optional[Any] = None
        self._post_fill_sync_followup2: Optional[Any] = None

        # 체결 후 계좌 동기화(after 1/2/3차) 서비스 추출
        self.post_fill_account_sync_service = PostFillAccountSyncService(
            self, log_func=self.log
        )

        self.settings = self.config_handler.load_settings(DEFAULT_SETTINGS)
        # Tick dispatch 주기는 SignalEngine이 메인 스레드를 과점유할 때 UI가 멎는 문제를 직접 줄이므로,
        # 애플리케이션 인스턴스에 모듈 상수값을 그대로 심어둔다.
        try:
            self.REALTIME_TICK_DISPATCH_FLUSH_MS = int(REALTIME_TICK_DISPATCH_FLUSH_MS)
        except Exception:
            self.REALTIME_TICK_DISPATCH_FLUSH_MS = 150
        
        self.bars_data = self.config_handler.load_bars_cache()
        self._bars_data_lock = threading.RLock()

        # [v4.5.7] Essential attributes for SignalEngine
        # 실시간 감시 전용 15분봉 DB(하베스팅 trading_data.db 와 분리) — 슬롯 변경 시 확정 봉 적재
        from core.realtime_monitor_bars_store import RealtimeMonitorBarsStore
        try:
            _rt15_ret = int(
                float(self.settings.get("realtime_15m_calendar_retention_days", 365))
            )
        except (TypeError, ValueError):
            _rt15_ret = 365
        _rt15_ret = max(1, min(_rt15_ret, 3650))
        self.realtime_monitor_bars_store = RealtimeMonitorBarsStore(
            logger_callback=self.log,
            retention_calendar_days=_rt15_ret,
        )
        from core.stream_processor import StreamProcessor

        _rt15_store = self.realtime_monitor_bars_store

        def _on_rt15_bar_closed(code, ts, o, h, l, c, v):
            try:
                _rt15_store.upsert_completed_bar(code, ts, o, h, l, c, v)
            except Exception as ex:
                try:
                    self.log(f"⚠️ [RT15mDB] 확정봉 저장 실패: {ex}")
                except Exception:
                    pass

        self.stream_processor = StreamProcessor(
            self.settings,
            logger_callback=self.log,
            on_fifteen_min_bar_closed=_on_rt15_bar_closed,
            bars_data_lock=self._bars_data_lock,
        )
        self.streaam_processor = self.stream_processor # [Typo Alias Safety]
        self.rsi_signals = {}
        self._update_timer_active = False # [v4.5.7] UI 업데이트 타이머 상태
        self._updaate_timer_active = False # [Typo Alias Safety]
        
        from core.order_manager import OrderManager
        from core.watchlist_store import WatchlistStore
        from core.signal_engine import SignalEngine
        self.watchlist_store = WatchlistStore(logger_callback=self.log)
        # [옵션 A] 실시간 감시 시작 직후 종목명 표시를 위해 kodex200.csv에서 Code/Name 선적재
        try:
            _added = self.watchlist_store.preload_from_kodex200_csv(logger_callback=self.log)
            if _added > 0:
                self.log(f"[Watchlist] kodex200.csv 이름 캐시 선적재 완료: { _added }개")
        except Exception:
            pass
        self.watchlist_synth_state = {}
        self._active_stream_codes = []
        self.order_manager = OrderManager(self)
        self.signal_engine = SignalEngine(self)

        # SignalEngine/StrategyEngine 콜백 처리 서비스(판정 로직 분리)
        self.signal_callbacks_service = SignalCallbacksService(
            self, log_func=self.log
        )
        # 실시간 틱 배치 디스패치 서비스(메인 스레드 after 예약 포함)
        self.tick_dispatch_service = RealtimeTickDispatchService(
            self, self.signal_engine, log_func=self.log
        )
        # 틱 워커 스레드가 넣은 매수·청산·UI 갱신 요청을 메인에서만 처리
        self._tick_main_actions_queue: "queue.SimpleQueue[tuple]" = queue.SimpleQueue()
        self._tick_worker_ui_dirty: bool = False
        self._tick_worker_virtual_dirty: bool = False
        self._tick_main_poller_after_id: Optional[Any] = None
        self._tick_main_poller_lock = threading.Lock()
        # strategy_action(특히 EXIT 주문 REST) 메인스레드 블로킹 방지용 비동기 실행 가드
        self._strategy_action_lock = threading.Lock()
        self._strategy_action_inflight_codes: Set[str] = set()
        self._strategy_action_last_submit_ts: Dict[str, float] = {}
        # UI 렌더 루프가 끊겼을 때 자동 재무장하기 위한 watchdog 상태
        self._ui_render_last_enter_ts: float = 0.0
        self._ui_watchdog_after_id: Optional[Any] = None

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
            'stage1_amt', 's1_equity_pct', 'stage2_amt', 's2_equity_pct',
            'buy_rsi_min', 'buy_rsi_max_a', 'gc_window', 'rsi_window', 'ma60_min', 'ma60_max',
            'vol_surge', 'prev_vol_lookback_s1_a',
            'buy_rsi_max_b', 'vol_surge_b', 'prev_vol_lookback_s1_b',
            's2_ma20_gap', 's2_ma5_gap', 's2_rsi_gap', 's2_macd_gap', 's2_vol_ratio',
            'prev_vol_lookback_s2_a', 's2_profit_limit', 's2_delay_hours',
            's2_b_ma20_gap', 's2_b_rsi_gap', 's2_b_macd_gap', 's2_b_vol_ratio',
            'prev_vol_lookback_s2_b', 's2_b_profit_limit', 's2_b_delay_hours',
            'smart_buy_vp', 'smart_buy_ba_ratio', 'smart_exit_vp', 'smart_exit_ba_ratio', 'smart_exit_price_threshold',
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
        self.remote_commander = RemoteCommander(
            self,
            self.data_manager,
            log_func=self.log
        )
        self.trading_controller = TradingController(
            self,
            status_callback=None,  # 서비스에서 바인딩
            log_func=self.log
        )

        # TradingController status 이벤트를 UI 갱신 서비스로 위임
        self.ui_trading_status_service = UiTradingStatusService(
            self, log_func=self.log
        )
        self.trading_controller.status_cb = (
            self.ui_trading_status_service.on_trading_status_changed
        )

        # REST 인증/재시도 워커 서비스 추출
        self.rest_auth_retry_service = RestAuthRetryService(
            self, log_func=self.log
        )

        # 체결 동기화(REST/WS) 서비스 추출
        self.fill_reconciliation_service = FillReconciliationService(
            self, log_func=self.log
        )

        # Warmup 워커별 종목 준비 상태 UI 브리지 서비스
        self.warmup_stock_prep_ui_service = WarmupStockPrepUiService(
            self, log_func=self.log
        )
        from core.warmup_service import WarmupService
        self.warmup_service = WarmupService(
            self,
            logger_callback=self.log,
            stock_prep_callback=self.warmup_stock_prep_ui_service.on_warmup_stock_prep,
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
        # EOD 스냅샷 자동 저장 시작(15:30 + buffer 분)
        try:
            self._start_eod_equity_snapshot_scheduler()
        except Exception:
            pass
        
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
            
            self.lbl_header_title = ttk.Label(
                self.header_frame,
                text=f"K-Trader v{APP_VERSION}",
                font=("Malgun Gothic", 10, "bold"),
                foreground=self.colors["primary"],
            )
            self.lbl_header_title.pack(side=tk.LEFT)

            status_frame = ttk.Frame(self.root)
            status_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=2)
            
            self.status_label = ttk.Label(status_frame, text="상태: 대기 중", font=("Malgun Gothic", 9), foreground=self.colors['primary'])
            self.status_label.pack(side=tk.RIGHT, padx=5)
            
            self.lbl_remote_status = ttk.Label(status_frame, text="원격: 비활성", font=("Malgun Gothic", 9), foreground=self.colors['border'])
            self.lbl_remote_status.pack(side=tk.LEFT, padx=10)
            # 무거운 UI 갱신(포지션 탭 전체 그리기 등) 중 표시 — 탭 전환이 잠시 먹지 않을 때 원인 안내
            self.lbl_ui_reactivity = ttk.Label(
                status_frame,
                text="",
                font=("Malgun Gothic", 9),
                foreground="#EBCB8B",
            )
            self.lbl_ui_reactivity.pack(side=tk.LEFT, padx=(16, 0))
            
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
        self.notebook.add(self._tab_frames['virtual'], text=" 포지션 관리 ")
        self.notebook.add(self._tab_frames['settings'], text=" 전략 설정 ")
        self.notebook.add(self._tab_frames['backtest'], text=" 전략검증 ")

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
            # 인증은 내부에서 백그라운드 워커로 재시도하므로 여기서는 추가 스레드 불필요
            self.get_auth_token()
        try:
            # UI 루프가 끊긴 상태(_update_timer_active만 True로 고정 등)를 자동 복구
            if self._ui_watchdog_after_id is None:
                self._ui_watchdog_after_id = self.root.after(
                    1200, self._ui_render_watchdog_tick
                )
        except Exception:
            pass

    def _on_tab_changed(self, event):
        tab_text = self.notebook.tab(self.notebook.select(), "text")
        if "전략검증" in tab_text and hasattr(self, "backtest_tab"):
            # [v26.1] 탭 전환 시 현재 선택된 최적화 항목만 컬럼으로 표시하도록 즉시 리프레시
            self.backtest_tab._refresh_columns_from_settings()
        # 포지션 탭은 기동 직후에는 Notebook 에 안 그려져 Paned 높이가 0 — 아침 첫 진입 시 sash 복원 필요
        if "포지션" in tab_text and hasattr(self, "virtual_tab"):
            try:
                self.virtual_tab._schedule_restore_virtual_sash()
            except Exception:
                pass
        # 실시간 감시 탭도 동일: 숨겨진 상태에서 _setup_ui 직후 복원하면 높이가 안 잡혀 하단이 가려진 것처럼 보일 수 있음
        if "실시간 감시" in tab_text and hasattr(self, "monitor_tab"):
            try:
                self.monitor_tab._schedule_restore_monitor_sash()
            except Exception:
                pass

    def _boot_progress_set_message(self, message: str) -> None:
        """대시보드 기동 프로그레스 아래 짧은 안내(메인 스레드에서만 위젯 갱신)."""
        def _apply() -> None:
            try:
                if getattr(self, "_boot_progress_finished", False):
                    return
                dt = getattr(self, "dashboard_tab", None)
                lbl = getattr(dt, "boot_progress_label", None)
                if lbl is not None and lbl.winfo_exists():
                    lbl.config(text=str(message))
            except Exception:
                pass

        try:
            r = getattr(self, "root", None)
            if r is not None and r.winfo_exists():
                r.after(0, _apply)
            else:
                _apply()
        except Exception:
            pass

    def _boot_progress_finish(self, *, ok: bool = True) -> None:
        """기동 인디터미네이트 바 종료 후 잠시 뒤 영역을 접습니다."""
        if getattr(self, "_boot_progress_finished", False):
            return
        self._boot_progress_finished = True

        def _apply() -> None:
            try:
                dt = getattr(self, "dashboard_tab", None)
                if dt is None:
                    return
                pb = getattr(dt, "boot_progress", None)
                fr = getattr(dt, "boot_progress_frame", None)
                lbl = getattr(dt, "boot_progress_label", None)
                if pb is not None and pb.winfo_exists():
                    try:
                        pb.stop()
                    except (tk.TclError, Exception):
                        pass
                    try:
                        pb.configure(mode="determinate", maximum=100, value=100)
                    except (tk.TclError, Exception):
                        pass
                if lbl is not None and lbl.winfo_exists():
                    try:
                        lbl.config(
                            text=(
                                "기동 완료"
                                if ok
                                else "기동 완료 — 인증 실패(로그 확인)"
                            )
                        )
                    except (tk.TclError, Exception):
                        pass

                def _hide_frame() -> None:
                    try:
                        if fr is not None and fr.winfo_exists():
                            fr.pack_forget()
                    except (tk.TclError, Exception):
                        pass

                try:
                    self.root.after(900, _hide_frame)
                except (tk.TclError, Exception):
                    pass
            except Exception:
                pass

        try:
            r = getattr(self, "root", None)
            if r is not None and r.winfo_exists():
                r.after(0, _apply)
            else:
                _apply()
        except Exception:
            pass

    def log(self, message): self.gui_utils.log(message)

    def _set_ui_busy(self, busy: bool, hint: str = "") -> None:
        """
        메인 스레드에서 Treeview 등 무거운 작업 시 사용자에게 피드백.
        Tk는 동기 렌더 중 이벤트를 못 받아 탭 전환이 밀리므로, 호출부에서는 가능하면
        `after(1, ...)` 로 한 틱 미룬 뒤 실제 작업을 수행한다.
        """
        try:
            self._ui_busy = bool(busy)
            lbl = getattr(self, "lbl_ui_reactivity", None)
            if lbl is not None and lbl.winfo_exists():
                if busy and (hint or "").strip():
                    lbl.config(text=f"처리 중: {hint.strip()}")
                elif not busy:
                    lbl.config(text="")
            r = getattr(self, "root", None)
            if r is not None and r.winfo_exists():
                try:
                    r.config(cursor=("watch" if busy else "arrow"))
                except (tk.TclError, AttributeError):
                    pass
        except Exception:
            pass

    def _run_portfolio_sync_in_daemon_thread(
        self,
        *,
        silent: bool = True,
        force: bool = False,
        refresh_cash_deposit: bool = True,
        wait_for_lock: bool = False,
    ) -> None:
        """
        계좌 동기화는 REST(ka85·예수 등)·정합·JSON 저장으로 메인 스레드에서 실행 시
        탭 전환·클릭이 수 초~10초 이상 막힐 수 있어 워커 스레드에서 실행한다.
        gui_callback 은 PortfolioManager 가 root.after(0) 으로 메인에 넘긴다.
        동시에 두 번 돌면 상태가 꼬일 수 있어 락으로 직렬화.

        wait_for_lock=False: 이미 동기화 중이면 이번 호출 생략(기동·체결 후 등).
        wait_for_lock=True: 최대 약 120초 대기 후 실행(대시보드「동기화」등 사용자 명시).

        refresh_cash_deposit=False: ka10085 보유 정합만 하고 예수금상세 등 현금 API는 생략.
        예수·D+n·출금가능은 체결 후 동기화 등 refresh_cash_deposit=True 경로에서만.
        """
        def _worker() -> None:
            acquired = False
            try:
                if wait_for_lock:
                    acquired = self._portfolio_sync_thread_lock.acquire(timeout=120.0)
                else:
                    acquired = self._portfolio_sync_thread_lock.acquire(blocking=False)
                if not acquired:
                    try:
                        if not silent:
                            self.log(
                                "⏳ [Sync] 다른 계좌 동기화가 진행 중입니다. "
                                "잠시 후 다시 시도하거나, 완료될 때까지 기다려 주세요."
                            )
                    except Exception:
                        pass
                    return
                self.portfolio.sync_with_actual_account(
                    silent=silent,
                    force=force,
                    refresh_cash_deposit=refresh_cash_deposit,
                )
            except Exception as e:
                try:
                    r = getattr(self, "root", None)
                    if r is not None:
                        r.after(
                            0,
                            lambda m=str(e): self.log(
                                f"❌ [Sync] 백그라운드 계좌 동기화 예외: {m}"
                            ),
                        )
                except Exception:
                    pass
            finally:
                if acquired:
                    try:
                        self._portfolio_sync_thread_lock.release()
                    except Exception:
                        pass

        try:
            threading.Thread(target=_worker, daemon=True).start()
        except Exception as e:
            try:
                self.log(f"❌ [Sync] 동기화 스레드 시작 실패: {e}")
            except Exception:
                pass

    def get_auth_token(self) -> None:
        """REST 토큰 발급을 서비스에 위임합니다."""
        try:
            self.rest_auth_retry_service.start()
        except Exception:
            pass

    def _run_auth_token_with_retries(self) -> None:
        """백그라운드 전용: 토큰 발급 재시도 후 성공 시에만 부가 초기화 수행."""
        # 서비스 구현으로 위임(호환용; 외부 호출 가능성 대비 유지)
        try:
            self.rest_auth_retry_service._run_auth_token_with_retries()
        except Exception:
            pass

    def get_kospi_200(self, force=False):
        try:
            idx_choice = self.settings.get("universe_index_choice", "KODEX200")
        except Exception:
            idx_choice = "KODEX200"
        self.log(f"[Data] 유니버스({idx_choice}) 로드(kodex200.csv/kospi_universe.csv)...")
        df_200 = self.data_manager.get_kospi_list(
            count=200,
            force=force,
            settings=getattr(self, "settings", None),
            universe_log=self.log,
        )
        if df_200 is not None: 
            self.log(f"[Data] 감시 대상 {len(df_200)}개 종목 로드 완료")
            # [v4.5.2] 종목 선정 버튼 활성화
            if hasattr(self, "monitor_tab") and hasattr(self.monitor_tab, "btn_select"):
                self.root.after(0, lambda: self.monitor_tab.btn_select.config(state=tk.NORMAL))

    def _on_realtime_data_received(self, code, now_price, volume, trde_val, **kwargs):
        """
        웹소켓 콜백은 비메인 스레드일 수 있음.
        SignalEngine·monitor_info 갱신은 메인 루프에서만 수행하고, 동일 종목은 한 프레임당 마지막 틱만 반영.
        """
        try:
            self.tick_dispatch_service.on_tick_received(
                code, now_price, volume, trde_val, **kwargs
            )
        except Exception:
            try:
                self.signal_engine.on_tick_received(
                    code, now_price, volume, trde_val, **dict(kwargs)
                )
            except Exception:
                pass

    def _flush_realtime_tick_batch_on_main(self) -> None:
        """누적된 실시간 틱을 메인 스레드에서 일괄 처리."""
        # 기존 after 체인 핸들러가 남아 있을 수 있어, 서비스로 위임합니다.
        try:
            self.tick_dispatch_service.flush_on_main()
        except Exception:
            pass

    def enqueue_tick_main_action(self, kind: str, payload: Any) -> None:
        """워커 스레드에서 호출: 메인 스레드에서만 처리할 틱 후속 작업을 큐에 넣습니다."""
        try:
            self._tick_main_actions_queue.put((str(kind), payload))
        except Exception:
            pass

    def mark_tick_worker_ui_dirty(self) -> None:
        """워커가 감시 트리 갱신이 필요함을 표시합니다."""
        try:
            self._tick_worker_ui_dirty = True
        except Exception:
            pass

    def mark_tick_worker_virtual_dirty(self) -> None:
        """워커가 포지션 탭 실시간 갱신이 필요함을 표시합니다."""
        try:
            self._tick_worker_virtual_dirty = True
        except Exception:
            pass

    def _cancel_tick_main_poller(self) -> None:
        """감시 중단·종료 시 메인 큐 폴링 after 를 취소합니다."""
        try:
            jid = getattr(self, "_tick_main_poller_after_id", None)
            r = getattr(self, "root", None)
            if jid is not None and r is not None:
                try:
                    r.after_cancel(jid)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self._tick_main_poller_after_id = None
        except Exception:
            pass

    def _ensure_tick_main_action_poller(self) -> None:
        """메인 스레드에서 틱 후속 큐를 비우도록 after(0) 1건만 예약합니다."""
        try:
            r = getattr(self, "root", None)
            if r is None:
                return
            with self._tick_main_poller_lock:
                if self._tick_main_poller_after_id is not None:
                    return
                self._tick_main_poller_after_id = r.after(
                    0, self._drain_tick_worker_main_queue
                )
        except Exception:
            pass

    def _drain_tick_worker_main_queue(self) -> None:
        """
        메인 스레드 전용: 워커가 넣은 매수·청산 큐를 처리하고 UI 더티 플래그를 반영합니다.
        한 프레임에 처리 건수를 제한해 탭 전환 반응을 유지합니다.
        """
        try:
            t0 = time.perf_counter()
        except Exception:
            t0 = 0.0
        monitoring = bool(self.is_monitoring) or bool(
            getattr(self.trading_controller, "is_monitoring", False)
        )
        processed = 0
        try:
            max_burst = 48
            try:
                st = getattr(self, "settings", None) or {}
                max_burst = int(float(st.get("realtime_tick_main_queue_burst", 48)))
            except (TypeError, ValueError):
                max_burst = 48
            max_burst = max(4, min(max_burst, 200))
            while processed < max_burst:
                try:
                    kind, payload = self._tick_main_actions_queue.get_nowait()
                except queue.Empty:
                    break
                processed += 1
                try:
                    if kind == "buy_signal" and isinstance(payload, tuple):
                        (
                            bcode,
                            npv,
                            rsi_sig,
                            macd_val,
                            osc_val,
                            ma60_ratio,
                        ) = payload
                        buy_ok = bool(
                            self._on_buy_signal_received(
                                bcode,
                                float(npv),
                                float(rsi_sig),
                                float(macd_val),
                                float(osc_val),
                                float(ma60_ratio),
                            )
                        )
                        if buy_ok:
                            try:
                                self.signal_engine.last_buy_sig_time[bcode] = (
                                    datetime.now()
                                )
                            except Exception:
                                pass
                    elif kind == "strategy_action" and isinstance(payload, tuple):
                        scode, sact, sreason, skw = payload[0], payload[1], payload[2], (
                            payload[3] if len(payload) > 3 else {}
                        )
                        if not isinstance(skw, dict):
                            skw = {}
                        # 주문 REST 호출이 메인 루프를 붙잡지 않도록 워커 스레드에서 처리
                        try:
                            nk = str(scode).split("_")[0].zfill(6)
                        except Exception:
                            nk = str(scode)
                        now_ts = time.time()
                        try:
                            st3 = getattr(self, "settings", None) or {}
                            submit_cd = float(
                                st3.get("strategy_action_submit_cooldown_sec", 0.7)
                            )
                        except Exception:
                            submit_cd = 0.7
                        submit_cd = max(0.1, min(submit_cd, 10.0))

                        skip_submit = False
                        try:
                            with self._strategy_action_lock:
                                last = float(
                                    self._strategy_action_last_submit_ts.get(nk, 0.0) or 0.0
                                )
                                if nk in self._strategy_action_inflight_codes:
                                    skip_submit = True
                                elif (now_ts - last) < submit_cd:
                                    skip_submit = True
                                else:
                                    self._strategy_action_inflight_codes.add(nk)
                                    self._strategy_action_last_submit_ts[nk] = now_ts
                        except Exception:
                            skip_submit = False

                        if skip_submit:
                            continue

                        def _run_strategy_action_async() -> None:
                            try:
                                self._on_strategy_action_needed(
                                    scode,
                                    sact,
                                    sreason,
                                    **skw,
                                )
                            except Exception:
                                pass
                            finally:
                                try:
                                    with self._strategy_action_lock:
                                        self._strategy_action_inflight_codes.discard(nk)
                                except Exception:
                                    pass

                        try:
                            threading.Thread(
                                target=_run_strategy_action_async,
                                daemon=True,
                            ).start()
                        except Exception:
                            try:
                                with self._strategy_action_lock:
                                    self._strategy_action_inflight_codes.discard(nk)
                            except Exception:
                                pass
                except Exception:
                    pass
        except Exception:
            pass
        try:
            if getattr(self, "_tick_worker_ui_dirty", False):
                self._tick_worker_ui_dirty = False
                self.ensure_monitor_tree_refresh_scheduled(immediate=False)
        except Exception:
            pass
        try:
            if getattr(self, "_tick_worker_virtual_dirty", False):
                self._tick_worker_virtual_dirty = False
                self.request_virtual_tab_realtime_render()
        except Exception:
            pass
        try:
            busy = (
                not self._tick_main_actions_queue.empty()
                or getattr(self, "_tick_worker_ui_dirty", False)
                or getattr(self, "_tick_worker_virtual_dirty", False)
            )
            with self._tick_main_poller_lock:
                # 현재 실행 중인 콜백 슬롯을 해제한 뒤, 필요할 때만 다음 콜백 1건을 예약합니다.
                self._tick_main_poller_after_id = None
                if monitoring and busy:
                    r2 = getattr(self, "root", None)
                    if r2 is not None:
                        delay = 16
                        try:
                            st2 = getattr(self, "settings", None) or {}
                            delay = int(
                                float(st2.get("realtime_tick_main_poller_ms", 16))
                            )
                        except (TypeError, ValueError):
                            delay = 16
                        delay = max(8, min(delay, 120))
                        self._tick_main_poller_after_id = r2.after(
                            delay, self._drain_tick_worker_main_queue
                        )
        except Exception:
            pass

        # 메인 이벤트 루프가 오래 점유되면 탭 전환/렌더가 멈춘 것처럼 보일 수 있음
        try:
            dt = time.perf_counter() - t0
            if dt >= 0.50:
                sl = getattr(self, "session_logger", None)
                if sl is not None:
                    sl.log(
                        "ui_main_queue_drain_slow",
                        {"dt_sec": float(dt), "processed": int(processed)},
                    )
        except Exception:
            pass

    def _resolve_fill_snapshot_reason(
        self,
        code: str,
        side: str,
        order_reason: Optional[str] = None,
        channel: str = "REST",
    ) -> str:
        """
        capture_snapshot에 넣을 사유 문자열.
        주문 시 전달된 reason(EXIT_*) 우선, 매도는 포지션 exit_reason, 마지막에 채널 토큰(REST_SELL 등).
        """
        try:
            o = str(order_reason or "").strip()
            if o:
                return o
        except Exception:
            pass
        if str(side).lower() != "sell":
            try:
                return f"{channel}_{str(side).upper()}"
            except Exception:
                return f"{channel}_BUY"
        try:
            pm = self.portfolio
            try:
                nc = pm._normalize_stock_code(str(code))
            except Exception:
                nc = str(code).zfill(6)
            with pm.lock:
                for k, pos in pm.virtual_positions.items():
                    try:
                        if pm._normalize_stock_code(str(k)) == nc:
                            er = pos.get("exit_reason")
                            if er:
                                return str(er)
                    except Exception:
                        continue
        except Exception:
            pass
        try:
            return f"{channel}_{str(side).upper()}"
        except Exception:
            return "MANUAL"

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
        try:
            self.fill_reconciliation_service.reconcile_exchange_fill_after_order_accept(
                code=code,
                order_no=order_no,
                side=side,
                qty=qty,
                fill_price=fill_price,
                name=name,
                reason=reason,
            )
        except Exception:
            try:
                self.log("⚠️ [체결 동기화] 위임 실패")
            except Exception:
                pass

    def _cancel_post_fill_sync_jobs(self) -> None:
        """체결 후 동기화 예약(1·2·3차)을 취소합니다. 연속 체결 디바운스 시 사용."""
        try:
            self.post_fill_account_sync_service.cancel_jobs()
        except Exception:
            pass

    def _schedule_account_sync_after_fill(self) -> None:
        """
        체결 후 키움 잔고·예수 REST를 맞춥니다.

        - 2.5초 단일 지연은 증권사 반영이 늦을 때 **이전 잔고**가 오는 경우가 있어,
          첫 동기화는 약 4.2초 뒤, **약 7초 뒤 2차**, 이후 **약 22초 뒤 3차**로 D+2·예수 반영 지연을 흡수합니다.
        - 연속 체결 시마다 after가 쌓이면 락 경쟁으로 스킵되기 쉬우므로 **마지막 체결 기준으로 디바운스**합니다.
        - 동기화 스레드는 ``wait_for_lock=True`` 로 이전 동기화가 끝날 때까지 대기합니다.
        """
        try:
            self.post_fill_account_sync_service.schedule_after_fill()
        except Exception as e:
            try:
                self.log(f"⚠️ [체결 동기화] 예약 실패: {e}")
            except Exception:
                pass

    def _on_execution_received(self, code, order_no, qty, price, side):
        try:
            self.fill_reconciliation_service.on_execution_received(
                code=code,
                order_no=order_no,
                qty=qty,
                price=price,
                side=side,
            )
        except Exception:
            try:
                self.log("⚠️ [체결] 위임 실패")
            except Exception:
                pass

    def ensure_monitor_tree_refresh_scheduled(self, *, immediate: bool = False) -> None:
        """
        감시 트리 갱신 타이머를 1회만 예약합니다(틱마다 after 폭주 방지).
        immediate=True 이면 다음 이벤트 루프에서 곧바로 _render_tree_updates 실행.
        """
        try:
            if self._update_timer_active:
                # 타이머 플래그가 True인데 실제 렌더 진입이 오래 없으면 고착 상태로 판단해 강제 재무장
                try:
                    now_ts = time.time()
                    last_enter = float(getattr(self, "_ui_render_last_enter_ts", 0.0) or 0.0)
                    stale_sec = float((getattr(self, "settings", None) or {}).get("ui_render_stale_rearm_sec", 2.5))
                    stale_sec = max(1.0, min(stale_sec, 15.0))
                    if (now_ts - last_enter) >= stale_sec:
                        self._update_timer_active = False
                        r_force = getattr(self, "root", None)
                        if r_force is not None:
                            r_force.after(0, self._render_tree_updates)
                            self._update_timer_active = True
                            try:
                                sl = getattr(self, "session_logger", None)
                                if sl is not None:
                                    sl.log(
                                        "ui_update_timer_stale_forced_rearm",
                                        {
                                            "stale_sec": float(now_ts - last_enter),
                                            "immediate": bool(immediate),
                                        },
                                    )
                            except Exception:
                                pass
                            return
                except Exception:
                    pass
                # UI 멈춤처럼 보일 때, 타이머가 계속 걸린 상태인지 확인용
                try:
                    sl = getattr(self, "session_logger", None)
                    if sl is not None:
                        now_ts = time.time()
                        last = float(
                            getattr(self, "_ui_update_timer_skip_log_last_ts", 0.0)
                            or 0.0
                        )
                        if now_ts - last >= 10.0:
                            setattr(self, "_ui_update_timer_skip_log_last_ts", now_ts)
                            sl.log(
                                "ui_update_timer_already_active",
                                {
                                    "immediate": bool(immediate),
                                    "_update_timer_active": bool(
                                        getattr(self, "_update_timer_active", False)
                                    ),
                                },
                            )
                except Exception:
                    pass
                return
            r = getattr(self, "root", None)
            if r is None:
                return
            self._update_timer_active = True
            delay_ms = 48
            try:
                delay_ms = int(
                    float((self.settings or {}).get("realtime_tree_first_refresh_delay_ms", 48))
                )
            except (TypeError, ValueError):
                delay_ms = 48
            delay_ms = max(0, min(delay_ms, 800))
            if immediate:
                r.after(0, self._render_tree_updates)
            else:
                r.after(delay_ms, self._render_tree_updates)
        except Exception:
            try:
                self._update_timer_active = False
            except Exception:
                pass

    def _ui_render_watchdog_tick(self) -> None:
        """
        UI 렌더 타이머 체인이 끊겼을 때 자동 재무장합니다.
        - 감시 중인데 _update_timer_active=False 이면 즉시 렌더 예약
        - _update_timer_active=True 인데 최근 렌더 진입이 오래 없으면(기본 3.5초) 강제 재무장
        """
        try:
            self._ui_watchdog_after_id = None
            monitoring = bool(self.is_monitoring) or bool(
                getattr(self.trading_controller, "is_monitoring", False)
            )
            if monitoring:
                now_ts = time.time()
                last_enter = float(getattr(self, "_ui_render_last_enter_ts", 0.0) or 0.0)
                if not bool(getattr(self, "_update_timer_active", False)):
                    self.ensure_monitor_tree_refresh_scheduled(immediate=True)
                else:
                    stale_sec = 3.5
                    if (now_ts - last_enter) >= stale_sec:
                        try:
                            sl = getattr(self, "session_logger", None)
                            if sl is not None:
                                sl.log(
                                    "ui_render_watchdog_rearm",
                                    {
                                        "stale_sec": float(now_ts - last_enter),
                                        "_update_timer_active": bool(
                                            getattr(self, "_update_timer_active", False)
                                        ),
                                    },
                                )
                        except Exception:
                            pass
                        self._update_timer_active = False
                        self.ensure_monitor_tree_refresh_scheduled(immediate=True)
        except Exception:
            pass
        finally:
            try:
                r = getattr(self, "root", None)
                if r is not None and r.winfo_exists():
                    self._ui_watchdog_after_id = r.after(
                        1200, self._ui_render_watchdog_tick
                    )
            except Exception:
                self._ui_watchdog_after_id = None

    def _render_tree_updates(self) -> None:
        """
        감시 트리 실시간 갱신. 틱이 STARTED 콜백보다 먼저 올 수 있어
        trading_controller.is_monitoring 과 app.is_monitoring 둘 다 본다.
        감시 중이 아니면 타이머 체인을 멈추고 _update_timer_active 를 풀어 재무장 가능하게 한다.
        """
        try:
            try:
                self._ui_render_last_enter_ts = time.time()
            except Exception:
                pass
            # _render_tree_updates 호출 자체가 멈췄는지 확인(쿨다운 10초)
            try:
                sl = getattr(self, "session_logger", None)
                if sl is not None:
                    now_ts = time.time()
                    last = float(
                        getattr(self, "_ui_render_call_last_ts", 0.0) or 0.0
                    )
                    if now_ts - last >= 10.0:
                        setattr(self, "_ui_render_call_last_ts", now_ts)
                        sl.log("ui_render_tree_updates_called", {"pending_size": int(len(getattr(self, "pending_tree_updates", {}) or {}))})
            except Exception:
                pass
            import time as _t
            t0 = _t.perf_counter()
            pending_size = len(getattr(self, "pending_tree_updates", {}) or {})
            if not self.monitor_tab:
                self._update_timer_active = False
                return
            try:
                if not self.root.winfo_exists():
                    self._update_timer_active = False
                    return
            except Exception:
                self._update_timer_active = False
                return
            monitoring = bool(self.is_monitoring) or bool(
                getattr(self.trading_controller, "is_monitoring", False)
            )
            if not monitoring:
                self._update_timer_active = False
                return
            # 안전망: pending 업데이트가 비거나 누락되는 구간에서도 monitor_info 숫자열을 주기적으로 강제 반영
            try:
                self._inject_min_monitor_updates_from_monitor_info()
            except Exception:
                pass
            self.monitor_tab.update_view(
                self.pending_tree_updates, self.portfolio.virtual_positions
            )
            try:
                # 실시간 장중에는 reconcile(대량 delete/insert 보정)가 메인 루프를 장시간 점유해
                # 탭 전환 불가/응답없음을 유발할 수 있어 기본 비활성화.
                # 필요 시 settings.json 에 enable_monitor_tree_reconcile=true 로만 활성화.
                st = getattr(self, "settings", None) or {}
                if bool(st.get("enable_monitor_tree_reconcile", False)):
                    self.monitor_tab.maybe_reconcile_monitor_trees(
                        getattr(self, "monitor_info", None) or {},
                        self.portfolio.virtual_positions,
                    )
            except Exception:
                pass
            pending_after = 0
            try:
                pending_after = len(getattr(self, "pending_tree_updates", {}) or {})
            except Exception:
                pending_after = 0
            backlog_size = max(int(pending_size), int(pending_after))

            next_delay_ms = REALTIME_TREE_UI_REFRESH_MS
            try:
                st = getattr(self, "settings", None) or {}
                base_ms = int(float(st.get("realtime_tree_ui_refresh_ms_base", 220)))
                busy_ms = int(float(st.get("realtime_tree_ui_refresh_ms_busy", 90)))
                busy_threshold = int(float(st.get("realtime_tree_ui_refresh_busy_pending_threshold", 12)))
                idle_ms = int(float(st.get("realtime_tree_ui_refresh_ms_idle", base_ms)))
            except Exception:
                base_ms = 220
                busy_ms = 90
                busy_threshold = 12
                idle_ms = base_ms
            base_ms = max(50, min(base_ms, 1500))
            busy_ms = max(20, min(busy_ms, base_ms))
            idle_ms = max(base_ms, min(idle_ms, 2000))
            busy_threshold = max(5, min(busy_threshold, 500))
            if backlog_size >= busy_threshold:
                # 대기 큐가 많은 프레임에서는 주기를 잠시 줄여 최신가 지연을 완화합니다.
                next_delay_ms = busy_ms
            elif backlog_size <= 0:
                # 대기 큐가 비어 있으면 기본보다 살짝 느리게 돌려 메인 루프 점유를 낮춥니다.
                next_delay_ms = idle_ms
            else:
                next_delay_ms = base_ms
            self.root.after(next_delay_ms, self._render_tree_updates)
        except Exception as e:
            try:
                self.log(f"⚠️ [UI] _render_tree_updates: {e}")
            except Exception:
                pass
            try:
                sl = getattr(self, "session_logger", None)
                if sl is not None:
                    sl.log(
                        "ui_render_tree_updates_exception",
                        {"error": str(e)},
                    )
            except Exception:
                pass
            try:
                self._update_timer_active = False
            except Exception:
                pass
        finally:
            try:
                dt = _t.perf_counter() - t0
                if dt >= 0.50:
                    sl = getattr(self, "session_logger", None)
                    if sl is not None:
                        sl.log(
                            "ui_render_tree_updates_slow",
                            {"dt_sec": float(dt), "pending_size": int(pending_size)},
                        )
            except Exception:
                pass

    def _inject_min_monitor_updates_from_monitor_info(self) -> None:
        """
        SignalEngine pending enqueue가 누락되는 케이스를 우회하기 위한 최소 숫자열 강제 업데이트.
        - 감시 대상 monitor_codes 만
        - 가격 변화가 있거나 일정 시간(기본 3초) 경과 시에만 enqueue
        """
        try:
            mi = getattr(self, "monitor_info", None) or {}
            if not isinstance(mi, dict) or not mi:
                return
            try:
                pending_now = len(getattr(self, "pending_tree_updates", {}) or {})
            except Exception:
                pending_now = 0
            try:
                st = getattr(self, "settings", None) or {}
                inject_skip_threshold = int(
                    float(st.get("ui_force_row_refresh_skip_pending_threshold", 120))
                )
            except Exception:
                inject_skip_threshold = 120
            inject_skip_threshold = max(20, min(inject_skip_threshold, 1000))
            # 이미 처리 대기량이 큰 프레임에서는 강제 주입을 건너뛰어 UI 점유 시간을 줄입니다.
            if pending_now >= inject_skip_threshold:
                return
            mc = list(getattr(self.trading_controller, "monitor_codes", None) or [])
            if not mc:
                mc = list(getattr(self, "monitor_codes", None) or [])
            if not mc:
                return
            now_ts = time.time()
            force_sec = 3.0
            try:
                st = getattr(self, "settings", None) or {}
                force_sec = float(st.get("ui_force_row_refresh_sec", 3.0))
            except Exception:
                force_sec = 3.0
            force_sec = max(0.5, min(force_sec, 30.0))
            max_codes = 60
            try:
                st = getattr(self, "settings", None) or {}
                max_codes = int(float(st.get("ui_force_row_refresh_max_codes", 60)))
            except Exception:
                max_codes = 60
            max_codes = max(5, min(max_codes, 300))

            total_codes = len(mc)
            if total_codes <= 0:
                return

            start_idx = 0
            try:
                start_idx = int(getattr(self, "_ui_force_row_rr_index", 0) or 0)
            except Exception:
                start_idx = 0
            if start_idx < 0:
                start_idx = 0
            if total_codes > 0:
                start_idx = start_idx % total_codes

            rotated_codes = mc[start_idx:] + mc[:start_idx]
            count = 0
            scanned = 0
            for raw in rotated_codes:
                scanned += 1
                if count >= max_codes:
                    break
                nk = str(raw).split("_")[0].zfill(6)
                info = mi.get(nk)
                if not isinstance(info, dict):
                    continue
                try:
                    now_p = float(
                        info.get("price")
                        if info.get("price") not in (None, "", 0)
                        else (info.get("last_price") or 0)
                    )
                except Exception:
                    now_p = 0.0
                if now_p <= 0:
                    continue
                try:
                    last_p = float(self._ui_force_row_last_price.get(nk, 0.0) or 0.0)
                except Exception:
                    last_p = 0.0
                try:
                    last_push = float(self._ui_force_row_last_push_ts.get(nk, 0.0) or 0.0)
                except Exception:
                    last_push = 0.0
                if abs(now_p - last_p) < 0.0001 and (now_ts - last_push) < force_sec:
                    continue

                self._ui_force_row_last_price[nk] = now_p
                self._ui_force_row_last_push_ts[nk] = now_ts
                count += 1

                nm = str(info.get("name", nk) or nk)
                try:
                    sc = float(info.get("score", 0) or 0)
                except Exception:
                    sc = 0.0
                try:
                    vol_i = int(float(info.get("volume", 0) or 0))
                except Exception:
                    vol_i = 0
                try:
                    vol_pow = float(info.get("vol_power", 0) or 0)
                except Exception:
                    vol_pow = 0.0
                try:
                    tr_raw = str(
                        info.get("sel_track", info.get("SelTrack", "AB")) or "AB"
                    ).strip().upper()
                    if tr_raw == "A":
                        strat_label = "전략 A"
                    elif tr_raw == "B":
                        strat_label = "전략 B"
                    else:
                        strat_label = "전략 A+B"
                except Exception:
                    strat_label = "전략 A+B"
                ratio_disp = str(info.get("ratio", "0"))
                status_text = str(info.get("status_text") or info.get("confluence") or "대기")
                try:
                    raw_diff = info.get("diff")
                    if raw_diff in (None, "", "-", "—"):
                        vs_prev = "—"
                    else:
                        iv = int(round(float(str(raw_diff).replace(",", "").strip())))
                        vs_prev = "0" if iv == 0 else f"{iv:+,}"
                except Exception:
                    vs_prev = "—"
                try:
                    ta = int(float(info.get("total_ask", 0) or 0))
                    tb = int(float(info.get("total_bid", 0) or 0))
                    if ta > 0 and tb > 0:
                        br = float(ta) / float(tb)
                        bid_ask_cell = f"{ta:,} / {tb:,} ({br:.1f})"
                    else:
                        bid_ask_cell = "— / — (—)"
                except Exception:
                    bid_ask_cell = "— / — (—)"
                full_values = (
                    f"{sc:.1f}",
                    nk,
                    nm,
                    strat_label,
                    f"{now_p:,.0f}",
                    "—",
                    vs_prev,
                    ratio_disp,
                    status_text,
                    f"{vol_pow:.1f}%",
                    bid_ask_cell,
                    f"{vol_i:,}",
                )
                try:
                    direction = self.monitor_tab._monitor_ratio_direction(ratio_disp)
                except Exception:
                    direction = "stable"
                p_tag = (
                    "price_up"
                    if direction == "up"
                    else ("price_down" if direction == "down" else "price_stable")
                )
                self.pending_tree_updates[nk] = (
                    full_values,
                    ("status_low", p_tag),
                    False,
                )
            try:
                if total_codes > 0:
                    self._ui_force_row_rr_index = int((start_idx + scanned) % total_codes)
            except Exception:
                self._ui_force_row_rr_index = 0
        except Exception:
            pass

    def stream_subscription_codes(self) -> List[str]:
        """
        웹소켓 REG용 구독 종목: 감시 ∪ 관심 ∪ 보유를 합친 뒤, 워밍업에서 채운 _active_stream_codes 를 **추가** 병합한다.
        (_active_stream_codes 만 반환하면 워밍업이 일부만 반영될 때 나머지 종목은 REG 에 안 올라가 틱이 영구히 안 옴)
        """
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
                try:
                    base.append(str(c).split("_")[0].zfill(6))
                except Exception:
                    continue
        except Exception:
            pass
        seen: Set[str] = set()
        out: List[str] = []
        for c in base:
            if c not in seen:
                seen.add(c)
                out.append(c)
        try:
            ac = getattr(self, "_active_stream_codes", None)
            if isinstance(ac, list) and ac:
                for c in ac:
                    cn = str(c).zfill(6)
                    if cn not in seen:
                        seen.add(cn)
                        out.append(cn)
        except Exception:
            pass
        if not out:
            try:
                p = os.path.join(os.getcwd(), "selected_targets.csv")
                if os.path.isfile(p):
                    df = pd.read_csv(p, dtype={"Code": str})
                    for raw in df.get("Code", pd.Series(dtype=str)).tolist():
                        cn = str(raw).zfill(6)
                        if cn not in seen:
                            seen.add(cn)
                            out.append(cn)
            except Exception:
                pass
        return out

    def _maybe_stop_account_sync_timer(self) -> None:
        """구버전에서 예약된 주기 동기화(after)가 있으면 취소합니다."""
        try:
            if self._account_sync_job is not None:
                try:
                    self.root.after_cancel(self._account_sync_job)
                except Exception:
                    pass
                self._account_sync_job = None
        except Exception:
            pass

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
            if now - self._virtual_tab_render_last_ts < 0.2:
                if not self._virtual_tab_render_pending:
                    self._virtual_tab_render_pending = True
                    self.root.after(180, self._flush_virtual_tab_realtime_render)
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

    def _schedule_vp_stub_price_refresh_after_auth(self) -> None:
        """인증 직후 토큰·REST 준비 타이밍을 두고 보유 전용 스텁 last_price 를 재시드합니다."""
        try:
            r = getattr(self, "root", None)
            if r is not None:
                r.after(400, self._refresh_vp_price_stubs_after_auth_safe)
        except Exception:
            pass

    def _refresh_vp_price_stubs_after_auth_safe(self) -> None:
        try:
            self._refresh_vp_price_stubs_after_auth()
        except Exception as e:
            try:
                self.log(f"⚠️ [기동] 보유 참고가(스텁) 지연 갱신 실패: {e}")
            except Exception:
                pass

    def _refresh_vp_price_stubs_after_auth(self) -> None:
        """
        첫 렌더에서 토큰이 없어 last_price=0 이었던 vp_price_stub 행을 REST로 다시 채웁니다.
        또한 **실시간 틱 price 가 아직 없을 때** 잘못 시드된 last_price(예: 역순 REST로
        들어간 전일 값)도 인증 직후 한 번 교정합니다.
        """
        om = getattr(self, "order_manager", None)
        if om is None:
            return
        try:
            tok = getattr(getattr(self, "auth_manager", None), "get_auth_token", None)
            if not callable(tok) or not tok():
                return
        except Exception:
            return
        try:
            vp = getattr(self.portfolio, "virtual_positions", None) or {}
        except Exception:
            vp = {}
        if not vp:
            return
        mi = getattr(self, "monitor_info", None)
        if not isinstance(mi, dict):
            return
        updated = False
        for c_raw, pos in list(vp.items()):
            try:
                if not isinstance(pos, dict):
                    continue
                nk = str(c_raw).split("_")[0].zfill(6)
                row = mi.get(nk)
                if not isinstance(row, dict) or not row.get("vp_price_stub"):
                    continue
                # 실시간 체결가가 이미 오면 last_price 는 건드리지 않음
                try:
                    raw_p = row.get("price")
                    if raw_p not in (None, "", 0):
                        v = float(raw_p)
                        if v > 0:
                            continue
                except (TypeError, ValueError):
                    pass
                ref_p = float(
                    virtual_position_reference_price_for_monitor(
                        pos,
                        order_manager=om,
                        code=nk,
                    )
                )
                if ref_p <= 0:
                    continue
                try:
                    lp = float(row.get("last_price") or 0)
                except (TypeError, ValueError):
                    lp = 0.0
                if lp > 0 and abs(ref_p - lp) < 0.501:
                    continue
                row = dict(row)
                row["last_price"] = ref_p
                mi[nk] = row
                try:
                    self.trading_controller.monitor_info[nk] = dict(row)
                except Exception:
                    pass
                updated = True
            except Exception:
                continue
        if updated and hasattr(self, "virtual_tab"):
            try:
                self.virtual_tab._render_virtual_positions()
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
            try:
                ref_p = float(
                    virtual_position_reference_price_for_monitor(
                        pos,
                        order_manager=getattr(self, "order_manager", None),
                        code=nk,
                    )
                )
            except Exception:
                ref_p = 0.0
            stub = {
                "name": nm,
                "ma20": 0.0,
                "ma60": 0.0,
                "prev_vol": 0.0,
                "score": 0.0,
                "ma_buffer_scale": 1.0,
                "open": ap,
                # 계좌 스냅(ka_cur_prc 등)이 있으면 보유 탭과 같이 참고가 표시. 평단은 넣지 않음.
                "last_price": ref_p,
                "sel_track": "AB",
                "vp_price_stub": True,
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
            # EOD 스냅샷 스케줄러가 멈췄거나(예: UI 재초기화) 비정상 상태면 재시작합니다.
            try:
                self._start_eod_equity_snapshot_scheduler()
            except Exception:
                pass
            if self.btn_monitor_toggle: self.btn_monitor_toggle.config(text="⏹️ 감시 중단", style="Warning.TButton")
            if self.lbl_monitor_status: self.lbl_monitor_status.config(text=f"상태: 감시 중 ({data}종목)", foreground=self.colors['success'])
            if self.status_label:
                self.status_label.config(
                    text=f"상태: 감시 중 ({data}종목)",
                    foreground=self.colors["success"],
                )
            self.ensure_monitor_tree_refresh_scheduled(immediate=True)

            # 실시간 감시 시작 시 계좌정보 텔레그램 1회
            if not self._telegram_sent_on_monitor_start:
                self._telegram_sent_on_monitor_start = True
                self._start_account_summary_telegram_worker(
                    header="실시간감시가 시작되었습니다 +",
                    seq=0,
                )
        elif status == "STOPPED":
            self.is_monitoring = False
            self._active_stream_codes = []
            self._maybe_stop_account_sync_timer()
            # 다음 감시 시작 시 다시 1회 발송되도록 리셋
            self._telegram_sent_on_monitor_start = False
            if self.btn_monitor_toggle: self.btn_monitor_toggle.config(text="▶️ 실시간 감시 시작", style="Accent.TButton")
            if self.lbl_monitor_status: self.lbl_monitor_status.config(text="상태: 대기 중", foreground=self.colors['primary'])
            if self.status_label:
                self.status_label.config(text="상태: 대기 중", foreground=self.colors["primary"])

    def on_close(self):
        self.log("⏹️ [System] 프로그램을 종료합니다.")
        try:
            self._equity_snapshot_cancelled = True
            try:
                if self._equity_snapshot_after_id is not None:
                    self.root.after_cancel(self._equity_snapshot_after_id)
            except Exception:
                pass
        except Exception:
            pass
        try:
            self._cancel_post_fill_sync_jobs()
        except Exception:
            pass
        try:
            mt = getattr(self, "monitor_tab", None)
            if mt is not None and hasattr(mt, "flush_monitor_ui_before_quit"):
                mt.flush_monitor_ui_before_quit()
        except Exception as e:
            try:
                self.log(f"⚠️ [UI] 감시 탭 레이아웃 저장 실패: {e}")
            except Exception:
                pass
        try:
            self._cancel_tick_main_poller()
        except Exception:
            pass
        try:
            if hasattr(self, "tick_dispatch_service"):
                self.tick_dispatch_service.shutdown()
        except Exception:
            pass
        if hasattr(self, 'trading_controller'): self.trading_controller.stop()
        self.root.destroy()

    def _start_eod_equity_snapshot_scheduler(self) -> None:
        # 기존 구현을 서비스로 위임
        try:
            self.eod_equity_snapshot_service.start()
        except Exception:
            pass

    def _schedule_eod_equity_snapshot_tick(self) -> None:
        # 서비스가 after 예약을 담당합니다.
        # (호환용) 과거 코드 호출 경로가 남아있으면 서비스 tick()으로 위임합니다.
        try:
            self.eod_equity_snapshot_service.tick()
        except Exception:
            pass

    def _eod_equity_snapshot_tick(self) -> None:
        # 서비스 tick()으로 위임
        try:
            self.eod_equity_snapshot_service.tick()
        except Exception:
            pass

    def _job_save_eod_equity_snapshot(self, snap_date_str: str) -> None:
        # 기존 구현을 서비스로 위임
        try:
            # 서비스의 tick() 경로가 job을 직접 스폰하지만,
            # 호환 목적의 호출이 남아있을 수 있어 직접 위임합니다.
            self.eod_equity_snapshot_service._job_save_eod_equity_snapshot(
                snap_date_str
            )
        except Exception:
            pass

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
        # 판정/주문 로직은 SignalCallbacksService로 위임
        try:
            return self.signal_callbacks_service.on_buy_signal_received(
                code=code,
                now_price=now_price,
                rsi_sig=rsi_sig,
                macd_val=macd_val,
                osc_val=osc_val,
                ma60_ratio=ma60_ratio,
            )
        except Exception:
            return False

    def _on_strategy_action_needed(self, code, action, reason, **kwargs):
        try:
            return self.signal_callbacks_service.on_strategy_action_needed(
                code=code, action=action, reason=reason, **kwargs
            )
        except Exception:
            return None

    def _recalculate_all_indicators(self, code):
        return self.strategy_engine.recalculate_indicators(code, self.bars_data, self.settings)

    def send_telegram_message(self, message: str, parse_mode: Optional[str] = "HTML") -> None:
        try:
            ok = self.data_manager.send_telegram_message(message, parse_mode=parse_mode)
            if not ok:
                self.log("⚠️ [Telegram] 전송 실패 또는 미설정 — config.py 의 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 를 확인하세요.")
        except Exception as e:
            self.log(f"⚠️ [Telegram] 예외: {e}")

    def _build_account_summary_telegram_message(self, header: str) -> Optional[str]:
        """
        포지션관리 상단 계좌 요약(virtual_tab vars)을 동일 항목으로 텔레그램에 전송합니다.

        포함 항목:
        - 총자산
        - D+2예수금
        - 주식평가금
        - 미실현 손익
        - 금일손익
        """
        try:
            vt = getattr(self, "virtual_tab", None)
            if vt is None:
                return None

            # virtual_tab 상단 요약 vars 키는 UI에서 고정
            total = str((vt.vars.get("total").get() if vt.vars.get("total") else "") or "").strip()
            d2_cash = str((vt.vars.get("d2_cash").get() if vt.vars.get("d2_cash") else "") or "").strip()
            stock = str((vt.vars.get("stock").get() if vt.vars.get("stock") else "") or "").strip()
            open_ur = str((vt.vars.get("open_ur").get() if vt.vars.get("open_ur") else "") or "").strip()
            day = str((vt.vars.get("day").get() if vt.vars.get("day") else "") or "").strip()

            if not any([total, d2_cash, stock, open_ur, day]):
                return None

            msg = (
                f"📌 <b>[계좌요약]</b>\n{header}\n"
                f"총자산: {total}\n"
                f"D+2예수금: {d2_cash}\n"
                f"주식평가금: {stock}\n"
                f"미실현 손익: {open_ur}\n"
                f"금일손익: {day}"
            )
            return msg
        except Exception:
            return None

    def _start_account_summary_telegram_worker(self, *, header: str, seq: int) -> None:
        """
        계좌 동기화(REST 포함) 후, UI 요약을 다시 읽어 텔레그램을 발송합니다.

        seq는 타이머 중복 실행 시 최신 요청만 처리하기 위한 용도입니다.
        """
        try:
            root = getattr(self, "root", None)
            if root is None:
                return
        except Exception:
            return

        def _worker() -> None:
            acquired = False
            try:
                acquired = self._portfolio_sync_thread_lock.acquire(timeout=120.0)
                if not acquired:
                    return
                # silent=True: UI/터미널 로그 스팸 방지
                self.portfolio.sync_with_actual_account(
                    silent=True,
                    force=False,
                    refresh_cash_deposit=True,
                )
            except Exception:
                pass
            finally:
                if acquired:
                    try:
                        self._portfolio_sync_thread_lock.release()
                    except Exception:
                        pass

            def _after() -> None:
                try:
                    # 상단 요약 값 갱신 후 읽기
                    if hasattr(self, "virtual_tab"):
                        self.virtual_tab.refresh_portfolio_light()
                except Exception:
                    pass

                msg = self._build_account_summary_telegram_message(header=header)
                if msg:
                    self.send_telegram_message(msg, parse_mode="HTML")

            try:
                root.after(0, _after)
            except Exception:
                pass

        try:
            import threading

            threading.Thread(target=_worker, daemon=True).start()
        except Exception:
            pass

    def schedule_account_summary_telegram_after_trade(self, *, delay_sec: int = 40) -> None:
        """
        매수/매도 발송 이후 delay_sec(기본 약 40초) 지연 후
        계좌 동기화 완료 결과로 계좌요약 텔레그램을 1회 추가 전송합니다.

        연속 주문이 들어오면 최신 주문 기준으로 이전 타이머는 취소(최종 1회만 발송).
        """
        try:
            root = getattr(self, "root", None)
            if root is None:
                return
        except Exception:
            return

        try:
            delay_ms = max(0, int(delay_sec * 1000))
        except Exception:
            delay_ms = 1800 * 1000

        self._account_summary_after_trade_seq += 1
        seq = self._account_summary_after_trade_seq

        try:
            if self._account_summary_after_trade_job_id is not None:
                try:
                    root.after_cancel(self._account_summary_after_trade_job_id)
                except Exception:
                    pass
        except Exception:
            pass

        def _job() -> None:
            try:
                # 최신 seq만 처리
                if seq != self._account_summary_after_trade_seq:
                    return
                self._start_account_summary_telegram_worker(
                    header="주문 후 계좌정보 동기화 완료 +",
                    seq=seq,
                )
            except Exception:
                pass

        try:
            self._account_summary_after_trade_job_id = root.after(
                delay_ms, _job
            )
        except Exception:
            pass
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
        전체 리빌드는 무거우므로 한 틱 지연 후 수행하고, 그동안 하단에 처리 중 표시.
        """
        try:
            self.trading_controller._ensure_virtual_positions_monitor_info()
        except Exception:
            pass
        try:
            self.monitor_info = dict(self.trading_controller.monitor_info or {})
        except Exception:
            pass
        if not hasattr(self, "virtual_tab"):
            return
        try:
            sig = self._vp_structure_signature()
            prev = getattr(self, "_last_vp_render_sig", None)
            if prev is not None and sig == prev:
                self.virtual_tab.refresh_portfolio_light()
                try:
                    mt = getattr(self, "monitor_tab", None)
                    if mt is not None:
                        mt._purge_stale_active_monitor_rows(
                            self.portfolio.virtual_positions,
                            getattr(self, "monitor_info", None) or {},
                        )
                except Exception:
                    pass
                return
        except Exception:
            pass

        def _heavy_vp_render() -> None:
            try:
                self.virtual_tab._render_virtual_positions()
                self._last_vp_render_sig = self._vp_structure_signature()
            except Exception:
                pass
            finally:
                try:
                    self._set_ui_busy(False)
                except Exception:
                    pass
            try:
                mt = getattr(self, "monitor_tab", None)
                if mt is not None:
                    mt._purge_stale_active_monitor_rows(
                        self.portfolio.virtual_positions,
                        getattr(self, "monitor_info", None) or {},
                    )
            except Exception:
                pass

        try:
            self._set_ui_busy(True, "보유·주문 화면을 그리는 중")
            r = getattr(self, "root", None)
            if r is not None:
                r.after(1, _heavy_vp_render)
            else:
                _heavy_vp_render()
        except Exception:
            try:
                self._set_ui_busy(False)
            except Exception:
                pass

    def refresh_virtual_tab_orders_only(self) -> None:
        """포지션 관리 탭 — 주문 로그 트리만 갱신(증권사 체결 API 병합 후 경량 호출)."""
        try:
            vt = getattr(self, "virtual_tab", None)
            if vt is not None:
                vt._render_orders()
        except Exception:
            pass

    def refresh_virtual_tab_full(self) -> None:
        """주문 로그 반영 등 히스토리·주문 트리까지 전부 다시 그릴 때 사용."""
        try:
            self.trading_controller._ensure_virtual_positions_monitor_info()
        except Exception:
            pass
        try:
            self.monitor_info = dict(self.trading_controller.monitor_info or {})
        except Exception:
            pass

        def _heavy_full() -> None:
            try:
                if hasattr(self, "virtual_tab"):
                    try:
                        self.virtual_tab._render_virtual_positions()
                    except Exception:
                        pass
                    try:
                        self.virtual_tab._render_orders()
                    except Exception:
                        pass
                try:
                    self._last_vp_render_sig = self._vp_structure_signature()
                except Exception:
                    pass
                try:
                    mt = getattr(self, "monitor_tab", None)
                    if mt is not None:
                        mt._purge_stale_active_monitor_rows(
                            self.portfolio.virtual_positions,
                            getattr(self, "monitor_info", None) or {},
                        )
                except Exception:
                    pass
            finally:
                try:
                    self._set_ui_busy(False)
                except Exception:
                    pass

        try:
            self._set_ui_busy(True, "포지션 탭 전체 갱신")
            r = getattr(self, "root", None)
            if r is not None:
                r.after(1, _heavy_full)
            else:
                _heavy_full()
        except Exception:
            try:
                self._set_ui_busy(False)
            except Exception:
                pass

    def sync_with_actual_account(
        self,
        silent: bool = False,
        force: bool = False,
        *,
        refresh_cash_deposit: bool = True,
    ) -> None:
        """UI에서 호출되는 계좌 동기화 — 메인 스레드를 막지 않도록 백그라운드로 위임."""
        self._run_portfolio_sync_in_daemon_thread(
            silent=silent,
            force=force,
            refresh_cash_deposit=refresh_cash_deposit,
        )

    def _sync_account_deposit(self) -> None:
        # [v4.9.0] 전체 계좌 동기화 — 버튼 클릭 시 API 스로틀을 우회(force), 예수·D+1/D+2 REST 반드시 포함
        self._run_portfolio_sync_in_daemon_thread(
            silent=False,
            force=True,
            refresh_cash_deposit=True,
            wait_for_lock=True,
        )
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
