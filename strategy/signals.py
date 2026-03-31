import pandas as pd
import numpy as np

def get_buy_signals_detailed(df, settings, market_trend='UP'):
    """매수 신호 상세 분석 (Type A/B 통합)"""
    # [v4.4 Rule] 1. 시장지수 필터링 (3단계 차등 적용)
    if settings.get('use_market_filter', True) and market_trend == 'DOWN':
        # DOWN (역풍): 모든 신호 차단
        return pd.DataFrame({
            'Signal': False, 'Sig_Type': 'NONE', 
            'Met_Count': 0, 'Total_Count': 1, 'Met_Ratio': 0.0,
            'RSI_Range': False, 'RSI_Cross': False,
            'MACD_OK': False, 'MA60_Range': False, 'MA_GC': False, 'Vol_Surge': False
        }, index=df.index)
    
    # NEUTRAL (중립): Type B는 나중에 차단 (signal_b에서 처리)
        
    settings = settings or {}
    ma60_ratio = (df['Close'] / df['MA60']) * 100
    
    # 2. RSI 조건
    # [v4.5 Sync] Respect use_rsi_min toggle
    buy_rsi_min = settings.get('buy_rsi_min', 30.0) if settings.get('use_rsi_min', True) else 0.0
    buy_rsi_max_b = settings.get('buy_rsi_max_b', 70.0)
    buy_rsi_max_a = settings.get('buy_rsi_max_a', 65.0) # [v4.5 Sync] Default 55.0 -> 65.0
    
    cond_rsi_range = (df['RSI'] >= buy_rsi_min) & (df['RSI'] <= buy_rsi_max_b)
    cond_rsi_type_a = (df['RSI'] >= buy_rsi_min) & (df['RSI'] <= buy_rsi_max_a)
    
    # [v26.11] RSI Signal Filter: Ensure RSI is above signal AND increasing (momentum strengthening)
    cond_rsi_sig = (df['RSI'] >= df['RSI_Sig']) & (df['RSI'] > df['RSI'].shift(1))
    use_rsi_sig_filter = settings.get('use_rsi_sig_filter', False)
    
    # 3. MACD/이평선 조건
    cond_macd_basic = (df['MACD'] >= df['MACD_Sig']) 
    
    ma60_low = settings.get('ma60_min', 100)
    ma60_high = settings.get('ma60_max', 112)
    cond_ma60 = (ma60_ratio >= ma60_low) & (ma60_ratio <= ma60_high)
    
    ma_conv_limit = settings.get('ma_convergence', 5.0) 
    ma_gap = (df['MA5'] / df['MA20'] - 1) * 100
    
    # [v16.92] Optimization: Check if maintenance/pulse columns were pre-calculated
    # This avoids redundant .groupby().cumsum() calls in high-frequency loops
    if 'MA_Maint_Count' in df.columns:
        ma_count_maint = df['MA_Maint_Count']
        is_ma_aligned = (df['MA5'] > df['MA20'])
    else:
        is_ma_aligned = (df['MA5'] > df['MA20'])
        ma_group_id = (~is_ma_aligned).cumsum()
        ma_count_maint = is_ma_aligned.groupby(ma_group_id).cumsum()
    
    gc_bars = max(1, int(settings.get('gc_window', 90) / 15))
    cond_ma_gc = (is_ma_aligned) & (ma_count_maint >= 1) & (ma_count_maint <= gc_bars) & (ma_gap <= ma_conv_limit)
    
    # 4. 거래량 조건 (Prev Day Same Time 대비)
    v_surge_a = settings.get('vol_surge', 2.0)
    v_surge_b = settings.get('vol_surge_b', 2.0)
    
    cond_vol_a = (df['Volume'] > df['Prev_Day_Vol'] * v_surge_a)
    cond_vol_b = (df['Volume'] > df['Prev_Day_Vol'] * v_surge_b)
    
    # 5. Type A: 눌림목 (RSI 40-65 + MA_GC + MA60 근접)
    # 이미 구현되어 있는 RSI/MA 필터를 활용하여 중복 제거
    use_a = settings.get('use_type_a', False)
    signal_a = use_a & cond_rsi_type_a 
    
    if settings.get('use_gc', True): signal_a &= cond_ma_gc
    if settings.get('use_vol_surge', True): signal_a &= cond_vol_a
    if settings.get('use_ma60_safe', True): signal_a &= cond_ma60
    
    # MA5 Recovery: 가격이 단기 이평선(5일선) 위로 올라타는 확정 시점에만 진입
    signal_a &= (df['Close'] >= df['MA5']) 
    
    # [Point] RSI 반등은 이미 구현된 cond_rsi_sig 필터(L27)를 활성화하여 처리
    if settings.get('use_rsi_sig_filter', True): signal_a &= cond_rsi_sig
    
    # [v26.7] Type A RSI 50 Pulse Logic (Breakthrough + Duration + Invalidation)
    if settings.get('use_rsi_signal', True):
        r_window_bars = max(1, int(settings.get('rsi_window', 45) / 15))
        is_above_50 = (df['RSI'] >= 50.0)
        
        # [v16.92] Optimization: Check for pre-calculated Pulse Count
        if 'RSI_Pulse_Count' in df.columns:
            count_maint = df['RSI_Pulse_Count']
        else:
            # 50선 하향 이탈 시마다 그룹을 나누어 연속 돌파 유지 기간 계산
            group_id = (df['RSI'] < 50.0).cumsum()
            count_maint = is_above_50.groupby(group_id).cumsum()
        
        # 조건: 현재 50 이상 유지 중이면서, 돌파(카운트 시작) 후 설정된 바(N) 이내일 것
        cond_rsi_50_pulse = (is_above_50) & (count_maint >= 1) & (count_maint <= r_window_bars)
        signal_a &= cond_rsi_50_pulse


    # 6. Type B: 추세 돌파 (RSI 제약 + 정배열 + MACD GC)
    # [v26.3] Default to False if missing for safety
    use_b = settings.get('use_type_b', False)
    # [v27.3] 중립(NEUTRAL) 상태에서는 백테스팅 엔진과 동일하게 Type B 진입을 차단함
    if market_trend == 'NEUTRAL': use_b = False
    
    cond_trend_aligned = (df['MA20'] > df['MA60']) & (df['Close'] > df['MA20'])
    cond_macd_gc = (df['MACD'] > df['MACD_Sig']) & (df['MACD'].shift(1) <= df['MACD_Sig'].shift(1))
    
    signal_b = use_b & cond_rsi_range 
    if settings.get('use_macd_gc_b', True): signal_b &= cond_macd_gc
    if settings.get('use_trend_aligned_b', True): signal_b &= cond_trend_aligned
    if use_rsi_sig_filter: signal_b &= cond_rsi_sig
    
    # [v4.5 Rule] 시장지수 'NEUTRAL' 상태일 경우 Type B 진입 차단
    if settings.get('use_market_filter', True) and market_trend == 'NEUTRAL':
        signal_b[:] = False
    
    # 7. 최종 신호 및 공통 필터
    signal = signal_a | signal_b
    
    if settings.get('use_macd_signal', True):
        # [v26.10] MACD Momentum Filter: Ensure OSC is positive AND increasing (energy strengthening)
        signal &= (df['OSC'] > 0) & (df['OSC'] > df['OSC'].shift(1))
    
    # 8. 수급 합치도 (Confluence Score) 계산 - [v3.5]
    def calc_met_count(sig_type):
        if sig_type == 'TYPE_A':
            conds = [cond_rsi_type_a]
            if settings.get('use_gc', True): conds.append(cond_ma_gc)
            if settings.get('use_vol_surge', True): conds.append(cond_vol_a)
            if settings.get('use_ma60_safe', True): conds.append(cond_ma60)
            if settings.get('use_rsi_sig_filter', True): conds.append(cond_rsi_sig)
            if settings.get('use_rsi_signal', True): conds.append(cond_rsi_50_pulse)
        elif sig_type == 'TYPE_B':
            conds = [cond_rsi_range]
            if settings.get('use_macd_gc_b', True): conds.append(cond_macd_gc)
            if settings.get('use_trend_aligned_b', True): conds.append(cond_trend_aligned)
            if settings.get('use_type_b_vol', True): conds.append(cond_vol_b)
            if use_rsi_sig_filter: conds.append(cond_rsi_sig)
        else:
            return 0, 1
        
        # 공통 필터 추가
        if settings.get('use_macd_signal', True):
            conds.append((df['OSC'] > 0) & (df['OSC'] > df['OSC'].shift(1)))
        
        met = sum([c.astype(int) for c in conds])
        return met, len(conds)

    met_a, total_a = calc_met_count('TYPE_A')
    met_b, total_b = calc_met_count('TYPE_B')
    
    # 더 높은 합치도를 가진 타입을 기준으로 점수 산출
    ratio_a = met_a / total_a
    ratio_b = met_b / total_b
    
    final_met = np.where(ratio_b >= ratio_a, met_b, met_a)
    final_total = np.where(ratio_b >= ratio_a, total_b, total_a)

    return pd.DataFrame({
        'Signal': signal,
        'Sig_Type': np.where(signal_b, 'TYPE_B', np.where(signal_a, 'TYPE_A', 'NONE')),
        'Met_Count': final_met,
        'Total_Count': final_total,
        'Met_Ratio': np.where(final_total > 0, final_met / final_total, 0),
        'RSI_Range': cond_rsi_range,
        'MACD_OK': cond_macd_basic,
        'MA60_Range': cond_ma60,
        'MA_GC': cond_ma_gc,
        'Vol_Surge': cond_vol_a | cond_vol_b
    }, index=df.index)

def get_buy_signals(df, settings, market_trend='UP'):
    """매수 신호 벡터만 반환"""
    return get_buy_signals_detailed(df, settings, market_trend)['Signal']
