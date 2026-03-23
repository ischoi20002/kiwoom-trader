import pandas as pd
import numpy as np

def get_buy_signals_detailed(df, settings, market_trend='UP'):
    """매수 신호 상세 분석 (Type A/B 통합)"""
    # 1. 하락장 필터링
    if settings.get('use_market_filter', True) and market_trend == 'DOWN':
        return pd.DataFrame({
            'Signal': False, 'Sig_Type': 'NONE', 'RSI_Range': False, 'RSI_Cross': False,
            'MACD_OK': False, 'MA60_Range': False, 'MA_GC': False, 'Vol_Surge': False
        }, index=df.index)
        
    settings = settings or {}
    ma60_ratio = (df['Close'] / df['MA60']) * 100
    
    # 2. RSI 조건
    buy_rsi_min = settings.get('buy_rsi_min', 30.0)
    buy_rsi_max_b = settings.get('buy_rsi_max_b', 70.0)
    buy_rsi_max_a = settings.get('buy_rsi_max_a', 58.0) 
    
    cond_rsi_range = (df['RSI'] >= buy_rsi_min) & (df['RSI'] <= buy_rsi_max_b)
    cond_rsi_type_a = (df['RSI'] >= buy_rsi_min) & (df['RSI'] <= buy_rsi_max_a)
    
    # [v6.4] RSI 시그널(R_SP) 돌파 조건 (RSI > RSI_Sig)
    cond_rsi_sig = (df['RSI'] >= df['RSI_Sig']) 
    use_rsi_sig_filter = settings.get('use_rsi_sig_filter', False)
    
    # 3. MACD/이평선 조건
    cond_macd_basic = (df['MACD'] >= df['MACD_Sig']) 
    
    ma60_low = settings.get('ma60_min', 100)
    ma60_high = settings.get('ma60_max', 112)
    cond_ma60 = (ma60_ratio >= ma60_low) & (ma60_ratio <= ma60_high)
    
    ma_gap_limit = settings.get('ma_gap_limit', 2.5)
    ma_gap = (df['MA5'] / df['MA20'] - 1) * 100
    
    gc_bars = max(1, int(settings.get('gc_window', 15) / 15))
    cond_ma_gc = (df['MA5'] > df['MA20']) & (df['MA5'].shift(gc_bars) <= df['MA20'].shift(gc_bars)) & (ma_gap <= ma_gap_limit)
    
    # 4. 거래량 조건 (Prev Day Same Time 대비)
    v_surge_a = settings.get('vol_surge', 1.2)
    v_surge_b = settings.get('vol_surge_b', 1.5)
    
    cond_vol_a = (df['Volume'] > df['Prev_Day_Vol'] * v_surge_a)
    cond_vol_b = (df['Volume'] > df['Prev_Day_Vol'] * v_surge_b)
    
    # 5. Type A: 눌림목 (RSI 40-58 + MA_GC + MA60 근접)
    use_a = settings.get('use_type_a', True)
    signal_a = use_a & cond_rsi_type_a
    if settings.get('use_gc', True): signal_a &= cond_ma_gc
    if settings.get('use_vol_surge', True): signal_a &= cond_vol_a
    if settings.get('use_ma60_safe', True): signal_a &= cond_ma60
    if use_rsi_sig_filter: signal_a &= cond_rsi_sig
    
    # Type A RSI 50 유지 조건
    if settings.get('use_rsi_signal', True):
        r_window_bars = max(1, int(settings.get('rsi_window', 45) / 15))
        cond_rsi_50_maint = (df['RSI'].rolling(window=r_window_bars).min() >= 50.0)
        signal_a &= cond_rsi_50_maint

    # 6. Type B: 추세 돌파 (RSI 제약 + 정배열 + MACD GC)
    use_b = settings.get('use_type_b', True)
    cond_trend_aligned = (df['MA20'] > df['MA60']) & (df['Close'] > df['MA20'])
    cond_macd_gc = (df['MACD'] > df['MACD_Sig']) & (df['MACD'].shift(1) <= df['MACD_Sig'].shift(1))
    
    signal_b = use_b & cond_rsi_range 
    if settings.get('use_macd_gc_b', True): signal_b &= cond_macd_gc
    if settings.get('use_trend_aligned_b', True): signal_b &= cond_trend_aligned
    if settings.get('use_type_b_vol', True): signal_b &= cond_vol_b
    if use_rsi_sig_filter: signal_b &= cond_rsi_sig
    
    # 7. 최종 신호 및 공통 필터
    signal = signal_a | signal_b
    
    if settings.get('use_macd_signal', True):
        signal &= (df['OSC'] > 0)
    
    return pd.DataFrame({
        'Signal': signal,
        'Sig_Type': np.where(signal_b, 'TYPE_B', np.where(signal_a, 'TYPE_A', 'NONE')),
        'RSI_Range': cond_rsi_range,
        'MACD_OK': cond_macd_basic,
        'MA60_Range': cond_ma60,
        'MA_GC': cond_ma_gc,
        'Vol_Surge': cond_vol_a | cond_vol_b
    }, index=df.index)

def get_buy_signals(df, settings, market_trend='UP'):
    """매수 신호 벡터만 반환"""
    return get_buy_signals_detailed(df, settings, market_trend)['Signal']
