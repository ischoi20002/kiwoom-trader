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
        # [v3.5] 15분 단위 슬롯으로 원복 (지표 안정성 확보)
        target_minute = ((now.minute // 15) + 1) * 15
        target_hour = now.hour
        if target_minute == 60:
            target_minute, target_hour = 0, target_hour + 1
        slot_time = f"{target_hour:02d}{target_minute:02d}"

        # [v35.7] 슬롯 시간을 현재 날짜와 결합하여 전체 타임스탬프 생성
        now_dt = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
        dt_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

        df = bars_data.get(code)
        if df is None or df.empty:
            # 첫 진입 시 Volume은 0으로 시작 (기준점)
            new_row = {'datetime': dt_str, 'time': slot_time, 'Open': price, 'High': price, 'Low': price, 'Close': price, 'Volume': 0, 'cum_v_start': volume}
            new_df = pd.DataFrame([new_row])
            new_df['datetime'] = pd.to_datetime(new_df['datetime'])
            new_df.set_index('datetime', inplace=True)
            bars_data[code] = new_df
            return bars_data
        
        last_idx = df.index[-1]
        if df.at[last_idx, 'time'] == slot_time:
            # 현재 봉 업데이트
            df.at[last_idx, 'Close'] = price
            if price > df.at[last_idx, 'High']: df.at[last_idx, 'High'] = price
            if price < df.at[last_idx, 'Low']: df.at[last_idx, 'Low'] = price
            # 봉 내 거래량 = 현재 누계 - 봉 시작 누계
            start_v = df.at[last_idx, 'cum_v_start'] if 'cum_v_start' in df.columns else volume
            df.at[last_idx, 'Volume'] = max(0, volume - start_v)
        else:
            # 새 봉 추가 및 300개 유지 (충분한 지표 계산용)
            new_row_data = {'datetime': dt_str, 'time': slot_time, 'Open': price, 'High': price, 'Low': price, 'Close': price, 'Volume': 0, 'cum_v_start': volume}
            new_row_df = pd.DataFrame([new_row_data])
            new_row_df['datetime'] = pd.to_datetime(new_row_df['datetime'])
            new_row_df.set_index('datetime', inplace=True)
            
            df = pd.concat([df, new_row_df]).tail(300)
            
            # MA 계산 (15분봉 기준)
            df['ma5'] = df['Close'].rolling(window=5).mean()
            df['ma20'] = df['Close'].rolling(window=20).mean()
            df['ma60'] = df['Close'].rolling(window=60).mean()
            
            bars_data[code] = df
        return bars_data

    def calculate_indicators(self, df, period=14):
        """RSI 및 MACD 지표 계산"""
        if df is None or len(df) <= period: 
            return 0.0, 0.0, 0.0, 0.0 # RSI, RSI_Sig, MACD, OSC
        
        try:
            # 1. RSI
            delta = df['Close'].diff()
            up = delta.clip(lower=0)
            down = -1 * delta.clip(upper=0)
            avg_up = up.ewm(com=period-1, adjust=False).mean()
            avg_down = down.ewm(com=period-1, adjust=False).mean()
            rs = avg_up / (avg_down + 1e-10)
            rsi = 100 - (100 / (1 + rs))
            
            rsi_sig_period = int(self.settings.get('rsi_sig_period', 9))
            rsi_sig = rsi.rolling(rsi_sig_period).mean().iloc[-1]
            rsi_val = float(rsi.iloc[-1])
            
            # 2. MACD
            macd_fast = int(self.settings.get('sig_macd_fast', 12))
            macd_slow = int(self.settings.get('sig_macd_slow', 26))
            macd_sig = int(self.settings.get('sig_macd_sig', 9))
            
            ema_f = df['Close'].ewm(span=macd_fast, adjust=False).mean()
            ema_s = df['Close'].ewm(span=macd_slow, adjust=False).mean()
            macd_line = ema_f - ema_s
            macd_signal = macd_line.ewm(span=macd_sig, adjust=False).mean()
            osc = macd_line - macd_signal
            
            return rsi_val, float(rsi_sig), float(macd_line.iloc[-1]), float(osc.iloc[-1])
        except: 
            return 0.0, 0.0, 0.0, 0.0
