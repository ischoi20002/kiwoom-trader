import pandas as pd
import numpy as np


def _normalize_same_bar_priority(settings: dict | None) -> str:
    """동일 봉 A+B 동시 신호 시 Sig_Type 우선순위 (settings 키: same_bar_sig_priority).

    브레드스 게이트 ON 시 선정/백테스트가 넣는 ``breadth_runtime_sig_priority`` 가 있으면 우선.
    """
    try:
        if settings is None:
            return "confluence"
        # 레짐 런타임 우선순위 (UP→prefer_b, NEUTRAL→confluence 등)
        if bool(settings.get("use_market_breadth_gate", False)):
            rt = settings.get("breadth_runtime_sig_priority")
            if rt is not None and str(rt).strip() != "":
                p_rt = str(rt).strip().lower()
                if p_rt in ("prefer_b", "prefer_a", "confluence"):
                    return p_rt
        raw = settings.get("same_bar_sig_priority", "confluence")
        p = str(raw).strip().lower()
        if p in ("prefer_b", "prefer_a", "confluence"):
            return p
        return "confluence"
    except Exception:
        return "confluence"


def _compute_sig_type_column(
    signal_a: np.ndarray,
    signal_b: np.ndarray,
    ratio_a: np.ndarray,
    ratio_b: np.ndarray,
    priority: str,
) -> np.ndarray:
    """
    Sig_Type 열 산출. 동일 봉에서 signal_a·signal_b가 모두 True일 때만 priority 규칙 적용.
    - confluence: Met_Ratio(합치도)가 더 큰 타입(동점이면 TYPE_A — NEUTRAL·Track A 정합성)
    - prefer_a / prefer_b: 동시 만족 봉에서 각각 고정
    """
    try:
        sa = np.asarray(signal_a, dtype=bool)
        sb = np.asarray(signal_b, dtype=bool)
        ra = np.asarray(ratio_a, dtype=float)
        rb = np.asarray(ratio_b, dtype=float)
        only_b = sb & ~sa
        only_a = sa & ~sb
        both = sa & sb
        out = np.where(only_b, "TYPE_B", np.where(only_a, "TYPE_A", "NONE"))
        pri = (priority or "confluence").strip().lower()
        if pri not in ("prefer_b", "prefer_a", "confluence"):
            pri = "confluence"
        if pri == "prefer_b":
            return np.where(both, "TYPE_B", out)
        if pri == "prefer_a":
            return np.where(both, "TYPE_A", out)
        # confluence
        valid = np.isfinite(ra) & np.isfinite(rb)
        both_valid = both & valid
        both_invalid = both & ~valid
        out = np.where(both_invalid, "TYPE_B", out)
        # both_valid: rb>ra → B, 그 외(ra>rb 또는 동점) → A
        sub = np.where(both_valid & (rb > ra), "TYPE_B", np.where(both_valid, "TYPE_A", "NONE"))
        return np.where(both_valid, sub, out)
    except Exception:
        return np.where(np.asarray(signal_b, dtype=bool), "TYPE_B", np.where(np.asarray(signal_a, dtype=bool), "TYPE_A", "NONE"))


def _settings_bool(settings: dict | None, key: str, default: bool = False) -> bool:
    """JSON/최적화 경로에서 bool·0/1·문자열이 섞여도 안전하게 불리언으로 해석."""
    try:
        if settings is None:
            return default
        v = settings.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return int(v) != 0
        if isinstance(v, str):
            t = v.strip().lower()
            if t in ("true", "1", "yes", "on"):
                return True
            if t in ("false", "0", "no", "off", ""):
                return False
        return bool(v) if v is not None else default
    except Exception:
        return default


