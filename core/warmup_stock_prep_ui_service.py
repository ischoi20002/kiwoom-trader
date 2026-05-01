# -*- coding: utf-8 -*-
from typing import Any, Optional


class WarmupStockPrepUiService:
    """
    Warmup 워커 스레드 이벤트를 Tk 메인 스레드 UI로 브리지합니다.

    - TradingController status 이벤트와 별개로,
      `WarmupService.run(..., on_stock_ready=...)` 내부에서 종목별 prep 상태를
      `stock_prep_callback`으로 내려주는 흐름을 정리합니다.
    """

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print

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
        def update() -> None:
            try:
                if hasattr(self.app, "monitor_tab"):
                    self.app.monitor_tab.update_warmup_stock_prep(
                        event, cur, total, name, code, reason
                    )

                # prep_ok 는 종목 행만 갱신 — 상단 라벨은 덮지 않음
                if hasattr(self.app, "lbl_monitor_status"):
                    if event == "prep_ok":
                        pass
                    elif event == "prep_progress" and (reason or "").strip():
                        line = (
                            f"상태: ({cur}/{total}) {name} — {reason.strip()}"
                        )
                        self.app.lbl_monitor_status.config(
                            text=line, foreground=self.app.colors["primary"]
                        )
                    else:
                        line = f"상태: 데이터 준비 중 ({cur}/{total}) {name}"
                        self.app.lbl_monitor_status.config(
                            text=line, foreground=self.app.colors["primary"]
                        )

                if hasattr(self.app, "status_label") and self.app.status_label:
                    if event == "prep_ok":
                        pass
                    elif event == "prep_progress" and (reason or "").strip():
                        self.app.status_label.config(
                            text=f"상태: ({cur}/{total}) {name} — {reason.strip()}"
                        )
                    else:
                        self.app.status_label.config(
                            text=f"상태: 데이터 준비 ({cur}/{total}) {name}"
                        )
            except Exception as e:
                try:
                    self.log(f"⚠️ [UI] 워밍업 표시 갱신 실패: {e}")
                except Exception:
                    pass

        try:
            r = getattr(self.app, "root", None)
            if r is not None:
                r.after(0, update)
            else:
                update()
        except Exception:
            try:
                update()
            except Exception:
                pass

