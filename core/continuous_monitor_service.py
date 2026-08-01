# -*- coding: utf-8 -*-
"""
연속 실시간 감시 스케줄.

- **16:00**(15:30+버퍼, 기본 30분): 감시 중단 → 일일 선정 (재시작 없음)
- **08:50**(설정): 다음날/당일 장전 실시간 감시 시작
  - 직전 장마감 선정이 **실패**했으면 장전에 **강제 재선정** 후 시작
  - CSV 신선도 만료 시에도 기존처럼 선정 후 시작

「실시간 감시 시작」과 함께 ``arm()`` 됩니다.
"""
from __future__ import annotations

import threading
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable, Optional, Tuple


class ContinuousMonitorService:
    """장마감 선정·장전 감시 시작 스케줄러(실시간 감시와 연동)."""

    def __init__(self, app: Any, *, log_func: Optional[Callable[[str], None]] = None) -> None:
        self.app = app
        self.log = log_func or print
        self.armed: bool = False
        self._after_id: Any = None
        self._cancelled: bool = False
        self._cycle_in_progress: bool = False
        self._last_cycle_trading_day: Optional[str] = None
        self._last_morning_start_day: Optional[str] = None
        # EOD 사이클용 stop() 인지 — STOPPED 콜백에서 연속 모드 해제 방지
        self._eod_stop_for_cycle: bool = False
        # 장마감 선정 실패 → 다음 장전(08:50)에 강제 재선정
        self._pending_morning_reselect: bool = False
        # 장전 시작(선정 포함) 진행 중 — 30초 tick 중복 기동 방지
        self._morning_start_in_progress: bool = False

    def arm(self) -> None:
        """연속 스케줄 활성화. 이미 armed 면 스케줄러만 유지."""
        already = bool(self.armed)
        self.armed = True
        self._cancelled = False
        self.start_scheduler()
        if already:
            return
        try:
            eod_hm = self._format_eod_hhmm()
            start_hm = self._auto_start_hhmm_str()
            self.log(
                f"🔄 [연속감시] 실시간 감시와 연동 — "
                f"장마감 선정 {eod_hm}, 감시 시작 {start_hm}"
            )
        except Exception:
            pass

    def disarm(self, *, manual: bool = True) -> None:
        """연속 감시 모드 해제."""
        was_armed = bool(self.armed)
        self.armed = False
        self._cycle_in_progress = False
        self._eod_stop_for_cycle = False
        if manual and was_armed:
            try:
                self.log("⏹️ [연속감시] 자동 전환을 해제했습니다.")
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
        """15:30 기준 버퍼(기본 30 → 선정 시각 16:00)."""
        try:
            st = getattr(self.app, "settings", None) or {}
            v = int(
                float(
                    st.get(
                        "continuous_monitor_eod_buffer_minutes",
                        30,
                    )
                )
            )
            return max(5, min(v, 120))
        except (TypeError, ValueError):
            return 30

    def _format_eod_hhmm(self) -> str:
        try:
            due = datetime.combine(date.today(), dtime(15, 30, 0)) + timedelta(
                minutes=self._eod_buffer_minutes()
            )
            return due.strftime("%H:%M")
        except Exception:
            return "16:00"

    def _auto_start_hhmm_str(self) -> str:
        h, m = self._auto_start_hm()
        return f"{h:02d}:{m:02d}"

    def _auto_start_hm(self) -> Tuple[int, int]:
        try:
            st = getattr(self.app, "settings", None) or {}
            raw = str(st.get("realtime_monitor_auto_start_hhmm", "08:50") or "08:50")
            parts = raw.strip().replace(".", ":").split(":")
            h = int(parts[0])
            m = int(parts[1]) if len(parts) > 1 else 0
            h = max(0, min(23, h))
            m = max(0, min(59, m))
            return h, m
        except Exception:
            return 8, 50

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

    def _should_morning_start(self, now: datetime) -> bool:
        """armed·미감시 상태에서 당일 08:50 이후 1회 시작 여부."""
        try:
            from reporting.krx_trading_calendar import is_trading_day

            today_d = now.date()
            if not is_trading_day(today_d):
                return False
        except Exception:
            today_d = now.date()
            if today_d.weekday() >= 5:
                return False
        day_key = self._trading_day_key(today_d)
        if self._last_morning_start_day == day_key:
            return False
        h, m = self._auto_start_hm()
        start_at = datetime.combine(today_d, dtime(h, m, 0))
        # 장전 시작 시각~EOD 선정 시각 전까지만 오전 자동 시작
        eod_due = datetime.combine(today_d, dtime(15, 30, 0)) + timedelta(
            minutes=self._eod_buffer_minutes()
        )
        return start_at <= now < eod_due

    def tick(self) -> None:
        """30초마다 EOD 선정·장전 시작 조건 확인."""
        try:
            if self._cancelled:
                return
            self._schedule_tick()

            if not self.armed or self._cycle_in_progress:
                return

            tc = getattr(self.app, "trading_controller", None)
            if tc is None:
                return

            now = datetime.now()
            monitoring = bool(getattr(tc, "is_monitoring", False))

            # 1) 장전 자동 시작 (미감시)
            if not monitoring:
                if self._morning_start_in_progress:
                    return
                if self._should_morning_start(now):
                    self._begin_morning_start(now.date())
                return

            # 2) 장마감 선정 (감시 중)
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

    def _begin_morning_start(self, day_d: date) -> None:
        """08:50 등 장전 감시 시작(필요 시 재선정)."""
        day_key = self._trading_day_key(day_d)
        force_sel = bool(self._pending_morning_reselect)
        try:
            hm = self._auto_start_hhmm_str()
            if force_sel:
                self.log(
                    f"🔄 [연속감시] {day_key} {hm} 장전 감시 시작 "
                    f"— 직전 장마감 선정 실패분, 재선정 후 시작"
                )
            else:
                self.log(f"🔄 [연속감시] {day_key} {hm} 실시간 감시 자동 시작")
        except Exception:
            pass

        self._morning_start_in_progress = True

        def _on_done(ok: bool) -> None:
            try:
                self._morning_start_in_progress = False
                if ok:
                    self._last_morning_start_day = day_key
                    self._pending_morning_reselect = False
                    return
                # 실패 시 당일 플래그를 남기지 않아 EOD 전까지 재시도
                if force_sel:
                    self._pending_morning_reselect = True
                try:
                    self.log(
                        "⚠️ [연속감시] 장전 시작 미완료 — "
                        "선정/시작 성공 시까지 주기적으로 재시도합니다."
                    )
                except Exception:
                    pass
            except Exception:
                self._morning_start_in_progress = False

        try:
            ens = getattr(self.app, "ensure_fresh_selection_then_start", None)
            if callable(ens):
                ens(force_selection=force_sel, on_done=_on_done)
                return
            tc = getattr(self.app, "trading_controller", None)
            started_ok = False
            if tc is not None:
                started_ok = bool(tc.start())
            _on_done(started_ok)
        except Exception as e:
            try:
                self.log(f"⚠️ [연속감시] 장전 시작 실패: {e}")
            except Exception:
                pass
            _on_done(False)

    def _begin_eod_cycle(self, day_key: str) -> None:
        self._cycle_in_progress = True
        self._last_cycle_trading_day = day_key
        try:
            eod_hm = self._format_eod_hhmm()
            start_hm = self._auto_start_hhmm_str()
            self.log(
                f"🔄 [연속감시] {day_key} 장마감 선정 시작 "
                f"({eod_hm}: 중단→일일 선정, 감시 재시작은 {start_hm})"
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
        """2단계: 일일 감시목록 선정(FDR api_only). 재시작은 장전 스케줄에 맡김."""
        try:
            mt = getattr(self.app, "monitor_tab", None)
            if mt is None or not hasattr(mt, "select_monitoring_targets"):
                self._finish_cycle(False, "monitor_tab 없음")
                return

            def _on_complete(ok: bool) -> None:
                try:
                    root = getattr(self.app, "root", None)
                    if root is not None:
                        root.after(0, lambda o=ok: self._phase_after_selection(o))
                    else:
                        self._phase_after_selection(ok)
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

    def _phase_after_selection(self, selection_ok: bool) -> None:
        """3단계: 선정만 완료 — 감시 재시작은 익일/당일 장전 시각."""
        try:
            self._eod_stop_for_cycle = False
            if not self.armed:
                self._finish_cycle(False, "연속 감시 해제됨")
                return
            if not selection_ok:
                self._finish_cycle(False, "일일 선정 실패 또는 종목 없음")
                return
            start_hm = self._auto_start_hhmm_str()
            self._finish_cycle(
                True,
                f"일일 선정 완료 — 감시 시작은 {start_hm}에 자동 수행",
            )
        except Exception as e:
            self._finish_cycle(False, f"선정 후처리 오류: {e}")

    def _finish_cycle(self, ok: bool, detail: str) -> None:
        self._cycle_in_progress = False
        self._eod_stop_for_cycle = False
        try:
            if ok:
                self._pending_morning_reselect = False
            else:
                # 다음 거래일 장전(08:50)에 강제 재선정
                self._pending_morning_reselect = True
        except Exception:
            self._pending_morning_reselect = not bool(ok)
        try:
            msg = (
                f"✅ [연속감시] 장마감 선정 완료 — {detail}"
                if ok
                else (
                    f"⚠️ [연속감시] 장마감 선정 실패 — {detail} "
                    f"(다음 장전 {self._auto_start_hhmm_str()}에 재선정 예정)"
                )
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
        # 감시 중단 상태 유지 — 장전 시각까지 대기 문구
        if ok and self.armed:
            try:
                hm = self._auto_start_hhmm_str()
                txt = f"상태: 대기 중 (감시 시작 {hm})"
                root = getattr(self.app, "root", None)

                def _ui() -> None:
                    try:
                        lbl = getattr(self.app, "lbl_monitor_status", None)
                        if lbl is not None:
                            lbl.config(
                                text=txt,
                                foreground=getattr(self.app, "colors", {}).get(
                                    "primary", "#88C0D0"
                                ),
                            )
                        slb = getattr(self.app, "status_label", None)
                        if slb is not None:
                            slb.config(
                                text=txt,
                                foreground=getattr(self.app, "colors", {}).get(
                                    "primary", "#88C0D0"
                                ),
                            )
                    except Exception:
                        pass

                if root is not None:
                    root.after(0, _ui)
                else:
                    _ui()
            except Exception:
                pass

    def consume_eod_stop_flag(self) -> bool:
        """STOPPED 콜백에서 EOD 중단 여부를 한 번만 소비."""
        if self._eod_stop_for_cycle:
            self._eod_stop_for_cycle = False
            return True
        return False

    def ms_until_auto_start(self, now: Optional[datetime] = None) -> int:
        """
        다음 자동 감시 시작까지 밀리초.

        - 거래일·장전 시작 시각 이전이면 그 시각까지
        - 거래일·장전~EOD 이전이면 0(즉시)
        - 그 외(EOD 이후·비거래일)면 다음 거래일 장전 시각까지
        """
        t = now if now is not None else datetime.now()
        h, m = self._auto_start_hm()
        buf = self._eod_buffer_minutes()
        try:
            from reporting.krx_trading_calendar import (
                is_trading_day,
                next_trading_day,
            )
        except Exception:

            def is_trading_day(d: date) -> bool:
                return d.weekday() < 5

            def next_trading_day(d: date, max_lookahead: int = 10) -> Optional[date]:
                cur = d
                for _ in range(max_lookahead):
                    cur = cur + timedelta(days=1)
                    if cur.weekday() < 5:
                        return cur
                return None

        def _start_at(day_d: date) -> datetime:
            return datetime.combine(day_d, dtime(h, m, 0))

        def _eod_at(day_d: date) -> datetime:
            return datetime.combine(day_d, dtime(15, 30, 0)) + timedelta(minutes=buf)

        today = t.date()
        try:
            if is_trading_day(today):
                st = _start_at(today)
                eod = _eod_at(today)
                if t < st:
                    return max(0, int((st - t).total_seconds() * 1000))
                if t < eod:
                    return 0
            nxt = next_trading_day(today)
            if nxt is None:
                nxt = today + timedelta(days=1)
            st2 = _start_at(nxt)
            return max(1000, int((st2 - t).total_seconds() * 1000))
        except Exception:
            return 8_000
