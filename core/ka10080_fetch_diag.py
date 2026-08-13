# -*- coding: utf-8 -*-
"""
ka10080(1분·15분) REST 조회 실패·성공 통계 — 대시보드·session JSONL.

실패 빈도·원인(HTTP 429/502, 네트워크, 빈 응답, row_not_found 등) 분석용.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any, Callable, DefaultDict, Dict, Optional

from utils.normalization import canonical_stock_code

# 모듈 싱글톤 — ``k_trader_gui`` 기동 시 ``configure`` 로 UI·세션 로거 연결
ka10080_fetch_diag = None  # type: ignore  # 아래 클래스 인스턴스로 덮어씀


class Ka10080FetchDiag:
    """ka10080 호출 진단(누적 카운트 + JSONL + 스로틀 UI)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._session_logger: Any = None
        self._ui_log: Optional[Callable[[str], None]] = None
        self._settings: Dict[str, Any] = {}
        self._fail_total: int = 0
        self._ok_total: int = 0
        self._fail_by_reason: DefaultDict[str, int] = defaultdict(int)
        self._fail_by_context: DefaultDict[str, int] = defaultdict(int)
        self._fail_by_code: DefaultDict[str, int] = defaultdict(int)
        self._ui_throttle_at: Dict[str, float] = {}
        self._summary_last_mono: float = 0.0
        # UI 요약은 「신규 실패」가 있을 때만 — 누적 FAIL 을 매 슬롯 반복 출력하지 않음
        self._ui_summary_last_fail_total: int = 0
        self._summary_last_ok: int = 0
        self._summary_last_fail: int = 0

    def configure(
        self,
        *,
        session_logger: Any = None,
        ui_log: Optional[Callable[[str], None]] = None,
        settings: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            if session_logger is not None:
                self._session_logger = session_logger
            if ui_log is not None:
                self._ui_log = ui_log
            if settings is not None:
                self._settings = dict(settings)

    def _ui_throttle_sec(self) -> float:
        try:
            v = float(
                self._settings.get("realtime_ka10080_fail_log_ui_throttle_sec", 90.0)
            )
        except (TypeError, ValueError):
            v = 90.0
        return max(15.0, min(v, 600.0))

    def _summary_interval_sec(self) -> float:
        try:
            v = float(
                self._settings.get("realtime_ka10080_fail_summary_interval_sec", 300.0)
            )
        except (TypeError, ValueError):
            v = 300.0
        return max(60.0, min(v, 3600.0))

    def snapshot_stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "ok_total": int(self._ok_total),
                "fail_total": int(self._fail_total),
                "fail_by_reason": dict(self._fail_by_reason),
                "fail_by_context": dict(self._fail_by_context),
                "fail_by_code_top": self._top_codes(12),
            }

    def _top_codes(self, n: int) -> Dict[str, int]:
        items = sorted(
            self._fail_by_code.items(), key=lambda x: (-x[1], x[0])
        )[: max(1, n)]
        return {k: int(v) for k, v in items}

    def record_ok(
        self,
        code: str,
        *,
        tic_scope: str = "1",
        context: str = "",
    ) -> None:
        try:
            nk = canonical_stock_code(str(code).strip())
        except Exception:
            nk = str(code or "").strip()
        with self._lock:
            self._ok_total += 1
        self._maybe_emit_summary(force=False)

    def record_fail(
        self,
        code: str,
        *,
        tic_scope: str = "1",
        context: str = "",
        reason: str = "unknown",
        http_status: int = 0,
        detail: str = "",
        closed_minute: str = "",
        attempt: int = 0,
        return_code: Any = None,
        return_msg: str = "",
        body_bytes: int = 0,
        body_keys: Any = None,
    ) -> None:
        """
        실패 1건 기록.

        Parameters
        ----------
        context
            ``m1_poll`` | ``rt15m_official`` | ``warmup`` | ``bt_hp_bridge`` | ...
        reason
            ``http_429`` | ``http_502`` | ``http_other`` | ``network_error`` |
            ``empty_response`` | ``row_not_found`` | ``no_token`` | ...
        return_code / return_msg
            키움 JSON 본문 코드·메시지(empty_response·429 구분용).
        """
        try:
            nk = canonical_stock_code(str(code).strip())
        except Exception:
            nk = str(code or "").strip()
        ctx = str(context or "unknown").strip() or "unknown"
        rsn = str(reason or "unknown").strip() or "unknown"

        payload: Dict[str, Any] = {
            "code": nk,
            "tic_scope": str(tic_scope or "1"),
            "context": ctx,
            "reason": rsn,
            "http_status": int(http_status or 0),
            "detail": str(detail or "")[:500],
            "closed_minute": str(closed_minute or ""),
            "attempt": int(attempt or 0),
        }
        try:
            if return_code is not None and return_code != "":
                payload["return_code"] = return_code
        except Exception:
            pass
        try:
            rm = str(return_msg or "").strip()
            if rm:
                payload["return_msg"] = rm[:300]
        except Exception:
            pass
        try:
            bb = int(body_bytes or 0)
            if bb > 0:
                payload["body_bytes"] = bb
        except Exception:
            pass
        try:
            if body_keys is not None:
                if isinstance(body_keys, (list, tuple)):
                    payload["body_keys"] = [str(x) for x in list(body_keys)[:24]]
                else:
                    payload["body_keys"] = str(body_keys)[:200]
        except Exception:
            pass
        stats_snap: Dict[str, Any]

        with self._lock:
            self._fail_total += 1
            self._fail_by_reason[rsn] += 1
            self._fail_by_context[ctx] += 1
            if nk:
                self._fail_by_code[nk] += 1
            stats_snap = self.snapshot_stats()

        # JSONL: 분석용 — 스로틀 없이 매 실패 기록
        try:
            sl = self._session_logger
            if sl is not None and hasattr(sl, "log"):
                row = dict(payload)
                row["stats"] = stats_snap
                sl.log("ka10080_fetch_fail", row)
        except Exception:
            pass

        # 대시보드: 동일 (context, code, reason) 은 스로틀
        throttle_key = f"{ctx}|{nk}|{rsn}"
        now_m = time.monotonic()
        emit_ui = True
        with self._lock:
            last = float(self._ui_throttle_at.get(throttle_key, 0.0) or 0.0)
            if now_m - last < self._ui_throttle_sec():
                emit_ui = False
            else:
                self._ui_throttle_at[throttle_key] = now_m

        if emit_ui and self._ui_log is not None:
            try:
                ts = str(tic_scope or "1")
                ctx_kr = {"m1_poll": "M1", "rt15m_official": "15m확정"}.get(ctx, ctx)
                hp = f" HTTP{http_status}" if http_status else ""
                cm = f" 분={closed_minute}" if closed_minute else ""
                rc_s = ""
                try:
                    if return_code is not None and return_code != "":
                        rc_s = f" rc={return_code}"
                except Exception:
                    rc_s = ""
                msg_s = ""
                try:
                    rm = str(return_msg or "").strip()
                    if rm:
                        # 대시보드는 짧게
                        msg_s = f" msg={rm[:80]}"
                except Exception:
                    msg_s = ""
                self._ui_log(
                    f"⚠️ [ka10080/{ctx_kr}] {nk} 실패 {rsn}{hp}{rc_s}{msg_s}{cm} "
                    f"(누적 실패 {stats_snap.get('fail_total', 0)}건)"
                )
            except Exception:
                pass

        self._maybe_emit_summary(force=False)

    def _maybe_emit_summary(self, *, force: bool) -> None:
        """
        주기 요약.

        - session JSONL: 간격마다(또는 신규 실패 시) 누적·구간 델타 기록
        - UI: **신규 실패가 늘었을 때만** 출력 (누적 FAIL 을 매 15분 반복 출력하지 않음)
        """
        now_m = time.monotonic()
        with self._lock:
            fail_n = int(self._fail_total)
            ok_n = int(self._ok_total)
            ui_fail_delta = max(0, fail_n - int(self._ui_summary_last_fail_total))
            interval_ok = bool(force) or (
                (now_m - self._summary_last_mono) >= self._summary_interval_sec()
            )
            # 신규 실패면 간격과 무관하게 요약 허용
            if not interval_ok and ui_fail_delta <= 0:
                return
            if not force and fail_n <= 0 and ui_fail_delta <= 0:
                return
            self._summary_last_mono = now_m
            ok_delta = max(0, ok_n - int(self._summary_last_ok))
            fail_delta = max(0, fail_n - int(self._summary_last_fail))
            self._summary_last_ok = ok_n
            self._summary_last_fail = fail_n
            snap = {
                "ok_total": ok_n,
                "fail_total": fail_n,
                "ok_delta": int(ok_delta),
                "fail_delta": int(fail_delta),
                "fail_by_reason": dict(self._fail_by_reason),
                "fail_by_context": dict(self._fail_by_context),
                "fail_by_code_top": self._top_codes(12),
            }

        try:
            sl = self._session_logger
            if sl is not None and hasattr(sl, "log"):
                sl.log("ka10080_fetch_stats", snap)
        except Exception:
            pass

        # UI: 신규 실패가 있을 때만
        if self._ui_log is None or ui_fail_delta <= 0:
            return
        try:
            by_ctx = snap.get("fail_by_context") or {}
            by_rsn = snap.get("fail_by_reason") or {}
            ctx_s = ", ".join(f"{k}:{v}" for k, v in sorted(by_ctx.items()))
            rsn_s = ", ".join(f"{k}:{v}" for k, v in sorted(by_rsn.items()))
            self._ui_log(
                f"ℹ️ [ka10080 통계] 신규 FAIL +{ui_fail_delta} "
                f"(누적 OK {snap.get('ok_total', 0)} / FAIL {snap.get('fail_total', 0)}) | "
                f"경로 [{ctx_s}] | 원인 [{rsn_s}]"
            )
            with self._lock:
                self._ui_summary_last_fail_total = int(snap.get("fail_total", 0) or 0)
        except Exception:
            pass


ka10080_fetch_diag = Ka10080FetchDiag()
