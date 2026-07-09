# -*- coding: utf-8 -*-
"""
웹소켓(0B) 체결가(fid 10) 파싱·선택적 이상치 필터.

키움 WS fid10 은 하락 시 ``-105700`` 처럼 **부호만 붙은 현재가**가 올 수 있습니다(품질 이슈 아님).
필터는 기본 **꺼짐** — 켰을 때만 극단적 단일 틱(양쪽 기준가 대비 동시 이탈)만 차단합니다.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

from utils.normalization import canonical_stock_code, get_tick_size


def parse_ws_current_price(raw: Any) -> Optional[int]:
    """
    WS fid 10(현재가) 파싱. 실패·0·비현실적 값은 None.

    ``-170300`` / ``+170300`` / ``170,300`` 모두 절대값 현재가로 해석합니다.
    """
    try:
        if raw is None:
            return None
        s = str(raw).strip()
        if not s or s in ("-", "—", "–", "0", "00"):
            return None
        for ch in ("▲", "▼", "△", "▽"):
            s = s.replace(ch, "")
        s = s.replace(",", "").strip()
        if not s:
            return None
        if s[0] in "+-":
            s = s[1:].strip()
        if not s:
            return None
        v = float(s)
        if v != v or v <= 0:
            return None
        if v < 50 or v > 50_000_000:
            return None
        return int(round(v))
    except (TypeError, ValueError):
        return None


def _settings_float(settings: Optional[Dict[str, Any]], key: str, default: float) -> float:
    try:
        if not settings:
            return default
        return float(settings.get(key, default))
    except (TypeError, ValueError):
        return default


def _settings_bool(settings: Optional[Dict[str, Any]], key: str, default: bool) -> bool:
    try:
        if not settings:
            return default
        v = settings.get(key, default)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return default


def _last_accepted_price(info: Optional[Dict[str, Any]]) -> float:
    if not isinstance(info, dict):
        return 0.0
    for fld in ("price", "last_price"):
        try:
            v = info.get(fld)
            if v in (None, "", 0):
                continue
            fv = float(v)
            if fv > 0:
                return fv
        except (TypeError, ValueError):
            continue
    return 0.0


def _official_m1_last_close(m1_store: Any, code: str) -> float:
    try:
        if m1_store is None:
            return 0.0
        fn = getattr(m1_store, "last_completed_close", None)
        if callable(fn):
            return float(fn(code) or 0.0)
    except Exception:
        pass
    return 0.0


def _jump_limit(ref_px: float, settings: Optional[Dict[str, Any]]) -> float:
    """단일 틱 허용 절대 변동(원)."""
    try:
        ref = float(ref_px)
        if ref <= 0:
            return 0.0
        tick = max(1, int(get_tick_size(ref)))
        max_jump_ticks = int(
            _settings_float(settings, "realtime_tick_max_single_jump_ticks", 30)
        )
        max_jump_ticks = max(5, min(max_jump_ticks, 120))
        pct_cap = _settings_float(settings, "realtime_tick_max_single_jump_pct", 1.5)
        pct_cap = max(0.2, min(pct_cap, 10.0))
        return max(float(max_jump_ticks) * float(tick), ref * (pct_cap / 100.0))
    except (TypeError, ValueError):
        return 0.0


# 동일 종목·유사 가격이 연속 거절되면 기준가 고착을 풀기 위한 서킷브레이커
_reject_streak: Dict[str, Dict[str, Any]] = {}
_REJECT_STREAK_WINDOW_SEC = 30.0
_REJECT_STREAK_ACCEPT_AFTER = 4


def should_accept_ws_tick_price(
    code: str,
    new_px: int,
    info: Optional[Dict[str, Any]],
    *,
    m1_store: Any = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    WS 0B 현재가 수용 여부. 기본은 전부 수용(필터 OFF).

    ON 일 때: 직전 수용가·공식 1분 종가 **둘 다** 있는 경우에만,
    **양쪽 모두** 단일 틱 한도를 크게 넘을 때만 거절(오파싱·fid 혼선용).
  한쪽이라도 가까우면 수용 — 워밍업 시가 대비 장중 하락을 오탐하지 않음.
    """
    try:
        if not _settings_bool(settings, "realtime_tick_price_guard_enabled", False):
            return True, ""

        px = int(new_px)
        if px <= 0:
            return False, "price<=0"

        nk = canonical_stock_code(code)
        if not nk:
            return True, ""

        # 연속 거절 시 유사 가격이면 수용(기준가 갱신 불가 악순환 방지)
        try:
            now = time.time()
            st = _reject_streak.get(nk) or {}
            streak_px = float(st.get("px", 0) or 0)
            streak_n = int(st.get("n", 0) or 0)
            streak_ts = float(st.get("ts", 0) or 0)
            if now - streak_ts > _REJECT_STREAK_WINDOW_SEC:
                streak_n = 0
            if (
                streak_n >= _REJECT_STREAK_ACCEPT_AFTER
                and streak_px > 0
                and abs(float(px) - streak_px) <= max(50.0, streak_px * 0.002)
            ):
                _reject_streak.pop(nk, None)
                return True, "circuit_breaker"
        except Exception:
            pass

        refs: list[float] = []
        last_px = _last_accepted_price(info)
        if last_px > 0:
            refs.append(last_px)
        m1_close = _official_m1_last_close(m1_store, code)
        if m1_close > 0 and (last_px <= 0 or abs(m1_close - last_px) > 1.0):
            refs.append(m1_close)

        if not refs:
            return True, ""

        # 하나라도 한도 안이면 정상 체결로 수용
        for ref_px in refs:
            limit = _jump_limit(ref_px, settings)
            if limit <= 0 or abs(float(px) - float(ref_px)) <= limit:
                _reject_streak.pop(nk, None)
                return True, ""

        # 모든 기준가에서 동시에 크게 벗어남 → 거절
        ref0 = refs[0]
        limit0 = _jump_limit(ref0, settings)
        try:
            now = time.time()
            st = _reject_streak.get(nk) or {}
            if (
                now - float(st.get("ts", 0) or 0) <= _REJECT_STREAK_WINDOW_SEC
                and abs(float(px) - float(st.get("px", 0) or 0)) <= max(50.0, px * 0.002)
            ):
                st["n"] = int(st.get("n", 0) or 0) + 1
            else:
                st = {"n": 1, "px": float(px), "ts": now}
            st["ts"] = now
            st["px"] = float(px)
            _reject_streak[nk] = st
        except Exception:
            pass

        return (
            False,
            f"jump_all_refs px={px} refs={[int(r) for r in refs]} limit~{limit0:.0f}",
        )
    except Exception as e:
        return True, f"guard_err:{e!r}"


