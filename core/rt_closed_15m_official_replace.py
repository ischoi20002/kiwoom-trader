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


def m1_api_poller_enabled(settings: Any) -> bool:
    """[옵션 A] M1 폴러 제거 — 항상 False(하위 호환)."""
    return False


def closed_15m_eval_after_official_enabled(settings: Any) -> bool:
    """
    저정밀 경로: 공식 15분봉 교체 성공 후에만 S1·S2·청산 평가.

    ``realtime_s1_eval_on_local_15m_close`` 가 꺼져 있으면 False.
    """
    try:
        if not _settings_bool(settings, "realtime_s1_eval_on_local_15m_close", True):
            return False
        return _settings_bool(
            settings, "realtime_closed_15m_eval_after_official", True
        )
    except Exception:
        return True


def official_replace_priority_score(app: Any, code: str) -> float:
    """
    공식 15분 교체 우선순위(0~1, 높을수록 먼저).

    - 보유(청산·S2 필요): 1.0
    - 미보유: ``monitor_info.met_ratio`` (신호 완성도에 근접할수록 높음)
    """
    try:
        nk = canonical_stock_code(str(code).strip())
    except Exception:
        nk = str(code or "").strip()
    if not nk:
        return 0.0

    # 보유 종목 최우선
    try:
        pf = getattr(app, "portfolio", None)
        vp = getattr(pf, "virtual_positions", None) if pf is not None else None
        if not isinstance(vp, dict):
            vp = getattr(app, "virtual_positions", None) or {}
        if isinstance(vp, dict):
            for k in list(vp.keys()):
                try:
                    if canonical_stock_code(str(k).strip()) == nk:
                        return 1.0
                except Exception:
                    if str(k).zfill(6)[-6:] == nk:
                        return 1.0
    except Exception:
        pass

    met = 0.0
    try:
        mi = getattr(app, "monitor_info", None)
        if isinstance(mi, dict):
            info = None
            for k in (nk, str(code), code):
                if k in mi and isinstance(mi.get(k), dict):
                    info = mi[k]
                    break
            if info is not None:
                met = float(info.get("met_ratio", 0.0) or 0.0)
    except Exception:
        met = 0.0
    if met < 0.0:
        met = 0.0
    if met > 1.0:
        met = 1.0
    return float(met)


