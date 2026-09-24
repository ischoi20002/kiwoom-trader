import pandas as pd
import os
import pickle
import re
import tempfile
import time
import copy
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
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
from utils.krx_bars import (
    is_krx_aftermarket_last_15m_slot,
    is_krx_auction_market_fill_slot,
    is_krx_first_15m_slot,
    is_krx_last_15m_slot,
    is_krx_no_signal_eval_15m_slot,
    krx_same_day_1530_label,
    slice_m1_for_15m_end,
)
from utils.krx_session_hours import get_session_hours
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
from typing import Any, Callable, Dict, List, Optional, Tuple
from core.bt_prep_cache import (
    cache_enabled as _bt_prep_cache_enabled,
    get_ohlcv as _bt_cache_get_ohlcv,
    get_signals as _bt_cache_get_signals,
    get_universe as _bt_cache_get_universe,
    ohlcv_fingerprint as _bt_ohlcv_fingerprint,
    put_ohlcv as _bt_cache_put_ohlcv,
    put_signals as _bt_cache_put_signals,
    put_universe as _bt_cache_put_universe,
    signal_fingerprint as _bt_signal_fingerprint,
    universe_fingerprint as _bt_universe_fingerprint,
)
from core.bt_sig15_workers import (
    indicators_and_signals_15m_frame,
    sig15_pool_init as _sig15_pool_init,
    sig15_pool_job as _sig15_pool_job,
)


