# -*- coding: utf-8 -*-
"""
KRX 서킷브레이커·시장중단 구간의 15분 슬롯 감지·등록.

서킷브레이커 등으로 해당 15분 구간에 체결이 거의 없으면 ka10080(15분) 공식봉이
비거나 ``row_not_found`` 가 전 종목에 동시에 발생할 수 있다. 이 경우 실패가 아니라
**시장중단 슬롯**으로 기록하고 15분 REST 재조회·S1 신호평가를 억제한다.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from utils.bars_data_keys import resolve_bars_data_frame
from utils.krx_bars import is_krx_15m_slot_end, m1_window_start_for_15m_end
from utils.normalization import canonical_stock_code


def _settings_bool(settings: Any, key: str, default: bool = True) -> bool:
    try:
        if settings is None:
            return default
        v = settings.get(key, default)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return default


def _settings_float(settings: Any, key: str, default: float) -> float:
    try:
        return float(settings.get(key, default))
    except (TypeError, ValueError):
        return default


def norm_halt_slot_ts(bar_ts: Any) -> pd.Timestamp:
    """15분 슬롯 종가 라벨(초 단위)."""
    try:
        return pd.Timestamp(bar_ts).floor("s")
    except Exception:
        return pd.Timestamp(bar_ts)


def _halt_probe_bar_on_current_session_day(bar_ts: Any) -> bool:
    """
    M1 정적 프로브·당일 S1 억제는 **오늘(KRX 달력일) 슬롯**에만 적용합니다.

    장 시작 직후 슬롯 롤업이 전일 15:30을 지나갈 때, 당일 M1 스토어가 비어 있으면
    전일 마지막 슬롯을 오탐(시장중단)하는 것을 막습니다.
    """
    try:
        from datetime import datetime

        slot = norm_halt_slot_ts(bar_ts).normalize()
        today = pd.Timestamp(datetime.now()).normalize()
        return slot >= today
    except Exception:
        return False


def _slot_key(bar_ts: Any) -> str:
    try:
        return norm_halt_slot_ts(bar_ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(bar_ts)


class KrxMarketHaltSlotRegistry:
    """세션 단위 시장중단 15분 슬롯 레지스트리."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # slot_key -> {reason, source, marked_at}
        self._halt_slots: Dict[str, Dict[str, Any]] = {}
        # slot_key -> set of codes (official row_not_found)
        self._official_miss_codes: Dict[str, Set[str]] = {}
        self._official_miss_first_at: Dict[str, float] = {}
        self._ui_logged_slots: Set[str] = set()
        # slot_key -> 지연 공식봉 재조회 상태
        self._halt_retry_state: Dict[str, Dict[str, Any]] = {}

    def is_halt(self, bar_ts: Any) -> bool:
        try:
            sk = _slot_key(bar_ts)
            with self._lock:
                return sk in self._halt_slots
        except Exception:
            return False

    def purge_slots_before_day(self, day0: Any) -> int:
        """``day0`` 이전 날짜의 시장중단 슬롯·재조회 상태를 제거합니다."""
        try:
            cutoff = pd.Timestamp(day0).normalize()
            cutoff_key = cutoff.strftime("%Y-%m-%d")
            removed = 0
            with self._lock:
                for sk in list(self._halt_slots.keys()):
                    try:
                        if str(sk)[:10] < cutoff_key:
                            self._halt_slots.pop(sk, None)
                            self._official_miss_codes.pop(sk, None)
                            self._official_miss_first_at.pop(sk, None)
                            self._halt_retry_state.pop(sk, None)
                            self._ui_logged_slots.discard(sk)
                            removed += 1
                    except Exception:
                        continue
            return removed
        except Exception:
            return 0

    def mark(
        self,
        bar_ts: Any,
        *,
        reason: str,
        source: str = "probe",
    ) -> bool:
        """등록. 이미 있으면 False."""
        try:
            sk = _slot_key(bar_ts)
            ts_norm = norm_halt_slot_ts(bar_ts)
            with self._lock:
                if sk in self._halt_slots:
                    return False
                self._halt_slots[sk] = {
                    "bar_ts": ts_norm.strftime("%Y-%m-%d %H:%M:%S"),
                    "reason": str(reason or "")[:200],
                    "source": str(source or "")[:64],
                    "marked_at": time.time(),
                }
                if sk not in self._halt_retry_state:
                    self._halt_retry_state[sk] = {
                        "bar_ts": ts_norm.strftime("%Y-%m-%d %H:%M:%S"),
                        "attempts": 0,
                        "last_attempt_at": 0.0,
                        "success_codes": set(),
                        "recovered": False,
                    }
                return True
        except Exception:
            return False

    def register_official_retry(self, bar_ts: Any) -> None:
        """시장중단 슬롯에 지연 공식봉 재조회 상태를 붙입니다."""
        try:
            sk = _slot_key(bar_ts)
            ts_norm = norm_halt_slot_ts(bar_ts)
            with self._lock:
                row = self._halt_retry_state.get(sk)
                if row is None:
                    self._halt_retry_state[sk] = {
                        "bar_ts": ts_norm.strftime("%Y-%m-%d %H:%M:%S"),
                        "attempts": 0,
                        "last_attempt_at": 0.0,
                        "success_codes": set(),
                        "recovered": False,
                    }
                elif row.get("recovered"):
                    row["recovered"] = False
        except Exception:
            pass

    def pending_halt_retries(self) -> List[Tuple[str, Dict[str, Any]]]:
        """복구되지 않은 시장중단 슬롯 (slot_key, retry_state)."""
        out: List[Tuple[str, Dict[str, Any]]] = []
        try:
            with self._lock:
                for sk, halt in self._halt_slots.items():
                    rs = self._halt_retry_state.get(sk)
                    if rs is None:
                        continue
                    if bool(rs.get("recovered")):
                        continue
                    merged = {
                        "bar_ts": halt.get("bar_ts") or rs.get("bar_ts"),
                        "reason": halt.get("reason", ""),
                        "attempts": int(rs.get("attempts") or 0),
                        "last_attempt_at": float(rs.get("last_attempt_at") or 0.0),
                        "success_count": len(rs.get("success_codes") or set()),
                    }
                    out.append((sk, merged))
        except Exception:
            pass
        return out

    def mark_retry_attempt(self, bar_ts: Any) -> int:
        """재조회 배치 시도 횟수 증가."""
        try:
            sk = _slot_key(bar_ts)
            with self._lock:
                rs = self._halt_retry_state.get(sk)
                if rs is None:
                    return 0
                rs["attempts"] = int(rs.get("attempts") or 0) + 1
                rs["last_attempt_at"] = time.monotonic()
                return int(rs["attempts"])
        except Exception:
            return 0

    def note_official_retry_success(self, bar_ts: Any, code: str) -> int:
        """공식봉 재조회 성공 종목 수."""
        try:
            sk = _slot_key(bar_ts)
            nc = canonical_stock_code(str(code).strip())
            if not nc:
                return 0
            with self._lock:
                rs = self._halt_retry_state.get(sk)
                if rs is None:
                    return 0
                codes = rs.get("success_codes")
                if not isinstance(codes, set):
                    codes = set()
                    rs["success_codes"] = codes
                codes.add(nc)
                return len(codes)
        except Exception:
            return 0

    def clear_halt_recovered(
        self,
        bar_ts: Any,
        *,
        reason: str,
        success_count: int = 0,
    ) -> bool:
        """공식봉 복구 완료 — 시장중단 등록 해제."""
        try:
            sk = _slot_key(bar_ts)
            with self._lock:
                if sk not in self._halt_slots:
                    rs = self._halt_retry_state.get(sk)
                    if rs is not None:
                        rs["recovered"] = True
                    return False
                self._halt_slots.pop(sk, None)
                self._official_miss_codes.pop(sk, None)
                self._official_miss_first_at.pop(sk, None)
                rs = self._halt_retry_state.get(sk)
                if rs is not None:
                    rs["recovered"] = True
            return True
        except Exception:
            return False

    def record_official_miss(self, bar_ts: Any, code: str) -> int:
        """공식 15m 행 미발견 종목 수(고유 코드)."""
        try:
            sk = _slot_key(bar_ts)
            nc = canonical_stock_code(str(code).strip())
            if not nc:
                return 0
            now_m = time.monotonic()
            with self._lock:
                if sk not in self._official_miss_codes:
                    self._official_miss_codes[sk] = set()
                    self._official_miss_first_at[sk] = now_m
                self._official_miss_codes[sk].add(nc)
                return len(self._official_miss_codes[sk])
        except Exception:
            return 0

    def clear_official_miss(self, bar_ts: Any) -> None:
        try:
            sk = _slot_key(bar_ts)
            with self._lock:
                self._official_miss_codes.pop(sk, None)
                self._official_miss_first_at.pop(sk, None)
        except Exception:
            pass

    def halt_info(self, bar_ts: Any) -> Optional[Dict[str, Any]]:
        try:
            sk = _slot_key(bar_ts)
            with self._lock:
                row = self._halt_slots.get(sk)
                return dict(row) if row else None
        except Exception:
            return None

    def should_log_ui(self, bar_ts: Any) -> bool:
        try:
            sk = _slot_key(bar_ts)
            with self._lock:
                if sk in self._ui_logged_slots:
                    return False
                self._ui_logged_slots.add(sk)
                return True
        except Exception:
            return True


