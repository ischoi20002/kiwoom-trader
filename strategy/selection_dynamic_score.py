# -*- coding: utf-8 -*-
"""동적 유니버스 일봉 스코어·lookback 헬퍼.

``strategy_selector`` 에서 분리. re-export 로 호환.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional, Set

import pandas as pd

from strategy.selection_track_batch import (
    _apply_sel_track_a_batch_v2,
    _apply_sel_track_b_batch_v2,
    _apply_sel_track_c_batch_latest,
)


def _score_dynamic_universe_daily_ohlcv(
    df_all: pd.DataFrame,
    settings: dict,
    *,
    with_b_debug: bool = False,
    track_c_codes: Optional[Set[str]] = None,
) -> None:
    """
    ``prepare_dynamic_universe`` 와 동일: 일봉 OHLCV → 지표·Valid_A/B/C까지 in-place.
    with_b_debug=True 이면 Track B 부분조건 열(Dbg_*)을 붙여 일자별 진단에 쓴다.

    groupby("code") 를 재사용해 슬라이딩 일자 루프의 반복 비용을 줄인다.
    수식·EWM 재시작 의미는 동일하다.

    track_c_codes: Track C 평가 종목(당기 유니버스). None 이면 패널 전 종목.
    """
    # 4. 벡터 지표
    df_all["close"] = df_all["close"].astype(float)
    df_all["volume"] = df_all["volume"].astype(float)
    df_all["trde_val"] = df_all["close"] * df_all["volume"]

    g = df_all.groupby("code", sort=False)
    close_g = g["close"]
    df_all["MA5"] = close_g.rolling(5).mean().reset_index(level=0, drop=True)
    df_all["MA20"] = close_g.rolling(20).mean().reset_index(level=0, drop=True)
    df_all["MA60"] = close_g.rolling(60).mean().reset_index(level=0, drop=True)

    delta = close_g.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    # gain/loss 는 code 인덱스가 유지되므로 동일 키로 ewm
    code_key = df_all["code"]
    ema_up = (
        gain.groupby(code_key, sort=False)
        .ewm(com=13, adjust=False)
        .mean()
        .reset_index(level=0, drop=True)
    )
    ema_down = (
        loss.groupby(code_key, sort=False)
        .ewm(com=13, adjust=False)
        .mean()
        .reset_index(level=0, drop=True)
    )
    df_all["RSI"] = 100 - (100 / (1 + ema_up / (ema_down + 1e-10)))

    m_f, m_sl, m_sg = (
        int(settings.get("macd_fast_daily", 12)),
        int(settings.get("macd_slow_daily", 26)),
        int(settings.get("macd_sig_daily", 9)),
    )
    ema_f = close_g.ewm(span=m_f, adjust=False).mean().reset_index(level=0, drop=True)
    ema_s = close_g.ewm(span=m_sl, adjust=False).mean().reset_index(level=0, drop=True)
    df_all["MACD"] = ema_f - ema_s
    macd_s = (
        df_all.groupby("code", sort=False)["MACD"]
        .ewm(span=m_sg, adjust=False)
        .mean()
        .reset_index(level=0, drop=True)
    )
    df_all["OSC"] = df_all["MACD"] - macd_s

    min_억 = float(settings.get("min_trde_val", 40.0))
    conv = float(settings.get("ma_convergence", 3.0)) / 100.0
    vol_lookback = int(settings.get("vol_lookback", 10))
    df_all["AvgVal5"] = (
        g["trde_val"].rolling(5).mean().reset_index(level=0, drop=True)
    )
    df_all["MA_Diff"] = (df_all["MA5"] - df_all["MA20"]).abs() / df_all["MA20"]
    df_all["Pos_Diff"] = (df_all["close"] - df_all["MA20"]).abs() / df_all["MA20"]
    prev_close = close_g.shift(1)
    prev_vol = g["volume"].shift(1)
    df_all["VolRatio"] = df_all["volume"] / prev_vol
    df_all["OSC_Prev"] = df_all.groupby("code", sort=False)["OSC"].shift(1)

    vol_ratio_a = float(settings.get("vol_ratio", 1.5))
    df_all["VolBurst_A"] = (df_all["VolRatio"] >= vol_ratio_a) & (
        df_all["close"] > prev_close
    )
    df_all["VolBurstActive_A"] = (
        df_all.groupby("code", sort=False)["VolBurst_A"]
        .rolling(vol_lookback)
        .max()
        .reset_index(level=0, drop=True)
    )
    vol_ratio_b = float(settings.get("sel_b_vol_ratio", vol_ratio_a))
    vol_lookback_b = int(settings.get("sel_b_vol_lookback", vol_lookback))
    df_all["VolBurst_B"] = (df_all["VolRatio"] >= vol_ratio_b) & (
        df_all["close"] > prev_close
    )
    df_all["VolBurstActive_B"] = (
        df_all.groupby("code", sort=False)["VolBurst_B"]
        .rolling(vol_lookback_b)
        .max()
        .reset_index(level=0, drop=True)
    )

    use_a = bool(settings.get("use_sel_track_a", True))
    if use_a:
        _apply_sel_track_a_batch_v2(df_all, settings, min_억, conv)
    else:
        # use_a=False 여도 아래 Track B union에서 column 존재가 필요합니다.
        df_all["Valid_A"] = pd.Series(False, index=df_all.index)
        df_all["Score_A"] = pd.Series(0.0, index=df_all.index)

    use_b = bool(settings.get("use_sel_track_b", True))
    if use_b:
        _apply_sel_track_b_batch_v2(df_all, settings, with_b_debug=with_b_debug)
    else:
        df_all["Valid_B"] = pd.Series(False, index=df_all.index)
        df_all["Score_B"] = pd.Series(0.0, index=df_all.index)
    _apply_sel_track_c_batch_latest(df_all, settings, codes=track_c_codes)
    df_all["Valid"] = df_all[["Valid_A", "Valid_B", "Valid_C"]].fillna(False).any(axis=1)
    df_all["Score"] = df_all[["Score_A", "Score_B", "Score_C"]].fillna(0.0).max(axis=1)


def _selection_daily_lookback_calendar_days(settings: Optional[dict] = None) -> int:
    """
    선정용 일봉 lookback (달력일).

    실시간 FDR·동적 BT 배치가 동일 키를 쓴다.
    평가일 D 기준 ``[D-N, D]`` 창으로 지표를 다시 계산한다(슬라이딩).
    """
    try:
        lookback = int(
            (settings or {}).get("bt_universe_indicator_lookback_days", 280) or 280
        )
    except Exception:
        lookback = 280
    try:
        return max(120, min(int(lookback), 800))
    except Exception:
        return 280


def _universe_batch_indicator_lookback_days(settings: Optional[dict] = None) -> int:
    """하위 호환 별칭 — ``_selection_daily_lookback_calendar_days`` 와 동일."""
    return _selection_daily_lookback_calendar_days(settings)


def _universe_batch_indicator_left_clip(
    end_date: str,
    settings: Optional[dict] = None,
) -> str:
    """하위 호환: 평가일(end) 기준 lookback 시작일."""
    try:
        end_dt = pd.to_datetime(end_date)
    except Exception:
        end_dt = pd.Timestamp(end_date)
    lookback = _selection_daily_lookback_calendar_days(settings)
    return (end_dt - timedelta(days=lookback)).strftime("%Y-%m-%d")
