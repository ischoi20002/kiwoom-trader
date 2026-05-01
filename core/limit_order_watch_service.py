# -*- coding: utf-8 -*-
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set


@dataclass(frozen=True)
class LimitOrderMeta:
    """미체결 지정가 타임아웃 예약 메타(단일 주문번호 기준)."""

    code: str
    kind: str
    after_id: Any
    qty: int = 0


class LimitOrderWatchService:
    """
    지정가 미체결 타임아웃 감시/취소/플래그 정리 서비스.

    - 등록: `root.after(ms, cb)` 로 타임아웃 콜백 예약
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
                    "limit_order_unfilled_timeout_sec", 5.0
                )
            )
        except (TypeError, ValueError):
            sec = 5.0
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

    def register(self, *, order_no: str, code: str, kind: str, qty: int = 0) -> None:
        """지정가 접수 후 미체결 타임아웃 감시 예약."""
        oid = str(order_no or "").strip()
        nc = str(code).zfill(6)
        if not oid or not nc:
            return

        # 같은 주문번호는 중복 예약 방지
        self.clear(oid)

        root = self._get_root()
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
            self.log(f"⏱️ [지정가] 미체결 감시 등록 ({kind}) {nc} ord={oid} / {ms // 1000}초")
        except Exception:
            pass

        after_id = root.after(ms, _cb)
        meta = LimitOrderMeta(code=nc, kind=kind, after_id=after_id, qty=int(qty))

        with self._lock:
            self.limit_order_meta[oid] = meta

        if kind == "s1_buy":
            self.s1_buy_pending_codes.add(nc)

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

        ok = False
        try:
            ok = bool(self.app.order_manager.cancel_order(oid, nc, "0"))
        except Exception as e:
            try:
                self.log(f"⚠️ [미체결타임아웃] 취소 API 예외: {e}")
            except Exception:
                pass

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
                    },
                )
        except Exception:
            pass

        # kind별 플래그 정리 (TradingController 기존 동작 그대로)
        if kind == "exit_sell":
            try:
                with self.app.portfolio.lock:
                    p = self.app.portfolio.virtual_positions.get(nc)
                    if p:
                        p["exit_pending"] = False
                        p.pop("pending_exit_ord_no", None)
            except Exception:
                pass
            try:
                self.log(f"⏱️ [미체결타임아웃] 매도 취소 시도({ok}) — 종목 {nc} 전략 재평가 가능")
            except Exception:
                pass

        elif kind == "s1_buy":
            self.s1_buy_pending_codes.discard(nc)
            try:
                self.log(f"⏱️ [미체결타임아웃] S1 매수 취소 시도({ok}) — 종목 {nc} 재신호 가능")
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
            try:
                self.log(f"⏱️ [미체결타임아웃] S2 매수 취소 시도({ok}) — 종목 {nc} 재조건 시 재추매 가능")
            except Exception:
                pass

