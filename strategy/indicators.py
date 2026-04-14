from __future__ import annotations

import pandas as pd
import numpy as np

from strategy.indicator_constants import (
    MACD_ENTRY_FAST,
    MACD_ENTRY_SLOW,
    MACD_ENTRY_SIGNAL,
    RSI_MAIN_PERIOD,
    RSI_SIG_LOOKBACK,
)

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


def _read_prev_vol_lookback_days(
    settings: dict | None,
    key_candidates: tuple[str, ...],
    *,
    default: int,
) -> int:
    """신규·구 설정 키에서 전일 동시간 거래량 평균 일수를 읽는다."""
    if settings is None:
        return max(1, int(default))
    for k in key_candidates:
        try:
            if k in settings and settings[k] is not None and settings[k] != "":
                return max(1, int(float(settings[k])))
        except (TypeError, ValueError):
            continue
    return max(1, int(default))


def attach_prev_day_volume_quartet(df: pd.DataFrame, settings: dict | None) -> None:
    """
    S1/S2 × A/B 각각 독립 lookback으로 4열을 만든 뒤,
    레거시 호환용 Prev_Day_Vol(=S1A)·Prev_Day_Vol_B(=S1B)를 동기화한다.
    """
    s = settings or {}
    lb_s1a = _read_prev_vol_lookback_days(
        s, ("prev_vol_lookback_s1_a", "prev_vol_lookback", "vol_lookback"), default=10
    )
    lb_s1b = _read_prev_vol_lookback_days(s, ("prev_vol_lookback_s1_b", "prev_vol_lookback_b"), default=lb_s1a)
    lb_s2a = _read_prev_vol_lookback_days(
        s, ("prev_vol_lookback_s2_a", "prev_vol_lookback", "vol_lookback"), default=lb_s1a
    )
    lb_s2b = _read_prev_vol_lookback_days(s, ("prev_vol_lookback_s2_b", "prev_vol_lookback_b"), default=lb_s2a)

    cols = (
        "Prev_Day_Vol_S1_A",
        "Prev_Day_Vol_S1_B",
        "Prev_Day_Vol_S2_A",
        "Prev_Day_Vol_S2_B",
        "Prev_Day_Vol",
        "Prev_Day_Vol_B",
    )
    if not isinstance(df.index, pd.DatetimeIndex) or len(df) == 0 or "Volume" not in df.columns:
        for c in cols:
            df[c] = 0
        return

    done_lb: dict[int, str] = {}
    for lb, col in (
        (lb_s1a, "Prev_Day_Vol_S1_A"),
        (lb_s1b, "Prev_Day_Vol_S1_B"),
        (lb_s2a, "Prev_Day_Vol_S2_A"),
        (lb_s2b, "Prev_Day_Vol_S2_B"),
    ):
        src = done_lb.get(lb)
        if src is not None:
            df[col] = df[src].copy()
        else:
            attach_prev_day_same_time_volume(df, lb, col)
            done_lb[lb] = col

    df["Prev_Day_Vol"] = df["Prev_Day_Vol_S1_A"].copy()
    df["Prev_Day_Vol_B"] = df["Prev_Day_Vol_S1_B"].copy()


