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
        self.buy_f_rate = settings.get('buy_fee_bt', settings.get('buy_fee_rate', 0.015)) / 100.0 if settings else 0.00015
        self.sell_f_rate = settings.get('sell_fee_bt', settings.get('sell_fee_rate', 0.215)) / 100.0 if settings else 0.00215
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
        # [Perf] S2 진단 로그를 매 호출마다 파일에 쓰지 않고 메모리에 버퍼링 (일별 flush)
        self._s2_log_buffer: list = []
        
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
        # [v38.0] Warmup: MA120/MSI 등 지표 정확도를 위해 시작일 200영업일 이전부터 데이터 로드
        warmup_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=280)).strftime("%Y-%m-%d")
        market_df_raw = self.db.get_daily_bars('001', warmup_start, end_date)
        vix_df = self.db.get_daily_bars('^VIX', warmup_start, end_date)
        if market_df_raw is None or market_df_raw.empty:
            print("[Error] [Dynamic] 시장 데이터(KS11)가 없어 백테스트를 진행할 수 없습니다.")
            return None
            
        market_df_raw['time'] = pd.to_datetime(market_df_raw['time'])
        market_df_raw.set_index('time', inplace=True)
        
        # [v38.0] 지표를 warmup 포함 전체 데이터로 계산 후, 실제 백테스트 기간만 잘라냄
        market_df_full = TradingStrategy.calculate_indicators(market_df_raw, self.settings)
        market_df = market_df_full.loc[start_date:]
        if market_df.empty:
            print(f"[Error] [Dynamic] {start_date} 이후 시장 데이터가 없습니다.")
            return None

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
        s2_debug_path = os.path.join(BASE_DIR, "s2_debug.csv")
        for f in [SIGNAL_CSV, TRADE_CSV, s2_debug_path]:
            if os.path.exists(f): 
                try: os.remove(f)
                except: pass
        # s2_debug.csv 헤더 작성 및 메모리 버퍼 초기화
        self._s2_log_buffer = []
        try:
            with open(s2_debug_path, 'w', encoding='utf-8') as f:
                f.write(f"# S2 진단 로그 | 기간: {start_date} ~ {end_date}\n")
                f.write("datetime,code,result,profit_pct,reject_reason\n")
        except Exception:
            pass

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
            
            # 2-2. [v4.5.10] Unified Triple-Lock 기반 시장 트렌드 통합 판별 (Single Source of Truth)
            m_trend = 'UP'
            try:
                if prev_day_idx >= 0:
                    m_data = market_df.loc[trading_days[prev_day_idx]]
                    msi = m_data.get('MSI', 0)
                    adx = m_data.get('ADX', 20)
                    
                    # [v18.9] VIX 데이터 정밀 연동 (Reindex/FFill)
                    v_val = 15.0
                    if vix_df is not None and not vix_df.empty:
                        # vix_df index와 m_data index(trading_days) 매칭
                        current_dt = pd.to_datetime(trading_days[prev_day_idx])
                        try:
                            v_val = vix_df.loc[vix_df.index <= current_dt, 'Close'].iloc[-1]
                        except: pass
                    
                    # [Rule v4.5.10 Hardened]
                    # 1. DOWN (Stop All): MSI < -0.10 OR VIX > 30
                    # 2. NEUTRAL (Stop Type B): ADX < 15 OR (-0.10 <= MSI <= 0.05)
                    # 3. UP: Everything else
                    if msi < -0.10 or v_val > 30:
                        m_trend = 'DOWN'
                    elif adx < 15 or (msi >= -0.10 and msi <= 0.05):
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
                            self._process_intra_bar_dynamic(code, m1_chunk, day_data.get(code), m_trend, today_sells, curr_time)
                            m1_processed.add(code)

                # 2-4-1. 매도 감시 (보유 중인 종목)
                for code, h_info in list(self.holdings.items()):
                    # [v36.1] 고정밀 모드인 경우 15분 단위 매도 패스 (하위 1분 루프에서 처리됨)
                    if self.high_precision: continue
                    if code in today_sells or code in m1_processed: continue
                    d_df = day_data.get(code)
                    if d_df is None or curr_time not in d_df.index: continue
                    
                    curr_row = d_df.loc[curr_time]
                    res = TradingStrategy.evaluate_position(
                        code, curr_row['Close'], h_info, self.settings,
                        indicator_row=curr_row, current_time=curr_time,
                        is_realtime=False
                    )
                    
                    if res['action'] == 'EXIT':
                        # TP인 경우 가격 보정 (고가 돌파 기준)
                        e_price = curr_row['Close']
                        if res['reason'] == 'TAKE_PROFIT':
                            is_type_b_h = "TYPE_B" in str(h_info.get('sig_type', ''))
                            tp_key = 's2_take_profit' if h_info['entry_n'] == 2 else ('s1_b_take_profit' if is_type_b_h else 's1_a_take_profit')
                            tp_pct = float(self.settings.get(tp_key, 5.0))
                            e_price = h_info['avg_p'] * (1 + tp_pct/100.0)
                        
                        self._execute_sell(code, e_price, curr_time, res['reason'], indicator_row=curr_row)
                        today_sells.add(code)

                # 2-4-2. 매수 감시
                buy_candidates = [c for c in monitoring_list if (c not in self.holdings or self.holdings.get(c, {}).get('entry_n', 0) < 2) and (c not in today_sells)]
                if buy_candidates and not self.high_precision:
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
                                        ab_ratio = last_sig.get('ask_bid_ratio', last_sig.get('Ask_Bid_Ratio', 1.0 / (last_sig.get('bid_ask_ratio', 1.0) + 1e-10)))
                                        v_power = last_sig.get('vol_power', 100.0)
                                        if ab_ratio < self.sb_ba or v_power < self.sb_vp:
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
                                    ab_ratio = last_sig.get('ask_bid_ratio', last_sig.get('Ask_Bid_Ratio', 1.0 / (last_sig.get('bid_ask_ratio', 1.0) + 1e-10)))
                                    v_power = last_sig.get('vol_power', 100.0)
                                    ba_min = float(self.settings.get('smart_buy_ba_ratio', 1.5))
                                    vp_min = float(self.settings.get('smart_buy_vp', 120.0))
                                    
                                    if ab_ratio < ba_min or v_power < vp_min:
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
                                        reason_rej = f"SmartBuy Rejected|AB:{ab_ratio:.2f}|VP:{v_power:.0f}{p_inc_str}"
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
            
            # [Perf] S2 진단 로그 일괄 flush (매 호출마다 open/write/close 대신 하루치를 한번에 기록)
            if self._s2_log_buffer:
                try:
                    s2_debug_path = os.path.join(BASE_DIR, "s2_debug.csv")
                    with open(s2_debug_path, 'a', encoding='utf-8') as _f:
                        _f.writelines(self._s2_log_buffer)
                    self._s2_log_buffer.clear()
                except Exception:
                    self._s2_log_buffer.clear()
            
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

    def _handle_buy_execution(self, code, sig_res, curr_time, last_row, indicator_row=None) -> bool:
        """매수 실행. 실제 매수가 이루어진 경우 True 반환, 조건 미충족 시 False 반환."""
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
            if self.cash < s1_amt * 0.5: return False
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
        if qty <= 0: return False
        real_amt = qty * price
        buy_fee = int(real_amt * self.buy_f_rate)
        
        if self.cash < (real_amt + buy_fee): return False

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
        return True

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

            # [v37.9] 시장지수 필터(MSI/ADX) 계산을 위해 백테스트 시작일 기준 200일의 과거 버퍼 추가 확보
            report_start = (datetime.strptime(self.start_date, "%Y-%m-%d") - timedelta(days=200)).strftime("%Y-%m-%d")
            
            generate_trade_report(trades_df, interval, trd_path, len(exit_trades), win_rate, avg_ret, self.settings, 
                                  market_df=self.db.get_daily_bars('001', report_start, self.end_date),
                                  vix_df=self.db.get_daily_bars('^VIX', report_start, self.end_date),
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
                df = pd.read_sql_query(query, conn, params=[code, date_str + " 09:00:00", date_str + " 15:45:00"])
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
                    df['ask_bid_ratio'] = 1.0 / (ratios + 1e-10)

                self.cached_1m_data[cache_key] = df
        
        m1_full = self.cached_1m_data[cache_key]
        if m1_full is not None:
            try:
                # [v37.5] 15분봉 시각(curr_time)은 해당 봉의 마감 시각이므로, T-14분~T 범위를 가져와야 정합성이 맞음
                return m1_full.loc[curr_time - timedelta(minutes=14) : curr_time]
            except: 
                return None
        return None

    def _prepare_1m_data(self, universe_schedule, start_date, end_date, callback):
        # [REMOVED] v23.0부터는 지연 로딩을 사용하므로 사전 로드가 불필요함
        return {}

    def _process_intra_bar_dynamic(self, code, m1_chunk, day_df_15m, m_trend, today_sells, curr_15m_time):
        """[v36.2] 통합된 1분봉 정밀 시뮬레이션 (매도/매수 순차 처리 및 지표 동기화)"""
        # [v37.8] 전달받은 curr_15m_time을 사용하여 15분봉 데이터 매칭 (m1_dt_first 대신 사용)
        curr_15m = day_df_15m.loc[curr_15m_time] if day_df_15m is not None and curr_15m_time in day_df_15m.index else None
        
        h_info = self.holdings.get(code)
        
        # [v36.2] 단일 루프 구조로 개편하여 모든 종목에 대해 1분 단위 시뮬레이션 수행
        for m1_idx in range(len(m1_chunk)):
            m1_r = m1_chunk.iloc[m1_idx]
            m1_time = m1_chunk.index[m1_idx]
            
            # [v36.8] 초정밀 1분 지표 시뮬레이션 (Intra-bar Indicator Simulation)
            # 15분봉 고정 지표가 아니라, 현재 1분봉 가격을 반영한 실시간 지표를 매 루프마다 생성
            full_15m = self.cached_15m_data.get(code)
            sim_15m = None
            if full_15m is not None and curr_15m is not None:
                # 현재 15분봉(curr_15m) 직전까지의 데이터만 슬라이싱
                hist_15m = full_15m.loc[:curr_15m.name].iloc[:-1]
                sim_15m = TradingStrategy.simulate_intrabar_indicators(hist_15m, curr_15m.name, m1_r, self.settings)
            
            # 시뮬레이션 지표(1M 기반)로 RSI/MACD 등을 업데이트하되,
            # 매수/추매 판단에 직접 쓰는 Signal/Ready/Sig_Type은
            # 반드시 현재 타겟 15분봉(curr_15m)의 값을 유지해야 한다.
            # 그렇지 않으면 sim_15m이 참조한 "직전 15분봉"의 Signal 상태가 그대로 남아
            # buy 타점이 영영 발생하지 않을 수 있다.
            if sim_15m is not None and curr_15m is not None:
                for k in ["Signal", "Ready", "Sig_Type", "Volume", "Prev_Day_Vol", "Vol_MA20"]:
                    try:
                        if hasattr(curr_15m, "get"):
                            sim_15m[k] = curr_15m.get(k, sim_15m.get(k))
                        else:
                            sim_15m[k] = sim_15m.get(k)
                    except Exception:
                        pass
                indicator_to_use = sim_15m
            else:
                indicator_to_use = curr_15m

            # 1. 매도(Exit) 감시 - 보유 중인 경우 최우선 순위
            if h_info is not None:
                # 고가 갱신 (익절/트레일링용)
                if m1_r['High'] > h_info['max_p']: h_info['max_p'] = m1_r['High']
                
                # 1분 단위 정밀 매도 평가 (indicator_to_use에 시뮬레이션 데이터 전달)
                res = TradingStrategy.evaluate_position(
                    code, m1_r['Close'], h_info, self.settings,
                    indicator_row=indicator_to_use, # [v36.8] 시뮬레이션된 15분 지표 사용
                    current_time=m1_time,
                    is_realtime=False,
                    kwargs={'bid_ask_ratio': m1_r.get('bid_ask_ratio', 1.0), 'vol_power': m1_r.get('vol_power', 100.0)}
                )
                
                if res['action'] == 'EXIT':
                    e_price = m1_r['Close']
                    # 익절(TP)인 경우 지침에 따라 가격 보정 (평단 + 목표익절%)
                    if res['reason'] == 'TAKE_PROFIT':
                        is_type_b_h = "TYPE_B" in str(h_info.get('sig_type', ''))
                        tp_key = 's2_take_profit' if h_info['entry_n'] == 2 else ('s1_b_take_profit' if is_type_b_h else 's1_a_take_profit')
                        tp_val = self.settings.get(tp_key, 5.0)
                        e_price = h_info['avg_p'] * (1 + float(tp_val)/100.0)
                    
                    reason = f"1M_{res['reason']}+{m1_idx}m"
                    self._execute_sell(code, e_price, m1_time, reason, indicator_row=indicator_to_use)
                    today_sells.add(code)
                    return # 매도 발생 시 해당 15분 블록 내 추가 작업 중단

            # 2. 매수(Buy/Addon) 감시 - 매수 신호가 있거나 추가매수 타겟인 경우
            if indicator_to_use is not None:
                # [v36.8] 시뮬레이션 지표 기반으로 신호 실시간 감지 (RSI, MACD 등)
                is_buy_signal = (indicator_to_use.get('Signal', False) and indicator_to_use.get('Ready', False))
                is_addon_target = (h_info is not None and h_info['entry_n'] < 2)
                
                if is_buy_signal or is_addon_target:
                    sig_type = indicator_to_use.get('Sig_Type', 'TYPE_B')
                    use_sb_for_type = self.use_sb_a if (sig_type == "TYPE_A" and not is_addon_target) else \
                                       self.use_sb_b if (sig_type == "TYPE_B" and not is_addon_target) else \
                                       self.use_sb_s2
                    
                    can_execute = True
                    reason_base = "1M_SIG" if not is_addon_target else f"1M_ADDON(S2)|R:{indicator_to_use.get('RSI',0):.1f}|O:{indicator_to_use.get('OSC',0):.1f}"
                    
                    if is_addon_target:
                        if not self._check_s2_approval(code, h_info, indicator_to_use, market_trend=m_trend):
                            can_execute = False 
                    
                    if can_execute and use_sb_for_type:
                        # [High Precision] 1분봉의 개별 호가/체결강도 데이터 사용
                        ab_ratio = m1_r.get('ask_bid_ratio', 1.0 / (m1_r.get('bid_ask_ratio', 1.0) + 1e-10))
                        v_power = m1_r.get('vol_power', 100.0)
                        if ab_ratio < self.sb_ba or v_power < self.sb_vp:
                            can_execute = False
                    
                    if can_execute:
                        reason = reason_base
                        if use_sb_for_type:
                            reason = "SmartBuy|" + reason
                        # [v36.0] 1분 단위 타임스탬프 기록
                        reason += f"+{m1_idx}m"
                        executed = self._handle_buy_execution(code, {'reason': reason, 'Sig_Type': sig_type}, m1_time, m1_r, indicator_row=indicator_to_use)
                        if executed:
                            return # 매수 실제 체결된 경우에만 해당 15분 청크 종료

            # [v36.8] 사용자 요청에 따라 'REJECTED_SIG' 리포트 로그 생성 중단 (클러터 제거)
            # 15분 내내 조건을 만족하지 못한 경우라도 리포트에는 실제 체결 내역만 남김

    def _check_s2_approval(self, code, h_info, last_row, indicator_row=None, market_trend='UP'):
        """[v22.1] 2차 매수 승인 로직 (backtest_engine.TradingStrategy._check_s2_approval 통합 연동)"""
        s = self.settings
        ind = indicator_row if indicator_row is not None else last_row
        curr_price = last_row['Close'] if hasattr(last_row, '__getitem__') else 0

        # --- 진단용 조건별 평가 (Stage 2 거절 원인 추적) ---
        reasons = []

        # 0. 시장 필터
        if s.get('use_market_filter', True) and market_trend != 'UP':
            reasons.append(f"MARKET:{market_trend}")

        # 1. 24시간 대기
        if s.get('use_s2_delay', True):
            try:
                entry_dt = pd.to_datetime(h_info['entry_date'])
                curr_dt = pd.to_datetime(last_row.name)
                elapsed_h = (curr_dt - entry_dt).total_seconds() / 3600
                min_hours = float(s.get('s2_delay_hours', 24))
                if elapsed_h < min_hours:
                    reasons.append(f"DELAY:{elapsed_h:.1f}h<{min_hours}h")
            except Exception:
                pass

        # 2. MA20 위치
        if s.get('use_s2_ma20', True):
            ma20 = ind.get('MA20', 0) if hasattr(ind, 'get') else 0
            gap = float(s.get('s2_ma20_gap', 0.0))
            if curr_price < ma20 * (1 + gap / 100.0):
                reasons.append(f"MA20:{curr_price}<{ma20*(1+gap/100.0):.0f}")

        # 3. MA5 지지
        if s.get('use_s2_ma5', True):
            ma5 = ind.get('MA5', 0) if hasattr(ind, 'get') else 0
            gap = float(s.get('s2_ma5_gap', -0.5))
            if curr_price < ma5 * (1 + gap / 100.0):
                reasons.append(f"MA5:{curr_price}<{ma5*(1+gap/100.0):.0f}")

        # 4. 거래량
        if s.get('use_s2_vol_surge', True):
            vol = ind.get('Volume', 0) if hasattr(ind, 'get') else 0
            vol_tgt = ind.get('Prev_Day_Vol', ind.get('Vol_MA20', ind.get('Volume', 0))) if hasattr(ind, 'get') else 0
            ratio = float(s.get('s2_vol_ratio', 1.5))
            if vol < vol_tgt * ratio:
                reasons.append(f"VOL:{vol}<{vol_tgt*ratio:.0f}")

        # 5. RSI
        if s.get('use_s2_rsi_sig', True):
            rsi_val = ind.get('RSI', 0) if hasattr(ind, 'get') else 0
            rsi_sig = ind.get('RSI_Sig', 50) if hasattr(ind, 'get') else 50
            rsi_prev = ind.get('Prev_RSI', rsi_val) if hasattr(ind, 'get') else rsi_val
            gap = float(s.get('s2_rsi_gap', 0.0))
            if rsi_val < rsi_sig * (1 + gap / 100.0) or rsi_val <= rsi_prev:
                reasons.append(f"RSI:{rsi_val:.1f}<sig{rsi_sig:.1f}orDn")

        # 6. MACD/OSC
        if s.get('use_s2_macd_gc', True):
            macd_val = ind.get('MACD', 0) if hasattr(ind, 'get') else 0
            macd_sig = ind.get('MACD_Sig', 0) if hasattr(ind, 'get') else 0
            osc_val = ind.get('OSC', 1) if hasattr(ind, 'get') else 1
            osc_prev = ind.get('Prev_OSC', osc_val) if hasattr(ind, 'get') else osc_val
            gap = float(s.get('s2_macd_gap', 0.0))
            if macd_val < macd_sig + gap or osc_val <= 0 or osc_val <= osc_prev:
                reasons.append(f"MACD:{macd_val:.3f}<sig{macd_sig:.3f}orOSC:{osc_val:.3f}")

        # 7. 수익권
        if s.get('use_s2_profit', True):
            profit_limit = float(s.get('s2_profit_limit', 1.0))
            avg_p = h_info.get('avg_p', curr_price)
            if avg_p > 0:
                profit_pct = (curr_price / avg_p - 1) * 100
                if profit_pct < profit_limit:
                    reasons.append(f"PROFIT:{profit_pct:.2f}%<{profit_limit}%")

        # [Perf] 로그를 파일에 직접 쓰지 않고 메모리 버퍼에 추가 (일별 flush로 I/O 최소화)
        try:
            approved = len(reasons) == 0
            ts = str(getattr(last_row, 'name', ''))[:19]
            avg_p_val = h_info.get('avg_p', 0)
            profit_log = (curr_price / avg_p_val - 1) * 100 if avg_p_val > 0 else 0
            log_line = f"{ts},{code},{'OK' if approved else 'REJECT'},{profit_log:.2f}%,{';'.join(reasons) if reasons else 'ALL_PASS'}\n"
            self._s2_log_buffer.append(log_line)
        except Exception:
            pass

        # backtest_engine 형식을 맞추기 위해 가상의 pos 객체 생성
        pos = {
            'entry_date': h_info['entry_date'],
            'avg_price': h_info['avg_p'],
            'qty': h_info['qty'],
            'invested': h_info['avg_p'] * h_info['qty']
        }
        # backtest_engine의 정적 메서드 호출
        return TradingStrategy._check_s2_approval(pos, last_row, self.settings, indicator_row=indicator_row, market_trend=market_trend)
