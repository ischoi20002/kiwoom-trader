import pandas as pd
import os
import time
from datetime import datetime, timedelta
from data_manager_v2 import DataManagerV2
import strategy.signals
import strategy_selector
import backtest_engine as bt
import importlib
importlib.reload(strategy.signals) # [Force] 시그널 로직 업데이트 반영
importlib.reload(strategy_selector) 
importlib.reload(bt) 
import numpy as np
from backtest_engine import (
    TradingStrategy,
    SIGNAL_CSV,
    TRADE_CSV,
    BASE_DIR,
    append_trade_csv_line,
    clear_trade_csv_buffer,
    flush_trade_csv_buffer,
    settings_for_backtest,
)
from utils.fee_rates import fee_decimal_rates_backtest
from strategy.synthetic_realtime_fill import (
    synthesize_bid_ask_ratio_vectorized,
    synthesize_vol_power_vectorized,
)
from typing import Any, Callable, List, Optional


def _m1_sell_price_conservative(m1_r: Any) -> float:
    """
    고정밀 1분: 매도 체결·손익(보수적) — 저가(없으면 종가).
    1분 안에서 낙폭·손절을 가혹히 반영.
    """
    try:
        if m1_r is None:
            return 0.0
        c = _m1_float_ohlc(m1_r, "Close", "close", 0.0)
        v = _m1_float_ohlc(m1_r, "Low", "low", float("nan"))
        if v > 0 and np.isfinite(v):
            return float(v)
        if c > 0:
            return float(c)
    except (TypeError, ValueError, KeyError):
        pass
    return 0.0


def _m1_buy_price_conservative(m1_r: Any) -> float:
    """
    고정밀 1분: 매수 체결(보수적) — 고가(없으면 종가).
    체결가를 불리하게(더 비쌈) 잡기.
    """
    try:
        if m1_r is None:
            return 0.0
        c = _m1_float_ohlc(m1_r, "Close", "close", 0.0)
        v = _m1_float_ohlc(m1_r, "High", "high", float("nan"))
        if v > 0 and np.isfinite(v):
            return float(v)
        if c > 0:
            return float(c)
    except (TypeError, ValueError, KeyError):
        pass
    return 0.0


def _m1_float_ohlc(
    m1_r: Any, cname: str, cname2: str, default: float
) -> float:
    """Series/dict 겸용 OHLC 스칼라 읽기."""
    try:
        for name in (cname, cname2):
            v = None
            if hasattr(m1_r, "get"):
                v = m1_r.get(name)
            elif name in m1_r:
                v = m1_r[name]
            if v is not None and not pd.isna(v):
                f = float(v)
                if np.isfinite(f):
                    return f
    except (TypeError, KeyError, ValueError):
        pass
    return float(default)


def _dual_track_new_candidates(
    top_codes_a: List[str],
    top_codes_b: List[str],
    holdings_codes: List[str],
    max_sa: int,
    max_sb: int,
) -> List[str]:
    """
    신규 감시 후보만 Track A/B 각각 max_slots_a·max_slots_b 상한으로 잘라 합친다.
    (구버전 단일 max_slots로 B가 잘리던 문제 제거)
    """
    try:
        hc = set(holdings_codes)
        picked: set[str] = set()
        new_a: list[str] = []
        for c in top_codes_a:
            if len(new_a) >= max(0, max_sa):
                break
            if c not in hc and c not in picked:
                picked.add(c)
                new_a.append(str(c))
        new_b: list[str] = []
        for c in top_codes_b:
            if len(new_b) >= max(0, max_sb):
                break
            if c not in hc and c not in picked:
                picked.add(c)
                new_b.append(str(c))
        return _interleave_track_codes(new_a, new_b)
    except Exception:
        return []


def _interleave_track_codes(codes_a: List[str], codes_b: List[str]) -> List[str]:
    """
    Track A 전체 뒤에 B를 붙이면 감시·정렬 순서에서 B가 뒤로 밀릴 수 있음.
    인덱스 기준으로 A·B를 교차 병합해 듀얼 트랙에서 B도 앞쪽 타임라인에 섞이게 한다.
    """
    try:
        seen: set[str] = set()
        out: list[str] = []
        na, nb = len(codes_a), len(codes_b)
        for i in range(max(na, nb)):
            if i < na:
                c = str(codes_a[i])
                if c not in seen:
                    seen.add(c)
                    out.append(c)
            if i < nb:
                c = str(codes_b[i])
                if c not in seen:
                    seen.add(c)
                    out.append(c)
        return out
    except Exception:
        return list(dict.fromkeys(list(codes_a or []) + list(codes_b or [])))