def get_market_halt_registry(app: Any) -> KrxMarketHaltSlotRegistry:
    """``app`` 에 레지스트리를 붙여 재사용."""
    try:
        reg = getattr(app, "krx_market_halt_slot_registry", None)
        if isinstance(reg, KrxMarketHaltSlotRegistry):
            return reg
        reg = KrxMarketHaltSlotRegistry()
        setattr(app, "krx_market_halt_slot_registry", reg)
        return reg
    except Exception:
        return KrxMarketHaltSlotRegistry()


def market_halt_detection_enabled(app: Any) -> bool:
    try:
        st = getattr(app, "settings", None) or {}
        return _settings_bool(st, "realtime_market_halt_slot_detection", True)
    except Exception:
        return True


def is_market_halt_15m_slot(app: Any, bar_ts: Any) -> bool:
    if not market_halt_detection_enabled(app):
        return False
    try:
        if not is_krx_15m_slot_end(bar_ts):
            return False
    except Exception:
        return False
    return get_market_halt_registry(app).is_halt(bar_ts)


def mark_market_halt_15m_slot(
    app: Any,
    bar_ts: Any,
    *,
    reason: str,
    source: str = "probe",
    log_ui: bool = True,
) -> bool:
    """시장중단 슬롯 등록 + JSONL/UI 1회 로그."""
    if not market_halt_detection_enabled(app):
        return False
    try:
        if not is_krx_15m_slot_end(bar_ts):
            return False
    except Exception:
        return False

    reg = get_market_halt_registry(app)
    if not reg.mark(bar_ts, reason=reason, source=source):
        return False

    ts_s = norm_halt_slot_ts(bar_ts).strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "bar_ts": ts_s,
        "reason": str(reason or "")[:200],
        "source": str(source or "")[:64],
    }
    try:
        sl = getattr(app, "session_logger", None)
        if sl is not None:
            sl.log("market_halt_15m_slot", payload)
    except Exception:
        pass

    if log_ui and reg.should_log_ui(bar_ts):
        try:
            log_fn = getattr(app, "log", None)
            if callable(log_fn):
                log_fn(
                    f"ℹ️ [시장중단-15m] {ts_s} 슬롯 — "
                    f"즉시 공식 15m 보정·S1 평가 억제, 지연 공식봉 재조회 예약 ({reason})"
                )
        except Exception:
            pass

    reg.register_official_retry(bar_ts)
    try:
        schedule_halt_slot_official_retry_batch(
            app,
            bar_ts,
            reason="halt_marked",
            first_batch=True,
        )
    except Exception:
        pass
    return True


