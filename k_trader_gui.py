


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
from core.status_api import StatusApiServer
from core.trading_controller import TradingController, virtual_position_reference_price_for_monitor
from core.strategy_engine import StrategyEngine
from core.execution.post_fill_account_sync_service import PostFillAccountSyncService
from core.realtime_tick_dispatch_service import RealtimeTickDispatchService
from core.eod_equity_snapshot_service import EodEquitySnapshotService
from core.continuous_monitor_service import ContinuousMonitorService
from core.regime_display_service import RegimeDisplayService
from core.rest_auth_retry_service import RestAuthRetryService
from core.execution.fill_reconciliation_service import FillReconciliationService
from core.execution.order_execution_service import OrderExecutionService
from core.execution.order_action_dispatcher import OrderActionDispatcher
from core.execution.fill_confirmation_service import FillConfirmationService
from core.defaults import DEFAULT_SETTINGS, get_allowed_keys
from core.version import APP_VERSION
from core.monitor_signal_status_display import monitor_signal_status_display_text
from core.supply_display import capture_supply_snapshot
from core.signal_callbacks_service import SignalCallbacksService
from core.ui_trading_status_service import UiTradingStatusService
from core.warmup_stock_prep_ui_service import WarmupStockPrepUiService
from core.rt_ui_responsiveness import drain_ui_call_queue_once
from core.auth_manager import AuthManager
from core.config_handler import ConfigHandler
from core.account_summary_telegram_service import AccountSummaryTelegramService
from core.selection_monitor_start_service import SelectionMonitorStartService
from core.rt_watchlist_m1_api_poller import RtWatchlistM1ApiPoller
from core.next_open_pending_action_service import NextOpenPendingActionService
from core.nxt_eligibility_service import NxtEligibilityService

from utils.monitor_vp_stub import is_vp_price_stub_active, stamp_vp_price_stub
from utils.normalization import canonical_stock_code

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


# AuthManager / ConfigHandler 는 core 로 이전 (단일 구현).


