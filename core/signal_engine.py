import copy
import pandas as pd
import numpy as np
import threading
import contextlib
from datetime import datetime, timedelta
import time
import traceback
from backtest_engine import TradingStrategy
from core.bt_hp_intrabar_unified import (
    evaluate_bt_hp_intrabar_unified,
    resolve_position_entry_ts,
)
from core.bt_hp_signal_bridge import evaluate_close_now_s1_at_m1_idx
from core.defaults import effective_min_15m_bars_for_monitoring
from core.monitor_code_sets import watchlist_only_codes_set
from core.monitor_signal_status_display import monitor_signal_status_display_text
from core.signal_monitor_tree_ui import SignalMonitorTreeUiMixin
from core.signal_slot_time_helpers import SignalSlotTimeHelpersMixin
from core.signal_s1_eval import SignalS1EvalMixin
from core.signal_held_eval import SignalHeldEvalMixin
from core.signal_synthetic_s2 import SignalSyntheticS2Mixin
from core.signal_tick_handler import SignalTickHandlerMixin
from core.signal_order_routing import SignalOrderRoutingMixin
from core.signal_monitor_display import (
    clear_warmup_status_if_placeholder,
    format_monitor_vs_prev_cell,
    format_s1_confluence_status_line,
    resolve_monitor_strat_label,
    sync_status_text_from_confluence,
)
from utils.krx_session import is_krx_regular_session, is_krx_tradable_session
from utils.bars_data_keys import resolve_bars_data_frame
from utils.normalization import canonical_stock_code
from utils.monitor_vp_stub import clear_vp_price_stub_if_expired
from typing import Any, Dict, Optional, Tuple


