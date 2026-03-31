import FinanceDataReader as fdr
import os
import pandas as pd
# [Fix] Silencing FutureWarnings regarding dtype downcasting (pandas 2.2+)
pd.set_option('future.no_silent_downcasting', True)
import json
import time
import requests
import config
from datetime import datetime, timedelta
import numpy as np
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from concurrent.futures import ThreadPoolExecutor as TPE, as_completed
import threading
from data_manager_v2 import DataManagerV2
import strategy_selector
import itertools

# [Modularization Phase 1]
from utils.normalization import normalize_code
from strategy.data_loader import BacktestDataLoader
# from reporting.signal_report import generate_signal_report (DELETED)
from reporting.charts import generate_stock_market_chart
import reporting.trade_report
import importlib
importlib.reload(reporting.trade_report)
from reporting.trade_report import generate_trade_report
from strategy import indicators, signals
from strategy.indicators import calculate_all_indicators
from strategy.simulation_models import SimulatedRealtimeMetrics

csv_lock = threading.Lock()
# [v15.3.7] Script-relative absolute paths for robust log management
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNAL_CSV = os.path.join(BASE_DIR, "backtest_signals.csv")
TRADE_CSV = os.path.join(BASE_DIR, "backtest_trades.csv")
# print(f"DEBUG [backtest_engine]: BASE_DIR={BASE_DIR}")
# print(f"DEBUG [backtest_engine]: TRADE_CSV={TRADE_CSV}")

class TradingStrategy:
    """[v6.4] 실시간 감시와 백테스팅이 공유하는 매매 전략 엔진"""
    _name_map = None
    
    @staticmethod
    def get_stock_name_map():
        """종목코드 -> 종목명 맵핑 로드 (v6.3)"""
        if TradingStrategy._name_map is not None:
            return TradingStrategy._name_map
            
        csv_path = "kodex200.csv"  # [v30.5 Fixed] kospi_top_500.csv -> kodex200.csv
        try:
            if not os.path.exists(csv_path):
                # 다른 kodex_*.csv/kodex*.csv 또는 kospi_*.csv 파일이 있는지 확인
                import glob
                files = glob.glob("kodex*.csv") + glob.glob("kospi_*.csv")
                if files:
                    csv_path = files[0]
                else:
                    TradingStrategy._name_map = {}
                    return {}
            
            if os.path.exists(csv_path):
                df_name = pd.read_csv(csv_path, dtype={'종목코드': str, 'Code': str}, encoding='utf-8')
                # 한글 헤더 처리 (v31.0)
                if '종목코드' in df_name.columns:
                    df_name.rename(columns={'종목코드': 'Code', '종목명': 'Name'}, inplace=True)
                
                # 종목코드가 6자리가 되도록 패딩 (예: 660 -> 000660)
                df_name['Code'] = df_name['Code'].apply(lambda x: str(x).zfill(6))
                TradingStrategy._name_map = dict(zip(df_name['Code'], df_name['Name']))
            else:
                TradingStrategy._name_map = {}
        except Exception as e:
            print(f"  (!) Name Map Load Error (Path: {csv_path}): {e}")
            TradingStrategy._name_map = {}
        return TradingStrategy._name_map

    @staticmethod
    def calculate_buy_indicators(df, settings):
        """[v15.2] 매수 신호용 경량 지표 계산 (RSI, MACD, MA 전용)"""
        return indicators.calculate_buy_indicators_light(df, settings)

    @staticmethod
    def calculate_indicators(df, settings):
        """지표 계산 (공통)"""
        return calculate_all_indicators(df, settings)

    @staticmethod
    def get_market_status(market_df, vix_value=None):
        """[v8.2] MSI, ADX, VIX를 조합한 시장 상태 판정"""
        if market_df is None or len(market_df) < 120:
            return 'SAFE'
            
        # 시장지수 지표 계산
        m_df = TradingStrategy.calculate_indicators(market_df, {})
        last = m_df.iloc[-1]
        
        msi = last.get('MSI', 0)
        adx = last.get('ADX', 0)
        vix = vix_value if vix_value is not None else 15.0 # 기본값
        
        # 판정 로직 (v8.2 명세)
        if msi > 0 and adx >= 25 and vix <= 20:
            return 'AGGRESSIVE'
        elif msi > 0 and vix <= 20:
            return 'SAFE'
        elif msi < 0 and vix >= 30:
            return 'RISK'
        else:
            return 'WATCH'

    @staticmethod
    def get_buy_signals_detailed(df, settings, market_trend='UP'):
        """매수 신호 상세 분석 (v6.2: 상태값 포함)"""
        return signals.get_buy_signals_detailed(df, settings, market_trend)

    @staticmethod
    def get_buy_signals(df, settings, market_trend='UP'):
        """매수 신호 벡터 계산 (v6.1 통합: Trigger 기반)"""
        return TradingStrategy.get_buy_signals_detailed(df, settings, market_trend)['Signal']

    @staticmethod
    def _check_s2_approval(pos, last_row, settings, indicator_row=None, market_trend='UP'):
        """[v22.2] 설정창의 7가지 조건과 100% 동기화된 2차 매수 승인 로직 (Static)"""
        s = settings
        
        # [v4.5 Rule] 시장지수 필터 (Stage 2 공통)
        if s.get('use_market_filter', True) and market_trend != 'UP':
            return False
        
        # 0. 최소 대기 기간 (use_s2_delay)
        if s.get('use_s2_delay', True):
            min_hours = float(s.get('s2_delay_hours', 24))
            entry_dt = pd.to_datetime(pos['entry_date'])
            curr_dt = pd.to_datetime(last_row.name)
            if (curr_dt - entry_dt).total_seconds() / 3600 < min_hours:
                return False

        # 기술 지표 참조 (indicator_row 우선)
        ind = indicator_row if indicator_row is not None else last_row
        curr_price = last_row['Close']
        
        # 1. 20일선 위 위치 (use_s2_ma20)
        if s.get('use_s2_ma20', True):
            ma20_val = ind.get('MA20', 0)
            gap = float(s.get('s2_ma20_gap', 0.0))
            if curr_price < ma20_val * (1 + gap/100.0):
                return False

        # 2. MA5 지지 확인 (use_s2_ma5)
        if s.get('use_s2_ma5', True):
            ma5_val = ind.get('MA5', 0)
            gap = float(s.get('s2_ma5_gap', -0.5)) # 기본값 -0.5% (유격 허용)
            if curr_price < ma5_val * (1 + gap/100.0):
                return False
                
        # 3. 거래 수급 확인 (use_s2_vol_surge)
        if s.get('use_s2_vol_surge', True):
            # [v4.5 Sync] Use Prev_Day_Vol for better intraday surge detection
            vol_target = ind.get('Prev_Day_Vol', ind.get('Vol_MA20', ind.get('Volume', 0)))
            ratio = float(s.get('s2_vol_ratio', 1.5))
            if ind.get('Volume', 0) < vol_target * ratio:
                return False

        # 4. RSI 강세 유지 및 모멘텀 상승 (use_s2_rsi_sig)
        if s.get('use_s2_rsi_sig', True):
            rsi_sig = ind.get('RSI_Sig', 50)
            rsi_val = ind.get('RSI', 0)
            rsi_prev = ind.get('Prev_RSI', rsi_val) # 이전값이 없으면 동일한 값 취급
            gap = float(s.get('s2_rsi_gap', 0.0))
            # [v27.1] RSI-Signal 상회(Level) + RSI 상승 중(Momentum) 동시 만족 조건 (S1과 동계열)
            if rsi_val < rsi_sig * (1 + gap/100.0) or rsi_val <= rsi_prev:
                return False

        # 5. MACD 정배열 및 모멘텀 상승 (use_s2_macd_gc)
        if s.get('use_s2_macd_gc', True):
            macd_val = ind.get('MACD', 0)
            macd_sig = ind.get('MACD_Sig', 0)
            osc_val = ind.get('OSC', 1) 
            osc_prev = ind.get('Prev_OSC', osc_val)
            gap = float(s.get('s2_macd_gap', 0.0))
            # [v27.2] MACD-Signal 상회 + OSC 양수 + OSC 증가 중 (S1과 동계열)
            if macd_val < macd_sig + gap or osc_val <= 0 or osc_val <= osc_prev:
                return False
        # 6. 수익권 확인 (use_s2_profit)
        if s.get('use_s2_profit', True):
            trigger = float(s.get('s2_profit_limit', 1.0))
            if (curr_price / pos['avg_price'] - 1) * 100 < trigger:
                return False
                
        return True



