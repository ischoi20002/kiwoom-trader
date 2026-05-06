# -*- coding: utf-8 -*-
from typing import Any, Dict, Optional, Set

from core.supply_display import capture_supply_snapshot


class FillReconciliationService:
    """
    REST/WS 체결 이벤트 동기화 및 중복 방지(가상 on_fill/DB 반영).

    KiwoomApp 안에 있던 “체결 처리” 책임을 서비스로 이동해 오케스트레이션 크기 감소 목적.
    """

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print

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

    def reconcile_exchange_fill_after_order_accept(
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
        REST 주문 접수 성공 직후 웹소켓 CHE가 오지 않을 때도 DB 체결가·포지션을 맞춥니다.
        CHE가 나중에 오면 동일 주문번호로 on_fill은 생략하고 체결가만 보정합니다.
        """
        try:
            oid = str(order_no or "").strip()
            if oid and oid in self.app._exchange_fill_order_ids:
                return

            # REST 접수 후 체결확정 폴링 동기화 in-flight 중복 방지
            if oid and hasattr(self.app, "_exchange_fill_order_inflight"):
                try:
                    if oid in self.app._exchange_fill_order_inflight:
                        return
                    self.app._exchange_fill_order_inflight.add(oid)
                except Exception:
                    pass

            # 핵심: REST 주문 접수 직후에는 'FILLED'로 가정하지 않습니다.
            # (프로그램 재시작/WS 지연 상황에서 on_fill로 인해 보유를 조기 제거할 수 있음)
            # 실제 체결 확정(kt00007 병합/DB filled_qty>0) 확인 후에만 on_fill을 호출합니다.
            def _confirm_and_apply() -> None:
                try:
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
                            st.get("exchange_fill_confirm_retry_interval_sec", 1.0)
                        )
                    except Exception:
                        retry_interval_sec = 1.0

                    max_attempts = max(2, min(max_attempts, 20))
                    retry_interval_sec = max(0.2, min(retry_interval_sec, 5.0))

                    for _i in range(max_attempts):
                        if oid and oid in self.app._exchange_fill_order_ids:
                            return

                        # 해당 일자 범위의 kt00007 체결 병합을 짧게 재시도합니다.
                        try:
                            import datetime as _dt

                            today = _dt.date.today()
                            om = getattr(self.app, "order_manager", None)
                            dm = getattr(self.app, "data_manager", None)
                            if om is not None and dm is not None:
                                om.sync_exchange_fills_into_database(
                                    dm,
                                    dates=[today],
                                    silent=True,
                                    logger=self.log,
                                )
                        except Exception:
                            pass

                        # order_no 기준 order_history 1건 확인
                        try:
                            row = self.app.data_manager.get_order_history_row_by_order_no(
                                oid
                            )
                        except Exception:
                            row = None

                        if row:
                            try:
                                res = (
                                    str(row.get("result", "") or "")
                                    .strip()
                                    .upper()
                                )
                            except Exception:
                                res = ""
                            try:
                                fqty = int(row.get("filled_qty", 0) or 0)
                            except Exception:
                                fqty = 0
                            try:
                                fp = float(row.get("filled_price", 0) or 0)
                            except Exception:
                                fp = 0.0

                            # FILLED 또는 filled_qty>0일 때만 확정 반영
                            if res == "FILLED" or (fqty > 0 and fp > 0):
                                if oid and oid in self.app._exchange_fill_order_ids:
                                    return

                                confirmed_price = (
                                    fp if fp > 0 else float(fill_price)
                                )
                                confirmed_qty = (
                                    fqty if fqty > 0 else int(qty)
                                )

                                try:
                                    _fs = capture_supply_snapshot(self.app, code)
                                except Exception:
                                    _fs = {"vol_power": None, "bid_ask_text": ""}

                                try:
                                    self.app.data_manager.update_order_status(
                                        code,
                                        order_no,
                                        "FILLED",
                                        filled_price=int(confirmed_price),
                                        filled_qty=int(confirmed_qty),
                                        fill_snap_vol_power=_fs.get("vol_power"),
                                        fill_snap_bid_ask=_fs.get(
                                            "bid_ask_text"
                                        )
                                        or "— / — (—)",
                                    )
                                except Exception:
                                    pass

                                snap_reason = self.app._resolve_fill_snapshot_reason(
                                    code, side, reason, channel="REST"
                                )
                                snapshot = self.app.portfolio.capture_snapshot(
                                    code, confirmed_price, reason=snap_reason
                                )

                                applied = self.app.portfolio.on_fill(
                                    code,
                                    side,
                                    confirmed_price,
                                    confirmed_qty,
                                    name=name or None,
                                    reason=reason or None,
                                    snapshot=snapshot,
                                    fill_channel="exchange",
                                    order_no=str(order_no or "").strip() or None,
                                )

                                if (
                                    not applied
                                    and str(side or "").lower() == "sell"
                                ):
                                    # 로컬 보유가 없어 on_fill 이 스킵된 매도: 종료내역 보강
                                    try:
                                        applied = self.app.portfolio.record_orphan_sell_close(
                                            code,
                                            float(confirmed_price),
                                            int(confirmed_qty),
                                            str(name or ""),
                                            str(reason or ""),
                                            str(order_no or ""),
                                            snapshot,
                                            "exchange",
                                        )
                                    except Exception:
                                        pass

                                if oid and applied:
                                    self.app._exchange_fill_order_ids.add(oid)

                                if applied:
                                    if oid and str(side or "").lower() in ("buy", "sell"):
                                        self._maybe_send_buy_sell_telegram(
                                            code, oid, side
                                        )

                                    try:
                                        self.log(
                                            f"✅ [체결 동기화] REST 접수 후 체결 확정 반영 {code} "
                                            f"{'매수' if str(side).lower() == 'buy' else '매도'} {confirmed_qty}주 "
                                            f"({int(confirmed_price):,}원)"
                                        )
                                    except Exception:
                                        pass

                                    try:
                                        tc = getattr(self.app, "trading_controller", None)
                                        if tc and oid:
                                            tc.limit_order_watch_service.clear(oid)
                                    except Exception:
                                        pass

                                    self.app._schedule_account_sync_after_fill()
                                return

                        # 아직 미확정이면 잠깐 대기 후 재시도
                        try:
                            import time as _time

                            _time.sleep(retry_interval_sec)
                        except Exception:
                            return

                    # max_attempts 초과: FILLED 확인 실패
                    self.log(
                        f"⚠️ [체결 동기화] REST 접수(order_no={oid}) 체결 확정 실패 — FILLED 반영을 보류"
                    )
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

    def on_execution_received(
        self,
        code: str,
        order_no: str,
        qty: int,
        price: float,
        side: str,
    ) -> None:
        """
        웹소켓 CHE 처리:
        - 이미 REST 동기화가 on_fill까지 완료했던 경우(oid in _exchange_fill_order_ids)는
          DB 체결가 갱신 + 계좌 동기화만 수행하고 on_fill은 생략
        - 그렇지 않으면 DB 갱신 + portfolio.on_fill / (필요 시) 종료내역 보강 수행
        """
        try:
            oid = str(order_no or "").strip()
            if oid and oid in self.app._exchange_fill_order_ids:
                try:
                    try:
                        _fs = capture_supply_snapshot(self.app, code)
                    except Exception:
                        _fs = {"vol_power": None, "bid_ask_text": ""}

                    self.app.data_manager.update_order_status(
                        code,
                        order_no,
                        "FILLED",
                        filled_price=price,
                        filled_qty=qty,
                        fill_snap_vol_power=_fs.get("vol_power"),
                        fill_snap_bid_ask=_fs.get("bid_ask_text")
                        or "— / — (—)",
                    )
                except Exception as ue:
                    self.log(f"⚠️ [체결 보정] DB 갱신 실패: {ue}")

                self.log(
                    f"ℹ️ [체결 보정] CHE 체결가 반영 {code} {price:,}원 (주문번호 {oid})"
                )
                self.app._schedule_account_sync_after_fill()
                return

            try:
                try:
                    _fs = capture_supply_snapshot(self.app, code)
                except Exception:
                    _fs = {"vol_power": None, "bid_ask_text": ""}

                self.app.data_manager.update_order_status(
                    code,
                    order_no,
                    "FILLED",
                    filled_price=price,
                    filled_qty=qty,
                    fill_snap_vol_power=_fs.get("vol_power"),
                    fill_snap_bid_ask=_fs.get("bid_ask_text")
                    or "— / — (—)",
                )
            except Exception as ue:
                self.log(f"⚠️ [체결] DB 갱신 실패: {ue}")

            snap_reason = self.app._resolve_fill_snapshot_reason(
                code, side, None, channel="EXEC"
            )
            snapshot = self.app.portfolio.capture_snapshot(
                code, price, reason=snap_reason
            )
            applied = self.app.portfolio.on_fill(
                code,
                side,
                price,
                qty,
                snapshot=snapshot,
                fill_channel="exchange",
                order_no=oid or None,
            )

            # 로컬 보유가 없어 on_fill 이 스킵된 매도: 주문 로그는 있는데 종료내역만 누락되는 경우 보강
            if (not applied and str(side or "").lower() == "sell"):
                meta: Optional[Dict[str, Any]] = None
                try:
                    meta = self.app.data_manager.get_sell_order_meta_by_order_no(
                        oid
                    )
                except Exception:
                    meta = None
                try:
                    nm = str((meta or {}).get("name") or "")
                    rs = str((meta or {}).get("reason") or "")
                except Exception:
                    nm, rs = "", ""

                try:
                    applied = self.app.portfolio.record_orphan_sell_close(
                        code,
                        float(price),
                        int(qty),
                        nm,
                        rs,
                        oid,
                        snapshot,
                        "exchange",
                    )
                except Exception as oe:
                    self.log(f"⚠️ [체결] 종료내역 보강 실패: {oe}")

            if applied:
                # 텔레그램 전송 전에 중복 방지 set에 oid를 먼저 넣어
                # REST/WS 경로가 엇갈리는 극단 케이스에서 2회 전송 레이스를 제거합니다.
                if oid and applied:
                    self.app._exchange_fill_order_ids.add(oid)
                if oid and str(side or "").lower() in ("buy", "sell"):
                    self._maybe_send_buy_sell_telegram(code, oid, side)

                self.log(
                    f"✅ [체결] {code} {'매수' if side == 'buy' else '매도'} {qty}주 완료 ({price:,}원)"
                )
            elif str(side or "").lower() == "sell":
                self.log(
                    f"⚠️ [체결] CHE 매도는 포트폴리오에 미반영({code}) "
                    f"— 로컬 보유 없음·종료내역 보강도 실패. 주문 로그를 확인하세요."
                )

            # 위에서 already add

            try:
                tc = getattr(self.app, "trading_controller", None)
                if tc and oid:
                    tc.limit_order_watch_service.clear(oid)
            except Exception:
                pass

            self.app._schedule_account_sync_after_fill()
        except Exception as e:
            self.log(f"⚠️ [체결] 처리 실패: {e}")