def snapshot_holdings_for_same_slot_s1(
    holdings: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    같은 15분 슬롯 S1을 동시주문으로 볼 때 쓰는 보유 스냅샷.

    슬롯 안에서 먼저 체결된 종목이 다음 종목의 동수 타이브레이크·트랙 판정을
    바꾸지 않게, 얕은 복사본을 고정한다. (실시간은 봉 마감 평가 시점에
    같은 슬롯 신규 매수가 아직 보유에 없는 것과 맞춘다.)
    """
    try:
        return dict(holdings or {})
    except Exception:
        return {}


def _dual_track_new_candidates(
    top_codes_a: List[str],
    top_codes_b: List[str],
    holdings_codes: List[str],
    max_sa: int,
    max_sb: int,
    top_codes_c: Optional[List[str]] = None,
    max_sc: int = 0,
) -> List[str]:
    """
    신규 감시 후보를 Track A/B/C별 슬롯 상한으로 잘라 합칩니다.
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
        new_c: list[str] = []
        for c in top_codes_c or []:
            if len(new_c) >= max(0, max_sc):
                break
            if c not in hc and c not in picked:
                picked.add(c)
                new_c.append(str(c))
        return _interleave_track_codes(
            _interleave_track_codes(new_a, new_b),
            new_c,
        )
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
        self.bt_1530_signal_next_open_fill = bool(
            self.settings.get("next_open_1530_enabled", False)
        )
        self.bt_1945_signal_next_open_fill = bool(
            self.settings.get("next_open_1945_enabled", True)
        )
        # BT 전용: 09:00 슬롯에서 첫 1분봉으로 조기 평가 후, 같은 슬롯 완성 15분 평가 이어감
        self.bt_open_auction_pass_enabled = bool(
            self.settings.get("bt_open_auction_pass_enabled", False)
        )
        # 15:30 신호 허용(모의 next_open·실전 NXT 공통). 체결 경로만 갈라짐.
        self.settings["_bt_allow_1530_signal_eval"] = bool(
            self.bt_1530_signal_next_open_fill
        )
        self.settings["allow_1530_signal_eval"] = bool(
            self.bt_1530_signal_next_open_fill
        )
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
        # 현재 15분 슬롯 S1 타입 판정용. None=미고정, dict=슬롯 시작 보유.
        self._s1_slot_holdings_snapshot: Optional[Dict[str, Any]] = None
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
        # prepare_dynamic_universe 4번째 반환값 — 일별 transition 레짐
        self.transition_regime_by_day: dict[str, str] = {}
        # ATR_SL 청산 후 동일종목 S1 재진입 금지 만료일(일자 normalize)
        self._atr_sl_reentry_until: Dict[str, pd.Timestamp] = {}
        # 종목별 ATR_SL 청산일(연속 손절 쿨다운 판정용)
        self._atr_sl_exit_dates: Dict[str, List[pd.Timestamp]] = {}
        # 일일 신규 Type A S1 건수 — 체결일(YYYY-MM-DD) 키
        # (NEXT_OPEN은 전일 루프에서 체결 처리되므로 일자별 dict 필요)
        self._daily_new_s1_a_by_day: Dict[str, int] = {}
        # G1 로테이션 A 가드: ref_day → True 이면 당일 신규 TYPE_A 차단
        self.rotation_a_guard_g1_by_ref_day: Dict[str, bool] = {}
        # R5: RS 순위지속성 저하 → Type A 청산 타이트 (전일 ref_day 키)
        self.rs_persist_guard_by_ref_day: Dict[str, bool] = {}
        self._r5_exit_tight_today: bool = False
        # R5 조건부 A 청산 타이트: 평소값 백업 (일별 복원용)
        self._r5_exit_base_flash_a: Optional[float] = None
        self._r5_exit_base_atr_sl_a: Optional[float] = None
        self._bt_trading_days: List[str] = []

    def _normalize_bt_day(self, day: Any) -> str:
        """BT 일자 키를 YYYY-MM-DD 로 통일."""
        try:
            if day is None or day == "":
                return ""
            return pd.Timestamp(day).strftime("%Y-%m-%d")
        except Exception:
            try:
                return str(day)[:10]
            except Exception:
                return ""

    def _apply_r5_exit_tighten(self, active: bool) -> None:
        """
        R5 발동일에 Type A Flash/ATR SL만 임시 타이트.
        진입은 막지 않음. evaluate_position이 settings를 매 봉 읽으므로 일별 덮어쓰기면 충분.
        """
        try:
            if self._r5_exit_base_flash_a is None:
                self._r5_exit_base_flash_a = float(
                    self.settings.get("s1_a_flash_exit_pct", 4.0) or 4.0
                )
            if self._r5_exit_base_atr_sl_a is None:
                self._r5_exit_base_atr_sl_a = float(
                    self.settings.get("atr_multiplier_sl_a", 2.8) or 2.8
                )
            if active:
                flash_t = float(
                    self.settings.get("rs_persist_exit_flash_a", 2.5) or 2.5
                )
                atr_t = float(
                    self.settings.get("rs_persist_exit_atr_sl_a", 2.0) or 2.0
                )
                self.settings["s1_a_flash_exit_pct"] = flash_t
                self.settings["atr_multiplier_sl_a"] = atr_t
                self._r5_exit_tight_today = True
            else:
                self.settings["s1_a_flash_exit_pct"] = float(
                    self._r5_exit_base_flash_a
                )
                self.settings["atr_multiplier_sl_a"] = float(
                    self._r5_exit_base_atr_sl_a
                )
                self._r5_exit_tight_today = False
        except Exception:
            self._r5_exit_tight_today = False

    def _max_daily_new_s1_a(self) -> int:
        """일일 Type A 신규 S1 상한. 0 이하면 무제한."""
        try:
            return int(self.settings.get("max_daily_new_s1_a", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _reset_daily_new_s1_a(self, day: str) -> None:
        """영업일 시작 훅(호환). 실제 카운트는 체결일 dict 기준."""
        try:
            d = self._normalize_bt_day(day)
            self._daily_new_s1_a_date = d
            # 당일 키는 유지(전일 NEXT_OPEN이 이미 채워 둔 카운트 보존)
            if not hasattr(self, "_daily_new_s1_a_by_day") or self._daily_new_s1_a_by_day is None:
                self._daily_new_s1_a_by_day = {}
            self._daily_new_s1_a_count = int(self._daily_new_s1_a_by_day.get(d, 0) or 0)
        except Exception:
            self._daily_new_s1_a_date = ""
            self._daily_new_s1_a_count = 0

    def _can_open_new_s1_a(self, day: Optional[str] = None) -> bool:
        """체결일 기준 Type A S1 추가 진입 가능 여부."""
        lim = self._max_daily_new_s1_a()
        if lim <= 0:
            return True
        try:
            d = self._normalize_bt_day(
                day if day is not None else getattr(self, "_daily_new_s1_a_date", "")
            )
            if not d:
                return True
            by = getattr(self, "_daily_new_s1_a_by_day", None)
            if not isinstance(by, dict):
                by = {}
                self._daily_new_s1_a_by_day = by
            return int(by.get(d, 0) or 0) < lim
        except (TypeError, ValueError):
            return True

    def _note_new_s1_a(self, day: Optional[str] = None) -> None:
        """Type A S1 체결 후 체결일 카운터 증가."""
        try:
            d = self._normalize_bt_day(
                day if day is not None else getattr(self, "_daily_new_s1_a_date", "")
            )
            if not d:
                return
            by = getattr(self, "_daily_new_s1_a_by_day", None)
            if not isinstance(by, dict):
                by = {}
                self._daily_new_s1_a_by_day = by
            by[d] = int(by.get(d, 0) or 0) + 1
            if d == str(getattr(self, "_daily_new_s1_a_date", "") or ""):
                self._daily_new_s1_a_count = by[d]
        except (TypeError, ValueError):
            pass

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

    def _bump_holding_bar_high(self, h_info: Optional[dict], high: Any) -> None:
        """Flash/ATR High 모드에 맞춰 max_p / max_p_high 갱신."""
        if not isinstance(h_info, dict):
            return
        try:
            use_f = bool(self.settings.get("use_flash_peak_bar_high", False))
            use_a = bool(self.settings.get("use_atr_peak_bar_high", False))
        except Exception:
            use_f, use_a = False, False
        if not use_f and not use_a:
            return
        try:
            hi = float(high)
        except (TypeError, ValueError):
            return
        if hi <= 0:
            return
        if use_a:
            try:
                if hi > float(h_info.get("max_p_high", 0) or 0):
                    h_info["max_p_high"] = hi
            except Exception:
                h_info["max_p_high"] = hi
        if use_f:
            try:
                if hi > float(h_info.get("max_p", 0) or 0):
                    h_info["max_p"] = hi
                    h_info["max_price"] = hi
            except Exception:
                pass

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

    def _begin_s1_slot_holdings_snapshot(self) -> None:
        """현재 15분 시각 처리 직전 보유를 S1 타입·동수 판정용으로 고정."""
        try:
            self._s1_slot_holdings_snapshot = snapshot_holdings_for_same_slot_s1(
                self.holdings
            )
        except Exception:
            self._s1_slot_holdings_snapshot = {}

    def _s1_holdings_for_type_decision(self) -> Dict[str, Any]:
        """S1 확정 타입·동수 차단에 쓰는 보유. 슬롯 스냅샷이 있으면 그것을 우선."""
        try:
            snap = getattr(self, "_s1_slot_holdings_snapshot", None)
            if snap is not None and isinstance(snap, dict):
                return snap
        except Exception:
            pass
        try:
            return self.holdings if isinstance(self.holdings, dict) else {}
        except Exception:
            return {}

    def _effective_s1_sig_type_for_buy(
        self, row: Any, code_track: Any = None
    ) -> str:
        """15분 행·1분 indicator dict 등에서 Sig_Type 확정(동시신호+보유 A/B 동점 시 B 우선).

        보유는 **현재 슬롯 시작 스냅샷**을 쓴다. 같은 봉에서 먼저 산 종목이
        다음 종목의 동수 타이브레이크를 바꾸지 않게 한다.

        ``code_track`` 은 ``same_bar_sig_prefer_sel_track`` 이 켜졌을 때만 사용된다.
        """
        return TradingStrategy.effective_s1_sig_type_for_buy(
            self.settings,
            row,
            self._s1_holdings_for_type_decision(),
            code_track,
        )

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

        # 파라미터 튜닝은 동일한 일별 유니버스·15분 지표를 반복 사용합니다.
        # 호출자가 명시적으로 주입한 읽기 전용 컨텍스트만 재사용하여 일반 실행과 분리합니다.
        try:
            precomputed = getattr(self, "_precomputed_bt_context", None)
            cached_start = pd.to_datetime((precomputed or {}).get("start_date"))
            cached_end = pd.to_datetime((precomputed or {}).get("end_date"))
            requested_start = pd.to_datetime(start_date)
            requested_end = pd.to_datetime(end_date)
            reusable = (
                isinstance(precomputed, dict)
                and cached_start <= requested_start
                and cached_end >= requested_end
                and isinstance(precomputed.get("cached_15m_data"), dict)
            )
        except (TypeError, ValueError, KeyError):
            precomputed = None
            reusable = False
        if reusable and isinstance(precomputed, dict):
            try:
                self.schedule_a = precomputed.get("schedule_a", {}) or {}
                self.schedule_b = precomputed.get("schedule_b", {}) or {}
                self.schedule_c = precomputed.get("schedule_c", {}) or {}
                self.schedule_c_meta = precomputed.get("schedule_c_meta", {}) or {}
                self.transition_regime_by_day = (
                    precomputed.get("transition_regime_by_day", {}) or {}
                )
                self.cached_15m_data = precomputed["cached_15m_data"]
                self.cached_1m_data = {}
                self._bt_trading_days = trading_days
                print(
                    "[Dynamic] 사전 계산 컨텍스트 재사용 "
                    f"({len(self.cached_15m_data)}종목)"
                )
                return True
            except (KeyError, TypeError, ValueError) as exc:
                print(f"[Dynamic] 사전 계산 컨텍스트 재사용 실패: {exc}")

        # prepare_dynamic_universe 시작일:
        # 1) 첫 시뮬일의 ref_day(직전 영업일) 키가 스케줄에 포함되도록 최소 확장
        # 2) transition share_delta 등 일별 통계 워밍업(지표 EWM 은 end 앵커 lookback)
        sched_anchor = self._bt_ref_day_for_first or start_date
        sched_start = str(sched_anchor)
        try:
            warm_n = int(
                self.settings.get("bt_universe_schedule_warmup_trading_days", 20) or 0
            )
        except Exception:
            warm_n = 20
        try:
            warm_n = max(0, min(int(warm_n), 60))
        except Exception:
            warm_n = 20
        if warm_n > 0:
            try:
                anchor_dt = pd.to_datetime(sched_anchor)
                warm_from = (
                    anchor_dt - timedelta(days=max(45, warm_n * 3))
                ).strftime("%Y-%m-%d")
                warm_to = (anchor_dt - timedelta(days=1)).strftime("%Y-%m-%d")
                warm_days = self.db.get_trading_days(warm_from, warm_to) or []
                if warm_days:
                    pick = (
                        warm_days[-warm_n:]
                        if len(warm_days) >= warm_n
                        else list(warm_days)
                    )
                    sched_start = pd.to_datetime(pick[0]).strftime("%Y-%m-%d")
            except Exception:
                sched_start = str(sched_anchor)
        try:
            if str(sched_start) != str(sched_anchor):
                print(
                    f"[Dynamic] 유니버스 선정 워밍업: {sched_start} ~ {end_date} "
                    f"(ref={sched_anchor}, +{warm_n}영업일)"
                )
        except Exception:
            pass
        t_start = time.time()
        use_prep_cache = _bt_prep_cache_enabled(self.settings)
        universe_schedule = None
        univ_fp = ""
        try:
            extra_files = self._bt_prep_extra_files()
            db_path = getattr(self.db, "db_path", None) or "trading_data.db"
            univ_fp = _bt_universe_fingerprint(
                self.settings,
                sched_start=str(sched_start),
                end_date=str(end_date),
                db_path=str(db_path),
                extra_files=extra_files,
            )
        except Exception:
            univ_fp = ""
            use_prep_cache = False

        cached_univ = _bt_cache_get_universe(univ_fp) if (use_prep_cache and univ_fp) else None
        if isinstance(cached_univ, dict) and cached_univ.get("schedule") is not None:
            try:
                universe_schedule = cached_univ.get("schedule") or {}
                self.schedule_a = cached_univ.get("schedule_a") or {}
                self.schedule_b = cached_univ.get("schedule_b") or {}
                self.schedule_c = cached_univ.get("schedule_c") or {}
                self.schedule_c_meta = cached_univ.get("schedule_c_meta") or {}
                self.transition_regime_by_day = dict(
                    cached_univ.get("transition_regime_by_day") or {}
                )
                self.rotation_a_guard_g1_by_ref_day = dict(
                    cached_univ.get("rotation_a_guard_g1_by_ref_day") or {}
                )
                self.rs_persist_guard_by_ref_day = dict(
                    cached_univ.get("rs_persist_guard_by_ref_day") or {}
                )
                self.universe_schedule = universe_schedule or {}
                # 캐시에 G1 없으면 러너 사전계산으로 보완
                try:
                    _pre_g1 = self.settings.pop("_precomputed_rotation_a_guard_g1", None)
                    if (
                        isinstance(_pre_g1, dict)
                        and _pre_g1
                        and not self.rotation_a_guard_g1_by_ref_day
                    ):
                        self.rotation_a_guard_g1_by_ref_day = {
                            str(k): bool(v) for k, v in _pre_g1.items()
                        }
                    if bool(self.settings.get("use_rotation_a_guard_g1", False)):
                        _n_g1 = sum(
                            1
                            for v in (self.rotation_a_guard_g1_by_ref_day or {}).values()
                            if v
                        )
                        print(
                            f"[G1] 맵(캐시 HIT): 발동 {_n_g1}일",
                            flush=True,
                        )
                    if bool(self.settings.get("use_rs_persist_guard", False)):
                        _n_r5 = sum(
                            1
                            for v in (self.rs_persist_guard_by_ref_day or {}).values()
                            if v
                        )
                        print(
                            f"[R5] 맵(캐시 HIT): 발동 {_n_r5}일",
                            flush=True,
                        )
                except Exception:
                    pass
                t_duration = time.time() - t_start
                msg = f"유니버스 캐시 HIT ({t_duration:.2f}초)"
                print(f"[Done] {msg}")
                if callback:
                    callback(msg, 5)
            except Exception:
                cached_univ = None
                universe_schedule = None

        if universe_schedule is None:
            _result = strategy_selector.prepare_dynamic_universe(
                self.settings,
                sched_start,
                end_date,
                self.db,
                callback if callback else print,
                include_track_c_schedule=True,
            )
            if isinstance(_result, tuple) and len(_result) >= 3:
                universe_schedule = _result[0]
                self.schedule_a = _result[1]
                self.schedule_b = _result[2]
                try:
                    self.transition_regime_by_day = dict(_result[3]) if len(_result) >= 4 else {}
                except Exception:
                    self.transition_regime_by_day = {}
                try:
                    self.schedule_c = dict(_result[4]) if len(_result) >= 5 else {}
                except Exception:
                    self.schedule_c = {}
                try:
                    self.schedule_c_meta = dict(_result[5]) if len(_result) >= 6 else {}
                except Exception:
                    self.schedule_c_meta = {}
                try:
                    self.rotation_a_guard_g1_by_ref_day = (
                        dict(_result[6]) if len(_result) >= 7 else {}
                    )
                except Exception:
                    self.rotation_a_guard_g1_by_ref_day = {}
                try:
                    self.rs_persist_guard_by_ref_day = (
                        dict(_result[7]) if len(_result) >= 8 else {}
                    )
                except Exception:
                    self.rs_persist_guard_by_ref_day = {}
            else:
                universe_schedule = _result
                self.schedule_a = universe_schedule
                self.schedule_b = {}
                self.schedule_c = {}
                self.schedule_c_meta = {}
                self.transition_regime_by_day = {}
                self.rotation_a_guard_g1_by_ref_day = {}
                self.rs_persist_guard_by_ref_day = {}
            self.universe_schedule = universe_schedule or {}
            t_duration = time.time() - t_start
            msg = f"사전 계산 완료 ({t_duration:.2f}초)"
            print(f"[Done] {msg}")
            if callback:
                callback(msg, 5)

            # G1: prepare 결과 우선, 없으면 러너 사전계산 주입
            try:
                if not getattr(self, "rotation_a_guard_g1_by_ref_day", None):
                    self.rotation_a_guard_g1_by_ref_day = {}
                _pre_g1 = self.settings.pop("_precomputed_rotation_a_guard_g1", None)
                if isinstance(_pre_g1, dict) and _pre_g1:
                    # prepare 맵이 비어 있을 때만 사전계산으로 채움
                    if not self.rotation_a_guard_g1_by_ref_day:
                        self.rotation_a_guard_g1_by_ref_day = {
                            str(k): bool(v) for k, v in _pre_g1.items()
                        }
                _n_g1 = sum(
                    1
                    for v in (self.rotation_a_guard_g1_by_ref_day or {}).values()
                    if v
                )
                if bool(self.settings.get("use_rotation_a_guard_g1", False)):
                    print(f"[G1] 맵 준비: 발동 {_n_g1}일", flush=True)
                if not getattr(self, "rs_persist_guard_by_ref_day", None):
                    self.rs_persist_guard_by_ref_day = {}
                _n_r5 = sum(
                    1
                    for v in (self.rs_persist_guard_by_ref_day or {}).values()
                    if v
                )
                if bool(self.settings.get("use_rs_persist_guard", False)):
                    print(f"[R5] 맵 준비: 발동 {_n_r5}일", flush=True)
            except Exception as _g1_ex:
                try:
                    print(f"[G1] 맵 로드 생략: {_g1_ex}", flush=True)
                except Exception:
                    pass
                if not getattr(self, "rotation_a_guard_g1_by_ref_day", None):
                    self.rotation_a_guard_g1_by_ref_day = {}
                if not getattr(self, "rs_persist_guard_by_ref_day", None):
                    self.rs_persist_guard_by_ref_day = {}

            if use_prep_cache and univ_fp:
                try:
                    _bt_cache_put_universe(
                        univ_fp,
                        {
                            "schedule": universe_schedule,
                            "schedule_a": self.schedule_a,
                            "schedule_b": self.schedule_b,
                            "schedule_c": getattr(self, "schedule_c", {}) or {},
                            "schedule_c_meta": getattr(self, "schedule_c_meta", {}) or {},
                            "transition_regime_by_day": self.transition_regime_by_day,
                            "rotation_a_guard_g1_by_ref_day": getattr(
                                self, "rotation_a_guard_g1_by_ref_day", {}
                            )
                            or {},
                            "rs_persist_guard_by_ref_day": getattr(
                                self, "rs_persist_guard_by_ref_day", {}
                            )
                            or {},
                        },
                    )
                except Exception:
                    pass

        if not universe_schedule:
            if callback:
                callback("  [Warning] 선택된 종목이 없습니다. 사전 계산(Phase 2)을 건너뜁니다.", 0)
            self.cached_15m_data = {}
        else:
            self.cached_15m_data = self._prepare_15m_cached_data(
                universe_schedule,
                start_date,
                end_date,
                callback,
                univ_fp=univ_fp if use_prep_cache else "",
            )
        self.cached_1m_data = {}
        self._bt_trading_days = trading_days
        return True

    def _bt_prep_extra_files(self) -> List[str]:
        """유니버스 지문에 넣을 부가 파일 (CSV 유니버스 변경 감지)."""
        files: List[str] = []
        try:
            # KODEX200 구성 유니버스 폐기 — KOSPI CSV만 지문에 포함
            files.extend(["kospi_universe.csv", "kospi_top_500.csv"])
        except Exception:
            pass
        return files

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
        idx_choice = self.settings.get("universe_index_choice", "KOSPI200")
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
                max_sa = int(self.settings.get("max_slots_a", 20))
                max_sb = int(self.settings.get("max_slots_b", 10))
                if not top_codes:
                    # run() 과 동일: 느린 실시간 선정 폴백 없이 스케줄만 사용
                    monitoring_list = []
                elif top_codes_a or top_codes_b:
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
            self._daily_new_s1_a_by_day = {}
            self._daily_new_s1_a_date = ""
            self._daily_new_s1_a_count = 0
        except Exception:
            self._atr_sl_reentry_until = {}
            self._atr_sl_exit_dates = {}
            self._daily_new_s1_a_by_day = {}
            self._daily_new_s1_a_date = ""
            self._daily_new_s1_a_count = 0
        idx_choice = self.settings.get("universe_index_choice", "KOSPI200")
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
        if not bool(getattr(self, "_suppress_backtest_artifacts", False)):
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

            # [실험] 날짜별 파라미터 스케줄 — 보유 포지션을 유지한 채 청산 키 등만 교체
            # tools/walkforward_* 에서 tester._param_schedule_by_day = {day: {key: val}}
            try:
                _ps = getattr(self, "_param_schedule_by_day", None)
                if isinstance(_ps, dict) and current_day in _ps:
                    _ov = _ps.get(current_day)
                    if isinstance(_ov, dict) and _ov:
                        self.settings.update(_ov)
            except Exception:
                pass
            
            # 2-1. 오전 종목 선정 (사전 계산된 스케줄 활용, Track A/B 분리)
            prev_day_idx = idx - 1
            ref_day = (
                trading_days[prev_day_idx]
                if prev_day_idx >= 0
                else (self._bt_ref_day_for_first or current_day)
            )
            
            top_codes_a = self.schedule_a.get(ref_day, [])
            top_codes_b = self.schedule_b.get(ref_day, [])
            top_codes_c = (
                self.schedule_c.get(ref_day, [])
                if (
                    bool(self.settings.get("use_type_c", False))
                    and not self.high_precision
                )
                else []
            )
            self._current_box_c_meta = (
                getattr(self, "schedule_c_meta", {}).get(ref_day, {}) or {}
            )
            top_codes = _interleave_track_codes(
                _interleave_track_codes(top_codes_a, top_codes_b),
                top_codes_c,
            )

            # [Transition] 레짐 전환일: tiered — TRANSITION=S2 선택, HARD=S1+S2
            try:
                _block_s1 = False
                _block_s2 = False
                _tr = "OFF"
                if bool(self.settings.get("use_transition_gate", False)):
                    _tr_map = getattr(self, "transition_regime_by_day", None) or {}
                    _tr = str(
                        _tr_map.get(ref_day)
                        or _tr_map.get(current_day)
                        or "OFF"
                    )
                    if _tr == "TRANSITION" and bool(
                        self.settings.get("transition_ignore_soft", False)
                    ):
                        _tr = "OFF"
                    _block_s1, _block_s2 = strategy_selector.resolve_transition_runtime_blocks(
                        _tr, self.settings
                    )
                    self.settings["transition_runtime_regime"] = _tr
                else:
                    self.settings.pop("transition_runtime_regime", None)

                self.settings.pop("index_dd_runtime_block_s1", None)
                self.settings["transition_runtime_block_s1"] = _block_s1
                self.settings["transition_runtime_block_s2"] = _block_s2

                # G1: Type A 신규 차단 / R5: Type A 청산만 타이트
                _g1_on = False
                try:
                    if bool(self.settings.get("use_rotation_a_guard_g1", False)):
                        _g1_map = (
                            getattr(self, "rotation_a_guard_g1_by_ref_day", None) or {}
                        )
                        _g1_on = bool(_g1_map.get(str(ref_day), False))
                except Exception:
                    _g1_on = False
                try:
                    if bool(self.settings.get("use_rs_persist_guard", False)):
                        _r5_map = (
                            getattr(self, "rs_persist_guard_by_ref_day", None) or {}
                        )
                        _r5_on = bool(_r5_map.get(str(ref_day), False))
                        self._apply_r5_exit_tighten(bool(_r5_on))
                    else:
                        self._apply_r5_exit_tighten(False)
                except Exception:
                    try:
                        self._apply_r5_exit_tighten(False)
                    except Exception:
                        pass
                self.settings["rotation_a_guard_block_type_a"] = bool(_g1_on)
            except Exception:
                pass

            # Track A/B/C 소속 맵 (매수 시 sig_type 검증용)
            track_map: dict[str, str] = {}
            for c in top_codes_a:
                track_map[c] = 'A'
            for c in top_codes_b:
                if c in track_map:
                    track_map[c] = 'AB'
                else:
                    track_map[c] = 'B'
            for c in top_codes_c:
                previous = track_map.get(c, "")
                track_map[c] = "".join(
                    track for track in ("A", "B", "C") if track in previous + "C"
                )
            self._current_track_map = track_map
            
            if not top_codes and not bool(self.settings.get("transition_runtime_block_s1", False)):
                # 스케줄 공백 시 실시간 select_monitoring_targets 폴백은
                # 일당 수십 초(FDR/교차섹션)라 포트폴리오 BT에서는 쓰지 않는다.
                # 보유 종목만 감시(신규 진입 없음).
                pass
            
            # 2-2. 감시 대상 확정 (보유 종목 + 신규 후보: Track A/B/C 개별 슬롯)
            max_sa = int(self.settings.get("max_slots_a", 20))
            max_sb = int(self.settings.get("max_slots_b", 10))
            max_sc = int(self.settings.get("max_slots_c", 15))
            holdings_codes = list(self.holdings.keys())
            if top_codes_a or top_codes_b or top_codes_c:
                new_candidates = _dual_track_new_candidates(
                    top_codes_a,
                    top_codes_b,
                    holdings_codes,
                    max_sa,
                    max_sb,
                    top_codes_c,
                    max_sc,
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
                day_full = base_df.loc[current_day:current_day + " 23:59:59"]
                if day_full.empty: continue
                # Track C는 당일 프레임에 박스 경계를 쓰므로 캐시 원본을 복사한다.
                if "C" in track_map.get(code, ""):
                    day_full = day_full.copy()

                # C는 평가 전일(ref_day)에 확정된 박스 경계를 당일 전체 봉에 고정 적용합니다.
                if "C" in track_map.get(code, ""):
                    try:
                        box_meta = (
                            getattr(self, "schedule_c_meta", {})
                            .get(ref_day, {})
                            .get(code, {})
                        )
                        if box_meta:
                            signal_cache = getattr(
                                self, "_type_c_signal_cache", None
                            )
                            cache_key = (
                                str(code).zfill(6),
                                str(ref_day),
                                str(current_day),
                                float(box_meta.get("box_lower", 0) or 0),
                                float(box_meta.get("box_upper", 0) or 0),
                            )
                            c_detailed = (
                                signal_cache.get(cache_key)
                                if isinstance(signal_cache, dict)
                                else None
                            )
                            if c_detailed is None:
                                try:
                                    requested_history = int(
                                        self.settings.get(
                                            "type_c_signal_history_bars",
                                            self.settings.get(
                                                "high_precision_causal_lookback_bars",
                                                400,
                                            ),
                                        )
                                    )
                                except (TypeError, ValueError):
                                    requested_history = 400
                                history = base_df.loc[
                                    : current_day + " 23:59:59"
                                ].tail(max(20, requested_history))
                                c_detailed = (
                                    TradingStrategy.get_buy_signals_detailed(
                                        history,
                                        self.settings,
                                        "UP",
                                        signal_context={
                                            "sel_track": track_map.get(code, "C"),
                                            "box_lower": box_meta.get("box_lower"),
                                            "box_upper": box_meta.get("box_upper"),
                                        },
                                    )
                                )
                                if isinstance(signal_cache, dict):
                                    signal_cache[cache_key] = c_detailed.loc[
                                        current_day : current_day + " 23:59:59"
                                    ].copy()
                            common_index = day_full.index.intersection(c_detailed.index)
                            for column in c_detailed.columns:
                                day_full.loc[common_index, column] = c_detailed.loc[
                                    common_index, column
                                ]
                    except Exception as e:
                        try:
                            print(
                                f"[Dynamic] TYPE_C 일봉 경계 신호 계산 실패 "
                                f"({code} {current_day}): {e}"
                            )
                        except Exception:
                            pass

                day_data[code] = day_full
                day_signals[code] = day_full

            # 2-4. 분단위/15분단위 타임라인 루프
            timeline = []
            if day_data:
                all_times = []
                for df in day_data.values(): all_times.extend(df.index.tolist())
                timeline = sorted(list(set(all_times)))
            # SmartBuy shadow 등에서 timeline.index(O(n)) 반복을 피한다.
            timeline_pos = {t: i for i, t in enumerate(timeline)}
            
            today_sells = set()
            self._reset_daily_new_s1_a(str(current_day))
            # [Phase 2] Track A 우선순위 → Track B 순서로 정렬
            code_to_rank = {}
            for i, c in enumerate(top_codes_a):
                code_to_rank[c] = i
            offset_b = len(top_codes_a)
            for i, c in enumerate(top_codes_b):
                if c not in code_to_rank:
                    code_to_rank[c] = offset_b + i
            offset_c = len(top_codes_a) + len(top_codes_b)
            for i, c in enumerate(top_codes_c):
                if c not in code_to_rank:
                    code_to_rank[c] = offset_c + i
            
            for curr_time in timeline:
                # 같은 슬롯 S1은 동시주문 — 타입·동수는 이 시각 직전 보유로만 판정
                self._begin_s1_slot_holdings_snapshot()
                # BT 전용 조기 패스: 당일 개장 슬롯에서 첫 1분봉으로 EXIT/S1 → 이후 완성15분(Pass2)
                try:
                    _is_open_slot = bool(is_krx_first_15m_slot(curr_time))
                except Exception:
                    _is_open_slot = False
                if (
                    self.bt_open_auction_pass_enabled
                    and (not self.high_precision)
                    and _is_open_slot
                ):
                    for code in monitoring_list:
                        try:
                            self._process_0900_open_auction_pass(
                                code, curr_time, today_sells, track_map
                            )
                        except Exception as e:
                            try:
                                print(
                                    f"[Dynamic] 개장 시초 패스 실패 ({code} @ {curr_time}): {e!r}",
                                    flush=True,
                                )
                            except Exception:
                                pass

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
                try:
                    _skip_eod_slot = bool(is_krx_no_signal_eval_15m_slot(curr_time))
                    if (
                        _skip_eod_slot
                        and bool(self.bt_1530_signal_next_open_fill)
                        and bool(is_krx_last_15m_slot(curr_time))
                    ):
                        _skip_eod_slot = False
                except Exception:
                    _skip_eod_slot = False
                for code, h_info in list(self.holdings.items()):
                    # [v36.1] 고정밀 모드인 경우 15분 단위 매도 패스 (하위 1분 루프에서 처리됨)
                    if self.high_precision: continue
                    if _skip_eod_slot: continue
                    if code in today_sells or code in m1_processed: continue
                    d_df = day_data.get(code)
                    if d_df is None or curr_time not in d_df.index: continue
                    
                    curr_row = d_df.loc[curr_time]
                    # 저정밀: Flash/ATR High 모드면 완성 15m High 선반영
                    try:
                        hi = float(curr_row.get("High", curr_row.get("high", 0)) or 0)
                        self._bump_holding_bar_high(h_info, hi)
                    except Exception:
                        pass
                    res = TradingStrategy.evaluate_position(
                        code, curr_row['Close'], h_info, self.settings,
                        indicator_row=curr_row, current_time=curr_time,
                        is_realtime=False, time_is_bar_label=True
                    )
                    
                    if res['action'] == 'EXIT':
                        # TP인 경우 가격 보정 (고가 돌파 기준) — 15:15 auction이면 아래에서 15:30 종가로 덮음
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

                        fill = self._resolve_auction_market_fill(
                            code, curr_time, d_df, e_price, res["reason"]
                        )
                        if fill is None:
                            continue
                        e_price, fill_ts, sell_reason = fill
                        self._execute_sell(
                            code,
                            e_price,
                            fill_ts,
                            sell_reason,
                            indicator_row=curr_row,
                        )
                        today_sells.add(code)

                # 2-4-2. 매수 감시
                buy_candidates = [c for c in monitoring_list if (c not in self.holdings or self.holdings.get(c, {}).get('entry_n', 0) < 2) and (c not in today_sells)]
                if buy_candidates and not self.high_precision:
                    # 기본은 15:30 슬롯 스킵. 옵션 ON이면 익일 09:00 시가 체결 판정으로 이월한다.
                    if _skip_eod_slot:
                        buy_candidates = []
                    # Signal/Ready 셀만 먼저 보고, 후보만 상세 판정(대부분 종목·슬롯 스킵).
                    active_buy: list = []
                    for code in buy_candidates:
                        if code in today_sells or code in m1_processed:
                            continue
                        d_df = day_data.get(code)
                        if d_df is None or curr_time not in d_df.index:
                            continue
                        sig_df = day_signals.get(code)
                        if sig_df is None or curr_time not in sig_df.index:
                            continue
                        h_info = self.holdings.get(code)
                        is_addon_target = h_info is not None and h_info.get("entry_n", 0) < 2
                        try:
                            if not is_addon_target:
                                if not (
                                    bool(sig_df.at[curr_time, "Signal"])
                                    and bool(sig_df.at[curr_time, "Ready"])
                                ):
                                    continue
                            last_sig = sig_df.loc[curr_time]
                        except Exception:
                            continue
                        is_buy_signal = bool(last_sig["Signal"]) and bool(last_sig["Ready"])
                        if not (is_buy_signal or is_addon_target):
                            continue
                        active_buy.append(
                            (
                                code_to_rank.get(code, 999),
                                code,
                                d_df,
                                last_sig,
                                h_info,
                                is_buy_signal,
                                is_addon_target,
                            )
                        )
                    active_buy.sort(key=lambda x: x[0])
                    for (
                        _rank,
                        code,
                        d_df,
                        last_sig,
                        h_info,
                        is_buy_signal,
                        is_addon_target,
                    ) in active_buy:
                        if code in today_sells or code in m1_processed:
                            continue
                        if is_buy_signal or is_addon_target:
                            can_execute = True
                            reason = "SIG" # 일관성 있게 backtest_engine의 기본 SIG와 통일
                            
                            if is_addon_target:
                                if bool(self.settings.get("transition_runtime_block_s2", False)):
                                    can_execute = False
                                elif self._check_s2_approval(code, h_info, last_sig, market_trend="UP"):
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
                                if bool(self.settings.get("transition_runtime_block_s1", False)):
                                    can_execute = False
                                # [Phase 2] Track-Signal 교차 검증 (enforce_track_signal_match)
                                code_track = track_map.get(code, 'A')
                                sig_type = self._effective_s1_sig_type_for_buy(
                                    last_sig, code_track
                                )
                                if not TradingStrategy.track_signal_match_allows(
                                    self.settings, sig_type, code_track
                                ):
                                    can_execute = False
                                elif TradingStrategy.simultaneous_type_a_blocked(
                                    self.settings, last_sig, sig_type
                                ):
                                    can_execute = False
                                elif TradingStrategy.simultaneous_equal_holdings_blocked(
                                    self.settings,
                                    last_sig,
                                    self._s1_holdings_for_type_decision(),
                                ):
                                    can_execute = False
                                
                                # [v31.5] Smart Buy (Supply/Demand Confirmation)
                                if sig_type == "TYPE_A":
                                    use_sb_for_type = self.use_sb_a
                                elif sig_type == "TYPE_B":
                                    use_sb_for_type = self.use_sb_b
                                else:
                                    # TYPE_C는 가격행동·거래량 조건 자체로 확정합니다.
                                    use_sb_for_type = False
                                
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
                                            curr_idx = timeline_pos.get(curr_time, -1)
                                            if curr_idx < 0:
                                                raise KeyError(curr_time)
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
                                    sig_type = self._effective_s1_sig_type_for_buy(
                                        last_sig, track_map.get(code, 'A')
                                    )
                                else:
                                    sig_type = str(last_sig.get('Sig_Type', 'TYPE_B'))
                                if not is_addon_target:
                                    if sig_type == "TYPE_A":
                                        use_sb_for_type = self.use_sb_a
                                    elif sig_type == "TYPE_B":
                                        use_sb_for_type = self.use_sb_b
                                    else:
                                        use_sb_for_type = False
                                else:
                                    use_sb_for_type = (
                                        self.use_sb_s2_b
                                        if "TYPE_B"
                                        in str((h_info or {}).get("sig_type", ""))
                                        else self.use_sb_s2_a
                                    )
                                
                                # [v33.9] Prefix "SmartBuy|" if it was enabled and passed
                                if use_sb_for_type:
                                    reason = "SmartBuy|" + reason

                                if can_execute:
                                    buy_reason = reason
                                    fill_px: Optional[float] = None
                                    fill_ts = curr_time
                                    fill = self._resolve_auction_market_fill(
                                        code,
                                        curr_time,
                                        d_df,
                                        float(d_df.loc[curr_time]["Close"]),
                                        buy_reason,
                                    )
                                    if fill is None:
                                        continue
                                    fill_px, fill_ts, buy_reason = fill
                                    self._handle_buy_execution(
                                        code,
                                        {"reason": buy_reason, "Sig_Type": sig_type},
                                        fill_ts,
                                        d_df.loc[curr_time],
                                        indicator_row=d_df.loc[curr_time],
                                        fill_price=fill_px,
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
            # 시작점(start_date 08:59:00)과 동일하게 시각을 포함해야
            # 리포트 문자열 정렬 시 같은 날 EOD가 시작점보다 앞에 오지 않음
            try:
                _eod_hm = int(get_session_hours(current_day).close_minutes)
                _eod_h, _eod_m = divmod(max(0, _eod_hm), 60)
                _eod_date = f"{current_day} {_eod_h:02d}:{_eod_m:02d}:00"
            except Exception:
                _eod_date = f"{current_day} 15:30:00"
            self.daily_equity.append(
                {
                    "date": _eod_date,
                    "equity": current_equity,
                    "cash": self.cash,
                    "holdings_n": len(self.holdings),
                }
            )
            
            # [Perf] 매매 CSV 버퍼 일괄 flush (하루 단위로 디스크 I/O 감소)
            if not bool(getattr(self, "_suppress_backtest_artifacts", False)):
                try:
                    flush_trade_csv_buffer()
                except Exception:
                    pass
            
            day_t_duration = time.time() - day_t_start
            if not bool(getattr(self, "_quiet_backtest", False)):
                try:
                    n_td = len(trading_days)
                    if (
                        idx == 0
                        or (idx + 1) == n_td
                        or ((idx + 1) % 20 == 0)
                        or day_t_duration >= 0.8
                    ):
                        print(f"[Day] {current_day} 처리 완료 ({day_t_duration:.2f}초)")
                except Exception:
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
        if not bool(getattr(self, "_suppress_backtest_artifacts", False)):
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
            if not bool(getattr(self, "_quiet_backtest", False)):
                print(
                    f"[Dynamic] 모드={_mode} 소요={self.last_run_elapsed_sec:.1f}초"
                )
        except Exception:
            pass
        return self._finalize_backtest(interval)

    def _lookup_same_day_1530_close(
        self,
        code: str,
        curr_time: Any,
        day_df: Any = None,
    ) -> Optional[Tuple[pd.Timestamp, float]]:
        """당일 bars_15m 종가 단일가(SessionHours.close) Close. 없으면 None."""
        try:
            lab = krx_same_day_1530_label(curr_time)
            if lab is None:
                return None
            close_hm = get_session_hours(pd.Timestamp(curr_time).date()).close_minutes
            day0 = pd.Timestamp(curr_time).normalize()
        except Exception:
            return None

        def _from_df(df: Any) -> Optional[Tuple[pd.Timestamp, float]]:
            if df is None or getattr(df, "empty", True):
                return None
            try:
                if lab in df.index:
                    c = float(df.loc[lab, "Close"])
                    if c > 0:
                        return pd.Timestamp(lab), c
            except Exception:
                pass
            # 라벨 미소속 시: 전구간 순회 금지 — 당일 슬라이스만 검사
            try:
                day_slice = df.loc[day0 : day0 + pd.Timedelta(hours=23, minutes=59)]
            except Exception:
                try:
                    day_slice = df[
                        (pd.DatetimeIndex(df.index).normalize() == day0)
                    ]
                except Exception:
                    return None
            try:
                for idx in day_slice.index:
                    try:
                        t = pd.Timestamp(idx).floor("min")
                        if int(t.hour) * 60 + int(t.minute) == close_hm:
                            c = float(day_slice.loc[idx, "Close"])
                            if c > 0:
                                return t, c
                    except Exception:
                        continue
            except Exception:
                pass
            return None

        got = _from_df(day_df)
        if got is not None:
            return got
        try:
            cache = getattr(self, "cached_15m_data", None) or {}
            for key in (code, str(code).zfill(6), str(code).strip()):
                try:
                    got = _from_df(cache.get(key))
                    if got is not None:
                        return got
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def _tag_auction_reason(self, reason: Any) -> str:
        """동시호가 시장가 체결 reason 접두사."""
        try:
            r = str(reason or "")
        except Exception:
            r = ""
        if r.startswith("AUCTION_MKT|"):
            return r
        return f"AUCTION_MKT|src=15:15|{r}"

    def _tag_next_open_reason(self, reason: Any, *, src: str = "15:30") -> str:
        """말봉 신호를 익일 시가 체결로 이월할 때의 reason 접두사."""
        try:
            r = str(reason or "")
        except Exception:
            r = ""
        try:
            tag = str(src or "15:30").strip() or "15:30"
        except Exception:
            tag = "15:30"
        pfx = f"NEXT_OPEN_0900|src={tag}|"
        if r.startswith(pfx):
            return r
        return f"{pfx}{r}"

    def _lookup_next_day_0900_open(
        self,
        code: str,
        curr_time: Any,
        day_df: Any = None,
    ) -> Optional[Tuple[pd.Timestamp, float]]:
        """
        현재 슬롯 이후 최초 거래일의 개장(SessionHours.open) 시가를 찾는다.

        장기 백테에서 잔여 15분봉 전체를 순회하면 O(잔여봉)로 하루가
        수십 초까지 늘어나므로, 다음 거래일 개장 라벨을 직접 조회한다.
        """
        try:
            curr_ts = pd.Timestamp(curr_time)
        except Exception:
            return None

        def _px_at(df: Any, open_ts: pd.Timestamp) -> Optional[Tuple[pd.Timestamp, float]]:
            if df is None or getattr(df, "empty", True):
                return None
            try:
                if open_ts in df.index:
                    px = float(df.loc[open_ts, "Open"])
                    if px > 0:
                        return pd.Timestamp(open_ts), px
            except Exception:
                pass
            # 분 단위 일치 폴백 (타임존/초 차이)
            try:
                day0 = open_ts.normalize()
                open_hm = int(open_ts.hour) * 60 + int(open_ts.minute)
                day_slice = df.loc[day0 : day0 + pd.Timedelta(hours=23, minutes=59)]
                for idx in day_slice.index:
                    t = pd.Timestamp(idx).floor("min")
                    if int(t.hour) * 60 + int(t.minute) == open_hm:
                        px = float(day_slice.loc[idx, "Open"])
                        if px > 0:
                            return t, px
            except Exception:
                pass
            return None

        def _search_in(df: Any) -> Optional[Tuple[pd.Timestamp, float]]:
            if df is None or getattr(df, "empty", True):
                return None
            try:
                from reporting.krx_trading_calendar import next_trading_day

                d0 = curr_ts.date()
                nxt = next_trading_day(d0, max_lookahead=15)
            except Exception:
                nxt = None
                try:
                    # 캘린더 실패 시 평일만 전진
                    d = curr_ts.normalize() + pd.Timedelta(days=1)
                    for _ in range(15):
                        if int(d.weekday()) < 5:
                            nxt = d.date()
                            break
                        d = d + pd.Timedelta(days=1)
                except Exception:
                    return None
            if nxt is None:
                return None
            try:
                hours = get_session_hours(nxt)
                open_ts = pd.Timestamp(nxt) + pd.Timedelta(
                    hours=int(hours.open.hour), minutes=int(hours.open.minute)
                )
            except Exception:
                open_ts = pd.Timestamp(nxt) + pd.Timedelta(hours=9)
            return _px_at(df, open_ts)

        got = _search_in(day_df)
        if got is not None:
            return got
        try:
            cache = getattr(self, "cached_15m_data", None) or {}
            for key in (code, str(code).zfill(6), str(code).strip()):
                got = _search_in(cache.get(key))
                if got is not None:
                    return got
        except Exception:
            pass
        return None

    def _resolve_auction_market_fill(
        self,
        code: str,
        curr_time: Any,
        day_df: Any,
        price: float,
        reason: Any,
    ) -> Optional[Tuple[float, Any, str]]:
        """
        일반 슬롯: (price, curr_time, reason) 그대로.
        동시호가 직전: 당일 종가 Close·시각·AUCTION_MKT reason. Close 없으면 None(주문 스킵).
        종가 단일가+next_open: 익일 개장 시가.
        """
        try:
            px = float(price)
        except (TypeError, ValueError):
            px = 0.0
        try:
            rsn = str(reason or "")
        except Exception:
            rsn = ""
        try:
            if bool(self.bt_1530_signal_next_open_fill) and bool(
                is_krx_last_15m_slot(curr_time)
            ):
                got = self._lookup_next_day_0900_open(code, curr_time, day_df)
                if got is None:
                    return None
                fill_ts, open_px = got
                return float(open_px), fill_ts, self._tag_next_open_reason(rsn, src="15:30")
            if bool(self.bt_1945_signal_next_open_fill) and bool(
                is_krx_aftermarket_last_15m_slot(curr_time)
            ):
                got = self._lookup_next_day_0900_open(code, curr_time, day_df)
                if got is None:
                    return None
                fill_ts, open_px = got
                return float(open_px), fill_ts, self._tag_next_open_reason(rsn, src="19:45")
        except Exception:
            pass
        try:
            if not is_krx_auction_market_fill_slot(curr_time):
                return px, curr_time, rsn
        except Exception:
            return px, curr_time, rsn
        got = self._lookup_same_day_1530_close(code, curr_time, day_df)
        if got is None:
            return None
        lab, close = got
        return float(close), lab, self._tag_auction_reason(rsn)

    def _tag_open_auction_reason(self, reason: Any) -> str:
        """09:00 조기 패스(첫 1분봉) 체결 reason에 OPEN_AUCTION 접두를 붙인다."""
        try:
            r = str(reason or "")
        except Exception:
            r = ""
        if r.startswith("OPEN_AUCTION|"):
            return r
        return f"OPEN_AUCTION|src=first_m1|{r}"

    def _lookup_day_first_m1_ohlcv(
        self,
        code: str,
        day: Any,
    ) -> Optional[Tuple[pd.Timestamp, Dict[str, float]]]:
        """
        당일 첫 1분봉 OHLCV.

        DB 라벨은 보통 개장+1분(분 종료 시각). 시초 단일가+첫 1분 연속매매 거래량이 포함된다.
        """
        try:
            day0 = pd.Timestamp(day).normalize()
            date_str = day0.strftime("%Y-%m-%d")
            hours = get_session_hours(day0.date())
            open_td = pd.Timedelta(minutes=int(hours.open_minutes))
            open_end_td = pd.Timedelta(minutes=int(hours.open_minutes) + 5)
            open_hour_end_td = pd.Timedelta(minutes=int(hours.open_minutes) + 60)
        except Exception:
            return None

        def _from_df(df: Any) -> Optional[Tuple[pd.Timestamp, Dict[str, float]]]:
            if df is None or getattr(df, "empty", True):
                return None
            try:
                idx = pd.DatetimeIndex(pd.to_datetime(df.index))
                day_m1 = df.loc[(idx >= day0 + open_td) & (idx < day0 + open_hour_end_td)]
            except Exception:
                return None
            if day_m1 is None or day_m1.empty:
                return None
            try:
                row = day_m1.iloc[0]
                ts = pd.Timestamp(day_m1.index[0])
                o = float(row.get("Open", row.get("open", 0)) or 0)
                h = float(row.get("High", row.get("high", 0)) or 0)
                l = float(row.get("Low", row.get("low", 0)) or 0)
                c = float(row.get("Close", row.get("close", 0)) or 0)
                v = float(row.get("Volume", row.get("volume", 0)) or 0)
            except Exception:
                return None
            if o <= 0 or c <= 0:
                return None
            if h <= 0:
                h = max(o, c)
            if l <= 0:
                l = min(o, c)
            return ts, {
                "Open": o,
                "High": h,
                "Low": l,
                "Close": c,
                "Volume": max(0.0, v),
            }

        # 1) 당일 1분 캐시/온디맨드
        try:
            chunk = self._get_m1_chunk_on_demand(
                code, date_str, day0 + open_td
            )
            got = _from_df(chunk)
            if got is not None:
                return got
        except Exception:
            pass

        # 2) DB 직접 조회(첫 봉만)
        try:
            q = """
                SELECT datetime AS time, open, high, low, close, volume
                FROM bars_1m
                WHERE code = ? AND datetime >= ? AND datetime < ?
                ORDER BY datetime
                LIMIT 1
            """
            open_s = (day0 + open_td).strftime("%Y-%m-%d %H:%M:%S")
            open_e = (day0 + open_end_td).strftime("%Y-%m-%d %H:%M:%S")
            with self.db._get_connection() as conn:
                df = pd.read_sql_query(
                    q,
                    conn,
                    params=[
                        str(code).zfill(6),
                        open_s,
                        open_e,
                    ],
                )
            if df is None or df.empty:
                return None
            df["time"] = pd.to_datetime(df["time"])
            df = df.set_index("time")
            df.columns = [c.capitalize() for c in df.columns]
            return _from_df(df)
        except Exception:
            return None

    def _lookup_prev_trading_day_first_m1_vol(
        self,
        code: str,
        day: Any,
    ) -> Optional[float]:
        """직전 거래일(최대 14일 역탐색)의 첫 1분봉 거래량 — SQL만 사용."""
        try:
            day0 = pd.Timestamp(day).normalize()
            code6 = str(code).zfill(6)
        except Exception:
            return None
        for i in range(1, 15):
            try:
                prev = day0 - pd.Timedelta(days=int(i))
            except Exception:
                continue
            try:
                if int(prev.weekday()) >= 5:
                    continue
            except Exception:
                continue
            date_str = prev.strftime("%Y-%m-%d")
            try:
                hours = get_session_hours(prev.date())
                open_s = f"{date_str} {hours.open.strftime('%H:%M:%S')}"
                open_e_dt = hours.open_datetime() + timedelta(minutes=5)
                open_e = open_e_dt.strftime("%Y-%m-%d %H:%M:%S")
                q = """
                    SELECT volume
                    FROM bars_1m
                    WHERE code = ? AND datetime >= ? AND datetime < ?
                    ORDER BY datetime
                    LIMIT 1
                """
                with self.db._get_connection() as conn:
                    df = pd.read_sql_query(
                        q,
                        conn,
                        params=[
                            code6,
                            open_s,
                            open_e,
                        ],
                    )
                if df is None or df.empty:
                    continue
                vol = float(df.iloc[0]["volume"] or 0)
                if vol > 0:
                    return vol
            except Exception:
                continue
        return None

    def _build_0900_open_auction_signal_row(
        self,
        code: str,
        curr_time: Any,
    ) -> Optional[pd.Series]:
        """
        전일까지 완성 15분봉 + 당일 **첫 1분봉** 으로 합성한 마지막 행.

        - OHLC·Volume: 당일 첫 1분봉 그대로
        - ``Prev_Day_Vol*``: 전일(직전 거래일) 첫 1분 거래량으로 덮어씀
          → ``vol_surge`` 가 「전일 첫1분 대비」로 동작
        캐시된 09:00 15분 Close/Signal/Volume 은 쓰지 않음.
        ``Ready`` 는 히스토리 봉 수 기준으로 별도 부여한다.
        """
        try:
            cache = getattr(self, "cached_15m_data", None) or {}
            full = None
            for key in (code, str(code).zfill(6), str(code).strip()):
                full = cache.get(key)
                if full is not None and not getattr(full, "empty", True):
                    break
            if full is None or getattr(full, "empty", True):
                return None
            t = pd.Timestamp(curr_time)
            if t not in full.index:
                return None
            first = self._lookup_day_first_m1_ohlcv(code, t)
            if first is None:
                return None
            m1_ts, m1 = first
            hist = full.loc[full.index < t]
            if hist is None or hist.empty:
                return None
            try:
                lb = int(
                    float(
                        self.settings.get("high_precision_causal_lookback_bars", 400)
                    )
                )
            except (TypeError, ValueError):
                lb = 400
            lb = max(60, lb)
            if len(hist) > lb:
                hist = hist.iloc[-lb:]
            ohlcv_cols = ["Open", "High", "Low", "Close", "Volume"]
            for c in ohlcv_cols:
                if c not in hist.columns:
                    return None
            hist_ohlcv = hist[ohlcv_cols].copy()
            # 가상 15분 라벨은 슬롯 09:00 유지, OHLCV=첫1분 그대로
            virtual = hist_ohlcv.iloc[[-1]].copy()
            virtual.index = pd.DatetimeIndex([t])
            virtual.loc[t, "Open"] = float(m1["Open"])
            virtual.loc[t, "High"] = float(m1["High"])
            virtual.loc[t, "Low"] = float(m1["Low"])
            virtual.loc[t, "Close"] = float(m1["Close"])
            raw_vol = float(m1["Volume"])
            virtual.loc[t, "Volume"] = raw_vol
            combined = pd.concat([hist_ohlcv, virtual], axis=0)
            combined = TradingStrategy.calculate_indicators(combined, self.settings)
            # 거래량 기준: 전일 첫 1분 대비 (15분 Prev_Day_Vol 덮어쓰기)
            prev_m1_vol = self._lookup_prev_trading_day_first_m1_vol(code, t)
            if prev_m1_vol is not None and prev_m1_vol > 0:
                for col in (
                    "Prev_Day_Vol",
                    "Prev_Day_Vol_B",
                    "Prev_Day_Vol_S1_A",
                    "Prev_Day_Vol_S1_B",
                    "Prev_Day_Vol_S2_A",
                    "Prev_Day_Vol_S2_B",
                ):
                    try:
                        if col in combined.columns:
                            combined.loc[t, col] = float(prev_m1_vol)
                    except Exception:
                        pass
            else:
                # 전일 첫1분 없으면 거래량 조건 판정 불가 → 신호 스킵
                return None
            sigd = TradingStrategy.get_buy_signals_detailed(
                combined, self.settings, "UP"
            )
            if sigd is None or sigd.empty:
                return None
            last = combined.iloc[-1].copy()
            for k, v in sigd.iloc[-1].items():
                last[k] = v
            try:
                ready_min = max(
                    1,
                    int(
                        float(
                            self.settings.get("hp_causal_ready_min_bar_index", 19)
                        )
                    ),
                )
            except (TypeError, ValueError):
                ready_min = 19
            last["Ready"] = bool(len(hist_ohlcv) >= ready_min)
            try:
                last["_open_pass_fill_ts"] = m1_ts
                last["_open_pass_raw_m1_vol"] = raw_vol
                last["_open_pass_prev_m1_vol"] = float(prev_m1_vol)
            except Exception:
                pass
            return last
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            try:
                print(
                    f"[Dynamic] 09:00 첫1분 신호 재계산 실패 ({code} @ {curr_time}): {e!r}",
                    flush=True,
                )
            except Exception:
                pass
            return None

    def _process_0900_open_auction_pass(
        self,
        code: str,
        curr_time: Any,
        today_sells: Any,
        track_map: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        09:00 슬롯 조기 평가(BT 전용) — 당일 첫 1분봉 완성 기준.

        - 보유: evaluate_position(첫1분 Close) → EXIT면 해당가 체결
        - 미보유: 합성봉 Signal∧Ready → S1 (Smart Buy 생략)
        - S2는 생략(완성 15분 Pass2에 맡김)
        Pass2(같은 09:00 완성 Close 평가)는 호출측에서 이어서 수행한다.
        """
        try:
            if code in today_sells:
                return
            sig_row = self._build_0900_open_auction_signal_row(code, curr_time)
            if sig_row is None:
                return
            fill_px = float(sig_row.get("Close", 0) or 0)
            if fill_px <= 0:
                return
            try:
                fill_ts = pd.Timestamp(sig_row.get("_open_pass_fill_ts", curr_time))
            except Exception:
                fill_ts = curr_time
            h_info = self.holdings.get(code)

            # 보유 청산 — 첫 1분 Close 기준
            if h_info is not None:
                try:
                    hi = float(sig_row.get("High", fill_px) or fill_px)
                    self._bump_holding_bar_high(h_info, hi)
                except Exception:
                    pass
                res = TradingStrategy.evaluate_position(
                    code,
                    fill_px,
                    h_info,
                    self.settings,
                    indicator_row=sig_row,
                    current_time=fill_ts,
                    is_realtime=False,
                )
                if isinstance(res, dict) and res.get("action") == "EXIT":
                    e_price = fill_px
                    if res.get("reason") == "TAKE_PROFIT":
                        tpp = res.get("tp_pct_applied")
                        if tpp is not None:
                            e_price = float(h_info["avg_p"]) * (
                                1 + float(tpp) / 100.0
                            )
                    sell_reason = self._tag_open_auction_reason(res.get("reason"))
                    self._execute_sell(
                        code,
                        e_price,
                        fill_ts,
                        sell_reason,
                        indicator_row=sig_row,
                    )
                    try:
                        today_sells.add(code)
                    except Exception:
                        pass
                return

            # S1 신규 (첫 1분 패스)
            if bool(self.settings.get("transition_runtime_block_s1", False)):
                return
            try:
                is_sig = bool(sig_row.get("Signal", False)) and bool(
                    sig_row.get("Ready", False)
                )
            except Exception:
                is_sig = False
            if not is_sig:
                return
            tm = track_map if track_map is not None else getattr(
                self, "_current_track_map", {}
            )
            code_track = str((tm or {}).get(code, "A"))
            sig_type = self._effective_s1_sig_type_for_buy(sig_row, code_track)
            if not TradingStrategy.track_signal_match_allows(
                self.settings, sig_type, code_track
            ):
                return
            if TradingStrategy.simultaneous_type_a_blocked(
                self.settings, sig_row, sig_type
            ):
                return
            if TradingStrategy.simultaneous_equal_holdings_blocked(
                self.settings, sig_row, self._s1_holdings_for_type_decision()
            ):
                return
            buy_reason = self._tag_open_auction_reason("SIG")
            self._handle_buy_execution(
                code,
                {"reason": buy_reason, "Sig_Type": sig_type},
                fill_ts,
                sig_row,
                indicator_row=sig_row,
                fill_price=fill_px,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            try:
                print(
                    f"[Dynamic] 09:00 첫1분 패스 처리 실패 ({code} @ {curr_time}): {e!r}",
                    flush=True,
                )
            except Exception:
                pass

    def _process_1515_auction_completed_bar(
        self,
        code: str,
        day_df_15m: Any,
        today_sells: Any,
        curr_15m_time: Any,
    ) -> None:
        """
        고정밀 경로의 15:15: 1분 인트라바 체결 없이 완성 15:15 평가 → 15:30 종가 1회.
        """
        try:
            if code in today_sells:
                return
            if day_df_15m is None or curr_15m_time not in day_df_15m.index:
                return
            curr_row = day_df_15m.loc[curr_15m_time]
            h_info = self.holdings.get(code)

            if h_info is not None:
                try:
                    hi = float(curr_row.get("High", curr_row.get("high", 0)) or 0)
                    self._bump_holding_bar_high(h_info, hi)
                except Exception:
                    pass
                res = TradingStrategy.evaluate_position(
                    code,
                    float(curr_row["Close"]),
                    h_info,
                    self.settings,
                    indicator_row=curr_row,
                    current_time=curr_15m_time,
                    is_realtime=False,
                    time_is_bar_label=True,
                )
                if isinstance(res, dict) and res.get("action") == "EXIT":
                    e_price = float(curr_row["Close"])
                    if res.get("reason") == "TAKE_PROFIT":
                        tpp = res.get("tp_pct_applied")
                        if tpp is not None:
                            e_price = float(h_info["avg_p"]) * (
                                1 + float(tpp) / 100.0
                            )
                    fill = self._resolve_auction_market_fill(
                        code,
                        curr_15m_time,
                        day_df_15m,
                        e_price,
                        res.get("reason"),
                    )
                    if fill is None:
                        return
                    e_price, fill_ts, sell_reason = fill
                    self._execute_sell(
                        code,
                        e_price,
                        fill_ts,
                        sell_reason,
                        indicator_row=curr_row,
                    )
                    try:
                        today_sells.add(code)
                    except Exception:
                        pass
                    return

                # S2 추매
                try:
                    entry_n = int(h_info.get("entry_n", 1) or 1)
                except Exception:
                    entry_n = 1
                if entry_n < 2:
                    try:
                        ok_s2 = bool(
                            self._check_s2_approval(
                                code, h_info, curr_row, market_trend="UP"
                            )
                        )
                    except Exception:
                        ok_s2 = False
                    if ok_s2:
                        fill = self._resolve_auction_market_fill(
                            code,
                            curr_15m_time,
                            day_df_15m,
                            float(curr_row["Close"]),
                            "ADDON(S2)|AUCTION",
                        )
                        if fill is None:
                            return
                        fill_px, fill_ts, buy_reason = fill
                        self._handle_buy_execution(
                            code,
                            {
                                "reason": buy_reason,
                                "Sig_Type": h_info.get("sig_type", "TYPE_A"),
                            },
                            fill_ts,
                            curr_row,
                            indicator_row=curr_row,
                            fill_price=fill_px,
                        )
                return

            # S1 신규
            try:
                is_sig = bool(curr_row.get("Signal", False)) and bool(
                    curr_row.get("Ready", False)
                )
            except Exception:
                is_sig = False
            if not is_sig:
                return
            if bool(self.settings.get("transition_runtime_block_s1", False)):
                return
            code_track = str(
                (getattr(self, "_current_track_map", {}) or {}).get(code, "A")
            )
            sig_type = self._effective_s1_sig_type_for_buy(curr_row, code_track)
            if not TradingStrategy.track_signal_match_allows(
                self.settings, sig_type, code_track
            ):
                return
            if TradingStrategy.simultaneous_type_a_blocked(
                self.settings, curr_row, sig_type
            ):
                return
            if TradingStrategy.simultaneous_equal_holdings_blocked(
                self.settings,
                curr_row,
                self._s1_holdings_for_type_decision(),
            ):
                return
            fill = self._resolve_auction_market_fill(
                code,
                curr_15m_time,
                day_df_15m,
                float(curr_row["Close"]),
                "SIG",
            )
            if fill is None:
                return
            fill_px, fill_ts, buy_reason = fill
            self._handle_buy_execution(
                code,
                {"reason": buy_reason, "Sig_Type": sig_type},
                fill_ts,
                curr_row,
                indicator_row=curr_row,
                fill_price=fill_px,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            try:
                print(
                    f"[Dynamic] 15:15 auction 처리 실패 ({code} @ {curr_15m_time}): {e!r}",
                    flush=True,
                )
            except Exception:
                pass

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
        # 포지션 사이징: 현재 총자산(현금+보유평가) 대비 %만 사용
        holdings_value = 0.0
        try:
            for _c, h in self.holdings.items():
                holdings_value += float(h['qty']) * float(h.get('last_p', h['avg_p']))
        except Exception:
            holdings_value = 0.0
        try:
            total_equity = float(self.cash) + float(holdings_value)
        except (TypeError, ValueError):
            total_equity = float(holdings_value)
        try:
            s1_pct = float(self.settings.get('s1_equity_pct', 10.0))
        except (TypeError, ValueError):
            s1_pct = 10.0
        try:
            s2_pct = float(self.settings.get('s2_equity_pct', 20.0))
        except (TypeError, ValueError):
            s2_pct = 20.0
        s1_amt = total_equity * (s1_pct / 100.0)
        s2_amt = total_equity * (s2_pct / 100.0)

        if h_info is None:
            sig_type = str(sig_res.get("Sig_Type", "TYPE_A"))
            if sig_type == "TYPE_C":
                try:
                    box_meta = (
                        getattr(self, "_current_box_c_meta", {}).get(code, {}) or {}
                    )
                    if not (
                        float(box_meta.get("box_upper", 0) or 0)
                        > float(box_meta.get("box_lower", 0) or 0)
                        > 0
                    ):
                        return False
                except (TypeError, ValueError):
                    return False
            # 일일 Type A 신규 S1 상한 — 체결시각(curr_time) 일자 기준
            # NEXT_OPEN은 전일 루프에서 호출되므로 반드시 fill 일자를 씀
            if str(sig_type).upper() == "TYPE_A":
                try:
                    if bool(self.settings.get("rotation_a_guard_block_type_a", False)):
                        return False
                except Exception:
                    pass
                fill_day = self._normalize_bt_day(curr_time)
                if not self._can_open_new_s1_a(fill_day):
                    return False
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
            if "TYPE_C" in str(h_info.get("sig_type", "") or "").upper():
                return False
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
            if sig_type == "TYPE_C":
                try:
                    box_meta = (
                        getattr(self, "_current_box_c_meta", {}).get(code, {}) or {}
                    )
                    lower = float(box_meta.get("box_lower", 0) or 0)
                    upper = float(box_meta.get("box_upper", 0) or 0)
                    atr_c = float(last_row.get("ATR_C", 0) or 0)
                    if upper > lower > 0:
                        self.holdings[code].update(
                            {
                                "box_lower": lower,
                                "box_upper": upper,
                                "box_atr_entry": atr_c,
                                "box_breakdown_closes": 0,
                                "box_last_breakdown_bar": "",
                            }
                        )
                except (KeyError, TypeError, ValueError):
                    pass
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
        # Type A S1 체결 시에만 일일 신규 카운터 증가(체결일 기준)
        try:
            if entry_n == 1 and str(sig_type).upper() == "TYPE_A":
                _fd = self._normalize_bt_day(curr_time)
                self._note_new_s1_a(_fd)
        except Exception:
            pass
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
        if bool(getattr(self, "_suppress_backtest_artifacts", False)):
            return
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
        if not bool(getattr(self, "_quiet_backtest", False)):
            print(f"[Dynamic] 시뮬레이션 완료. 총 {len(self.trades_history)}건의 매매(평가 포함) 발생.")
        trades_df = pd.DataFrame(self.trades_history)
        if not trades_df.empty:
            trades_df['Date'] = pd.to_datetime(trades_df['Date'])
            if bool(getattr(self, "_suppress_backtest_artifacts", False)):
                return trades_df
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

    def _fill_synthetic_15m_group(self, group: pd.DataFrame) -> pd.DataFrame:
        """15분 그룹에 합성 수급을 채운다 (캐시 재사용 시 동일 결과)."""
        try:
            if not self.settings.get("use_synthetic_realtime", True):
                return group
        except Exception:
            return group
        try:
            for col in ["Vol_power", "Total_ask", "Total_bid"]:
                if col in group.columns:
                    group[col] = pd.to_numeric(group[col], errors="coerce")
            p_changes = group["Close"].pct_change().fillna(0).values
            vol_mas = group["Volume"].rolling(20).mean().bfill().values
            vol_ratios = (group["Volume"] / (vol_mas + 1e-10)).values
            vps = group["Vol_power"].values
            tasks = group["Total_ask"].values
            tbids = group["Total_bid"].values
            nan_vp = np.isnan(vps)
            if np.any(nan_vp):
                vps[nan_vp] = synthesize_vol_power_vectorized(
                    p_changes[nan_vp], vol_ratios[nan_vp]
                )
            nan_ba = (np.isnan(tasks)) | (np.any(np.isnan(tbids)))
            if np.any(nan_ba):
                r_vector = synthesize_bid_ask_ratio_vectorized(
                    p_changes[nan_ba], vol_ratios[nan_ba], vps[nan_ba]
                )
                tasks[nan_ba] = 100000
                tbids[nan_ba] = (100000 * r_vector).astype(int)
            group = group.copy()
            group["Vol_power"] = vps
            group["Total_ask"] = tasks
            group["Total_bid"] = tbids
            group["bid_ask_ratio"] = group["Total_bid"] / (group["Total_ask"] + 1e-10)
            group["vol_power"] = vps
            return group
        except Exception:
            return group

    def _indicators_and_signals_15m(self, group: pd.DataFrame) -> pd.DataFrame:
        """종목 15분 프레임에 지표·펄스·신호를 한 번에 붙인다."""
        return indicators_and_signals_15m_frame(group, self.settings)

    def _prepare_15m_cached_data(
        self,
        universe_schedule,
        start_date,
        end_date,
        callback,
        univ_fp: str = "",
    ):
        """유니버스 합집합 15분봉을 로드하고 지표·신호를 붙인다. 튜닝 재실행 시 캐시 재사용."""
        t0 = time.time()
        if callback:
            callback("15분봉 데이터 일괄 로드 중...", 0)
        u_codes = set()
        try:
            for codes in universe_schedule.values():
                u_codes.update(codes)
        except Exception:
            return {}
        if not u_codes:
            return {}

        ohlcv_fp = ""
        sig_fp = ""
        try:
            if univ_fp:
                ohlcv_fp = _bt_ohlcv_fingerprint(
                    univ_fp,
                    self.settings,
                    start_date=str(start_date),
                    end_date=str(end_date),
                )
                sig_fp = _bt_signal_fingerprint(ohlcv_fp, self.settings)
        except Exception:
            ohlcv_fp = ""
            sig_fp = ""

        cached_sig = _bt_cache_get_signals(sig_fp) if sig_fp else None
        if isinstance(cached_sig, dict) and cached_sig:
            try:
                print(
                    f"[Dynamic] 15분 신호 캐시 HIT ({time.time() - t0:.2f}초, {len(cached_sig)}종목)"
                )
            except Exception:
                pass
            if callback:
                callback("15분 신호 캐시 재사용", 8)
            return cached_sig

        ohlcv_map = _bt_cache_get_ohlcv(ohlcv_fp) if ohlcv_fp else None
        if not isinstance(ohlcv_map, dict) or not ohlcv_map:
            all_codes = list(u_codes)
            pad_start = (
                datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=21)
            ).strftime("%Y-%m-%d")
            placeholders = ",".join(["?"] * len(all_codes))
            query = f"""
                SELECT b.code, b.datetime as time, b.open, b.high, b.low, b.close, b.volume,
                       NULL AS vol_power, NULL AS total_ask, NULL AS total_bid
                FROM bars_15m b
                WHERE b.code IN ({placeholders}) AND b.datetime BETWEEN ? AND ?
                ORDER BY b.code, b.datetime
            """
            with self.db._get_connection() as conn:
                df_all = pd.read_sql_query(
                    query, conn, params=all_codes + [pad_start, end_date + " 23:59:59"]
                )
            if df_all.empty:
                return {}
            df_all.columns = [
                c.capitalize() if c.lower() != "time" else "time" for c in df_all.columns
            ]
            df_all["time"] = pd.to_datetime(df_all["time"])
            df_all.set_index("time", inplace=True)
            ohlcv_map = {}
            for code, group in df_all.groupby("Code"):
                ohlcv_map[code] = self._fill_synthetic_15m_group(group)
            if ohlcv_fp:
                try:
                    _bt_cache_put_ohlcv(ohlcv_fp, ohlcv_map)
                except Exception:
                    pass
        else:
            try:
                print(
                    f"[Dynamic] 15분 OHLCV 캐시 HIT — 지표·신호만 재계산 ({len(ohlcv_map)}종목)"
                )
            except Exception:
                pass

        items = list(ohlcv_map.items())
        cached: Dict[str, Any] = {}
        n_workers = 1
        try:
            # CPU 바운드 → ProcessPool (스레드는 GIL로 효과 제한). 상한 16
            n_workers = min(16, max(1, int(os.cpu_count() or 1)), max(1, len(items)))
        except Exception:
            n_workers = 1

        def _one(pair: Tuple[Any, pd.DataFrame]) -> Tuple[Any, Optional[pd.DataFrame]]:
            code, group = pair
            try:
                return code, indicators_and_signals_15m_frame(group, self.settings)
            except Exception as e:
                try:
                    print(f"[Dynamic] 15분 지표/신호 실패 ({code}): {e!r}", flush=True)
                except Exception:
                    pass
                return code, None

        pool_mode = "serial"
        if n_workers <= 1 or len(items) <= 1:
            for pair in items:
                code, df_ind = _one(pair)
                if df_ind is not None:
                    cached[code] = df_ind
        else:
            cache_path = ""
            try:
                fd, cache_path = tempfile.mkstemp(
                    prefix="bt_sig15_", suffix=".pkl"
                )
                os.close(fd)
                with open(cache_path, "wb") as f:
                    pickle.dump(
                        {"ohlcv": ohlcv_map, "settings": dict(self.settings or {})},
                        f,
                        protocol=pickle.HIGHEST_PROTOCOL,
                    )
                codes = [c for c, _ in items]
                chunksize = max(1, len(codes) // max(1, n_workers * 4))
                with ProcessPoolExecutor(
                    max_workers=n_workers,
                    initializer=_sig15_pool_init,
                    initargs=(cache_path,),
                ) as ex:
                    for code, df_ind in ex.map(
                        _sig15_pool_job, codes, chunksize=chunksize
                    ):
                        if df_ind is not None:
                            cached[code] = df_ind
                pool_mode = "process"
            except Exception as e:
                try:
                    print(
                        f"[Dynamic] 15분 ProcessPool 실패 → 스레드 폴백: {e!r}",
                        flush=True,
                    )
                except Exception:
                    pass
                cached.clear()
                try:
                    with ThreadPoolExecutor(max_workers=n_workers) as ex:
                        for code, df_ind in ex.map(_one, items):
                            if df_ind is not None:
                                cached[code] = df_ind
                    pool_mode = "thread"
                except Exception as e2:
                    try:
                        print(
                            f"[Dynamic] 15분 스레드 폴백 실패 → 순차: {e2!r}",
                            flush=True,
                        )
                    except Exception:
                        pass
                    cached.clear()
                    for pair in items:
                        code, df_ind = _one(pair)
                        if df_ind is not None:
                            cached[code] = df_ind
                    pool_mode = "serial"
            finally:
                if cache_path:
                    try:
                        if os.path.isfile(cache_path):
                            os.remove(cache_path)
                    except Exception:
                        pass

        if sig_fp and cached:
            try:
                _bt_cache_put_signals(sig_fp, cached)
            except Exception:
                pass
        try:
            print(
                f"[Dynamic] 15분 지표+신호 준비 {len(cached)}종목 "
                f"({time.time() - t0:.2f}초, workers={n_workers}, mode={pool_mode})"
            )
        except Exception:
            pass
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
                try:
                    hours = get_session_hours(date_str)
                    m1_lo = f"{date_str} {hours.open.strftime('%H:%M:%S')}"
                    m1_hi_dt = hours.close_datetime() + timedelta(minutes=15)
                    m1_hi = m1_hi_dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    m1_lo = date_str + " 09:00:00"
                    m1_hi = date_str + " 15:45:00"
                df = pd.read_sql_query(
                    query, conn, params=[code, m1_lo, m1_hi]
                )
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
                # 15m 종가 라벨 기준 타임스탬프 창 (개수 고정 아님) — utils.krx_bars
                _ct = (
                    pd.Timestamp(curr_time)
                    if not isinstance(curr_time, pd.Timestamp)
                    else curr_time
                )
                return slice_m1_for_15m_end(m1_full, _ct)
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

            # 15:30 — 신호평가·체결 스킵 / 15:15 — 인트라바 없이 15:30 종가 1회
            try:
                if is_krx_no_signal_eval_15m_slot(curr_15m_time):
                    return
                if is_krx_auction_market_fill_slot(curr_15m_time):
                    self._process_1515_auction_completed_bar(
                        code, day_df_15m, today_sells, curr_15m_time
                    )
                    return
            except Exception:
                pass

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
                # 고가 갱신 (Flash/ATR High 모드)
                try:
                    self._bump_holding_bar_high(h_info, m1_r["High"])
                except Exception:
                    pass
                
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
                    if (not is_addon_target) and bool(
                        self.settings.get("transition_runtime_block_s1", False)
                    ):
                        continue
                    # [Phase 2] Track-Signal 교차 검증 (1분봉 경로)
                    code_track = getattr(self, '_current_track_map', {}).get(code, 'A')
                    if h_info is None:
                        sig_type = self._effective_s1_sig_type_for_buy(
                            indicator_to_use, code_track
                        )
                    else:
                        sig_type = str(indicator_to_use.get('Sig_Type', 'TYPE_B'))
                    
                    if not TradingStrategy.track_signal_match_allows(
                        self.settings, sig_type, code_track
                    ):
                        continue
                    if (not is_addon_target) and TradingStrategy.simultaneous_type_a_blocked(
                        self.settings, indicator_to_use, sig_type
                    ):
                        continue
                    if (not is_addon_target) and TradingStrategy.simultaneous_equal_holdings_blocked(
                        self.settings,
                        indicator_to_use,
                        self._s1_holdings_for_type_decision(),
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
        if bool(self.settings.get("transition_runtime_block_s2", False)):
            return False
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
