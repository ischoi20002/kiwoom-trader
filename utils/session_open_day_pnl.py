# -*- coding: utf-8 -*-
"""
금일 손익 = 현재 총자산 − 장시작(시초가 반영 전) 총자산.

기준 총자산은 **당일 시초가가 반영되기 전** 스냅이어야 한다.
- 장전(개장 미만) 감시 중: 당시 총자산으로 기준을 갱신하다가 개장에 고정.
- 장전 캡처를 못 했거나 **장중·장마감 후**에 감시를 켠 경우:
  1) 직전 거래일 EOD 스냅샷 ``total_assets``
  2) 보유 종목 **직전 종가** 합성 총자산
  순으로 폴백한다 (더 이상 장중 시작만으로 ``N/A`` 고정하지 않음).
- 비거래일 감시 시작만 ``N/A``.
개장·종가 시각은 ``utils.krx_session_hours`` (특수일 JSON 포함)를 따른다.
"""
from __future__ import annotations

from datetime import date, datetime
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
        t = datetime.now()
    st = _state(app)
    rolled = False
    try:
        if str(st.get("day") or "") != day_key:
            st = _reset_state(app, day_key)
            rolled = True
    except Exception:
        st = _reset_state(app, day_key)
        rolled = True
    # 자정 롤오버 후에도 감시가 켜져 있으면 당일 금일손익 기준을 다시 잡는다.
    # (리셋만 하고 on_monitoring_started 를 안 부르면 started_at 이 비어 「—」로 고착)
    if rolled:
        try:
            _rearm_day_pnl_if_monitoring(app, now=t)
            st = _state(app)
        except Exception:
            pass
    return st


def _is_app_monitoring(app: Any) -> bool:
    """GUI·TradingController 감시 플래그."""
    try:
        if bool(getattr(app, "is_monitoring", False)):
            return True
    except Exception:
        pass
    try:
        tc = getattr(app, "trading_controller", None)
        if tc is not None and bool(getattr(tc, "is_monitoring", False)):
            return True
    except Exception:
        pass
    return False


def _rearm_day_pnl_if_monitoring(
    app: Any, *, now: Optional[datetime] = None
) -> None:
    """날짜 변경 직후, 이미 감시 중이면 금일손익 세션을 재무장한다."""
    try:
        if not _is_app_monitoring(app):
            return
        on_monitoring_started(app, now=now)
    except Exception:
        pass


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


def _session_open_close_minutes(day) -> tuple[int, int]:
    """당일 개장·종가(분). 실패 시 기본 09:00 / 15:30."""
    try:
        from utils.krx_session_hours import get_session_hours

        h = get_session_hours(day)
        return int(h.open_minutes), int(h.close_minutes)
    except Exception:
        return 9 * 60, 15 * 60 + 30


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


def _baseline_from_prev_equity_snapshot(app: Any) -> Optional[float]:
    """
    직전 거래일 운영 EOD 스냅샷의 총자산(로컬 JSON, REST 없음).

    장중·장마감 후 감시 시작 시 금일손익 분모로 우선 사용한다.
    """
    try:
        from reporting.krx_trading_calendar import previous_trading_day
        from utils.equity_snapshots_store import (
            equity_snapshots_root_candidates,
            read_equity_snapshot,
            read_equity_snapshot_from_root,
        )

        today = date.today()
        prev = previous_trading_day(today)
        if prev is None:
            return None
        rec: Optional[Dict[str, Any]] = None
        try:
            rec = read_equity_snapshot(prev)
        except Exception:
            rec = None
        if not isinstance(rec, dict):
            try:
                for root in equity_snapshots_root_candidates():
                    rec = read_equity_snapshot_from_root(root, prev)
                    if isinstance(rec, dict):
                        break
            except Exception:
                rec = None
        if not isinstance(rec, dict):
            return None
        for key in ("total_assets", "total_equity"):
            try:
                fv = float(rec.get(key))
            except (TypeError, ValueError):
                continue
            if fv > 0 and fv == fv:
                return fv
        return None
    except Exception:
        return None


def _apply_fallback_baseline_if_needed(app: Any, *, now: Optional[datetime] = None) -> bool:
    """
    장전 스냅이 없을 때 EOD 스냅 → (비동기) 직전종가 합성 순으로 기준을 채운다.

    Returns
    -------
    bool
        동기 경로에서 기준을 이미 고정했으면 True.
    """
    try:
        t = now if now is not None else datetime.now()
        st = ensure_day_state(app, t)
        if st.get("na"):
            return False
        if st.get("baseline") is not None and st.get("baseline_locked"):
            return True
        snap = _baseline_from_prev_equity_snapshot(app)
        if snap is not None:
            _set_baseline(
                app,
                st,
                float(snap),
                source="prev_eod_snapshot",
                now=t,
                locked=True,
            )
            _cancel_capture_after(app, st)
            return True
        _schedule_prev_close_baseline(app)
        return False
    except Exception:
        return False


