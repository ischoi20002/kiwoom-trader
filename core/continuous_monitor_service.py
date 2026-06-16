# -*- coding: utf-8 -*-
"""
연속 실시간 감시: 장마감 후 ``감시 중단 → 일일 선정 → 감시 재시작`` 자동 수행.

일반 「실시간 감시 시작」과 별도로 ``arm()`` 된 경우에만 동작합니다.
"""
from __future__ import annotations

import threading
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable, Optional


class ContinuousMonitorService:
    """장마감 이후 감시 종목 갱신·재기동 스케줄러."""

    def __init__(self, app: Any, *, log_func: Optional[Callable[[str], None]] = None) -> None:
        self.app = app
        self.log = log_func or print
        self.armed: bool = False
        self._after_id: Any = None
        self._cancelled: bool = False
        self._cycle_in_progress: bool = False
        self._last_cycle_trading_day: Optional[str] = None
        # EOD 사이클용 stop() 인지 — STOPPED 콜백에서 연속 모드 해제 방지
        self._eod_stop_for_cycle: bool = False

    def arm(self) -> None:
        """연속 감시 모드 활성화."""
        self.armed = True
        self._cancelled = False
        self.start_scheduler()
        try:
            self.log(
                "🔄 [연속감시] 활성화 — 장마감 후 자동으로 "
                "감시 중단 → 일일 선정 → 감시 재시작을 수행합니다."
            )
        except Exception:
            pass
        try:
            fn = getattr(self.app, "_sync_continuous_monitor_ui", None)
            if callable(fn):
                fn()
        except Exception:
            pass

    def disarm(self, *, manual: bool = True) -> None:
        """연속 감시 모드 해제."""
        self.armed = False
        self._cycle_in_progress = False
        self._eod_stop_for_cycle = False
        if manual:
            try:
                self.log("⏹️ [연속감시] 자동 전환을 해제했습니다.")
            except Exception:
                pass
        try:
            fn = getattr(self.app, "_sync_continuous_monitor_ui", None)
            if callable(fn):
                fn()
        except Exception:
            pass

    def shutdown(self) -> None:
        """앱 종료 시 after 예약 취소."""
        self._cancelled = True
        self.armed = False
        try:
            root = getattr(self.app, "root", None)
            if root is not None and self._after_id is not None:
                root.after_cancel(self._after_id)
        except Exception:
            pass
        self._after_id = None

    def start_scheduler(self) -> None:
        """30초 주기 tick 예약(중복 방지)."""
        if self._cancelled:
            return
        self._schedule_tick()

    def _schedule_tick(self) -> None:
        try:
            root = getattr(self.app, "root", None)
            if root is None:
                return
            if self._after_id is not None:
                try:
                    root.after_cancel(self._after_id)
                except Exception:
                    pass
            self._after_id = root.after(30_000, self.tick)
        except Exception:
            self._after_id = None

    def _eod_buffer_minutes(self) -> int:
        try:
            st = getattr(self.app, "settings", None) or {}
            v = int(
                float(
                    st.get(
                        "continuous_monitor_eod_buffer_minutes",
                        st.get("daily_report_session_buffer_minutes", 15),
                    )
                )
            )
            return max(5, min(v, 120))
        except (TypeError, ValueError):
            return 15

    def _stop_settle_ms(self) -> int:
        try:
            st = getattr(self.app, "settings", None) or {}
            v = int(float(st.get("continuous_monitor_stop_settle_sec", 5.0)))
            return max(2, min(v, 30)) * 1000
        except (TypeError, ValueError):
            return 5000

    @staticmethod
    def _trading_day_key(day_d: date) -> str:
        return day_d.strftime("%Y-%m-%d")

    def _resolve_eod_trading_day(self, now: datetime) -> Optional[date]:
        """오늘이 거래일이고 장마감+buffer 이후면 당일, 아니면 None."""
        try:
            from reporting.krx_trading_calendar import is_trading_day

            today_d = now.date()
            if not is_trading_day(today_d):
                return None
            due = datetime.combine(today_d, dtime(15, 30, 0)) + timedelta(
                minutes=self._eod_buffer_minutes()
            )
            if now >= due:
                return today_d
        except Exception:
            try:
                today_d = now.date()
                if today_d.weekday() >= 5:
                    return None
                due = datetime.combine(today_d, dtime(15, 30, 0)) + timedelta(
                    minutes=self._eod_buffer_minutes()
                )
                if now >= due:
                    return today_d
            except Exception:
                pass
        return None

    def tick(self) -> None:
        """30초마다 장마감 전환 조건 확인."""
        try:
            if self._cancelled:
                return
            self._schedule_tick()

            if not self.armed or self._cycle_in_progress:
                return

            tc = getattr(self.app, "trading_controller", None)
            if tc is None or not bool(getattr(tc, "is_monitoring", False)):
                return

            now = datetime.now()
            trade_day = self._resolve_eod_trading_day(now)
            if trade_day is None:
                return

            day_key = self._trading_day_key(trade_day)
            if self._last_cycle_trading_day == day_key:
                return

            self._begin_eod_cycle(day_key)
        except Exception as e:
            try:
                self.log(f"⚠️ [연속감시] 스케줄 tick 오류: {e}")
            except Exception:
                pass

    def _begin_eod_cycle(self, day_key: str) -> None:
        self._cycle_in_progress = True
        self._last_cycle_trading_day = day_key
        try:
            self.log(
                f"🔄 [연속감시] {day_key} 장마감 전환 시작 "
                f"(중단 → 일일 선정 → 재시작)"
            )
            sl = getattr(self.app, "session_logger", None)
            if sl is not None:
                sl.log("continuous_monitor_cycle_start", {"trading_day": day_key})
        except Exception:
            pass
        try:
            root = getattr(self.app, "root", None)
            if root is not None:
                root.after(0, self._phase_stop)
            else:
                self._phase_stop()
        except Exception:
            self._finish_cycle(False, "메인 스레드 예약 실패")

    def _phase_stop(self) -> None:
        """1단계: 감시 중단."""
        try:
            self._eod_stop_for_cycle = True
            tc = getattr(self.app, "trading_controller", None)
            if tc is not None and bool(getattr(tc, "is_monitoring", False)):
                try:
                    tc.stop()
                except Exception as e:
                    self.log(f"⚠️ [연속감시] 감시 중단 오류: {e}")
            settle = self._stop_settle_ms()
            root = getattr(self.app, "root", None)
            if root is not None:
                root.after(settle, self._phase_selection)
            else:
                self._phase_selection()
        except Exception as e:
            self._finish_cycle(False, f"중단 단계 오류: {e}")

    def _phase_selection(self) -> None:
        """2단계: 일일 감시목록 선정(FDR api_only)."""
        try:
            mt = getattr(self.app, "monitor_tab", None)
            if mt is None or not hasattr(mt, "select_monitoring_targets"):
                self._finish_cycle(False, "monitor_tab 없음")
                return

            def _on_complete(ok: bool) -> None:
                try:
                    root = getattr(self.app, "root", None)
                    if root is not None:
                        root.after(0, lambda o=ok: self._phase_restart(o))
                    else:
                        self._phase_restart(ok)
                except Exception as ex:
                    self._finish_cycle(False, f"선정 콜백 오류: {ex}")

            threading.Thread(
                target=lambda: mt.select_monitoring_targets(
                    auto_confirm=True,
                    on_complete=_on_complete,
                ),
                daemon=True,
                name="ContinuousMonitorSelection",
            ).start()
        except Exception as e:
            self._finish_cycle(False, f"선정 단계 오류: {e}")

    def _phase_restart(self, selection_ok: bool) -> None:
        """3단계: 워밍업 포함 감시 재시작."""
        try:
            self._eod_stop_for_cycle = False
            if not self.armed:
                self._finish_cycle(False, "연속 감시 해제됨")
                return
            if not selection_ok:
                self._finish_cycle(False, "일일 선정 실패 또는 종목 없음")
                return

            tc = getattr(self.app, "trading_controller", None)
            if tc is None:
                self._finish_cycle(False, "trading_controller 없음")
                return

            if bool(getattr(tc, "is_monitoring", False)):
                self._finish_cycle(True, "이미 감시 중")
                return

            if getattr(tc, "_run_start_sequence_active", False):
                self._finish_cycle(False, "감시 시작 절차가 이미 진행 중")
                return

            try:
                self.log("🔄 [연속감시] 일일 선정 완료 — 감시를 재시작합니다.")
            except Exception:
                pass

            started = bool(tc.start())
            if started:
                self._finish_cycle(True, "재시작 요청 완료")
            else:
                self._finish_cycle(False, "감시 재시작 거부(선정 CSV·워밍업 확인)")
        except Exception as e:
            self._finish_cycle(False, f"재시작 단계 오류: {e}")

    def _finish_cycle(self, ok: bool, detail: str) -> None:
        self._cycle_in_progress = False
        self._eod_stop_for_cycle = False
        try:
            msg = (
                f"✅ [연속감시] 장마감 전환 완료 — {detail}"
                if ok
                else f"⚠️ [연속감시] 장마감 전환 실패 — {detail}"
            )
            self.log(msg)
            sl = getattr(self.app, "session_logger", None)
            if sl is not None:
                sl.log(
                    "continuous_monitor_cycle_end",
                    {"ok": bool(ok), "detail": str(detail)},
                )
        except Exception:
            pass
        try:
            fn = getattr(self.app, "_sync_continuous_monitor_ui", None)
            if callable(fn):
                fn()
        except Exception:
            pass

    def consume_eod_stop_flag(self) -> bool:
        """STOPPED 콜백에서 EOD 중단 여부를 한 번만 소비."""
        if self._eod_stop_for_cycle:
            self._eod_stop_for_cycle = False
            return True
        return False
