# -*- coding: utf-8 -*-
"""
GUI 상단 B 추세 레짐(전환 게이트) 표시.

갱신 시점:
  - GUI 기동(재시작) 직후 1회 — **직전 완료 거래일** 데이터로 판정
  - 거래일 장마감(15:30) + 버퍼(기본 30분) 이후 1회/일 — 당일 완료분 반영

표시 일자:
  - 판정은 완료 거래일(데이터) 기준이지만, UI에는 **적용되는 다음 영업일**을 표기한다.

장중에는 미완성 당일 일봉으로 재계산하지 않는다 (백테 ref일 정합).
"""
from __future__ import annotations

import threading
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable, List, Optional

import strategy_selector


class RegimeDisplayService:
    """전환 게이트 레짐을 헤더 중앙 라벨에 표시한다."""

    _REGIME_LABELS: dict[str, str] = {
        "GATE_OFF": "— (게이트 OFF)",
        "OFF": "유지",
        "TRANSITION": "약화",
        "HARD": "붕괴(chop)",
        "UNKNOWN": "—",
        "LOADING": "계산 중…",
        "ERROR": "조회 실패",
    }

    _REGIME_COLORS: dict[str, str] = {
        "GATE_OFF": "#81A1C1",
        "OFF": "#A3BE8C",
        "TRANSITION": "#EBCB8B",
        "HARD": "#BF616A",
        "UNKNOWN": "#81A1C1",
        "LOADING": "#88C0D0",
        "ERROR": "#BF616A",
    }

    def __init__(self, app: Any, *, log_func: Optional[Callable[[str], None]] = None) -> None:
        self.app = app
        self.log = log_func or print
        self._after_id: Any = None
        self._cancelled: bool = False
        self._refresh_in_progress: bool = False
        self._last_eod_refresh_day: Optional[str] = None
        self._cached_regime: str = "UNKNOWN"
        # 데이터 판정 기준일(완료 거래일)
        self._cached_as_of: str = ""
        # UI 표시용 — 레짐이 적용되는 다음 영업일
        self._cached_apply_on: str = ""
        self._cached_detail: str = ""
        self._history_in_progress: bool = False

    def shutdown(self) -> None:
        """앱 종료 시 after 예약 취소."""
        self._cancelled = True
        try:
            root = getattr(self.app, "root", None)
            if root is not None and self._after_id is not None:
                root.after_cancel(self._after_id)
        except Exception:
            pass
        self._after_id = None

    def start(self) -> None:
        """스케줄러 시작 + 기동 직후 1회 갱신(직전 완료 거래일)."""
        self._cancelled = False
        self._schedule_tick()
        try:
            root = getattr(self.app, "root", None)
            if root is not None:
                root.after(2500, lambda: self.refresh_async(reason="startup"))
        except Exception:
            pass

    def _eod_buffer_minutes(self) -> int:
        try:
            st = getattr(self.app, "settings", None) or {}
            v = int(float(st.get("regime_display_eod_buffer_minutes", 30)))
            return max(5, min(v, 120))
        except (TypeError, ValueError):
            return 30

    def _completed_asof_date(self, now: Optional[datetime] = None) -> date:
        """직전 완료 거래일 (장중이면 전일, EOD+버퍼 후면 당일)."""
        try:
            from reporting.krx_trading_calendar import completed_trading_day_asof_local

            return completed_trading_day_asof_local(
                now or datetime.now(),
                eod_buffer_minutes=self._eod_buffer_minutes(),
            )
        except Exception:
            t = now or datetime.now()
            return t.date() - timedelta(days=1)

    @staticmethod
    def _apply_on_from_data_asof(data_as_of: str) -> str:
        """
        데이터 판정일(완료 거래일) → 레짐이 적용되는 다음 영업일 문자열.

        실패 시 입력값을 그대로 반환한다.
        """
        s = str(data_as_of or "").strip()[:10]
        if len(s) < 10:
            return s
        try:
            from reporting.krx_trading_calendar import next_trading_day

            d0 = date.fromisoformat(s)
            nxt = next_trading_day(d0)
            if nxt is not None:
                return nxt.strftime("%Y-%m-%d")
        except Exception:
            pass
        return s

    def _schedule_tick(self) -> None:
        if self._cancelled:
            return
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

    def _resolve_eod_trading_day(self, now: datetime) -> Optional[date]:
        """오늘이 거래일이고 close+버퍼 이후면 당일, 아니면 None."""
        try:
            from reporting.krx_trading_calendar import is_trading_day
            from utils.krx_session_hours import get_session_hours

            today_d = now.date()
            if not is_trading_day(today_d):
                return None
            due = get_session_hours(today_d).close_datetime() + timedelta(
                minutes=self._eod_buffer_minutes()
            )
            if now >= due:
                return today_d
        except Exception:
            try:
                from utils.krx_session_hours import get_session_hours

                today_d = now.date()
                if today_d.weekday() >= 5:
                    return None
                due = get_session_hours(today_d).close_datetime() + timedelta(
                    minutes=self._eod_buffer_minutes()
                )
                if now >= due:
                    return today_d
            except Exception:
                pass
        return None

    def tick(self) -> None:
        """30초마다 장마감+버퍼 시점 갱신 여부 확인."""
        try:
            if self._cancelled:
                return
            self._schedule_tick()

            now = datetime.now()
            trade_day = self._resolve_eod_trading_day(now)
            if trade_day is None:
                return

            day_key = trade_day.strftime("%Y-%m-%d")
            if self._last_eod_refresh_day == day_key:
                return

            self.refresh_async(reason=f"eod_{day_key}", as_of=trade_day)
        except Exception as e:
            try:
                self.log(f"⚠️ [레짐표시] 스케줄 tick 오류: {e}")
            except Exception:
                pass

    def refresh_async(
        self,
        *,
        reason: str = "manual",
        as_of: Optional[date] = None,
    ) -> None:
        """백그라운드에서 레짐 조회 후 메인 스레드 라벨 갱신."""
        if self._refresh_in_progress:
            return
        self._refresh_in_progress = True
        try:
            self._apply_label("LOADING", "", "", "")
        except Exception:
            pass

        # 기동/수동: 미지정 시 직전 완료 거래일 고정 (장중 '오늘' 재계산 방지)
        resolved_asof = as_of
        if resolved_asof is None:
            try:
                resolved_asof = self._completed_asof_date()
            except Exception:
                resolved_asof = None

        def _worker() -> None:
            payload: dict[str, Any]
            try:
                payload = self._fetch_regime_payload(as_of=resolved_asof)
                payload["reason"] = reason
            except Exception as ex:
                payload = {
                    "regime_key": "ERROR",
                    "as_of": "",
                    "apply_on": "",
                    "detail": str(ex)[:80],
                    "reason": reason,
                }
            try:
                root = getattr(self.app, "root", None)
                if root is not None:
                    root.after(0, lambda: self._on_refresh_done(payload))
                else:
                    self._on_refresh_done(payload)
            except Exception:
                self._refresh_in_progress = False

        threading.Thread(target=_worker, daemon=True, name="RegimeDisplay").start()

    def _fetch_regime_payload(self, *, as_of: Optional[date] = None) -> dict[str, Any]:
        st = dict(getattr(self.app, "settings", None) or {})
        if not bool(st.get("use_transition_gate", False)):
            return {
                "regime_key": "GATE_OFF",
                "as_of": "",
                "apply_on": "",
                "detail": "",
                "reason": "gate_off",
            }

        dm = getattr(self.app, "data_manager", None)
        if dm is None:
            return {
                "regime_key": "ERROR",
                "as_of": "",
                "apply_on": "",
                "detail": "data_manager 없음",
                "reason": "no_dm",
            }

        if as_of is None:
            as_of = self._completed_asof_date()
        as_of_dt = datetime.combine(as_of, dtime(0, 0))
        tg = strategy_selector.resolve_transition_gate_for_live(
            st,
            dm,
            as_of_dt,
            logger_callback=lambda *_a, **_k: None,
        )
        reg = str(tg.get("regime", "OFF")).strip().upper()
        if reg not in ("OFF", "TRANSITION", "HARD"):
            reg = "OFF"

        slots_a = int(tg.get("slots_a", 0) or 0)
        slots_b = int(tg.get("slots_b", 0) or 0)
        detail = ""
        if reg == "TRANSITION":
            detail = f"감시 {slots_a}/{slots_b}"
        elif reg == "HARD":
            detail = "신규 차단"

        as_of_s = str(tg.get("as_of") or as_of_dt.strftime("%Y-%m-%d"))
        apply_on = self._apply_on_from_data_asof(as_of_s)
        return {
            "regime_key": reg,
            "as_of": as_of_s,
            "apply_on": apply_on,
            "detail": detail,
            "reason": "ok",
        }

    def _on_refresh_done(self, payload: dict[str, Any]) -> None:
        self._refresh_in_progress = False
        try:
            key = str(payload.get("regime_key", "UNKNOWN"))
            as_of = str(payload.get("as_of", "") or "")
            apply_on = str(payload.get("apply_on", "") or "")
            if not apply_on and as_of:
                apply_on = self._apply_on_from_data_asof(as_of)
            detail = str(payload.get("detail", "") or "")
            self._cached_regime = key
            self._cached_as_of = as_of
            self._cached_apply_on = apply_on
            self._cached_detail = detail

            reason = str(payload.get("reason", "") or "")
            if reason.startswith("eod_"):
                self._last_eod_refresh_day = reason.replace("eod_", "", 1)
            else:
                try:
                    now = datetime.now()
                    td = self._resolve_eod_trading_day(now)
                    if td is not None and as_of == td.strftime("%Y-%m-%d"):
                        self._last_eod_refresh_day = as_of
                except Exception:
                    pass

            self._apply_label(key, as_of, apply_on, detail)

            if reason == "startup":
                try:
                    show_d = apply_on or as_of
                    self.log(
                        f"📊 [레짐표시] B 추세 레짐={self._REGIME_LABELS.get(key, key)}"
                        + (f" (적용 {show_d})" if show_d else "")
                    )
                except Exception:
                    pass
        except Exception as e:
            try:
                self.log(f"⚠️ [레짐표시] UI 반영 실패: {e}")
            except Exception:
                pass

    def open_history_async(self) -> None:
        """최근 영업일 레짐 이력 다이얼로그(백그라운드 조회)."""
        if getattr(self, "_history_in_progress", False):
            return
        self._history_in_progress = True
        btn = getattr(self.app, "btn_regime_history", None)
        try:
            if btn is not None:
                btn.config(state="disabled")
        except Exception:
            pass

        st = dict(getattr(self.app, "settings", None) or {})
        gate_on = bool(st.get("use_transition_gate", False))
        try:
            lookback = int(float(st.get("regime_history_lookback_days", 22)))
        except (TypeError, ValueError):
            lookback = 22
        lookback = max(5, min(lookback, 60))

        def _worker() -> None:
            rows: List[dict[str, Any]] = []
            err = ""
            try:
                dm = getattr(self.app, "data_manager", None)
                if dm is None:
                    err = "data_manager 없음"
                else:
                    rows = strategy_selector.compute_transition_regime_history(
                        st,
                        dm,
                        lookback_trading_days=lookback,
                    )
            except Exception as ex:
                err = str(ex)

            def _finish() -> None:
                self._history_in_progress = False
                try:
                    if btn is not None:
                        btn.config(state="normal")
                except Exception:
                    pass
                if err:
                    try:
                        from tkinter import messagebox

                        messagebox.showerror(
                            "레짐 이력",
                            f"조회 실패:\n{err}",
                            parent=getattr(self.app, "root", None),
                        )
                    except Exception:
                        pass
                    return
                try:
                    from ui.regime_history_dialog import open_regime_history_dialog

                    open_regime_history_dialog(
                        getattr(self.app, "root", None),
                        self.app,
                        rows,
                        gate_enabled=gate_on,
                    )
                except Exception as ex:
                    try:
                        self.log(f"⚠️ [레짐표시] 이력 창 오류: {ex}")
                    except Exception:
                        pass

            try:
                root = getattr(self.app, "root", None)
                if root is not None:
                    root.after(0, _finish)
                else:
                    _finish()
            except Exception:
                self._history_in_progress = False

        threading.Thread(
            target=_worker, daemon=True, name="RegimeHistory"
        ).start()

    def _apply_label(
        self,
        regime_key: str,
        data_as_of: str,
        apply_on: str,
        detail: str,
    ) -> None:
        lbl = getattr(self.app, "lbl_regime_status", None)
        if lbl is None:
            return
        try:
            label_txt = self._REGIME_LABELS.get(regime_key, regime_key)
            parts = [f"B 추세 레짐: {label_txt}"]
            if detail:
                parts[0] += f" · {detail}"
            # 표시는 적용일(다음 장). 데이터 기준일은 캐시에만 보관.
            show = str(apply_on or "").strip() or str(data_as_of or "").strip()
            if show and regime_key not in ("LOADING", "GATE_OFF"):
                short_d = show[5:] if len(show) >= 10 else show
                parts[0] += f"  (적용 {short_d})"
            text = parts[0]
            color = self._REGIME_COLORS.get(regime_key, "#ECEFF4")
            bg = (getattr(self.app, "colors", None) or {}).get("bg", "#2E3440")
            lbl.config(text=text, fg=color, bg=bg)
        except Exception:
            pass