def attach_prev_day_same_time_volume(
    df: pd.DataFrame,
    vol_lookback_days: int,
    out_column: str,
) -> None:
    """
    전일 동시간대 거래량의 rolling 평균을 out_column에 기록.
    attach_prev_day_volume_quartet에서 S1/S2·A/B 열별로 호출된다.
    """
    vol_lookback = max(1, int(vol_lookback_days))
    if not isinstance(df.index, pd.DatetimeIndex) or len(df) == 0:
        df[out_column] = 0
        return
    try:
        df_tmp = df[["Volume"]].copy()
        df_tmp["date_only"] = df_tmp.index.normalize()
        df_tmp["time_only"] = df_tmp.index.time
        pivoted = df_tmp.pivot_table(index="date_only", columns="time_only", values="Volume")
        pivoted_rolled = pivoted.shift(1).rolling(window=vol_lookback, min_periods=1).mean()
        stacked = pivoted_rolled.stack(future_stack=True)
        lookup_keys = pd.MultiIndex.from_arrays([df_tmp.index.normalize(), df_tmp.index.time])
        df[out_column] = stacked.reindex(lookup_keys).values
        df[out_column] = df[out_column].ffill().fillna(0)
    except Exception:
        df[out_column] = 0


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
    
    # 3–4. MACD + RSI (S1 공통 15분: MACD·RSI 시그널 주기는 indicator_constants 고정)
    fast, slow, sig = MACD_ENTRY_FAST, MACD_ENTRY_SLOW, MACD_ENTRY_SIGNAL
    # [v37.9] Optimization: Preserve raw calculation states for Incremental Updates in 1m simulation
    df['_ema12'] = df['Close'].ewm(span=max(1, fast), adjust=False).mean()
    df['_ema26'] = df['Close'].ewm(span=max(1, slow), adjust=False).mean()
    df['MACD'] = df['_ema12'] - df['_ema26']

    sig_p = max(1, sig)
    df['_macd_sig'] = df['MACD'].ewm(span=sig_p, adjust=False).mean()
    df['MACD_Sig'] = df['_macd_sig']
    df['OSC'] = df['MACD'] - df['MACD_Sig']
    df['Prev_OSC'] = df['OSC'].shift(1)

    period = RSI_MAIN_PERIOD
    delta = df['Close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    df['_avg_gain'] = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    df['_avg_loss'] = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = df['_avg_gain'] / (df['_avg_loss'] + 1e-10)
    df['RSI'] = 100 - (100 / (1 + rs))

    rsi_sig_p = max(1, RSI_SIG_LOOKBACK)
    df['RSI_Sig'] = calculate_ma(df['RSI'], rsi_sig_p)
    df['Prev_RSI'] = df['RSI'].shift(1)

    # ... rest of the original logic (MSI, ADX, ATR, etc.)
    # 5. MSI (Market Strength Index)
    if 'MA120' in df.columns:
        df['MSI'] = (df['MA20'] / (df['MA60'] + 1e-10) - 1) + (df['MA60'] / (df['MA120'] + 1e-10) - 1)
    
    # 6. ADX & ATR (분리: ADX 예외가 나도 ATR은 이어서 계산 — 실시간 감시 TRL/SL·백테스트 청산)
    if "High" in df.columns and "Low" in df.columns:
        try:
            _, _, df["ADX"] = calculate_adx(df, 14)
        except Exception:
            df["ADX"] = np.nan
        # Type A/B 청산용 ATR 주기 분리 (구버전 atr_period → A 트랙 폴백)
        atr_pa = int(settings.get("atr_period_a", settings.get("atr_period", 14)))
        atr_pb = int(settings.get("atr_period_b", settings.get("atr_period", 14)))
        df["ATR_A"] = calculate_atr(df, max(1, atr_pa))
        df["ATR_B"] = calculate_atr(df, max(1, atr_pb))
        df["ATR"] = df["ATR_A"]

    # 전일 동시간 거래량: S1/S2 × A/B lookback 완전 분리 (+ 레거시 2열)
    if "Volume" in df.columns:
        attach_prev_day_volume_quartet(df, settings)

    return df

def calculate_buy_indicators_light(df, settings):
    """매수 신호용 경량 지표 계산 (실시간성 강조)"""
    df = df.copy()
    df['MA5'] = calculate_ma(df['Close'], 5)
    df['MA20'] = calculate_ma(df['Close'], 20)
    df['MA60'] = calculate_ma(df['Close'], 60)
    df['Vol_MA20'] = calculate_ma(df['Volume'], 20)
    
    df['RSI'] = calculate_rsi(df['Close'], RSI_MAIN_PERIOD)
    rsi_sig_p = max(1, RSI_SIG_LOOKBACK)
    df['RSI_Sig'] = calculate_ma(df['RSI'], rsi_sig_p)
    df['Prev_RSI'] = df['RSI'].shift(1)

    df['MACD'], df['MACD_Sig'], df['OSC'] = calculate_macd(
        df['Close'],
        max(1, MACD_ENTRY_FAST),
        max(1, MACD_ENTRY_SLOW),
        max(1, MACD_ENTRY_SIGNAL),
    )
    df['Prev_OSC'] = df['OSC'].shift(1)
    
    if isinstance(df.index, pd.DatetimeIndex) and len(df) > 0:
        attach_prev_day_volume_quartet(df, settings)
    else:
        for c in (
            "Prev_Day_Vol_S1_A",
            "Prev_Day_Vol_S1_B",
            "Prev_Day_Vol_S2_A",
            "Prev_Day_Vol_S2_B",
            "Prev_Day_Vol",
            "Prev_Day_Vol_B",
        ):
            df[c] = 0

    # ATR (v31.0: Added for exit monitoring)
    # If High/Low exist, calculate ATR (required for SL/Trail)
    if 'High' in df.columns and 'Low' in df.columns:
        atr_pa = int(settings.get("atr_period_a", settings.get("atr_period", 14)))
        atr_pb = int(settings.get("atr_period_b", settings.get("atr_period", 14)))
        df["ATR_A"] = calculate_atr(df, max(1, atr_pa))
        df["ATR_B"] = calculate_atr(df, max(1, atr_pb))
        df["ATR"] = df["ATR_A"]
    
    return df