def get_buy_signals_detailed(df, settings, market_trend='UP'):
    """매수 신호 상세 분석 (Type A/B 통합). market_trend 인자는 호환용(시장·레짐 비사용 시 무시)."""
    _ = market_trend  # 시장 지표/레짐 제거: 미사용
    settings = settings or {}
    ma60_ratio = (df['Close'] / df['MA60']) * 100
    
    # 2. RSI 조건 — 하한은 S1 공통(use_rsi_min·buy_rsi_min), 상한만 A/B 분리
    buy_rsi_min = settings.get('buy_rsi_min', 30.0) if settings.get('use_rsi_min', True) else 0.0
    buy_rsi_max_b = settings.get('buy_rsi_max_b', 70.0)
    buy_rsi_max_a = settings.get('buy_rsi_max_a', 65.0) # [v4.5 Sync] Default 55.0 -> 65.0
    
    cond_rsi_range = (df['RSI'] >= buy_rsi_min) & (df['RSI'] <= buy_rsi_max_b)
    cond_rsi_type_a = (df['RSI'] >= buy_rsi_min) & (df['RSI'] <= buy_rsi_max_a)
    
    # [v26.11] RSI Signal Filter: Ensure RSI is above signal AND increasing (momentum strengthening)
    cond_rsi_sig = (df['RSI'] >= df['RSI_Sig']) & (df['RSI'] > df['RSI'].shift(1))
    # defaults.py·실제 적용과 동일하게 True 기본 (이전 False 기본은 Type B 분기만 흔들던 버그)
    use_rsi_sig_filter = _settings_bool(settings, "use_rsi_sig_filter", True)
    
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
    
    # S1 전용 기준열 (lookback은 prev_vol_lookback_s1_a / s1_b)
    col_s1a = "Prev_Day_Vol_S1_A" if "Prev_Day_Vol_S1_A" in df.columns else "Prev_Day_Vol"
    col_s1b = "Prev_Day_Vol_S1_B" if "Prev_Day_Vol_S1_B" in df.columns else "Prev_Day_Vol_B"
    cond_vol_a = (df["Volume"] > df[col_s1a] * v_surge_a)
    cond_vol_b = (df["Volume"] > df[col_s1b] * v_surge_b)
    
    # 5. Type A: 눌림목 (RSI 40-65 + MA_GC + MA60 근접)
    # 이미 구현되어 있는 RSI/MA 필터를 활용하여 중복 제거
    use_a = settings.get('use_type_a', False)
    signal_a = use_a & cond_rsi_type_a 
    
    if settings.get('use_gc', True): signal_a &= cond_ma_gc
    if settings.get('use_vol_surge', True): signal_a &= cond_vol_a
    if settings.get('use_ma60_safe', True): signal_a &= cond_ma60
    
    # MA5 Recovery: 가격이 단기 이평선(5일선) 위로 올라타는 확정 시점에만 진입
    signal_a &= (df['Close'] >= df['MA5']) 
    
    # [Point] RSI 반등은 cond_rsi_sig로 처리 — Type A만 쓸 때도 기본 ON이면 전봉 대비 RSI 상승이 먼저 걸러짐
    if use_rsi_sig_filter:
        signal_a &= cond_rsi_sig
    
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

    cond_trend_aligned = (df['MA20'] > df['MA60']) & (df['Close'] > df['MA20'])
    cond_macd_gc = (df['MACD'] > df['MACD_Sig']) & (df['MACD'].shift(1) <= df['MACD_Sig'].shift(1))
    
    signal_b = use_b & cond_rsi_range 
    if settings.get('use_macd_gc_b', True): signal_b &= cond_macd_gc
    if settings.get('use_trend_aligned_b', True): signal_b &= cond_trend_aligned
    if use_rsi_sig_filter: signal_b &= cond_rsi_sig
    # Type A의 use_vol_surge와 대칭: 돌파 거래량(vol_surge_b)이 실제 진입 신호에 반영되어야 함
    if _settings_bool(settings, "use_type_b_vol", True):
        signal_b &= cond_vol_b

    # 7. 최종 신호 및 공통 필터
    signal = signal_a | signal_b
    
    if _settings_bool(settings, "use_macd_signal", True):
        # 15분 MACD 필터 ON: 히스토그램 양수(OSC>0)만 적용 (전봉 대비 상승 조건은 Type A/B와 중복되어 제거됨)
        signal &= df["OSC"] > 0

    # 동시 신호: A·B 원시 조건이 모두 참이고 최종 Signal 도 참 — 동적 백테에서 보유 균형 동점 시 TYPE_B 우선에 사용
    s1_simul_ab = (
        np.asarray(signal_a, dtype=bool)
        & np.asarray(signal_b, dtype=bool)
        & np.asarray(signal, dtype=bool)
    )
    
    # 8. 수급 합치도 (Confluence Score) 계산 - [v3.5]
    def calc_met_count(sig_type):
        if sig_type == 'TYPE_A':
            conds = [cond_rsi_type_a]
            if settings.get('use_gc', True): conds.append(cond_ma_gc)
            if settings.get('use_vol_surge', True): conds.append(cond_vol_a)
            if settings.get('use_ma60_safe', True): conds.append(cond_ma60)
            if use_rsi_sig_filter:
                conds.append(cond_rsi_sig)
            if settings.get('use_rsi_signal', True): conds.append(cond_rsi_50_pulse)
        elif sig_type == 'TYPE_B':
            conds = [cond_rsi_range]
            if settings.get('use_macd_gc_b', True): conds.append(cond_macd_gc)
            if settings.get('use_trend_aligned_b', True): conds.append(cond_trend_aligned)
            if settings.get('use_type_b_vol', True): conds.append(cond_vol_b)
            if use_rsi_sig_filter: conds.append(cond_rsi_sig)
        else:
            return 0, 1
        
        if _settings_bool(settings, "use_macd_signal", True):
            conds.append(df["OSC"] > 0)
        
        met = sum([c.astype(int) for c in conds])
        return met, len(conds)

    met_a, total_a = calc_met_count('TYPE_A')
    met_b, total_b = calc_met_count('TYPE_B')
    
    # 더 높은 합치도를 가진 타입을 기준으로 점수 산출
    ratio_a = met_a / np.maximum(total_a, 1)
    ratio_b = met_b / np.maximum(total_b, 1)

    same_bar_pri = _normalize_same_bar_priority(settings)
    sig_type_col = _compute_sig_type_column(signal_a, signal_b, ratio_a, ratio_b, same_bar_pri)
    is_type_b_row = sig_type_col == "TYPE_B"
    # Series·ndarray 혼용 시 위치 기준 합치도가 어긋나지 않도록 ndarray로 고정
    m_a = np.asarray(met_a, dtype=np.int32)
    m_b = np.asarray(met_b, dtype=np.int32)
    ta_i = max(1, int(total_a))
    tb_i = max(1, int(total_b))
    final_met = np.where(is_type_b_row, m_b, m_a)
    final_total = np.where(is_type_b_row, tb_i, ta_i)

    n_rows = len(df.index)
    tot_a_col = np.full(n_rows, ta_i, dtype=np.int32)
    tot_b_col = np.full(n_rows, tb_i, dtype=np.int32)

    return pd.DataFrame({
        'Signal': signal,
        'Sig_Type': sig_type_col,
        'Met_Count': final_met,
        'Total_Count': final_total,
        'Met_Ratio': np.where(final_total > 0, final_met / final_total, 0),
        # UI·로그: 종목별 눌림목(A)·추세돌파(B) 충족 수 (분모는 설정에 따른 조건 개수)
        'Met_A': m_a,
        'Tot_A': tot_a_col,
        'Met_B': m_b,
        'Tot_B': tot_b_col,
        'RSI_Range': cond_rsi_range,
        'MACD_OK': cond_macd_basic,
        'MA60_Range': cond_ma60,
        'MA_GC': cond_ma_gc,
        "Vol_Surge": cond_vol_a | cond_vol_b,
        "S1_Simultaneous_AB": s1_simul_ab,
    }, index=df.index)

def get_buy_signals(df, settings, market_trend='UP'):
    """매수 신호 벡터만 반환"""
    return get_buy_signals_detailed(df, settings, market_trend)['Signal']
