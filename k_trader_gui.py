


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
from core.defaults import DEFAULT_SETTINGS, get_allowed_keys
from core.version import APP_VERSION
from core.supply_display import capture_supply_snapshot

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

# 실시간 감시 탭 트리 UI 갱신 주기(ms). 틱·지표 계산은 그대로 — 발열이 불편하면 220~400 사이에서만 조정.
REALTIME_TREE_UI_REFRESH_MS: int = 280


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

        self.notebook = None
        self.status_label = None
        self.pending_tree_updates = {} # [v4.5.3] UI 지연 업데이트용 큐
        # REST 선체결 vs 웹소켓 CHE 중복 on_fill 방지 (주문번호 기준)
        self._exchange_fill_order_ids: Set[str] = set()
        # (구) 주기 계좌 동기화 타이머 취소용. 자동 동기화는 기동 후 1회만 수행한다.
        self._account_sync_job: Optional[Any] = None
        self._virtual_tab_render_pending: bool = False
        self._virtual_tab_render_last_ts: float = 0.0
        # 웹소켓 틱 → 메인 스레드에서 배치 처리 (종목당 마지막 틱만 유지)
        self._rt_tick_latest: dict = {}
        self._tick_flush_after_id: Optional[Any] = None
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

        self.settings = self.config_handler.load_settings(DEFAULT_SETTINGS)
        
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
        """
        REST 토큰 발급 및 초기 동기화(포트폴리오·시장·원격·계좌·유니버스).
        실패 시 백그라운드에서 지수 백오프로 자동 재시도하며, UI 스레드는 막지 않습니다.
        """
        with self._auth_worker_lock:
            if self._auth_worker_running:
                try:
                    self.log("ℹ️ [Auth] 인증·연결 갱신이 이미 진행 중입니다.")
                except Exception:
                    pass
                return
            self._auth_worker_running = True

        def worker() -> None:
            try:
                self._run_auth_token_with_retries()
            finally:
                with self._auth_worker_lock:
                    self._auth_worker_running = False

        threading.Thread(target=worker, daemon=True).start()

    def _run_auth_token_with_retries(self) -> None:
        """백그라운드 전용: 토큰 발급 재시도 후 성공 시에만 부가 초기화 수행."""
        max_attempts = 12
        delay_sec = 3.0
        max_delay = 45.0
        backoff_mult = 1.25
        token: Optional[str] = None
        try:
            err_fg = self.colors.get("error", "#BF616A")
            ok_fg = self.colors.get("success", "#A3BE8C")
        except Exception:
            err_fg, ok_fg = "#BF616A", "#A3BE8C"

        self._boot_progress_set_message("키움 REST 인증 연결 중…")

        for attempt in range(1, max_attempts + 1):
            try:
                if attempt > 1:
                    try:
                        self.auth_manager.invalidate_cached_token()
                    except Exception:
                        pass
                    try:
                        self.log(
                            f"🔁 [Auth] {attempt}/{max_attempts}차 인증 시도 "
                            f"({delay_sec:.1f}초 대기 후)..."
                        )
                    except Exception:
                        pass
                    time.sleep(delay_sec)
                    delay_sec = min(delay_sec * backoff_mult, max_delay)

                try:
                    token = self.auth_manager.get_auth_token()
                except Exception as ex:
                    token = None
                    try:
                        self.log(f"⚠️ [Auth] {attempt}/{max_attempts}차 예외: {ex}")
                    except Exception:
                        pass

                if token:
                    if attempt > 1:
                        try:
                            self.log(f"✅ [Auth] {attempt}회차에서 인증 성공")
                        except Exception:
                            pass
                    break
            except Exception as loop_ex:
                try:
                    self.log(f"⚠️ [Auth] 재시도 루프 오류: {loop_ex}")
                except Exception:
                    pass

        if not token:
            try:
                self.log(
                    f"❌ [Auth] {max_attempts}회 시도 후에도 인증에 실패했습니다. "
                    "네트워크·키움 API 상태를 확인하거나 잠시 뒤 앱을 다시 시작해 주세요."
                )
            except Exception:
                pass
            try:
                self.root.after(
                    0,
                    lambda c=err_fg: (
                        self.status_label.config(
                            text="상태: 인증 실패 — 연결 확인", foreground=c
                        )
                        if self.status_label
                        else None
                    ),
                )
            except Exception:
                pass
            self._boot_progress_finish(ok=False)
            return

        try:
            self.root.after(
                0,
                lambda c=ok_fg: (
                    self.status_label.config(
                        text="상태: 로그인 완료", foreground=c
                    )
                    if self.status_label
                    else None
                ),
            )
        except Exception:
            pass

        self._boot_progress_set_message("계좌·시장 데이터 준비 중…")

        try:
            # 상세 요약은 첫 계좌 동기화(REST)가 끝난 뒤 `PortfolioManager` 에서 한 번만 출력한다.
            try:
                self.log(
                    "[기동] 인증 완료 → 약 2초 후 첫 계좌 동기화(백그라운드). "
                    "그동안 탭 전환 가능. 로그는 동기화가 끝나는 대로 올라옵니다."
                )
            except Exception:
                pass
            self.portfolio.load_all()
            self.remote_commander.start()
            try:
                self._schedule_vp_stub_price_refresh_after_auth()
            except Exception:
                pass
            self.root.after(
                2000,
                lambda: self._run_portfolio_sync_in_daemon_thread(
                    silent=True, force=True
                ),
            )
            threading.Thread(
                target=lambda: self.get_kospi_200(force=True), daemon=True
            ).start()
            self._boot_progress_finish(ok=True)
        except Exception as post_ex:
            try:
                self.log(f"❌ [Auth] 인증 후 초기화 중 오류: {post_ex}")
            except Exception:
                pass
            self._boot_progress_finish(ok=False)

    def get_kospi_200(self, force=False):
        self.log("[Data] KOSPI200 유니버스 로드(kodex200.csv·설정 kodex200_universe_priority)...")
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
        """
        REST 주문 접수 성공 직후 웹소켓 CHE가 오지 않을 때도 DB 체결가·포지션을 맞춥니다.
        CHE가 나중에 오면 동일 주문번호로 on_fill은 생략하고 체결가만 보정합니다.
        """
        try:
            oid = str(order_no or "").strip()
            # CHE가 먼저 처리된 경우 참고가로 덮어쓰지 않음
            if oid and oid in self._exchange_fill_order_ids:
                return
            try:
                try:
                    _fs = capture_supply_snapshot(self, code)
                except Exception:
                    _fs = {"vol_power": None, "bid_ask_text": ""}
                self.data_manager.update_order_status(
                    code,
                    order_no,
                    "FILLED",
                    filled_price=int(fill_price),
                    filled_qty=int(qty),
                    fill_snap_vol_power=_fs.get("vol_power"),
                    fill_snap_bid_ask=_fs.get("bid_ask_text") or "— / — (—)",
                )
            except Exception as ue:
                self.log(f"⚠️ [체결 동기화] DB 갱신 실패: {ue}")
            snap_reason = self._resolve_fill_snapshot_reason(
                code, side, reason, channel="REST"
            )
            snapshot = self.portfolio.capture_snapshot(code, fill_price, reason=snap_reason)
            applied = self.portfolio.on_fill(
                code,
                side,
                fill_price,
                qty,
                name=name or None,
                reason=reason or None,
                snapshot=snapshot,
                fill_channel="exchange",
                order_no=str(order_no or "").strip() or None,
            )
            # 로컬 보유가 없어 on_fill 이 스킵된 매도: 주문 로그는 있는데 종료내역만 누락되는 경우 보강
            if (
                not applied
                and str(side or "").lower() == "sell"
            ):
                try:
                    applied = self.portfolio.record_orphan_sell_close(
                        code,
                        float(fill_price),
                        int(qty),
                        str(name or ""),
                        str(reason or ""),
                        str(order_no or ""),
                        snapshot,
                        "exchange",
                    )
                except Exception as oe:
                    self.log(f"⚠️ [체결 동기화] 종료내역 보강 실패: {oe}")
            # on_fill이 매도 스킵(False)인데 여기서 oid를 먼저 넣으면, 뒤따른 CHE가 on_fill을 영원히 못 타
            # 매매종료내역 누락으로 이어질 수 있음 — 실제 반영(True)일 때만 중복 마킹
            if oid and applied:
                self._exchange_fill_order_ids.add(oid)
            if applied:
                self.log(
                    f"✅ [체결 동기화] REST 접수 직후 가상체결 {code} "
                    f"{'매수' if side == 'buy' else '매도'} {qty}주 ({int(fill_price):,}원)"
                )
            elif str(side or "").lower() == "sell":
                self.log(
                    f"⚠️ [체결 동기화] REST 매도 체결은 포트폴리오에 미반영({code}) "
                    f"— 로컬 보유 없음·종료내역 보강도 실패. 주문 로그와 DB를 확인하세요."
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

    def _cancel_post_fill_sync_jobs(self) -> None:
        """체결 후 동기화 예약(1·2·3차)을 취소합니다. 연속 체결 디바운스 시 사용."""
        try:
            r = getattr(self, "root", None)
            if r is None:
                return
            for attr in (
                "_post_fill_sync_job",
                "_post_fill_sync_followup",
                "_post_fill_sync_followup2",
            ):
                try:
                    jid = getattr(self, attr, None)
                    if jid is not None:
                        try:
                            r.after_cancel(jid)
                        except Exception:
                            pass
                        setattr(self, attr, None)
                except Exception:
                    pass
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
            r = getattr(self, "root", None)
            if r is None:
                return
            self._cancel_post_fill_sync_jobs()

            # ms: 기동 직후와 동시에 오가면 너무 길면 부담 — 필요 시 settings 키로만 조정 예정
            first_ms = 4200
            follow_ms = 7000
            # 2차 실행 시점 기준 추가 대기(증권사 예수금상세·D+2 백엔드 지연)
            third_after_second_ms = 22000

            def _first() -> None:
                try:
                    self._post_fill_sync_job = None
                    self._run_portfolio_sync_in_daemon_thread(
                        silent=True,
                        force=True,
                        refresh_cash_deposit=True,
                        wait_for_lock=True,
                    )
                except Exception as e1:
                    try:
                        self.log(f"⚠️ [체결 동기화] 1차 실행 예외: {e1}")
                    except Exception:
                        pass
                try:
                    r2 = getattr(self, "root", None)
                    if r2 is None:
                        return

                    def _second() -> None:
                        try:
                            self._post_fill_sync_followup = None
                            self._run_portfolio_sync_in_daemon_thread(
                                silent=True,
                                force=True,
                                refresh_cash_deposit=True,
                                wait_for_lock=True,
                            )
                        except Exception as e2:
                            try:
                                self.log(f"⚠️ [체결 동기화] 2차 실행 예외: {e2}")
                            except Exception:
                                pass
                        try:
                            r3 = getattr(self, "root", None)
                            if r3 is None:
                                return

                            def _third() -> None:
                                try:
                                    self._post_fill_sync_followup2 = None
                                    self._run_portfolio_sync_in_daemon_thread(
                                        silent=True,
                                        force=True,
                                        refresh_cash_deposit=True,
                                        wait_for_lock=True,
                                    )
                                except Exception as e3b:
                                    try:
                                        self.log(
                                            f"⚠️ [체결 동기화] 3차 실행 예외: {e3b}"
                                        )
                                    except Exception:
                                        pass

                            self._post_fill_sync_followup2 = r3.after(
                                third_after_second_ms, _third
                            )
                        except Exception as e3a:
                            try:
                                self.log(f"⚠️ [체결 동기화] 3차 예약 실패: {e3a}")
                            except Exception:
                                pass

                    self._post_fill_sync_followup = r2.after(follow_ms, _second)
                except Exception as e3:
                    try:
                        self.log(f"⚠️ [체결 동기화] 2차 예약 실패: {e3}")
                    except Exception:
                        pass

            self._post_fill_sync_job = r.after(first_ms, _first)
        except Exception as e0:
            try:
                self.log(f"⚠️ [체결 동기화] 예약 실패: {e0}")
            except Exception:
                pass

    def _on_execution_received(self, code, order_no, qty, price, side):
        try:
            oid = str(order_no or "").strip()
            if oid and oid in self._exchange_fill_order_ids:
                try:
                    try:
                        _fs = capture_supply_snapshot(self, code)
                    except Exception:
                        _fs = {"vol_power": None, "bid_ask_text": ""}
                    self.data_manager.update_order_status(
                        code,
                        order_no,
                        "FILLED",
                        filled_price=price,
                        filled_qty=qty,
                        fill_snap_vol_power=_fs.get("vol_power"),
                        fill_snap_bid_ask=_fs.get("bid_ask_text") or "— / — (—)",
                    )
                except Exception as ue:
                    self.log(f"⚠️ [체결 보정] DB 갱신 실패: {ue}")
                self.log(f"ℹ️ [체결 보정] CHE 체결가 반영 {code} {price:,}원 (주문번호 {oid})")
                self._schedule_account_sync_after_fill()
                return
            try:
                try:
                    _fs = capture_supply_snapshot(self, code)
                except Exception:
                    _fs = {"vol_power": None, "bid_ask_text": ""}
                self.data_manager.update_order_status(
                    code,
                    order_no,
                    "FILLED",
                    filled_price=price,
                    filled_qty=qty,
                    fill_snap_vol_power=_fs.get("vol_power"),
                    fill_snap_bid_ask=_fs.get("bid_ask_text") or "— / — (—)",
                )
            except Exception as ue:
                self.log(f"⚠️ [체결] DB 갱신 실패: {ue}")
            snap_reason = self._resolve_fill_snapshot_reason(
                code, side, None, channel="EXEC"
            )
            snapshot = self.portfolio.capture_snapshot(code, price, reason=snap_reason)
            applied = self.portfolio.on_fill(
                code,
                side,
                price,
                qty,
                snapshot=snapshot,
                fill_channel="exchange",
                order_no=oid or None,
            )
            if (
                not applied
                and str(side or "").lower() == "sell"
            ):
                meta: Optional[Dict[str, Any]] = None
                try:
                    meta = self.data_manager.get_sell_order_meta_by_order_no(oid)
                except Exception:
                    meta = None
                try:
                    nm = str((meta or {}).get("name") or "")
                    rs = str((meta or {}).get("reason") or "")
                except Exception:
                    nm, rs = "", ""
                try:
                    applied = self.portfolio.record_orphan_sell_close(
                        code,
                        float(price),
                        int(qty),
                        nm,
                        rs,
                        oid,
                        snapshot,
                        "exchange",
                    )
                except Exception as oe:
                    self.log(f"⚠️ [체결] 종료내역 보강 실패: {oe}")
            if applied:
                self.log(
                    f"✅ [체결] {code} {'매수' if side == 'buy' else '매도'} {qty}주 완료 ({price:,}원)"
                )
            elif str(side or "").lower() == "sell":
                self.log(
                    f"⚠️ [체결] CHE 매도는 포트폴리오에 미반영({code}) "
                    f"— 로컬 보유 없음·종료내역 보강도 실패. 주문 로그를 확인하세요."
                )
            if oid and applied:
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

    def _render_tree_updates(self) -> None:
        """
        감시 트리 실시간 갱신. 틱이 STARTED 콜백보다 먼저 올 수 있어
        trading_controller.is_monitoring 과 app.is_monitoring 둘 다 본다.
        감시 중이 아니면 타이머 체인을 멈추고 _update_timer_active 를 풀어 재무장 가능하게 한다.
        """
        try:
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
            self.monitor_tab.update_view(
                self.pending_tree_updates, self.portfolio.virtual_positions
            )
            try:
                self.monitor_tab.maybe_reconcile_monitor_trees(
                    getattr(self, "monitor_info", None) or {},
                    self.portfolio.virtual_positions,
                )
            except Exception:
                pass
            self.root.after(REALTIME_TREE_UI_REFRESH_MS, self._render_tree_updates)
        except Exception as e:
            try:
                self.log(f"⚠️ [UI] _render_tree_updates: {e}")
            except Exception:
                pass
            try:
                self._update_timer_active = False
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
                base.append(str(c).zfill(6))
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
                c = str(c_raw).zfill(6)
                row = mi.get(c)
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
                        code=c,
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
                mi[c] = row
                try:
                    self.trading_controller.monitor_info[c] = dict(row)
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
                # prep_ok 는 종목 행(update_warmup_stock_prep)만 갱신 — 상단은 건드리지 않음.
                # (그렇지 않으면 STARTED 로 이미 "감시 중"이 된 뒤에도 매 종목마다
                #  "데이터 준비 중"으로 덮여 감시가 안 된 것처럼 보임.)
                if hasattr(self, "lbl_monitor_status"):
                    if event == "prep_ok":
                        pass
                    elif event == "prep_progress" and (reason or "").strip():
                        line = f"상태: ({cur}/{total}) {name} — {reason.strip()}"
                        self.lbl_monitor_status.config(
                            text=line,
                            foreground=self.colors["primary"],
                        )
                    else:
                        line = f"상태: 데이터 준비 중 ({cur}/{total}) {name}"
                        self.lbl_monitor_status.config(
                            text=line,
                            foreground=self.colors["primary"],
                        )
                if self.status_label:
                    if event == "prep_ok":
                        pass
                    elif event == "prep_progress" and (reason or "").strip():
                        self.status_label.config(
                            text=f"상태: ({cur}/{total}) {name} — {reason.strip()}"
                        )
                    else:
                        self.status_label.config(
                            text=f"상태: 데이터 준비 ({cur}/{total}) {name}"
                        )
            except Exception as e:
                self.log(f"⚠️ [UI] 워밍업 표시 갱신 실패: {e}")

        self.root.after(0, update)

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
        if hasattr(self, 'trading_controller'): self.trading_controller.stop()
        self.root.destroy()

    def _start_eod_equity_snapshot_scheduler(self) -> None:
        """
        매일(거래일 기준) 15:30 + buffer 이후 딱 1회 스냅샷을 저장합니다.
        같은 날짜 중복/덮어쓰기는 파일(YYYY-MM-DD.json) override로 처리합니다.
        """
        try:
            # 이미 예약이 있으면 중복 예약 방지
            if self._equity_snapshot_after_id is not None:
                try:
                    self.root.after_cancel(self._equity_snapshot_after_id)
                except Exception:
                    pass
                self._equity_snapshot_after_id = None
        except Exception:
            pass

        self._equity_snapshot_cancelled = False
        self._schedule_eod_equity_snapshot_tick()

    def _schedule_eod_equity_snapshot_tick(self) -> None:
        try:
            if getattr(self, "root", None) is None:
                return
            self._equity_snapshot_after_id = self.root.after(30000, self._eod_equity_snapshot_tick)
        except Exception:
            self._equity_snapshot_after_id = None

    def _eod_equity_snapshot_tick(self) -> None:
        """
        30초마다 체크:
        - 오늘이 거래일이면 snap_date=today
        - 휴장일이면 snap_date=previous_trading_day
        - 현재 시각이 15:30 + buffer 이후면 스냅샷 저장 job 스폰
        """
        try:
            if self._equity_snapshot_cancelled:
                return
            now = datetime.now()
            buffer_minutes = 10
            try:
                buffer_minutes = int(float(self.settings.get("daily_report_session_buffer_minutes", 10)))
            except Exception:
                buffer_minutes = 10

            # 거래일 판정은 KRX 캘린더(폴백 포함) 사용
            try:
                from reporting.krx_trading_calendar import is_trading_day, previous_trading_day
            except Exception:
                is_trading_day = None
                previous_trading_day = None

            today_d = now.date()

            def _end_of_day_due(day_d: date) -> datetime:
                return datetime.combine(day_d, dtime(15, 30, 0)) + timedelta(
                    minutes=max(0, int(buffer_minutes))
                )

            # (복구 포함) "이미 due가 지난 가장 최근 거래일"을 스냅샷 대상으로 선택
            snap_date: Optional[date] = None
            if callable(is_trading_day) and callable(previous_trading_day):
                try:
                    # 오늘이 휴장이라면 마지막 거래일로 우선 스냅샷 후보
                    last_trading_day = today_d if is_trading_day(today_d) else previous_trading_day(today_d)
                    # 마지막 거래일의 due가 아직 안 지났으면, 한 거래일 전으로 내려가 복구
                    due1 = _end_of_day_due(last_trading_day)
                    if now >= due1:
                        snap_date = last_trading_day
                    else:
                        snap_date = previous_trading_day(last_trading_day)
                except Exception:
                    snap_date = today_d
            else:
                # 예외 상황: 주말만 휴장 처리
                if today_d.weekday() < 5:
                    last_trading_day = today_d
                else:
                    last_trading_day = today_d - timedelta(days=1)
                if now >= _end_of_day_due(last_trading_day):
                    snap_date = last_trading_day
                else:
                    snap_date = last_trading_day - timedelta(days=1)

            if snap_date is None:
                return

            snap_date_str = str(snap_date)[:10]

            # 같은 날짜는 성공 저장된 것만 1회 스킵
            if self._equity_snapshot_last_saved_date == snap_date_str:
                return
            if self._equity_snapshot_pending_date == snap_date_str:
                return
            if self._equity_snapshot_job_in_progress:
                return

            self._equity_snapshot_pending_date = snap_date_str
            self._equity_snapshot_job_in_progress = True

            threading.Thread(
                target=self._job_save_eod_equity_snapshot,
                args=(snap_date_str,),
                daemon=True,
            ).start()
        except Exception:
            # 체크 실패는 다음 tick에서 재시도
            pass
        finally:
            self._schedule_eod_equity_snapshot_tick()

    def _job_save_eod_equity_snapshot(self, snap_date_str: str) -> None:
        """
        백그라운드 작업:
        - (가능하면) 계좌 동기화 1회 force(refresh_cash_deposit=True)
        - 포트폴리오 total_assets(=d2_deposit + holdings_stocks_mv_api) 스냅샷 저장
        """
        saved_ok = False
        try:
            if self._equity_snapshot_cancelled:
                return

            # 스냅샷 직전 계좌 동기화(최소 1회)로 D+2/평가를 맞춥니다.
            try:
                self.portfolio.sync_with_actual_account(
                    silent=True,
                    force=True,
                    refresh_cash_deposit=True,
                )
            except Exception as sync_ex:
                # 동기화 실패해도 스냅샷을 남겨 디버깅 가능(다만 값 신뢰도는 낮을 수 있음)
                try:
                    self.log(f"⚠️ [EOD Snapshot] 동기화 실패: {sync_ex}")
                except Exception:
                    pass

            d2 = 0.0
            stock_eval = 0.0
            d1 = 0.0
            total_equity = 0.0
            with self.portfolio.lock:
                d2 = float(getattr(self.portfolio, "d2_deposit", 0) or 0)
                d1 = float(getattr(self.portfolio, "d1_deposit", 0) or 0)
                stock_eval = float(getattr(self.portfolio, "holdings_stocks_mv_api", 0.0) or 0.0)
                total_equity = float(getattr(self.portfolio, "total_equity", d2 + stock_eval) or 0.0)

            total_assets = d2 + stock_eval

            record = {
                "date": snap_date_str,
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "d2_deposit": d2,
                "d1_deposit": d1,
                "stock_eval": stock_eval,
                "total_assets": total_assets,
                "total_equity": total_equity,
            }

            from utils.equity_snapshots_store import upsert_equity_snapshot

            path = upsert_equity_snapshot(snap_date_str, record)
            if path:
                saved_ok = True
        except Exception as e:
            try:
                self.log(f"❌ [EOD Snapshot] 저장 실패({snap_date_str}): {e}")
            except Exception:
                pass
        finally:
            try:
                self._equity_snapshot_job_in_progress = False
                self._equity_snapshot_pending_date = None
                if saved_ok:
                    self._equity_snapshot_last_saved_date = snap_date_str
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
            # 선정 트랙과 확정 신호 타입 불일치: 의도적 무시 (백테와 동일). 로그·세션 기록 생략.
            if tr == "A" and sig_type == "TYPE_B":
                return False
            if tr == "B" and sig_type == "TYPE_A":
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