def _schedule_prev_close_baseline(app: Any) -> None:
    """
    장전 스냅·EOD 스냅을 못 잡았을 때: 직전 종가 합성 총자산을 백그라운드로 구해 고정.
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
        src = "prev_close"
        try:
            # 워커에서도 스냅샷을 한 번 더(감시 직후 파일 갱신 레이스)
            snap = _baseline_from_prev_equity_snapshot(app)
            if snap is not None:
                got = float(snap)
                src = "prev_eod_snapshot"
            else:
                from utils.eod_equity_snapshots import (
                    compute_synthetic_prior_total_assets_at_prev_close,
                )

                got = compute_synthetic_prior_total_assets_at_prev_close(
                    app, allow_rest=True
                )
                src = "prev_close"
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
                    source=str(src or "prev_close"),
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

    - 개장 전: 현재 총자산으로 기준을 **갱신**(장전 최신 D+2·시세 반영).
    - 개장 도달: 마지막 장전 값을 **고정**. 장전 값이 없으면 직전종가 합성 폴백 예약.
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
        open_m, _close_m = _session_open_close_minutes(t.date())
        # 장전: 시초가 전 스냅으로 계속 갱신
        if hm < open_m:
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

        # 개장 이후: 장전 스냅이 있으면 고정만, 없으면 직전종가 폴백
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
            # 실시간 시세(시초가 포함 가능)로 캡처하지 않음 → EOD/직전종가 폴백
            _apply_fallback_baseline_if_needed(app, now=t)
        return False
    except Exception:
        return False


def _schedule_capture_tick(app: Any) -> None:
    """장전까지 기준을 갱신하고, 개장 시각에 고정한다."""
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
                open_m, _c = _session_open_close_minutes(now.date())
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
                if hm < open_m:
                    # 장시작 직전일수록 촘촘히 (최대 15초)
                    remain_sec = max(
                        1, (open_m - hm) * 60 - int(now.second)
                    )
                    delay_ms = int(min(15_000, max(1_000, remain_sec * 1000)))
                    # 개장 1분 전부터는 2초 간격
                    if hm >= (open_m - 1):
                        delay_ms = 2_000
                    try:
                        st2["capture_after_id"] = root.after(delay_ms, _tick)
                    except Exception:
                        pass
                    return
                # 개장 이후인데 아직 미고정 → 직전종가 폴백 대기, 5초마다 확인
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
    장전이면 시초가 전 기준 캡처, 장중·장마감 후면 직전 EOD/종가 폴백.
    비거래일만 ``N/A``.
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
        open_m, close_m = _session_open_close_minutes(t.date())

        # 장전 시작: 시초가 전 스냅 → 개장 고정
        if hm < open_m:
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
            return

        # 장중·장마감 후: 장시작 스냅 불가 → 직전 거래일 EOD/종가 폴백 (N/A 고정 안 함)
        st["na"] = False
        why = "장중" if hm <= close_m else "장마감 후"
        try:
            log = getattr(app, "log", None)
            if callable(log):
                log(
                    f"📌 [금일손익] {why} 감시 시작 — "
                    "직전 거래일 EOD/종가 총자산으로 기준 산출"
                )
        except Exception:
            pass
        _apply_fallback_baseline_if_needed(app, now=t)
        # 폴백 비동기 대기 중에도 주기적으로 UI 갱신
        try:
            root = getattr(app, "root", None)
            if root is not None and not st.get("baseline_locked"):

                def _poll_fallback() -> None:
                    try:
                        st2 = ensure_day_state(app)
                        if st2.get("na") or st2.get("baseline_locked"):
                            try:
                                req = getattr(
                                    app, "request_virtual_tab_realtime_render", None
                                )
                                if callable(req):
                                    req()
                            except Exception:
                                pass
                            return
                        if st2.get("baseline") is None:
                            _apply_fallback_baseline_if_needed(app)
                        try:
                            st2["capture_after_id"] = root.after(5_000, _poll_fallback)
                        except Exception:
                            pass
                    except Exception:
                        pass

                try:
                    st["capture_after_id"] = root.after(800, _poll_fallback)
                except Exception:
                    pass
        except Exception:
            pass
    except Exception:
        pass


def format_day_pnl_text(app: Any, current_total_assets: float) -> str:
    """포지션 탭 「금일 손익」 표시 문자열."""
    try:
        now = datetime.now()
        st = ensure_day_state(app, now)
        # 감시 중인데 기준 세션이 비어 있으면(자정 롤오버 누락 등) 즉시 재무장.
        if not st.get("started_at") and _is_app_monitoring(app):
            try:
                on_monitoring_started(app, now=now)
                st = ensure_day_state(app, now)
            except Exception:
                pass
        # 구버전/과거 로직으로 N/A 고정된 거래일 → 폴백으로 복구
        if st.get("na") and _is_trading_day(now.date()) and _is_app_monitoring(app):
            try:
                st["na"] = False
                if not st.get("started_at"):
                    st["started_at"] = now.isoformat(timespec="seconds")
                if st.get("baseline") is None:
                    _apply_fallback_baseline_if_needed(app, now=now)
                st = ensure_day_state(app, now)
            except Exception:
                pass
        if st.get("na"):
            return "N/A"
        if not st.get("started_at"):
            # 장마감 후·자정 롤오버로 기준이 비었으면 원인을 드러냄
            try:
                _o, close_m = _session_open_close_minutes(now.date())
                if _hm(now) > close_m and _is_app_monitoring(app):
                    return "— (장시작 기준 없음)"
            except Exception:
                pass
            return "—"
        if st.get("baseline") is None or not st.get("baseline_locked"):
            try_capture_open_baseline(app, now=now)
            st = ensure_day_state(app, now)
            if st.get("baseline") is None:
                _apply_fallback_baseline_if_needed(app, now=now)
                st = ensure_day_state(app, now)
        baseline = st.get("baseline")
        if baseline is None:
            open_m, close_m = _session_open_close_minutes(now.date())
            hm = _hm(now)
            if hm < open_m:
                return "— (장시작 대기)"
            return "— (기준 계산 중)"
        # 장전에도 기준이 있으면 잠정 표시(개장에 최종 고정)
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
