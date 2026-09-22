# -*- coding: utf-8 -*-
"""15:30/19:45·다음 시가 라우팅·터치 호가 프리패치 믹스인."""
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


class SignalOrderRoutingMixin:
    def _mark_1515_auction_market_order(self, code: Any, bar_ts: Any) -> bool:
        """
        15:15 슬롯이면 전 종목 동시호가 시장가(03) 플래그를 건다.

        NXT 가능 여부와 무관. True면 해당 슬롯(라이브가 게이트 생략용).
        """
        try:
            from utils.krx_bars import is_krx_auction_market_fill_slot

            if not bool(is_krx_auction_market_fill_slot(bar_ts)):
                return False
        except Exception:
            return False
        try:
            oes = self._order_execution_service()
            if oes is not None and hasattr(oes, "mark_auction_market_order"):
                oes.mark_auction_market_order(code)
        except Exception:
            pass
        return True


    def _route_1530_strategy_execution(
        self,
        code: str,
        action: str,
        *,
        reason: str = "",
        limit_price: float = 0.0,
        profit_ratio: float = 0.0,
        bar_ts: Any = None,
    ) -> str:
        """
        15:30 S2/청산 체결 경로.

        Returns
        -------
        ``queued``
            next_open 예약 완료 → 즉시 주문 금지.
            (모의 또는 실전 NXT 불가 종목)
        ``skipped``
            ``order_1530_enabled`` OFF → 주문·예약 모두 생략.
        ``nxt``
            실전 NXT 가능 종목 플래그 설정 → 호출측이 바로 주문.
        ``none``
            15:30 아님 또는 해당 없음.
        """
        try:
            act = str(action or "").upper()
            if act not in ("EXIT", "STAGE2"):
                return "none"
            ts = bar_ts
            if ts is None:
                try:
                    from utils.krx_bars import krx_stream_slot_end_datetime

                    ts = krx_stream_slot_end_datetime()
                except Exception:
                    ts = None
            if not self._is_1530_bar_slot(ts):
                return "none"
            if not self._order_1530_enabled():
                return "skipped"
            nk = str(canonical_stock_code(code) or code).zfill(6)
            if self._code_uses_nxt_touch(nk):
                oes = self._order_execution_service()
                if oes is not None and hasattr(oes, "mark_nxt_touch_order"):
                    oes.mark_nxt_touch_order(nk, src="15:30")
                return "nxt"
            # 모의 또는 실전 NXT 불가 → 익일 09:00 예약
            if self._next_open_1530_enabled() or self._use_nxt_sor_touch_orders():
                ok = self._queue_next_open_strategy_action(
                    code=nk,
                    action=act,
                    reason=str(reason or ""),
                    limit_price=float(limit_price or 0.0),
                    profit_ratio=float(profit_ratio or 0.0),
                    bar_ts=ts,
                )
                if ok:
                    try:
                        if self._use_nxt_sor_touch_orders():
                            self.app.log(
                                f"📌 [NextOpen] 실전 NXT 불가 → 익일09:00 예약 "
                                f"({nk} {act})"
                            )
                    except Exception:
                        pass
                return "queued" if ok else "none"
        except Exception:
            return "none"
        return "none"


    def _route_1945_strategy_execution(
        self,
        code: str,
        action: str,
        *,
        reason: str = "",
        limit_price: float = 0.0,
        profit_ratio: float = 0.0,
        bar_ts: Any = None,
    ) -> str:
        """
        19:45(애프터 말봉) S2/청산 → 익일 시초 지정가 예약.

        Returns
        -------
        ``queued`` / ``skipped`` / ``none``
        """
        try:
            act = str(action or "").upper()
            if act not in ("EXIT", "STAGE2"):
                return "none"
            ts = bar_ts
            if ts is None:
                try:
                    from utils.krx_bars import krx_stream_slot_end_datetime

                    ts = krx_stream_slot_end_datetime()
                except Exception:
                    ts = None
            if not self._is_1945_bar_slot(ts):
                return "none"
            if not self._next_open_1945_enabled():
                return "skipped"
            nk = str(canonical_stock_code(code) or code).zfill(6)
            ok = self._queue_next_open_strategy_action(
                code=nk,
                action=act,
                reason=str(reason or ""),
                limit_price=float(limit_price or 0.0),
                profit_ratio=float(profit_ratio or 0.0),
                bar_ts=ts,
            )
            return "queued" if ok else "none"
        except Exception:
            return "none"
        return "none"


    def _order_execution_service(self) -> Any:
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is not None:
                return oes
            tc = getattr(self.app, "trading_controller", None)
            return getattr(tc, "order_execution_service", None) if tc else None
        except Exception:
            return None


    def _queue_next_open_buy_signal(
        self,
        *,
        code: str,
        name: str,
        sig_type: str,
        limit_price: float,
        bar_ts: Any,
    ) -> bool:
        """15:30 S1 신호를 익일 시초 동시호가 대기 액션으로 저장."""
        try:
            svc = getattr(self.app, "next_open_pending_action_service", None)
            if svc is None:
                try:
                    self.app.log(
                        f"⚠️ [NextOpen] 15:30 매수 예약 실패 ({str(code).zfill(6)}) "
                        f"— NextOpen 서비스 없음"
                    )
                except Exception:
                    pass
                return False
            return bool(
                svc.queue_buy_signal(
                    code=str(code).zfill(6),
                    name=str(name or ""),
                    sig_type=str(sig_type or "TYPE_A"),
                    limit_price=float(limit_price),
                    source_slot=bar_ts,
                )
            )
        except Exception as e:
            try:
                self.app.log(
                    f"⚠️ [NextOpen] 15:30 매수 예약 예외 ({str(code).zfill(6)}): {e}"
                )
            except Exception:
                pass
            return False


    def _queue_next_open_strategy_action(
        self,
        *,
        code: str,
        action: str,
        reason: str,
        limit_price: float,
        profit_ratio: float,
        bar_ts: Any,
    ) -> bool:
        """15:30 S2/청산 신호를 익일 시초 동시호가 대기 액션으로 저장."""
        try:
            svc = getattr(self.app, "next_open_pending_action_service", None)
            if svc is None:
                try:
                    self.app.log(
                        f"⚠️ [NextOpen] 15:30 {action} 예약 실패 ({str(code).zfill(6)}) "
                        f"— NextOpen 서비스 없음"
                    )
                except Exception:
                    pass
                return False
            return bool(
                svc.queue_strategy_action(
                    code=str(code).zfill(6),
                    action=str(action or ""),
                    reason=str(reason or ""),
                    limit_price=float(limit_price),
                    profit_ratio=float(profit_ratio),
                    source_slot=bar_ts,
                )
            )
        except Exception as e:
            try:
                self.app.log(
                    f"⚠️ [NextOpen] 15:30 {action} 예약 예외 ({str(code).zfill(6)}): {e}"
                )
            except Exception:
                pass
            return False


    def _wake_tick_main_action_poller(self) -> None:
        """백그라운드 스레드에서 큐 적재 후 메인 폴러를 깨웁니다."""
        try:
            fn = getattr(self.app, "_ensure_tick_main_action_poller", None)
            if callable(fn):
                fn()
        except Exception:
            pass


    def _enqueue_closed_15m_order(self, kind: str, payload: Any) -> bool:
        """
        로컬 15분 확정 주문: Tk 메인 큐를 우회하는 패스트패스.
        실패 시 기존 틱 메인 큐로 폴백합니다.
        """
        try:
            fast = getattr(self.app, "enqueue_closed_15m_order_action", None)
            if callable(fast):
                if bool(fast(str(kind), payload)):
                    return True
        except Exception:
            pass
        try:
            enq = getattr(self.app, "enqueue_tick_main_action", None)
            if callable(enq):
                enq(str(kind), payload)
                self._wake_tick_main_action_poller()
                return True
        except Exception:
            pass
        return False


    def _prefetch_touch_quote_async(self, code: str) -> None:
        """touch 주문용 ka10004를 백그라운드 조회(레거시·선택적)."""
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is None:
                tc = getattr(self.app, "trading_controller", None)
                oes = getattr(tc, "order_execution_service", None) if tc else None
            if oes is not None and hasattr(oes, "_realtime_live_order_spec"):
                _ot, ex_style = oes._realtime_live_order_spec()
                if str(ex_style) != "touch":
                    return
        except Exception:
            pass

        nk = str(canonical_stock_code(code) or code).zfill(6)

        def _run() -> None:
            try:
                om = getattr(self.app, "order_manager", None)
                if om is not None and hasattr(om, "prefetch_best_bid_ask_rest"):
                    om.prefetch_best_bid_ask_rest(nk)
            except Exception:
                pass

        try:
            threading.Thread(
                target=_run, name=f"quote_pf_{nk}", daemon=True
            ).start()
        except Exception:
            pass


    def _prefetch_touch_quote_sync_for_order(self, code: str) -> None:
        """
        매수/청산 확정 직후 해당 종목만 ka10004를 동기 조회해 캐시에 넣습니다.

        슬롯 경계 전 종목 prefetch는 유량 큐를 막아 주문만 늦추므로,
        신호가 난 종목에만 1회 호출합니다.
        """
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is None:
                tc = getattr(self.app, "trading_controller", None)
                oes = getattr(tc, "order_execution_service", None) if tc else None
            if oes is not None and hasattr(oes, "_realtime_live_order_spec"):
                _ot, ex_style = oes._realtime_live_order_spec()
                if str(ex_style) != "touch":
                    return
        except Exception:
            pass
        nk = str(canonical_stock_code(code) or code).zfill(6)
        if len(nk) != 6:
            return
        try:
            om = getattr(self.app, "order_manager", None)
            if om is not None and hasattr(om, "prefetch_best_bid_ask_rest"):
                om.prefetch_best_bid_ask_rest(nk)
        except Exception:
            pass

