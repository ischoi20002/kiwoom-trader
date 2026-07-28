# -*- coding: utf-8 -*-
"""
금일 손익 = 현재 총자산 − 장시작(시초가 반영 전) 총자산.

기준 총자산은 **당일 시초가가 반영되기 전** 스냅이어야 한다.
- 장전(09:00 미만) 감시 중: 당시 총자산으로 기준을 갱신하다가 09:00에 고정.
- 09:00 전에 캡처를 못 한 경우: 보유 종목을 **직전 종가**로 평가한 합성 총자산으로 폴백.
- 감시가 정규장 장중(09:00~15:30)에 시작되면 ``N/A``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional


def _state(app: Any) -> Dict[str, Any]:
    st = getattr(app, "_session_open_day_pnl", None)
    if not isinstance(st, dict):
        st = {}
        try:
            app._session_open_day_pnl = st
        except Exception:
            pass
    return st


def _reset_state(app: Any, day_key: str) -> Dict[str, Any]:
    st: Dict[str, Any] = {
        "day": day_key,
        "na": False,
        "baseline": None,
        "baseline_locked": False,  # 09:00 이후 고정
        "baseline_source": None,  # "pre_open" | "prev_close"
        "started_at": None,
        "captured_at": None,
        "capture_after_id": None,
        "prev_close_warmup_inflight": False,
    }
    try:
        app._session_open_day_pnl = st
    except Exception:
        pass
    return st


def ensure_day_state(app: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
    """캘린더일이 바뀌면 상태를 초기화한다."""
    try:
        t = now if now is not None else datetime.now()
        day_key = t.date().isoformat()
    except Exception:
        day_key = ""
    st = _state(app)
    try:
        if str(st.get("day") or "") != day_key:
            return _reset_state(app, day_key)
    except Exception:
        return _reset_state(app, day_key)
    return st


def _is_trading_day(d) -> bool:
    try:
        from reporting.krx_trading_calendar import is_trading_day

        return bool(is_trading_day(d))
    except Exception:
        try:
            return d.weekday() < 5
        except Exception:
            return False


def _hm(now: datetime) -> int:
    return int(now.hour) * 60 + int(now.minute)


def _cancel_capture_after(app: Any, st: Dict[str, Any]) -> None:
    after_id = st.get("capture_after_id")
    if after_id is None:
        return
    try:
        root = getattr(app, "root", None)
        if root is not None:
            root.after_cancel(after_id)
    except Exception:
        pass
    st["capture_after_id"] = None


def compute_current_total_assets_day_pnl(app: Any) -> Optional[float]:
    """
    화면과 동일 정의의 현재 총자산(D+2 + 순매각 합, 증권사 총평가 치환 없음).
    장전 캡처용 — 시초가 틱이 들어오기 전에는 전일 종가·장전 시세가 남는다.
    """
    try:
        vt = getattr(app, "virtual_tab", None)
        if vt is not None:
            fn = getattr(vt, "compute_day_pnl_total_assets", None)
            if callable(fn):
                v = fn()
                if v is not None:
                    fv = float(v)
                    if fv > 0 and fv == fv:  # finite
                        return fv
    except Exception:
        pass
    try:
        portfolio = getattr(app, "portfolio", None)
        if portfolio is None:
            return None
        d2 = float(getattr(portfolio, "d2_deposit", 0) or 0)
        if d2 > 0:
            return float(d2)
    except Exception:
        pass
    return None


def _set_baseline(
    app: Any,
    st: Dict[str, Any],
    value: float,
    *,
    source: str,
    now: datetime,
    locked: bool,
) -> None:
    st["baseline"] = float(value)
    st["baseline_source"] = source
    st["baseline_locked"] = bool(locked)
    st["captured_at"] = now.isoformat(timespec="seconds")
    try:
        setattr(app, "_vt_day_pnl_tick_cache", None)
    except Exception:
        pass
    try:
        log = getattr(app, "log", None)
        if callable(log):
            lock_txt = "고정" if locked else "갱신"
            log(
                f"📌 [금일손익] 장시작 기준 총자산 {lock_txt}: {int(value):,} "
                f"(source={source}, {st.get('captured_at')})"
            )
    except Exception:
        pass


def _schedule_prev_close_baseline(app: Any) -> None:
    """
    장전 스냅을 못 잡았을 때: 직전 종가 합성 총자산을 백그라운드로 구해 기준으로 고정.
    (당일 시초가가 섞이지 않도록 REST 직전종가만 사용)
    """
    try:
        st = ensure_day_state(app)
        if st.get("na") or st.get("baseline") is not None:
            return
        if st.get("prev_close_warmup_inflight"):
            return
        st["prev_close_warmup_inflight"] = True
    except Exception:
        return

    def _worker() -> None:
        got: Optional[float] = None
        try:
            from utils.eod_equity_snapshots import (
                compute_synthetic_prior_total_assets_at_prev_close,
            )

            got = compute_synthetic_prior_total_assets_at_prev_close(
                app, allow_rest=True
            )
        except Exception:
            got = None
        finally:
            try:
                st2 = ensure_day_state(app)
                st2["prev_close_warmup_inflight"] = False
            except Exception:
                pass

        def _apply() -> None:
            try:
                st3 = ensure_day_state(app)
                if st3.get("na") or st3.get("baseline") is not None:
                    return
                if got is None:
                    return
                try:
                    fv = float(got)
                except (TypeError, ValueError):
                    return
                if not (fv > 0) or fv != fv:
                    return
                _set_baseline(
                    app,
                    st3,
                    fv,
                    source="prev_close",
                    now=datetime.now(),
                    locked=True,
                )
                _cancel_capture_after(app, st3)
                try:
                    req = getattr(app, "request_virtual_tab_realtime_render", None)
                    if callable(req):
                        req()
                except Exception:
                    pass
            except Exception:
                pass

        try:
            root = getattr(app, "root", None)
            if root is not None:
                root.after(0, _apply)
            else:
                _apply()
        except Exception:
            try:
                _apply()
            except Exception:
                pass

    try:
        import threading

        threading.Thread(
            target=_worker, name="DayPnlPrevCloseBaseline", daemon=True
        ).start()
    except Exception:
        try:
            st = ensure_day_state(app)
            st["prev_close_warmup_inflight"] = False
        except Exception:
            pass


def try_capture_open_baseline(app: Any, *, now: Optional[datetime] = None) -> bool:
    """
    시초가 반영 전 기준 총자산 확보.

    - 09:00 전: 현재 총자산으로 기준을 **갱신**(장전 최신 D+2·시세 반영).
    - 09:00 도달: 마지막 장전 값을 **고정**. 장전 값이 없으면 직전종가 합성 폴백 예약.
    - 이미 고정됐으면 no-op.
    """
    try:
        t = now if now is not None else datetime.now()
        st = ensure_day_state(app, t)
        if st.get("na"):
            return False
        if st.get("baseline_locked") and st.get("baseline") is not None:
            return False
        if not _is_trading_day(t.date()):
            return False

        hm = _hm(t)
        # 장전: 시초가 전 스냅으로 계속 갱신
        if hm < 9 * 60:
            total = compute_current_total_assets_day_pnl(app)
            if total is None:
                return False
            try:
                fv = float(total)
            except (TypeError, ValueError):
                return False
            if not (fv > 0) or fv != fv:
                return False
            _set_baseline(
                app, st, fv, source="pre_open", now=t, locked=False
            )
            return True

        # 09:00 이후: 장전 스냅이 있으면 고정만, 없으면 직전종가 폴백
        if st.get("baseline") is not None and not st.get("baseline_locked"):
            st["baseline_locked"] = True
            try:
                log = getattr(app, "log", None)
                if callable(log):
                    log(
                        f"📌 [금일손익] 장시작 기준 고정(시초가 전 스냅): "
                        f"{int(float(st['baseline'])):,}"
                    )
            except Exception:
                pass
            _cancel_capture_after(app, st)
            return True

        if st.get("baseline") is None:
            # 실시간 시세(시초가 포함 가능)로 캡처하지 않음
            _schedule_prev_close_baseline(app)
        return False
    except Exception:
        return False


def _schedule_capture_tick(app: Any) -> None:
    """장전까지 기준을 갱신하고, 09:00에 고정한다."""
    try:
        root = getattr(app, "root", None)
        if root is None:
            return
        st = ensure_day_state(app)
        if st.get("na") or st.get("baseline_locked"):
            return
        _cancel_capture_after(app, st)

        def _tick() -> None:
            try:
                st2 = ensure_day_state(app)
                st2["capture_after_id"] = None
                if st2.get("na") or st2.get("baseline_locked"):
                    return
                now = datetime.now()
                hm = _hm(now)
                try_capture_open_baseline(app, now=now)
                st2 = ensure_day_state(app)
                if st2.get("baseline_locked") or st2.get("na"):
                    try:
                        req = getattr(
                            app, "request_virtual_tab_realtime_render", None
                        )
                        if callable(req):
                            root.after(0, req)
                    except Exception:
                        pass
                    return
                if hm < 9 * 60:
                    # 장시작 직전일수록 촘촘히 (최대 15초)
                    remain_sec = max(
                        1, (9 * 60 - hm) * 60 - int(now.second)
                    )
                    delay_ms = int(min(15_000, max(1_000, remain_sec * 1000)))
                    # 08:59 이후는 2초 간격
                    if hm >= (8 * 60 + 59):
                        delay_ms = 2_000
                    try:
                        st2["capture_after_id"] = root.after(delay_ms, _tick)
                    except Exception:
                        pass
                    return
                # 09:00 이후인데 아직 미고정 → 직전종가 폴백 대기, 5초마다 확인
                try:
                    st2["capture_after_id"] = root.after(5_000, _tick)
                except Exception:
                    pass
            except Exception:
                pass

        try:
            st["capture_after_id"] = root.after(500, _tick)
        except Exception:
            pass
    except Exception:
        pass


def on_monitoring_started(app: Any, *, now: Optional[datetime] = None) -> None:
    """
    실시간 감시 STARTED 시 호출.
    장중 시작이면 ``N/A``, 장전이면 시초가 전 기준 캡처를 예약한다.
    """
    try:
        t = now if now is not None else datetime.now()
        st = ensure_day_state(app, t)
        st["started_at"] = t.isoformat(timespec="seconds")
        st["baseline"] = None
        st["baseline_locked"] = False
        st["baseline_source"] = None
        st["captured_at"] = None
        st["na"] = False
        st["prev_close_warmup_inflight"] = False
        _cancel_capture_after(app, st)
        try:
            setattr(app, "_vt_day_pnl_tick_cache", None)
        except Exception:
            pass

        if not _is_trading_day(t.date()):
            st["na"] = True
            try:
                log = getattr(app, "log", None)
                if callable(log):
                    log("📌 [금일손익] 비거래일 감시 시작 — N/A")
            except Exception:
                pass
            return

        hm = _hm(t)
        # 정규장 장중(09:00~15:30) 시작 → N/A
        if (9 * 60) <= hm <= (15 * 60 + 30):
            st["na"] = True
            try:
                log = getattr(app, "log", None)
                if callable(log):
                    log(
                        "📌 [금일손익] 장중 감시 시작 — "
                        "장시작 기준 총자산을 알 수 없어 N/A"
                    )
            except Exception:
                pass
            return

        # 장마감 이후 시작 → 당일 장시작 기준 불가
        if hm > (15 * 60 + 30):
            st["na"] = True
            try:
                log = getattr(app, "log", None)
                if callable(log):
                    log("📌 [금일손익] 장마감 후 감시 시작 — N/A")
            except Exception:
                pass
            return

        # 장전 시작: 시초가 전 스냅 → 09:00 고정
        st["na"] = False
        try:
            log = getattr(app, "log", None)
            if callable(log):
                log(
                    "📌 [금일손익] 장전 감시 시작 — "
                    "시초가 반영 전 총자산을 기준으로 사용"
                )
        except Exception:
            pass
        try_capture_open_baseline(app, now=t)
        _schedule_capture_tick(app)
    except Exception:
        pass


def format_day_pnl_text(app: Any, current_total_assets: float) -> str:
    """포지션 탭 「금일 손익」 표시 문자열."""
    try:
        now = datetime.now()
        st = ensure_day_state(app, now)
        if st.get("na"):
            return "N/A"
        if not st.get("started_at"):
            return "—"
        if st.get("baseline") is None or not st.get("baseline_locked"):
            try_capture_open_baseline(app, now=now)
            st = ensure_day_state(app, now)
        baseline = st.get("baseline")
        if baseline is None:
            if _hm(now) < 9 * 60:
                return "— (장시작 대기)"
            return "— (기준 계산 중)"
        # 장전에도 기준이 있으면 잠정 표시(09:00에 최종 고정)
        try:
            base = float(baseline)
            cur = float(current_total_assets)
        except (TypeError, ValueError):
            return "—"
        if not (base > 0) or base != base or cur != cur:
            return "—"
        d_amt = cur - base
        d_pct = (d_amt / base) * 100.0
        if d_pct != d_pct:  # NaN
            d_pct = 0.0
        return f"{int(d_amt):+,} ({d_pct:+.2f}%)"
    except Exception:
        return "—"
