# -*- coding: utf-8 -*-
"""
감시 종목(_active_stream_codes) 실시간 1분·완성15 경로.

**주경로 (틱 선적재)**
- 매분 틱으로 1분봉을 seal (``RtTickM1Store.feed_tick`` + 벽시계 seal).
- 슬롯 말분: 틱(+공식 교체분)으로 15분 합성 → S1·S2·청산 평가.

**보조 (공식 soft replace)**
- head 구간 닫힌 뒤 ka10080 1페이지 catchup으로 공식 1분을 store에 덮어씀.
  실패해도 평가를 막지 않음(틱봉 유지).
- 말분 공식 REST는 설정 ``realtime_15m_eval_on_official_last_m1`` ON일 때만.
  단, 장중 재시작으로 말분 틱이 불완전하면 공식 말분 1분을 기다렸다가 공식 1분만으로 평가.

**15:30 단일가**: 틱 확인 즉시 평가, 무거래 시 짧은 타임아웃 후 공식 폴백.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pandas as pd

from core.bt_hp_signal_bridge import ka10080_raw_rows_to_1m_df
from core.ka10080_spread import (
    effective_m1_head_catchup_spread_sec,
    effective_m1_poll_spread_sec,
)
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
# head = 공식 soft catchup, last = 말분(공식) 1회
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
        # 15:30 단일가: 당일 프로브·틱확인 라운드 1회
        self._auction_1530_scheduled_day: str = ""
        self._auction_1530_probe_started: bool = False
        self._auction_1530_tick_debounce: Dict[str, Any] = {}
        self._auction_1530_lock = threading.Lock()
        # 완성15 평가 로그 집계(슬롯별) — 종목별 반복 방지
        self._closed15_eval_log_agg: Dict[str, Dict[str, Any]] = {}
        self._closed15_eval_log_lock = threading.Lock()

    def _emit(self, msg: str) -> None:
        if self._log is None:
            return
        try:
            self._log(msg)
        except Exception:
            pass

    def _note_closed15_eval_log(
        self,
        slot: Any,
        *,
        ok: bool = True,
        reason: str = "",
        label: str = "틱말분 완성15",
    ) -> None:
        """완성15 평가 성공/실패를 슬롯 단위로 모아 디바운스 후 1회 로그."""
        if self._log is None:
            return
        try:
            slot_key = pd.Timestamp(slot).floor("min").strftime("%Y-%m-%d %H:%M:%S")
            slot_hm = pd.Timestamp(slot).strftime("%H:%M")
        except Exception:
            return
        timer_to_cancel: Any = None
        new_timer: Any = None
        try:
            with self._closed15_eval_log_lock:
                st = self._closed15_eval_log_agg.get(slot_key)
                if st is None:
                    st = {
                        "ok": 0,
                        "fail": 0,
                        "reasons": {},
                        "timer": None,
                        "slot_hm": slot_hm,
                        "label": str(label or "완성15"),
                    }
                    self._closed15_eval_log_agg[slot_key] = st
                if ok:
                    st["ok"] = int(st.get("ok", 0) or 0) + 1
                    r = str(reason or "").strip() or "ok"
                    reasons = st.get("reasons")
                    if not isinstance(reasons, dict):
                        reasons = {}
                        st["reasons"] = reasons
                    reasons[r] = int(reasons.get(r, 0) or 0) + 1
                else:
                    st["fail"] = int(st.get("fail", 0) or 0) + 1
                timer_to_cancel = st.get("timer")
                st["timer"] = None

                def _flush(sk: str = slot_key) -> None:
                    self._flush_closed15_eval_log(sk)

                try:
                    new_timer = threading.Timer(2.5, _flush)
                    new_timer.daemon = True
                    st["timer"] = new_timer
                except Exception:
                    new_timer = None
        except Exception:
            return
        if timer_to_cancel is not None:
            try:
                timer_to_cancel.cancel()
            except Exception:
                pass
        if new_timer is not None:
            try:
                new_timer.start()
            except Exception:
                try:
                    self._flush_closed15_eval_log(slot_key)
                except Exception:
                    pass

    def _flush_closed15_eval_log(self, slot_key: str) -> None:
        """슬롯 완성15 평가 집계 로그 1회 출력."""
        st: Optional[Dict[str, Any]] = None
        try:
            with self._closed15_eval_log_lock:
                st = self._closed15_eval_log_agg.pop(slot_key, None)
        except Exception:
            st = None
        if not st:
            return
        try:
            ok_n = int(st.get("ok", 0) or 0)
            fail_n = int(st.get("fail", 0) or 0)
            hm = str(st.get("slot_hm") or "")
            label = str(st.get("label") or "완성15")
            reasons = st.get("reasons") if isinstance(st.get("reasons"), dict) else {}
        except Exception:
            return
        if ok_n <= 0 and fail_n <= 0:
            return
        extra = ""
        try:
            if reasons:
                parts = [
                    f"{k}={int(v)}"
                    for k, v in sorted(reasons.items(), key=lambda x: str(x[0]))
                    if int(v or 0) > 0
                ]
                if parts:
                    extra = " (" + ", ".join(parts) + ")"
        except Exception:
            extra = ""
        try:
            if fail_n <= 0:
                self._emit(
                    f"ℹ️ [M1Poll] {label} 평가 완료 slot={hm} n={ok_n}{extra}"
                )
            elif ok_n <= 0:
                self._emit(
                    f"⚠️ [M1Poll] {label} 평가 실패 slot={hm} n={fail_n}"
                )
            else:
                self._emit(
                    f"ℹ️ [M1Poll] {label} 평가 완료 slot={hm} n={ok_n} "
                    f"(실패 {fail_n}){extra}"
                )
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

    def _trim_tick_tail_eval_done_unlocked(self) -> None:
        try:
            if len(self._tick_tail_eval_done) > 4000:
                keep = sorted(self._tick_tail_eval_done, key=lambda x: x[1])[-2000:]
                self._tick_tail_eval_done = set(keep)
        except Exception:
            pass

    def _claim_tick_tail_eval(self, nk: str, slot: pd.Timestamp) -> bool:
        """
        (종목, 슬롯) 평가 점유. :00틱과 타임아웃이 동시에 submit 하지 않게 한다.

        Returns:
            True 면 이 스레드가 평가를 진행. False 면 이미 점유·완료.
        """
        try:
            key = self._slot_eval_key(nk, slot)
            with self._tick_tail_eval_lock:
                if key in self._tick_tail_eval_done:
                    return False
                self._tick_tail_eval_done.add(key)
                self._trim_tick_tail_eval_done_unlocked()
            return True
        except Exception:
            return True

    def _release_tick_tail_eval(self, nk: str, slot: pd.Timestamp) -> None:
        """합성 실패 시 점유를 풀어 타임아웃·다음 틱이 재시도하게 한다."""
        try:
            key = self._slot_eval_key(nk, slot)
            with self._tick_tail_eval_lock:
                self._tick_tail_eval_done.discard(key)
        except Exception:
            pass

    def _mark_tick_tail_eval_done(self, nk: str, slot: pd.Timestamp) -> None:
        try:
            self._claim_tick_tail_eval(nk, slot)
        except Exception:
            pass

    def _is_tick_tail_eval_done(self, nk: str, slot: pd.Timestamp) -> bool:
        try:
            key = self._slot_eval_key(nk, slot)
            with self._tick_tail_eval_lock:
                return key in self._tick_tail_eval_done
        except Exception:
            return False

    def _order_codes_for_closed15_eval(self, codes: List[str]) -> List[str]:
        """보유·고합치도 종목을 앞에 둔다."""
        try:
            from core.rt_closed_15m_official_replace import (
                official_replace_priority_score,
            )

            uniq: List[str] = []
            seen = set()
            for raw in codes:
                try:
                    nk = canonical_stock_code(str(raw).strip())
                except Exception:
                    nk = str(raw or "").strip()
                if not nk or nk in seen:
                    continue
                seen.add(nk)
                uniq.append(nk)
            return sorted(
                uniq,
                key=lambda c: (
                    -float(official_replace_priority_score(self._app, c)),
                    str(c),
                ),
            )
        except Exception:
            out: List[str] = []
            seen = set()
            for raw in codes:
                try:
                    nk = canonical_stock_code(str(raw).strip())
                except Exception:
                    nk = str(raw or "").strip()
                if nk and nk not in seen:
                    seen.add(nk)
                    out.append(nk)
            return out

    def _count_leading_held_codes(self, ordered: List[str]) -> int:
        """정렬된 목록 앞쪽 보유 종목 수."""
        n = 0
        try:
            from core.rt_closed_15m_official_replace import (
                official_replace_priority_score,
            )

            for c in ordered:
                try:
                    if float(official_replace_priority_score(self._app, c)) >= 0.999:
                        n += 1
                    else:
                        break
                except Exception:
                    break
        except Exception:
            return 0
        return n

    def _schedule_tick_tail_evals(
        self, codes: List[str], closed_lab: pd.Timestamp
    ) -> None:
        """
        무거래 폴백: 말분 종료 + wait_sec 타임아웃 후 틱 seal → 합성 → 평가.

        활발한 종목은 ``on_tick_tail_next_minute``(:00 틱)이 먼저 평가한다.
        ``15:30`` 단일가는 ``_schedule_auction_1530_tick_path`` 를 사용한다.
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

        # 신호·주문 우선: 틱말분 평가 창 동안 공식 M1/15분 REST·정렬 양보
        # (종목 수에 비례해 quiet를 늘려 평가가 끝날 때까지 공식 반영이 GIL을 뺏지 않게)
        try:
            from core.closed_15m_fast_path import (
                closed_15m_defer_official_sec,
                mark_rt15_official_quiet,
            )

            base = float(closed_15m_defer_official_sec(self._app))
            quiet = max(base, min(45.0, 6.0 + 1.2 * float(len(codes))))
            mark_rt15_official_quiet(self._app, quiet)
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

        ordered = self._order_codes_for_closed15_eval(list(codes))
        held_n = self._count_leading_held_codes(ordered)
        try:
            self._emit(
                f"ℹ️ [M1Poll] 틱말분 타임아웃 예약 "
                f"lab={closed_lab.strftime('%H:%M')} wait={wait:.1f}s "
                f"delay={delay:.2f}s n={len(ordered)} held={held_n} "
                f"(:00틱 우선, 보유 먼저)"
            )
        except Exception:
            pass

        for i, nk in enumerate(ordered):
            extra = 0.0
            try:
                if i >= int(held_n):
                    extra = min(1.5, 0.05 * float(i - int(held_n)))
            except Exception:
                extra = 0.0
            try:
                t = threading.Timer(
                    delay + extra,
                    self._job_tick_tail_eval,
                    args=(nk, closed_lab, "timeout"),
                )
                t.daemon = True
                t.start()
            except Exception:
                continue

    def _auction_1530_tick_wait_sec(self) -> float:
        """15:30 단일가: 첫 틱 없을 때 타임아웃(초). 기본 5.0."""
        try:
            v = float(
                (self._settings() or {}).get("realtime_auction_1530_tick_wait_sec", 5.0)
            )
        except (TypeError, ValueError):
            v = 5.0
        return max(0.5, min(v, 30.0))

    def _auction_1530_eval_max_attempts(self) -> int:
        """15:30 공식1분/합성 미준비 시 종목당 최대 평가 시도 횟수."""
        try:
            v = int(
                float(
                    (self._settings() or {}).get(
                        "realtime_auction_1530_eval_max_attempts", 12
                    )
                )
            )
        except (TypeError, ValueError):
            v = 12
        return max(1, min(v, 30))

    def _auction_1530_eval_retry_gap_sec(self) -> float:
        """15:30 재시도 간격(초). ka10080 유량(1/s)을 고려해 기본 2초."""
        try:
            v = float(
                (self._settings() or {}).get(
                    "realtime_auction_1530_eval_retry_gap_sec", 2.0
                )
            )
        except (TypeError, ValueError):
            v = 2.0
        return max(0.5, min(v, 10.0))

    def _auction_1530_pending_codes(
        self, codes: List[str], auction_lab: pd.Timestamp
    ) -> List[str]:
        """15:30 슬롯 완성15 평가가 아직 안 끝난 종목."""
        pending: List[str] = []
        try:
            lab = pd.Timestamp(auction_lab).floor("min")
            slot = krx_15m_label_for_m1_open(lab)
            if slot is None:
                return list(codes)
        except Exception:
            return list(codes)
        for raw in codes:
            try:
                nk = canonical_stock_code(str(raw).strip())
                if not nk:
                    continue
                if not self._is_tick_tail_eval_done(nk, slot):
                    pending.append(nk)
            except Exception:
                continue
        return pending

    def _schedule_auction_1530_tick_path(
        self, codes: List[str], auction_lab: pd.Timestamp
    ) -> None:
        """
        15:30 단일가: 프로브 → 틱 확인 즉시 평가(틱말분과 동일 계열).

        벽시계 15:30분대에 1회 기동. 틱이 오면 ``on_auction_1530_tick`` 이 즉시 평가하고,
        무거래는 ``auction_lab + wait`` 타임아웃으로 공식 1분 폴백·평가.
        공식1분·틱이 늦으면 재시도·최종 스윕·(필요 시) 평봉 폴백으로 전 종목 평가를 보장한다.
        """
        try:
            if not self._last_minute_from_ticks_enabled():
                # 틱 경로 OFF 면 기존 공식 last 경로(15:31 wake)에 맡김
                return
            lab = pd.Timestamp(auction_lab).floor("min")
            if not is_krx_last_15m_slot(lab):
                return
        except Exception:
            return

        day_key = lab.strftime("%Y-%m-%d")
        sk = lab.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self._auction_1530_lock:
                if self._auction_1530_scheduled_day == day_key:
                    return
                self._auction_1530_scheduled_day = day_key
                self._auction_1530_probe_started = False
        except Exception:
            return

        wait = self._auction_1530_tick_wait_sec()
        max_att = self._auction_1530_eval_max_attempts()
        gap = self._auction_1530_eval_retry_gap_sec()
        try:
            due = lab.to_pydatetime() + timedelta(seconds=wait)
            delay = max(0.05, (due - datetime.now()).total_seconds())
        except Exception:
            delay = wait

        try:
            self._emit(
                f"ℹ️ [M1Poll] 15:30 단일가 틱경로 기동 "
                f"(프로브+틱확인 즉시평가 · 타임아웃 {wait:.1f}s · "
                f"재시도 {max_att}회/{gap:.1f}s · n={len(codes)})"
            )
        except Exception:
            pass
        try:
            sl = getattr(self._app, "session_logger", None)
            if sl is not None and hasattr(sl, "log"):
                sl.log(
                    "auction_1530_scheduled",
                    {
                        "slot": sk,
                        "n_codes": int(len(codes)),
                        "wait_sec": float(wait),
                        "max_attempts": int(max_att),
                        "retry_gap_sec": float(gap),
                    },
                )
        except Exception:
            pass

        # 신호·주문 우선: 단일가 초반(틱 즉시평가) 동안만 공식 REST 양보.
        # 이후 타임아웃·재시도의 공식1분 폴백은 조용 구간 종료 후 진행.
        try:
            from core.closed_15m_fast_path import (
                closed_15m_defer_official_sec,
                mark_rt15_official_quiet,
            )

            quiet = max(float(wait) + 1.0, float(closed_15m_defer_official_sec(self._app)))
            mark_rt15_official_quiet(self._app, quiet)
        except Exception:
            pass

        # 프로브 → 공식 1분 라운드(틱 없는 종목 폴백용). 틱 우선 평가는 별도.
        try:
            t_probe = threading.Timer(
                0.0,
                self._job_probe_round,
                args=(list(codes), lab, sk, 0, None, "last"),
            )
            t_probe.daemon = True
            t_probe.start()
            with self._auction_1530_lock:
                self._auction_1530_probe_started = True
        except Exception:
            pass

        code_list = [canonical_stock_code(str(c).strip()) for c in codes]
        code_list = [c for c in code_list if c]
        for nk in code_list:
            try:
                t = threading.Timer(
                    delay,
                    self._job_auction_1530_timeout_eval,
                    args=(nk, lab, 0),
                )
                t.daemon = True
                t.start()
            except Exception:
                continue

        # 최종 스윕: 재시도 창 종료 후에도 남은 종목 강제 평가
        try:
            sweep_delay = max(delay + float(max_att) * float(gap) + 1.0, delay + 3.0)
            t_sw = threading.Timer(
                sweep_delay,
                self._job_auction_1530_final_sweep,
                args=(list(code_list), lab),
            )
            t_sw.daemon = True
            t_sw.start()
        except Exception:
            pass

    def on_auction_1530_tick(self, code: Any, auction_lab: Any) -> None:
        """
        15:30 단일가 체결 틱 확인 → (짧은 디바운스 후) seal·즉시 완성15 평가.

        틱말분의 ``:00`` seal 트리거와 동일한 평가 경로(``_job_tick_tail_eval``).
        """
        try:
            if not self._last_minute_from_ticks_enabled():
                return
            nk = canonical_stock_code(str(code).strip())
            lab = pd.Timestamp(auction_lab).floor("min")
            if not nk or not is_krx_last_15m_slot(lab):
                return
            slot = krx_15m_label_for_m1_open(lab)
            if slot is None or self._is_tick_tail_eval_done(nk, slot):
                return
        except Exception:
            return

        # 프로브가 아직이면 기동(벽시계 스케줄보다 틱이 먼저일 수 있음)
        try:
            day_key = lab.strftime("%Y-%m-%d")
            codes = self._active_codes()
            with self._auction_1530_lock:
                need_sched = self._auction_1530_scheduled_day != day_key
            if need_sched and codes:
                self._schedule_auction_1530_tick_path(codes, lab)
            elif codes:
                with self._auction_1530_lock:
                    started = bool(self._auction_1530_probe_started)
                if not started:
                    sk = lab.strftime("%Y-%m-%d %H:%M:%S")
                    try:
                        t_probe = threading.Timer(
                            0.0,
                            self._job_probe_round,
                            args=(list(codes), lab, sk, 0, None, "last"),
                        )
                        t_probe.daemon = True
                        t_probe.start()
                        with self._auction_1530_lock:
                            self._auction_1530_probe_started = True
                    except Exception:
                        pass
        except Exception:
            pass

        # 종목별 짧은 디바운스(동일 초 다중 체결 병합)
        try:
            with self._auction_1530_lock:
                old = self._auction_1530_tick_debounce.pop(nk, None)
            if old is not None:
                try:
                    old.cancel()
                except Exception:
                    pass

            def _run(c: str = nk, lb: pd.Timestamp = lab) -> None:
                try:
                    with self._auction_1530_lock:
                        self._auction_1530_tick_debounce.pop(c, None)
                except Exception:
                    pass
                try:
                    m1s = getattr(self._app, "rt_tick_m1_store", None)
                    if m1s is not None:
                        m1s.seal_tick_tail_minute(c, lb)
                except Exception:
                    pass
                self._job_tick_tail_eval(
                    c, lb, "auction_tick", record_compose_fail=False
                )

            t = threading.Timer(0.15, _run)
            t.daemon = True
            with self._auction_1530_lock:
                self._auction_1530_tick_debounce[nk] = t
            t.start()
        except Exception:
            try:
                self._job_tick_tail_eval(
                    nk, lab, "auction_tick", record_compose_fail=False
                )
            except Exception:
                pass

    def _job_auction_1530_timeout_eval(
        self,
        nk: str,
        auction_lab: pd.Timestamp,
        attempt: int = 0,
    ) -> None:
        """
        틱 미수신(또는 합성 미준비) 종목: 공식 1분 반영 후 완성15 평가.

        ka10080 유량·단일가 지연으로 첫 시도가 비어도 재시도하고,
        마지막 시도에서는 무거래 평봉 폴백까지 적용해 평가를 보장한다.
        """
        if self._stop.is_set() or not self._monitoring():
            return
        try:
            lab = pd.Timestamp(auction_lab).floor("min")
            slot = krx_15m_label_for_m1_open(lab)
            if slot is None or self._is_tick_tail_eval_done(nk, slot):
                return
            max_att = self._auction_1530_eval_max_attempts()
            att = max(0, int(attempt or 0))
            is_last = att >= (max_att - 1)

            # 공식 1분이 있으면 completed에 반영(틱 없을 때 compose 폴백)
            try:
                sk = lab.strftime("%Y-%m-%d %H:%M:%S")
                self._job_fetch_one(nk, lab, sk)
            except Exception:
                pass
            try:
                m1s = getattr(self._app, "rt_tick_m1_store", None)
                if m1s is not None:
                    m1s.seal_tick_tail_minute(nk, lab)
            except Exception:
                pass

            ok = self._job_tick_tail_eval(
                nk,
                lab,
                "auction_timeout" if not is_last else "auction_timeout_final",
                record_compose_fail=is_last,
            )
            if ok or self._is_tick_tail_eval_done(nk, slot):
                return

            # 마지막 시도: 공식행 영구 부재(무거래)면 평봉으로라도 평가
            if is_last:
                try:
                    sk = lab.strftime("%Y-%m-%d %H:%M:%S")
                    if self._apply_flat_no_trade_minute(nk, lab, sk):
                        ok2 = self._job_tick_tail_eval(
                            nk,
                            lab,
                            "auction_flat",
                            record_compose_fail=True,
                        )
                        if ok2 or self._is_tick_tail_eval_done(nk, slot):
                            return
                except Exception:
                    pass
                try:
                    self._emit(
                        f"⚠️ [M1Poll] 15:30 평가 최종 실패 {nk} "
                        f"(시도 {att + 1}/{max_att})"
                    )
                except Exception:
                    pass
                try:
                    sl = getattr(self._app, "session_logger", None)
                    if sl is not None and hasattr(sl, "log"):
                        sl.log(
                            "auction_1530_eval_final_fail",
                            {
                                "code": nk,
                                "slot": lab.strftime("%Y-%m-%d %H:%M:%S"),
                                "attempt": int(att + 1),
                                "max_attempts": int(max_att),
                            },
                        )
                except Exception:
                    pass
                return

            gap = self._auction_1530_eval_retry_gap_sec()
            try:
                t = threading.Timer(
                    gap,
                    self._job_auction_1530_timeout_eval,
                    args=(nk, lab, att + 1),
                )
                t.daemon = True
                t.start()
            except Exception:
                pass
        except Exception as e:
            try:
                self._emit(f"⚠️ [M1Poll] 15:30 타임아웃 평가 예외 ({nk}): {e!r}")
            except Exception:
                pass

    def _job_auction_1530_final_sweep(
        self, codes: List[str], auction_lab: pd.Timestamp
    ) -> None:
        """재시도 창 종료 후 미평가 종목 일괄 강제 평가(평봉 폴백 포함)."""
        if self._stop.is_set() or not self._monitoring():
            return
        try:
            lab = pd.Timestamp(auction_lab).floor("min")
            pending = self._auction_1530_pending_codes(codes, lab)
        except Exception:
            return
        if not pending:
            try:
                self._emit(
                    f"ℹ️ [M1Poll] 15:30 최종 스윕 — 전 종목 평가 완료 "
                    f"n={len(codes)}"
                )
            except Exception:
                pass
            return
        try:
            self._emit(
                f"⚠️ [M1Poll] 15:30 최종 스윕 — 미평가 {len(pending)}종목 "
                f"강제 평가: {','.join(pending)}"
            )
        except Exception:
            pass
        max_att = self._auction_1530_eval_max_attempts()
        for i, nk in enumerate(pending):
            try:
                # 유량 보호: 종목 간 짧게 분산
                delay = 0.05 + 0.35 * float(i)
                t = threading.Timer(
                    delay,
                    self._job_auction_1530_timeout_eval,
                    args=(nk, lab, max(0, max_att - 1)),
                )
                t.daemon = True
                t.start()
            except Exception:
                try:
                    self._job_auction_1530_timeout_eval(nk, lab, max(0, max_att - 1))
                except Exception:
                    continue
        # 스윕 직후 잔여 확인 로그
        try:

            def _report() -> None:
                try:
                    left = self._auction_1530_pending_codes(codes, lab)
                    if left:
                        self._emit(
                            f"❌ [M1Poll] 15:30 평가 미완 잔여 {len(left)}: "
                            f"{','.join(left)}"
                        )
                    else:
                        self._emit(
                            "ℹ️ [M1Poll] 15:30 최종 스윕 후 전 종목 평가 완료"
                        )
                except Exception:
                    pass

            t_r = threading.Timer(max(3.0, 0.4 * float(len(pending)) + 2.0), _report)
            t_r.daemon = True
            t_r.start()
        except Exception:
            pass

    def on_tick_tail_next_minute(self, code: Any, closed_lab: Any) -> None:
        """
        다음 분(:00) 체결로 말분이 seal된 직후 — 즉시 완성15 평가.

        WS 틱 스레드에서 호출될 수 있으므로 짧은 타이머(0)로 분리해도 되고,
        중복은 ``_tick_tail_eval_done`` 으로 막는다.
        ``15:30`` 은 ``on_auction_1530_tick`` 경로를 사용한다.
        """
        try:
            if not self._last_minute_from_ticks_enabled():
                return
            nk = canonical_stock_code(str(code).strip())
            lab = pd.Timestamp(closed_lab).floor("min")
            if not nk or not is_last_m1_open_of_15m_slot(lab):
                return
            if is_krx_last_15m_slot(lab):
                # 단일가: 다음 분 틱이 없을 수 있어 auction 경로로 위임
                self.on_auction_1530_tick(nk, lab)
                return
            slot = krx_15m_label_for_m1_open(lab)
            if slot is None or self._is_tick_tail_eval_done(nk, slot):
                return
            try:
                from core.realtime_official_bars_sync import (
                    last_minute_ticks_complete_for_eval,
                )

                if not last_minute_ticks_complete_for_eval(self._app, nk, lab):
                    return
            except Exception:
                pass
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
        *,
        record_compose_fail: bool = True,
        retry: int = 0,
    ) -> bool:
        """
        틱 말분 seal·합성 15분 평가 (``:00`` 틱 또는 타임아웃).

        Returns:
            완성15 평가 제출 성공 여부.
        """
        if self._stop.is_set() or not self._monitoring():
            return False
        claimed = False
        slot: Optional[pd.Timestamp] = None
        try:
            slot = krx_15m_label_for_m1_open(closed_lab)
            if slot is None:
                return False
            if self._is_tick_tail_eval_done(nk, slot):
                return True

            try:
                from core.realtime_official_bars_sync import (
                    last_minute_ticks_complete_for_eval,
                )

                if not last_minute_ticks_complete_for_eval(
                    self._app, nk, closed_lab
                ):
                    # 부분 틱 말분으로 평가하지 않음 — 공식 말분 1분 대기
                    return False
            except Exception:
                return False

            st = self._settings()
            if not self._settings_bool(st, "realtime_s1_eval_on_local_15m_close", True):
                return False
            from core.rt_closed_15m_official_replace import (
                closed_15m_eval_after_official_enabled,
            )

            if closed_15m_eval_after_official_enabled(st):
                return False

            # :00틱 vs 타임아웃 레이스 — 합성 전에 점유해야 이중 평가가 없다
            if not self._claim_tick_tail_eval(nk, slot):
                return True
            claimed = True

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
                    self._release_tick_tail_eval(nk, slot)
                except Exception:
                    pass
                claimed = False
                # 합성 미준비(:00틱이 타임아웃보다 먼저 실패 등) — 짧게 재시도
                try:
                    att = int(retry or 0)
                except (TypeError, ValueError):
                    att = 0
                if att < 6:
                    try:
                        t_r = threading.Timer(
                            0.4,
                            self._job_tick_tail_eval,
                            args=(nk, closed_lab, reason),
                            kwargs={
                                "record_compose_fail": att >= 5,
                                "retry": att + 1,
                            },
                        )
                        t_r.daemon = True
                        t_r.start()
                    except Exception:
                        pass
                    return False
                if record_compose_fail:
                    try:
                        self._note_closed15_eval_log(
                            slot,
                            ok=False,
                            reason=str(reason or "compose_fail"),
                            label="틱말분 완성15",
                        )
                    except Exception:
                        pass
                return False
            o, h, l_, c, v = composed

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
                    self._note_closed15_eval_log(
                        slot,
                        ok=True,
                        reason=str(reason or "ok"),
                        label="틱말분 완성15",
                    )
                    return True
            except Exception as ex:
                self._emit(f"⚠️ [M1Poll] {nk} 틱말분 submit_eval 실패: {ex!r}")
            try:
                self._release_tick_tail_eval(nk, slot)
            except Exception:
                pass
            return False
        except Exception as e:
            try:
                if claimed and slot is not None:
                    self._release_tick_tail_eval(nk, slot)
            except Exception:
                pass
            try:
                self._emit(f"⚠️ [M1Poll] 틱말분 평가 예외 ({nk}): {e!r}")
            except Exception:
                pass
            return False

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

    def _bars_data_has_slot(self, nk: str, slot_ts: pd.Timestamp) -> bool:
        """워밍업 bars_data 에 해당 15분 슬롯 행이 있는지."""
        try:
            from utils.bars_data_keys import resolve_bars_data_frame

            bd = getattr(self._app, "bars_data", None)
            if not isinstance(bd, dict):
                return False
            lk = getattr(self._app, "_bars_data_lock", None)

            def _has() -> bool:
                _k, df = resolve_bars_data_frame(bd, nk, migrate_alias=True)
                if df is None or getattr(df, "empty", True):
                    return False
                want = pd.Timestamp(slot_ts).floor("min")
                try:
                    if want in df.index:
                        return True
                except Exception:
                    pass
                try:
                    for ix in df.index:
                        if pd.Timestamp(ix).floor("min") == want:
                            return True
                except Exception:
                    pass
                return False

            if lk is not None:
                with lk:
                    return bool(_has())
            return bool(_has())
        except Exception:
            return False

    def catchup_eval_inprogress_slot_after_stream_ready(self) -> None:
        """
        감시 시작(워밍업) 직후: **당일 직전 완성 15분 슬롯**을 한 번 평가합니다.

        예전에는 ``rt_monitor_inprogress_slot``(형성 중 봉)만 봐서,
        예: 13:48 재시작 시 13:45봉은 미완성 → 조기 return 하고
        이미 끝난 13:30봉 평가가 영구 누락됐습니다.
        워밍업으로 채워 둔 15분 OHLCV가 있으면 M1 합성 실패여도 평가합니다.
        """
        try:
            if self._stop.is_set() or not self._monitoring():
                return
            from utils.krx_bars import last_completed_krx_15m_slot_end

            slot_ts = last_completed_krx_15m_slot_end(datetime.now())
            if slot_ts is None:
                return
            slot_ts = pd.Timestamp(slot_ts).floor("min")

            try:
                st = self._settings()
                if not self._settings_bool(
                    st, "realtime_s1_eval_on_local_15m_close", True
                ):
                    return
                from core.rt_closed_15m_official_replace import (
                    closed_15m_eval_after_official_enabled,
                )

                if closed_15m_eval_after_official_enabled(st):
                    return
            except Exception:
                pass

            codes = self._active_codes()
            if not codes:
                return
            # 보유·합치도 높은 종목 우선
            try:
                from core.rt_closed_15m_official_replace import (
                    official_replace_priority_score,
                )

                codes = sorted(
                    codes,
                    key=lambda c: (
                        -float(official_replace_priority_score(self._app, c)),
                        str(c),
                    ),
                )
            except Exception:
                pass

            n_try = 0
            n_skip = 0
            for raw in codes:
                try:
                    nk = canonical_stock_code(str(raw).strip())
                    if not nk:
                        continue
                    if self._is_tick_tail_eval_done(nk, slot_ts):
                        n_skip += 1
                        continue
                    if not self._claim_tick_tail_eval(nk, slot_ts):
                        n_skip += 1
                        continue
                    try:
                        from core.realtime_official_bars_sync import (
                            apply_composed_closed_15m_from_m1,
                            maybe_roll_15m_slot_on_clock,
                        )

                        maybe_roll_15m_slot_on_clock(self._app, nk)
                        try:
                            apply_composed_closed_15m_from_m1(
                                self._app, nk, slot_ts
                            )
                        except Exception:
                            pass
                    except Exception:
                        pass
                    # M1 스토어가 비어 compose 실패해도 워밍업 15분봉으로 평가
                    if not self._bars_data_has_slot(nk, slot_ts):
                        try:
                            self._release_tick_tail_eval(nk, slot_ts)
                        except Exception:
                            pass
                        continue
                    pool = getattr(self._app, "_closed_15m_eval_pool", None)
                    if pool is None:
                        try:
                            self._release_tick_tail_eval(nk, slot_ts)
                        except Exception:
                            pass
                        continue
                    pool.submit_eval(nk, slot_ts)
                    n_try += 1
                    try:
                        self._note_closed15_eval_log(
                            slot_ts,
                            ok=True,
                            reason="restart_catchup",
                            label="재시작 완성15",
                        )
                    except Exception:
                        pass
                except Exception:
                    continue
            if n_try > 0 or n_skip > 0:
                try:
                    self._emit(
                        f"ℹ️ [M1Poll] 재시작 완성슬롯 평가 "
                        f"{slot_ts.strftime('%H:%M')} "
                        f"submit={n_try} skip={n_skip}"
                    )
                except Exception:
                    pass
        except Exception as e:
            try:
                self._emit(f"⚠️ [M1Poll] 재시작 슬롯 catchup 예외: {e!r}")
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
                # :13 마감~:00 평가(~60s) 안에 끝나도록 헤드 전용 spread
                # (레거시 M1 40s 바닥·해시 분산 대신 우선순위 순 균등 간격)
                n_codes = len(ordered)
                try:
                    spread = effective_m1_head_catchup_spread_sec(
                        self._settings(), n_codes
                    )
                except Exception:
                    spread = max(3.0, min(50.0, float(n_codes) * 1.1))
                spread = max(1.0, min(float(spread), 55.0))
                gap = (
                    float(spread) / float(max(n_codes - 1, 1))
                    if n_codes > 1
                    else 0.0
                )
                for i, cc in enumerate(ordered):
                    try:
                        delay = min(float(spread), float(i) * gap)
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

    def _head_official_catchup_enabled(self) -> bool:
        """슬롯 head 구간 공식 soft catchup(기본 ON). 실패해도 평가 비차단."""
        return self._settings_bool(
            self._settings(), "realtime_m1_head_official_catchup_enabled", True
        )

    def _schedule_minute_tick_seals(
        self, codes: List[str], closed_lab: pd.Timestamp
    ) -> None:
        """
        닫힌 1분을 벽시계 기준으로 틱 seal (다음분 틱이 없어도 확정).

        말분은 ``_schedule_tick_tail_evals`` 가 seal+평가를 담당하므로 여기서는
        seal만 중복 호출(멱등)해도 무방하나, 말분은 스킵해 타이머 수를 줄인다.
        """
        try:
            if is_last_m1_open_of_15m_slot(closed_lab):
                return
            if is_krx_last_15m_slot(closed_lab):
                return
        except Exception:
            return

        wait = self._tick_tail_wait_sec()
        try:
            end_wall = (
                pd.Timestamp(closed_lab).floor("min") + pd.Timedelta(minutes=1)
            ).to_pydatetime()
            due = end_wall + timedelta(seconds=wait)
            delay = max(0.05, (due - datetime.now()).total_seconds())
        except Exception:
            delay = wait

        for raw in codes:
            try:
                nk = canonical_stock_code(str(raw).strip())
                if not nk:
                    continue
                t = threading.Timer(
                    delay,
                    self._job_seal_tick_minute,
                    args=(nk, closed_lab),
                )
                t.daemon = True
                t.start()
            except Exception:
                continue

    def _job_seal_tick_minute(
        self, nk: str, closed_lab: pd.Timestamp
    ) -> None:
        """닫힌 1분 틱 forming → seal (API 없음)."""
        if self._stop.is_set() or not self._monitoring():
            return
        try:
            m1s = getattr(self._app, "rt_tick_m1_store", None)
            if m1s is None:
                return
            m1s.seal_tick_tail_minute(nk, closed_lab)
        except Exception:
            pass

    def _tick_minute_has_bar(self, nk: str, closed_lab: pd.Timestamp) -> bool:
        """해당 분에 틱 1분봉(종가>0)이 있으면 True."""
        try:
            m1s = getattr(self._app, "rt_tick_m1_store", None)
            if m1s is None:
                return False
            bar = m1s.tick_minute_ohlcv(nk, closed_lab)
            return bar is not None and float(bar[3]) > 0
        except Exception:
            return False

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
        """새로 닫힌 1분 라벨이 바뀌었을 때만: 틱 seal · (선택) head soft catchup · 말분 평가."""
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

                # 15:30 단일가: 벽시계 15:30분대에 프로브+틱확인 경로 기동
                # (just-completed 규칙은 15:31에야 15:30을 닫힌 분으로 보므로 별도 처리)
                try:
                    wf = pd.Timestamp(wall).floor("min")
                    if int(wf.hour) == 15 and int(wf.minute) == 30:
                        codes_a = self._active_codes()
                        if codes_a:
                            self._schedule_auction_1530_tick_path(codes_a, wf)
                except Exception:
                    pass

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
                tick_last_incomplete = False
                try:
                    if use_tick and is_last and (not is_auc):
                        from core.realtime_official_bars_sync import (
                            tick_stream_covers_minute_open,
                        )

                        tick_last_incomplete = not tick_stream_covers_minute_open(
                            self._app, closed_lab
                        )
                except Exception:
                    tick_last_incomplete = bool(use_tick and is_last)

                # 매분 틱 seal (말분·단일가 제외 — 말분은 tick_tail 경로)
                try:
                    self._schedule_minute_tick_seals(codes, closed_lab)
                except Exception:
                    pass

                # 말분 틱 평가 예약(15:30 제외 — 단일가는 auction 틱경로)
                # 말분 틱이 불완전하면 공식 말분 1분을 기다림
                try:
                    if (
                        is_last
                        and (not is_auc)
                        and use_tick
                        and (not tick_last_incomplete)
                    ):
                        self._schedule_tick_tail_evals(codes, closed_lab)
                except Exception:
                    pass

                need_head = bool(
                    is_head_done and self._head_official_catchup_enabled()
                )
                need_last_official = bool(
                    is_last
                    and (
                        is_auc
                        or ((not use_tick) and use_official_last)
                        or tick_last_incomplete
                    )
                )
                # 당일 15:30 틱경로를 이미 기동했어도, 미평가 종목은 15:31 공식 last로 구조
                if need_last_official and is_auc and use_tick:
                    try:
                        day_key = closed_lab.strftime("%Y-%m-%d")
                        with self._auction_1530_lock:
                            auc_scheduled = self._auction_1530_scheduled_day == day_key
                        if auc_scheduled:
                            pending = self._auction_1530_pending_codes(
                                codes, closed_lab
                            )
                            if not pending:
                                need_last_official = False
                            else:
                                codes = pending
                                try:
                                    self._emit(
                                        f"ℹ️ [M1Poll] 15:30 미평가 {len(pending)}종목 "
                                        f"→ 15:31 공식1분 구조"
                                    )
                                except Exception:
                                    pass
                    except Exception:
                        pass
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

        틱 1분이 이미 있으면 덮어쓰지 않는다(틱 선적재 · 공식 soft replace).

        Returns:
            반영 성공 여부.
        """
        try:
            # 틱봉이 있으면 평봉으로 공식 completed를 오염시키지 않음
            try:
                if self._settings_bool(
                    self._settings(),
                    "realtime_15m_compose_tick_fill_on_api_miss",
                    True,
                ) and self._tick_minute_has_bar(nk, closed_lab):
                    return False
            except Exception:
                pass
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
                # 단, 말분 틱이 불완전하면 공식 1분 도착 후 평가
                use_tick = self._last_minute_from_ticks_enabled()
                is_auc = False
                try:
                    is_auc = bool(is_krx_last_15m_slot(closed_lab))
                except Exception:
                    is_auc = False
                tick_ok = True
                try:
                    from core.realtime_official_bars_sync import (
                        last_minute_ticks_complete_for_eval,
                    )

                    tick_ok = bool(
                        last_minute_ticks_complete_for_eval(
                            self._app, nk, closed_lab
                        )
                    )
                except Exception:
                    tick_ok = False
                if use_tick and (not is_auc) and tick_ok:
                    return
                if (not is_auc) and (not self._eval_on_official_last_m1_enabled()) and tick_ok:
                    return
                self._maybe_eval_closed_15m_from_m1(nk, closed_lab)
        except Exception:
            pass

    def _maybe_eval_closed_15m_from_m1(
        self, nk: str, closed_lab: pd.Timestamp
    ) -> None:
        """
        슬롯 마지막 1분 반영 후 합성 15분으로 완성봉 평가.

        감시 종목 전부에 대해 평가(합치도 게이트 없음). 신호·사이징은 평가 내부에서 결정.
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
            )

            # 공식 15분 대기 모드면 M1 경로에서 평가하지 않음
            if closed_15m_eval_after_official_enabled(st):
                return
        except Exception:
            return

        slot = krx_15m_label_for_m1_open(closed_lab)
        if slot is None:
            return
        if not self._claim_tick_tail_eval(nk, slot):
            return

        try:
            from core.realtime_official_bars_sync import (
                apply_composed_closed_15m_from_m1,
                maybe_roll_15m_slot_on_clock,
            )

            maybe_roll_15m_slot_on_clock(self._app, nk)
            composed = apply_composed_closed_15m_from_m1(self._app, nk, slot)
            if composed is None:
                try:
                    self._release_tick_tail_eval(nk, slot)
                except Exception:
                    pass
                return
            o, h, l_, c, v = composed
        except Exception as ex:
            try:
                self._release_tick_tail_eval(nk, slot)
            except Exception:
                pass
            try:
                self._emit(f"⚠️ [M1Poll] {nk} 15분 합성 실패: {ex!r}")
            except Exception:
                pass
            return

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
                    self._note_closed15_eval_log(
                        slot, ok=True, reason="m1_path", label="완성15"
                    )
                except Exception:
                    pass
                return
            try:
                self._release_tick_tail_eval(nk, slot)
            except Exception:
                pass
        except Exception as ex:
            try:
                self._release_tick_tail_eval(nk, slot)
            except Exception:
                pass
            try:
                self._emit(f"⚠️ [M1Poll] {nk} submit_eval 실패: {ex!r}")
            except Exception:
                pass
            return

    def _log_m1_head_catchup(
        self,
        report: Dict[str, Any],
        *,
        emit_incomplete: bool = True,
    ) -> None:
        """헤드 catchup 커버리지를 세션 JSONL에 기록합니다."""
        try:
            sl = getattr(self._app, "session_logger", None)
            if sl is not None and hasattr(sl, "log"):
                sl.log("m1_head_catchup_coverage", dict(report))
        except Exception:
            pass
        try:
            if not emit_incomplete:
                return
            if bool(report.get("complete")):
                return
            nk = report.get("code")
            exp = report.get("expected_n")
            stn = report.get("store_n")
            miss = report.get("missing") or []
            status = report.get("status")
            self._emit(
                f"⚠️ [M1Poll] head catchup 불완전 {nk} "
                f"store={stn}/{exp} status={status} missing={miss}"
            )
        except Exception:
            pass

    def _fill_missing_15m_after_head_catchup(self, code: str) -> None:
        """헤드 1분 catchup 직후, 닫힌 15분 누락·스텁을 공식1분 합성으로 채웁니다."""
        try:
            st = self._settings()
            if not bool(st.get("realtime_m1_head_fill_missing_15m_enabled", True)):
                return
            max_bars = int(st.get("realtime_m1_head_fill_missing_15m_max_bars", 4) or 4)
        except Exception:
            max_bars = 4
        try:
            from core.realtime_official_bars_sync import (
                fill_missing_closed_15m_from_official_m1,
            )

            rep = fill_missing_closed_15m_from_official_m1(
                self._app, code, max_bars=max_bars
            )
        except Exception:
            return
        try:
            n_miss = len(rep.get("filled_missing") or [])
            n_stub = len(rep.get("filled_stub") or [])
            n_fail = len(rep.get("failed") or [])
            if n_miss or n_stub or n_fail:
                sl = getattr(self._app, "session_logger", None)
                if sl is not None and hasattr(sl, "log"):
                    sl.log("m1_head_15m_gap_fill", dict(rep))
            if n_miss or n_stub:
                self._emit(
                    f"[M1Poll] 15m gap-fill {rep.get('code')} "
                    f"missing={n_miss} stub={n_stub} fail={n_fail}"
                )
        except Exception:
            pass

    def _job_fetch_slot_head(
        self,
        code: str,
        head_last_open: pd.Timestamp,
        sk: str,
        t0_mono: Optional[float] = None,
    ) -> None:
        """
        슬롯 head 구간 **공식 soft catchup** — ka10080 1페이지로 공식 1분을 store에 덮어씀.

        틱으로 이미 seal된 분은 compose 시 공식이 우선 교체된다.
        조회 실패 시 평봉으로 틱을 덮지 않으며, 말분 평가도 막지 않는다.
        ``head_last_open``: head의 마지막 1분 open (예: 09:13, 15:18).
        """
        slot: Optional[pd.Timestamp] = None
        head_start: Optional[pd.Timestamp] = None
        nk = ""
        try:
            if not self._monitoring():
                return
            if not self._head_official_catchup_enabled():
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
            try:
                from core.realtime_m1_fallback import build_m1_head_catchup_report

                if head_start is not None and slot is not None:
                    rep = build_m1_head_catchup_report(
                        self._app,
                        nk,
                        slot=slot,
                        head_start=head_start,
                        head_last_open=head_last_open,
                        chart_df=chart_df,
                        applied=0,
                        status="minute_not_ready_timeout",
                        detail=str(last_err or "")[:200],
                    )
                    self._log_m1_head_catchup(rep)
            except Exception:
                pass
            return

        if chart_df is None or getattr(chart_df, "empty", True):
            # soft catchup 실패: 틱봉이 있으면 평봉 미적용(틱 유지)
            if status in ("row_not_found", "fetch_error"):
                try:
                    if not self._tick_minute_has_bar(nk, head_last_open):
                        self._apply_flat_no_trade_minute(nk, head_last_open, sk)
                except Exception:
                    pass
            try:
                from core.realtime_m1_fallback import build_m1_head_catchup_report

                if head_start is not None and slot is not None:
                    rep = build_m1_head_catchup_report(
                        self._app,
                        nk,
                        slot=slot,
                        head_start=head_start,
                        head_last_open=head_last_open,
                        chart_df=chart_df,
                        applied=0,
                        status=f"chart_empty:{status}",
                        detail=str(last_err or "")[:200],
                    )
                    self._log_m1_head_catchup(rep)
            except Exception:
                pass
            return

        applied = 0
        try:
            from core.realtime_m1_fallback import (
                apply_chart_catchup_to_m1_store,
                build_m1_head_catchup_report,
            )

            # 헤드 구간 + 직전 누락 15분 메우기용 1분을 같은 차트 페이지로 반영
            catchup_from = head_start
            try:
                st = self._settings()
                if bool(st.get("realtime_m1_head_fill_missing_15m_enabled", True)):
                    heal_n = int(st.get("realtime_m1_head_fill_missing_15m_max_bars", 4) or 4)
                    heal_n = max(1, min(heal_n, 16))
                    heal_from = pd.Timestamp(head_start) - pd.Timedelta(
                        minutes=15 * int(heal_n)
                    )
                    if heal_from < pd.Timestamp(head_start):
                        catchup_from = heal_from
            except Exception:
                catchup_from = head_start

            applied = int(
                apply_chart_catchup_to_m1_store(
                    self._app,
                    nk,
                    chart_df,
                    head_last_open,
                    from_open=catchup_from,
                )
                or 0
            )
            if head_start is not None and slot is not None:
                rep = build_m1_head_catchup_report(
                    self._app,
                    nk,
                    slot=slot,
                    head_start=head_start,
                    head_last_open=head_last_open,
                    chart_df=chart_df,
                    applied=applied,
                    status="ok",
                    detail="",
                )
                if bool(rep.get("complete")):
                    rep["status"] = "ok"
                else:
                    rep["status"] = "incomplete"
                self._log_m1_head_catchup(rep)
            # 공식 15분 교체 보조: 최근 닫힌 슬롯의 누락·스텁을 1분 합성으로 채움
            try:
                self._fill_missing_15m_after_head_catchup(nk)
            except Exception:
                pass
        except Exception as ex:
            try:
                self._emit(f"⚠️ [M1Poll] {nk} head soft catchup 실패: {ex!r}")
            except Exception:
                pass
            try:
                from core.realtime_m1_fallback import build_m1_head_catchup_report

                if head_start is not None and slot is not None:
                    rep = build_m1_head_catchup_report(
                        self._app,
                        nk,
                        slot=slot,
                        head_start=head_start,
                        head_last_open=head_last_open,
                        chart_df=chart_df,
                        applied=int(applied),
                        status="exception",
                        detail=repr(ex),
                    )
                    self._log_m1_head_catchup(rep)
            except Exception:
                pass
            return

        # head 마지막 분이 차트에 없으면 — 틱 없을 때만 평봉
        if row is None and status == "row_not_found":
            try:
                if not self._tick_minute_has_bar(nk, head_last_open):
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
            # 슬롯 확정 직후 조용 구간: 말분 공식 조회도 양보.
            # (틱 말분이 신호·주문을 담당 — 공식 말분 REST가 ka10004/주문과 경합하지 않게)
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
            # API miss(empty 등)여도 슬롯 말분이면 틱 누적 합성으로 완성15 평가.
            # 말분 틱이 불완전하면 부분 틱으로 평가하지 않고 공식 재시도를 기다린다.
            try:
                if is_last_m1_open_of_15m_slot(closed_lab):
                    tick_ok = True
                    try:
                        from core.realtime_official_bars_sync import (
                            last_minute_ticks_complete_for_eval,
                        )

                        tick_ok = bool(
                            last_minute_ticks_complete_for_eval(
                                self._app, nk, closed_lab
                            )
                        )
                    except Exception:
                        tick_ok = False
                    if tick_ok:
                        try:
                            m1s = getattr(self._app, "rt_tick_m1_store", None)
                            if m1s is not None:
                                m1s.seal_tick_tail_minute(nk, closed_lab)
                        except Exception:
                            pass
                        self._maybe_eval_closed_15m_from_m1(nk, closed_lab)
            except Exception:
                pass
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
