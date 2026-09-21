import pandas as pd
import numpy as np


def settings_allow_1530_signal_eval(settings: dict | None) -> bool:
    """
    종가 단일가(15:30) 슬롯에서 S1·S2·청산 평가를 허용할지.

    BT ``next_open`` / RT 모의 익일예약 / 실전 NXT 모두 이 플래그로 신호를 살리고,
    체결만 경로가 갈라진다.
    """
    try:
        st = settings or {}
        return bool(
            st.get("_bt_allow_1530_signal_eval", False)
            or st.get("allow_1530_signal_eval", False)
            or st.get("next_open_1530_enabled", False)
        )
    except Exception:
        return False


def _normalize_same_bar_priority(settings: dict | None) -> str:
    """동일 봉 A+B 동시 신호 시 Sig_Type 우선순위 (settings 키: same_bar_sig_priority)."""
    try:
        if settings is None:
            return "prefer_a"
        raw = settings.get("same_bar_sig_priority", "prefer_a")
        p = str(raw).strip().lower()
        # 구 confluence → prefer_a (합치도 초안 폐기)
        if p == "confluence":
            return "prefer_a"
        if p in ("prefer_b", "prefer_a", "prefer_track"):
            return p
        return "prefer_a"
    except Exception:
        return "prefer_a"


def _compute_sig_type_column(
    signal_a: np.ndarray,
    signal_b: np.ndarray,
    ratio_a: np.ndarray,
    ratio_b: np.ndarray,
    priority: str,
) -> np.ndarray:
    """
    Sig_Type 열 산출. 동일 봉에서 signal_a·signal_b가 모두 True일 때만 priority 규칙 적용.
    - prefer_a / prefer_b: 동시 만족 봉에서 각각 고정
    - prefer_track: 종목 트랙은 매수 확정 시점에 반영(여기선 열 산출용으로 prefer_a 와 동일)
    """
    try:
        sa = np.asarray(signal_a, dtype=bool)
        sb = np.asarray(signal_b, dtype=bool)
        only_b = sb & ~sa
        only_a = sa & ~sb
        both = sa & sb
        out = np.where(only_b, "TYPE_B", np.where(only_a, "TYPE_A", "NONE"))
        pri = (priority or "prefer_a").strip().lower()
        if pri == "confluence":
            pri = "prefer_a"
        if pri not in ("prefer_b", "prefer_a", "prefer_track"):
            pri = "prefer_a"
        if pri == "prefer_b":
            return np.where(both, "TYPE_B", out)
        # prefer_a · prefer_track(열 단계): 동시 봉은 TYPE_A
        return np.where(both, "TYPE_A", out)
    except Exception:
        return np.where(
            np.asarray(signal_b, dtype=bool),
            "TYPE_B",
            np.where(np.asarray(signal_a, dtype=bool), "TYPE_A", "NONE"),
        )


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


