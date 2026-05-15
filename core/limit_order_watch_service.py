# -*- coding: utf-8 -*-
import threading
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Optional, Set


@dataclass(frozen=True)
class LimitOrderMeta:
    """미체결 지정가 타임아웃 예약 메타(단일 주문번호 기준)."""

    code: str
    kind: str
    after_id: Any
    qty: int = 0
    # False: 시장가 등 — 타임아웃 시 취소 API 없음(체결 시 clear()로만 정리)
    cancel_on_timeout: bool = True


class LimitOrderWatchService:
    """
    지정가 미체결 타임아웃 감시/취소/플래그 정리 서비스.

    - 등록: `root.after(ms, cb)` 로 타임아웃 콜백 예약(지정가)
    - 시장가: 메타만 등록하고 타이머·취소 API는 생략 — 체결 시 `clear()` 로 플래그 정리
    - 취소(체결): `clear(order_no)` 로 예약 타이머 취소 + 상태 정리
    - 타임아웃: `timeout(order_no)` 로 취소 API 호출 + 상태 정리
    """

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print

        # 상태: (TradingController 내부에 있던 것) -> 서비스로 이동
        self.limit_order_meta: Dict[str, LimitOrderMeta] = {}
        # S1 지정가 접수 후 체결 전, 동일 종목 중복 매수 방지
        self.s1_buy_pending_codes: Set[str] = set()

        # timer 콜백과 clear 동시 경합 완화
        self._lock = threading.RLock()

    def _get_root(self) -> Optional[Any]:
        try:
            return getattr(self.app, "root", None)
        except Exception:
            return None

    def _get_timeout_ms(self) -> int:
        try:
            sec = float(
                (getattr(self.app, "settings", None) or {}).get(
                    "limit_order_unfilled_timeout_sec", 45.0
                )
            )
        except (TypeError, ValueError):
            sec = 45.0
        sec = max(2.0, min(sec, 120.0))
        return int(sec * 1000)

    def _normalize_code_6(self, code: Any) -> str:
        try:
            return str(code).zfill(6)
        except Exception:
            return "000000"

    def clear(self, order_no: Optional[str]) -> None:
        """주문번호로 예약 타이머 취소 + 관련 상태 플래그 정리."""
        if not order_no:
            return
        oid = str(order_no).strip()
        if not oid:
            return

        with self._lock:
            meta = self.limit_order_meta.pop(oid, None)

        if meta is None:
            return

        root = self._get_root()
        if root is not None:
            aid = getattr(meta, "after_id", None)
            if aid is not None:
                try:
                    root.after_cancel(aid)
                except Exception:
                    pass

        kind = meta.kind
        code = meta.code
        nc = code

        # kind별 플래그 정리 (TradingController 기존 동작 그대로)
        if kind == "s1_buy" and nc:
            self.s1_buy_pending_codes.discard(nc)
        if kind == "s2_buy" and nc:
            try:
                with self.app.portfolio.lock:
                    p2 = self.app.portfolio.virtual_positions.get(nc)
                    if p2:
                        p2.pop("s2_limit_order_pending", None)
                        p2.pop("s2_pending_ord_no", None)
            except Exception:
                pass

    def register(
        self,
        *,
        order_no: str,
        code: str,
        kind: str,
        qty: int = 0,
        cancel_on_timeout: bool = True,
    ) -> None:
        """주문 접수 후 감시 등록. 지정가는 미체결 타임아웃·취소 API, 시장가는 플래그만(취소 없음)."""
        oid = str(order_no or "").strip()
        nc = str(code).zfill(6)
        if not oid or not nc:
            return

        # 같은 주문번호는 중복 예약 방지
        self.clear(oid)

        root = self._get_root()
        after_id: Any = None
        if cancel_on_timeout:
            if root is None:
                return
            ms = self._get_timeout_ms()

            def _cb() -> None:
                try:
                    self.timeout(oid)
                except Exception as e:
                    try:
                        self.log(f"⚠️ [미체결타임아웃] 처리 오류: {e}")
                    except Exception:
                        pass

            try:
                self.log(
                    f"⏱️ [지정가] 미체결 감시 등록 ({kind}) {nc} ord={oid} / {ms // 1000}초"
                )
            except Exception:
                pass

            after_id = root.after(ms, _cb)
        else:
            try:
                self.log(
                    f"⏱️ [시장가] 접수 감시 등록 ({kind}) {nc} ord={oid} "
                    f"(미체결 취소 타이머 없음 — 체결 시 정리)"
                )
            except Exception:
                pass

        meta = LimitOrderMeta(
            code=nc,
            kind=kind,
            after_id=after_id,
            qty=int(qty),
            cancel_on_timeout=bool(cancel_on_timeout),
        )

        with self._lock:
            self.limit_order_meta[oid] = meta

        if kind == "s1_buy":
            self.s1_buy_pending_codes.add(nc)

    def _schedule_boost_fill_reconcile(
        self, *, order_no: str, code: str, kind: str, qty: int
    ) -> None:
        """
        취소 거부(미체결 없음) 직후: 주문 로그 행 기준으로 kt00007 재폴링을 강하게 돌립니다.
        체결가·수량은 DB/kt00007 확정 후에만 on_fill 됩니다.
        """
        oid = str(order_no or "").strip()
        nc = self._normalize_code_6(code)
        if not oid or not nc:
            return
        try:
            dm = getattr(self.app, "data_manager", None)
            if dm is None:
                return
            row = dm.get_order_history_row_by_order_no(oid)
        except Exception:
            row = None
        if not isinstance(row, dict):
            try:
                self.log(
                    f"ℹ️ [지정가] 강화 체결동기화 생략 — 주문로그 없음(ord={oid}, {nc})"
                )
            except Exception:
                pass
            return
        try:
            c = str(row.get("code") or nc).split("_")[0].zfill(6)
            side = str(row.get("side") or "").strip().lower()
            if side not in ("buy", "sell"):
                side = "sell" if kind == "exit_sell" else "buy"
            try:
                q = int(row.get("qty") or qty or 0)
            except Exception:
                q = int(qty or 0)
            try:
                px = float(row.get("price") or 0)
            except Exception:
                px = 0.0
            nm = str(row.get("name") or "")
            rs = str(row.get("reason") or "")
        except Exception:
            return
        if q <= 0 or px <= 0:
            try:
                self.log(
                    f"ℹ️ [지정가] 강화 체결동기화 생략 — 수량/가격 부족(ord={oid})"
                )
            except Exception:
                pass
            return

        def _run() -> None:
            try:
                self.app.reconcile_exchange_fill_after_order_accept(
                    c,
                    oid,
                    side,
                    int(q),
                    float(px),
                    nm,
                    rs,
                    boost_polling=True,
                )
            except Exception as ex:
                try:
                    self.log(f"⚠️ [지정가] 강화 체결동기화 예약 실패: {ex}")
                except Exception:
                    pass

        try:
            root = self._get_root()
            if root is not None and hasattr(root, "after"):
                root.after(0, _run)
            else:
                _run()
        except Exception:
            try:
                _run()
            except Exception:
                pass

    def _schedule_exit_sell_cancel_recovery(
        self,
        *,
        code: str,
        cancelled_ord_no: str,
        order_qty_hint: int,
    ) -> None:
        """
        매도 지정가가 미체결 타임아웃으로 취소된 직후 처리.

        부분 체결 뒤 잔량만 취소되는 경우, 증권사 매도가능수량은 이미 줄었는데
        로컬 VP 수량·exit_pending 해제 타이밍이 어긋나면 곧바로 전량 매도를 재시도해
        800033(매도가능수량 부족)이 난다. kt00007 병합 후 잔고 동기화로 수량을 맞춘 뒤
        exit_pending 을 해제한다.
        """
        nc = self._normalize_code_6(code)
        oid = str(cancelled_ord_no or "").strip()
        if not nc or not oid:
            return
        hint_q = int(order_qty_hint or 0)

        def _on_main_finish() -> None:
            try:
                self._schedule_boost_fill_reconcile(
                    order_no=oid,
                    code=nc,
                    kind="exit_sell",
                    qty=hint_q,
                )
            except Exception:
                pass
            try:
                with self.app.portfolio.lock:
                    p = self.app.portfolio.virtual_positions.get(nc)
                    if p:
                        p["exit_pending"] = False
                        p.pop("pending_exit_ord_no", None)
            except Exception:
                pass
            try:
                self.log(
                    f"ℹ️ [미체결타임아웃] 매도 취소 후 잔고·주문로그 동기화 완료 — "
                    f"종목 {nc} 재매도 판단 가능"
                )
            except Exception:
                pass

        def _bg_sync() -> None:
            try:
                om = getattr(self.app, "order_manager", None)
                dm = getattr(self.app, "data_manager", None)
                if om is not None and dm is not None:
                    om.sync_exchange_fills_into_database(
                        dm,
                        dates=[date.today()],
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
                root = self._get_root()
                if root is not None and hasattr(root, "after"):
                    root.after(0, _on_main_finish)
                else:
                    _on_main_finish()
            except Exception:
                try:
                    _on_main_finish()
                except Exception:
                    pass

        try:
            threading.Thread(target=_bg_sync, daemon=True).start()
        except Exception as ex:
            try:
                self.log(
                    f"⚠️ [미체결타임아웃] 매도 취소 후 동기화 스레드 실패({ex}) — "
                    f"플래그만 즉시 해제합니다(잔고 괴리 가능)."
                )
            except Exception:
                pass
            try:
                with self.app.portfolio.lock:
                    p = self.app.portfolio.virtual_positions.get(nc)
                    if p:
                        p["exit_pending"] = False
                        p.pop("pending_exit_ord_no", None)
            except Exception:
                pass

    def timeout(self, order_no: str) -> None:
        """미체결 타임아웃: 취소 API 호출 후 대기 플래그 해제."""
        if not order_no:
            return
        oid = str(order_no).strip()
        if not oid:
            return

        with self._lock:
            meta = self.limit_order_meta.pop(oid, None)

        if meta is None:
            return

        nc = meta.code
        kind = meta.kind

        cancel_st = "failed"
        try:
            cancel_st = str(
                self.app.order_manager.cancel_order(oid, nc, "0") or "failed"
            )
        except Exception as e:
            try:
                self.log(f"⚠️ [미체결타임아웃] 취소 API 예외: {e}")
            except Exception:
                pass

        ok = cancel_st == "cancelled"
        no_open = cancel_st == "no_open_qty"

        # 주문 로그(세션) 기록
        try:
            sl = getattr(self.app, "session_logger", None)
            if sl:
                sl.log(
                    "limit_order_unfilled_timeout",
                    {
                        "ord_no": oid,
                        "code": nc,
                        "kind": kind,
                        "cancel_ok": ok,
                        "cancel_st": cancel_st,
                    },
                )
        except Exception:
            pass

        # kind별 플래그 정리 (TradingController 기존 동작 그대로)
        if kind == "exit_sell":
            if no_open:
                # 전량 체결 가능성 — exit_pending 유지(중복 매도 방지), 강화 체결동기화
                try:
                    self._schedule_boost_fill_reconcile(
                        order_no=oid, code=nc, kind=kind, qty=int(meta.qty or 0)
                    )
                except Exception:
                    pass
                try:
                    self.log(
                        f"⏱️ [미체결타임아웃] 매도 취소 생략(미체결 없음) — "
                        f"종목 {nc} 체결동기화 강화(ord={oid}), exit_pending 유지"
                    )
                except Exception:
                    pass
            else:
                try:
                    self.log(
                        f"⏱️ [미체결타임아웃] 매도 취소 시도({ok}) — "
                        f"종목 {nc} 잔고 동기화 후 재매도 판단"
                    )
                except Exception:
                    pass
                try:
                    self._schedule_exit_sell_cancel_recovery(
                        code=nc,
                        cancelled_ord_no=oid,
                        order_qty_hint=int(meta.qty or 0),
                    )
                except Exception as ex:
                    try:
                        self.log(
                            f"⚠️ [미체결타임아웃] 매도 취소 후 동기화 예약 실패({ex}) — "
                            f"플래그만 즉시 해제"
                        )
                    except Exception:
                        pass
                    try:
                        with self.app.portfolio.lock:
                            p = self.app.portfolio.virtual_positions.get(nc)
                            if p:
                                p["exit_pending"] = False
                                p.pop("pending_exit_ord_no", None)
                    except Exception:
                        pass

        elif kind == "s1_buy":
            self.s1_buy_pending_codes.discard(nc)
            if no_open:
                try:
                    self._schedule_boost_fill_reconcile(
                        order_no=oid, code=nc, kind=kind, qty=int(meta.qty or 0)
                    )
                except Exception:
                    pass
            try:
                self.log(
                    f"⏱️ [미체결타임아웃] S1 매수 취소 시도({ok}, no_open={no_open}) "
                    f"— 종목 {nc} 재신호 가능"
                )
            except Exception:
                pass

        elif kind == "s2_buy":
            try:
                with self.app.portfolio.lock:
                    p = self.app.portfolio.virtual_positions.get(nc)
                    if p:
                        p.pop("s2_limit_order_pending", None)
                        p.pop("s2_pending_ord_no", None)
            except Exception:
                pass
            if no_open:
                try:
                    self._schedule_boost_fill_reconcile(
                        order_no=oid, code=nc, kind=kind, qty=int(meta.qty or 0)
                    )
                except Exception:
                    pass
            try:
                self.log(
                    f"⏱️ [미체결타임아웃] S2 매수 취소 시도({ok}, no_open={no_open}) "
                    f"— 종목 {nc} 재조건 시 재추매 가능"
                )
            except Exception:
                pass