class KiwoomApp:
    @property
    def access_token(self): return self.auth_manager.access_token
    @access_token.setter
    def access_token(self, value): self.auth_manager.access_token = value
        
    @property
    def virtual_positions(self): return self.portfolio.virtual_positions

    def _sync_ka10080_rest_pacing_from_settings(self) -> None:
        """settings → config.KA10080_GLOBAL_MIN_INTERVAL_SEC (ka10080 REST 전역 최소 간격)."""
        try:
            st = getattr(self, "settings", None) or {}
            # 키움 ka10080 유량=1 → 기본 1.05초(여유). 0.5 이하는 429 위험이 큼.
            g = float(st.get("realtime_ka10080_global_min_interval_sec", 1.05))
            g = max(0.8, min(g, 3.0))
            self.config.KA10080_GLOBAL_MIN_INTERVAL_SEC = g
        except Exception:
            try:
                self.config.KA10080_GLOBAL_MIN_INTERVAL_SEC = 1.05
            except Exception:
                pass

    def _configure_ka10080_fetch_diag(self) -> None:
        """ka10080 조회 실패·통계 진단 — session JSONL + 대시보드 로그 연결."""
        try:
            from core.ka10080_fetch_diag import ka10080_fetch_diag

            ka10080_fetch_diag.configure(
                session_logger=getattr(self, "session_logger", None),
                ui_log=self.log,
                settings=getattr(self, "settings", None) or {},
            )
        except Exception:
            pass

    def __init__(self, root, auto_start=False):
        self.root = root
        self._main_thread_id = threading.get_ident()
        # 비메인 스레드 → 메인 Tk 루프 작업 큐 (root.after 를 워커에서 직접 호출하지 않음)
        self._ui_call_queue: queue.Queue = queue.Queue()
        self._ui_call_drain_after_id: Optional[Any] = None
        try:
            from utils.gui_restart import read_and_clear_restart_state

            _restart_state = read_and_clear_restart_state()
        except Exception:
            _restart_state = None
        if _restart_state is not None:
            auto_start = bool(_restart_state.get("resume_monitoring", False))
            self._suppress_auto_monitor_start = not auto_start
            self._restart_resume_monitoring = auto_start
        else:
            self._suppress_auto_monitor_start = False
            self._restart_resume_monitoring = False
        self._restart_resume_started = False
        self.auto_start = bool(auto_start)
        self.root.title(f"K-Trader v{APP_VERSION} [Remote Monitoring]")
        self.root.geometry("1400x900")
        try:
            self.root.after(50, self._drain_ui_call_queue)
        except Exception:
            pass
        
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
        self.continuous_monitor_service = ContinuousMonitorService(
            self, log_func=self.log
        )
        self.regime_display_service = RegimeDisplayService(
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
        # REST 주문 접수 직후 체결확정 확인(폴링) in-flight 중복 방지
        self._exchange_fill_order_inflight: Set[str] = set()
        # (구) 주기 계좌 동기화 타이머 취소용. 자동 동기화는 기동 후 1회만 수행한다.
        self._account_sync_job: Optional[Any] = None
        self._virtual_tab_render_pending: bool = False
        self._vt_orders_refresh_after_id: Any = None
        self._vt_light_refresh_after_id: Any = None
        self._vt_light_refresh_dirty: bool = False
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
        self.account_summary_telegram_service = AccountSummaryTelegramService(self)
        self.selection_monitor_start_service = SelectionMonitorStartService(self)

        self.settings = self.config_handler.load_settings(DEFAULT_SETTINGS)
        # KODEX200 구성 유니버스 폐기 → 항상 KOSPI200
        try:
            if isinstance(self.settings, dict):
                self.settings["universe_index_choice"] = "KOSPI200"
        except Exception:
            pass
        # 실시간 15분 슬롯 격자의 애프터 포함 여부를 현재 설정으로 판정(설정 재로드·저장 즉시 반영)
        try:
            from utils.aftermarket_policy import register_live_settings_provider

            register_live_settings_provider(lambda: getattr(self, "settings", None))
        except Exception as e:
            print(f"⚠️ 애프터 봉 정책 등록 실패: {e}")
        # 애프터 정책 OFF 시 실시간 선정·레짐용 정규장 일봉(키움 15분봉) 캐시 — 장전·장마감 후 사전조회
        try:
            from core.regular_daily_prefetch import RegularDailyPrefetcher
            from data_manager.krx_regular_daily import register_kiwoom_token_provider

            register_kiwoom_token_provider(self.auth_manager.get_auth_token)
            self.regular_daily_prefetcher = RegularDailyPrefetcher(self)
            self.regular_daily_prefetcher.start()
        except Exception as e:
            print(f"⚠️ 정규장 일봉 사전조회 등록 실패: {e}")
        self._sync_ka10080_rest_pacing_from_settings()
        self._configure_ka10080_fetch_diag()
        # Tick dispatch 주기는 SignalEngine이 메인 스레드를 과점유할 때 UI가 멎는 문제를 직접 줄이므로,
        # 애플리케이션 인스턴스에 모듈 상수값을 그대로 심어둔다.
        try:
            self.REALTIME_TICK_DISPATCH_FLUSH_MS = int(REALTIME_TICK_DISPATCH_FLUSH_MS)
        except Exception:
            self.REALTIME_TICK_DISPATCH_FLUSH_MS = 150
        
        self.bars_data = self.config_handler.load_bars_cache()
        self._bars_data_lock = threading.RLock()

        # [v4.5.7] 실시간 감시 전용 15분봉 DB(하베스팅 trading_data.db 와 분리) — 슬롯 변경 시 확정 봉 적재.
        # ``realtime_15m_calendar_retention_days`` 는 SQLite 롤링(오래된 봉 삭제)에만 쓰이며,
        # 워밍업이 과거 N일을 채우는 용도는 아님.
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
        from core.rt_tick_m1_store import (
            RtTickM1Store,
            official_m1_retention_for_gap_fill,
        )
        from core.stream_processor import StreamProcessor

        try:
            _m1_heal_bars = (self.settings or {}).get(
                "realtime_m1_head_fill_missing_15m_max_bars", 8
            )
            _official_m1_keep = official_m1_retention_for_gap_fill(_m1_heal_bars)
        except Exception:
            _official_m1_keep = 128
        self.rt_tick_m1_store = RtTickM1Store(
            max_completed_per_code=_official_m1_keep,
            logger_callback=self.log,
        )
        from core.rt_tick_m1_accumulator import RtTickM1Accumulator

        # 검증용 틱→1분 DB 저장(기본 OFF). 신호용 말분 틱은 rt_tick_m1_store.feed_tick.
        self.rt_tick_m1_accumulator = None
        try:
            _tick_save = bool(
                (self.settings or {}).get(
                    "realtime_tick_m1_accumulate_save_only", False
                )
            )
        except Exception:
            _tick_save = False
        if _tick_save:
            try:
                self.rt_tick_m1_accumulator = RtTickM1Accumulator(
                    settings=self.settings,
                    logger_callback=self.log,
                    get_settings=lambda: getattr(self, "settings", None) or {},
                )
                from utils.server_mode_paths import realtime_tick_1m_db_path

                self.log(
                    "ℹ️ [Tick1m] 틱→1분 검증 누적 ON "
                    f"(전용DB={realtime_tick_1m_db_path()}, 신호·주문 미사용)"
                )
            except Exception as e:
                self.rt_tick_m1_accumulator = None
                try:
                    self.log(f"⚠️ [Tick1m] 검증 누적 기동 실패: {e!r}")
                except Exception:
                    pass
        else:
            try:
                self.log(
                    "ℹ️ [Tick1m] 틱→1분 검증 DB 저장 OFF "
                    "(신호용 말분 틱 누적은 RtTickM1Store 유지)"
                )
            except Exception:
                pass

        _rt15_store = self.realtime_monitor_bars_store
        from core.closed_15m_fast_path import Closed15mDbUpsertQueue, Closed15mEvalPool

        self._closed_15m_db_queue = Closed15mDbUpsertQueue(_rt15_store, log=self.log)
        self._closed_15m_db_queue.start()
        self._closed_15m_eval_pool = Closed15mEvalPool(self, log=self.log)

        def _on_rt15_bar_closed(code, ts, o, h, l, c, v):
            # DB upsert는 비동기 — 신호 평가 경로를 블로킹하지 않음
            try:
                self._closed_15m_db_queue.enqueue(code, ts, o, h, l, c, v)
            except Exception as ex:
                try:
                    self.log(f"⚠️ [RT15mDB] 확정봉 큐잉 실패: {ex}")
                except Exception:
                    pass

            st = self.settings or {}
            try:
                from core.rt_closed_15m_official_replace import (
                    closed_15m_eval_after_official_enabled,
                    m1_api_poller_enabled,
                )

                eval_after_official = closed_15m_eval_after_official_enabled(st)
                m1_on = m1_api_poller_enabled(st)
            except Exception:
                eval_after_official = bool(
                    st.get("realtime_closed_15m_eval_after_official", False)
                ) and bool(st.get("realtime_s1_eval_on_local_15m_close", True))
                m1_on = bool(st.get("realtime_watchlist_m1_api_poller_enabled", True))

            # M1 경로: 말분 공식/틱 보조로 평가 — 로컬 롤 직후 즉시 평가 금지
            # 레거시(M1 OFF + eval_after_official OFF): 로컬 확정 즉시 평가
            # 공식 15분 정렬교체는 사용하지 않음(M1 합성·누락채움으로 대체).
            try:
                if (
                    bool(st.get("realtime_s1_eval_on_local_15m_close", True))
                    and not eval_after_official
                    and not m1_on
                ):
                    self._closed_15m_eval_pool.submit_eval(code, ts)
            except Exception:
                pass

        self._rt15_bar_closed_cb = _on_rt15_bar_closed
        self.rt_closed_15m_official_scheduler = None
        self.rt_watchlist_m1_api_poller = RtWatchlistM1ApiPoller(self, log=self.log)
        try:
            from core.rt_closed_15m_official_replace import m1_api_poller_enabled as _m1_en_fn

            _m1_en = bool(_m1_en_fn(self.settings or {}))
        except Exception:
            try:
                _m1_en = bool(
                    (self.settings or {}).get(
                        "realtime_watchlist_m1_api_poller_enabled", True
                    )
                )
            except Exception:
                _m1_en = True
        if _m1_en:
            self.rt_watchlist_m1_api_poller.start()
            try:
                self.log(
                    "ℹ️ [M1Poll] 활성 — 마지막 1분 완성 시 보유·고합치도 우선 평가"
                )
            except Exception:
                pass
        else:
            try:
                self.log(
                    "ℹ️ [M1Poll] 비활성(realtime_watchlist_m1_api_poller_enabled=false)"
                )
            except Exception:
                pass
        from core.realtime_official_bars_sync import Rt15mSlotBoundaryWatcher

        self.rt_15m_slot_boundary_watcher = Rt15mSlotBoundaryWatcher(self, log=self.log)
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
        # KOSPI 유니버스 CSV만 종목명 캐시로 선적재 (KODEX200 구성 유니버스 폐기)
        try:
            self.watchlist_store.preload_from_universe_index(
                "KOSPI200", logger_callback=self.log
            )
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
        # 렌더 타이머가 이미 돌 때도 틱 갱신 요청을 잃지 않도록 더티 플래그
        self._monitor_tree_dirty: bool = False
        self._inject_min_last_ts: float = 0.0
        self._tick_main_poller_after_id: Optional[Any] = None
        self._tick_main_poller_lock = threading.Lock()
        # 주문 액션 디스패처는 OrderActionDispatcher 가 담당 (아래에서 초기화)
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
        )
        
        # [v16.50] 최적화 상태 데이터 중앙 집중화 (Singleton State Management)
        # 탭 전환 시 데이터 유실 방지를 위해 메인 앱 인스턴스에서 직접 관리
        self.opt_params_vars = {}
        self.opt_param_labels: dict[str, str] = {}
        # 시드: Simple 경로 핵심 키 (설정 탭 빌드 시 add_param_row가 나머지 UI 키를 자동 등록)
        opt_keys = [
            's1_equity_pct', 's2_equity_pct',
            'buy_rsi_min', 'buy_rsi_max_a', 'gc_window', 'rsi_window', 'ma_convergence',
            'ma60_min', 'ma60_max',
            'vol_surge', 'prev_vol_lookback_s1_a',
            'buy_rsi_min_b', 'buy_rsi_max_b', 'vol_surge_b', 'prev_vol_lookback_s1_b',
            's2_ma20_gap', 's2_ma5_gap', 's2_rsi_gap', 's2_macd_gap', 's2_vol_ratio',
            'prev_vol_lookback_s2_a', 's2_profit_limit', 's2_delay_hours',
            's2_b_ma20_gap', 's2_b_ma5_gap', 's2_b_rsi_gap', 's2_b_macd_gap', 's2_b_vol_ratio',
            'prev_vol_lookback_s2_b', 's2_b_profit_limit', 's2_b_delay_hours',
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
            'atr_sl_entry_grace_hours', 'atr_sl_entry_grace_mult',
            # --- ON/OFF (SimpleBacktester 경로에서 실제 반영되는 것만) ---
            'use_rsi_min', 'use_macd_signal', 'use_rsi_sig_filter',
            'use_type_a', 'use_gc', 'use_rsi_signal', 'use_ma60_safe', 'use_vol_surge',
            'use_type_b', 'use_type_b_vol', 'use_trend_aligned_b', 'use_macd_gc_b',
            'use_s2_master_a', 'use_s2_ma20', 'use_s2_ma5', 'use_s2_rsi_sig',
            'use_s2_macd_gc', 'use_s2_vol_surge', 'use_s2_profit', 'use_s2_delay',
            'use_s2_master_b', 'use_s2_b_ma20', 'use_s2_b_ma5', 'use_s2_b_rsi_sig',
            'use_s2_b_macd_gc', 'use_s2_b_vol_surge', 'use_s2_b_profit', 'use_s2_b_delay',
            'use_gap_down_exit_a', 'use_gap_grace_a', 'use_atr_exit_a',
            'use_s1_a_tp', 'use_s1_a_stop_loss', 'use_s1_a_flash_exit',
            'use_atr_stop_loss_a', 'use_atr_trailing_stop_a', 'use_atr_trail_only_after_tp_s1_a',
            'use_s2_tp', 'use_s2_stop_loss', 'use_s2_flash_exit',
            'use_atr_stop_loss_s2', 'use_atr_trailing_stop_s2', 'use_atr_trail_only_after_tp_s2_a',
            'use_gap_down_exit_b', 'use_gap_grace_b', 'use_atr_exit_b',
            'use_s1_b_tp', 'use_s1_b_stop_loss', 'use_s1_b_flash_exit',
            'use_atr_stop_loss_b', 'use_atr_trailing_stop_b', 'use_atr_trail_only_after_tp_s1_b',
            'use_s2_b_tp', 'use_s2_b_stop_loss', 'use_s2_b_flash_exit',
            'use_atr_stop_loss_s2_b', 'use_atr_trailing_stop_s2_b', 'use_atr_trail_only_after_tp_s2_b',
            'atr_sl_entry_grace_same_day',
        ]
        for key in opt_keys:
            # 설정 파일에 저장된 값이 있으면 그것을 사용, 없으면 False 초기화
            saved_val = self.settings.get(f"opt_{key}", False)
            self.opt_params_vars[key] = tk.BooleanVar(value=saved_val)
        self.status_api_server = StatusApiServer(self, log_func=self.log)
        # 주문 집행(S1/S2/EXIT) — TradingController 와 분리
        self.order_execution_service = OrderExecutionService(
            self, log_func=self.log
        )
        self.next_open_pending_action_service = NextOpenPendingActionService(
            self, log_func=self.log
        )
        self.nxt_eligibility_service = NxtEligibilityService(
            self, log_func=self.log
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

        # 매수/매도 액션 큐·워커 (SignalEngine 틱 후속 → 비동기 집행)
        self.order_action_dispatcher = OrderActionDispatcher(
            self,
            on_buy_signal=self._on_buy_signal_received,
            on_strategy_action=self._on_strategy_action_needed,
            log_func=self.log,
        )

        # REST 인증/재시도 워커 서비스 추출
        self.rest_auth_retry_service = RestAuthRetryService(
            self, log_func=self.log
        )

        # 체결 동기화(REST/WS) — FillConfirmationService 먼저 초기화
        self.fill_confirmation_service = FillConfirmationService(
            self, log_func=self.log
        )
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
        try:
            self._start_sleep_inhibit()
        except Exception:
            pass
        # EOD 스냅샷 자동 저장 시작(15:30 + buffer 분)
        try:
            self._start_eod_equity_snapshot_scheduler()
        except Exception:
            pass
        try:
            self.next_open_pending_action_service.start_scheduler()
        except Exception:
            pass
        try:
            self.regime_display_service.start()
        except Exception:
            pass

        # 앱이 켜져 있으면 거래일마다 개장 N분 전(기본 25분)에 감시를 자동 시작.
        try:
            if bool(getattr(self, "_suppress_auto_monitor_start", False)):
                self.log(
                    "[Auto] 재시작(감시 OFF) — 장전·자동 감시 시작을 건너뜁니다."
                )
            else:
                cms = getattr(self, "continuous_monitor_service", None)
                resume_after_restart = bool(
                    getattr(self, "_restart_resume_monitoring", False)
                )
                monitor_scheduled = False
                if cms is not None:
                    cms.arm()
                    if resume_after_restart:
                        self.log(
                            "[Auto] 재시작(감시 ON) — 인증 완료 후 실시간 감시를 복원합니다."
                        )
                    else:
                        ms = int(cms.ms_until_auto_start())
                        hm = cms._auto_start_hhmm_str()
                        in_morning_window = ms <= 0
                        mid_day_catchup = False
                        try:
                            mid_day_catchup = bool(
                                self.auto_start
                                and (not in_morning_window)
                                and cms.is_before_eod_on_trading_day()
                            )
                        except Exception:
                            mid_day_catchup = bool(self.auto_start)
                        if in_morning_window or mid_day_catchup:
                            monitor_scheduled = True
                            self.log(
                                f"[Auto] 장전 시작 시각({hm}) 경과 — "
                                "실시간 감시를 곧 시작합니다..."
                            )
                            if in_morning_window:
                                self.root.after(1000, cms.request_morning_start)
                            else:
                                self.root.after(
                                    1000, self.ensure_fresh_selection_then_start
                                )
                        else:
                            try:
                                mins = int(cms._minutes_before_open())
                            except Exception:
                                mins = 25
                            self.log(
                                f"[Auto] 연속 스케줄 활성화 — "
                                f"감시 시작 {hm} (개장 {mins}분 전, 약 {ms // 1000}초 후), "
                                f"장마감 선정 {cms._format_eod_hhmm()}"
                            )
                            try:
                                wait_txt = f"상태: 대기 중 (감시 시작 {hm})"
                                if getattr(self, "lbl_monitor_status", None) is not None:
                                    self.lbl_monitor_status.config(
                                        text=wait_txt,
                                        foreground=self.colors.get("primary", "#88C0D0"),
                                    )
                                if getattr(self, "status_label", None) is not None:
                                    self.status_label.config(
                                        text=wait_txt,
                                        foreground=self.colors.get("primary", "#88C0D0"),
                                    )
                            except Exception:
                                pass
                if (
                    self.auto_start
                    and not monitor_scheduled
                    and not resume_after_restart
                ):
                    monitor_scheduled = True
                    self.log("[Auto] 8초 후 실시간 감시를 자동으로 시작합니다...")
                    self.root.after(8000, self.ensure_fresh_selection_then_start)
        except Exception:
            if self.auto_start and not bool(
                getattr(self, "_suppress_auto_monitor_start", False)
            ) and not bool(getattr(self, "_restart_resume_monitoring", False)):
                self.log("[Auto] 8초 후 실시간 감시를 자동으로 시작합니다...")
                self.root.after(8000, self.ensure_fresh_selection_then_start)
        
    def _final_save_settings(self):
        """메모리 설정을 settings.json 에 저장한다.

        백테스트/최적화 시작 등에서 호출될 때, 설정 탭 UI 와 메모리가
        어긋난 채로 이전 값(예: 게이트 OFF)을 파일에 덮어쓰지 않도록
        저장 직전에 설정 탭 위젯 → 메모리를 동기화한다.
        """
        try:
            st = getattr(self, "settings_tab", None)
            if st is not None and hasattr(st, "collect_from_ui"):
                st.collect_from_ui()
        except Exception as e:
            try:
                self.log(f"⚠️ 설정 탭 → 메모리 동기화 실패(저장은 계속): {e}")
            except Exception:
                pass
        if self.config_handler.save_settings(self.settings):
            self.log("💿 설정 파일 저장 및 데이터 동기화 완료")
            return True
        return False

    def reset_ui_tabs(self):
        self.settings = self.config_handler.load_settings(DEFAULT_SETTINGS)
        # KODEX200 구성 유니버스 폐기 → 항상 KOSPI200
        try:
            if isinstance(self.settings, dict):
                self.settings["universe_index_choice"] = "KOSPI200"
        except Exception:
            pass
        self._sync_ka10080_rest_pacing_from_settings()
        self._configure_ka10080_fetch_diag()
        try:
            self._apply_sleep_inhibit_from_settings()
        except Exception:
            pass
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
            self.header_frame.columnconfigure(0, weight=1)
            self.header_frame.columnconfigure(1, weight=0)
            self.header_frame.columnconfigure(2, weight=1)

            self.lbl_header_title = ttk.Label(
                self.header_frame,
                text=f"K-Trader v{APP_VERSION}",
                font=("Malgun Gothic", 10, "bold"),
                foreground=self.colors["primary"],
            )
            self.lbl_header_title.grid(row=0, column=0, sticky="w")

            self.regime_header_box = tk.Frame(
                self.header_frame,
                bg=self.colors["bg"],
            )
            self.regime_header_box.grid(row=0, column=1, padx=12)

            self.lbl_regime_status = tk.Label(
                self.regime_header_box,
                text="B 추세 레짐: —",
                font=("Malgun Gothic", 12, "bold"),
                fg=self.colors["primary"],
                bg=self.colors["bg"],
                anchor="center",
            )
            self.lbl_regime_status.pack(side=tk.LEFT)

            # G1(Type A 신규 억제)·R5(Type A 청산 타이트) — 레짐과 별도 색상
            self.lbl_g1_status = tk.Label(
                self.regime_header_box,
                text="",
                font=("Malgun Gothic", 10, "bold"),
                fg=self.colors["primary"],
                bg=self.colors["bg"],
            )
            self.lbl_g1_status.pack(side=tk.LEFT, padx=(10, 0))

            self.lbl_r5_status = tk.Label(
                self.regime_header_box,
                text="",
                font=("Malgun Gothic", 10, "bold"),
                fg=self.colors["primary"],
                bg=self.colors["bg"],
            )
            self.lbl_r5_status.pack(side=tk.LEFT, padx=(8, 0))

            self.btn_regime_history = ttk.Button(
                self.regime_header_box,
                text="30일",
                width=4,
                command=self._on_regime_history_clicked,
            )
            self.btn_regime_history.pack(side=tk.LEFT, padx=(6, 0))

            status_frame = ttk.Frame(self.root)
            status_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=5, pady=2)
            
            self.status_label = ttk.Label(status_frame, text="상태: 대기 중", font=("Malgun Gothic", 9), foreground=self.colors['primary'])
            self.status_label.pack(side=tk.RIGHT, padx=5)
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
        self.btn_monitor_toggle = self.dashboard_tab.btn_monitor_toggle
        self.btn_select = self.dashboard_tab.btn_select
        self.lbl_monitor_status = self.monitor_tab.lbl_monitor_status
        
        self.log("[System] 화면 구성 및 GUI 모듈 초기화 완료")
        if not hasattr(self, '_initial_threads_started'):
            self._initial_threads_started = True
            # 인증은 내부에서 백그라운드 워커로 재시도하므로 여기서는 추가 스레드 불필요
            self.get_auth_token()
        try:
            # 모바일 읽기 API: 인증과 독립적으로 설정에 따라 기동
            self.root.after(1200, self._maybe_start_status_api)
        except Exception:
            pass
        try:
            # UI 루프가 끊긴 상태(_update_timer_active만 True로 고정 등)를 자동 복구
            if self._ui_watchdog_after_id is None:
                self._ui_watchdog_after_id = self.root.after(
                    800, self._ui_render_watchdog_tick
                )
        except Exception:
            pass

    def _maybe_start_status_api(self) -> None:
        """설정이 켜져 있으면 모바일 상태 API를 기동하고 대시보드 라벨을 갱신합니다."""
        try:
            api = getattr(self, "status_api_server", None)
            if api is None:
                return
            api.start()
        except Exception as e:
            try:
                self.log(f"[MobileAPI] 기동 예외: {e}")
            except Exception:
                pass
        try:
            dt = getattr(self, "dashboard_tab", None)
            if dt is not None and hasattr(dt, "refresh_mobile_api_status"):
                dt.refresh_mobile_api_status()
        except Exception:
            pass

    def restart_status_api_from_settings(self) -> None:
        """설정 저장 후 모바일 API 재기동."""
        try:
            api = getattr(self, "status_api_server", None)
            if api is None:
                return
            if api.is_enabled():
                api.restart()
            else:
                api.stop()
        except Exception as e:
            try:
                self.log(f"[MobileAPI] 재기동 실패: {e}")
            except Exception:
                pass
        try:
            dt = getattr(self, "dashboard_tab", None)
            if dt is not None and hasattr(dt, "refresh_mobile_api_status"):
                dt.refresh_mobile_api_status()
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
            self.run_on_ui_thread(_apply, 0)
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
            self.run_on_ui_thread(_apply, 0)
        except Exception:
            pass

    def log(self, message): self.gui_utils.log(message)

    def _is_ui_main_thread(self) -> bool:
        """현재 스레드가 Tk 메인 스레드인지."""
        try:
            return threading.get_ident() == int(getattr(self, "_main_thread_id", 0) or 0)
        except Exception:
            return False

    def run_on_ui_thread(self, callback: Any, delay_ms: int = 0) -> None:
        """
        Tk 위젯 갱신·``root.after`` 예약을 **메인 스레드에서만** 수행합니다.

        인증/계좌 동기화 등 백그라운드에서 ``root.after`` / ``winfo_exists`` 를 직접 호출하면
        Windows 에서 ``main thread is not in main loop`` 로 프로세스가 죽을 수 있습니다.
        """
        if not callable(callback):
            return
        try:
            delay = max(0, int(delay_ms))
        except Exception:
            delay = 0

        if self._is_ui_main_thread():
            try:
                r = getattr(self, "root", None)
                if r is None:
                    callback()
                    return
                if delay <= 0:
                    r.after(0, callback)
                else:
                    r.after(delay, callback)
            except Exception:
                try:
                    callback()
                except Exception:
                    pass
            return

        try:
            self._ui_call_queue.put((delay, callback))
        except Exception:
            return
        # 메인 폴러가 이미 돌고 있으면 enqueue 만으로 충분.
        # 폴러가 끊겼을 때만 메인에서 재기동되도록 플래그만 남긴다.

    def _drain_ui_call_queue(self) -> None:
        """메인 스레드: 백그라운드가 넣은 UI 작업을 꺼내 after 로 실행/예약."""
        self._ui_call_drain_after_id = None
        remaining = False
        try:
            budget_sec = 0.012
            max_n = 8
            try:
                st = getattr(self, "settings", None) or {}
                budget_sec = float(st.get("ui_call_queue_drain_budget_sec", 0.012))
                max_n = int(float(st.get("ui_call_queue_drain_max_n", 8)))
            except (TypeError, ValueError):
                budget_sec = 0.012
                max_n = 8
            r = getattr(self, "root", None)

            def _run_now(cb: Any) -> None:
                if r is None or not callable(cb):
                    return
                try:
                    cb()
                except Exception:
                    pass

            def _schedule_delayed(d: int, cb: Any) -> None:
                if r is None or not callable(cb):
                    return
                try:
                    r.after(d, cb)
                except Exception:
                    pass

            remaining = bool(
                drain_ui_call_queue_once(
                    self._ui_call_queue,
                    run_now=_run_now,
                    schedule_delayed=_schedule_delayed,
                    budget_sec=budget_sec,
                    max_n=max_n,
                )
            )
        except Exception:
            remaining = False
        try:
            r2 = getattr(self, "root", None)
            if r2 is not None:
                # 남은 작업이 있으면 바로 이어서, 없으면 50ms 폴링
                delay_ms = 1 if remaining else 50
                self._ui_call_drain_after_id = r2.after(
                    delay_ms, self._drain_ui_call_queue
                )
        except Exception:
            pass

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
        schedule_gui_refresh: bool = True,
        schedule_orders_refresh: bool = True,
        sync_context: str = "",
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
                    schedule_gui_refresh=schedule_gui_refresh,
                    schedule_orders_refresh=schedule_orders_refresh,
                    sync_context=str(sync_context or "").strip(),
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
            idx_choice = "KOSPI200"
            try:
                from strategy.universe_codes import normalize_universe_index_choice

                idx_choice = normalize_universe_index_choice(
                    (self.settings or {}).get("universe_index_choice")
                )
            except Exception:
                idx_choice = "KOSPI200"
            # 구 설정에 KODEX200 이 남아 있어도 강제 마이그레이션
            if isinstance(self.settings, dict):
                self.settings["universe_index_choice"] = "KOSPI200"
        except Exception:
            idx_choice = "KOSPI200"
        self.log(f"[Data] 유니버스({idx_choice}) 로드...")
        df_200 = self.data_manager.get_kospi_list(
            count=200,
            force=force,
            settings=getattr(self, "settings", None),
            universe_log=self.log,
        )
        if df_200 is not None: 
            self.log(f"[Data] 감시 대상 {len(df_200)}개 종목 로드 완료")
            # [v4.5.2] 종목 선정 버튼 활성화
            if getattr(self, "btn_select", None) is not None:
                self.root.after(0, lambda: self.btn_select.config(state=tk.NORMAL))

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

    def enqueue_closed_15m_order_action(self, kind: str, payload: Any) -> bool:
        """
        로컬 15분 확정 평가용 주문 패스트패스.

        Tk 메인 큐(버스트/폴러)를 우회하고 OrderActionDispatcher 로 직행합니다.
        """
        try:
            return bool(
                self._enqueue_order_action(
                    str(kind), payload, source="closed_15m"
                )
            )
        except Exception:
            return False

    def mark_tick_worker_ui_dirty(self) -> None:
        """워커가 감시 트리 갱신이 필요함을 표시합니다."""
        try:
            self._tick_worker_ui_dirty = True
            self._monitor_tree_dirty = True
        except Exception:
            pass

    def mark_monitor_tree_dirty(self) -> None:
        """감시 트리에 반영할 변경이 있음을 표시합니다(렌더 체인 가동 중에도 유실 방지)."""
        try:
            self._monitor_tree_dirty = True
        except Exception:
            pass

    def mark_tick_worker_virtual_dirty(self) -> None:
        """워커가 포지션 탭 실시간 갱신이 필요함을 표시합니다."""
        try:
            self._tick_worker_virtual_dirty = True
        except Exception:
            pass

    def _enqueue_order_action(
        self, kind: str, payload: Any, *, source: str = "tick"
    ) -> bool:
        """OrderActionDispatcher 위임."""
        try:
            disp = getattr(self, "order_action_dispatcher", None)
            if disp is not None:
                return bool(disp.enqueue(kind, payload, source=source))
        except Exception:
            pass
        return False

    def _shutdown_order_action_workers(self) -> None:
        """OrderActionDispatcher 위임."""
        try:
            disp = getattr(self, "order_action_dispatcher", None)
            if disp is not None:
                disp.shutdown()
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
            budget_sec = 0.015
            try:
                budget_sec = float(
                    st.get("realtime_tick_main_queue_budget_sec", 0.015)
                )
            except (TypeError, ValueError):
                budget_sec = 0.015
            budget_sec = max(0.004, min(budget_sec, 0.080))
            while processed < max_burst:
                try:
                    if processed > 0 and (time.perf_counter() - t0) >= budget_sec:
                        break
                except Exception:
                    break
                try:
                    kind, payload = self._tick_main_actions_queue.get_nowait()
                except queue.Empty:
                    break
                processed += 1
                try:
                    if kind == "buy_signal" and isinstance(payload, tuple):
                        if not self._enqueue_order_action("buy_signal", payload):
                            try:
                                bcode = (
                                    str(payload[0]).zfill(6)
                                    if len(payload) > 0
                                    else ""
                                )
                                sl = getattr(self, "session_logger", None)
                                if sl is not None:
                                    sl.log(
                                        "signal_buy_skipped",
                                        {
                                            "code": bcode,
                                            "reason": "order_action_enqueue_rejected",
                                        },
                                    )
                            except Exception:
                                pass
                    elif kind == "strategy_action" and isinstance(payload, tuple):
                        if not self._enqueue_order_action(
                            "strategy_action", payload
                        ):
                            try:
                                scode = (
                                    str(payload[0]).zfill(6)
                                    if len(payload) > 0
                                    else ""
                                )
                                sl = getattr(self, "session_logger", None)
                                if sl is not None:
                                    sl.log(
                                        "strategy_action_skipped",
                                        {
                                            "code": scode,
                                            "reason": "order_action_enqueue_rejected",
                                        },
                                    )
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
        *,
        boost_polling: bool = False,
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
                boost_polling=boost_polling,
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
                # 체인이 돌고 있어도 더티만 표시 — 다음 프레임에서 pending 반영
                try:
                    self._monitor_tree_dirty = True
                except Exception:
                    pass
                # 렌더가 이미 메인 스레드에서 돌고 있으면 after(0) 중복 예약 금지(폭주·멈춤 악화)
                if bool(getattr(self, "_ui_render_in_progress", False)):
                    try:
                        now_ts = time.time()
                        last_enter = float(
                            getattr(self, "_ui_render_last_enter_ts", 0.0) or 0.0
                        )
                        stale_sec = float(
                            (getattr(self, "settings", None) or {}).get(
                                "ui_render_stale_rearm_sec", 2.5
                            )
                        )
                        stale_sec = max(1.0, min(stale_sec, 15.0))
                        if (now_ts - last_enter) < stale_sec:
                            return
                        # 렌더 진입 후 오래 멈춘 것으로 보이면 플래그를 풀고 재무장
                        self._ui_render_in_progress = False
                    except Exception:
                        return
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
                    stale_sec = 2.0
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
                        self._ui_render_in_progress = False
                        self.ensure_monitor_tree_refresh_scheduled(immediate=True)
        except Exception:
            pass
        finally:
            try:
                r = getattr(self, "root", None)
                if r is not None and r.winfo_exists():
                    self._ui_watchdog_after_id = r.after(
                        800, self._ui_render_watchdog_tick
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
            self._ui_render_in_progress = True
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
            next_delay_ms = REALTIME_TREE_UI_REFRESH_MS
            try:
                pending_now = len(getattr(self, "pending_tree_updates", {}) or {})
            except Exception:
                pending_now = 0
            # pending 이 많을 때 inject 가 큐를 더 키워 렌더가 밀리는 것을 방지
            try:
                inject_skip_pending = int(
                    float(
                        (getattr(self, "settings", None) or {}).get(
                            "ui_force_row_refresh_skip_when_pending_ge", 22
                        )
                    )
                )
            except (TypeError, ValueError):
                inject_skip_pending = 22
            inject_skip_pending = max(8, min(inject_skip_pending, 200))
            if pending_now < inject_skip_pending:
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
            tree_dirty = bool(getattr(self, "_monitor_tree_dirty", False))
            if tree_dirty:
                try:
                    self._monitor_tree_dirty = False
                except Exception:
                    pass
            if backlog_size >= busy_threshold or tree_dirty:
                next_delay_ms = busy_ms
            elif backlog_size <= 0 and not tree_dirty:
                next_delay_ms = idle_ms
            else:
                next_delay_ms = base_ms
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
                self._ui_render_in_progress = False
            except Exception:
                pass
            try:
                monitoring_fin = bool(self.is_monitoring) or bool(
                    getattr(self.trading_controller, "is_monitoring", False)
                )
                r_fin = getattr(self, "root", None)
                if monitoring_fin and r_fin is not None:
                    try:
                        if r_fin.winfo_exists():
                            delay_fin = int(
                                locals().get("next_delay_ms", REALTIME_TREE_UI_REFRESH_MS)
                            )
                            delay_fin = max(20, min(delay_fin, 2000))
                            r_fin.after(delay_fin, self._render_tree_updates)
                            self._update_timer_active = True
                    except Exception:
                        pass
            except Exception:
                pass
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
            inject_iv = 2.5
            try:
                st_iv = getattr(self, "settings", None) or {}
                inject_iv = float(st_iv.get("ui_inject_min_interval_sec", 2.5))
            except (TypeError, ValueError):
                inject_iv = 2.5
            inject_iv = max(0.8, min(inject_iv, 30.0))
            now_gate = time.time()
            # STARTED 직후 수 초는 강제 inject 생략(틱·REG와 겹쳐 UI 멈춤 완화)
            try:
                grace_until = float(
                    getattr(self, "_monitor_start_ui_grace_until", 0.0) or 0.0
                )
                if grace_until > 0.0 and now_gate < grace_until:
                    return
            except Exception:
                pass
            if now_gate - float(getattr(self, "_inject_min_last_ts", 0.0) or 0.0) < inject_iv:
                return
            self._inject_min_last_ts = now_gate

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
            # 미선정(virtual_positions 등) 종목은 SignalEngine pending enqueue가 누락될 수 있어
            # 최소 UI 갱신용 강제 enqueue 대상에 포함합니다.
            try:
                vpd = getattr(self.portfolio, "virtual_positions", None) or {}
                for c_raw in vpd.keys():
                    try:
                        nk = str(c_raw).split("_")[0].zfill(6)
                    except Exception:
                        continue
                    if nk and nk not in mc:
                        mc.append(nk)
            except Exception:
                pass
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
                try:
                    ratio_disp = str(info.get("ratio", "0"))
                    se_inj = getattr(self, "signal_engine", None)
                    if se_inj is not None and hasattr(
                        se_inj, "ratio_cell_display_for_monitor_tree"
                    ):
                        ratio_disp = se_inj.ratio_cell_display_for_monitor_tree(
                            info, float(now_p)
                        )
                except Exception:
                    ratio_disp = str(info.get("ratio", "0"))
                status_text = monitor_signal_status_display_text(info)
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

    def request_virtual_tab_sync_light_refresh(self) -> None:
        """
        계좌 동기화·늦은 체결 확인 직후: 전체 ``_render_virtual_positions`` 대신
        보유 행 정리·요약·주문 로그만 갱신(금일손익 REST·히스토리 전량 스캔 생략).
        """
        try:
            if not hasattr(self, "virtual_tab"):
                return
            self._vt_light_refresh_dirty = True

            def _schedule() -> None:
                try:
                    r = getattr(self, "root", None)
                    if r is None:
                        return
                    try:
                        if self._vt_light_refresh_after_id is not None:
                            r.after_cancel(self._vt_light_refresh_after_id)
                    except Exception:
                        pass
                    try:
                        delay = 450
                        st = getattr(self, "settings", None) or {}
                        delay = int(
                            float(st.get("virtual_tab_light_refresh_debounce_ms", 450))
                        )
                        delay = max(150, min(delay, 3000))
                    except Exception:
                        delay = 450
                    self._vt_light_refresh_after_id = r.after(
                        delay, self._flush_virtual_tab_sync_light_refresh
                    )
                except Exception:
                    pass

            self.run_on_ui_thread(_schedule, 0)
        except Exception:
            pass

    def _flush_virtual_tab_sync_light_refresh(self) -> None:
        """메인 스레드: 동기화 직후 경량 포지션 탭 갱신."""
        try:
            self._vt_light_refresh_after_id = None
            if not self._vt_light_refresh_dirty:
                return
            self._vt_light_refresh_dirty = False
            if not hasattr(self, "virtual_tab"):
                return
            try:
                self.trading_controller._ensure_virtual_positions_monitor_info()
            except Exception:
                pass
            try:
                self.monitor_info = dict(self.trading_controller.monitor_info or {})
            except Exception:
                pass
            vt = self.virtual_tab
            try:
                merged = vt._vp_items_by_normalized()
                tr = getattr(vt, "tree_active", None)
                if tr is not None:
                    for nk in list(tr.get_children()):
                        if nk not in merged:
                            try:
                                vt._remove_active_tree_row(nk)
                            except Exception:
                                pass
            except Exception:
                pass
            try:
                vt.refresh_portfolio_light()
            except Exception:
                pass
            try:
                sig = self._vp_structure_signature()
                setattr(self, "_last_vp_render_sig", sig)
            except Exception:
                pass
            try:
                self.request_virtual_tab_orders_refresh()
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
        except Exception as e:
            try:
                self.log(f"⚠️ [UI] 경량 포지션 갱신 실패: {e}")
            except Exception:
                pass

    def request_virtual_tab_orders_refresh(self) -> None:
        """주문·체결 트리만 trailing debounce 로 갱신(체결·동기화 연속 호출 완화)."""
        try:
            if not hasattr(self, "virtual_tab"):
                return
            r = getattr(self, "root", None)
            if r is None:
                return
            try:
                delay = 600
                st = getattr(self, "settings", None) or {}
                delay = int(float(st.get("virtual_tab_orders_refresh_debounce_ms", 600)))
                delay = max(250, min(delay, 5000))
            except Exception:
                delay = 600
            try:
                jid = getattr(self, "_vt_orders_refresh_after_id", None)
                if jid is not None:
                    r.after_cancel(jid)
            except Exception:
                pass

            def _flush() -> None:
                try:
                    self._vt_orders_refresh_after_id = None
                    self.refresh_virtual_tab_orders_only()
                except Exception:
                    pass

            self._vt_orders_refresh_after_id = r.after(delay, _flush)
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
            self.run_on_ui_thread(self._refresh_vp_price_stubs_after_auth_safe, 400)
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
        REST는 **백그라운드**에서만 호출하고, UI 갱신은 메인 스레드로 위임합니다.
        """
        try:
            import threading

            threading.Thread(
                target=self._refresh_vp_price_stubs_worker,
                daemon=True,
            ).start()
        except Exception:
            try:
                self._refresh_vp_price_stubs_worker()
            except Exception:
                pass

    def _refresh_vp_price_stubs_worker(self) -> None:
        """vp_price_stub 참고가 REST 보강(비메인 스레드)."""
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
        patches: Dict[str, float] = {}
        for c_raw, pos in list(vp.items()):
            try:
                if not isinstance(pos, dict):
                    continue
                nk = str(c_raw).split("_")[0].zfill(6)
                mi = getattr(self, "monitor_info", None)
                if not isinstance(mi, dict):
                    return
                row = mi.get(nk)
                if not isinstance(row, dict) or not is_vp_price_stub_active(row):
                    continue
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
                        allow_rest=True,
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
                patches[nk] = ref_p
            except Exception:
                continue
        if not patches:
            return

        def _apply_on_main() -> None:
            try:
                mi = getattr(self, "monitor_info", None)
                if not isinstance(mi, dict):
                    return
                for nk, ref_p in patches.items():
                    row = mi.get(nk)
                    if not isinstance(row, dict):
                        continue
                    row["last_price"] = float(ref_p)
                    # 컨트롤러와 같은 행 객체를 공유해야 monitor_info 재할당 시
                    # 웹소켓 전일대비(diff/ratio)가 옛 사본으로 되돌아가지 않는다.
                    try:
                        self.trading_controller.monitor_info[nk] = row
                    except Exception:
                        pass
                if hasattr(self, "virtual_tab"):
                    try:
                        self.virtual_tab.refresh_portfolio_light()
                    except Exception:
                        pass
            except Exception:
                pass

        try:
            self.run_on_ui_thread(_apply_on_main, 0)
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
            # 워커/메인 모두: 스텁 생성 시 동기 REST 금지(체결 직후 UI 멈춤 방지)
            ref_p = float(ap) if ap > 0 else 0.0
            stub: Dict[str, Any] = {
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
            }
            stamp_vp_price_stub(stub, settings=getattr(self, "settings", None))
            self.monitor_info[nk] = stub
            try:
                self.trading_controller.monitor_info[nk] = stub
            except Exception:
                pass
            if hasattr(self, "monitor_tab"):
                self.monitor_tab._pending_reg_refresh = True
        except Exception:
            pass

    def toggle_monitoring(self):
        """
        실시간 감시 토글.

        시작 시 일일 감시목록이 만료·없으면 선정을 먼저 수행한 뒤 감시를 시작합니다.
        ContinuousMonitorService 가 연동되어 장마감 후 중단→선정,
        다음날 개장 N분 전 자동 재시작을 수행합니다. 수동 중단 시에도
        다음날 장전 스케줄은 유지됩니다.
        """
        tc = self.trading_controller
        try:
            if bool(getattr(tc, "is_monitoring", False)):
                try:
                    cms = getattr(self, "continuous_monitor_service", None)
                    if cms is not None:
                        # 수동 중단은 당일만 멈추고, 다음날 장전 자동 시작은 유지
                        cms.mark_morning_start_done()
                except Exception:
                    pass
                return tc.stop()
        except Exception:
            pass
        return self.ensure_fresh_selection_then_start()

    def ensure_fresh_selection_then_start(
        self,
        force_selection: bool = False,
        on_done: Optional[Any] = None,
    ) -> bool:
        """선정 신선도 확인 후 감시 시작 (SelectionMonitorStartService 위임)."""
        try:
            return bool(
                self.selection_monitor_start_service.ensure_fresh_selection_then_start(
                    force_selection=force_selection,
                    on_done=on_done,
                )
            )
        except Exception as e:
            try:
                self.log(f"❌ [감시] ensure_fresh_selection_then_start 실패: {e}")
            except Exception:
                pass
            if callable(on_done):
                try:
                    on_done(False)
                except Exception:
                    pass
            return False


    def _sync_continuous_monitor_ui(self) -> None:
        """레거시 no-op(연속 감시 전용 버튼 제거)."""
        return

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
            try:
                nxt_svc = getattr(self, "nxt_eligibility_service", None)
                if nxt_svc is not None:
                    nxt_svc.refresh_codes(
                        self.monitor_codes, force=False, background=True
                    )
            except Exception:
                pass
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
            # 금일손익: 장전→시초前 스냅 / 장중·장마감後→직전 EOD·종가 폴백
            try:
                from utils.session_open_day_pnl import on_monitoring_started

                on_monitoring_started(self)
            except Exception:
                pass
            # 실전: 감시·보유 종목 NXT 거래가능 여부 아침/기동 갱신
            try:
                nxt_svc = getattr(self, "nxt_eligibility_service", None)
                if nxt_svc is not None:
                    nxt_svc.refresh_morning(force=True)
            except Exception:
                pass
            try:
                cms = getattr(self, "continuous_monitor_service", None)
                if cms is not None:
                    # 실시간 감시 = 연속(EOD) 전환 연동
                    if not cms.armed:
                        cms.arm()
                    else:
                        cms.start_scheduler()
                    try:
                        cms.mark_morning_start_done()
                    except Exception:
                        pass
            except Exception:
                pass
            if self.btn_monitor_toggle: self.btn_monitor_toggle.config(text="감시 중단", style="Warning.TButton")
            try:
                from core.monitor_started_display import format_monitor_started_status_text

                _started_txt = format_monitor_started_status_text(data)
            except Exception:
                _started_txt = f"상태: 감시 중 ({data}종목)"
            if self.lbl_monitor_status:
                self.lbl_monitor_status.config(
                    text=_started_txt, foreground=self.colors["success"]
                )
            if self.status_label:
                self.status_label.config(
                    text=_started_txt,
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
            cms = getattr(self, "continuous_monitor_service", None)
            eod_cycle_stop = False
            try:
                if cms is not None and cms.consume_eod_stop_flag():
                    eod_cycle_stop = True
            except Exception:
                pass
            self.is_monitoring = False
            self._active_stream_codes = []
            self._maybe_stop_account_sync_timer()
            # 다음 감시 시작 시 다시 1회 발송되도록 리셋
            self._telegram_sent_on_monitor_start = False
            # 감시 중단 직후 최대절전 억제 재평가(EOD 후 즉시 해제)
            try:
                self._apply_sleep_inhibit_from_settings()
            except Exception:
                pass
            if self.btn_monitor_toggle:
                self.btn_monitor_toggle.config(
                    text="감시 시작", style="Accent.TButton"
                )
            if eod_cycle_stop and cms is not None and cms.armed:
                try:
                    hm = cms._auto_start_hhmm_str()
                except Exception:
                    hm = "08:35"
                _wait_txt = f"상태: 장마감 선정 중 → 감시 시작 {hm}"
                if self.lbl_monitor_status:
                    self.lbl_monitor_status.config(
                        text=_wait_txt, foreground=self.colors.get("primary", "#88C0D0")
                    )
                if self.status_label:
                    self.status_label.config(
                        text=_wait_txt, foreground=self.colors.get("primary", "#88C0D0")
                    )
            else:
                try:
                    if cms is not None:
                        cms.mark_morning_start_done()
                        if not cms.armed:
                            cms.arm()
                except Exception:
                    pass
                wait_txt = "상태: 대기 중"
                try:
                    if cms is not None:
                        hm = cms._auto_start_hhmm_str()
                        wait_txt = f"상태: 대기 중 (감시 시작 {hm})"
                except Exception:
                    pass
                if self.lbl_monitor_status:
                    self.lbl_monitor_status.config(
                        text=wait_txt, foreground=self.colors["primary"]
                    )
                if self.status_label:
                    self.status_label.config(
                        text=wait_txt, foreground=self.colors["primary"]
                    )

    def try_resume_monitoring_after_restart(self) -> None:
        """재시작(감시 ON) 직후 — 인증 완료 뒤 실시간 감시를 복원한다."""
        if not bool(getattr(self, "_restart_resume_monitoring", False)):
            return
        if bool(getattr(self, "_restart_resume_started", False)):
            return
        tc = getattr(self, "trading_controller", None)
        if tc is not None and bool(getattr(tc, "is_monitoring", False)):
            self._restart_resume_monitoring = False
            return
        self._restart_resume_started = True
        self._restart_resume_monitoring = False
        try:
            cms = getattr(self, "continuous_monitor_service", None)
            if cms is not None:
                trade_day = cms._resolve_eod_trading_day(datetime.now())
                if trade_day is not None:
                    cms.mark_eod_cycle_consumed(trade_day)
                    self.log(
                        f"ℹ️ [재시작] {trade_day.strftime('%Y-%m-%d')} "
                        "장마감 선정 재실행 방지 — 감시만 복원합니다."
                    )
        except Exception:
            pass
        try:
            self.log("🔄 [재시작] 인증 완료 — 실시간 감시 복원을 시작합니다.")
        except Exception:
            pass
        try:
            self.ensure_fresh_selection_then_start()
        except Exception as ex:
            try:
                self.log(f"⚠️ [재시작] 감시 복원 실패: {ex}")
            except Exception:
                pass

    def restart_gui(self, *, confirm: bool = True) -> None:
        """GUI를 재시작한다. 감시 중이었으면 감시 ON, 아니면 OFF 상태로 기동."""
        from tkinter import messagebox

        from utils.gui_restart import is_monitoring_active, spawn_gui_restart

        monitoring = is_monitoring_active(self)
        if confirm:
            state_txt = "감시 ON" if monitoring else "감시 OFF"
            if not messagebox.askyesno(
                "프로그램 재시작",
                f"K-Trader를 다시 시작합니다.\n\n"
                f"현재 상태: {state_txt}\n"
                f"재시작 후에도 {state_txt} 상태로 복원됩니다.\n\n"
                "계속하시겠습니까?",
                parent=self.root,
            ):
                return
        if not spawn_gui_restart(resume_monitoring=monitoring):
            try:
                messagebox.showerror(
                    "재시작 실패",
                    "run_k_trader.bat 을 찾을 수 없습니다.",
                    parent=self.root,
                )
            except Exception:
                pass
            return
        try:
            self.log(
                "🔄 [재시작] 프로그램을 다시 시작합니다... "
                f"(감시: {'ON' if monitoring else 'OFF'})"
            )
        except Exception:
            pass
        try:
            self.root.after(500, self.on_close)
        except Exception:
            pass

    def _start_sleep_inhibit(self) -> None:
        """GUI 기동 시 Windows 유휴 최대절전 억제를 시작한다."""
        try:
            from utils.sleep_inhibit import SleepInhibitor

            if getattr(self, "_sleep_inhibitor", None) is None:
                self._sleep_inhibitor = SleepInhibitor(logger=self.log)
            self._apply_sleep_inhibit_from_settings()
            self._schedule_sleep_inhibit_refresh()
        except Exception as e:
            try:
                self.log(f"⚠️ [최대절전방지] 초기화 실패: {e}")
            except Exception:
                pass

    def _apply_sleep_inhibit_from_settings(self) -> None:
        """settings.json 의 최대절전 방지 토글을 즉시 반영한다."""
        try:
            si = getattr(self, "_sleep_inhibitor", None)
            if si is None:
                return
            from utils.sleep_inhibit_policy import sleep_inhibit_decision

            st = getattr(self, "settings", None) or {}
            keep_display = bool(st.get("prevent_sleep_keep_display", False))
            want, reason = sleep_inhibit_decision(self)
            if want:
                si.acquire(keep_display=keep_display)
            else:
                # want=False 여도 Clear 실패 핸들이 있으면 매 tick 재시도
                si.release()
            # 상태 전이 시에만 사유 로그 (50초 주기 스팸 방지)
            now_active = bool(getattr(si, "active", False))
            handle_stuck = getattr(si, "_power_handle", None) is not None
            log_key = (reason, now_active, handle_stuck)
            if getattr(self, "_sleep_inhibit_last_key", None) != log_key:
                self._sleep_inhibit_last_key = log_key
                self._sleep_inhibit_last_reason = reason
                try:
                    if now_active:
                        self.log(
                            f"🔆 [최대절전방지] 억제 유지 (사유: {reason})"
                        )
                    elif handle_stuck:
                        self.log(
                            f"⚠️ [최대절전방지] 해제 재시도 중 (사유: {reason}, "
                            "PowerRequest 잔존)"
                        )
                    else:
                        self.log(
                            f"🌙 [최대절전방지] 억제 해제 상태 (사유: {reason})"
                        )
                except Exception:
                    pass
        except Exception as e:
            try:
                self.log(f"⚠️ [최대절전방지] 설정 반영 실패: {e}")
            except Exception:
                pass

    def _schedule_sleep_inhibit_refresh(self) -> None:
        """약 50초마다 최대절전 억제를 갱신한다 (일부 전원 정책 대비)."""
        try:
            after_id = getattr(self, "_sleep_inhibit_after_id", None)
            if after_id is not None:
                self.root.after_cancel(after_id)
        except Exception:
            pass
        self._sleep_inhibit_after_id = None

        def _tick() -> None:
            self._sleep_inhibit_after_id = None
            try:
                self._apply_sleep_inhibit_from_settings()
            except Exception:
                pass
            try:
                self._sleep_inhibit_after_id = self.root.after(50_000, _tick)
            except Exception:
                pass

        try:
            self._sleep_inhibit_after_id = self.root.after(50_000, _tick)
        except Exception:
            pass

    def _stop_sleep_inhibit(self) -> None:
        """종료 시 최대절전 억제와 갱신 타이머를 해제한다."""
        try:
            after_id = getattr(self, "_sleep_inhibit_after_id", None)
            if after_id is not None:
                self.root.after_cancel(after_id)
        except Exception:
            pass
        self._sleep_inhibit_after_id = None
        try:
            si = getattr(self, "_sleep_inhibitor", None)
            if si is not None:
                si.release()
        except Exception:
            pass

    def on_close(self):
        self.log("⏹️ [System] 프로그램을 종료합니다.")
        try:
            self._stop_sleep_inhibit()
        except Exception:
            pass
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
            cms = getattr(self, "continuous_monitor_service", None)
            if cms is not None:
                cms.shutdown()
        except Exception:
            pass
        try:
            rds = getattr(self, "regime_display_service", None)
            if rds is not None:
                rds.shutdown()
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
        try:
            self._shutdown_order_action_workers()
        except Exception:
            pass
        try:
            poller = getattr(self, "rt_watchlist_m1_api_poller", None)
            if poller is not None:
                poller.stop()
        except Exception:
            pass
        try:
            svc = getattr(self, "next_open_pending_action_service", None)
            if svc is not None:
                svc.stop_scheduler()
                svc.save()
        except Exception:
            pass
        try:
            acc = getattr(self, "rt_tick_m1_accumulator", None)
            if acc is not None and hasattr(acc, "shutdown"):
                acc.shutdown()
        except Exception:
            pass
        if hasattr(self, 'trading_controller'): self.trading_controller.stop()
        try:
            self.root.destroy()
        except Exception:
            pass
        # 백그라운드 스레드가 남아 부모 cmd 창이 안 닫히는 경우 방지
        try:
            os._exit(0)
        except Exception:
            try:
                import sys as _sys

                _sys.exit(0)
            except Exception:
                pass

    def _on_regime_history_clicked(self) -> None:
        """헤더 「30일」 — 적용일(다음 장) 기준 레짐 이력 창."""
        try:
            svc = getattr(self, "regime_display_service", None)
            if svc is not None:
                svc.open_history_async()
        except Exception as e:
            try:
                self.log(f"⚠️ [레짐표시] 이력 버튼 오류: {e}")
            except Exception:
                pass

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

    def _apply_official_closed_15m_bar(
        self,
        code: str,
        bar_ts: Any,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> bool:
        """
        확정된 15분 봉 한 줄을 REST(ka10080) OHLCV로 덮어씁니다.
        Tk 메인 스레드에서만 호출합니다.
        """
        try:
            want = pd.Timestamp(bar_ts).floor("s")
            row = (want, float(open_), float(high), float(low), float(close), float(volume))
        except Exception:
            return False
        return self._apply_official_closed_15m_bars(code, [row], primary_ts=bar_ts)

    def _apply_official_closed_15m_bars(
        self,
        code: str,
        bars: Any,
        *,
        primary_ts: Any = None,
        log_ohlcv_diff: bool = True,
        log_extra_heal: Optional[bool] = None,
    ) -> bool:
        """
        공식 15분봉 여러 줄을 덮어씁니다. 형성 중 봉은 호출 측에서 제외해야 합니다.

        ``primary_ts`` 행이 bars_data 에 있으면 True(평가 가능).
        값이 다른 이전 슬롯도 같이 맞춰 MA60 이 어긋나지 않게 합니다.

        메모리에 없는 API 과거 봉은 DB에 넣지 않습니다(1페이지 수십~수백 행 ×
        종목당 purge 가 반영을 수 초씩 늘리던 원인).

        틱 합성봉과 OHLCV 차이가 있어 실제 덮어쓴 경우 ``log_ohlcv_diff`` 로 로그합니다.
        ``log_extra_heal`` 은 구 인자(호환) — 지정 시 ``log_ohlcv_diff`` 대신 사용.
        """
        if log_extra_heal is not None:
            log_ohlcv_diff = bool(log_extra_heal)
        from utils.bars_data_keys import resolve_bars_data_frame
        from core.rt_closed_15m_official_replace import ts_to_slot_hhmm

        def _ohlcv_differs(
            row: Any,
            o: float,
            h: float,
            l_: float,
            c: float,
            v: float,
        ) -> bool:
            try:
                if abs(float(row["Open"]) - float(o)) >= 0.5:
                    return True
                if abs(float(row["High"]) - float(h)) >= 0.5:
                    return True
                if abs(float(row["Low"]) - float(l_)) >= 0.5:
                    return True
                if abs(float(row["Close"]) - float(c)) >= 0.5:
                    return True
                if abs(float(row["Volume"]) - float(v)) >= 0.5:
                    return True
            except Exception:
                return True
            return False

        try:
            cc = canonical_stock_code(str(code).strip())
            if not cc:
                return False
        except Exception:
            return False
        try:
            primary = (
                pd.Timestamp(primary_ts).floor("s")
                if primary_ts is not None
                else None
            )
        except Exception:
            primary = None

        parsed: List[Tuple[Any, float, float, float, float, float]] = []
        try:
            for item in list(bars or []):
                try:
                    ts = pd.Timestamp(item[0]).floor("s")
                    o = float(item[1])
                    h = float(item[2])
                    l_ = float(item[3])
                    c = float(item[4])
                    v = float(item[5])
                    if o > 0 or h > 0 or l_ > 0 or c > 0:
                        parsed.append((ts, o, h, l_, c, v))
                except Exception:
                    continue
        except Exception:
            parsed = []
        if not parsed:
            return False

        lk = getattr(self, "_bars_data_lock", None)
        bd = getattr(self, "bars_data", None)
        if lk is None or not isinstance(bd, dict):
            # 메모리 프레임이 없으면 primary 만 DB 보강(전체 페이지 폭주 방지)
            try:
                store = getattr(self, "realtime_monitor_bars_store", None)
                if store is not None:
                    only = parsed
                    if primary is not None:
                        only = [r for r in parsed if r[0] == primary] or parsed[-1:]
                    if hasattr(store, "upsert_completed_bars_batch"):
                        store.upsert_completed_bars_batch(cc, only)
                    else:
                        for ts, o, h, l_, c, v in only:
                            store.upsert_completed_bar(cc, ts, o, h, l_, c, v)
            except Exception:
                pass
            return False

        recalc_key: Optional[str] = None
        primary_applied = False
        primary_changed = False
        extra_changed = 0
        upserts: List[Tuple[Any, float, float, float, float, float]] = []
        try:
            with lk:
                nk, df = resolve_bars_data_frame(bd, cc, migrate_alias=True)
                use_key = nk or cc
                if not use_key:
                    return False
                if df is None or getattr(df, "empty", True):
                    try:
                        store = getattr(self, "realtime_monitor_bars_store", None)
                        if store is not None:
                            only = parsed
                            if primary is not None:
                                only = [
                                    r for r in parsed if r[0] == primary
                                ] or parsed[-1:]
                            if hasattr(store, "upsert_completed_bars_batch"):
                                store.upsert_completed_bars_batch(use_key, only)
                            else:
                                for ts, o, h, l_, c, v in only:
                                    store.upsert_completed_bar(
                                        use_key, ts, o, h, l_, c, v
                                    )
                    except Exception:
                        pass
                    return False

                idx_map: Dict[Any, Any] = {}
                try:
                    for ix in df.index:
                        try:
                            idx_map[pd.Timestamp(ix).floor("s")] = ix
                        except Exception:
                            continue
                except Exception:
                    idx_map = {}

                any_write = False
                for ts, o, h, l_, c, v in parsed:
                    hit_idx = idx_map.get(ts)
                    if hit_idx is None:
                        # 메모리에 없는 과거 API 봉 → 정렬/MA 대상 아님(DB 폭주 방지)
                        continue
                    is_primary = primary is not None and ts == primary
                    try:
                        row = df.loc[hit_idx]
                        differs = _ohlcv_differs(row, o, h, l_, c, v)
                    except Exception:
                        differs = True
                    if not differs and not is_primary:
                        continue
                    if not differs and is_primary:
                        primary_applied = True
                        continue
                    try:
                        df.at[hit_idx, "Open"] = float(o)
                        df.at[hit_idx, "High"] = float(h)
                        df.at[hit_idx, "Low"] = float(l_)
                        df.at[hit_idx, "Close"] = float(c)
                        df.at[hit_idx, "Volume"] = float(v)
                        if "time" in df.columns:
                            df.at[hit_idx, "time"] = ts_to_slot_hhmm(hit_idx)
                    except Exception as ex:
                        try:
                            self.log(
                                f"⚠️ [RT15m-Official] OHLCV 덮어쓰기 실패 ({use_key}): {ex!r}"
                            )
                        except Exception:
                            pass
                        continue
                    any_write = True
                    upserts.append((ts, o, h, l_, c, v))
                    if is_primary:
                        primary_applied = True
                        primary_changed = True
                    elif primary is None or ts != primary:
                        extra_changed += 1

                if not any_write and not primary_applied:
                    return False
                if any_write:
                    try:
                        df["ma5"] = df["Close"].rolling(window=5).mean()
                        df["ma20"] = df["Close"].rolling(window=20).mean()
                        df["ma60"] = df["Close"].rolling(window=60).mean()
                    except Exception:
                        pass
                    bd[use_key] = df
                    recalc_key = use_key
                elif primary_applied:
                    return True
            if recalc_key:
                def _do_recalc() -> None:
                    try:
                        self.strategy_engine.recalculate_indicators(
                            recalc_key, bd, self.settings
                        )
                    except Exception:
                        pass

                # 전체 지표 재계산은 교체를 막지 않음(1워커 후순위).
                # 예전엔 워커에서 동기 계산 + 전역 락이라 26종이 종목당 ~1분씩 걸렸다.
                try:
                    from core.closed_15m_fast_path import (
                        enqueue_official_indicator_recalc,
                    )

                    enqueue_official_indicator_recalc(self, recalc_key)
                except Exception:
                    try:
                        on_main = (
                            threading.current_thread() is threading.main_thread()
                        )
                    except Exception:
                        on_main = False
                    if on_main:
                        threading.Thread(
                            target=_do_recalc,
                            name="rt15m-ind-recalc",
                            daemon=True,
                        ).start()
                    else:
                        _do_recalc()
            if recalc_key:
                try:
                    store = getattr(self, "realtime_monitor_bars_store", None)
                    if store is not None and upserts:
                        if hasattr(store, "upsert_completed_bars_batch"):
                            store.upsert_completed_bars_batch(recalc_key, upserts)
                        else:
                            for ts, o, h, l_, c, v in upserts:
                                store.upsert_completed_bar(
                                    recalc_key, ts, o, h, l_, c, v
                                )
                except Exception:
                    pass
                if any_write and log_ohlcv_diff and upserts:
                    try:
                        parts: List[str] = []
                        for ts, _o, _h, _l, _c, _v in upserts:
                            try:
                                parts.append(pd.Timestamp(ts).strftime("%H:%M"))
                            except Exception:
                                continue
                        slot_txt = ",".join(parts) if parts else "?"
                        kind = []
                        if primary_changed:
                            kind.append("당슬롯")
                        if extra_changed > 0:
                            kind.append(f"이전{extra_changed}")
                        kind_txt = "+".join(kind) if kind else "OHLCV"
                        self.log(
                            f"ℹ️ [RT15m-Official] {cc} 공식15 정렬 차이 반영 "
                            f"{len(upserts)}봉 ({slot_txt}, {kind_txt})"
                        )
                    except Exception:
                        pass
                return bool(primary_applied or any_write)
            return bool(primary_applied)
        except Exception as e:
            try:
                self.log(f"⚠️ [RT15m-Official] _apply_official_closed_15m_bars 예외: {e!r}")
            except Exception:
                pass
            return False

    def send_telegram_message(self, message: str, parse_mode: Optional[str] = "HTML") -> None:
        """텔레그램 전송. 실패 시에만 구체 사유를 남기고, 미설정 오탐 문구는 쓰지 않습니다."""
        try:
            token = ""
            cid = ""
            try:
                import config as _cfg

                token = str(getattr(_cfg, "TELEGRAM_BOT_TOKEN", "") or "").strip()
                cid = str(getattr(_cfg, "TELEGRAM_CHAT_ID", "") or "").strip()
            except Exception:
                pass
            ok = self.data_manager.send_telegram_message(message, parse_mode=parse_mode)
            if ok:
                return
            if not token or not cid or "발급" in token:
                self.log(
                    "⚠️ [Telegram] 미설정 — config.py 의 "
                    "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 를 확인하세요."
                )
            # 토큰이 있는데 실패한 경우: telegram_sender 가 이미 상세를 print —
            # GUI 에는 일반 실패만 짧게 (오탐 '미설정' 제거)
            else:
                self.log("⚠️ [Telegram] 전송 실패(상세는 콘솔/로그 참고)")
        except Exception as e:
            self.log(f"⚠️ [Telegram] 예외: {e}")

    def _build_account_summary_telegram_message(self, header: str) -> Optional[str]:
        """포지션관리 상단 계좌 요약 → 텔레그램 본문 (서비스 위임)."""
        try:
            return self.account_summary_telegram_service.build_message(header)
        except Exception:
            return None

    def _start_account_summary_telegram_worker(self, *, header: str, seq: int) -> None:
        """force 계좌 동기화 후 계좌요약 텔레그램 (서비스 위임)."""
        try:
            self.account_summary_telegram_service.start_worker(header=header, seq=seq)
        except Exception:
            pass

    def schedule_account_summary_telegram_after_trade(self, *, delay_sec: int = 12) -> None:
        """체결 후 지연 계좌요약 텔레그램 (서비스 위임)."""
        try:
            self.account_summary_telegram_service.schedule_after_trade(
                delay_sec=delay_sec
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
        전체 리빌드는 무거우므로 한 틱 지연 후 수행합니다.
        """
        if not hasattr(self, "virtual_tab"):
            return
        # 백그라운드에서 직접 들어오면 메인으로 위임 (Tk 크래시 방지)
        try:
            if not self._is_ui_main_thread():
                self.run_on_ui_thread(self._render_virtual_positions, 0)
                return
        except Exception:
            pass
        try:
            sig = self._vp_structure_signature()
            prev = getattr(self, "_last_vp_render_sig", None)
            if prev is not None and sig == prev:
                self.virtual_tab.refresh_portfolio_light()
                # 보유 구조가 같아도 주문·체결(접수→FILLED)은 바뀔 수 있음 — 주문 트리 갱신 누락 방지
                try:
                    self.request_virtual_tab_orders_refresh()
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
                return
        except Exception:
            pass

        def _heavy_vp_render() -> None:
            try:
                try:
                    self.trading_controller._ensure_virtual_positions_monitor_info()
                except Exception:
                    pass
                try:
                    self.monitor_info = dict(self.trading_controller.monitor_info or {})
                except Exception:
                    pass
                # 보유 트리·요약만 먼저 — 주문 트리는 debounce 경로로 분리
                self.virtual_tab._render_virtual_positions(render_orders=False)
                self._last_vp_render_sig = self._vp_structure_signature()
                try:
                    setattr(self.app, "_vt_day_pnl_tick_cache", None)
                except Exception:
                    pass
            except Exception:
                pass
            try:
                self.request_virtual_tab_orders_refresh()
            except Exception:
                pass
            try:
                self._schedule_vp_stub_price_refresh_after_auth()
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
            r = getattr(self, "root", None)
            if r is not None:
                r.after(1, _heavy_vp_render)
            else:
                _heavy_vp_render()
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
            except Exception:
                pass

        try:
            r = getattr(self, "root", None)
            if r is not None:
                r.after(1, _heavy_full)
            else:
                _heavy_full()
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