def purge_stale_market_halt_slots(app: Any) -> int:
    """당일 이전에 등록된 시장중단 슬롯을 정리합니다(세션 시작·감시 재개 시)."""
    try:
        from datetime import datetime

        reg = get_market_halt_registry(app)
        return reg.purge_slots_before_day(pd.Timestamp(datetime.now()).normalize())
    except Exception:
        return 0


def probe_m1_quiet_market_halt(app: Any, bar_ts: Any) -> bool:
    """
    감시 종목 M1 합산 거래량이 극히 적으면 시장중단 후보로 판단.

    M1 수집이 늦거나 일부 분봉만 들어온 상태에서는 정상 장도 거래량 합계가 작게
    보일 수 있으므로, 충분한 1분봉 커버리지가 있는 종목만 판정에 사용합니다.
    """
    if not market_halt_detection_enabled(app):
        return False
    if not _halt_probe_bar_on_current_session_day(bar_ts):
        return False
    try:
        st = getattr(app, "settings", None) or {}
    except Exception:
        st = {}
    try:
        ratio_need = _settings_float(st, "realtime_market_halt_m1_quiet_ratio", 0.70)
        max_vol = _settings_float(st, "realtime_market_halt_m1_max_volume", 50.0)
        min_codes = max(3, int(_settings_float(st, "realtime_market_halt_m1_min_codes", 6)))
        min_bars_per_code = max(
            1,
            int(_settings_float(st, "realtime_market_halt_m1_min_bars_per_code", 8)),
        )
        scale_volume_by_bars = _settings_bool(
            st,
            "realtime_market_halt_m1_scale_volume_by_bars",
            True,
        )
    except (TypeError, ValueError):
        ratio_need, max_vol, min_codes = 0.70, 50.0, 6
        min_bars_per_code, scale_volume_by_bars = 8, True

    m1s = getattr(app, "rt_tick_m1_store", None)
    if m1s is None:
        return False

    codes = list(getattr(app, "_active_stream_codes", None) or [])
    if not codes:
        return False

    try:
        slot_end = norm_halt_slot_ts(bar_ts)
        t0 = m1_window_start_for_15m_end(slot_end)
    except Exception:
        return False

    quiet = 0
    checked = 0
    with_m1_rows = 0
    eligible = 0
    for c in codes:
        try:
            nk = canonical_stock_code(str(c).strip())
            if not nk:
                continue
            try:
                m1df = m1s.dataframe_for_window(nk, t0, slot_end, include_open_bucket=False)
            except TypeError:
                m1df = m1s.dataframe_for_window(nk, t0, slot_end)
            checked += 1
            if m1df is None or getattr(m1df, "empty", True):
                continue
            with_m1_rows += 1
            try:
                row_count = int(len(m1df.index))
            except Exception:
                row_count = 0
            if row_count < min_bars_per_code:
                continue
            eligible += 1
            try:
                vol_sum = float(m1df["Volume"].sum())
            except Exception:
                vol_sum = 0.0
            threshold = max_vol
            if scale_volume_by_bars:
                try:
                    threshold = max_vol * (float(row_count) / 15.0)
                except Exception:
                    threshold = max_vol
            if vol_sum <= threshold:
                quiet += 1
        except Exception:
            continue

    if checked < min_codes:
        return False
    # M1 구간에 행 자체가 없으면 '거래 정적'이 아니라 아직 미수집(장 시작·롤업 직후)
    if with_m1_rows < min_codes:
        return False
    # 15분 창 일부만 수집된 종목으로는 시장중단을 확정하지 않는다.
    if eligible < min_codes:
        return False
    try:
        return (float(quiet) / float(eligible)) >= ratio_need
    except Exception:
        return False


