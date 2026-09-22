# -*- coding: utf-8 -*-
"""체결 후 계좌요약 텔레그램 발송 서비스 (GUI 앱 위임)."""
from __future__ import annotations

import threading
from typing import Any, Optional


class AccountSummaryTelegramService:
    """KiwoomApp 의 계좌요약 텔레그램 스케줄·워커."""

    def __init__(self, app: Any) -> None:
        self.app = app

    def build_message(self, header: str) -> Optional[str]:
        """포지션관리 상단 계좌 요약 vars → 텔레그램 HTML 본문."""
        try:
            vt = getattr(self.app, "virtual_tab", None)
            if vt is None:
                return None
            total = str(
                (vt.vars.get("total").get() if vt.vars.get("total") else "") or ""
            ).strip()
            d2_cash = str(
                (vt.vars.get("d2_cash").get() if vt.vars.get("d2_cash") else "") or ""
            ).strip()
            stock = str(
                (vt.vars.get("stock").get() if vt.vars.get("stock") else "") or ""
            ).strip()
            open_ur = str(
                (vt.vars.get("open_ur").get() if vt.vars.get("open_ur") else "") or ""
            ).strip()
            day = str(
                (vt.vars.get("day").get() if vt.vars.get("day") else "") or ""
            ).strip()
            if not any([total, d2_cash, stock, open_ur, day]):
                return None
            return (
                f"📌 <b>[계좌요약]</b>\n{header}\n"
                f"총자산: {total}\n"
                f"D+2예수금: {d2_cash}\n"
                f"주식평가금: {stock}\n"
                f"미실현 손익: {open_ur}\n"
                f"금일손익: {day}"
            )
        except Exception:
            return None

    def start_worker(self, *, header: str, seq: int) -> None:
        """force 계좌 동기화 후 UI 요약으로 텔레그램 발송."""
        app = self.app
        try:
            root = getattr(app, "root", None)
            if root is None:
                return
        except Exception:
            return

        def _worker() -> None:
            acquired = False
            try:
                acquired = app._portfolio_sync_thread_lock.acquire(timeout=120.0)
                if acquired:
                    app.portfolio.sync_with_actual_account(
                        silent=True,
                        force=True,
                        refresh_cash_deposit=True,
                    )
            except Exception:
                pass
            finally:
                if acquired:
                    try:
                        app._portfolio_sync_thread_lock.release()
                    except Exception:
                        pass

            def _after() -> None:
                try:
                    if seq != app._account_summary_after_trade_seq:
                        return
                except Exception:
                    pass
                try:
                    if hasattr(app, "virtual_tab"):
                        app.virtual_tab.refresh_portfolio_light()
                except Exception:
                    pass
                msg = self.build_message(header=header)
                if msg:
                    app.send_telegram_message(msg, parse_mode="HTML")

            try:
                root.after(0, _after)
            except Exception:
                pass

        try:
            threading.Thread(target=_worker, daemon=True).start()
        except Exception:
            pass

    def schedule_after_trade(self, *, delay_sec: int = 12) -> None:
        """체결 확정 후 delay_sec 뒤 최신 1회만 계좌요약 텔레그램."""
        app = self.app
        try:
            root = getattr(app, "root", None)
            if root is None:
                return
        except Exception:
            return
        try:
            delay_ms = max(0, int(delay_sec * 1000))
        except Exception:
            delay_ms = 12 * 1000

        app._account_summary_after_trade_seq += 1
        seq = app._account_summary_after_trade_seq
        try:
            if app._account_summary_after_trade_job_id is not None:
                try:
                    root.after_cancel(app._account_summary_after_trade_job_id)
                except Exception:
                    pass
        except Exception:
            pass

        def _job() -> None:
            try:
                if seq != app._account_summary_after_trade_seq:
                    return
                self.start_worker(header="체결 후 계좌정보 동기화", seq=seq)
            except Exception:
                pass

        try:
            app._account_summary_after_trade_job_id = root.after(delay_ms, _job)
        except Exception:
            pass
