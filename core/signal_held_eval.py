# -*- coding: utf-8 -*-
"""SignalEngine 보유(S2/청산) 확정봉·인트라바 평가 믹스인."""
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


class SignalHeldEvalMixin:
    def _log_held_eval_diag(
        self,
        nk: str,
        slot_key: str,
        reason: str,
        *,
        extra: Optional[Dict[str, Any]] = None,
        to_gui: bool = True,
    ) -> None:
        """
        보유 완성15 평가 skip/HOLD 사유를 세션 JSONL(+선택 GUI)에 남깁니다.
        동일 (종목, 슬롯, 사유)는 1회만 기록해 재시도 폭주를 막습니다.
        """
        try:
            key = f"{nk}|{slot_key}|{reason}"
            seen = getattr(self, "_held_eval_diag_once", None)
            if not isinstance(seen, set):
                self._held_eval_diag_once = set()
                seen = self._held_eval_diag_once
            if key in seen:
                return
            seen.add(key)
            # 메모리 상한
            if len(seen) > 4000:
                try:
                    self._held_eval_diag_once = set(list(seen)[-2000:])
                except Exception:
                    self._held_eval_diag_once = set()
        except Exception:
            pass
        payload: Dict[str, Any] = {
            "code": nk,
            "slot": slot_key,
            "reason": str(reason),
        }
        try:
            if isinstance(extra, dict):
                for k, v in extra.items():
                    if k not in payload:
                        payload[k] = v
        except Exception:
            pass
        try:
            sl = getattr(self.app, "session_logger", None)
            if sl is not None and hasattr(sl, "log"):
                sl.log("signal_held_eval_diag", payload)
        except Exception:
            pass
        if to_gui:
            try:
                bits: list[str] = [f"ℹ️ [보유평가] {nk} @{slot_key} {reason}"]
                if isinstance(extra, dict):
                    hr = extra.get("hold_reason")
                    er = extra.get("exit_hold_reason")
                    if hr:
                        bits.append(f"S2:{hr}")
                    if er:
                        bits.append(f"EXIT:{er}")
                self.app.log(" | ".join(bits))
            except Exception:
                pass


    def eval_held_on_completed_15m(self, code: str, bar_ts: Any) -> None:
        """
        완성 15분 슬롯 확정 후 보유 종목 청산·S2 평가(저정밀 백테 정합).

        기본 운영에서는 공식 15분봉 교체 반영 후 ``eval_on_completed_15m`` 경로로 호출됩니다.
        틱/15초 주기 주문 대신, 완성 15분 종가·지표 행으로 ``evaluate_position_scoped(full)`` 1회.
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
            try:
                if not self._allow_closed_15m_eval_wall_now(bar_ts):
                    # 보유만 진단(미보유 호출 폭주 방지)
                    if self._code_in_virtual_positions(nk):
                        self._log_held_eval_diag(
                            nk, slot_key, "skip_wall_time_gate", to_gui=False
                        )
                    return
            except Exception:
                pass

            # 미보유면 done 점유하지 않음(같은 슬롯에 나중에 보유될 수 있음)
            if not self._code_in_virtual_positions(nk):
                return

            # bars ≤ bar_ts (미준비 시 done 미마킹 → 재시도)
            lk = getattr(self.app, "_bars_data_lock", None)
            bd_root = getattr(self.app, "bars_data", None)
            if not isinstance(bd_root, dict):
                self._log_held_eval_diag(nk, slot_key, "skip_no_bars_data")
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
                self._log_held_eval_diag(nk, slot_key, "skip_empty_bars")
                return
            try:
                closed_df = src_df.loc[src_df.index <= slot_ts].copy()
            except Exception:
                try:
                    closed_df = src_df[src_df.index <= slot_ts].copy()
                except Exception:
                    self._log_held_eval_diag(nk, slot_key, "skip_closed_slice_error")
                    return
            if closed_df is None or getattr(closed_df, "empty", True):
                self._log_held_eval_diag(nk, slot_key, "skip_empty_closed")
                return
            try:
                last_ix = pd.Timestamp(closed_df.index[-1])
                if last_ix != slot_ts:
                    self._log_held_eval_diag(
                        nk,
                        slot_key,
                        "skip_slot_mismatch",
                        extra={"last_bar": last_ix.strftime("%Y-%m-%d %H:%M:%S")},
                    )
                    return
            except Exception:
                pass

            if not self._mark_s1_closed_slot_done(nk, slot_key, prefix="held"):
                self._log_held_eval_diag(
                    nk, slot_key, "skip_already_done", to_gui=False
                )
                return

            settings = self._settings_for_s1_signals()
            # 보유 평가도 전 종목 prefetch 하지 않음 — 액션 확정 후 동기 1회
            try:
                calc_df = self._closed_df_for_indicator_calc(closed_df)
                df_ind = TradingStrategy.calculate_indicators(calc_df, settings)
                last_row = df_ind.iloc[-1].to_dict()
            except Exception as e:
                try:
                    self._unmark_s1_closed_slot_done(nk, slot_key, prefix="held")
                except Exception:
                    pass
                self._log_held_eval_diag(
                    nk,
                    slot_key,
                    "skip_indicator_fail",
                    extra={"error": str(e)},
                )
                try:
                    print(
                        f"[SignalEngine] eval_held_on_completed_15m 지표 실패 ({nk}): {e}"
                    )
                except Exception:
                    pass
                return
            try:
                close_px = float(self._to_scalar(last_row.get("Close"), 0.0))
            except Exception:
                close_px = 0.0
            if close_px <= 0:
                try:
                    close_px = float(closed_df["Close"].iloc[-1])
                except Exception:
                    close_px = 0.0
            if close_px <= 0:
                try:
                    self._unmark_s1_closed_slot_done(nk, slot_key, prefix="held")
                except Exception:
                    pass
                self._log_held_eval_diag(nk, slot_key, "skip_close_px_invalid")
                return

            # monitor_info 수급(없으면 중립)
            ba = ab = 1.0
            vp = 100.0
            try:
                mi = getattr(self.app, "monitor_info", None) or {}
                info = None
                for k in (nk, str(code), code):
                    if k in mi and isinstance(mi.get(k), dict):
                        info = mi[k]
                        break
                if isinstance(info, dict):
                    ba = float(self._to_scalar(info.get("bid_ask_ratio"), 1.0))
                    ab = float(self._to_scalar(info.get("ask_bid_ratio"), 1.0))
                    vp = float(self._to_scalar(info.get("vol_power"), 100.0))
                    info["last_row_cache"] = last_row
                    info["s1_closed_slot_key"] = slot_key
            except Exception:
                info = None

            se = getattr(self.app, "strategy_engine", None)
            if se is None:
                self._log_held_eval_diag(nk, slot_key, "skip_no_strategy_engine")
                return
            try:
                res = se.evaluate_position_scoped(
                    nk,
                    float(close_px),
                    scope="full",
                    ind=last_row,
                    bid_ask_ratio=ba,
                    ask_bid_ratio=ab,
                    vol_power=vp,
                    bar_confirmed=True,
                    bar_key=slot_key,
                    sel_track=(info or {}).get("sel_track")
                    or (info or {}).get("SelTrack"),
                )
            except Exception:
                # 키 변형 재시도
                try:
                    res = se.evaluate_position_scoped(
                        code,
                        float(close_px),
                        scope="full",
                        ind=last_row,
                        bid_ask_ratio=ba,
                        ask_bid_ratio=ab,
                        vol_power=vp,
                    )
                except Exception as e:
                    self._log_held_eval_diag(
                        nk,
                        slot_key,
                        "skip_eval_exception",
                        extra={"error": str(e)},
                    )
                    try:
                        print(f"[SignalEngine] eval_held_on_completed_15m 평가 실패 ({nk}): {e}")
                    except Exception:
                        pass
                    return

            if not isinstance(res, dict):
                self._log_held_eval_diag(nk, slot_key, "skip_eval_bad_result")
                return
            act = str(res.get("action") or "HOLD")
            if act == "HOLD":
                self._log_held_eval_diag(
                    nk,
                    slot_key,
                    "hold",
                    extra={
                        "hold_reason": str(res.get("hold_reason") or "unspecified"),
                        "exit_hold_reason": str(res.get("exit_hold_reason") or ""),
                        "stage": res.get("stage"),
                        "sig_type": res.get("sig_type"),
                        "profit_ratio": res.get("profit_ratio"),
                        "close": float(close_px),
                        "vol": float(self._to_scalar(last_row.get("Volume"), 0.0)),
                    },
                )
                return

            order_px = float(close_px)
            try:
                _routed = self._route_1530_strategy_execution(
                    nk,
                    act,
                    reason=str(res.get("reason") or ""),
                    limit_price=float(order_px),
                    profit_ratio=float(res.get("profit_ratio", 0) or 0),
                    bar_ts=bar_ts,
                )
                if _routed == "none":
                    _routed = self._route_1945_strategy_execution(
                        nk,
                        act,
                        reason=str(res.get("reason") or ""),
                        limit_price=float(order_px),
                        profit_ratio=float(res.get("profit_ratio", 0) or 0),
                        bar_ts=bar_ts,
                    )
                if _routed in ("queued", "skipped"):
                    _after_close_off = bool(
                        _routed == "skipped" and not self._order_after_close_enabled()
                    )
                    self._log_held_eval_diag(
                        nk,
                        slot_key,
                        (
                            "skipped_after_close"
                            if _after_close_off
                            else (
                                "skipped_1530"
                                if _routed == "skipped" and self._is_1530_bar_slot(bar_ts)
                                else (
                                    "skipped_1945"
                                    if _routed == "skipped"
                                    else (
                                        "queued_1945"
                                        if self._is_1945_bar_slot(bar_ts)
                                        else "queued_1530"
                                    )
                                )
                            )
                        ),
                        extra={"action": act, "reason": str(res.get("reason") or "")},
                    )
                    if _routed == "skipped":
                        try:
                            if _after_close_off:
                                self.app.log(
                                    f"ℹ️ [장마감후] 주문 OFF — {act} 신호만 기록 "
                                    f"({nk} slot={slot_key})"
                                )
                            else:
                                tag = "19:45" if self._is_1945_bar_slot(bar_ts) else "15:30"
                                self.app.log(
                                    f"ℹ️ [{tag}] 주문 비활성 — {act} 신호만 기록 ({nk})"
                                )
                        except Exception:
                            pass
                        try:
                            if _after_close_off and isinstance(info, dict):
                                msg = f"⏸ 장마감후 주문 OFF · {act} 신호만 기록"
                                info["confluence"] = msg
                                info["status_text"] = msg
                        except Exception:
                            pass
                    return
            except Exception:
                pass
            _is_1530 = self._is_1530_bar_slot(bar_ts)
            _use_nxt = self._code_uses_nxt_touch(nk)
            # 15:15: 전 종목 동시호가 시장가 (NXT 분기 없음)
            _auction_1515 = self._mark_1515_auction_market_order(nk, bar_ts)

            # EXIT/STAGE2: S1과 동일 — 주문 직전 라이브가 게이트, 통과 시 라이브가 주문
            _skip_live_gate = bool(_auction_1515 or (_is_1530 and _use_nxt))
            if act in ("EXIT", "STAGE2") and (not _skip_live_gate):
                try:
                    gate_ok, live_px, gate_reason = self._s1_live_confirm_gate(
                        nk,
                        info if isinstance(info, dict) else {},
                        float(close_px),
                        last_row if isinstance(last_row, dict) else {},
                        slot_key=slot_key,
                    )
                except Exception as e:
                    gate_ok, live_px, gate_reason = False, 0.0, f"gate_error:{e}"
                if not gate_ok:
                    try:
                        msg = f"라이브가 게이트({act}): {gate_reason}"
                        if isinstance(info, dict):
                            info["confluence"] = msg
                            info["status_text"] = msg
                    except Exception:
                        pass
                    try:
                        sl = getattr(self.app, "session_logger", None)
                        if sl:
                            sl.log(
                                "held_live_gate_skip",
                                {
                                    "code": nk,
                                    "slot": slot_key,
                                    "action": act,
                                    "bar_close": float(close_px),
                                    "live": float(live_px or 0.0),
                                    "reason": str(gate_reason),
                                },
                            )
                    except Exception:
                        pass
                    try:
                        self.app.log(
                            f"ℹ️ [{act}] {nk} 라이브가 게이트 스킵 — {gate_reason}"
                        )
                    except Exception:
                        pass
                    return
                if live_px > 0:
                    order_px = float(live_px)

            _act_kw: Dict[str, Any] = {
                "now_price": float(order_px),
                "profit_ratio": float(res.get("profit_ratio", 0) or 0),
            }
            try:
                sl = getattr(self.app, "session_logger", None)
                if sl:
                    sl.log(
                        "signal_held_closed_15m",
                        {
                            "code": nk,
                            "slot": slot_key,
                            "action": act,
                            "reason": str(res.get("reason") or ""),
                            "price": float(order_px),
                            "bar_close": float(close_px),
                        },
                    )
            except Exception:
                pass

            try:
                # EXIT/S2 확정 시에만 해당 종목 호가 동기 조회
                try:
                    self._prefetch_touch_quote_sync_for_order(nk)
                except Exception:
                    pass
                payload = (
                    nk,
                    act,
                    str(res.get("reason") or ""),
                    _act_kw,
                )
                if not self._enqueue_closed_15m_order("strategy_action", payload):
                    self.app._on_strategy_action_needed(
                        nk,
                        act,
                        res.get("reason"),
                        **_act_kw,
                    )
            except Exception as e:
                try:
                    print(f"[SignalEngine] eval_held_on_completed_15m 액션 실패 ({nk}): {e}")
                except Exception:
                    pass
        except Exception as e:
            try:
                print(f"[SignalEngine] eval_held_on_completed_15m 실패 ({code}): {e}")
            except Exception:
                pass


    def _mirror_held_exit_blocked_pre_confirm(
        self, nk: str, pos_live: Dict[str, Any]
    ) -> bool:
        """S1 미체결·확정 매수 이벤트 없음 — 인트라바 청산 미러 보류."""
        try:
            tc = getattr(self.app, "trading_controller", None)
            if tc is not None:
                getter = getattr(tc, "_s1_buy_order_pending_codes", None)
                pending = getter() if callable(getter) else None
                if isinstance(pending, (set, list, tuple)) and nk in pending:
                    return True
        except Exception:
            pass
        try:
            pf = getattr(self.app, "portfolio", None)
            if pf is not None:
                if pf._is_synced_placeholder_position(pos_live):
                    return True
                if not pf._has_confirmed_buy_events(pos_live):
                    # DB 복구·재시작 포지션은 events=[] 이어도 reason+진입시각 있으면 청산 허용
                    if not self._position_has_strategy_entry_reason(pos_live):
                        return True
        except Exception:
            pass
        try:
            if resolve_position_entry_ts(pos_live) is None:
                return True
        except Exception:
            return True
        return False


    def _realtime_gap_down_mirror_ok(
        self,
        nk: str,
        pos: Dict[str, Any],
        info: Dict[str, Any],
        *,
        reason: str,
    ) -> bool:
        """
        인트라바 미러 GAP_DOWN: M1 보수적 저가만으로 청산하지 않도록 실시간 틱·진입 유예를 검사.

        Returns
        -------
        True
            GAP_DOWN 청산 허용.
        False
            보류(HOLD).
        """
        try:
            if str(reason or "").strip().upper() != "GAP_DOWN":
                return True
            st = getattr(self.app, "settings", None) or {}
            if not bool(st.get("realtime_gap_down_tick_confirm", True)):
                return True

            # 진입 직후 유예
            try:
                entry_grace = float(st.get("realtime_gap_down_entry_grace_sec", 90.0))
            except (TypeError, ValueError):
                entry_grace = 90.0
            entry_grace = max(0.0, min(entry_grace, 600.0))
            if entry_grace > 0:
                et = pos.get("entry_time") or pos.get("entry_date")
                if et is not None:
                    try:
                        entry_ts = float(pd.Timestamp(et).timestamp())
                        if (time.time() - entry_ts) < entry_grace:
                            return False
                    except Exception:
                        pass

            avg_p = float(
                self._to_scalar(pos.get("avg_price", pos.get("avg_p", 0)), 0.0)
            )
            if avg_p <= 0:
                return True

            is_type_b = TradingStrategy._holding_sig_is_type_b(pos)
            if not is_type_b:
                try:
                    is_type_b = "TYPE_B" in str(pos.get("reason", "")).upper()
                except Exception:
                    is_type_b = False
            _rx = TradingStrategy._exit_track_risk_bundle(st, is_type_b)
            gap_limit = float(_rx.get("gap_down_limit", -5.0))

            live_px = 0.0
            try:
                raw_p = info.get("price")
                if raw_p not in (None, "", 0):
                    live_px = float(raw_p)
            except (TypeError, ValueError):
                live_px = 0.0
            if live_px <= 0:
                try:
                    raw_lp = info.get("last_price")
                    if raw_lp not in (None, "", 0):
                        live_px = float(raw_lp)
                except (TypeError, ValueError):
                    live_px = 0.0
            if live_px <= 0:
                # 틱 없으면 M1 저가 단독 GAP_DOWN 신뢰하지 않음
                return False

            live_pr = (live_px / avg_p - 1.0) * 100.0
            return bool(live_pr <= gap_limit)
        except Exception:
            return True


    def _intrabar_mirror_held_eval(
        self,
        code: str,
        info: Dict[str, Any],
        full_15m_src: Any,
        *,
        skip_exit: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        보유 시 청산·S2 를 ``evaluate_bt_hp_intrabar_unified``(동적 고정밀)과 동일 분기로 판정.

        Parameters
        ----------
        skip_exit
            True면 M1 루프에서 청산 ``evaluate_position`` 호출만 생략(S2·max_p 갱신은 유지).

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
            # [버그픽스] S1 매수 미체결·잔고 선반영(SYNCED)만 있는 동안 청산 미러 금지
            if self._mirror_held_exit_blocked_pre_confirm(nk, pos_live):
                return None
            pair = self._intrabar_mirror_get_full15m_and_m1(nk, full_15m_src)
            if pair is None:
                return None
            full_15m, m1df = pair
            pos_sim = copy.deepcopy(pos_live)
            st = getattr(self.app, "settings", None) or {}
            # use_flash_peak_bar_high → max_p High 시드
            # use_atr_peak_bar_high → max_p_high High 시드 (Flash 종가 / ATR 고가 분리)
            try:
                use_flash_hi = bool(st.get("use_flash_peak_bar_high", False))
                use_atr_hi = bool(st.get("use_atr_peak_bar_high", False))
                if use_flash_hi or use_atr_hi:
                    entry_ts = pos_live.get("entry_time") or ""
                    peak_hi = TradingStrategy._peak_high_from_bars_df(
                        full_15m, since=(entry_ts or None)
                    )
                    if peak_hi is not None and float(peak_hi) > 0:
                        if use_atr_hi:
                            try:
                                cur_h = float(pos_sim.get("max_p_high", 0) or 0)
                            except (TypeError, ValueError):
                                cur_h = 0.0
                            pos_sim["max_p_high"] = max(cur_h, float(peak_hi))
                        if use_flash_hi:
                            cur_mp = 0.0
                            for _mk in ("max_p", "max_price", "max_now"):
                                try:
                                    _v = float(pos_sim.get(_mk, 0) or 0)
                                    if _v > cur_mp:
                                        cur_mp = _v
                                except (TypeError, ValueError):
                                    continue
                            seeded = max(cur_mp, float(peak_hi))
                            pos_sim["max_p"] = seeded
                            pos_sim["max_price"] = seeded
            except Exception:
                pass
            ct = str(
                info.get("sel_track", info.get("SelTrack", "AB")) or "AB"
            ).strip().upper()
            if ct not in ("A", "B", "AB"):
                ct = "AB"
            out = evaluate_bt_hp_intrabar_unified(
                st, nk, full_15m, m1df, pos_sim, ct, skip_holder_exit=skip_exit
            )
            if not isinstance(out, dict):
                return {"action": "HOLD"}
            ex = out.get("exit_first")
            if isinstance(ex, dict):
                ep = float(ex.get("eval_price", 0) or 0)
                pr = float(ex.get("profit_ratio", 0) or 0)
                rs = str(ex.get("reason", "") or "EXIT")
                if not self._realtime_gap_down_mirror_ok(
                    nk, pos_live, info, reason=rs
                ):
                    return {"action": "HOLD"}
                exit_out: Dict[str, Any] = {
                    "action": "EXIT",
                    "reason": rs,
                    "profit_ratio": pr,
                    "now_price": ep,
                }
                try:
                    self._note_atr_sl_exit_for_reentry_cooldown(nk, rs)
                except Exception:
                    pass
                try:
                    _mb = ex.get("m1_bar")
                    if _mb is not None and str(_mb).strip():
                        exit_out["m1_bar"] = str(_mb)
                except Exception:
                    pass
                try:
                    _ets = resolve_position_entry_ts(pos_live)
                    if _ets is not None:
                        exit_out["entry_time"] = str(_ets)
                except Exception:
                    pass
                return exit_out
            s2 = out.get("stage2_addon")
            if isinstance(s2, dict):
                if bool(pos_live.get("s2_limit_order_pending", False)):
                    return {"action": "HOLD"}
                stg = int(pos_live.get("entry_n", pos_live.get("stage", 1)) or 1)
                try:
                    pf = getattr(self.app, "portfolio", None)
                    if pf is not None and hasattr(pf, "position_effective_stage"):
                        stg = int(pf.position_effective_stage(pos_live))
                except Exception:
                    pass
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
            # forming 분 저가는 완료 M1에 없음 — 화면(틱가) ATR SL과 청산 판정 불일치 방지
            if not skip_exit:
                try:
                    live_px = float(
                        self._to_scalar(
                            info.get("price") or info.get("last_price"),
                            0.0,
                        )
                    )
                except (TypeError, ValueError):
                    live_px = 0.0
                if live_px > 0:
                    ind_tick = out.get("indicator_last")
                    if ind_tick is None:
                        try:
                            ind_tick = self._indicator_row_to_plain_dict(
                                full_15m.iloc[-1]
                            )
                        except Exception:
                            ind_tick = None
                    try:
                        ba_m = float(
                            self._to_scalar(info.get("bid_ask_ratio"), 1.0)
                        )
                        ab_m = float(
                            self._to_scalar(info.get("ask_bid_ratio"), 1.0)
                        )
                        vp_m = float(
                            self._to_scalar(info.get("vol_power"), 100.0)
                        )
                    except (TypeError, ValueError):
                        ba_m, ab_m, vp_m = 1.0, 1.0, 100.0
                    try:
                        tick_res = TradingStrategy.evaluate_position(
                            nk,
                            live_px,
                            pos_sim,
                            st,
                            indicator_row=ind_tick,
                            current_time=datetime.now(),
                            is_realtime=True,
                            kwargs={
                                "bid_ask_ratio": ba_m,
                                "ask_bid_ratio": ab_m,
                                "vol_power": vp_m,
                                "sel_track": ct,
                                "bars_df": full_15m,
                            },
                        )
                    except Exception:
                        tick_res = None
                    if (
                        isinstance(tick_res, dict)
                        and str(tick_res.get("action") or "") == "EXIT"
                    ):
                        rs = str(tick_res.get("reason") or "EXIT")
                        if not self._realtime_gap_down_mirror_ok(
                            nk, pos_live, info, reason=rs
                        ):
                            return {"action": "HOLD"}
                        try:
                            self._note_atr_sl_exit_for_reentry_cooldown(nk, rs)
                        except Exception:
                            pass
                        return {
                            "action": "EXIT",
                            "reason": rs,
                            "profit_ratio": float(
                                tick_res.get("profit_ratio", 0) or 0
                            ),
                            "now_price": live_px,
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