_reject_log_last: Dict[str, float] = {}
_REJECT_LOG_COOLDOWN_SEC = 120.0


def maybe_log_rejected_tick(
    app: Any,
    code: str,
    raw_px: Any,
    parsed_px: int,
    reason: str,
) -> None:
    """이상 틱 거절 시 앱 로그·JSONL(종목별 2분 쿨다운)."""
    try:
        nk = canonical_stock_code(code)
        if not nk or not reason:
            return
        now = time.time()
        last = float(_reject_log_last.get(nk, 0.0) or 0.0)
        if now - last < _REJECT_LOG_COOLDOWN_SEC:
            return
        _reject_log_last[nk] = now
        msg = (
            f"⚠️ [TickGuard] {nk} 극단 틱 무시 — "
            f"raw={raw_px!r} parsed={parsed_px} reason={reason}"
        )
        try:
            log_fn = getattr(app, "log", None)
            if callable(log_fn):
                log_fn(msg)
        except Exception:
            pass
        try:
            sl = getattr(app, "session_logger", None)
            if sl is not None:
                sl.log(
                    "ws_tick_price_rejected",
                    {
                        "code": nk,
                        "raw": str(raw_px),
                        "parsed": int(parsed_px),
                        "reason": str(reason),
                    },
                )
        except Exception:
            pass
    except Exception:
        pass
