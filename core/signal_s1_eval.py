# -*- coding: utf-8 -*-
"""SignalEngine S1(미보유) 확정봉·인트라바 평가 믹스인."""
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
    format_monitor_vs_prev_cell,
    format_s1_confluence_status_line,
    resolve_monitor_strat_label,
    sync_status_text_from_confluence,
)
from utils.bars_data_keys import resolve_bars_data_frame
from utils.krx_session import is_krx_regular_session, is_krx_tradable_session
from utils.monitor_vp_stub import clear_vp_price_stub_if_expired
from utils.normalization import canonical_stock_code


class SignalS1EvalMixin:
    def _s1_live_confirm_gate(
        self,
        code: str,
        info: Optional[Dict[str, Any]],
        bar_close: float,
        last_row: Optional[Dict[str, Any]],
        *,
        slot_key: str = "",
    ) -> Tuple[bool, float, str]:
        """
        완성봉 S1·S2·EXIT 판정 후 주문 직전 라이브가 확인.

        Returns
        -------
        (ok, live_price, reason)
            ok=False 이면 주문 스킵(S1은 쿨다운 미소모). live_price는 통과 시 주문가.
        """
        st: Dict[str, Any] = {}
        try:
            st = getattr(self.app, "settings", None) or {}
            if not isinstance(st, dict):
                st = {}
            if not bool(st.get("realtime_s1_live_confirm_gate_enabled", True)):
                # 게이트 OFF: 봉종가 또는 info 가격 사용
                try:
                    px = float(
                        self._to_scalar(
                            (info or {}).get("price")
                            or (info or {}).get("last_price")
                            or bar_close,
                            0.0,
                        )
                    )
                except Exception:
                    px = float(bar_close or 0.0)
                if px <= 0:
                    px = float(bar_close or 0.0)
                return (px > 0, px, "")
        except Exception:
            st = {}

        try:
            bc = float(bar_close or 0.0)
        except Exception:
            bc = 0.0
        if bc <= 0:
            return False, 0.0, "no_bar_close"

        try:
            live = float(
                self._to_scalar(
                    (info or {}).get("price") or (info or {}).get("last_price"),
                    0.0,
                )
            )
        except Exception:
            live = 0.0
        if live <= 0:
            return False, 0.0, "no_live_price"

        try:
            ma20 = 0.0
            if isinstance(last_row, dict):
                ma20 = float(self._to_scalar(last_row.get("MA20", 0), 0.0))
        except Exception:
            ma20 = 0.0

        try:
            from strategy.s1_confirm_gate import s1_price_confirm_gate

            ok, reason = s1_price_confirm_gate(bc, live, ma20, st)
        except Exception as e:
            try:
                print(f"[SignalEngine] s1_price_confirm_gate 실패: {e}")
            except Exception:
                pass
            return False, live, "gate_error"

        return bool(ok), float(live), str(reason or "")


    def _apply_s1_track_match_gate(
        self,
        code: str,
        info: Dict[str, Any],
        last_sig: Any,
        *,
        is_buy: bool,
        now_price: float = 0.0,
        log_skip: bool = False,
    ) -> bool:
        """
        선정 트랙(sel_track)과 확정 Sig_Type 교차 검증.

        불일치면 ``is_buy`` 를 False 로 내리고 감시 문구를
        ``타점(A) · 트랙B 제외`` 형태로 갱신한다.
        ``log_skip=True`` 이면 세션에 ``signal_buy_skipped`` 를 남긴다
        (15분 확정 경로처럼 콜백에 도달하지 않고 조기 return 할 때).
        """
        if not is_buy:
            return False
        try:
            settings = getattr(self.app, "settings", None) or {}
            tr = str(
                (info or {}).get("sel_track", (info or {}).get("SelTrack", "AB"))
                or "AB"
            ).strip().upper()
            holdings = {}
            try:
                holdings = getattr(self.app.portfolio, "virtual_positions", None) or {}
            except Exception:
                holdings = {}
            row = last_sig if isinstance(last_sig, dict) else {}
            eff = TradingStrategy.effective_s1_sig_type_for_buy(
                settings, row, holdings, tr
            )
            if TradingStrategy.track_signal_match_allows(settings, eff, tr):
                if TradingStrategy.simultaneous_type_a_blocked(settings, row, eff):
                    try:
                        if isinstance(info, dict):
                            info["confluence"] = "동시신호 A 차단"
                            info["status_text"] = "동시신호 A 차단"
                    except Exception:
                        pass
                    if log_skip:
                        try:
                            sl = getattr(self.app, "session_logger", None)
                            if sl:
                                sl.log(
                                    "signal_buy_skipped",
                                    {
                                        "code": str(code).zfill(6),
                                        "name": str((info or {}).get("name", "")),
                                        "reason": "simul_type_a_blocked",
                                        "sig_type": str(eff),
                                        "sel_track": tr,
                                        "price": float(now_price) if now_price else None,
                                    },
                                )
                        except Exception:
                            pass
                    return False
                if TradingStrategy.simultaneous_equal_holdings_blocked(
                    settings, row, holdings
                ):
                    try:
                        if isinstance(info, dict):
                            info["confluence"] = "동수 동시신호 차단"
                            info["status_text"] = "동수 동시신호 차단"
                    except Exception:
                        pass
                    if log_skip:
                        try:
                            sl = getattr(self.app, "session_logger", None)
                            if sl:
                                sl.log(
                                    "signal_buy_skipped",
                                    {
                                        "code": str(code).zfill(6),
                                        "name": str((info or {}).get("name", "")),
                                        "reason": "simul_equal_holdings_blocked",
                                        "sig_type": str(eff),
                                        "sel_track": tr,
                                        "price": float(now_price) if now_price else None,
                                    },
                                )
                        except Exception:
                            pass
                    return False
                return True
            try:
                from core.signal_callbacks_service import SignalCallbacksService

                status = SignalCallbacksService.format_track_mismatch_status(tr, eff)
            except Exception:
                status = f"타점 · 트랙{tr} 제외"
            try:
                if isinstance(info, dict):
                    info["confluence"] = status
                    info["status_text"] = status
            except Exception:
                pass
            if log_skip:
                try:
                    from core.signal_callbacks_service import SignalCallbacksService

                    raw = ""
                    try:
                        raw = str(row.get("Sig_Type", "") or "")
                    except Exception:
                        raw = ""
                    SignalCallbacksService.log_track_signal_mismatch(
                        self.app,
                        code=str(code).zfill(6),
                        name=str((info or {}).get("name", "")),
                        sel_track=tr,
                        sig_type=eff,
                        raw_sig_type=raw,
                        price=float(now_price) if now_price else None,
                    )
                except Exception:
                    pass
            return False
        except Exception:
            return bool(is_buy)


    def _mark_s1_closed_slot_done(
        self, code: str, slot_key: str, *, prefix: str = "s1"
    ) -> bool:
        """
        슬롯당 1회만 True(첫 호출). 이미 처리됐으면 False.
        ``prefix``: ``s1`` / ``held`` 등 평가 종류 구분.
        """
        try:
            nk = str(canonical_stock_code(code) or code).zfill(6)
            key = f"{prefix}|{nk}|{slot_key}"
            done = getattr(self, "_s1_closed_slot_done", None)
            if not isinstance(done, dict):
                self._s1_closed_slot_done = {}
                done = self._s1_closed_slot_done
            if key in done:
                return False
            now_m = time.monotonic()
            done[key] = now_m
            # 오래된 키 정리(약 1거래일분)
            if len(done) > 4000:
                cutoff = now_m - 86400.0
                for k in list(done.keys()):
                    try:
                        if float(done.get(k, 0) or 0) < cutoff:
                            done.pop(k, None)
                    except Exception:
                        done.pop(k, None)
            return True
        except Exception:
            return True


    def eval_s1_on_completed_15m(self, code: str, bar_ts: Any) -> None:
        """
        로컬/공식 15분 슬롯 확정 후 S1 1회 평가(저정밀 백테 정합).

        - 기본 운영: 공식 ka10080 15분봉 교체 반영 후 호출됨
          (``realtime_closed_15m_eval_after_official``).
        - ``bars_data`` 를 ``bar_ts`` 이하로 잘라 완성봉 기준으로 지표·신호 계산
        - 보유·관심전용·정규장 외·쿨다운·ATR_SL 재진입은 기존과 동일하게 차단
        """
        try:
            if not self._s1_eval_on_local_15m_close_enabled():
                return
            try:
                from utils.krx_bars import is_krx_no_signal_eval_15m_slot

                if is_krx_no_signal_eval_15m_slot(bar_ts) and not (
                    self._eval_1530_slot_enabled()
                    and self._is_1530_bar_slot(bar_ts)
                ):
                    return
            except Exception:
                pass
            nk = str(canonical_stock_code(code) or code).zfill(6)
            if len(nk) != 6:
                return
            try:
                slot_ts = pd.Timestamp(bar_ts)
                slot_key = slot_ts.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                return
            # 세션·봉 미준비는 done 마킹 전 — 실패 시 재시도 가능해야 함
            try:
                if not self._allow_closed_15m_eval_wall_now(bar_ts):
                    return
            except Exception:
                pass

            try:
                halt = self._market_halt_suppresses_s1(nk)
                if halt:
                    return
            except Exception:
                pass
            if self._code_in_virtual_positions(nk):
                return
            try:
                wl_only = nk in self._watchlist_only_codes()
            except Exception:
                wl_only = False
            if wl_only:
                return

            # monitor_info 확보
            mi = getattr(self.app, "monitor_info", None)
            if not isinstance(mi, dict):
                return
            info = None
            mi_key = None
            for k in (nk, str(code), code):
                if k in mi and isinstance(mi.get(k), dict):
                    info = mi[k]
                    mi_key = k
                    break
            if info is None:
                # 감시 목록에 없으면 스킵(일일 선정 외) — done 미마킹(웜업 레이스 재시도)
                return

            # bars: bar_ts 이하만 (새 forming 행 제외)
            lk = getattr(self.app, "_bars_data_lock", None)
            bd_root = getattr(self.app, "bars_data", None)
            if not isinstance(bd_root, dict):
                return
            src_df = None
            try:
                if lk is not None:
                    with lk:
                        _k, df = resolve_bars_data_frame(bd_root, nk, migrate_alias=True)
                        if df is not None and not getattr(df, "empty", True):
                            src_df = df.copy()
                else:
                    _k, df = resolve_bars_data_frame(bd_root, nk, migrate_alias=True)
                    if df is not None and not getattr(df, "empty", True):
                        src_df = df.copy()
            except Exception:
                src_df = None
            if src_df is None or getattr(src_df, "empty", True):
                return
            try:
                closed_df = src_df.loc[src_df.index <= slot_ts].copy()
            except Exception:
                try:
                    closed_df = src_df[src_df.index <= slot_ts].copy()
                except Exception:
                    return
            if closed_df is None or getattr(closed_df, "empty", True):
                return
            try:
                if pd.Timestamp(closed_df.index[-1]) != slot_ts:
                    # 확정 시각 행이 없으면 평가하지 않음(부분 데이터) — 재시도 허용
                    return
            except Exception:
                pass

            try:
                min_bars = effective_min_15m_bars_for_monitoring(
                    getattr(self.app, "settings", None) or {}
                )
            except Exception:
                min_bars = 60
            bar_count = int(len(closed_df))
            if bar_count < int(min_bars):
                try:
                    info["confluence"] = f"봉 충전 {bar_count}/{min_bars} (15분 확정 대기)"
                except Exception:
                    pass
                return

            # 봉·최소길이 준비된 뒤에만 슬롯 점유(조기 done으로 15:30 재시도가 막히던 버그 방지)
            if not self._mark_s1_closed_slot_done(nk, slot_key):
                return

            settings = self._settings_for_s1_signals()
            # 전 종목 ka10004 prefetch 금지 — 유량 큐가 막혀 실제 주문만 지연됨.
            # 매수 확정 시에만 `_prefetch_touch_quote_sync_for_order` 호출.
            try:
                calc_df = self._closed_df_for_indicator_calc(closed_df)
                df_ind = TradingStrategy.calculate_indicators(calc_df, settings)
                df_ind = self._apply_ready_column_to_indicators(df_ind)
                _sel_tr = str(
                    info.get("sel_track", info.get("SelTrack", "AB")) or "AB"
                ).strip().upper()
                signal_context = {
                    "sel_track": _sel_tr,
                    "box_lower": info.get("box_lower", info.get("BoxLower")),
                    "box_upper": info.get("box_upper", info.get("BoxUpper")),
                }
                detailed = TradingStrategy.get_buy_signals_detailed(
                    df_ind,
                    settings,
                    "UP",
                    signal_context=signal_context,
                )
                last_ser = detailed.iloc[-1]
                last_sig = (
                    last_ser.to_dict() if hasattr(last_ser, "to_dict") else dict(last_ser)
                )
                last_sig = self._merge_ready_into_sig(last_sig, bar_count)
                is_buy = self._s1_buy_from_sig_dict(last_sig, bar_count)
                info["last_sig_cache"] = last_sig
                info["last_detailed_sig"] = detailed
                info["last_row_cache"] = df_ind.iloc[-1].to_dict()
                info["last_bar_count"] = bar_count
                info["confluence"] = self._format_s1_confluence_status_line(
                    last_sig, _sel_tr
                )
                info["met_ratio"] = float(
                    self._to_scalar(last_sig.get("Met_Ratio"), 0.0)
                )
                info["s1_closed_slot_key"] = slot_key
            except Exception as e:
                # 지표 계산 실패 시 재시도 가능해야 함
                try:
                    self._unmark_s1_closed_slot_done(nk, slot_key)
                except Exception:
                    pass
                try:
                    print(
                        f"[SignalEngine] eval_s1_on_completed_15m 지표 실패 ({nk}): {e}"
                    )
                except Exception:
                    pass
                return

            # 15:30은 신호 유무와 무관하게 평가 결과를 남겨 next_open 누락을 추적
            try:
                if self._is_1530_bar_slot(slot_ts):
                    self._session_log(
                        "signal_s1_closed_15m_eval",
                        {
                            "code": nk,
                            "name": str(info.get("name", "")),
                            "slot": slot_key,
                            "signal_on": bool(is_buy),
                            "sig_type": str(
                                last_sig.get("Sig_Type") or last_sig.get("sig_type") or ""
                            ),
                            "met_ratio": float(
                                self._to_scalar(info.get("met_ratio", 0.0), 0.0)
                            ),
                            "confluence": str(info.get("confluence", "")),
                        },
                    )
            except Exception:
                pass

            if not is_buy:
                return
            try:
                if self._s1_blocked_by_atr_sl_reentry(nk):
                    info["confluence"] = "⏳ ATR_SL 재진입 쿨다운"
                    return
            except Exception:
                pass

            _cool_sec = self._s1_buy_signal_cooldown_sec()
            if nk in self.last_buy_sig_time:
                try:
                    if (
                        datetime.now() - self.last_buy_sig_time[nk]
                    ).total_seconds() < _cool_sec:
                        return
                except Exception:
                    pass
            # code 키 변형도 쿨다운 확인
            for ck in (mi_key, code, str(code)):
                if ck and ck in self.last_buy_sig_time and ck != nk:
                    try:
                        if (
                            datetime.now() - self.last_buy_sig_time[ck]
                        ).total_seconds() < _cool_sec:
                            return
                    except Exception:
                        pass

            try:
                now_price = float(
                    self._to_scalar(
                        info.get("price") or info.get("last_price") or closed_df["Close"].iloc[-1],
                        0.0,
                    )
                )
            except Exception:
                now_price = 0.0
            if now_price <= 0:
                try:
                    now_price = float(closed_df["Close"].iloc[-1])
                except Exception:
                    return
            if now_price <= 0:
                return

            last_row = info.get("last_row_cache") or {}
            try:
                rsi_sig = float(self._to_scalar(last_row.get("RSI_Sig", last_row.get("RSI", 0)), 0.0))
            except Exception:
                rsi_sig = 0.0
            try:
                macd_val = float(self._to_scalar(last_row.get("MACD", 0), 0.0))
            except Exception:
                macd_val = 0.0
            try:
                osc_val = float(self._to_scalar(last_row.get("OSC", 0), 0.0))
            except Exception:
                osc_val = 0.0
            try:
                ma60 = float(self._to_scalar(last_row.get("MA60", 0), 0.0))
                ma60_ratio = (now_price / ma60 - 1.0) * 100.0 if ma60 > 0 else 0.0
            except Exception:
                ma60_ratio = 0.0

            try:
                sl = getattr(self.app, "session_logger", None)
                if sl:
                    sl.log(
                        "signal_s1_closed_15m",
                        {
                            "code": nk,
                            "name": str(info.get("name", "")),
                            "slot": slot_key,
                            "price": float(now_price),
                            "met_ratio": float(
                                self._to_scalar(info.get("met_ratio", 0.0), 0.0)
                            ),
                            "confluence": str(info.get("confluence", "")),
                        },
                    )
            except Exception:
                pass

            # 선정 트랙 ↔ 신호 타입 불일치: 주문·쿨다운 소모 전에 차단
            try:
                _sel_tr = str(
                    info.get("sel_track", info.get("SelTrack", "AB")) or "AB"
                ).strip().upper()
                sig_type = TradingStrategy.effective_s1_sig_type_for_buy(
                    getattr(self.app, "settings", None) or {},
                    last_sig,
                    getattr(self.app.portfolio, "virtual_positions", None) or {},
                    _sel_tr,
                )
            except Exception:
                sig_type = str(last_sig.get("Sig_Type", "TYPE_A") or "TYPE_A")
            if not self._apply_s1_track_match_gate(
                nk,
                info,
                last_sig,
                is_buy=True,
                now_price=float(now_price),
                log_skip=True,
            ):
                return

            # 15:30: 주문 OFF면 신호만 / ON이면 NXT 터치 또는 익일09:00 예약
            # 19:45: 익일 시초 지정가 예약(당일 애프터 종료 후 즉시 주문 없음)
            _is_1530 = self._is_1530_bar_slot(bar_ts)
            _is_1945 = self._is_1945_bar_slot(bar_ts)
            _use_nxt = self._code_uses_nxt_touch(nk)
            try:
                if _is_1945:
                    if self._next_open_1945_enabled():
                        ok_next = self._queue_next_open_buy_signal(
                            code=nk,
                            name=str(info.get("name", "")),
                            sig_type=str(sig_type),
                            limit_price=float(now_price),
                            bar_ts=bar_ts,
                        )
                        if ok_next:
                            return
                    try:
                        self.app.log(
                            f"ℹ️ [19:45] NextOpen 비활성/실패 — S1 당일 주문 생략 ({nk})"
                        )
                    except Exception:
                        pass
                    return
                if _is_1530:
                    if not self._order_1530_enabled():
                        try:
                            self.app.log(
                                f"ℹ️ [15:30] 주문 비활성 — S1 신호만 기록 ({nk})"
                            )
                        except Exception:
                            pass
                        return
                    if _use_nxt:
                        oes = self._order_execution_service()
                        if oes is not None and hasattr(oes, "mark_nxt_touch_order"):
                            oes.mark_nxt_touch_order(nk, src="15:30")
                    elif self._next_open_1530_enabled() or self._use_nxt_sor_touch_orders():
                        ok_next = self._queue_next_open_buy_signal(
                            code=nk,
                            name=str(info.get("name", "")),
                            sig_type=str(sig_type),
                            limit_price=float(now_price),
                            bar_ts=bar_ts,
                        )
                        if ok_next:
                            try:
                                if self._use_nxt_sor_touch_orders():
                                    self.app.log(
                                        f"📌 [NextOpen] 실전 NXT 불가 → 익일09:00 매수 예약 "
                                        f"({nk})"
                                    )
                            except Exception:
                                pass
                            return
            except Exception:
                pass

            # 15:15: 전 종목 동시호가 시장가 (NXT 분기 없음)
            _auction_1515 = self._mark_1515_auction_market_order(nk, bar_ts)

            # 완성봉 신호 유지 + 주문 직전 라이브가 게이트 (추격/이탈·MA20)
            # 15:15(동시호가)·15:30(NXT) 은 호가 경로라 게이트 생략
            try:
                bar_close = float(closed_df["Close"].iloc[-1])
            except Exception:
                bar_close = float(now_price or 0.0)
            _skip_live_gate = bool(_auction_1515 or (_is_1530 and _use_nxt))
            if not _skip_live_gate:
                gate_ok, live_px, gate_reason = self._s1_live_confirm_gate(
                    nk,
                    info,
                    bar_close,
                    last_row if isinstance(last_row, dict) else {},
                    slot_key=slot_key,
                )
                if not gate_ok:
                    try:
                        msg = f"라이브가 게이트: {gate_reason}"
                        info["confluence"] = msg
                        info["status_text"] = msg
                    except Exception:
                        pass
                    try:
                        sl = getattr(self.app, "session_logger", None)
                        if sl:
                            sl.log(
                                "s1_live_gate_skip",
                                {
                                    "code": nk,
                                    "name": str(info.get("name", "")),
                                    "slot": slot_key,
                                    "bar_close": float(bar_close),
                                    "live": float(live_px or 0.0),
                                    "reason": str(gate_reason),
                                },
                            )
                    except Exception:
                        pass
                    try:
                        self.app.log(
                            f"ℹ️ [S1] {nk} 라이브가 게이트 스킵 — {gate_reason}"
                        )
                    except Exception:
                        pass
                    return
                if live_px > 0:
                    now_price = float(live_px)
                    try:
                        ma60 = float(
                            self._to_scalar(last_row.get("MA60", 0), 0.0)
                        )
                        ma60_ratio = (
                            (now_price / ma60 - 1.0) * 100.0 if ma60 > 0 else 0.0
                        )
                    except Exception:
                        pass

            # 주문 패스트패스(Tk 메인 큐 우회) — 틱 경로와 분리
            try:
                # 매수 확정 종목만 호가 1회 동기 조회 → 주문 경로 prefer_cache 히트
                try:
                    self._prefetch_touch_quote_sync_for_order(nk)
                except Exception:
                    pass
                self.last_buy_sig_time[nk] = datetime.now()
                if mi_key and mi_key != nk:
                    self.last_buy_sig_time[mi_key] = self.last_buy_sig_time[nk]
                payload = (
                    nk,
                    float(now_price),
                    float(rsi_sig),
                    float(macd_val),
                    float(osc_val),
                    float(ma60_ratio),
                )
                if not self._enqueue_closed_15m_order("buy_signal", payload):
                    self.app._on_buy_signal_received(
                        nk,
                        now_price,
                        rsi_sig,
                        macd_val,
                        osc_val,
                        ma60_ratio,
                    )
            except Exception as e:
                try:
                    print(f"[SignalEngine] eval_s1_on_completed_15m 주문 실패 ({nk}): {e}")
                except Exception:
                    pass
        except Exception as e:
            try:
                print(f"[SignalEngine] eval_s1_on_completed_15m 실패 ({code}): {e}")
            except Exception:
                pass


    def _confirm_s1_with_close_now(
        self,
        code: str,
        info: Dict[str, Any],
        df_with_ind: Any,
        full_15m_src: Any,
        last_sig: dict[str, Any],
        *,
        sel_track: str,
    ) -> Tuple[bool, dict[str, Any]]:
        """
        forming 15m ``Signal`` 과 별도로, 같은 M1 close-now 재판정으로 S1 매수 확정.

        Returns
        -------
        (confirmed, updated_last_sig)
        """
        try:
            if not self._intrabar_close_now_enabled():
                return True, last_sig
            if df_with_ind is None or getattr(df_with_ind, "empty", True):
                return True, last_sig
            nk = str(code).strip().zfill(6)
            pair = self._intrabar_mirror_get_full15m_and_m1(nk, full_15m_src)
            if pair is None:
                return True, last_sig
            full_15m, m1df = pair
            if m1df is None or getattr(m1df, "empty", True):
                return True, last_sig
            m1_idx = len(m1df) - 1
            st = getattr(self.app, "settings", None) or {}
            ct = str(sel_track or "AB").strip().upper()
            cn = evaluate_close_now_s1_at_m1_idx(
                st,
                df_with_ind,
                full_15m,
                m1df,
                m1_idx,
                ct,
                holdings={},
            )
            if bool(cn.get("can_execute")):
                upd = dict(last_sig)
                row_cn = cn.get("indicator_row")
                if isinstance(row_cn, dict):
                    upd.update(row_cn)
                upd["Signal"] = True
                upd["Ready"] = True
                upd["Sig_Type"] = str(cn.get("sig_type") or upd.get("Sig_Type", "TYPE_B"))
                return True, upd

            # close-now 에서 탈락 — forming-only 신호(128940류) 차단
            try:
                sl = getattr(self.app, "session_logger", None)
                if sl is not None and hasattr(sl, "log"):
                    sl.log(
                        "intrabar_close_now_blocked",
                        {
                            "code": nk,
                            "name": str(info.get("name", "")),
                            "forming_sig_type": str(
                                last_sig.get("Sig_Type", "")
                            ),
                            "close_now_sig_type": str(cn.get("sig_type") or ""),
                            "close_now_reason": str(cn.get("reason") or ""),
                        },
                    )
            except Exception:
                pass
            return False, last_sig
        except Exception:
            return True, last_sig


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
                st,
                nk,
                full_15m,
                m1df,
                None,
                ct,
                df_with_ind=df_with_ind,
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


    def _apply_intrabar_mirror_s1_after_eval(
        self,
        code: str,
        info: Dict[str, Any],
        full_15m_src: Any,
        df_with_ind: Any,
        *,
        is_buy: bool,
        last_sig: dict[str, Any],
        last_row: Optional[dict[str, Any]],
    ) -> Tuple[bool, dict[str, Any], Optional[dict[str, Any]]]:
        """
        풀계산·캐시 경로 공통: 동적 고정밀 인트라바 미러로 S1 매수 여부를 최종 보정.
        """
        try:
            if not self._realtime_s1_mirror_dynamic_intrabar_enabled():
                return is_buy, last_sig, last_row
            if self._code_in_virtual_positions(code):
                return is_buy, last_sig, last_row
            pack = self._intrabar_mirror_s1_buy_pack(
                code, full_15m_src, df_with_ind, info
            )
            if isinstance(pack, dict) and pack.get("force_buy"):
                is_buy = True
                if isinstance(pack.get("last_sig"), dict):
                    last_sig = pack["last_sig"]
                    info["last_sig_cache"] = last_sig
                if isinstance(pack.get("last_row"), dict):
                    last_row = pack["last_row"]
                    info["last_row_cache"] = last_row
            return is_buy, last_sig, last_row
        except Exception as e:
            try:
                if self._last_realtime_err != str(e):
                    self._last_realtime_err = str(e)
                    print(
                        f"[SignalEngine] 인트라바 S1 미러 예외 ({code}): {e}"
                    )
            except Exception:
                pass
            return is_buy, last_sig, last_row


    def _s1_fingerprint_15m_tail(self, bars_df: Any) -> Tuple[Any, ...]:
        """
        15분봉 ``bars_data`` 프레임 꼬리 지문.
        공식 싱크로 마지막 행이 바뀔 때만 변하도록 인덱스·길이·OHLCV 를 묶는다.
        """
        try:
            if bars_df is None or getattr(bars_df, "empty", True):
                return ("", 0)
            n = int(len(bars_df))
            last_idx = bars_df.index[-1]
            idx_s = str(pd.Timestamp(last_idx))
            last = bars_df.iloc[-1]

            def _cell(col: str, default: float = 0.0) -> float:
                try:
                    return float(last[col])
                except Exception:
                    return float(default)

            return (
                idx_s,
                n,
                _cell("Open"),
                _cell("High"),
                _cell("Low"),
                _cell("Close"),
                _cell("Volume"),
            )
        except Exception:
            return ("err", -1)


    def _log_s1_watch_diag(
        self,
        code: str,
        *,
        bars_ready: bool,
        bar_count: int,
        min_bars: int,
        is_buy: bool,
        info: Dict[str, Any],
        now_price: float,
    ) -> None:
        """
        [진단] 감시(신규 진입 후보) 종목의 S1 평가 상태를 세션 로그에 주기 기록.

        백테스트에는 잡히는데 실시간은 매수하지 않는 종목의 원인
        (봉 준비 미충족 / 신호 자체 미발생 / 신호는 떴으나 게이트 차단)을 사후 특정하기 위함.
        신호 판단 로직은 건드리지 않고 관측만 한다.

        - 대상: 일일 감시·자동매매 후보(``monitor_info`` 등록, 관심목록 전용·보유 제외).
        - 스로틀: 15분 슬롯 변경 시 1회 + 종목별 주기(``realtime_s1_watch_diag_interval_sec``, 기본 300초).
        - 신호(Signal)=True 인데 미매수인 경우는 짧은 주기(``realtime_s1_watch_diag_signal_no_buy_min_sec``, 기본 30초)로 기록.
        - JSONL 기록은 데몬 스레드로 넘겨 틱·UI 스레드 I/O 블로킹을 방지한다.
        """
        try:
            sl = getattr(self.app, "session_logger", None)
            if sl is None:
                return
            try:
                interval = float(
                    self.app.settings.get("realtime_s1_watch_diag_interval_sec", 300.0)
                )
            except (TypeError, ValueError):
                interval = 300.0
            if interval <= 0:
                return

            last_sig = info.get("last_sig_cache") if isinstance(info, dict) else None
            sig_on = False
            sig_type = ""
            try:
                if isinstance(last_sig, dict):
                    sig_on = bool(self._to_scalar(last_sig.get("Signal", False), False))
                    sig_type = str(last_sig.get("Sig_Type", "") or "")
            except Exception:
                sig_on = False

            # 15분 슬롯 변경 시에도 1회 기록(백테스트 슬롯 대비 진단용)
            slot_key = ""
            try:
                _bk, _bdf = self._bars_key_and_frame(code)
                slot_key = self._current_slot_key_from_bars(_bdf)
            except Exception:
                slot_key = ""
            slot_store = getattr(self, "_s1_watch_diag_last_slot", None)
            if slot_store is None:
                slot_store = {}
                self._s1_watch_diag_last_slot = slot_store
            last_slot = str(slot_store.get(code, "") or "")
            slot_changed = bool(slot_key) and slot_key != last_slot

            store = getattr(self, "_s1_watch_diag_last_ts", None)
            if store is None:
                store = {}
                self._s1_watch_diag_last_ts = store
            now_t = time.time()
            last_t = float(store.get(code, 0.0) or 0.0)
            signal_but_no_buy = sig_on and not is_buy
            due = (now_t - last_t) >= interval
            # 신호=True·미매수도 틱마다 기록하면 JSONL·데몬 스레드가 폭주해 UI가 멈출 수 있음
            if signal_but_no_buy:
                try:
                    nb_sec = float(
                        self.app.settings.get(
                            "realtime_s1_watch_diag_signal_no_buy_min_sec", 30.0
                        )
                    )
                except (TypeError, ValueError):
                    nb_sec = 30.0
                nb_sec = max(5.0, min(nb_sec, interval))
                if (now_t - last_t) < nb_sec and not slot_changed:
                    return
            if not (signal_but_no_buy or slot_changed or due):
                return

            # 전역 버스트 제한 — 동시 다종목·signal_but_no_buy 틱 폭주 시 JSONL 락 경합 완화
            global_last = float(
                getattr(self, "_s1_watch_diag_global_last_ts", 0.0) or 0.0
            )
            burst_sec = 0.75 if signal_but_no_buy else (2.5 if slot_changed else 3.0)
            if (now_t - global_last) < burst_sec:
                return

            store[code] = now_t
            if slot_changed:
                slot_store[code] = slot_key
            self._s1_watch_diag_global_last_ts = now_t

            payload = {
                "code": str(code).zfill(6),
                "name": str(info.get("name", "")) if isinstance(info, dict) else "",
                "bars_ready": bool(bars_ready),
                "bar_count": int(bar_count),
                "min_bars": int(min_bars),
                "signal_on": bool(sig_on),
                "sig_type": sig_type,
                "is_buy": bool(is_buy),
                "signal_but_no_buy": bool(signal_but_no_buy),
                "slot_key": slot_key,
                "slot_changed": bool(slot_changed),
                "met_ratio": float(
                    self._to_scalar(info.get("met_ratio", 0.0), 0.0)
                )
                if isinstance(info, dict)
                else 0.0,
                "confluence": str(info.get("confluence", ""))
                if isinstance(info, dict)
                else "",
                "now_price": float(now_price),
            }

            def _write() -> None:
                try:
                    sl.log("s1_watch_eval_diag", payload)
                except Exception:
                    pass

            threading.Thread(target=_write, daemon=True).start()
        except Exception:
            # 진단 로깅 실패는 매매/실시간 흐름을 절대 막지 않는다.
            pass

