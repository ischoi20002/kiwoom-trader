# -*- coding: utf-8 -*-

import threading
from datetime import datetime
import config

class StrategyEngine:
    """
    [v4.5.3] 보유 중인 포지션의 상태를 분석하고 매매 결정을 내리는 전략 집행 엔진.
    GUI 클래스에서 비즈니스 로직(익절, 손절, 추매 판단)을 완전히 분리합니다.
    """
    def __init__(self, app):
        self.app = app # KiwoomApp 인스턴스 (설정 및 데이터 참조용)
        self.log = app.log
        
    def _get_val(self, data, key, default=0):
        """[v4.5.9] 헬퍼: 데이터(dict/Series)에서 안전하게 단일 스칼라 값 추출"""
        if data is None: return default
        val = data.get(key, default)
        if hasattr(val, 'iloc'): return val.iloc[0]
        return val

    def evaluate_position(self, code, now_price, **kwargs):
        """
        보유 종목에 대한 실시간 상태 평가 및 액션 결정.
        결과 형태: {'action': 'EXIT'/'STAGE2'/'HOLD', 'reason': '...', 'data': {...}}
        """
        if now_price <= 0: return {'action': 'HOLD'}
        if code not in self.app.virtual_positions: return {'action': 'HOLD'}
        
        pos = self.app.virtual_positions[code]
        if pos.get('exit_pending', False): return {'action': 'HOLD'}
        
        # 기본 정보 산출
        stage = pos['stage']
        avg_p = pos['avg_price']
        profit_ratio = (now_price / avg_p - 1) * 100
        pos['current_price'] = now_price
        
        # 최고가 갱신 (ATR 및 고점 대비 하락 체크용)
        max_now = max(pos.get('max_price', now_price), now_price)
        pos['max_price'] = max_now
        
        # 지표 데이터 확보
        ind = kwargs.get('ind') # SignalEngine에서 전달받은 마지막 봉 데이터
        is_power_exhausted = kwargs.get('is_power_exhausted', False)
        bid_ask_ratio = kwargs.get('bid_ask_ratio', 0)
        ask_bid_ratio = kwargs.get('ask_bid_ratio', 0)
        vol_power = kwargs.get('vol_power', 100.0)

        is_exit = False
        exit_reason = ""
        is_type_b = "TYPE_B" in pos.get('reason', '')
        s = self.app.settings

        # ---------------------------------------------------------
        # [1단계] 탈출(Exit) 조건 검사
        # ---------------------------------------------------------
        
        # 0. Gap Down (Global)
        if s.get('use_gap_down_exit', True):
            gap_limit = float(s.get('gap_down_limit', -5.0))
            if profit_ratio <= gap_limit:
                is_exit, exit_reason = True, "GAP_DOWN"

        # 1. 익절(Take Profit)
        if not is_exit:
            use_tp = s.get('use_s2_tp' if stage == 2 else ('use_s1_b_tp' if is_type_b else 'use_s1_a_tp'), True)
            if use_tp:
                tp_pct = float(s.get('s2_take_profit' if stage == 2 else ('s1_b_take_profit' if is_type_b else 's1_a_take_profit'), 5.0))
                if now_price >= avg_p * (1.0 + tp_pct / 100.0):
                    is_exit, exit_reason = True, "TAKE_PROFIT"

        # 2. 손절(Stop Loss - % 기준)
        if not is_exit:
            use_sl = s.get('use_s2_stop_loss' if stage == 2 else ('use_s1_b_stop_loss' if is_type_b else 'use_s1_a_stop_loss'), True)
            if use_sl:
                sl_pct = float(s.get('s2_stop_loss' if stage == 2 else ('s1_b_stop_loss' if is_type_b else 's1_a_stop_loss'), 3.0))
                if now_price <= avg_p * (1.0 - sl_pct / 100.0):
                    is_exit, exit_reason = True, "STOP_LOSS"

        # 3. Smart Exit (Supply/Demand Trap)
        if not is_exit and s.get('use_smart_exit', True):
            threshold = float(s.get('smart_exit_price_threshold', -0.5))
            if now_price <= max_now * (1.0 + threshold / 100.0):
                if is_power_exhausted:
                    min_trap = float(s.get('smart_exit_ba_ratio', 1.3))
                    if bid_ask_ratio >= min_trap:
                        is_exit, exit_reason = True, "SMART_EXIT"

        # 4. ATR 기반 손절 및 트레일링 스톱
        if not is_exit and s.get('use_atr_exit', False):
            curr_atr = float(self._get_val(ind, 'ATR', 0))
            entry_atr = float(pos.get('atr_at_entry', curr_atr))
            if curr_atr > 0:
                suff = "_s2" if stage == 2 else ("_b" if is_type_b else "_a")
                sl_mult = float(s.get(f'atr_multiplier_sl{suff}', 2.5))
                trail_mult = float(s.get(f'atr_multiplier_trail{suff}', 4.0))
                use_sl_atr = s.get(f'use_atr_stop_loss{suff}', True)
                use_trail_atr = s.get(f'use_atr_trailing_stop{suff}', True)

                if use_sl_atr and now_price <= avg_p - (sl_mult * entry_atr):
                    is_exit, exit_reason = True, "ATR_SL"
                elif use_trail_atr and now_price <= max_now - (trail_mult * curr_atr):
                    is_exit, exit_reason = True, "ATR_TRAIL"

        if is_exit:
            # [v4.5.10] 개장 초기 매도 유예 (9:15까지 휩소 방어)
            if s.get('use_gap_grace', True):
                now = datetime.now()
                if now.hour == 9 and now.minute < 15:
                    # 익절(TAKE_PROFIT)을 제외한 모든 손실성 매도는 유예
                    if exit_reason != "TAKE_PROFIT":
                        return {'action': 'HOLD', 'reason': f'GRACE_PERIOD({exit_reason})'}

            return {'action': 'EXIT', 'reason': exit_reason, 'profit_ratio': profit_ratio}

        # ---------------------------------------------------------
        # [2단계] 추가 매수(Stage 2 Scale-in) 조건 검사
        # ---------------------------------------------------------
        if stage == 1:
            # 0. 시장지수 필터
            if s.get('use_market_filter', True):
                if getattr(self.app, 'market_trend', 'NEUTRAL') in ['NEUTRAL', 'DOWN']:
                    return {'action': 'HOLD'}

            # 1. 시간 지연 필터 (Stage 2 Delay)
            if s.get('use_s2_delay', True):
                min_h = float(s.get('s2_delay_hours', 24.0))
                if pos.get('entry_time'):
                    elapsed = (datetime.now() - datetime.fromisoformat(pos['entry_time'])).total_seconds() / 3600
                    if elapsed < min_h: return {'action': 'HOLD'}

            # 2. 기술적 합치도 평가
            can_addon = True
            if ind is not None:
                if s.get('use_s2_ma20', True):
                    gap = float(s.get('s2_ma20_gap', 0.0))
                    ma20 = float(self._get_val(ind, 'MA20', 0))
                    if now_price < ma20 * (1 + gap/100.0): can_addon = False
                if can_addon and s.get('use_s2_ma5', True):
                    gap = float(s.get('s2_ma5_gap', -0.5))
                    ma5 = float(self._get_val(ind, 'MA5', 0))
                    if now_price < ma5 * (1 + gap/100.0): can_addon = False
                if can_addon and s.get('use_s2_vol_surge', True):
                    vol_ma20 = float(self._get_val(ind, 'Vol_MA20', self._get_val(ind, 'Volume', 0)))
                    curr_vol = float(self._get_val(ind, 'Volume', 0))
                    if curr_vol < vol_ma20 * float(s.get('s2_vol_ratio', 1.0)): can_addon = False
                if can_addon and s.get('use_s2_rsi_sig', True):
                    r_sig = float(self._get_val(ind, 'RSI_Sig', 50))
                    rsi = float(self._get_val(ind, 'RSI', 0))
                    prev_rsi = float(self._get_val(ind, 'Prev_RSI', 0))
                    if rsi < r_sig * (1 + float(s.get('s2_rsi_gap', 0.0))/100.0) or rsi <= prev_rsi: 
                        can_addon = False
                if can_addon and s.get('use_s2_macd_gc', True):
                    m_sig = float(self._get_val(ind, 'MACD_Sig', 0))
                    macd = float(self._get_val(ind, 'MACD', 0))
                    osc = float(self._get_val(ind, 'OSC', 0))
                    prev_osc = float(self._get_val(ind, 'Prev_OSC', 0))
                    if macd < m_sig + float(s.get('s2_macd_gap', 0.0)) or osc <= 0 or osc <= prev_osc: 
                        can_addon = False

            # 3. 수급 컨펌 (Smart Buy S2)
            if can_addon and s.get('use_smart_buy_s2', True):
                if vol_power < float(s.get('smart_buy_vp', 120.0)) or ask_bid_ratio < float(s.get('smart_buy_ba_ratio', 1.5)):
                    can_addon = False
                    
            # 4. 수익률 트리거 확인
            if can_addon:
                s2_trigger = float(s.get('s2_profit_limit', 1.0))
                if profit_ratio >= s2_trigger:
                    return {'action': 'STAGE2', 'reason': 'PROFIT_TRIGGER', 'profit_ratio': profit_ratio}

        return {'action': 'HOLD'}

    def calculate_confluence_status(self, code, now_price, ind, vol_power, ask_bid_ratio):
        """[v22.0] 실시간 감시창의 '상태' 컬럼에 표시할 합합도 텍스트 산출"""
        if code not in self.app.virtual_positions: return "-", 0.0
        pos = self.app.virtual_positions[code]
        if pos.get('stage', 1) == 2:
            profit = (now_price / pos['avg_price'] - 1) * 100
            return f"보유 ({profit:+.1f}%)", 1.0
            
        s = self.app.settings
        total, met = 0, 0
        
        # 0. 시장지수
        if s.get('use_market_filter', True):
            total += 1
            if getattr(self.app, 'market_trend', 'NEUTRAL') == 'UP': met += 1
        
        # 1. 시간 지연
        if s.get('use_s2_delay', True):
            total += 1
            min_h = float(s.get('s2_delay_hours', 24.0))
            if pos.get('entry_time') and (datetime.now() - datetime.fromisoformat(pos['entry_time'])).total_seconds() / 3600 >= min_h:
                met += 1
        
        # 2. 수익률 트리거
        if s.get('use_s2_profit', True):
            total += 1
            profit = (now_price / pos['avg_price'] - 1) * 100
            if profit >= float(s.get('s2_profit_limit', 1.0)): met += 1
        
        # 3. 기술적 지표 (MA20, MA5, RSI, MACD)
        if ind is not None:
            if s.get('use_s2_ma20', True):
                total += 1
                ma20 = float(self._get_val(ind, 'MA20', 0))
                if now_price >= ma20 * (1 + float(s.get('s2_ma20_gap', 0.0))/100.0): met += 1
            if s.get('use_s2_ma5', True):
                total += 1
                ma5 = float(self._get_val(ind, 'MA5', 0))
                if now_price >= ma5 * (1 + float(s.get('s2_ma5_gap', -0.5))/100.0): met += 1
            if s.get('use_s2_rsi_sig', True):
                total += 1
                rsi = float(self._get_val(ind, 'RSI', 0))
                rsi_sig = float(self._get_val(ind, 'RSI_Sig', 50))
                prev_rsi = float(self._get_val(ind, 'Prev_RSI', 0))
                if rsi >= rsi_sig and rsi > prev_rsi: met += 1
            if s.get('use_s2_macd_gc', True):
                total += 1
                macd = float(self._get_val(ind, 'MACD', 0))
                macd_sig = float(self._get_val(ind, 'MACD_Sig', 0))
                osc = float(self._get_val(ind, 'OSC', 0))
                prev_osc = float(self._get_val(ind, 'Prev_OSC', 0))
                if macd >= macd_sig and osc > 0 and osc > prev_osc: met += 1

        # 4. 수급 합치도
        if s.get('use_smart_buy_s2', True):
            total += 1
            if vol_power >= float(s.get('smart_buy_vp', 120.0)) and ask_bid_ratio >= float(s.get('smart_buy_ba_ratio', 1.5)):
                met += 1
                
        ratio = met / total if total > 0 else 0
        return f"추매 ({met}/{total})", ratio

    def recalculate_indicators(self, code, bars_data, settings):
        """[v4.5.3] 특정 종목의 모든 기술적 지표를 최신 설정으로 재계산합니다."""
        df = bars_data.get(code)
        if df is None or df.empty: return None
        try:
            from backtest_engine import TradingStrategy
            # [v31.0] 전체 지표 통합 계산 (ATR 포함)
            updated_df = TradingStrategy.calculate_indicators(df, settings)
            bars_data[code] = updated_df
            return updated_df
        except Exception as e:
            print(f"[DEBUG] Indicator Recalc Error ({code}): {e}")
            return None
