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
from core.signal_engine import SignalEngine
from gui.gui_utils import GuiUtils
from gui.styles import NordicStyle

from kiwoom_rest_api.auth.token import TokenManager

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

# 기본 전략 설정
DEFAULT_SETTINGS = {
    "min_trde_val": 50.0,
    "sel_rsi_min": 30.0,
    "sel_rsi_max": 70.0,
    "buy_rsi_min": 30.0,
    "buy_rsi_max_b": 70.0,
    "buy_rsi_max_a": 65.0,
    "use_type_a": True,
    "use_type_b": False,
    "vol_surge": 1.2,
    "use_vol_surge": True,
    "vol_surge_b": 2.0,
    "use_type_b_vol": False,
    "use_macd_gc_b": False,
    "use_trend_aligned_b": False,
    "total_capital": 10000000.0,
    "max_slots": 20.0,
    "vol_ratio": 1.5,
    "vol_lookback": 10.0,
    "ma_convergence": 0.05,
    "use_sel_track_a": True,  # [v30.9] 안정형 선정 트랙 (수렴 중심)
    "use_sel_track_b": True,  # [v30.9] 공격형 선정 트랙 (추세 중심)
    "use_gc": True,
    "gc_window": 75.0,
    "use_rsi_signal": True,
    "rsi_window": 45.0,
    "use_vol_power": True,
    "vol_power_lvl": 120.0,
    "use_ma60_safe": True,
    "ma60_min": 100.0,
    "ma60_max": 110.0,
    "use_market_filter": True,
    "use_macd_signal": True,
    "macd_fast": 12.0,
    "macd_slow": 26.0,
    "macd_sig": 9.0,
    "sig_macd_fast": 12.0,
    "sig_macd_slow": 26.0,
    "sig_macd_sig": 9.0,
    "rsi_sig_period": 9.0,
    "use_auto_virtual": True,
    "flash_exit_pct": 3.0,
    "gap_down_limit": -5.0,
    "stage1_amt": 1000000.0,
    "stage2_amt": 2000000.0,
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
    "s1_a_flash_exit_pct": 1.5,
    "s1_b_flash_exit_pct": 1.5,
    "s1_a_stop_loss": 3.0,
    "s1_b_stop_loss": 2.0,
    "s1_a_take_profit": 5.0,
    "s1_b_take_profit": 4.0,
    "use_s1_flash_exit": True,
    "use_s2_flash_exit": True,
    "s2_flash_exit_pct": 3.0,
    "use_s1_stop_loss": True,
    "use_s2_stop_loss": True,
    "use_stop_loss": True,
    "stop_loss": 3.0,
    "s2_stop_loss": 2.0,
    "stage2_trigger": 1.0,
    "take_profit": 5.0,
    "s2_take_profit": 6.0,
    "use_s1_tp": True,
    "use_rsi_min": True,
    "use_s2_tp": True,
    "use_rsi_sig_filter": True,
    "use_gap_grace": True
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
        self.root.title("Antigravity 스윙매매 전략 플랫폼 v25.7 (Kiwoom REST)")
        self.root.geometry("1400x900")
        
        self.auth_manager = AuthManager(logger_callback=self.log)
        self.config_handler = ConfigHandler(logger_callback=self.log)
        self.data_manager = DataManager()
        
        self.settings = self.config_handler.load_settings(DEFAULT_SETTINGS)
        
        self.stream_processor = StreamProcessor(self.settings, logger_callback=self.log)
        self.gui_utils = GuiUtils(root=self.root)
        self.signal_engine = SignalEngine(self)
        self.nordic_style = NordicStyle(self.root)
        
        self._init_attributes()
        self.bars_data = self.config_handler.load_bars_cache() 
        self._setup_ui()
        
    def _init_attributes(self):
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
        self.active_blinks = {}

        self.notebook = None
        self.tab_dashboard = None
        self.tab_settings = None
        self.tab_monitor = None
        self.status_label = None
        self.update_kospi_var = None
        self.update_theme_var = None
        self.btn_auth = None
        self.virtual_positions = {} 
        self.virtual_history = []    
        self.tree_virtual = None     
        self.tab_backtest = None
        self.tree_backtest = None
        self.backtest_entries = {}
        self.backtest_force_refresh_var = tk.BooleanVar(value=False)
        self.backtest_high_precision_var = tk.BooleanVar(value=False)
        self.base_backtest_settings = DEFAULT_SETTINGS.copy()
        
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
        self.colors = self.nordic_style.colors

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        self.tab_dashboard = ttk.Frame(self.notebook)
        self.tab_settings = ttk.Frame(self.notebook)
        self.tab_monitor = ttk.Frame(self.notebook)  
        self.tab_virtual = ttk.Frame(self.notebook) 
        self.tab_backtest = ttk.Frame(self.notebook)
        
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
        
        self.lbl_market_indices = ttk.Label(status_frame, text="📊 시장 지수: 로딩 중...", font=("Helvetica", 10))
        self.lbl_market_indices.pack(side=tk.LEFT, padx=5)
        
        self.status_label = ttk.Label(status_frame, text="상태: 대기 중", foreground="blue")
        self.status_label.pack(side=tk.RIGHT, padx=5)
        
        self.gui_utils.log_area = self.log_area
        self.log("🚀 프로그램이 시작되었습니다.")
        
        threading.Thread(target=self._fetch_indices_loop, daemon=True).start()
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
        if self.lbl_criteria:
            self.lbl_criteria.config(text=self._get_criteria_text(format_type="ui"))

    def _get_criteria_text(self, format_type="ui"):
        return self.gui_utils.get_criteria_text(self.settings, format_type)
        
    def log(self, message):
        self.gui_utils.log(message)

    def get_auth_token(self):
        token = self.auth_manager.get_auth_token()
        if token:
            if self.status_label:
                self.root.after(0, lambda: self.status_label.config(text="상태: 로그인 완료", foreground="green"))
            threading.Thread(target=lambda: self.get_kospi_200(force=True), daemon=True).start()

    def get_kospi_200(self, force=False):
        self.log(f"\n[기초] KOSPI 종목 리스트 수집/확인 중...")
        # 1. 실시간 감시용 (200종목)
        df_200 = self.data_manager.get_kospi_list(count=200, force=force)
        # 2. 전체 백업/하베스팅용 (500종목)
        self.data_manager.get_kospi_list(count=500, force=force)
        
        if df_200 is not None:
            self.log(f"✅ 감시 대상 {len(df_200)}개 종목을 로드했습니다.")
            if self.btn_select:
                self.root.after(0, lambda: self.btn_select.config(state=tk.NORMAL))
        self.log("=========================================================\n")

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
            self.log(f"❌ 종목 선정 중 예외 발생: {e}")
            self.root.after(0, lambda: self.btn_select.config(state=tk.NORMAL) if self.btn_select else None)

    def _fetch_indices_loop(self):
        while True:
            try:
                self._update_market_indices()
            except Exception as e:
                print(f"[ERROR] Market Index Fetch Error: {e}")
            time.sleep(120)

    def _update_market_indices(self):
        res_dict = self.data_manager.get_market_indices()
        results = []
        for name, data in res_dict.items():
            self.market_status[name] = data
            price, ratio, trend = data['val'], data['ratio'], data['trend']
            sign = "▲" if ratio > 0 else "▼"
            results.append(f"{name}: {price:,.2f} ({sign}{abs(ratio):.2f}%)")
        if results:
            status_text = " | ".join(results)
            if self.lbl_market_indices:
                self.root.after(0, lambda: self.lbl_market_indices.config(text=f"📊 시장 지수: {status_text}"))
            m_trend = "🟢순풍" if all(v['trend']=="UP" for v in self.market_status.values()) else "⚠️역풍"
            if not hasattr(self, '_last_m_trend') or self._last_m_trend != m_trend:
                self._last_m_trend = m_trend
                self.log(f"ℹ️ [시장] 현재 지수 추세: {m_trend}")

    def _on_realtime_data_received(self, code, now_price, volume, trde_val):
        """[v25.7] SignalEngine을 통해 실시간 데이터 처리 (모듈 전환)"""
        self.signal_engine.on_tick_received(code, now_price, volume, trde_val)

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

    def _render_tree_updates(self):
        if not self.tree_monitor or not self.is_monitoring: 
            self._update_timer_active = False
            return
        try:
            if self.pending_tree_updates:
                items = list(self.pending_tree_updates.items())
                self.pending_tree_updates.clear()
                for iid, (values, tag, is_blink) in items:
                    if self.tree_monitor.exists(iid):
                        if is_blink:
                            current_tags = list(self.tree_monitor.item(iid, 'tags'))
                            if "blink" not in current_tags:
                                current_tags.append("blink")
                                self.tree_monitor.item(iid, values=values, tags=tuple(current_tags))
                                self.root.after(400, lambda i=iid: self._remove_blink(i))
                            else:
                                self.tree_monitor.item(iid, values=values)
                        else:
                            self.tree_monitor.item(iid, values=values, tags=(tag,))
        except Exception as e:
            print(f"[DEBUG] Render error: {e}")
        finally:
            self.root.after(300, self._render_tree_updates)

    def _remove_blink(self, iid):
        try:
            if self.tree_monitor.exists(iid):
                current_tags = list(self.tree_monitor.item(iid, 'tags'))
                if "blink" in current_tags:
                    current_tags.remove("blink")
                    self.tree_monitor.item(iid, tags=tuple(current_tags))
        except: pass

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

        for _, row in df_targets.iterrows():
            c = str(row['Code']).zfill(6)
            name = row.get('Name', 'Unknown')
            self.monitor_info[c] = {
                'name': name, 'ma20': float(row.get('MA20', 0)), 'ma60': float(row.get('MA60', 0)),
                'prev_vol': float(row.get('PrevVol', 0)),
                'score': row.get('Score', 0.0), 'ma_buffer_scale': float(row.get('MABufScale', 1.0)),
                'open': float(row.get('Open', 0)), 'last_price': float(row.get('Price', 0)) if row.get('Price') else 0
            }
            if self.tree_monitor:
                vals = (
                    f"{row.get('Score', 0.0)}", c, name,
                    f"{int(row.get('Open', 0)):,}" if row.get('Open', 0) else "",
                    f"{int(row.get('Price', 0)):,}" if row.get('Price', 0) else "",
                    f"{int(row.get('Diff', 0)):+d}" if row.get('Diff', 0) else "",
                    f"{row.get('Ratio', 0):+.2f}%" if row.get('Ratio', 0) else "",
                    f"{int(row.get('Volume', 0)):,}" if row.get('Volume', 0) else "",
                    f"{row.get('AvgTrdeVal', 0)}억"
                )
                self.tree_monitor.insert("", tk.END, iid=c, values=vals)
        
        if self.lbl_monitor_status: 
            self.root.after(0, lambda: self.lbl_monitor_status.config(text=f"상태: 분석 중 ({len(self.monitor_codes)}종목)", foreground="blue"))
        
        def start_sequence():
            self.log("\n📊 [4단계] 분봉 데이터 분석 및 프로필 최적화 시작...")
            self._prepare_volume_profiles()
            self.is_monitoring = True
            if self.btn_monitor_toggle: self.root.after(0, self.btn_monitor_toggle.config, {"text": "⏹️ 감시 중단", "style": "Toggle.TButton"})
            if self.lbl_monitor_status: self.root.after(0, self.lbl_monitor_status.config, {"text": f"상태: 연결 중 ({len(self.monitor_codes)}종목)", "foreground": "blue"})
            self.monitor_thread = threading.Thread(target=self._ws_worker, daemon=True)
            self.monitor_thread.start()
        threading.Thread(target=start_sequence, daemon=True).start()

    def _prepare_volume_profiles(self):
        token_manager = TokenManager()
        token = token_manager.get_token()
        if not token: return
        self.data_manager.prepare_volume_profiles(self.monitor_codes, token, self.bars_data, self.volume_profiles, self.min_volume_profiles)
        self.log(f"✅ {len(self.volume_profiles)}/{len(self.monitor_codes)}개 종목 분석 완료")
        self.config_handler.save_bars_cache(self.bars_data) 

    def _recalculate_all_indicators(self, code):
        df = self.bars_data.get(code)
        if df is None or df.empty: return
        try:
            rsi_series = strategy_selector.calculate_rsi(df['Close'], 14)
            df['rsi_history'] = rsi_series
            self.bars_data[code] = df
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
            c = str(row['Code']).zfill(6)
            self.log(f"{i:3d} | {row['Score']:5.1f} | {c:6s} | {row['Name']:16s} | {int(row['Price']):8,d} | {row['Ratio']:>6.1f}% | {row['AvgTrdeVal']:>4d}억 | {row['RSI']:>5.1f}")
            msg += f"<b>{i}. {row['Name']}</b> ({c}) - <b>{row['Score']}점</b>\n📊 현재가: {int(row['Price']):,}원 ({row['Ratio']:+.2f}%)\n📈 RSI: {row['RSI']:.1f} | 💰 대금: {row['AvgTrdeVal']}억\n\n"
        threading.Thread(target=self.send_telegram_message, args=(msg,), daemon=True).start()

    def _render_virtual_positions(self):
        self.virtual_tab._render_virtual_positions()

    def _virtual_buy_stage_1(self, code, name, price):
        if code in self.virtual_positions: return
        invest_amt = int(self.settings.get('stage1_amt', 900000))
        qty = max(1, invest_amt // price)
        self.virtual_positions[code] = {
            "name": name, "avg_price": price, "qty": qty, "stage": 1,
            "invested": qty * price, "entry_time": datetime.now(), "max_now": price,
            "rsi_part_sold_s2": False, "ma_buffer_scale": self.monitor_info.get(code, {}).get('ma_buffer_scale', 1.0)
        }
        self.log(f"💰 [가상매수] {name}({code}) 1차 진입 완료. ({price:,}원, {qty}주)")
        msg = f"<b>💰 [가상매수/1차]</b>\n종목: {name}({code})\n가: {price:,}원 / 수: {qty}주\n시간: {datetime.now().strftime('%H:%M:%S')}"
        threading.Thread(target=self.send_telegram_message, args=(msg,), daemon=True).start()
        self.notebook.select(self.tab_virtual)
        self._render_virtual_positions()

    def _clear_virtual_positions(self):
        if not self.virtual_positions: return
        if messagebox.askyesno("확인", "모든 가상 포지션을 삭제하시겠습니까?"):
            self.virtual_positions.clear()
            self._render_virtual_positions()

    def _process_virtual_scenarios(self, code, now_price, rsi_val, macd_val, osc_val, vol_power, is_vol_spike, ind=None):
        """[v22.0] 설정창의 7가지 조건과 동기화된 가상 매매 시나리오 처리"""
        if code not in self.virtual_positions: return
        pos = self.virtual_positions[code]
        stage = pos['stage']
        avg_p = pos['avg_price']
        profit_ratio = (now_price / avg_p - 1) * 100
        pos['current_price'] = now_price
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

        if not is_exit and self.settings.get('use_stop_loss', True):
            sl_pct = float(self.settings.get('stop_loss' if stage == 1 else 's2_stop_loss', 3.0))
            if now_price <= avg_p * (1.0 - sl_pct / 100.0):
                is_exit, exit_reason = True, "STOP_LOSS"

        if is_exit:
            self.log(f"🚨 [가상종료/{exit_reason}] {pos['name']}({code}) 탈출 ({profit_ratio:+.1f}%)")
            msg = f"<b>🚨 [가상종료/{exit_reason}]</b>\n종목: {pos['name']}({code})\n수익률: {profit_ratio:+.1f}%\n시간: {datetime.now().strftime('%H:%M:%S')}"
            threading.Thread(target=self.send_telegram_message, args=(msg,), daemon=True).start()
            del self.virtual_positions[code]
            self._render_virtual_positions()
            return

        # 2. 2단계 매수(추매) 승인 체크
        if stage == 1:
            s = self.settings
            # 0. 최소 대기 기간 (Time Delay)
            if s.get('use_s2_delay', True):
                min_hours = float(s.get('s2_delay_hours', 24.0))
                if pos.get('entry_time') and (datetime.now() - pos['entry_time']).total_seconds() / 3600 < min_hours:
                    return
            
            # 기술적 조건 체크 (ind 기반)
            can_addon = True
            if ind is not None:
                rsi_val = ind.get('RSI', 0)
                macd_val = ind.get('MACD', 0)
                osc_val = ind.get('OSC', 0)
                # 1. 20일선 위 (s2_ma20_gap)
                if s.get('use_s2_ma20', True):
                    gap = float(s.get('s2_ma20_gap', 0.0))
                    if now_price < ind.get('MA20', 0) * (1 + gap/100.0): can_addon = False
                
                # 2. MA5 지지 (s2_ma5_gap)
                if can_addon and s.get('use_s2_ma5', True):
                    gap = float(s.get('s2_ma5_gap', -0.5))
                    if now_price < ind.get('MA5', 0) * (1 + gap/100.0): can_addon = False
                
                # 3. 거래 수급 (s2_vol_ratio)
                if can_addon and s.get('use_s2_vol_surge', True):
                    v_ma20 = ind.get('Vol_MA20', ind.get('Volume', 0))
                    if ind.get('Volume', 0) < v_ma20 * float(s.get('s2_vol_ratio', 1.0)): can_addon = False
                
                # 4. RSI 강세 (s2_rsi_gap)
                if can_addon and s.get('use_s2_rsi_sig', True):
                    r_sig = ind.get('RSI_Sig', 50)
                    if rsi_val < r_sig * (1 + float(s.get('s2_rsi_gap', 0.0))/100.0): can_addon = False
                
                # 5. MACD 정배열 (s2_macd_gap)
                if can_addon and s.get('use_s2_macd_gc', True):
                    m_sig = ind.get('MACD_Sig', 0)
                    if macd_val < m_sig + float(s.get('s2_macd_gap', 0.0)): can_addon = False
                

            # 7. 수익권 확인 (stage2_trigger)
            if can_addon:
                s2_trigger = float(s.get('stage2_trigger', 1.0))
                if profit_ratio >= s2_trigger:
                    add_amt = int(s.get('stage2_amt', 2000000))
                    add_qty = add_amt // now_price
                    if add_qty > 0:
                        new_inv = pos['invested'] + (add_qty * now_price)
                        pos['qty'] += add_qty
                        pos['avg_price'] = new_inv / pos['qty']
                        pos['invested'] = new_inv
                        pos['stage'] = 2
                        self.log(f"🔥 [가상추매] {pos['name']}({code}) 2차 진입! (평단: {pos['avg_price']:.0f})")
                        self._render_virtual_positions()

if __name__ == "__main__":
    root = tk.Tk()
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except: pass
    KiwoomApp(root)
    root.mainloop()