def get_buy_signals_detailed(
    df,
    settings,
    market_trend='UP',
    signal_context: dict | None = None,
):
    """
    매수 신호 상세 분석(Type A/B/C 통합).

    Track C는 종목선정 시 전일까지 확정된 ``box_lower``/``box_upper``를
    ``signal_context``로 전달받을 때만 계산하여 미래참조를 방지합니다.
    """
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

    # 6a. Type C: 박스 하단 접촉 → 박스 안 재진입 → 이전 봉 고가 돌파 + RSI 상승
    signal_c = pd.Series(False, index=df.index, dtype=bool)
    cond_c_touch = pd.Series(False, index=df.index, dtype=bool)
    cond_c_reclaim = pd.Series(False, index=df.index, dtype=bool)
    cond_c_prev_high_break = pd.Series(False, index=df.index, dtype=bool)
    cond_c_rsi_rising = pd.Series(False, index=df.index, dtype=bool)
    cond_c_candle = pd.Series(False, index=df.index, dtype=bool)
    cond_c_volume = pd.Series(False, index=df.index, dtype=bool)
    try:
        context = signal_context if isinstance(signal_context, dict) else {}
        sel_track = str(context.get("sel_track", "") or "").strip().upper()
        box_lower = float(context.get("box_lower", context.get("BoxLower")))
        box_upper = float(context.get("box_upper", context.get("BoxUpper")))
        use_c = (
            _settings_bool(settings, "use_type_c", False)
            and "C" in sel_track
            and np.isfinite(box_lower)
            and np.isfinite(box_upper)
            and box_upper > box_lower > 0
        )
        if use_c:
            atr_col = "ATR_C" if "ATR_C" in df.columns else "ATR"
            atr_c = pd.to_numeric(df[atr_col], errors="coerce")
            touch_atr = max(
                0.0, float(settings.get("type_c_touch_atr_tolerance", 0.3))
            )
            reclaim_atr = max(
                0.0, float(settings.get("type_c_reclaim_atr_buffer", 0.1))
            )
            breakdown_atr = max(
                0.0, float(settings.get("type_c_breakdown_atr_tolerance", 0.5))
            )
            touch_lookback = max(
                1, int(float(settings.get("type_c_touch_lookback_bars", 3)))
            )

            touched_now = df["Low"] <= (box_lower + atr_c * touch_atr)
            cond_c_touch = (
                touched_now.astype(int)
                .rolling(touch_lookback, min_periods=1)
                .max()
                .astype(bool)
            )
            cond_c_reclaim = df["Close"] >= (box_lower + atr_c * reclaim_atr)
            cond_c_prev_high_break = df["Close"] > df["High"].shift(1)
            cond_c_rsi_rising = (
                (df["RSI"] > df["RSI"].shift(1))
                & (df["RSI"] >= float(settings.get("type_c_rsi_min", 35.0)))
                & (df["RSI"] <= float(settings.get("type_c_rsi_max", 55.0)))
            )

            candle_range = (df["High"] - df["Low"]).replace(0.0, np.nan)
            close_location = (df["Close"] - df["Low"]) / candle_range
            bullish = df["Close"] > df["Open"]
            strong_close = close_location >= float(
                settings.get("type_c_min_close_location", 0.65)
            )
            higher_low = df["Low"] > df["Low"].shift(1)
            confirmation_count = (
                bullish.astype(int)
                + strong_close.fillna(False).astype(int)
                + higher_low.astype(int)
            )
            min_confirmations = max(
                0, min(3, int(float(settings.get("type_c_min_candle_confirmations", 1))))
            )
            cond_c_candle = confirmation_count >= min_confirmations

            volume_ratio = pd.to_numeric(
                df.get("Vol_Ratio", pd.Series(np.nan, index=df.index)),
                errors="coerce",
            )
            cond_c_volume = volume_ratio.between(
                float(settings.get("type_c_min_volume_ratio", 0.8)),
                float(settings.get("type_c_max_volume_ratio", 2.0)),
                inclusive="both",
            )
            max_position = float(
                settings.get("type_c_max_box_position_pct", 35.0)
            )
            max_entry_price = box_lower + (box_upper - box_lower) * (
                max_position / 100.0
            )
            cond_c_position = df["Close"] <= max_entry_price

            breakdown = df["Close"] < (box_lower - atr_c * breakdown_atr)
            consecutive_breakdown = breakdown & breakdown.shift(1).fillna(False)
            signal_c = (
                cond_c_touch
                & cond_c_reclaim
                & cond_c_prev_high_break
                & cond_c_rsi_rising
                & cond_c_candle
                & cond_c_volume
                & cond_c_position
                & ~breakdown
                & ~consecutive_breakdown
                & atr_c.notna()
            )
    except (KeyError, TypeError, ValueError, ArithmeticError):
        signal_c = pd.Series(False, index=df.index, dtype=bool)
    except Exception:
        signal_c = pd.Series(False, index=df.index, dtype=bool)

    # 6b. 트리플 볼린저 과열 차단 (기본 OFF — 백테스트 비교용)
    try:
        if "BB_Z" in df.columns:
            bb_z = pd.to_numeric(df["BB_Z"], errors="coerce")
            if _settings_bool(settings, "use_bb_block_a_2s", False):
                try:
                    thr_a = float(settings.get("bb_block_a_z", 2.0))
                except (TypeError, ValueError):
                    thr_a = 2.0
                signal_a = signal_a & ~(bb_z >= thr_a)
            if _settings_bool(settings, "use_bb_block_all_3s", False):
                try:
                    thr_all = float(settings.get("bb_block_all_z", 3.0))
                except (TypeError, ValueError):
                    thr_all = 3.0
                hot = bb_z >= thr_all
                signal_a = signal_a & ~hot
                signal_b = signal_b & ~hot
                signal_c = signal_c & ~hot
    except Exception:
        pass

    # 7. 최종 신호 및 공통 필터
    signal_ab = signal_a | signal_b
    
    if _settings_bool(settings, "use_macd_signal", True):
        # 15분 MACD 필터 ON: 히스토그램 양수(OSC>0)만 적용 (전봉 대비 상승 조건은 Type A/B와 중복되어 제거됨)
        signal_ab &= df["OSC"] > 0

    # 평균회귀 C에는 추세형 MACD 양수 필터를 적용하지 않습니다.
    signal = signal_ab | signal_c

    # 동시 신호: A·B 원시 조건이 모두 참이고 최종 Signal 도 참 — 동적 백테에서 보유 균형 동점 시 TYPE_B 우선에 사용
    s1_simul_ab = (
        np.asarray(signal_a, dtype=bool)
        & np.asarray(signal_b, dtype=bool)
        & np.asarray(signal, dtype=bool)
    )

    # 15:30(종가 단일가) S1 신호:
    # - 기본 OFF(마스크)
    # - next_open_1530_enabled / allow_1530_signal_eval / _bt_allow_1530_signal_eval
    #   이면 허용. 체결만 경로가 갈림(모의=익일09:00 예약, 실전=NXT+SOR, BT=시초 이월).
    try:
        _idx = pd.DatetimeIndex(pd.to_datetime(df.index))
        _allow_1530 = bool(settings_allow_1530_signal_eval(settings))
        try:
            from utils.krx_bars import is_krx_last_15m_slot

            _eod = np.asarray(
                [bool(is_krx_last_15m_slot(t)) for t in _idx],
                dtype=bool,
            )
        except Exception:
            _hm = _idx.hour.to_numpy(dtype=np.int32) * 60 + _idx.minute.to_numpy(
                dtype=np.int32
            )
            _eod = _hm == (15 * 60 + 30)
        if not _allow_1530:
            signal = np.asarray(signal, dtype=bool) & ~_eod
            signal_c = np.asarray(signal_c, dtype=bool) & ~_eod
            s1_simul_ab = np.asarray(s1_simul_ab, dtype=bool) & ~_eod
        else:
            signal = np.asarray(signal, dtype=bool)
            signal_c = np.asarray(signal_c, dtype=bool)
            s1_simul_ab = np.asarray(s1_simul_ab, dtype=bool)
    except Exception:
        pass
    
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
    c_conditions = [
        cond_c_touch,
        cond_c_reclaim,
        cond_c_prev_high_break,
        cond_c_rsi_rising,
        cond_c_candle,
        cond_c_volume,
    ]
    met_c = sum(condition.astype(int) for condition in c_conditions)
    total_c = len(c_conditions)
    
    # 더 높은 합치도를 가진 타입을 기준으로 점수 산출
    ratio_a = met_a / np.maximum(total_a, 1)
    ratio_b = met_b / np.maximum(total_b, 1)

    same_bar_pri = _normalize_same_bar_priority(settings)
    sig_type_col = _compute_sig_type_column(signal_a, signal_b, ratio_a, ratio_b, same_bar_pri)
    # C 전용 반등이 확인된 봉은 A/B 동시 여부와 무관하게 TYPE_C로 확정합니다.
    sig_type_col = np.where(np.asarray(signal_c, dtype=bool), "TYPE_C", sig_type_col)
    is_type_b_row = sig_type_col == "TYPE_B"
    is_type_c_row = sig_type_col == "TYPE_C"
    # Series·ndarray 혼용 시 위치 기준 합치도가 어긋나지 않도록 ndarray로 고정
    m_a = np.asarray(met_a, dtype=np.int32)
    m_b = np.asarray(met_b, dtype=np.int32)
    ta_i = max(1, int(total_a))
    tb_i = max(1, int(total_b))
    m_c = np.asarray(met_c, dtype=np.int32)
    tc_i = max(1, int(total_c))
    final_met = np.where(is_type_c_row, m_c, np.where(is_type_b_row, m_b, m_a))
    final_total = np.where(is_type_c_row, tc_i, np.where(is_type_b_row, tb_i, ta_i))

    n_rows = len(df.index)
    tot_a_col = np.full(n_rows, ta_i, dtype=np.int32)
    tot_b_col = np.full(n_rows, tb_i, dtype=np.int32)
    tot_c_col = np.full(n_rows, tc_i, dtype=np.int32)

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
        'Met_C': m_c,
        'Tot_C': tot_c_col,
        'C_Touch': cond_c_touch,
        'C_Reclaim': cond_c_reclaim,
        'C_PrevHighBreak': cond_c_prev_high_break,
        'C_RSI_Rising': cond_c_rsi_rising,
        'C_Candle': cond_c_candle,
        'C_Volume': cond_c_volume,
        'RSI_Range': cond_rsi_range,
        'MACD_OK': cond_macd_basic,
        'MA60_Range': cond_ma60,
        'MA_GC': cond_ma_gc,
        "Vol_Surge": cond_vol_a | cond_vol_b,
        "S1_Simultaneous_AB": s1_simul_ab,
    }, index=df.index)

def get_buy_signals(df, settings, market_trend='UP', signal_context: dict | None = None):
    """매수 신호 벡터만 반환"""
    return get_buy_signals_detailed(
        df, settings, market_trend, signal_context=signal_context
    )['Signal']
