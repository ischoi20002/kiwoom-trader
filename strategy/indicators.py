import pandas as pd
import numpy as np

def calculate_ma(series, window):
    """이동평균선 계산"""
    return series.rolling(window=window).mean()

def calculate_rsi(series, period=14):
    """RSI (Wilder's Smoothing) 계산"""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    
    # Wilder's Smoothing (EMA with alpha = 1/period)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))

def calculate_macd(series, fast=12, slow=26, signal=9):
    """MACD, Signal, Oscillator 계산"""
    ema_f = series.ewm(span=fast, adjust=False).mean()
    ema_s = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_f - ema_s
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    osc = macd_line - signal_line
    return macd_line, signal_line, osc

def calculate_adx(df, period=14):
    """ADX (Average Directional Index) 계산"""
    df = df.copy()
    df['High_Low'] = df['High'] - df['Low']
    df['High_PC'] = abs(df['High'] - df['Close'].shift(1))
    df['Low_PC'] = abs(df['Low'] - df['Close'].shift(1))
    df['TR'] = df[['High_Low', 'High_PC', 'Low_PC']].max(axis=1)
    
    df['Up_Move'] = df['High'] - df['High'].shift(1)
    df['Down_Move'] = df['Low'].shift(1) - df['Low']
    
    df['Plus_DM'] = np.where((df['Up_Move'] > df['Down_Move']) & (df['Up_Move'] > 0), df['Up_Move'], 0)
    df['Minus_DM'] = np.where((df['Down_Move'] > df['Up_Move']) & (df['Down_Move'] > 0), df['Down_Move'], 0)
    
    tr_smooth = df['TR'].ewm(alpha=1/period, adjust=False).mean()
    pdm_smooth = df['Plus_DM'].ewm(alpha=1/period, adjust=False).mean()
    mdm_smooth = df['Minus_DM'].ewm(alpha=1/period, adjust=False).mean()
    
    plus_di = 100 * (pdm_smooth / (tr_smooth + 1e-10))
    minus_di = 100 * (mdm_smooth / (tr_smooth + 1e-10))
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10)
    adx = dx.ewm(alpha=1/period, adjust=False).mean()
    
    return plus_di, minus_di, adx

def calculate_all_indicators(df, settings):
    """백테스팅 및 실시간 감시 통합 지표 계산"""
    df = df.copy()
    
    # 1. 이동평균선
    df['MA5'] = calculate_ma(df['Close'], 5)
    df['MA20'] = calculate_ma(df['Close'], 20)
    df['MA60'] = calculate_ma(df['Close'], 60)
    if 'MA120' in df.columns or len(df) >= 120:
        df['MA120'] = calculate_ma(df['Close'], 120)
    
    # 2. 거래량 지표
    df['Vol_MA20'] = calculate_ma(df['Volume'], 20)
    df['Vol_Ratio'] = df['Volume'] / (df['Vol_MA20'] + 1e-10)
    
    # 3. RSI
    df['RSI'] = calculate_rsi(df['Close'], 14)
    df['RSI_Sig'] = calculate_ma(df['RSI'], settings.get('rsi_sig_period', 9))
    
    # 4. MACD
    fast = settings.get('macd_fast', 12)
    slow = settings.get('macd_slow', 26)
    sig = settings.get('macd_sig', 9)
    df['MACD'], df['MACD_Sig'], df['OSC'] = calculate_macd(df['Close'], fast, slow, sig)
    df['Prev_OSC'] = df['OSC'].shift(1)
    
    # 5. MSI (Market Strength Index)
    if 'MA120' in df.columns:
        df['MSI'] = (df['MA20'] / (df['MA60'] + 1e-10) - 1) + (df['MA60'] / (df['MA120'] + 1e-10) - 1)
    
    # 6. ADX
    if 'High' in df.columns and 'Low' in df.columns:
        _, _, df['ADX'] = calculate_adx(df, 14)
        
    # 7. Prev Day Same Time Volume (전일 동시간 거래량)
    if len(df) > 0:
        df['Prev_Day_Vol'] = df.groupby(df.index.time)['Volume'].shift(1)
        
    return df

def calculate_buy_indicators_light(df, settings):
    """매수 신호용 경량 지표 계산 (실시간성 강조)"""
    df = df.copy()
    df['MA5'] = calculate_ma(df['Close'], 5)
    df['MA20'] = calculate_ma(df['Close'], 20)
    df['MA60'] = calculate_ma(df['Close'], 60)
    df['Vol_MA20'] = calculate_ma(df['Volume'], 20)
    
    df['RSI'] = calculate_rsi(df['Close'], 14)
    df['RSI_Sig'] = calculate_ma(df['RSI'], settings.get('rsi_sig_period', 9))
    
    fast = settings.get('macd_fast', 12)
    slow = settings.get('macd_slow', 26)
    sig = settings.get('macd_sig', 9)
    df['MACD'], df['MACD_Sig'], df['OSC'] = calculate_macd(df['Close'], fast, slow, sig)
    df['Prev_OSC'] = df['OSC'].shift(1)
    
    # Prev Day same time vol
    df['Prev_Day_Vol'] = df.groupby(df.index.time)['Volume'].shift(1)
    
    return df
