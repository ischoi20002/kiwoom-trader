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

            try:
                try:
                    _fs = capture_supply_snapshot(self.app, code)
                except Exception:
                    _fs = {"vol_power": None, "bid_ask_text": ""}

                self.app.data_manager.update_order_status(
                    code,
                    order_no,
                    "FILLED",
                    filled_price=int(fill_price),
                    filled_qty=int(qty),
                    fill_snap_vol_power=_fs.get("vol_power"),
                    fill_snap_bid_ask=_fs.get("bid_ask_text")
                    or "— / — (—)",
                )
            except Exception as ue:
                self.log(f"⚠️ [체결 동기화] DB 갱신 실패: {ue}")

            snap_reason = self.app._resolve_fill_snapshot_reason(
                code, side, reason, channel="REST"
            )
            snapshot = self.app.portfolio.capture_snapshot(
                code, fill_price, reason=snap_reason
            )
            applied = self.app.portfolio.on_fill(
                code,
                side,
                fill_price,
                qty,
                name=name or None,
                reason=reason or None,
                snapshot=snapshot,
                fill_channel="exchange",
                order_no=str(order_no or "").strip() or None,
            )

            # 로컬 보유가 없어 on_fill 이 스킵된 매도: 주문 로그는 있는데 종료내역만 누락되는 경우 보강
            if not applied and str(side or "").lower() == "sell":
                try:
                    applied = self.app.portfolio.record_orphan_sell_close(
                        code,
                        float(fill_price),
                        int(qty),
                        str(name or ""),
                        str(reason or ""),
                        str(order_no or ""),
                        snapshot,
                        "exchange",
                    )
                except Exception as oe:
                    self.log(
                        f"⚠️ [체결 동기화] 종료내역 보강 실패: {oe}"
                    )

            # on_fill이 매도 스킵(False)인데 여기서 oid를 먼저 넣으면, 뒤따른 CHE가 on_fill을 영원히 못 타
            # 매매종료내역 누락으로 이어질 수 있음 — 실제 반영(True)일 때만 중복 마킹
            if oid and applied:
                self.app._exchange_fill_order_ids.add(oid)

            if applied:
                self.log(
                    f"✅ [체결 동기화] REST 접수 직후 가상체결 {code} "
                    f"{'매수' if side == 'buy' else '매도'} {qty}주 ({int(fill_price):,}원)"
                )
            elif str(side or "").lower() == "sell":
                self.log(
                    f"⚠️ [체결 동기화] REST 매도 체결은 포트폴리오에 미반영({code}) "
                    f"— 로컬 보유 없음·종료내역 보강도 실패. 주문 로그와 DB를 확인하세요."
                )

            try:
                tc = getattr(self.app, "trading_controller", None)
                if tc and oid:
                    tc.limit_order_watch_service.clear(oid)
            except Exception:
                pass

            self.app._schedule_account_sync_after_fill()
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
                self.log(
                    f"✅ [체결] {code} {'매수' if side == 'buy' else '매도'} {qty}주 완료 ({price:,}원)"
                )
            elif str(side or "").lower() == "sell":
                self.log(
                    f"⚠️ [체결] CHE 매도는 포트폴리오에 미반영({code}) "
                    f"— 로컬 보유 없음·종료내역 보강도 실패. 주문 로그를 확인하세요."
                )

            if oid and applied:
                self.app._exchange_fill_order_ids.add(oid)

            try:
                tc = getattr(self.app, "trading_controller", None)
                if tc and oid:
                    tc.limit_order_watch_service.clear(oid)
            except Exception:
                pass

            self.app._schedule_account_sync_after_fill()
        except Exception as e:
            self.log(f"⚠️ [체결] 처리 실패: {e}")

