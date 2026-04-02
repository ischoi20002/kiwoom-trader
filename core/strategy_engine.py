# -*- coding: utf-8 -*-

import threading
from datetime import datetime
import config

class StrategyEngine:
    """
    [v4.5.3] 보유 중인 포지션의 상태를 분석하고 매매 결정을 처리하는 전략 집행 엔진.
    GUI 프레임워크에서 비즈니스 로직(익절, 손절, 추매 판단)을 완전히 분리합니다.
    """
    def __init__(self, app):
        self.app = app # KiwoomApp 인스턴스 (설정 및 데이터 참조용)
        self.log = app.log
        
    def _get_val(self, data, key, default=0):
        """[v4.5.9] 헬퍼: 데이터(dict/Series)에서 안전하게 단일 스칼라 값 추출"""
        if data is None: return default
        if hasattr(data, 'get'): return data.get(key, default)
        if hasattr(data, 'iloc'): 
            try: return data.iloc[0].get(key, default)
            except: return data.get(key, default)
        return default

    def evaluate_position(self, code, now_price, **kwargs):
        """
        보유 종목의 현재 시세/상태 분석 및 액션 결정.
        결과 형태: {'action': 'EXIT'/'STAGE2'/'HOLD', 'reason': '...', 'data': {...}}
        """
        if now_price <= 0: return {'action': 'HOLD'}
        if code not in self.app.virtual_positions: return {'action': 'HOLD'}
        
        pos = self.app.virtual_positions[code]
        if pos.get('exit_pending', False): return {'action': 'HOLD'}
        
        # [v35.8] 통합 매도 판단 함수 호출 (Modularized)
        from backtest_engine import TradingStrategy
        res = TradingStrategy.evaluate_position(
            code, now_price, pos, self.app.settings,
            indicator_row=kwargs.get('ind'),
            current_time=datetime.now(),
            is_realtime=True,
            kwargs=kwargs
        )
        
        if res['action'] == 'EXIT':
            return {'action': 'EXIT', 'reason': res['reason'], 'profit_ratio': res.get('profit_ratio', 0)}

        # [v4.5.11] 상태 업데이트 (함수 내부에서 max_p 등은 이미 업데이트됨)
        page_avg_p = pos.get('avg_price', now_price)
        profit_ratio = (now_price / page_avg_p - 1) * 100
        pos['current_price'] = now_price
        
        # Helper variables for Scale-in logic
        s = self.app.settings
        ind = kwargs.get('ind')
        now = datetime.now()
        vol_power = kwargs.get('vol_power', 100.0)
        ask_bid_ratio = kwargs.get('ask_bid_ratio', 1.0)
        
        # ---------------------------------------------------------
        # [2단계] 추가 매수(Stage 2 Scale-in) 조건 검토
        # ---------------------------------------------------------
        stage = pos.get('stage', 1)
        if stage == 1:
            # 0. 시장지수 필터
            if s.get('use_market_filter', True):
                if getattr(self.app, 'market_trend', 'NEUTRAL') in ['NEUTRAL', 'DOWN']:
                    return {'action': 'HOLD'}

            # 1. 시간 지연 필터 (Stage 2 Delay)
            if s.get('use_s2_delay', True):
                min_h = float(s.get('s2_delay_hours', 24.0))
                if pos.get('entry_time'):
                    try:
                        elapsed = (datetime.now() - datetime.fromisoformat(pos['entry_time'])).total_seconds() / 3600
                        if elapsed < min_h: return {'action': 'HOLD'}
                    except: pass

            # 2. 기술적 위치/합치 분석
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

            # 3. 수급 컨펌 (Smart Buy S2) — 전술적 수급 VP/BA와 동일 키 사용
            if can_addon and s.get('use_smart_buy_s2', True):
                if vol_power < float(s.get('smart_buy_vp', 120.0)) or ask_bid_ratio < float(s.get('smart_buy_ba_ratio', 1.5)):
                    can_addon = False
                    
            # 4. 수익권/트리거 확인
            if can_addon:
                s2_trigger = float(s.get('s2_profit_limit', 1.0))
                if profit_ratio >= s2_trigger:
                    return {'action': 'STAGE2', 'reason': 'PROFIT_TRIGGER', 'profit_ratio': profit_ratio}

        return {'action': 'HOLD'}

    def calculate_confluence_status(self, code, now_price, ind, vol_power, ask_bid_ratio):
        """[v22.0] 실시간 감시창의 '상태' 컬럼에 표시할 종합 지수 산출"""
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
            # [v4.7] AGGRESSIVE는 UP과 동일하게 충족으로 처리
            if getattr(self.app, 'market_trend', 'NEUTRAL') in ('UP', 'AGGRESSIVE'): met += 1
        
        # 1. 시간 지연
        if s.get('use_s2_delay', True):
            total += 1
            min_h = float(s.get('s2_delay_hours', 24.0))
            if pos.get('entry_time'):
                try:
                    if (datetime.now() - datetime.fromisoformat(pos['entry_time'])).total_seconds() / 3600 >= min_h:
                        met += 1
                except: pass
        
        # 2. 수익권 트리거
        if s.get('use_s2_profit', True):
            total += 1
            profit = (now_price / pos['avg_price'] - 1) * 100
            if profit >= float(s.get('s2_profit_limit', 1.0)): met += 1
        
        # 3. 기술적 지표(MA20, MA5, RSI, MACD)
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

        # 4. 수급 지표 (추매 Smart Buy — smart_buy_vp / smart_buy_ba_ratio)
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