class SignalEngine(
    SignalMonitorTreeUiMixin,
    SignalSlotTimeHelpersMixin,
    SignalS1EvalMixin,
    SignalHeldEvalMixin,
    SignalSyntheticS2Mixin,
    SignalTickHandlerMixin,
    SignalOrderRoutingMixin,
):
    """[v25.7] 실시간 체결 데이터 분석 및 시그널 포착 엔진 클래스"""
    def __init__(self, app):
        self.app = app
        self._last_realtime_err = None
        self.last_buy_sig_time = {} # [v1.0] 중복 시그널 방지용 쿨타임 관리
        # ATR_SL 청산 후 동일종목 S1 재진입 금지 만료(일자 normalize)
        self._atr_sl_reentry_until: Dict[str, datetime] = {}
        # 종목별 ATR_SL 청산일(연속 손절 쿨다운 판정용)
        self._atr_sl_exit_dates: Dict[str, list] = {}
        self._wl_buy_last_tm = {}
        self._wl_sell_last_tm = {}
        # monitor_info 누락 코드가 틱에서 들어올 때 스킵되는 상황 확인용(과도 로그 방지: 최대 N개만)
        self._missing_monitor_info_logged: set[str] = set()
        self._missing_monitor_info_log_limit: int = 10

        # UI 갱신 경로가 멈춘 케이스 추적(해당 코드만 주기적으로 기록)
        self._ui_debug_log_last_ts: dict[str, float] = {}
        # 실시간 예외가 동일 문자열로 반복될 때도 디버그 코드 관측을 남기기 위한 쿨다운 맵
        self._tick_exception_log_last_ts: dict[str, float] = {}
        # 예외 발생 시 최소 UI 값(현재가/거래량) 복구 enqueue 쿨다운
        self._ui_fallback_enqueue_last_ts: dict[str, float] = {}
        # 정상 경로에서도 숫자 열이 멎지 않도록 최소 enqueue를 강제하는 쿨다운
        self._ui_force_min_enqueue_last_ts: dict[str, float] = {}
        # wl_only(관심목록만) 종목은 기존에 UI 갱신이 제한되어 대비(vs_prev) 등이 고정될 수 있어
        # 최소 갱신을 위한 쿨다운 맵을 별도로 둔다.
        self._wl_only_ui_force_min_enqueue_last_ts: dict[str, float] = {}
        # 보유 완성15 평가 skip/HOLD 진단 — (code, slot, reason) 1회만 세션·GUI 기록
        self._held_eval_diag_once: set[str] = set()

        # RSI는 틱마다(특히 메인 스레드에서) 계산하면 Pandas rolling 비용으로 UI가 멈출 수 있어,
        # 최소 재계산 간격을 둬서 틱당 연산량을 줄인다.
        try:
            st = getattr(self.app, "settings", None) or {}
            self._realtime_rsi_calc_min_interval_sec: float = float(
                st.get("realtime_rsi_calc_min_interval_sec", 1.0)
            )
        except Exception:
            self._realtime_rsi_calc_min_interval_sec = 1.0
        self._realtime_rsi_calc_min_interval_sec = max(
            0.05, min(self._realtime_rsi_calc_min_interval_sec, 10.0)
        )
        self._rsi_calc_last_ts: dict[str, float] = {}
        self._rsi_calc_last_val: dict[str, float] = {}

        # 매 틱마다 Pandas 기반 지표/시그널을 모두 재계산하면 Tk 메인스레드가 장시간 점유될 수 있어
        # 종목별 "풀 계산" 및 "포지션 평가" 호출을 강하게 스로틀한다.
        try:
            st = getattr(self.app, "settings", None) or {}
            # 표시용 합치도/신호상태 — 주문 평가는 완성15 경로. 기본 간격을 넉넉히.
            self._realtime_s1_full_calc_min_interval_sec: float = float(
                st.get("realtime_s1_full_calc_min_interval_sec", 5.0)
            )
            self._realtime_eval_position_min_interval_sec: float = float(
                st.get("realtime_eval_position_min_interval_sec", 15.0)
            )
            self._realtime_exit_indicator_min_interval_sec: float = float(
                st.get("realtime_exit_indicator_min_interval_sec", 15.0)
            )
        except Exception:
            self._realtime_s1_full_calc_min_interval_sec = 5.0
            self._realtime_eval_position_min_interval_sec = 15.0
            self._realtime_exit_indicator_min_interval_sec = 15.0

        # 표시용: 최대 60초까지 허용(너무 잦은 풀계산 방지)
        self._realtime_s1_full_calc_min_interval_sec = max(
            0.05, min(self._realtime_s1_full_calc_min_interval_sec, 60.0)
        )
        # 청산·S2: 너무 짧으면 매수 직후 틱 노이즈에 과민 반응 — 상한은 5분까지 허용
        self._realtime_eval_position_min_interval_sec = max(
            0.05, min(self._realtime_eval_position_min_interval_sec, 300.0)
        )
        self._realtime_exit_indicator_min_interval_sec = max(
            0.05, min(self._realtime_exit_indicator_min_interval_sec, 300.0)
        )

        # 15m 꼬리·M1 리비전 기반 풀계산 트리거 시, 데이터가 안 바뀌어도 주기적으로 한 번은 재계산(지문 누락·예외 대비)
        try:
            st_fb = getattr(self.app, "settings", None) or {}
            self._realtime_s1_full_calc_fallback_sec: float = float(
                st_fb.get("realtime_s1_full_calc_fallback_sec", 120.0)
            )
        except Exception:
            self._realtime_s1_full_calc_fallback_sec = 120.0
        self._realtime_s1_full_calc_fallback_sec = max(
            15.0, min(self._realtime_s1_full_calc_fallback_sec, 900.0)
        )
        self._s1_last_15m_fingerprint: dict[str, Tuple[Any, ...]] = {}
        self._s1_last_m1_revision: dict[str, int] = {}

        self._s1_full_calc_last_ts: dict[str, float] = {}
        # 보유: M1 스토어 없을 때 시간 폴백 앵커(레거시·on_m1_revision_only=False)
        self._eval_exit_last_ts: dict[str, float] = {}
        # 보유 S2: 완성 15분 평가 경로와 동기(저정밀)
        self._held_s2_last_m1_rev: dict[str, int] = {}
        self._exit_indicator_last_ts: dict[str, float] = {}

        # 감시 트리 ATR 표시용 스로틀
        try:
            st2 = getattr(self.app, "settings", None) or {}
            self._realtime_monitor_atr_display_min_interval_sec: float = float(
                st2.get("realtime_monitor_atr_display_min_interval_sec", 0.5)
            )
        except Exception:
            self._realtime_monitor_atr_display_min_interval_sec = 0.5
        self._realtime_monitor_atr_display_min_interval_sec = max(
            0.05, min(self._realtime_monitor_atr_display_min_interval_sec, 5.0)
        )
        self._monitor_atr_cache_ts: dict[str, float] = {}
        self._monitor_atr_bg_inflight: set[str] = set()
        self._monitor_atr_bg_lock = threading.Lock()
        # 감시 트리 UI enqueue 스로틀(종목별) — 틱마다 pending 적재 시 Tk 메인 부하 완화
        self._ui_tree_enqueue_last_ts: dict[str, float] = {}
        self._ui_tree_enqueued_codes: set[str] = set()
        # 15분 슬롯 내 S1 pre-signal( Signal/Ready/Sig_Type )을 고정해
        # 틱마다 지표가 흔들리며 매수 신호가 on/off 되는 현상을 줄입니다.
        # key: code, value: {"slot_key": str, "signal": bool, "ready": bool, "sig_type": str}
        self._s1_slot_gate_cache: dict[str, dict[str, Any]] = {}
        # 로컬 15분 확정 슬롯당 S1 1회 평가 중복 방지: (code, slot_key) → monotonic ts
        self._s1_closed_slot_done: Dict[str, float] = {}

    def _s1_buy_signal_cooldown_sec(self) -> float:
        """
        동일 종목 S1 매수가 성공한 직후부터, 같은 종목에 다시 S1 매수를 허용하기까지 최소 대기(초).
        틱 단위 중복 신호·연속 주문 방지. 설정 `s1_buy_signal_cooldown_sec` (기본 1800=30분).
        """
        try:
            st = getattr(self.app, "settings", None) or {}
            v = float(st.get("s1_buy_signal_cooldown_sec", 1800))
        except (TypeError, ValueError):
            v = 1800.0
        return float(max(10.0, min(v, 86400.0)))

    def _atr_sl_reentry_cooldown_days(self) -> int:
        """ATR_SL 청산 후 동일종목 S1 재진입 금지 일수(0=비활성)."""
        try:
            st = getattr(self.app, "settings", None) or {}
            return max(0, int(float(st.get("atr_sl_reentry_cooldown_days", 0) or 0)))
        except (TypeError, ValueError):
            return 0

    def _atr_sl_streak_params(self) -> tuple[int, int, int]:
        """연속 ATR_SL 쿨다운: (lookback_days, streak_cooldown_days, min_count)."""
        st = getattr(self.app, "settings", None) or {}
        try:
            lookback = max(1, int(float(st.get("atr_sl_streak_lookback_days", 10) or 10)))
        except (TypeError, ValueError):
            lookback = 10
        try:
            streak = max(0, int(float(st.get("atr_sl_streak_cooldown_days", 0) or 0)))
        except (TypeError, ValueError):
            streak = 0
        try:
            min_count = max(2, int(float(st.get("atr_sl_streak_min_count", 2) or 2)))
        except (TypeError, ValueError):
            min_count = 2
        return lookback, streak, min_count

    def _note_atr_sl_exit_for_reentry_cooldown(self, code: str, reason: Any = None) -> None:
        """ATR_SL 청산 시 재진입 쿨다운 만료일 기록(실시간, 연속 손절 시 연장)."""
        try:
            base_days = self._atr_sl_reentry_cooldown_days()
            if base_days <= 0:
                return
            rs = str(reason or "").upper()
            if "ATR_SL" not in rs:
                return
            nk = str(canonical_stock_code(code) or code).zfill(6)
            exit_d = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            lookback, streak_days, min_count = self._atr_sl_streak_params()

            if not hasattr(self, "_atr_sl_exit_dates") or self._atr_sl_exit_dates is None:
                self._atr_sl_exit_dates = {}
            hist = list(self._atr_sl_exit_dates.get(nk) or [])
            hist.append(exit_d)
            cutoff = exit_d - timedelta(days=int(lookback))
            hist = [d for d in hist if isinstance(d, datetime) and d >= cutoff]
            self._atr_sl_exit_dates[nk] = hist

            days = int(base_days)
            if streak_days > base_days and len(hist) >= min_count:
                days = int(streak_days)

            new_until = exit_d + timedelta(days=int(days))
            if not hasattr(self, "_atr_sl_reentry_until") or self._atr_sl_reentry_until is None:
                self._atr_sl_reentry_until = {}
            prev = self._atr_sl_reentry_until.get(nk)
            if prev is not None:
                try:
                    p = prev
                    if hasattr(p, "to_pydatetime"):
                        p = p.to_pydatetime()
                    if hasattr(p, "replace"):
                        p = p.replace(hour=0, minute=0, second=0, microsecond=0)
                    if p > new_until:
                        return
                except Exception:
                    pass
            self._atr_sl_reentry_until[nk] = new_until
        except Exception:
            pass

    def _s1_blocked_by_atr_sl_reentry(self, code: str) -> bool:
        """ATR_SL 재진입 쿨다운 중이면 True."""
        try:
            nk = str(canonical_stock_code(code) or code).zfill(6)
            until = (getattr(self, "_atr_sl_reentry_until", None) or {}).get(nk)
            if until is None:
                return False
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            u = until
            if hasattr(u, "to_pydatetime"):
                u = u.to_pydatetime()
            if hasattr(u, "replace"):
                u = u.replace(hour=0, minute=0, second=0, microsecond=0)
            return bool(today <= u)
        except Exception:
            return False

    def _use_fixed_s1_presignal_gate(self) -> bool:
        """
        True면 동일 15분 슬롯 내에서는 첫 판정 시점의 S1 Signal/Ready를 고정합니다.

        ``realtime_s1_mirror_dynamic_intrabar`` 가 켜져 있으면 동적 고정밀과 동일하게
        분봉 단위로 판정하므로 슬롯 고정 게이트는 사용하지 않습니다(장전 False 고정 등 오판 방지).
        """
        try:
            if self._realtime_s1_mirror_dynamic_intrabar_enabled():
                return False
            st = getattr(self.app, "settings", None) or {}
            return bool(st.get("realtime_use_fixed_s1_presignal_gate", True))
        except Exception:
            return True

    def _market_halt_suppresses_s1(self, code: str) -> Optional[str]:
        """서킷브레이커·시장중단 15m 슬롯이면 S1 평가 억제 사유, 아니면 None."""
        try:
            from core.krx_market_halt_slots import should_suppress_s1_eval_for_code

            suppressed, reason = should_suppress_s1_eval_for_code(self.app, code)
            return reason if suppressed else None
        except Exception:
            return None

    def _hp_causal_ready_min_bar_index(self) -> int:
        """동적 고정밀 ``_prepare_15m_cached_data`` 의 Ready 최소 봉 인덱스."""
        try:
            st = getattr(self.app, "settings", None) or {}
            return max(0, int(float(st.get("hp_causal_ready_min_bar_index", 19))))
        except (TypeError, ValueError):
            return 19

    def _apply_ready_column_to_indicators(self, df_ind: Any) -> Any:
        """백테스트와 동일: 전역 15분봉 인덱스가 임계 이상일 때만 Ready=True."""
        try:
            if df_ind is None or getattr(df_ind, "empty", True):
                return df_ind
            ready_min = self._hp_causal_ready_min_bar_index()
            out = df_ind.copy()
            out["Ready"] = out.reset_index(drop=True).index >= ready_min
            return out
        except Exception:
            return df_ind

    def _last_bar_ready(self, bar_count: int) -> bool:
        try:
            return int(bar_count) >= self._hp_causal_ready_min_bar_index()
        except (TypeError, ValueError):
            return False

    def _merge_ready_into_sig(
        self, last_sig: dict[str, Any], bar_count: int
    ) -> dict[str, Any]:
        """``get_buy_signals_detailed`` 결과에 백테와 동일한 Ready 를 병합."""
        try:
            if not isinstance(last_sig, dict):
                return {}
            out = dict(last_sig)
            if "Ready" not in out:
                out["Ready"] = self._last_bar_ready(bar_count)
            return out
        except Exception:
            return last_sig if isinstance(last_sig, dict) else {}

    def _s1_buy_from_sig_dict(
        self, last_sig: dict[str, Any], bar_count: int
    ) -> bool:
        """동적 고정밀과 동일: ``Signal ∧ Ready``."""
        try:
            sig = self._merge_ready_into_sig(last_sig, bar_count)
            return bool(self._to_scalar(sig.get("Signal"), False)) and bool(
                self._to_scalar(sig.get("Ready"), False)
            )
        except Exception:
            return False



    def _s1_presignal_ready_for_gate(self, last_sig: dict[str, Any]) -> bool:
        """
        고정 프리시그널 게이트에 넣을 Ready 초기값.

        ``get_buy_signals_detailed`` 결과에는 ``Ready`` 가 없을 수 있으므로,
        호출 전 ``_merge_ready_into_sig`` 로 병합하는 것이 권장됩니다.
        """
        try:
            if not isinstance(last_sig, dict):
                return False
            if "Ready" not in last_sig:
                return False
            return bool(self._to_scalar(last_sig.get("Ready"), False))
        except Exception:
            return False


    def _s1_eval_on_local_15m_close_enabled(self) -> bool:
        """
        True면 로컬 15분 슬롯 확정 콜백에서 S1·S2·청산을 평가한다(저정밀 백테 정합).
        틱 주기 EXIT/STAGE2 주문은 생략한다.
        """
        try:
            st = getattr(self.app, "settings", None) or {}
            return bool(st.get("realtime_s1_eval_on_local_15m_close", True))
        except Exception:
            return True

    def _next_open_1530_enabled(self) -> bool:
        """15:30 신호를 익일 09:00 시초 동시호가 지정가로 이월할지."""
        try:
            st = getattr(self.app, "settings", None) or {}
            return bool(st.get("next_open_1530_enabled", False))
        except Exception:
            return False

    def _next_open_1945_enabled(self) -> bool:
        """19:45(애프터 말봉) 신호를 익일 시초 동시호가 지정가로 이월할지."""
        try:
            st = getattr(self.app, "settings", None) or {}
            return bool(st.get("next_open_1945_enabled", True))
        except Exception:
            return True

    def _order_1530_enabled(self) -> bool:
        """
        15:30 슬롯 실주문 허용(NXT 터치·익일시초 예약·당일 즉시).

        기본 False — 평가는 하되 주문은 당분간 생략.
        """
        try:
            st = getattr(self.app, "settings", None) or {}
            return bool(st.get("order_1530_enabled", False))
        except Exception:
            return False

    def _use_nxt_sor_touch_orders(self) -> bool:
        """
        실전 서버: 15:30 슬롯 NXT 1호가 터치 + SOR.

        15:15는 NXT와 무관하게 전 종목 동시호가 시장가.
        모의 서버는 False — 익일 09:00 예약 유지.
        종목별 가능 여부는 ``_code_uses_nxt_touch`` 를 사용한다.
        """
        try:
            import config

            return not bool(getattr(config, "USE_MOCK", False))
        except Exception:
            return False

    def _code_uses_nxt_touch(self, code: Any) -> bool:
        """
        실전 + 해당 종목 NXT 거래가능일 때만 True.

        불가·미조회는 False → 15:30은 next_open.
        15:15는 이 판정과 무관하게 동시호가 시장가.
        """
        try:
            svc = getattr(self.app, "nxt_eligibility_service", None)
            if svc is None:
                return False
            if hasattr(svc, "code_uses_nxt_touch"):
                return bool(svc.code_uses_nxt_touch(code, fetch_if_unknown=True))
            if not self._use_nxt_sor_touch_orders():
                return False
            return bool(svc.is_nxt_tradable(code, fetch_if_unknown=True))
        except Exception:
            return False

    def _eval_1530_slot_enabled(self) -> bool:
        """15:30 슬롯 완성15 평가 허용(모의 next_open 또는 실전 NXT 즉시)."""
        try:
            if self._use_nxt_sor_touch_orders():
                return True
            return bool(self._next_open_1530_enabled())
        except Exception:
            return False

    def _settings_for_s1_signals(self) -> Dict[str, Any]:
        """
        S1 신호 계산용 settings 복사본.

        모의 next_open·실전 NXT 모두 15:30 신호를 허용한다.
        (체결만 예약 vs SOR 로 갈라짐 — ``signals.py`` 마스크 해제)
        """
        try:
            st = dict(getattr(self.app, "settings", None) or {})
        except Exception:
            st = {}
        try:
            if self._eval_1530_slot_enabled():
                st["allow_1530_signal_eval"] = True
                st["_bt_allow_1530_signal_eval"] = True
        except Exception:
            pass
        return st














    def _closed_df_for_indicator_calc(self, closed_df: Any) -> Any:
        """
        확정 15분 지표 계산용 lookback 제한.

        bars_data 는 이미 tail(300) 이지만, 설정으로 상한을 줄여
        슬롯 경계 CPU 경합을 완화할 수 있습니다(신호 정합에 필요한 최소봉은 유지).
        """
        try:
            st = getattr(self.app, "settings", None) or {}
            max_rows = int(float(st.get("realtime_closed_15m_indicator_max_rows", 300)))
            max_rows = max(80, min(2000, max_rows))
        except Exception:
            max_rows = 300
        try:
            if closed_df is not None and len(closed_df) > max_rows:
                return closed_df.iloc[-max_rows:].copy()
        except Exception:
            pass
        return closed_df

    def _realtime_s1_mirror_dynamic_intrabar_enabled(self) -> bool:
        """
        [옵션 A: 저정밀 전용] 인트라바(M1 미러) 동적 고정밀 판정은 제거되어 항상 비활성입니다.

        과거에는 동적 고정밀과 동일하게 M1 봉 단위로 S1/청산/S2를 판정했으나,
        저정밀(공식 15분 확정 평가) 전용 구성으로 정리되며 이 경로는 완전히 꺼졌습니다.
        호출부 호환을 위해 메서드는 유지하고 항상 ``False`` 를 반환합니다.
        """
        return False

    def _held_eval_on_m1_revision_only(self) -> bool:
        """
        True면 보유 청산·S2 평가를 M1 리비전(완료 1분 반영) 시점에 맞춥니다.

        M1 폴러·스토어가 있을 때 ``realtime_eval_position_on_m1_revision_only`` 를 따릅니다.
        """
        try:
            st = getattr(self.app, "settings", None) or {}
            if not bool(st.get("realtime_eval_position_on_m1_revision_only", True)):
                return False
            try:
                from core.rt_closed_15m_official_replace import m1_api_poller_enabled

                if not m1_api_poller_enabled(st):
                    return False
            except Exception:
                if not bool(st.get("realtime_watchlist_m1_api_poller_enabled", True)):
                    return False
            return getattr(self.app, "rt_tick_m1_store", None) is not None
        except Exception:
            return False

    def _intrabar_mirror_get_full15m_and_m1(
        self, nk: str, full_15m_src: Any
    ) -> Optional[Tuple[pd.DataFrame, pd.DataFrame]]:
        """
        [옵션 A: 저정밀 전용] 인트라바(M1) 미러 경로는 제거된 죽은 경로입니다.

        과거에는 현재 15분 슬롯에 맞춘 15m 사본과 공식 1분 구간 M1 DataFrame을
        반환했으나, M1 의존성이 완전히 제거되어 항상 ``None`` 을 반환합니다.
        호출부 호환을 위해 메서드 시그니처만 유지합니다.
        """
        return None

    @staticmethod
    def _indicator_row_to_plain_dict(row: Any) -> Dict[str, Any]:
        """인트라바 지표 행(Series/dict)을 캐시용 dict 로 변환."""
        if row is None:
            return {}
        try:
            if isinstance(row, dict):
                return dict(row)
            if hasattr(row, "to_dict"):
                return dict(row.to_dict())
        except Exception:
            pass
        return {}

    def _intrabar_close_now_enabled(self) -> bool:
        """[옵션 A: 저정밀 전용] 인트라바 즉시가 판정은 제거되어 항상 비활성입니다."""
        return False


    def _unmark_s1_closed_slot_done(
        self, code: str, slot_key: str, *, prefix: str = "s1"
    ) -> None:
        """봉 미준비·예외 등으로 평가를 못 끝냈을 때 재시도 가능하도록 done 해제."""
        try:
            nk = str(canonical_stock_code(code) or code).zfill(6)
            key = f"{prefix}|{nk}|{slot_key}"
            done = getattr(self, "_s1_closed_slot_done", None)
            if isinstance(done, dict):
                done.pop(key, None)
        except Exception:
            pass

    def _session_log(self, kind: str, data: Dict[str, Any]) -> None:
        """세션 이벤트 로그(진단용). 실패해도 본 로직에 영향 없음."""
        try:
            sl = getattr(self.app, "session_logger", None)
            if sl is not None and hasattr(sl, "log"):
                sl.log(str(kind), dict(data or {}))
        except Exception:
            pass




    def eval_on_completed_15m(self, code: str, bar_ts: Any) -> None:
        """로컬 15분 확정: S1(미보유) + 청산/S2(보유) 일괄."""
        try:
            from utils.krx_bars import is_krx_no_signal_eval_15m_slot

            if is_krx_no_signal_eval_15m_slot(bar_ts) and not (
                self._eval_1530_slot_enabled()
                and self._is_1530_bar_slot(bar_ts)
            ):
                return
        except Exception:
            pass
        try:
            self.eval_s1_on_completed_15m(code, bar_ts)
        except Exception:
            pass
        try:
            self.eval_held_on_completed_15m(code, bar_ts)
        except Exception:
            pass





    @staticmethod
    def _position_has_strategy_entry_reason(pos: Dict[str, Any]) -> bool:
        """전략 진입 reason(TYPE_A/B 등) — DB active_positions 복구 시 events 없어도 청산 가능."""
        try:
            rs = str(pos.get("reason", "")).strip().upper()
            if rs in ("TYPE_A", "TYPE_B"):
                return True
            if "TYPE_A" in rs or "TYPE_B" in rs:
                return True
            if rs.startswith("S1_") or rs.startswith("S2_"):
                return True
        except Exception:
            pass
        return False




    def _format_s1_confluence_status_line(
        self, last_sig: Any, sel_track: str = "AB"
    ) -> str:
        """S1 합치도 표시 — ``core.signal_monitor_display`` 위임."""
        return format_s1_confluence_status_line(
            last_sig, sel_track, to_scalar=self._to_scalar
        )

    def _format_monitor_vs_prev_cell(self, info: dict) -> str:
        """감시 트리 '대비' 열 — ``core.signal_monitor_display`` 위임."""
        return format_monitor_vs_prev_cell(info)

    def _has_open_position(self, code: str) -> bool:
        """virtual_positions 에 보유 중인지(6자리·접미사 키 호환)."""
        try:
            vp = getattr(self.app, "virtual_positions", None) or {}
            if not vp:
                return False
            if code in vp:
                return True
            nk = str(code).zfill(6)
            if nk in vp:
                return True
            for k in vp:
                try:
                    if str(k).split("_")[0].zfill(6) == nk:
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False


    def _bars_key_and_frame(self, code: str) -> Tuple[str, Optional[Any]]:
        """
        ``bars_data`` 에서 종목 DF 조회.
        워밍업이 ``005930`` 으로 넣었는데 조회만 ``5930`` 처럼 어긋나면 canonical 로 승격합니다.
        """
        try:
            lk = getattr(self.app, "_bars_data_lock", None)
            ctx = lk if lk is not None else contextlib.nullcontext()
            with ctx:
                nk, df = resolve_bars_data_frame(
                    getattr(self.app, "bars_data", None), code, migrate_alias=True
                )
                return nk, df
        except Exception:
            try:
                nk = canonical_stock_code(code)
                return nk, (self.app.bars_data or {}).get(nk)
            except Exception:
                return str(code).split("_")[0].zfill(6), None

    def _indicator_last_row_for_exit(self, code: str) -> Optional[dict]:
        """
        S1 신호용 min_15m_bars 미만이어도 청산·S2 판단에 쓸 마지막 지표 행.
        (기존에는 bars_ready 가 False 면 evaluate_position 자체를 호출하지 않아 ATR 등 청산이 멈춤)
        """
        try:
            _nk, _bd = self._bars_key_and_frame(code)
            if _bd is None or getattr(_bd, "empty", True):
                return None
            if len(_bd) < 2:
                return None
            df_with_ind = TradingStrategy.calculate_indicators(_bd, self.app.settings)
            return df_with_ind.iloc[-1].to_dict()
        except Exception:
            return None



    def _schedule_monitor_atr_row_cache_bg(self, code: str, bars_df: Any) -> None:
        """보유행 ATR 표시용 지표행을 백그라운드에서 계산(틱 경로에서 calculate_indicators 금지)."""
        try:
            nk = str(code).split("_")[0].zfill(6)
            if not nk or bars_df is None or getattr(bars_df, "empty", True):
                return
            with self._monitor_atr_bg_lock:
                if nk in self._monitor_atr_bg_inflight:
                    return
                self._monitor_atr_bg_inflight.add(nk)
        except Exception:
            return

        settings = getattr(self.app, "settings", None) or {}
        bd_copy = bars_df

        def _worker() -> None:
            row_local: Optional[dict] = None
            try:
                df_i = TradingStrategy.calculate_indicators(bd_copy, settings)
                row_local = df_i.iloc[-1].to_dict()
            except Exception:
                row_local = None
            try:
                if isinstance(row_local, dict):
                    mi = getattr(self.app, "monitor_info", None) or {}
                    for key, info in mi.items():
                        try:
                            if str(key).split("_")[0].zfill(6) != nk:
                                continue
                            if isinstance(info, dict):
                                info["_monitor_atr_row_cache"] = row_local
                        except Exception:
                            continue
                    self._monitor_atr_cache_ts[nk] = time.time()
            except Exception:
                pass
            finally:
                try:
                    with self._monitor_atr_bg_lock:
                        self._monitor_atr_bg_inflight.discard(nk)
                except Exception:
                    pass

        try:
            threading.Thread(
                target=_worker, name=f"MonitorAtrCache-{nk}", daemon=True
            ).start()
        except Exception:
            try:
                with self._monitor_atr_bg_lock:
                    self._monitor_atr_bg_inflight.discard(nk)
            except Exception:
                pass



    def _code_in_virtual_positions(self, code: str) -> bool:
        """가상·실제 보유(virtual_positions)에 해당 종목이 있는지 6자리 기준으로 확인합니다."""
        try:
            nk = str(code).split("_")[0].zfill(6)
            vp = getattr(self.app.portfolio, "virtual_positions", None) or {}
            if nk in vp or code in vp:
                return True
            for k in vp:
                try:
                    if str(k).split("_")[0].zfill(6) == nk:
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _held_position_stage(self, code: str) -> int:
        """virtual_positions 기준 단계(``entry_n`` 우선)."""
        try:
            nk = str(code).split("_")[0].zfill(6)
            vp = getattr(self.app.portfolio, "virtual_positions", None) or {}
            pos_live = vp.get(code) or vp.get(nk)
            if pos_live is None:
                for _k, v in vp.items():
                    try:
                        if str(_k).split("_")[0].zfill(6) == nk and isinstance(v, dict):
                            pos_live = v
                            break
                    except Exception:
                        continue
            if not isinstance(pos_live, dict):
                return 1
            try:
                pf = getattr(self.app, "portfolio", None)
                if pf is not None and hasattr(pf, "position_effective_stage"):
                    return int(pf.position_effective_stage(pos_live))
            except Exception:
                pass
            return int(pos_live.get("entry_n", pos_live.get("stage", 1)) or 1)
        except Exception:
            return 1


    def _s1_m1_revision_snapshot(self, bars_key: str) -> int:
        """``RtTickM1Store`` 공식 1분 반영 리비전(없으면 0)."""
        try:
            nk = canonical_stock_code(str(bars_key).strip()) if bars_key else ""
            if len(nk) != 6:
                return 0
            m1s = getattr(self.app, "rt_tick_m1_store", None)
            if m1s is None:
                return 0
            rev_fn = getattr(m1s, "revision_for_code", None)
            if rev_fn is None or not callable(rev_fn):
                return 0
            return int(rev_fn(nk))
        except Exception:
            return 0



    def _watchlist_only_codes(self) -> set:
        """감시 선정에 없고 관심목록에만 있는 종목 (6자리). `core.monitor_code_sets` 와 동일 규칙."""
        try:
            mc = getattr(self.app.trading_controller, "monitor_codes", None) or []
            wl = self.app.watchlist_store.codes_set()
            return watchlist_only_codes_set(wl, mc)
        except Exception:
            return set()

    def _send_watchlist_telegram(self, msg: str) -> None:
        try:
            self.app.send_telegram_message(msg)
        except Exception:
            pass


