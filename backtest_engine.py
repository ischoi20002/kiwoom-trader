from __future__ import annotations

from typing import Any, Optional, Tuple

import FinanceDataReader as fdr
import os
import pandas as pd
# [Fix] Silencing FutureWarnings regarding dtype downcasting (pandas 2.2+)
pd.set_option('future.no_silent_downcasting', True)
import json
import time
import requests
import config
from datetime import datetime, timedelta
import numpy as np
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('Agg')
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from concurrent.futures import ThreadPoolExecutor as TPE, ProcessPoolExecutor as PPE, as_completed
import threading
import pickle
from data_manager_v2 import DataManagerV2
import strategy_selector
import itertools

# [Modularization Phase 1]
from utils.normalization import normalize_code
from strategy.data_loader import BacktestDataLoader
# from reporting.signal_report import generate_signal_report (DELETED)
import reporting.trade_report
import importlib
importlib.reload(reporting.trade_report)
from reporting.trade_report import generate_trade_report
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

csv_lock = threading.Lock()
# [v15.3.7] Script-relative absolute paths for robust log management
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def settings_for_backtest(base_settings):
    """
    백테스트 전용 설정: Smart Buy / Smart Exit 는 실시간 감시에만 적용.
    UI·settings.json 값은 그대로 두고 엔진에 넘길 때만 비활성화한다.
    """
    try:
        s = dict(base_settings or {})
    except Exception:
        s = {}
    s["use_smart_buy_a"] = False
    s["use_smart_buy_b"] = False
    s["use_smart_buy_s2"] = False
    s["use_smart_buy_s2_b"] = False
    s["use_smart_exit"] = False
    return s
SIGNAL_CSV = os.path.join(BASE_DIR, "backtest_signals.csv")
TRADE_CSV = os.path.join(BASE_DIR, "backtest_trades.csv")
# print(f"DEBUG [backtest_engine]: BASE_DIR={BASE_DIR}")
# print(f"DEBUG [backtest_engine]: TRADE_CSV={TRADE_CSV}")

# Dynamic 백테스트 등에서 매 거래마다 파일 open/append 하지 않도록 행 단위 버퍼
TRADE_CSV_HEADER_LINE: str = "Date,Code,Name,Action,Price,Qty,Return,Reason,Fee,Sig_Type\n"
_trade_csv_buffer: list[str] = []


def clear_trade_csv_buffer() -> None:
    """TRADE_CSV 버퍼만 비움 (시뮬 시작 시 로그 파일 삭제와 함께 호출)."""
    with csv_lock:
        _trade_csv_buffer.clear()


def append_trade_csv_line(line: str) -> None:
    """TRADE_CSV용 한 행(끝에 \\n 포함)을 버퍼에 적재. 스레드 안전."""
    with csv_lock:
        _trade_csv_buffer.append(line)


def flush_trade_csv_buffer() -> None:
    """버퍼 내용을 TRADE_CSV에 일괄 기록. 실패 시 버퍼를 유지해 이후 flush에서 재시도 가능."""
    with csv_lock:
        if not _trade_csv_buffer:
            return
        try:
            need_header = (not os.path.exists(TRADE_CSV)) or os.path.getsize(TRADE_CSV) == 0
            with open(TRADE_CSV, "a", encoding="utf-8-sig") as f:
                if need_header:
                    f.write(TRADE_CSV_HEADER_LINE)
                f.writelines(_trade_csv_buffer)
            _trade_csv_buffer.clear()
        except OSError as e:
            try:
                print(f"[WARN] TRADE_CSV flush 실패(OSError): {e}")
            except Exception:
                pass
        except Exception as e:
            try:
                print(f"[WARN] TRADE_CSV flush 실패: {e}")
            except Exception:
                pass

# [v40.0] Multiprocessing Infrastructure for Portfolio Backtesting
_G_BT_DATA = {}
_G_BT_M1 = {}
def init_bt_worker(cache_path):
    """Worker process initialization: Load data from pickle cache once."""
    global _G_BT_DATA, _G_BT_M1
    try:
        if os.path.exists(cache_path):
            with open(cache_path, 'rb') as f:
                cache = pickle.load(f)
                _G_BT_DATA = cache.get('data', {})
                _G_BT_M1 = cache.get('m1', {})
    except Exception as e:
        print(f"  (!) BT Worker Init Error: {e}")

def _run_single_backtest_task(args):
    """Top-level function for ProcessPoolExecutor to run a single stock backtest."""
    code, settings, interval, force_refresh, high_precision = args
    
    global _G_BT_DATA, _G_BT_M1

    data = _G_BT_DATA.get(code)
    m1_pre = _G_BT_M1.get(code)

    if data is not None:
        tester = SimpleBacktester(settings)
        try:
            summary = tester.run(data, code, market_df=None, vix_df=None, 
                              log_to_csv=True, high_precision=high_precision, 
                              m1_df_preloaded=m1_pre)
            return code, data, summary
        except Exception as e:
            print(f"  (!) PPE Task Error ({code}): {e}")
    return code, None, None

