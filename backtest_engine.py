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
from reporting.signal_report import generate_signal_report
from reporting.charts import generate_stock_market_chart
import reporting.trade_report
import importlib
importlib.reload(reporting.trade_report)
from reporting.trade_report import generate_trade_report
from strategy import indicators, signals

csv_lock = threading.Lock()
# [v15.3.7] Script-relative absolute paths for robust log management
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SIGNAL_CSV = os.path.join(BASE_DIR, "backtest_signals.csv")
TRADE_CSV = os.path.join(BASE_DIR, "backtest_trades.csv")
print(f"DEBUG [backtest_engine]: BASE_DIR={BASE_DIR}")
print(f"DEBUG [backtest_engine]: TRADE_CSV={TRADE_CSV}")

class TradingStrategy:
    """[v6.4] 실시간 감시와 백테스팅이 공유하는 매매 전략 엔진"""
    _name_map = None
    
    @staticmethod
    def get_stock_name_map():
        """종목코드 -> 종목명 맵핑 로드 (v6.3)"""
        if TradingStrategy._name_map is not None:
            return TradingStrategy._name_map
            
        csv_path = "kospi_200.csv"  # [v30.5 Fixed] kospi_top_500.csv -> kospi_200.csv
        try:
            if not os.path.exists(csv_path):
                # 다른 kospi_*.csv 파일이 있는지 확인
                import glob
                files = glob.glob("kospi_*.csv")
                if files:
                    csv_path = files[0]
                else:
                    TradingStrategy._name_map = {}
                    return {}
            
            if os.path.exists(csv_path):
                # Unnamed: 0,Code,ISU_CD,Name ...
                df_name = pd.read_csv(csv_path, dtype={'Code': str}, encoding='utf-8')
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
        return indicators.calculate_all_indicators(df, settings)

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
    def _check_s2_approval(pos, last_row, settings, indicator_row=None):
        """[v22.2] 설정창의 7가지 조건과 100% 동기화된 2차 매수 승인 로직 (Static)"""
        s = settings
        
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
            vol_ma20 = ind.get('Vol_MA20', ind.get('Volume', 0))
            ratio = float(s.get('s2_vol_ratio', 1.0))
            if ind.get('Volume', 0) < vol_ma20 * ratio:
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
            trigger = float(s.get('stage2_trigger', s.get('s2_profit_limit', 1.0)))
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
            
            print(f"  [Sim] Strategy Run: {code} | Data: {len(df)} rows | HighPrec: {high_precision}")
            
            params = self._init_simulation(df, code, market_df, vix_df, high_precision)
            
            # 2. Main Simulation Loop (v23.0 Optimized)
            # [Perf] 미리 벡터화된 신호 및 가격 정보를 로컬 변수로 캐싱하여 루프 부하 최적화
            close_vals = df['Close'].values
            high_vals = df['High'].values
            low_vals = df['Low'].values
            time_indices = df.index
            
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
                # Convert 1M to numpy for zero-copy access
                m1_np_high = m1_df_preloaded['High'].values
                m1_np_close = m1_df_preloaded['Close'].values
                m1_np_time = m1_df_preloaded.index.values # For exit time lookup
            else:
                m1_boundaries = None
            
            # 신호 및 필터 미리 정리
            signal_shifted = df['Signal_Shifted'].values if 'Signal_Shifted' in df.columns else np.zeros(len(df), dtype=bool)
            
            total_bars = len(df)
            for i in range(total_bars):
                # i가 0이면 이전 바가 없으므로 건너뛰
                if i == 0: 
                    params['equity_curve'][0] = params['balance']
                    continue
                
                # [v30.7] Pass pre-calculated m1_chunk info to avoid slicing overhead
                m1_info = (m1_np_high, m1_np_close, m1_np_time, m1_boundaries[i]) if m1_boundaries else None
                params = self._sim_loop_step_v2(i, close_vals, high_vals, low_vals, time_indices, signal_shifted, df, code, m1_info, high_precision, params, log_to_csv)
                params['equity_curve'][i] = params['balance'] + (params['position']['qty'] * close_vals[i] if params['position'] else 0)
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
        max_slots = self.settings.get('max_slots', 10)
        total_capital_val = self.settings.get('total_capital', 30000000)
        slot_budget = total_capital_val / max_slots
        
        self.s1_amt = self.settings.get('stage1_amt', slot_budget * 0.3)
        self.s2_amt = self.settings.get('stage2_amt', slot_budget * 0.7)
        self.buy_f_rate = self.settings.get('buy_fee_rate', 0.015) / 100.0
        self.sell_f_rate = self.settings.get('sell_fee_rate', 0.195) / 100.0
        
        stock_name_map = TradingStrategy.get_stock_name_map()
        stock_name = stock_name_map.get(code, code)

        # 시장 상태 계산
        if market_df is not None and not market_df.empty:
            m_df = TradingStrategy.calculate_indicators(market_df.copy(), {})
            # [Fix] Align VIX with market index to prevent shape mismatch (broadcast error)
            vix_s = vix_df['Close'].reindex(m_df.index).ffill().fillna(15.0) if vix_df is not None and 'Close' in vix_df.columns else pd.Series(15.0, index=m_df.index)
            
            m_df['Status'] = 'WATCH'
            m_df.loc[(m_df['MSI'] > 0) & (m_df['ADX'] >= 25) & (vix_s <= 20), 'Status'] = 'AGGRESSIVE'
            m_df.loc[(m_df['MSI'] > 0) & (vix_s <= 20) & (m_df['Status'] == 'WATCH'), 'Status'] = 'SAFE'
            m_df.loc[(m_df['MSI'] < -0.05) | (vix_s >= 30), 'Status'] = 'RISK'
            
            # [v26.9] Triple-Lock Safety System (MSI -0.05, VIX 30, ADX 15)
            # Market is 'DOWN' (Blocked) if ANY of these trigger:
            lock_msi = m_df['MSI'] < -0.05
            lock_vix = vix_s > 30
            lock_adx = m_df['ADX'] < 15
            triple_lock_down = lock_msi | lock_vix | lock_adx
            
            m_df['Trend'] = np.where(triple_lock_down, 'DOWN', 
                                     np.where(m_df['MSI'] > 0, 'UP', 'NEUTRAL'))
            
            # [Realism] Today's market trend is determined by yesterday's close data to avoid look-ahead bias
            m_df['Status'] = m_df['Status'].shift(1).fillna('WATCH')
            m_df['Trend'] = m_df['Trend'].shift(1).fillna('UP')
            
            df_daily_idx = df.index.normalize()
            df['Market_Status'] = df_daily_idx.map(m_df['Status']).fillna('WATCH')
            df['Market_Trend'] = df_daily_idx.map(m_df['Trend']).fillna('UP')
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
        df.loc[df['Market_Trend'] == 'DOWN', 'Signal'] = False
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

    def _sim_loop_step_v2(self, i, close_vals, high_vals, low_vals, time_indices, signal_shifted, df, code, m1_info, high_precision, params, log_to_csv=False):
        """[v23.1] 로직 무결성이 보장된 고속 시뮬레이션: 2차 매수 및 모든 익절/손절 조건 포함"""
        curr_close = close_vals[i]
        curr_date = time_indices[i]
        curr_high = high_vals[i]
        curr_low = low_vals[i]
        
        # [v30.7] Optimized High Precision Exit Monitoring (Using pre-calculated NumPy slices)
        if high_precision and m1_info is not None and params['position'] is not None:
             try:
                m1_high_total, m1_close_total, m1_time_total, (s_idx, e_idx) = m1_info
                if e_idx > s_idx:
                    m1_highs = m1_high_total[s_idx:e_idx]
                    m1_closes = m1_close_total[s_idx:e_idx]
                    # Pass numpy arrays correctly to optimized function
                    params = self._process_intra_bar_exit_only(i, df, code, m1_highs, m1_closes, m1_time_total[s_idx:e_idx], params, log_to_csv)
                    if params['position'] is None:
                        return params
             except Exception as e:
                if i % 100 == 0:
                    print(f"  [Error] Intra-bar error ({code} @ {curr_date}): {e}")

        # Standard Sim (v23.1 Full Logic)
        if params['position'] is not None:
            pos = params['position']
            if curr_high > pos['max_price']: pos['max_price'] = curr_high
            
            # 1. Exit Check (TP/SL/Flash/Gap)
            exit_res = self._check_exits(curr_date, curr_close, curr_high, curr_low, pos)
            if exit_res:
                params = self._execute_sell(curr_date, exit_res['price'], exit_res['reason'], params, indicator_row=df.iloc[i])
            
            # 2. Stage 2 Buy Check (Secondary)
            elif pos['stage'] == 1:
                row = df.iloc[i]
                if TradingStrategy._check_s2_approval(pos, row, self.settings):
                    # [v22.1] 2차 매입 사유 상세 기록 (지표 상태 포함)
                    s2_reason = f"ADDON(S2)|R:{row.get('RSI',0):.1f}|M:{row.get('MACD',0):.1f}|O:{row.get('OSC',0):.1f}|V:{row.get('Volume',0)}"
                    params = self._execute_buy(curr_date, curr_close, "BUY_2ND", params, stage=2, reason=s2_reason, indicator_row=row)
        
        # 3. Entry Check (Initial)
        elif signal_shifted[i]:
            row = df.iloc[i]
            mkt_status = row.get('Market_Status', 'SAFE')
            is_ok = (mkt_status != 'RISK')
            # Type A 신호 필터링 (신호 발생봉 무지연 체크)
            if mkt_status == 'WATCH' and row.get('Sig_Type') != 'TYPE_A': is_ok = False
            
            if is_ok:
                params = self._execute_buy(curr_date, close_vals[i], "BUY_1ST", params, stage=1, entry_idx=i, max_p=curr_high, reason="SIG", indicator_row=row, sig_type=row.get('Sig_Type', 'TYPE_B'))
            
        return params

    def _process_intra_bar_exit_only(self, i, df, code, m1_highs, m1_closes, m1_times, params, log_to_csv):
        """[v30.7] Vectorized Intra-bar Exit check using pre-sliced NumPy arrays (Zero Pandas overhead)"""
        prev_row_base = df.iloc[i-1] if i > 0 else df.iloc[i]
        
        pos = params['position']
        sig_type = pos.get('sig_type', 'TYPE_A')
        
        # Cache threshold parameters outside the loop
        suffix = "_a" if sig_type == "TYPE_A" else "_b"
        if pos['stage'] == 1:
            f_pct = float(self.settings.get(f's1{suffix}_flash_exit_pct', 3.0)) if self.settings.get('use_s1_flash_exit', True) else 0.0
            sl_pct = float(self.settings.get(f's1{suffix}_stop_loss', 3.0)) if self.settings.get('use_s1_stop_loss', True) else 0.0
            tp_pct = float(self.settings.get(f's1{suffix}_take_profit', 5.0)) if self.settings.get('use_s1_take_profit', True) else 0.0
        else:
            f_pct = float(self.settings.get('s2_flash_exit_pct', 3.0)) if self.settings.get('use_s2_flash_exit', True) else 0.0
            sl_pct = float(self.settings.get('s2_stop_loss', 2.0)) if self.settings.get('use_s2_stop_loss', True) else 0.0
            tp_pct = float(self.settings.get('s2_take_profit', 5.0)) if self.settings.get('use_s2_take_profit', True) else 0.0
        
        g_lmt = float(self.settings.get('gap_down_limit', -5.0))
        
        prev_max = pos.get('max_price', pos['avg_price'])
        # Vectorized monitoring
        running_max = np.maximum.accumulate(m1_highs)
        running_max = np.maximum(running_max, prev_max)
        
        avg_price = pos['avg_price']
        
        flash_exited = (f_pct > 0) & (m1_closes < running_max * (1 - f_pct/100))
        stop_lossed = (sl_pct > 0) & (m1_closes / avg_price - 1 <= -sl_pct/100)
        take_profited = (tp_pct > 0) & (m1_highs / avg_price - 1 >= tp_pct/100)
        gap_downed = (m1_closes / avg_price - 1 <= g_lmt / 100)
        
        # [v25.8/v25.9] 15-minute Gap-Down Grace Period: Allow recovery between SL/Flash and Gap Limit during first 15m
        if self.settings.get('use_gap_grace', True):
            m1_t = pd.DatetimeIndex(m1_times)
            # Check if this is the opening 15m bar of the day (09:00)
            if m1_t[0].hour == 9 and m1_t[0].minute == 0:
                # In the morning (09:00-09:15), only GapDown can trigger if price is between boundaries
                grace_mask = (m1_t.hour == 9).to_numpy() & (m1_t.minute < 15).to_numpy()
                stop_lossed = stop_lossed & (~grace_mask)
                flash_exited = flash_exited & (~grace_mask)
        
        exit_mask = flash_exited | stop_lossed | take_profited | gap_downed
        exit_idxs = np.where(exit_mask)[0]
        
        if len(exit_idxs) > 0:
            idx = int(exit_idxs[0])
            e_price, e_time = m1_closes[idx], m1_times[idx]
            if bool(take_profited[idx]): e_price = avg_price * (1 + tp_pct/100)
            
            reason = "1M_SL"
            if bool(gap_downed[idx]): reason = "1M_GapDown"
            elif bool(flash_exited[idx]): reason = "1M_FlashExit"
            elif bool(take_profited[idx]): reason = "1M_TP"
            
            params = self._execute_sell(e_time, e_price, reason, params, indicator_row=prev_row_base)
            
        return params

    def _process_standard_bar(self, i, df, code, params, log_to_csv=False):
        """15분봉 기반 표준 시나리오 시뮬레이션"""
        row = df.iloc[i]
        curr_date = df.index[i]
        curr_open, curr_high, curr_low, curr_close = row['Open'], row['High'], row['Low'], row['Close']

        if params['position'] is not None:
            pos = params['position']
            if curr_high > pos['max_price']: pos['max_price'] = curr_high
            
            # 익절/손절 체크 (봉 단위)
            exit_res = self._check_exits(curr_date, curr_close, curr_high, curr_low, pos)
            if exit_res:
                params = self._execute_sell(curr_date, exit_res['price'], exit_res['reason'], params, indicator_row=row)
            elif pos['stage'] == 1:
                if TradingStrategy._check_s2_approval(pos, row, self.settings): 
                    # [v22.1] 2차 매입 사유 상세 기록 (지표 상태 포함)
                    s2_reason = f"ADDON(S2)|R:{row.get('RSI',0):.1f}|M:{row.get('MACD',0):.1f}|O:{row.get('OSC',0):.1f}|V:{row.get('Volume',0)}"
                    params = self._execute_buy(curr_date, curr_close, "BUY_2ND", params, stage=2, reason=s2_reason, indicator_row=row)
        elif row.get('Signal_Shifted', False):
            mkt_status = row.get('Market_Status', 'SAFE')
            sig_type = row.get('Sig_Type', 'TYPE_B')
            is_ok = (mkt_status != 'RISK')
            if mkt_status == 'WATCH' and sig_type != 'TYPE_A': is_ok = False
            
            if is_ok:
                params = self._execute_buy(curr_date, curr_open, "BUY_1ST", params, stage=1, entry_idx=i, max_p=curr_high, reason="SIG", indicator_row=row, sig_type=sig_type)
        
        # [v15.6] Log ALL candidate signals for Signal Report
        if log_to_csv and not self.settings.get('skip_indicators', False):
            if row.get('Signal', False) or (row.get('RSI', 0) > 30 and row.get('RSI', 0) < 80):
                self._log_signal(curr_date, code, params['stock_name'], curr_close, row, row, row.get('Market_Trend', 'UP'), params)
        
        return params

    def _log_signal(self, date, code, name, price, indicators, signal_row, mkt_trend, params):
        """신호 로그 기록 (SIGNAL_CSV 용 데이터 수집)"""
        params['signal_logs'].append({
            'Date': date, 'Code': code, 'Name': name,
            'Price': price, 'RSI': indicators.get('RSI'), 'MACD': indicators.get('MACD'),
            'RSI_Range': (indicators.get('RSI', 0) >= self.settings.get('buy_rsi_min', 40) and indicators.get('RSI', 0) <= self.settings.get('buy_rsi_max_b', 70)),
            'MACD_OK': (indicators.get('MACD', 0) >= indicators.get('MACD_Sig', 0)),
            'MA_GC': (indicators.get('MA5', 0) > indicators.get('MA20', 0)),
            'MA60_Range': (price / indicators.get('MA60', 1) >= 1.0 and price / indicators.get('MA60', 1) <= 1.12),
            'Vol_Surge': (indicators.get('Volume', 0) > indicators.get('Vol_MA20', 1) * 1.5),
            'Market_Trend': mkt_trend,
            'Final_Signal': signal_row.get('Signal', False)
        })
    
        return params

    def _check_exits(self, date, price, high, low, pos):
        """Flash Exit, Gap Down, TP/SL 조건 체크 (v26.0: Type A/B 분리 대응)"""
        # 0. 공통 상수 및 설정 로드
        s = self.settings
        
        # 진입 유형에 따른 접미사 결정 (v26.4: Type B 비활성화 시 강제로 _a 사용 연동)
        use_b = s.get('use_type_b', False)
        sig_type = pos.get('sig_type', 'TYPE_A')
        
        if not use_b:
            suffix = "_a"
        else:
            suffix = "_b" if sig_type == "TYPE_B" else "_a"

        # 1. Flash Exit
        if pos['stage'] == 1:
            f_pct = float(s.get(f's1{suffix}_flash_exit_pct', 3.0)) / 100.0
            if price <= pos['max_price'] * (1.0 - f_pct):
                drop_pct = (price / pos['max_price'] - 1) * 100
                return {'price': price, 'reason': f"FLASH_EXIT(S1-{sig_type.split('_')[-1]})|Drop:{drop_pct:.1f}%"}
        else:
            f_pct = float(s.get('s2_flash_exit_pct', 3.0)) / 100.0
            if price <= pos['max_price'] * (1.0 - f_pct):
                drop_pct = (price / pos['max_price'] - 1) * 100
                return {'price': price, 'reason': f"FLASH_EXIT(S2)|Drop:{drop_pct:.1f}%"}
            
        # 2. Gap Down Limit (S1/S2 공통)
        g_lmt = float(s.get('gap_down_limit', -5.0))
        if (price / pos['avg_price'] - 1) * 100 <= g_lmt:
            ret_pct = (price / pos['avg_price'] - 1) * 100
            return {'price': price, 'reason': f"GAP_DOWN|Ret:{ret_pct:.1f}%"}

        # 3. Take Profit
        if pos['stage'] == 1:
            tp_pct = float(s.get(f's1{suffix}_take_profit', 5.0)) / 100.0
        else:
            tp_pct = float(s.get('s2_take_profit', 5.0)) / 100.0
            
        if (high / pos['avg_price'] - 1) >= tp_pct:
            return {'price': pos['avg_price'] * (1.0 + tp_pct), 'reason': f"TP_LIMIT(S{pos['stage']})"}

        # 4. Stop Loss
        if pos['stage'] == 1:
            if s.get('use_s1_stop_loss', True):
                sl_pct = float(s.get(f's1{suffix}_stop_loss', 3.0)) / 100.0
                if (price / pos['avg_price'] - 1) <= -sl_pct:
                    return {'price': price, 'reason': f"STOP_LOSS(S1-{sig_type.split('_')[-1]})"}
        else:
            if s.get('use_s2_stop_loss', True):
                sl_pct = float(s.get('s2_stop_loss', 2.0)) / 100.0
                if (price / pos['avg_price'] - 1) <= -sl_pct:
                    return {'price': price, 'reason': f"STOP_LOSS(S2)"}
            
        return None

    def _execute_buy(self, date, price, action, params, stage=1, entry_idx=None, max_p=None, reason="", indicator_row=None, sig_type='TYPE_A'):
        """매수 실행 및 params 업데이트"""
        # [v27.0] 전략 타입(A/B/S2)을 매수 사유(Reason) 접두어로 명시하여 리포트 집계 정확도 향상
        if stage == 1:
            if not self.settings.get('use_type_b', False) and sig_type == 'TYPE_B':
                sig_type = 'TYPE_A'
            reason = f"[{sig_type}] {reason}"
        else:
            reason = f"[Stage 2] {reason}"
        if indicator_row is not None:
            try:
                rsi_val = indicator_row.get('RSI', 0)
                ma5_val = indicator_row.get('MA5', 0)
                osc_val = indicator_row.get('OSC', 0)
                reason += f"|RSI:{rsi_val:.1f}|MA5:{int(ma5_val):,}|OSC:{osc_val:.2f}"
            except: pass

        amt = self.s1_amt if stage == 1 else self.s2_amt
        qty = int(amt // price)
        if qty <= 0: return params
        
        fee = int(price * qty * self.buy_f_rate)
        params['balance'] -= (qty * price + fee)
        
        if stage == 1:
            params['position'] = {
                'entry_date': date, 'entry_idx': entry_idx, 
                'avg_price': price, 'qty': qty, 'stage': 1, 'max_price': max_p,
                'sig_type': sig_type
            }
        else:
            old_q = params['position']['qty']
            pos = params['position']
            pos['avg_price'] = (pos['avg_price'] * old_q + price * qty) / (old_q + qty)
            pos['qty'] += qty
            pos['stage'] = 2

        self.collect_trade(date, action, price, qty, params['position']['qty'], params['position']['qty'] * price, reason_str=reason, fee=fee)
        return params

    def _execute_sell(self, date, price, reason, params, indicator_row=None):
        """매도 실행 및 params 업데이트"""
        if indicator_row is not None:
            try:
                rsi_val = indicator_row.get('RSI', 0)
                ma5_val = indicator_row.get('MA5', 0)
                osc_val = indicator_row.get('OSC', 0)
                if "RSI:" not in str(reason): # RSI 정보가 없을 때만 추가
                    prefix = "|" if "|" not in str(reason) else ", "
                    reason += f"{prefix}RSI:{rsi_val:.1f}|MA5:{int(ma5_val):,}|OSC:{osc_val:.2f}"
            except: pass
            
        pos = params['position']
        ret_v = (price / pos['avg_price'] - 1) * 100
        fee = int(price * pos['qty'] * self.sell_f_rate)
        
        self.collect_trade(date, "SELL", price, pos['qty'], 0, 0, ret_val=ret_v, reason_str=reason, fee=fee)
        params['balance'] += (pos['qty'] * price - fee)
        params['history'].append({'entry': pos['entry_date'], 'exit': date, 'ret': ret_v})
        params['position'] = None
        return params

    def collect_trade(self, date, action, price, qty, total_qty, total_val, ret_val=0, reason_str="", fee=0):
        self.trades.append({
            'Date': date, 'Action': action, 'Price': price, 'Qty': qty,
            'Total_Qty': total_qty, 'Total_Val': total_val, 'Return': ret_val, 
            'Reason': reason_str, 'Fee': fee
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
                               position['qty'] * final_price, ret_val=ret, reason_str=reason, fee=sell_fee)
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
        print("="*40)
        
        df_sum.to_csv("backtest_summary_multi.csv", index=False, encoding='utf-8-sig')
        
        # Reports
        sig_path = os.path.join(BASE_DIR, "signal_report.html")
        trd_path = os.path.join(BASE_DIR, "trade_report.html")
        generate_signal_report(output_path=sig_path, target_codes=self.codes)
        
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
        'sig_macd_fast': 12, 'sig_macd_slow': 26, 'sig_macd_sig': 9,
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
