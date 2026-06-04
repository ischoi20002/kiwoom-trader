# -*- coding: utf-8 -*-
"""
주문별 체결 확인·증분 포트폴리오 반영 단일 오케스트레이터.

CHE(WS)·kt00007(REST)·폴링 경로가 공통으로 ``apply_exchange_delta`` 를 호출합니다.
"""
from __future__ import annotations

import datetime as _dt
import threading
import time
from typing import Any, Dict, Literal, Optional, Set

from core.supply_display import capture_supply_snapshot


class FillConfirmationService:
    """주문번호 단위 체결 상태·증분 on_fill 집행."""

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print
        self._lock = threading.RLock()
        self._kt00007_sync_lock = threading.Lock()
        self._kt00007_last_sync_ts: float = 0.0
        self._kt00007_order_last_sync_ts: Dict[str, float] = {}

    # ------------------------------------------------------------------ #
    # 상태 조회
    # ------------------------------------------------------------------ #

    def get_state(self, order_no: str) -> Optional[Dict[str, Any]]:
        try:
            dm = getattr(self.app, "data_manager", None)
            if dm is None:
                return None
            return dm.get_order_fill_state(str(order_no or "").strip())
        except Exception:
            return None

    def get_applied_qty(self, order_no: str) -> int:
        st = self.get_state(order_no)
        if not isinstance(st, dict):
            return 0
        try:
            return int(st.get("applied_qty", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def get_order_qty(self, order_no: str) -> int:
        st = self.get_state(order_no)
        if isinstance(st, dict):
            try:
                oq = int(st.get("order_qty", 0) or 0)
                if oq > 0:
                    return oq
            except (TypeError, ValueError):
                pass
        try:
            dm = getattr(self.app, "data_manager", None)
            if dm is None:
                return 0
            row = dm.get_order_history_row_by_order_no(str(order_no or "").strip())
            if isinstance(row, dict):
                return int(row.get("qty", 0) or 0)
        except Exception:
            pass
        return 0

    def get_exchange_filled_qty(self, order_no: str) -> int:
        """거래소·DB 기준 누적 체결 수량."""
        st = self.get_state(order_no)
        if isinstance(st, dict):
            try:
                ef = int(st.get("exchange_filled_qty", 0) or 0)
                if ef > 0:
                    return ef
            except (TypeError, ValueError):
                pass
        try:
            dm = getattr(self.app, "data_manager", None)
            if dm is not None:
                return int(dm.sum_order_history_filled_qty(str(order_no or "").strip()) or 0)
        except Exception:
            pass
        return 0

    def is_fully_applied(self, order_no: str) -> bool:
        oid = str(order_no or "").strip()
        if not oid:
            return False
        oq = self.get_order_qty(oid)
        if oq <= 0:
            try:
                if oid in getattr(self.app, "_exchange_fill_order_ids", set()):
                    return True
            except Exception:
                pass
            return False
        return self.get_applied_qty(oid) >= oq

    def has_pending_confirmations(self) -> bool:
        """당일 접수 주문 중 exchange_filled > applied 또는 inflight 폴링 존재."""
        try:
            inflight: Set[str] = getattr(self.app, "_exchange_fill_order_inflight", set())
            if inflight:
                for oid in inflight:
                    if not self.is_fully_applied(str(oid)):
                        return True
        except Exception:
            pass
        try:
            dm = getattr(self.app, "data_manager", None)
            if dm is None:
                return False
            pending = dm.list_order_fill_states_pending_today()
            return bool(pending)
        except Exception:
            return False

    def _mark_complete_if_fully_applied(self, order_no: str) -> None:
        oid = str(order_no or "").strip()
        if not oid:
            return
        if not self.is_fully_applied(oid):
            return
        try:
            self.app._exchange_fill_order_ids.add(oid)
        except Exception:
            pass

    def ensure_submitted_state(
        self,
        order_no: str,
        *,
        code: str,
        side: str,
        order_qty: int,
        name: str = "",
        reason: str = "",
    ) -> None:
        """접수 직후 order_fill_state 행을 준비합니다."""
        try:
            dm = getattr(self.app, "data_manager", None)
            if dm is None:
                return
            dm.upsert_order_fill_state_submitted(
                str(order_no or "").strip(),
                code=str(code or "").zfill(6),
                side=str(side or "").lower(),
                order_qty=int(order_qty or 0),
                name=str(name or ""),
                reason=str(reason or ""),
            )
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # kt00007 동기화
    # ------------------------------------------------------------------ #

    def _kt00007_global_min_interval_sec(self) -> float:
        try:
            st = getattr(self.app, "settings", None) or {}
            v = float(st.get("exchange_fill_kt00007_sync_min_interval_sec", 5.0))
            return max(1.5, min(v, 30.0))
        except Exception:
            return 5.0

    def _kt00007_per_order_min_interval_sec(self) -> float:
        try:
            st = getattr(self.app, "settings", None) or {}
            v = float(st.get("exchange_fill_kt00007_per_order_min_sec", 1.2))
            return max(0.4, min(v, 5.0))
        except Exception:
            return 1.2

    def sync_kt00007(
        self,
        *,
        order_no: Optional[str] = None,
        force: bool = False,
    ) -> int:
        """
        kt00007 → order_history 병합.
        order_no 지정 시 per-order 긴급 경로(전역 스로틀 bypass).
        """
        oid = str(order_no or "").strip()
        today = _dt.date.today()
        try:
            now = time.time()
            if oid:
                min_iv = self._kt00007_per_order_min_interval_sec()
                if not force:
                    last = float(self._kt00007_order_last_sync_ts.get(oid, 0.0) or 0.0)
                    if (now - last) < min_iv:
                        return 0
            else:
                min_iv = self._kt00007_global_min_interval_sec()
                if not force:
                    if (now - float(self._kt00007_last_sync_ts or 0.0)) < min_iv:
                        return 0

            with self._kt00007_sync_lock:
                now = time.time()
                if oid:
                    if not force:
                        last = float(
                            self._kt00007_order_last_sync_ts.get(oid, 0.0) or 0.0
                        )
                        if (now - last) < min_iv:
                            return 0
                else:
                    if not force:
                        if (now - float(self._kt00007_last_sync_ts or 0.0)) < min_iv:
                            return 0

                om = getattr(self.app, "order_manager", None)
                dm = getattr(self.app, "data_manager", None)
                if om is None or dm is None:
                    return 0

                if oid:
                    added = int(
                        om.sync_exchange_fills_for_order_no(
                            dm,
                            oid,
                            dates=[today],
                            silent=True,
                            logger=self.log,
                        )
                        or 0
                    )
                    self._kt00007_order_last_sync_ts[oid] = time.time()
                else:
                    added = int(
                        om.sync_exchange_fills_into_database(
                            dm,
                            dates=[today],
                            silent=True,
                            logger=self.log,
                        )
                        or 0
                    )
                    self._kt00007_last_sync_ts = time.time()
                return added
        except Exception:
            return 0

    # ------------------------------------------------------------------ #
    # 증분 반영
    # ------------------------------------------------------------------ #

    def _resolve_exchange_cumulative_qty(
        self, order_no: str, row: Optional[Dict[str, Any]]
    ) -> int:
        oid = str(order_no or "").strip()
        try:
            dm = getattr(self.app, "data_manager", None)
            if dm is not None:
                s = int(dm.sum_order_history_filled_qty(oid) or 0)
                if s > 0:
                    return s
        except Exception:
            pass
        if isinstance(row, dict):
            try:
                return int(row.get("filled_qty", 0) or 0)
            except (TypeError, ValueError):
                pass
        return self.get_exchange_filled_qty(oid)

    def apply_exchange_delta(
        self,
        *,
        code: str,
        order_no: str,
        side: str,
        delta_qty: int,
        fill_price: float,
        name: str = "",
        reason: str = "",
        channel: Literal["CHE", "REST"] = "REST",
        update_db_status: bool = True,
        cumulative_filled_qty: Optional[int] = None,
    ) -> bool:
        """
        delta_qty 만큼 portfolio.on_fill 을 호출하고 applied_qty 를 갱신합니다.
        Returns True if any qty was applied to portfolio.
        """
        oid = str(order_no or "").strip()
        if not oid or delta_qty <= 0:
            return False

        try:
            dq = int(delta_qty)
        except (TypeError, ValueError):
            return False

        try:
            fp = float(fill_price)
        except (TypeError, ValueError):
            fp = 0.0
        if fp <= 0:
            return False

        side_l = str(side or "").strip().lower()
        if side_l not in ("buy", "sell"):
            return False

        nc = str(code or "").split("_")[0].zfill(6)

        try:
            if self.get_state(oid) is None:
                oq = self.get_order_qty(oid)
                if oq <= 0 and cumulative_filled_qty:
                    oq = int(cumulative_filled_qty)
                self.ensure_submitted_state(
                    oid,
                    code=nc,
                    side=side_l,
                    order_qty=int(oq or 0),
                    name=str(name or ""),
                    reason=str(reason or ""),
                )
        except Exception:
            pass

        if update_db_status:
            try:
                cum = cumulative_filled_qty
                if cum is None:
                    cum = self.get_applied_qty(oid) + dq
                _fs = capture_supply_snapshot(self.app, nc)
                self.app.data_manager.update_order_status(
                    nc,
                    oid,
                    "FILLED" if self.is_fully_applied(oid) or (
                        self.get_applied_qty(oid) + dq >= self.get_order_qty(oid) > 0
                    ) else "PARTIAL",
                    filled_price=int(fp),
                    filled_qty=int(cum),
                    fill_snap_vol_power=_fs.get("vol_power"),
                    fill_snap_bid_ask=_fs.get("bid_ask_text") or "— / — (—)",
                )
            except Exception:
                pass

        snap_reason = self.app._resolve_fill_snapshot_reason(
            nc, side_l, reason or None, channel=channel
        )
        snapshot = self.app.portfolio.capture_snapshot(
            nc, fp, reason=snap_reason
        )

        applied = self.app.portfolio.on_fill(
            nc,
            side_l,
            fp,
            dq,
            name=name or None,
            reason=reason or None,
            snapshot=snapshot,
            fill_channel="exchange",
            order_no=oid,
        )

        if not applied and side_l == "sell":
            try:
                pm = self.app.portfolio
                if hasattr(pm, "is_redundant_late_exchange_sell_fill") and pm.is_redundant_late_exchange_sell_fill(
                    nc, oid
                ):
                    try:
                        self.log(
                            f"ℹ️ [체결] 매도 증분 생략({nc}) — "
                            f"동기화·주문로그 반영 완료(중복 방지)"
                        )
                    except Exception:
                        pass
                    try:
                        dm = getattr(self.app, "data_manager", None)
                        if dm is not None:
                            dm.increment_order_fill_applied_qty(oid, dq)
                    except Exception:
                        pass
                    self._mark_complete_if_fully_applied(oid)
                    return False
                applied = pm.record_orphan_sell_close(
                    nc,
                    fp,
                    dq,
                    str(name or ""),
                    str(reason or ""),
                    oid,
                    snapshot,
                    "exchange",
                )
            except Exception:
                pass

        if applied:
            try:
                dm = getattr(self.app, "data_manager", None)
                if dm is not None:
                    ex_cum = cumulative_filled_qty
                    if ex_cum is None:
                        ex_cum = self.get_exchange_filled_qty(oid) + dq
                    dm.increment_order_fill_applied_qty(
                        oid, dq, exchange_filled_qty=int(ex_cum)
                    )
            except Exception:
                pass

            if oid and side_l in ("buy", "sell"):
                try:
                    frs = getattr(self.app, "fill_reconciliation_service", None)
                    if frs is not None and hasattr(frs, "_maybe_send_buy_sell_telegram"):
                        if self.is_fully_applied(oid):
                            frs._maybe_send_buy_sell_telegram(nc, oid, side_l)
                except Exception:
                    pass

            try:
                ch = "CHE" if channel == "CHE" else "REST"
                self.log(
                    f"✅ [체결/{ch}] {nc} "
                    f"{'매수' if side_l == 'buy' else '매도'} +{dq}주 "
                    f"(적용누적 {self.get_applied_qty(oid)}/{self.get_order_qty(oid)}주, "
                    f"{int(fp):,}원)"
                )
            except Exception:
                pass

            self._clear_limit_watch_if_done(nc, oid, side_l)
            self._mark_complete_if_fully_applied(oid)
            self._schedule_account_sync_after_fill_if_ready()
            return True

        try:
            self.log(
                f"🧪 [체결증분실패] code={nc} ord={oid} side={side_l} delta={dq} "
                f"price={fp} applied_qty={self.get_applied_qty(oid)}"
            )
        except Exception:
            pass
        return False

    def ingest_che(
        self,
        code: str,
        order_no: str,
        qty: int,
        price: float,
        side: str,
    ) -> bool:
        """WS CHE 1건 — 이벤트 수량을 증분 반영."""
        oid = str(order_no or "").strip()
        if not oid:
            return False
        try:
            event_qty = int(qty or 0)
        except (TypeError, ValueError):
            event_qty = 0
        if event_qty <= 0:
            return False

        if self.is_fully_applied(oid):
            try:
                _fs = capture_supply_snapshot(self.app, code)
                self.app.data_manager.update_order_status(
                    code,
                    oid,
                    "FILLED",
                    filled_price=int(price),
                    filled_qty=int(self.get_exchange_filled_qty(oid)),
                    fill_snap_vol_power=_fs.get("vol_power"),
                    fill_snap_bid_ask=_fs.get("bid_ask_text") or "— / — (—)",
                )
                self.log(
                    f"ℹ️ [체결 보정] CHE DB만 갱신 {code} (주문 {oid}, 이미 전량 반영)"
                )
            except Exception:
                pass
            self._schedule_account_sync_after_fill_if_ready()
            return False

        try:
            dm = getattr(self.app, "data_manager", None)
            if dm is not None:
                ex_new = self.get_exchange_filled_qty(oid) + event_qty
                dm.increment_order_fill_exchange_qty(oid, event_qty)
        except Exception:
            ex_new = None

        meta = None
        nm, rs = "", ""
        try:
            row = self.app.data_manager.get_order_history_row_by_order_no(oid)
            if isinstance(row, dict):
                nm = str(row.get("name") or "")
                rs = str(row.get("reason") or "")
        except Exception:
            pass
        if not nm and str(side or "").lower() == "sell":
            try:
                meta = self.app.data_manager.get_sell_order_meta_by_order_no(oid)
                nm = str((meta or {}).get("name") or "")
                rs = str((meta or {}).get("reason") or rs)
            except Exception:
                pass

        return self.apply_exchange_delta(
            code=code,
            order_no=oid,
            side=side,
            delta_qty=event_qty,
            fill_price=float(price),
            name=nm,
            reason=rs,
            channel="CHE",
            cumulative_filled_qty=ex_new,
        )

    def ingest_rest_confirmed(
        self,
        *,
        code: str,
        order_no: str,
        side: str,
        qty: int,
        fill_price: float,
        name: str = "",
        reason: str = "",
        row: Dict[str, Any],
    ) -> bool:
        """kt00007/DB 확정 row — 누적 체결 대비 미반영분만 on_fill."""
        oid = str(order_no or "").strip()
        if not oid:
            return False
        if self.is_fully_applied(oid):
            return False

        exchange_cum = self._resolve_exchange_cumulative_qty(oid, row)
        if exchange_cum <= 0:
            try:
                exchange_cum = int(row.get("filled_qty", 0) or 0)
            except (TypeError, ValueError):
                exchange_cum = 0
        if exchange_cum <= 0:
            return False

        try:
            dm = getattr(self.app, "data_manager", None)
            if dm is not None:
                dm.set_order_fill_exchange_qty(oid, int(exchange_cum))
        except Exception:
            pass

        applied = self.get_applied_qty(oid)
        delta = int(exchange_cum) - int(applied)
        if delta <= 0:
            self._mark_complete_if_fully_applied(oid)
            return False

        try:
            fp = float(row.get("filled_price", 0) or 0)
        except (TypeError, ValueError):
            fp = 0.0
        if fp <= 0:
            fp = float(fill_price)

        return self.apply_exchange_delta(
            code=code,
            order_no=oid,
            side=side,
            delta_qty=delta,
            fill_price=fp,
            name=name,
            reason=reason,
            channel="REST",
            cumulative_filled_qty=int(exchange_cum),
        )

    def _clear_limit_watch_if_done(
        self, code: str, order_no: str, side: str
    ) -> None:
        try:
            frs = getattr(self.app, "fill_reconciliation_service", None)
            if frs is None:
                return
            tc = getattr(self.app, "trading_controller", None)
            if tc is None:
                return
            lws = tc.limit_order_watch_service
            if lws is None:
                return
            oid = str(order_no or "").strip()
            if str(side or "").lower() == "buy":
                lws.clear(oid)
            elif frs._should_clear_limit_order_watch_for_sell(code):
                lws.clear(oid)
        except Exception:
            pass

    def apply_trusted_fill_on_no_open_qty(
        self,
        *,
        code: str,
        order_no: str,
        side: str,
        qty: int,
        fill_price_hint: float,
        name: str = "",
        reason: str = "",
        kind: str = "",
    ) -> bool:
        """
        RC4033·no_open_qty: 증권사가 '미체결 없음'을 확인한 경우 — kt00007 대기 없이 잔여 수량 즉시 on_fill.

        모의투자에서 kt00007 반영이 수 초~수십 초 늦을 때 주기 동기화에만 의존하지 않도록 합니다.
        """
        oid = str(order_no or "").strip()
        nc = str(code or "").split("_")[0].zfill(6)
        side_l = str(side or "").strip().lower()
        if not oid or not nc or side_l not in ("buy", "sell"):
            return False

        if self.is_fully_applied(oid):
            self._release_exit_pending_if_sell(nc, oid, kind)
            return True

        oq = max(int(qty or 0), self.get_order_qty(oid))
        if oq <= 0:
            try:
                row = self.app.data_manager.get_order_history_row_by_order_no(oid)
                if isinstance(row, dict):
                    oq = int(row.get("qty", 0) or 0)
            except Exception:
                oq = 0
        if oq <= 0:
            return False

        applied = self.get_applied_qty(oid)
        delta = int(oq) - int(applied)
        if delta <= 0:
            if side_l == "sell":
                try:
                    with self.app.portfolio.lock:
                        pos = self.app.portfolio.virtual_positions.get(nc)
                        if isinstance(pos, dict):
                            rem = int(pos.get("qty", 0) or 0)
                            if rem > 0:
                                delta = rem
                except Exception:
                    pass
        if delta <= 0:
            self._release_exit_pending_if_sell(nc, oid, kind)
            return self.is_fully_applied(oid)

        px = float(fill_price_hint or 0)
        if px <= 0:
            try:
                row = self.app.data_manager.get_order_history_row_by_order_no(oid)
                if isinstance(row, dict):
                    px = float(
                        row.get("filled_price")
                        or row.get("price")
                        or 0
                    )
            except Exception:
                px = 0.0
        if px <= 0:
            return False

        try:
            self.log(
                f"✅ [체결/RC4033] {nc} "
                f"{'매수' if side_l == 'buy' else '매도'} +{delta}주 @ {int(px):,}원 "
                f"(증권사 미체결없음 — 즉시 반영, ord={oid})"
            )
        except Exception:
            pass

        ok = self.apply_exchange_delta(
            code=nc,
            order_no=oid,
            side=side_l,
            delta_qty=delta,
            fill_price=px,
            name=name,
            reason=reason,
            channel="REST",
            cumulative_filled_qty=int(oq),
        )
        if ok or self.is_fully_applied(oid):
            self._release_exit_pending_if_sell(nc, oid, kind)
            try:
                threading.Thread(
                    target=lambda: self.sync_kt00007(order_no=oid, force=True),
                    daemon=True,
                    name=f"Kt00007AfterRC4033-{oid}",
                ).start()
            except Exception:
                pass
            return True
        return False

    def confirm_after_no_open_qty(
        self,
        *,
        code: str,
        order_no: str,
        side: str,
        qty: int,
        fill_price_hint: float,
        name: str = "",
        reason: str = "",
        kind: str = "",
    ) -> bool:
        """
        RC4033·취소 no_open_qty — 거래소상 체결됐으나 kt00007/DB가 늦을 때 즉시 확정.

        짧은 간격 kt00007 재시도 → 실패 시 매도는 잔고 동기화·힌트가로 증분 on_fill.
        """
        oid = str(order_no or "").strip()
        nc = str(code or "").split("_")[0].zfill(6)
        side_l = str(side or "").strip().lower()
        if not oid or not nc or side_l not in ("buy", "sell"):
            return False

        if self.is_fully_applied(oid):
            self._release_exit_pending_if_sell(nc, oid, kind)
            return True

        try:
            st = getattr(self.app, "settings", None) or {}
            trust_first = bool(st.get("exchange_fill_trust_no_open_qty", True))
        except Exception:
            trust_first = True

        if trust_first and side_l == "sell":
            if self.apply_trusted_fill_on_no_open_qty(
                code=nc,
                order_no=oid,
                side=side_l,
                qty=int(qty or 0),
                fill_price_hint=float(fill_price_hint or 0),
                name=name,
                reason=reason,
                kind=kind,
            ):
                return True

        try:
            st = getattr(self.app, "settings", None) or {}
            max_a = int(float(st.get("exchange_fill_no_open_confirm_attempts", 8)))
            interval = float(st.get("exchange_fill_no_open_confirm_interval_sec", 0.8))
        except (TypeError, ValueError):
            max_a = 8
            interval = 0.8
        max_a = max(3, min(max_a, 12))
        interval = max(0.35, min(interval, 3.0))

        frs = getattr(self.app, "fill_reconciliation_service", None)
        oq = max(int(qty or 0), self.get_order_qty(oid))

        for i in range(max_a):
            if self.is_fully_applied(oid):
                self._release_exit_pending_if_sell(nc, oid, kind)
                return True

            self.sync_kt00007(order_no=oid, force=True)

            if frs is not None and hasattr(frs, "try_confirm_fill_once"):
                try:
                    status = frs.try_confirm_fill_once(
                        nc,
                        oid,
                        side_l,
                        oq,
                        float(fill_price_hint or 0),
                        name,
                        reason,
                        force_kt00007_sync=True,
                    )
                    if status in ("filled", "skipped"):
                        self._release_exit_pending_if_sell(nc, oid, kind)
                        return True
                    if status == "terminal":
                        break
                except Exception:
                    pass

            try:
                row = self.app.data_manager.get_order_history_row_by_order_no(oid)
            except Exception:
                row = None
            if isinstance(row, dict):
                try:
                    if self.ingest_rest_confirmed(
                        code=nc,
                        order_no=oid,
                        side=side_l,
                        qty=oq,
                        fill_price=float(fill_price_hint or 0),
                        name=name,
                        reason=reason,
                        row=row,
                    ):
                        if self.is_fully_applied(oid):
                            self._release_exit_pending_if_sell(nc, oid, kind)
                            return True
                except Exception:
                    pass

            if side_l == "sell" and i >= 0:
                if self._try_apply_sell_when_holdings_gone(
                    nc,
                    oid,
                    oq,
                    float(fill_price_hint or 0),
                    name,
                    reason,
                    kind,
                ):
                    return True

            if i + 1 < max_a:
                try:
                    time.sleep(interval)
                except Exception:
                    break

        if self.is_fully_applied(oid):
            self._release_exit_pending_if_sell(nc, oid, kind)
            return True
        return False

    def _try_apply_sell_when_holdings_gone(
        self,
        code: str,
        order_no: str,
        order_qty: int,
        fill_price_hint: float,
        name: str,
        reason: str,
        kind: str,
    ) -> bool:
        """kt00007 없을 때: REST 잔고 동기화 후 종목 미보유면 힌트가로 매도 증분."""
        try:
            self.app._run_portfolio_sync_in_daemon_thread(
                silent=True,
                force=True,
                refresh_cash_deposit=False,
                wait_for_lock=True,
                schedule_gui_refresh=False,
                schedule_orders_refresh=False,
            )
        except TypeError:
            try:
                self.app.portfolio.sync_with_actual_account(
                    silent=True,
                    force=True,
                    refresh_cash_deposit=False,
                    schedule_gui_refresh=False,
                    schedule_orders_refresh=False,
                )
            except Exception:
                pass
        except Exception:
            pass

        nc = str(code).zfill(6)
        oid = str(order_no or "").strip()
        try:
            with self.app.portfolio.lock:
                still = nc in (self.app.portfolio.virtual_positions or {})
        except Exception:
            still = True

        if still:
            return False

        delta = int(order_qty or 0) - int(self.get_applied_qty(oid))
        if delta <= 0:
            self._release_exit_pending_if_sell(nc, oid, kind)
            return self.is_fully_applied(oid)

        px = float(fill_price_hint or 0)
        if px <= 0:
            try:
                row = self.app.data_manager.get_order_history_row_by_order_no(oid)
                if isinstance(row, dict):
                    px = float(row.get("price") or row.get("filled_price") or 0)
            except Exception:
                px = 0.0
        if px <= 0:
            return False

        try:
            self.log(
                f"ℹ️ [체결/잔고] {nc} kt00007 지연 — 잔고 동기화로 매도 확정 시도 "
                f"(ord={oid}, {delta}주 @ {int(px):,}원)"
            )
        except Exception:
            pass

        ok = self.apply_exchange_delta(
            code=nc,
            order_no=oid,
            side="sell",
            delta_qty=delta,
            fill_price=px,
            name=name,
            reason=reason,
            channel="REST",
            update_db_status=True,
        )
        if ok or self.is_fully_applied(oid):
            self._release_exit_pending_if_sell(nc, oid, kind)
            return True
        return False

    def _release_exit_pending_if_sell(
        self, code: str, order_no: str, kind: str
    ) -> None:
        if str(kind or "") != "exit_sell":
            return
        nc = str(code).split("_")[0].zfill(6)
        try:
            with self.app.portfolio.lock:
                p = self.app.portfolio.virtual_positions.get(nc)
                if isinstance(p, dict):
                    p["exit_pending"] = False
                    p.pop("pending_exit_ord_no", None)
        except Exception:
            pass

    def _schedule_account_sync_after_fill_if_ready(self) -> None:
        """미확정 주문이 없을 때만 체결 후 계좌 동기화 예약(Phase 6)."""
        try:
            st = getattr(self.app, "settings", None) or {}
            if not bool(st.get("post_fill_sync_skip_while_pending", True)):
                self.app._schedule_account_sync_after_fill()
                return
            if self.has_pending_confirmations():
                try:
                    self.log(
                        "ℹ️ [체결 동기화] 미확정 주문 존재 — post-fill 계좌동기화 생략"
                    )
                except Exception:
                    pass
                return
            self.app._schedule_account_sync_after_fill()
        except Exception:
            try:
                self.app._schedule_account_sync_after_fill()
            except Exception:
                pass