def record_rt15m_official_miss_and_maybe_halt(
    app: Any,
    bar_ts: Any,
    code: str,
) -> bool:
    """
    공식 15m ``row_not_found`` 누적. 임계 종목 수 이상이면 시장중단 슬롯으로 등록.

    Returns:
        등록·이미 등록된 시장중단 슬롯이면 True (실패 로그·재시도 억제용).
    """
    if not market_halt_detection_enabled(app):
        return False
    if is_market_halt_15m_slot(app, bar_ts):
        return True

    try:
        st = getattr(app, "settings", None) or {}
        min_codes = max(
            2, int(_settings_float(st, "realtime_market_halt_official_miss_min_codes", 6))
        )
        window_sec = max(
            30.0,
            _settings_float(st, "realtime_market_halt_official_miss_window_sec", 180.0),
        )
    except (TypeError, ValueError):
        min_codes, window_sec = 6, 180.0

    reg = get_market_halt_registry(app)
    n = reg.record_official_miss(bar_ts, code)

    # 윈도우 밖 누적이면 리셋 후 재집계
    try:
        sk = _slot_key(bar_ts)
        with reg._lock:
            first_at = float(reg._official_miss_first_at.get(sk, 0.0) or 0.0)
        if first_at > 0 and (time.monotonic() - first_at) > window_sec:
            reg.clear_official_miss(bar_ts)
            n = reg.record_official_miss(bar_ts, code)
    except Exception:
        pass

    if n < min_codes:
        return False

    mark_market_halt_15m_slot(
        app,
        bar_ts,
        reason=f"공식15m {n}종목 행 미발견(시장중단 자동 추정)",
        source="official_miss_cluster",
    )
    return True


