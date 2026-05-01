# -*- coding: utf-8 -*-
from typing import Any, Optional

import tkinter as tk
from tkinter import messagebox


class UiTradingStatusService:
    """
    TradingController status_callback 이벤트를 UI 갱신으로 연결합니다.

    목적:
    - `k_trader_gui.py`의 거대 오케스트레이터 책임을 줄이고
      상태 이벤트 처리 로직을 한 곳으로 모으기 위함
    """

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print

    def _safe_call(self, fn: Any, *args: Any, **kwargs: Any) -> None:
        try:
            if callable(fn):
                fn(*args, **kwargs)
        except Exception:
            pass

    def on_trading_status_changed(self, status: str, data: Any) -> None:
        """
        TradingController -> Tk UI 이벤트 브리지.

        status 종류는 `k_trader_gui.py` 기존 `_on_trading_status_changed` 분기와 동일하게 유지합니다.
        """
        try:
            if status == "SHOW_WARNING":
                messagebox.showwarning("경고", data)
            elif status == "STATUS_TEXT":
                if hasattr(self.app, "lbl_monitor_status"):
                    try:
                        self.app.lbl_monitor_status.config(
                            text=data,
                            foreground=self.app.colors["primary"],
                        )
                    except Exception:
                        pass
            elif status == "TARGETS_LOADED":
                try:
                    self.app.monitor_codes = (
                        self.app.trading_controller.monitor_codes
                    )
                    self.app.monitor_info = dict(
                        self.app.trading_controller.monitor_info
                        or {}
                    )
                except Exception:
                    pass
                if hasattr(self.app, "monitor_tab"):
                    self._safe_call(
                        self.app.monitor_tab.update_watchlist,
                        self.app.monitor_info,
                    )
            elif status == "WARMUP_BEGIN":
                codes = data if isinstance(data, list) else []
                if hasattr(self.app, "monitor_tab"):
                    self._safe_call(
                        self.app.monitor_tab.reset_warmup_row_states, codes
                    )
            elif status == "WARMUP_FAILED":
                results = (data or {}).get("results", {})
                failed = (data or {}).get("failed", [])
                lines = []
                for c in failed:
                    r = results.get(c, {})
                    mi = getattr(self.app, "monitor_info", None) or {}
                    nm = mi.get(c, {}).get("name", c)
                    lines.append(
                        f"• {nm} ({c}): {r.get('reason', '알 수 없음')}"
                    )
                body = (
                    "다음 종목의 15분봉·장중 구간을 API·DB로 채우지 못해 감시를 시작할 수 없습니다.\n\n"
                    + "\n".join(lines)
                )
                self._safe_call(
                    self.app.root.after,
                    0,
                    lambda msg=body: messagebox.showerror("워밍업 실패", msg),
                )
                if getattr(self.app, "lbl_monitor_status", None):
                    try:
                        self.app.lbl_monitor_status.config(
                            text="상태: 워밍업 실패 — 감시 미시작",
                            foreground=self.app.colors["danger"],
                        )
                    except Exception:
                        pass
            elif status == "MONITOR_CODES_FILTERED":
                self.app.monitor_codes = list((data or {}).get("codes", []))
                try:
                    self.app.trading_controller.monitor_codes = self.app.monitor_codes
                except Exception:
                    pass
                if hasattr(self.app, "monitor_tab"):
                    self._safe_call(
                        self.app.monitor_tab.update_watchlist,
                        self.app.monitor_info,
                    )
            elif status == "STARTED":
                self.app.is_monitoring = True
                try:
                    self.app.monitor_info = dict(
                        self.app.trading_controller.monitor_info or {}
                    )
                except Exception:
                    pass
                try:
                    self.app._start_eod_equity_snapshot_scheduler()
                except Exception:
                    pass

                if getattr(self.app, "btn_monitor_toggle", None):
                    try:
                        self.app.btn_monitor_toggle.config(
                            text="⏹️ 감시 중단",
                            style="Warning.TButton",
                        )
                    except Exception:
                        pass
                if getattr(self.app, "lbl_monitor_status", None):
                    try:
                        self.app.lbl_monitor_status.config(
                            text=f"상태: 감시 중 ({data}종목)",
                            foreground=self.app.colors["success"],
                        )
                    except Exception:
                        pass
                if getattr(self.app, "status_label", None):
                    try:
                        self.app.status_label.config(
                            text=f"상태: 감시 중 ({data}종목)",
                            foreground=self.app.colors["success"],
                        )
                    except Exception:
                        pass
                self._safe_call(self.app._render_tree_updates)
            elif status == "STOPPED":
                self.app.is_monitoring = False
                self.app._active_stream_codes = []
                self._safe_call(self.app._maybe_stop_account_sync_timer)
                if getattr(self.app, "btn_monitor_toggle", None):
                    try:
                        self.app.btn_monitor_toggle.config(
                            text="▶️ 실시간 감시 시작",
                            style="Accent.TButton",
                        )
                    except Exception:
                        pass
                if getattr(self.app, "lbl_monitor_status", None):
                    try:
                        self.app.lbl_monitor_status.config(
                            text="상태: 대기 중",
                            foreground=self.app.colors["primary"],
                        )
                    except Exception:
                        pass
                if getattr(self.app, "status_label", None):
                    try:
                        self.app.status_label.config(
                            text="상태: 대기 중",
                            foreground=self.app.colors["primary"],
                        )
                    except Exception:
                        pass
        except Exception as e:
            try:
                self.log(f"⚠️ [UI] on_trading_status_changed 실패: {e}")
            except Exception:
                pass

