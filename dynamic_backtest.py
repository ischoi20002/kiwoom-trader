import pandas as pd
import os
import time
from datetime import datetime, timedelta
from data_manager_v2 import DataManagerV2
import strategy.signals
import strategy_selector
import backtest_engine as bt
import importlib
importlib.reload(strategy.signals) # [Force] 시그널 로직 업데이트 반영
importlib.reload(strategy_selector) 
importlib.reload(bt) 
import numpy as np
from backtest_engine import TradingStrategy, csv_lock, SIGNAL_CSV, TRADE_CSV, BASE_DIR
from strategy.simulation_models import SimulatedRealtimeMetrics

class DynamicBacktester:
    """[v16.0] 동적 포트폴리오 백테스터: 매일 종목을 새로 선정하고 1,000만원 예산을 공유하여 운용"""
    def __init__(self, settings, total_budget=None):
        self.settings = settings
        self.db = DataManagerV2("trading_data.db")
        # [v16.7] 설정에서 예산 가져오기 (기본값 1000만)
        budget = total_budget if total_budget is not None else float(settings.get('backtest_initial_capital', 10_000_000))
        self.initial_cash = budget
        self.cash = budget
        # [v17.4] 매매 비용 설정 (기본값: 매수 0.015%, 매도 0.195%)
        # [v17.5] 수수료/세금 설정 (기본값 방어)
        self.buy_f_rate = settings.get('buy_fee_rate', 0.015) / 100.0 if settings else 0.00015
        self.sell_f_rate = settings.get('sell_fee_rate', 0.195) / 100.0 if settings else 0.00195
        self.holdings = {} # {code: {'qty': Q, 'avg_p': P, 'entry_n': N, 'max_p': H, 'min_p': L}}
        self.trades_history = []
        self.daily_equity = []
        self.cached_15m_data = {} # Step 3 (Phase 2) initialized as empty
        self.start_date = None
        self.end_date = None
        self.high_precision = False  # [v20.0] 고정밀 모드 플래그
        self.cached_1m_data = {}     # [v20.0] 1분봉 데이터 캐시
        
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
        self.use_s1a_tp = True
        self.use_s1b_tp = True
        self.use_s1a_sl = True
        self.use_s1b_sl = True
        self.use_s1a_flash = True
        self.use_s1b_flash = True
        self.use_s2_flash = True
        self.use_atr = False
        self.use_type_b = False
        self.gap_down_lmt = -5.0
        self.stock_name_map = {}
        
    def run(self, start_date, end_date, interval='15m', callback=None, high_precision=False):
        """메인 시뮬레이션 루프 (일단위 유니버스 갱신 + 분단위/15분단위 체결)"""
        self.high_precision = high_precision
        # [v18.8] Date Normalization (YYYY-MM-DD)
        start_date = pd.to_datetime(start_date).strftime('%Y-%m-%d')
        end_date = pd.to_datetime(end_date).strftime('%Y-%m-%d')
        
        self.start_date = start_date
        self.end_date = end_date
        
        # [Perf] Cache settings and maps once to avoid lookups in hot loops (v33.8)
        s = self.settings
        self.use_sb_a = self.settings.get('use_smart_buy_a', False)
        self.use_sb_b = self.settings.get('use_smart_buy_b', False)
        self.use_sb_s2 = self.settings.get('use_smart_buy_s2', False)
        self.sb_ba = float(s.get('smart_buy_ba_ratio', 1.2))
        self.sb_vp = float(s.get('smart_buy_vp', 110.0))
        self.use_sx = s.get('use_smart_exit', False)
        self.sx_ba = float(s.get('smart_exit_ba_ratio', 0.8))
        self.sx_vp = float(s.get('smart_exit_vp', 90.0))
        self.sx_p_thr = float(s.get('smart_exit_price_threshold', 0.0))
        
        self.use_s1a_tp = s.get('use_s1_a_tp', True)
        self.use_s1b_tp = s.get('use_s1_b_tp', True)
        self.use_s1a_sl = s.get('use_s1_a_stop_loss', True)
        self.use_s1b_sl = s.get('use_s1_b_stop_loss', True)
        self.use_s2_tp = s.get('use_s2_tp', True)
        self.use_s2_sl = s.get('use_s2_stop_loss', True)
        self.use_s1a_flash = s.get('use_s1_a_flash_exit', True)
        self.use_s1b_flash = s.get('use_s1_b_flash_exit', True)
        self.use_s2_flash = s.get('use_s2_flash_exit', True)
        self.use_atr = s.get('use_atr_exit', False)
        self.use_type_b = s.get('use_type_b', False)
        self.gap_down_lmt = float(s.get('gap_down_limit', -5.0))
        
        self.stock_name_map = TradingStrategy.get_stock_name_map()
        
        if callback: callback("백테스트 준비 중...", 0)
        
        # 1. 기초 데이터 (지수 및 VIX) 로드
        if callback: callback("시장지수 및 VIX 데이터 로드 중...", 2)
        market_df = self.db.get_daily_bars('001', start_date, end_date)
        vix_df = self.db.get_daily_bars('^VIX', start_date, end_date)
        if market_df is None or market_df.empty:
            print("[Error] [Dynamic] 시장 데이터(KS11)가 없어 백테스트를 진행할 수 없습니다.")
            return None
            
        market_df['time'] = pd.to_datetime(market_df['time'])
        market_df.set_index('time', inplace=True)
        
        # [v4.5] 지수 지표 계산 (MA60, MSI 등 포함)
        market_df = TradingStrategy.calculate_indicators(market_df, self.settings)
        
        trading_days = market_df.index.strftime('%Y-%m-%d').tolist()
        
        print(f"[Dynamic] 백테스트 시작: {start_date} ~ {end_date} ({len(trading_days)} 영업일)")
        
        # [v17.7] 성능 핵심 최적화 (Phase 1): 전 기간 유니우스 사전 계산 (Batch)
        import time
        t_start = time.time()
        universe_schedule = strategy_selector.prepare_dynamic_universe(
            self.settings, start_date, end_date, self.db, callback if callback else print
        )
        t_duration = time.time() - t_start
        msg = f"사전 계산 완료 ({t_duration:.2f}초)"
        print(f"[Done] {msg}")
        if callback: callback(msg, 5)

        # [v17.7] 성능 핵심 최적화 (Phase 2): 전 기간 15분봉 지표 사전 계산 (Batch)
        if not universe_schedule:
            if callback: callback(f"  [Warning] 선택된 종목이 없습니다. 사전 계산(Phase 2)을 건너뜁니다.", 0)
            self.cached_15m_data = {}
        else:
            self.cached_15m_data = self._prepare_15m_cached_data(universe_schedule, start_date, end_date, callback)
        
        # [v23.0] 고정밀 모사용 1분봉 데이터는 이제 '지연 로딩(Lazy Loading)' 방식으로 처리 (성능 최적화)
        self.cached_1m_data = {} # (code, date_str) -> DataFrame
        
        # [v17.3] 시작 시점(Day 0) 자산 기록 (트레이딩 시작 전일 혹은 시작일 아침으로 기록하여 중복 방지)
        # [v18.9] Plotly date parsing 호환성을 위해 유효한 시간 형식(08:59) 사용
        self.daily_equity = [{'date': start_date + " 08:59:00", 'equity': self.initial_cash, 'cash': self.initial_cash, 'holdings_n': 0}]
        
        # 신호 기록 초기화
        for f in [SIGNAL_CSV, TRADE_CSV]:
            if os.path.exists(f): 
                try: os.remove(f)
                except: pass

        # 2. 일단위 루프
        for idx, current_day in enumerate(trading_days):
            day_t_start = time.time()
            progress = (idx / len(trading_days)) * 100
            if callback: callback(f"[{current_day}] 시계열 모의투자 중...", progress)
            
            # 2-1. 오전 종목 선정 (사전 계산된 스케줄 활용)
            prev_day_idx = idx - 1
            if prev_day_idx >= 0:
                prev_day_str = trading_days[prev_day_idx]
                top_codes = universe_schedule.get(prev_day_str, [])
            else:
                top_codes = universe_schedule.get(current_day, [])
            
            if not top_codes:
                prev_day_dt = datetime.strptime(current_day, "%Y-%m-%d") - timedelta(days=1)
                candidates_df = strategy_selector.select_monitoring_targets(
                    self.settings, logger_callback=lambda x: None, target_date=prev_day_dt, data_manager=self.db
                )
                top_codes = candidates_df.head(int(self.settings.get('max_slots', 20)))['Code'].tolist() if candidates_df is not None else []
            
            # 2-2. 감시 대상 확정
            max_s = int(self.settings.get('max_slots', 20))
            holdings_codes = list(self.holdings.keys())
            new_candidates = [c for c in top_codes if c not in holdings_codes]
            monitoring_list = holdings_codes + new_candidates[:max(0, max_s - len(holdings_codes))]
            
            # 2-2. [v4.5] Triple-Lock 기반 시장 트렌드 통합 판별 (Single Source of Truth)
            m_trend = 'UP'
            try:
                if prev_day_idx >= 0:
                    m_data = market_df.loc[trading_days[prev_day_idx]]
                    price = m_data['Close']
                    ma60 = m_data.get('MA60', price)
                    msi = m_data.get('MSI', 0)
                    
                    # [Rule]
                    # 1. DOWN: Price < MA60 (가장 강력한 블로킹)
                    # 2. NEUTRAL: Price >= MA60 이지만 MSI <= 0
                    # 3. UP: Price >= MA60 이고 MSI > 0
                    if price < ma60:
                        m_trend = 'DOWN'
                    elif msi <= 0:
                        m_trend = 'NEUTRAL'
                    else:
                        m_trend = 'UP'
            except: pass

            # 2-3. 당일 데이터 및 지표 추출
            day_data = {}
            day_signals = {}
            for code in monitoring_list:
                if code not in self.cached_15m_data: continue
                
                base_df = self.cached_15m_data[code]
                day_full = base_df.loc[current_day:current_day + " 23:59:59"].copy()
                if day_full.empty: continue
                
                # [v16.92] Optimization: Signal calculation was moved to _prepare_15m_cached_data (PRE-CALC)
                # The 'm_trend' filter is still applied here if needed, but the base signals are ready.
                if self.settings.get('use_market_filter', True) and m_trend == 'DOWN':
                    day_full['Signal'] = False
                elif self.settings.get('use_market_filter', True) and m_trend == 'NEUTRAL':
                    # Type B is blocked in NEUTRAL
                    is_type_b = (day_full['Sig_Type'] == 'TYPE_B')
                    day_full.loc[is_type_b, 'Signal'] = False
                
                day_data[code] = day_full
                day_signals[code] = day_full

            # 2-4. 분단위/15분단위 타임라인 루프
            timeline = []
            if day_data:
                all_times = []
                for df in day_data.values(): all_times.extend(df.index.tolist())
                timeline = sorted(list(set(all_times)))
            
            today_sells = set()
            code_to_rank = {c: i for i, c in enumerate(top_codes)}
            
            for curr_time in timeline:
                # [v23.0] 고정밀 모드 처리 (지연 로딩 및 타켓팅 최적화)
                m1_processed = set()
                if self.high_precision:
                    # 보유 종목이거나 매수 신호가 있는 종목만 1분봉 시뮬레이션 수행
                    m1_targets = [c for c in monitoring_list if (c in self.holdings) or 
                                  (day_signals.get(c) is not None and curr_time in day_signals[c].index and day_signals[c].loc[curr_time, 'Signal'])]
                    
                    for code in m1_targets:
                        m1_chunk = self._get_m1_chunk_on_demand(code, current_day, curr_time)
                        if m1_chunk is not None and not m1_chunk.empty:
                            self._process_intra_bar_dynamic(code, m1_chunk, day_data.get(code), m_trend, today_sells)
                            m1_processed.add(code)

                # 2-4-1. 매도 감시
                for code, h_info in list(self.holdings.items()):
                    if code in today_sells or code in m1_processed: continue
                    d_df = day_data.get(code)
                    if d_df is None or curr_time not in d_df.index: continue
                    
                    curr_price_row = d_df.loc[curr_time]
                    curr_price = curr_price_row['Close']
                    h_info['max_p'] = max(h_info.get('max_p', 0), curr_price)
                    h_info['last_p'] = curr_price # [v32.0] Store for dynamic sizing baseline
                    
                    ret_pct = (curr_price / h_info['avg_p'] - 1) * 100
                    exit_reason = ""
                    
                    g_lmt = float(self.settings.get('gap_down_limit', -5.0))
                    if ret_pct <= g_lmt:
                        exit_reason = f"Gap Down Limit ({ret_pct:.1f}%)|RSI:{curr_price_row['RSI']:.1f}|MA5:{int(curr_price_row['MA5']):,}|OSC:{curr_price_row['OSC']:.2f}"

                    if not exit_reason:
                        if h_info['entry_n'] == 1:
                            sig_type = h_info.get('sig_type', 'TYPE_A')
                            suffix = "_a" if sig_type == "TYPE_A" else "_b"
                            
                            use_flash = self.settings.get(f'use_s1{suffix}_flash_exit', True)
                            if use_flash:
                                f_pct = float(self.settings.get(f's1{suffix}_flash_exit_pct', 3.0))
                                if curr_price < h_info['max_p'] * (1 - f_pct/100):
                                    drop_pct = (curr_price / h_info['max_p'] - 1) * 100
                                    exit_reason = f"Flash Exit (S1-{sig_type})|MaxP:{int(h_info['max_p']):,}|Drop:{drop_pct:.1f}%"
                            
                            if not exit_reason:
                                use_tp = self.settings.get(f'use_s1{suffix}_tp', True)
                                if use_tp:
                                    s1_tp = float(self.settings.get(f's1{suffix}_take_profit', 5.0))
                                    if ret_pct >= s1_tp:
                                        exit_reason = f"Take Profit (S1-{sig_type})"
                            
                            if not exit_reason:
                                use_sl = self.settings.get(f'use_s1{suffix}_stop_loss', True)
                                if use_sl:
                                    s1_sl = float(self.settings.get(f's1{suffix}_stop_loss', 3.0))
                                    if ret_pct <= -s1_sl:
                                        exit_reason = f"Stop Loss (S1-{sig_type})"
                        else:  # entry_n == 2
                            if self.settings.get('use_s2_flash_exit', True):
                                f_pct = float(self.settings.get('s2_flash_exit_pct', 3.0))
                                if curr_price < h_info['max_p'] * (1 - f_pct/100):
                                    drop_pct = (curr_price / h_info['max_p'] - 1) * 100
                                    exit_reason = f"Flash Exit (S2)|MaxP:{int(h_info['max_p']):,}|Drop:{drop_pct:.1f}%"
                            
                            if not exit_reason and self.settings.get('use_s2_tp', True):
                                s2_tp = float(self.settings.get('s2_take_profit', 5.0))
                                if ret_pct >= s2_tp:
                                    exit_reason = f"Take Profit (S2)|RSI:{curr_price_row['RSI']:.1f}|MA5:{int(curr_price_row['MA5']):,}|OSC:{curr_price_row['OSC']:.2f}"

                            if not exit_reason and self.settings.get('use_s2_stop_loss', True):
                                s2_sl = float(self.settings.get('s2_stop_loss', 2.0))
                                if ret_pct <= -s2_sl:
                                    exit_reason = f"Stop Loss (S2)|RSI:{curr_price_row['RSI']:.1f}|MA5:{int(curr_price_row['MA5']):,}|OSC:{curr_price_row['OSC']:.2f}"
                            
                        # [v31.0] ATR Exit (Standard Mode) - [v34.5-Advanced] Granular Selection (A/B Separation)
                        if not exit_reason and self.settings.get('use_atr_exit', False):
                            sig_type = h_info.get('sig_type', 'TYPE_A')
                            suffix = "_a" if sig_type == "TYPE_A" else "_b"
                            
                            use_atr_sl = self.settings.get(f'use_atr_stop_loss{suffix}', True)
                            use_atr_trail = self.settings.get(f'use_atr_trailing_stop{suffix}', True)
                            
                            curr_atr = curr_price_row.get('ATR', 0)
                            entry_atr = h_info.get('atr_at_entry', curr_atr)
                            
                            if not np.isnan(curr_atr) and curr_atr > 0:
                                sl_mult = float(self.settings.get(f'atr_multiplier_sl{suffix}', self.settings.get('atr_multiplier_sl', 2.0)))
                                trail_mult = float(self.settings.get(f'atr_multiplier_trail{suffix}', self.settings.get('atr_multiplier_trail', 3.0)))
                                
                                if use_atr_sl and curr_price <= h_info['avg_p'] - (sl_mult * entry_atr):
                                    exit_reason = f"ATR Stop Loss (S{h_info['entry_n']}-{sig_type})|ATR:{curr_atr:.1f}"
                                elif use_atr_trail and curr_price <= h_info['max_p'] - (trail_mult * curr_atr):
                                    exit_reason = f"ATR Trail Stop (S{h_info['entry_n']}-{sig_type})|ATR:{curr_atr:.1f}"
                    
                    # [v31.5] Smart Exit (VolPower, Bid/Ask Ratio)
                    if not exit_reason and self.settings.get('use_smart_exit', False):
                        ba_ratio = curr_price_row.get('bid_ask_ratio', 1.0)
                        v_power = curr_price_row.get('vol_power', 100.0)
                        ba_threshold = float(self.settings.get('smart_exit_ba_ratio', 0.8))
                        vp_threshold = float(self.settings.get('smart_exit_vp', 90.0))
                        
                        if ba_ratio < ba_threshold and v_power < vp_threshold:
                            # [v31.6] Smart Exit Price Threshold
                            p_thr = float(self.settings.get('smart_exit_price_threshold', 0.0))
                            if p_thr == 0 or curr_price <= h_info['max_p'] * (1 + p_thr / 100.0):
                                exit_reason = f"Smart Exit|BA:{ba_ratio:.2f}|VP:{v_power:.0f}"
                    
                    if exit_reason:
                        # [v31.8] Gap Grace Period check (9:00 AM ~ N minutes)
                        is_grace = False
                        if self.settings.get('use_gap_grace', True):
                            try:
                                dt_time = pd.to_datetime(curr_time)
                                grace_mins = int(self.settings.get('gap_grace_mins', 15))
                                if dt_time.hour == 9 and dt_time.minute < grace_mins:
                                    # 익절(Take Profit)을 제외한 모든 손실성 매도는 유예
                                    if "Take Profit" not in exit_reason:
                                        is_grace = True
                            except: pass
                        
                        if is_grace:
                            # Skip sell during grace period
                            pass
                        else:
                            self._execute_sell(code, curr_price, curr_time, exit_reason, indicator_row=curr_price_row)
                            today_sells.add(code)

                # 2-4-2. 매수 감시
                buy_candidates = [c for c in monitoring_list if (c not in self.holdings or self.holdings.get(c, {}).get('entry_n', 0) < 2) and (c not in today_sells)]
                if buy_candidates:
                    for code in sorted(buy_candidates, key=lambda c: code_to_rank.get(c, 999)):
                        if code in today_sells or code in m1_processed: continue
                        d_df = day_data.get(code)
                        if d_df is None or curr_time not in d_df.index: continue
                        last_sig = day_signals[code].loc[curr_time]
                        h_info = self.holdings.get(code)
                        
                        # 2차 매수 대상인 경우 승인 조건 체크 (v22.1)
                        is_buy_signal = (last_sig['Signal'] and last_sig['Ready'])
                        is_addon_target = (h_info is not None and h_info['entry_n'] < 2)
                        
                        if is_buy_signal or is_addon_target:
                            can_execute = True
                            reason = "SIG" # 일관성 있게 backtest_engine의 기본 SIG와 통일
                            
                            if is_addon_target:
                                # 추매 승인 조건 체크 (TradingStrategy 통합 로직 사용)
                                if self._check_s2_approval(code, h_info, last_sig, market_trend=m_trend):
                                    # [v33.9] Stage 2 Smart Buy Filter
                                    can_execute_s2 = True
                                    if self.use_sb_s2:
                                        ba_ratio = last_sig.get('bid_ask_ratio', 1.0)
                                        v_power = last_sig.get('vol_power', 100.0)
                                        if ba_ratio < self.sb_ba or v_power < self.sb_vp:
                                            can_execute_s2 = False
                                    
                                    if can_execute_s2:
                                        reason = f"ADDON(S2)|R:{last_sig.get('RSI',0):.1f}|M:{last_sig.get('MACD',0):.1f}|O:{last_sig.get('OSC',0):.1f}|V:{int(last_sig.get('Volume',0))}"
                                    else:
                                        can_execute = False
                                else:
                                    can_execute = False
                            
                            if can_execute and not is_addon_target:
                                # [v31.5] Smart Buy (Supply/Demand Confirmation)
                                sig_type = last_sig.get('Sig_Type', 'TYPE_B')
                                use_sb_for_type = self.use_sb_a if sig_type == "TYPE_A" else self.use_sb_b
                                
                                if use_sb_for_type:
                                    ba_ratio = last_sig.get('bid_ask_ratio', 1.0)
                                    v_power = last_sig.get('vol_power', 100.0)
                                    ba_min = float(self.settings.get('smart_buy_ba_ratio', 1.2))
                                    vp_min = float(self.settings.get('smart_buy_vp', 110.0))
                                    
                                    if ba_ratio < ba_min or v_power < vp_min:
                                        can_execute = False
                                        # [v27.5] Shadow Return (1h Lookahead)
                                        p_inc_str = ""
                                        try:
                                            curr_idx = timeline.index(curr_time)
                                            future_idx = min(curr_idx + 4, len(timeline) - 1)
                                            future_time = timeline[future_idx]
                                            if future_time in d_df.index:
                                                p_inc = (d_df.loc[future_time, 'Close'] / last_sig['Close'] - 1) * 100
                                                p_inc_str = f"|1h:{p_inc:+.2f}%"
                                        except: pass

                                        # [v27.5] Log Rejection
                                        reason_rej = f"SmartBuy Rejected|BA:{ba_ratio:.2f}|VP:{v_power:.0f}{p_inc_str}"
                                        self._log_trade_to_csv(curr_time, code, "REJECTED_SIG", last_sig['Close'], 0, reason_rej, sig_type=sig_type)
                                        self.trades_history.append({
                                            'Date': curr_time, 'Code': code, 'Name': self.stock_name_map.get(code, code),
                                            'Action': 'REJECTED_SIG', 'Price': last_sig['Close'], 'Qty': 0, 'Amount': 0, 
                                            'Reason': reason_rej, 'Cash': self.cash,
                                            'Sig_Type': sig_type
                                        })
                            
                            if can_execute:
                                sig_type = last_sig.get('Sig_Type', 'TYPE_B')
                                use_sb_for_type = self.use_sb_a if (sig_type == "TYPE_A" and not is_addon_target) else \
                                                   self.use_sb_b if (sig_type == "TYPE_B" and not is_addon_target) else \
                                                   self.use_sb_s2
                                
                                # [v33.9] Prefix "SmartBuy|" if it was enabled and passed
                                if use_sb_for_type:
                                    reason = "SmartBuy|" + reason
                                    
                                self._handle_buy_execution(code, {'reason': reason, 'Sig_Type': sig_type}, curr_time, d_df.loc[curr_time], indicator_row=d_df.loc[curr_time])

            # 2-5. 일일 정산
            current_equity = self.cash + sum(h['qty'] * day_data[c]['Close'].iloc[-1] for c, h in self.holdings.items() if c in day_data)
            self.daily_equity.append({'date': current_day, 'equity': current_equity, 'cash': self.cash, 'holdings_n': len(self.holdings)})
            
            day_t_duration = time.time() - day_t_start
            print(f"[Day] {current_day} 처리 완료 ({day_t_duration:.2f}초)")

        if self.holdings:
            last_day = trading_days[-1]
            for code, h_info in list(self.holdings.items()):
                if code in day_data:
                    last_price = day_data[code]['Close'].iloc[-1]
                    last_time = day_data[code].index[-1]
                    sell_fee = int(last_price * h_info['qty'] * self.sell_f_rate)
                    ret = (last_price / h_info['avg_p'] - 1) * 100
                    last_row = day_data[code].iloc[-1]
                    reason = f"Final Evaluation|RSI:{last_row['RSI']:.1f}|MA5:{int(last_row['MA5']):,}|OSC:{last_row['OSC']:.2f}"
                    self._log_trade_to_csv(last_time, code, 'HOLD', last_price, h_info['qty'], reason, ret=ret, fee=sell_fee, sig_type=h_info.get('sig_type', 'TYPE_A'))
                    self.trades_history.append({
                        'Date': last_time, 'Code': code, 'Name': TradingStrategy.get_stock_name_map().get(code, code), 
                        'Action': 'HOLD', 'Price': last_price, 'Qty': h_info['qty'], 'Amount': h_info['qty'] * last_price, 
                        'Return': ret, 'Reason': reason, 'Cash': self.cash, 'Fee': sell_fee,
                        'Sig_Type': h_info.get('sig_type', 'TYPE_A')
                    })
        return self._finalize_backtest(interval)

    def _handle_buy_execution(self, code, sig_res, curr_time, last_row, indicator_row=None):
        price = last_row['Close']
        h_info = self.holdings.get(code)
        s1_amt = float(self.settings.get('stage1_amt', 1_000_000))
        s2_amt = float(self.settings.get('stage2_amt', 2_000_000))
        
        # [v33.0] Unified Sizing (Unit-based: % or 원)
        s1_unit = self.settings.get('s1_sizing_unit', "원")
        s2_unit = self.settings.get('s2_sizing_unit', "원")
        
        if s1_unit == '%' or s2_unit == '%':
            # Calculate current total equity (Cash + Current Holdings Value)
            holdings_value = 0
            for c, h in self.holdings.items():
                holdings_value += h['qty'] * h.get('last_p', h['avg_p'])
            total_equity = self.cash + holdings_value
            
            if s1_unit == '%':
                s1_pct = float(self.settings.get('s1_equity_pct', 10.0))
                s1_amt = total_equity * (s1_pct / 100.0)
            if s2_unit == '%':
                s2_pct = float(self.settings.get('s2_equity_pct', 20.0))
                s2_amt = total_equity * (s2_pct / 100.0)

        if h_info is None:
            if self.cash < s1_amt * 0.5: return 
            target_amt = s1_amt
            entry_n = 1
            sig_type = sig_res.get('Sig_Type', 'TYPE_A')
        else:
            if h_info['entry_n'] >= 2: return
            # market_trend는 handle_buy_execution 호출부에서 넘겨받기 어렵지만, s2_approval은 이미 위에서 체크됨
            # 하지만 안전을 위해 다시 체크하려면 파라미터 추가 필요 (일단 위 339라인에서 체크 완료됨)
            target_amt = s2_amt
            entry_n = 2
            sig_type = h_info.get('sig_type', 'TYPE_A')
            
        qty = int(target_amt // (price * (1 + self.buy_f_rate)))
        if qty <= 0: return
        real_amt = qty * price
        buy_fee = int(real_amt * self.buy_f_rate)
        
        if self.cash < (real_amt + buy_fee): return

        self.cash -= (real_amt + buy_fee)
        if h_info is None:
            self.holdings[code] = {
                'qty': qty, 'avg_p': price, 'entry_n': 1, 'max_p': price, 
                'entry_date': curr_time, 'sig_type': sig_type,
                'atr_at_entry': last_row.get('ATR', 0) # Store ATR for SL
            }
        else:
            old_qty = h_info['qty']
            h_info['avg_p'] = (h_info['avg_p'] * old_qty + real_amt) / (old_qty + qty)
            h_info['qty'] += qty
            h_info['entry_n'] = 2
            
        # [v27.0] 전략 타입(A/B/S2)을 매수 사유(Reason) 접두어로 명시하여 리포트 집계 정확도 향상
        prefix = f"[{sig_type}] " if entry_n == 1 else "[Stage 2] "
        reason_str = prefix + sig_res['reason']
        if indicator_row is not None:
            try:
                rsi_val = indicator_row.get('RSI', 0)
                ma5_val = indicator_row.get('MA5', 0)
                osc_val = indicator_row.get('OSC', 0)
                reason_str += f"|RSI:{rsi_val:.1f}|MA5:{int(ma5_val):,}|OSC:{osc_val:.2f}"
            except: pass

        self.trades_history.append({
            'Date': curr_time, 'Code': code, 'Name': self.stock_name_map.get(code, code),
            'Action': f'BUY{entry_n}', 'Price': price, 'Qty': qty, 'Amount': real_amt, 'Fee': buy_fee,
            'Reason': reason_str, 'Cash': self.cash, 'Sig_Type': sig_type
        })
        self._log_trade_to_csv(curr_time, code, f'BUY{entry_n}', price, qty, reason_str, fee=buy_fee, sig_type=sig_type)

    def _execute_sell(self, code, price, curr_time, reason, indicator_row=None):
        """매도 실행 및 기록 (v8.2: 분할매도 유령로직 제거)"""
        if indicator_row is not None:
            try:
                rsi_val = indicator_row.get('RSI', 0)
                ma5_val = indicator_row.get('MA5', 0)
                osc_val = indicator_row.get('OSC', 0)
                if "RSI:" not in str(reason):
                    prefix = "|" if "|" not in str(reason) else ", "
                    reason += f"{prefix}RSI:{rsi_val:.1f}|MA5:{int(ma5_val):,}|OSC:{osc_val:.2f}"
            except: pass
            
        h_info = self.holdings.pop(code, None)
        if h_info is None: return
        
        sell_qty = h_info['qty']
        action = 'SELL'

        gross = sell_qty * price
        fee = int(gross * self.sell_f_rate)
        ret = (price / h_info['avg_p'] - 1) * 100
        self.cash += (gross - fee)
        
        sig_type = h_info.get('sig_type', 'TYPE_A')
        self.trades_history.append({
            'Date': curr_time, 'Code': code, 'Name': self.stock_name_map.get(code, code),
            'Action': action, 'Price': price, 'Qty': sell_qty, 'Amount': gross, 'Fee': fee,
            'Reason': reason, 'Return': ret, 'Cash': self.cash, 'Sig_Type': sig_type
        })
        self._log_trade_to_csv(curr_time, code, action, price, sell_qty, reason, ret=ret, fee=fee, sig_type=sig_type)

    def _log_trade_to_csv(self, dt, code, action, price, qty, reason, ret=0, fee=0, sig_type=None):
        name = self.stock_name_map.get(code, code)
        safe_reason = str(reason).replace(',', ';')
        row = [pd.to_datetime(dt).strftime('%Y-%m-%d %H:%M'), code, name, action, price, qty, f"{ret:.2f}", safe_reason, fee, sig_type]
        with csv_lock:
            if not os.path.exists(TRADE_CSV):
                with open(TRADE_CSV, 'w', encoding='utf-8-sig') as f:
                    f.write("Date,Code,Name,Action,Price,Qty,Return,Reason,Fee,Sig_Type\n")
            with open(TRADE_CSV, 'a', encoding='utf-8-sig') as f:
                f.write(",".join(map(str, row)) + "\n")

    def _finalize_backtest(self, interval):
        print(f"[Dynamic] 시뮬레이션 완료. 총 {len(self.trades_history)}건의 매매(평가 포함) 발생.")
        trades_df = pd.DataFrame(self.trades_history)
        if not trades_df.empty:
            trades_df['Date'] = pd.to_datetime(trades_df['Date'])
            trd_path = os.path.join(BASE_DIR, "dynamic_trade_report.html")
            import reporting.trade_report
            importlib.reload(reporting.trade_report)
            from reporting.trade_report import generate_trade_report
            exit_trades = trades_df[trades_df['Action'].isin(['SELL', 'HOLD'])]
            win_rate = (len(exit_trades[exit_trades['Return'] > 0]) / max(1, len(exit_trades))) * 100
            avg_ret = (exit_trades['Return'] * exit_trades['Amount']).sum() / exit_trades['Amount'].sum() if not exit_trades.empty and exit_trades['Amount'].sum() > 0 else 0
            
            # [v25.3] Save equity curve to CSV for consistency with standard backtest
            try:
                equity_df = pd.DataFrame(self.daily_equity)
                if not equity_df.empty:
                    equity_df.set_index('date')[['equity']].rename(columns={'equity': 'Total'}).to_csv(
                        os.path.join(BASE_DIR, "backtest_portfolio_curve.csv"), encoding='utf-8-sig'
                    )
            except Exception as e:
                print(f"[Error] Failed to save portfolio curve: {e}")

            generate_trade_report(trades_df, interval, trd_path, len(exit_trades), win_rate, avg_ret, self.settings, 
                                  market_df=self.db.get_daily_bars('001', self.start_date, self.end_date),
                                  vix_df=self.db.get_daily_bars('^VIX', self.start_date, self.end_date),
                                  start_date=self.start_date, end_date=self.end_date, daily_equity=self.daily_equity,
                                  high_precision=self.high_precision)
        return trades_df

    def _prepare_15m_cached_data(self, universe_schedule, start_date, end_date, callback):
        if callback: callback("15분봉 데이터 일괄 로드 중...", 0)
        u_codes = set()
        for codes in universe_schedule.values(): u_codes.update(codes)
        if not u_codes: return {}
        all_codes = list(u_codes)
        pad_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=21)).strftime("%Y-%m-%d")
        placeholders = ",".join(["?"] * len(all_codes))
        # [v31.5] Updated query to join with realtime_snapshots
        query = f"""
            SELECT b.code, b.datetime as time, b.open, b.high, b.low, b.close, b.volume,
                   r.vol_power, r.total_ask, r.total_bid
            FROM bars_15m b
            LEFT JOIN realtime_snapshots r ON b.code = r.code AND b.datetime = r.datetime
            WHERE b.code IN ({placeholders}) AND b.datetime BETWEEN ? AND ?
            ORDER BY b.code, b.datetime
        """
        with self.db._get_connection() as conn:
            df_all = pd.read_sql_query(query, conn, params=all_codes + [pad_start, end_date + " 23:59:59"])
        if df_all.empty: return {}
        df_all.columns = [c.capitalize() if c.lower() != 'time' else 'time' for c in df_all.columns]
        df_all['time'] = pd.to_datetime(df_all['time'])
        df_all.set_index('time', inplace=True)
        cached = {}
        for code, group in df_all.groupby('Code'):
            # [v31.5] Synthetic Metrics Pre-calculation for Dynamic Backtester
            if self.settings.get('use_synthetic_realtime', True):
                # [v31.5] Ensure numeric types for join columns
                for col in ['Vol_power', 'Total_ask', 'Total_bid']:
                    if col in group.columns:
                        group[col] = pd.to_numeric(group[col], errors='coerce')
                
                p_changes = group['Close'].pct_change().fillna(0).values
                vol_mas = group['Volume'].rolling(20).mean().bfill().values
                vol_ratios = (group['Volume'] / (vol_mas + 1e-10)).values
                vps = group['Vol_power'].values
                tasks = group['Total_ask'].values
                tbids = group['Total_bid'].values
                
                nan_vp = np.isnan(vps)
                if np.any(nan_vp):
                    vps[nan_vp] = SimulatedRealtimeMetrics.simulate_vol_power_vectorized(p_changes[nan_vp], vol_ratios[nan_vp])
                
                nan_ba = (np.isnan(tasks)) | (np.any(np.isnan(tbids)))
                if np.any(nan_ba):
                    r_vector = SimulatedRealtimeMetrics.simulate_bid_ask_ratio_vectorized(p_changes[nan_ba], vol_ratios[nan_ba], vps[nan_ba])
                    tasks[nan_ba] = 100000
                    tbids[nan_ba] = (100000 * r_vector).astype(int)
                
                group = group.copy() # In-place modification warning prevention
                group['Vol_power'] = vps
                group['Total_ask'] = tasks
                group['Total_bid'] = tbids
                group['bid_ask_ratio'] = group['Total_bid'] / (group['Total_ask'] + 1e-10)
                group['vol_power'] = vps # [v31.5] Maintain lowercase for backward compatibility if needed

            # [v16.92] Deep Optimization: Pre-calculate PULSE logic once per stock for the entire period
            # This avoids O(N*M) redundant .groupby().cumsum() calls in the daily loop
            df_ind = TradingStrategy.calculate_indicators(group, self.settings)
            
            is_ma_aligned = (df_ind['MA5'] > df_ind['MA20'])
            ma_group_id = (~is_ma_aligned).cumsum()
            df_ind['MA_Maint_Count'] = is_ma_aligned.groupby(ma_group_id).cumsum()
            
            is_rsi_above_50 = (df_ind['RSI'] >= 50.0)
            rsi_group_id = (df_ind['RSI'] < 50.0).cumsum()
            df_ind['RSI_Pulse_Count'] = is_rsi_above_50.groupby(rsi_group_id).cumsum()
            
            # [v16.92] Pre-calculate signals for the ENTIRE time series at once (Vectorized)
            # This makes the daily loop in run() a simple O(1) lookup
            full_sigs = TradingStrategy.get_buy_signals_detailed(df_ind, self.settings, market_trend='UP') # Trend-independent pre-check
            df_ind = df_ind.join(full_sigs.drop(columns='Signal', errors='ignore'))
            df_ind['Signal'] = full_sigs['Signal']
            
            df_ind['Ready'] = (df_ind.reset_index().index >= 19)
            cached[code] = df_ind
        return cached

    def _get_m1_chunk_on_demand(self, code, date_str, curr_time):
        """[v23.0] 필요한 시점에만 1분봉 데이터를 DB에서 가져와 캐싱 (성능 핵심)"""
        cache_key = (code, date_str)
        if cache_key not in self.cached_1m_data:
            # [v31.5] Join with realtime_snapshots for high-precision smart metrics
            query = """
                SELECT b.datetime as time, b.open, b.high, b.low, b.close, b.volume,
                       r.vol_power, r.total_ask, r.total_bid
                FROM bars_1m b
                LEFT JOIN realtime_snapshots r ON b.code = r.code AND b.datetime = r.datetime
                WHERE b.code = ? AND b.datetime BETWEEN ? AND ?
                ORDER BY b.datetime
            """
            with self.db._get_connection() as conn:
                df = pd.read_sql_query(query, conn, params=[code, date_str + " 09:00:00", date_str + " 15:30:00"])
            if df.empty:
                self.cached_1m_data[cache_key] = None
            else:
                df.columns = [c.capitalize() if c.lower() != 'time' else 'time' for c in df.columns]
                df['time'] = pd.to_datetime(df['time'])
                df.set_index('time', inplace=True)
                
                # [v31.5] Pre-calculate synthetic metrics for 1m data
                if self.settings.get('use_synthetic_realtime', True):
                    # [v31.5] Ensure numeric types
                    for col in ['Vol_power', 'Total_ask', 'Total_bid']:
                        if col in df.columns:
                            df[col] = pd.to_numeric(df[col], errors='coerce')

                    p_changes = df['Close'].pct_change().fillna(0).values
                    # 1m MA20
                    vol_mas = df['Volume'].rolling(20).mean().bfill().values
                    vol_ratios = (df['Volume'] / (vol_mas + 1e-10)).values
                    vps = df['Vol_power'].values
                    nan_vp = np.isnan(vps)
                    if np.any(nan_vp):
                        vps[nan_vp] = SimulatedRealtimeMetrics.simulate_vol_power_vectorized(p_changes[nan_vp], vol_ratios[nan_vp])
                    
                    ratios = SimulatedRealtimeMetrics.simulate_bid_ask_ratio_vectorized(p_changes, vol_ratios, vps)
                    
                    df = df.copy()
                    df['Vol_power'] = vps
                    df['vol_power'] = vps # Compatibility
                    df['bid_ask_ratio'] = ratios

                self.cached_1m_data[cache_key] = df
        
        m1_full = self.cached_1m_data[cache_key]
        if m1_full is not None:
            try:
                # 15분 단위 청크 슬라이싱
                return m1_full.loc[curr_time : curr_time + timedelta(minutes=14, seconds=59)]
            except: return None
        return None

    def _prepare_1m_data(self, universe_schedule, start_date, end_date, callback):
        # [REMOVED] v23.0부터는 지연 로딩을 사용하므로 사전 로드가 불필요함
        return {}

    def _process_intra_bar_dynamic(self, code, m1_chunk, day_df_15m, m_trend, today_sells):
        """[v23.0] 15분봉 내부의 1분봉 시뮬레이션: 넘파이 벡터연산으로 최적화"""
        # [v26.1] Move curr_15m to top to avoid UnboundLocalError on exits
        m1_dt_first = m1_chunk.index[0]
        curr_15m = day_df_15m.loc[m1_dt_first] if day_df_15m is not None and m1_dt_first in day_df_15m.index else None
        
        h_info = self.holdings.get(code)
        
        if h_info is not None:
            # 1. 보유 종목인 경우: 매도(Exit) 조건 검사 (벡터화)
            m1_closes = m1_chunk['Close'].values
            m1_highs = m1_chunk['High'].values
            m1_times = m1_chunk.index
            
            # 누적 최고가 업데이트 (벡터화)
            prev_max = h_info.get('max_p', 0)
            running_max = np.maximum.accumulate(m1_highs)
            running_max = np.maximum(running_max, prev_max)
            
            # (A) Flash Exit 조건 계산
            if h_info['entry_n'] == 1:
                is_type_b = (h_info.get('sig_type') == "TYPE_B" and self.use_type_b)
                suffix = "_b" if is_type_b else "_a"
                use_flash = self.use_s1b_flash if is_type_b else self.use_s1a_flash
                if use_flash:
                    f_pct = float(self.settings.get(f's1{suffix}_flash_exit_pct', 3.0))
            else:
                if self.use_s2_flash:
                    f_pct = float(self.settings.get('s2_flash_exit_pct', 3.0))
            
            flash_exited = (f_pct > 0) & (m1_closes < running_max * (1 - f_pct/100))
            
            # (B) Stop Loss & Take Profit 조건 계산
            sl_pct = 0.0
            tp_pct = 0.0
            if h_info['entry_n'] == 1:
                is_type_b = (h_info.get('sig_type') == "TYPE_B" and self.use_type_b)
                suffix = "_b" if is_type_b else "_a"
                use_sl = self.use_s1b_sl if is_type_b else self.use_s1a_sl
                if use_sl:
                    sl_pct = float(self.settings.get(f's1{suffix}_stop_loss', 3.0))
                use_tp = self.use_s1b_tp if is_type_b else self.use_s1a_tp
                if use_tp:
                    tp_pct = float(self.settings.get(f's1{suffix}_take_profit', 5.0))
            else:
                if self.use_s2_sl:
                    sl_pct = float(self.settings.get('s2_stop_loss', 2.0))
                if self.use_s2_tp:
                    tp_pct = float(self.settings.get('s2_take_profit', 5.0))
            
            stop_lossed = (sl_pct > 0) & (m1_closes / h_info['avg_p'] - 1 <= -sl_pct/100)
            take_profited = (tp_pct > 0) & (m1_highs / h_info['avg_p'] - 1 >= tp_pct/100)            
            
            # (C) Gap Down 조건 (S1/S2 공통)
            gap_downed = (m1_closes / h_info['avg_p'] - 1 <= self.gap_down_lmt / 100)
            
            # [v25.8/v25.9] 15-minute Gap-Down Grace Period: Allow recovery between SL/Flash and Gap Limit during first 15m
            if self.settings.get('use_gap_grace', True):
                if m1_times[0].hour == 9 and m1_times[0].minute == 0:
                    grace_mask = (m1_times.hour == 9) & (m1_times.minute < 15)
                    stop_lossed = stop_lossed & (~grace_mask)
                    flash_exited = flash_exited & (~grace_mask)
            
            # (D) ATR Exit (v31.0) - [v34.5-Advanced] Granular Strategy Control
            atr_trailed = np.zeros(len(m1_closes), dtype=bool)
            atr_sl_triggered = np.zeros(len(m1_closes), dtype=bool)
            if self.use_atr:
                sig_type = h_info.get('sig_type', 'TYPE_A')
                suffix = "_a" if sig_type == "TYPE_A" else "_b"
                
                use_atr_sl = self.settings.get(f'use_atr_stop_loss{suffix}', True)
                use_atr_trail = self.settings.get(f'use_atr_trailing_stop{suffix}', True)
                
                atr_val = curr_15m.get('ATR', 0) if curr_15m is not None else 0
                entry_atr = h_info.get('atr_at_entry', atr_val)
                
                if not np.isnan(atr_val) and atr_val > 0:
                    sl_mult = float(self.settings.get(f'atr_multiplier_sl{suffix}', self.settings.get('atr_multiplier_sl', 2.0)))
                    trail_mult = float(self.settings.get(f'atr_multiplier_trail{suffix}', self.settings.get('atr_multiplier_trail', 3.0)))
                    
                    if use_atr_trail:
                        atr_trailed = (m1_closes <= running_max - (trail_mult * atr_val))
                    if use_atr_sl:
                        atr_sl_triggered = (m1_closes <= h_info['avg_p'] - (sl_mult * entry_atr))

            # 최초 발생 지점 찾기
            exit_mask = flash_exited | stop_lossed | take_profited | gap_downed | atr_trailed | atr_sl_triggered
            exit_idxs = np.where(exit_mask)[0]
            
            if len(exit_idxs) > 0:
                idx = exit_idxs[0]
                exit_price = m1_closes[idx]
                exit_time = m1_times[idx]
                
                # 상세 사유 결정
                if gap_downed[idx]: reason = "1M Gap Down"
                elif take_profited[idx]: reason = f"1M Take Profit (S{h_info['entry_n']})"
                elif flash_exited[idx]: 
                    drop = (exit_price / running_max[idx] - 1) * 100
                    reason = f"1M Flash Exit (S{h_info['entry_n']})|Drop:{drop:.1f}%"
                elif stop_lossed[idx]:
                    reason = f"1M Stop Loss (S{h_info['entry_n']}-{h_info.get('sig_type','').split(' ')[-1]})"
                elif atr_trailed[idx]:
                    reason = f"1M ATR Trail Stop (S{h_info['entry_n']})"
                elif atr_sl_triggered[idx]:
                    reason = f"1M ATR Stop Loss (S{h_info['entry_n']})"
                # [v31.5] Smart Exit in High-Precision Mode
                elif self.use_sx:
                    ba_ratio = m1_chunk.iloc[idx].get('bid_ask_ratio', 1.0)
                    v_power = m1_chunk.iloc[idx].get('vol_power', 100.0)
                    if ba_ratio >= self.sx_ba and v_power < self.sx_vp:
                        if self.sx_p_thr == 0 or exit_price <= running_max[idx] * (1 + self.sx_p_thr / 100.0):
                            reason = f"1M Smart Exit(Trap)|BA:{ba_ratio:.2f}|VP:{v_power:.0f}"
                        else: reason = "1M Unknown Exit"
                    else: reason = "1M Unknown Exit"
                else:
                    reason = "1M Unknown Exit"
                
                self._execute_sell(code, exit_price, exit_time, reason, indicator_row=curr_15m)
                today_sells.add(code)
                return

            # 매도가 없었다면 최고가 정보만 업데이트
            h_info['max_p'] = running_max[-1]
            
        # 2. 미보유 또는 추가매수 가능 종목: 매수(Buy) 조건 검사
        # 매수 조건은 지표(15분봉) 기반이므로 첫 번째 분봉에서 처리
        m1_r_first = m1_chunk.iloc[0]
        
        if curr_15m is not None:
            h_info = self.holdings.get(code)
            
            is_buy_signal = (curr_15m['Signal'] and curr_15m['Ready'])
            is_addon_target = (h_info is not None and h_info['entry_n'] < 2)
            
            if is_buy_signal or is_addon_target:
                can_execute = True
                reason = "1M_SIG" # 최종 클린 버전 명시
                
                if is_addon_target:
                    # 1분봉 정밀 시물레이션에서도 시장상태 체크 (m_trend_val은 _process_intra_bar_dynamic에서 전달됨)
                    if self._check_s2_approval(code, h_info, curr_15m, market_trend=m_trend):
                        reason = f"1M_ADDON(S2)|R:{curr_15m.get('RSI',0):.1f}|O:{curr_15m.get('OSC',0):.1f}"
                    else:
                        can_execute = False
                
                # [v31.5] Granular Smart Buy Selection
                sig_type = curr_15m.get('Sig_Type', 'TYPE_B')
                use_sb_for_type = self.use_sb_a if (sig_type == "TYPE_A" and not is_addon_target) else \
                                   self.use_sb_b if (sig_type == "TYPE_B" and not is_addon_target) else \
                                   self.use_sb_s2

                if can_execute and use_sb_for_type:
                    ba_ratio = curr_15m.get('bid_ask_ratio', 1.0)
                    v_power = curr_15m.get('vol_power', 100.0)
                    
                    if ba_ratio < self.sb_ba or v_power < self.sb_vp:
                        can_execute = False
                        
                        # [v27.5] Shadow Return (1h Lookahead)
                        p_inc_str = ""
                        try:
                            # [v9.0] Correct reference: cached_15m_data
                            full_df = self.cached_15m_data.get(code)
                            if full_df is not None:
                                idx_list = full_df.index.tolist()
                                curr_idx = idx_list.index(curr_15m.name)
                                future_idx = min(curr_idx + 4, len(idx_list) - 1)
                                future_p = full_df.iloc[future_idx]['Close']
                                p_inc = (future_p / m1_r_first['Close'] - 1) * 100
                                p_inc_str = f"|1h:{p_inc:+.2f}%"
                        except: pass

                        # [v27.5] Log Rejection (1M)
                        self.trades_history.append({
                            'Date': m1_r_first.name, 'Code': code, 'Name': TradingStrategy.get_stock_name_map().get(code, code),
                            'Action': 'REJECTED_SIG', 'Price': m1_r_first['Close'], 'Qty': 0, 'Amount': 0, 
                            'Reason': f"1M SmartBuy Filter Rejected|BA:{ba_ratio:.2f}|VP:{v_power:.0f}{p_inc_str}", 'Cash': self.cash
                        })
                
                if can_execute:
                    if use_sb_for_type:
                        reason = "1M_SmartBuy|" + reason
                    self._handle_buy_execution(code, {'reason': reason, 'Sig_Type': sig_type}, m1_r_first.name, m1_r_first, indicator_row=curr_15m)

    def _check_s2_approval(self, code, h_info, last_row, indicator_row=None, market_trend='UP'):
        """[v22.1] 2차 매수 승인 로직 (backtest_engine.TradingStrategy._check_s2_approval 통합 연동)"""
        # backtest_engine 형식을 맞추기 위해 가상의 pos 객체 생성
        pos = {
            'entry_date': h_info['entry_date'],
            'avg_price': h_info['avg_p'],
            'qty': h_info['qty'],
            'invested': h_info['avg_p'] * h_info['qty']
        }
        # backtest_engine의 정적 메서드 호출
        return TradingStrategy._check_s2_approval(pos, last_row, self.settings, indicator_row=indicator_row, market_trend=market_trend)
