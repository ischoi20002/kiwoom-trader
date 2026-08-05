# -*- coding: utf-8 -*-
import time
from typing import Any, Optional


class WarmupStockPrepUiService:
    """
    Warmup 워커 스레드 이벤트를 Tk 메인 스레드 UI로 브리지합니다.

    - TradingController status 이벤트와 별개로,
      `WarmupService.run(..., on_stock_ready=...)` 내부에서 종목별 prep 상태를
      `stock_prep_callback`으로 내려주는 흐름을 정리합니다.
    - ka10080 페이지마다 after(0)를 쌓으면 메인 루프가 멈춰 보이므로
      prep_progress 는 스로틀·병합합니다.
    """

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print
        self._pending_args: Optional[tuple[Any, ...]] = None
        self._after_id: Optional[Any] = None
        self._last_apply_ts: float = 0.0
        # prep_progress 최소 간격(초). prep_ok/fail/start 는 즉시.
        self._progress_min_interval_sec: float = 0.35

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
        args = (event, cur, total, name, code, reason)
        try:
            immediate = str(event or "") != "prep_progress"
        except Exception:
            immediate = True

        def update() -> None:
            try:
                self._after_id = None
                payload = self._pending_args
                self._pending_args = None
                if payload is None:
                    return
                ev, c_i, t_i, nm, cd, rs = payload
                if hasattr(self.app, "monitor_tab"):
                    self.app.monitor_tab.update_warmup_stock_prep(
                        ev, c_i, t_i, nm, cd, rs
                    )

                # prep_ok 는 종목 행만 갱신 — 상단 라벨은 덮지 않음
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
            r = getattr(self.app, "root", None)
            if r is None:
                self._pending_args = args
                update()
                return

            self._pending_args = args
            if immediate:
                try:
                    if self._after_id is not None:
                        r.after_cancel(self._after_id)
                except Exception:
                    pass
                self._after_id = None
                r.after(0, update)
                return

            # prep_progress: 이미 예약되어 있으면 최신 args만 덮어씀(병합)
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
                self._pending_args = args
                update()
            except Exception:
                pass