class TradingStrategy:
    """[v6.4] 실시간 감시와 백테스팅이 공유하는 매매 전략 엔진"""
    # 유니버스 인덱스 소스별( KODEX200 / KOSPI200 ) 코드->이름 맵 캐시
    _name_map_by_choice: dict[str, dict[str, str]] = {}
    
    @staticmethod
    def get_stock_name_map(universe_index_choice: str = "KODEX200"):
        """종목코드 -> 종목명 맵핑 로드.

        Parameters
        ----------
        universe_index_choice:
            - 'KODEX200': kodex200.csv
            - 'KOSPI200': kospi_universe.csv
        """
        try:
            choice = str(universe_index_choice).strip().upper()
        except Exception:
            choice = "KODEX200"
        if choice in ("KOSPI", "KOSPI200"):
            choice = "KOSPI200"
        else:
            choice = "KODEX200"

        cached = TradingStrategy._name_map_by_choice.get(choice)
        if cached is not None:
            return cached

        csv_path = "kodex200.csv"
        if choice == "KOSPI200":
            csv_path = "kospi_universe.csv"

        try:
            if not os.path.exists(csv_path):
                # 다른 kodex_*.csv/kodex*.csv 또는 kospi_universe/*.csv 파일이 있는지 확인
                import glob
                if choice == "KOSPI200":
                    files = glob.glob("kospi_universe*.csv") + glob.glob("kospi_*.csv") + glob.glob("kospi200*.csv")
                else:
                    files = glob.glob("kodex*.csv") + glob.glob("kospi_*.csv")
                if files:
                    csv_path = files[0]
                else:
                    TradingStrategy._name_map_by_choice[choice] = {}
                    return {}
            
            if os.path.exists(csv_path):
                # 기존 kodex200.csv 포맷(종목코드/종목명)과 kospi200.csv 포맷(Code/Name) 모두 허용
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
    def get_buy_signals_detailed(df, settings, market_trend='UP'):
        """매수 신호 상세 분석 (v6.2: 상태값 포함)"""
        return signals.get_buy_signals_detailed(df, settings, market_trend)

    @staticmethod
    def get_buy_signals(df, settings, market_trend='UP'):
        """매수 신호 벡터 계산 (v6.1 통합: Trigger 기반)"""
        return TradingStrategy.get_buy_signals_detailed(df, settings, market_trend)['Signal']

    @staticmethod
    def _check_s2_approval(pos, last_row, settings, indicator_row=None, market_trend='UP', curr_dt=None, indicators_dict=None, i=None):
        """[Phase 2] Type A/B 완전 분리된 2차 매수 승인 로직 (v40.0: Optimized)"""
        s = settings

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
        Track A/B 선정과 S1 Sig_Type 교차 허용 여부.
        ``enforce_track_signal_match`` 가 False 이면 항상 True (미스매치 허용).
        """
        try:
            if not bool((settings or {}).get("enforce_track_signal_match", True)):
                return True
        except Exception:
            pass
        try:
            st = str(sig_type or "").upper()
            ct = str(code_track or "A").strip().upper()
            if st == "TYPE_A" and ct == "B":
                return False
            if st == "TYPE_B" and ct == "A":
                return False
            return True
        except Exception:
            return True

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



class SimpleBacktester:
    """고정밀 15분봉/일봉 백테스터 (v5.1)"""
    def __init__(self, settings):
        self.settings = settings_for_backtest(settings)
        self.trades = []
        self.s1_amt = 0
        self.s2_amt = 0
        self.buy_f_rate = 0
        self.sell_f_rate = 0
        self.trades_history = []
        
        # [Perf] Pre-init cached settings placeholders (v33.8)
        self.use_sb = False
        self.use_sb_a = False
        self.use_sb_b = False
        self.use_sb_s2_a = False
        self.use_sb_s2_b = False
        self.sb_ba = 1.2
        self.sb_vp = 110.0
        self.use_sx = False
        self.sx_ba = 0.8
        self.sx_vp = 90.0
        self.sx_p_thr = 0.0
        self.use_atr = False
        self.atr_sl_mult = 2.0
        self.atr_trail_mult = 3.0
        self.gap_down_limit = -5.0
        self.use_s2_tp_active = True
        self.use_type_b = False

    def _use_flash_peak_bar_high(self) -> bool:
        """True면 Flash 고점에 봉 High 반영. 기본 False(종가·평가가)."""
        try:
            return bool(self.settings.get("use_flash_peak_bar_high", False))
        except Exception:
            return False

    def _use_atr_peak_bar_high(self) -> bool:
        """True면 ATR 트레일 고점에 봉 High 반영."""
        try:
            return bool(self.settings.get("use_atr_peak_bar_high", False))
        except Exception:
            return False

    def _bump_pos_max_from_high(self, pos: Optional[dict], high: Any) -> None:
        """Flash/ATR 중 High 모드인 쪽에만 고점 상향."""
        if not pos:
            return
        use_flash = self._use_flash_peak_bar_high()
        use_atr = self._use_atr_peak_bar_high()
        if not use_flash and not use_atr:
            return
        try:
            hi = float(high)
            if not (np.isfinite(hi) and hi > 0):
                return
            if use_atr:
                try:
                    if hi > float(pos.get("max_p_high", 0) or 0):
                        pos["max_p_high"] = hi
                except Exception:
                    pos["max_p_high"] = hi
            if use_flash:
                if hi > float(pos.get("max_price", 0) or 0):
                    pos["max_price"] = hi
                try:
                    if hi > float(pos.get("max_p", 0) or 0):
                        pos["max_p"] = hi
                except Exception:
                    pass
        except (TypeError, ValueError):
            pass

    def _entry_max_p(self, fill_px: float, bar_high: Any) -> float:
        """매수 직후 max_p 초기값 — Flash High 모드면 봉고가, 아니면 체결가."""
        if self._use_flash_peak_bar_high():
            try:
                hi = float(bar_high)
                if np.isfinite(hi) and hi > 0:
                    return max(float(fill_px), hi)
            except (TypeError, ValueError):
                pass
        return float(fill_px)
        
    def calculate_rsi(self, series, period=14):
        delta = series.diff()
        up = delta.clip(lower=0)
        down = -1 * delta.clip(upper=0)
        
        avg_up = up.rolling(window=period).mean()
        avg_down = down.rolling(window=period).mean()
        
        rs = avg_up / avg_down
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def run(self, df, code="SAMPLE", market_df=None, vix_df=None, log_to_csv=False, skip_indicators=False, high_precision=False, m1_df_preloaded=None):
        """[v15.0] Ultra-High Performance Backtest Engine"""
        try:
            # [v30.6] 멀티스레드: In-place·공유 캐시(_G_DATA)로 인한 충돌 방지. 항상 deep copy.
            # [v40.1] skip_indicators(최적화) + 얕은 복사 시 trial 간 Signal/수익·MDD 동일 버그
            df = df.copy()
            # 1. Initialization
            self.df = df # [v36.9.2] 인스턴스 변수에 저장하여 정밀 루프에서 참조 가능하게 함
            if not skip_indicators:
                df = TradingStrategy.calculate_indicators(df, self.settings)
                self.df = df # 지표가 계산된 버전으로 갱신
            
            if log_to_csv:
                print(f"  [Sim] Strategy Run: {code} | Data: {len(df)} rows | HighPrec: {high_precision}")
            
            params = self._init_simulation(df, code, market_df, vix_df, high_precision)

            # 2. Main Simulation Loop (v23.0 Optimized)
            # [Perf] 미리 벡터화된 신호 및 가격 정보를 로컬 변수로 캐싱하여 루프 부하 최적화
            # [v31.4] Real-time only columns preparation
            if self.settings.get('use_synthetic_realtime', True):
                if 'vol_power' not in df.columns: df['vol_power'] = np.nan
                if 'total_ask' not in df.columns: df['total_ask'] = np.nan
                if 'total_bid' not in df.columns: df['total_bid'] = np.nan
                if 'bid_ask_ratio' not in df.columns: df['bid_ask_ratio'] = np.nan
                
                # Fill NaNs with vectorized heuristic model (v35.8 Optimized)
                p_changes = df['Close'].pct_change().fillna(0).values
                vol_mas = df['Vol_MA20'].values if 'Vol_MA20' in df.columns else df['Volume'].rolling(20).mean().bfill().values
                vol_ratios = (df['Volume'] / (vol_mas + 1e-10)).values
                
                vps = df['vol_power'].values.copy()
                tasks = df['total_ask'].values.copy()
                tbids = df['total_bid'].values.copy()
                ratios = df['bid_ask_ratio'].values.copy()
                
                # Vectorized Fill
                nan_vp = np.isnan(vps)
                if np.any(nan_vp):
                    vps[nan_vp] = synthesize_vol_power_vectorized(p_changes[nan_vp], vol_ratios[nan_vp])
                
                nan_ba = np.isnan(ratios)
                if np.any(nan_ba):
                    r_new = synthesize_bid_ask_ratio_vectorized(p_changes[nan_ba], vol_ratios[nan_ba], vps[nan_ba])
                    ratios[nan_ba] = r_new
                    tasks[nan_ba] = 100000
                    tbids[nan_ba] = (100000 * r_new).astype(int)
                
                df['vol_power'] = vps
                df['total_ask'] = tasks
                df['total_bid'] = tbids
                df['bid_ask_ratio'] = ratios
                df['ask_bid_ratio'] = 1.0 / (ratios + 1e-10)

            close_vals = df['Close'].values
            high_vals = df['High'].values
            low_vals = df['Low'].values
            vol_vals = df['Volume'].values
            vol_ma_vals = df['Vol_MA20'].values if 'Vol_MA20' in df.columns else (df['Volume'].rolling(20).mean().values)
            
            # [v31.4] Real-time only columns (VolPower, Bid/Ask)
            vp_vals = df['vol_power'].values if 'vol_power' in df.columns else np.full(len(df), np.nan)
            task_vals = df['total_ask'].values if 'total_ask' in df.columns else np.full(len(df), np.nan)
            tbid_vals = df['total_bid'].values if 'total_bid' in df.columns else np.full(len(df), np.nan)
            
            # [Perf] Extract all potentially needed indicators to NumPy for zero-copy access in loops
            # This avoids expensive df.iloc[i] and df.get(col) calls
            np_indicators = {
                'Close': close_vals,
                'High': high_vals,
                'Low': low_vals,
                'Volume': vol_vals,
                'RSI': df['RSI'].values if 'RSI' in df.columns else np.zeros(len(df)),
                'MACD': df['MACD'].values if 'MACD' in df.columns else np.zeros(len(df)),
                'MACD_Sig': df['MACD_Sig'].values if 'MACD_Sig' in df.columns else np.zeros(len(df)),
                'OSC': df['OSC'].values if 'OSC' in df.columns else np.zeros(len(df)),
                'Prev_OSC': df['Prev_OSC'].values if 'Prev_OSC' in df.columns else np.zeros(len(df)),
                'MA5': df['MA5'].values if 'MA5' in df.columns else np.zeros(len(df)),
                'MA20': df['MA20'].values if 'MA20' in df.columns else np.zeros(len(df)),
                'Vol_MA20': vol_ma_vals,
                'ATR': df['ATR'].values if 'ATR' in df.columns else np.zeros(len(df)),
                'ATR_A': df['ATR_A'].values if 'ATR_A' in df.columns else (df['ATR'].values if 'ATR' in df.columns else np.zeros(len(df))),
                'ATR_B': df['ATR_B'].values if 'ATR_B' in df.columns else (df['ATR'].values if 'ATR' in df.columns else np.zeros(len(df))),
                'RSI_Sig': df['RSI_Sig'].values if 'RSI_Sig' in df.columns else np.zeros(len(df)),
                'Prev_RSI': df['Prev_RSI'].values if 'Prev_RSI' in df.columns else np.zeros(len(df)),
                'Market_Status': df['Market_Status'].values if 'Market_Status' in df.columns else np.full(len(df), 'SAFE', dtype=object),
                'Market_Trend': df['Market_Trend'].values if 'Market_Trend' in df.columns else np.full(len(df), 'UP', dtype=object),
                'Market_Regime': df['Market_Regime'].values if 'Market_Regime' in df.columns else np.full(len(df), 'UP', dtype=object),
                'Sig_Type': df['Sig_Type'].values if 'Sig_Type' in df.columns else np.full(len(df), 'TYPE_A', dtype=object),
                'bid_ask_ratio': df['bid_ask_ratio'].values if 'bid_ask_ratio' in df.columns else np.full(len(df), 1.0),
                'ask_bid_ratio': df['ask_bid_ratio'].values if 'ask_bid_ratio' in df.columns else np.full(len(df), 1.0),
                'vol_power': df['vol_power'].values if 'vol_power' in df.columns else np.full(len(df), 100.0),
                'Signal': df['Signal'].values if 'Signal' in df.columns else np.zeros(len(df), dtype=bool),
                # dynamic_backtest 와 동일: 최소 봉 수 이후에만 S1 신호 평가(고정밀 루프에서 Ready 누락 방지)
                "Ready": (np.arange(len(df)) >= 19),
                "S1_Simultaneous_AB": df["S1_Simultaneous_AB"].values
                if "S1_Simultaneous_AB" in df.columns
                else np.zeros(len(df), dtype=bool),
                'MA60': df['MA60'].values if 'MA60' in df.columns else np.zeros(len(df)),
                "Prev_Day_Vol": df["Prev_Day_Vol"].values if "Prev_Day_Vol" in df.columns else np.zeros(len(df)),
                "Prev_Day_Vol_B": df["Prev_Day_Vol_B"].values if "Prev_Day_Vol_B" in df.columns else np.zeros(len(df)),
                "Prev_Day_Vol_S1_A": df["Prev_Day_Vol_S1_A"].values
                if "Prev_Day_Vol_S1_A" in df.columns
                else np.zeros(len(df)),
                "Prev_Day_Vol_S1_B": df["Prev_Day_Vol_S1_B"].values
                if "Prev_Day_Vol_S1_B" in df.columns
                else np.zeros(len(df)),
                "Prev_Day_Vol_S2_A": df["Prev_Day_Vol_S2_A"].values
                if "Prev_Day_Vol_S2_A" in df.columns
                else np.zeros(len(df)),
                "Prev_Day_Vol_S2_B": df["Prev_Day_Vol_S2_B"].values
                if "Prev_Day_Vol_S2_B" in df.columns
                else np.zeros(len(df)),
            }
            
            time_indices = df.index
            # [Perf] Pre-cache hour/minute for gap grace check
            time_hours = time_indices.hour.values
            time_minutes = time_indices.minute.values
            
            m1_np_high = None
            m1_np_close = None
            m1_np_low = None
            m1_np_time = None
            m1_boundaries = None
            if high_precision and m1_df_preloaded is not None:
                # Find the starting index of 1M data for each 15M timestamp
                times_15m = df.index
                m1_times = m1_df_preloaded.index
                # 타임스탬프 창: 일반 [T-15,T), 15:30은 15:15~15:19+15:30 (utils.krx_bars)
                from utils.krx_bars import m1_index_bounds_for_15m_end

                m1_boundaries = [
                    m1_index_bounds_for_15m_end(m1_times, ts) for ts in times_15m
                ]
                # [v4.5.10] Extract VP/BA for 1M Smart Exit simulation
                m1_np_high = m1_df_preloaded['High'].values
                m1_np_close = m1_df_preloaded['Close'].values
                m1_np_low = (
                    m1_df_preloaded['Low'].values
                    if 'Low' in m1_df_preloaded.columns
                    else m1_df_preloaded['Close'].values
                )
                m1_np_time = m1_df_preloaded.index.values
                # Bid/Ask Ratio mapping (v4.5: Ensure column names match data_loader)
                m1_np_ba = m1_df_preloaded['total_bid'].values / m1_df_preloaded['total_ask'].values if 'total_bid' in m1_df_preloaded.columns else np.full(len(m1_df_preloaded), 1.0)
                m1_np_vp = m1_df_preloaded['vol_power'].values if 'vol_power' in m1_df_preloaded.columns else np.full(len(m1_df_preloaded), 100.0)
                # 고정밀 인과 S1: 1분 거래량 누적(없으면 15분 거래량을 구간 길이로 비례 배분)
                m1_np_vol = (
                    m1_df_preloaded["Volume"].values.astype("float64", copy=False)
                    if "Volume" in m1_df_preloaded.columns
                    else None
                )
            else:
                m1_boundaries = None
                m1_np_ba = None
                m1_np_vp = None
                m1_np_vol = None
            
            # 신호 및 필터 미리 정리
            signal_shifted = df['Signal_Shifted'].values if 'Signal_Shifted' in df.columns else np.zeros(len(df), dtype=bool)
            
            total_bars = len(df)
            # [v35.5] High-Performance Simulation Step (Zero Dict Creation)
            # Initialize equity_curve[0] outside the loop
            params['equity_curve'][0] = params['balance']
            equity_curve = params['equity_curve'] # Reference to modify directly
            
            # [v35.6] Ultra-Performance Unpacked Loop
            # Dictionary lookups are slow in million-bar loops. Unpack arrays to local variables once.
            ind_signal = np_indicators['Signal']
            ind_sig_type = np_indicators['Sig_Type']
            ind_ba_ratio = np_indicators['bid_ask_ratio']
            ind_vp = np_indicators['vol_power']
            ind_rsi = np_indicators['RSI']
            
            # Unpack hot parameters for simulation step to avoid repeated dict/attr access
            pos = params['position']
            balance = params['balance']
            
            for i in range(1, total_bars):
                m1_info = (
                    (
                        m1_np_high,
                        m1_np_close,
                        m1_np_low,
                        m1_np_time,
                        m1_boundaries[i],
                        m1_np_ba,
                        m1_np_vp,
                        m1_np_vol,
                    )
                    if m1_boundaries
                    else None
                )
                
                # Update loop locals
                params['position'] = pos
                params['balance'] = balance
                
                # [v35.6] Optimized Step
                params = self._sim_loop_step_v3(
                    i, close_vals, high_vals, low_vals, time_indices, time_hours, time_minutes, 
                    signal_shifted, np_indicators, code, m1_info, high_precision, params, log_to_csv
                )
                
                pos = params['position']
                balance = params['balance']
                equity_curve[i] = balance + (pos['qty'] * close_vals[i] if pos else 0)
                if not params['position'] and i > 0 and params['balance'] != params['initial_balance']:
                    # [v27.0] Slot Budget Protection: If all sold and net profit exists
                    pass 

            # 3. Finalize and Return Results
            return self._finalize_simulation(df, code, params, log_to_csv)

        except Exception as e:
            print(f"  [Error] Simulation failed for {code}: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _init_simulation(self, df, code, market_df, vix_df, high_precision):
        """시뮬레이션 초기 설정 및 시장 상태 계산"""
        total_capital_val = float(self.settings.get('backtest_initial_capital', 30000000))
        # [v4.5 Design Update] 슬롯별 예산 제한 폐지. 각 종목은 전체 가용 자산을 기준으로 시뮬레이션됨.
        slot_budget = total_capital_val
        
        # 포지션 사이징: 총자산 대비 %만 사용
        s1_pct = float(self.settings.get('s1_equity_pct', 20.0))
        self.s1_amt = total_capital_val * (s1_pct / 100.0)
        s2_pct = float(self.settings.get('s2_equity_pct', 30.0))
        self.s2_amt = total_capital_val * (s2_pct / 100.0)

        # [Phase 2] Type B S2 사이징 (별도 비중)
        s2_b_pct = float(self.settings.get('s2_b_equity_pct', s2_pct))
        self.s2_b_amt = total_capital_val * (s2_b_pct / 100.0)

        self.buy_f_rate, self.sell_f_rate = fee_decimal_rates_backtest(self.settings)
        
        idx_choice = self.settings.get("universe_index_choice", "KODEX200")
        stock_name_map = TradingStrategy.get_stock_name_map(idx_choice)
        stock_name = stock_name_map.get(code, code)

        # [Perf] Cache Smart Filter & Exit settings to avoid repeated dictionary lookups in hot loops
        s = self.settings
        self.use_sb_a = s.get("use_smart_buy_a", False)  # Type A SB UI 비노출 — defaults와 동일하게 기본 False
        self.use_sb_b = s.get('use_smart_buy_b', True)
        self.use_sb_s2_a = s.get("use_smart_buy_s2", True)
        self.use_sb_s2_b = s.get("use_smart_buy_s2_b", True)
        self.sb_ba = float(s.get('smart_buy_ba_ratio', 1.2))
        self.sb_vp = float(s.get('smart_buy_vp', 110.0))
        
        self.use_sx = s.get('use_smart_exit', True) # [v4.5 Sync] Default True
        self.sx_ba = float(s.get('smart_exit_ba_ratio', 1.3)) # [v4.5 Sync] UI/SignalEngine Default 1.3
        self.sx_vp = float(s.get('smart_exit_vp', 90.0))
        # 고점 대비 하락 %(양수). evaluate_position 과 동일하게 절댓값만 캐시(미사용·진단용).
        self.sx_p_thr = abs(float(s.get("smart_exit_price_threshold", 1.0)))
        
        # [Perf] Cache TP/SL parameters (v33.6)
        self.use_s1a_tp_active = s.get('use_s1_a_tp', True)
        self.use_s1b_tp_active = s.get('use_s1_b_tp', True)
        self.s1a_tp = float(s.get('s1_a_take_profit', 5.0)) / 100.0
        self.s1b_tp = float(s.get('s1_b_take_profit', 5.0)) / 100.0
        self.s2_tp = float(s.get('s2_take_profit', 5.0)) / 100.0
        
        self.use_s1a_sl_active = s.get('use_s1_a_stop_loss', True)
        self.use_s1b_sl_active = s.get('use_s1_b_stop_loss', True)
        self.use_s2_sl = s.get('use_s2_stop_loss', True) # [v35.3] Added missing S2 SL flag
        self.s1a_sl = float(s.get('s1_a_stop_loss', 3.0)) / 100.0
        self.s1b_sl = float(s.get('s1_b_stop_loss', 3.0)) / 100.0
        self.s2_sl = float(s.get('s2_stop_loss', 2.0)) / 100.0
        
        self.use_type_b = s.get('use_type_b', False)
        
        # [Perf] Cache ATR & Gap (트랙별 토글 OR — 핫루프는 evaluate_position이 설정 전체 참조)
        self.use_atr = bool(
            s.get("use_atr_exit_a", s.get("use_atr_exit", False))
            or s.get("use_atr_exit_b", s.get("use_atr_exit", False))
        )
        
        self.use_atr_sl_a = s.get("use_atr_stop_loss_a", False)
        self.use_atr_sl_b = s.get("use_atr_stop_loss_b", False)
        self.use_atr_sl_s2 = s.get("use_atr_stop_loss_s2", True)
        
        self.use_atr_trail_a = s.get('use_atr_trailing_stop_a', True)
        self.use_atr_trail_b = s.get('use_atr_trailing_stop_b', True)
        self.use_atr_trail_s2 = s.get('use_atr_trailing_stop_s2', True)
        
        self.atr_sl_mult_a = float(s.get('atr_multiplier_sl_a', 2.5))
        self.atr_sl_mult_b = float(s.get('atr_multiplier_sl_b', 2.5))
        self.atr_sl_mult_s2 = float(s.get('atr_multiplier_sl_s2', 2.5))
        
        self.atr_trail_mult_a = float(s.get('atr_multiplier_trail_a', 4.0))
        self.atr_trail_mult_b = float(s.get('atr_multiplier_trail_b', 4.0))
        self.atr_trail_mult_s2 = float(s.get('atr_multiplier_trail_s2', 4.0))
        
        self.gap_down_limit = float(s.get("gap_down_limit_a", s.get("gap_down_limit", -5.0)))
        self.use_gap_down_exit = s.get("use_gap_down_exit_a", s.get("use_gap_down_exit", True))
        self.use_s2_tp_active = s.get('use_s2_tp', True)
        self.use_gap_grace = s.get("use_gap_grace_a", s.get("use_gap_grace", True))
        
        # [Perf] Cache Flash Exit settings
        self.use_s1a_flash_active = s.get('use_s1_a_flash_exit', True)
        self.use_s1b_flash_active = s.get('use_s1_b_flash_exit', True)
        self.use_s2_flash = s.get('use_s2_flash_exit', True)
        self.s1a_flash = float(s.get('s1_a_flash_exit_pct', 3.0))
        self.s1b_flash = float(s.get('s1_b_flash_exit_pct', 3.0))
        self.s2_flash = float(s.get('s2_flash_exit_pct', 3.0))
        
        # [Perf] Cache S2 Approval settings
        self.use_s2_delay = s.get('use_s2_delay', True)
        self.s2_delay_h = float(s.get('s2_delay_hours', 24))
        self.use_s2_ma20 = s.get('use_s2_ma20', True)
        self.s2_ma20_g = float(s.get('s2_ma20_gap', 0.0))
        self.use_s2_ma5 = s.get('use_s2_ma5', True)
        self.s2_ma5_g = float(s.get('s2_ma5_gap', -0.5))
        self.use_s2_vol = s.get('use_s2_vol_surge', True)
        self.s2_vol_r = float(s.get('s2_vol_ratio', 1.5)) # Default sync
        self.use_s2_rsi = s.get('use_s2_rsi_sig', True)
        self.s2_rsi_g = float(s.get('s2_rsi_gap', 0.0))
        self.use_s2_macd = s.get('use_s2_macd_gc', True)
        self.s2_macd_g = float(s.get('s2_macd_gap', 0.0))
        self.use_s2_profit = s.get('use_s2_profit', True)
        self.s2_trigger = float(s.get('s2_profit_limit', 1.0))
        
        # 시장 지표/레짐 비사용 — 호환 열은 항상 UP(허용)로 고정 (market_df/vix_df 인자는 무시)
        df['Market_Status'] = 'SAFE'
        df['Market_Trend'] = 'UP'
        df['Market_Regime'] = 'UP'

        # [v26.1] 신호 미리 계산
        sigs = TradingStrategy.get_buy_signals_detailed(df, self.settings, market_trend="UP")
        for c in sigs.columns:
            df[c] = sigs[c]

        # [v30.0] 로직 변경: 신호 발생 즉시 매수 (지연 없음)
        df['Signal_Shifted'] = df['Signal'].astype(bool)
        df['Sig_Type_Shifted'] = df['Sig_Type']

        self.trades = []
        return {
            'code': code,
            'balance': slot_budget,
            'initial_balance': slot_budget,
            'equity_curve': [slot_budget] * len(df),
            'position': None,
            'history': [],
            'signal_logs': [],
            'm1_checks': 0,
            'stock_name': stock_name
        }

    def _check_s2_approval_v2(self, pos, indicators, i, curr_dt=None):
        return TradingStrategy._check_s2_approval(
            pos, None, self.settings,
            market_trend="UP",
            curr_dt=curr_dt,
            indicators_dict=indicators, i=i
        )

    def _check_s2_approval_legacy(self, pos, indicators, i):
        """[v35.5] Ultra-Fast 2nd Buy approval (Direct Indexing, No Dict Creation)"""
        if self.use_s2_ma20:
            ma20 = indicators['MA20'][i]
            if ma20 == 0 or indicators['Close'][i] < ma20 * (1 + self.s2_ma20_g/100.0): return False
        if self.use_s2_ma5:
            ma5 = indicators['MA5'][i]
            if ma5 > 0 and indicators['Close'][i] < ma5 * (1 + self.s2_ma5_g/100.0): return False
        if self.use_s2_vol:
            _pv = indicators.get("Prev_Day_Vol_S2_A")
            if _pv is None:
                _pv = indicators.get("Prev_Day_Vol")
            if _pv is None or float(_pv[i]) <= 0:
                return False
            if indicators["Volume"][i] < float(_pv[i]) * self.s2_vol_r:
                return False
        if self.use_s2_rsi:
            rsi = indicators['RSI'][i]
            if rsi < indicators['RSI_Sig'][i] * (1 + self.s2_rsi_g/100.0) or rsi <= indicators['Prev_RSI'][i]: return False
        if self.use_s2_macd:
            macd = indicators['MACD'][i]
            osc = indicators['OSC'][i]
            if macd < indicators['MACD_Sig'][i] + self.s2_macd_g or osc <= 0 or osc <= indicators['Prev_OSC'][i]: return False
        if self.use_s2_profit:
            if (indicators['Close'][i] / pos['avg_price'] - 1) * 100 < self.s2_trigger: return False
        return True

    def _hp_hist_slice_for_causal_s1(self, i: int) -> Optional[pd.DataFrame]:
        """
        인과적 고정밀 S1용: 현재 15분 인덱스 i 직전까지의 닫힌 15분봉만 잘라 반환합니다.
        lookback 은 설정 ``high_precision_causal_lookback_bars``(기본 400)과 신호 최소봉 중 큰 값으로 캡합니다.
        """
        try:
            if i <= 0:
                return None
            try:
                from core.defaults import effective_min_15m_bars_for_monitoring

                need = int(effective_min_15m_bars_for_monitoring(self.settings))
            except Exception:
                need = 60
            try:
                lb = int(float(self.settings.get("high_precision_causal_lookback_bars", 400)))
            except (TypeError, ValueError):
                lb = 400
            lb = max(max(need + 10, 120), min(lb, 2500))
            start = max(0, i - lb)
            return self.df.iloc[start:i].copy()
        except Exception:
            return None

    def _hp_causal_s1_buy_at_k(
        self,
        i: int,
        k: int,
        m1_highs: Any,
        m1_closes: Any,
        m1_lows: Any,
        m1_vols: Any,
        hist_slice: pd.DataFrame,
    ) -> tuple[bool, str]:
        """
        1~k분 누적 OHLCV 로 마지막 가상 15분봉을 만들고, 닫힌 봉 히스토리와 합쳐 지표·S1 신호를 재계산합니다.
        (완성된 i번째 봉 전체를 미리 쓰는 기존 고정밀 S1 경로의 미래참조를 제거)
        """
        try:
            if k <= 0 or hist_slice is None or getattr(hist_slice, "empty", True):
                return False, "TYPE_B"
            seg_open = float(self.df.iloc[i]["Open"])
            h_seg = float(np.max(np.asarray(m1_highs[:k], dtype=float)))
            l_seg = float(np.min(np.asarray(m1_lows[:k], dtype=float)))
            c_seg = float(m1_closes[k - 1])
            nm = max(1, int(len(m1_highs)))
            if m1_vols is not None and len(m1_vols) >= k:
                v_seg = float(np.sum(np.asarray(m1_vols[:k], dtype=float)))
            else:
                v15 = float(self.df.iloc[i].get("Volume", 0) or 0)
                v_seg = v15 * float(k) / float(nm)
            virtual = self.df.iloc[[i]].copy()
            ix0 = virtual.index[0]
            virtual.loc[ix0, "Open"] = seg_open
            virtual.loc[ix0, "High"] = h_seg
            virtual.loc[ix0, "Low"] = l_seg
            virtual.loc[ix0, "Close"] = c_seg
            virtual.loc[ix0, "Volume"] = max(0.0, v_seg)
            combined = pd.concat([hist_slice, virtual], axis=0)
            combined = TradingStrategy.calculate_indicators(combined, self.settings)
            sigd = TradingStrategy.get_buy_signals_detailed(combined, self.settings, "UP")
            last_sig = bool(sigd.iloc[-1]["Signal"])
            try:
                ready_min = max(
                    1, int(float(self.settings.get("hp_causal_ready_min_bar_index", 19)))
                )
            except (TypeError, ValueError):
                ready_min = 19
            ready_ok = int(i) >= int(ready_min)
            sig_type = str(sigd.iloc[-1].get("Sig_Type", "TYPE_B"))
            return bool(last_sig and ready_ok), sig_type
        except Exception as exc:
            try:
                print(f"[HighPrec] 인과 S1 재계산 실패 (i={i}, k={k}): {exc}")
            except Exception:
                pass
            return False, "TYPE_B"

    def _process_intra_bar_full_v3(
        self,
        i,
        code,
        m1_highs,
        m1_closes,
        m1_lows,
        m1_times,
        params,
        slot_ts,
        m1_ba=None,
        m1_vp=None,
        m1_vols=None,
        indicators=None,
        signal_shifted=False,
    ):
        """
        고정밀 1분 시뮬.
        - 보유 중: 기존과 동일하게 인트라바 지표 + 청산·S2(스마트바이 설정 따름).
        - 미보유 S1: 15분 벡터 신호를 그대로 덮어쓰지 않고, 1~k분 누적 가상 15분봉으로
          ``calculate_indicators`` + ``get_buy_signals_detailed`` 를 다시 돌려 인과적으로 진입 시점만 잡음.
        """
        # 종가 단일가 슬롯: 기본 S1·S2·청산 스킵. next_open 허용 시 평가(체결은 호출측).
        try:
            from utils.krx_bars import (
                is_krx_auction_market_fill_slot,
                is_krx_last_15m_slot,
            )
            from strategy.signals import settings_allow_1530_signal_eval

            _skip_eod_sig = bool(is_krx_last_15m_slot(slot_ts))
            if _skip_eod_sig and settings_allow_1530_signal_eval(self.settings):
                _skip_eod_sig = False
            _auction_1515 = bool(is_krx_auction_market_fill_slot(slot_ts))
        except Exception:
            _skip_eod_sig = False
            _auction_1515 = False
        if _skip_eod_sig or _auction_1515:
            signal_shifted = False
            try:
                pos = params.get("position")
                if pos is not None and m1_highs is not None and len(m1_highs) > 0:
                    try:
                        hi = float(np.nanmax(np.asarray(m1_highs, dtype=float)))
                        self._bump_pos_max_from_high(pos, hi)
                    except Exception:
                        pass
            except Exception:
                pass
            return params

        # [보수적 1분 체결] 매도/EXIT 판가: 저가(m1_l), 매수 체결: 고가(m1_h) — 시뮬·지표 경로는 종가(m1_p) 유지
        # [Perf] Pre-calculated tail data for incremental indicator updates
        # Using index i-1 as the base for the current 15m bar simulation
        base_idx = max(0, i - 1)
        last_15m_dict = {k: v[base_idx] for k, v in indicators.items()}

        prices_tail = {
            5: self.df['Close'].iloc[max(0, i-4):i].sum(),
            20: self.df['Close'].iloc[max(0, i-19):i].sum(),
            60: self.df['Close'].iloc[max(0, i-59):i].sum(),
            'rsi_vals': self.df['Close'].iloc[max(0, i-15):i].values
        }

        hist_slice_s1: Optional[pd.DataFrame] = None
        try:
            if params.get("position") is None and signal_shifted:
                hist_slice_s1 = self._hp_hist_slice_for_causal_s1(i)
        except Exception:
            hist_slice_s1 = None

        for idx in range(len(m1_highs)):
            m1_p = m1_closes[idx]
            m1_h = m1_highs[idx]
            m1_l = m1_lows[idx] if idx < len(m1_lows) else m1_p
            m1_t = m1_times[idx]
            m1_ba_val = m1_ba[idx] if m1_ba is not None else 1.0
            m1_vp_val = m1_vp[idx] if m1_vp is not None else 100.0
            
            # [v40.0] Incremental Indicator Simulation
            m1_row_data = {'Close': m1_p, 'High': m1_h, 'Low': m1_l, 'bid_ask_ratio': m1_ba_val, 'vol_power': m1_vp_val}
            indicator_to_use = TradingStrategy.simulate_intrabar_indicators(last_15m_dict, m1_row_data, self.settings, hist_df_prices_tail=prices_tail)
            # 베이스는 직전 15m(i-1) 종가 상태라 Signal 등이 한 봉 밀릴 수 있음. 현재 봉 i의 사전계산 신호로 덮어씀.
            if indicator_to_use is not None and indicators is not None:
                try:
                    for _k in ("Signal", "Ready", "Sig_Type", "S1_Simultaneous_AB"):
                        if _k in indicators:
                            indicator_to_use[_k] = indicators[_k][i]
                except (KeyError, IndexError, TypeError, ValueError):
                    pass

            if indicator_to_use is None:
                indicator_to_use = {k: v[i] for k, v in indicators.items()}

            # 1. 매도(Exit) 및 추가매수(Addon) 감시 (보유 중인 경우)
            if params['position'] is not None:
                pos = params['position']
                self._bump_pos_max_from_high(pos, m1_h)
                
                try:
                    _lo = float(m1_l)
                    px_exit = _lo if (np.isfinite(_lo) and _lo > 0) else float(m1_p)
                except (TypeError, ValueError):
                    px_exit = float(m1_p)
                # 1m 저가 < 동일 15m 행 저가 인 경우(소스/구간 엇갈림) — 15m Low 를 하한으로
                try:
                    if indicators is not None and "Low" in indicators:
                        _lo_15m = float(indicators["Low"][i])
                        if (
                            np.isfinite(_lo_15m)
                            and _lo_15m > 0
                            and float(px_exit) < float(_lo_15m)
                        ):
                            px_exit = _lo_15m
                except (TypeError, ValueError, KeyError, IndexError):
                    pass
                res = TradingStrategy.evaluate_position(
                    code, px_exit, pos, self.settings, indicator_row=indicator_to_use, current_time=m1_t
                )
                
                if res['action'] == 'EXIT':
                    e_price = px_exit
                    params = self._execute_sell(m1_t, e_price, f"1M_{res['reason']}+{idx}m", params, indicators=indicators, i=i)
                    return params # 매도 발생 시 블록 종료
                
                elif pos['stage'] == 1:
                    # [v36.9] 시뮬레이션 지표 기반 S2 Approval 체크
                    if TradingStrategy._check_s2_approval(pos, None, self.settings, indicator_row=indicator_to_use, market_trend=params.get('market_trend', 'UP')):
                        can_buy_s2 = True
                        _use_s2_sb = self.use_sb_s2_b if pos.get("sig_type") == "TYPE_B" else self.use_sb_s2_a
                        if _use_s2_sb:
                            ab_ratio = 1.0 / (m1_ba_val + 1e-10)
                            if ab_ratio < self.sb_ba or m1_vp_val < self.sb_vp:
                                can_buy_s2 = False
                            
                        if can_buy_s2:
                            s2_reason = f"1M_ADDON(S2)|R:{indicator_to_use.get('RSI',0):.1f}|O:{indicator_to_use.get('OSC',0):.2f}+{idx}m"
                            if _use_s2_sb:
                                s2_reason = "1M_SmartBuy|" + s2_reason
                            try:
                                _hi = float(m1_h)
                                px_in = _hi if (np.isfinite(_hi) and _hi > 0) else float(m1_p)
                            except (TypeError, ValueError):
                                px_in = float(m1_p)
                            params = self._execute_buy(
                                m1_t, px_in, "BUY_2ND", params, stage=2, reason=s2_reason,
                                indicators=indicators, i=i, max_p=px_in
                            )
                            return params # 추가매수 시 해당 분 사이클 종료
            
            # 2. 신규 진입(Entry) 감시 — 인과적 가상 15분봉(1~k분 누적)으로 S1만 재판정
            else:
                if not signal_shifted or hist_slice_s1 is None or getattr(
                    hist_slice_s1, "empty", True
                ):
                    continue
                k = int(idx) + 1
                try:
                    is_causal, sig_type = self._hp_causal_s1_buy_at_k(
                        i,
                        k,
                        m1_highs,
                        m1_closes,
                        m1_lows,
                        m1_vols,
                        hist_slice_s1,
                    )
                except Exception:
                    is_causal, sig_type = False, "TYPE_B"
                if is_causal:
                    use_sb = self.use_sb_a if sig_type == "TYPE_A" else self.use_sb_b
                    can_buy = True
                    if use_sb:
                        ab_ratio = 1.0 / (m1_ba_val + 1e-10)
                        if ab_ratio < self.sb_ba or m1_vp_val < self.sb_vp:
                            can_buy = False
                    if can_buy:
                        b_reason = (
                            f"1M_CAUSAL_S1|k={k}|C:{m1_p:.2f}|H:{m1_h:.0f}|L:{m1_l:.0f}"
                        )
                        if use_sb:
                            b_reason = "1M_SmartBuy|" + b_reason
                        try:
                            _hi2 = float(m1_h)
                            px_in = (
                                _hi2
                                if (np.isfinite(_hi2) and _hi2 > 0)
                                else float(m1_p)
                            )
                        except (TypeError, ValueError):
                            px_in = float(m1_p)
                        params = self._execute_buy(
                            m1_t,
                            px_in,
                            "BUY_1ST",
                            params,
                            stage=1,
                            reason=b_reason,
                            indicators=indicators,
                            i=i,
                            max_p=px_in,
                            sig_type=sig_type,
                        )
                        return params

        return params

    def _sim_loop_step_v3(self, i, close_vals, high_vals, low_vals, time_indices, time_hours, time_minutes, signal_shifted, indicators, code, m1_info, high_precision, params, log_to_csv=False):
        """[v35.5] Ultra-Fast Simulation Step (Direct Indexing, No Dict Creation)"""
        curr_close = close_vals[i]
        curr_date = time_indices[i]
        curr_high = high_vals[i]
        curr_low = low_vals[i]

        # 종가 단일가 — 기본 스킵(RT 완성봉 평가 스킵과 정합). next_open 허용 시 평가.
        try:
            from utils.krx_bars import (
                is_krx_auction_market_fill_slot,
                is_krx_last_15m_slot,
            )
            from strategy.signals import settings_allow_1530_signal_eval

            _skip_eod_sig = bool(is_krx_last_15m_slot(curr_date))
            if _skip_eod_sig and settings_allow_1530_signal_eval(self.settings):
                _skip_eod_sig = False
            _auction_1515 = bool(is_krx_auction_market_fill_slot(curr_date))
        except Exception:
            _skip_eod_sig = False
            _auction_1515 = False
        if _skip_eod_sig:
            try:
                pos = params.get("position")
                if pos is not None:
                    if high_precision and m1_info is not None:
                        (
                            m1_high_total,
                            _mc,
                            _ml,
                            _mt,
                            (s_idx, e_idx),
                            _ba,
                            _vp,
                            _mv,
                        ) = m1_info
                        if e_idx > s_idx:
                            try:
                                hi = float(
                                    np.nanmax(
                                        np.asarray(
                                            m1_high_total[s_idx:e_idx], dtype=float
                                        )
                                    )
                                )
                                self._bump_pos_max_from_high(pos, hi)
                            except Exception:
                                pass
                    else:
                        self._bump_pos_max_from_high(pos, curr_high)
            except Exception:
                pass
            return params

        # 15:15 — 인트라바 없이 완성봉 평가 → 당일 15:30 종가 체결
        if _auction_1515:
            return self._sim_loop_step_auction_1515(
                i,
                close_vals,
                high_vals,
                time_indices,
                indicators,
                signal_shifted,
                params,
            )
        
        # 1. High Precision Entry/Exit/Addon Monitoring
        if high_precision and m1_info is not None:
            (
                m1_high_total,
                m1_close_total,
                m1_low_total,
                m1_time_total,
                (s_idx, e_idx),
                m1_ba_total,
                m1_vp_total,
                m1_vol_total,
            ) = m1_info
            if e_idx > s_idx:
                m1_highs = m1_high_total[s_idx:e_idx]
                m1_closes = m1_close_total[s_idx:e_idx]
                m1_lows = m1_low_total[s_idx:e_idx]
                m1_ba = m1_ba_total[s_idx:e_idx] if m1_ba_total is not None else None
                m1_vp = m1_vp_total[s_idx:e_idx] if m1_vp_total is not None else None
                m1_vols = (
                    m1_vol_total[s_idx:e_idx]
                    if m1_vol_total is not None
                    else None
                )

                # Check if we should process high-precision (Position exists OR Signal exists)
                if params['position'] is not None or signal_shifted[i]:
                    old_pos = params['position']
                    params = self._process_intra_bar_full_v3(
                        i, code, m1_highs, m1_closes, m1_lows, m1_time_total[s_idx:e_idx],
                        params, curr_date,
                        m1_ba=m1_ba, m1_vp=m1_vp, m1_vols=m1_vols, indicators=indicators,
                        signal_shifted=signal_shifted[i]
                    )
                    # If position status changed or a trade happened, we might skip standard logic
                    # For simplicity, if we processed intra-bar, we skip standard logic for this 15m block
                    return params

        # 2. Standard Sim Logic
        if params['position'] is not None:
            pos = params['position']
            self._bump_pos_max_from_high(pos, curr_high)
            
            # Exit Check
            exit_res = self._check_exits_optimized(curr_date, curr_close, curr_high, curr_low, pos, indicators=indicators, i=i)
            if exit_res:
                params = self._execute_sell(
                    curr_date,
                    float(exit_res["price"]),
                    exit_res["reason"],
                    params,
                    indicators=indicators,
                    i=i,
                )
            
            # Stage 2 Buy Check
            elif pos['stage'] == 1:
                if self._check_s2_approval_v2(pos, indicators, i, curr_dt=time_indices[i]):
                    can_buy_s2 = True
                    _use_s2_sb = self.use_sb_s2_b if pos.get("sig_type") == "TYPE_B" else self.use_sb_s2_a
                    if _use_s2_sb:
                        ab_ratio = indicators['ask_bid_ratio'][i]
                        v_power = indicators['vol_power'][i]
                        if ab_ratio < self.sb_ba or v_power < self.sb_vp:
                            can_buy_s2 = False
                    
                    if can_buy_s2:
                        s2_reason = f"ADDON(S2)|R:{indicators['RSI'][i]:.1f}|M:{indicators['MACD'][i]:.1f}|O:{indicators['OSC'][i]:.2f}|V:{indicators['Volume'][i]}"
                        if _use_s2_sb:
                            s2_reason = "SmartBuy|" + s2_reason
                        params = self._execute_buy(
                            curr_date,
                            float(curr_close),
                            "BUY_2ND",
                            params,
                            stage=2,
                            reason=s2_reason,
                            indicators=indicators,
                            i=i,
                        )
        
        # 3. Entry Check
        elif signal_shifted[i]:
            sig_type = indicators['Sig_Type'][i]
            is_ok = True
            if is_ok:
                use_sb_for_type = self.use_sb_a if sig_type == "TYPE_A" else self.use_sb_b
                if use_sb_for_type:
                    ab_ratio = indicators['ask_bid_ratio'][i]
                    v_power = indicators['vol_power'][i]
                    # [v4.4] S1 진입 정석: 매도벽(Ask) 두터울 때
                    if ab_ratio < self.sb_ba or v_power < self.sb_vp:
                        is_ok = False
                
                if is_ok:
                    reason = "SmartBuy|SIG" if use_sb_for_type else "SIG"
                    params = self._execute_buy(
                        curr_date,
                        float(curr_close),
                        "BUY_1ST",
                        params,
                        stage=1,
                        entry_idx=i,
                        max_p=self._entry_max_p(float(curr_close), curr_high),
                        reason=reason,
                        indicators=indicators,
                        i=i,
                        sig_type=sig_type,
                    )
        
        if log_to_csv and not self.settings.get('skip_indicators', False):
            if indicators['Signal'][i] or (indicators['RSI'][i] > 30 and indicators['RSI'][i] < 80):
                self._log_signal(curr_date, code, params['stock_name'], curr_close, indicators, i, indicators['Market_Trend'][i], params)
        return params

    def _check_exits_optimized(self, date, price, high, low, pos, indicators=None, i=None):
        """[v35.8] Unified Exit check using TradingStrategy.evaluate_position"""
        ind_row = None
        if indicators is not None and i is not None:
            try:
                ind_row = {k: v[i] for k, v in indicators.items()}
            except: pass
            
        res = TradingStrategy.evaluate_position(
            None, price, pos, self.settings,
            current_time=date, is_realtime=False,
            indicators_dict=indicators, i=i,
            time_is_bar_label=True
        )
        
        if res['action'] == 'EXIT':
            e_price = price
            # TP 가격 보정: evaluate_position과 동일 % (entry_n/stage 불일치·키 재조회 오류 방지)
            if res['reason'] == 'TAKE_PROFIT':
                tpp = res.get('tp_pct_applied')
                if tpp is not None:
                    avg_u = float(pos.get('avg_p', pos.get('avg_price', price)))
                    e_price = avg_u * (1 + float(tpp) / 100.0)
                else:
                    stage_eff = int(pos.get('entry_n', pos.get('stage', 1)) or 1)
                    _is_b = TradingStrategy._holding_sig_is_type_b(pos)
                    if stage_eff == 2:
                        tp_key = 's2_b_take_profit' if _is_b else 's2_take_profit'
                    else:
                        tp_key = 's1_b_take_profit' if _is_b else 's1_a_take_profit'
                    tp_pct = float(self.settings.get(tp_key, 5.0))
                    e_price = float(pos.get('avg_p', pos.get('avg_price', price))) * (1 + tp_pct / 100.0)
            
            return {'price': e_price, 'reason': res['reason']}
            
        return None
        
        return None

    def _check_exits(self, date, price, high, low, pos, indicators=None, i=None):
        return self._check_exits_optimized(date, price, high, low, pos, indicators=indicators, i=i)


    def _log_signal(self, date, code, name, price, indicators, i, mkt_trend, params):
        """신호 로그 기록 (SIGNAL_CSV 용 데이터 수집)"""
        params['signal_logs'].append({
            'Date': date, 'Code': code, 'Name': name,
            'Price': price, 'RSI': indicators['RSI'][i], 'MACD': indicators['MACD'][i],
            'RSI_Range': (indicators['RSI'][i] >= self.settings.get('buy_rsi_min', 40) and indicators['RSI'][i] <= self.settings.get('buy_rsi_max_b', 70)),
            'MACD_OK': (indicators['MACD'][i] >= indicators['MACD_Sig'][i]),
            'MA_GC': (indicators['MA5'][i] > indicators['MA20'][i]),
            'MA60_Range': (price / indicators['MA60'][i] >= 1.0 and price / indicators['MA60'][i] <= 1.12),
            'Vol_Surge': (indicators['Volume'][i] > indicators['Vol_MA20'][i] * 1.5),
            'Market_Trend': mkt_trend,
            'Final_Signal': indicators['Signal'][i]
        })
    
        return params


    def _lookup_same_day_1530_close(
        self, curr_date: Any
    ) -> Optional[Tuple[Any, float]]:
        """당일 종가 단일가(SessionHours.close) Close. 없으면 None."""
        try:
            from utils.krx_bars import krx_same_day_1530_label
            from utils.krx_session_hours import get_session_hours

            lab = krx_same_day_1530_label(curr_date)
            if lab is None:
                return None
            close_hm = get_session_hours(pd.Timestamp(curr_date).date()).close_minutes
        except Exception:
            return None
        df = getattr(self, "df", None)
        if df is None or getattr(df, "empty", True):
            return None
        try:
            if lab in df.index:
                c = float(df.loc[lab, "Close"])
                if c > 0:
                    return lab, c
        except Exception:
            pass
        try:
            lab_n = pd.Timestamp(lab).normalize()
            for idx in df.index:
                try:
                    t = pd.Timestamp(idx)
                    if (
                        t.normalize() == lab_n
                        and int(t.hour) * 60 + int(t.minute) == close_hm
                    ):
                        c = float(df.loc[idx, "Close"])
                        if c > 0:
                            return t, c
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _tag_auction_reason(self, reason: Any) -> str:
        try:
            r = str(reason or "")
        except Exception:
            r = ""
        if r.startswith("AUCTION_MKT|"):
            return r
        return f"AUCTION_MKT|src=15:15|{r}"

    def _resolve_auction_market_fill(
        self, curr_date: Any, price: float, reason: Any
    ) -> Optional[Tuple[float, Any, str]]:
        """15:15 → 15:30 Close·시각. Close 없으면 None."""
        try:
            from utils.krx_bars import is_krx_auction_market_fill_slot

            if not is_krx_auction_market_fill_slot(curr_date):
                return float(price), curr_date, str(reason or "")
        except Exception:
            return float(price), curr_date, str(reason or "")
        got = self._lookup_same_day_1530_close(curr_date)
        if got is None:
            return None
        lab, close = got
        return float(close), lab, self._tag_auction_reason(reason)

    def _sim_loop_step_auction_1515(
        self,
        i: int,
        close_vals: Any,
        high_vals: Any,
        time_indices: Any,
        indicators: Any,
        signal_shifted: Any,
        params: dict,
    ) -> dict:
        """완성 15:15 평가 후 당일 15:30 종가로 1회 체결(인트라바 없음)."""
        try:
            curr_close = float(close_vals[i])
            curr_date = time_indices[i]
            curr_high = float(high_vals[i])
        except Exception:
            return params

        if params.get("position") is not None:
            pos = params["position"]
            self._bump_pos_max_from_high(pos, curr_high)

            exit_res = self._check_exits_optimized(
                curr_date,
                curr_close,
                curr_high,
                curr_close,
                pos,
                indicators=indicators,
                i=i,
            )
            if exit_res:
                fill = self._resolve_auction_market_fill(
                    curr_date, float(exit_res["price"]), exit_res["reason"]
                )
                if fill is None:
                    return params
                px, fill_ts, rsn = fill
                return self._execute_sell(
                    fill_ts, px, rsn, params, indicators=indicators, i=i
                )

            if int(pos.get("stage", 1) or 1) == 1:
                if self._check_s2_approval_v2(
                    pos, indicators, i, curr_dt=time_indices[i]
                ):
                    can_buy_s2 = True
                    _use_s2_sb = (
                        self.use_sb_s2_b
                        if pos.get("sig_type") == "TYPE_B"
                        else self.use_sb_s2_a
                    )
                    if _use_s2_sb:
                        try:
                            ab_ratio = indicators["ask_bid_ratio"][i]
                            v_power = indicators["vol_power"][i]
                            if ab_ratio < self.sb_ba or v_power < self.sb_vp:
                                can_buy_s2 = False
                        except Exception:
                            can_buy_s2 = False
                    if can_buy_s2:
                        s2_reason = (
                            f"ADDON(S2)|R:{indicators['RSI'][i]:.1f}|"
                            f"M:{indicators['MACD'][i]:.1f}|"
                            f"O:{indicators['OSC'][i]:.2f}|"
                            f"V:{indicators['Volume'][i]}"
                        )
                        if _use_s2_sb:
                            s2_reason = "SmartBuy|" + s2_reason
                        fill = self._resolve_auction_market_fill(
                            curr_date, float(curr_close), s2_reason
                        )
                        if fill is None:
                            return params
                        px, fill_ts, rsn = fill
                        return self._execute_buy(
                            fill_ts,
                            px,
                            "BUY_2ND",
                            params,
                            stage=2,
                            reason=rsn,
                            indicators=indicators,
                            i=i,
                        )
            return params

        try:
            has_sig = bool(signal_shifted[i])
        except Exception:
            has_sig = False
        if has_sig:
            sig_type = indicators["Sig_Type"][i]
            is_ok = True
            use_sb_for_type = (
                self.use_sb_a if sig_type == "TYPE_A" else self.use_sb_b
            )
            if use_sb_for_type:
                try:
                    ab_ratio = indicators["ask_bid_ratio"][i]
                    v_power = indicators["vol_power"][i]
                    if ab_ratio < self.sb_ba or v_power < self.sb_vp:
                        is_ok = False
                except Exception:
                    is_ok = False
            if is_ok:
                reason = "SmartBuy|SIG" if use_sb_for_type else "SIG"
                fill = self._resolve_auction_market_fill(
                    curr_date, float(curr_close), reason
                )
                if fill is None:
                    return params
                px, fill_ts, rsn = fill
                return self._execute_buy(
                    fill_ts,
                    px,
                    "BUY_1ST",
                    params,
                    stage=1,
                    entry_idx=i,
                    max_p=self._entry_max_p(float(px), curr_high),
                    reason=rsn,
                    indicators=indicators,
                    i=i,
                    sig_type=sig_type,
                )
        return params

    def _execute_buy(self, date, price, action, params, stage=1, entry_idx=None, max_p=None, reason="", indicators=None, i=None, sig_type='TYPE_A'):
        """매수 실행 및 params 업데이트"""
        # [v27.0] 전략 타입(A/B/S2)을 매수 사유(Reason) 접두어로 명시하여 리포트 집계 정확도 향상
        if stage == 1:
            if not self.settings.get('use_type_b', False) and sig_type == 'TYPE_B':
                sig_type = 'TYPE_A'
            reason = f"[{sig_type}] {reason}"
        else:
            reason = f"[Stage 2] {reason}"
        if indicators is not None and i is not None:
            try:
                rsi_val = indicators['RSI'][i]
                ma5_val = indicators['MA5'][i]
                osc_val = indicators['OSC'][i]
                reason += f"|RSI:{rsi_val:.1f}|MA5:{int(ma5_val):,}|OSC:{osc_val:.2f}"
            except: pass

        # 포지션 사이징: 현재 총자산(잔고+보유평가) 대비 %
        total_equity = params['balance']
        if params['position'] is not None:
            total_equity += params['position']['qty'] * price
        s_pct = float(
            self.settings.get(
                's1_equity_pct' if stage == 1 else 's2_equity_pct', 10.0
            )
        )
        amt = total_equity * (s_pct / 100.0)

        qty = int(amt // price)
        if qty <= 0: return params
        
        fee = int(price * qty * self.buy_f_rate)
        params['balance'] -= (qty * price + fee)
        
        if stage == 1:
            _atr_e = 0.0
            if indicators is not None and i is not None:
                try:
                    _is_b1 = "TYPE_B" in str(sig_type).upper()
                    _acol = "ATR_B" if _is_b1 else "ATR_A"
                    if _acol in indicators:
                        _atr_e = float(indicators[_acol][i])
                    elif "ATR" in indicators:
                        _atr_e = float(indicators["ATR"][i])
                except (TypeError, ValueError, KeyError, IndexError):
                    _atr_e = 0.0
            params['position'] = {
                'entry_date': date, 'entry_idx': entry_idx, 
                'avg_price': price, 'qty': qty, 'stage': 1, 'max_price': max_p,
                'sig_type': sig_type,
                'atr_at_entry': _atr_e
            }
        else:
            old_q = params['position']['qty']
            pos = params['position']
            pos['avg_price'] = (pos['avg_price'] * old_q + price * qty) / (old_q + qty)
            pos['qty'] += qty
            pos['stage'] = 2
            # S2 체결 시점 ATR로 손절선 갱신(기존: S1 진입 ATR만 유지 → SL 배수가 체감되기 어려움)
            try:
                if indicators is not None and i is not None:
                    _is_b2 = TradingStrategy._holding_sig_is_type_b(pos)
                    _acol2 = "ATR_B" if _is_b2 else "ATR_A"
                    _atr = float(indicators[_acol2][i]) if _acol2 in indicators else float(indicators["ATR"][i])
                    if _atr > 0:
                        pos['atr_at_entry'] = _atr
            except Exception:
                pass

        curr_sig_type = params['position']['sig_type']
        self.collect_trade(date, action, price, qty, params['position']['qty'], params['position']['qty'] * price, reason_str=reason, fee=fee, sig_type=curr_sig_type)
        return params

    def _execute_sell(self, date, price, reason, params, indicators=None, i=None):
        """매도 실행 및 params 업데이트"""
        if indicators is not None and i is not None:
            try:
                rsi_val = indicators['RSI'][i]
                ma5_val = indicators['MA5'][i]
                osc_val = indicators['OSC'][i]
                if "RSI:" not in str(reason): # RSI 정보가 없을 때만 추가
                    prefix = "|" if "|" not in str(reason) else ", "
                    reason += f"{prefix}RSI:{rsi_val:.1f}|MA5:{int(ma5_val):,}|OSC:{osc_val:.2f}"
            except: pass
            
        pos = params['position']
        ret_v = (price / pos['avg_price'] - 1) * 100
        fee = int(price * pos['qty'] * self.sell_f_rate)
        
        entry_date = pos['entry_date']
        entry_sig = pos['sig_type']
        
        self.collect_trade(date, "SELL", price, pos['qty'], 0, 0, ret_val=ret_v, reason_str=reason, fee=fee, sig_type=entry_sig)
        if reason is None:
            reason = "SELL" # Default
        
        # [v4.0] Include stage for precise S1/S2 filtering
        params['history'].append({
            'date': date, 'type': 'SELL', 'price': price, 'ret': ret_v, 
            'reason': reason, 'sig_type': entry_sig, 'stage': pos.get('stage', 1)
        })
        params['balance'] += (pos['qty'] * price - fee)
        params['position'] = None
        return params

    def collect_trade(self, date, action, price, qty, total_qty, total_val, ret_val=0, reason_str="", fee=0, sig_type=None):
        self.trades.append({
            'Date': date, 'Action': action, 'Price': price, 'Qty': qty,
            'Total_Qty': total_qty, 'Total_Val': total_val, 'Return': ret_val, 
            'Reason': reason_str, 'Fee': fee, 'Sig_Type': sig_type
        })

    def _finalize_simulation(self, df, code, params, log_to_csv=False):
        """결과 정리 및 요약 생성"""
        equity_curve = params['equity_curve']
        position = params['position']
        balance = params['balance']

        if position is not None:
            final_time = df.index[-1]
            final_price = df['Close'].iloc[-1]
            sell_fee = int(final_price * position['qty'] * self.sell_f_rate)
            ret = (final_price / position['avg_price'] - 1) * 100
            last_row = df.iloc[-1]
            reason = f"Final|RSI:{last_row.get('RSI',0):.1f}|MA5:{int(last_row.get('MA5',0)):,}|OSC:{last_row.get('OSC',0):.2f}"
            self.collect_trade(final_time, "HOLD", final_price, position['qty'], position['qty'], 
                               position['qty'] * final_price, ret_val=ret, reason_str=reason, fee=sell_fee, sig_type=position['sig_type'])
            balance += (position['qty'] * final_price - sell_fee)
            equity_curve[-1] = balance

        df['Equity'] = equity_curve
        final_ret = (equity_curve[-1] / params['initial_balance'] - 1) * 100 if len(equity_curve) > 0 else 0
        
        peak = -1e10
        mdd = 0
        for val in equity_curve:
            if val > peak: peak = val
            dd = (val / peak - 1) * 100
            if dd < mdd: mdd = dd
            
        history = params['history']
        win_rate = len([h for h in history if h['ret'] > 0]) / len(history) * 100 if history else 0
        s2_count = len([t for t in self.trades if t['Action'] == 'BUY_2ND'])
        
        # [v35.8] Granular Averages for Type A/B
        # [v4.0] Granular Stage 1 A/B filtering (Excluding S2 via stage field and Reason string)
        s1_a_rets = [h['ret'] for h in history if "TYPE_A" in str(h.get('sig_type', '')) and h.get('stage', 1) == 1 and "[Stage 2]" not in str(h.get('reason', ''))]
        s1_b_rets = [h['ret'] for h in history if "TYPE_B" in str(h.get('sig_type', '')) and h.get('stage', 1) == 1 and "[Stage 2]" not in str(h.get('reason', ''))]
        
        avg_s1_a = sum(s1_a_rets) / len(s1_a_rets) if s1_a_rets else 0
        avg_s1_b = sum(s1_b_rets) / len(s1_b_rets) if s1_b_rets else 0
        
        # [v15.6] Write batch logs to files
        if log_to_csv:
            with csv_lock:
                if self.trades:
                    trade_df = pd.DataFrame(self.trades)
                    trade_df['Reason'] = trade_df['Reason'].astype(str).str.replace(',', ';')
                    trade_df['Code'] = code
                    trade_df['Name'] = params['stock_name']
                    trade_header = not os.path.exists(TRADE_CSV)
                    trade_df.to_csv(TRADE_CSV, mode='a', index=False, header=trade_header, encoding='utf-8-sig')
                
                if params['signal_logs']:
                    sig_df = pd.DataFrame(params['signal_logs'])
                    sig_header = not os.path.exists(SIGNAL_CSV)
                    sig_df.to_csv(SIGNAL_CSV, mode='a', index=False, header=sig_header, encoding='utf-8-sig')

        return {
            'code': code,
            'final_ret': final_ret,
            'mdd': mdd,
            'trades': len(params['history']),
            'win_rate': win_rate,
            's2_trades': s2_count,
            'avg_s1_a': avg_s1_a,
            'avg_s1_b': avg_s1_b,
            's1_a_rets': s1_a_rets,
            's1_b_rets': s1_b_rets,
            'history': params['history'],
            'final_equity': equity_curve[-1]
        }

    # _calculate_whipsaw_scale 메소드 제거 (pre-calculation으로 대체)

class PortfolioBacktester:
    """다중 종목 포트폴리오 백테스터 (v5.1)"""
    def __init__(self, settings, codes):
        self.settings = settings
        self.codes = codes
        self.loader = BacktestDataLoader()
        self.tester = SimpleBacktester(settings)
        
    def run_all(self, start_date, end_date, interval='1d', force_refresh=False, high_precision=False):
        """다중 종목 포트폴리오 백테스트 실행 (v16.0)"""
        start_date = pd.to_datetime(start_date).strftime('%Y-%m-%d')
        end_date = pd.to_datetime(end_date).strftime('%Y-%m-%d')
        
        # 1. 초기화
        self._reset_csv_logs()
        # 2. 실행
        print(f"Starting Backtest for {len(self.codes)} stocks ({start_date} ~ {end_date}, {interval})")
        summaries, all_equity = self._run_parallel_backtests(start_date, end_date, interval, force_refresh, high_precision)
        
        # 3. 결과 리포트 생성
        if summaries:
            self._generate_summary_reports(summaries, all_equity, start_date, end_date, interval, high_precision=high_precision)
            return pd.DataFrame(summaries)
        return None

    def _reset_csv_logs(self):
        """기존 백테스트 로그 파일 초기화"""
        for f in [SIGNAL_CSV, TRADE_CSV]:
            if os.path.exists(f): 
                try: os.remove(f)
                except:
                    try: open(f, 'w').close()
                    except: pass

    def _run_parallel_backtests(self, start_date, end_date, interval, force_refresh, high_precision):
        """병렬로 개별 종목 백테스트 수행 (v40.0: Multiprocessing Optimized)"""
        w_start_dt = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=200)).strftime("%Y-%m-%d")
        self.tester.settings['_original_start_dt'] = start_date

        # 1. 전 종목 데이터 선행 로딩 (PPE 오버헤드 감소)
        data_cache = {}
        data_cache_m1 = {}
        print(f"  [Perf] Pre-loading historical data for {len(self.codes)} stocks...")
        
        def _load_data(code):
            d15 = self.loader.get_data(code, w_start_dt, end_date, interval, force_refresh=force_refresh)
            dm1 = None
            if high_precision:
                dm1 = self.loader.get_data(code, w_start_dt, end_date, interval='1m')
            return code, d15, dm1

        # 데이터 로딩은 I/O 바운드이므로 스레드 사용
        with TPE(max_workers=min(len(self.codes), 8)) as loader_executor:
            for code, d15, dm1 in loader_executor.map(_load_data, self.codes):
                if d15 is not None:
                    data_cache[code] = d15
                    if dm1 is not None:
                        data_cache_m1[code] = dm1

        # 3. 데이터 캐싱 및 워커 초기화 준비 (Windows 최적화)
        cache_data = {'data': data_cache, 'm1': data_cache_m1}
        cache_path = os.path.join(BASE_DIR, "bt_portfolio_cache.tmp")
        try:
            with open(cache_path, 'wb') as f:
                pickle.dump(cache_data, f)
        except Exception as e:
            print(f"  (!) Cache Write Error: {e}")

        # 4. 멀티프로세싱 실행
        summaries = []
        all_equity = pd.DataFrame()
        
        # CPU 코어 수에 맞춰 워커 설정 (사용자 정보 반영)
        num_workers = min(os.cpu_count() or 4, len(self.codes))
        # [Perf] 워커 개수가 너무 적으면 오버헤드가 더 클 수 있음
        if num_workers > 1:
            print(f"  [Perf] Executing Parallel Backtest with {num_workers} processes...")
            tasks = [(code, self.settings, interval, force_refresh, high_precision) for code in data_cache.keys()]
            
            with PPE(max_workers=num_workers, initializer=init_bt_worker, initargs=(cache_path,)) as executor:
                for code, res_df, summary in executor.map(_run_single_backtest_task, tasks):
                    if summary:
                        summaries.append(summary)
                        if res_df is not None and 'Equity' in res_df.columns:
                            all_equity[code] = res_df['Equity']
        else:
            # 1개 프로세스인 경우 (디버그 또는 저사양 환경)
            print(f"  [Perf] Executing Serial Backtest...")
            init_bt_worker(cache_path) # 메인 프로세스 데이터 설정
            for code in data_cache.keys():
                _, res_df, summary = _run_single_backtest_task((code, self.settings, interval, force_refresh, high_precision))
                if summary:
                    summaries.append(summary)
                    if res_df is not None and 'Equity' in res_df.columns:
                        all_equity[code] = res_df['Equity']

        # 임시 캐시 삭제
        if os.path.exists(cache_path):
            try: os.remove(cache_path)
            except: pass
            
        return summaries, all_equity

    def _generate_summary_reports(self, summaries, all_equity, start_date, end_date, interval, high_precision=False):
        """최종 통계 출력 및 리포트 생성"""
        df_sum = pd.DataFrame(summaries)
        
        # Portfolio All Equity Curve (Total)
        if not all_equity.empty:
            all_equity['Total'] = all_equity.sum(axis=1) / len(self.codes)
            all_equity[['Total']].to_csv("backtest_portfolio_curve.csv", encoding='utf-8-sig')

        print("\n" + "="*40)
        print(f"Portfolio Backtest Results ({len(summaries)} stocks)")
        print(f" - Avg Return: {df_sum['final_ret'].mean():+.2f}%")
        print(f" - Avg Win Rate: {df_sum['win_rate'].mean():.1f}%")
        
        # [v4.0] True Global Stage 1 Averages (Aggregated across ALL stocks)
        all_s1_a = [r for s in summaries for r in s.get('s1_a_rets', [])]
        all_s1_b = [r for s in summaries for r in s.get('s1_b_rets', [])]
        
        grand_avg_s1_a = sum(all_s1_a) / len(all_s1_a) if all_s1_a else 0
        grand_avg_s1_b = sum(all_s1_b) / len(all_s1_b) if all_s1_b else 0
        
        print(f" - S1 Type A Avg: {grand_avg_s1_a:+.2f}% ({len(all_s1_a)} trades)")
        print(f" - S1 Type B Avg: {grand_avg_s1_b:+.2f}% ({len(all_s1_b)} trades)")
        print("="*40)
        
        # [v4.0] Overwrite per-stock averages with global grand averages for Optimizer compatibility
        if summaries:
            for s in summaries:
                s['avg_s1_a'] = grand_avg_s1_a
                s['avg_s1_b'] = grand_avg_s1_b
        
        df_sum.to_csv("backtest_summary_multi.csv", index=False, encoding='utf-8-sig')
        
        # Reports (signal_report DELETED)
        trd_path = os.path.join(BASE_DIR, "trade_report.html")
        
        trades_df = pd.DataFrame()
        if os.path.exists(TRADE_CSV):
            try:
                trades_df = pd.read_csv(TRADE_CSV, dtype={'Code': str})
                trades_df['Date'] = pd.to_datetime(trades_df['Date'])
            except: pass
            
        daily_equity_list = [{'date': start_date + " 08:59:00", 'equity': 100.0, 'cash': 100.0}]
        if not all_equity.empty and 'Total' in all_equity.columns:
            from utils.krx_session_hours import get_session_hours

            for d, val in all_equity['Total'].items():
                try:
                    close_s = get_session_hours(pd.Timestamp(d).date()).close.strftime(
                        "%H:%M:%S"
                    )
                    day_s = pd.Timestamp(d).strftime("%Y-%m-%d")
                    eq_ts = f"{day_s} {close_s}"
                except Exception:
                    eq_ts = pd.Timestamp(d).strftime("%Y-%m-%d") + " 15:30:00"
                daily_equity_list.append(
                    {'date': eq_ts, 'equity': val, 'cash': val}
                )
        
        generate_trade_report(trades_df, interval, trd_path, len(summaries), 
                              df_sum['win_rate'].mean(), df_sum['final_ret'].mean(), self.settings,
                              target_codes=self.codes,
                              start_date=start_date, end_date=end_date, daily_equity=daily_equity_list,
                              high_precision=high_precision)





if __name__ == "__main__":
    # 통합 테스트용 샘플 (v6.4: 산일전기 062040 추가)
    sample_codes = ['005930', '000660', '005380', '055550', '051910', '012450', '035420', '015760', '028260', '009150']
    
    # 신호가 잘 발생하도록 완화된 설정 사용 (검증용)
    lenient_settings = {
        'buy_rsi_min': 30, 'buy_rsi_max_b': 65,
        'ma60_min': 90, 'ma60_max': 120,
        'vol_surge': 1.0,
        'macd_fast_entry': 12, 'macd_slow_entry': 26, 'macd_sig_entry': 9,
        'stage1_amt': 900000, 'stage2_amt': 2100000,
        'use_s2_ma5': True, 'use_s2_profit': True, 'use_s2_osc': True,
        'use_s2_rsi_sig': True,
        'take_profit': 5.0, 'stop_loss': 3.0, 'rsi_exit': 50.0
    }
    
    start = "2024-01-01"
    end = "2024-12-31"
    
    # 포트폴리오 백테스트 실행 예시
    tester = PortfolioBacktester(lenient_settings, sample_codes)
    tester.run_all(start, end)
