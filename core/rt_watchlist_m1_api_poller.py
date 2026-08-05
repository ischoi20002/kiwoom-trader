# -*- coding: utf-8 -*-
"""
감시 종목(_active_stream_codes)에 대해 ka10080(1분)을 **슬롯 단위로 일괄 조회**합니다.

- **매분 전종목 폴링 없음.** 15분 슬롯 앞구간(보통 14봉, ``15:15``는 4봉)이 닫힌 뒤
  종목별 분산으로 1페이지를 받아 head만 ``RtTickM1Store``에 반영.
- **말분**: 기본은 틱 누적 seal 후 합성 15분 평가. 공식 말분 모드면 말분 마감 후
  1회 더 조회해 평가. ``15:30``은 공식 단일가 경로.
- 평가 후 공식 15분 교체는 기존 ``rt_closed_15m_official_replace`` 정렬 경로.
- 매 head/말분 라운드 시작 시 상위 후보 프로브로 API 준비만 짧게 확인.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pandas as pd

from core.bt_hp_signal_bridge import ka10080_raw_rows_to_1m_df
from core.ka10080_spread import effective_m1_poll_spread_sec
from utils.krx_bars import (
    chart_covers_m1_closed_minute,
    is_krx_last_15m_slot,
    is_krx_m1_closed_open_label,
    is_last_m1_open_of_15m_slot,
    is_slot_head_complete_m1_open,
    krx_15m_label_for_m1_open,
    m1_bar_open_label_for_just_completed_minute,
    slot_head_m1_range,
)
from utils.normalization import canonical_stock_code

CacheKey = Tuple[str, str]
CacheVal = Tuple[float, float, float, float, float]
FetchResult = Tuple[Optional[pd.Series], Optional[pd.DataFrame], str]
# head = 슬롯 앞구간 일괄, last = 말분(공식) 1회
RoundKind = str


class RtWatchlistM1ApiPoller:
    """
    데몬 스레드 1개 + 분마다 ``threading.Timer`` 로 종목별 지연 실행.
    """

    def __init__(self, app: Any, log: Optional[Callable[[str], None]] = None) -> None:
        self._app = app
        self._log = log
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()
        self._last_scheduled_round: str = ""
        self._cache: Dict[CacheKey, CacheVal] = {}
        self._cache_order: List[CacheKey] = []
        self._cache_lock = threading.Lock()
        self._round_lock = threading.Lock()
        self._dormant_rounds: Set[str] = set()
        self._active_rounds: Set[str] = set()
        # 틱 말분 경로로 이미 평가한 (code, slot_key) — 공식 말분 fetch 중복 평가 방지
        self._tick_tail_eval_done: Set[Tuple[str, str]] = set()
        self._tick_tail_eval_lock = threading.Lock()
        self._tick_tail_scheduled_rounds: Set[str] = set()

    def _emit(self, msg: str) -> None:
        if self._log is None:
            return
        try:
            self._log(msg)
        except Exception:
            pass

    def _settings(self) -> Dict[str, Any]:
        try:
            return getattr(self._app, "settings", None) or {}
        except Exception:
            return {}

    @staticmethod
    def _settings_bool(settings: Dict[str, Any], key: str, default: bool = True) -> bool:
        try:
            v = settings.get(key, default)
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() in ("1", "true", "yes", "on")
        except Exception:
            return default

    def _last_minute_from_ticks_enabled(self) -> bool:
        return self._settings_bool(
            self._settings(), "realtime_15m_last_minute_from_ticks", True
        )

    def _tick_tail_wait_sec(self) -> float:
        """무거래 등 :00 틱이 없을 때 말분 종료 후 타임아웃(초). 기본 1.5."""
        try:
            v = float(self._settings().get("realtime_15m_tick_tail_wait_sec", 1.5))
            return max(0.5, min(5.0, v))
        except (TypeError, ValueError):
            return 1.5

    def _eval_on_official_last_m1_enabled(self) -> bool:
        """공식 말분 1분 REST 도착 시 평가할지(기본 OFF — 틱 말분 경로 사용)."""
        return self._settings_bool(
            self._settings(), "realtime_15m_eval_on_official_last_m1", False
        )

    @staticmethod
    def _slot_eval_key(nk: str, slot: pd.Timestamp) -> Tuple[str, str]:
        return (
            str(nk).zfill(6),
            pd.Timestamp(slot).floor("min").strftime("%Y-%m-%d %H:%M:%S"),
        )

    def _mark_tick_tail_eval_done(self, nk: str, slot: pd.Timestamp) -> None:
        try:
            key = self._slot_eval_key(nk, slot)
            with self._tick_tail_eval_lock:
                self._tick_tail_eval_done.add(key)
                # 슬롯이 쌓이면 오래된 항목 정리
                if len(self._tick_tail_eval_done) > 4000:
                    keep = sorted(self._tick_tail_eval_done, key=lambda x: x[1])[-2000:]
                    self._tick_tail_eval_done = set(keep)
        except Exception:
            pass

    def _is_tick_tail_eval_done(self, nk: str, slot: pd.Timestamp) -> bool:
        try:
            key = self._slot_eval_key(nk, slot)
            with self._tick_tail_eval_lock:
                return key in self._tick_tail_eval_done
        except Exception:
            return False

    def _schedule_tick_tail_evals(
        self, codes: List[str], closed_lab: pd.Timestamp
    ) -> None:
        """
        무거래 폴백: 말분 종료 + wait_sec 타임아웃 후 틱 seal → 합성 → 평가.

        활발한 종목은 ``on_tick_tail_next_minute``(:00 틱)이 먼저 평가한다.
        """
        try:
            if is_krx_last_15m_slot(closed_lab):
                return
            if not is_last_m1_open_of_15m_slot(closed_lab):
                return
            if not self._last_minute_from_ticks_enabled():
                return
        except Exception:
            return

        sk = pd.Timestamp(closed_lab).floor("min").strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self._tick_tail_eval_lock:
                if sk in self._tick_tail_scheduled_rounds:
                    return
                self._tick_tail_scheduled_rounds.add(sk)
                if len(self._tick_tail_scheduled_rounds) > 64:
                    self._tick_tail_scheduled_rounds = set(
                        sorted(self._tick_tail_scheduled_rounds)[-32:]
                    )
        except Exception:
            pass

        wait = self._tick_tail_wait_sec()
        try:
            end_wall = (
                pd.Timestamp(closed_lab).floor("min") + pd.Timedelta(minutes=1)
            ).to_pydatetime()
            due = end_wall + timedelta(seconds=wait)
            delay = max(0.05, (due - datetime.now()).total_seconds())
        except Exception:
            delay = wait

        try:
            self._emit(
                f"ℹ️ [M1Poll] 틱말분 타임아웃 예약 "
                f"lab={closed_lab.strftime('%H:%M')} wait={wait:.1f}s "
                f"delay={delay:.2f}s n={len(codes)} (:00틱 우선)"
            )
        except Exception:
            pass

        for nk in codes:
            try:
                t = threading.Timer(
                    delay,
                    self._job_tick_tail_eval,
                    args=(nk, closed_lab, "timeout"),
                )
                t.daemon = True
                t.start()
            except Exception:
                continue

    def on_tick_tail_next_minute(self, code: Any, closed_lab: Any) -> None:
        """
        다음 분(:00) 체결로 말분이 seal된 직후 — 즉시 완성15 평가.

        WS 틱 스레드에서 호출될 수 있으므로 짧은 타이머(0)로 분리해도 되고,
        중복은 ``_tick_tail_eval_done`` 으로 막는다.
        """
        try:
            if not self._last_minute_from_ticks_enabled():
                return
            nk = canonical_stock_code(str(code).strip())
            lab = pd.Timestamp(closed_lab).floor("min")
            if not nk or not is_last_m1_open_of_15m_slot(lab):
                return
            if is_krx_last_15m_slot(lab):
                return
            slot = krx_15m_label_for_m1_open(lab)
            if slot is None or self._is_tick_tail_eval_done(nk, slot):
                return
            # 즉시 평가(다음 분 틱 = 말분 종료 신호). 늦은 :59는 seal amend만.
            t = threading.Timer(
                0.0,
                self._job_tick_tail_eval,
                args=(nk, lab, "next_min_tick"),
            )
            t.daemon = True
            t.start()
        except Exception:
            pass

    def _job_tick_tail_eval(
        self,
        nk: str,
        closed_lab: pd.Timestamp,
        reason: str = "timeout",
    ) -> None:
        """틱 말분 seal·합성 15분 평가 (``:00`` 틱 또는 타임아웃)."""
        if self._stop.is_set() or not self._monitoring():
            return
        try:
            slot = krx_15m_label_for_m1_open(closed_lab)
            if slot is None:
                return
            if self._is_tick_tail_eval_done(nk, slot):
                return

            st = self._settings()
            if not self._settings_bool(st, "realtime_s1_eval_on_local_15m_close", True):
                return
            from core.rt_closed_15m_official_replace import (
                closed_15m_eval_after_official_enabled,
                official_replace_eval_eligible,
            )

            if closed_15m_eval_after_official_enabled(st):
                return
            if not official_replace_eval_eligible(self._app, nk, st):
                return

            m1s = getattr(self._app, "rt_tick_m1_store", None)
            if m1s is not None:
                try:
                    m1s.seal_tick_tail_minute(nk, closed_lab)
                except Exception:
                    pass

            from core.realtime_official_bars_sync import (
                apply_composed_closed_15m_from_m1,
                maybe_roll_15m_slot_on_clock,
            )

            maybe_roll_15m_slot_on_clock(self._app, nk)
            composed = apply_composed_closed_15m_from_m1(self._app, nk, slot)
            if composed is None:
                try:
                    self._emit(
                        f"⚠️ [M1Poll] 틱말분 합성 실패/스킵 {nk} "
                        f"slot={slot.strftime('%H:%M')} reason={reason}"
                    )
                except Exception:
                    pass
                return
            o, h, l_, c, v = composed

            self._mark_tick_tail_eval_done(nk, slot)

            try:
                q = getattr(self._app, "_closed_15m_db_queue", None)
                if q is not None:
                    q.enqueue(nk, slot, o, h, l_, c, v)
            except Exception:
                pass

            try:
                pool = getattr(self._app, "_closed_15m_eval_pool", None)
                if pool is not None:
                    pool.submit_eval(nk, slot)
                    self._emit(
                        f"ℹ️ [M1Poll] 틱말분 완성15 평가 {nk} "
                        f"slot={slot.strftime('%H:%M')} reason={reason} "
                        f"@ {datetime.now().strftime('%H:%M:%S.%f')[:-3]}"
                    )
            except Exception as ex:
                self._emit(f"⚠️ [M1Poll] {nk} 틱말분 submit_eval 실패: {ex!r}")
        except Exception as e:
            try:
                self._emit(f"⚠️ [M1Poll] 틱말분 평가 예외 ({nk}): {e!r}")
            except Exception:
                pass

    def _schedule_signal_eval(
        self, nk: str, *, close_px: float = 0.0, volume: float = 0.0
    ) -> None:
        """공식 M1 반영 직후(또는 실패 폴백) 신호 재평가."""
        try:
            from core.realtime_official_bars_sync import schedule_official_sync_and_signal_eval

            schedule_official_sync_and_signal_eval(
                self._app,
                nk,
                now_price_fallback=float(close_px or 0.0),
                volume_fallback=int(volume or 0),
            )
        except Exception:
            pass

    def _monitoring(self) -> bool:
        try:
            tc = getattr(self._app, "trading_controller", None)
            return bool(getattr(tc, "is_monitoring", False))
        except Exception:
            return False

    def _active_codes(self) -> List[str]:
        """웜업·감시에 올라간 구독 종목(정규 6자리)."""
        out: List[str] = []
        try:
            ac = getattr(self._app, "_active_stream_codes", None)
            if isinstance(ac, list):
                for c in ac:
                    nk = canonical_stock_code(str(c).strip())
                    if nk and nk not in out:
                        out.append(nk)
        except Exception:
            pass
        return out

    def _cache_max_entries(self) -> int:
        try:
            n = int(
                float(
                    self._settings().get(
                        "realtime_watchlist_m1_api_cache_max_entries", 4000
                    )
                )
            )
        except (TypeError, ValueError):
            n = 4000
        return max(200, min(n, 50_000))

    def _remember_cache(self, key: CacheKey, val: CacheVal) -> None:
        mx = self._cache_max_entries()
        with self._cache_lock:
            if key in self._cache:
                self._cache[key] = val
                return
            self._cache[key] = val
            self._cache_order.append(key)
            while len(self._cache_order) > mx:
                old = self._cache_order.pop(0)
                self._cache.pop(old, None)

    def get_cached_row(
        self, code: str, closed_open_label: pd.Timestamp
    ) -> Optional[CacheVal]:
        """캐시된 (O,H,L,C,V) 또는 None."""
        try:
            nk = canonical_stock_code(str(code).strip())
            if not nk:
                return None
            sk = pd.Timestamp(closed_open_label).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return None
        with self._cache_lock:
            return self._cache.get((nk, sk))

    def start(self) -> None:
        """중복 기동 방지."""
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._wake_loop, name="RtWatchlistM1ApiPoller", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """앱 종료 등에서 호출."""
        self._stop.set()
        try:
            th = self._thread
            if th is not None and th.is_alive():
                th.join(timeout=2.0)
        except Exception:
            pass

    def reset_round_state(self) -> None:
        """감시 중단 시 호출: 다음 장중 첫 라운드가 스케줄되도록 라운드 키를 비웁니다."""
        try:
            self._last_scheduled_round = ""
            with self._round_lock:
                self._dormant_rounds.clear()
                self._active_rounds.clear()
        except Exception:
            pass

    def _is_round_dormant(self, sk: str) -> bool:
        with self._round_lock:
            return sk in self._dormant_rounds

    def _mark_round_dormant(self, sk: str) -> None:
        with self._round_lock:
            self._dormant_rounds.add(sk)
            self._active_rounds.discard(sk)

    def _mark_round_active(self, sk: str) -> None:
        with self._round_lock:
            self._active_rounds.add(sk)
            self._dormant_rounds.discard(sk)

    def _pick_probe_code(self, codes: List[str]) -> str:
        """프로브용 대표 종목 — 슬롯 마지막 1분이면 보유·고 met_ratio 우선."""
        if not codes:
            return ""
        try:
            from core.rt_closed_15m_official_replace import official_replace_priority_score

            ranked = sorted(
                codes,
                key=lambda c: (
                    -float(official_replace_priority_score(self._app, c)),
                    str(c),
                ),
            )
            return str(ranked[0])
        except Exception:
            try:
                return sorted(codes)[0]
            except Exception:
                return str(codes[0])

    def _priority_delay_sec(self, code: str) -> float:
        """슬롯 마지막 1분 평가용 우선순위 지연(초)."""
        st = self._settings()
        try:
            from core.rt_closed_15m_official_replace import official_replace_priority_score

            score = float(official_replace_priority_score(self._app, code))
        except Exception:
            score = 0.0
        score = max(0.0, min(1.0, score))
        try:
            min_d = float(st.get("realtime_closed_15m_official_replace_min_delay_sec", 0.5))
        except (TypeError, ValueError):
            min_d = 0.5
        min_d = max(0.0, min(min_d, 30.0))
        try:
            n = max(1, len(self._active_codes()))
            spread = max(2.0, min(25.0, float(n) * 0.8))
        except Exception:
            spread = 8.0
        try:
            h = abs(hash((canonical_stock_code(str(code)), "m1pri"))) % 1000
            jitter = (h / 1000.0) * 0.15
        except Exception:
            jitter = 0.0
        return float(min_d + (1.0 - score) * spread + jitter)

    def _schedule_round_jobs(
        self,
        codes: List[str],
        closed_lab: pd.Timestamp,
        sk: str,
        *,
        kind: RoundKind = "last",
    ) -> None:
        """프로브 통과 후 종목별 분산 조회 타이머 등록 (head 일괄 또는 말분 공식)."""
        try:
            ordered = list(codes)
            try:
                from core.rt_closed_15m_official_replace import (
                    official_replace_priority_score,
                )

                ordered = sorted(
                    codes,
                    key=lambda c: (
                        -float(official_replace_priority_score(self._app, c)),
                        str(c),
                    ),
                )
            except Exception:
                ordered = list(codes)

            if kind == "head":
                # 말분 시작~완성 전에 head가 끝나도록 spread 상한
                n_codes = len(ordered)
                try:
                    spread = effective_m1_poll_spread_sec(self._settings(), n_codes)
                except Exception:
                    try:
                        spread = float(
                            self._settings().get(
                                "realtime_watchlist_m1_api_spread_sec", 40.0
                            )
                        )
                    except (TypeError, ValueError):
                        spread = 40.0
                spread = max(8.0, min(float(spread), 45.0))
                for cc in ordered:
                    try:
                        h = abs(hash((cc, sk, "head")))
                        frac = (h % 1_000_003) / 1_000_003.0
                        delay = spread * frac
                    except Exception:
                        delay = 0.0
                    t = threading.Timer(
                        delay,
                        self._job_fetch_slot_head,
                        args=(cc, closed_lab, sk),
                    )
                    t.daemon = True
                    t.start()
                return

            # kind == "last": 말분·15:30 공식 1분
            for cc in ordered:
                try:
                    delay = self._priority_delay_sec(cc)
                except Exception:
                    delay = 0.5
                t = threading.Timer(
                    delay,
                    self._job_fetch_one,
                    args=(cc, closed_lab, sk),
                )
                t.daemon = True
                t.start()
        except Exception:
            pass

    @staticmethod
    def _is_pollable_m1_label(closed_lab: pd.Timestamp) -> bool:
        """
        KRX 연속매매 1분봉이 실제로 존재하는 라벨만 True.

        장전·장후·주말·15:20~15:29 동시호가 분은 False → ka10080 조회 생략.
        15:30 단일가는 ``is_krx_m1_closed_open_label`` 결과에 따름.
        """
        try:
            return bool(is_krx_m1_closed_open_label(closed_lab))
        except Exception:
            return False

    def _wake_loop(self) -> None:
        """새로 닫힌 1분 라벨이 바뀌었을 때만 head 일괄·말분(틱/공식) 스케줄."""
        while not self._stop.wait(0.25):
            try:
                try:
                    st = self._settings()
                    from core.rt_closed_15m_official_replace import m1_api_poller_enabled

                    if not m1_api_poller_enabled(st):
                        continue
                except Exception:
                    continue
                if not self._monitoring():
                    continue
                wall = datetime.now()
                closed_lab = m1_bar_open_label_for_just_completed_minute(wall)
                sk = closed_lab.strftime("%Y-%m-%d %H:%M:%S")
                if sk == self._last_scheduled_round:
                    continue
                if not self._is_pollable_m1_label(closed_lab):
                    self._last_scheduled_round = sk
                    continue
                codes = self._active_codes()
                if not codes:
                    continue
                self._last_scheduled_round = sk
                with self._round_lock:
                    self._dormant_rounds = {
                        x for x in self._dormant_rounds if x == sk
                    }
                    self._active_rounds = {
                        x for x in self._active_rounds if x == sk
                    }

                is_last = False
                is_auc = False
                is_head_done = False
                try:
                    is_last = bool(is_last_m1_open_of_15m_slot(closed_lab))
                except Exception:
                    is_last = False
                try:
                    is_auc = bool(is_krx_last_15m_slot(closed_lab))
                except Exception:
                    is_auc = False
                try:
                    is_head_done = bool(is_slot_head_complete_m1_open(closed_lab))
                except Exception:
                    is_head_done = False

                use_tick = bool(self._last_minute_from_ticks_enabled())
                use_official_last = bool(self._eval_on_official_last_m1_enabled())

                # 말분 틱 평가 예약(15:30 제외)
                try:
                    if is_last and (not is_auc) and use_tick:
                        self._schedule_tick_tail_evals(codes, closed_lab)
                except Exception:
                    pass

                need_head = bool(is_head_done)
                need_last_official = bool(
                    is_last
                    and (is_auc or ((not use_tick) and use_official_last))
                )
                if not need_head and not need_last_official:
                    continue
                if self._is_round_dormant(sk):
                    continue

                kind: RoundKind = "head" if need_head else "last"
                # head와 last가 같은 분에 겹치지 않음(13 vs 14). 동시 True면 head 우선.
                if need_head and need_last_official:
                    kind = "head"

                with self._round_lock:
                    already_active = sk in self._active_rounds
                if already_active:
                    self._schedule_round_jobs(codes, closed_lab, sk, kind=kind)
                    continue
                t = threading.Timer(
                    0.0,
                    self._job_probe_round,
                    args=(codes, closed_lab, sk, 0, None, kind),
                )
                t.daemon = True
                t.start()
            except Exception as e:
                try:
                    self._emit(f"⚠️ [M1Poll] wake_loop 예외: {e!r}")
                except Exception:
                    pass

    def _pick_row_for_closed_open(
        self, df: pd.DataFrame, closed_open: pd.Timestamp
    ) -> Optional[pd.Series]:
        """
        ka10080 1분 인덱스는 cntr_tm = **봉 종가 시각**(분 끝).
        ``closed_open`` 은 ``m1_bar_open_label_for_just_completed_minute`` = 구간 시작 라벨.
        """
        try:
            if df is None or getattr(df, "empty", True):
                return None
            end_ts = pd.Timestamp(closed_open) + pd.Timedelta(minutes=1)
            if end_ts in df.index:
                return df.loc[end_ts]
            want = end_ts.floor("s")
            for ix in df.index:
                try:
                    if pd.Timestamp(ix).floor("s") == want:
                        return df.loc[ix]
                except Exception:
                    continue
            return None
        except Exception:
            return None

    def _classify_fetch_result(
        self,
        row: Optional[pd.Series],
        chart_df: Optional[pd.DataFrame],
        closed_lab: pd.Timestamp,
        last_err: str,
    ) -> str:
        """``ok`` | ``minute_not_ready`` | ``row_not_found`` | ``fetch_error``."""
        if row is not None:
            return "ok"
        if last_err in ("no_token", "empty_raw") or last_err.startswith("http_"):
            return "fetch_error"
        if chart_df is not None and not chart_df.empty:
            if not chart_covers_m1_closed_minute(chart_df, closed_lab):
                return "minute_not_ready"
            return "row_not_found"
        if last_err == "row_not_found":
            return "row_not_found"
        return "fetch_error"

    def _fetch_closed_minute_row(
        self,
        nk: str,
        closed_lab: pd.Timestamp,
        sk: str,
        *,
        max_retries: int,
    ) -> FetchResult:
        """ka10080 1분 1페이지 조회 → (row, chart_df, last_err)."""
        st = self._settings()
        try:
            base_backoff = float(
                st.get("realtime_watchlist_m1_api_backoff_base_sec", 0.45)
            )
        except (TypeError, ValueError):
            base_backoff = 0.45
        base_backoff = max(0.1, min(base_backoff, 5.0))
        try:
            dmin = float(st.get("realtime_warmup_ka10080_page_delay_min", 0.08))
            dmax = float(st.get("realtime_warmup_ka10080_page_delay_max", 0.22))
        except (TypeError, ValueError):
            dmin, dmax = 0.08, 0.22
        if dmax < dmin:
            dmin, dmax = dmax, dmin

        dm = getattr(self._app, "data_manager", None)
        if dm is None:
            return None, None, "no_data_manager"

        row: Optional[pd.Series] = None
        chart_df: Optional[pd.DataFrame] = None
        last_err = "unknown"
        retries = max(1, min(int(max_retries), 8))

        for attempt in range(retries):
            try:
                token = None
                try:
                    token = self._app.auth_manager.get_auth_token()
                except Exception:
                    token = None
                if not token:
                    last_err = "no_token"
                    try:
                        from core.ka10080_fetch_diag import ka10080_fetch_diag

                        ka10080_fetch_diag.record_fail(
                            nk,
                            tic_scope="1",
                            context="m1_poll",
                            reason="no_token",
                            closed_minute=sk,
                            attempt=attempt + 1,
                        )
                    except Exception:
                        pass
                    break
                raw_list, _nk = dm.fetch_kiwoom_minute_chart_page(
                    nk,
                    str(token).strip(),
                    "",
                    tic_scope="1",
                    fetch_context="m1_poll",
                )
                if not raw_list:
                    try:
                        meta = getattr(dm, "last_ka10080_fetch_meta", None) or {}
                        last_err = str(meta.get("reason") or "empty_raw")
                    except Exception:
                        last_err = "empty_raw"
                else:
                    df = ka10080_raw_rows_to_1m_df(raw_list)
                    chart_df = df
                    row = self._pick_row_for_closed_open(df, closed_lab)
                    if row is not None:
                        last_err = "ok"
                        break
                    if not chart_covers_m1_closed_minute(df, closed_lab):
                        last_err = "minute_not_ready"
                        break
                    last_err = "row_not_found"
            except Exception as e:
                last_err = repr(e)

            status = self._classify_fetch_result(row, chart_df, closed_lab, last_err)
            if status in ("ok", "minute_not_ready"):
                break
            if attempt + 1 < retries:
                try:
                    sleep_s = base_backoff * (2**attempt) + random.uniform(0, 0.12)
                    time.sleep(min(sleep_s, 8.0))
                except Exception:
                    time.sleep(base_backoff)
                try:
                    time.sleep(random.uniform(dmin, dmax))
                except Exception:
                    pass

        return row, chart_df, last_err

    def _not_ready_poll_sec(self) -> float:
        try:
            v = float(
                self._settings().get("realtime_m1_probe_not_ready_poll_sec", 0.5)
            )
            return max(0.2, min(v, 3.0))
        except (TypeError, ValueError):
            return 0.5

    def _probe_not_ready_max_wait_sec(self) -> float:
        """프로브(시장 준비) 대기 — 짧게. 한 종목 무거래에 전체를 묶지 않음."""
        try:
            v = float(
                self._settings().get(
                    "realtime_m1_probe_not_ready_max_wait_sec", 2.5
                )
            )
            return max(0.5, min(v, 8.0))
        except (TypeError, ValueError):
            return 2.5

    def _fetch_not_ready_max_wait_sec(self) -> float:
        """종목별 1분 조회 대기."""
        try:
            v = float(
                self._settings().get(
                    "realtime_m1_fetch_not_ready_max_wait_sec", 12.0
                )
            )
            return max(2.0, min(v, 60.0))
        except (TypeError, ValueError):
            return 12.0

    def _not_ready_max_wait_sec(self) -> float:
        """하위 호환 — 종목별 대기와 동일."""
        return self._fetch_not_ready_max_wait_sec()

    def _ranked_codes(self, codes: List[str]) -> List[str]:
        try:
            from core.rt_closed_15m_official_replace import official_replace_priority_score

            return sorted(
                codes,
                key=lambda c: (
                    -float(official_replace_priority_score(self._app, c)),
                    str(c),
                ),
            )
        except Exception:
            try:
                return sorted(codes)
            except Exception:
                return list(codes)

    def _probe_candidate_codes(self, codes: List[str]) -> List[str]:
        """프로브용 후보(복수). 한 종목 무거래에 막히지 않도록 상위 N개."""
        ranked = self._ranked_codes(codes)
        try:
            n = int(
                float(self._settings().get("realtime_m1_probe_candidate_codes", 3))
            )
        except (TypeError, ValueError):
            n = 3
        n = max(1, min(n, 8, len(ranked) if ranked else 1))
        return ranked[:n]

    def _open_round_jobs(
        self,
        codes: List[str],
        closed_lab: pd.Timestamp,
        sk: str,
        reason: str,
        *,
        kind: RoundKind = "last",
    ) -> None:
        """전 종목 조회 라운드 개시(프로브 통과·타임아웃·차트준비 공통)."""
        try:
            self._mark_round_active(sk)
            self._schedule_round_jobs(codes, closed_lab, sk, kind=kind)
        except Exception:
            pass

    def _resolve_prev_close_for_flat(self, nk: str, closed_lab: pd.Timestamp) -> float:
        """무거래 평봉용 직전 종가: M1 스토어 → bars_data → monitor 시세."""
        try:
            m1s = getattr(self._app, "rt_tick_m1_store", None)
            if m1s is not None:
                px = float(m1s.last_completed_close(nk) or 0.0)
                if px > 0:
                    return px
        except Exception:
            pass
        try:
            from utils.bars_data_keys import resolve_bars_data_frame

            bd = getattr(self._app, "bars_data", None)
            lk = getattr(self._app, "_bars_data_lock", None)
            if isinstance(bd, dict):
                def _read() -> float:
                    _k, df = resolve_bars_data_frame(bd, nk, migrate_alias=True)
                    if df is None or getattr(df, "empty", True):
                        return 0.0
                    return float(df["Close"].iloc[-1])

                if lk is not None:
                    with lk:
                        px = _read()
                else:
                    px = _read()
                if px > 0:
                    return px
        except Exception:
            pass
        try:
            mi = getattr(self._app, "monitor_info", None) or {}
            info = mi.get(nk) if isinstance(mi, dict) else None
            if isinstance(info, dict):
                for fld in ("price", "last_price", "Close"):
                    v = info.get(fld)
                    if v is not None and float(v) > 0:
                        return float(v)
        except Exception:
            pass
        return 0.0

    def _apply_flat_no_trade_minute(
        self, nk: str, closed_lab: pd.Timestamp, sk: str
    ) -> bool:
        """
        공식 1분 행이 영구 부재(무거래 등)일 때 직전 종가 평봉(V=0)을 채운다.

        Returns:
            반영 성공 여부.
        """
        try:
            px = float(self._resolve_prev_close_for_flat(nk, closed_lab))
            if px <= 0:
                return False
            m1s = getattr(self._app, "rt_tick_m1_store", None)
            if m1s is None:
                return False
            m1s.apply_official_closed_minute(nk, closed_lab, px, px, px, px, 0.0)
            try:
                key: CacheKey = (nk, sk)
                self._remember_cache(key, (px, px, px, px, 0.0))
            except Exception:
                pass
            self._schedule_signal_eval(nk, close_px=px, volume=0.0)
            try:
                if is_last_m1_open_of_15m_slot(closed_lab):
                    self._maybe_eval_closed_15m_from_m1(nk, closed_lab)
            except Exception:
                pass
            try:
                self._emit(
                    f"ℹ️ [M1Poll] {nk} {sk} 무거래 평봉 채움 (C={px:.0f}, V=0)"
                )
            except Exception:
                pass
            return True
        except Exception:
            return False

    def _job_probe_round(
        self,
        codes: List[str],
        closed_lab: pd.Timestamp,
        sk: str,
        attempt: int,
        t0_mono: Optional[float] = None,
        kind: RoundKind = "last",
    ) -> None:
        """
        상위 후보 종목으로 '시장 1분 API가 열렸는지'만 짧게 확인한 뒤
        head 일괄 또는 말분 공식 조회 라운드를 연다.
        """
        try:
            if not self._monitoring():
                return
            if not self._is_pollable_m1_label(closed_lab):
                return
            if self._is_round_dormant(sk):
                return
            # 이미 라운드가 열렸으면 중복 스케줄만
            with self._round_lock:
                if sk in self._active_rounds:
                    return
            if t0_mono is None:
                t0_mono = time.monotonic()
            try:
                from core.closed_15m_fast_path import rt15_official_quiet_remaining_sec

                rem = float(rt15_official_quiet_remaining_sec(self._app))
            except Exception:
                rem = 0.0
            if rem > 0.05:
                try:
                    t = threading.Timer(
                        rem,
                        self._job_probe_round,
                        args=(codes, closed_lab, sk, attempt, t0_mono, kind),
                    )
                    t.daemon = True
                    t.start()
                except Exception:
                    pass
                return

            candidates = self._probe_candidate_codes(codes)
            saw_fetch_error = False
            all_not_ready = True
            for raw in candidates:
                nk = canonical_stock_code(str(raw).strip())
                if not nk:
                    continue
                row, chart_df, last_err = self._fetch_closed_minute_row(
                    nk, closed_lab, sk, max_retries=1
                )
                status = self._classify_fetch_result(
                    row, chart_df, closed_lab, last_err
                )
                if status == "ok":
                    self._open_round_jobs(
                        codes, closed_lab, sk, f"probe_ok:{nk}", kind=kind
                    )
                    return
                if status == "row_not_found":
                    # 차트 시각은 목표 분을 지남 — 시장 API는 준비됨(이 종목만 무거래 가능)
                    self._open_round_jobs(
                        codes,
                        closed_lab,
                        sk,
                        f"probe_chart_ready:{nk}",
                        kind=kind,
                    )
                    return
                if status == "fetch_error":
                    saw_fetch_error = True
                    all_not_ready = False
                    continue
                # minute_not_ready
                all_not_ready = all_not_ready and (status == "minute_not_ready")

            elapsed = time.monotonic() - float(t0_mono)
            max_w = self._probe_not_ready_max_wait_sec()
            if all_not_ready and elapsed < max_w:
                poll = self._not_ready_poll_sec()
                try:
                    t = threading.Timer(
                        poll,
                        self._job_probe_round,
                        args=(codes, closed_lab, sk, attempt + 1, t0_mono, kind),
                    )
                    t.daemon = True
                    t.start()
                except Exception:
                    pass
                return

            if saw_fetch_error and attempt < 1 and elapsed < max_w:
                t = threading.Timer(
                    1.0,
                    self._job_probe_round,
                    args=(codes, closed_lab, sk, attempt + 1, t0_mono, kind),
                )
                t.daemon = True
                t.start()
                return

            # 시장 지연 짧게 기다렸거나 후보가 모두 애매 — 전 종목 각자 처리
            self._open_round_jobs(
                codes, closed_lab, sk, "probe_open_anyway", kind=kind
            )
        except Exception:
            pass

    def _apply_fetch_success(
        self,
        nk: str,
        closed_lab: pd.Timestamp,
        sk: str,
        key: CacheKey,
        row: pd.Series,
        chart_df: Optional[pd.DataFrame],
    ) -> None:
        """조회 성공 시 M1 저장·캐시·신호 평가."""
        st = self._settings()
        catchup_on = self._settings_bool(
            st, "realtime_m1_poller_apply_chart_catchup", True
        )
        try:
            ohlcv: CacheVal = (
                float(row["Open"]),
                float(row["High"]),
                float(row["Low"]),
                float(row["Close"]),
                float(row["Volume"]),
            )
        except Exception as e:
            try:
                self._emit(f"⚠️ [M1Poll] {nk} {sk} 파싱 실패: {e!r}")
            except Exception:
                pass
            return

        o, h, l_, c, v = ohlcv
        if catchup_on and chart_df is not None:
            try:
                from core.realtime_m1_fallback import apply_chart_catchup_to_m1_store

                apply_chart_catchup_to_m1_store(
                    self._app, nk, chart_df, closed_lab
                )
            except Exception:
                pass

        try:
            m1s = getattr(self._app, "rt_tick_m1_store", None)
            if m1s is not None:
                m1s.apply_official_closed_minute(nk, closed_lab, o, h, l_, c, v)
        except Exception as ex:
            try:
                self._emit(f"⚠️ [M1Poll] {nk} RtTickM1Store 반영 실패: {ex!r}")
            except Exception:
                pass
        try:
            self._remember_cache(key, (o, h, l_, c, v))
        except Exception:
            pass
        self._schedule_signal_eval(nk, close_px=c, volume=v)
        try:
            if is_last_m1_open_of_15m_slot(closed_lab):
                # 틱 말분 ON + 일반 슬롯: 공식 말분 REST로 평가하지 않음
                # 공식 말분 평가 토글 OFF(기본)면 15:30 외에도 스킵
                use_tick = self._last_minute_from_ticks_enabled()
                is_auc = False
                try:
                    is_auc = bool(is_krx_last_15m_slot(closed_lab))
                except Exception:
                    is_auc = False
                if use_tick and not is_auc:
                    return
                if (not is_auc) and (not self._eval_on_official_last_m1_enabled()):
                    return
                self._maybe_eval_closed_15m_from_m1(nk, closed_lab)
        except Exception:
            pass

    def _maybe_eval_closed_15m_from_m1(
        self, nk: str, closed_lab: pd.Timestamp
    ) -> None:
        """
        슬롯 마지막 1분 반영 후 합성 15분으로 완성봉 평가.

        보유·합치도 임박 종목만 평가(공식15 교체 우선순위와 동일 필터).
        틱 말분 경로로 이미 평가한 (code, slot) 은 스킵.
        """
        st = self._settings()
        try:
            if not self._settings_bool(st, "realtime_s1_eval_on_local_15m_close", True):
                return
        except Exception:
            return
        try:
            from core.rt_closed_15m_official_replace import (
                closed_15m_eval_after_official_enabled,
                official_replace_eval_eligible,
            )

            # 공식 15분 대기 모드면 M1 경로에서 평가하지 않음
            if closed_15m_eval_after_official_enabled(st):
                return
            if not official_replace_eval_eligible(self._app, nk, st):
                return
        except Exception:
            return

        slot = krx_15m_label_for_m1_open(closed_lab)
        if slot is None:
            return
        if self._is_tick_tail_eval_done(nk, slot):
            return

        try:
            from core.realtime_official_bars_sync import (
                apply_composed_closed_15m_from_m1,
                maybe_roll_15m_slot_on_clock,
            )

            maybe_roll_15m_slot_on_clock(self._app, nk)
            composed = apply_composed_closed_15m_from_m1(self._app, nk, slot)
            if composed is None:
                return
            o, h, l_, c, v = composed
        except Exception as ex:
            try:
                self._emit(f"⚠️ [M1Poll] {nk} 15분 합성 실패: {ex!r}")
            except Exception:
                pass
            return

        self._mark_tick_tail_eval_done(nk, slot)

        # DB upsert (비동기)
        try:
            q = getattr(self._app, "_closed_15m_db_queue", None)
            if q is not None:
                q.enqueue(nk, slot, o, h, l_, c, v)
        except Exception:
            pass

        try:
            pool = getattr(self._app, "_closed_15m_eval_pool", None)
            if pool is not None:
                pool.submit_eval(nk, slot)
                try:
                    self._emit(
                        f"ℹ️ [M1Poll] 완성15 평가 요청 {nk} slot={slot.strftime('%H:%M')}"
                    )
                except Exception:
                    pass
        except Exception as ex:
            try:
                self._emit(f"⚠️ [M1Poll] {nk} submit_eval 실패: {ex!r}")
            except Exception:
                pass
            return

    def _job_fetch_slot_head(
        self,
        code: str,
        head_last_open: pd.Timestamp,
        sk: str,
        t0_mono: Optional[float] = None,
    ) -> None:
        """
        슬롯 앞구간(공식 head) 일괄 조회 — ka10080 1페이지에서 head만 store 반영.

        ``head_last_open``: head의 마지막 1분 open (예: 09:13, 15:18).
        말분 평가·합성은 틱/공식 말분 경로가 담당한다.
        """
        try:
            if not self._monitoring():
                return
            if not self._is_pollable_m1_label(head_last_open):
                return
            if self._is_round_dormant(sk):
                return
            if t0_mono is None:
                t0_mono = time.monotonic()
            try:
                from core.closed_15m_fast_path import rt15_official_quiet_remaining_sec

                rem = float(rt15_official_quiet_remaining_sec(self._app))
            except Exception:
                rem = 0.0
            if rem > 0.05:
                try:
                    t = threading.Timer(
                        rem,
                        self._job_fetch_slot_head,
                        args=(code, head_last_open, sk, t0_mono),
                    )
                    t.daemon = True
                    t.start()
                except Exception:
                    pass
                return

            nk = canonical_stock_code(str(code).strip())
            if not nk:
                return
            slot = krx_15m_label_for_m1_open(head_last_open)
            if slot is None:
                return
            head_rng = slot_head_m1_range(slot)
            if head_rng is None:
                return
            head_start, _head_end_excl = head_rng
        except Exception:
            return

        row, chart_df, last_err = self._fetch_closed_minute_row(
            nk, head_last_open, sk, max_retries=2
        )
        status = self._classify_fetch_result(
            row, chart_df, head_last_open, last_err
        )

        if status == "minute_not_ready":
            elapsed = time.monotonic() - float(t0_mono)
            max_w = self._fetch_not_ready_max_wait_sec()
            if elapsed < max_w:
                poll = self._not_ready_poll_sec()
                try:
                    t = threading.Timer(
                        poll,
                        self._job_fetch_slot_head,
                        args=(code, head_last_open, sk, t0_mono),
                    )
                    t.daemon = True
                    t.start()
                except Exception:
                    pass
            return

        if chart_df is None or getattr(chart_df, "empty", True):
            # head 전 구간 평봉 채움은 과도 — 마지막 분만 평봉 시도
            if status in ("row_not_found", "fetch_error"):
                try:
                    self._apply_flat_no_trade_minute(nk, head_last_open, sk)
                except Exception:
                    pass
            return

        try:
            from core.realtime_m1_fallback import apply_chart_catchup_to_m1_store

            applied = apply_chart_catchup_to_m1_store(
                self._app,
                nk,
                chart_df,
                head_last_open,
                from_open=head_start,
            )
        except Exception as ex:
            try:
                self._emit(f"⚠️ [M1Poll] {nk} head catchup 실패: {ex!r}")
            except Exception:
                pass
            return

        # head 마지막 분이 차트에 없으면 평봉 보강
        if row is None and status == "row_not_found":
            try:
                self._apply_flat_no_trade_minute(nk, head_last_open, sk)
            except Exception:
                pass

        close_px = 0.0
        vol = 0.0
        try:
            if row is not None:
                close_px = float(row["Close"])
                vol = float(row["Volume"])
        except Exception:
            close_px = 0.0
            vol = 0.0
        if close_px > 0:
            try:
                self._schedule_signal_eval(nk, close_px=close_px, volume=vol)
            except Exception:
                pass
        try:
            if applied > 0:
                self._emit(
                    f"ℹ️ [M1Poll] {nk} slot={slot.strftime('%H:%M')} "
                    f"head 반영 {int(applied)}분"
                )
        except Exception:
            pass

    def _job_fetch_one(
        self,
        code: str,
        closed_lab: pd.Timestamp,
        sk: str,
        t0_mono: Optional[float] = None,
    ) -> None:
        """Timer 스레드: 말분(또는 15:30) 공식 1분 1회 조회."""
        try:
            if not self._monitoring():
                return
            if not self._is_pollable_m1_label(closed_lab):
                return
            if self._is_round_dormant(sk):
                return
            if t0_mono is None:
                t0_mono = time.monotonic()
            # 15분 슬롯 확정 직후 조용 구간: 중간 분만 연기.
            # 마지막 1분은 평가 트리거라 즉시 조회.
            if not is_last_m1_open_of_15m_slot(closed_lab):
                try:
                    from core.closed_15m_fast_path import rt15_official_quiet_remaining_sec

                    rem = float(rt15_official_quiet_remaining_sec(self._app))
                except Exception:
                    rem = 0.0
                if rem > 0.05:
                    try:
                        t = threading.Timer(
                            rem,
                            self._job_fetch_one,
                            args=(code, closed_lab, sk, t0_mono),
                        )
                        t.daemon = True
                        t.start()
                    except Exception:
                        pass
                    return
            nk = canonical_stock_code(str(code).strip())
            if not nk:
                return
            key: CacheKey = (nk, sk)
        except Exception:
            return

        st = self._settings()
        eval_on_fail = self._settings_bool(
            st, "realtime_m1_poller_eval_on_fetch_fail", True
        )
        eval_on_cache = self._settings_bool(
            st, "realtime_m1_poller_eval_on_cache_hit", True
        )
        db_fb_on = self._settings_bool(st, "realtime_intrabar_m1_db_fallback", True)

        cached_ohlcv: Optional[CacheVal] = None
        try:
            with self._cache_lock:
                cached_ohlcv = self._cache.get(key)
        except Exception:
            cached_ohlcv = None

        if cached_ohlcv is not None and eval_on_cache:
            try:
                _o, _h, _l, _c, _v = cached_ohlcv
                self._schedule_signal_eval(nk, close_px=_c, volume=_v)
            except Exception:
                pass
            return

        try:
            max_retries = int(
                float(st.get("realtime_watchlist_m1_api_max_retries", 3))
            )
        except (TypeError, ValueError):
            max_retries = 3

        row, chart_df, last_err = self._fetch_closed_minute_row(
            nk, closed_lab, sk, max_retries=max_retries
        )
        status = self._classify_fetch_result(row, chart_df, closed_lab, last_err)

        if status == "minute_not_ready":
            # 라운드 전체 dormant 금지 — 해당 종목만 재시도
            elapsed = time.monotonic() - float(t0_mono or time.monotonic())
            max_w = self._fetch_not_ready_max_wait_sec()
            if elapsed < max_w:
                poll = self._not_ready_poll_sec()
                try:
                    t = threading.Timer(
                        poll,
                        self._job_fetch_one,
                        args=(code, closed_lab, sk, t0_mono),
                    )
                    t.daemon = True
                    t.start()
                except Exception:
                    pass
                return
            # 타임아웃: DB → 무거래 평봉
            ohlcv_to: Optional[CacheVal] = None
            if db_fb_on:
                try:
                    from core.realtime_m1_fallback import closed_minute_ohlcv_from_db

                    ohlcv_to = closed_minute_ohlcv_from_db(
                        self._app, nk, closed_lab
                    )
                except Exception:
                    ohlcv_to = None
            if ohlcv_to is not None:
                o, h, l_, c, v = ohlcv_to
                try:
                    m1s = getattr(self._app, "rt_tick_m1_store", None)
                    if m1s is not None:
                        m1s.apply_official_closed_minute(
                            nk, closed_lab, o, h, l_, c, v
                        )
                except Exception:
                    pass
                self._schedule_signal_eval(nk, close_px=c, volume=v)
                try:
                    if is_last_m1_open_of_15m_slot(closed_lab):
                        self._maybe_eval_closed_15m_from_m1(nk, closed_lab)
                except Exception:
                    pass
                return
            self._apply_flat_no_trade_minute(nk, closed_lab, sk)
            return

        if status == "ok" and row is not None:
            self._apply_fetch_success(nk, closed_lab, sk, key, row, chart_df)
            return

        ohlcv: Optional[CacheVal] = None
        if db_fb_on:
            try:
                from core.realtime_m1_fallback import closed_minute_ohlcv_from_db

                ohlcv = closed_minute_ohlcv_from_db(self._app, nk, closed_lab)
            except Exception:
                ohlcv = None

        if ohlcv is None:
            if status == "row_not_found":
                # 차트는 다음 분까지 왔는데 해당 분 행만 없음 = 무거래 가능성 큼
                if self._apply_flat_no_trade_minute(nk, closed_lab, sk):
                    return
                try:
                    from core.ka10080_fetch_diag import ka10080_fetch_diag

                    ka10080_fetch_diag.record_fail(
                        nk,
                        tic_scope="1",
                        context="m1_poll",
                        reason="row_not_found",
                        closed_minute=sk,
                        detail="closed_minute_not_in_chart",
                    )
                except Exception:
                    pass
            if eval_on_fail:
                self._schedule_signal_eval(nk)
            return

        o, h, l_, c, v = ohlcv
        try:
            m1s = getattr(self._app, "rt_tick_m1_store", None)
            if m1s is not None:
                m1s.apply_official_closed_minute(nk, closed_lab, o, h, l_, c, v)
        except Exception:
            pass
        try:
            self._remember_cache(key, (o, h, l_, c, v))
        except Exception:
            pass
        self._schedule_signal_eval(nk, close_px=c, volume=v)
        try:
            if is_last_m1_open_of_15m_slot(closed_lab):
                self._maybe_eval_closed_15m_from_m1(nk, closed_lab)
        except Exception:
            pass
