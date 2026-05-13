import copy
import pandas as pd
import numpy as np
import threading
import contextlib
from datetime import datetime
import time
import traceback
from backtest_engine import TradingStrategy
from core.bt_hp_intrabar_unified import evaluate_bt_hp_intrabar_unified
from core.bt_hp_signal_bridge import _apply_synthetic_metrics_m1
from core.defaults import effective_min_15m_bars_for_monitoring
from core.realtime_official_bars_sync import stream_slot_end_datetime
from utils.krx_bars import (
    m1_bar_open_label_for_just_completed_minute,
    m1_window_start_for_15m_end,
)
from core.monitor_code_sets import watchlist_only_codes_set
from core.monitor_signal_status_display import monitor_signal_status_display_text
from ui.tabs.monitor_tab import format_entry_signal_type_label
from utils.krx_session import is_krx_regular_session
from utils.bars_data_keys import resolve_bars_data_frame
from utils.normalization import canonical_stock_code
from typing import Any, Dict, Optional, Tuple


class SignalEngine:
    """[v25.7] 실시간 체결 데이터 분석 및 시그널 포착 엔진 클래스"""
    def __init__(self, app):
        self.app = app
        self._last_realtime_err = None
        self.last_buy_sig_time = {} # [v1.0] 중복 시그널 방지용 쿨타임 관리
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
            self._realtime_s1_full_calc_min_interval_sec: float = float(
                st.get("realtime_s1_full_calc_min_interval_sec", 1.0)
            )
            self._realtime_eval_position_min_interval_sec: float = float(
                st.get("realtime_eval_position_min_interval_sec", 1.0)
            )
            self._realtime_exit_indicator_min_interval_sec: float = float(
                st.get("realtime_exit_indicator_min_interval_sec", 1.0)
            )
        except Exception:
            self._realtime_s1_full_calc_min_interval_sec = 1.0
            self._realtime_eval_position_min_interval_sec = 1.0
            self._realtime_exit_indicator_min_interval_sec = 1.0

        self._realtime_s1_full_calc_min_interval_sec = max(
            0.05, min(self._realtime_s1_full_calc_min_interval_sec, 10.0)
        )
        self._realtime_eval_position_min_interval_sec = max(
            0.05, min(self._realtime_eval_position_min_interval_sec, 10.0)
        )
        self._realtime_exit_indicator_min_interval_sec = max(
            0.05, min(self._realtime_exit_indicator_min_interval_sec, 10.0)
        )

        self._s1_full_calc_last_ts: dict[str, float] = {}
        self._eval_position_last_ts: dict[str, float] = {}
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
        # 15분 슬롯 내 S1 pre-signal( Signal/Ready/Sig_Type )을 고정해
        # 틱마다 지표가 흔들리며 매수 신호가 on/off 되는 현상을 줄입니다.
        # key: code, value: {"slot_key": str, "signal": bool, "ready": bool, "sig_type": str}
        self._s1_slot_gate_cache: dict[str, dict[str, Any]] = {}

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

    def _use_fixed_s1_presignal_gate(self) -> bool:
        """
        True면 동일 15분 슬롯 내에서는 첫 판정 시점의 S1 Signal/Ready를 고정합니다.
        (백테 고정밀 경로의 pre-signal 게이팅과 유사하게 맞추기 위한 옵션)
        """
        try:
            st = getattr(self.app, "settings", None) or {}
            return bool(st.get("realtime_use_fixed_s1_presignal_gate", True))
        except Exception:
            return True

    def _s1_presignal_ready_for_gate(self, last_sig: dict[str, Any]) -> bool:
        """
        고정 프리시그널 게이트에 넣을 Ready 초기값.

        ``get_buy_signals_detailed`` 결과 dict에는 ``Ready`` 키가 없는 경우가 많습니다.
        이 때 ``bool(last_sig.get('Ready', False))`` 로만 읽으면 ready 가 항상 False 가 되어
        ``signal and ready`` 게이트가 매수를 영구 차단하는 버그가 생깁니다.
        키가 없으면 ``Signal`` 과 동일하게 두어, 슬롯 첫 판정의 신호가 그대로 통과합니다.
        ``Ready`` 키가 있으면(인과 S1 등) 그 값을 그대로 따릅니다.
        """
        try:
            if not isinstance(last_sig, dict):
                return False
            if "Ready" not in last_sig:
                return bool(self._to_scalar(last_sig.get("Signal"), False))
            return bool(self._to_scalar(last_sig.get("Ready"), False))
        except Exception:
            return False

    def _current_slot_key_from_bars(self, bars_df: Any) -> str:
        """bars_data 마지막 행 기준 15분 슬롯 키(YYYY-mm-dd HH:MM:SS)."""
        try:
            if bars_df is None or getattr(bars_df, "empty", True):
                return ""
            last_idx = bars_df.index[-1]
            return pd.Timestamp(last_idx).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""

    def _realtime_s1_mirror_dynamic_intrabar_enabled(self) -> bool:
        """
        동적 고정밀과 동일한 인트라바 판정 사용 여부.

        - 미보유: S1 신규(기존)
        - 보유: 청산(EXIT)·추매(S2) — ``evaluate_bt_hp_intrabar_unified`` 분기
        """
        try:
            st = getattr(self.app, "settings", None) or {}
            return bool(st.get("realtime_s1_mirror_dynamic_intrabar", True))
        except Exception:
            return True

    def _intrabar_mirror_get_full15m_and_m1(
        self, nk: str, full_15m_src: Any
    ) -> Optional[Tuple[pd.DataFrame, pd.DataFrame]]:
        """
        현재 15분 슬롯에 맞춘 15m 사본과 공식 1분 구간 ``[t0, t_hi]`` M1 DataFrame.

        ``None`` 이면 M1 저장소/봉 부재 등으로 인트라바 미러를 적용하지 않습니다.
        """
        try:
            if len(str(nk).strip()) != 6:
                return None
            m1s = getattr(self.app, "rt_tick_m1_store", None)
            if m1s is None:
                return None
            if full_15m_src is None or getattr(full_15m_src, "empty", True):
                return None
            full_15m = full_15m_src.copy()
            try:
                slot_end = pd.Timestamp(full_15m.index[-1])
            except Exception:
                slot_end = stream_slot_end_datetime(datetime.now())
            t0 = m1_window_start_for_15m_end(slot_end)
            try:
                wall = datetime.now()
                closed_open = m1_bar_open_label_for_just_completed_minute(wall)
                t_hi = min(slot_end, closed_open + pd.Timedelta(minutes=1))
            except Exception:
                try:
                    t_hi = min(
                        slot_end,
                        pd.Timestamp(datetime.now()).floor("min"),
                    )
                except Exception:
                    t_hi = slot_end
            if t_hi < t0:
                return None
            try:
                m1df = m1s.dataframe_for_window(
                    nk, t0, t_hi, include_open_bucket=False
                )
            except TypeError:
                m1df = m1s.dataframe_for_window(nk, t0, t_hi)
            if m1df is None or getattr(m1df, "empty", True):
                return None
            m1df = m1df.copy()
            try:
                _apply_synthetic_metrics_m1(
                    m1df, getattr(self.app, "settings", None) or {}
                )
            except Exception:
                pass
            return full_15m, m1df
        except Exception:
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

    def _intrabar_mirror_s1_buy_pack(
        self,
        code: str,
        full_15m_src: Any,
        df_with_ind: Any,
        info: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        미보유 S1: ``evaluate_bt_hp_intrabar_unified`` 로 동적 고정밀과 동일 분기.
        매수 성립 시 ``force_buy`` 및 갱신할 ``last_sig``/``last_row`` 를 돌려줍니다.
        """
        try:
            nk = str(code).strip().zfill(6)
            if len(nk) != 6:
                return None
            st = getattr(self.app, "settings", None) or {}
            pair = self._intrabar_mirror_get_full15m_and_m1(nk, full_15m_src)
            if pair is None:
                return None
            full_15m, m1df = pair
            ct = str(
                info.get("sel_track", info.get("SelTrack", "AB")) or "AB"
            ).strip().upper()
            if ct not in ("A", "B", "AB"):
                ct = "AB"
            out = evaluate_bt_hp_intrabar_unified(
                st, nk, full_15m, m1df, None, ct
            )
            if not isinstance(out, dict):
                return None
            se = out.get("s1_eval")
            if not isinstance(se, dict):
                return None
            if not bool(se.get("can_execute_entry")) or not bool(
                se.get("is_buy_signal")
            ):
                return None
            ind = se.get("indicator_to_use")
            ind_d = self._indicator_row_to_plain_dict(ind)
            base_row = self._indicator_row_to_plain_dict(
                df_with_ind.iloc[-1] if df_with_ind is not None and len(df_with_ind) else None
            )
            merged_row = dict(base_row)
            for k, v in ind_d.items():
                try:
                    merged_row[k] = v
                except Exception:
                    pass
            base_sig = {}
            try:
                detailed = info.get("last_detailed_sig")
                if detailed is not None and len(detailed) > 0:
                    ls = detailed.iloc[-1]
                    base_sig = (
                        ls.to_dict()
                        if hasattr(ls, "to_dict")
                        else dict(ls)
                    )
            except Exception:
                base_sig = {}
            if not base_sig:
                base_sig = dict(info.get("last_sig_cache") or {})
            base_sig["Signal"] = True
            base_sig["Ready"] = True
            try:
                stp = str(se.get("sig_type") or base_sig.get("Sig_Type") or "TYPE_B")
                base_sig["Sig_Type"] = stp
            except Exception:
                pass
            return {
                "force_buy": True,
                "last_sig": base_sig,
                "last_row": merged_row,
            }
        except Exception as e:
            try:
                if self._last_realtime_err != str(e):
                    self._last_realtime_err = str(e)
                    print(
                        f"[SignalEngine] _intrabar_mirror_s1_buy_pack 실패 ({code}): {e}"
                    )
            except Exception:
                pass
            return None

    def _intrabar_mirror_held_eval(
        self,
        code: str,
        info: Dict[str, Any],
        full_15m_src: Any,
    ) -> Optional[Dict[str, Any]]:
        """
        보유 시 청산·S2 를 ``evaluate_bt_hp_intrabar_unified``(동적 고정밀)과 동일 분기로 판정.

        Returns
        -------
        None
            M1 윈도우 없음·설정 OFF 등 → 기존 ``StrategyEngine.evaluate_position`` 사용.
        dict
            ``action`` 이 ``HOLD``/``EXIT``/``STAGE2`` — 미러 판정 완료 시 레거시 틱 평가 생략.
        """
        try:
            if not self._realtime_s1_mirror_dynamic_intrabar_enabled():
                return None
            if not self._code_in_virtual_positions(code):
                return None
            nk = str(code).strip().zfill(6)
            if len(nk) != 6:
                return None
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
                return None
            if bool(pos_live.get("exit_pending", False)):
                return {"action": "HOLD"}
            pair = self._intrabar_mirror_get_full15m_and_m1(nk, full_15m_src)
            if pair is None:
                return None
            full_15m, m1df = pair
            pos_sim = copy.deepcopy(pos_live)
            ct = str(
                info.get("sel_track", info.get("SelTrack", "AB")) or "AB"
            ).strip().upper()
            if ct not in ("A", "B", "AB"):
                ct = "AB"
            st = getattr(self.app, "settings", None) or {}
            out = evaluate_bt_hp_intrabar_unified(
                st, nk, full_15m, m1df, pos_sim, ct
            )
            if not isinstance(out, dict):
                return {"action": "HOLD"}
            ex = out.get("exit_first")
            if isinstance(ex, dict):
                ep = float(ex.get("eval_price", 0) or 0)
                pr = float(ex.get("profit_ratio", 0) or 0)
                rs = str(ex.get("reason", "") or "EXIT")
                return {
                    "action": "EXIT",
                    "reason": rs,
                    "profit_ratio": pr,
                    "now_price": ep,
                }
            s2 = out.get("stage2_addon")
            if isinstance(s2, dict):
                if bool(pos_live.get("s2_limit_order_pending", False)):
                    return {"action": "HOLD"}
                stg = int(pos_live.get("entry_n", pos_live.get("stage", 1)) or 1)
                if stg >= 2:
                    return {"action": "HOLD"}
                fill_px = float(s2.get("fill_price", 0) or 0)
                avg_p = float(
                    self._to_scalar(
                        pos_live.get("avg_price", pos_live.get("avg_p", 0)), 0.0
                    )
                )
                try:
                    pr_s2 = (
                        (fill_px / avg_p - 1.0) * 100.0
                        if avg_p > 0 and fill_px > 0
                        else 0.0
                    )
                except (TypeError, ValueError, ZeroDivisionError):
                    pr_s2 = 0.0
                return {
                    "action": "STAGE2",
                    "reason": str(s2.get("reason", "PROFIT_TRIGGER")),
                    "profit_ratio": pr_s2,
                    "now_price": fill_px,
                }
            return {"action": "HOLD"}
        except Exception as e:
            try:
                if self._last_realtime_err != str(e):
                    self._last_realtime_err = str(e)
                    print(
                        f"[SignalEngine] _intrabar_mirror_held_eval 실패 ({code}): {e}"
                    )
            except Exception:
                pass
            return None

    def _to_scalar(self, val, default=0):
        """[v4.5.9] 헬퍼: Series/DF/Numpy 등 어떤 형태에서든 안전하게 단일 스칼라 값 추출"""
        if val is None: return default
        # 1. Pandas 객체 처리 (Series, DataFrame)
        if hasattr(val, 'iloc'):
            if hasattr(val, 'size') and val.size == 0: return default
            return self._to_scalar(val.iloc[0], default)
        # 2. Numpy 스칼라 처리
        if hasattr(val, 'item') and not hasattr(val, 'shape'): # 진짜 스칼라인 경우만
            return val.item()
        # 3. 리스트/어레이 처리
        if isinstance(val, (list, np.ndarray)) and len(val) > 0:
            return self._to_scalar(val[0], default)
        return val

    def _format_s1_confluence_status_line(
        self, last_sig: Any, sel_track: str = "AB"
    ) -> str:
        """
        S1 합치도 표시: 눌림목(A)과 추세돌파(B) 조건 충족 수를 보여준다.
        선정 트랙이 A/B 단일이면 해당 트랙 지표만 표시(교차 트랙 문구 생략).
        """
        try:
            tr_raw = str(sel_track or "AB").strip().upper()
            tr = tr_raw if tr_raw in ("A", "B", "AB") else "AB"
            ma_c = int(self._to_scalar(last_sig.get("Met_A", 0), 0))
            ta_t = max(1, int(self._to_scalar(last_sig.get("Tot_A", 1), 1)))
            mb_c = int(self._to_scalar(last_sig.get("Met_B", 0), 0))
            tb_t = max(1, int(self._to_scalar(last_sig.get("Tot_B", 1), 1)))
            if tr == "A":
                return f"눌림A {ma_c}/{ta_t}"
            if tr == "B":
                return f"추세B {mb_c}/{tb_t}"
            st = str(self._to_scalar(last_sig.get("Sig_Type", "NONE"), "NONE"))
            if st == "TYPE_B":
                return f"추세B {mb_c}/{tb_t} (눌림A {ma_c}/{ta_t})"
            if st == "TYPE_A":
                return f"눌림A {ma_c}/{ta_t} (추세B {mb_c}/{tb_t})"
            return f"눌림A {ma_c}/{ta_t} · 추세B {mb_c}/{tb_t}"
        except Exception:
            try:
                mc = int(self._to_scalar(last_sig["Met_Count"], 0))
                tc = max(1, int(self._to_scalar(last_sig["Total_Count"], 1)))
                return f"({mc}/{tc})"
            except Exception:
                return "—"

    def _format_monitor_vs_prev_cell(self, info: dict) -> str:
        """감시 트리 '대비' 열: 전일 종가 대비 등락폭(원). 웹소켓에서 info['diff'] 유지."""
        try:
            raw = info.get("diff")
            if raw is None:
                return "—"
            s = str(raw).strip().replace(",", "")
            if s in ("", "-", "—"):
                return "—"
            iv = int(round(float(s)))
            if iv == 0:
                return "0"
            return f"{iv:+,}"
        except Exception:
            return "—"

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

    def _resolve_monitor_info_dict_key(self, nk: str) -> Optional[str]:
        """
        ``monitor_info`` 의 실제 dict 키를 반환합니다.
        선정 CSV·레거시에서 ``2380`` / ``2380.0`` 등으로 들어간 경우에도
        틱의 canonical(``002380``)과 매칭합니다.
        """
        try:
            mi = getattr(self.app, "monitor_info", None) or {}
            if not nk:
                return None
            if nk in mi:
                return nk
            for k in mi:
                try:
                    if canonical_stock_code(k) == nk:
                        return str(k)
                except Exception:
                    continue
        except Exception:
            pass
        return None

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

    def _parse_ws_vs_prev_close(self, info: dict) -> Tuple[Optional[float], Optional[float]]:
        """
        실시간 0B 패킷 fid 11·12: 전일 종가 대비 (원), 등락률 (% 수치, 부호 포함).
        파싱 실패·미수신 시 (None, None).
        """
        try:
            d_raw = info.get("diff")
            r_raw = info.get("ratio")
            d_f: Optional[float] = None
            r_f: Optional[float] = None
            if d_raw is not None:
                ds = str(d_raw).strip().replace(",", "")
                if ds not in ("", "-", "—"):
                    d_f = float(ds)
            if r_raw is not None:
                rs = (
                    str(r_raw)
                    .strip()
                    .replace("%", "")
                    .replace(",", "")
                )
                if rs not in ("", "-", "—"):
                    r_f = float(rs)
            return (d_f, r_f)
        except (TypeError, ValueError):
            return (None, None)

    def _monitor_tree_ratio_and_direction(
        self,
        info: dict,
        now_price: float,
        intraday_diff: float,
        open_price: float,
    ) -> Tuple[str, float]:
        """
        등락율 열 문자열과 행 방향(양수/음수/0).
        전일 종가 기준 fid 12·11을 우선하고, 없을 때만 시가 대비로 폴백한다.
        (시가 대비 %는 전일대비 '대비' 열과 불일치하는 원인이 됨)
        """
        try:
            d_ws, r_ws = self._parse_ws_vs_prev_close(info)
            # 전일대비(원)을 먼저 쓰면 '대비' 열( fid 11 )과 등락율이 항상 같은 기준이다.
            if d_ws is not None:
                if abs(d_ws) < 1e-12:
                    return ("0.00%", 0.0)
                prev_c = float(now_price) - float(d_ws)
                if prev_c > 1e-9:
                    pct = (float(d_ws) / prev_c) * 100.0
                    sg = "▲" if d_ws > 0 else "▼"
                    return (f"{sg} {abs(pct):.2f}%", float(d_ws))
                return ("0.00%", 0.0)
            if r_ws is not None:
                if abs(r_ws) < 1e-12:
                    return ("0.00%", 0.0)
                sg = "▲" if r_ws > 0 else "▼"
                return (f"{sg} {abs(r_ws):.2f}%", float(r_ws))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        try:
            op = float(open_price)
            if op > 1e-12:
                pct = (float(intraday_diff) / op) * 100.0
                if abs(pct) < 1e-12:
                    return ("0.00%", 0.0)
                sg = "▲" if intraday_diff > 0 else "▼"
                return (f"{sg} {abs(pct):.2f}%", float(pct))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        return ("0.00%", 0.0)

    def ratio_cell_display_for_monitor_tree(
        self, info: dict, now_price: float
    ) -> str:
        """
        감시 트리 「등락률」 열 표시 문자열 (항상 ▲ / ▼ + 수치%).
        ``info['ratio']`` 가 플러스/마이너스 문자열로 왔다 갔다 해도 동일 규칙으로 렌더한다.
        """
        try:
            op = float(self._to_scalar(info.get("open", 0), 0))
            if op <= 1e-12:
                op = float(now_price)
            np_f = float(now_price)
            intraday_diff = np_f - op
            return self._monitor_tree_ratio_and_direction(
                info, np_f, intraday_diff, op
            )[0]
        except Exception:
            return "0.00%"

    def _format_monitor_bid_ask_cell(self, info: dict) -> str:
        """감시 트리 Bid/Ask 열: 매도잔량 / 매수잔량 및 매도÷매수 비율(소수 1자리)."""
        try:
            from core.supply_display import format_bid_ask_monitor_display

            return format_bid_ask_monitor_display(
                self._to_scalar(info.get("total_ask", 0), 0),
                self._to_scalar(info.get("total_bid", 0), 0),
            )
        except Exception:
            return "— / — (—)"

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

    def on_tick_received(
        self,
        code,
        now_price,
        volume,
        _trde_val,
        *,
        defer_main_callbacks: bool = False,
        **kwargs: Any,
    ) -> None:
        """
        실시간 체결 데이터(Tick) 처리 본체. `_trde_val` 은 호출 규격 유지용(감시 트리 대금 열 제거).

        defer_main_callbacks=True:
            매수·전략 액션·Tk 트리/가상탭 예약은 메인 스레드 큐로만 넘깁니다(워커 스레드 전용).
        """
        # UI/디버그 목적용 특정 종목만 예외 정보를 JSONL로 남깁니다.
        debug_codes: set[str] = {"016360", "042700", "267250", "454910"}
        try:
            if now_price <= 0: return # [v2.2] 유효하지 않은 가격 데이터 로직 진입 차단
            code = canonical_stock_code(code)
            mi_key = self._resolve_monitor_info_dict_key(code)
            if self._code_in_virtual_positions(code) and mi_key is None:
                try:
                    self.app._ensure_vp_monitor_stub(code)
                except Exception:
                    pass
                mi_key = self._resolve_monitor_info_dict_key(code)
            if mi_key is None:
                # 틱 수신이 있어도 UI/신호 업데이트는 monitor_info가 있어야만 진행됩니다.
                # 일부 종목만 숫자가 안 바뀌는 원인 추적용으로 최소 경고만 남깁니다.
                try:
                    if len(self._missing_monitor_info_logged) < self._missing_monitor_info_log_limit:
                        if code not in self._missing_monitor_info_logged:
                            self._missing_monitor_info_logged.add(code)
                            self.app.log(f"⚠️ [Tick] monitor_info 없음 → 틱 스킵 코드={code}")
                            # GUI 로그가 안 보일 수 있으니 JSONL에도 남김
                            try:
                                sl = getattr(self.app, "session_logger", None)
                                if sl is not None:
                                    sl.log(
                                        "signal_engine_monitor_info_missing",
                                        {"code": str(code).zfill(6), "tried_type": type(code).__name__},
                                    )
                            except Exception:
                                pass
                except Exception:
                    # 로깅 실패는 무시 (매매/틱 흐름 막지 않기)
                    pass
                return
            info = self.app.monitor_info[mi_key]
            # 지정가 주문 시세 검증용: 틱 단가로 캐시를 매 틱 동기화 (화면·주문 기준 불일치 방지)
            try:
                info["last_price"] = float(now_price)
                info["price"] = float(now_price)
            except (TypeError, ValueError):
                pass
            try:
                if info.get("vp_price_stub"):
                    info.pop("vp_price_stub", None)
            except Exception:
                pass
            try:
                wl_all_set = self.app.watchlist_store.codes_set()
            except Exception:
                wl_all_set = set()
            is_in_watchlist = code in wl_all_set
            wl_only_set = self._watchlist_only_codes()
            is_wl_only = code in wl_only_set
            try:
                _wl_ap = self.app.watchlist_store.get_avg_price(code)
            except Exception:
                _wl_ap = None
            has_wl_avg = _wl_ap is not None and float(_wl_ap) > 0

            # [진단] pending_tree_updates 큐잉에서 제외(wl_only)되는지 여부 확인용
            try:
                debug_codes = {"016360", "042700", "267250", "454910"}
                if str(code).zfill(6) in debug_codes:
                    now_t = time.time()
                    last_t = float(self._ui_debug_log_last_ts.get(code, 0.0) or 0.0)
                    if now_t - last_t >= 5.0:
                        self._ui_debug_log_last_ts[code] = now_t
                        sl = getattr(self.app, "session_logger", None)
                        if sl is not None:
                            sl.log(
                                "signal_engine_wl_only_state",
                                {
                                    "code": str(code).zfill(6),
                                    "is_in_watchlist": bool(is_in_watchlist),
                                    "is_wl_only": bool(is_wl_only),
                                    "bars_ready": bool(bars_ready) if "bars_ready" in locals() else None,
                                    "now_price": float(now_price),
                                },
                            )
            except Exception:
                pass
            debug_enabled = str(code).zfill(6) in {"016360", "042700", "267250", "454910"}

            # 0D(호가) 전용 패킷은 봉 업데이트를 건너뛰고 수급 필드만 반영합니다.
            is_orderbook_only = bool(kwargs.get("_orderbook_only", False))

            # Update additional fields from kwargs (vol_power, total_ask, total_bid 등)
            for k, v in kwargs.items():
                if k == "_orderbook_only":
                    continue
                if str(k).startswith("_"):
                    continue
                info[k] = v

            # 틱 시각: 인과 S1 1분 완성 정렬(없으면 수신 시각으로 채움 → 분 경계 판정용)
            try:
                if kwargs.get("ts_wall") is None:
                    kwargs["ts_wall"] = datetime.now().strftime(
                        "%Y-%m-%d %H:%M:%S.%f"
                    )
            except Exception:
                pass

            # 1. 15분 OHLCV는 ``realtime_official_bars_sync``(공식 1분·슬롯 롤)만 갱신. 틱으로 봉을 바꾸지 않음.
            # 2. 실시간 지표 추출 (RSI, MACD 등)
            rsi_val = self._calculate_realtime_rsi(code)
            
            # 3. 데이터 추출
            open_price = info.get('open', 0)
            if open_price == 0:
                open_price = now_price
                info['open'] = open_price
            # 시가 대비(내부용). 감시 트리 '등락율'은 전일종가 fid 12 우선(_monitor_tree_ratio_and_direction).
            intraday_diff = float(now_price) - float(open_price)
            
            # 4. 시그널 상태 제어
            tag = ""
            ma60_ratio = 0.0
            info['confluence'] = info.get('confluence', '-')
            
            # RSI 50 돌파 신호 로직
            s_state = self.app.rsi_signals.get(code, {'last_rsi': 0.0, 'signal_time': None})
            last_rsi_f = float(s_state.get('last_rsi', 0.0))
            now_dt = datetime.now()
            try:
                _session_ok = is_krx_regular_session(now_dt)
            except Exception:
                _session_ok = True

            if last_rsi_f > 0 and last_rsi_f < 50.0 and rsi_val >= 50.0:
                s_state['signal_time'] = now_dt
            
            if s_state['signal_time']:
                if rsi_val < 50.0:
                    s_state['signal_time'] = None
                elif (now_dt - s_state['signal_time']).total_seconds() > self.app.settings.get('rsi_window', 45) * 60:
                    s_state['signal_time'] = None
                else:
                    tag = "rsi_signal"
            
            s_state['last_rsi'] = float(rsi_val)
            self.app.rsi_signals[code] = s_state

            # [Phase 5] 수급 분석 (Orderbook Analysis)
            total_ask = info.get('total_ask', 0)
            total_bid = info.get('total_bid', 1) # Prevent div by zero
            ask_bid_ratio = total_ask / total_bid if total_bid > 0 else 0
            bid_ask_ratio = total_bid / total_ask if total_ask > 0 else 0
            # 0D TR은 total_* 만 넣고 ask_bid_ratio 키는 안 넣음. Smart Buy가 info.get(..., 1.0)을 쓰면
            # BA 임계(예: 1.5)보다 낮은 가짜 1.0으로 매수가 전부 막히는 오류가 난다 → 계산값을 info에 반영.
            try:
                info["ask_bid_ratio"] = float(ask_bid_ratio)
                info["bid_ask_ratio"] = float(bid_ask_ratio)
            except (TypeError, ValueError):
                try:
                    info["ask_bid_ratio"] = 0.0
                    info["bid_ask_ratio"] = 0.0
                except Exception:
                    pass

            # 체결강도 하회 지속 관리
            vol_power = info.get('vol_power', 100.0)
            is_power_exhausted = False
            if vol_power < self.app.settings.get('smart_exit_vp', 100.0):
                if info.get('power_below_start') is None:
                    info['power_below_start'] = now_dt
                else:
                    duration = (now_dt - info['power_below_start']).total_seconds() / 60.0
                    if duration >= 2: # Keep 2 mins as standard for now or use a new key
                        is_power_exhausted = True
            else:
                info['power_below_start'] = None

            # [v35.7] 자가 학습형 합성 모델 보정 (Adaptive Calibration)
            
            # 5. S1 지표·신호: 워밍업(bars_ready) 후에는 매 체결마다 재계산한다.
            # 과거 100틱 스로틀은 조건 충족 시점이 최대 한 15분 슬롯만큼 밀리는 원인이었다.
            try:
                _min_sig_bars = effective_min_15m_bars_for_monitoring(
                    self.app.settings
                )
            except Exception:
                _min_sig_bars = 60
            bars_key, _bd = self._bars_key_and_frame(code)
            try:
                current_bar_count = len(_bd) if _bd is not None and not getattr(_bd, "empty", True) else 0
            except Exception:
                current_bar_count = 0
            # 15분봉(마지막 미완성 1봉 포함) 행 수가 기준 미만이면 S1 신호·청산·S2 전부 보류
            bars_ready = (
                _bd is not None
                and not getattr(_bd, "empty", True)
                and current_bar_count >= _min_sig_bars
            )

            is_buy = False
            last_row = None
            if bars_ready:
                now_ts = time.time()
                last_calc_ts = float(self._s1_full_calc_last_ts.get(code, 0.0) or 0.0)
                cached_last_row = info.get("last_row_cache")
                cached_last_sig_cache = info.get("last_sig_cache")

                # 워밍업에서 last_* 캐시를 채운 뒤 last_calc_ts 가 아직 없으면(0),
                # 첫 실시간 틱에서 전량 재계산하면 종목 수만큼 지연·큐 적체가 생김.
                if cached_last_row is None or cached_last_sig_cache is None:
                    need_full_calc = True
                elif last_calc_ts <= 0.0:
                    need_full_calc = False
                else:
                    need_full_calc = (
                        (now_ts - last_calc_ts)
                        >= self._realtime_s1_full_calc_min_interval_sec
                    )

                last_sig: dict = {}
                last_row = cached_last_row
                _bt_hp_res: Optional[Dict[str, Any]] = None

                if need_full_calc:
                    _src_df = self.app.bars_data.get(bars_key) if bars_key else None
                    if _src_df is None:
                        _src_df = _bd
                    df_with_ind = TradingStrategy.calculate_indicators(
                        _src_df, self.app.settings
                    )
                    try:
                        info["last_detailed_sig"] = TradingStrategy.get_buy_signals_detailed(
                            df_with_ind, self.app.settings, "UP"
                        )
                        info["last_bar_count"] = current_bar_count
                        info["last_row_cache"] = df_with_ind.iloc[-1].to_dict()

                        detailed_signals = info["last_detailed_sig"]
                        last_ser = detailed_signals.iloc[-1]
                        last_sig = (
                            last_ser.to_dict()
                            if hasattr(last_ser, "to_dict")
                            else dict(last_ser)
                        )
                        info["last_sig_cache"] = last_sig
                    except Exception as exc_sig:
                        # 시그널/지표 계산 실패 시 캐시가 있으면 아래 분기에서 안내 없이 재사용
                        if self._last_realtime_err != str(exc_sig):
                            self._last_realtime_err = str(exc_sig)
                            try:
                                print(
                                    f"[SignalEngine] get_buy_signals_detailed 실패 ({code}): {exc_sig}"
                                )
                            except Exception:
                                pass
                            traceback.print_exc()

                    # 풀 계산이 성공했는지(캐시 갱신 확인)
                    if info.get("last_row_cache") is None or info.get("last_sig_cache") is None:
                        info["confluence"] = "시그널 계산 실패(봉·지표 확인)"
                        info["met_ratio"] = 0.0
                        is_buy = False
                        last_row = None
                        rsi_sig = 0.0
                        macd_val = 0.0
                        osc_val = 0.0
                        ma60_scalar = 0.0
                        ma60_ratio = 0.0
                    else:
                        self._s1_full_calc_last_ts[code] = now_ts
                        last_sig = info["last_sig_cache"]
                        last_row = info["last_row_cache"]
                        # [v0.8.11] 미보유 S1: 동적 고정밀과 동일 인트라바 통합 판정(가능 시)
                        is_buy = bool(self._to_scalar(last_sig.get("Signal"), False))
                        try:
                            if (
                                self._realtime_s1_mirror_dynamic_intrabar_enabled()
                                and not self._code_in_virtual_positions(code)
                            ):
                                pack = self._intrabar_mirror_s1_buy_pack(
                                    code, _src_df, df_with_ind, info
                                )
                                if isinstance(pack, dict) and pack.get("force_buy"):
                                    is_buy = True
                                    if isinstance(pack.get("last_sig"), dict):
                                        last_sig = pack["last_sig"]
                                        info["last_sig_cache"] = last_sig
                                    if isinstance(pack.get("last_row"), dict):
                                        last_row = pack["last_row"]
                                        info["last_row_cache"] = last_row
                        except Exception as _mir_e:
                            try:
                                print(
                                    f"[SignalEngine] 인트라바 S1 미러 예외 ({code}): {_mir_e}"
                                )
                            except Exception:
                                pass
                        _sel_tr = str(
                            info.get("sel_track", info.get("SelTrack", "AB")) or "AB"
                        ).strip().upper()
                        info["confluence"] = self._format_s1_confluence_status_line(
                            last_sig, _sel_tr
                        )
                        info["met_ratio"] = float(
                            self._to_scalar(last_sig.get("Met_Ratio"), 0.0)
                        )

                        rsi_sig = float(self._to_scalar(last_row.get("RSI_Sig", 0), 0.0))
                        macd_val = float(self._to_scalar(last_row.get("MACD", 0), 0.0))
                        osc_val = float(self._to_scalar(last_row.get("OSC", 0), 0.0))

                        ma60_scalar = float(self._to_scalar(last_row.get("MA60"), 0.0))
                        ma60_ratio = (
                            (now_price / ma60_scalar) * 100.0 if ma60_scalar > 0 else 0.0
                        )
                else:
                    # 캐시 재사용 경로
                    # 아래 게이트·스마트바이 분기에서 항상 참조되도록 선행 정의(else 경로 NameError 방지)
                    _sel_tr = str(
                        info.get("sel_track", info.get("SelTrack", "AB")) or "AB"
                    ).strip().upper()
                    last_sig = cached_last_sig_cache if isinstance(cached_last_sig_cache, dict) else {}
                    last_row = cached_last_row
                    if last_sig and isinstance(last_row, dict):
                        is_buy = bool(self._to_scalar(last_sig.get("Signal"), False))
                        info["confluence"] = self._format_s1_confluence_status_line(
                            last_sig, _sel_tr
                        )
                        info["met_ratio"] = float(
                            self._to_scalar(last_sig.get("Met_Ratio"), 0.0)
                        )

                        rsi_sig = float(self._to_scalar(last_row.get("RSI_Sig", 0), 0.0))
                        macd_val = float(self._to_scalar(last_row.get("MACD", 0), 0.0))
                        osc_val = float(self._to_scalar(last_row.get("OSC", 0), 0.0))
                        ma60_scalar = float(self._to_scalar(last_row.get("MA60"), 0.0))
                        ma60_ratio = (
                            (now_price / ma60_scalar) * 100.0 if ma60_scalar > 0 else 0.0
                        )
                    else:
                        info["confluence"] = "시그널 계산 대기(캐시 확인)"
                        info["met_ratio"] = 0.0
                        is_buy = False
                        last_row = None
                        rsi_sig = 0.0
                        macd_val = 0.0
                        osc_val = 0.0
                        ma60_scalar = 0.0
                        ma60_ratio = 0.0
                
                    # [v0.6.43] 동일 15분 슬롯 내 S1 pre-signal 고정 게이트
                    # - 슬롯이 바뀌면 현재 계산 결과로 게이트를 새로 생성
                    # - 슬롯이 유지되는 동안은 게이트 signal/ready/sig_type 유지
                    #   (틱별 미세 OHLC 변화로 신호가 흔들리는 문제 완화)
                    try:
                        if self._use_fixed_s1_presignal_gate():
                            slot_key = self._current_slot_key_from_bars(_bd)
                            if slot_key:
                                gate = self._s1_slot_gate_cache.get(code)
                                if (
                                    not isinstance(gate, dict)
                                    or str(gate.get("slot_key", "")) != slot_key
                                ):
                                    gate = {
                                        "slot_key": slot_key,
                                        "signal": bool(self._to_scalar(last_sig.get("Signal"), False)),
                                        "ready": bool(self._s1_presignal_ready_for_gate(last_sig)),
                                        "sig_type": str(self._to_scalar(last_sig.get("Sig_Type", "TYPE_B"), "TYPE_B")),
                                    }
                                    self._s1_slot_gate_cache[code] = gate
                                is_buy = bool(gate.get("signal", False) and gate.get("ready", False))
                                last_sig["Signal"] = bool(gate.get("signal", False))
                                last_sig["Ready"] = bool(gate.get("ready", False))
                                last_sig["Sig_Type"] = str(gate.get("sig_type", last_sig.get("Sig_Type", "TYPE_B")))
                    except Exception:
                        pass
                    # 슬롯 게이트로 last_sig 가 바뀐 뒤에도 합치도 문구가 갱신 전 값으로 남으면 신호상태 열이 "고정"된 것처럼 보일 수 있음
                    try:
                        if last_sig and isinstance(last_row, dict):
                            info["confluence"] = self._format_s1_confluence_status_line(
                                last_sig, _sel_tr
                            )
                            info["met_ratio"] = float(
                                self._to_scalar(last_sig.get("Met_Ratio"), 0.0)
                            )
                    except Exception:
                        pass

                    if is_buy:
                        sig_type = str(
                            self._to_scalar(last_sig.get("Sig_Type", "TYPE_B"), "TYPE_B")
                        )
                        # Smart Buy 스위치는 감시 '전략유형'(sel_track)과 맞춤. sel_track=A 인데 Sig_Type만 B면
                        # Type B SB가 적용되어 '수급 대기'가 뜨는 불일치를 방지함.
                        if _sel_tr == "A":
                            use_sb_for_type = bool(
                                self.app.settings.get("use_smart_buy_a", False)
                            )
                        elif _sel_tr == "B":
                            use_sb_for_type = bool(
                                self.app.settings.get("use_smart_buy_b", True)
                            )
                        else:
                            st_up = str(sig_type).upper()
                            if "TYPE_B" in st_up:
                                use_sb_for_type = bool(
                                    self.app.settings.get("use_smart_buy_b", True)
                                )
                            elif "TYPE_A" in st_up:
                                use_sb_for_type = bool(
                                    self.app.settings.get("use_smart_buy_a", False)
                                )
                            else:
                                use_sb_for_type = False
                    
                        if use_sb_for_type:
                            vol_power = float(self._to_scalar(info.get('vol_power', 100.0), 100.0))
                            ask_bid_ratio = float(self._to_scalar(info.get('ask_bid_ratio', 1.0), 1.0))
                            min_p = float(self.app.settings.get('smart_buy_vp', 100.0))
                            # S2·백테스트와 동일 키. 구버전에 smart_buy_ratio만 있으면 폴백
                            _st = self.app.settings
                            min_r = float(
                                _st.get("smart_buy_ba_ratio", _st.get("smart_buy_ratio", 1.5))
                            )
                        
                            if vol_power < min_p or ask_bid_ratio < min_r:
                                is_buy = False
                                info['confluence'] = "수급 대기"
                                # 종합 리포트에서 스마트 진입 거절 건수 집계용 — 동일 15분봉당 1회만 기록
                                try:
                                    _sb_log_key = "_smart_buy_block_logged_bar"
                                    if info.get(_sb_log_key) != current_bar_count:
                                        info[_sb_log_key] = current_bar_count
                                        _sl = getattr(self.app, "session_logger", None)
                                        if _sl:
                                            _sl.log(
                                                "smart_buy_blocked",
                                                {
                                                    "code": str(code).zfill(6),
                                                    "name": str(info.get("name", "")),
                                                    "vol_power": vol_power,
                                                    "ask_bid_ratio": ask_bid_ratio,
                                                    "min_vp": min_p,
                                                    "min_ba": min_r,
                                                    "sig_type": str(
                                                        self._to_scalar(
                                                            last_sig.get("Sig_Type", ""),
                                                            "",
                                                        )
                                                    ),
                                                },
                                            )
                                except Exception:
                                    pass

                # 관심목록 전용: Type B 매수 신호만 (감시와 동일 필터 후 B만)
                if is_wl_only and is_buy:
                    _st_wl = str(self._to_scalar(last_sig.get("Sig_Type", "TYPE_B"), "TYPE_B"))
                    if "TYPE_B" not in _st_wl.upper():
                        is_buy = False

                # 정규장 외(장전·장후): 매수·알림 전에 차단 (합치도 문구만 통일)
                if not _session_ok:
                    info["confluence"] = "⏳ 정규장 외(실시간 체결 미진행) · 봉 준비"
                    info["met_ratio"] = 0.0
                    is_buy = False

                # 쿨타임 및 중복 진입 방지
                _cool_sec = self._s1_buy_signal_cooldown_sec()
                is_cool = True
                if code in self.last_buy_sig_time:
                    if (datetime.now() - self.last_buy_sig_time[code]).total_seconds() < _cool_sec:
                        is_cool = False
                
                if is_buy and is_cool and (not is_wl_only) and code not in self.app.virtual_positions:
                    try:
                        sl = getattr(self.app, "session_logger", None)
                        if sl:
                            sl.log(
                                "signal_s1_raw",
                                {
                                    "code": code,
                                    "name": str(info.get("name", "")),
                                    "price": float(now_price),
                                    "met_ratio": float(
                                        self._to_scalar(info.get("met_ratio", 0.0), 0.0)
                                    ),
                                    "confluence": str(info.get("confluence", "")),
                                },
                            )
                    except Exception:
                        pass
                    buy_ok = False
                    if defer_main_callbacks:
                        try:
                            # 메인 처리 전 동일 종목이 연속 큐 적재되는 것을 막기 위해 쿨타임 앵커를 먼저 둡니다.
                            self.last_buy_sig_time[code] = datetime.now()
                            self.app.enqueue_tick_main_action(
                                "buy_signal",
                                (
                                    str(code).zfill(6),
                                    float(now_price),
                                    float(rsi_sig),
                                    float(macd_val),
                                    float(osc_val),
                                    float(ma60_ratio),
                                ),
                            )
                        except Exception:
                            pass
                    else:
                        try:
                            buy_ok = bool(
                                self.app._on_buy_signal_received(
                                    code,
                                    now_price,
                                    rsi_sig,
                                    macd_val,
                                    osc_val,
                                    ma60_ratio,
                                )
                            )
                        except Exception:
                            buy_ok = False
                        if buy_ok:
                            self.last_buy_sig_time[code] = datetime.now()

                # 관심목록(감시 제외): 평단 미등록 시에만 매수 신호 텔레그램 (등록 시 매도만 감시)
                if is_wl_only and is_buy and is_cool and not has_wl_avg:
                    _t_wb = self._wl_buy_last_tm.get(code)
                    if _t_wb is None or (datetime.now() - _t_wb).total_seconds() >= _cool_sec:
                        self._wl_buy_last_tm[code] = datetime.now()
                        try:
                            _nm = self.app.watchlist_store.get_display_name(code)
                        except Exception:
                            _nm = str(info.get("name", code))
                        _mr = float(self._to_scalar(info.get("met_ratio", 0.0), 0.0))
                        _msg = (
                            "<b>⭐ [텔레그램·관심목록 전용]</b>\n"
                            "<i>감시 자동매매/주문과 별도 채널 (알림만)</i>\n\n"
                            "<b>TYPE B 매수 신호</b>\n"
                            f"종목: {_nm} ({code})\n"
                            f"가: {int(now_price):,}원\n"
                            f"합치도: {_mr * 100:.0f}%\n"
                            f"시간: {datetime.now().strftime('%H:%M:%S')}"
                        )
                        threading.Thread(
                            target=self._send_watchlist_telegram,
                            args=(_msg,),
                            daemon=True,
                        ).start()
                try:
                    info["_bt_hp_bridge_res"] = None
                except Exception:
                    pass
            else:
                info['confluence'] = f"봉 충전 {current_bar_count}/{_min_sig_bars} (신호·매매 대기)"
                info['met_ratio'] = 0.0
                try:
                    info["_bt_hp_bridge_res"] = None
                except Exception:
                    pass

            # 청산·S2 표시 공통: 마지막 지표 행 (봉 부족 시 보유면 보조 계산)
            eval_ind: Optional[dict] = None
            if bars_ready:
                eval_ind = last_row
            elif self._has_open_position(code):
                now_ts = time.time()
                last_exit_ts = float(self._exit_indicator_last_ts.get(code, 0.0) or 0.0)
                cached_exit_row = info.get("exit_last_row_cache")
                if (
                    cached_exit_row is not None
                    and (now_ts - last_exit_ts) < self._realtime_exit_indicator_min_interval_sec
                ):
                    eval_ind = cached_exit_row
                else:
                    eval_ind = self._indicator_last_row_for_exit(code)
                    if eval_ind is not None:
                        info["exit_last_row_cache"] = eval_ind
                        self._exit_indicator_last_ts[code] = now_ts

            # 보유 중이면 신호상태는 S1 합치도 대신 S2 추매 합치도
            if self._code_in_virtual_positions(code) and eval_ind is not None:
                try:
                    vp = getattr(self.app.portfolio, "virtual_positions", None) or {}
                    pos_h = vp.get(code) or vp.get(str(code).zfill(6))
                    if pos_h is None:
                        nk = str(code).zfill(6)
                        for k, v in vp.items():
                            if str(k).split("_")[0].zfill(6) == nk:
                                pos_h = v
                                break
                    if isinstance(pos_h, dict):
                        stg = int(pos_h.get("entry_n", pos_h.get("stage", 1)) or 1)
                        _sel_h = info.get("sel_track") or info.get("SelTrack")
                        # stage>=2(추매 체결)여도 S1 진입 시각 기준 지연 미충족이면
                        # 「추매(S2) 완료」로 덮어쓰지 않음(동기화·DB 불일치 시 오표시 방지).
                        if stg >= 2 and TradingStrategy.s2_delay_elapsed_for_position(
                            pos_h,
                            self.app.settings,
                            datetime.now(),
                            sel_track_hint=_sel_h,
                        ):
                            info["confluence"] = "추매(S2) 완료"
                            info["met_ratio"] = 1.0
                        else:
                            s2_txt, s2_mr = (
                                TradingStrategy.compute_s2_confluence_display_text(
                                    pos_h,
                                    self.app.settings,
                                    eval_ind,
                                    market_trend="UP",
                                    curr_dt=datetime.now(),
                                    sel_track_hint=_sel_h,
                                    now_price=float(now_price),
                                )
                            )
                            info["confluence"] = s2_txt
                            info["met_ratio"] = float(s2_mr)
                except Exception:
                    pass

            # 7. 실시간 보유 포지션: 청산·S2 (지표 행 확보 시; 봉 수 부족 시에도 보유면 보조 행 사용)
            bid_ask_ratio = float(self._to_scalar(info.get('bid_ask_ratio', 1.0), 1.0))
            ask_bid_ratio = float(self._to_scalar(info.get('ask_bid_ratio', 1.0), 1.0))
            vol_power = float(self._to_scalar(info.get('vol_power', 100.0), 100.0))

            if eval_ind is not None and not is_wl_only and _session_ok:
                now_ts = time.time()
                last_eval_ts = float(self._eval_position_last_ts.get(code, 0.0) or 0.0)
                if (now_ts - last_eval_ts) >= self._realtime_eval_position_min_interval_sec:
                    self._eval_position_last_ts[code] = now_ts
                    res: Dict[str, Any] = {"action": "HOLD"}
                    mirrored: Optional[Dict[str, Any]] = None
                    try:
                        bars_key_m, _bd_m = self._bars_key_and_frame(code)
                        _src_m = (
                            self.app.bars_data.get(bars_key_m) if bars_key_m else None
                        )
                        if _src_m is None:
                            _src_m = _bd_m
                        mirrored = self._intrabar_mirror_held_eval(code, info, _src_m)
                    except Exception:
                        mirrored = None

                    if mirrored is None:
                        try:
                            res = self.app.strategy_engine.evaluate_position(
                                code,
                                now_price,
                                ind=eval_ind,
                                is_power_exhausted=is_power_exhausted,
                                bid_ask_ratio=bid_ask_ratio,
                                ask_bid_ratio=ask_bid_ratio,
                                vol_power=vol_power,
                                sel_track=info.get("sel_track")
                                or info.get("SelTrack"),
                            )
                        except Exception:
                            try:
                                res = self.app.strategy_engine.evaluate_position(
                                    code,
                                    now_price,
                                    ind=eval_ind,
                                    is_power_exhausted=is_power_exhausted,
                                    bid_ask_ratio=bid_ask_ratio,
                                    ask_bid_ratio=ask_bid_ratio,
                                    vol_power=vol_power,
                                    sel_track=info.get("sel_track")
                                    or info.get("SelTrack"),
                                )
                            except Exception:
                                res = {"action": "HOLD"}
                    else:
                        res = (
                            mirrored
                            if isinstance(mirrored, dict)
                            else {"action": "HOLD"}
                        )

                    try:
                        _raw_np = res.get("now_price")
                        _eff_np = (
                            float(_raw_np)
                            if _raw_np is not None
                            else float(now_price)
                        )
                    except (TypeError, ValueError):
                        _eff_np = float(now_price)
                    try:
                        if not np.isfinite(_eff_np) or _eff_np <= 0:
                            _eff_np = float(now_price)
                    except Exception:
                        _eff_np = float(now_price)

                    if res.get("action") != "HOLD":
                        if defer_main_callbacks:
                            try:
                                self.app.enqueue_tick_main_action(
                                    "strategy_action",
                                    (
                                        str(code).zfill(6),
                                        str(res.get("action") or ""),
                                        str(res.get("reason") or ""),
                                        {
                                            "now_price": float(_eff_np),
                                            "profit_ratio": float(
                                                res.get("profit_ratio", 0) or 0
                                            ),
                                        },
                                    ),
                                )
                            except Exception:
                                pass
                        else:
                            self.app._on_strategy_action_needed(
                                code,
                                res.get("action"),
                                res.get("reason"),
                                now_price=_eff_np,
                                profit_ratio=res.get("profit_ratio", 0),
                            )

            # 관심목록 전용: 평단(기준가) 등록 시에만 S2-B 매도 신호 텔레그램 (미등록 시 매수만)
            if (
                bars_ready
                and is_wl_only
                and last_row is not None
                and _session_ok
                and has_wl_avg
            ):
                self._tick_watchlist_synthetic_s2_exit(
                    code,
                    now_price,
                    last_row,
                    info,
                    bid_ask_ratio=bid_ask_ratio,
                    ask_bid_ratio=ask_bid_ratio,
                    vol_power=vol_power,
                    is_power_exhausted=is_power_exhausted,
                    ref_avg=float(_wl_ap),
                )

            # 8. 트리뷰 업데이트 큐잉
            # - 기존: 관심목록 전용(wl_only)만 UI 갱신을 제외하여 부하를 줄였음
            # - 문제: monitor_codes 상한(예: max_slots=30) 때문에 남는 selected_targets가 wl_only로 분류되며
            #         가격/거래량이 UI에서 갱신되지 않음
            # - 현재: wl_only도 최소 트리 갱신(Price/Volume/기본 열)을 수행하도록 허용
            try:
                np_f = float(now_price)
            except (TypeError, ValueError):
                np_f = 0.0

            try:
                op_f = float(open_price)
            except (TypeError, ValueError):
                op_f = 0.0

            ratio_cell, dir_rn = self._monitor_tree_ratio_and_direction(
                info, np_f, intraday_diff, op_f
            )
            p_tag = (
                "price_up"
                if dir_rn > 0
                else ("price_down" if dir_rn < 0 else "price_stable")
            )

            # wl_only 인 종목은 아래 UI 큐잉/compose 분기에서 status_text를 덮어쓰지 않을 수 있어,
            # 워밍업 '✅ 15분봉·검증 완료'가 confluence 플레이스홀더로 고정되는 문제를
            # 여기서 선제적으로 보정한다.
            try:
                conf_s = str(info.get("confluence", "")).strip()
                prev_s = str(info.get("status_text", "")).strip()
                if prev_s.startswith("✅ 15분봉") and (not conf_s or conf_s in ("-", "—")):
                    info["status_text"] = "대기"
            except Exception:
                pass

            if not is_wl_only:
                strat_label = "—"
                atr_cell = "—"
                pos = None
                try:
                    # ATR 기준가 열: 가상·실제 보유 종목만 (감시만 하는 종목은 '—')
                    nk = str(code).zfill(6)
                    vp = self.app.virtual_positions
                    pos = vp.get(nk)
                    if pos is None:
                        for k, v in vp.items():
                            if str(k).split("_")[0].zfill(6) == nk:
                                pos = v
                                break
                    strat_label = format_entry_signal_type_label(
                        pos if isinstance(pos, dict) else None,
                        info.get("sel_track", info.get("SelTrack", "AB")),
                        code=nk,
                        app=self.app,
                    )
                except Exception:
                    strat_label = format_entry_signal_type_label(
                        None,
                        info.get("sel_track", info.get("SelTrack", "AB")),
                        code=str(code).zfill(6),
                        app=self.app,
                    )
                _vp_str = f"{float(self._to_scalar(info.get('vol_power', 0.0), 0.0)):.1f}%"
                if not _session_ok:
                    _vp_str = "—"
                # 신호상태 열은 monitor_tab 과 동일하게 status_text 단일 소스.
                # 합치도(confluence)가 '-'/'—'(플레이스홀더)이면 status_text가 워밍업의
                # '✅ 15분봉·검증 완료'로 남아 "고정"되는 케이스가 있어,
                # 그 문구일 때는 '대기'로 되돌려야 이후 틱에서 갱신된다.
                try:
                    conf_s = str(info.get("confluence", "")).strip()
                    prev_s = str(info.get("status_text", "")).strip()
                    if conf_s and conf_s not in ("-", "—"):
                        info["status_text"] = conf_s
                    else:
                        # confluence 플레이스홀더인데도 워밍업 완료 문구가 유지되면
                        # SignalEngine이 이후 실제 신호 계산을 못해도 화면이 고정된다.
                        if prev_s.startswith("✅ 15분봉"):
                            info["status_text"] = "대기"
                        elif not prev_s:
                            info["status_text"] = "대기"
                except Exception:
                    pass
                try:
                    if pos is not None and np_f > 0:
                        # 신호·매매용 last_row 는 bars_ready 일 때만 채워짐.
                        # 표시는 봉 수와 무관하게 마지막 봉+지표로 ATR 열을 채운다.
                        _, _bd_atr = self._bars_key_and_frame(code)
                        row_for_atr: Optional[dict] = None
                        if last_row is not None:
                            if isinstance(last_row, dict):
                                row_for_atr = last_row
                            elif hasattr(last_row, "to_dict"):
                                row_for_atr = last_row.to_dict()
                            else:
                                row_for_atr = dict(last_row)
                        else:
                            if (
                                _bd_atr is not None
                                and not getattr(_bd_atr, "empty", True)
                            ):
                                _now_atr = time.time()
                                _atr_iv = float(
                                    self._realtime_monitor_atr_display_min_interval_sec
                                )
                                _last_atr = float(
                                    self._monitor_atr_cache_ts.get(code, 0.0) or 0.0
                                )
                                _cached_atr = info.get("_monitor_atr_row_cache")
                                if (
                                    isinstance(_cached_atr, dict)
                                    and (_now_atr - _last_atr) < _atr_iv
                                ):
                                    row_for_atr = _cached_atr
                                else:
                                    try:
                                        df_i = TradingStrategy.calculate_indicators(
                                            _bd_atr, self.app.settings
                                        )
                                        row_for_atr = df_i.iloc[-1].to_dict()
                                        try:
                                            info["_monitor_atr_row_cache"] = row_for_atr
                                        except Exception:
                                            pass
                                        self._monitor_atr_cache_ts[code] = _now_atr
                                    except Exception:
                                        row_for_atr = None
                        if row_for_atr is not None:
                            atr_cell = TradingStrategy.compute_atr_exit_display_text(
                                pos,
                                self.app.settings,
                                row_for_atr,
                                float(np_f),
                                bars_df=_bd_atr,
                                sel_track_hint=info.get("sel_track")
                                or info.get("SelTrack"),
                            )
                except Exception:
                    atr_cell = "—"
                # 미보유일 때만 S1 매수 신호 → 타점 문구 (보유 시 위에서 S2 합치도 유지)
                try:
                    if is_buy and not self._code_in_virtual_positions(code):
                        info["confluence"] = "🔥 타점 포착"
                        info["status_text"] = "🔥 타점 포착"
                except Exception:
                    pass
                full_values = (
                    f"{float(self._to_scalar(info.get('score', 0.0), 0.0)):.1f}",
                    code,
                    info["name"],
                    strat_label,
                    f"{int(now_price):,}",
                    atr_cell,
                    self._format_monitor_vs_prev_cell(info),
                    ratio_cell,
                    str(monitor_signal_status_display_text(info)),
                    _vp_str,
                    self._format_monitor_bid_ask_cell(info),
                    f"{int(volume):,}",
                )
                
                final_tags = []
                
                # 합치도 기반 색상 태그
                m_ratio = float(self._to_scalar(info.get('met_ratio', 0.0), 0.0))
                if is_buy and not self._code_in_virtual_positions(code):
                    final_tags.append("status_high")
                elif m_ratio >= 0.7:
                    final_tags.append("status_high")
                else:
                    final_tags.append("status_low")

                # 가격 태그를 가장 뒤에 추가하여 최우선 적용
                final_tags.append(p_tag)
                    
                # [v27.9] is_blink 플래그를 항상 False로 전달
                self.app.pending_tree_updates[code] = (full_values, tuple(final_tags), False)

                # [진단] UI가 멈춘 듯 보일 때 pending_tree_updates가 실제로 쌓이는지 확인
                try:
                    debug_codes = {"016360", "042700", "267250", "454910"}
                    if str(code).zfill(6) in debug_codes:
                        now_t = time.time()
                        last_t = float(self._ui_debug_log_last_ts.get(code, 0.0) or 0.0)
                        if now_t - last_t >= 5.0:
                            self._ui_debug_log_last_ts[code] = now_t
                            sl = getattr(self.app, "session_logger", None)
                            if sl is not None:
                                sl.log(
                                    "signal_engine_pending_tree_updates_set",
                                    {
                                        "code": str(code).zfill(6),
                                        "now_price": float(now_price),
                                        "volume": int(volume),
                                        "is_wl_only": bool(is_wl_only),
                                        "bars_ready": bool(bars_ready),
                                        "met_ratio": float(
                                            self._to_scalar(
                                                info.get("met_ratio", 0.0), 0.0
                                            )
                                        ),
                                        "tag": list(final_tags),
                                    },
                                )
                except Exception:
                    pass

                if defer_main_callbacks:
                    try:
                        self.app.mark_tick_worker_ui_dirty()
                    except Exception:
                        pass
                else:
                    try:
                        self.app.ensure_monitor_tree_refresh_scheduled(immediate=False)
                    except Exception:
                        try:
                            if not self.app._update_timer_active:
                                self.app._update_timer_active = True
                                self.app.root.after(100, self.app._render_tree_updates)
                        except Exception:
                            pass
            else:
                # wl_only(관심목록만) 종목도 대비(vs_prev) 등이 고정되지 않도록 최소 숫자 열을 갱신한다.
                # (ATR/전략라벨 등은 wl_only에선 무의미하거나 비용이 크므로 생략)
                nk = str(code).split("_")[0].zfill(6)
                skip_enqueue = False
                try:
                    # 너무 잦은 enqueue는 Tk 부담이 커질 수 있어, 짧게 쿨다운을 둔다.
                    now_t = time.time()
                    last_t = float(
                        self._wl_only_ui_force_min_enqueue_last_ts.get(nk, 0.0) or 0.0
                    )
                    if now_t - last_t < 0.25:
                        skip_enqueue = True
                    self._wl_only_ui_force_min_enqueue_last_ts[nk] = now_t
                except Exception:
                    # 쿨다운 계산 실패해도 화면 갱신은 막지 않는다.
                    pass

                if skip_enqueue:
                    # on_tick_received 전체를 종료하지 말고, UI 큐잉만 스킵한다.
                    pass
                else:

                    try:
                        _vp_str = f"{float(self._to_scalar(info.get('vol_power', 0.0), 0.0)):.1f}%"
                    except Exception:
                        _vp_str = "—"

                    try:
                        _nm = str(info.get("name") or nk)
                    except Exception:
                        _nm = nk

                    # wl_only에서도 '대비'는 ws fid 11(diff) 기반으로 표시한다.
                    full_values = (
                        f"{float(self._to_scalar(info.get('score', 0.0), 0.0)):.1f}",
                        nk,
                        _nm,
                        "—",
                        f"{int(float(now_price)):,}",
                        "—",
                        self._format_monitor_vs_prev_cell(info),
                        ratio_cell,
                        str(
                            monitor_signal_status_display_text(info)
                        ),
                        _vp_str,
                        self._format_monitor_bid_ask_cell(info),
                        f"{int(float(volume)):,}",
                    )

                    final_tags: list[str] = []
                    try:
                        m_ratio = float(
                            self._to_scalar(info.get('met_ratio', 0.0), 0.0)
                        )
                        final_tags.append(
                            "status_high" if m_ratio >= 0.7 else "status_low"
                        )
                    except Exception:
                        final_tags.append("status_low")

                    # 가격 방향 태그를 가장 뒤에 추가(우선 적용)
                    final_tags.append(p_tag)

                    self.app.pending_tree_updates[code] = (
                        full_values,
                        tuple(final_tags),
                        False,
                    )

                    if defer_main_callbacks:
                        try:
                            self.app.mark_tick_worker_ui_dirty()
                        except Exception:
                            pass
                    else:
                        try:
                            self.app.ensure_monitor_tree_refresh_scheduled(immediate=False)
                        except Exception:
                            pass
            # 안전망: 감시 대상 종목은 조건 분기와 무관하게 최소 숫자열 enqueue를 주기적으로 강제
            # (특정 코드만 pending enqueue가 누락되어 숫자가 영구 정지되는 현상 우회)
            try:
                nk = str(code).split("_")[0].zfill(6)
                mc = getattr(getattr(self.app, "trading_controller", None), "monitor_codes", None) or []
                mc_set = {str(c).split("_")[0].zfill(6) for c in mc}
                if nk in mc_set:
                    now_t = time.time()
                    last_t = float(self._ui_force_min_enqueue_last_ts.get(nk, 0.0) or 0.0)
                    if now_t - last_t >= 0.25:
                        self._ui_force_min_enqueue_last_ts[nk] = now_t
                        try:
                            _nm = str(info.get("name") or nk)
                        except Exception:
                            _nm = nk
                        try:
                            _vp_str = f"{float(self._to_scalar(info.get('vol_power', 0.0), 0.0)):.1f}%"
                        except Exception:
                            _vp_str = "—"
                        min_values = (
                            f"{float(self._to_scalar(info.get('score', 0.0), 0.0)):.1f}",
                            nk,
                            _nm,
                            "—",
                            f"{int(float(now_price)):,}",
                            "—",
                            self._format_monitor_vs_prev_cell(info),
                            self.ratio_cell_display_for_monitor_tree(
                                info, float(now_price)
                            ),
                            str(monitor_signal_status_display_text(info)),
                            _vp_str,
                            self._format_monitor_bid_ask_cell(info),
                            f"{int(float(volume)):,}",
                        )
                        self.app.pending_tree_updates[nk] = (
                            min_values,
                            ("status_low", p_tag if "p_tag" in locals() else "price_stable"),
                            False,
                        )
                        if defer_main_callbacks:
                            try:
                                self.app.mark_tick_worker_ui_dirty()
                            except Exception:
                                pass
                        else:
                            try:
                                self.app.ensure_monitor_tree_refresh_scheduled(immediate=False)
                            except Exception:
                                pass
            except Exception:
                pass
            # 보유 종목이 있으면 포지션 관리 탭 실시간 평가·요약 갱신 (스로틀은 앱 쪽)
            if self._code_in_virtual_positions(code):
                if defer_main_callbacks:
                    try:
                        self.app.mark_tick_worker_virtual_dirty()
                    except Exception:
                        pass
                else:
                    try:
                        self.app.request_virtual_tab_realtime_render()
                    except Exception:
                        pass

        except Exception as e:
            # 동일 에러 폭주 방지
            try:
                last_err = getattr(self, "_last_realtime_err", None)
                cur_err = str(e)
            except Exception:
                last_err = None
                cur_err = str(e)

            if last_err != cur_err:
                err_msg = f"⚠️ [SignalEngine] {code} 처리 오류: {e}"
                try:
                    self.app.log(err_msg)
                except Exception:
                    pass
                self._last_realtime_err = cur_err

            # GUI가 멈춰 `app.log()`를 확인 못하는 상황을 대비해, JSONL에도 남김.
            # _last_realtime_err 와 무관하게 디버그 코드는 쿨다운(5초)으로 계속 관측합니다.
            try:
                sl = getattr(self.app, "session_logger", None)
                c = str(code).split("_")[0].zfill(6) if code is not None else ""
                if sl is not None and c in debug_codes:
                    now_t = time.time()
                    last_t = float(self._tick_exception_log_last_ts.get(c, 0.0) or 0.0)
                    if now_t - last_t >= 5.0:
                        self._tick_exception_log_last_ts[c] = now_t
                        sl.log(
                            "signal_engine_tick_exception",
                            {
                                "code": c,
                                "now_price": now_price,
                                "volume": volume,
                                "error": str(e),
                            },
                        )
            except Exception:
                pass

            # 예외가 나도 숫자 열(현재가/거래량)만큼은 최소한 갱신되도록 폴백 enqueue.
            # (복잡 지표/태그 계산은 건너뛰고 UI 멈춤 체감을 우선 완화)
            try:
                c = str(code).split("_")[0].zfill(6) if code is not None else ""
                if c:
                    now_t = time.time()
                    last_fb = float(self._ui_fallback_enqueue_last_ts.get(c, 0.0) or 0.0)
                    if now_t - last_fb >= 0.7:
                        self._ui_fallback_enqueue_last_ts[c] = now_t
                        mi = getattr(self.app, "monitor_info", None) or {}
                        info = mi.get(c)
                        if isinstance(info, dict):
                            name = str(info.get("name", c))
                            try:
                                np_i = int(float(now_price))
                            except Exception:
                                np_i = int(float(info.get("last_price", 0) or 0))
                            try:
                                vol_i = int(float(volume))
                            except Exception:
                                vol_i = int(float(info.get("volume", 0) or 0))
                            full_values = (
                                f"{float(self._to_scalar(info.get('score', 0.0), 0.0)):.1f}",
                                c,
                                name,
                                "—",
                                f"{np_i:,}",
                                "—",
                                self._format_monitor_vs_prev_cell(info),
                                self.ratio_cell_display_for_monitor_tree(
                                    info, float(now_price)
                                ),
                                str(monitor_signal_status_display_text(info)),
                                f"{float(self._to_scalar(info.get('vol_power', 0.0), 0.0)):.1f}%",
                                self._format_monitor_bid_ask_cell(info),
                                f"{vol_i:,}",
                            )
                            self.app.pending_tree_updates[c] = (
                                full_values,
                                ("status_low", "price_stable"),
                                False,
                            )
                            if defer_main_callbacks:
                                try:
                                    self.app.mark_tick_worker_ui_dirty()
                                except Exception:
                                    pass
                            else:
                                try:
                                    self.app.ensure_monitor_tree_refresh_scheduled(
                                        immediate=False
                                    )
                                except Exception:
                                    pass
            except Exception:
                pass

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

    def _tick_watchlist_synthetic_s2_exit(
        self,
        code: str,
        now_price: float,
        last_row: dict,
        info: dict,
        *,
        bid_ask_ratio: float,
        ask_bid_ratio: float,
        vol_power: float,
        is_power_exhausted: bool,
        ref_avg: Optional[float] = None,
    ) -> None:
        """
        보유 여부와 무관하게 Type B · Stage2 청산 규칙만 적용한 합성 포지션 평가.
        ref_avg 가 있으면 사용자 등록 평단을 기준으로, 없으면 당일 시가(info['open'])를 평단으로 둠.
        EXIT 시 텔레그램만 발송.
        """
        try:
            st_root = getattr(self.app, "watchlist_synth_state", None)
            if not isinstance(st_root, dict):
                return
            use_ref = ref_avg is not None and float(ref_avg) > 0
            day = datetime.now().date().isoformat()
            s = st_root.get(code)
            if not isinstance(s, dict):
                s = {}
                st_root[code] = s
            if use_ref:
                base_avg = float(ref_avg)
            else:
                open_p = float(self._to_scalar(info.get("open", 0), 0.0))
                if open_p <= 0:
                    open_p = float(now_price)
                base_avg = open_p
            if s.get("session") != day:
                s["session"] = day
                s["avg_p"] = base_avg
                s["max_p"] = max(base_avg, float(now_price))
            else:
                if use_ref:
                    s["avg_p"] = base_avg
                s["max_p"] = max(float(s.get("max_p", now_price)), float(now_price))
            avg_p = float(s.get("avg_p", base_avg))
            pos = {
                "stage": 2,
                "entry_n": 2,
                "avg_p": avg_p,
                "avg_price": avg_p,
                "max_p": float(s["max_p"]),
                "max_price": float(s["max_p"]),
                "sig_type": "TYPE_B",
                "reason": "WATCHLIST_SYNTH_S2B",
            }
            res = TradingStrategy.evaluate_position(
                code,
                float(now_price),
                pos,
                self.app.settings,
                indicator_row=last_row,
                current_time=datetime.now(),
                is_realtime=True,
                kwargs={
                    "is_power_exhausted": is_power_exhausted,
                    "bid_ask_ratio": bid_ask_ratio,
                    "ask_bid_ratio": ask_bid_ratio,
                    "vol_power": vol_power,
                },
            )
            try:
                s["max_p"] = float(pos.get("max_p", s["max_p"]))
            except Exception:
                pass
            if res.get("action") != "EXIT":
                return
            t0 = self._wl_sell_last_tm.get(code)
            if t0 is not None and (datetime.now() - t0).total_seconds() < 180:
                return
            self._wl_sell_last_tm[code] = datetime.now()
            try:
                nm = self.app.watchlist_store.get_display_name(code)
            except Exception:
                nm = str(info.get("name", code))
            pr = float(res.get("profit_ratio", 0) or 0)
            er = str(res.get("reason", ""))
            anchor = (
                f"등록 평단 {avg_p:,.0f}원 기준"
                if use_ref
                else "당일 시가 앵커"
            )
            msg = (
                "<b>⭐ [텔레그램·관심목록 전용]</b>\n"
                "<i>감시 자동매매/주문과 별도 · 합성 S2-B 평가(보유 무관)</i>\n\n"
                f"<b>TYPE B S2 매도 신호 — {er}</b>\n"
                f"종목: {nm} ({code})\n"
                f"합성 수익률: {pr:+.1f}% ({anchor})\n"
                f"시간: {datetime.now().strftime('%H:%M:%S')}"
            )
            threading.Thread(
                target=self._send_watchlist_telegram,
                args=(msg,),
                daemon=True,
            ).start()
            if use_ref:
                s["avg_p"] = float(ref_avg)
                s["max_p"] = max(float(s.get("max_p", now_price)), float(now_price))
            else:
                s["avg_p"] = float(now_price)
                s["max_p"] = float(now_price)
        except Exception:
            pass

    def _calculate_realtime_rsi(self, code: str) -> float:
        """
        [v15.0] 실시간 봉 데이터를 기반으로 즉시 RSI 산출.

        틱마다 rolling 을 돌리면 메인 스레드가 과점유되므로 최소 재계산 간격을 둔다.
        """
        try:
            now_ts = time.time()
            last_ts = float(self._rsi_calc_last_ts.get(code, 0.0) or 0.0)
            if now_ts - last_ts < self._realtime_rsi_calc_min_interval_sec:
                return float(self._rsi_calc_last_val.get(code, 0.0) or 0.0)
        except Exception:
            pass

        try:
            _, df = self._bars_key_and_frame(code)
            if df is None or getattr(df, "empty", True) or len(df) < 15:
                return 0.0

            delta = df["Close"].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()

            rs = gain / (loss + 1e-10)
            rsi = 100 - (100 / (1 + rs))
            val = rsi.iloc[-1] if not pd.isna(rsi.iloc[-1]) else 0.0
            out = float(val) if val is not None else 0.0
            try:
                self._rsi_calc_last_ts[code] = time.time()
                self._rsi_calc_last_val[code] = out
            except Exception:
                pass
            return out
        except Exception:
            try:
                return float(self._rsi_calc_last_val.get(code, 0.0) or 0.0)
            except Exception:
                return 0.0
