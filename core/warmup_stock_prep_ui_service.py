# -*- coding: utf-8 -*-
"""
Warmup 워커 스레드 이벤트를 Tk 메인 스레드 UI로 브리지합니다.

ka10080 페이지마다 after(0)를 쌓으면 메인 루프가 멈춰 보이므로
prep_progress 만 스로틀·병합합니다. prep_ok/fail/start 는 한 칸에
덮어쓰지 않고 큐에 남겨, 보유 종목 신호상태가
「15분봉 API 1/40페이지」에 멈추지 않게 합니다.
"""
from __future__ import annotations

import threading
import time
from typing import Any, List, Optional, Tuple

PrepArgs = Tuple[Any, ...]


class WarmupStockPrepUiService:
    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print
        self._lock = threading.Lock()
        self._event_q: List[PrepArgs] = []
        self._pending_progress: Optional[PrepArgs] = None
        self._after_id: Optional[Any] = None
        self._last_apply_ts: float = 0.0
        self._progress_min_interval_sec: float = 0.35

    def _norm_code(self, code: Any) -> str:
        try:
            return str(code).split("_")[0].zfill(6)
        except Exception:
            return ""

    def _enqueue(self, args: PrepArgs, *, immediate: bool) -> None:
        ev = str(args[0] or "") if args else ""
        cd = self._norm_code(args[4] if len(args) > 4 else "")
        with self._lock:
            if immediate:
                self._event_q.append(args)
                if ev in ("prep_ok", "prep_fail") and self._pending_progress is not None:
                    try:
                        pcd = self._norm_code(self._pending_progress[4])
                    except Exception:
                        pcd = ""
                    if pcd and pcd == cd:
                        self._pending_progress = None
            else:
                self._pending_progress = args

    def _drain(self) -> List[PrepArgs]:
        """메인 스레드에서 적용할 이벤트. ok/fail 이후 같은 종목 progress 는 버림."""
        with self._lock:
            q = list(self._event_q)
            self._event_q = []
            prog = self._pending_progress
            self._pending_progress = None
        done: set[str] = set()
        out: List[PrepArgs] = []
        try:
            for payload in q:
                out.append(payload)
                try:
                    ev = str(payload[0] or "")
                    cd = self._norm_code(payload[4] if len(payload) > 4 else "")
                    if ev in ("prep_ok", "prep_fail") and cd:
                        done.add(cd)
                except Exception:
                    pass
            if prog is not None:
                try:
                    pcd = self._norm_code(prog[4] if len(prog) > 4 else "")
                except Exception:
                    pcd = ""
                if (not pcd) or (pcd not in done):
                    out.append(prog)
        except Exception:
            return q if q else ([prog] if prog is not None else [])
        return out

    def _apply_one(self, payload: PrepArgs) -> None:
        try:
            ev, c_i, t_i, nm, cd, rs = payload
        except Exception:
            return
        try:
            if hasattr(self.app, "monitor_tab"):
                self.app.monitor_tab.update_warmup_stock_prep(
                    ev, c_i, t_i, nm, cd, rs
                )
        except Exception:
            pass
        try:
            if hasattr(self.app, "lbl_monitor_status"):
                if ev == "prep_ok":
                    pass
                elif ev == "prep_progress" and (rs or "").strip():
                    line = f"상태: ({c_i}/{t_i}) {nm} — {str(rs).strip()}"
                    self.app.lbl_monitor_status.config(
                        text=line, foreground=self.app.colors["primary"]
                    )
                else:
                    line = f"상태: 데이터 준비 중 ({c_i}/{t_i}) {nm}"
                    self.app.lbl_monitor_status.config(
                        text=line, foreground=self.app.colors["primary"]
                    )
        except Exception:
            pass
        try:
            if hasattr(self.app, "status_label") and self.app.status_label:
                if ev == "prep_ok":
                    pass
                elif ev == "prep_progress" and (rs or "").strip():
                    self.app.status_label.config(
                        text=f"상태: ({c_i}/{t_i}) {nm} — {str(rs).strip()}"
                    )
                else:
                    self.app.status_label.config(
                        text=f"상태: 데이터 준비 ({c_i}/{t_i}) {nm}"
                    )
        except Exception:
            pass

    def on_warmup_stock_prep(
        self,
        event: str,
        cur: int,
        total: int,
        name: str,
        code: str,
        reason: str = "",
    ) -> None:
        """워커 스레드 → 메인 스레드 UI 갱신."""
        args: PrepArgs = (event, cur, total, name, code, reason)
        try:
            immediate = str(event or "") != "prep_progress"
        except Exception:
            immediate = True

        def update() -> None:
            try:
                self._after_id = None
                batch = self._drain()
                if not batch:
                    return
                for payload in batch:
                    try:
                        self._apply_one(payload)
                    except Exception:
                        continue
                try:
                    self._last_apply_ts = time.time()
                except Exception:
                    pass
            except Exception as e:
                try:
                    self.log(f"⚠️ [UI] 워밍업 표시 갱신 실패: {e}")
                except Exception:
                    pass

        try:
            self._enqueue(args, immediate=immediate)
        except Exception:
            return

        try:
            r = getattr(self.app, "root", None)
            if r is None:
                update()
                return

            if immediate:
                try:
                    if self._after_id is not None:
                        r.after_cancel(self._after_id)
                except Exception:
                    pass
                self._after_id = None
                r.after(0, update)
                return

            if self._after_id is not None:
                return
            try:
                elapsed = time.time() - float(self._last_apply_ts or 0.0)
            except Exception:
                elapsed = 999.0
            if elapsed < float(self._progress_min_interval_sec):
                delay_ms = int(
                    max(
                        50.0,
                        (float(self._progress_min_interval_sec) - elapsed) * 1000.0,
                    )
                )
            else:
                delay_ms = 50
            self._after_id = r.after(delay_ms, update)
        except Exception:
            try:
                update()
            except Exception:
                pass
