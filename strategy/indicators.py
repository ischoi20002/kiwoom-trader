import pandas as pd
import numpy as np

def calculate_atr(df, period=14):
    """ATR (Average True Range) 계산 (Wilder's Smoothing)"""
    if df.empty or len(df) < period:
        return pd.Series(np.nan, index=df.index)
        
    df = df.copy()
    high_low = df['High'] - df['Low']
    high_pc = abs(df['High'] - df['Close'].shift(1))
    low_pc = abs(df['Low'] - df['Close'].shift(1))
    
    tr = pd.concat([high_low, high_pc, low_pc], axis=1).max(axis=1)
    # Wilder's Smoothing: ewm with alpha=1/period is equivalent to Wilder's
    atr = tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    return atr

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
    rsi_sig_p = int(settings.get('rsi_sig_period', 9))
    df['RSI_Sig'] = calculate_ma(df['RSI'], max(1, rsi_sig_p))
    # 4. MACD (v6.2: 실시간/백테스트 신호용 파라미터 분리 적용)
    fast = int(settings.get('macd_fast_entry', 12))
    slow = int(settings.get('macd_slow_entry', 26))
    sig = int(settings.get('macd_sig_entry', 9))

    # [v37.9] Optimization: Preserve raw calculation states for Incremental Updates in 1m simulation
    # 1. MACD EMA states
    df['_ema12'] = df['Close'].ewm(span=max(1, fast), adjust=False).mean()
    df['_ema26'] = df['Close'].ewm(span=max(1, slow), adjust=False).mean()
    df['MACD'] = df['_ema12'] - df['_ema26']
    
    sig_p = max(1, sig)
    df['_macd_sig'] = df['MACD'].ewm(span=sig_p, adjust=False).mean()
    df['MACD_Sig'] = df['_macd_sig']
    df['OSC'] = df['MACD'] - df['MACD_Sig']
    df['Prev_OSC'] = df['OSC'].shift(1)
    
    # 2. RSI Wilder's states
    period = 14
    delta = df['Close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    df['_avg_gain'] = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    df['_avg_loss'] = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    
    rs = df['_avg_gain'] / (df['_avg_loss'] + 1e-10)
    df['RSI'] = 100 - (100 / (1 + rs))
    
    rsi_sig_p = int(settings.get('rsi_sig_period', 9))
    df['RSI_Sig'] = calculate_ma(df['RSI'], max(1, rsi_sig_p))
    df['Prev_RSI'] = df['RSI'].shift(1)

    # ... rest of the original logic (MSI, ADX, ATR, etc.)
    # 5. MSI (Market Strength Index)
    if 'MA120' in df.columns:
        df['MSI'] = (df['MA20'] / (df['MA60'] + 1e-10) - 1) + (df['MA60'] / (df['MA120'] + 1e-10) - 1)
    
    # 6. ADX & ATR
    if 'High' in df.columns and 'Low' in df.columns:
        _, _, df['ADX'] = calculate_adx(df, 14)
        atr_p = int(settings.get('atr_period', 14))
        df['ATR'] = calculate_atr(df, max(1, atr_p))
        
        # [Perf] Vectorized Prev Day Same Time Volume calculation
        v_lookback = int(settings.get('vol_lookback', 10))
        vol_lookback = max(1, v_lookback)
        
        df_tmp = df[['Volume']].copy()
        if isinstance(df_tmp.index, pd.DatetimeIndex):
            df_tmp['date_only'] = df_tmp.index.normalize()
            df_tmp['time_only'] = df_tmp.index.time
            pivoted = df_tmp.pivot_table(index='date_only', columns='time_only', values='Volume')
            pivoted_rolled = pivoted.shift(1).rolling(window=vol_lookback, min_periods=1).mean()
            stacked = pivoted_rolled.stack(future_stack=True)
            lookup_keys = pd.MultiIndex.from_arrays([df_tmp.index.normalize(), df_tmp.index.time])
            df['Prev_Day_Vol'] = stacked.reindex(lookup_keys).values
            df['Prev_Day_Vol'] = df['Prev_Day_Vol'].ffill().fillna(0)
        else:
            df['Prev_Day_Vol'] = 0
        
    return df

def calculate_buy_indicators_light(df, settings):
    """매수 신호용 경량 지표 계산 (실시간성 강조)"""
    df = df.copy()
    df['MA5'] = calculate_ma(df['Close'], 5)
    df['MA20'] = calculate_ma(df['Close'], 20)
    df['MA60'] = calculate_ma(df['Close'], 60)
    df['Vol_MA20'] = calculate_ma(df['Volume'], 20)
    
    df['RSI'] = calculate_rsi(df['Close'], 14)
    rsi_sig_p = int(settings.get('rsi_sig_period', 9))
    df['RSI_Sig'] = calculate_ma(df['RSI'], max(1, rsi_sig_p))
    df['Prev_RSI'] = df['RSI'].shift(1)
    
    fast = int(settings.get('macd_fast_entry', 12))
    slow = int(settings.get('macd_slow_entry', 26))
    sig = int(settings.get('macd_sig_entry', 9))
    df['MACD'], df['MACD_Sig'], df['OSC'] = calculate_macd(df['Close'], max(1, fast), max(1, slow), max(1, sig))
    df['Prev_OSC'] = df['OSC'].shift(1)
    
    # Prev Day same time vol (전일 동시간 거래량 평균) - Optimized for v35.8
    v_lookback = int(settings.get('vol_lookback', 10))
    vol_lookback = max(1, v_lookback)
    
    if isinstance(df.index, pd.DatetimeIndex) and len(df) > 0:
        # Vectorized pivot-based approach (v35.8: Optimized with reindex)
        df_tmp = df[['Volume']].copy()
        df_tmp['date_only'] = df_tmp.index.normalize()
        df_tmp['time_only'] = df_tmp.index.time
        
        pivoted = df_tmp.pivot_table(index='date_only', columns='time_only', values='Volume')
        pivoted_rolled = pivoted.shift(1).rolling(window=vol_lookback, min_periods=1).mean()
        
        # Map back efficiently (Avoid merge/KeyError issues)
        stacked = pivoted_rolled.stack(future_stack=True)
        if isinstance(df.index, pd.DatetimeIndex):
            lookup_keys = pd.MultiIndex.from_arrays([df.index.normalize(), df.index.time])
        else:
            now_norm = pd.Timestamp.now().normalize()
            lookup_keys = pd.MultiIndex.from_arrays([[now_norm]*len(df), [None]*len(df)])
            
        df['Prev_Day_Vol'] = stacked.reindex(lookup_keys).values
        df['Prev_Day_Vol'] = df['Prev_Day_Vol'].ffill().fillna(0)
    else:
        df['Prev_Day_Vol'] = 0
    
    # ATR (v31.0: Added for exit monitoring)
    # If High/Low exist, calculate ATR (required for SL/Trail)
    if 'High' in df.columns and 'Low' in df.columns:
        atr_p = int(settings.get('atr_period', 14))
        df['ATR'] = calculate_atr(df, max(1, atr_p))
    
    return df