def probe_and_maybe_mark_halt_slot(app: Any, bar_ts: Any) -> bool:
    """M1 거래 정적 프로브 후 필요 시 시장중단 슬롯 등록."""
    if not _halt_probe_bar_on_current_session_day(bar_ts):
        return False
    if is_market_halt_15m_slot(app, bar_ts):
        return True
    if not probe_m1_quiet_market_halt(app, bar_ts):
        return False
    return mark_market_halt_15m_slot(
        app,
        bar_ts,
        reason="M1 거래 정적(자동 추정, 오탐 가능)",
        source="m1_quiet_probe",
    )


def should_skip_rt15m_official_replace(app: Any, bar_ts: Any) -> bool:
    """15분 공식 REST 덮어쓰기 스킵 여부."""
    if not market_halt_detection_enabled(app):
        return False
    try:
        st = getattr(app, "settings", None) or {}
        if not _settings_bool(st, "realtime_market_halt_skip_official_replace", True):
            return False
    except Exception:
        pass
    if is_market_halt_15m_slot(app, bar_ts):
        return True
    if probe_and_maybe_mark_halt_slot(app, bar_ts):
        return True
    return False


def last_closed_15m_bar_ts(app: Any, code: str) -> Optional[pd.Timestamp]:
    """종목 ``bars_data`` 의 마지막 **확정** 15분 슬롯 종가 시각."""
    try:
        nk = canonical_stock_code(str(code).strip())
        if not nk:
            return None
        bd = getattr(app, "bars_data", None)
        if not isinstance(bd, dict):
            return None
        lk = getattr(app, "_bars_data_lock", None)

        def _read() -> Optional[pd.Timestamp]:
            _k, df = resolve_bars_data_frame(bd, nk, migrate_alias=True)
            if df is None or getattr(df, "empty", True):
                return None
            try:
                from core.realtime_official_bars_sync import stream_slot_end_datetime
                from datetime import datetime

                slot_end = stream_slot_end_datetime(datetime.now())
                last_ix = pd.Timestamp(df.index[-1])
                if last_ix >= slot_end and len(df.index) >= 2:
                    return pd.Timestamp(df.index[-2])
                return last_ix
            except Exception:
                if len(df.index) >= 2:
                    return pd.Timestamp(df.index[-2])
                return pd.Timestamp(df.index[-1])

        if lk is not None:
            with lk:
                return _read()
        return _read()
    except Exception:
        return None


