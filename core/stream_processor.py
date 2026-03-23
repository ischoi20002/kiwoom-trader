# -*- coding: utf-8 -*-
from datetime import datetime
import pandas as pd
from backtest_engine import TradingStrategy

class StreamProcessor:
    """실시간 시세 데이터 처리 및 지표 계산 전용 클래스"""
    def __init__(self, settings, logger_callback=None):
        self.settings = settings
        self.logger = logger_callback or print

    def log(self, message):
        if self.logger:
            self.logger(message)

    def update_realtime_bar(self, code, price, volume, bars_data):
        """틱 정보를 DataFrame 기반 봉에 반영"""
        now = datetime.now()
        # 15분 단위 슬롯 계산
        target_minute = ((now.minute // 15) + 1) * 15
        target_hour = now.hour
        if target_minute == 60:
            target_minute, target_hour = 0, target_hour + 1
        slot_time = f"{target_hour:02d}{target_minute:02d}"

        df = bars_data.get(code)
        if df is None or df.empty:
            # 첫 진입 시 v는 0으로 시작 (기준점)
            bars_data[code] = pd.DataFrame([{'time': slot_time, 'o': price, 'h': price, 'l': price, 'c': price, 'v': 0, 'cum_v_start': volume}])
            return bars_data
        
        last_idx = df.index[-1]
        if df.at[last_idx, 'time'] == slot_time:
            # 현재 봉 업데이트
            df.at[last_idx, 'c'] = price
            if price > df.at[last_idx, 'h']: df.at[last_idx, 'h'] = price
            if price < df.at[last_idx, 'l']: df.at[last_idx, 'l'] = price
            # 봉 내 거래량 = 현재 누계 - 봉 시작 누계
            start_v = df.at[last_idx, 'cum_v_start'] if 'cum_v_start' in df.columns else volume
            df.at[last_idx, 'v'] = max(0, volume - start_v)
        else:
            # 새 봉 추가 및 100개 유지
            # 새 봉의 시작 누계는 이전 봉의 최종 누계(즉, 현재 volume)
            new_row = pd.DataFrame([{'time': slot_time, 'o': price, 'h': price, 'l': price, 'c': price, 'v': 0, 'cum_v_start': volume}])
            df = pd.concat([df, new_row], ignore_index=True).tail(100)
            
            # MA 계산 (15분봉 기준)
            df['ma5'] = df['c'].rolling(window=5).mean()
            df['ma20'] = df['c'].rolling(window=20).mean()
            df['ma60'] = df['c'].rolling(window=60).mean()
            
            bars_data[code] = df
        return bars_data
        return bars_data


    def calculate_indicators(self, df, period=14):
        """RSI 및 MACD 지표 계산"""
        if df is None or len(df) <= period: 
            return 0.0, 0.0, 0.0, 0.0 # RSI, RSI_Sig, MACD, OSC
        
        try:
            # 1. RSI
            delta = df['c'].diff()
            up = delta.clip(lower=0)
            down = -1 * delta.clip(upper=0)
            avg_up = up.ewm(com=period-1, adjust=False).mean()
            avg_down = down.ewm(com=period-1, adjust=False).mean()
            rs = avg_up / (avg_down + 1e-10)
            rsi = 100 - (100 / (1 + rs))
            
            rsi_sig_period = int(self.settings.get('rsi_sig_period', 9))
            rsi_sig = rsi.rolling(rsi_sig_period).mean().iloc[-1]
            rsi_val = float(rsi.iloc[-1])
            
            # 2. MACD (실시간 신지용 파라미터 적용)
            macd_fast = int(self.settings.get('sig_macd_fast', 12))
            macd_slow = int(self.settings.get('sig_macd_slow', 26))
            macd_sig = int(self.settings.get('sig_macd_sig', 9))
            
            ema_f = df['c'].ewm(span=macd_fast, adjust=False).mean()
            ema_s = df['c'].ewm(span=macd_slow, adjust=False).mean()
            macd_line = ema_f - ema_s
            macd_signal = macd_line.ewm(span=macd_sig, adjust=False).mean()
            osc = macd_line - macd_signal
            
            return rsi_val, float(rsi_sig), float(macd_line.iloc[-1]), float(osc.iloc[-1])
        except: 
            return 0.0, 0.0, 0.0, 0.0
