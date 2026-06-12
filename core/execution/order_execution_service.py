# -*- coding: utf-8 -*-
"""
주문 집행(S1/S2/EXIT) 및 지정가 미체결 감시를 담당하는 독립 서비스.

TradingController·KiwoomApp 에서 분리 — 이후 전면 개편 시 이 모듈을 중심으로 교체.
"""
import threading
import time
import config
from datetime import datetime
from typing import Any, Dict, Optional, Set, Tuple

from core.execution.limit_order_watch_service import LimitOrderWatchService
from utils.normalization import canonical_stock_code


class OrderExecutionService:
    """실시간 매수·매도 주문 제출·미체결 감시 오케스트레이션."""

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print
        self.limit_order_watch_service = LimitOrderWatchService(
            self.app, log_func=self.log
        )
        self._limit_order_meta = self.limit_order_watch_service.limit_order_meta
        self._buy_skip_d2_one_lot_log_at: Dict[str, float] = {}
        self._exit_retry_not_before: Dict[str, float] = {}
        self._exit_retry_skip_log_at: Dict[str, float] = {}
        self._exit_stale_pending_probe_at: Dict[str, float] = {}

    @property
    def s1_buy_order_pending_codes(self) -> Set[str]:
        return self.limit_order_watch_service.s1_buy_pending_codes

    def _notify_s1_buy_submitted(self, code: object, reason: object) -> None:
        """S1 매수 접수 후 감시 메타(sel_track) 보정 — TradingController에 위임."""
        try:
            tc = getattr(self.app, "trading_controller", None)
            if tc is not None and hasattr(tc, "_narrow_monitor_sel_track_after_s1_buy"):
                tc._narrow_monitor_sel_track_after_s1_buy(code, reason)
        except Exception:
            pass

    def _supply_log_fields(self, code: object) -> Dict[str, Any]:
        """일일 JSONL(order/fill)용: 감시 화면과 동일한 체결강도·호가 스냅샷."""
        try:
            from core.supply_display import (
                capture_supply_snapshot,
                format_vol_power_column,
            )

            nc = str(code).split("_")[0].strip().zfill(6)
            s = capture_supply_snapshot(self.app, nc)
            return {
                "snap_vol_power": s.get("vol_power"),
                "snap_vol_power_str": format_vol_power_column(s.get("vol_power")),
                "snap_bid_ask": s.get("bid_ask_text"),
            }
        except Exception:
            return {
                "snap_vol_power": None,
                "snap_vol_power_str": "—",
                "snap_bid_ask": "— / — (—)",
            }

    def _realtime_live_order_spec(self) -> Tuple[str, str]:
        """
        실시간 REST 주문 (주문유형, 실행 스타일).

        터치호가(매수=매도1·매도=매수1 + 정정 루프)만 사용합니다.
        체결 확인은 ``LimitOrderWatchService`` 단일 경로(kt00007)에서 처리합니다.
        """
        return ("00", "touch")

    def _realtime_order_type(self) -> str:
        """하위 호환: 주문 유형 문자열만 필요할 때."""
        try:
            return str(self._realtime_live_order_spec()[0])
        except Exception:
            return "00"

    @staticmethod
    def _exit_order_label(
        ot: str, ex_style: str, exit_ord_price: int = 0
    ) -> str:
        """EXIT 로그·텔레그램용 주문 유형 문자열."""
        try:
            if ex_style == "touch":
                return f"터치지정가(매수1호가) {int(exit_ord_price):,}원"
            if str(ot).strip() == "03":
                return "시장가"
            return f"지정가 {int(exit_ord_price):,}원"
        except Exception:
            return "지정가"

    def _live_submit_reference_price(self, code: Any, signal_hint: float) -> float:
        """
        지정가 주문 제출 시점에 쓸 참고 현재가.

        - 메인 스레드: 감시 트리 표시가 > ``monitor_info`` 틱 캐시 > 신호 시점 가격.
        - 그 외(주문 워커 스레드 등): Tk 미사용 — ``monitor_info`` 틱 캐시 > 신호 시점 가격.
        """
        try:
            fb = float(signal_hint)
        except (TypeError, ValueError):
            fb = 0.0
        try:
            nk = canonical_stock_code(str(code).split("_")[0])
        except Exception:
            try:
                nk = str(code).split("_")[0].zfill(6)
            except Exception:
                nk = ""
        if not nk or nk == "000000":
            return fb if fb > 0 else 0.0

        on_main = threading.current_thread() is threading.main_thread()
        if on_main:
            try:
                mt = getattr(self.app, "monitor_tab", None)
                if mt is not None and hasattr(mt, "get_tree_display_price"):
                    tp = mt.get_tree_display_price(nk)
                    if tp is not None:
                        v = float(tp)
                        if v > 0:
                            return v
            except Exception:
                pass

        try:
            mi = getattr(self.app, "monitor_info", None) or {}
            mi_key: Optional[str] = nk if nk in mi else None
            if mi_key is None:
                for k in mi:
                    try:
                        if canonical_stock_code(k) == nk:
                            mi_key = str(k)
                            break
                    except Exception:
                        continue
            if mi_key is not None:
                ent = mi.get(mi_key) or {}
                if isinstance(ent, dict):
                    for fld in ("last_price", "price", "cur_prc"):
                        try:
                            v = float(ent.get(fld) or 0)
                            if v > 0:
                                return v
                        except (TypeError, ValueError):
                            continue
        except Exception:
            pass

        return fb if fb > 0 else 0.0

    def _maybe_clear_stale_exit_pending(
        self, code: str, pos: Dict[str, Any]
    ) -> bool:
        """
        exit_pending 이지만 증권사 미체결이 없는(또는 취소 가능한) 고착 상태를 해제합니다.
        해제되면 True — 호출부가 매도 주문을 이어갈 수 있습니다.
        """
        nk = str(code).zfill(6)
        try:
            if not bool(pos.get("exit_pending", False)):
                return True
        except Exception:
            return True

        now_epoch = time.time()
        try:
            last_probe = float(
                self._exit_stale_pending_probe_at.get(nk, 0.0) or 0.0
            )
        except Exception:
            last_probe = 0.0
        if now_epoch - last_probe < 3.0:
            return False
        self._exit_stale_pending_probe_at[nk] = now_epoch

        oid = str(pos.get("pending_exit_ord_no") or "").strip()
        om = getattr(self.app, "order_manager", None)
        if om is None:
            try:
                pos["exit_pending"] = False
                pos.pop("pending_exit_ord_no", None)
            except Exception:
                pass
            return True

        try:
            if oid:
                st = om.cancel_order(oid, nk)
                if st in ("cancelled", "no_open_qty", "stale_ord_no"):
                    try:
                        pos["exit_pending"] = False
                        pos.pop("pending_exit_ord_no", None)
                        self.app.portfolio._discard_pending_exit_snapshot(oid)
                    except Exception:
                        pass
                    try:
                        self.log(
                            f"ℹ️ [Exit] {pos.get('name', nk)}({nk}) "
                            f"매도대기 해제(원주문 {oid}, {st})"
                        )
                    except Exception:
                        pass
                    return True
            else:
                om.cancel_stale_open_sell_orders_for_code(
                    nk, log_fn=self.log
                )
                try:
                    om.purge_local_sell_lock_refs_for_code(
                        nk, log_fn=self.log
                    )
                except Exception:
                    pass
                try:
                    pos["exit_pending"] = False
                    pos.pop("pending_exit_ord_no", None)
                except Exception:
                    pass
                return True
        except Exception as ex:
            try:
                self.log(f"⚠️ [Exit] 매도대기 해제 시도 실패({nk}): {ex}")
            except Exception:
                pass
        return False

    def _resolve_exit_sell_qty(self, code: str, pos: Dict[str, Any]) -> int:
        """매도 주문 수량 — 로컬 보유에서 접수 매도 잠금분을 제외."""
        nk = str(code).zfill(6)
        try:
            local_q = int(pos.get("qty", 0) or 0)
        except (TypeError, ValueError):
            local_q = 0
        if local_q <= 0:
            return 0
        try:
            om = getattr(self.app, "order_manager", None)
            locked = (
                int(om.sum_open_sell_lock_qty_for_code(nk))
                if om is not None
                else 0
            )
        except Exception:
            locked = 0
        sell_q = max(0, local_q - locked)
        if locked > 0 and sell_q < local_q:
            try:
                self.log(
                    f"ℹ️ [Exit] {pos.get('name', nk)}({nk}) "
                    f"매도수량 조정 {local_q}→{sell_q}주 "
                    f"(매도 잠금 추정 {locked}주)"
                )
            except Exception:
                pass
        if sell_q <= 0 and local_q > 0:
            try:
                broker_lock = int(
                    om.sum_broker_unfilled_sell_qty_for_code(nk)
                    if om is not None
                    else 0
                )
            except Exception:
                broker_lock = 0
            if broker_lock > 0:
                try:
                    self.log(
                        f"ℹ️ [Exit] {pos.get('name', nk)}({nk}) "
                        f"증권사 미체결 매도 {broker_lock}주 — 잠금 해제 후 재시도"
                    )
                except Exception:
                    pass
        return sell_q

    def execute_buy_signal(self, code, name, price, reason="TYPE_A") -> bool:
        """[v18.0/v4.5.4] 1단계 진입 시그널에 따른 매매 집행 (실전/가상 통합).
        Returns True iff 주문 접수(ord_no) 또는 가상 체결까지 성공 — SignalEngine 쿨타임 갱신용.
        """
        try:
            sig_hint = float(price)
        except (TypeError, ValueError):
            sig_hint = 0.0
        order_ref = self._live_submit_reference_price(code, sig_hint)
        if order_ref <= 0:
            order_ref = sig_hint

        ot, ex_style = self._realtime_live_order_spec()
        adj_price = 0
        try:
            if ex_style == "touch":
                _nc = str(code).zfill(6)
                _bid, _ask = self.app.order_manager.fetch_best_bid_ask_rest(_nc)
                if _ask is None or float(_ask) <= 0:
                    self.log(
                        f"⚠️ [Skip] {name}({code}) S1 매수 생략: ka10004 매도1호가 조회 실패"
                    )
                    try:
                        sl = getattr(self.app, "session_logger", None)
                        if sl:
                            sl.log(
                                "buy_skipped",
                                {
                                    "code": code,
                                    "name": name,
                                    "reason": "touch_ask_unavailable",
                                },
                            )
                    except Exception:
                        pass
                    return False
                adj_price = int(round(float(_ask)))
            else:
                adj_price = int(
                    self.app.order_manager.get_adjusted_price(
                        float(order_ref), "buy"
                    )
                )
        except Exception:
            try:
                adj_price = int(float(order_ref))
            except Exception:
                adj_price = 0

        try:
            sl = getattr(self.app, "session_logger", None)
            if sl:
                sl.log(
                    "buy_execute_start",
                    {
                        "code": code,
                        "name": name,
                        "price": float(adj_price),
                        "ref_price": float(order_ref),
                        "signal_hint_price": float(sig_hint),
                        "reason": reason,
                        "order_exec_style": ex_style,
                    },
                )
        except Exception:
            pass

        if code in self.app.portfolio.virtual_positions:
            try:
                sl = getattr(self.app, "session_logger", None)
                if sl:
                    sl.log(
                        "buy_skipped",
                        {"code": code, "reason": "already_in_position"},
                    )
            except Exception:
                pass
            return False

        try:
            nc = str(code).zfill(6)
            if nc in self.s1_buy_order_pending_codes:
                self.log(
                    f"⚠️ [Skip] {name}({code}) S1 매수 미체결 대기 중 — 중복 주문 방지"
                )
                return False
        except Exception:
            pass

        # 1. 투자금액 산출 (Risk Management)
        use_mock = getattr(config, 'USE_MOCK', False)
        allow_order = getattr(config, 'ALLOW_MOCK_ORDER', False) if use_mock else getattr(config, 'ALLOW_REAL_ORDER', False)
        
        invest_amt = self.app.portfolio.calculate_order_quantity(stage=1, is_simulation=not allow_order)
        if invest_amt <= 0:
            self.log(f"⚠️ [Skip] {name}({code}) 진입 건너뜀 (투자금액 0 또는 자산 정보 부재)")
            try:
                sl = getattr(self.app, "session_logger", None)
                if sl:
                    sl.log(
                        "buy_skipped",
                        {"code": code, "name": name, "reason": "zero_invest_amt"},
                    )
            except Exception:
                pass
            return False
            
        # 2. 주문 수량 및 가격 보정
        # invest_amt는 D+2/(1+매수수수료율) 이하의 매수 원금 상한이다. (portfolio._max_buy_principal_cash_d2_limit)
        # adj_price(호가+1틱) 기준으로 산출한 주수가 0이면, 1주도 살 돈이 없는 것이므로
        # max(1, ...)로 1주를 강제하면 D+2 예수금이 마이너스(미수)로 간다 — 절대 금지.
        try:
            qty = int(invest_amt // int(adj_price))
        except (TypeError, ValueError, ZeroDivisionError):
            qty = 0
        if qty <= 0:
            try:
                d2v = float(getattr(self.app.portfolio, "d2_deposit", 0) or 0)
            except (TypeError, ValueError):
                d2v = 0.0
            try:
                try:
                    nc_key = str(code).zfill(6)
                except Exception:
                    nc_key = "000000"
                lk = f"{nc_key}:d2_one_lot"
                _now = time.time()
                _last = float(self._buy_skip_d2_one_lot_log_at.get(lk, 0.0) or 0.0)
                if _now - _last >= 180.0:
                    self._buy_skip_d2_one_lot_log_at[lk] = _now
                    self.log(
                        f"⚠️ [Skip] {name}({code}) S1 매수 생략: D+2 현금 한도로 "
                        f"지정가 기준 1주({adj_price:,}원)도 살 수 없음 "
                        f"(투자원금 상한 {invest_amt:,}원, D+2≈{d2v:,.0f}원). "
                        f"고가주·잔고 부족 시 발생합니다."
                    )
            except Exception as _log_e:
                try:
                    self.log(
                        f"⚠️ [Skip] {name}({code}) S1 매수 생략(D+2 한도, 1주 불가) — 로그 처리 오류: {_log_e}"
                    )
                except Exception:
                    pass
            try:
                sl = getattr(self.app, "session_logger", None)
                if sl:
                    sl.log(
                        "buy_skipped",
                        {
                            "code": code,
                            "name": name,
                            "reason": "d2_insufficient_for_one_lot",
                            "invest_amt": int(invest_amt),
                            "adj_price": int(adj_price),
                            "d2_deposit": d2v,
                        },
                    )
            except Exception:
                pass
            return False

        try:
            if ex_style == "touch":
                _ot_label = "터치지정가(매도1호가)"
            elif ot == "03":
                _ot_label = "시장가"
            else:
                _ot_label = "지정가"
        except Exception:
            _ot_label = "주문"
        self.log(
            f"🚀 [Buy Signal] {name}({code}) {reason} · "
            f"{_ot_label} {adj_price:,}원 × {qty}주"
        )

        # 3. 실전/모의 주문 발송 또는 가상 체결 처리
        ord_no = None
        if allow_order:
            try:
                if ex_style == "touch":
                    ord_no = self.app.order_manager.place_order(
                        code,
                        qty,
                        float(adj_price),
                        order_type="00",
                        side="buy",
                        name=name,
                        reason=reason,
                        skip_price_guard=True,
                        apply_limit_tick_adjust=False,
                        skip_immediate_fill_reconcile=True,
                    )
                elif ex_style == "market":
                    ord_no = self.app.order_manager.place_order(
                        code,
                        qty,
                        order_ref,
                        order_type="03",
                        side="buy",
                        name=name,
                        reason=reason,
                    )
                else:
                    ord_no = self.app.order_manager.place_order(
                        code,
                        qty,
                        order_ref,
                        order_type="00",
                        side="buy",
                        name=name,
                        reason=reason,
                    )
            except Exception as _po_e:
                try:
                    self.log(f"⚠️ [Buy Signal] place_order 예외: {_po_e}")
                except Exception:
                    pass
                ord_no = None
            try:
                sl = getattr(self.app, "session_logger", None)
                if sl:
                    _ob = {
                        "code": code,
                        "name": name,
                        "qty": int(qty),
                        "price": int(adj_price),
                        "order_type": ot,
                        "order_exec_style": ex_style,
                        "result": "submitted" if ord_no else "failed",
                        "order_no": ord_no or "",
                        "reason": reason,
                    }
                    _ob.update(self._supply_log_fields(code))
                    sl.log("order_buy", _ob)
            except Exception:
                pass
        else:
            fill_px = (
                float(adj_price) if ex_style == "touch" else float(order_ref)
            )
            snapshot = self.app.portfolio.capture_snapshot(
                code, fill_px, reason=reason
            )
            self.app.portfolio.on_fill(
                code, "buy", fill_px, qty, name=name, reason=reason, snapshot=snapshot
            )

        if allow_order and not ord_no:
            self.log(
                f"⚠️ [Buy Signal] {name}({code}) 매수 주문 실패 — 다음 신호에서 재판정·재시도 가능"
            )
            return False

        if allow_order and ord_no:
            try:
                self._register_limit_order_watch(
                    str(ord_no).strip(),
                    str(code).zfill(6),
                    "s1_buy",
                    qty=int(qty),
                    cancel_on_timeout=(ot == "00"),
                    touch_mode=(ex_style == "touch"),
                    touch_side="buy",
                    touch_name=name,
                    touch_reason=reason,
                    fill_price=float(adj_price),
                )
            except Exception as e:
                self.log(f"⚠️ [Buy Signal] 미체결 감시 등록 실패: {e}")

        try:
            self._notify_s1_buy_submitted(code, reason)
        except Exception:
            pass

        # 4. 텔레그램 알림 (비동기)
        import threading
        try:
            if ex_style == "touch":
                _tl = f"터치지정가(매도1호가) {adj_price:,}원"
            elif ot == "03":
                _tl = "시장가"
            else:
                _tl = f"지정가 {adj_price:,}원"
        except Exception:
            _tl = "주문"
        msg = (
            f"<b>💰 [매수발송/{reason}]</b>\n종목: {name}({code})\n"
            f"유형: {_tl}\n참고가: {int(order_ref):,}원 / 수: {qty}주\n"
            f"시간: {datetime.now().strftime('%H:%M:%S')}"
        )
        threading.Thread(target=self.app.send_telegram_message, args=(msg,), daemon=True).start()
        return True

    def handle_strategy_action(self, code, action, reason, **kwargs):
        """[v4.5.3/v4.5.4] StrategyEngine에서 결정된 액션(EXIT/STAGE2)을 처리합니다."""
        if action == "HOLD":
            return

        try:
            sl = getattr(self.app, "session_logger", None)
            if sl:
                sl.log(
                    "strategy_action",
                    {
                        "code": code,
                        "action": action,
                        "reason": reason,
                        "now_price": float(kwargs.get("now_price", 0) or 0),
                        "profit_ratio": float(kwargs.get("profit_ratio", 0) or 0),
                    },
                )
        except Exception:
            pass

        pos = self.app.portfolio.virtual_positions.get(code)
        if not pos:
            return

        now_price = kwargs.get('now_price', 0)
        profit_ratio = kwargs.get('profit_ratio', 0.0)

        try:
            np_hint = float(now_price)
        except (TypeError, ValueError):
            np_hint = 0.0
        order_ref_px = self._live_submit_reference_price(code, np_hint)
        if order_ref_px <= 0:
            order_ref_px = np_hint
        
        use_mock = getattr(config, 'USE_MOCK', False)
        allow_order = getattr(config, 'ALLOW_MOCK_ORDER', False) if use_mock else getattr(config, 'ALLOW_REAL_ORDER', False)
        ot, ex_style = self._realtime_live_order_spec()

        if action == "EXIT":
            # 매도: 터치(매수1호가), 지정가(참고가−1틱), 시장가(03)
            if ex_style == "touch":
                try:
                    _nk = str(code).zfill(6)
                    bid_t, ask_t = self.app.order_manager.fetch_best_bid_ask_rest(_nk)
                except Exception:
                    bid_t, ask_t = None, None
                if bid_t is None or float(bid_t) <= 0:
                    try:
                        self.log(
                            f"⚠️ [Exit Signal/{reason}] {pos.get('name', code)}({code}) "
                            f"매도 생략: ka10004 매수1호가 조회 실패"
                        )
                    except Exception:
                        pass
                    return
                try:
                    exit_ord_price = int(round(float(bid_t)))
                except (TypeError, ValueError):
                    exit_ord_price = 0
            else:
                try:
                    exit_ord_price = int(
                        self.app.order_manager.get_adjusted_price(
                            float(order_ref_px), "sell"
                        )
                    )
                except Exception:
                    try:
                        exit_ord_price = int(float(order_ref_px))
                    except Exception:
                        exit_ord_price = 0
            try:
                ref_px = int(float(order_ref_px or 0))
            except Exception:
                ref_px = 0

            # 이미 매도 주문 접수 대기 중이면 중복 EXIT 제출 금지
            # (단, 증권사 미체결 없음·취소 가능한 고착은 해제 후 재시도)
            try:
                if bool(pos.get("exit_pending", False)):
                    if not self._maybe_clear_stale_exit_pending(code, pos):
                        return
            except Exception:
                pass

            sell_qty = self._resolve_exit_sell_qty(code, pos)
            if sell_qty <= 0:
                try:
                    nk_pre = str(code).zfill(6)
                    om_pre = getattr(self.app, "order_manager", None)
                    if om_pre is not None:
                        om_pre.cancel_stale_open_sell_orders_for_code(
                            nk_pre, log_fn=self.log
                        )
                    sell_qty = self._resolve_exit_sell_qty(code, pos)
                except Exception:
                    sell_qty = 0
            if sell_qty <= 0:
                try:
                    self.log(
                        f"⚠️ [Exit Signal/{reason}] {pos.get('name', code)}({code}) "
                        f"매도 생략: 매도가능 수량 0(접수 매도·잠금 확인)"
                    )
                except Exception:
                    pass
                return

            # [Audit] Exit Execution — 주문 실패 시 exit_pending 미설정(재평가·재시도 가능)
            if allow_order:
                # 주문 거부/네트워크 지연 시 매 틱 동기 place_order를 반복하면 Tk 메인루프가 장시간 점유됩니다.
                # 실패 후 짧은 쿨다운 동안은 재제출을 스킵해 UI 프리징을 완화합니다.
                nk = str(code).zfill(6)
                now_epoch = time.time()
                try:
                    not_before = float(
                        self._exit_retry_not_before.get(nk, 0.0) or 0.0
                    )
                except Exception:
                    not_before = 0.0
                if now_epoch < not_before:
                    try:
                        last_skip = float(
                            self._exit_retry_skip_log_at.get(nk, 0.0) or 0.0
                        )
                    except Exception:
                        last_skip = 0.0
                    if now_epoch - last_skip >= 5.0:
                        self._exit_retry_skip_log_at[nk] = now_epoch
                        try:
                            wait_s = max(0.0, not_before - now_epoch)
                            self.log(
                                f"ℹ️ [Exit Signal/{reason}] {pos.get('name', code)}({nk}) "
                                f"재시도 쿨다운 {wait_s:.1f}s 남음 — 동기 재주문 스킵"
                            )
                        except Exception:
                            pass
                    return

                ord_no = None
                try:
                    if ex_style == "touch":
                        ord_no = self.app.order_manager.place_order(
                            code,
                            sell_qty,
                            float(exit_ord_price),
                            order_type="00",
                            side="sell",
                            name=pos["name"],
                            reason=f"EXIT_{reason}",
                            skip_price_guard=True,
                            apply_limit_tick_adjust=False,
                            skip_immediate_fill_reconcile=True,
                        )
                    elif ex_style == "market":
                        ord_no = self.app.order_manager.place_order(
                            code,
                            sell_qty,
                            order_ref_px,
                            order_type="03",
                            side="sell",
                            name=pos["name"],
                            reason=f"EXIT_{reason}",
                        )
                    else:
                        ord_no = self.app.order_manager.place_order(
                            code,
                            sell_qty,
                            order_ref_px,
                            order_type="00",
                            side="sell",
                            name=pos["name"],
                            reason=f"EXIT_{reason}",
                        )
                except Exception as _ex_po:
                    try:
                        self.log(f"⚠️ [Exit] place_order 예외: {_ex_po}")
                    except Exception:
                        pass
                    ord_no = None
                try:
                    sl = getattr(self.app, "session_logger", None)
                    if sl:
                        _os = {
                            "code": code,
                            "name": pos.get("name", ""),
                            "qty": int(sell_qty),
                            "price": int(exit_ord_price),
                            "ref_price": float(ref_px),
                            "order_exec_style": ex_style,
                            "result": "submitted" if ord_no else "failed",
                            "order_no": ord_no or "",
                            "reason": f"EXIT_{reason}",
                        }
                        _os.update(self._supply_log_fields(code))
                        sl.log("order_sell", _os)
                except Exception:
                    pass
                if ord_no:
                    # 접수 성공 시 실패 쿨다운 상태 초기화
                    try:
                        self._exit_retry_not_before.pop(nk, None)
                        self._exit_retry_skip_log_at.pop(nk, None)
                    except Exception:
                        pass
                    pos["exit_pending"] = True
                    # 주문 로그 `reason` 컬럼과 동일 문자열 — 종료내역·on_fill과 정합
                    pos["exit_reason"] = f"EXIT_{reason}"
                    pos["pending_exit_ord_no"] = str(ord_no).strip()
                    # 동기화로 로컬 보유가 사라져도 체결 시 종료 확정할 수 있게 접수 직후 스냅샷 저장
                    try:
                        self.app.portfolio.register_pending_exit_snapshot(
                            str(ord_no).strip(),
                            code,
                            pos,
                            f"EXIT_{reason}",
                        )
                    except Exception as ex:
                        self.log(f"⚠️ [Exit] 매도 스냅샷 등록 실패: {ex}")
                    try:
                        self._register_limit_order_watch(
                            str(ord_no).strip(),
                            str(code).zfill(6),
                            "exit_sell",
                            qty=int(sell_qty),
                            cancel_on_timeout=(ot == "00"),
                            touch_mode=(ex_style == "touch"),
                            touch_side="sell",
                            touch_name=str(pos.get("name", "") or ""),
                            touch_reason=f"EXIT_{reason}",
                            fill_price=float(exit_ord_price),
                        )
                    except Exception as e:
                        self.log(f"⚠️ [Exit] 미체결 감시 등록 실패: {e}")
                    self.log(
                        f"🚨 [Exit Signal/{reason}] {pos['name']}({code}) 매도 접수 "
                        f"({self._exit_order_label(ot, ex_style, exit_ord_price)}, {profit_ratio:+.2f}%) "
                        f"참고가 {ref_px:,}원"
                        + (
                            f" · 호가 {exit_ord_price:,}원"
                            if ot == "00"
                            else ""
                        )
                    )
                    import threading
                    _exit_tl = self._exit_order_label(ot, ex_style, exit_ord_price)
                    msg = (
                        f"<b>🚀 [매도발송/{reason}]</b>\n종목: {pos['name']}({code})\n"
                        f"유형: {_exit_tl}\n"
                        f"참고가: {ref_px:,}원\n"
                        f"수익률: {profit_ratio:+.1f}%\n시간: {datetime.now().strftime('%H:%M:%S')}"
                    )
                    threading.Thread(
                        target=self.app.send_telegram_message, args=(msg,), daemon=True
                    ).start()
                else:
                    # 실패 시 짧은 쿨다운(기본 8초) 적용
                    try:
                        reject = str(
                            getattr(
                                self.app.order_manager, "last_order_reject_msg", ""
                            )
                            or ""
                        )
                    except Exception:
                        reject = ""
                    insuff_sell = (
                        "800033" in reject
                        or "매도가능" in reject
                        or "매도가능수량" in reject
                    )
                    if insuff_sell:
                        # 서버상 매도 불가(이미 전량 매도된 경우 등) — 로컬·kt00007 괴리 보정
                        try:
                            pos["exit_pending"] = False
                            pos.pop("pending_exit_ord_no", None)
                        except Exception:
                            pass

                        def _recover_after_insufficient_sell() -> None:
                            try:
                                om = self.app.order_manager
                                dm = self.app.data_manager
                                if om is not None:
                                    try:
                                        om.cancel_stale_open_sell_orders_for_code(
                                            nk, log_fn=self.log
                                        )
                                    except Exception:
                                        pass
                                if om is not None and dm is not None:
                                    import datetime as _dt_rec

                                    om.sync_exchange_fills_into_database(
                                        dm,
                                        dates=[_dt_rec.date.today()],
                                        silent=True,
                                        logger=self.log,
                                    )
                            except Exception:
                                pass
                            try:
                                self.app.portfolio.sync_with_actual_account(
                                    silent=True, force=True
                                )
                            except Exception:
                                pass
                            try:
                                with self.app.portfolio.lock:
                                    p2 = self.app.portfolio.virtual_positions.get(
                                        nk
                                    )
                                    if p2 is not None:
                                        p2["exit_pending"] = False
                                        p2.pop("pending_exit_ord_no", None)
                            except Exception:
                                pass

                        try:
                            rt = getattr(self.app, "root", None)
                            if rt is not None and hasattr(rt, "after"):
                                rt.after(250, _recover_after_insufficient_sell)
                            else:
                                import threading as _th_rec

                                _th_rec.Thread(
                                    target=_recover_after_insufficient_sell,
                                    daemon=True,
                                ).start()
                        except Exception:
                            try:
                                _recover_after_insufficient_sell()
                            except Exception:
                                pass
                        try:
                            self.log(
                                f"ℹ️ [Exit Signal/{reason}] {pos.get('name', nk)}({nk}) "
                                f"매도가능수량 부족 응답 — 체결내역 병합·계좌 동기화를 예약했습니다."
                            )
                        except Exception:
                            pass
                    try:
                        st = getattr(self.app, "settings", None) or {}
                        cd = float(st.get("exit_order_retry_cooldown_sec", 8.0))
                    except Exception:
                        cd = 8.0
                    cd = max(1.0, min(cd, 120.0))
                    try:
                        self._exit_retry_not_before[nk] = time.time() + cd
                    except Exception:
                        pass
                    self.log(
                        f"⚠️ [Exit Signal/{reason}] {pos['name']}({code}) 매도 주문 실패 "
                        f"— {cd:.1f}s 후 재시도 ({profit_ratio:+.2f}%)"
                    )
                    try:
                        sl = getattr(self.app, "session_logger", None)
                        if sl:
                            sl.log(
                                "order_exit_failed",
                                {
                                    "code": code,
                                    "name": pos.get("name", ""),
                                    "reason": f"EXIT_{reason}",
                                    "profit_ratio": float(profit_ratio),
                                },
                            )
                    except Exception:
                        pass
            else:
                fill_px = (
                    float(exit_ord_price)
                    if ex_style == "touch"
                    else float(order_ref_px)
                )
                snapshot = self.app.portfolio.capture_snapshot(
                    code, fill_px, reason=f"EXIT_{reason}"
                )
                self.app.portfolio.on_fill(
                    code,
                    "sell",
                    fill_px,
                    sell_qty,
                    name=pos.get("name"),
                    reason=f"EXIT_{reason}",
                    snapshot=snapshot,
                )
                self.log(
                    f"🚨 [Exit Signal/{reason}] {pos['name']}({code}) 가상 청산 "
                    f"({profit_ratio:+.2f}%) 주문가 {exit_ord_price:,}원 · 참고가 {ref_px:,}원"
                )
                import threading
                msg = (
                    f"<b>🚀 [매도발송/{reason}]</b>\n종목: {pos['name']}({code})\n"
                    f"주문가: {exit_ord_price:,}원 (참고가 {ref_px:,}원)\n"
                    f"수익률: {profit_ratio:+.1f}%\n시간: {datetime.now().strftime('%H:%M:%S')}"
                )
                threading.Thread(
                    target=self.app.send_telegram_message, args=(msg,), daemon=True
                ).start()

        elif action == "STAGE2":
            # [Audit] Stage 2 Scale-in Execution
            add_amt = self.app.portfolio.calculate_order_quantity(stage=2, is_simulation=not allow_order)
            if add_amt <= 0: return

            if ex_style == "touch":
                try:
                    _nk2 = str(code).zfill(6)
                    bid_s2, ask_s2 = self.app.order_manager.fetch_best_bid_ask_rest(_nk2)
                except Exception:
                    bid_s2, ask_s2 = None, None
                if ask_s2 is None or float(ask_s2) <= 0:
                    try:
                        self.log(
                            f"⚠️ [Scale-in] {pos.get('name', code)}({code}) "
                            f"2차 생략: ka10004 매도1호가 조회 실패"
                        )
                    except Exception:
                        pass
                    return
                try:
                    adj_price = int(round(float(ask_s2)))
                except (TypeError, ValueError):
                    adj_price = 0
            else:
                adj_price = self.app.order_manager.get_adjusted_price(
                    order_ref_px, "buy"
                )
            add_qty = add_amt // adj_price
            
            if add_qty > 0:
                if allow_order:
                    ord_no = None
                    try:
                        if ex_style == "touch":
                            ord_no = self.app.order_manager.place_order(
                                code,
                                add_qty,
                                float(adj_price),
                                order_type="00",
                                side="buy",
                                name=pos['name'],
                                reason="Stage 2 Scaling",
                                skip_price_guard=True,
                                apply_limit_tick_adjust=False,
                                skip_immediate_fill_reconcile=True,
                            )
                        elif ex_style == "market":
                            ord_no = self.app.order_manager.place_order(
                                code,
                                add_qty,
                                order_ref_px,
                                order_type="03",
                                side="buy",
                                name=pos['name'],
                                reason="Stage 2 Scaling",
                            )
                        else:
                            ord_no = self.app.order_manager.place_order(
                                code,
                                add_qty,
                                order_ref_px,
                                order_type="00",
                                side="buy",
                                name=pos['name'],
                                reason="Stage 2 Scaling",
                            )
                    except Exception as _s2e:
                        try:
                            self.log(f"⚠️ [S2] place_order 예외: {_s2e}")
                        except Exception:
                            pass
                        ord_no = None
                    try:
                        sl = getattr(self.app, "session_logger", None)
                        if sl:
                            _os2 = {
                                "code": code,
                                "name": pos.get("name", ""),
                                "qty": int(add_qty),
                                "price": int(adj_price),
                                "order_exec_style": ex_style,
                                "result": "submitted" if ord_no else "failed",
                                "order_no": ord_no or "",
                                "reason": "Stage 2 Scaling",
                            }
                            _os2.update(self._supply_log_fields(code))
                            sl.log("order_buy_s2", _os2)
                    except Exception:
                        pass
                    if ord_no:
                        pos["s2_limit_order_pending"] = True
                        pos["s2_pending_ord_no"] = str(ord_no).strip()
                        try:
                            self._register_limit_order_watch(
                                str(ord_no).strip(),
                                str(code).zfill(6),
                                "s2_buy",
                                qty=int(add_qty),
                                cancel_on_timeout=(ot == "00"),
                                touch_mode=(ex_style == "touch"),
                                touch_side="buy",
                                touch_name=str(pos.get("name", "") or ""),
                                touch_reason="Stage 2 Scaling",
                                fill_price=float(adj_price),
                            )
                        except Exception as e:
                            self.log(f"⚠️ [S2] 미체결 감시 등록 실패: {e}")
                        self.log(
                            f"🔥 [Scale-in] {pos['name']}({code}) 2차 추매 접수 "
                            f"({self._s2_order_label(ot, ex_style)}, 참고가 {int(order_ref_px):,}원"
                            f"{'' if ex_style == 'market' else f', 호가 {adj_price:,}원'}, {add_qty}주)"
                        )
                        import threading
                        msg = (
                            f"<b>🔥 [추매발송/2차]</b>\n종목: {pos['name']}({code})\n"
                            f"유형: {self._s2_order_label(ot, ex_style, adj_price)}\n"
                            f"참고가: {int(order_ref_px):,}원\n"
                            f"평단대비: {profit_ratio:+.1f}%\n시간: {datetime.now().strftime('%H:%M:%S')}"
                        )
                        threading.Thread(
                            target=self.app.send_telegram_message, args=(msg,), daemon=True
                        ).start()
                    else:
                        self.log(
                            f"⚠️ [Scale-in] {pos['name']}({code}) 2차 매수 주문 실패 — 재조건 충족 시 재시도"
                        )
                else:
                    fill_s2 = (
                        float(adj_price)
                        if ex_style == "touch"
                        else float(order_ref_px)
                    )
                    snapshot = self.app.portfolio.capture_snapshot(
                        code, fill_s2, reason="Stage 2 Scaling"
                    )
                    self.app.portfolio.on_fill(
                        code, "buy", fill_s2, add_qty, snapshot=snapshot
                    )
                    self.log(
                        f"🔥 [Scale-in] {pos['name']}({code}) 2차 추매 가상체결 "
                        f"({float(adj_price) if ex_style == 'touch' else float(order_ref_px):,.0f}원, {add_qty}주)"
                    )
                    import threading
                    msg = (
                        f"<b>🔥 [추매발송/2차]</b>\n종목: {pos['name']}({code})\n"
                        f"평단대비: {profit_ratio:+.1f}%\n시간: {datetime.now().strftime('%H:%M:%S')}"
                    )
                    threading.Thread(
                        target=self.app.send_telegram_message, args=(msg,), daemon=True
                    ).start()

    @staticmethod
    def _s2_order_label(
        ot: str, ex_style: str, adj_price: int = 0
    ) -> str:
        """S2 로그·텔레그램용 주문 유형."""
        try:
            if ex_style == "touch":
                return f"터치지정가(매도1호가) {int(adj_price):,}원"
            if str(ot).strip() == "03":
                return "시장가"
            return "지정가"
        except Exception:
            return "지정가"

    def _limit_order_timeout_ms(self) -> int:
        """settings.json `limit_order_unfilled_timeout_sec` — 서비스에서 사용."""
        try:
            return self.limit_order_watch_service._get_timeout_ms()
        except Exception:
            return 5000

    def clear_limit_order_watch(self, order_no: Optional[str]) -> None:
        """체결 등으로 주문이 종료되면 타이머·대기 플래그를 정리합니다."""
        try:
            self.limit_order_watch_service.clear(order_no)
        except Exception:
            # 컨트롤러 호출은 UI/체결 이벤트에서 발생 가능 → 전체 중단 방지
            pass

    def _register_limit_order_watch(
        self,
        order_no: str,
        code: str,
        kind: str,
        qty: int = 0,
        *,
        cancel_on_timeout: bool = True,
        touch_mode: bool = False,
        touch_side: str = "",
        touch_name: str = "",
        touch_reason: str = "",
        fill_price: float = 0.0,
    ) -> None:
        """지정가: 미체결 타임아웃 시 취소 API. 시장가: 플래그만. 터치: 체결 확인+정정 루프."""
        try:
            self.limit_order_watch_service.register(
                order_no=order_no,
                code=code,
                kind=kind,
                qty=qty,
                cancel_on_timeout=cancel_on_timeout,
                touch_mode=touch_mode,
                touch_side=touch_side,
                touch_name=touch_name,
                touch_reason=touch_reason,
                fill_price_hint=float(fill_price or 0),
            )
        except Exception as e:
            try:
                self.log(f"⚠️ [지정가] 미체결 감시 등록 실패: {e}")
            except Exception:
                pass

    def _handle_limit_order_timeout(self, order_no: str) -> None:
        """미체결 타임아웃: 서비스 위임(취소 API 호출 후 플래그 해제)."""
        try:
            self.limit_order_watch_service.timeout(order_no)
        except Exception:
            pass
