import pandas as pd
import os
import time
from datetime import datetime, timedelta
from data_manager_v2 import DataManagerV2
import strategy_selector
import backtest_engine as bt
import importlib
importlib.reload(bt) # [Force] 전 엔진 로직 업데이트 반영 (AttributeError 방지)
import numpy as np
from backtest_engine import TradingStrategy, csv_lock, SIGNAL_CSV, TRADE_CSV, BASE_DIR

class DynamicBacktester:
    """[v16.0] 동적 포트폴리오 백테스터: 매일 종목을 새로 선정하고 1,000만원 예산을 공유하여 운용"""
    def __init__(self, settings, total_budget=None):
        self.settings = settings
        self.db = DataManagerV2("trading_data.db")
        # [v16.7] 설정에서 예산 가져오기 (기본값 1000만)
        budget = total_budget if total_budget is not None else float(settings.get('total_capital', 10_000_000))
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
        
    def run(self, start_date, end_date, interval='15m', callback=None, high_precision=False):
        """메인 시뮬레이션 루프 (일단위 유니버스 갱신 + 분단위/15분단위 체결)"""
        self.high_precision = high_precision
        # [v18.8] Date Normalization (YYYY-MM-DD)
        start_date = pd.to_datetime(start_date).strftime('%Y-%m-%d')
        end_date = pd.to_datetime(end_date).strftime('%Y-%m-%d')
        
        self.start_date = start_date
        self.end_date = end_date
        if callback: callback("백테스트 준비 중...", 0)
        
        # 1. 기초 데이터 (지수 및 VIX) 로드
        if callback: callback("시장지수 및 VIX 데이터 로드 중...", 2)
        market_df = self.db.get_daily_bars('KS11', start_date, end_date)
        vix_df = self.db.get_daily_bars('^VIX', start_date, end_date)
        if market_df is None or market_df.empty:
            print("[Error] [Dynamic] 시장 데이터(KS11)가 없어 백테스트를 진행할 수 없습니다.")
            return None
            
        market_df['time'] = pd.to_datetime(market_df['time'])
        market_df.set_index('time', inplace=True)
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
            
            # 2-2. 시장 트렌드 판단
            m_trend = 'UP'
            try:
                m_hist = market_df.loc[:pd.to_datetime(current_day)].tail(60)
                if len(m_hist) >= 60:
                    ma60 = m_hist['close'].mean()
                    if m_hist['close'].iloc[-1] < ma60: m_trend = 'DOWN'
            except: pass

            # 2-3. 당일 데이터 및 지표 추출
            day_data = {}
            day_signals = {}
            for code in monitoring_list:
                if code not in self.cached_15m_data: continue
                
                base_df = self.cached_15m_data[code]
                day_base = base_df.loc[current_day:current_day + " 23:59:59"].copy()
                if day_base.empty: continue
                
                # 당일 시그널 계산 (m_trend 반영)
                sigs = TradingStrategy.get_buy_signals_detailed(day_base, self.settings, m_trend)
                # 시그널 정보를 당일 데이터와 결합
                day_full = day_base.join(sigs.drop(columns='Signal', errors='ignore'))
                day_full['Signal'] = sigs['Signal']
                
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
                            m_trend_val = m_trend # outer scope
                            self._process_intra_bar_dynamic(code, m1_chunk, day_data.get(code), m_trend_val, today_sells)
                            m1_processed.add(code)

                # 2-4-1. 매도 감시
                for code, h_info in list(self.holdings.items()):
                    if code in today_sells or code in m1_processed: continue
                    d_df = day_data.get(code)
                    if d_df is None or curr_time not in d_df.index: continue
                    
                    curr_price_row = d_df.loc[curr_time]
                    curr_price = curr_price_row['Close']
                    h_info['max_p'] = max(h_info.get('max_p', 0), curr_price)
                    
                    ret_pct = (curr_price / h_info['avg_p'] - 1) * 100
                    exit_reason = ""
                    
                    g_lmt = float(self.settings.get('gap_down_limit', -5.0))
                    if ret_pct <= g_lmt:
                        exit_reason = f"Gap Down Limit ({ret_pct:.1f}%)|RSI:{curr_price_row['RSI']:.1f}|MA5:{int(curr_price_row['MA5']):,}|OSC:{curr_price_row['OSC']:.2f}"

                    if not exit_reason:
                        if h_info['entry_n'] == 1:
                            sig_type = h_info.get('sig_type', 'TYPE_A')
                            suffix = "_a" if sig_type == "TYPE_A" else "_b"
                            if self.settings.get('use_s1_flash_exit', True):
                                f_pct = float(self.settings.get(f's1{suffix}_flash_exit_pct', 3.0))
                                if curr_price < h_info['max_p'] * (1 - f_pct/100):
                                    drop_pct = (curr_price / h_info['max_p'] - 1) * 100
                                    exit_reason = f"Flash Exit (S1-{sig_type})|MaxP:{int(h_info['max_p']):,}|Drop:{drop_pct:.1f}%"
                            
                            if not exit_reason and self.settings.get('use_s1_take_profit', True):
                                s1_tp = float(self.settings.get(f's1{suffix}_take_profit', 5.0))
                                if ret_pct >= s1_tp:
                                    exit_reason = f"Take Profit (S1-{sig_type})|RSI:{curr_price_row['RSI']:.1f}|MA5:{int(curr_price_row['MA5']):,}|OSC:{curr_price_row['OSC']:.2f}"

                            if not exit_reason and self.settings.get('use_s1_stop_loss', True):
                                s1_sl = float(self.settings.get(f's1{suffix}_stop_loss', 3.0))
                                if ret_pct <= -s1_sl:
                                    exit_reason = f"Stop Loss (S1-{sig_type})|RSI:{curr_price_row['RSI']:.1f}|MA5:{int(curr_price_row['MA5']):,}|OSC:{curr_price_row['OSC']:.2f}"
                        else:  # entry_n == 2
                            if self.settings.get('use_s2_flash_exit', True):
                                f_pct = float(self.settings.get('s2_flash_exit_pct', 3.0))
                                if curr_price < h_info['max_p'] * (1 - f_pct/100):
                                    drop_pct = (curr_price / h_info['max_p'] - 1) * 100
                                    exit_reason = f"Flash Exit (S2)|MaxP:{int(h_info['max_p']):,}|Drop:{drop_pct:.1f}%"
                            
                            if not exit_reason and self.settings.get('use_s2_take_profit', True):
                                s2_tp = float(self.settings.get('s2_take_profit', 5.0))
                                if ret_pct >= s2_tp:
                                    exit_reason = f"Take Profit (S2)|RSI:{curr_price_row['RSI']:.1f}|MA5:{int(curr_price_row['MA5']):,}|OSC:{curr_price_row['OSC']:.2f}"

                            if not exit_reason and self.settings.get('use_s2_stop_loss', True):
                                s2_sl = float(self.settings.get('s2_stop_loss', 2.0))
                                if ret_pct <= -s2_sl:
                                    exit_reason = f"Stop Loss (S2)|RSI:{curr_price_row['RSI']:.1f}|MA5:{int(curr_price_row['MA5']):,}|OSC:{curr_price_row['OSC']:.2f}"
                    
                    if exit_reason:
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
                                if self._check_s2_approval(code, h_info, last_sig):
                                    reason = f"ADDON(S2)|R:{last_sig.get('RSI',0):.1f}|M:{last_sig.get('MACD',0):.1f}|O:{last_sig.get('OSC',0):.1f}|V:{int(last_sig.get('Volume',0))}"
                                else:
                                    can_execute = False
                            
                            if can_execute:
                                self._handle_buy_execution(code, {'reason': reason, 'Sig_Type': last_sig.get('Sig_Type', 'TYPE_B')}, curr_time, d_df.loc[curr_time], indicator_row=d_df.loc[curr_time])

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
                    self.trades_history.append({
                        'Date': last_time, 'Code': code, 'Name': TradingStrategy.get_stock_name_map().get(code, code), 
                        'Action': 'HOLD', 'Price': last_price, 'Qty': h_info['qty'], 'Amount': h_info['qty'] * last_price, 
                        'Return': ret, 'Reason': reason, 'Cash': self.cash, 'Fee': sell_fee
                    })
        return self._finalize_backtest(interval)

    def _handle_buy_execution(self, code, sig_res, curr_time, last_row, indicator_row=None):
        price = last_row['Close']
        h_info = self.holdings.get(code)
        s1_amt = float(self.settings.get('stage1_amt', 1_000_000))
        s2_amt = float(self.settings.get('stage2_amt', 2_000_000))

        if h_info is None:
            if self.cash < s1_amt * 0.5: return 
            target_amt = s1_amt
            entry_n = 1
            sig_type = sig_res.get('Sig_Type', 'TYPE_A')
        else:
            if h_info['entry_n'] >= 2: return
            if not self._check_s2_approval(code, h_info, last_row, indicator_row=indicator_row): return
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
            self.holdings[code] = {'qty': qty, 'avg_p': price, 'entry_n': 1, 'max_p': price, 'entry_date': curr_time, 'sig_type': sig_type}
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
            'Date': curr_time, 'Code': code, 'Name': TradingStrategy.get_stock_name_map().get(code, code),
            'Action': f'BUY{entry_n}', 'Price': price, 'Qty': qty, 'Amount': real_amt, 'Fee': buy_fee,
            'Reason': reason_str, 'Cash': self.cash
        })
        self._log_trade_to_csv(curr_time, code, f'BUY{entry_n}', price, qty, reason_str, fee=buy_fee)

    def _execute_sell(self, code, price, curr_time, reason, ratio=1.0, indicator_row=None):
        """매도 실행 및 기록 (v8.1: 툴팁용 지표 데이터 포함)"""
        if indicator_row is not None:
            try:
                rsi_val = indicator_row.get('RSI', 0)
                ma5_val = indicator_row.get('MA5', 0)
                osc_val = indicator_row.get('OSC', 0)
                if "RSI:" not in str(reason):
                    prefix = "|" if "|" not in str(reason) else ", "
                    reason += f"{prefix}RSI:{rsi_val:.1f}|MA5:{int(ma5_val):,}|OSC:{osc_val:.2f}"
            except: pass
            
        h_info = self.holdings.get(code)
        if h_info is None: return
        sell_qty = int(h_info['qty'] * ratio)
        if sell_qty <= 0: return
        
        if ratio >= 1.0 or sell_qty >= h_info['qty']:
            sell_qty = h_info['qty']
            h_pop = self.holdings.pop(code)
            action = 'SELL'
        else:
            h_info['qty'] -= sell_qty
            h_pop = h_info
            action = 'PARTIAL_SELL'

        gross = sell_qty * price
        fee = int(gross * self.sell_f_rate)
        ret = (price / h_pop['avg_p'] - 1) * 100
        self.cash += (gross - fee)
        
        self.trades_history.append({
            'Date': curr_time, 'Code': code, 'Name': TradingStrategy.get_stock_name_map().get(code, code),
            'Action': action, 'Price': price, 'Qty': sell_qty, 'Amount': gross, 'Fee': fee,
            'Reason': reason, 'Return': ret, 'Cash': self.cash
        })
        self._log_trade_to_csv(curr_time, code, action, price, sell_qty, reason, ret=ret, fee=fee)

    def _log_trade_to_csv(self, dt, code, action, price, qty, reason, ret=0, fee=0):
        name = TradingStrategy.get_stock_name_map().get(code, code)
        safe_reason = str(reason).replace(',', ';')
        row = [pd.to_datetime(dt).strftime('%Y-%m-%d %H:%M'), code, name, action, price, qty, f"{ret:.2f}", safe_reason, fee]
        with csv_lock:
            if not os.path.exists(TRADE_CSV):
                with open(TRADE_CSV, 'w', encoding='utf-8-sig') as f:
                    f.write("Date,Code,Name,Action,Price,Qty,Return,Reason,Fee\n")
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
            exit_trades = trades_df[trades_df['Action'].isin(['SELL', 'HOLD', 'PARTIAL_SELL'])]
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
                                  market_df=self.db.get_daily_bars('KS11', self.start_date, self.end_date),
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
        query = f"SELECT code, datetime as time, open, high, low, close, volume FROM bars_15m WHERE code IN ({placeholders}) AND datetime BETWEEN ? AND ? ORDER BY code, datetime"
        with self.db._get_connection() as conn:
            df_all = pd.read_sql_query(query, conn, params=all_codes + [pad_start, end_date + " 23:59:59"])
        if df_all.empty: return {}
        df_all.columns = [c.capitalize() if c.lower() != 'time' else 'time' for c in df_all.columns]
        df_all['time'] = pd.to_datetime(df_all['time'])
        df_all.set_index('time', inplace=True)
        cached = {}
        for code, group in df_all.groupby('Code'):
            df_ind = TradingStrategy.calculate_indicators(group, self.settings)
            df_ind['Ready'] = (df_ind.reset_index().index >= 19)
            cached[code] = df_ind
        return cached

    def _get_m1_chunk_on_demand(self, code, date_str, curr_time):
        """[v23.0] 필요한 시점에만 1분봉 데이터를 DB에서 가져와 캐싱 (성능 핵심)"""
        cache_key = (code, date_str)
        if cache_key not in self.cached_1m_data:
            query = "SELECT datetime as time, open, high, low, close, volume FROM bars_1m WHERE code = ? AND datetime BETWEEN ? AND ? ORDER BY datetime"
            with self.db._get_connection() as conn:
                df = pd.read_sql_query(query, conn, params=[code, date_str + " 09:00:00", date_str + " 15:30:00"])
            if df.empty:
                self.cached_1m_data[cache_key] = None
            else:
                df.columns = [c.capitalize() if c.lower() != 'time' else 'time' for c in df.columns]
                df['time'] = pd.to_datetime(df['time'])
                df.set_index('time', inplace=True)
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
            f_pct = 0.0
            if h_info['entry_n'] == 1:
                sig_type = h_info.get('sig_type', 'TYPE_A')
                suffix = "_a" if sig_type == "TYPE_A" else "_b"
                if self.settings.get('use_s1_flash_exit', True):
                    f_pct = float(self.settings.get(f's1{suffix}_flash_exit_pct', 3.0))
            else:
                if self.settings.get('use_s2_flash_exit', True):
                    f_pct = float(self.settings.get('s2_flash_exit_pct', 3.0))
            
            flash_exited = (f_pct > 0) & (m1_closes < running_max * (1 - f_pct/100))
            
            # (B) Stop Loss & Take Profit 조건 계산
            sl_pct = 0.0
            tp_pct = 0.0
            if h_info['entry_n'] == 1:
                sig_type = h_info.get('sig_type', 'TYPE_A')
                suffix = "_a" if sig_type == "TYPE_A" else "_b"
                if self.settings.get('use_s1_stop_loss', True):
                    sl_pct = float(self.settings.get(f's1{suffix}_stop_loss', 3.0))
                if self.settings.get('use_s1_take_profit', True):
                    tp_pct = float(self.settings.get(f's1{suffix}_take_profit', 5.0))
            else:
                if self.settings.get('use_s2_stop_loss', True):
                    sl_pct = float(self.settings.get('s2_stop_loss', 2.0))
                if self.settings.get('use_s2_take_profit', True):
                    tp_pct = float(self.settings.get('s2_take_profit', 5.0))
            
            stop_lossed = (sl_pct > 0) & (m1_closes / h_info['avg_p'] - 1 <= -sl_pct/100)
            take_profited = (tp_pct > 0) & (m1_highs / h_info['avg_p'] - 1 >= tp_pct/100)
            
            # (C) Gap Down 조건 (S1/S2 공통)
            g_lmt = float(self.settings.get('gap_down_limit', -5.0))
            gap_downed = (m1_closes / h_info['avg_p'] - 1 <= g_lmt / 100)
            
            # [v25.8/v25.9] 15-minute Gap-Down Grace Period: Allow recovery between SL/Flash and Gap Limit during first 15m
            if self.settings.get('use_gap_grace', True):
                if m1_times[0].hour == 9 and m1_times[0].minute == 0:
                    grace_mask = (m1_times.hour == 9) & (m1_times.minute < 15)
                    stop_lossed = stop_lossed & (~grace_mask)
                    flash_exited = flash_exited & (~grace_mask)
            
            # 최초 발생 지점 찾기
            exit_mask = flash_exited | stop_lossed | take_profited | gap_downed
            exit_idxs = np.where(exit_mask)[0]
            
            if len(exit_idxs) > 0:
                idx = exit_idxs[0]
                exit_price = m1_closes[idx]
                exit_time = m1_times[idx]
                
                # 상세 사유 결정
                if gap_downed[idx]: reason = "1M Gap Down"
                elif flash_exited[idx]: 
                    drop = (exit_price / running_max[idx] - 1) * 100
                    reason = f"1M Flash Exit (S{h_info['entry_n']})|Drop:{drop:.1f}%"
                elif take_profited[idx]:
                    exit_price = h_info['avg_p'] * (1 + tp_pct/100) # 고전밀에서는 가능하면 TP 가격 체결 가정
                    reason = f"1M Take Profit (S{h_info['entry_n']}-{h_info.get('sig_type','').split(' ')[-1]})"
                else: reason = f"1M Stop Loss (S{h_info['entry_n']}-{h_info.get('sig_type','').split(' ')[-1]})"
                
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
                reason = "1M_SIG" # 일관성 유지
                
                if is_addon_target:
                    if self._check_s2_approval(code, h_info, curr_15m):
                        reason = f"1M_ADDON(S2)|R:{curr_15m.get('RSI',0):.1f}|O:{curr_15m.get('OSC',0):.1f}"
                    else:
                        can_execute = False
                
                if can_execute:
                    self._handle_buy_execution(code, {'reason': reason, 'Sig_Type': curr_15m.get('Sig_Type', 'TYPE_B')}, m1_dt_first, m1_r_first, indicator_row=curr_15m)

    def _check_s2_approval(self, code, h_info, last_row, indicator_row=None):
        """[v22.1] 2차 매수 승인 로직 (backtest_engine.TradingStrategy._check_s2_approval 통합 연동)"""
        # backtest_engine 형식을 맞추기 위해 가상의 pos 객체 생성
        pos = {
            'entry_date': h_info['entry_date'],
            'avg_price': h_info['avg_p'],
            'qty': h_info['qty'],
            'invested': h_info['avg_p'] * h_info['qty']
        }
        # backtest_engine의 정적 메서드 호출
        return TradingStrategy._check_s2_approval(pos, last_row, self.settings, indicator_row=indicator_row)
