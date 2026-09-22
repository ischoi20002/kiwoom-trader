# -*- coding: utf-8 -*-
"""SignalEngine.on_tick_received — 틱 수신 메인 경로 믹스인."""
from __future__ import annotations

import copy
import threading
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

from backtest_engine import TradingStrategy
from core.bt_hp_intrabar_unified import (
    evaluate_bt_hp_intrabar_unified,
    resolve_position_entry_ts,
)
from core.bt_hp_signal_bridge import evaluate_close_now_s1_at_m1_idx
from core.defaults import effective_min_15m_bars_for_monitoring
from core.monitor_code_sets import watchlist_only_codes_set
from core.monitor_signal_status_display import monitor_signal_status_display_text
from core.signal_monitor_display import (
    clear_warmup_status_if_placeholder,
    format_s1_confluence_status_line,
    resolve_monitor_strat_label,
    sync_status_text_from_confluence,
)
from utils.bars_data_keys import resolve_bars_data_frame
from utils.krx_session import is_krx_regular_session, is_krx_tradable_session
from utils.monitor_vp_stub import clear_vp_price_stub_if_expired
from utils.normalization import canonical_stock_code


class SignalTickHandlerMixin:
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
        self._ui_tree_enqueued_codes.clear()
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
            official_sync_eval = bool(kwargs.get("_official_sync_eval", False))

            # 슬롯 경계: 워커 큐 적체 시에도 15분 확정 콜백을 앞당김
            if not official_sync_eval and not bool(kwargs.get("_orderbook_only", False)):
                try:
                    from core.realtime_official_bars_sync import try_roll_15m_slot_on_tick

                    try_roll_15m_slot_on_tick(self.app, code)
                except Exception:
                    pass
            # 지정가 주문 시세 검증용: 틱 단가로 캐시를 매 틱 동기화 (화면·주문 기준 불일치 방지)
            # 공식 M1 평가 경로는 신호만 재계산하고, 이미 살아 있는 WS 틱가를 덮어쓰지 않는다.
            try:
                live_px = self.monitor_display_price(info, 0.0)
            except Exception:
                live_px = 0.0
            if official_sync_eval and live_px > 0:
                display_price = float(live_px)
            else:
                try:
                    info["last_price"] = float(now_price)
                    info["price"] = float(now_price)
                    display_price = float(now_price)
                except (TypeError, ValueError):
                    display_price = float(now_price) if now_price else 0.0
            try:
                clear_vp_price_stub_if_expired(info)
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

            # 1. 15분 OHLCV 본체는 ``realtime_official_bars_sync``(공식 1분 집계).
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
                _session_ok = is_krx_tradable_session(now_dt)
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

            # [진단] wl_only·bars_ready (bars_ready 계산 이후에만 의미 있음)
            try:
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
                                    "bars_ready": bool(bars_ready),
                                    "now_price": float(now_price),
                                },
                            )
            except Exception:
                pass

            is_buy = False
            last_row = None
            halt_s1_msg = self._market_halt_suppresses_s1(code) if bars_ready else None
            if bars_ready and halt_s1_msg:
                info["confluence"] = halt_s1_msg
                info["met_ratio"] = 0.0
                is_buy = False
            elif bars_ready:
                now_ts = time.time()
                fp_s1 = self._s1_fingerprint_15m_tail(_bd)
                try:
                    m1_rev_s1 = (
                        self._s1_m1_revision_snapshot(str(bars_key))
                        if bars_key
                        else 0
                    )
                except Exception:
                    m1_rev_s1 = 0
                last_calc_ts = float(self._s1_full_calc_last_ts.get(code, 0.0) or 0.0)
                cached_last_row = info.get("last_row_cache")
                cached_last_sig_cache = info.get("last_sig_cache")

                # 워밍업에서 last_* 캐시를 채운 뒤 last_calc_ts 가 아직 없으면(0),
                # 첫 실시간 틱에서 전량 재계산하면 종목 수만큼 지연·큐 적체가 생김.
                if cached_last_row is None or cached_last_sig_cache is None:
                    need_full_calc = True
                elif last_calc_ts <= 0.0:
                    # 워밍업이 last_* 만 채우고 풀계산 시각은 비어 있는 경우.
                    # 타임스탬프만 기록하고 캐시를 우선 사용(아래 공통 인트라바 미러가 보정).
                    need_full_calc = False
                    try:
                        self._s1_full_calc_last_ts[code] = float(now_ts)
                    except Exception:
                        pass
                else:
                    # 표시용 풀계산: 15m 지문/M1 변경 시에도 min_interval 미만이면 캐시 유지.
                    # (주문 평가는 완성15 `submit_eval` 경로 — 틱 합치도와 분리)
                    # ``realtime_s1_full_calc_fallback_sec``: 지문이 안 바뀌어도 주기적 재계산(예외·누락 안전망).
                    try:
                        mirror_on = self._realtime_s1_mirror_dynamic_intrabar_enabled()
                    except Exception:
                        mirror_on = True
                    last_fp = self._s1_last_15m_fingerprint.get(code)
                    last_m1r = self._s1_last_m1_revision.get(code)
                    fp_changed = last_fp != fp_s1
                    try:
                        m1_changed = bool(mirror_on) and (
                            last_m1r is None or int(last_m1r) != int(m1_rev_s1)
                        )
                    except Exception:
                        m1_changed = bool(mirror_on)
                    elapsed = float(now_ts) - float(last_calc_ts)
                    stale_fb = elapsed >= float(
                        self._realtime_s1_full_calc_fallback_sec
                    )
                    min_iv = float(self._realtime_s1_full_calc_min_interval_sec)
                    too_soon = elapsed < min_iv
                    # fallback(장기 미갱신)은 min_interval보다 길므로 too_soon과 겹치지 않음
                    need_full_calc = (not too_soon) and (
                        fp_changed or m1_changed or stale_fb
                    )

                last_sig: dict = {}
                last_row = cached_last_row
                _bt_hp_res: Optional[Dict[str, Any]] = None
                _mirror_15m_src: Any = None
                _mirror_df_ind: Any = None

                if need_full_calc:
                    _src_df = self.app.bars_data.get(bars_key) if bars_key else None
                    if _src_df is None:
                        _src_df = _bd
                    _mirror_15m_src = _src_df
                    df_with_ind = TradingStrategy.calculate_indicators(
                        _src_df, self._settings_for_s1_signals()
                    )
                    df_with_ind = self._apply_ready_column_to_indicators(df_with_ind)
                    _mirror_df_ind = df_with_ind
                    try:
                        _sig_st = self._settings_for_s1_signals()
                        _sig_context = {
                            "sel_track": str(
                                info.get("sel_track", info.get("SelTrack", "AB"))
                                or "AB"
                            ).strip().upper(),
                            "box_lower": info.get(
                                "box_lower", info.get("BoxLower")
                            ),
                            "box_upper": info.get(
                                "box_upper", info.get("BoxUpper")
                            ),
                        }
                        info["last_detailed_sig"] = TradingStrategy.get_buy_signals_detailed(
                            df_with_ind,
                            _sig_st,
                            "UP",
                            signal_context=_sig_context,
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
                        last_sig = self._merge_ready_into_sig(last_sig, current_bar_count)
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
                        try:
                            self._s1_last_15m_fingerprint[code] = fp_s1
                            self._s1_last_m1_revision[code] = int(m1_rev_s1)
                        except Exception:
                            pass
                        last_sig = info["last_sig_cache"]
                        last_row = info["last_row_cache"]
                        is_buy = self._s1_buy_from_sig_dict(last_sig, current_bar_count)
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
                        last_sig = self._merge_ready_into_sig(last_sig, current_bar_count)
                        is_buy = self._s1_buy_from_sig_dict(last_sig, current_bar_count)
                        _mirror_15m_src = self.app.bars_data.get(bars_key) if bars_key else _bd
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

                # 풀계산·캐시 공통: 동적 고정밀 인트라바 미러(워밍업 직후 캐시 경로 포함)
                if bars_ready and last_sig and isinstance(last_row, dict):
                    try:
                        if _mirror_15m_src is None:
                            _mirror_15m_src = (
                                self.app.bars_data.get(bars_key) if bars_key else _bd
                            )
                        is_buy, last_sig, last_row = self._apply_intrabar_mirror_s1_after_eval(
                            code,
                            info,
                            _mirror_15m_src,
                            _mirror_df_ind,
                            is_buy=is_buy,
                            last_sig=last_sig,
                            last_row=last_row,
                        )
                        _sel_tr = str(
                            info.get("sel_track", info.get("SelTrack", "AB")) or "AB"
                        ).strip().upper()
                        info["confluence"] = self._format_s1_confluence_status_line(
                            last_sig, _sel_tr
                        )
                        info["met_ratio"] = float(
                            self._to_scalar(last_sig.get("Met_Ratio"), 0.0)
                        )
                    except Exception:
                        pass

                # forming 15m Signal 만 켜진 경우 close-now 로 최종 확정(128940류 필터)
                if (
                    is_buy
                    and bars_ready
                    and self._intrabar_close_now_enabled()
                    and _mirror_df_ind is not None
                ):
                    try:
                        _src_cn = _mirror_15m_src
                        if _src_cn is None:
                            _src_cn = (
                                self.app.bars_data.get(bars_key)
                                if bars_key
                                else _bd
                            )
                        ok_cn, last_sig = self._confirm_s1_with_close_now(
                            code,
                            info,
                            _mirror_df_ind,
                            _src_cn,
                            last_sig,
                            sel_track=_sel_tr,
                        )
                        if not ok_cn:
                            is_buy = False
                            info["confluence"] = "close-now 불일치"
                            info["met_ratio"] = 0.0
                        else:
                            info["last_sig_cache"] = last_sig
                            _sel_tr = str(
                                info.get("sel_track", info.get("SelTrack", "AB"))
                                or "AB"
                            ).strip().upper()
                            info["confluence"] = self._format_s1_confluence_status_line(
                                last_sig, _sel_tr
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

                # ATR_SL 이후 동일종목 재진입 쿨다운
                try:
                    if is_buy and self._s1_blocked_by_atr_sl_reentry(code):
                        is_buy = False
                        info["confluence"] = "⏳ ATR_SL 재진입 쿨다운"
                except Exception:
                    pass

                # 선정 트랙 ↔ 신호 타입 불일치: 주문·「타점 포착」표시 전에 차단
                # (세션 로그는 15분 확정/콜백 경로에서 남김 — 틱마다 중복 기록 방지)
                if is_buy and last_sig is not None:
                    try:
                        is_buy = self._apply_s1_track_match_gate(
                            code,
                            info,
                            last_sig,
                            is_buy=True,
                            now_price=float(now_price) if now_price else 0.0,
                            log_skip=False,
                        )
                    except Exception:
                        pass

                # 쿨타임 및 중복 진입 방지
                _cool_sec = self._s1_buy_signal_cooldown_sec()
                is_cool = True
                if code in self.last_buy_sig_time:
                    if (datetime.now() - self.last_buy_sig_time[code]).total_seconds() < _cool_sec:
                        is_cool = False
            
                if is_buy and is_cool and (not is_wl_only) and code not in self.app.virtual_positions:
                    # 로컬 15분 확정 모드: 틱/형성봉 경로에서는 주문하지 않음(확정 콜백만)
                    if self._s1_eval_on_local_15m_close_enabled():
                        pass
                    else:
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
                                try:
                                    self._prefetch_touch_quote_sync_for_order(code)
                                except Exception:
                                    pass
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
                                try:
                                    self._prefetch_touch_quote_sync_for_order(code)
                                except Exception:
                                    pass
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

            # [진단] 신규 진입 후보(일일 감시·비보유·비관심전용)의 S1 평가 상태 기록.
            # ``is_in_watchlist``(관심목록 파일)가 아니라 monitor_info 등록 종목 기준 —
            # 관심목록 0건이어도 일일 선정 23종목에 진단이 쌓여야 함.
            try:
                if (not is_wl_only) and (not self._code_in_virtual_positions(code)):
                    self._log_s1_watch_diag(
                        code,
                        bars_ready=bool(bars_ready),
                        bar_count=int(current_bar_count),
                        min_bars=int(_min_sig_bars),
                        is_buy=bool(is_buy),
                        info=info,
                        now_price=float(now_price),
                    )
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

            # 저정밀 정합: 보유 청산·S2 주문은 15분 확정 콜백에서만 (틱 경로는 합치도 UI만)
            _lp_held_orders = self._s1_eval_on_local_15m_close_enabled()

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
            # 저정밀(로컬 15분 확정) 모드면 주문성 평가는 확정 콜백으로만 — 틱 주기 EXIT/S2 생략
            bid_ask_ratio = float(self._to_scalar(info.get('bid_ask_ratio', 1.0), 1.0))
            ask_bid_ratio = float(self._to_scalar(info.get('ask_bid_ratio', 1.0), 1.0))
            vol_power = float(self._to_scalar(info.get('vol_power', 100.0), 100.0))

            if (
                eval_ind is not None
                and not is_wl_only
                and _session_ok
                and (not _lp_held_orders)
            ):
                now_ts = time.time()
                exit_interval = float(self._realtime_eval_position_min_interval_sec)
                last_exit_ts = float(self._eval_exit_last_ts.get(code, 0.0) or 0.0)
                time_fallback_due = (now_ts - last_exit_ts) >= exit_interval

                bars_key_m, _bd_m = self._bars_key_and_frame(code)
                _src_m = (
                    self.app.bars_data.get(bars_key_m) if bars_key_m else None
                )
                if _src_m is None:
                    _src_m = _bd_m

                stg_h = self._held_position_stage(code)

                on_m1_schedule = bool(self._held_eval_on_m1_revision_only())
                m1_rev_h = 0
                try:
                    m1_rev_h = int(
                        self._s1_m1_revision_snapshot(str(bars_key_m) if bars_key_m else code)
                    )
                except Exception:
                    m1_rev_h = 0
                prev_m1r = self._held_s2_last_m1_rev.get(code)
                try:
                    has_m1_store = getattr(self.app, "rt_tick_m1_store", None) is not None
                except Exception:
                    has_m1_store = False
                m1_rev_changed = False
                if has_m1_store:
                    try:
                        m1_rev_changed = (
                            prev_m1r is None or int(prev_m1r) != int(m1_rev_h)
                        )
                    except (TypeError, ValueError):
                        m1_rev_changed = False

                if on_m1_schedule:
                    # 청산·S2: M1 완성 시 평가 + M1 부재 시 시간 폴백
                    if has_m1_store:
                        held_eval_due = bool(m1_rev_changed or time_fallback_due)
                    else:
                        held_eval_due = time_fallback_due
                    need_skip_exit = False
                    exit_due = held_eval_due
                    run_s2_legacy = int(stg_h) == 1 and held_eval_due
                else:
                    exit_due = time_fallback_due
                    s2_m1_due = False
                    if int(stg_h) == 1:
                        if has_m1_store:
                            s2_m1_due = m1_rev_changed
                        else:
                            s2_m1_due = exit_due
                    held_eval_due = exit_due or s2_m1_due
                    need_skip_exit = bool(s2_m1_due and not exit_due)
                    run_s2_legacy = bool(s2_m1_due)

                if held_eval_due:
                    res: Dict[str, Any] = {"action": "HOLD"}
                    mirrored: Optional[Dict[str, Any]] = None

                    try:
                        if self._realtime_s1_mirror_dynamic_intrabar_enabled():
                            mirrored = self._intrabar_mirror_held_eval(
                                code, info, _src_m, skip_exit=need_skip_exit
                            )
                    except Exception:
                        mirrored = None

                    run_exit_legacy = bool(exit_due)

                    if mirrored is not None:
                        res = (
                            mirrored
                            if isinstance(mirrored, dict)
                            else {"action": "HOLD"}
                        )
                        # 미러 HOLD( M1 저가 미충족 등) 시 레거시 틱 청산 폴백
                        if run_exit_legacy and str(res.get("action") or "") == "HOLD":
                            try:
                                leg = self.app.strategy_engine.evaluate_position_scoped(
                                    code,
                                    now_price,
                                    scope="exit_only",
                                    ind=eval_ind,
                                    is_power_exhausted=is_power_exhausted,
                                    bid_ask_ratio=bid_ask_ratio,
                                    ask_bid_ratio=ask_bid_ratio,
                                    vol_power=vol_power,
                                    sel_track=info.get("sel_track")
                                    or info.get("SelTrack"),
                                )
                                if isinstance(leg, dict) and leg.get("action") != "HOLD":
                                    res = leg
                            except Exception:
                                pass
                        if run_exit_legacy:
                            self._eval_exit_last_ts[code] = now_ts
                    else:
                        if run_exit_legacy:
                            try:
                                res = self.app.strategy_engine.evaluate_position_scoped(
                                    code,
                                    now_price,
                                    scope="exit_only",
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
                                    res = self.app.strategy_engine.evaluate_position_scoped(
                                        code,
                                        now_price,
                                        scope="exit_only",
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
                            self._eval_exit_last_ts[code] = now_ts

                        if (res.get("action") == "HOLD") and run_s2_legacy:
                            try:
                                res2 = self.app.strategy_engine.evaluate_position_scoped(
                                    code,
                                    now_price,
                                    scope="s2_only",
                                    ind=eval_ind,
                                    is_power_exhausted=is_power_exhausted,
                                    bid_ask_ratio=bid_ask_ratio,
                                    ask_bid_ratio=ask_bid_ratio,
                                    vol_power=vol_power,
                                    sel_track=info.get("sel_track")
                                    or info.get("SelTrack"),
                                )
                                if isinstance(res2, dict) and res2.get(
                                    "action"
                                ) == "STAGE2":
                                    res = res2
                            except Exception:
                                pass

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
                        _act_kw: Dict[str, Any] = {
                            "now_price": float(_eff_np),
                            "profit_ratio": float(
                                res.get("profit_ratio", 0) or 0
                            ),
                        }
                        for _ak in ("m1_bar", "entry_time"):
                            try:
                                _av = res.get(_ak)
                                if _av is not None and str(_av).strip():
                                    _act_kw[_ak] = str(_av)
                            except Exception:
                                pass
                        # 15:30: 모의→익일예약 / 실전→NXT (완성봉 held 경로와 동일)
                        # 19:45: 익일 시초 지정가 예약
                        _skip_immediate_order = False
                        try:
                            _r1530 = self._route_1530_strategy_execution(
                                str(code).zfill(6),
                                str(res.get("action") or ""),
                                reason=str(res.get("reason") or ""),
                                limit_price=float(_eff_np),
                                profit_ratio=float(
                                    res.get("profit_ratio", 0) or 0
                                ),
                                bar_ts=None,
                            )
                            if _r1530 == "none":
                                _r1530 = self._route_1945_strategy_execution(
                                    str(code).zfill(6),
                                    str(res.get("action") or ""),
                                    reason=str(res.get("reason") or ""),
                                    limit_price=float(_eff_np),
                                    profit_ratio=float(
                                        res.get("profit_ratio", 0) or 0
                                    ),
                                    bar_ts=None,
                                )
                            if _r1530 in ("queued", "skipped"):
                                _skip_immediate_order = True
                        except Exception:
                            _skip_immediate_order = False
                        if not _skip_immediate_order:
                            if defer_main_callbacks:
                                try:
                                    self.app.enqueue_tick_main_action(
                                        "strategy_action",
                                        (
                                            str(code).zfill(6),
                                            str(res.get("action") or ""),
                                            str(res.get("reason") or ""),
                                            _act_kw,
                                        ),
                                    )
                                except Exception:
                                    pass
                            else:
                                self.app._on_strategy_action_needed(
                                    code,
                                    res.get("action"),
                                    res.get("reason"),
                                    **_act_kw,
                                )

                    if held_eval_due and has_m1_store:
                        try:
                            self._held_s2_last_m1_rev[code] = int(m1_rev_h)
                        except Exception:
                            pass

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
                np_f = float(display_price)
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
            clear_warmup_status_if_placeholder(info)

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
                    strat_label = resolve_monitor_strat_label(
                        pos=pos,
                        sel_track=info.get("sel_track", info.get("SelTrack", "AB")),
                        code=nk,
                        app=self.app,
                    )
                except Exception:
                    strat_label = resolve_monitor_strat_label(
                        pos=None,
                        sel_track=info.get("sel_track", info.get("SelTrack", "AB")),
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
                sync_status_text_from_confluence(info)
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
                                if isinstance(_cached_atr, dict) and (
                                    (_now_atr - _last_atr) < _atr_iv
                                    or (_now_atr - _last_atr) < 30.0
                                ):
                                    row_for_atr = _cached_atr
                                else:
                                    if isinstance(_cached_atr, dict):
                                        row_for_atr = _cached_atr
                                    self._schedule_monitor_atr_row_cache_bg(
                                        code, _bd_atr
                                    )
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
                    f"{int(np_f):,}",
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

                nk_ui = str(code).split("_")[0].zfill(6)
                _buy_ui = bool(is_buy and not self._code_in_virtual_positions(code))
                if self._should_enqueue_monitor_tree_ui(
                    nk_ui, force=False, is_buy=_buy_ui
                ):
                    # [v27.9] is_blink 플래그를 항상 False로 전달
                    self.app.pending_tree_updates[code] = (
                        full_values,
                        tuple(final_tags),
                        False,
                    )
                    self._ui_tree_enqueued_codes.add(nk_ui)

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
                            try:
                                if not self.app._update_timer_active:
                                    self.app._update_timer_active = True
                                    self.app.root.after(
                                        100, self.app._render_tree_updates
                                    )
                            except Exception:
                                pass

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
                elif self._should_enqueue_monitor_tree_ui(nk, force=False, is_buy=False):
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
                        f"{int(float(np_f)):,}",
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
                    self._ui_tree_enqueued_codes.add(nk)

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

