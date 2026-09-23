# -*- coding: utf-8 -*-
"""
동적 BT 15분 지표+신호 ProcessPool 워커.

dynamic_backtest 모듈은 import 시 reload 가 있어 Windows spawn 에 무겁다.
워커는 이 경량 모듈만 import 한다.
"""

from __future__ import annotations

import pickle
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from strategy.trading_strategy import TradingStrategy

# initializer 로 OHLCV·설정 1회 적재
_SIG15_POOL_OHLCV: Optional[Dict[str, Any]] = None
_SIG15_POOL_SETTINGS: Optional[dict] = None


def indicators_and_signals_15m_frame(
    group: pd.DataFrame,
    settings: dict,
) -> pd.DataFrame:
    """종목 15분 프레임에 지표·펄스·신호를 한 번에 붙인다."""
    df_ind = TradingStrategy.calculate_indicators(group, settings)
    is_ma_aligned = df_ind["MA5"] > df_ind["MA20"]
    ma_group_id = (~is_ma_aligned).cumsum()
    df_ind["MA_Maint_Count"] = is_ma_aligned.groupby(ma_group_id).cumsum()
    is_rsi_above_50 = df_ind["RSI"] >= 50.0
    rsi_group_id = (df_ind["RSI"] < 50.0).cumsum()
    df_ind["RSI_Pulse_Count"] = is_rsi_above_50.groupby(rsi_group_id).cumsum()
    full_sigs = TradingStrategy.get_buy_signals_detailed(
        df_ind, settings, market_trend="UP"
    )
    df_ind = df_ind.join(full_sigs.drop(columns="Signal", errors="ignore"))
    df_ind["Signal"] = full_sigs["Signal"]
    df_ind["Ready"] = df_ind.reset_index().index >= 19
    return df_ind


def sig15_pool_init(cache_path: str) -> None:
    """자식 프로세스에 OHLCV·설정을 1회만 적재."""
    global _SIG15_POOL_OHLCV, _SIG15_POOL_SETTINGS
    with open(cache_path, "rb") as f:
        payload = pickle.load(f)
    _SIG15_POOL_OHLCV = payload.get("ohlcv") or {}
    _SIG15_POOL_SETTINGS = payload.get("settings") or {}


def sig15_pool_job(code: str) -> Tuple[Any, Optional[pd.DataFrame]]:
    """ProcessPool 단일 종목 지표+신호."""
    try:
        if not isinstance(_SIG15_POOL_OHLCV, dict) or _SIG15_POOL_SETTINGS is None:
            return code, None
        group = _SIG15_POOL_OHLCV.get(code)
        if group is None or not isinstance(group, pd.DataFrame) or group.empty:
            return code, None
        return code, indicators_and_signals_15m_frame(group, _SIG15_POOL_SETTINGS)
    except Exception as e:
        try:
            print(f"[Dynamic] 15분 지표/신호 실패 ({code}): {e!r}", flush=True)
        except Exception:
            pass
        return code, None