class SimpleBacktester:
    """고정밀 15분봉/일봉 백테스터 (v5.1)"""
    def __init__(self, settings):
        self.settings = settings
        self.trades = []
        self.s1_amt = 0
        self.s2_amt = 0
        self.buy_f_rate = 0
        self.sell_f_rate = 0
        self.trades_history = []
        
        # [Perf] Pre-init cached settings placeholders (v33.8)
        self.use_sb = False
        self.use_sb_a = False
        self.use_sb_b = False
        self.use_sb_s2 = False
        self.sb_ba = 1.2
        self.sb_vp = 110.0
        self.use_sx = False
        self.sx_ba = 0.8
        self.sx_vp = 90.0
        self.sx_p_thr = 0.0
        self.use_atr = False
        self.atr_sl_mult = 2.0
        self.atr_trail_mult = 3.0
        self.gap_down_limit = -5.0
        self.use_s2_tp_active = True
        self.use_type_b = False
        
    def calculate_rsi(self, series, period=14):
        delta = series.diff()
        up = delta.clip(lower=0)
        down = -1 * delta.clip(upper=0)
        
        avg_up = up.rolling(window=period).mean()
        avg_down = down.rolling(window=period).mean()
        
        rs = avg_up / avg_down
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def run(self, df, code="SAMPLE", market_df=None, vix_df=None, log_to_csv=False, skip_indicators=False, high_precision=False, m1_df_preloaded=None):
        """[v15.0] Ultra-High Performance Backtest Engine"""
        try:
            # [v30.6/v30.8] 멀티스레드 환경에서 컬럼 추가/수정(In-place)으로 인한 pandas 내부 Index 충돌 버그 방지
            if not skip_indicators:
                df = df.copy()
            else:
                df = df.copy(deep=False) # 얕은 복사로 메모리는 아끼고, Series 주소망 분리
            # 1. Initialization
            if not skip_indicators:
                df = TradingStrategy.calculate_indicators(df, self.settings)
            
            if log_to_csv:
                print(f"  [Sim] Strategy Run: {code} | Data: {len(df)} rows | HighPrec: {high_precision}")
            
            params = self._init_simulation(df, code, market_df, vix_df, high_precision)
            
            # 2. Main Simulation Loop (v23.0 Optimized)
            # [Perf] 미리 벡터화된 신호 및 가격 정보를 로컬 변수로 캐싱하여 루프 부하 최적화
            # [v31.4] Real-time only columns preparation
            if self.settings.get('use_synthetic_realtime', True):
                if 'vol_power' not in df.columns: df['vol_power'] = np.nan
                if 'total_ask' not in df.columns: df['total_ask'] = np.nan
                if 'total_bid' not in df.columns: df['total_bid'] = np.nan
                if 'bid_ask_ratio' not in df.columns: df['bid_ask_ratio'] = np.nan
                
                # Fill NaNs with vectorized heuristic model (v35.8 Optimized)
                p_changes = df['Close'].pct_change().fillna(0).values
                vol_mas = df['Vol_MA20'].values if 'Vol_MA20' in df.columns else df['Volume'].rolling(20).mean().bfill().values
                vol_ratios = (df['Volume'] / (vol_mas + 1e-10)).values
                
                vps = df['vol_power'].values.copy()
                tasks = df['total_ask'].values.copy()
                tbids = df['total_bid'].values.copy()
                ratios = df['bid_ask_ratio'].values.copy()
                
                # Vectorized Fill
                nan_vp = np.isnan(vps)
                if np.any(nan_vp):
                    vps[nan_vp] = SimulatedRealtimeMetrics.simulate_vol_power_vectorized(p_changes[nan_vp], vol_ratios[nan_vp])
                
                nan_ba = np.isnan(ratios)
                if np.any(nan_ba):
                    r_new = SimulatedRealtimeMetrics.simulate_bid_ask_ratio_vectorized(p_changes[nan_ba], vol_ratios[nan_ba], vps[nan_ba])
                    ratios[nan_ba] = r_new
                    tasks[nan_ba] = 100000
                    tbids[nan_ba] = (100000 * r_new).astype(int)
                
                df['vol_power'] = vps
                df['total_ask'] = tasks
                df['total_bid'] = tbids
                df['bid_ask_ratio'] = ratios
                df['ask_bid_ratio'] = 1.0 / (ratios + 1e-10)

            close_vals = df['Close'].values
            high_vals = df['High'].values
            low_vals = df['Low'].values
            vol_vals = df['Volume'].values
            vol_ma_vals = df['Vol_MA20'].values if 'Vol_MA20' in df.columns else (df['Volume'].rolling(20).mean().values)
            
            # [v31.4] Real-time only columns (VolPower, Bid/Ask)
            vp_vals = df['vol_power'].values if 'vol_power' in df.columns else np.full(len(df), np.nan)
            task_vals = df['total_ask'].values if 'total_ask' in df.columns else np.full(len(df), np.nan)
            tbid_vals = df['total_bid'].values if 'total_bid' in df.columns else np.full(len(df), np.nan)
            
            # [Perf] Extract all potentially needed indicators to NumPy for zero-copy access in loops
            # This avoids expensive df.iloc[i] and df.get(col) calls
            np_indicators = {
                'Close': close_vals,
                'High': high_vals,
                'Low': low_vals,
                'Volume': vol_vals,
                'RSI': df['RSI'].values if 'RSI' in df.columns else np.zeros(len(df)),
                'MACD': df['MACD'].values if 'MACD' in df.columns else np.zeros(len(df)),
                'MACD_Sig': df['MACD_Sig'].values if 'MACD_Sig' in df.columns else np.zeros(len(df)),
                'OSC': df['OSC'].values if 'OSC' in df.columns else np.zeros(len(df)),
                'Prev_OSC': df['Prev_OSC'].values if 'Prev_OSC' in df.columns else np.zeros(len(df)),
                'MA5': df['MA5'].values if 'MA5' in df.columns else np.zeros(len(df)),
                'MA20': df['MA20'].values if 'MA20' in df.columns else np.zeros(len(df)),
                'Vol_MA20': vol_ma_vals,
                'ATR': df['ATR'].values if 'ATR' in df.columns else np.zeros(len(df)),
                'RSI_Sig': df['RSI_Sig'].values if 'RSI_Sig' in df.columns else np.zeros(len(df)),
                'Prev_RSI': df['Prev_RSI'].values if 'Prev_RSI' in df.columns else np.zeros(len(df)),
                'Market_Status': df['Market_Status'].values if 'Market_Status' in df.columns else np.full(len(df), 'SAFE', dtype=object),
                'Market_Trend': df['Market_Trend'].values if 'Market_Trend' in df.columns else np.full(len(df), 'UP', dtype=object),
                'Sig_Type': df['Sig_Type'].values if 'Sig_Type' in df.columns else np.full(len(df), 'TYPE_A', dtype=object),
                'bid_ask_ratio': df['bid_ask_ratio'].values if 'bid_ask_ratio' in df.columns else np.full(len(df), 1.0),
                'ask_bid_ratio': df['ask_bid_ratio'].values if 'ask_bid_ratio' in df.columns else np.full(len(df), 1.0),
                'vol_power': df['vol_power'].values if 'vol_power' in df.columns else np.full(len(df), 100.0),
                'Signal': df['Signal'].values if 'Signal' in df.columns else np.zeros(len(df), dtype=bool),
                'MA60': df['MA60'].values if 'MA60' in df.columns else np.zeros(len(df)),
                'Prev_Day_Vol': df['Prev_Day_Vol'].values if 'Prev_Day_Vol' in df.columns else np.zeros(len(df))
            }
            
            time_indices = df.index
            # [Perf] Pre-cache hour/minute for gap grace check
            time_hours = time_indices.hour.values
            time_minutes = time_indices.minute.values
            
            m1_np_high = None
            m1_np_close = None
            m1_np_time = None
            m1_boundaries = None
            if high_precision and m1_df_preloaded is not None:
                # Find the starting index of 1M data for each 15M timestamp
                times_15m = df.index
                m1_times = m1_df_preloaded.index
                start_idxs = np.searchsorted(m1_times, times_15m)
                end_idxs = np.searchsorted(m1_times, times_15m + pd.Timedelta(minutes=14, seconds=59), side='right')
                # Store as (start, end) tuples or arrays
                m1_boundaries = list(zip(start_idxs, end_idxs))
                # [v4.5.10] Extract VP/BA for 1M Smart Exit simulation
                m1_np_high = m1_df_preloaded['High'].values
                m1_np_close = m1_df_preloaded['Close'].values
                m1_np_time = m1_df_preloaded.index.values
                # Bid/Ask Ratio mapping (v4.5: Ensure column names match data_loader)
                m1_np_ba = m1_df_preloaded['total_bid'].values / m1_df_preloaded['total_ask'].values if 'total_bid' in m1_df_preloaded.columns else np.full(len(m1_df_preloaded), 1.0)
                m1_np_vp = m1_df_preloaded['vol_power'].values if 'vol_power' in m1_df_preloaded.columns else np.full(len(m1_df_preloaded), 100.0)
            else:
                m1_boundaries = None
                m1_np_ba = None
                m1_np_vp = None
            
            # 신호 및 필터 미리 정리
            signal_shifted = df['Signal_Shifted'].values if 'Signal_Shifted' in df.columns else np.zeros(len(df), dtype=bool)
            
            total_bars = len(df)
            # [v35.5] High-Performance Simulation Step (Zero Dict Creation)
            # Initialize equity_curve[0] outside the loop
            params['equity_curve'][0] = params['balance']
            equity_curve = params['equity_curve'] # Reference to modify directly
            
            # [v35.6] Ultra-Performance Unpacked Loop
            # Dictionary lookups are slow in million-bar loops. Unpack arrays to local variables once.
            ind_signal = np_indicators['Signal']
            ind_sig_type = np_indicators['Sig_Type']
            ind_mkt_status = np_indicators['Market_Status']
            ind_mkt_trend = np_indicators['Market_Trend']
            ind_ba_ratio = np_indicators['bid_ask_ratio']
            ind_vp = np_indicators['vol_power']
            ind_rsi = np_indicators['RSI']
            
            # Unpack hot parameters for simulation step to avoid repeated dict/attr access
            pos = params['position']
            balance = params['balance']
            
            for i in range(1, total_bars):
                m1_info = (m1_np_high, m1_np_close, m1_np_time, m1_boundaries[i], m1_np_ba, m1_np_vp) if m1_boundaries else None
                
                # Update loop locals
                params['position'] = pos
                params['balance'] = balance
                
                # [v35.6] Optimized Step
                params = self._sim_loop_step_v3(
                    i, close_vals, high_vals, low_vals, time_indices, time_hours, time_minutes, 
                    signal_shifted, np_indicators, code, m1_info, high_precision, params, log_to_csv
                )
                
                pos = params['position']
                balance = params['balance']
                equity_curve[i] = balance + (pos['qty'] * close_vals[i] if pos else 0)
                if not params['position'] and i > 0 and params['balance'] != params['initial_balance']:
                    # [v27.0] Slot Budget Protection: If all sold and net profit exists
                    pass 

            # 3. Finalize and Return Results
            return self._finalize_simulation(df, code, params, log_to_csv)

        except Exception as e:
            print(f"  [Error] Simulation failed for {code}: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _init_simulation(self, df, code, market_df, vix_df, high_precision):
        """시뮬레이션 초기 설정 및 시장 상태 계산"""
        total_capital_val = float(self.settings.get('backtest_initial_capital', 30000000))
        # [v4.5 Design Update] 슬롯별 예산 제한 폐지. 각 종목은 전체 가용 자산을 기준으로 시뮬레이션됨.
        slot_budget = total_capital_val
        
        # [v4.5 Sync] Handle Sizing Units (Won / %)
        s1_unit = self.settings.get('s1_sizing_unit', '%')
        if s1_unit == '원':
            self.s1_amt = float(self.settings.get('stage1_amt', 1000000.0))
        else:
            s1_pct = float(self.settings.get('s1_equity_pct', 20.0))
            self.s1_amt = total_capital_val * (s1_pct / 100.0)

        s2_unit = self.settings.get('s2_sizing_unit', '%')
        if s2_unit == '원':
            self.s2_amt = float(self.settings.get('stage2_amt', 2000000.0))
        else:
            s2_pct = float(self.settings.get('s2_equity_pct', 30.0))
            self.s2_amt = total_capital_val * (s2_pct / 100.0)

        self.buy_f_rate = self.settings.get('buy_fee_bt', self.settings.get('buy_fee_rate', 0.015)) / 100.0
        self.sell_f_rate = self.settings.get('sell_fee_bt', self.settings.get('sell_fee_rate', 0.215)) / 100.0
        
        stock_name_map = TradingStrategy.get_stock_name_map()
        stock_name = stock_name_map.get(code, code)

        # [Perf] Cache Smart Filter & Exit settings to avoid repeated dictionary lookups in hot loops
        s = self.settings
        self.use_sb_a = s.get('use_smart_buy_a', True)
        self.use_sb_b = s.get('use_smart_buy_b', True)
        self.use_sb_s2 = s.get('use_smart_buy_s2', True)
        self.sb_ba = float(s.get('smart_buy_ba_ratio', 1.2))
        self.sb_vp = float(s.get('smart_buy_vp', 110.0))
        
        self.use_sx = s.get('use_smart_exit', True) # [v4.5 Sync] Default True
        self.sx_ba = float(s.get('smart_exit_ba_ratio', 1.3)) # [v4.5 Sync] UI/SignalEngine Default 1.3
        self.sx_vp = float(s.get('smart_exit_vp', 90.0))
        self.sx_p_thr = float(s.get('smart_exit_price_threshold', -0.5)) # [v4.5 Sync] Default -0.5
        
        # [Perf] Cache TP/SL parameters (v33.6)
        self.use_s1a_tp_active = s.get('use_s1_a_tp', True)
        self.use_s1b_tp_active = s.get('use_s1_b_tp', True)
        self.s1a_tp = float(s.get('s1_a_take_profit', 5.0)) / 100.0
        self.s1b_tp = float(s.get('s1_b_take_profit', 5.0)) / 100.0
        self.s2_tp = float(s.get('s2_take_profit', 5.0)) / 100.0
        
        self.use_s1a_sl_active = s.get('use_s1_a_stop_loss', True)
        self.use_s1b_sl_active = s.get('use_s1_b_stop_loss', True)
        self.use_s2_sl = s.get('use_s2_stop_loss', True) # [v35.3] Added missing S2 SL flag
        self.s1a_sl = float(s.get('s1_a_stop_loss', 3.0)) / 100.0
        self.s1b_sl = float(s.get('s1_b_stop_loss', 3.0)) / 100.0
        self.s2_sl = float(s.get('s2_stop_loss', 2.0)) / 100.0
        
        self.use_type_b = s.get('use_type_b', False)
        
        # [Perf] Cache ATR & Gap Down settings (v33.7) - Granular S1A/S1B/S2 (v4.5 Sync)
        self.use_atr = s.get('use_atr_exit', False)
        
        self.use_atr_sl_a = s.get('use_atr_stop_loss_a', True)
        self.use_atr_sl_b = s.get('use_atr_stop_loss_b', True)
        self.use_atr_sl_s2 = s.get('use_atr_stop_loss_s2', True)
        
        self.use_atr_trail_a = s.get('use_atr_trailing_stop_a', True)
        self.use_atr_trail_b = s.get('use_atr_trailing_stop_b', True)
        self.use_atr_trail_s2 = s.get('use_atr_trailing_stop_s2', True)
        
        self.atr_sl_mult_a = float(s.get('atr_multiplier_sl_a', 2.5))
        self.atr_sl_mult_b = float(s.get('atr_multiplier_sl_b', 2.5))
        self.atr_sl_mult_s2 = float(s.get('atr_multiplier_sl_s2', 2.5))
        
        self.atr_trail_mult_a = float(s.get('atr_multiplier_trail_a', 4.0))
        self.atr_trail_mult_b = float(s.get('atr_multiplier_trail_b', 4.0))
        self.atr_trail_mult_s2 = float(s.get('atr_multiplier_trail_s2', 4.0))
        
        self.gap_down_limit = float(s.get('gap_down_limit', -5.0))
        self.use_gap_down_exit = s.get('use_gap_down_exit', True) # [v4.5 Sync] New toggle
        self.use_s2_tp_active = s.get('use_s2_tp', True)
        self.use_gap_grace = s.get('use_gap_grace', True)
        
        # [Perf] Cache Flash Exit settings
        self.use_s1a_flash_active = s.get('use_s1_a_flash_exit', True)
        self.use_s1b_flash_active = s.get('use_s1_b_flash_exit', True)
        self.use_s2_flash = s.get('use_s2_flash_exit', True)
        self.s1a_flash = float(s.get('s1_a_flash_exit_pct', 3.0))
        self.s1b_flash = float(s.get('s1_b_flash_exit_pct', 3.0))
        self.s2_flash = float(s.get('s2_flash_exit_pct', 3.0))
        
        # [Perf] Cache S2 Approval settings
        self.use_s2_delay = s.get('use_s2_delay', True)
        self.s2_delay_h = float(s.get('s2_delay_hours', 24))
        self.use_s2_ma20 = s.get('use_s2_ma20', True)
        self.s2_ma20_g = float(s.get('s2_ma20_gap', 0.0))
        self.use_s2_ma5 = s.get('use_s2_ma5', True)
        self.s2_ma5_g = float(s.get('s2_ma5_gap', -0.5))
        self.use_s2_vol = s.get('use_s2_vol_surge', True)
        self.s2_vol_r = float(s.get('s2_vol_ratio', 1.5)) # Default sync
        self.use_s2_rsi = s.get('use_s2_rsi_sig', True)
        self.s2_rsi_g = float(s.get('s2_rsi_gap', 0.0))
        self.use_s2_macd = s.get('use_s2_macd_gc', True)
        self.s2_macd_g = float(s.get('s2_macd_gap', 0.0))
        self.use_s2_profit = s.get('use_s2_profit', True)
        self.s2_trigger = float(s.get('s2_profit_limit', 1.0))
        
        # 시장 상태 계산
        if market_df is not None and not market_df.empty:
            # [Perf] Pre-calculated market indicators check
            if 'MSI' in market_df.columns and 'ADX' in market_df.columns:
                m_df = market_df
            else:
                m_df = TradingStrategy.calculate_indicators(market_df.copy(), {})
            # [Fix] Align VIX with market index to prevent shape mismatch (broadcast error)
            vix_s = vix_df['Close'].reindex(m_df.index).ffill().fillna(15.0) if vix_df is not None and 'Close' in vix_df.columns else pd.Series(15.0, index=m_df.index)
            
            # [v4.5.10] Unified Triple-Lock Safety System (DNA Sync)
            # Thresholds: DOWN (Stop All), NEUTRAL (Stop Type B), UP (Go All)
            lock_down = (m_df['MSI'] < -0.10) | (vix_s > 30)
            lock_neutral = (m_df['ADX'] < 15) | ((m_df['MSI'] >= -0.10) & (m_df['MSI'] <= 0.05))
            
            m_df['Trend'] = np.where(lock_down, 'DOWN', 
                                     np.where(lock_neutral, 'NEUTRAL', 'UP'))
            
            # Status Mapping (v4.5.10 Hardened)
            m_df['Status'] = 'WATCH'
            m_df.loc[(m_df['Trend'] == 'UP') & (m_df['ADX'] >= 25) & (vix_s <= 20), 'Status'] = 'AGGRESSIVE'
            m_df.loc[(m_df['Trend'] == 'UP') & (m_df['Status'] == 'WATCH'), 'Status'] = 'SAFE'
            m_df.loc[m_df['Trend'] == 'DOWN', 'Status'] = 'RISK'

            # [Realism] Today's market trend is determined by yesterday's close data to avoid look-ahead bias
            m_df['Trend'] = m_df['Trend'].shift(1).fillna('UP')
            m_df['Status'] = m_df['Status'].shift(1).fillna('WATCH')
            
            # [v35.7] Safety: ensure DatetimeIndex for normalize()
            if not isinstance(df.index, pd.DatetimeIndex):
                if 'datetime' in df.columns:
                    df.index = pd.to_datetime(df['datetime'])
                elif 'time' in df.columns:
                    df.index = pd.to_datetime(df['time'])
                else:
                    # If index is still RangeIndex, normalize() will fail
                    pass
            
            if isinstance(df.index, pd.DatetimeIndex):
                df_daily_idx = df.index.normalize()
                df['Market_Status'] = df_daily_idx.map(m_df['Status']).fillna('WATCH')
                df['Market_Trend'] = df_daily_idx.map(m_df['Trend']).fillna('UP')
            else:
                df['Market_Status'] = 'WATCH'
                df['Market_Trend'] = 'UP'
            # [Fix] FutureWarning: infer_objects to ensure clean dtype
            df['Market_Status'] = df['Market_Status'].infer_objects(copy=False)
            df['Market_Trend'] = df['Market_Trend'].infer_objects(copy=False)
        else:
            df['Market_Status'] = 'SAFE'
            df['Market_Trend'] = 'UP'

        # [v26.1] 신호 미리 계산 (최적화 시 신호 파라미터가 바뀔 수 있으므로 매번 수행)
        sigs = TradingStrategy.get_buy_signals_detailed(df, self.settings, market_trend='UP')
        for c in sigs.columns:
            df[c] = sigs[c]
        # [v4.5 Rule] 시장지수 필터링 (UP/NEUTRAL/DOWN 3단계)
        if self.settings.get('use_market_filter', True):
            # 1. DOWN (역풍): 모든 신호 차단
            df.loc[df['Market_Trend'] == 'DOWN', 'Signal'] = False
            # 2. NEUTRAL (중립): Type B 신호 차단
            df.loc[(df['Market_Trend'] == 'NEUTRAL') & (df['Sig_Type'] == 'TYPE_B'), 'Signal'] = False

        # [v30.0] 로직 변경: 신호 발생 즉시 매수 (지연 없음)
        df['Signal_Shifted'] = df['Signal'].astype(bool)
        df['Sig_Type_Shifted'] = df['Sig_Type']

        self.trades = []
        return {
            'code': code,
            'balance': slot_budget,
            'initial_balance': slot_budget,
            'equity_curve': [slot_budget] * len(df),
            'position': None,
            'history': [],
            'signal_logs': [],
            'm1_checks': 0,
            'stock_name': stock_name
        }

    def _check_s2_approval_v2(self, pos, indicators, i):
        # [v4.5 Rule] 시장지수 필터 (Stage 2 공통)
        m_trend = indicators['Market_Trend'][i] if 'Market_Trend' in indicators else 'UP'
        if self.settings.get('use_market_filter', True) and m_trend != 'UP':
            return False
            
        return TradingStrategy._check_s2_approval(
            pos, None, self.settings, 
            indicator_row={k: v[i] for k, v in indicators.items()},
            market_trend=m_trend
        )

    def _check_s2_approval_legacy(self, pos, indicators, i):
        """[v35.5] Ultra-Fast 2nd Buy approval (Direct Indexing, No Dict Creation)"""
        if self.use_s2_ma20:
            ma20 = indicators['MA20'][i]
            if ma20 == 0 or indicators['Close'][i] < ma20 * (1 + self.s2_ma20_g/100.0): return False
        if self.use_s2_ma5:
            ma5 = indicators['MA5'][i]
            if ma5 > 0 and indicators['Close'][i] < ma5 * (1 + self.s2_ma5_g/100.0): return False
        if self.use_s2_vol:
            # [v35.8] Unified Volume Strategy: Use Same-Time Average (Prev_Day_Vol) instead of MA20
            if indicators['Volume'][i] < indicators['Prev_Day_Vol'][i] * self.s2_vol_r: return False
        if self.use_s2_rsi:
            rsi = indicators['RSI'][i]
            if rsi < indicators['RSI_Sig'][i] * (1 + self.s2_rsi_g/100.0) or rsi <= indicators['Prev_RSI'][i]: return False
        if self.use_s2_macd:
            macd = indicators['MACD'][i]
            osc = indicators['OSC'][i]
            if macd < indicators['MACD_Sig'][i] + self.s2_macd_g or osc <= 0 or osc <= indicators['Prev_OSC'][i]: return False
        if self.use_s2_profit:
            if (indicators['Close'][i] / pos['avg_price'] - 1) * 100 < self.s2_trigger: return False
        return True

            
    def _process_intra_bar_exit_only_v2(self, i, code, m1_highs, m1_closes, m1_times, params, time_h, time_m, m1_ba=None, m1_vp=None, indicators=None):
        """[v35.0] Ultra-Fast Intra-bar Exit check (v4.5.10 ATR/Smart Sync)"""
        pos = params['position']
        sig_type = pos.get('sig_type', 'TYPE_A')
        is_type_b = (sig_type == "TYPE_B" and self.use_type_b)
        stage = pos['stage']
        
        if stage == 1:
            use_flash = self.use_s1b_flash_active if is_type_b else self.use_s1a_flash_active
            f_pct = (self.s1b_flash if is_type_b else self.s1a_flash) if use_flash else 0.0
            sl_pct = (self.s1b_sl if is_type_b else self.s1a_sl) * 100 if (self.use_s1b_sl_active if is_type_b else self.use_s1a_sl_active) else 0.0
            tp_pct = (self.s1b_tp if is_type_b else self.s1a_tp) * 100 if (self.use_s1b_tp_active if is_type_b else self.use_s1a_tp_active) else 0.0
        else:
            f_pct = self.s2_flash if self.use_s2_flash else 0.0
            sl_pct = self.s2_sl * 100 if self.use_s2_sl else 0.0
            tp_pct = self.s2_tp * 100 if self.use_s2_tp_active else 0.0
        
        avg_price = pos['avg_price']
        prev_max = pos.get('max_price', avg_price)
        running_max = np.maximum.accumulate(m1_highs)
        running_max = np.maximum(running_max, prev_max)
        
        flash_exited = (f_pct > 0) & (m1_closes < running_max * (1 - f_pct/100))
        stop_lossed = (sl_pct > 0) & (m1_closes / avg_price - 1 <= -sl_pct/100)
        take_profited = (tp_pct > 0) & (m1_highs / avg_price - 1 >= tp_pct/100)
        gap_downed = (self.use_gap_down_exit) & (m1_closes / avg_price - 1 <= self.gap_down_limit / 100)

        # [v4.5.10] ATR Exit Logic (Sync with StrategyEngine)
        atr_exit = np.zeros(len(m1_closes), dtype=bool)
        atr_trail = np.zeros(len(m1_closes), dtype=bool)
        if self.use_atr and indicators is not None:
            atr_val = indicators['ATR'][i]
            entry_atr = pos.get('atr_at_entry', atr_val)
            if not np.isnan(atr_val) and atr_val > 0:
                suff = "_s2" if stage == 2 else ("_b" if is_type_b else "_a")
                if getattr(self, f'use_atr_sl{suff}'):
                    atr_exit = (m1_closes <= avg_price - (getattr(self, f'atr_sl_mult{suff}') * entry_atr))
                if getattr(self, f'use_atr_trail{suff}'):
                    atr_trail = (m1_closes <= running_max - (getattr(self, f'atr_trail_mult{suff}') * atr_val))

        # [v4.5.10] Smart Exit Logic (Sync with DNA / StrategyEngine)
        smart_exited = np.zeros(len(m1_closes), dtype=bool)
        if self.use_sx and m1_ba is not None and m1_vp is not None:
            # DNA/StrategyEngine rule: Bid Trap (High Bid wall + Weak VolPower + Price Stall)
            # Bid/Ask >= 1.3 AND VolPower < 90 AND (optional) Price logic
            smart_exited = (m1_ba >= self.sx_ba) & (m1_vp < self.sx_vp)
            if self.sx_p_thr != 0:
                smart_exited &= (m1_closes <= running_max * (1 + self.sx_p_thr / 100.0))

        if self.use_gap_grace and time_h == 9 and time_m == 0:
            grace_idx_limit = 15 
            if len(stop_lossed) > 0: stop_lossed[:grace_idx_limit] = False
            if len(gap_downed) > 0: gap_downed[:grace_idx_limit] = False
            if len(flash_exited) > 0: flash_exited[:grace_idx_limit] = False
                
        exit_mask = flash_exited | stop_lossed | take_profited | gap_downed | atr_exit | atr_trail | smart_exited
        exit_idxs = np.where(exit_mask)[0]
        if len(exit_idxs) > 0:
            idx = int(exit_idxs[0])
            e_price, e_time = m1_closes[idx], m1_times[idx]
            if bool(take_profited[idx]): e_price = avg_price * (1 + tp_pct/100)
            
            reason = "1M_SL"
            if bool(gap_downed[idx]): reason = "1M_GapDown"
            elif bool(flash_exited[idx]): reason = "1M_FlashExit"
            elif bool(take_profited[idx]): reason = "1M_TP"
            elif bool(atr_exit[idx]): reason = "1M_ATR_SL"
            elif bool(atr_trail[idx]): reason = "1M_ATR_Trail"
            elif bool(smart_exited[idx]): reason = f"1M_SmartExit(BA:{m1_ba[idx]:.2f}/VP:{m1_vp[idx]:.0f})"
            
            params = self._execute_sell(e_time, e_price, reason, params)
        return params

    def _sim_loop_step_v3(self, i, close_vals, high_vals, low_vals, time_indices, time_hours, time_minutes, signal_shifted, indicators, code, m1_info, high_precision, params, log_to_csv=False):
        """[v35.5] Ultra-Fast Simulation Step (Direct Indexing, No Dict Creation)"""
        curr_close = close_vals[i]
        curr_date = time_indices[i]
        curr_high = high_vals[i]
        curr_low = low_vals[i]
        
        # 1. High Precision Exit Monitoring
        if high_precision and m1_info is not None and params['position'] is not None:
            m1_high_total, m1_close_total, m1_time_total, (s_idx, e_idx), m1_ba_total, m1_vp_total = m1_info
            if e_idx > s_idx:
                m1_highs = m1_high_total[s_idx:e_idx]
                m1_closes = m1_close_total[s_idx:e_idx]
                m1_ba = m1_ba_total[s_idx:e_idx] if m1_ba_total is not None else None
                m1_vp = m1_vp_total[s_idx:e_idx] if m1_vp_total is not None else None
                params = self._process_intra_bar_exit_only_v2(
                    i, code, m1_highs, m1_closes, m1_time_total[s_idx:e_idx], 
                    params, time_hours[i], time_minutes[i], 
                    m1_ba=m1_ba, m1_vp=m1_vp, indicators=indicators
                )
                if params['position'] is None:
                    return params

        # 2. Standard Sim Logic
        if params['position'] is not None:
            pos = params['position']
            if curr_high > pos['max_price']: pos['max_price'] = curr_high
            
            # Exit Check
            exit_res = self._check_exits_optimized(curr_date, curr_close, curr_high, curr_low, pos, indicators=indicators, i=i)
            if exit_res:
                params = self._execute_sell(curr_date, exit_res['price'], exit_res['reason'], params, indicators=indicators, i=i)
            
            # Stage 2 Buy Check
            elif pos['stage'] == 1:
                if self._check_s2_approval_v2(pos, indicators, i):
                    can_buy_s2 = True
                    if self.use_sb_s2:
                        ab_ratio = indicators['ask_bid_ratio'][i]
                        v_power = indicators['vol_power'][i]
                        # [v4.4] S2 추매 정석: 매도벽(Ask) 두터울 때
                        if ab_ratio < self.sb_ba or v_power < self.sb_vp:
                            can_buy_s2 = False
                    
                    if can_buy_s2:
                        s2_reason = f"ADDON(S2)|R:{indicators['RSI'][i]:.1f}|M:{indicators['MACD'][i]:.1f}|O:{indicators['OSC'][i]:.2f}|V:{indicators['Volume'][i]}"
                        if self.use_sb_s2: s2_reason = "SmartBuy|" + s2_reason
                        params = self._execute_buy(curr_date, curr_close, "BUY_2ND", params, stage=2, reason=s2_reason, indicators=indicators, i=i)
        
        # 3. Entry Check
        elif signal_shifted[i]:
            mkt_status = indicators['Market_Status'][i]
            sig_type = indicators['Sig_Type'][i]
            is_ok = (mkt_status != 'RISK')
            if mkt_status == 'WATCH' and sig_type != 'TYPE_A': is_ok = False
            
            if is_ok:
                use_sb_for_type = self.use_sb_a if sig_type == "TYPE_A" else self.use_sb_b
                if use_sb_for_type:
                    ab_ratio = indicators['ask_bid_ratio'][i]
                    v_power = indicators['vol_power'][i]
                    # [v4.4] S1 진입 정석: 매도벽(Ask) 두터울 때
                    if ab_ratio < self.sb_ba or v_power < self.sb_vp:
                        is_ok = False
                
                if is_ok:
                    reason = "SmartBuy|SIG" if use_sb_for_type else "SIG"
                    params = self._execute_buy(curr_date, curr_close, "BUY_1ST", params, stage=1, entry_idx=i, max_p=curr_high, reason=reason, indicators=indicators, i=i, sig_type=sig_type)
        
        if log_to_csv and not self.settings.get('skip_indicators', False):
            if indicators['Signal'][i] or (indicators['RSI'][i] > 30 and indicators['RSI'][i] < 80):
                self._log_signal(curr_date, code, params['stock_name'], curr_close, indicators, i, indicators['Market_Trend'][i], params)
        return params

    def _check_exits_optimized(self, date, price, high, low, pos, indicators=None, i=None):
        """[v35.5] Ultra-Fast Exit check (v4.5 Granular ATR Sync)"""
        sig_type = pos.get('sig_type', 'TYPE_A')
        is_type_b = (sig_type == "TYPE_B" and self.use_type_b)
        stage = pos.get('stage', 1)

        # 1. Flash Exit
        if stage == 1:
            f_pct = (self.s1b_flash if is_type_b else self.s1a_flash) / 100.0
            use_f = (self.use_s1b_flash_active if is_type_b else self.use_s1a_flash_active)
        else:
            f_pct = self.s2_flash / 100.0
            use_f = self.use_s2_flash
            
        if use_f and f_pct > 0 and price <= pos['max_price'] * (1.0 - f_pct):
            return {'price': price, 'reason': f"FLASH_EXIT(S{stage})"}
            
        # 2. Gap Down (Global) - [v4.5 Sync] Toggle Enforcement
        if self.use_gap_down_exit and (price / pos['avg_price'] - 1) * 100 <= self.gap_down_limit:
            return {'price': price, 'reason': f"GAP_DOWN"}

        # 3. Smart Exit (Tactical)
        if self.use_sx and indicators is not None and i is not None:
            bid_ratio = indicators['bid_ask_ratio'][i]
            v_power = indicators['vol_power'][i]
            # [v4.5 Audit] Smart Exit triggers on Bid Trap (High Bid/Ask) + Low Power
            if bid_ratio >= self.sx_ba and v_power < self.sx_vp:
                if self.sx_p_thr == 0 or price <= pos['max_price'] * (1 + self.sx_p_thr / 100.0):
                    return {'price': price, 'reason': f"SMART_EXIT"}

        # 4. ATR Exit (Granular)
        if self.use_atr and indicators is not None and i is not None:
            curr_atr = indicators['ATR'][i]
            entry_atr = pos.get('atr_at_entry', curr_atr)
            if curr_atr > 0:
                if stage == 2:
                    sl_mult = self.atr_sl_mult_s2
                    trail_mult = self.atr_trail_mult_s2
                    use_sl = self.use_atr_sl_s2
                    use_trail = self.use_atr_trail_s2
                else:
                    sl_mult = self.atr_sl_mult_b if is_type_b else self.atr_sl_mult_a
                    trail_mult = self.atr_trail_mult_b if is_type_b else self.atr_trail_mult_a
                    use_sl = self.use_atr_sl_b if is_type_b else self.use_atr_sl_a
                    use_trail = self.use_atr_trail_b if is_type_b else self.use_atr_trail_a

                if use_sl and price <= pos['avg_price'] - (sl_mult * entry_atr):
                    return {'price': price, 'reason': f"ATR_SL(S{stage})"}
                if use_trail and price <= pos['max_price'] - (trail_mult * curr_atr):
                    return {'price': price, 'reason': f"ATR_TRAIL(S{stage})"}

        # 5. Fixed TP/SL
        if stage == 1:
            # TP
            use_tp = self.use_s1b_tp_active if is_type_b else self.use_s1a_tp_active
            tp_pct = self.s1b_tp if is_type_b else self.s1a_tp
            if use_tp and (high / pos['avg_price'] - 1) >= tp_pct:
                return {'price': pos['avg_price'] * (1.0 + tp_pct), 'reason': f"TP_LIMIT(S1)"}
            # SL
            use_sl = self.use_s1b_sl_active if is_type_b else self.use_s1a_sl_active
            sl_pct = self.s1b_sl if is_type_b else self.s1a_sl
            if use_sl and (low / pos['avg_price'] - 1) <= -sl_pct:
                return {'price': pos['avg_price'] * (1.0 - sl_pct), 'reason': f"SL_LIMIT(S1)"}
        else:
            # TP
            if self.use_s2_tp_active and (high / pos['avg_price'] - 1) >= self.s2_tp:
                return {'price': pos['avg_price'] * (1.0 + self.s2_tp), 'reason': f"TP_LIMIT(S2)"}
            # SL
            if self.use_s2_sl and (low / pos['avg_price'] - 1) <= -self.s2_sl:
                return {'price': pos['avg_price'] * (1.0 - self.s2_sl), 'reason': f"SL_LIMIT(S2)"}
        
        return None

    def _check_exits(self, date, price, high, low, pos, indicators=None, i=None):
        return self._check_exits_optimized(date, price, high, low, pos, indicators=indicators, i=i)


    def _log_signal(self, date, code, name, price, indicators, i, mkt_trend, params):
        """신호 로그 기록 (SIGNAL_CSV 용 데이터 수집)"""
        params['signal_logs'].append({
            'Date': date, 'Code': code, 'Name': name,
            'Price': price, 'RSI': indicators['RSI'][i], 'MACD': indicators['MACD'][i],
            'RSI_Range': (indicators['RSI'][i] >= self.settings.get('buy_rsi_min', 40) and indicators['RSI'][i] <= self.settings.get('buy_rsi_max_b', 70)),
            'MACD_OK': (indicators['MACD'][i] >= indicators['MACD_Sig'][i]),
            'MA_GC': (indicators['MA5'][i] > indicators['MA20'][i]),
            'MA60_Range': (price / indicators['MA60'][i] >= 1.0 and price / indicators['MA60'][i] <= 1.12),
            'Vol_Surge': (indicators['Volume'][i] > indicators['Vol_MA20'][i] * 1.5),
            'Market_Trend': mkt_trend,
            'Final_Signal': indicators['Signal'][i]
        })
    
        return params


    def _execute_buy(self, date, price, action, params, stage=1, entry_idx=None, max_p=None, reason="", indicators=None, i=None, sig_type='TYPE_A'):
        """매수 실행 및 params 업데이트"""
        # [v27.0] 전략 타입(A/B/S2)을 매수 사유(Reason) 접두어로 명시하여 리포트 집계 정확도 향상
        if stage == 1:
            if not self.settings.get('use_type_b', False) and sig_type == 'TYPE_B':
                sig_type = 'TYPE_A'
            reason = f"[{sig_type}] {reason}"
        else:
            reason = f"[Stage 2] {reason}"
        if indicators is not None and i is not None:
            try:
                rsi_val = indicators['RSI'][i]
                ma5_val = indicators['MA5'][i]
                osc_val = indicators['OSC'][i]
                reason += f"|RSI:{rsi_val:.1f}|MA5:{int(ma5_val):,}|OSC:{osc_val:.2f}"
            except: pass

        amt = self.s1_amt if stage == 1 else self.s2_amt
        
        # [v33.0] Unified Sizing (Unit-based: % or 원)
        unit = self.settings.get('s1_sizing_unit' if stage == 1 else 's2_sizing_unit', "원")
        if unit == '%':
            total_equity = params['balance']
            if params['position'] is not None:
                total_equity += params['position']['qty'] * price
            
            s_pct = float(self.settings.get('s1_equity_pct' if stage == 1 else 's2_equity_pct', 10.0))
            amt = total_equity * (s_pct / 100.0)
        else:
            # 원화 고정 금액 (이미 상단에서 amt에 할당됨. 명시적으로 재할당 가능)
            amt = float(self.settings.get('stage1_amt' if stage == 1 else 'stage2_amt', 1_000_000))

        qty = int(amt // price)
        if qty <= 0: return params
        
        fee = int(price * qty * self.buy_f_rate)
        params['balance'] -= (qty * price + fee)
        
        if stage == 1:
            params['position'] = {
                'entry_date': date, 'entry_idx': entry_idx, 
                'avg_price': price, 'qty': qty, 'stage': 1, 'max_price': max_p,
                'sig_type': sig_type,
                'atr_at_entry': indicators['ATR'][i] if indicators is not None and i is not None else 0
            }
        else:
            old_q = params['position']['qty']
            pos = params['position']
            pos['avg_price'] = (pos['avg_price'] * old_q + price * qty) / (old_q + qty)
            pos['qty'] += qty
            pos['stage'] = 2

        curr_sig_type = params['position']['sig_type']
        self.collect_trade(date, action, price, qty, params['position']['qty'], params['position']['qty'] * price, reason_str=reason, fee=fee, sig_type=curr_sig_type)
        return params

    def _execute_sell(self, date, price, reason, params, indicators=None, i=None):
        """매도 실행 및 params 업데이트"""
        if indicators is not None and i is not None:
            try:
                rsi_val = indicators['RSI'][i]
                ma5_val = indicators['MA5'][i]
                osc_val = indicators['OSC'][i]
                if "RSI:" not in str(reason): # RSI 정보가 없을 때만 추가
                    prefix = "|" if "|" not in str(reason) else ", "
                    reason += f"{prefix}RSI:{rsi_val:.1f}|MA5:{int(ma5_val):,}|OSC:{osc_val:.2f}"
            except: pass
            
        pos = params['position']
        ret_v = (price / pos['avg_price'] - 1) * 100
        fee = int(price * pos['qty'] * self.sell_f_rate)
        
        entry_date = pos['entry_date']
        entry_sig = pos['sig_type']
        
        self.collect_trade(date, "SELL", price, pos['qty'], 0, 0, ret_val=ret_v, reason_str=reason, fee=fee, sig_type=entry_sig)
        if reason is None:
            reason = "SELL" # Default
        
        # [v4.0] Include stage for precise S1/S2 filtering
        params['history'].append({
            'date': date, 'type': 'SELL', 'price': price, 'ret': ret_v, 
            'reason': reason, 'sig_type': entry_sig, 'stage': pos.get('stage', 1)
        })
        params['balance'] += (pos['qty'] * price - fee)
        params['position'] = None
        return params

    def collect_trade(self, date, action, price, qty, total_qty, total_val, ret_val=0, reason_str="", fee=0, sig_type=None):
        self.trades.append({
            'Date': date, 'Action': action, 'Price': price, 'Qty': qty,
            'Total_Qty': total_qty, 'Total_Val': total_val, 'Return': ret_val, 
            'Reason': reason_str, 'Fee': fee, 'Sig_Type': sig_type
        })

    def _finalize_simulation(self, df, code, params, log_to_csv=False):
        """결과 정리 및 요약 생성"""
        equity_curve = params['equity_curve']
        position = params['position']
        balance = params['balance']

        if position is not None:
            final_time = df.index[-1]
            final_price = df['Close'].iloc[-1]
            sell_fee = int(final_price * position['qty'] * self.sell_f_rate)
            ret = (final_price / position['avg_price'] - 1) * 100
            last_row = df.iloc[-1]
            reason = f"Final|RSI:{last_row.get('RSI',0):.1f}|MA5:{int(last_row.get('MA5',0)):,}|OSC:{last_row.get('OSC',0):.2f}"
            self.collect_trade(final_time, "HOLD", final_price, position['qty'], position['qty'], 
                               position['qty'] * final_price, ret_val=ret, reason_str=reason, fee=sell_fee, sig_type=position['sig_type'])
            balance += (position['qty'] * final_price - sell_fee)
            equity_curve[-1] = balance

        df['Equity'] = equity_curve
        final_ret = (equity_curve[-1] / params['initial_balance'] - 1) * 100 if len(equity_curve) > 0 else 0
        
        peak = -1e10
        mdd = 0
        for val in equity_curve:
            if val > peak: peak = val
            dd = (val / peak - 1) * 100
            if dd < mdd: mdd = dd
            
        history = params['history']
        win_rate = len([h for h in history if h['ret'] > 0]) / len(history) * 100 if history else 0
        s2_count = len([t for t in self.trades if t['Action'] == 'BUY_2ND'])
        
        # [v35.8] Granular Averages for Type A/B
        # [v4.0] Granular Stage 1 A/B filtering (Excluding S2 via stage field and Reason string)
        s1_a_rets = [h['ret'] for h in history if "TYPE_A" in str(h.get('sig_type', '')) and h.get('stage', 1) == 1 and "[Stage 2]" not in str(h.get('reason', ''))]
        s1_b_rets = [h['ret'] for h in history if "TYPE_B" in str(h.get('sig_type', '')) and h.get('stage', 1) == 1 and "[Stage 2]" not in str(h.get('reason', ''))]
        
        avg_s1_a = sum(s1_a_rets) / len(s1_a_rets) if s1_a_rets else 0
        avg_s1_b = sum(s1_b_rets) / len(s1_b_rets) if s1_b_rets else 0
        
        # [v15.6] Write batch logs to files
        if log_to_csv:
            with csv_lock:
                if self.trades:
                    trade_df = pd.DataFrame(self.trades)
                    trade_df['Reason'] = trade_df['Reason'].astype(str).str.replace(',', ';')
                    trade_df['Code'] = code
                    trade_df['Name'] = params['stock_name']
                    trade_header = not os.path.exists(TRADE_CSV)
                    trade_df.to_csv(TRADE_CSV, mode='a', index=False, header=trade_header, encoding='utf-8-sig')
                
                if params['signal_logs']:
                    sig_df = pd.DataFrame(params['signal_logs'])
                    sig_header = not os.path.exists(SIGNAL_CSV)
                    sig_df.to_csv(SIGNAL_CSV, mode='a', index=False, header=sig_header, encoding='utf-8-sig')

        return {
            'code': code,
            'final_ret': final_ret,
            'mdd': mdd,
            'trades': len(params['history']),
            'win_rate': win_rate,
            's2_trades': s2_count,
            'avg_s1_a': avg_s1_a,
            'avg_s1_b': avg_s1_b,
            's1_a_rets': s1_a_rets,
            's1_b_rets': s1_b_rets,
            'history': params['history'],
            'final_equity': equity_curve[-1]
        }

    # _calculate_whipsaw_scale 메소드 제거 (pre-calculation으로 대체)

class PortfolioBacktester:
    """다중 종목 포트폴리오 백테스터 (v5.1)"""
    def __init__(self, settings, codes):
        self.settings = settings
        self.codes = codes
        self.loader = BacktestDataLoader()
        self.tester = SimpleBacktester(settings)
        
    def run_all(self, start_date, end_date, interval='1d', force_refresh=False, high_precision=False):
        """다중 종목 포트폴리오 백테스트 실행 (v16.0)"""
        start_date = pd.to_datetime(start_date).strftime('%Y-%m-%d')
        end_date = pd.to_datetime(end_date).strftime('%Y-%m-%d')
        
        # 1. 초기화
        self._reset_csv_logs()
        market_df, vix_df = self._prepare_market_data(start_date, end_date)
        
        # 2. 실행
        print(f"Starting Backtest for {len(self.codes)} stocks ({start_date} ~ {end_date}, {interval})")
        summaries, all_equity = self._run_parallel_backtests(start_date, end_date, market_df, vix_df, interval, force_refresh, high_precision)
        
        # 3. 결과 리포트 생성
        if summaries:
            self._generate_summary_reports(summaries, all_equity, market_df, vix_df, start_date, end_date, interval)
            return pd.DataFrame(summaries)
        return None

    def _reset_csv_logs(self):
        """기존 백테스트 로그 파일 초기화"""
        for f in [SIGNAL_CSV, TRADE_CSV]:
            if os.path.exists(f): 
                try: os.remove(f)
                except:
                    try: open(f, 'w').close()
                    except: pass

    def _prepare_market_data(self, start_date, end_date):
        """시장 지수 및 변동성 지수 데이터 준비"""
        print(f"  [API] Fetching Market Index & Volatility data...")
        w_start_dt = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=200)).strftime("%Y-%m-%d")
        
        try:
            import FinanceDataReader as fdr
            market_df = fdr.DataReader('KS11', w_start_dt, end_date)
            try:
                vix_df = fdr.DataReader('VIX', w_start_dt, end_date)
            except:
                try: vix_df = fdr.DataReader('FRED:VIXCLS', w_start_dt, end_date)
                except: 
                    vix_df = pd.DataFrame(index=market_df.index)
                    vix_df['Close'] = np.nan
            
            if market_df is not None:
                market_df.columns = [c.capitalize() if c.lower() == 'close' else c for c in market_df.columns]
                
            if vix_df is not None:
                if 'Close' not in vix_df.columns:
                    if not vix_df.empty:
                        vix_df.columns = ['Close'] + list(vix_df.columns[1:])
                    else:
                        vix_df['Close'] = np.nan
                vix_df.columns = [c.capitalize() if c.lower() == 'close' else c for c in vix_df.columns]
                vix_df = vix_df.reindex(market_df.index).ffill()
            return market_df, vix_df
        except Exception as e:
            print(f"  (!) Market/VIX Data Error: {e}")
            return None, None

    def _run_parallel_backtests(self, start_date, end_date, market_df, vix_df, interval, force_refresh, high_precision):
        """병렬로 개별 종목 백테스트 수행"""
        w_start_dt = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=200)).strftime("%Y-%m-%d")
        self.tester.settings['_original_start_dt'] = start_date
        
        data_cache_m1 = {}
        if high_precision:
            print(f"  [Perf] Pre-loading 1m data...")
            for code in self.codes:
                m1_data = self.loader.get_data(code, w_start_dt, end_date, interval='1m')
                if m1_data is not None: data_cache_m1[code] = m1_data

        summaries = []
        all_equity = pd.DataFrame()

        def _run_single(code):
            data = self.loader.get_data(code, w_start_dt, end_date, interval, force_refresh=force_refresh)
            if data is not None:
                # tester.run now modifies 'data' in place (adds 'Equity' column) and returns summary
                summary = self.tester.run(data, code, market_df=market_df, vix_df=vix_df, log_to_csv=True, high_precision=high_precision, m1_df_preloaded=data_cache_m1.get(code))
                return data, summary
            return None, None

        with TPE(max_workers=5) as executor:
            future_to_code = {executor.submit(_run_single, code): code for code in self.codes}
            for future in as_completed(future_to_code):
                code = future_to_code[future]
                try:
                    res_df, summary = future.result()
                    if summary:
                        summaries.append(summary)
                        if res_df is not None and 'Equity' in res_df.columns:
                            all_equity[code] = res_df['Equity']
                except Exception as e:
                    print(f"  (!) Error backtesting {code}: {e}")
        return summaries, all_equity

    def _generate_summary_reports(self, summaries, all_equity, market_df, vix_df, start_date, end_date, interval):
        """최종 통계 출력 및 리포트 생성"""
        df_sum = pd.DataFrame(summaries)
        
        # Portfolio All Equity Curve (Total)
        if not all_equity.empty:
            all_equity['Total'] = all_equity.sum(axis=1) / len(self.codes)
            all_equity[['Total']].to_csv("backtest_portfolio_curve.csv", encoding='utf-8-sig')

        print("\n" + "="*40)
        print(f"Portfolio Backtest Results ({len(summaries)} stocks)")
        print(f" - Avg Return: {df_sum['final_ret'].mean():+.2f}%")
        print(f" - Avg Win Rate: {df_sum['win_rate'].mean():.1f}%")
        
        # [v4.0] True Global Stage 1 Averages (Aggregated across ALL stocks)
        all_s1_a = [r for s in summaries for r in s.get('s1_a_rets', [])]
        all_s1_b = [r for s in summaries for r in s.get('s1_b_rets', [])]
        
        grand_avg_s1_a = sum(all_s1_a) / len(all_s1_a) if all_s1_a else 0
        grand_avg_s1_b = sum(all_s1_b) / len(all_s1_b) if all_s1_b else 0
        
        print(f" - S1 Type A Avg: {grand_avg_s1_a:+.2f}% ({len(all_s1_a)} trades)")
        print(f" - S1 Type B Avg: {grand_avg_s1_b:+.2f}% ({len(all_s1_b)} trades)")
        print("="*40)
        
        # [v4.0] Overwrite per-stock averages with global grand averages for Optimizer compatibility
        if summaries:
            for s in summaries:
                s['avg_s1_a'] = grand_avg_s1_a
                s['avg_s1_b'] = grand_avg_s1_b
        
        df_sum.to_csv("backtest_summary_multi.csv", index=False, encoding='utf-8-sig')
        
        # Reports (signal_report DELETED)
        trd_path = os.path.join(BASE_DIR, "trade_report.html")
        
        trades_df = pd.DataFrame()
        if os.path.exists(TRADE_CSV):
            try:
                trades_df = pd.read_csv(TRADE_CSV, dtype={'Code': str})
                trades_df['Date'] = pd.to_datetime(trades_df['Date'])
            except: pass
            
        daily_equity_list = [{'date': start_date + " 08:59:00", 'equity': 100.0, 'cash': 100.0}]
        if not all_equity.empty and 'Total' in all_equity.columns:
            for d, val in all_equity['Total'].items():
                daily_equity_list.append({'date': d.strftime('%Y-%m-%d') + " 15:30:00", 'equity': val, 'cash': val}) 
        
        generate_trade_report(trades_df, interval, trd_path, len(summaries), 
                              df_sum['win_rate'].mean(), df_sum['final_ret'].mean(), self.settings,
                              market_df=market_df, vix_df=vix_df, target_codes=self.codes,
                              start_date=start_date, end_date=end_date, daily_equity=daily_equity_list,
                              high_precision=False)





if __name__ == "__main__":
    # 통합 테스트용 샘플 (v6.4: 산일전기 062040 추가)
    sample_codes = ['005930', '000660', '005380', '055550', '051910', '012450', '035420', '015760', '028260', '009150']
    
    # 신호가 잘 발생하도록 완화된 설정 사용 (검증용)
    lenient_settings = {
        'buy_rsi_min': 30, 'buy_rsi_max_b': 65,
        'ma60_min': 90, 'ma60_max': 120,
        'vol_surge': 1.0,
        'macd_fast_entry': 12, 'macd_slow_entry': 26, 'macd_sig_entry': 9,
        'stage1_amt': 900000, 'stage2_amt': 2100000,
        'use_s2_ma5': True, 'use_s2_profit': True, 'use_s2_osc': True,
        'use_s2_rsi_sig': True,
        'take_profit': 5.0, 'stop_loss': 3.0, 'rsi_exit': 50.0
    }
    
    start = "2024-01-01"
    end = "2024-12-31"
    
    # 포트폴리오 백테스트 실행 예시
    tester = PortfolioBacktester(lenient_settings, sample_codes)
    tester.run_all(start, end)
