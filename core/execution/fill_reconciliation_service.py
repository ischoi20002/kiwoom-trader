# -*- coding: utf-8 -*-
import threading
import time
from typing import Any, Dict, Literal, Optional, Set

from core.supply_display import capture_supply_snapshot


class FillReconciliationService:
    """
    REST/WS 체결 이벤트 동기화 및 중복 방지(가상 on_fill/DB 반영).

    KiwoomApp 안에 있던 “체결 처리” 책임을 서비스로 이동해 오케스트레이션 크기 감소 목적.
    """

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print

    def _fcs(self) -> Any:
        return getattr(self.app, "fill_confirmation_service", None)

    @staticmethod
    def _order_side_to_kr(side: str) -> str:
        try:
            s = str(side or "").strip().lower()
        except Exception:
            s = ""
        if s == "buy":
            return "매수"
        if s == "sell":
            return "매도"
        return "?"

    @staticmethod
    def _fmt_int_comma(v: Any) -> str:
        try:
            iv = int(v or 0)
            return f"{iv:,}" if iv > 0 else "-"
        except Exception:
            return "-"

    @staticmethod
    def _fmt_price_comma(v: Any) -> str:
        try:
            pv = float(v or 0)
            return f"{pv:,.0f}" if pv > 0 else "-"
        except Exception:
            return "-"

    @staticmethod
    def _fmt_vp_cell(v: Any) -> str:
        """포지션관리탭 VP 셀과 동일하게 소수 1자리 %."""
        try:
            if v is None:
                return "-"
            return f"{float(v):.1f}%"
        except Exception:
            return "-"

    @staticmethod
    def _fmt_ba_cell(v: Any) -> str:
        try:
            return (str(v or "").strip() or "-")
        except Exception:
            return "-"

    def _maybe_send_buy_sell_telegram(self, code: str, order_no: str, side: str) -> None:
        """
        체결 DB(row) 기준으로 '매수/매도' 텔레그램을 1회만 보냅니다.
        (주문번호 + 중복 방지 set을 이미 처리하고 있는 호출부만 이 함수를 호출)
        """
        try:
            if not getattr(self.app, "send_telegram_message", None):
                return
        except Exception:
            return

        try:
            row = self.app.data_manager.get_order_history_row_for_telegram(code, order_no)
            if not row:
                return

            side_kr = self._order_side_to_kr(row.get("side"))
            msg = (
                f"📌 <b>[매수/매도 체결]</b>\n"
                f"매수/매도: {side_kr}\n"
                f"종목명: {row.get('name') or '-'}\n"
                f"주문가: {self._fmt_price_comma(row.get('price'))}원\n"
                f"체결가: {self._fmt_price_comma(row.get('filled_price'))}원\n"
                f"체결수량: {self._fmt_int_comma(row.get('filled_qty'))}주\n"
                f"VP: {self._fmt_vp_cell(row.get('snap_vol_power'))}\n"
                f"호가: {self._fmt_ba_cell(row.get('snap_bid_ask'))}\n"
                f"사유: {row.get('reason') or '-'}\n"
                f"일시: {str(row.get('timestamp') or '').strip() or '-'}"
            )

            import threading

            threading.Thread(
                target=self.app.send_telegram_message,
                args=(msg,),
                daemon=True,
            ).start()
        except Exception as e:
            try:
                self.log(f"⚠️ [Telegram] 메시지 생성 실패: {e}")
            except Exception:
                pass

    def _kt00007_sync_min_interval_sec(self) -> float:
        try:
            fcs = self._fcs()
            if fcs is not None:
                return float(fcs._kt00007_global_min_interval_sec())
        except Exception:
            pass
        return 5.0

    def _sync_exchange_fills_today_silent(
        self, *, force: bool = False, order_no: Optional[str] = None
    ) -> None:
        """당일 kt00007 병합 — FillConfirmationService 위임."""
        try:
            fcs = self._fcs()
            if fcs is not None:
                fcs.sync_kt00007(order_no=order_no, force=bool(force))
        except Exception:
            pass

    @staticmethod
    def _exchange_fill_row_decision(row: Dict[str, Any]) -> Optional[bool]:
        """
        주문 로그 1행의 체결 상태 판정.

        Returns
        -------
        True
            체결 확정(FILLED 또는 filled_qty>0).
        False
            체결 없이 종료(취소·거부 등).
        None
            아직 미확정.
        """
        try:
            res = str(row.get("result", "") or "").strip().upper()
        except Exception:
            res = ""
        try:
            fqty = int(row.get("filled_qty", 0) or 0)
        except Exception:
            fqty = 0
        if res in ("CANCELLED", "CANCELED", "REJECTED", "FAILED") and fqty <= 0:
            return False
        if res in ("FILLED", "PARTIAL") or fqty > 0:
            return True
        return None

    def _enrich_row_with_cumulative_fills(
        self,
        order_no: str,
        row: Dict[str, Any],
        *,
        expect_qty: int = 0,
    ) -> Dict[str, Any]:
        """
        접수 행의 filled_qty=0 이어도 kt00007·fill_state 누적 체결을 row에 합칩니다.
        """
        out = dict(row or {})
        oid = str(order_no or "").strip()
        try:
            cur = int(out.get("filled_qty") or 0)
        except (TypeError, ValueError):
            cur = 0
        best = cur
        try:
            dm = getattr(self.app, "data_manager", None)
            if dm is not None:
                best = max(best, int(dm.sum_order_history_filled_qty(oid) or 0))
        except Exception:
            pass
        try:
            fcs = self._fcs()
            if fcs is not None:
                best = max(
                    best,
                    int(fcs.get_exchange_filled_qty(oid) or 0),
                    int(fcs.get_applied_qty(oid) or 0),
                )
        except Exception:
            pass
        if best <= cur:
            return out
        out["filled_qty"] = int(best)
        try:
            oq = int(out.get("qty") or 0)
        except (TypeError, ValueError):
            oq = 0
        if oq <= 0:
            oq = max(0, int(expect_qty or 0))
        res_u = str(out.get("result") or "").strip().upper()
        if res_u in ("CANCELLED", "CANCELED", "REJECTED", "FAILED"):
            # 종료 상태는 유지하되 filled_qty 만 보강(판정·증분 반영용)
            pass
        elif oq > 0 and best >= oq:
            out["result"] = "FILLED"
        else:
            out["result"] = "PARTIAL"
        return out

    def _apply_confirmed_exchange_fill(
        self,
        *,
        code: str,
        order_no: str,
        side: str,
        qty: int,
        fill_price: float,
        name: str,
        reason: str,
        row: Dict[str, Any],
    ) -> bool:
        """체결 확정 row 기준 증분 on_fill — FillConfirmationService 위임."""
        try:
            fcs = self._fcs()
            if fcs is not None:
                return bool(
                    fcs.ingest_rest_confirmed(
                        code=code,
                        order_no=order_no,
                        side=side,
                        qty=int(qty),
                        fill_price=float(fill_price),
                        name=name,
                        reason=reason,
                        row=row,
                    )
                )
        except Exception as e:
            try:
                self.log(f"⚠️ [체결 동기화] ingest_rest 실패: {e}")
            except Exception:
                pass
        return False

    def try_confirm_fill_once(
        self,
        code: str,
        order_no: str,
        side: str,
        qty: int,
        fill_price: float,
        name: str = "",
        reason: str = "",
        *,
        force_kt00007_sync: bool = False,
    ) -> Literal["filled", "terminal", "pending", "skipped"]:
        """
        kt00007 1회(per-order) 동기화 + DB 1회 확인(터치호가 감시용).
        """
        oid = str(order_no or "").strip()
        if not oid:
            return "pending"
        try:
            fcs = self._fcs()
            if fcs is not None and fcs.is_fully_applied(oid):
                return "skipped"
            elif oid in self.app._exchange_fill_order_ids:
                fcs2 = self._fcs()
                if fcs2 is None or fcs2.is_fully_applied(oid):
                    return "skipped"
        except Exception:
            try:
                if oid in self.app._exchange_fill_order_ids:
                    return "skipped"
            except Exception:
                pass

        self._sync_exchange_fills_today_silent(
            force=bool(force_kt00007_sync), order_no=oid
        )

        try:
            row = self.app.data_manager.get_order_history_row_by_order_no(oid)
        except Exception:
            row = None
        if not isinstance(row, dict):
            return "pending"

        # 접수 행(SUCCESS·filled_qty=0)만 있어도 kt00007 부분체결 합산을 row에 얹는다.
        try:
            row = self._enrich_row_with_cumulative_fills(oid, row, expect_qty=int(qty))
        except Exception:
            pass

        try:
            st = getattr(self.app, "settings", None) or {}
            if bool(st.get("enable_fill_reconcile_row_debug", False)):
                _res_u = str((row or {}).get("result") or "").strip().upper()
                _fq = int((row or {}).get("filled_qty") or 0)
                _qp = int((row or {}).get("qty") or 0)
                _fp = float((row or {}).get("filled_price") or 0)
                _op = float((row or {}).get("price") or 0)
                _aq = 0
                try:
                    fcs = self._fcs()
                    if fcs is not None:
                        _aq = fcs.get_applied_qty(oid)
                except Exception:
                    pass
                self.log(
                    "🧪 [체결확인-row] "
                    f"code={code} ord={oid} side={side} "
                    f"row.result={_res_u} row.filled_qty={_fq} row.qty={_qp} "
                    f"row.filled_price={_fp} row.price={_op} "
                    f"applied_qty={_aq} expect_qty={qty} hint_price={fill_price}"
                )
        except Exception:
            pass

        decision = self._exchange_fill_row_decision(row)
        if decision is False:
            # CANCELLED 라도 누적 체결이 있으면 부분체결 반영 후 terminal
            try:
                fq_term = int((row or {}).get("filled_qty") or 0)
            except (TypeError, ValueError):
                fq_term = 0
            if fq_term > 0:
                try:
                    self._apply_confirmed_exchange_fill(
                        code=code,
                        order_no=order_no,
                        side=side,
                        qty=int(qty),
                        fill_price=float(fill_price),
                        name=name,
                        reason=reason,
                        row=row,
                    )
                except Exception:
                    pass
                try:
                    res = str(row.get("result", "") or "").strip().upper()
                    self.log(
                        f"ℹ️ [체결 동기화] 주문번호 {oid} — "
                        f"종료({res})·부분체결 {fq_term}주 반영 후 폴링 중단"
                    )
                except Exception:
                    pass
                return "terminal"
            try:
                res = str(row.get("result", "") or "").strip().upper()
                self.log(
                    f"ℹ️ [체결 동기화] 주문번호 {oid} — "
                    f"체결 없이 종료({res}), 폴링 중단"
                )
            except Exception:
                pass
            return "terminal"
        if decision is not True:
            return "pending"

        if self._apply_confirmed_exchange_fill(
            code=code,
            order_no=order_no,
            side=side,
            qty=int(qty),
            fill_price=float(fill_price),
            name=name,
            reason=reason,
            row=row,
        ):
            try:
                fcs = self._fcs()
                if fcs is not None:
                    if fcs.is_fully_applied(oid):
                        return "filled"
                    if fcs.get_applied_qty(oid) > 0:
                        return "pending"
            except Exception:
                pass
            return "filled"
        try:
            fcs = self._fcs()
            if fcs is not None and fcs.get_applied_qty(oid) > 0:
                if fcs.is_fully_applied(oid):
                    return "filled"
                return "pending"
        except Exception:
            pass
        return "pending"

    def run_light_boost_confirm(
        self,
        code: str,
        order_no: str,
        side: str,
        qty: int,
        fill_price: float,
        name: str = "",
        reason: str = "",
    ) -> None:
        """
        RC4033(미체결 없음)·타임아웃 등에서 전량 체결 가능성이 있을 때 경량 확인.
        ``reconcile_exchange_fill_after_order_accept(boost_polling=True)`` 대신
        짧은 ``try_confirm_fill_once`` 루프만 돌려 REST·스레드 폭주를 줄입니다.
        """
        oid = str(order_no or "").strip()
        if not oid:
            return
        try:
            if oid in self.app._exchange_fill_order_ids:
                fcs = self._fcs()
                if fcs is None or fcs.is_fully_applied(oid):
                    return
        except Exception:
            pass

        def _worker() -> None:
            try:
                try:
                    st = getattr(self.app, "settings", None) or {}
                    max_a = int(float(st.get("exchange_fill_boost_max_attempts", 6)))
                    interval = float(
                        st.get("exchange_fill_boost_interval_sec", 1.5)
                    )
                except (TypeError, ValueError):
                    max_a = 6
                    interval = 1.5
                max_a = max(3, min(max_a, 10))
                interval = max(0.5, min(interval, 4.0))
                for i in range(max_a):
                    try:
                        fcs = self._fcs()
                        if fcs is not None and fcs.is_fully_applied(oid):
                            return
                        if oid in self.app._exchange_fill_order_ids:
                            if fcs is None or fcs.is_fully_applied(oid):
                                return
                    except Exception:
                        pass
                    status = self.try_confirm_fill_once(
                        code,
                        oid,
                        side,
                        int(qty),
                        float(fill_price),
                        name,
                        reason,
                        force_kt00007_sync=True,
                    )
                    if status in ("filled", "terminal", "skipped"):
                        return
                    if i + 1 < max_a:
                        try:
                            time.sleep(interval)
                        except Exception:
                            return
            except Exception:
                pass

        try:
            threading.Thread(target=_worker, daemon=True, name="FillBoostLight").start()
        except Exception:
            pass

    def reconcile_exchange_fill_after_order_accept(
        self,
        code: str,
        order_no: str,
        side: str,
        qty: int,
        fill_price: float,
        name: str = "",
        reason: str = "",
        *,
        boost_polling: bool = False,
    ) -> None:
        """
        REST 주문 접수 성공 직후 웹소켓 CHE가 오지 않을 때도 DB 체결가·포지션을 맞춥니다.
        CHE가 나중에 오면 동일 주문번호로 on_fill은 생략하고 체결가만 보정합니다.
        """
        try:
            oid = str(order_no or "").strip()
            try:
                fcs = self._fcs()
                if fcs is not None and fcs.is_fully_applied(oid):
                    return
                if oid and oid in self.app._exchange_fill_order_ids:
                    if fcs is None or fcs.is_fully_applied(oid):
                        return
            except Exception:
                if oid and oid in self.app._exchange_fill_order_ids:
                    return

            # REST 접수 후 체결확정 폴링 동기화 in-flight 중복 방지
            boost_after_other = False
            if oid and hasattr(self.app, "_exchange_fill_order_inflight"):
                try:
                    if oid in self.app._exchange_fill_order_inflight:
                        if not boost_polling:
                            return
                        # boost: 기존 폴링 스레드가 끝날 때까지 대기 후 이어서 kt00007 재시도
                        boost_after_other = True
                    else:
                        self.app._exchange_fill_order_inflight.add(oid)
                except Exception:
                    pass

            # 핵심: REST 주문 접수 직후에는 'FILLED'로 가정하지 않습니다.
            # (프로그램 재시작/WS 지연 상황에서 on_fill로 인해 보유를 조기 제거할 수 있음)
            # 실제 체결 확정(kt00007 병합/DB filled_qty>0) 확인 후에만 on_fill을 호출합니다.
            def _confirm_and_apply() -> None:
                try:
                    if boost_after_other:
                        import time as _twait

                        for _w in range(80):
                            try:
                                if oid not in self.app._exchange_fill_order_inflight:
                                    break
                            except Exception:
                                break
                            _twait.sleep(0.5)
                        try:
                            self.app._exchange_fill_order_inflight.add(oid)
                        except Exception:
                            pass

                    # settings.json 우선, 없으면 보수적 기본값
                    try:
                        st = getattr(self.app, "settings", None) or {}
                    except Exception:
                        st = {}
                    try:
                        max_attempts = int(
                            float(
                                st.get(
                                    "exchange_fill_confirm_max_attempts", 8
                                )
                            )
                        )
                    except Exception:
                        max_attempts = 8
                    try:
                        retry_interval_sec = float(
                            st.get("exchange_fill_confirm_retry_interval_sec", 5.0)
                        )
                    except Exception:
                        retry_interval_sec = 1.0

                    if boost_polling:
                        try:
                            boost_cap = int(
                                float(
                                    st.get("exchange_fill_boost_max_attempts", 6)
                                )
                            )
                        except (TypeError, ValueError):
                            boost_cap = 6
                        boost_cap = max(2, min(boost_cap, 6))
                        max_attempts = min(max(max_attempts, boost_cap), boost_cap)
                        retry_interval_sec = max(retry_interval_sec, 2.5)

                    upper_cap = 12 if boost_polling else 12
                    max_attempts = max(2, min(max_attempts, upper_cap))
                    retry_interval_sec = max(0.5, min(retry_interval_sec, 5.0))

                    for _i in range(max_attempts):
                        try:
                            fcs = self._fcs()
                            if fcs is not None and fcs.is_fully_applied(oid):
                                return
                        except Exception:
                            if oid and oid in self.app._exchange_fill_order_ids:
                                return

                        self._sync_exchange_fills_today_silent(
                            force=(_i == 0), order_no=oid
                        )

                        try:
                            row = self.app.data_manager.get_order_history_row_by_order_no(
                                oid
                            )
                        except Exception:
                            row = None

                        if isinstance(row, dict):
                            decision = self._exchange_fill_row_decision(row)
                            if decision is False:
                                try:
                                    res = (
                                        str(row.get("result", "") or "")
                                        .strip()
                                        .upper()
                                    )
                                    self.log(
                                        f"ℹ️ [체결 동기화] 주문번호 {oid} — "
                                        f"체결 없이 종료({res}), 폴링 중단"
                                    )
                                except Exception:
                                    pass
                                return
                            if decision is True:
                                if self._apply_confirmed_exchange_fill(
                                    code=code,
                                    order_no=order_no,
                                    side=side,
                                    qty=int(qty),
                                    fill_price=float(fill_price),
                                    name=name,
                                    reason=reason,
                                    row=row,
                                ):
                                    return

                        # 아직 미확정이면 잠깐 대기 후 재시도
                        try:
                            import time as _time

                            _time.sleep(retry_interval_sec)
                        except Exception:
                            return

                    # max_attempts 초과: FILLED 확인 실패 — 지연 재시도·계좌 동기화로 흡수
                    try:
                        self.log(
                            f"⚠️ [체결 동기화] REST 접수(order_no={oid}) 즉시 체결 확정 실패 — "
                            f"지연 재시도·동기화 예약"
                        )
                    except Exception:
                        pass
                    try:
                        import threading as _th2

                        def _late_recover() -> None:
                            try:
                                if oid and oid in self.app._exchange_fill_order_ids:
                                    return
                                try:
                                    st2 = getattr(self.app, "settings", None) or {}
                                    delay = float(
                                        st2.get(
                                            "exchange_fill_late_reconfirm_sec",
                                            35.0,
                                        )
                                    )
                                except (TypeError, ValueError):
                                    delay = 35.0
                                delay = max(12.0, min(delay, 180.0))
                                import time as _t2

                                _t2.sleep(delay)
                                if oid and oid in self.app._exchange_fill_order_ids:
                                    return
                                self.reconcile_exchange_fill_after_order_accept(
                                    code,
                                    oid,
                                    side,
                                    qty,
                                    fill_price,
                                    name,
                                    reason,
                                    boost_polling=True,
                                )
                                try:
                                    # GUI 프리징 방지: Tk 메인스레드(root.after)로 즉시 sync를 태우지 않고
                                    # 백그라운드 스레드에서 처리합니다.
                                    def _bg_sync() -> None:
                                        try:
                                            self.app.portfolio.sync_with_actual_account(
                                                silent=True, force=True
                                            )
                                        except Exception:
                                            pass

                                    threading.Thread(target=_bg_sync, daemon=True).start()
                                except Exception:
                                    try:
                                        self.app.portfolio.sync_with_actual_account(
                                            silent=True, force=True
                                        )
                                    except Exception:
                                        pass
                            except Exception:
                                pass

                        _th2.Thread(target=_late_recover, daemon=True).start()
                    except Exception:
                        pass
                finally:
                    if oid and hasattr(self.app, "_exchange_fill_order_inflight"):
                        try:
                            self.app._exchange_fill_order_inflight.discard(oid)
                        except Exception:
                            pass

            import threading
            threading.Thread(target=_confirm_and_apply, daemon=True).start()
        except Exception as e:
            self.log(f"⚠️ [체결 동기화] 실패: {e}")

    def _safe_int(self, v: Any, default: int = 0) -> int:
        """값을 안전하게 int로 변환합니다."""
        try:
            return int(v or 0)
        except Exception:
            return default

    def _normalize_code_6(self, raw_code: Any) -> str:
        """코드를 6자리 종목코드로 정규화합니다."""
        try:
            return str(raw_code).split("_")[0].zfill(6)
        except Exception:
            return str(raw_code)[:6] if raw_code is not None else "000000"

    def _should_clear_limit_order_watch_for_sell(
        self, code: str
    ) -> bool:
        """
        안전가드:
        - 매도 주문이 부분체결이면, 포트폴리오에 잔여 수량이 남아있어야 합니다.
        - 이때 limit_order_watch_service.clear()가 호출되면 exit_pending이 해제될 수 있어 위험합니다.
        - 따라서 'sell'의 경우 포트폴리오 잔여 qty가 0 이하일 때만 clear를 허용합니다.
        """
        try:
            vp = getattr(self.app.portfolio, "virtual_positions", None) or {}
        except Exception:
            return True  # 결정 불가면 보수적으로 clear
        nc = self._normalize_code_6(code)
        try:
            pos = vp.get(nc) or vp.get(code) or {}
        except Exception:
            pos = {}
        try:
            remaining_qty = self._safe_int(pos.get("qty"), 0)
        except Exception:
            remaining_qty = 0
        return remaining_qty <= 0

    def on_execution_received(
        self,
        code: str,
        order_no: str,
        qty: int,
        price: float,
        side: str,
    ) -> None:
        """웹소켓 CHE — FillConfirmationService 증분 반영."""
        try:
            fcs = self._fcs()
            if fcs is not None:
                fcs.ingest_che(code, order_no, qty, price, side)
                return
        except Exception as e:
            try:
                self.log(f"⚠️ [체결] CHE 위임 실패: {e}")
            except Exception:
                pass

