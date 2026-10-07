# -*- coding: utf-8 -*-
"""BT/RT 공유 매매 전략 엔진 (TradingStrategy).

``backtest_engine`` 에서 분리. 기존 ``from backtest_engine import TradingStrategy`` 는
backtest_engine 의 re-export 로 호환됩니다.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

import FinanceDataReader as fdr
import os
import pandas as pd

pd.set_option("future.no_silent_downcasting", True)
import json
import time
import requests
import config
from datetime import datetime, timedelta
import numpy as np
from data_manager_v2 import DataManagerV2
import strategy_selector
import itertools

from utils.normalization import normalize_code
from strategy.data_loader import BacktestDataLoader
from strategy import indicators, signals
from strategy.indicators import calculate_all_indicators
from strategy.indicator_constants import (
    MACD_ENTRY_FAST,
    MACD_ENTRY_SLOW,
    MACD_ENTRY_SIGNAL,
    RSI_MAIN_PERIOD,
)
from strategy.synthetic_realtime_fill import (
    synthesize_bid_ask_ratio_vectorized,
    synthesize_vol_power_vectorized,
)
from utils.fee_rates import fee_decimal_rates_backtest


class TradingStrategy:
    """[v6.4] 실시간 감시와 백테스팅이 공유하는 매매 전략 엔진"""
    # 유니버스 코드->이름 맵 캐시 (KOSPI 시총 상위)
    _name_map_by_choice: dict[str, dict[str, str]] = {}
    
    @staticmethod
    def get_stock_name_map(universe_index_choice: str = "KOSPI200"):
        """종목코드 -> 종목명 맵핑 로드 (``kospi_universe.csv``).

        KODEX200 구성 유니버스는 폐기되어 항상 KOSPI CSV를 사용한다.
        """
        try:
            from strategy.universe_codes import normalize_universe_index_choice

            choice = normalize_universe_index_choice(universe_index_choice)
        except Exception:
            choice = "KOSPI200"

        cached = TradingStrategy._name_map_by_choice.get(choice)
        if cached is not None:
            return cached

        csv_path = "kospi_universe.csv"

        try:
            if not os.path.exists(csv_path):
                import glob
                files = (
                    glob.glob("kospi_universe*.csv")
                    + glob.glob("kospi_*.csv")
                    + glob.glob("kospi200*.csv")
                )
                if files:
                    csv_path = files[0]
                else:
                    TradingStrategy._name_map_by_choice[choice] = {}
                    return {}
            
            if os.path.exists(csv_path):
                # kospi_universe.csv 포맷(종목코드/종목명 또는 Code/Name) 허용
                df_name = pd.read_csv(csv_path, dtype=str, encoding='utf-8-sig')

                if '종목코드' in df_name.columns:
                    df_name.rename(columns={'종목코드': 'Code', '종목명': 'Name'}, inplace=True)
                elif '종목명' in df_name.columns and 'Name' not in df_name.columns:
                    df_name.rename(columns={'종목명': 'Name'}, inplace=True)
                
                # 종목코드가 6자리가 되도록 패딩 (예: 660 -> 000660)
                if 'Code' not in df_name.columns:
                    TradingStrategy._name_map_by_choice[choice] = {}
                    return {}
                if 'Name' not in df_name.columns:
                    df_name['Name'] = df_name['Code']

                df_name['Code'] = df_name['Code'].apply(lambda x: str(x).zfill(6))

                TradingStrategy._name_map_by_choice[choice] = dict(zip(df_name['Code'], df_name['Name']))
            else:
                TradingStrategy._name_map_by_choice[choice] = {}
        except Exception as e:
            print(f"  (!) Name Map Load Error (Path: {csv_path}): {e}")
            TradingStrategy._name_map_by_choice[choice] = {}
        return TradingStrategy._name_map_by_choice[choice]

    @staticmethod
    def calculate_buy_indicators(df, settings):
        """[v15.2] 매수 신호용 경량 지표 계산 (RSI, MACD, MA 전용)"""
        return indicators.calculate_buy_indicators_light(df, settings)

    @staticmethod
    def calculate_indicators(df, settings):
        """지표 계산 (공통)"""
        return calculate_all_indicators(df, settings)

    @staticmethod
    def simulate_intrabar_indicators(last_15m_dict, m1_row, settings, hist_df_prices_tail=None):
        """
        [v40.0] Optimized Incremental Indicator Update
        - Accepts dict for O(1) access and avoids Series copying.
        """
        m1_p = m1_row['Close']
        # Dictionary copy is faster than Series copy
        combined = last_15m_dict.copy()
        combined['Close'] = m1_p
        
        # 1. Moving Averages (MA) - Using pre-calculated sums if available or simple mean
        for n in [5, 20, 60]:
            col = f'MA{n}'
            if col in last_15m_dict and hist_df_prices_tail is not None and n in hist_df_prices_tail:
                # MA_new = (Sum(last n-1) + new_p) / n
                combined[col] = (hist_df_prices_tail[n] + m1_p) / n

        # 2. RSI (Wilder's Smoothing)
        _rp = max(2, int(RSI_MAIN_PERIOD))
        if 'RSI' in last_15m_dict and hist_df_prices_tail is not None and 'rsi_vals' in hist_df_prices_tail:
            diff = m1_p - last_15m_dict['Close']
            gain = max(0, diff)
            loss = max(0, -diff)

            alpha = 1.0 / float(_rp)
            avg_gain = last_15m_dict.get('_avg_gain', 0)
            avg_loss = last_15m_dict.get('_avg_loss', 0)
            
            if avg_gain == 0: # Fallback using tail prices
                vals = hist_df_prices_tail['rsi_vals']
                diffs = np.diff(np.append(vals, [m1_p]))
                avg_gain = np.maximum(0, diffs).mean()
                avg_loss = np.maximum(0, -diffs).mean()
            else:
                avg_gain = alpha * gain + (1 - alpha) * avg_gain
                avg_loss = alpha * loss + (1 - alpha) * avg_loss
            
            rs = avg_gain / (avg_loss + 1e-10)
            combined['RSI'] = 100 - (100 / (1 + rs))

        # 3. MACD (EMA)
        for span, col_prefix in [(MACD_ENTRY_FAST, '_ema12'), (MACD_ENTRY_SLOW, '_ema26')]:
            alpha = 2.0 / (span + 1.0)
            prev_ema = last_15m_dict.get(col_prefix, last_15m_dict['Close'])
            combined[col_prefix] = m1_p * alpha + prev_ema * (1 - alpha)

        if '_ema12' in combined and '_ema26' in combined:
            combined['MACD'] = combined['_ema12'] - combined['_ema26']
            sig_span = max(1, int(MACD_ENTRY_SIGNAL))
            sig_alpha = 2.0 / (sig_span + 1.0)
            prev_sig = last_15m_dict.get('_macd_sig', last_15m_dict['MACD'])
            combined['_macd_sig'] = combined['MACD'] * sig_alpha + prev_sig * (1 - sig_alpha)
            combined['MACD_Sig'] = combined['_macd_sig']
            combined['OSC'] = combined['MACD'] - combined['MACD_Sig']

        if 'bid_ask_ratio' in m1_row:
            combined['bid_ask_ratio'] = m1_row['bid_ask_ratio']
            try:
                _ba_m1 = float(m1_row['bid_ask_ratio'])
                combined['ask_bid_ratio'] = 1.0 / (_ba_m1 + 1e-10)
            except (TypeError, ValueError):
                pass
        elif 'ask_bid_ratio' in m1_row:
            combined['ask_bid_ratio'] = m1_row['ask_bid_ratio']
        if 'vol_power' in m1_row: combined['vol_power'] = m1_row['vol_power']
        
        return combined

        # 3. MACD (EMA based) — MACD_ENTRY_* 고정
        # EMA_new = price * alpha + prev_ema * (1 - alpha)
        for span, col_prefix in [(MACD_ENTRY_FAST, '_ema12'), (MACD_ENTRY_SLOW, '_ema26')]:
            alpha = 2.0 / (span + 1.0)
            prev_ema = last_15m.get(col_prefix, last_15m['Close']) # Fallback
            new_ema = m1_p * alpha + prev_ema * (1 - alpha)
            combined[col_prefix] = new_ema

        if '_ema12' in combined and '_ema26' in combined:
            combined['MACD'] = combined['_ema12'] - combined['_ema26']
            # Signal Line (EMA of MACD line)
            sig_span = max(1, int(MACD_ENTRY_SIGNAL))
            sig_alpha = 2.0 / (sig_span + 1.0)
            prev_sig = last_15m.get('_macd_sig', last_15m['MACD'])
            combined['_macd_sig'] = combined['MACD'] * sig_alpha + prev_sig * (1 - sig_alpha)
            combined['MACD_Sig'] = combined['_macd_sig']
            combined['OSC'] = combined['MACD'] - combined['MACD_Sig']

        # [v37.9] Smart Buy 필터용 수급 지표 반영 (시뮬레이션 필요 없음. 1분봉 데이터 직결)
        if 'bid_ask_ratio' in m1_row: combined['bid_ask_ratio'] = m1_row['bid_ask_ratio']
        if 'vol_power' in m1_row: combined['vol_power'] = m1_row['vol_power']
        
        return combined

    @staticmethod
    def get_buy_signals_detailed(
        df,
        settings,
        market_trend='UP',
        signal_context: Optional[dict[str, Any]] = None,
    ):
        """매수 신호 상세 분석(Type A/B/C 상태값 포함)."""
        return signals.get_buy_signals_detailed(
            df,
            settings,
            market_trend,
            signal_context=signal_context,
        )

    @staticmethod
    def get_buy_signals(df, settings, market_trend='UP'):
        """매수 신호 벡터 계산 (v6.1 통합: Trigger 기반)"""
        return TradingStrategy.get_buy_signals_detailed(df, settings, market_trend)['Signal']

    @staticmethod
    def _check_s2_approval(pos, last_row, settings, indicator_row=None, market_trend='UP', curr_dt=None, indicators_dict=None, i=None):
        """[Phase 2] Type A/B 완전 분리된 2차 매수 승인 로직 (v40.0: Optimized)"""
        s = settings

        # Box Reversion은 상단 회귀를 노리는 단일 진입 전략으로 S2 추매를 금지합니다.
        try:
            sig_type = str(pos.get("sig_type", pos.get("reason", "")) or "").upper()
            if "TYPE_C" in sig_type:
                return False
        except Exception:
            return False

        # 0. 기술 지표 참조 (Dict created if not provided - O(1) if passed directly)
        ind = indicator_row
        if ind is None and indicators_dict is not None and i is not None:
             # Lazy-access pattern to avoid dict creation for entire row
             pass # Use indicators_dict[key][i] directly below
        elif ind is None:
             ind = last_row
        
        def _get_val(key, default=0):
            if indicators_dict is not None and i is not None:
                return indicators_dict.get(key, {}).get(i, default) if hasattr(indicators_dict.get(key), 'get') else indicators_dict[key][i] if key in indicators_dict else default
            return ind.get(key, default) if ind is not None else default

        # Type A / Type B: S2 파라미터 완전 분리 (교차 폴백 없음)
        _is_b = TradingStrategy._holding_sig_is_type_b(pos)
        if not _is_b:
            try:
                _is_b = "TYPE_B" in str(pos.get("reason", "")).upper()
            except Exception:
                _is_b = False

        # Type A/B 각각 추매(S2) 전체 ON/OFF — OFF면 세부 조건과 무관하게 2차 매수 불가
        try:
            if _is_b:
                if not bool(s.get("use_s2_master_b", True)):
                    return False
            else:
                if not bool(s.get("use_s2_master_a", True)):
                    return False
        except Exception:
            pass

        def _s2(a_key: str, b_key: str, default):
            """Type A는 s2_* 만, Type B는 s2_b_* 만 사용."""
            return s.get(b_key, default) if _is_b else s.get(a_key, default)

        # 0. 최소 대기 기간
        if _s2('use_s2_delay', 'use_s2_b_delay', True):
            min_hours = float(_s2('s2_delay_hours', 's2_b_delay_hours', 24))
            entry_dt = pd.to_datetime(pos['entry_date'])
            if curr_dt is None:
                if last_row is not None: curr_dt = pd.to_datetime(last_row.name)
            else:
                curr_dt = pd.to_datetime(curr_dt)
            
            if curr_dt is not None and (curr_dt - entry_dt).total_seconds() / 3600 < min_hours:
                return False

        curr_price = float(_get_val('Close', 0.0))

        # 1. MA20 지지 (Type A/B 각각 전용 키)
        if _s2('use_s2_ma20', 'use_s2_b_ma20', True):
            ma20_val = _get_val('MA20', 0)
            gap = float(_s2('s2_ma20_gap', 's2_b_ma20_gap', 0.0))
            if ma20_val == 0 or curr_price < ma20_val * (1 + gap / 100.0):
                return False

        # 2. MA5 지지
        if _s2('use_s2_ma5', 'use_s2_b_ma5', True):
            ma5_val = _get_val('MA5', 0)
            gap = float(_s2('s2_ma5_gap', 's2_b_ma5_gap', -0.5))
            if curr_price < ma5_val * (1 + gap / 100.0):
                return False

        # 3. 거래 수급 — S2 전용 기준열 (lookback은 prev_vol_lookback_s2_a / s2_b)
        if _s2('use_s2_vol_surge', 'use_s2_b_vol_surge', True):
            if _is_b:
                vol_target = _get_val(
                    "Prev_Day_Vol_S2_B",
                    _get_val("Prev_Day_Vol_B", _get_val("Vol_MA20", _get_val("Volume", 0))),
                )
            else:
                vol_target = _get_val(
                    "Prev_Day_Vol_S2_A",
                    _get_val("Prev_Day_Vol", _get_val("Vol_MA20", _get_val("Volume", 0))),
                )
            ratio = float(_s2('s2_vol_ratio', 's2_b_vol_ratio', 1.5))
            if _get_val('Volume', 0) < vol_target * ratio:
                return False

        # 4. 모멘텀 확인
        rsi_ok = True
        macd_ok = True
        if _s2('use_s2_rsi_sig', 'use_s2_b_rsi_sig', True):
            rsi_sig = _get_val('RSI_Sig', 50)
            rsi_val = _get_val('RSI', 0)
            rsi_prev = _get_val('Prev_RSI', rsi_val)
            gap = float(_s2('s2_rsi_gap', 's2_b_rsi_gap', 0.0))
            rsi_ok = rsi_val >= rsi_sig * (1 + gap / 100.0) and rsi_val > rsi_prev

        if _s2('use_s2_macd_gc', 'use_s2_b_macd_gc', True):
            macd_val = _get_val('MACD', 0)
            macd_sig_val = _get_val('MACD_Sig', 0)
            osc_val = _get_val('OSC', 1)
            osc_prev = _get_val('Prev_OSC', osc_val)
            gap = float(_s2('s2_macd_gap', 's2_b_macd_gap', 0.0))
            macd_ok = macd_val >= macd_sig_val + gap and osc_val > 0 and osc_val > osc_prev

        use_rsi = _s2('use_s2_rsi_sig', 'use_s2_b_rsi_sig', True)
        use_macd = _s2('use_s2_macd_gc', 'use_s2_b_macd_gc', True)
        if use_rsi and use_macd:
            if not (rsi_ok or macd_ok):
                return False
        elif use_rsi and not rsi_ok:
            return False
        elif use_macd and not macd_ok:
            return False

        # 5. 수익권 확인
        if _s2('use_s2_profit', 'use_s2_b_profit', True):
            profit_limit = float(_s2('s2_profit_limit', 's2_b_profit_limit', 1.0))
            avg_price = pos.get('avg_price', pos.get('avg_p', curr_price))
            if avg_price > 0:
                current_profit_pct = (curr_price / avg_price - 1) * 100
                if current_profit_pct < profit_limit:
                    return False

        return True

    @staticmethod
    def explain_s2_approval_failure(
        pos,
        last_row,
        settings,
        indicator_row=None,
        market_trend="UP",
        curr_dt=None,
        indicators_dict=None,
        i=None,
    ) -> str | None:
        """
        ``_check_s2_approval`` 가 False 인 첫 실패 사유(진단용).
        통과하면 None. 승인 로직과 동일 분기를 따르되 부작용 없음.
        """
        try:
            if TradingStrategy._check_s2_approval(
                pos,
                last_row,
                settings,
                indicator_row=indicator_row,
                market_trend=market_trend,
                curr_dt=curr_dt,
                indicators_dict=indicators_dict,
                i=i,
            ):
                return None
        except Exception as e:
            return f"s2_check_exception:{e}"

        s = settings
        try:
            sig_type = str(pos.get("sig_type", pos.get("reason", "")) or "").upper()
            if "TYPE_C" in sig_type:
                return "s2_type_c_disabled"
        except Exception:
            return "s2_type_unknown"
        ind = indicator_row
        if ind is None and indicators_dict is not None and i is not None:
            pass
        elif ind is None:
            ind = last_row

        def _get_val(key, default=0):
            if indicators_dict is not None and i is not None:
                return (
                    indicators_dict.get(key, {}).get(i, default)
                    if hasattr(indicators_dict.get(key), "get")
                    else indicators_dict[key][i]
                    if key in indicators_dict
                    else default
                )
            return ind.get(key, default) if ind is not None else default

        _is_b = TradingStrategy._holding_sig_is_type_b(pos)
        if not _is_b:
            try:
                _is_b = "TYPE_B" in str(pos.get("reason", "")).upper()
            except Exception:
                _is_b = False

        try:
            if _is_b:
                if not bool(s.get("use_s2_master_b", True)):
                    return "s2_master_b_off"
            else:
                if not bool(s.get("use_s2_master_a", True)):
                    return "s2_master_a_off"
        except Exception:
            pass

        def _s2(a_key: str, b_key: str, default):
            return s.get(b_key, default) if _is_b else s.get(a_key, default)

        if _s2("use_s2_delay", "use_s2_b_delay", True):
            try:
                min_hours = float(_s2("s2_delay_hours", "s2_b_delay_hours", 24))
                entry_dt = pd.to_datetime(pos["entry_date"])
                _cdt = curr_dt
                if _cdt is None and last_row is not None:
                    _cdt = pd.to_datetime(last_row.name)
                else:
                    _cdt = pd.to_datetime(_cdt) if _cdt is not None else None
                if _cdt is not None:
                    elapsed_h = (_cdt - entry_dt).total_seconds() / 3600.0
                    if elapsed_h < min_hours:
                        return f"s2_delay:{elapsed_h:.2f}h<{min_hours:g}h"
            except Exception as e:
                return f"s2_delay_error:{e}"

        curr_price = float(_get_val("Close", 0.0))

        if _s2("use_s2_ma20", "use_s2_b_ma20", True):
            ma20_val = _get_val("MA20", 0)
            gap = float(_s2("s2_ma20_gap", "s2_b_ma20_gap", 0.0))
            if ma20_val == 0 or curr_price < ma20_val * (1 + gap / 100.0):
                return f"s2_ma20:px={curr_price:.0f}<ma20*{1+gap/100:.4g}({float(ma20_val):.0f})"

        if _s2("use_s2_ma5", "use_s2_b_ma5", True):
            ma5_val = _get_val("MA5", 0)
            gap = float(_s2("s2_ma5_gap", "s2_b_ma5_gap", -0.5))
            if curr_price < ma5_val * (1 + gap / 100.0):
                return f"s2_ma5:px={curr_price:.0f}<ma5*{1+gap/100:.4g}({float(ma5_val):.0f})"

        if _s2("use_s2_vol_surge", "use_s2_b_vol_surge", True):
            if _is_b:
                vol_target = _get_val(
                    "Prev_Day_Vol_S2_B",
                    _get_val("Prev_Day_Vol_B", _get_val("Vol_MA20", _get_val("Volume", 0))),
                )
            else:
                vol_target = _get_val(
                    "Prev_Day_Vol_S2_A",
                    _get_val("Prev_Day_Vol", _get_val("Vol_MA20", _get_val("Volume", 0))),
                )
            ratio = float(_s2("s2_vol_ratio", "s2_b_vol_ratio", 1.5))
            vol_now = float(_get_val("Volume", 0) or 0)
            need = float(vol_target or 0) * ratio
            if vol_now < need:
                return f"s2_vol:vol={vol_now:.0f}<need={need:.0f}(x{ratio:g})"

        rsi_ok = True
        macd_ok = True
        if _s2("use_s2_rsi_sig", "use_s2_b_rsi_sig", True):
            rsi_sig = _get_val("RSI_Sig", 50)
            rsi_val = _get_val("RSI", 0)
            rsi_prev = _get_val("Prev_RSI", rsi_val)
            gap = float(_s2("s2_rsi_gap", "s2_b_rsi_gap", 0.0))
            rsi_ok = rsi_val >= rsi_sig * (1 + gap / 100.0) and rsi_val > rsi_prev
        if _s2("use_s2_macd_gc", "use_s2_b_macd_gc", True):
            macd_val = _get_val("MACD", 0)
            macd_sig_val = _get_val("MACD_Sig", 0)
            osc_val = _get_val("OSC", 1)
            osc_prev = _get_val("Prev_OSC", osc_val)
            gap = float(_s2("s2_macd_gap", "s2_b_macd_gap", 0.0))
            macd_ok = macd_val >= macd_sig_val + gap and osc_val > 0 and osc_val > osc_prev
        use_rsi = _s2("use_s2_rsi_sig", "use_s2_b_rsi_sig", True)
        use_macd = _s2("use_s2_macd_gc", "use_s2_b_macd_gc", True)
        if use_rsi and use_macd:
            if not (rsi_ok or macd_ok):
                return "s2_momentum:rsi_and_macd_fail"
        elif use_rsi and not rsi_ok:
            return "s2_momentum:rsi_fail"
        elif use_macd and not macd_ok:
            return "s2_momentum:macd_fail"

        if _s2("use_s2_profit", "use_s2_b_profit", True):
            profit_limit = float(_s2("s2_profit_limit", "s2_b_profit_limit", 1.0))
            avg_price = pos.get("avg_price", pos.get("avg_p", curr_price))
            if avg_price and float(avg_price) > 0:
                current_profit_pct = (curr_price / float(avg_price) - 1) * 100
                if current_profit_pct < profit_limit:
                    return (
                        f"s2_profit:{current_profit_pct:.2f}%<{profit_limit:g}%"
                    )

        return "s2_unknown_fail"

    @staticmethod
    def explain_exit_hold(
        now_price: float,
        pos: dict | None,
        settings: dict | None,
        indicator_row=None,
        *,
        kwargs: dict | None = None,
        current_time=None,
        is_realtime: bool = False,
        time_is_bar_label: bool = False,
        eval_hold_reason: str | None = None,
    ) -> str:
        """
        ``evaluate_position`` 이 HOLD 일 때 청산이 안 난 이유를 짧게 요약(진단용).
        실제 청산 판정은 바꾸지 않으며, Flash/ATR/TP/SL/갭 게이트를 관측만 한다.
        """
        try:
            if eval_hold_reason and str(eval_hold_reason).startswith("GRACE"):
                return str(eval_hold_reason)
        except Exception:
            pass
        try:
            px = float(now_price or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px <= 0:
            return "exit:bad_price"
        if not isinstance(pos, dict):
            return "exit:no_pos"
        s = settings or {}
        parts: list[str] = []

        def _gv(key: str, default: float = 0.0) -> float:
            try:
                if indicator_row is None:
                    return float(default)
                v = indicator_row.get(key, default)
                return float(v if v is not None else default)
            except Exception:
                return float(default)

        try:
            stage = int(TradingStrategy._coerce_position_stage(pos))
        except Exception:
            stage = 1
        is_type_b = TradingStrategy._holding_sig_is_type_b(pos)
        if not is_type_b:
            try:
                is_type_b = "TYPE_B" in str(pos.get("reason", "")).upper()
            except Exception:
                is_type_b = False
        if not is_type_b and kwargs:
            try:
                sth = kwargs.get("sel_track") or kwargs.get("SelTrack")
                if sth is not None and str(sth).strip().upper() == "B":
                    is_type_b = True
            except Exception:
                pass

        try:
            avg_p = float(pos.get("avg_p", pos.get("avg_price", px)) or px)
        except (TypeError, ValueError):
            avg_p = px
        profit_ratio = (px / avg_p - 1.0) * 100.0 if avg_p > 0 else 0.0

        try:
            max_p = float(
                pos.get("max_p", pos.get("max_price", pos.get("max_now", px))) or px
            )
        except (TypeError, ValueError):
            max_p = px
        try:
            atr_peak = float(pos.get("max_p_atr", max_p) or max_p)
        except (TypeError, ValueError):
            atr_peak = max_p

        def _exit_key(a_key: str, b_key: str, s2_a_key: str, s2_b_key: str) -> str:
            if stage >= 2:
                return s2_b_key if is_type_b else s2_a_key
            return b_key if is_type_b else a_key

        def _atr_suff() -> str:
            if stage >= 2:
                return "_s2_b" if is_type_b else "_s2"
            return "_b" if is_type_b else "_a"

        try:
            _rx = TradingStrategy._exit_track_risk_bundle(s, is_type_b)
        except Exception:
            _rx = {
                "use_gap_down_exit": False,
                "gap_down_limit": -5.0,
                "use_atr_exit": True,
                "use_gap_grace": False,
                "gap_grace_mins": 0,
            }

        # Gap
        try:
            if bool(_rx.get("use_gap_down_exit")):
                gl = float(_rx.get("gap_down_limit", -5.0))
                if profit_ratio > gl:
                    parts.append(f"gap:pnl={profit_ratio:.2f}%>{gl:g}%")
                else:
                    parts.append(f"gap:HIT({profit_ratio:.2f}%)")
        except Exception:
            pass

        # TP / trail-only-after-TP / flash suppress
        _trail_only = False
        try:
            _trail_only = bool(
                TradingStrategy._use_atr_trail_only_after_tp_for_stage(
                    s, is_type_b, stage
                )
            )
        except Exception:
            _trail_only = False
        tp_key = _exit_key(
            "s1_a_take_profit",
            "s1_b_take_profit",
            "s2_take_profit",
            "s2_b_take_profit",
        )
        try:
            tp_pct = float(s.get(tp_key, 5.0))
        except (TypeError, ValueError):
            tp_pct = 5.0
        try:
            peak_pnl = (
                (atr_peak / avg_p - 1.0) * 100.0 if avg_p > 0 else profit_ratio
            )
        except Exception:
            peak_pnl = profit_ratio
        defer_tp = False
        if _trail_only:
            try:
                sfx = _atr_suff()
                raw_tr = bool(s.get(f"use_atr_trailing_stop{sfx}", True))
                defer_tp = bool(
                    TradingStrategy._effective_use_atr_trailing_for_stage(
                        s, _rx, stage, raw_tr
                    )
                )
            except Exception:
                defer_tp = False
        skip_flash = bool(defer_tp and peak_pnl >= tp_pct)
        if defer_tp:
            parts.append(
                f"tp:defer_trail(peak={peak_pnl:.2f}%/arm={tp_pct:g}%)"
            )
        else:
            use_tp = bool(
                s.get(
                    _exit_key("use_s1_a_tp", "use_s1_b_tp", "use_s2_tp", "use_s2_b_tp"),
                    True,
                )
            )
            if use_tp:
                if profit_ratio < tp_pct:
                    parts.append(f"tp:pnl={profit_ratio:.2f}%<{tp_pct:g}%")
                else:
                    parts.append(f"tp:would_hit({profit_ratio:.2f}%)")

        # Flash
        use_flash = bool(
            s.get(
                _exit_key(
                    "use_s1_a_flash_exit",
                    "use_s1_b_flash_exit",
                    "use_s2_flash_exit",
                    "use_s2_b_flash_exit",
                ),
                True,
            )
        )
        try:
            f_pct = float(
                s.get(
                    _exit_key(
                        "s1_a_flash_exit_pct",
                        "s1_b_flash_exit_pct",
                        "s2_flash_exit_pct",
                        "s2_b_flash_exit_pct",
                    ),
                    3.0,
                )
            )
        except (TypeError, ValueError):
            f_pct = 3.0
        if not use_flash:
            parts.append("flash:off")
        elif skip_flash:
            parts.append(
                f"flash:suppressed(trail_arm peak={peak_pnl:.2f}%>=TP{tp_pct:g}%)"
            )
        elif profit_ratio < 0:
            parts.append(f"flash:skip_loss(pnl={profit_ratio:.2f}%)")
        else:
            try:
                flash_px = max_p * (1.0 - f_pct / 100.0)
                drop = ((max_p - px) / max_p * 100.0) if max_p > 0 else 0.0
                if px > flash_px:
                    parts.append(
                        f"flash:drop={drop:.2f}%<{f_pct:g}%(trig<={flash_px:.0f})"
                    )
                else:
                    parts.append(f"flash:would_hit(drop={drop:.2f}%)")
            except Exception:
                parts.append("flash:calc_error")

        # ATR SL / Trail
        sfx = _atr_suff()
        atr_col = TradingStrategy._atr_column_for_holdings(is_type_b)
        curr_atr = _gv(atr_col, 0.0)
        if curr_atr <= 0:
            curr_atr = _gv("ATR", 0.0)
        try:
            use_sl_atr = bool(
                s.get(
                    f"use_atr_stop_loss{sfx}",
                    TradingStrategy._default_use_atr_stop_loss(sfx),
                )
            )
        except Exception:
            use_sl_atr = False
        try:
            use_trail = bool(s.get(f"use_atr_trailing_stop{sfx}", True))
            use_trail = bool(
                TradingStrategy._effective_use_atr_trailing_for_stage(
                    s, _rx, stage, use_trail
                )
            )
        except Exception:
            use_trail = False
        if curr_atr <= 0:
            parts.append("atr:0")
        else:
            try:
                entry_atr = float(
                    TradingStrategy._entry_atr_for_atr_sl(pos, curr_atr)
                )
            except Exception:
                entry_atr = curr_atr
            try:
                sl_mult = float(s.get(f"atr_multiplier_sl{sfx}", 2.5))
            except (TypeError, ValueError):
                sl_mult = 2.5
            try:
                tr_mult = float(s.get(f"atr_multiplier_trail{sfx}", 4.0))
            except (TypeError, ValueError):
                tr_mult = 4.0
            if use_sl_atr:
                sl_px = avg_p - sl_mult * entry_atr
                if px > sl_px:
                    parts.append(f"atr_sl:px>{sl_px:.0f}")
                else:
                    parts.append(f"atr_sl:would_hit(<={sl_px:.0f})")
            else:
                parts.append("atr_sl:off")
            trail_armed = True
            if _trail_only and use_trail:
                trail_armed = peak_pnl >= tp_pct
            if use_trail:
                if not trail_armed:
                    parts.append(
                        f"atr_trl:not_armed(peak={peak_pnl:.2f}%<TP{tp_pct:g}%)"
                    )
                else:
                    tr_px = atr_peak - tr_mult * curr_atr
                    if px > tr_px:
                        parts.append(f"atr_trl:armed_px>{tr_px:.0f}")
                    else:
                        parts.append(f"atr_trl:would_hit(<={tr_px:.0f})")
            else:
                parts.append("atr_trl:off")

        # % SL skipped note
        try:
            use_pct_sl = bool(
                s.get(
                    _exit_key(
                        "use_s1_a_stop_loss",
                        "use_s1_b_stop_loss",
                        "use_s2_stop_loss",
                        "use_s2_b_stop_loss",
                    ),
                    True,
                )
            )
            if use_pct_sl and use_sl_atr and curr_atr > 0:
                parts.append("pct_sl:skipped_by_atr_sl")
            elif use_pct_sl:
                sl_pct = float(
                    s.get(
                        _exit_key(
                            "s1_a_stop_loss",
                            "s1_b_stop_loss",
                            "s2_stop_loss",
                            "s2_b_stop_loss",
                        ),
                        3.0,
                    )
                )
                if profit_ratio > -sl_pct:
                    parts.append(f"pct_sl:pnl={profit_ratio:.2f}%>{-sl_pct:g}%")
        except Exception:
            pass

        typ = "B" if is_type_b else "A"
        head = f"exit:S{stage}{typ} pnl={profit_ratio:.2f}%"
        if not parts:
            return head + "|no_detail"
        return head + "|" + ";".join(parts[:8])

    @staticmethod
    def s2_delay_elapsed_for_position(
        pos: dict,
        settings: dict,
        curr_dt: object | None = None,
        sel_track_hint: str | None = None,
    ) -> bool:
        """
        S1 진입 시각(`entry_date` 또는 `entry_time`) 기준 추매 지연 시간 충족 여부.
        Type A/B 키 선택은 `compute_s2_confluence_display_text` 와 동일.
        `use_s2_delay` / `use_s2_b_delay` 가 꺼져 있으면 True.
        """
        try:
            if not pos:
                return False
            s = settings or {}
            _is_b = TradingStrategy._holding_sig_is_type_b(pos)
            if not _is_b:
                try:
                    _is_b = "TYPE_B" in str(pos.get("reason", "")).upper()
                except Exception:
                    _is_b = False
            if not _is_b and sel_track_hint:
                try:
                    if str(sel_track_hint).strip().upper() == "B":
                        _is_b = True
                except Exception:
                    pass

            def _s2(a_key: str, b_key: str, default):
                return s.get(b_key, default) if _is_b else s.get(a_key, default)

            if not bool(_s2("use_s2_delay", "use_s2_b_delay", True)):
                return True

            min_hours = float(_s2("s2_delay_hours", "s2_b_delay_hours", 24))
            et_raw = pos.get("entry_date") or pos.get("entry_time")
            if et_raw is None:
                return False
            entry_dt = pd.to_datetime(et_raw)
            cdt = (
                pd.to_datetime(curr_dt)
                if curr_dt is not None
                else pd.Timestamp.now()
            )
            return (cdt - entry_dt).total_seconds() / 3600 >= min_hours
        except Exception:
            return False

    @staticmethod
    def compute_s2_confluence_display_text(
        pos: dict,
        settings: dict,
        indicator_row: object,
        market_trend: str = "UP",
        curr_dt: object | None = None,
        sel_track_hint: str | None = None,
        now_price: float | None = None,
    ) -> tuple[str, float]:
        """
        감시 '신호상태' 열용 S2 추매 합치도 — `_check_s2_approval` 과 동일 조건을 충족 개수/전체 개수로 요약.
        반환: (표시 문자열, met_ratio 0~1 색상용)
        """
        try:
            if not pos or indicator_row is None:
                return ("추매 —", 0.0)
            s = settings or {}

            # Type C(박스)는 추매(S2)가 없음 → A 트랙 추매 합치도로 오표시하지 않고 C 청산 기준 표시
            if TradingStrategy._holding_sig_is_type_c(pos):
                return TradingStrategy.compute_type_c_holding_display_text(
                    pos, s, indicator_row, now_price=now_price, curr_dt=curr_dt
                )

            def _row_get(key: str, default: float = 0.0) -> float:
                try:
                    if hasattr(indicator_row, "get"):
                        v = indicator_row.get(key, default)
                        return float(v) if v is not None else float(default)
                except (TypeError, ValueError):
                    return float(default)
                return float(default)

            _is_b = TradingStrategy._holding_sig_is_type_b(pos)
            if not _is_b:
                try:
                    _is_b = "TYPE_B" in str(pos.get("reason", "")).upper()
                except Exception:
                    _is_b = False
            if not _is_b and sel_track_hint:
                try:
                    if str(sel_track_hint).strip().upper() == "B":
                        _is_b = True
                except Exception:
                    pass

            if _is_b:
                if not bool(s.get("use_s2_master_b", True)):
                    return ("S2 비활성(B)", 0.0)
            else:
                if not bool(s.get("use_s2_master_a", True)):
                    return ("S2 비활성(A)", 0.0)

            def _s2(a_key: str, b_key: str, default):
                return s.get(b_key, default) if _is_b else s.get(a_key, default)

            passed = 0
            total = 0

            if _s2("use_s2_delay", "use_s2_b_delay", True):
                total += 1
                if TradingStrategy.s2_delay_elapsed_for_position(
                    pos, s, curr_dt, sel_track_hint=sel_track_hint
                ):
                    passed += 1

            cp = float(now_price) if now_price is not None and float(now_price) > 0 else float(
                _row_get("Close", 0.0)
            )

            if _s2("use_s2_ma20", "use_s2_b_ma20", True):
                total += 1
                ma20_val = _row_get("MA20", 0)
                gap = float(_s2("s2_ma20_gap", "s2_b_ma20_gap", 0.0))
                if ma20_val != 0 and cp >= ma20_val * (1 + gap / 100.0):
                    passed += 1

            if _s2("use_s2_ma5", "use_s2_b_ma5", True):
                total += 1
                ma5_val = _row_get("MA5", 0)
                gap = float(_s2("s2_ma5_gap", "s2_b_ma5_gap", -0.5))
                if cp >= ma5_val * (1 + gap / 100.0):
                    passed += 1

            if _s2("use_s2_vol_surge", "use_s2_b_vol_surge", True):
                total += 1
                vol_now = _row_get("Volume", 0)
                if _is_b:
                    vol_target = float(
                        _row_get("Prev_Day_Vol_S2_B", 0)
                        or _row_get("Prev_Day_Vol_B", 0)
                        or _row_get("Vol_MA20", 0)
                        or _row_get("Volume", 0)
                    )
                else:
                    vol_target = float(
                        _row_get("Prev_Day_Vol_S2_A", 0)
                        or _row_get("Prev_Day_Vol", 0)
                        or _row_get("Vol_MA20", 0)
                        or _row_get("Volume", 0)
                    )
                ratio = float(_s2("s2_vol_ratio", "s2_b_vol_ratio", 1.5))
                if vol_now >= vol_target * ratio:
                    passed += 1

            use_rsi = bool(_s2("use_s2_rsi_sig", "use_s2_b_rsi_sig", True))
            use_macd = bool(_s2("use_s2_macd_gc", "use_s2_b_macd_gc", True))
            rsi_sig = _row_get("RSI_Sig", 50)
            rsi_val = _row_get("RSI", 0)
            rsi_prev = _row_get("Prev_RSI", rsi_val)
            rsi_gap = float(_s2("s2_rsi_gap", "s2_b_rsi_gap", 0.0))
            rsi_ok = rsi_val >= rsi_sig * (1 + rsi_gap / 100.0) and rsi_val > rsi_prev

            macd_val = _row_get("MACD", 0)
            macd_sig_val = _row_get("MACD_Sig", 0)
            osc_val = _row_get("OSC", 1)
            osc_prev = _row_get("Prev_OSC", osc_val)
            macd_gap = float(_s2("s2_macd_gap", "s2_b_macd_gap", 0.0))
            macd_ok = (
                macd_val >= macd_sig_val + macd_gap
                and osc_val > 0
                and osc_val > osc_prev
            )

            if use_rsi or use_macd:
                total += 1
                if use_rsi and use_macd:
                    if rsi_ok or macd_ok:
                        passed += 1
                elif use_rsi and rsi_ok:
                    passed += 1
                elif use_macd and macd_ok:
                    passed += 1

            if _s2("use_s2_profit", "use_s2_b_profit", True):
                total += 1
                profit_limit = float(_s2("s2_profit_limit", "s2_b_profit_limit", 1.0))
                avg_price = float(pos.get("avg_price", pos.get("avg_p", cp)) or 0)
                if avg_price > 0:
                    current_profit_pct = (cp / avg_price - 1) * 100
                    if current_profit_pct >= profit_limit:
                        passed += 1

            if total <= 0:
                return ("추매 조건 없음", 0.0)

            tr_lab = "B" if _is_b else "A"
            ratio_clr = min(1.0, max(0.0, float(passed) / float(total)))
            return (f"추매({tr_lab}) {passed}/{total}", ratio_clr)
        except Exception:
            return ("추매 —", 0.0)

    @staticmethod
    def compute_type_c_holding_display_text(
        pos: dict,
        settings: dict,
        indicator_row: object = None,
        now_price: float | None = None,
        curr_dt: object | None = None,
    ) -> tuple[str, float]:
        """
        Type C(박스) 보유 종목의 감시 '신호상태' 열 문자열.
        `_evaluate_type_c_exit` 와 같은 설정 키로 목표가·손절가·보유일·종가 이탈 횟수를 요약.
        반환: (표시 문자열, 손절가→목표가 구간 내 현재가 위치 0~1)
        """
        try:
            if not pos:
                return ("박스(C) —", 0.0)
            s = settings or {}

            def _row_get(key: str, default: float = 0.0) -> float:
                try:
                    if indicator_row is not None and hasattr(indicator_row, "get"):
                        v = indicator_row.get(key, default)
                        return float(v) if v is not None else float(default)
                except (TypeError, ValueError):
                    pass
                return float(default)

            def _setting_float(key: str, default: float) -> float:
                try:
                    return float(s.get(key, default))
                except (TypeError, ValueError):
                    return float(default)

            cp = 0.0
            try:
                if now_price is not None:
                    cp = float(now_price)
            except (TypeError, ValueError):
                cp = 0.0
            if cp <= 0:
                cp = _row_get("Close", 0.0)

            try:
                lower = float(pos.get("box_lower", 0) or 0)
                upper = float(pos.get("box_upper", 0) or 0)
                entry_atr = float(pos.get("box_atr_entry", 0) or 0)
            except (TypeError, ValueError):
                lower, upper, entry_atr = 0.0, 0.0, 0.0
            if entry_atr <= 0:
                entry_atr = _row_get("ATR_C", _row_get("ATR", 0.0))

            # 보유일: 진입일 포함 평일 수(청산 판정의 bars_df 미제공 폴백과 동일)
            max_days = max(1, int(_setting_float("type_c_max_holding_days", 5)))
            held_days = 0
            try:
                et_raw = pos.get("entry_date") or pos.get("entry_time")
                if et_raw is not None:
                    entry_ts = pd.Timestamp(et_raw)
                    eval_ts = (
                        pd.Timestamp(curr_dt) if curr_dt is not None else pd.Timestamp.now()
                    )
                    held_days = int(
                        np.busday_count(
                            entry_ts.date(),
                            (eval_ts + pd.Timedelta(days=1)).date(),
                        )
                    )
            except Exception:
                held_days = 0
            day_txt = f" · {held_days}/{max_days}일" if held_days > 0 else ""

            # 박스 메타데이터 유실 시 청산 로직은 비상 %손절만 유지
            if not (upper > lower > 0 and entry_atr > 0):
                emergency_sl = abs(_setting_float("type_c_missing_box_stop_loss_pct", 3.0))
                return (f"박스(C) 메타없음 · 비상SL -{emergency_sl:g}%{day_txt}", 0.0)

            stop_atr = max(0.0, _setting_float("type_c_stop_atr_tolerance", 0.5))
            target_buffer = max(0.0, _setting_float("type_c_target_atr_buffer", 0.2))
            stop_price = lower - stop_atr * entry_atr
            target_price = upper - target_buffer * entry_atr

            txt = f"박스(C) 목표 {target_price:,.0f} · 손절 {stop_price:,.0f}{day_txt}"
            try:
                bd_cnt = int(pos.get("box_breakdown_closes", 0) or 0)
            except (TypeError, ValueError):
                bd_cnt = 0
            if bd_cnt > 0:
                bd_limit = max(1, int(_setting_float("type_c_breakdown_confirm_bars", 2)))
                txt += f" · 이탈 {bd_cnt}/{bd_limit}"

            ratio = 0.0
            span = target_price - stop_price
            if cp > 0 and span > 0:
                ratio = min(1.0, max(0.0, (cp - stop_price) / span))
            return (txt, ratio)
        except Exception:
            return ("박스(C) —", 0.0)

    @staticmethod
    def _holding_sig_is_type_b(pos: dict) -> bool:
        """보유 dict의 Type B 판별(sig_type / Sig_Type, NaN·누락은 False)."""
        try:
            raw = pos.get("sig_type", pos.get("Sig_Type"))
            if raw is None:
                return False
            if pd.isna(raw):
                return False
            return "TYPE_B" in str(raw).upper()
        except Exception:
            return False

    @staticmethod
    def _holding_sig_is_type_c(pos: dict) -> bool:
        """보유 dict의 Type C 판별(sig_type·reason 모두 확인)."""
        try:
            raw = pos.get("sig_type", pos.get("Sig_Type"))
            if raw is not None and not pd.isna(raw):
                if "TYPE_C" in str(raw).upper():
                    return True
            return "TYPE_C" in str(pos.get("reason", "") or "").upper()
        except Exception:
            return False

    @staticmethod
    def _s1_simul_balance_tie_mode(settings: Optional[dict]) -> str:
        """
        동시신호 봉의 보유 A/B 동수 타이브레이크 적용 방식.

        - ``always``    : 동수(0:0 포함)면 TYPE_B 로 덮기
        - ``force_a``   : 동수면 TYPE_A 로 덮기 (B 차단·A 허용)
        - ``block``     : 동수면 S1 매수 전면 차단
        - ``empty_only``: 보유 0:0 일 때만 TYPE_B
        - ``when_held`` : 보유가 1건 이상일 때만 동수→TYPE_B
        - ``off``       : 타이브레이크 미적용(초안 유지)
        """
        try:
            v = str((settings or {}).get("s1_simul_balance_tie_mode", "always"))
            v = v.strip().lower()
            if v in ("always", "force_a", "block", "empty_only", "when_held", "off"):
                return v
        except Exception:
            pass
        return "always"

    @staticmethod
    def _count_holdings_ab(holdings: Optional[dict]) -> tuple:
        """보유 포지션의 TYPE_A / TYPE_B 개수."""
        try:
            n_a = sum(
                1
                for h in (holdings or {}).values()
                if "TYPE_B" not in str(h.get("sig_type", "") or "").upper()
                and "TYPE_C" not in str(h.get("sig_type", "") or "").upper()
            )
            n_b = sum(
                1
                for h in (holdings or {}).values()
                if "TYPE_B" in str(h.get("sig_type", "") or "").upper()
            )
            return int(n_a), int(n_b)
        except Exception:
            return 0, 0

    @staticmethod
    def _simul_balance_equal_applies(mode: str, n_a: int, n_b: int) -> bool:
        """동수 타이브레이크/차단 규칙이 이 보유 상태에서 적용되는지."""
        try:
            if n_a != n_b:
                return False
            m = str(mode or "always").strip().lower()
            if m == "off":
                return False
            if m == "when_held" and (n_a + n_b) <= 0:
                return False
            if m == "empty_only" and (n_a + n_b) > 0:
                return False
            # always / force_a / block / (when_held·empty_only 통과분)
            return True
        except Exception:
            return False

    @staticmethod
    def _s1_sig_type_simultaneous_balance_tie_b(
        settings: dict, sig_type: str, simultaneous_ab: bool, holdings: dict
    ) -> str:
        """
        A·B 동시 신호이고 보유 TYPE_A / TYPE_B 가 동수이면 타이브레이크 적용.

        ``always`` → TYPE_B, ``force_a`` → TYPE_A.
        ``block`` 은 타입을 바꾸지 않고 ``simultaneous_equal_holdings_blocked`` 에서 매수를 막는다.
        """
        try:
            if not bool(simultaneous_ab):
                return sig_type
            s = settings or {}
            use_a = bool(s.get("use_type_a", True))
            use_b = bool(s.get("use_type_b", False))
            if not (use_a and use_b):
                return sig_type
            mode = TradingStrategy._s1_simul_balance_tie_mode(s)
            if mode in ("off", "block"):
                return sig_type
            n_a, n_b = TradingStrategy._count_holdings_ab(holdings)
            if not TradingStrategy._simul_balance_equal_applies(mode, n_a, n_b):
                return sig_type
            if mode == "force_a":
                return "TYPE_A"
            # always / empty_only / when_held (적용 통과분)
            return "TYPE_B"
        except Exception:
            pass
        return sig_type

    @staticmethod
    def simultaneous_equal_holdings_blocked(
        settings: Optional[dict],
        row: Any,
        holdings: Optional[dict],
    ) -> bool:
        """
        동시신호 + 보유 A/B 동수일 때 S1 매수 전면 차단 여부.

        ``s1_simul_balance_tie_mode=block`` 일 때만 True.
        """
        try:
            if not TradingStrategy._row_is_simultaneous_ab(row):
                return False
        except Exception:
            return False
        try:
            mode = TradingStrategy._s1_simul_balance_tie_mode(settings)
            if mode != "block":
                return False
            n_a, n_b = TradingStrategy._count_holdings_ab(holdings)
            return TradingStrategy._simul_balance_equal_applies(mode, n_a, n_b)
        except Exception:
            return False

    @staticmethod
    def _row_is_simultaneous_ab(row: Any) -> bool:
        """지표 행이 동시신호(A+B) 봉인지."""
        try:
            if row is None or not hasattr(row, "get"):
                return False
            return bool(row.get("S1_Simultaneous_AB", False))
        except Exception:
            return False

    @staticmethod
    def simultaneous_buy_blocked(
        settings: Optional[dict], row: Any, sig_type: Any = None
    ) -> bool:
        """
        동시신호(A+B) 봉의 S1 매수 차단 여부.

        - ``same_bar_block_simul``: TYPE_A·B 전부 차단
        - ``same_bar_block_type_a``: 확정이 TYPE_A 일 때만 차단
        """
        try:
            if not TradingStrategy._row_is_simultaneous_ab(row):
                return False
        except Exception:
            return False
        try:
            if bool((settings or {}).get("same_bar_block_simul", False)):
                return True
        except Exception:
            pass
        try:
            if not bool((settings or {}).get("same_bar_block_type_a", False)):
                return False
        except Exception:
            return False
        try:
            return str(sig_type or "").strip().upper() == "TYPE_A"
        except Exception:
            return False

    @staticmethod
    def simultaneous_type_a_blocked(
        settings: Optional[dict], row: Any, sig_type: Any
    ) -> bool:
        """하위 호환 — ``simultaneous_buy_blocked`` 위임."""
        return TradingStrategy.simultaneous_buy_blocked(settings, row, sig_type)

    @staticmethod
    def track_signal_match_allows(
        settings: Optional[dict], sig_type: Any, code_track: Any
    ) -> bool:
        """
        Track A/B/C 선정과 S1 Sig_Type 교차 허용 여부.

        순수 Track C는 TYPE_C 구현 전 A/B 신호를 항상 차단합니다. 이 안전장치는
        ``enforce_track_signal_match`` 설정을 꺼도 유지됩니다.
        """
        try:
            st = str(sig_type or "").strip().upper()
            ct = str(code_track or "A").strip().upper() or "A"
        except Exception:
            return False

        # TYPE_C 미구현 단계의 하드 가드: C 관찰 종목이 기존 전략으로 매수되는 것을 방지합니다.
        if ct == "C" and st in ("TYPE_A", "TYPE_B"):
            return False
        if st == "TYPE_C":
            try:
                return "C" in ct and bool((settings or {}).get("use_type_c", False))
            except Exception:
                return False

        try:
            if not bool((settings or {}).get("enforce_track_signal_match", True)):
                return True
        except Exception:
            pass
        try:
            if st == "TYPE_A":
                return "A" in ct
            if st == "TYPE_B":
                return "B" in ct
            return True
        except Exception:
            return False

    @staticmethod
    def _same_bar_prefer_sel_track_enabled(settings: Optional[dict]) -> bool:
        """동시신호 봉에서 선정 트랙(A/B)에 맞는 신호를 우선 확정할지."""
        try:
            return bool(
                (settings or {}).get("same_bar_sig_prefer_sel_track", False)
            )
        except Exception:
            return False

    @staticmethod
    def _same_bar_allow_track_b_enabled(settings: Optional[dict]) -> bool:
        """
        동시신호 봉에서 선정 트랙 B 종목만 TYPE_B 로 확정할지.

        ``prefer_a`` 초안이면 비동수 보유 시 트랙 B 가 TYPE_A 로 확정되어
        교차검증에 막힌다. 이 플래그는 그 경우만 TYPE_B 로 살려 매수를 허용한다.
        트랙 A·동수 타이브레이크 동작은 바꾸지 않는다.
        """
        try:
            return bool((settings or {}).get("same_bar_sig_allow_track_b", False))
        except Exception:
            return False

    @staticmethod
    def effective_s1_sig_type_for_buy(
        settings: dict, row, holdings: dict, code_track: Any = None
    ) -> str:
        """15분 행·실시간 last_sig 등에서 Sig_Type 확정.

        동시신호(S1_Simultaneous_AB)면 현재 same_bar 우선순위
        (prefer_a / prefer_b / prefer_track)로 TYPE_A·B를 재결정한 뒤,
        보유 동수 타이브레이크(B 우선)를 적용한다.

        ``same_bar_sig_prefer_sel_track`` 이 켜져 있고 ``code_track`` 이 A/B 단일이면,
        동시신호 봉에 한해 **그 트랙의 신호로 확정**한다. 동시신호 봉은 A·B가 모두
        성립한 것이므로, 우선순위 규칙 때문에 반대편 타입으로 확정되어
        ``track_signal_match_allows`` 에서 폐기되는 기회 손실을 막는다.

        ``same_bar_sig_allow_track_b`` 는 트랙 B 만 TYPE_B 로 확정한다
        (트랙 A 의 prefer_a·동수 차단은 유지).
        """
        try:
            if row is None:
                return "TYPE_B"
            if not hasattr(row, "get"):
                return "TYPE_B"
            base = str(row.get("Sig_Type", "TYPE_B"))
            sim = bool(row.get("S1_Simultaneous_AB", False))
            s = settings or {}
            use_a = bool(s.get("use_type_a", True))
            use_b = bool(s.get("use_type_b", False))
            if sim and use_a and use_b:
                try:
                    if TradingStrategy._same_bar_prefer_sel_track_enabled(s):
                        ct = str(code_track or "").strip().upper()
                        if ct == "A":
                            return "TYPE_A"
                        if ct == "B":
                            return "TYPE_B"
                except Exception:
                    pass
                try:
                    if TradingStrategy._same_bar_allow_track_b_enabled(s):
                        ct = str(code_track or "").strip().upper()
                        if ct == "B":
                            return "TYPE_B"
                except Exception:
                    pass
                try:
                    from strategy.signals import _normalize_same_bar_priority

                    pri = _normalize_same_bar_priority(s)
                except Exception:
                    pri = "prefer_a"
                if pri == "prefer_b":
                    base = "TYPE_B"
                elif pri == "prefer_track":
                    # 선정 트랙과 일치(A→TYPE_A, B→TYPE_B). AB/미지정이면 A.
                    try:
                        ct = str(code_track or "").strip().upper()
                    except Exception:
                        ct = ""
                    if ct == "B":
                        base = "TYPE_B"
                    else:
                        base = "TYPE_A"
                else:
                    # prefer_a (및 구 confluence 정규화 결과)
                    base = "TYPE_A"
            out = TradingStrategy._s1_sig_type_simultaneous_balance_tie_b(
                settings, base, sim, holdings
            )
            u = str(out or "").strip().upper()
            if u == "TYPE_A":
                return "TYPE_A"
            if u == "TYPE_B":
                return "TYPE_B"
            if u == "TYPE_C":
                return "TYPE_C"
            return "TYPE_B"
        except Exception:
            return "TYPE_B"

    @staticmethod
    def _entry_atr_for_atr_sl(pos: dict, curr_atr: float) -> float:
        """
        ATR_SL 손절선용 진입 ATR.
        pos.get('atr_at_entry', curr_atr)는 키가 있고 값이 0이면 0을 그대로 반환해
        (sl_mult * 0)으로 배수가 무력화될 수 있음 → 유효할 때만 저장값 사용, 아니면 curr_atr.
        """
        try:
            c = float(curr_atr)
            if pd.isna(c) or c <= 0:
                return 0.0
        except (TypeError, ValueError):
            return 0.0
        try:
            if "atr_at_entry" not in pos:
                return c
            raw = pos["atr_at_entry"]
            if raw is None or pd.isna(raw):
                return c
            e = float(raw)
            if pd.isna(e) or e <= 0:
                return c
            return e
        except Exception:
            return c

    @staticmethod
    def _default_use_atr_stop_loss(sfx: str) -> bool:
        """
        use_atr_stop_loss{접미사} 키가 없을 때 기본값.
        S2(_s2, _s2_b)만 True, S1 A/B(_a, _b)는 False — ATR SL은 설정에서 명시할 때만.
        """
        try:
            return str(sfx) in ("_s2", "_s2_b")
        except Exception:
            return False

    @staticmethod
    def _exit_track_risk_bundle(settings: dict | None, is_type_b: bool) -> dict:
        """
        갭하락·개장유예·ATR(전역) — Type A/B 완전 분리.
        '목표 익절% 이후만 트레일'은 단계별 키로 _use_atr_trail_only_after_tp_for_stage에서 처리.
        구버전 공통 키(use_gap_down_exit 등)는 Type A 폴백으로만 사용.
        """
        s = settings or {}
        if is_type_b:
            return {
                "use_gap_down_exit": bool(s.get("use_gap_down_exit_b", True)),
                "gap_down_limit": float(s.get("gap_down_limit_b", s.get("gap_down_limit", -5.0))),
                "use_gap_grace": bool(s.get("use_gap_grace_b", True)),
                "gap_grace_mins": int(float(s.get("gap_grace_mins_b", s.get("gap_grace_mins", 15)))),
                "use_atr_exit": bool(s.get("use_atr_exit_b", s.get("use_atr_exit", False))),
            }
        return {
            "use_gap_down_exit": bool(s.get("use_gap_down_exit_a", s.get("use_gap_down_exit", True))),
            "gap_down_limit": float(s.get("gap_down_limit_a", s.get("gap_down_limit", -5.0))),
            "use_gap_grace": bool(s.get("use_gap_grace_a", s.get("use_gap_grace", True))),
            "gap_grace_mins": int(float(s.get("gap_grace_mins_a", s.get("gap_grace_mins", 15)))),
            "use_atr_exit": bool(s.get("use_atr_exit_a", s.get("use_atr_exit", False))),
        }

    @staticmethod
    def _effective_use_atr_trailing_for_stage(
        _settings: dict,
        _rx: dict,
        stage: Any,
        use_trailing_stop_toggle: bool,
    ) -> bool:
        """
        ATR 트레일(표시·청산) 적용 여부.

        설정 UI: S1에서「ATR 트레일링」토글은 ATR 분석 주기와 함께 설명되며,
        **S1 구간**에서는 트랙별 `use_atr_exit_a` / `use_atr_exit_b`(마스터)가 꺼져 있으면
        트레일을 쓰지 않는 것이 맞다. 기본값 `use_atr_trailing_stop_*` True 만으로 TRL 이
        표시되던 버그를 막는다.

        S2(`_s2` / `_s2_b`)는 추매 전용 토글·배수로 처리하고, 여기서는 단계만 구분한다.
        """
        try:
            st = int(float(stage))
        except (TypeError, ValueError):
            st = 1
        try:
            if st != 1:
                return bool(use_trailing_stop_toggle)
            if not bool((_rx or {}).get("use_atr_exit", False)):
                return False
        except Exception:
            try:
                return bool(use_trailing_stop_toggle)
            except Exception:
                return False
        return bool(use_trailing_stop_toggle)

    @staticmethod
    def _use_atr_trail_only_after_tp_for_stage(
        settings: dict | None, is_type_b: bool, stage: int | float
    ) -> bool:
        """
        S1/S2 단계별 '목표 익절% 이후에만 ATR 트레일' 여부.
        신규 키가 없으면 구버전 트랙 단일 키(use_atr_trail_only_after_tp_a/b)로 폴백.
        """
        s = settings or {}
        try:
            st = int(stage)
        except (TypeError, ValueError):
            st = 1
        is_s2 = st == 2
        try:
            if is_type_b:
                legacy = bool(s.get("use_atr_trail_only_after_tp_b", s.get("use_atr_trail_only_after_tp", False)))
                k = "use_atr_trail_only_after_tp_s2_b" if is_s2 else "use_atr_trail_only_after_tp_s1_b"
                return bool(s.get(k, legacy))
            legacy = bool(s.get("use_atr_trail_only_after_tp_a", s.get("use_atr_trail_only_after_tp", False)))
            k = "use_atr_trail_only_after_tp_s2_a" if is_s2 else "use_atr_trail_only_after_tp_s1_a"
            return bool(s.get(k, legacy))
        except Exception:
            return False

    @staticmethod
    def _coerce_position_stage(pos: dict | None) -> int:
        """
        entry_n·stage·BUY_S2 이벤트 기준 실효 단계(1 또는 2+).
        portfolio_manager.position_effective_stage 와 동일 — DB/동기화 pos 에
        stage 가 문자열이거나 entry_n 만 갱신된 경우에도 S2 키를 쓰도록 한다.
        """
        if not isinstance(pos, dict):
            return 1
        try:
            st = max(
                int(float(pos.get("entry_n", 0) or 0)),
                int(float(pos.get("stage", 1) or 1)),
            )
            if st >= 2:
                return st
            for ev in pos.get("events") or []:
                if isinstance(ev, dict) and str(ev.get("action", "")).upper() == "BUY_S2":
                    return 2
        except (TypeError, ValueError):
            pass
        return 1

    @staticmethod
    def _atr_column_for_holdings(is_type_b: bool) -> str:
        """보유 종목 트랙에 맞는 ATR 시리즈 컬럼명."""
        return "ATR_B" if is_type_b else "ATR_A"

    @staticmethod
    def _approx_atr_from_true_range(bars_df: Any) -> float:
        """
        ATR_A/B가 비어 있거나 전부 NaN일 때 마지막 True Range로 표시용 근사.
        (워밍업으로 봉은 충분한데 마지막 행만 NaN인 경우·지표 재계산 예외 폴백 등)
        """
        try:
            if bars_df is None or getattr(bars_df, "empty", True):
                return 0.0
            if "High" not in bars_df.columns or "Low" not in bars_df.columns:
                return 0.0
            df = bars_df
            h = pd.to_numeric(df["High"], errors="coerce")
            lo = pd.to_numeric(df["Low"], errors="coerce")
            c = pd.to_numeric(df["Close"], errors="coerce")
            tr1 = (h - lo).abs()
            tr2 = (h - c.shift(1)).abs()
            tr3 = (lo - c.shift(1)).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            last = tr.dropna()
            if last.empty:
                return 0.0
            v = float(last.iloc[-1])
            return v if np.isfinite(v) and v > 0 else 0.0
        except Exception:
            return 0.0

    @staticmethod
    def _last_finite_atr_from_bars_df(
        bars_df: Any, settings: dict, atr_col: str
    ) -> float:
        """
        마지막 행이 NaN이어도 직전 봉들에서 마지막 유효 ATR을 사용.
        실시간 스트림으로 막 닫힌 봉은 ATR이 아직 NaN인 경우가 많음.
        지표 ATR 열이 비었으면 True Range 근사로 폴백.
        """
        try:
            if bars_df is None or getattr(bars_df, "empty", True):
                return 0.0
            df_i = TradingStrategy.calculate_indicators(bars_df, settings or {})

            def _try_col(col: str) -> float:
                if col not in df_i.columns:
                    return 0.0
                ser = pd.to_numeric(df_i[col], errors="coerce").dropna()
                if ser.empty:
                    return 0.0
                v = float(ser.iloc[-1])
                return v if np.isfinite(v) and v > 0 else 0.0

            v = _try_col(atr_col)
            if v > 0:
                return v
            alt = "ATR_A" if atr_col == "ATR_B" else "ATR_B"
            v = _try_col(alt)
            if v > 0:
                return v
        except Exception:
            pass
        return TradingStrategy._approx_atr_from_true_range(bars_df)

    @staticmethod
    def _peak_anchor_timestamp_for_bars(pos: Optional[dict]) -> Optional[pd.Timestamp]:
        """
        봉 High 로 max_p 를 보강할 때 사용할 기준 시각(진입 또는 마지막 매수 체결).
        - S1: entry_time 등
        - S2: DB·포지션은 entry_time 이 S1 과 동일하게 남는 경우가 많음 → events 의 마지막 BUY 시각 우선
        """
        if not pos:
            return None
        raw: Any = None
        try:
            st = int(pos.get("stage", pos.get("entry_n", 1)) or 1)
        except (TypeError, ValueError):
            st = 1
        if st >= 2:
            try:
                ev = pos.get("events") or []
                if isinstance(ev, list):
                    for e in reversed(ev):
                        if not isinstance(e, dict):
                            continue
                        act = str(e.get("action") or "").upper()
                        if "BUY" in act:
                            raw = e.get("time") or e.get("timestamp")
                            break
            except Exception:
                raw = None
        if raw is None or raw == "":
            raw = (
                pos.get("entry_time")
                or pos.get("entry_datetime")
                or pos.get("entry_date")
            )
        if raw is None or raw == "":
            return None
        try:
            ts = pd.Timestamp(raw)
        except Exception:
            return None
        try:
            if pd.isna(ts):
                return None
        except Exception:
            return None
        try:
            from zoneinfo import ZoneInfo

            seoul = ZoneInfo("Asia/Seoul")
            if ts.tzinfo is None:
                ts = ts.tz_localize(
                    seoul, ambiguous="NaT", nonexistent="shift_forward"
                )
            else:
                ts = ts.tz_convert(seoul)
        except Exception:
            pass
        try:
            if pd.isna(ts):
                return None
        except Exception:
            pass
        return ts

    @staticmethod
    def _peak_high_from_bars_df(
        bars_df: Any,
        since: Optional[Any] = None,
        bar_minutes: int = 15,
        price_col: str = "High",
    ) -> Optional[float]:
        """
        15분(등) 봉 가격열(기본 High)로 Flash/ATR 고점을 복원할 때 사용한다.

        `since`(진입·추매 시각)가 있으면 **해당 시각 이전에 시작된 봉은 제외**하고,
        진입 이후 **전 영업일** 봉을 포함한다.
        (과거: 당일 봉만 써서 다일 보유 Flash가 BT와 어긋남 — 재시작 복원용 당일 제한 제거)

        인덱스는 `StreamProcessor` 기준 **봉 종료(슬롯) 시각**이므로
        bar_start = index - bar_minutes 로 시작 시각을 복원한다.
        price_col=\"Close\" 이면 종가 고점(옵션3)용.
        """
        try:
            if bars_df is None or getattr(bars_df, "empty", True):
                return None
            col = str(price_col or "High")
            if col not in bars_df.columns:
                return None
            hi = pd.to_numeric(bars_df[col], errors="coerce")
            if hi.isna().all():
                return None
            idx = bars_df.index
            if isinstance(idx, pd.DatetimeIndex) and len(idx) == len(bars_df):
                try:
                    from zoneinfo import ZoneInfo

                    seoul = ZoneInfo("Asia/Seoul")

                    since_ts: Optional[pd.Timestamp] = None
                    if since is not None:
                        try:
                            since_ts = pd.Timestamp(since)
                            if since_ts.tzinfo is None:
                                since_ts = since_ts.tz_localize(
                                    seoul,
                                    ambiguous="NaT",
                                    nonexistent="shift_forward",
                                )
                            else:
                                since_ts = since_ts.tz_convert(seoul)
                            if pd.isna(since_ts):
                                since_ts = None
                        except Exception:
                            since_ts = None

                    try:
                        step = pd.Timedelta(minutes=max(1, int(bar_minutes)))
                    except (TypeError, ValueError):
                        step = pd.Timedelta(minutes=15)

                    post_entry: list[bool] = []
                    for ts in idx:
                        try:
                            t_end = pd.Timestamp(ts)
                            if t_end.tzinfo is None:
                                t_end = t_end.tz_localize(
                                    seoul, ambiguous="NaT", nonexistent="shift_forward"
                                )
                            else:
                                t_end = t_end.tz_convert(seoul)
                            if since_ts is not None:
                                t_start = t_end - step
                                post_entry.append(bool(t_start >= since_ts))
                            else:
                                post_entry.append(True)
                        except Exception:
                            post_entry.append(True)
                    arr = hi.to_numpy(dtype=float, copy=False)
                    m_post = np.array(post_entry, dtype=bool)
                    picked = arr[m_post & ~np.isnan(arr)]
                    if picked.size > 0:
                        return float(np.max(picked))
                except Exception:
                    pass
            # since 가 있는데 인덱스로 필터할 수 없으면(레거시 DF 등) 전체 max 사용 금지
            if since is not None:
                return None
            tail = hi.dropna().tail(96)
            if tail.empty:
                return None
            return float(tail.max())
        except Exception:
            return None

    @staticmethod
    def _peak_close_from_bars_df(
        bars_df: Any,
        since: Optional[Any] = None,
        bar_minutes: int = 15,
    ) -> Optional[float]:
        """진입 이후 봉 Close 상한(Flash 종가 고점 모드)."""
        return TradingStrategy._peak_high_from_bars_df(
            bars_df, since=since, bar_minutes=bar_minutes, price_col="Close"
        )

    @staticmethod
    def _peak_price_from_position(
        pos: dict,
        now_price: float,
        bars_peak_high: Optional[float] = None,
    ) -> float:
        """
        Flash·ATR 트레일 무장(목표% 대비 고점) 등에 쓰는 누적 최고가.
        계좌/DB 동기화 포지션은 max_p·max_price 키가 없을 수 있음 — 이 경우에도
        evaluate_position 매 틱 pos 에 써 넣지 않으면 고가가 유지되지 않아
        'TRL 무장전'이 잘못 표시된다. max_now(구 스키마)는 동일 의미로 병합.
        bars_peak_high: `_peak_high_from_bars_df(..., since=진입시각)`로
        매수(·추매) 이후에 **시작된 봉**의 High/Close 상한(실시간·다일 포함).
        저장된 max_p 계열과 병합해 봉 복원이 낮아도 기존 고점을 잃지 않는다.
        """
        try:
            nw = float(now_price)
        except (TypeError, ValueError):
            nw = 0.0
        if nw < 0:
            nw = 0.0
        bests: list = [nw] if nw > 0 else []
        if bars_peak_high is not None:
            try:
                bh = float(bars_peak_high)
                if bh > 0:
                    bests.append(bh)
            except (TypeError, ValueError):
                pass
        for k in ("max_p", "max_price", "max_now", "max_p_close", "max_p_high"):
            try:
                v = (pos or {}).get(k)
                if v is None:
                    continue
                try:
                    if pd.isna(v):
                        continue
                except Exception:
                    pass
                fv = float(v)
                if fv > 0:
                    bests.append(fv)
            except (TypeError, ValueError):
                continue
        if not bests:
            return nw
        return float(max(bests))

    @staticmethod
    def _cached_bars_peak_high(
        pos: dict, bars_df: Any, price_col: str = "High"
    ) -> Optional[float]:
        """
        진입·추매 앵커 이후 ``bars_df`` 가격열 상한(Flash/ATR 고점 보강).

        ``evaluate_position`` 과 ATR 표시가 동일한 캐시를 재사용해
        봉 전체 스캔을 줄입니다. price_col=\"Close\" 이면 종가 고점 캐시.
        """
        try:
            if bars_df is None or getattr(bars_df, "empty", True):
                return None
            col = str(price_col or "High")
            if col not in bars_df.columns:
                return None
            anchor = TradingStrategy._peak_anchor_timestamp_for_bars(pos)
            try:
                lk = int(len(bars_df))
                li = bars_df.index[-1]
                last_px = float(
                    pd.to_numeric(bars_df[col], errors="coerce").iloc[-1]
                )
            except Exception:
                lk, li, last_px = -1, None, float("nan")
            try:
                ck = (col, lk, li, last_px, repr(anchor))
            except Exception:
                ck = (col, lk, None, last_px, "")
            cache_key = (
                "_rt_bars_peak_cache" if col == "High" else "_rt_bars_peak_close_cache"
            )
            cached = (pos or {}).get(cache_key)
            if isinstance(cached, tuple) and len(cached) == 2 and cached[0] == ck:
                return cached[1]
            peak = TradingStrategy._peak_high_from_bars_df(
                bars_df, since=anchor, price_col=col
            )
            try:
                if isinstance(pos, dict):
                    pos[cache_key] = (ck, peak)
            except Exception:
                pass
            return peak
        except Exception:
            return None

    @staticmethod
    def compute_atr_exit_display_text(
        pos: dict,
        settings: dict,
        indicator_row: object,
        now_price: float,
        bars_df: Any = None,
        sel_track_hint: Optional[str] = None,
    ) -> str:
        """
        감시 화면·일일 리포트용 ATR 손절·트레일 기준가 문자열.
        evaluate_position 의 ATR 구간과 동일 분기(표시만, pos/지표 비변경).
        bars_df: 있으면 진입 이후 봉으로 재시작 후 고가 복원(다일 포함).
        sel_track_hint: 감시 트리 sel_track 이 \"B\"일 때만 Type B ATR/TP 키로 표시 보정(계좌 동기화만 한 포지션 등).
        """
        try:
            if not pos or float(now_price) <= 0:
                return "—"
            s = settings or {}
            stage = TradingStrategy._coerce_position_stage(pos)
            is_type_b = TradingStrategy._holding_sig_is_type_b(pos)
            if not is_type_b:
                try:
                    is_type_b = "TYPE_B" in str(pos.get("reason", "")).upper()
                except Exception:
                    is_type_b = False
            if not is_type_b and sel_track_hint:
                try:
                    if str(sel_track_hint).strip().upper() == "B":
                        is_type_b = True
                except Exception:
                    pass
            avg_p = float(pos.get("avg_p", pos.get("avg_price", now_price)) or now_price)
            if avg_p <= 0:
                avg_p = float(now_price)
            try:
                use_flash_hi = bool(settings.get("use_flash_peak_bar_high", False))
            except Exception:
                use_flash_hi = False
            try:
                use_atr_hi = bool(settings.get("use_atr_peak_bar_high", False))
            except Exception:
                use_atr_hi = False
            # Flash 표시용 max_p (봉 복원 + 저장 고점 병합)
            if use_flash_hi:
                bh = TradingStrategy._cached_bars_peak_high(pos, bars_df)
                max_p = float(
                    TradingStrategy._peak_price_from_position(
                        pos, float(now_price), bars_peak_high=bh
                    )
                )
            else:
                bc = TradingStrategy._cached_bars_peak_high(
                    pos, bars_df, price_col="Close"
                )
                max_p = float(
                    TradingStrategy._peak_price_from_position(
                        pos, float(now_price), bars_peak_high=bc
                    )
                )
            # ATR 트레일 표시용 고점
            if use_atr_hi:
                bh2 = TradingStrategy._cached_bars_peak_high(pos, bars_df)
                atr_peak = float(
                    TradingStrategy._peak_price_from_position(
                        pos, float(now_price), bars_peak_high=bh2
                    )
                )
                try:
                    mph = float(pos.get("max_p_high", 0) or 0)
                    if mph > atr_peak:
                        atr_peak = mph
                except (TypeError, ValueError):
                    pass
            else:
                atr_peak = float(max_p)

            def _ir_get(key: str, default: float = 0.0) -> float:
                if indicator_row is None:
                    return default
                try:
                    if hasattr(indicator_row, "get"):
                        v = indicator_row.get(key, default)
                        return float(v) if v is not None else default
                except Exception:
                    pass
                return default

            _rx = TradingStrategy._exit_track_risk_bundle(s, is_type_b)
            _atr_col = TradingStrategy._atr_column_for_holdings(is_type_b)

            def _curr_atr() -> float:
                """
                마지막 봉 ATR. 주의: float('nan') or 0.0 은 NaN이 truthy라 그대로 NaN이 되어
                TRL 표시가 'TRL nan'으로 깨질 수 있음 — 유한·양수만 반환.
                """
                def _finite_pos(x: object) -> float:
                    try:
                        v = float(x)
                    except (TypeError, ValueError):
                        return 0.0
                    try:
                        if pd.isna(v) or not np.isfinite(v):
                            return 0.0
                    except Exception:
                        if not np.isfinite(v):
                            return 0.0
                    return v if v > 0 else 0.0

                a = _finite_pos(_ir_get(_atr_col, 0.0))
                if a > 0:
                    return a
                return _finite_pos(_ir_get("ATR", 0.0))

            def _exit_key(a_key: str, b_key: str, s2_a_key: str, s2_b_key: str) -> str:
                if stage >= 2:
                    return s2_b_key if is_type_b else s2_a_key
                return b_key if is_type_b else a_key

            def _atr_track_suff() -> str:
                if stage >= 2:
                    return "_s2_b" if is_type_b else "_s2"
                return "_b" if is_type_b else "_a"

            curr_atr = float(_curr_atr())
            # 막 형성된 막봉은 ATR_B/A가 NaN인 경우가 많음 → 직전 유효 봉의 ATR로 보강
            if not (curr_atr > 0) and bars_df is not None:
                fb = TradingStrategy._last_finite_atr_from_bars_df(
                    bars_df, s, _atr_col
                )
                if fb > 0:
                    curr_atr = fb
            suff = _atr_track_suff()
            use_sl_atr = bool(
                s.get(f"use_atr_stop_loss{suff}", TradingStrategy._default_use_atr_stop_loss(suff))
            )
            use_trail_atr = bool(s.get(f"use_atr_trailing_stop{suff}", True))
            use_trail_atr = TradingStrategy._effective_use_atr_trailing_for_stage(
                s, _rx, stage, use_trail_atr
            )
            run_atr_exit = bool(_rx["use_atr_exit"]) or (
                curr_atr > 0 and (use_sl_atr or use_trail_atr)
            )
            entry_atr: float = 0.0
            sl_mult: float = 0.0
            trail_mult: float = 0.0

            # TP 도달 여부로 TRL 후보를 결정 — evaluate_position 과 동일 분기
            _trail_only_after_tp = TradingStrategy._use_atr_trail_only_after_tp_for_stage(
                s, is_type_b, stage
            )
            tp_arm_pct = float(
                s.get(
                    _exit_key(
                        "s1_a_take_profit",
                        "s1_b_take_profit",
                        "s2_take_profit",
                        "s2_b_take_profit",
                    ),
                    5.0,
                )
            )
            try:
                peak_pct = (atr_peak / avg_p - 1.0) * 100.0 if avg_p > 0 else 0.0
            except Exception:
                peak_pct = 0.0
            trail_armed = bool(use_trail_atr)
            if _trail_only_after_tp and use_trail_atr:
                trail_armed = peak_pct >= tp_arm_pct
            # 고정TP 연기 모드 + TP 무장 후 Flash 표시·청산 모두 제외(evaluate_position 정합)
            skip_flash_for_atr_trail = False
            if _trail_only_after_tp and use_trail_atr:
                skip_flash_for_atr_trail = peak_pct >= tp_arm_pct

            def _fixed_sl_keys_for_display() -> tuple[bool, float]:
                """고정 SL(% 기준) 토글/퍼센트(%)를 단계/타입별로 resolve합니다."""
                try:
                    if stage >= 2:
                        if is_type_b:
                            return bool(s.get("use_s2_b_stop_loss", True)), float(
                                s.get("s2_b_stop_loss", 2.0)
                            )
                        return bool(s.get("use_s2_stop_loss", True)), float(
                            s.get("s2_stop_loss", 2.0)
                        )
                    # stage == 1
                    if is_type_b:
                        return bool(s.get("use_s1_b_stop_loss", True)), float(
                            s.get("s1_b_stop_loss", 3.0)
                        )
                    return bool(s.get("use_s1_a_stop_loss", True)), float(
                        s.get("s1_a_stop_loss", 3.0)
                    )
                except Exception:
                    return False, 0.0

            # 후보들 중 "가장 높은 가격(=가장 먼저 도달하는 하방 기준가)" 1개만 표시합니다.
            candidates: list[tuple[str, float]] = []

            # 1) SL% vs ATR SL (둘 중 하나만)
            atr_sl_px: Optional[float] = None
            if run_atr_exit and use_sl_atr and curr_atr > 0:
                try:
                    entry_atr = float(TradingStrategy._entry_atr_for_atr_sl(pos, curr_atr))
                    sl_mult = float(s.get(f"atr_multiplier_sl{suff}", 2.5))
                    sl_px = avg_p - (sl_mult * entry_atr)
                    if np.isfinite(sl_px):
                        atr_sl_px = float(sl_px)
                except Exception:
                    atr_sl_px = None

            if atr_sl_px is not None:
                candidates.append(("ATR SL", atr_sl_px))
            else:
                fixed_on, fixed_sl_pct = _fixed_sl_keys_for_display()
                if fixed_on and fixed_sl_pct > 0 and avg_p > 0:
                    try:
                        sl_px = avg_p * (1.0 - fixed_sl_pct / 100.0)
                        if np.isfinite(sl_px):
                            candidates.append(("SL%", float(sl_px)))
                    except Exception:
                        pass

            # 2) ATR TRL (TP 미도달이면 후보 제외)
            if run_atr_exit and use_trail_atr and trail_armed and curr_atr > 0:
                try:
                    trail_mult = float(s.get(f"atr_multiplier_trail{suff}", 4.0))
                    tr_px = atr_peak - (trail_mult * curr_atr)
                    if np.isfinite(tr_px) and np.isfinite(atr_peak):
                        candidates.append(("ATR TRL", float(tr_px)))
                except Exception:
                    pass

            # 3) FLASH Exit 기준가
            try:
                use_flash = bool(
                    s.get(
                        _exit_key(
                            "use_s1_a_flash_exit",
                            "use_s1_b_flash_exit",
                            "use_s2_flash_exit",
                            "use_s2_b_flash_exit",
                        ),
                        True,
                    )
                )
                flash_pct = float(
                    s.get(
                        _exit_key(
                            "s1_a_flash_exit_pct",
                            "s1_b_flash_exit_pct",
                            "s2_flash_exit_pct",
                            "s2_b_flash_exit_pct",
                        ),
                        3.0,
                    )
                )
                if (
                    use_flash
                    and not skip_flash_for_atr_trail
                    and flash_pct > 0
                    and max_p > 0
                ):
                    flash_px = max_p * (1.0 - flash_pct / 100.0)
                    if np.isfinite(flash_px):
                        candidates.append(("FLASH", float(flash_px)))
            except Exception:
                pass

            if not candidates:
                return "—"

            # 가장 높은 기준가 선택
            label, px = max(candidates, key=lambda x: x[1])
            return f"{label} {px:,.0f}"
        except Exception:
            return "—"

    @staticmethod
    def _time_gate_ts(current_time: Any, is_bar_label: bool = False) -> Any:
        """
        시간대 게이트(개장 매도 유예·진입 후 유예)에 쓸 **실제 판정 벽시계 시각**.

        BT 15분 경로의 ``current_time`` 은 봉의 좌측 라벨이라, 그 봉으로 판정하는
        실제 시점은 봉이 닫히는 시각(보통 라벨+15분)이다. 라벨을 그대로 쓰면
        벽시계로 판정하는 실시간보다 유예가 한 봉 더 길어져 RT/BT 정합이 깨진다.
        실시간·1분 경로는 이미 실제 시각이므로 그대로 반환한다.
        """
        ts = pd.to_datetime(current_time)
        if not is_bar_label:
            return ts
        try:
            from utils.krx_bars import is_krx_15m_slot_end, krx_15m_slot_complete_ts

            if is_krx_15m_slot_end(ts):
                return pd.Timestamp(krx_15m_slot_complete_ts(ts))
        except Exception:
            return ts
        return ts

    @staticmethod
    def _evaluate_type_c_exit(
        now_price: float,
        pos: dict,
        settings: dict,
        *,
        indicator_row: Any = None,
        current_time: Any = None,
        kwargs: Optional[dict] = None,
        time_is_bar_label: bool = False,
    ) -> dict:
        """진입 시 고정한 박스 경계를 사용해 TYPE_C 전용 청산을 판정합니다."""
        try:
            price = float(now_price)
            avg_price = float(pos.get("avg_p", pos.get("avg_price", price)) or price)
            profit_ratio = (
                (price / avg_price - 1.0) * 100.0 if avg_price > 0 else 0.0
            )
        except (TypeError, ValueError, ZeroDivisionError):
            return {"action": "HOLD", "reason": "BOX_C_BAD_PRICE"}

        def _indicator_value(key: str, default: float) -> float:
            try:
                if indicator_row is None:
                    return float(default)
                value = indicator_row.get(key, default)
                return float(value if value is not None else default)
            except (TypeError, ValueError, AttributeError):
                return float(default)

        def _finish(result: dict) -> dict:
            """A/B gap_grace와 동일: 개장 직후 N분간 익절 외 EXIT를 보류."""
            try:
                if result.get("action") != "EXIT":
                    return result
                if str(result.get("reason", "")) == "BOX_C_TAKE_PROFIT":
                    return result
                if not bool(settings.get("use_type_c_gap_grace", True)):
                    return result
                if current_time is None:
                    return result
                from utils.krx_session_hours import get_session_hours

                dt = TradingStrategy._time_gate_ts(
                    current_time, time_is_bar_label
                )
                open_m = get_session_hours(dt.date()).open_minutes
                hm = int(dt.hour) * 60 + int(dt.minute)
                try:
                    grace_mins = max(
                        0, int(float(settings.get("type_c_gap_grace_mins", 45)))
                    )
                except (TypeError, ValueError):
                    grace_mins = 45
                if grace_mins > 0 and open_m <= hm < open_m + grace_mins:
                    return {
                        "action": "HOLD",
                        "reason": f"GRACE({result.get('reason')})",
                    }
            except Exception:
                return result
            return result

        try:
            lower = float(pos.get("box_lower", 0) or 0)
            upper = float(pos.get("box_upper", 0) or 0)
            entry_atr = float(pos.get("box_atr_entry", 0) or 0)
        except (TypeError, ValueError):
            lower, upper, entry_atr = 0.0, 0.0, 0.0
        if entry_atr <= 0:
            entry_atr = _indicator_value(
                "ATR_C", _indicator_value("ATR", 0.0)
            )

        # 메타데이터 유실 시 박스 목표를 추정하지 않고 비상 %손절만 유지합니다.
        if not (upper > lower > 0 and entry_atr > 0):
            try:
                emergency_sl = abs(
                    float(settings.get("type_c_missing_box_stop_loss_pct", 3.0))
                )
            except (TypeError, ValueError):
                emergency_sl = 3.0
            if profit_ratio <= -emergency_sl:
                return _finish(
                    {
                        "action": "EXIT",
                        "reason": "BOX_C_METADATA_STOP",
                        "profit_ratio": profit_ratio,
                    }
                )
            return {"action": "HOLD", "reason": "BOX_C_METADATA_MISSING"}

        try:
            gap_limit = float(settings.get("type_c_gap_down_limit", -4.0))
        except (TypeError, ValueError):
            gap_limit = -4.0
        if bool(settings.get("use_type_c_gap_down_exit", True)) and profit_ratio <= gap_limit:
            return _finish(
                {
                    "action": "EXIT",
                    "reason": "BOX_C_GAP_DOWN",
                    "profit_ratio": profit_ratio,
                }
            )

        try:
            stop_atr = max(
                0.0, float(settings.get("type_c_stop_atr_tolerance", 0.5))
            )
        except (TypeError, ValueError):
            stop_atr = 0.5
        stop_price = lower - stop_atr * entry_atr
        bar_low = _indicator_value("Low", price)
        if min(price, bar_low) <= stop_price:
            return _finish(
                {
                    "action": "EXIT",
                    "reason": "BOX_C_STOP",
                    "profit_ratio": profit_ratio,
                    "stop_price": stop_price,
                }
            )

        # 연속 종가 이탈은 완결 봉에서만 1회 갱신합니다.
        confirmed_bar = bool(time_is_bar_label)
        try:
            confirmed_bar = confirmed_bar or bool((kwargs or {}).get("bar_confirmed"))
        except Exception:
            pass
        if confirmed_bar:
            bar_key = ""
            try:
                if kwargs and kwargs.get("bar_key") is not None:
                    bar_key = str(kwargs.get("bar_key"))
                elif indicator_row is not None and getattr(indicator_row, "name", None) is not None:
                    bar_key = str(pd.Timestamp(indicator_row.name))
                elif current_time is not None:
                    bar_key = str(pd.Timestamp(current_time))
            except (TypeError, ValueError):
                bar_key = str(current_time or "")
            previous_key = str(pos.get("box_last_breakdown_bar", "") or "")
            if bar_key and bar_key != previous_key:
                close_price = _indicator_value("Close", price)
                try:
                    count = max(0, int(pos.get("box_breakdown_closes", 0) or 0))
                except (TypeError, ValueError):
                    count = 0
                count = count + 1 if close_price < lower else 0
                pos["box_breakdown_closes"] = count
                pos["box_last_breakdown_bar"] = bar_key
            try:
                breakdown_limit = max(
                    1,
                    int(float(settings.get("type_c_breakdown_confirm_bars", 2))),
                )
            except (TypeError, ValueError):
                breakdown_limit = 2
            if int(pos.get("box_breakdown_closes", 0) or 0) >= breakdown_limit:
                return _finish(
                    {
                        "action": "EXIT",
                        "reason": "BOX_C_BREAKDOWN",
                        "profit_ratio": profit_ratio,
                    }
                )

        try:
            target_buffer = max(
                0.0, float(settings.get("type_c_target_atr_buffer", 0.2))
            )
        except (TypeError, ValueError):
            target_buffer = 0.2
        target_price = upper - target_buffer * entry_atr
        if price >= target_price:
            return _finish(
                {
                    "action": "EXIT",
                    "reason": "BOX_C_TAKE_PROFIT",
                    "profit_ratio": profit_ratio,
                    "target_price": target_price,
                }
            )

        # 진입일 포함 평일 수 기준. bars_df가 있으면 실제 거래일 수를 우선합니다.
        try:
            max_days = max(
                1, int(float(settings.get("type_c_max_holding_days", 5)))
            )
        except (TypeError, ValueError):
            max_days = 5
        held_days = 0
        try:
            entry_ts = pd.Timestamp(pos.get("entry_date") or pos.get("entry_time"))
            eval_ts = TradingStrategy._time_gate_ts(
                current_time or pd.Timestamp.now(), time_is_bar_label
            )
            bars_df = (kwargs or {}).get("bars_df")
            if bars_df is not None and not getattr(bars_df, "empty", True):
                dates = pd.DatetimeIndex(pd.to_datetime(bars_df.index)).normalize().unique()
                held_days = int(
                    sum(entry_ts.normalize() <= day <= eval_ts.normalize() for day in dates)
                )
            if held_days <= 0:
                held_days = int(
                    np.busday_count(
                        entry_ts.date(),
                        (eval_ts + pd.Timedelta(days=1)).date(),
                    )
                )
        except (TypeError, ValueError):
            held_days = 0
        if held_days >= max_days:
            return _finish(
                {
                    "action": "EXIT",
                    "reason": "BOX_C_TIME_EXIT",
                    "profit_ratio": profit_ratio,
                    "holding_days": held_days,
                }
            )

        return {
            "action": "HOLD",
            "reason": "BOX_C_HOLD",
            "profit_ratio": profit_ratio,
            "target_price": target_price,
            "stop_price": stop_price,
        }

    @staticmethod
    def evaluate_position(code, now_price, pos, settings, indicator_row=None, current_time=None, is_realtime=False, kwargs=None, indicators_dict=None, i=None, time_is_bar_label=False):
        """
        [v35.0] 실시간 감시와 모든 백테스팅(포트폴리오 포함)이 공유하는 통합 포지션 평가(매도 결정) 함수.
        (v40.0: Optimized direct indexing support)

        ``time_is_bar_label`` 이 True 면 ``current_time`` 을 15분봉 **좌측 라벨**로 보고
        시간대 게이트(개장 매도 유예 등)는 봉이 닫히는 시각으로 환산해 판정한다.
        """
        if now_price <= 0: return {'action': 'HOLD'}
        
        def _get_val(key, default=0):
            if indicators_dict is not None and i is not None:
                return indicators_dict.get(key, {}).get(i, default) if hasattr(indicators_dict.get(key), 'get') else indicators_dict[key][i] if key in indicators_dict else default
            return indicator_row.get(key, default) if indicator_row is not None else default

        # 1. 기본 정보 산출
        stage = TradingStrategy._coerce_position_stage(pos)
        avg_p = pos.get('avg_p', pos.get('avg_price', now_price))
        profit_ratio = (now_price / avg_p - 1) * 100
        
        # 2. 최고가 갱신 — Flash 고점(max_p)과 ATR 트레일 고점(max_p_high)을 분리 가능
        # use_flash_peak_bar_high / use_atr_peak_bar_high (기본 False=종가·평가가)
        try:
            use_flash_hi = bool(settings.get("use_flash_peak_bar_high", False))
        except Exception:
            use_flash_hi = False
        try:
            use_atr_hi = bool(settings.get("use_atr_peak_bar_high", False))
        except Exception:
            use_atr_hi = False
        bars_df_kw = None
        try:
            if kwargs:
                bars_df_kw = kwargs.get("bars_df")
        except Exception:
            bars_df_kw = None

        # --- 종가(평가가) 고점 ---
        # 봉 복원(진입 이후·다일) + 저장 max_p 병합 — 당일만 보면 전일 고점이 지워짐
        peak_close = None
        try:
            bc = TradingStrategy._cached_bars_peak_high(
                pos, bars_df_kw, price_col="Close"
            )
        except Exception:
            bc = None
        try:
            peak_close = float(
                TradingStrategy._peak_price_from_position(
                    pos, float(now_price), bars_peak_high=bc
                )
            )
        except Exception:
            try:
                peak_close = float(now_price)
            except (TypeError, ValueError):
                peak_close = 0.0

        # --- 봉 High 고점 ---
        peak_high = None
        if use_flash_hi or use_atr_hi:
            bars_hi = None
            try:
                bars_hi = TradingStrategy._cached_bars_peak_high(pos, bars_df_kw)
            except Exception:
                bars_hi = None
            if bars_hi is None:
                try:
                    ih = float(_get_val("High", 0) or 0)
                    if ih > 0:
                        bars_hi = ih
                except (TypeError, ValueError):
                    pass
            hi_bests: list = []
            try:
                hi_bests.append(float(now_price))
            except (TypeError, ValueError):
                pass
            if bars_hi is not None:
                try:
                    hi_bests.append(float(bars_hi))
                except (TypeError, ValueError):
                    pass
            for k in ("max_p_high",):
                try:
                    v = float((pos or {}).get(k, 0) or 0)
                    if v > 0:
                        hi_bests.append(v)
                except (TypeError, ValueError):
                    pass
            if use_flash_hi:
                # Flash도 High면 기존 max_p 키를 High 후보에 포함
                for k in ("max_p", "max_price", "max_now"):
                    try:
                        v = float((pos or {}).get(k, 0) or 0)
                        if v > 0:
                            hi_bests.append(v)
                    except (TypeError, ValueError):
                        pass
            peak_high = float(max(hi_bests)) if hi_bests else float(now_price)

        max_p = float(peak_high if use_flash_hi else peak_close)
        atr_peak = float(peak_high if use_atr_hi else peak_close)
        try:
            pos["max_p"] = max_p
            pos["max_price"] = max_p
            pos["max_p_close"] = float(peak_close)
            if peak_high is not None:
                pos["max_p_high"] = float(peak_high)
            pos["max_p_atr"] = float(atr_peak)
        except Exception:
            pass
        
        # 3. 지표 데이터 확보
        is_power_exhausted = False
        bid_ask_ratio = 0
        
        if kwargs:
            is_power_exhausted = kwargs.get('is_power_exhausted', False)
            bid_ask_ratio = kwargs.get('bid_ask_ratio', 0)
        else:
            bid_ask_ratio = _get_val('bid_ask_ratio', _get_val('Bid_Ask_Ratio', 0))

        # Smart Exit 호가비: Smart Buy '매도벽 비율'과 동일하게 매도잔량/매수잔량(Ask/Bid).
        # kwargs에 ask_bid_ratio가 있으면(실시간 틱) 우선 사용, 없으면 bid_ask(매수/매도)의 역수로 산출.
        sell_buy_wall_ratio = 0.0
        try:
            if kwargs is not None and kwargs.get("ask_bid_ratio") is not None:
                sell_buy_wall_ratio = float(kwargs["ask_bid_ratio"])
            else:
                ba = float(bid_ask_ratio or 0)
                if ba > 1e-12:
                    sell_buy_wall_ratio = 1.0 / ba
                else:
                    sell_buy_wall_ratio = float(
                        _get_val("ask_bid_ratio", _get_val("Ask_Bid_Ratio", 0)) or 0
                    )
        except (TypeError, ValueError):
            sell_buy_wall_ratio = 0.0

        is_exit = False
        exit_reason = ""
        if TradingStrategy._holding_sig_is_type_c(pos):
            return TradingStrategy._evaluate_type_c_exit(
                now_price,
                pos,
                settings,
                indicator_row=indicator_row,
                current_time=current_time,
                kwargs=kwargs,
                time_is_bar_label=time_is_bar_label,
            )
        is_type_b = TradingStrategy._holding_sig_is_type_b(pos)
        if not is_type_b:
            try:
                is_type_b = "TYPE_B" in str(pos.get("reason", "")).upper()
            except Exception:
                is_type_b = False
        # 실시간 감시: compute_atr_exit_display_text 의 sel_track_hint 와 동일.
        # 계좌 동기화만 한 포지션 등 sig_type 이 비어 있어도 트리가 B면 B 트랙 ATR·TP 키로 청산해야 함.
        if not is_type_b and kwargs:
            try:
                sth = kwargs.get("sel_track") or kwargs.get("SelTrack")
                if sth is not None and str(sth).strip().upper() == "B":
                    is_type_b = True
            except Exception:
                pass
        s = settings
        tp_pct_applied = None  # TAKE_PROFIT 시 체결가 보정용(판정과 동일 %)
        _rx = TradingStrategy._exit_track_risk_bundle(s, is_type_b)
        _atr_col = TradingStrategy._atr_column_for_holdings(is_type_b)

        def _curr_atr() -> float:
            """트랙별 ATR_A/ATR_B 우선, 없으면 구버전 ATR."""
            try:
                v = float(_get_val(_atr_col, 0.0) or 0.0)
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass
            try:
                return float(_get_val("ATR", 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        def _curr_atr_resolved() -> float:
            """
            마지막 봉 지표가 NaN이어도 bars_df·직전 유효 ATR·TR 근사로 보강.
            실시간 감시 화면(compute_atr_exit_display_text)과 동일 계열 폴백 — 청산만 0 ATR로 무력화되는 현상 방지.
            """
            try:
                base = float(_curr_atr())
                if base > 0:
                    return base
                kw = kwargs or {}
                bd = kw.get("bars_df")
                if bd is not None and not getattr(bd, "empty", True):
                    fb = TradingStrategy._last_finite_atr_from_bars_df(
                        bd, s, _atr_col
                    )
                    if fb > 0:
                        return float(fb)
            except Exception:
                pass
            return 0.0

        # ---------------------------------------------------------
        # [1단계] 탈출(Exit) 조건 검사
        # ---------------------------------------------------------
        
        # 0. Gap Down (트랙별)
        if _rx["use_gap_down_exit"]:
            gap_limit = float(_rx["gap_down_limit"])
            if profit_ratio <= gap_limit:
                is_exit, exit_reason = True, "GAP_DOWN"

        # [Phase 2] S2 청산 키도 Type A/B 분리
        def _exit_key(a_key: str, b_key: str, s2_a_key: str, s2_b_key: str):
            if stage >= 2:
                return s2_b_key if is_type_b else s2_a_key
            return b_key if is_type_b else a_key

        def _atr_track_suff() -> str:
            """ATR SL/트레일용 접미사 (_a / _b / _s2 / _s2_b)"""
            if stage >= 2:
                return "_s2_b" if is_type_b else "_s2"
            return "_b" if is_type_b else "_a"

        # 고정 익절 vs "목표% 이후 ATR 트레일만": 옵션 ON이면 TP%는 트레일 무장 기준이며 TAKE_PROFIT 청산은 하지 않음
        _trail_only_after_tp = TradingStrategy._use_atr_trail_only_after_tp_for_stage(s, is_type_b, stage)
        # 트레일 '익절% 이후만'은 ATR 분석 주기(use_atr_exit)와 별개로, ATR 트레일 토글만 켜져 있으면 적용
        defer_fixed_tp_for_trail = False
        if _trail_only_after_tp:
            try:
                _sfx_defer = _atr_track_suff()
                _raw_tr = bool(s.get(f"use_atr_trailing_stop{_sfx_defer}", True))
                if TradingStrategy._effective_use_atr_trailing_for_stage(
                    s, _rx, stage, _raw_tr
                ):
                    defer_fixed_tp_for_trail = True
            except Exception:
                pass

        # 고정TP 연기 모드에서 목표% 무장 후에는 Flash가 ATR 트레일보다 먼저 청산하는 것을 막음
        # (무장 판정은 ATR 고점 기준)
        tp_pct_for_trail_arm = float(
            s.get(
                _exit_key(
                    "s1_a_take_profit",
                    "s1_b_take_profit",
                    "s2_take_profit",
                    "s2_b_take_profit",
                ),
                5.0,
            )
        )
        try:
            peak_profit_pct_gate = (
                (atr_peak / float(avg_p) - 1.0) * 100.0
                if avg_p is not None and float(avg_p) > 0
                else profit_ratio
            )
        except (TypeError, ValueError, ZeroDivisionError):
            peak_profit_pct_gate = profit_ratio
        skip_flash_for_atr_trail = defer_fixed_tp_for_trail and peak_profit_pct_gate >= tp_pct_for_trail_arm

        # 1. 익절(Take Profit)
        if not is_exit:
            use_tp = s.get(_exit_key('use_s1_a_tp', 'use_s1_b_tp', 'use_s2_tp', 'use_s2_b_tp'), True)
            if use_tp:
                tp_pct = float(s.get(_exit_key('s1_a_take_profit', 's1_b_take_profit', 's2_take_profit', 's2_b_take_profit'), 5.0))
                if profit_ratio >= tp_pct:
                    if not defer_fixed_tp_for_trail:
                        is_exit, exit_reason = True, "TAKE_PROFIT"
                        tp_pct_applied = tp_pct

        # 2. 손절(Stop Loss - % 기준)
        if not is_exit:
            use_sl = s.get(_exit_key('use_s1_a_stop_loss', 'use_s1_b_stop_loss', 'use_s2_stop_loss', 'use_s2_b_stop_loss'), True)
            skip_pct_sl = False
            # ATR SL 토글만 켜도(청산 'ATR 분석 주기'와 무관) %손절과 이중 적용하지 않음 — 지표는 항상 ATR_A/B 계산
            try:
                _ca = float(_curr_atr_resolved())
                _sfx = _atr_track_suff()
                if _ca > 0 and s.get(
                    f"use_atr_stop_loss{_sfx}",
                    TradingStrategy._default_use_atr_stop_loss(_sfx),
                ):
                    skip_pct_sl = True
            except Exception:
                pass

            if use_sl and not skip_pct_sl:
                sl_pct = float(s.get(_exit_key('s1_a_stop_loss', 's1_b_stop_loss', 's2_stop_loss', 's2_b_stop_loss'), 3.0))
                if profit_ratio <= -sl_pct:
                    # 리포트·CSV에서 설정 반영 여부 확인용(STOP_LOSS 문자열은 유지 → SL 버킷 집계 동일)
                    is_exit, exit_reason = True, f"STOP_LOSS|SL%={sl_pct:g}"

        # 3. Flash Exit (Trailing from High) — Flash 고점(max_p) 기준
        if not is_exit:
            use_flash = s.get(_exit_key('use_s1_a_flash_exit', 'use_s1_b_flash_exit', 'use_s2_flash_exit', 'use_s2_b_flash_exit'), True)
            if use_flash and profit_ratio >= 0 and not skip_flash_for_atr_trail:
                f_pct = float(s.get(_exit_key('s1_a_flash_exit_pct', 's1_b_flash_exit_pct', 's2_flash_exit_pct', 's2_b_flash_exit_pct'), 3.0))
                if now_price <= max_p * (1.0 - f_pct / 100.0):
                    is_exit, exit_reason = True, "FLASH_EXIT"

        # 4. Smart Exit (Supply/Demand Trap) — Flash와 동일 고점(종가/고가 정책 공유)
        # smart_exit_price_threshold: 고점 대비 '하락 폭(%)'을 양수로 적는 것이 의도.
        # 과거 호환을 위해 음수 입력은 절댓값으로 동일 해석(예: -1 과 1 동일 → 1% 하락 후 매도벽 조건 평가).
        if not is_exit and s.get('use_smart_exit', True):
            try:
                raw_thr = float(s.get("smart_exit_price_threshold", 0.0))
            except (TypeError, ValueError):
                raw_thr = 0.0
            drop_pct = abs(raw_thr)
            if drop_pct == 0.0 or now_price <= max_p * (1.0 - drop_pct / 100.0):
                min_trap = float(s.get('smart_exit_ba_ratio', 1.3))
                if sell_buy_wall_ratio >= min_trap or (is_realtime and is_power_exhausted):
                    is_exit, exit_reason = True, "SMART_EXIT"

        # 5. ATR 기반 손절 및 트레일링 스톱 — ATR 고점(atr_peak) 기준
        # use_atr_exit: ATR 주기 UI 마스터. OFF여도 ATR SL/트레일 개별 토글이면 해당 로직만 수행(지표 ATR은 이미 존재)
        if not is_exit:
            try:
                curr_atr = float(_curr_atr_resolved())
                suff = _atr_track_suff()
                use_sl_atr = s.get(
                    f"use_atr_stop_loss{suff}",
                    TradingStrategy._default_use_atr_stop_loss(suff),
                )
                use_trail_atr = s.get(f"use_atr_trailing_stop{suff}", True)
                try:
                    use_trail_atr = TradingStrategy._effective_use_atr_trailing_for_stage(
                        s, _rx, stage, bool(use_trail_atr)
                    )
                except Exception:
                    use_trail_atr = bool(use_trail_atr)
                run_atr_exit = _rx["use_atr_exit"] or (
                    curr_atr > 0 and (use_sl_atr or use_trail_atr)
                )
                if run_atr_exit:
                    entry_atr = TradingStrategy._entry_atr_for_atr_sl(pos, curr_atr)
                    if curr_atr > 0:
                        sl_mult = float(s.get(f"atr_multiplier_sl{suff}", 2.5))
                        trail_mult = float(s.get(f"atr_multiplier_trail{suff}", 4.0))

                        trail_armed = True
                        if _trail_only_after_tp and use_trail_atr:
                            tp_arm_pct = float(
                                s.get(
                                    _exit_key(
                                        "s1_a_take_profit",
                                        "s1_b_take_profit",
                                        "s2_take_profit",
                                        "s2_b_take_profit",
                                    ),
                                    5.0,
                                )
                            )
                            peak_pct = (
                                (atr_peak / avg_p - 1.0) * 100.0
                                if avg_p and float(avg_p) > 0
                                else profit_ratio
                            )
                            trail_armed = peak_pct >= tp_arm_pct

                        # [초단기 유예] 당일 또는 N시간 동안 SL 배수 확대
                        eff_sl_mult = sl_mult
                        try:
                            grace_mult = float(s.get("atr_sl_entry_grace_mult", 0) or 0)
                            grace_same_day = bool(s.get("atr_sl_entry_grace_same_day", False))
                            grace_h = float(s.get("atr_sl_entry_grace_hours", 0) or 0)
                            if grace_mult > sl_mult and current_time is not None:
                                et_raw = pos.get("entry_date") or pos.get("entry_time")
                                if et_raw is not None:
                                    _ct = TradingStrategy._time_gate_ts(
                                        current_time, time_is_bar_label
                                    )
                                    _et = pd.to_datetime(et_raw)
                                    if grace_same_day:
                                        if _ct.normalize() == _et.normalize():
                                            eff_sl_mult = grace_mult
                                    elif grace_h > 0:
                                        _age_h = (_ct - _et).total_seconds() / 3600.0
                                        if 0.0 <= float(_age_h) < grace_h:
                                            eff_sl_mult = grace_mult
                        except Exception:
                            eff_sl_mult = sl_mult

                        if use_sl_atr and now_price <= avg_p - (eff_sl_mult * entry_atr):
                            is_exit, exit_reason = True, "ATR_SL"
                        elif use_trail_atr and trail_armed and now_price <= atr_peak - (trail_mult * curr_atr):
                            is_exit, exit_reason = True, "ATR_TRAIL"
            except Exception:
                pass

        if is_exit:
            # [v4.5.10] 개장 초기 매도 유예 (휩소 방어, 트랙별)
            if _rx["use_gap_grace"]:
                if current_time:
                    try:
                        from utils.krx_session_hours import get_session_hours

                        dt = TradingStrategy._time_gate_ts(
                            current_time, time_is_bar_label
                        )
                        open_m = get_session_hours(dt.date()).open_minutes
                        hm = int(dt.hour) * 60 + int(dt.minute)
                        if open_m <= hm < open_m + int(_rx["gap_grace_mins"]):
                            if exit_reason != "TAKE_PROFIT":
                                return {'action': 'HOLD', 'reason': f'GRACE({exit_reason})'}
                    except Exception:
                        pass

            out = {'action': 'EXIT', 'reason': exit_reason, 'profit_ratio': profit_ratio}
            if exit_reason == "TAKE_PROFIT" and tp_pct_applied is not None:
                out["tp_pct_applied"] = tp_pct_applied
            return out

        return {'action': 'HOLD'}