def should_suppress_s1_eval_for_code(app: Any, code: str) -> Tuple[bool, str]:
    """
    S1 신호평가 억제 여부.

    Returns:
        (억제 여부, UI confluence 문구)
    """
    if not market_halt_detection_enabled(app):
        return False, ""
    try:
        st = getattr(app, "settings", None) or {}
        if not _settings_bool(st, "realtime_market_halt_suppress_s1_eval", True):
            return False, ""
    except Exception:
        pass

    closed_ts = last_closed_15m_bar_ts(app, code)
    if closed_ts is None:
        return False, ""

    # 전일(이전 달력일) 확정봉에 걸린 시장중단은 당일 S1 억제에 쓰지 않음
    if not _halt_probe_bar_on_current_session_day(closed_ts):
        return False, ""

    if is_market_halt_15m_slot(app, closed_ts):
        info = get_market_halt_registry(app).halt_info(closed_ts) or {}
        rs = str(info.get("reason") or "").strip()
        msg = "시장중단(15m)"
        if rs:
            msg = f"{msg} · {rs[:40]}"
        return True, msg

    if probe_and_maybe_mark_halt_slot(app, closed_ts):
        return True, "시장중단(15m) · M1 거래 정적"

    return False, ""


def halt_official_retry_enabled(app: Any) -> bool:
    try:
        st = getattr(app, "settings", None) or {}
        return _settings_bool(st, "realtime_market_halt_official_retry_enabled", True)
    except Exception:
        return True


def _halt_retry_settings(app: Any) -> Tuple[int, float, float, int, float]:
    """max_attempts, min_interval_sec, first_delay_sec, min_codes_to_clear, fail_cooldown_sec."""
    try:
        st = getattr(app, "settings", None) or {}
    except Exception:
        st = {}
    try:
        max_attempts = max(
            1, int(_settings_float(st, "realtime_market_halt_official_retry_max_attempts", 8))
        )
        min_interval = max(
            60.0,
            _settings_float(st, "realtime_market_halt_official_retry_min_interval_sec", 600.0),
        )
        first_delay = max(
            30.0,
            _settings_float(st, "realtime_market_halt_official_retry_first_delay_sec", 180.0),
        )
        min_clear = max(
            1, int(_settings_float(st, "realtime_market_halt_official_retry_min_codes_to_clear", 3))
        )
        fail_cd = max(
            15.0,
            _settings_float(st, "realtime_market_halt_official_retry_fail_cooldown_sec", 60.0),
        )
        return max_attempts, min_interval, first_delay, min_clear, fail_cd
    except (TypeError, ValueError):
        return 8, 600.0, 180.0, 3, 60.0


def notify_halt_official_retry_success(
    app: Any,
    bar_ts: Any,
    code: str,
    *,
    volume: float = 0.0,
) -> None:
    """지연 공식봉 재조회 성공 시 복구 판정."""
    if not halt_official_retry_enabled(app):
        return
    if not is_market_halt_15m_slot(app, bar_ts):
        return

    reg = get_market_halt_registry(app)
    n_ok = reg.note_official_retry_success(bar_ts, code)
    _, _, _, min_clear, _ = _halt_retry_settings(app)

    try:
        vol = float(volume)
    except (TypeError, ValueError):
        vol = 0.0

    should_clear = n_ok >= min_clear or (n_ok >= 1 and vol > 0.0)
    if not should_clear:
        return

    ts_s = norm_halt_slot_ts(bar_ts).strftime("%Y-%m-%d %H:%M:%S")
    reason = f"공식15m 재조회 {n_ok}종목 확보"
    if reg.clear_halt_recovered(bar_ts, reason=reason, success_count=n_ok):
        payload = {
            "bar_ts": ts_s,
            "reason": reason,
            "success_codes": n_ok,
        }
        try:
            sl = getattr(app, "session_logger", None)
            if sl is not None:
                sl.log("market_halt_15m_recovered", payload)
        except Exception:
            pass
        try:
            log_fn = getattr(app, "log", None)
            if callable(log_fn):
                log_fn(
                    f"✅ [시장중단-15m] {ts_s} 슬롯 공식봉 복구 — "
                    f"S1 평가 재개 ({reason})"
                )
        except Exception:
            pass


