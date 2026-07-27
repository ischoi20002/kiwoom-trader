import pandas as pd
import os
import re
import time
import copy
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
from utils.krx_bars import m1_window_start_for_15m_end
from core.bt_hp_signal_bridge import compute_intrabar_indicator_step
from core.bt_hp_intrabar_unified import (
    evaluate_bt_hp_intrabar_unified,
    floor_sell_px_to_same_15m_bar,
    m1_buy_price_conservative,
    m1_float_ohlc,
    m1_sell_price_conservative,
)
from strategy.synthetic_realtime_fill import (
    synthesize_bid_ask_ratio_vectorized,
    synthesize_vol_power_vectorized,
)
from typing import Any, Callable, Dict, List, Optional


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
        # RT 정합: 감시 전 종목 × evaluate_bt_hp_intrabar_unified (기본 False=기존 Signal 게이트)
        self.rt_parity_intrabar = False
        # RT 정합 고속 하이브리드(close-now 사전필터·스로틀). 실시간에는 미적용.
        self.rt_parity_fast = False
        self.last_run_elapsed_sec: float = 0.0
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
        # 백테스트 첫 day(start_date)가 단일(예: start_date==end_date)인 경우에도
        # ref_day(선정 기준일)를 항상 "직전 영업일"로 맞추기 위한 값
        self._bt_ref_day_for_first: Optional[str] = None
        # prepare_dynamic_universe 4번째 반환값 — 일별 브레드스 레짐
        self.breadth_regime_by_day: dict[str, str] = {}
        # ATR_SL 청산 후 동일종목 S1 재진입 금지 만료일(일자 normalize)
        self._atr_sl_reentry_until: Dict[str, pd.Timestamp] = {}
        # 종목별 ATR_SL 청산일(연속 손절 쿨다운 판정용)
        self._atr_sl_exit_dates: Dict[str, List[pd.Timestamp]] = {}
        # DD 연동 사이징용 고점 자산
        self._equity_peak: float = 0.0

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
        try:
            self.atr_sl_reentry_cooldown_days = max(
                0, int(float(s.get("atr_sl_reentry_cooldown_days", 0) or 0))
            )
        except (TypeError, ValueError):
            self.atr_sl_reentry_cooldown_days = 0
        try:
            self.atr_sl_streak_lookback_days = max(
                1, int(float(s.get("atr_sl_streak_lookback_days", 10) or 10))
            )
        except (TypeError, ValueError):
            self.atr_sl_streak_lookback_days = 10
        try:
            self.atr_sl_streak_cooldown_days = max(
                0, int(float(s.get("atr_sl_streak_cooldown_days", 0) or 0))
            )
        except (TypeError, ValueError):
            self.atr_sl_streak_cooldown_days = 0
        try:
            self.atr_sl_streak_min_count = max(
                2, int(float(s.get("atr_sl_streak_min_count", 2) or 2))
            )
        except (TypeError, ValueError):
            self.atr_sl_streak_min_count = 2
        # DD(고점 대비 낙폭) 시 신규 매수 비중 축소
        try:
            self.use_dd_sizing = bool(s.get("use_dd_sizing", False))
        except Exception:
            self.use_dd_sizing = False
        try:
            self.dd_sizing_trigger_pct = abs(float(s.get("dd_sizing_trigger_pct", 10.0) or 10.0))
        except (TypeError, ValueError):
            self.dd_sizing_trigger_pct = 10.0
        try:
            self.dd_sizing_scale = float(s.get("dd_sizing_scale", 0.5) or 0.5)
            self.dd_sizing_scale = max(0.05, min(1.0, self.dd_sizing_scale))
        except (TypeError, ValueError):
            self.dd_sizing_scale = 0.5
        try:
            self.dd_slots_scale = float(s.get("dd_slots_scale", 1.0) or 1.0)
            self.dd_slots_scale = max(0.1, min(1.0, self.dd_slots_scale))
        except (TypeError, ValueError):
            self.dd_slots_scale = 1.0

    def _s1_blocked_by_atr_sl_cooldown(self, code: str, curr_time: Any) -> bool:
        """ATR_SL 이후 설정 일수 동안 동일 종목 S1 신규 진입 차단."""
        try:
            until = (getattr(self, "_atr_sl_reentry_until", None) or {}).get(code)
            if until is None:
                return False
            cur_d = pd.to_datetime(curr_time).normalize()
            return bool(cur_d <= pd.to_datetime(until).normalize())
        except Exception:
            return False

    def _note_atr_sl_exit_for_reentry_cooldown(self, code: str, curr_time: Any, reason: Any) -> None:
        """ATR_SL 청산 시 재진입 쿨다운 만료일을 기록(연속 손절 시 연장 가능)."""
        try:
            base_days = int(getattr(self, "atr_sl_reentry_cooldown_days", 0) or 0)
            if base_days <= 0:
                return
            rs = str(reason or "").upper()
            if "ATR_SL" not in rs:
                return
            nk = str(code).zfill(6)
            exit_d = pd.to_datetime(curr_time).normalize()
            lookback = int(getattr(self, "atr_sl_streak_lookback_days", 10) or 10)
            streak_days = int(getattr(self, "atr_sl_streak_cooldown_days", 0) or 0)
            min_count = int(getattr(self, "atr_sl_streak_min_count", 2) or 2)

            if not hasattr(self, "_atr_sl_exit_dates") or self._atr_sl_exit_dates is None:
                self._atr_sl_exit_dates = {}
            hist = list(self._atr_sl_exit_dates.get(nk) or [])
            hist.append(exit_d)
            cutoff = exit_d - pd.Timedelta(days=int(lookback))
            hist = [pd.to_datetime(d).normalize() for d in hist if pd.to_datetime(d).normalize() >= cutoff]
            self._atr_sl_exit_dates[nk] = hist

            days = int(base_days)
            if streak_days > base_days and len(hist) >= min_count:
                days = int(streak_days)

            new_until = exit_d + pd.Timedelta(days=int(days))
            if not hasattr(self, "_atr_sl_reentry_until") or self._atr_sl_reentry_until is None:
                self._atr_sl_reentry_until = {}
            prev = self._atr_sl_reentry_until.get(nk)
            if prev is not None and pd.to_datetime(prev).normalize() > new_until:
                return
            self._atr_sl_reentry_until[nk] = new_until
        except Exception:
            pass

    def _portfolio_equity_now(self) -> float:
        """현금 + 보유평가(last_p 우선, 없으면 평단)."""
        try:
            holdings_value = 0.0
            for _c, h in self.holdings.items():
                try:
                    px = float(h.get("last_p", h.get("avg_p", 0)) or 0)
                    holdings_value += float(h.get("qty", 0) or 0) * px
                except (TypeError, ValueError):
                    continue
            return float(self.cash) + float(holdings_value)
        except Exception:
            try:
                return float(self.cash)
            except Exception:
                return 0.0

    def _update_equity_peak(self, equity: Optional[float] = None) -> float:
        """고점 자산 갱신 후 현재 자산 반환."""
        try:
            eq = float(self._portfolio_equity_now() if equity is None else equity)
        except (TypeError, ValueError):
            eq = 0.0
        try:
            peak = float(getattr(self, "_equity_peak", 0) or 0)
            if eq > peak:
                self._equity_peak = eq
        except Exception:
            self._equity_peak = eq
        return eq

    def _dd_risk_scale(self) -> float:
        """
        고점 대비 낙폭이 임계 이상이면 dd_sizing_scale, 아니면 1.0.
        use_dd_sizing=False 이면 항상 1.0.
        """
        try:
            if not bool(getattr(self, "use_dd_sizing", False)):
                return 1.0
            eq = self._update_equity_peak()
            peak = float(getattr(self, "_equity_peak", 0) or 0)
            if peak <= 0:
                return 1.0
            dd_pct = (peak - eq) / peak * 100.0
            trig = float(getattr(self, "dd_sizing_trigger_pct", 10.0) or 10.0)
            if dd_pct >= trig:
                return float(getattr(self, "dd_sizing_scale", 0.5) or 0.5)
            return 1.0
        except Exception:
            return 1.0

    def _dd_effective_max_slots(self, max_sa: int, max_sb: int) -> tuple[int, int]:
        """DD 중이면 감시 슬롯도 축소(dd_slots_scale<1일 때)."""
        try:
            if not bool(getattr(self, "use_dd_sizing", False)):
                return int(max_sa), int(max_sb)
            scale = float(getattr(self, "dd_slots_scale", 1.0) or 1.0)
            if scale >= 0.999:
                return int(max_sa), int(max_sb)
            # 사이징과 동일 트리거일 때만 슬롯 축소
            if self._dd_risk_scale() >= 0.999:
                return int(max_sa), int(max_sb)
            sa = max(1, int(round(int(max_sa) * scale)))
            sb = max(1, int(round(int(max_sb) * scale)))
            return sa, sb
        except Exception:
            return int(max_sa), int(max_sb)
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
        영업일 캘린더(KRX)·유니버스·15분 캐시 로드. 시장 지표·VIX·레짐은 사용하지 않는다.
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
                "[Error] [Dynamic] 해당 기간에 영업일이 없습니다. "
                "기간·pandas_market_calendars(XKRX) 설치를 확인하세요."
            )
            return False
        print(f"[Dynamic] 백테스트 시작: {start_date} ~ {end_date} ({len(trading_days)} 영업일)")

        # 첫 시뮬레이션 day에 대해 ref_day를 항상 직전 영업일로 맞춘다.
        # (예: start_date=2026-05-07, end_date=2026-05-07이면 ref_day=2026-05-06 필요)
        try:
            sd = pd.to_datetime(start_date)
            prev_range_start = (sd - timedelta(days=30)).strftime("%Y-%m-%d")
            prev_range_end = (sd - timedelta(days=1)).strftime("%Y-%m-%d")
            prev_days = self.db.get_trading_days(prev_range_start, prev_range_end) or []
            if prev_days:
                self._bt_ref_day_for_first = pd.to_datetime(prev_days[-1]).strftime(
                    "%Y-%m-%d"
                )
            else:
                self._bt_ref_day_for_first = None
        except Exception:
            self._bt_ref_day_for_first = None

        # prepare_dynamic_universe 스케줄 생성도 ref_day에 해당하는 키가 포함되도록 시작일을 확장한다.
        sched_start = self._bt_ref_day_for_first or start_date
        t_start = time.time()
        _result = strategy_selector.prepare_dynamic_universe(
            self.settings, sched_start, end_date, self.db, callback if callback else print
        )
        if isinstance(_result, tuple) and len(_result) >= 3:
            universe_schedule = _result[0]
            self.schedule_a = _result[1]
            self.schedule_b = _result[2]
            try:
                self.breadth_regime_by_day = dict(_result[3]) if len(_result) >= 4 else {}
            except Exception:
                self.breadth_regime_by_day = {}
        else:
            universe_schedule = _result
            self.schedule_a = universe_schedule
            self.schedule_b = {}
            self.breadth_regime_by_day = {}
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
        idx_choice = self.settings.get("universe_index_choice", "KODEX200")
        self.stock_name_map = TradingStrategy.get_stock_name_map(idx_choice)
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
                ref_day = (
                    trading_days[prev_day_idx]
                    if prev_day_idx >= 0
                    else (self._bt_ref_day_for_first or current_day)
                )
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
        _run_t0 = time.time()
        self.last_run_elapsed_sec = 0.0
        self.high_precision = high_precision
        try:
            self.rt_parity_intrabar = bool(
                high_precision and self.settings.get("bt_rt_parity_intrabar", False)
            )
        except Exception:
            self.rt_parity_intrabar = False
        try:
            self.rt_parity_fast = bool(
                self.rt_parity_intrabar
                and self.settings.get("bt_rt_parity_fast", True)
            )
        except Exception:
            self.rt_parity_fast = bool(self.rt_parity_intrabar)
        # [v18.8] Date Normalization (YYYY-MM-DD)
        start_date = pd.to_datetime(start_date).strftime('%Y-%m-%d')
        end_date = pd.to_datetime(end_date).strftime('%Y-%m-%d')
        
        self.start_date = start_date
        self.end_date = end_date
        
        self._cache_run_hot_settings()
        try:
            self._atr_sl_reentry_until = {}
            self._atr_sl_exit_dates = {}
            self._equity_peak = float(getattr(self, "initial_cash", 0) or 0)
        except Exception:
            self._atr_sl_reentry_until = {}
            self._atr_sl_exit_dates = {}
            self._equity_peak = 0.0
        idx_choice = self.settings.get("universe_index_choice", "KODEX200")
        self.stock_name_map = TradingStrategy.get_stock_name_map(idx_choice)
        
        if callback: callback("백테스트 준비 중...", 0)
        
        if not self._bootstrap_portfolio_data(start_date, end_date, callback):
            try:
                self.last_run_elapsed_sec = float(time.time() - _run_t0)
            except Exception:
                self.last_run_elapsed_sec = 0.0
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
            ref_day = (
                trading_days[prev_day_idx]
                if prev_day_idx >= 0
                else (self._bt_ref_day_for_first or current_day)
            )
            
            top_codes_a = self.schedule_a.get(ref_day, [])
            top_codes_b = self.schedule_b.get(ref_day, [])
            top_codes = _interleave_track_codes(top_codes_a, top_codes_b)

            # [Breadth] 당일(또는 선정 기준일) 레짐 → 동시신호 우선순위 런타임 반영
            try:
                if bool(self.settings.get("use_market_breadth_gate", False)):
                    _reg_map = getattr(self, "breadth_regime_by_day", None) or {}
                    _reg = str(
                        _reg_map.get(current_day)
                        or _reg_map.get(ref_day)
                        or "UP"
                    )
                    _pol = strategy_selector.resolve_breadth_regime_policy(
                        _reg, self.settings
                    )
                    self.settings["breadth_runtime_regime"] = str(
                        _pol.get("regime", _reg)
                    )
                    self.settings["breadth_runtime_sig_priority"] = str(
                        _pol.get("sig_priority", "confluence")
                    )
                else:
                    self.settings.pop("breadth_runtime_sig_priority", None)
                    self.settings.pop("breadth_runtime_regime", None)
            except Exception:
                pass

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
            try:
                max_sa, max_sb = self._dd_effective_max_slots(max_sa, max_sb)
            except Exception:
                pass
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
                    # 대상 선정만 분기. 인트라바 판정은 항상 close-now(unified).
                    # - 고정밀(RT정합 OFF): 보유·완성 15분 Signal 종목만 1분 루프
                    #   → Signal=False면 스킵(속도). Signal=True여도 매수는 close-now 재판정.
                    # - RT정합 ON: 감시 목록 전부(실시간 미러와 동일 입구)
                    if self.rt_parity_intrabar:
                        m1_targets = list(monitoring_list)
                    else:
                        m1_targets = [
                            c
                            for c in monitoring_list
                            if (c in self.holdings)
                            or (
                                day_signals.get(c) is not None
                                and curr_time in day_signals[c].index
                                and day_signals[c].loc[curr_time, "Signal"]
                            )
                        ]

                    for code in m1_targets:
                        m1_chunk = self._get_m1_chunk_on_demand(code, current_day, curr_time)
                        if m1_chunk is not None and not m1_chunk.empty:
                            self._process_intra_bar_rt_parity(
                                code, m1_chunk, day_data.get(code), today_sells, curr_time
                            )
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
                                # [Phase 2] Track-Signal 교차 검증 (enforce_track_signal_match)
                                sig_type = self._effective_s1_sig_type_for_buy(last_sig)
                                code_track = track_map.get(code, 'A')
                                if not TradingStrategy.track_signal_match_allows(
                                    self.settings, sig_type, code_track
                                ):
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

            # 2-5. 일일 정산 (당일 분봉이 없어도 보유는 평단으로 평가 — 공백일 누락으로 자산 괴리 방지)
            holdings_mv = 0.0
            for c, h in self.holdings.items():
                try:
                    if c in day_data and not day_data[c].empty:
                        holdings_mv += float(h["qty"]) * float(
                            day_data[c]["Close"].iloc[-1]
                        )
                    else:
                        holdings_mv += float(h["qty"]) * float(
                            h.get("avg_p", 0) or 0
                        )
                except (TypeError, ValueError, KeyError):
                    try:
                        holdings_mv += float(h["qty"]) * float(h.get("avg_p", 0) or 0)
                    except (TypeError, ValueError, KeyError):
                        pass
            current_equity = self.cash + holdings_mv
            try:
                self._update_equity_peak(current_equity)
            except Exception:
                pass
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
                        'Date': last_time, 'Code': code, 'Name': self.stock_name_map.get(code, code),
                        'Action': 'HOLD', 'Price': last_price, 'Qty': h_info['qty'], 'Amount': h_info['qty'] * last_price, 
                        'Return': ret, 'Reason': reason, 'Cash': self.cash, 'Fee': sell_fee,
                        'Sig_Type': h_info.get('sig_type', 'TYPE_A')
                    })
        try:
            flush_trade_csv_buffer()
        except Exception:
            pass
        try:
            self.last_run_elapsed_sec = float(time.time() - _run_t0)
        except Exception:
            self.last_run_elapsed_sec = 0.0
        try:
            _mode = "RT정합고속" if (
                self.rt_parity_intrabar and getattr(self, "rt_parity_fast", False)
            ) else (
                "RT정합" if self.rt_parity_intrabar else (
                    "고정밀" if self.high_precision else "15분"
                )
            )
            print(
                f"[Dynamic] 모드={_mode} 소요={self.last_run_elapsed_sec:.1f}초"
            )
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

        # DD 연동: 고점 대비 낙폭 시 신규/추매 금액 축소
        try:
            _dd_sc = float(self._dd_risk_scale())
            if _dd_sc < 0.999:
                s1_amt = float(s1_amt) * _dd_sc
                s2_amt = float(s2_amt) * _dd_sc
        except Exception:
            pass

        if h_info is None:
            sig_type = str(sig_res.get("Sig_Type", "TYPE_A"))
            # ATR_SL 재진입 쿨다운
            try:
                if self._s1_blocked_by_atr_sl_cooldown(str(code).zfill(6), curr_time):
                    return False
            except Exception:
                pass
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
        try:
            self._note_atr_sl_exit_for_reentry_cooldown(str(code).zfill(6), curr_time, reason)
        except Exception:
            pass

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
                # [v38] 15m 행 시각=구간 끝: 09:15봉만 1m [09:00,09:15] (T-15), 나머지 (T-14) — utils.krx_bars
                _ct = (
                    pd.Timestamp(curr_time)
                    if not isinstance(curr_time, pd.Timestamp)
                    else curr_time
                )
                t0 = m1_window_start_for_15m_end(_ct)
                return m1_full.loc[t0:curr_time]
            except (TypeError, KeyError) as e:
                print(f"[_get_m1_chunk] 1m 슬라이스 실패: {e!r}", flush=True)
                return None
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as e:  # noqa: BLE001
                print(f"[_get_m1_chunk] 예기치 않은 실패: {e!r}", flush=True)
                return None
        return None

    def _prepare_1m_data(self, universe_schedule, start_date, end_date, callback):
        # [REMOVED] v23.0부터는 지연 로딩을 사용하므로 사전 로드가 불필요함
        return {}

    def _indicator_at_m1_idx_for_parity(
        self,
        full_hist: pd.DataFrame,
        curr_15m: Any,
        m1_chunk: pd.DataFrame,
        m1_idx: int,
    ) -> Any:
        """RT 정합 경로: 체결 시각의 인트라바 지표 행(실패 시 15분 행 폴백)."""
        holder: dict[str, Any] = {
            "initialized": False,
            "last_15m_dict": None,
            "prices_tail": None,
        }
        try:
            return compute_intrabar_indicator_step(
                self.settings, full_hist, curr_15m, m1_chunk, m1_idx, holder
            )
        except Exception:
            return curr_15m

    def _process_intra_bar_rt_parity(
        self,
        code: str,
        m1_chunk: pd.DataFrame,
        day_df_15m: Any,
        today_sells: Any,
        curr_15m_time: Any,
    ) -> None:
        """
        고정밀·RT정합 공통 인트라바: ``evaluate_bt_hp_intrabar_unified``(close-now)로
        EXIT/S2/S1 판정 후 체결 반영.

        호출 측에서 종목 집합만 좁힌다(완성 15분 Signal 게이트 vs 전 감시).
        완성봉 Signal을 1분에 정적 복사해 ``+0m`` 조기매수하던 구형 경로는 사용하지 않는다.
        """
        try:
            if code in today_sells:
                return
            if m1_chunk is None or getattr(m1_chunk, "empty", True):
                return
            if day_df_15m is None or curr_15m_time not in day_df_15m.index:
                return

            curr_15m = day_df_15m.loc[curr_15m_time]
            full = self.cached_15m_data.get(code)
            if full is None or getattr(full, "empty", True):
                return
            try:
                full_slice = full.loc[:curr_15m_time].copy()
            except (TypeError, KeyError):
                return

            h = self.holdings.get(code)
            pos = copy.deepcopy(h) if isinstance(h, dict) else None
            ct = str(getattr(self, "_current_track_map", {}).get(code, "AB"))
            if ct not in ("A", "B", "AB"):
                ct = "AB"

            # 고속 하이브리드는 RT정합 ON일 때만 (고정밀 게이트 경로에서는 완전 close-now)
            use_fast = bool(getattr(self, "rt_parity_intrabar", False)) and bool(
                getattr(self, "rt_parity_fast", False)
            )
            out = evaluate_bt_hp_intrabar_unified(
                self.settings,
                str(code).strip(),
                full_slice,
                m1_chunk,
                pos,
                ct,
                close_now_fast_skip=use_fast,
            )
            if not isinstance(out, dict):
                return

            ex = out.get("exit_first")
            if isinstance(ex, dict) and h is not None:
                try:
                    m1_idx = int(ex.get("m1_idx", 0))
                except (TypeError, ValueError):
                    return
                if m1_idx < 0 or m1_idx >= len(m1_chunk):
                    return
                m1_time = m1_chunk.index[m1_idx]
                px = float(ex.get("eval_price", 0) or 0)
                if px <= 0:
                    return
                ind = self._indicator_at_m1_idx_for_parity(
                    full, curr_15m, m1_chunk, m1_idx
                )
                rs = str(ex.get("reason", "EXIT"))
                reason = f"1M_{rs}+{m1_idx}m"
                self._execute_sell(code, px, m1_time, reason, indicator_row=ind)
                try:
                    today_sells.add(code)
                except Exception:
                    pass
                return

            s2 = out.get("stage2_addon")
            if isinstance(s2, dict) and h is not None:
                try:
                    m1_idx = int(s2.get("m1_idx", 0))
                except (TypeError, ValueError):
                    return
                if m1_idx < 0 or m1_idx >= len(m1_chunk):
                    return
                m1_r = m1_chunk.iloc[m1_idx]
                m1_time = m1_chunk.index[m1_idx]
                fill = float(s2.get("fill_price", 0) or 0)
                if fill <= 0:
                    return
                ind = self._indicator_at_m1_idx_for_parity(
                    full, curr_15m, m1_chunk, m1_idx
                )
                sig_type = str(h.get("sig_type", "TYPE_A"))
                reason = str(s2.get("reason", "1M_ADDON(S2)"))
                self._handle_buy_execution(
                    code,
                    {"reason": reason, "Sig_Type": sig_type},
                    m1_time,
                    m1_r,
                    indicator_row=ind,
                    fill_price=fill,
                )
                return

            if str(out.get("reason") or "") == "s1_executed_intrabar" and h is None:
                se = out.get("s1_eval")
                if not isinstance(se, dict):
                    return
                reason_s = str(se.get("reason", ""))
                m = re.search(r"\+(\d+)m$", reason_s)
                try:
                    m1_idx = int(m.group(1)) if m else max(0, len(m1_chunk) - 1)
                except (TypeError, ValueError, AttributeError):
                    m1_idx = max(0, len(m1_chunk) - 1)
                if m1_idx < 0 or m1_idx >= len(m1_chunk):
                    return
                m1_r = m1_chunk.iloc[m1_idx]
                m1_time = m1_chunk.index[m1_idx]
                ind = se.get("indicator_to_use")
                if not isinstance(ind, dict):
                    ind = self._indicator_at_m1_idx_for_parity(
                        full, curr_15m, m1_chunk, m1_idx
                    )
                sig_type = str(se.get("sig_type", "TYPE_A"))
                fill_px = float(m1_buy_price_conservative(m1_r))
                self._handle_buy_execution(
                    code,
                    {"reason": reason_s or "1M_SIG", "Sig_Type": sig_type},
                    m1_time,
                    m1_r,
                    indicator_row=ind,
                    fill_price=fill_px,
                )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            try:
                print(
                    f"[Dynamic] RT정합 인트라바 실패 ({code} @ {curr_15m_time}): {e!r}",
                    flush=True,
                )
            except Exception:
                pass

    def _process_intra_bar_dynamic(self, code, m1_chunk, day_df_15m, today_sells, curr_15m_time):
        """
        [호환] 고정밀 메인·검증 스크립트용 진입점.

        완성 15분 Signal 정적 복사 구형 루프는 쓰지 않고,
        close-now ``_process_intra_bar_rt_parity`` 로 위임한다.
        """
        return self._process_intra_bar_rt_parity(
            code, m1_chunk, day_df_15m, today_sells, curr_15m_time
        )

    def _process_intra_bar_dynamic_legacy(
        self, code, m1_chunk, day_df_15m, today_sells, curr_15m_time
    ):
        """[v36.2][legacy] Signal 정적 복사 1분 루프(비상·비교용, 메인 미사용)."""
        # [v37.8] 전달받은 curr_15m_time을 사용하여 15분봉 데이터 매칭 (m1_dt_first 대신 사용)
        curr_15m = day_df_15m.loc[curr_15m_time] if day_df_15m is not None and curr_15m_time in day_df_15m.index else None
        
        h_info = self.holdings.get(code)
        # 동적 고정밀 인트라바 지표: `core.bt_hp_signal_bridge` 와 실시간 감시가 동일 구현을 공유
        _intrabar_holder: dict[str, Any] = {
            "initialized": False,
            "last_15m_dict": None,
            "prices_tail": None,
        }

        # [v36.2] 단일 루프 구조로 개편하여 모든 종목에 대해 1분 단위 시뮬레이션 수행
        for m1_idx in range(len(m1_chunk)):
            m1_r = m1_chunk.iloc[m1_idx]
            m1_time = m1_chunk.index[m1_idx]
            
            # [v40.0] Optimized 1m Indicator Simulation
            full_15m = self.cached_15m_data.get(code)
            indicator_to_use = curr_15m # Fallback
            
            if full_15m is not None and curr_15m is not None:
                indicator_to_use = compute_intrabar_indicator_step(
                    self.settings,
                    full_15m,
                    curr_15m,
                    m1_chunk,
                    m1_idx,
                    _intrabar_holder,
                )

            # 1. 매도(Exit) 감시 - 보유 중인 경우 최우선 순위
            if h_info is not None:
                # 고가 갱신 (익절/트레일링용)
                if m1_r['High'] > h_info['max_p']: h_info['max_p'] = m1_r['High']
                
                # 보수적 백테: EXIT 판가·체결을 1분 저가 기준(종가 X)
                px_sell = m1_sell_price_conservative(m1_r)
                px_sell = floor_sell_px_to_same_15m_bar(px_sell, curr_15m)
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
                    if not TradingStrategy.track_signal_match_allows(
                        self.settings, sig_type, code_track
                    ):
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
                            fill_price=m1_buy_price_conservative(m1_r),
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