def official_replace_eval_eligible(
    app: Any,
    code: str,
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    공식교체+평가 대상 여부.

    - 보유: 항상 True (청산·S2)
    - 미보유: ``met_ratio >= realtime_closed_15m_official_min_met_ratio``
    - 공식교체 후 평가 모드가 아니면 전 종목 True(레거시)
    """
    try:
        st = settings if settings is not None else (
            getattr(app, "settings", None) or {}
        )
    except Exception:
        st = {}
    if not closed_15m_eval_after_official_enabled(st):
        return True
    try:
        score = float(official_replace_priority_score(app, code))
    except Exception:
        score = 0.0
    # 보유(1.0)는 항상
    if score >= 0.999:
        return True
    try:
        min_met = float(
            st.get("realtime_closed_15m_official_min_met_ratio", 0.5)
        )
    except (TypeError, ValueError):
        min_met = 0.5
    min_met = max(0.0, min(min_met, 1.0))
    return score >= min_met


def compute_rt15m_official_replace_delay_sec(
    app: Any,
    code: str,
    bar_ts: Any,
    *,
    fallback_delay_sec: float = 2.5,
) -> float:
    """
    15분 확정 ka10080 조회 지연(초).

    - M1 폴러 OFF 또는 공식교체 후 평가 모드: M1 가드 없이
      ``min_delay + (1 - priority) * priority_spread`` (신호 근접·보유 우선).
    - M1 폴러 ON(레거시): ``m1_spread + guard`` 이후 hash stagger.
    """
    try:
        settings = getattr(app, "settings", None) or {}
    except Exception:
        settings = {}

    use_priority = True  # 옵션 A: M1 폴러 없음 → 항상 우선순위 지연

    if use_priority:
        try:
            min_d = float(
                settings.get(
                    "realtime_closed_15m_official_replace_min_delay_sec", 0.5
                )
            )
        except (TypeError, ValueError):
            min_d = 0.5
        min_d = max(0.0, min(min_d, 30.0))
        if min_d <= 0.0:
            try:
                min_d = max(0.3, float(fallback_delay_sec))
            except (TypeError, ValueError):
                min_d = 0.5

        try:
            n_codes = _active_stream_code_count(app)
        except Exception:
            n_codes = 1
        try:
            per = float(
                settings.get(
                    "realtime_closed_15m_priority_spacing_sec", 0.45
                )
            )
        except (TypeError, ValueError):
            per = 0.45
        per = max(0.15, min(per, 3.0))
        # 최하위 우선순위가 전역 페이싱(0.5s×N)을 크게 넘지 않도록 캡
        try:
            gmin = float(
                settings.get("realtime_ka10080_global_min_interval_sec", 0.5)
            )
        except (TypeError, ValueError):
            gmin = 0.5
        spread = max(float(n_codes) * per, float(n_codes) * max(0.3, gmin))
        spread = max(2.0, min(spread, 45.0))

        try:
            score = float(official_replace_priority_score(app, code))
        except Exception:
            score = 0.0
        score = max(0.0, min(1.0, score))
        # 동일 점수 미세 분산(해시) — 동시 폭주 완화
        try:
            nk = canonical_stock_code(str(code).strip())
            sk = _norm_bar_ts(bar_ts).strftime("%Y-%m-%d %H:%M:%S")
            h = abs(hash((nk, sk)))
            jitter = ((h % 1000) / 1000.0) * min(0.35, per)
        except Exception:
            jitter = 0.0
        return float(min_d + (1.0 - score) * spread + jitter)

    # ---- 레거시: M1 폴러와 경쟁 회피 ----
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


def _elapsed_sec_since_slot(bar_ts: Any) -> float:
    """벽시계 기준 슬롯 **완성** 시각 이후 경과 초(좌측 라벨 → complete_ts)."""
    try:
        from datetime import datetime

        from utils.krx_bars import krx_15m_slot_complete_ts

        slot = krx_15m_slot_complete_ts(bar_ts).to_pydatetime()
        return max(0.0, (datetime.now() - slot).total_seconds())
    except Exception:
        return 0.0


def _monotonic_deadline_for_slot(bar_ts: Any, max_wait_sec: float) -> float:
    """
    슬롯 **완성** 시각 + max_wait 까지의 남은 시간으로 monotonic deadline 산출.

    늦게 시작된 백필도 프로브와 같은 「슬롯 완성 후 N초」 창을 공유합니다.
    이미 창을 지났으면 최소 5초만 더 시도합니다.
    """
    try:
        from datetime import datetime, timedelta

        from utils.krx_bars import krx_15m_slot_complete_ts

        slot = krx_15m_slot_complete_ts(bar_ts).to_pydatetime()
        remain = (slot + timedelta(seconds=float(max_wait_sec)) - datetime.now()).total_seconds()
        remain = max(5.0, float(remain))
        return time.monotonic() + remain
    except Exception:
        return time.monotonic() + max(5.0, float(max_wait_sec))


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
    app: Any = None,
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
                    halt_slot = False
                    try:
                        if app is not None:
                            from core.krx_market_halt_slots import (
                                is_market_halt_15m_slot,
                                record_rt15m_official_miss_and_maybe_halt,
                            )

                            halt_slot = (
                                is_market_halt_15m_slot(app, want)
                                or record_rt15m_official_miss_and_maybe_halt(
                                    app, want, nc
                                )
                            )
                    except Exception:
                        halt_slot = False
                    if not halt_slot:
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
                        if log:
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

    공식교체 후 평가 모드:
    - 1차: 보유·합치도(임박) 종목만 스케줄
    - 슬롯당 1종(프로브)만 공식봉 등장까지 재시도
    - 프로브 성공 후 나머지 감시 종목 백필 교체·평가(간격 분산, 1회 조회)
    """

    def __init__(self, log: Optional[Callable[[str], None]] = None) -> None:
        self._log = log
        self._lock = threading.Lock()
        self._scheduled: Dict[str, bool] = {}
        # 실패(행 미발견 등) 이후 동일 키를 일정 시간 동안 재시도/로그하지 않기 위한 쿨다운
        self._fail_cooldown_until: Dict[str, float] = {}
        self._fail_counts: Dict[str, int] = {}
        # 슬롯(봉 시각)별 공식봉 준비 게이트 — 프로브 1종만 재시도
        self._slot_gate: Dict[str, Dict[str, Any]] = {}

    def _key(self, code: str, bar_ts: Any) -> str:
        nc = canonical_stock_code(str(code).strip())
        try:
            s = _norm_bar_ts(bar_ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            s = str(bar_ts)
        return f"{nc}|{s}"

    def _slot_ts_key(self, bar_ts: Any) -> str:
        try:
            return _norm_bar_ts(bar_ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(bar_ts)

    def _get_slot_gate(self, slot_key: str) -> Dict[str, Any]:
        with self._lock:
            g = self._slot_gate.get(slot_key)
            if g is None:
                g = {
                    "probe_lock": threading.Lock(),
                    "probe_code": None,
                    "ready_event": threading.Event(),
                    "ready": False,
                    "backfill_started": False,
                }
                self._slot_gate[slot_key] = g
            # 오래된 슬롯 정리(최대 ~8개 유지)
            if len(self._slot_gate) > 12:
                try:
                    keys = sorted(self._slot_gate.keys())
                    for old in keys[:-8]:
                        if old != slot_key:
                            self._slot_gate.pop(old, None)
                except Exception:
                    pass
            return g

    def _list_watch_codes(self, app: Any) -> List[str]:
        out: List[str] = []
        seen = set()
        try:
            ac = getattr(app, "_active_stream_codes", None) or []
            for c in ac:
                try:
                    nk = canonical_stock_code(str(c).strip())
                except Exception:
                    nk = str(c or "").strip().zfill(6)[-6:]
                if nk and len(nk) == 6 and nk not in seen:
                    seen.add(nk)
                    out.append(nk)
        except Exception:
            pass
        return out

    def _allow_fallback_probe(
        self, app: Any, code: str, settings: Dict[str, Any]
    ) -> bool:
        """임박·보유가 없을 때, 감시 중 합치도 최고 종목 1만 프로브 허용."""
        try:
            nk = canonical_stock_code(str(code).strip())
        except Exception:
            nk = str(code or "").strip()
        if not nk:
            return False
        best_c = ""
        best_s = -1.0
        for c in self._list_watch_codes(app):
            try:
                sc = float(official_replace_priority_score(app, c))
            except Exception:
                sc = 0.0
            if sc > best_s or (sc == best_s and (not best_c or c < best_c)):
                best_s = sc
                best_c = c
        return bool(best_c) and best_c == nk

    def _enqueue_slot_backfill(
        self,
        app: Any,
        bar_ts: Any,
        *,
        probe_code: str,
        delay_sec: float,
    ) -> None:
        """
        프로브로 공식봉 등장이 확인된 뒤, 나머지 감시 종목도 느리게 교체·평가.

        임박이 아니어도 해당 봉에서 갑자기 Signal 나는 종목을 BT와 맞추기 위함.
        """
        try:
            settings = getattr(app, "settings", None) or {}
        except Exception:
            settings = {}
        if not closed_15m_eval_after_official_enabled(settings):
            return
        try:
            if not _settings_bool(
                settings, "realtime_closed_15m_backfill_after_ready", True
            ):
                return
        except Exception:
            pass

        slot_key = self._slot_ts_key(bar_ts)
        gate = self._get_slot_gate(slot_key)
        with self._lock:
            if gate.get("backfill_started"):
                return
            gate["backfill_started"] = True

        try:
            probe_nk = canonical_stock_code(str(probe_code).strip())
        except Exception:
            probe_nk = str(probe_code or "").strip()

        codes = self._list_watch_codes(app)
        pending: List[Tuple[float, str]] = []
        for nk in codes:
            if nk == probe_nk:
                continue
            k = self._key(nk, bar_ts)
            with self._lock:
                if self._scheduled.get(k):
                    continue
            try:
                sc = float(official_replace_priority_score(app, nk))
            except Exception:
                sc = 0.0
            pending.append((sc, nk))
        if not pending:
            return
        # 합치도 높은 쪽을 조금 더 먼저(여유 백필)
        pending.sort(key=lambda x: (-x[0], x[1]))

        try:
            spacing = float(
                settings.get("realtime_closed_15m_backfill_spacing_sec", 0.55)
            )
        except (TypeError, ValueError):
            spacing = 0.55
        spacing = max(0.35, min(spacing, 3.0))
        try:
            base = float(
                settings.get("realtime_closed_15m_backfill_base_delay_sec", 0.3)
            )
        except (TypeError, ValueError):
            base = 0.3
        base = max(0.0, min(base, 5.0))

        for i, (_sc, nk) in enumerate(pending):
            try:
                self.schedule(
                    app=app,
                    code=nk,
                    bar_ts=bar_ts,
                    delay_sec=max(0.3, float(delay_sec)),
                    prepend_delay_sec=0.0,
                    backfill_after_ready=True,
                    backfill_extra_delay_sec=base + float(i) * spacing,
                )
            except Exception:
                continue
        if self._log:
            try:
                self._log(
                    f"ℹ️ [RT15m-Official] {slot_key} 공식봉 확인 — "
                    f"나머지 {len(pending)}종 백필 교체·평가 예약"
                )
            except Exception:
                pass

    def schedule(
        self,
        *,
        app: Any,
        code: str,
        bar_ts: Any,
        delay_sec: float,
        force_halt_retry: bool = False,
        halt_retry_fail_cooldown_sec: float = 60.0,
        prepend_delay_sec: float = 0.0,
        backfill_after_ready: bool = False,
        backfill_extra_delay_sec: float = 0.0,
    ) -> None:
        """이미 동일 종목·동일 봉 시각이 예약돼 있으면 무시합니다."""
        try:
            if not is_krx_15m_slot_end(bar_ts):
                return
        except Exception:
            return

        try:
            settings = getattr(app, "settings", None) or {}
        except Exception:
            settings = {}

        # 저정밀 공식교체 후 평가: 1차=임박·보유만. 백필은 프로브 성공 후 전 감시.
        # 임박·보유가 전무하면 합치도 최고 1종을 프로브로 허용(성공 시 백필).
        if not force_halt_retry and not backfill_after_ready:
            try:
                if closed_15m_eval_after_official_enabled(settings):
                    if not official_replace_eval_eligible(app, code, settings):
                        if not self._allow_fallback_probe(app, code, settings):
                            return
            except Exception:
                pass

        k = self._key(code, bar_ts)
        with self._lock:
            if self._scheduled.get(k):
                return
            if not force_halt_retry:
                try:
                    now_m = time.monotonic()
                    until = float(self._fail_cooldown_until.get(k, 0.0) or 0.0)
                    if until > now_m:
                        return
                except Exception:
                    pass
            self._scheduled[k] = True

        if not force_halt_retry:
            try:
                from core.krx_market_halt_slots import should_skip_rt15m_official_replace

                if should_skip_rt15m_official_replace(app, bar_ts):
                    with self._lock:
                        self._scheduled.pop(k, None)
                    return
            except Exception:
                pass

        def _delay() -> float:
            if backfill_after_ready:
                try:
                    return max(0.0, float(backfill_extra_delay_sec))
                except (TypeError, ValueError):
                    return 0.3
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
                try:
                    pre = max(0.0, float(prepend_delay_sec))
                except (TypeError, ValueError):
                    pre = 0.0
                if pre > 0.0:
                    time.sleep(pre)
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

                try:
                    max_wait = float(
                        settings.get(
                            "realtime_closed_15m_official_replace_max_wait_sec",
                            75.0,
                        )
                    )
                except (TypeError, ValueError):
                    max_wait = 75.0
                # 실측 공식 15분 API ~50초 지연 → 기본 75, 상한 180
                max_wait = max(3.0, min(max_wait, 180.0))
                try:
                    poll_sec = float(
                        settings.get(
                            "realtime_closed_15m_official_replace_poll_sec",
                            1.5,
                        )
                    )
                except (TypeError, ValueError):
                    poll_sec = 1.5
                poll_sec = max(0.4, min(poll_sec, 5.0))

                use_slot_probe = (
                    (not force_halt_retry)
                    and closed_15m_eval_after_official_enabled(settings)
                )
                slot_key = self._slot_ts_key(bar_ts)
                is_probe = True
                gate: Optional[Dict[str, Any]] = None

                # 백필: 프로브가 공식봉 존재를 확인한 뒤 나머지 종목 조회
                # (조회는 아래 공통 폴링 — 단발 1회 조회 금지)
                if backfill_after_ready and use_slot_probe:
                    is_probe = False
                    gate = self._get_slot_gate(slot_key)
                elif use_slot_probe:
                    gate = self._get_slot_gate(slot_key)
                    plock = gate["probe_lock"]
                    claimed = False
                    try:
                        claimed = bool(plock.acquire(blocking=False))
                    except Exception:
                        claimed = False
                    if claimed:
                        try:
                            if gate.get("probe_code") is None:
                                gate["probe_code"] = canonical_stock_code(
                                    str(code).strip()
                                )
                                is_probe = True
                            else:
                                is_probe = False
                        finally:
                            try:
                                plock.release()
                            except Exception:
                                pass
                    else:
                        is_probe = False

                    if not is_probe:
                        try:
                            ev = gate.get("ready_event")
                            if ev is not None:
                                # 슬롯 기준 남은 대기 + 여유
                                rem = max(
                                    5.0,
                                    _monotonic_deadline_for_slot(bar_ts, max_wait)
                                    - time.monotonic(),
                                )
                                ev.wait(timeout=rem + 2.0)
                        except Exception:
                            pass

                per_try_retries = 1 if (max_wait >= 5.0) else max(1, min(mr, 3))
                deadline = _monotonic_deadline_for_slot(bar_ts, max_wait)
                tup = None

                if use_slot_probe and not is_probe:
                    if gate is not None and not bool(gate.get("ready")):
                        with self._lock:
                            self._scheduled.pop(k, None)
                        try:
                            if self._log:
                                cc = canonical_stock_code(str(code).strip())
                                el = _elapsed_sec_since_slot(bar_ts)
                                self._log(
                                    f"ℹ️ [RT15m-Official] {cc} {_norm_bar_ts(bar_ts)} "
                                    f"프로브 미확인 — 백필 생략 "
                                    f"(슬롯 후 {el:.0f}s)"
                                )
                        except Exception:
                            pass
                        return

                # 프로브·백필 공통: 슬롯 기준 deadline까지 폴링
                while True:
                    tup = fetch_official_15m_row_for_timestamp(
                        dm,
                        code,
                        token,
                        bar_ts,
                        tic_scope=tic,
                        max_pages=mp,
                        page_delay_min=dmin,
                        page_delay_max=dmax,
                        max_retries=per_try_retries,
                        inter_retry_sec=ir,
                        log=self._log,
                        app=app,
                    )
                    if tup is not None:
                        break
                    if time.monotonic() + poll_sec >= deadline:
                        break
                    try:
                        time.sleep(poll_sec)
                    except Exception:
                        break
                    try:
                        token = app.auth_manager.get_auth_token()
                    except Exception:
                        pass
                    if not token:
                        break

                if use_slot_probe and gate is not None and is_probe:
                    try:
                        gate["ready"] = tup is not None
                        ev = gate.get("ready_event")
                        if ev is not None:
                            ev.set()
                    except Exception:
                        pass
                    # 공식봉 확인 후 나머지 감시 종목 백필
                    if tup is not None:
                        try:
                            self._enqueue_slot_backfill(
                                app,
                                bar_ts,
                                probe_code=str(code),
                                delay_sec=float(delay_sec),
                            )
                        except Exception:
                            pass

                root = getattr(app, "root", None)
                if tup is None or root is None:
                    try:
                        from core.krx_market_halt_slots import is_market_halt_15m_slot

                        if is_market_halt_15m_slot(app, bar_ts) and not force_halt_retry:
                            with self._lock:
                                self._scheduled.pop(k, None)
                            return
                    except Exception:
                        pass
                    with self._lock:
                        if force_halt_retry:
                            try:
                                cd = max(
                                    15.0, float(halt_retry_fail_cooldown_sec)
                                )
                            except (TypeError, ValueError):
                                cd = 60.0
                            self._fail_cooldown_until[k] = time.monotonic() + cd
                        else:
                            cnt = int(self._fail_counts.get(k, 0) or 0) + 1
                            self._fail_counts[k] = cnt
                            delay = min(
                                900.0, 60.0 * (2 ** max(0, cnt - 1))
                            )
                            self._fail_cooldown_until[k] = time.monotonic() + delay
                        self._scheduled.pop(k, None)
                    if self._log and tup is None:
                        try:
                            cc = canonical_stock_code(str(code).strip())
                            el = _elapsed_sec_since_slot(bar_ts)
                            role = "프로브" if is_probe else "백필"
                            self._log(
                                f"ℹ️ [RT15m-Official] {cc} {_norm_bar_ts(bar_ts)} "
                                f"공식봉 대기 초과({role}, 한도 {max_wait:.0f}s, "
                                f"슬롯 후 {el:.0f}s) — 평가 생략"
                            )
                        except Exception:
                            pass
                    return
                o, h, l_, c, v = tup

                try:
                    if self._log:
                        cc = canonical_stock_code(str(code).strip())
                        el = _elapsed_sec_since_slot(bar_ts)
                        role = "프로브" if is_probe else "백필"
                        self._log(
                            f"✅ [RT15m-Official] {cc} {_norm_bar_ts(bar_ts)} "
                            f"공식봉 확인({role}, 슬롯 후 +{el:.1f}s) — 교체·평가"
                        )
                except Exception:
                    pass

                if force_halt_retry:
                    try:
                        from core.krx_market_halt_slots import (
                            is_market_halt_15m_slot,
                            notify_halt_official_retry_success,
                        )

                        if is_market_halt_15m_slot(app, bar_ts):
                            notify_halt_official_retry_success(
                                app, bar_ts, code, volume=v
                            )
                    except Exception:
                        pass

                def _apply_main() -> None:
                    applied = False
                    try:
                        ret = app._apply_official_closed_15m_bar(  # type: ignore[attr-defined]
                            code, bar_ts, o, h, l_, c, v
                        )
                        applied = True if ret is None else bool(ret)
                    except Exception as ex:
                        if self._log:
                            try:
                                cc = canonical_stock_code(str(code).strip())
                                self._log(
                                    f"⚠️ [RT15m-Official] 메인 반영 실패 ({cc}): {ex!r}"
                                )
                            except Exception:
                                pass
                        applied = False
                    finally:
                        with self._lock:
                            self._scheduled.pop(k, None)
                            self._fail_cooldown_until.pop(k, None)
                            self._fail_counts.pop(k, None)
                    if applied:
                        try:
                            st = getattr(app, "settings", None) or {}
                            if closed_15m_eval_after_official_enabled(st):
                                pool = getattr(app, "_closed_15m_eval_pool", None)
                                if pool is not None:
                                    pool.submit_eval(code, bar_ts)
                        except Exception:
                            pass

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