class DynamicBacktester:
    """[v16.0] 동적 포트폴리오 백테스터: 매일 종목을 새로 선정하고 1,000만원 예산을 공유하여 운용"""
    def __init__(self, settings, total_budget=None):
        self.settings = settings_for_backtest(settings)
        self.db = DataManagerV2("trading_data.db")
        # [v16.7] 설정에서 예산 가져오기 (기본값 1000만)
        budget = total_budget if total_budget is not None else float(
            self.settings.get("backtest_initial_capital", 10_000_000)
        )
        self.initial_cash = budget
        self.cash = budget
        # [v17.4] 매매 비용 설정 (기본값: 매수 0.015%, 매도 0.195%)
        # [v17.5] 수수료/세금 설정 (기본값 방어)
        self.buy_f_rate, self.sell_f_rate = fee_decimal_rates_backtest(self.settings)
        self.holdings = {} # {code: {'qty': Q, 'avg_p': P, 'entry_n': N, 'max_p': H, 'min_p': L}}
        self.trades_history = []
        self.daily_equity = []
        self.cached_15m_data = {} # Step 3 (Phase 2) initialized as empty
        self.start_date = None
        self.end_date = None
        self.high_precision = False  # [v20.0] 고정밀 모드 플래그
        self.cached_1m_data = {}     # [v20.0] 1분봉 데이터 캐시
        
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
        self.use_s1a_tp = True
        self.use_s1b_tp = True
        self.use_s1a_sl = True
        self.use_s1b_sl = True
        self.use_s1a_flash = True
        self.use_s1b_flash = True
        self.use_s2_flash = True
        self.use_atr = False
        self.use_type_b = False
        self.gap_down_lmt = -5.0
        self.stock_name_map = {}

    def _cache_run_hot_settings(self) -> None:
        """run / 진단 공통: 루프에서 자주 읽는 설정·임계값 캐시"""
        s = self.settings
        self.use_sb_a = self.settings.get('use_smart_buy_a', False)
        self.use_sb_b = self.settings.get('use_smart_buy_b', False)
        self.use_sb_s2_a = self.settings.get("use_smart_buy_s2", False)
        self.use_sb_s2_b = self.settings.get("use_smart_buy_s2_b", False)
        self.sb_ba = float(s.get('smart_buy_ba_ratio', 1.2))
        self.sb_vp = float(s.get('smart_buy_vp', 110.0))
        self.use_sx = s.get('use_smart_exit', False)
        self.sx_ba = float(s.get('smart_exit_ba_ratio', 0.8))
        self.sx_vp = float(s.get('smart_exit_vp', 90.0))
        # Smart Exit 가격 게이트: 설정은 '하락 %(양수)' 의도, 음수는 abs 로 동일 처리(evaluate_position 과 정합).
        self.sx_p_thr = abs(float(s.get("smart_exit_price_threshold", 0.0)))
        self.use_s1a_tp = s.get('use_s1_a_tp', True)
        self.use_s1b_tp = s.get('use_s1_b_tp', True)
        self.use_s1a_sl = s.get('use_s1_a_stop_loss', True)
        self.use_s1b_sl = s.get('use_s1_b_stop_loss', True)
        self.use_s2_tp = s.get('use_s2_tp', True)
        self.use_s2_sl = s.get('use_s2_stop_loss', True)
        self.use_s1a_flash = s.get('use_s1_a_flash_exit', True)
        self.use_s1b_flash = s.get('use_s1_b_flash_exit', True)
        self.use_s2_flash = s.get('use_s2_flash_exit', True)
        self.use_atr = bool(
            s.get("use_atr_exit_a", s.get("use_atr_exit", False))
            or s.get("use_atr_exit_b", s.get("use_atr_exit", False))
        )
        self.use_type_b = s.get('use_type_b', False)
        self.gap_down_lmt = float(s.get("gap_down_limit_a", s.get("gap_down_limit", -5.0)))
    def _type_balance_allows_new_s1(self, sig_type: str) -> bool:
        """
        신규 S1(Type A/B) 진입 허용 여부. 보유 **종목 수**로 A/B를 세고,
        목표 비율(settings: type_balance_a_pct % vs (100−A)%)에 비해 과다인 타입의 신규만 차단한다.
        (로직 본문은 TradingStrategy.type_balance_allows_new_s1 에 단일화)
        """
        return TradingStrategy.type_balance_allows_new_s1(self.settings, sig_type, self.holdings)

    def _effective_s1_sig_type_for_buy(self, row: Any) -> str:
        """15분 행·1분 indicator dict 등에서 Sig_Type 확정(동시신호+보유 A/B 동점 시 B 우선)."""
        return TradingStrategy.effective_s1_sig_type_for_buy(self.settings, row, self.holdings)

    def _bootstrap_portfolio_data(
        self,
        start_date: str,
        end_date: str,
        callback: Optional[Callable[[str, Any], None]],
    ) -> bool:
        """
        영업일 캘린더(001 bars_1d)·유니버스·15분 캐시 로드. 시장 지표·VIX·레짐은 사용하지 않는다.
        """
        if callback:
            callback("영업일 캘린더 로드 중...", 2)
        try:
            raw_days = self.db.get_trading_days(start_date, end_date)
        except Exception as e:
            print(f"[Error] [Dynamic] 영업일 조회 실패: {e}")
            return False
        trading_days: list[str] = []
        for d in raw_days or []:
            try:
                trading_days.append(pd.to_datetime(d).strftime("%Y-%m-%d"))
            except Exception:
                continue
        if not trading_days:
            print(
                "[Error] [Dynamic] 해당 기간에 영업일(001 bars_1d)이 없습니다. "
                "DB에 시장지수 001 일봉이 수집되어 있는지 확인하세요."
            )
            return False
        print(f"[Dynamic] 백테스트 시작: {start_date} ~ {end_date} ({len(trading_days)} 영업일)")
        t_start = time.time()
        _result = strategy_selector.prepare_dynamic_universe(
            self.settings, start_date, end_date, self.db, callback if callback else print
        )
        if isinstance(_result, tuple) and len(_result) == 3:
            universe_schedule, self.schedule_a, self.schedule_b = _result
        else:
            universe_schedule = _result
            self.schedule_a = universe_schedule
            self.schedule_b = {}
        t_duration = time.time() - t_start
        msg = f"사전 계산 완료 ({t_duration:.2f}초)"
        print(f"[Done] {msg}")
        if callback:
            callback(msg, 5)
        if not universe_schedule:
            if callback:
                callback("  [Warning] 선택된 종목이 없습니다. 사전 계산(Phase 2)을 건너뜁니다.", 0)
            self.cached_15m_data = {}
        else:
            self.cached_15m_data = self._prepare_15m_cached_data(
                universe_schedule, start_date, end_date, callback
            )
        self.cached_1m_data = {}
        self._bt_trading_days = trading_days
        return True

    def diagnose_intraday_entry_potential(
        self,
        start_date: str,
        end_date: str,
        *,
        high_precision: bool = True,
        callback: Optional[Callable[[str, Any], None]] = None,
    ) -> pd.DataFrame:
        """
        포트폴리오 백테스트와 동일한 데이터·시장필터 기준으로,
        일자별 15분봉 'Signal∧Ready' 개수를 집계하여 미체결이 전략상 자연스러운지 확인한다.
        (고정밀 1분 체결·잔고 유무·Smart Buy 최종 차단은 이 표에 포함되지 않음)
        """
        self.high_precision = high_precision
        start_date = pd.to_datetime(start_date).strftime("%Y-%m-%d")
        end_date = pd.to_datetime(end_date).strftime("%Y-%m-%d")
        self.start_date = start_date
        self.end_date = end_date
        self._cache_run_hot_settings()
        self.stock_name_map = TradingStrategy.get_stock_name_map()
        if callback:
            callback("진단: 백테스트와 동일 컨텍스트 로드 중...", 0)
        if not self._bootstrap_portfolio_data(start_date, end_date, callback):
            return pd.DataFrame()
        trading_days = self._bt_trading_days
        rows: list[dict[str, Any]] = []
        saved_holdings = self.holdings
        self.holdings = {}
        try:
            for idx, current_day in enumerate(trading_days):
                prev_day_idx = idx - 1
                ref_day = trading_days[prev_day_idx] if prev_day_idx >= 0 else current_day
                top_codes_a = self.schedule_a.get(ref_day, [])
                top_codes_b = self.schedule_b.get(ref_day, [])
                top_codes = _interleave_track_codes(top_codes_a, top_codes_b)
                if not top_codes:
                    prev_day_dt = datetime.strptime(current_day, "%Y-%m-%d") - timedelta(days=1)
                    candidates_df = strategy_selector.select_monitoring_targets(
                        self.settings, logger_callback=lambda x: None, target_date=prev_day_dt, data_manager=self.db
                    )
                    if candidates_df is not None and not candidates_df.empty:
                        max_fb = int(self.settings.get("max_slots_a", 20)) + int(
                            self.settings.get("max_slots_b", 10)
                        )
                        for _, row in candidates_df.head(max_fb).iterrows():
                            top_codes.append(row["Code"])
                max_sa = int(self.settings.get("max_slots_a", 20))
                max_sb = int(self.settings.get("max_slots_b", 10))
                if top_codes_a or top_codes_b:
                    monitoring_list = _dual_track_new_candidates(
                        top_codes_a, top_codes_b, [], max_sa, max_sb
                    )
                else:
                    monitoring_list = top_codes[: max_sa + max_sb]
                n_sig = 0
                n_bars = 0
                n_codes_with_data = 0
                for code in monitoring_list:
                    if code not in self.cached_15m_data:
                        continue
                    base_df = self.cached_15m_data[code]
                    day_full = base_df.loc[current_day : current_day + " 23:59:59"].copy()
                    if day_full.empty:
                        continue
                    n_codes_with_data += 1
                    n_bars += len(day_full)
                    sig_r = day_full["Signal"].astype(bool) & day_full["Ready"].astype(bool)
                    n_sig += int(sig_r.sum())
                rows.append(
                    {
                        "date": current_day,
                        "n_monitor_codes": len(monitoring_list),
                        "n_codes_15m": n_codes_with_data,
                        "n_15m_bars": n_bars,
                        "n_signal_ready_pre_mkt": n_sig,
                        "n_signal_ready_post_mkt": n_sig,
                    }
                )
        finally:
            self.holdings = saved_holdings
        return pd.DataFrame(rows)

    def run(self, start_date, end_date, interval='15m', callback=None, high_precision=False):
        """메인 시뮬레이션 루프 (일단위 유니버스 갱신 + 분단위/15분단위 체결)"""
        self.high_precision = high_precision
        # [v18.8] Date Normalization (YYYY-MM-DD)
        start_date = pd.to_datetime(start_date).strftime('%Y-%m-%d')
        end_date = pd.to_datetime(end_date).strftime('%Y-%m-%d')
        
        self.start_date = start_date
        self.end_date = end_date
        
        self._cache_run_hot_settings()
        self.stock_name_map = TradingStrategy.get_stock_name_map()
        
        if callback: callback("백테스트 준비 중...", 0)
        
        if not self._bootstrap_portfolio_data(start_date, end_date, callback):
            return None

        trading_days = self._bt_trading_days

        # [v17.3] 시작 시점(Day 0) 자산 기록 (트레이딩 시작 전일 혹은 시작일 아침으로 기록하여 중복 방지)
        # [v18.9] Plotly date parsing 호환성을 위해 유효한 시간 형식(08:59) 사용
        self.daily_equity = [{'date': start_date + " 08:59:00", 'equity': self.initial_cash, 'cash': self.initial_cash, 'holdings_n': 0}]
        
        # 신호 기록 초기화
        for f in [SIGNAL_CSV, TRADE_CSV]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass
        try:
            clear_trade_csv_buffer()
        except Exception:
            pass

        # 2. 일단위 루프
        for idx, current_day in enumerate(trading_days):
            day_t_start = time.time()
            progress = (idx / len(trading_days)) * 100
            if callback: callback(f"[{current_day}] 시계열 모의투자 중...", progress)
            
            # 2-1. 오전 종목 선정 (사전 계산된 스케줄 활용, Track A/B 분리)
            prev_day_idx = idx - 1
            ref_day = trading_days[prev_day_idx] if prev_day_idx >= 0 else current_day
            
            top_codes_a = self.schedule_a.get(ref_day, [])
            top_codes_b = self.schedule_b.get(ref_day, [])
            top_codes = _interleave_track_codes(top_codes_a, top_codes_b)

            # Track A/B 소속 맵 (매수 시 sig_type 검증용, 1분봉 정밀 모드에서도 사용)
            track_map: dict[str, str] = {}
            for c in top_codes_a:
                track_map[c] = 'A'
            for c in top_codes_b:
                if c in track_map:
                    track_map[c] = 'AB'
                else:
                    track_map[c] = 'B'
            self._current_track_map = track_map
            
            if not top_codes:
                # [v4.7] 폴백: select_monitoring_targets 결과에서 Track A/B 구분 시도
                prev_day_dt = datetime.strptime(current_day, "%Y-%m-%d") - timedelta(days=1)
                candidates_df = strategy_selector.select_monitoring_targets(
                    self.settings, logger_callback=lambda x: None, target_date=prev_day_dt, data_manager=self.db
                )
                if candidates_df is not None and not candidates_df.empty:
                    max_fb = int(self.settings.get("max_slots_a", 20)) + int(
                        self.settings.get("max_slots_b", 10)
                    )
                    use_b_fb = self.settings.get('use_sel_track_b', True)
                    for _, row in candidates_df.head(max_fb).iterrows():
                        c = row['Code']
                        top_codes.append(c)
                        # Valid_B 컬럼이 있으면 B 트랙 여부를 판별, 없으면 A로 기본 처리
                        if use_b_fb and row.get('Valid_B', False):
                            track_map[c] = 'B' if row.get('Valid_A', True) is False else 'AB'
                        else:
                            track_map.setdefault(c, 'A')
            
            # 2-2. 감시 대상 확정 (보유 종목 + 신규 후보: Track A/B 각각 max_slots_a·max_slots_b)
            max_sa = int(self.settings.get("max_slots_a", 20))
            max_sb = int(self.settings.get("max_slots_b", 10))
            holdings_codes = list(self.holdings.keys())
            if top_codes_a or top_codes_b:
                new_candidates = _dual_track_new_candidates(
                    top_codes_a, top_codes_b, holdings_codes, max_sa, max_sb
                )
            else:
                hc_set = set(holdings_codes)
                new_candidates = [
                    c for c in top_codes if c not in hc_set
                ][: max_sa + max_sb]
            monitoring_list = holdings_codes + new_candidates

            # 2-3. 당일 데이터 및 지표 추출 (시장·레짐 필터 없음)
            m_trend = "UP"  # 호환: S2 승인·1분 루프에만 사용
            day_data = {}
            day_signals = {}
            for code in monitoring_list:
                if code not in self.cached_15m_data: continue
                
                base_df = self.cached_15m_data[code]
                day_full = base_df.loc[current_day:current_day + " 23:59:59"].copy()
                if day_full.empty: continue

                day_data[code] = day_full
                day_signals[code] = day_full

            # 2-4. 분단위/15분단위 타임라인 루프
            timeline = []
            if day_data:
                all_times = []
                for df in day_data.values(): all_times.extend(df.index.tolist())
                timeline = sorted(list(set(all_times)))
            
            today_sells = set()
            # [Phase 2] Track A 우선순위 → Track B 순서로 정렬
            code_to_rank = {}
            for i, c in enumerate(top_codes_a):
                code_to_rank[c] = i
            offset_b = len(top_codes_a)
            for i, c in enumerate(top_codes_b):
                if c not in code_to_rank:
                    code_to_rank[c] = offset_b + i
            
            for curr_time in timeline:
                # [v23.0] 고정밀 모드 처리 (지연 로딩 및 타켓팅 최적화)
                m1_processed = set()
                if self.high_precision:
                    # 보유 종목이거나 매수 신호가 있는 종목만 1분봉 시뮬레이션 수행
                    m1_targets = [c for c in monitoring_list if (c in self.holdings) or 
                                  (day_signals.get(c) is not None and curr_time in day_signals[c].index and day_signals[c].loc[curr_time, 'Signal'])]
                    
                    for code in m1_targets:
                        m1_chunk = self._get_m1_chunk_on_demand(code, current_day, curr_time)
                        if m1_chunk is not None and not m1_chunk.empty:
                            self._process_intra_bar_dynamic(code, m1_chunk, day_data.get(code), today_sells, curr_time)
                            m1_processed.add(code)

                # 2-4-1. 매도 감시 (보유 중인 종목)
                for code, h_info in list(self.holdings.items()):
                    # [v36.1] 고정밀 모드인 경우 15분 단위 매도 패스 (하위 1분 루프에서 처리됨)
                    if self.high_precision: continue
                    if code in today_sells or code in m1_processed: continue
                    d_df = day_data.get(code)
                    if d_df is None or curr_time not in d_df.index: continue
                    
                    curr_row = d_df.loc[curr_time]
                    res = TradingStrategy.evaluate_position(
                        code, curr_row['Close'], h_info, self.settings,
                        indicator_row=curr_row, current_time=curr_time,
                        is_realtime=False
                    )
                    
                    if res['action'] == 'EXIT':
                        # TP인 경우 가격 보정 (고가 돌파 기준)
                        e_price = curr_row['Close']
                        if res['reason'] == 'TAKE_PROFIT':
                            tpp = res.get('tp_pct_applied')
                            if tpp is not None:
                                e_price = float(h_info['avg_p']) * (1 + float(tpp) / 100.0)
                            else:
                                is_type_b_h = "TYPE_B" in str(h_info.get('sig_type', ''))
                                if h_info['entry_n'] == 2:
                                    tp_key = 's2_b_take_profit' if is_type_b_h else 's2_take_profit'
                                else:
                                    tp_key = 's1_b_take_profit' if is_type_b_h else 's1_a_take_profit'
                                tp_pct = float(self.settings.get(tp_key, 5.0))
                                e_price = h_info['avg_p'] * (1 + tp_pct/100.0)
                        
                        self._execute_sell(code, e_price, curr_time, res['reason'], indicator_row=curr_row)
                        today_sells.add(code)

                # 2-4-2. 매수 감시
                buy_candidates = [c for c in monitoring_list if (c not in self.holdings or self.holdings.get(c, {}).get('entry_n', 0) < 2) and (c not in today_sells)]
                if buy_candidates and not self.high_precision:
                    for code in sorted(buy_candidates, key=lambda c: code_to_rank.get(c, 999)):
                        if code in today_sells or code in m1_processed: continue
                        d_df = day_data.get(code)
                        if d_df is None or curr_time not in d_df.index: continue
                        last_sig = day_signals[code].loc[curr_time]
                        h_info = self.holdings.get(code)
                        
                        # 2차 매수 대상인 경우 승인 조건 체크 (v22.1)
                        is_buy_signal = (last_sig['Signal'] and last_sig['Ready'])
                        is_addon_target = (h_info is not None and h_info['entry_n'] < 2)
                        
                        if is_buy_signal or is_addon_target:
                            can_execute = True
                            reason = "SIG" # 일관성 있게 backtest_engine의 기본 SIG와 통일
                            
                            if is_addon_target:
                                # 추매 승인 조건 체크 (TradingStrategy 통합 로직 사용)
                                if self._check_s2_approval(code, h_info, last_sig, market_trend="UP"):
                                    # [v33.9] Stage 2 Smart Buy Filter
                                    can_execute_s2 = True
                                    if (self.use_sb_s2_b if "TYPE_B" in str(h_info.get("sig_type", "")) else self.use_sb_s2_a):
                                        ab_ratio = last_sig.get('ask_bid_ratio', last_sig.get('Ask_Bid_Ratio', 1.0 / (last_sig.get('bid_ask_ratio', 1.0) + 1e-10)))
                                        v_power = float(last_sig.get('vol_power', last_sig.get('Vol_power', 100.0)))
                                        if ab_ratio < self.sb_ba or v_power < self.sb_vp:
                                            can_execute_s2 = False
                                    
                                    if can_execute_s2:
                                        reason = f"ADDON(S2)|R:{last_sig.get('RSI',0):.1f}|M:{last_sig.get('MACD',0):.1f}|O:{last_sig.get('OSC',0):.1f}|V:{int(last_sig.get('Volume',0))}"
                                    else:
                                        can_execute = False
                                else:
                                    can_execute = False
                            
                            if can_execute and not is_addon_target:
                                # [Phase 2] Track-Signal 교차 검증: 신호 유형이 종목 트랙과 일치하는지 확인
                                # 동시 A/B 신호 + 보유 A/B 동점 시 B 우선(트랙·스마트바이·체결 일치)
                                sig_type = self._effective_s1_sig_type_for_buy(last_sig)
                                code_track = track_map.get(code, 'A')
                                if sig_type == 'TYPE_A' and code_track == 'B':
                                    can_execute = False
                                elif sig_type == 'TYPE_B' and code_track == 'A':
                                    can_execute = False
                                
                                # [v31.5] Smart Buy (Supply/Demand Confirmation)
                                use_sb_for_type = self.use_sb_a if sig_type == "TYPE_A" else self.use_sb_b
                                
                                if use_sb_for_type:
                                    ab_ratio = last_sig.get('ask_bid_ratio', last_sig.get('Ask_Bid_Ratio', 1.0 / (last_sig.get('bid_ask_ratio', 1.0) + 1e-10)))
                                    v_power = last_sig.get('vol_power', 100.0)
                                    ba_min = float(self.settings.get('smart_buy_ba_ratio', 1.5))
                                    vp_min = float(self.settings.get('smart_buy_vp', 120.0))
                                    
                                    if ab_ratio < ba_min or v_power < vp_min:
                                        can_execute = False
                                        # [v27.5] Shadow Return (1h Lookahead)
                                        p_inc_str = ""
                                        try:
                                            curr_idx = timeline.index(curr_time)
                                            future_idx = min(curr_idx + 4, len(timeline) - 1)
                                            future_time = timeline[future_idx]
                                            if future_time in d_df.index:
                                                p_inc = (d_df.loc[future_time, 'Close'] / last_sig['Close'] - 1) * 100
                                                p_inc_str = f"|1h:{p_inc:+.2f}%"
                                        except: pass

                                        # [v27.5] Log Rejection
                                        reason_rej = f"SmartBuy Rejected|AB:{ab_ratio:.2f}|VP:{v_power:.0f}{p_inc_str}"
                                        self._log_trade_to_csv(curr_time, code, "REJECTED_SIG", last_sig['Close'], 0, reason_rej, sig_type=sig_type)
                                        self.trades_history.append({
                                            'Date': curr_time, 'Code': code, 'Name': self.stock_name_map.get(code, code),
                                            'Action': 'REJECTED_SIG', 'Price': last_sig['Close'], 'Qty': 0, 'Amount': 0, 
                                            'Reason': reason_rej, 'Cash': self.cash,
                                            'Sig_Type': sig_type
                                        })
                            
                            if can_execute:
                                if h_info is None and not is_addon_target:
                                    sig_type = self._effective_s1_sig_type_for_buy(last_sig)
                                else:
                                    sig_type = str(last_sig.get('Sig_Type', 'TYPE_B'))
                                use_sb_for_type = self.use_sb_a if (sig_type == "TYPE_A" and not is_addon_target) else \
                                                   self.use_sb_b if (sig_type == "TYPE_B" and not is_addon_target) else \
                                                   (self.use_sb_s2_b if "TYPE_B" in str(h_info.get("sig_type", "")) else self.use_sb_s2_a)
                                
                                # [v33.9] Prefix "SmartBuy|" if it was enabled and passed
                                if use_sb_for_type:
                                    reason = "SmartBuy|" + reason
                                    
                                self._handle_buy_execution(
                                    code,
                                    {"reason": reason, "Sig_Type": sig_type},
                                    curr_time,
                                    d_df.loc[curr_time],
                                    indicator_row=d_df.loc[curr_time],
                                )

            # 2-5. 일일 정산
            current_equity = self.cash + sum(h['qty'] * day_data[c]['Close'].iloc[-1] for c, h in self.holdings.items() if c in day_data)
            self.daily_equity.append({'date': current_day, 'equity': current_equity, 'cash': self.cash, 'holdings_n': len(self.holdings)})
            
            # [Perf] 매매 CSV 버퍼 일괄 flush (하루 단위로 디스크 I/O 감소)
            try:
                flush_trade_csv_buffer()
            except Exception:
                pass
            
            day_t_duration = time.time() - day_t_start
            print(f"[Day] {current_day} 처리 완료 ({day_t_duration:.2f}초)")

        if self.holdings:
            last_day = trading_days[-1]
            for code, h_info in list(self.holdings.items()):
                if code in day_data:
                    last_price = day_data[code]['Close'].iloc[-1]
                    last_time = day_data[code].index[-1]
                    sell_fee = int(last_price * h_info['qty'] * self.sell_f_rate)
                    ret = (last_price / h_info['avg_p'] - 1) * 100
                    last_row = day_data[code].iloc[-1]
                    reason = f"Final Evaluation|RSI:{last_row['RSI']:.1f}|MA5:{int(last_row['MA5']):,}|OSC:{last_row['OSC']:.2f}"
                    self._log_trade_to_csv(
                        last_time, code, "HOLD", last_price, h_info["qty"], reason,
                        ret=ret, fee=sell_fee, sig_type=h_info.get("sig_type", "TYPE_A"),
                    )
                    self.trades_history.append({
                        'Date': last_time, 'Code': code, 'Name': TradingStrategy.get_stock_name_map().get(code, code), 
                        'Action': 'HOLD', 'Price': last_price, 'Qty': h_info['qty'], 'Amount': h_info['qty'] * last_price, 
                        'Return': ret, 'Reason': reason, 'Cash': self.cash, 'Fee': sell_fee,
                        'Sig_Type': h_info.get('sig_type', 'TYPE_A')
                    })
        try:
            flush_trade_csv_buffer()
        except Exception:
            pass
        return self._finalize_backtest(interval)

    def _handle_buy_execution(
        self,
        code,
        sig_res,
        curr_time,
        last_row,
        indicator_row=None,
        fill_price: Optional[float] = None,
    ) -> bool:
        """매수 실행. 실제 매수가 이루어진 경우 True 반환, 조건 미충족 시 False 반환.
        fill_price: 고정밀 1분 — 보수적 체결가(고가) 등, 지정 시 우선(미지정 시 last_row['Close']).
        """
        try:
            if fill_price is not None and float(fill_price) > 0:
                price = float(fill_price)
            elif hasattr(last_row, "get"):
                price = float(last_row.get("Close", last_row.get("close", 0.0)) or 0.0)  # type: ignore[union-attr]
            else:
                price = float(last_row["Close"])
        except (TypeError, ValueError, KeyError):
            return False
        if price <= 0:
            return False
        h_info = self.holdings.get(code)
        s1_amt = float(self.settings.get('stage1_amt', 1_000_000))
        s2_amt = float(self.settings.get('stage2_amt', 2_000_000))
        
        # [v33.0] Unified Sizing (Unit-based: % or 원)
        s1_unit = self.settings.get('s1_sizing_unit', "원")
        s2_unit = self.settings.get('s2_sizing_unit', "원")
        
        if s1_unit == '%' or s2_unit == '%':
            # Calculate current total equity (Cash + Current Holdings Value)
            holdings_value = 0
            for c, h in self.holdings.items():
                holdings_value += h['qty'] * h.get('last_p', h['avg_p'])
            total_equity = self.cash + holdings_value
            
            if s1_unit == '%':
                s1_pct = float(self.settings.get('s1_equity_pct', 10.0))
                s1_amt = total_equity * (s1_pct / 100.0)
            if s2_unit == '%':
                s2_pct = float(self.settings.get('s2_equity_pct', 20.0))
                s2_amt = total_equity * (s2_pct / 100.0)

        if h_info is None:
            sig_type = str(sig_res.get("Sig_Type", "TYPE_A"))
            if not self._type_balance_allows_new_s1(str(sig_type)):
                try:
                    lab = "TYPE_B" if "TYPE_B" in str(sig_type).upper() else "TYPE_A"
                    na = sum(
                        1
                        for h in self.holdings.values()
                        if "TYPE_B" not in str(h.get("sig_type", "") or "").upper()
                    )
                    nb = sum(
                        1
                        for h in self.holdings.values()
                        if "TYPE_B" in str(h.get("sig_type", "") or "").upper()
                    )
                    try:
                        _tp = float(self.settings.get("type_balance_a_pct", 50.0))
                    except (TypeError, ValueError):
                        _tp = 50.0
                    reason_rej = (
                        f"TypeBalanceGate|목표A:{_tp:.1f}% B:{100.0 - _tp:.1f}%|보유A:{na} B:{nb}|{lab}신규차단"
                    )
                    self._log_trade_to_csv(
                        curr_time, code, "REJECTED_BALANCE", float(price), 0, reason_rej, sig_type=lab
                    )
                except Exception:
                    pass
                return False
            if self.cash < s1_amt * 0.5:
                return False
            target_amt = s1_amt
            entry_n = 1
        else:
            if h_info['entry_n'] >= 2: return
            # market_trend는 handle_buy_execution 호출부에서 넘겨받기 어렵지만, s2_approval은 이미 위에서 체크됨
            # 하지만 안전을 위해 다시 체크하려면 파라미터 추가 필요 (일단 위 339라인에서 체크 완료됨)
            target_amt = s2_amt
            entry_n = 2
            sig_type = h_info.get("sig_type", "TYPE_A")
            
        qty = int(target_amt // (price * (1 + self.buy_f_rate)))
        if qty <= 0: return False
        real_amt = qty * price
        buy_fee = int(real_amt * self.buy_f_rate)
        
        if self.cash < (real_amt + buy_fee): return False

        self.cash -= (real_amt + buy_fee)
        if h_info is None:
            self.holdings[code] = {
                'qty': qty, 'avg_p': price, 'entry_n': 1, 'max_p': price, 
                'entry_date': curr_time, 'sig_type': sig_type,
                'atr_at_entry': last_row.get('ATR', 0) # Store ATR for SL
            }
        else:
            old_qty = h_info['qty']
            h_info['avg_p'] = (h_info['avg_p'] * old_qty + real_amt) / (old_qty + qty)
            h_info['qty'] += qty
            h_info['entry_n'] = 2
            # S2 체결 시점 ATR로 ATR_SL 기준 갱신(S1 ATR 고정이면 배수 조정 효과가 거의 없을 수 있음)
            try:
                if indicator_row is not None:
                    _atr = float(indicator_row.get('ATR', 0) or 0)
                    if _atr > 0:
                        h_info['atr_at_entry'] = _atr
            except Exception:
                pass
            
        # [v27.0] 전략 타입(A/B/S2)을 매수 사유(Reason) 접두어로 명시하여 리포트 집계 정확도 향상
        prefix = f"[{sig_type}] " if entry_n == 1 else "[Stage 2] "
        reason_str = prefix + sig_res['reason']
        if indicator_row is not None:
            try:
                rsi_val = indicator_row.get('RSI', 0)
                ma5_val = indicator_row.get('MA5', 0)
                osc_val = indicator_row.get('OSC', 0)
                reason_str += f"|RSI:{rsi_val:.1f}|MA5:{int(ma5_val):,}|OSC:{osc_val:.2f}"
            except: pass

        self.trades_history.append({
            'Date': curr_time, 'Code': code, 'Name': self.stock_name_map.get(code, code),
            'Action': f'BUY{entry_n}', 'Price': price, 'Qty': qty, 'Amount': real_amt, 'Fee': buy_fee,
            'Reason': reason_str, 'Cash': self.cash, 'Sig_Type': sig_type
        })
        self._log_trade_to_csv(curr_time, code, f'BUY{entry_n}', price, qty, reason_str, fee=buy_fee, sig_type=sig_type)
        return True

    def _execute_sell(self, code, price, curr_time, reason, indicator_row=None):
        """매도 실행 및 기록 (v8.2: 분할매도 유령로직 제거)"""
        if indicator_row is not None:
            try:
                rsi_val = indicator_row.get('RSI', 0)
                ma5_val = indicator_row.get('MA5', 0)
                osc_val = indicator_row.get('OSC', 0)
                if "RSI:" not in str(reason):
                    prefix = "|" if "|" not in str(reason) else ", "
                    reason += f"{prefix}RSI:{rsi_val:.1f}|MA5:{int(ma5_val):,}|OSC:{osc_val:.2f}"
            except: pass
            
        h_info = self.holdings.pop(code, None)
        if h_info is None: return
        
        sell_qty = h_info['qty']
        action = 'SELL'

        gross = sell_qty * price
        fee = int(gross * self.sell_f_rate)
        ret = (price / h_info['avg_p'] - 1) * 100
        self.cash += (gross - fee)
        
        sig_type = h_info.get('sig_type', 'TYPE_A')
        self.trades_history.append({
            'Date': curr_time, 'Code': code, 'Name': self.stock_name_map.get(code, code),
            'Action': action, 'Price': price, 'Qty': sell_qty, 'Amount': gross, 'Fee': fee,
            'Reason': reason, 'Return': ret, 'Cash': self.cash, 'Sig_Type': sig_type
        })
        self._log_trade_to_csv(curr_time, code, action, price, sell_qty, reason, ret=ret, fee=fee, sig_type=sig_type)

    def _log_trade_to_csv(self, dt, code, action, price, qty, reason, ret=0, fee=0, sig_type=None):
        """매매 한 건을 메모리 버퍼에 적재. 일 단위로 flush_trade_csv_buffer 호출됨."""
        name = self.stock_name_map.get(code, code)
        safe_reason = str(reason).replace(",", ";")
        row = [
            pd.to_datetime(dt).strftime("%Y-%m-%d %H:%M"),
            code,
            name,
            action,
            price,
            qty,
            f"{ret:.2f}",
            safe_reason,
            fee,
            sig_type,
        ]
        try:
            append_trade_csv_line(",".join(map(str, row)) + "\n")
        except Exception as e:
            try:
                print(f"[WARN] 매매 CSV 버퍼 적재 실패: {e}")
            except Exception:
                pass

    def _finalize_backtest(self, interval):
        print(f"[Dynamic] 시뮬레이션 완료. 총 {len(self.trades_history)}건의 매매(평가 포함) 발생.")
        trades_df = pd.DataFrame(self.trades_history)
        if not trades_df.empty:
            trades_df['Date'] = pd.to_datetime(trades_df['Date'])
            trd_path = os.path.join(BASE_DIR, "dynamic_trade_report.html")
            import reporting.trade_report
            importlib.reload(reporting.trade_report)
            from reporting.trade_report import generate_trade_report
            exit_trades = trades_df[trades_df['Action'].isin(['SELL', 'HOLD'])]
            win_rate = (len(exit_trades[exit_trades['Return'] > 0]) / max(1, len(exit_trades))) * 100
            avg_ret = (exit_trades['Return'] * exit_trades['Amount']).sum() / exit_trades['Amount'].sum() if not exit_trades.empty and exit_trades['Amount'].sum() > 0 else 0
            
            # [v25.3] Save equity curve to CSV for consistency with standard backtest
            try:
                equity_df = pd.DataFrame(self.daily_equity)
                if not equity_df.empty:
                    equity_df.set_index('date')[['equity']].rename(columns={'equity': 'Total'}).to_csv(
                        os.path.join(BASE_DIR, "backtest_portfolio_curve.csv"), encoding='utf-8-sig'
                    )
            except Exception as e:
                print(f"[Error] Failed to save portfolio curve: {e}")

            generate_trade_report(trades_df, interval, trd_path, len(exit_trades), win_rate, avg_ret, self.settings,
                                  start_date=self.start_date, end_date=self.end_date, daily_equity=self.daily_equity,
                                  high_precision=self.high_precision)
        return trades_df

    def _prepare_15m_cached_data(self, universe_schedule, start_date, end_date, callback):
        if callback: callback("15분봉 데이터 일괄 로드 중...", 0)
        u_codes = set()
        for codes in universe_schedule.values(): u_codes.update(codes)
        if not u_codes: return {}
        all_codes = list(u_codes)
        pad_start = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=21)).strftime("%Y-%m-%d")
        placeholders = ",".join(["?"] * len(all_codes))
        query = f"""
            SELECT b.code, b.datetime as time, b.open, b.high, b.low, b.close, b.volume,
                   NULL AS vol_power, NULL AS total_ask, NULL AS total_bid
            FROM bars_15m b
            WHERE b.code IN ({placeholders}) AND b.datetime BETWEEN ? AND ?
            ORDER BY b.code, b.datetime
        """
        with self.db._get_connection() as conn:
            df_all = pd.read_sql_query(query, conn, params=all_codes + [pad_start, end_date + " 23:59:59"])
        if df_all.empty: return {}
        df_all.columns = [c.capitalize() if c.lower() != 'time' else 'time' for c in df_all.columns]
        df_all['time'] = pd.to_datetime(df_all['time'])
        df_all.set_index('time', inplace=True)
        cached = {}
        for code, group in df_all.groupby('Code'):
            # [v31.5] Synthetic Metrics Pre-calculation for Dynamic Backtester
            if self.settings.get('use_synthetic_realtime', True):
                # [v31.5] Ensure numeric types for join columns
                for col in ['Vol_power', 'Total_ask', 'Total_bid']:
                    if col in group.columns:
                        group[col] = pd.to_numeric(group[col], errors='coerce')
                
                p_changes = group['Close'].pct_change().fillna(0).values
                vol_mas = group['Volume'].rolling(20).mean().bfill().values
                vol_ratios = (group['Volume'] / (vol_mas + 1e-10)).values
                vps = group['Vol_power'].values
                tasks = group['Total_ask'].values
                tbids = group['Total_bid'].values
                
                nan_vp = np.isnan(vps)
                if np.any(nan_vp):
                    vps[nan_vp] = synthesize_vol_power_vectorized(p_changes[nan_vp], vol_ratios[nan_vp])
                
                nan_ba = (np.isnan(tasks)) | (np.any(np.isnan(tbids)))
                if np.any(nan_ba):
                    r_vector = synthesize_bid_ask_ratio_vectorized(p_changes[nan_ba], vol_ratios[nan_ba], vps[nan_ba])
                    tasks[nan_ba] = 100000
                    tbids[nan_ba] = (100000 * r_vector).astype(int)
                
                group = group.copy() # In-place modification warning prevention
                group['Vol_power'] = vps
                group['Total_ask'] = tasks
                group['Total_bid'] = tbids
                group['bid_ask_ratio'] = group['Total_bid'] / (group['Total_ask'] + 1e-10)
                group['vol_power'] = vps # [v31.5] Maintain lowercase for backward compatibility if needed

            # [v16.92] Deep Optimization: Pre-calculate PULSE logic once per stock for the entire period
            # This avoids O(N*M) redundant .groupby().cumsum() calls in the daily loop
            df_ind = TradingStrategy.calculate_indicators(group, self.settings)
            
            is_ma_aligned = (df_ind['MA5'] > df_ind['MA20'])
            ma_group_id = (~is_ma_aligned).cumsum()
            df_ind['MA_Maint_Count'] = is_ma_aligned.groupby(ma_group_id).cumsum()
            
            is_rsi_above_50 = (df_ind['RSI'] >= 50.0)
            rsi_group_id = (df_ind['RSI'] < 50.0).cumsum()
            df_ind['RSI_Pulse_Count'] = is_rsi_above_50.groupby(rsi_group_id).cumsum()
            
            # [v16.92] Pre-calculate signals for the ENTIRE time series at once (Vectorized)
            full_sigs = TradingStrategy.get_buy_signals_detailed(
                df_ind, self.settings, market_trend="UP"
            )
            df_ind = df_ind.join(full_sigs.drop(columns='Signal', errors='ignore'))
            df_ind['Signal'] = full_sigs['Signal']
            
            df_ind['Ready'] = (df_ind.reset_index().index >= 19)
            cached[code] = df_ind
        return cached

    def _get_m1_chunk_on_demand(self, code, date_str, curr_time):
        """[v23.0] 필요한 시점에만 1분봉 데이터를 DB에서 가져와 캐싱 (성능 핵심)"""
        cache_key = (code, date_str)
        if cache_key not in self.cached_1m_data:
            query = """
                SELECT b.datetime as time, b.open, b.high, b.low, b.close, b.volume,
                       NULL AS vol_power, NULL AS total_ask, NULL AS total_bid
                FROM bars_1m b
                WHERE b.code = ? AND b.datetime BETWEEN ? AND ?
                ORDER BY b.datetime
            """
            with self.db._get_connection() as conn:
                df = pd.read_sql_query(query, conn, params=[code, date_str + " 09:00:00", date_str + " 15:45:00"])
            if df.empty:
                self.cached_1m_data[cache_key] = None
            else:
                df.columns = [c.capitalize() if c.lower() != 'time' else 'time' for c in df.columns]
                df['time'] = pd.to_datetime(df['time'])
                df.set_index('time', inplace=True)
                
                # [v31.5] Pre-calculate synthetic metrics for 1m data
                if self.settings.get('use_synthetic_realtime', True):
                    # [v31.5] Ensure numeric types
                    for col in ['Vol_power', 'Total_ask', 'Total_bid']:
                        if col in df.columns:
                            df[col] = pd.to_numeric(df[col], errors='coerce')

                    p_changes = df['Close'].pct_change().fillna(0).values
                    # 1m MA20
                    vol_mas = df['Volume'].rolling(20).mean().bfill().values
                    vol_ratios = (df['Volume'] / (vol_mas + 1e-10)).values
                    vps = df['Vol_power'].values
                    nan_vp = np.isnan(vps)
                    if np.any(nan_vp):
                        vps[nan_vp] = synthesize_vol_power_vectorized(p_changes[nan_vp], vol_ratios[nan_vp])
                    
                    ratios = synthesize_bid_ask_ratio_vectorized(p_changes, vol_ratios, vps)
                    
                    df = df.copy()
                    df['Vol_power'] = vps
                    df['vol_power'] = vps # Compatibility
                    df['bid_ask_ratio'] = ratios
                    df['ask_bid_ratio'] = 1.0 / (ratios + 1e-10)

                self.cached_1m_data[cache_key] = df
        
        m1_full = self.cached_1m_data[cache_key]
        if m1_full is not None:
            try:
                # [v37.5] 15분봉 시각(curr_time)은 해당 봉의 마감 시각이므로, T-14분~T 범위를 가져와야 정합성이 맞음
                return m1_full.loc[curr_time - timedelta(minutes=14) : curr_time]
            except: 
                return None
        return None

    def _prepare_1m_data(self, universe_schedule, start_date, end_date, callback):
        # [REMOVED] v23.0부터는 지연 로딩을 사용하므로 사전 로드가 불필요함
        return {}

    def _process_intra_bar_dynamic(self, code, m1_chunk, day_df_15m, today_sells, curr_15m_time):
        """[v36.2] 통합된 1분봉 정밀 시뮬레이션 (매도/매수 순차 처리 및 지표 동기화)"""
        # [v37.8] 전달받은 curr_15m_time을 사용하여 15분봉 데이터 매칭 (m1_dt_first 대신 사용)
        curr_15m = day_df_15m.loc[curr_15m_time] if day_df_15m is not None and curr_15m_time in day_df_15m.index else None
        
        h_info = self.holdings.get(code)
        
        # [v36.2] 단일 루프 구조로 개편하여 모든 종목에 대해 1분 단위 시뮬레이션 수행
        for m1_idx in range(len(m1_chunk)):
            m1_r = m1_chunk.iloc[m1_idx]
            m1_time = m1_chunk.index[m1_idx]
            
            # [v40.0] Optimized 1m Indicator Simulation
            full_15m = self.cached_15m_data.get(code)
            indicator_to_use = curr_15m # Fallback
            
            if full_15m is not None and curr_15m is not None:
                # [Perf] Pre-calculate values needed for incremental update once per 15m bar
                if m1_idx == 0:
                    base_idx = full_15m.index.get_loc(curr_15m.name)
                    # prev_15m is the last finished bar
                    prev_idx = max(0, base_idx - 1)
                    self._last_15m_dict = full_15m.iloc[prev_idx].to_dict()
                    
                    # Pre-calculate sums for MA
                    self._prices_tail = {
                        5: full_15m['Close'].iloc[max(0, base_idx-4):base_idx].sum(),
                        20: full_15m['Close'].iloc[max(0, base_idx-19):base_idx].sum(),
                        60: full_15m['Close'].iloc[max(0, base_idx-59):base_idx].sum(),
                        'rsi_vals': full_15m['Close'].iloc[max(0, base_idx-15):base_idx].values
                    }
                
                sim_15m = TradingStrategy.simulate_intrabar_indicators(self._last_15m_dict, m1_r, self.settings, hist_df_prices_tail=self._prices_tail)
                
                if sim_15m is not None:
                    # Sync static fields from current 15m bar
                    for k in [
                        "Signal",
                        "Ready",
                        "Sig_Type",
                        "S1_Simultaneous_AB",
                        "Volume",
                        "Prev_Day_Vol",
                        "Prev_Day_Vol_B",
                        "Prev_Day_Vol_S1_A",
                        "Prev_Day_Vol_S1_B",
                        "Prev_Day_Vol_S2_A",
                        "Prev_Day_Vol_S2_B",
                        "Vol_MA20",
                    ]:
                        if k in curr_15m: sim_15m[k] = curr_15m[k]
                    indicator_to_use = sim_15m

            # 1. 매도(Exit) 감시 - 보유 중인 경우 최우선 순위
            if h_info is not None:
                # 고가 갱신 (익절/트레일링용)
                if m1_r['High'] > h_info['max_p']: h_info['max_p'] = m1_r['High']
                
                # 보수적 백테: EXIT 판가·체결을 1분 저가 기준(종가 X)
                px_sell = _m1_sell_price_conservative(m1_r)
                # 1분 단위 정밀 매도 평가 (indicator_to_use에 시뮬레이션 데이터 전달)
                res = TradingStrategy.evaluate_position(
                    code, px_sell, h_info, self.settings,
                    indicator_row=indicator_to_use, # [v36.8] 시뮬레이션된 15분 지표 사용
                    current_time=m1_time,
                    is_realtime=False,
                    kwargs={'bid_ask_ratio': m1_r.get('bid_ask_ratio', 1.0), 'vol_power': m1_r.get('vol_power', 100.0)}
                )
                
                if res['action'] == 'EXIT':
                    # 익절(TP)도 체결 기록은 1분 저가(보수적) — reason은 TAKE_PROFIT 유지
                    e_price = px_sell
                    
                    reason = f"1M_{res['reason']}+{m1_idx}m"
                    self._execute_sell(code, e_price, m1_time, reason, indicator_row=indicator_to_use)
                    today_sells.add(code)
                    return # 매도 발생 시 해당 15분 블록 내 추가 작업 중단

            # 2. 매수(Buy/Addon) 감시 - 매수 신호가 있거나 추가매수 타겟인 경우
            if indicator_to_use is not None:
                # [v36.8] 시뮬레이션 지표 기반으로 신호 실시간 감지 (RSI, MACD 등)
                is_buy_signal = (indicator_to_use.get('Signal', False) and indicator_to_use.get('Ready', False))
                is_addon_target = (h_info is not None and h_info['entry_n'] < 2)
                
                if is_buy_signal or is_addon_target:
                    if h_info is None:
                        sig_type = self._effective_s1_sig_type_for_buy(indicator_to_use)
                    else:
                        sig_type = str(indicator_to_use.get('Sig_Type', 'TYPE_B'))
                    
                    # [Phase 2] Track-Signal 교차 검증 (1분봉 경로)
                    code_track = getattr(self, '_current_track_map', {}).get(code, 'A')
                    if (sig_type == 'TYPE_A' and code_track == 'B') or \
                       (sig_type == 'TYPE_B' and code_track == 'A'):
                        continue
                    
                    use_sb_for_type = self.use_sb_a if (sig_type == "TYPE_A" and not is_addon_target) else \
                                       self.use_sb_b if (sig_type == "TYPE_B" and not is_addon_target) else \
                                       (self.use_sb_s2_b if "TYPE_B" in str(h_info.get("sig_type", "")) else self.use_sb_s2_a)
                    
                    can_execute = True
                    reason_base = "1M_SIG" if not is_addon_target else f"1M_ADDON(S2)|R:{indicator_to_use.get('RSI',0):.1f}|O:{indicator_to_use.get('OSC',0):.1f}"
                    
                    if is_addon_target:
                        if not self._check_s2_approval(
                            code, h_info, indicator_to_use, market_trend="UP", curr_dt_for_delay=m1_time
                        ):
                            can_execute = False

                    if can_execute and use_sb_for_type:
                        # 고정밀: S1은 1분 합성 수급으로 체결 정밀도 유지.
                        # S2는 15분봉(curr_15m) 수급을 사용 — 1분 VP/BA가 임계를 거의 못 넘겨 추매 0건이 되는 현상 방지
                        # (일반 모드 15분 경로의 last_sig 수급과 동일 계열)
                        if is_addon_target and curr_15m is not None:
                            try:
                                _cr = curr_15m
                                if hasattr(_cr, "get"):
                                    _ba = float(_cr.get("bid_ask_ratio", 1.0))
                                    ab_ratio = float(
                                        _cr.get(
                                            "ask_bid_ratio",
                                            _cr.get("Ask_Bid_Ratio", 1.0 / (_ba + 1e-10)),
                                        )
                                    )
                                    v_power = float(_cr.get("vol_power", _cr.get("Vol_power", 100.0)))
                                else:
                                    raise TypeError("curr_15m row has no .get")
                            except (TypeError, ValueError, KeyError, AttributeError):
                                ab_ratio = m1_r.get(
                                    "ask_bid_ratio", 1.0 / (m1_r.get("bid_ask_ratio", 1.0) + 1e-10)
                                )
                                v_power = float(m1_r.get("vol_power", m1_r.get("Vol_power", 100.0)))
                        else:
                            ab_ratio = m1_r.get(
                                "ask_bid_ratio", 1.0 / (m1_r.get("bid_ask_ratio", 1.0) + 1e-10)
                            )
                            v_power = float(m1_r.get("vol_power", m1_r.get("Vol_power", 100.0)))
                        if ab_ratio < self.sb_ba or v_power < self.sb_vp:
                            can_execute = False
                    
                    if can_execute:
                        reason = reason_base
                        if use_sb_for_type:
                            reason = "SmartBuy|" + reason
                        # [v36.0] 1분 단위 타임스탬프 기록
                        reason += f"+{m1_idx}m"
                        executed = self._handle_buy_execution(
                            code,
                            {"reason": reason, "Sig_Type": sig_type},
                            m1_time,
                            m1_r,
                            indicator_row=indicator_to_use,
                            fill_price=_m1_buy_price_conservative(m1_r),
                        )
                        if executed:
                            return # 매수 실제 체결된 경우에만 해당 15분 청크 종료

            # [v36.8] 사용자 요청에 따라 'REJECTED_SIG' 리포트 로그 생성 중단 (클러터 제거)
            # 15분 내내 조건을 만족하지 못한 경우라도 리포트에는 실제 체결 내역만 남김

    def _check_s2_approval(
        self,
        code: str,
        h_info: dict[str, Any],
        last_row: Any,
        indicator_row: Any = None,
        market_trend: str = "UP",
        curr_dt_for_delay: Any = None,
    ) -> bool:
        """2차 매수 승인 — TradingStrategy._check_s2_approval과 동일 규칙(Type A/B 파라미터 완전 분리)."""
        ind = indicator_row if indicator_row is not None else last_row
        if ind is None:
            return True
        pos = {
            "entry_date": h_info.get("entry_date"),
            "avg_p": h_info.get("avg_p"),
            "avg_price": h_info.get("avg_p"),
            "sig_type": h_info.get("sig_type", ""),
        }
        try:
            return bool(
                TradingStrategy._check_s2_approval(
                    pos,
                    last_row,
                    self.settings,
                    indicator_row=ind,
                    market_trend=market_trend,
                    curr_dt=curr_dt_for_delay,
                )
            )
        except Exception:
            return False
