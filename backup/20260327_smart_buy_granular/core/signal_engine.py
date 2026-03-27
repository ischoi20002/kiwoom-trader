import pandas as pd
from datetime import datetime
import traceback
from backtest_engine import TradingStrategy

class SignalEngine:
    """[v25.7] 실시간 체결 데이터 분석 및 시그널 포착 엔진 클래스"""
    def __init__(self, app):
        self.app = app
        self._last_realtime_err = None

    def on_tick_received(self, code, now_price, volume, trde_val, **kwargs):
        """실시간 체결 데이터(Tick) 처리 본체"""
        try:
            if code not in self.app.monitor_info: return
            info = self.app.monitor_info[code]
            
            # Update additional fields from kwargs (vol_power, total_ask, total_bid 등)
            for k, v in kwargs.items():
                info[k] = v
            
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
            info['confluence'] = info.get('confluence', '-')
            
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

            # [Phase 5] 수급 분석 (Orderbook Analysis)
            total_ask = info.get('total_ask', 0)
            total_bid = info.get('total_bid', 1) # Prevent div by zero
            ask_bid_ratio = total_ask / total_bid if total_bid > 0 else 0
            bid_ask_ratio = total_bid / total_ask if total_ask > 0 else 0
            
            # 체결강도 하회 지속 관리
            vol_power = info.get('vol_power', 100.0)
            is_power_exhausted = False
            if vol_power < self.app.settings.get('smart_exit_vp', 100.0):
                if info.get('power_below_start') is None:
                    info['power_below_start'] = now_dt
                else:
                    duration = (now_dt - info['power_below_start']).total_seconds() / 60.0
                    if duration >= 2: # Keep 2 mins as standard for now or use a new key
                        is_power_exhausted = True
            else:
                info['power_below_start'] = None

            # 5. 전략 기반 매수 신호 포착
            is_buy = False
            rsi_sig, macd_val, osc_val = 0.0, 0.0, 0.0

            if code in self.app.bars_data and len(self.app.bars_data[code]) >= 60:
                df_with_ind = TradingStrategy.calculate_indicators(self.app.bars_data[code], self.app.settings)
                market_trend = getattr(self.app, 'market_trend', 'UP')
                detailed_signals = TradingStrategy.get_buy_signals_detailed(df_with_ind, self.app.settings, market_trend)
                last_sig = detailed_signals.iloc[-1]
                is_buy = last_sig['Signal']
                met_c = int(last_sig['Met_Count'])
                total_c = int(last_sig['Total_Count'])
                info['confluence'] = f"대기 ({met_c}/{total_c})"
                info['met_ratio'] = last_sig['Met_Ratio']
                
                last_row = df_with_ind.iloc[-1]
                rsi_sig = last_row.get('RSI_Sig', 0)
                macd_val = last_row.get('MACD', 0)
                osc_val = last_row.get('OSC', 0)
                ma60_ratio = (now_price / last_row['MA60']) * 100 if 'MA60' in last_row and last_row['MA60'] > 0 else 0.0
                
                if is_buy:
                    # [Phase 5] 1단계 진입 시 'Smart Buy' 전술적 수급 컨펌
                    sig_type = last_sig.get('Sig_Type', 'TYPE_B')
                    use_sb_for_type = self.app.settings.get('use_smart_buy_a', True) if sig_type == "TYPE_A" else \
                                       self.app.settings.get('use_smart_buy_b', True)

                    if use_sb_for_type:
                        min_p = self.app.settings.get('smart_buy_vp', 120.0)
                        min_r = self.app.settings.get('smart_buy_ba_ratio', 1.5)
                        if vol_power < min_p or ask_bid_ratio < min_r:
                            is_buy = False
                    
                    if is_buy:
                        self.app.log(f"[Signal] {info['name']}({code}) 통합 전략 타점 포착!")
                        if self.app.settings.get('use_auto_virtual', True) and code not in self.app.virtual_positions:
                            self.app._virtual_buy_stage_1(code, info['name'], now_price)

            # 6. 볼륨 스파이크 탐지
            last_vol = info.get('last_vol', volume)
            is_vol_spike = self._check_volume_spike(code, volume - last_vol)
            info['last_vol'] = volume

            # 7. 실시간 가상 포지션 시나리오 처리 (익절/손절/추매)
            self.app._process_virtual_scenarios(
                code, now_price, rsi_val, macd_val, osc_val, 
                vol_power, is_vol_spike, 
                ind=last_row if 'last_row' in locals() else None,
                is_power_exhausted=is_power_exhausted,
                bid_ask_ratio=bid_ask_ratio,
                ask_bid_ratio=ask_bid_ratio
            )

            # 8. 거래량 변동성 체크
            is_price_changed = False
            if now_price != info.get('last_price', 0):
                is_price_changed = True
                info['last_price'] = now_price

            # 7. 트리뷰 업데이트 큐잉 (UI 업데이트는 App을 통해 수행)
            sign = "▲" if diff_val > 0 else "▼" if diff_val < 0 else ""
            abs_ratio = abs(diff_val / open_price * 100) if open_price else 0
            
            full_values = (
                f"{info.get('score', 0.0):.1f}", code, info['name'],
                f"{int(now_price):,}", f"{sign} {abs_ratio:.2f}%" if sign else "0.00%",
                f"{info.get('vol_power', 0.0):.1f}%", 
                info.get('confluence', '-'),
                f"{int(info.get('total_ask', 0)):,}",
                f"{int(info.get('total_bid', 0)):,}",
                f"{int(volume):,}", f"{trde_val:,}"
            )
            
            # [v3.5] 심박수(Heartbeat) 업데이트
            if hasattr(self.app, 'lbl_heartbeat'):
                self.app.lbl_heartbeat.config(text=f"💓 심박수: {datetime.now().strftime('%H:%M:%S')}")

            # [v27.9] 블링크(Blink) 포기 및 단순화: 시세 방향만 표시
            p_tag = "price_up" if diff_val > 0 else ("price_down" if diff_val < 0 else "price_stable")
            
            final_tags = [p_tag]
            if tag: 
                final_tags.insert(0, tag)
            
            # [v3.5] 합치도 기반 색상 태그 추가
            m_ratio = info.get('met_ratio', 0)
            if is_buy:
                info['confluence'] = "🔥 타점 포착"
                final_tags.append("status_high")
            elif m_ratio >= 0.7:
                final_tags.append("status_high")
            else:
                final_tags.append("status_low")
                
            # [v27.9] is_blink 플래그를 항상 False로 전달
            self.app.pending_tree_updates[code] = (full_values, tuple(final_tags), False)
            if not self.app._update_timer_active:
                self.app._update_timer_active = True
                self.app.root.after(100, self.app._render_tree_updates)

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
