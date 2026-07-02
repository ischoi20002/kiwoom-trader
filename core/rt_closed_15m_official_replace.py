# -*- coding: utf-8 -*-
"""
15분 슬롯이 바뀔 때 확정된 직전 봉을, 짧은 지연 후 키움 ka10080(15분)으로 조회해
메모리 ``bars_data`` 및 RT15m DB에 덮어씁니다(틱 누락 시 OHLCV 정합성).

- 완성 봉 한 줄만 갱신(형성 중 현재 봉은 틱 경로 유지).
- 종목·슬롯당 스케줄 1회(중복 타이머 방지).
- 조회는 데몬 스레드, ``bars_data`` 반영은 Tk ``after(0)`` 로 메인 스레드.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd

from core.ka10080_spread import (
    effective_m1_poll_spread_sec,
    effective_rt15m_stagger_spread_sec,
    _active_stream_code_count,
)
from utils.krx_bars import is_krx_15m_slot_end
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


def compute_rt15m_official_replace_delay_sec(
    app: Any,
    code: str,
    bar_ts: Any,
    *,
    fallback_delay_sec: float = 2.5,
) -> float:
    """
    15분 확정 ka10080 조회 지연(초).

    M1 폴러가 매 분 ``[0, m1_spread)`` 에 ka10080(1분)을 쓰므로, 15분 REST는
    ``m1_spread + guard`` 이후부터 ``spread`` 구간에 종목별로 분산합니다.
    (15분 슬롯 안에만 완료되면 됨 — M1과 경쟁하지 않도록.)
    """
    try:
        settings = getattr(app, "settings", None) or {}
    except Exception:
        settings = {}

    if not _settings_bool(
        settings, "realtime_closed_15m_official_replace_stagger", True
    ):
        try:
            return max(0.5, float(fallback_delay_sec))
        except (TypeError, ValueError):
            return 2.5

    try:
        n_codes = _active_stream_code_count(app)
    except Exception:
        n_codes = 1

    m1_spread = effective_m1_poll_spread_sec(settings, n_codes)

    try:
        guard = float(settings.get("realtime_m1_ka10080_guard_sec", 12.0))
    except (TypeError, ValueError):
        guard = 12.0
    guard = max(0.0, min(guard, 120.0))

    try:
        min_d = float(
            settings.get("realtime_closed_15m_official_replace_min_delay_sec", 0.0)
        )
    except (TypeError, ValueError):
        min_d = 0.0
    floor_d = m1_spread + guard
    if min_d <= 0.0:
        min_d = floor_d
    else:
        min_d = max(min_d, floor_d)

    spread = effective_rt15m_stagger_spread_sec(settings, n_codes)

    try:
        nk = canonical_stock_code(str(code).strip())
        sk = _norm_bar_ts(bar_ts).strftime("%Y-%m-%d %H:%M:%S")
        h = abs(hash((nk, sk)))
        frac = (h % 1_000_003) / 1_000_003.0
    except Exception:
        frac = 0.0
    return min_d + spread * frac


def ts_to_slot_hhmm(ts: Any) -> str:
    """완성 15분봉 인덱스(봉 종가 시각) → ``time`` 컬럼용 HHMM."""
    try:
        t = pd.Timestamp(ts)
        return f"{t.hour:02d}{t.minute:02d}"
    except Exception:
        return "0000"


def _minute_api_raw_to_15m_ohlcv_df(raw_api: Any) -> pd.DataFrame:
    """
    ka10080 응답 리스트 → 15분봉 OHLCV DataFrame (tic_scope=15 전제).
    ``WarmupService._minute_api_list_to_15m_ohlcv`` 와 동일 규약.
    """
    if not raw_api:
        return pd.DataFrame()
    try:
        df = pd.DataFrame(raw_api)
        f_cl, f_op, f_hg, f_lw, f_tm, f_vl = (
            "cur_prc",
            "open_pric",
            "high_pric",
            "low_pric",
            "cntr_tm",
            "trde_qty",
        )
        for c in (f_cl, f_op, f_hg, f_lw, f_vl):
            if c not in df.columns:
                return pd.DataFrame()
            df[c] = pd.to_numeric(
                df[c].astype(str).str.replace("+", "").str.replace("-", ""),
                errors="coerce",
            ).fillna(0)
        df["datetime"] = pd.to_datetime(
            df[f_tm].astype(str), format="%Y%m%d%H%M%S", errors="coerce"
        )
        df = df.dropna(subset=["datetime"])
        if df.empty:
            return pd.DataFrame()
        df = df.set_index("datetime").sort_index()
        df = df[~df.index.duplicated(keep="last")]
        out = df[[f_op, f_hg, f_lw, f_cl, f_vl]].copy()
        out.rename(
            columns={
                f_op: "Open",
                f_hg: "High",
                f_lw: "Low",
                f_cl: "Close",
                f_vl: "Volume",
            },
            inplace=True,
        )
        return out[["Open", "High", "Low", "Close", "Volume"]].copy()
    except Exception:
        return pd.DataFrame()


def _norm_bar_ts(ts: Any) -> pd.Timestamp:
    try:
        return pd.Timestamp(ts).floor("s")
    except Exception:
        return pd.Timestamp(ts)


def _find_row_for_bar_end(
    merged: pd.DataFrame, bar_ts: Any
) -> Optional[pd.Series]:
    """API로 모은 15m 프레임에서 ``bar_ts``(초 단위)와 일치하는 행."""
    try:
        if merged is None or getattr(merged, "empty", True):
            return None
        want = _norm_bar_ts(bar_ts)
        idx = merged.index
        try:
            loc = idx.get_loc(want)
        except KeyError:
            for i, t in enumerate(idx):
                if _norm_bar_ts(t) == want:
                    return merged.iloc[i]
            return None
        if isinstance(loc, slice):
            row = merged.iloc[loc.start : loc.stop]
            if row.empty:
                return None
            return row.iloc[-1]
        if isinstance(loc, int):
            return merged.iloc[loc]
        sub = merged.loc[merged.index == want]
        if sub.empty:
            return None
        return sub.iloc[-1]
    except Exception:
        return None


def fetch_official_15m_row_for_timestamp(
    data_manager: Any,
    code: str,
    token: str,
    bar_ts: Any,
    *,
    tic_scope: str = "15",
    max_pages: int = 8,
    page_delay_min: float = 0.08,
    page_delay_max: float = 0.22,
    max_retries: int = 3,
    inter_retry_sec: float = 0.6,
    log: Optional[Callable[[str], None]] = None,
) -> Optional[Tuple[float, float, float, float, float]]:
    """
    ka10080(15분) 연속조회로 ``bar_ts`` 에 해당하는 완성 봉 한 줄을 찾습니다.

    Returns:
        (open, high, low, close, volume) 또는 실패 시 None.
    """
    nc = canonical_stock_code(str(code).strip())
    if not nc or not token:
        return None
    want = _norm_bar_ts(bar_ts)
    mp = max(1, min(int(max_pages), 40))
    retries = max(1, min(int(max_retries), 8))

    for attempt in range(retries):
        try:
            merged_rows: List[pd.DataFrame] = []
            nk = ""
            for _page in range(mp):
                raw_list, nk_out = data_manager.fetch_kiwoom_minute_chart_page(
                    nc,
                    token,
                    nk,
                    tic_scope=str(tic_scope).strip() or "15",
                    fetch_context="rt15m_official",
                )
                if not raw_list:
                    break
                chunk = _minute_api_raw_to_15m_ohlcv_df(raw_list)
                if chunk is not None and not chunk.empty:
                    merged_rows.append(chunk)
                nk = (nk_out or "").strip()
                if not nk:
                    break
                try:
                    lo = float(page_delay_min)
                    hi = float(page_delay_max)
                except (TypeError, ValueError):
                    lo, hi = 0.08, 0.22
                hi = max(lo, hi)
                time.sleep(random.uniform(max(0.0, lo), hi))

            if not merged_rows:
                if attempt + 1 >= retries:
                    try:
                        meta = getattr(data_manager, "last_ka10080_fetch_meta", None) or {}
                        # HTTP 실패는 data_manager 가 기록 — 차트는 OK 인데 15m 병합만 비었을 때만 추가
                        if bool(meta.get("ok")):
                            from core.ka10080_fetch_diag import ka10080_fetch_diag

                            ka10080_fetch_diag.record_fail(
                                nc,
                                tic_scope=str(tic_scope or "15"),
                                context="rt15m_official",
                                reason="merged_empty",
                                http_status=int(meta.get("http_status") or 0),
                                detail=f"no_15m_rows bar={want}",
                                attempt=attempt + 1,
                            )
                    except Exception:
                        pass
                break
            merged = pd.concat(merged_rows).sort_index()
            merged = merged[~merged.index.duplicated(keep="last")]
            row = _find_row_for_bar_end(merged, want)
            if row is None:
                if attempt + 1 >= retries:
                    try:
                        from core.ka10080_fetch_diag import ka10080_fetch_diag

                        ka10080_fetch_diag.record_fail(
                            nc,
                            tic_scope=str(tic_scope or "15"),
                            context="rt15m_official",
                            reason="row_not_found",
                            detail=f"bar_ts={want} pages≤{mp}",
                            attempt=attempt + 1,
                        )
                    except Exception:
                        pass
                if log and (attempt + 1 >= retries):
                    try:
                        log(
                            f"ℹ️ [RT15m-Official] {nc} {want} 행 미발견 "
                            f"(pages≤{mp}, attempt {attempt + 1}/{retries})"
                        )
                    except Exception:
                        pass
            else:
                try:
                    o = float(row["Open"])
                    h = float(row["High"])
                    l_ = float(row["Low"])
                    c = float(row["Close"])
                    v = float(row["Volume"])
                    if o > 0 or h > 0 or l_ > 0 or c > 0:
                        return (o, h, l_, c, v)
                except Exception:
                    pass
        except Exception as e:
            if attempt + 1 >= retries:
                try:
                    from core.ka10080_fetch_diag import ka10080_fetch_diag

                    ka10080_fetch_diag.record_fail(
                        nc,
                        tic_scope=str(tic_scope or "15"),
                        context="rt15m_official",
                        reason="exception",
                        detail=repr(e),
                        attempt=attempt + 1,
                    )
                except Exception:
                    pass
            if log:
                try:
                    log(
                        f"⚠️ [RT15m-Official] {nc} 조회 예외 "
                        f"(attempt {attempt + 1}/{retries}): {e!r}"
                    )
                except Exception:
                    pass
        if attempt + 1 < retries:
            try:
                time.sleep(max(0.1, float(inter_retry_sec)))
            except (TypeError, ValueError):
                time.sleep(0.6)
    return None


class RtClosed15mOfficialReplaceScheduler:
    """
    슬롯 확정 콜백에서 ``schedule`` 만 호출합니다.
    HTTP는 지연 후 데몬 스레드에서 수행하고, 결과는 ``KiwoomApp._apply_official_closed_15m_bar`` 로 전달합니다.
    """

    def __init__(self, log: Optional[Callable[[str], None]] = None) -> None:
        self._log = log
        self._lock = threading.Lock()
        self._scheduled: Dict[str, bool] = {}
        # 실패(행 미발견 등) 이후 동일 키를 일정 시간 동안 재시도/로그하지 않기 위한 쿨다운
        self._fail_cooldown_until: Dict[str, float] = {}
        self._fail_counts: Dict[str, int] = {}

    def _key(self, code: str, bar_ts: Any) -> str:
        nc = canonical_stock_code(str(code).strip())
        try:
            s = _norm_bar_ts(bar_ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            s = str(bar_ts)
        return f"{nc}|{s}"

    def schedule(
        self,
        *,
        app: Any,
        code: str,
        bar_ts: Any,
        delay_sec: float,
    ) -> None:
        """이미 동일 종목·동일 봉 시각이 예약돼 있으면 무시합니다."""
        try:
            if not is_krx_15m_slot_end(bar_ts):
                return
        except Exception:
            return
        k = self._key(code, bar_ts)
        with self._lock:
            if self._scheduled.get(k):
                return
            try:
                now_m = time.monotonic()
                until = float(self._fail_cooldown_until.get(k, 0.0) or 0.0)
                if until > now_m:
                    return
            except Exception:
                pass
            self._scheduled[k] = True

        def _delay() -> float:
            try:
                return compute_rt15m_official_replace_delay_sec(
                    app, code, bar_ts, fallback_delay_sec=delay_sec
                )
            except Exception:
                try:
                    return max(0.5, float(delay_sec))
                except (TypeError, ValueError):
                    return 2.5

        def _worker() -> None:
            try:
                wait_s = _delay()
                time.sleep(wait_s)
            except Exception:
                pass
            try:
                mon = bool(getattr(app, "is_monitoring", False)) or bool(
                    getattr(
                        getattr(app, "trading_controller", None),
                        "is_monitoring",
                        False,
                    )
                )
                if not mon:
                    with self._lock:
                        self._scheduled.pop(k, None)
                    return
                token = None
                try:
                    token = app.auth_manager.get_auth_token()
                except Exception:
                    token = None
                if not token:
                    if self._log:
                        try:
                            cc = canonical_stock_code(str(code).strip())
                            self._log(
                                f"⚠️ [RT15m-Official] 토큰 없음 — 스킵 ({cc})"
                            )
                        except Exception:
                            pass
                    with self._lock:
                        self._scheduled.pop(k, None)
                    return
                dm = getattr(app, "data_manager", None)
                if dm is None:
                    with self._lock:
                        self._scheduled.pop(k, None)
                    return
                try:
                    settings = getattr(app, "settings", None) or {}
                except Exception:
                    settings = {}
                try:
                    tic = str(
                        settings.get("realtime_warmup_ka10080_tic_scope", "15")
                    ).strip() or "15"
                except Exception:
                    tic = "15"
                try:
                    mp = int(
                        float(
                            settings.get(
                                "realtime_closed_15m_official_replace_max_pages",
                                8,
                            )
                        )
                    )
                except (TypeError, ValueError):
                    mp = 8
                try:
                    mr = int(
                        float(
                            settings.get(
                                "realtime_closed_15m_official_replace_max_retries",
                                3,
                            )
                        )
                    )
                except (TypeError, ValueError):
                    mr = 3
                try:
                    ir = float(
                        settings.get(
                            "realtime_closed_15m_official_replace_inter_retry_sec",
                            0.6,
                        )
                    )
                except (TypeError, ValueError):
                    ir = 0.6
                try:
                    dmin = float(
                        settings.get("realtime_warmup_ka10080_page_delay_min", 0.08)
                    )
                    dmax = float(
                        settings.get("realtime_warmup_ka10080_page_delay_max", 0.22)
                    )
                except (TypeError, ValueError):
                    dmin, dmax = 0.08, 0.22

                tup = fetch_official_15m_row_for_timestamp(
                    dm,
                    code,
                    token,
                    bar_ts,
                    tic_scope=tic,
                    max_pages=mp,
                    page_delay_min=dmin,
                    page_delay_max=dmax,
                    max_retries=mr,
                    inter_retry_sec=ir,
                    log=self._log,
                )
                root = getattr(app, "root", None)
                if tup is None or root is None:
                    # 행 미발견/메인 root 없음 등 실패는 즉시 재스케줄 루프가 생길 수 있으므로
                    # 동일 키에 대해 쿨다운을 둡니다.
                    with self._lock:
                        cnt = int(self._fail_counts.get(k, 0) or 0) + 1
                        self._fail_counts[k] = cnt
                        delay = min(900.0, 60.0 * (2 ** max(0, cnt - 1)))  # 1분 → 2분 → 4분 … (최대 15분)
                        self._fail_cooldown_until[k] = time.monotonic() + delay
                    with self._lock:
                        self._scheduled.pop(k, None)
                    return
                o, h, l_, c, v = tup

                def _apply_main() -> None:
                    try:
                        app._apply_official_closed_15m_bar(  # type: ignore[attr-defined]
                            code, bar_ts, o, h, l_, c, v
                        )
                    except Exception as ex:
                        if self._log:
                            try:
                                cc = canonical_stock_code(str(code).strip())
                                self._log(
                                    f"⚠️ [RT15m-Official] 메인 반영 실패 ({cc}): {ex!r}"
                                )
                            except Exception:
                                pass
                    finally:
                        with self._lock:
                            self._scheduled.pop(k, None)
                            self._fail_cooldown_until.pop(k, None)
                            self._fail_counts.pop(k, None)

                try:
                    root.after(0, _apply_main)
                except Exception:
                    try:
                        _apply_main()
                    except Exception:
                        with self._lock:
                            self._scheduled.pop(k, None)
            except Exception as e:
                if self._log:
                    try:
                        self._log(f"⚠️ [RT15m-Official] 워커 예외: {e!r}")
                    except Exception:
                        pass
                with self._lock:
                    self._scheduled.pop(k, None)

        th = threading.Thread(
            target=_worker, name="rt15m-official-replace", daemon=True
        )
        th.start()
