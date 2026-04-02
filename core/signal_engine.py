import pandas as pd
import numpy as np
from datetime import datetime
import traceback
from backtest_engine import TradingStrategy
from strategy.simulation_models import SimulatedRealtimeMetrics

class SignalEngine:
    """[v25.7] 실시간 체결 데이터 분석 및 시그널 포착 엔진 클래스"""
    def __init__(self, app):
        self.app = app
        self._last_realtime_err = None
        self.last_buy_sig_time = {} # [v1.0] 중복 시그널 방지용 쿨타임 관리

    def _to_scalar(self, val, default=0):
        """[v4.5.9] 헬퍼: Series/DF/Numpy 등 어떤 형태에서든 안전하게 단일 스칼라 값 추출"""
        if val is None: return default
        # 1. Pandas 객체 처리 (Series, DataFrame)
        if hasattr(val, 'iloc'):
            if hasattr(val, 'size') and val.size == 0: return default
            return self._to_scalar(val.iloc[0], default)
        # 2. Numpy 스칼라 처리
        if hasattr(val, 'item') and not hasattr(val, 'shape'): # 진짜 스칼라인 경우만
            return val.item()
        # 3. 리스트/어레이 처리
        if isinstance(val, (list, np.ndarray)) and len(val) > 0:
            return self._to_scalar(val[0], default)
        return val

    def on_tick_received(self, code, now_price, volume, trde_val, **kwargs):
        """실시간 체결 데이터(Tick) 처리 본체"""
        try:
            if now_price <= 0: return # [v2.2] 유효하지 않은 가격 데이터 로직 진입 차단
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

            # [v35.7] 자가 학습형 합성 모델 보정 (Adaptive Calibration)
            
            # 5. 지표 및 시그널 계산 주기 관리 (100틱마다 또는 분봉 변경 시)
            info['tick_count'] = info.get('tick_count', 0) + 1
            last_bar_count = info.get('last_bar_count', 0)
            current_bar_count = len(self.app.bars_data[code])
            
            should_recalc = (current_bar_count > last_bar_count) or (info['tick_count'] % 100 == 1)
            
            is_buy = False
            last_row = None
            if code in self.app.bars_data and len(self.app.bars_data[code]) >= 60:
                if should_recalc or 'last_detailed_sig' not in info:
                    df_with_ind = TradingStrategy.calculate_indicators(self.app.bars_data[code], self.app.settings)
                    try:
                        from strategy.type_a_regime import apply_type_a_phase1_settings

                        _mt = getattr(self.app, "market_trend", "UP") or "UP"
                        _eff = apply_type_a_phase1_settings(self.app.settings, _mt)
                    except Exception:
                        _eff = self.app.settings
                    info['last_detailed_sig'] = TradingStrategy.get_buy_signals_detailed(
                        df_with_ind, _eff, getattr(self.app, "market_trend", "UP")
                    )
                    info['last_bar_count'] = current_bar_count
                    info['last_row_cache'] = df_with_ind.iloc[-1].to_dict()
                
                detailed_signals = info['last_detailed_sig']
                last_sig = detailed_signals.iloc[-1]
                
                # [v4.5.9] 전방위적 스칼라 쉴드 적용
                is_buy = bool(self._to_scalar(last_sig['Signal'], False))
                met_c = int(self._to_scalar(last_sig['Met_Count'], 0))
                total_c = int(self._to_scalar(last_sig['Total_Count'], 1))
                info['confluence'] = f"대기 ({met_c}/{total_c})"
                info['met_ratio'] = float(self._to_scalar(last_sig['Met_Ratio'], 0.0))
                
                last_row = info['last_row_cache']
                rsi_sig = float(self._to_scalar(last_row.get('RSI_Sig', 0), 0.0))
                macd_val = float(self._to_scalar(last_row.get('MACD', 0), 0.0))
                osc_val = float(self._to_scalar(last_row.get('OSC', 0), 0.0))
                
                ma60_scalar = float(self._to_scalar(last_row['MA60'], 0.0))
                ma60_ratio = (now_price / ma60_scalar) * 100 if ma60_scalar > 0 else 0.0
                
                if is_buy:
                    sig_type = str(self._to_scalar(last_sig.get('Sig_Type', 'TYPE_B'), 'TYPE_B'))
                    use_sb_for_type = self.app.settings.get('use_smart_buy_a', True) if sig_type == "TYPE_A" else self.app.settings.get('use_smart_buy_b', True)
                    
                    if use_sb_for_type:
                        vol_power = float(self._to_scalar(info.get('vol_power', 100.0), 100.0))
                        ask_bid_ratio = float(self._to_scalar(info.get('ask_bid_ratio', 1.0), 1.0))
                        min_p = float(self.app.settings.get('smart_buy_vp', 100.0))
                        # S2·백테스트와 동일 키. 구버전에 smart_buy_ratio만 있으면 폴백
                        _st = self.app.settings
                        min_r = float(
                            _st.get("smart_buy_ba_ratio", _st.get("smart_buy_ratio", 1.5))
                        )
                        
                        if vol_power < min_p or ask_bid_ratio < min_r:
                            is_buy = False
                            info['confluence'] = "수급 대기"
                
                # 쿨타임 및 중복 진입 방지
                is_cool = True
                if code in self.last_buy_sig_time:
                    if (datetime.now() - self.last_buy_sig_time[code]).total_seconds() < 180:
                        is_cool = False
                
                if is_buy and is_cool and code not in self.app.virtual_positions:
                    self.last_buy_sig_time[code] = datetime.now()
                    self.app._on_buy_signal_received(code, now_price, rsi_sig, macd_val, osc_val, ma60_ratio)

            # 7. 실시간 가상 포지션 시나리오 처리
            # is_power_exhausted는 이미 Line 88~95에서 계산됨
            bid_ask_ratio = float(self._to_scalar(info.get('bid_ask_ratio', 1.0), 1.0))
            ask_bid_ratio = float(self._to_scalar(info.get('ask_bid_ratio', 1.0), 1.0))
            vol_power = float(self._to_scalar(info.get('vol_power', 100.0), 100.0))
            
            res = self.app.strategy_engine.evaluate_position(
                code, now_price, 
                ind=last_row,
                is_power_exhausted=is_power_exhausted,
                bid_ask_ratio=bid_ask_ratio,
                ask_bid_ratio=ask_bid_ratio,
                vol_power=vol_power
            )
            
            if res['action'] != 'HOLD':
                self.app._on_strategy_action_needed(code, res['action'], res['reason'], now_price=now_price, profit_ratio=res.get('profit_ratio', 0))

            # 8. 트리뷰 업데이트 큐잉
            p_tag = "price_up" if diff_val > 0 else ("price_down" if diff_val < 0 else "price_stable")
            sign = "▲" if diff_val > 0 else ("▼" if diff_val < 0 else "")
            abs_ratio = abs(diff_val / open_price * 100) if open_price != 0 else 0
            
            full_values = (
                f"{float(self._to_scalar(info.get('score', 0.0), 0.0)):.1f}", code, info['name'],
                f"{int(now_price):,}", f"{sign} {abs_ratio:.2f}%" if sign else "0.00%",
                f"{float(self._to_scalar(info.get('vol_power', 0.0), 0.0)):.1f}%", 
                str(info.get('confluence', '-')),
                f"{int(float(self._to_scalar(info.get('total_ask', 0), 0))):,}",
                f"{int(float(self._to_scalar(info.get('total_bid', 0), 0))):,}",
                f"{int(volume):,}", f"{trde_val:,}"
            )
            
            final_tags = []
            
            # 합치도 기반 색상 태그
            m_ratio = float(self._to_scalar(info.get('met_ratio', 0.0), 0.0))
            if is_buy:
                info['confluence'] = "🔥 타점 포착"
                final_tags.append("status_high")
            elif m_ratio >= 0.7:
                final_tags.append("status_high")
            else:
                final_tags.append("status_low")

            # 가격 태그를 가장 뒤에 추가하여 최우선 적용
            final_tags.append(p_tag)
                
            # [v27.9] is_blink 플래그를 항상 False로 전달
            self.app.pending_tree_updates[code] = (full_values, tuple(final_tags), False)
            if not self.app._update_timer_active:
                self.app._update_timer_active = True
                self.app.root.after(100, self.app._render_tree_updates)

        except Exception as e:
            if not self._last_realtime_err == str(e):
                err_msg = f"⚠️ [SignalEngine] {code} 처리 오류: {e}"
                self.app.log(err_msg)
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
        # [v4.5.9] Series Ambiguity 방어: 볼륨 프로필에서 스칼라값 안전 추출
        avg_min_vol_v = self.app.min_volume_profiles[code].get(now_hm, 0)
        avg_min_vol = float(self._to_scalar(avg_min_vol_v, 0.0))
        
        c_vol = float(self._to_scalar(current_vol, 0.0))
        return bool(c_vol > (avg_min_vol * 3.0) and c_vol > 5000)