def schedule_halt_slot_official_retry_batch(
    app: Any,
    bar_ts: Any,
    *,
    reason: str = "boundary",
    first_batch: bool = False,
) -> int:
    """
    시장중단 슬롯에 대해 감시 종목 전체 공식 15m 재조회를 스케줄합니다.

    Returns:
        스케줄한 종목 수.
    """
    if not halt_official_retry_enabled(app):
        return 0
    if not is_market_halt_15m_slot(app, bar_ts):
        return 0

    sched = getattr(app, "rt_closed_15m_official_scheduler", None)
    if sched is None:
        return 0

    max_attempts, min_interval, first_delay, _, fail_cd = _halt_retry_settings(app)
    reg = get_market_halt_registry(app)

    try:
        sk = _slot_key(bar_ts)
        with reg._lock:
            rs = reg._halt_retry_state.get(sk) or {}
            attempts = int(rs.get("attempts") or 0)
            last_at = float(rs.get("last_attempt_at") or 0.0)
    except Exception:
        attempts, last_at = 0, 0.0

    if attempts >= max_attempts:
        return 0

    now_m = time.monotonic()
    if first_batch:
        if attempts > 0:
            return 0
        delay_sec = first_delay
    else:
        if last_at > 0.0 and (now_m - last_at) < min_interval:
            return 0
        try:
            from core.realtime_official_bars_sync import stream_slot_end_datetime
            from datetime import datetime

            cur_closed = stream_slot_end_datetime(datetime.now())
            halt_ts = norm_halt_slot_ts(bar_ts)
            if pd.Timestamp(cur_closed) <= halt_ts:
                return 0
        except Exception:
            pass
        delay_sec = 2.5

    reg.mark_retry_attempt(bar_ts)
    codes = list(getattr(app, "_active_stream_codes", None) or [])
    if not codes:
        return 0

    try:
        st = getattr(app, "settings", None) or {}
        base_dly = float(st.get("realtime_closed_15m_official_replace_delay_sec", 2.5))
    except (TypeError, ValueError):
        base_dly = 2.5

    n_sched = 0
    for c in codes:
        try:
            sched.schedule(
                app=app,
                code=c,
                bar_ts=bar_ts,
                delay_sec=base_dly,
                force_halt_retry=True,
                halt_retry_fail_cooldown_sec=fail_cd,
                prepend_delay_sec=delay_sec if first_batch else 0.0,
            )
            n_sched += 1
        except Exception:
            continue

    if n_sched > 0:
        try:
            sl = getattr(app, "session_logger", None)
            if sl is not None:
                sl.log(
                    "market_halt_15m_official_retry_scheduled",
                    {
                        "bar_ts": norm_halt_slot_ts(bar_ts).strftime("%Y-%m-%d %H:%M:%S"),
                        "reason": str(reason or "")[:64],
                        "codes": n_sched,
                        "attempt": attempts + 1,
                        "delay_sec": round(float(delay_sec), 1),
                    },
                )
        except Exception:
            pass
    return n_sched


def maybe_schedule_halt_slot_official_retries(app: Any) -> None:
    """슬롯 경계마다 미복구 시장중단 슬롯 공식봉 재조회."""
    if not halt_official_retry_enabled(app):
        return
    reg = get_market_halt_registry(app)
    for _sk, info in reg.pending_halt_retries():
        try:
            bt = info.get("bar_ts")
            if not bt:
                continue
            schedule_halt_slot_official_retry_batch(
                app,
                bt,
                reason="slot_boundary",
                first_batch=False,
            )
        except Exception:
            continue
