import pandas as pd
from datetime import datetime
import traceback
from backtest_engine import TradingStrategy

class SignalEngine:
    """[v25.7] 실시간 체결 데이터 분석 및 시그널 포착 엔진 클래스"""
    def __init__(self, app):
        self.app = app
        self._last_realtime_err = None

    def on_tick_received(self, code, now_price, volume, trde_val):
        """실시간 체결 데이터(Tick) 처리 본체"""
        try:
            if code not in self.app.monitor_info: return
            info = self.app.monitor_info[code]
            
            # 1. 지표 재계산 및 봉 데이터 업데이트
            self._update_realtime_bar(code, now_price, volume)
            
            # 2. 실시간 지표 추출 (RSI, MACD 등)
            rsi_val = self._calculate_realtime_rsi(code)
            
            # 3. 데이터 추출
            open_price = info.get('open', 0)
            if open_price == 0: 
                open_price = now_price
                info['open'] = open_price
            
            diff_val = now_price - open_price
            ratio_val = f"{(diff_val / open_price * 100):+.2f}" if open_price else "0.00"
            
            # 4. 시그널 상태 제어
            tag = ""
            ma60_ratio = 0.0
            
            # RSI 50 돌파 신호 로직
            s_state = self.app.rsi_signals.get(code, {'last_rsi': 0.0, 'signal_time': None})
            last_rsi_f = float(s_state.get('last_rsi', 0.0))
            now_dt = datetime.now()
            
            if last_rsi_f > 0 and last_rsi_f < 50.0 and rsi_val >= 50.0:
                s_state['signal_time'] = now_dt
            
            if s_state['signal_time']:
                if rsi_val < 50.0:
                    s_state['signal_time'] = None
                elif (now_dt - s_state['signal_time']).total_seconds() > self.app.settings.get('rsi_window', 45) * 60:
                    s_state['signal_time'] = None
                else:
                    tag = "rsi_signal"
            
            s_state['last_rsi'] = float(rsi_val)
            self.app.rsi_signals[code] = s_state

            # 5. 전략 기반 매수 신호 포착
            is_buy = False
            rsi_sig, macd_val, osc_val = 0.0, 0.0, 0.0
            if code in self.app.bars_data and len(self.app.bars_data[code]) >= 60:
                df_with_ind = TradingStrategy.calculate_indicators(self.app.bars_data[code], self.app.settings)
                market_trend = 'UP' if all(v.get('trend') == "UP" for v in self.app.market_status.values()) else 'DOWN'
                all_signals = TradingStrategy.get_buy_signals(df_with_ind, self.app.settings, market_trend)
                is_buy = all_signals.iloc[-1]
                
                last_row = df_with_ind.iloc[-1]
                rsi_sig = last_row.get('RSI_Sig', 0)
                macd_val = last_row.get('MACD', 0)
                osc_val = last_row.get('OSC', 0)
                ma60_ratio = (now_price / last_row['MA60']) * 100 if 'MA60' in last_row and last_row['MA60'] > 0 else 0.0
                
                if is_buy:
                    tag = "buy_signal"
                    self.app.log(f"🎯 [BUY SIGNAL] {info['name']}({code}) 통합 전략 타점 포착!")
                    
                    if self.app.settings.get('use_auto_virtual', True) and code not in self.app.virtual_positions:
                        self.app._virtual_buy_stage_1(code, info['name'], now_price)
                elif rsi_val > rsi_sig:
                    tag = "rsi_signal"
                elif (100.0 <= ma60_ratio <= 112.0) or (last_row['MA20'] > last_row['MA60'] and now_price > last_row['MA20']):
                    tag = "highlight_green"

            # 6. 볼륨 스파이크 탐지
            is_vol_spike = self._check_volume_spike(code, volume - info.get('last_vol', volume))
            info['last_vol'] = volume
            
            is_price_changed = False
            if now_price != info.get('last_price', 0):
                is_price_changed = True
                info['last_price'] = now_price

            # 7. 트리뷰 업데이트 큐잉 (UI 업데이트는 App을 통해 수행)
            full_values = (
                f"{info.get('score', 0.0)}", code, info['name'],
                f"{int(open_price):,}", f"{int(now_price):,}", f"{int(diff_val):+d}", f"{ratio_val}%",
                f"{int(volume):,}", f"{trde_val:,}억"
            )
            
            if not tag:
                if diff_val > 0: tag = "price_up"
                elif diff_val < 0: tag = "price_down"
                
            self.app.pending_tree_updates[code] = (full_values, tag, is_price_changed)
            if not self.app._update_timer_active:
                self.app._update_timer_active = True
                self.app.root.after(200, self.app._render_tree_updates)

        except Exception as e:
            if not self._last_realtime_err == str(e):
                print(f"[DEBUG] SignalEngine Process Error ({code}): {e}")
                self._last_realtime_err = str(e)

    def _update_realtime_bar(self, code, price, volume):
        self.app.bars_data = self.app.stream_processor.update_realtime_bar(code, price, volume, self.app.bars_data or {})

    def _calculate_realtime_rsi(self, code):
        """[v15.0] 실시간 봉 데이터를 기반으로 즉시 RSI 산출"""
        if code not in self.app.bars_data: return 0.0
        df = self.app.bars_data[code]
        if len(df) < 15: return 0.0
        
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        
        rs = gain / (loss + 1e-10)
        rsi = 100 - (100 / (1 + rs))
        return rsi.iloc[-1] if not pd.isna(rsi.iloc[-1]) else 0.0

    def _check_volume_spike(self, code, current_vol):
        """실시간 거래량 급증 탐지"""
        if not hasattr(self.app, 'min_volume_profiles') or code not in self.app.min_volume_profiles:
            return False
        now_hm = datetime.now().strftime('%H%M')
        avg_min_vol = self.app.min_volume_profiles[code].get(now_hm, 0)
        return current_vol > (avg_min_vol * 3.0) and current_vol > 5000
