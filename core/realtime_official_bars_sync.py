# -*- coding: utf-8 -*-
"""
실시간 **신호용** 15분봉: 벽시계 슬롯 롤로 ``bars_data`` 를 진행시키고,
확정 콜백에서 전용 DB·공식 15분 REST 덮어쓰기 스케줄을 호출합니다.

- 완성봉 OHLCV의 권위 소스는 공식 15분 교체(``rt_closed_15m_official_replace``)입니다.
  롤 시점의 행 값은 워밍업/틱 자리표시자이며, 공식 교체 후 평가가 기본입니다.
- 감시 트리·체결가 등 UI/주문용 틱 가격은 ``SignalEngine.on_tick_received`` 가 별도 처리합니다.
- 본 모듈은 ``_signal_skip_bar_update=True`` 로 신호 재평가만 트리거할 때 사용합니다.
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pandas as pd

from core.rt_closed_15m_official_replace import ts_to_slot_hhmm
from utils.bars_data_keys import resolve_bars_data_frame
from utils.krx_bars import (
    is_krx_15m_slot_end,
    krx_stream_slot_end_datetime,
    next_krx_15m_slot_end,
    seconds_until_next_krx_15m_boundary,
)
from utils.normalization import canonical_stock_code


def stream_slot_end_datetime(now: datetime) -> pd.Timestamp:
    """KRX 장중 15분봉 **좌측 라벨**(야간·장외 슬롯 생성 없음)."""
    return krx_stream_slot_end_datetime(now)


def _on_bar_closed_safe(app: Any, code: str, ts: Any, o: float, h: float, l: float, c: float, v: float) -> None:
    """확정 15분봉 콜백(전용 DB·공식 REST 스케줄)."""
    try:
        fn = getattr(app, "_rt15_bar_closed_cb", None)
        if callable(fn):
            fn(code, ts, o, h, l, c, v)
    except Exception:
        pass


def maybe_roll_15m_slot_on_clock(app: Any, code: str) -> bool:
    """
    ``bars_data`` 마지막 인덱스가 현재 슬롯 종가 시각보다 과거면,
    슬롯이 진행한 만큼 확정 콜백을 호출하고 새 행을 추가합니다.

    compose·콜백은 ``_bars_data_lock`` **밖**에서 수행해 락 경합·DB I/O가
    다른 종목 롤/평가를 막지 않게 합니다.

    Returns:
        ``bars_data`` 가 변경되었으면 True.
    """
    nk = canonical_stock_code(str(code).strip())
    if not nk:
        return False
    lk = getattr(app, "_bars_data_lock", None)
    bd = getattr(app, "bars_data", None)
    if lk is None or not isinstance(bd, dict):
        return False
    changed = False
    pending_cbs: List[Tuple[str, Any, float, float, float, float, float]] = []
    try:
        guard = 0
        while guard < 64:
            guard += 1
            use_key = nk
            last_ix: Optional[pd.Timestamp] = None
            want: Optional[pd.Timestamp] = None
            raw_o = raw_h = raw_l = raw_c = raw_v = 0.0
            try:
                with lk:
                    _k, df = resolve_bars_data_frame(bd, nk, migrate_alias=True)
                    use_key = _k or nk
                    if df is None or getattr(df, "empty", True):
                        break
                    want = stream_slot_end_datetime(datetime.now())
                    try:
                        last_ix = pd.Timestamp(df.index[-1])
                    except Exception:
                        break
                    if last_ix >= want:
                        break
                    try:
                        raw_o = float(df.at[last_ix, "Open"])
                        raw_h = float(df.at[last_ix, "High"])
                        raw_l = float(df.at[last_ix, "Low"])
                        raw_c = float(df.at[last_ix, "Close"])
                        raw_v = float(df.at[last_ix, "Volume"])
                    except Exception:
                        raw_o = raw_h = raw_l = raw_c = raw_v = 0.0
            except Exception:
                break

            if last_ix is None or want is None:
                break

            # 롤 시점 OHLCV는 자리표시자(워밍업/틱). 권위 봉은 이후 공식 15분 교체.
            o, h, l_, c, v = raw_o, raw_h, raw_l, raw_c, raw_v

            try:
                with lk:
                    _k2, df2 = resolve_bars_data_frame(bd, nk, migrate_alias=True)
                    use_key = _k2 or use_key
                    if df2 is None or getattr(df2, "empty", True):
                        break
                    try:
                        cur_last = pd.Timestamp(df2.index[-1])
                    except Exception:
                        break
                    # 다른 스레드가 이미 롤했으면 스킵
                    if cur_last != last_ix:
                        continue
                    try:
                        df2.at[last_ix, "Open"] = o
                        df2.at[last_ix, "High"] = h
                        df2.at[last_ix, "Low"] = l_
                        df2.at[last_ix, "Close"] = c
                        df2.at[last_ix, "Volume"] = v
                        bd[use_key] = df2
                        changed = True
                    except Exception:
                        pass
                    # 장외·야간 등 비정상 인덱스는 공식 REST 대상이 아니므로 콜백 생략
                    if is_krx_15m_slot_end(last_ix):
                        try:
                            from core.krx_market_halt_slots import (
                                probe_and_maybe_mark_halt_slot,
                            )

                            probe_and_maybe_mark_halt_slot(app, last_ix)
                        except Exception:
                            pass
                        pending_cbs.append((use_key, last_ix, o, h, l_, c, v))
                    try:
                        nxt = next_krx_15m_slot_end(last_ix)
                    except Exception:
                        nxt = None
                    if nxt is None:
                        break
                    if nxt > want:
                        nxt = want
                    if not is_krx_15m_slot_end(nxt):
                        break
                    if pd.Timestamp(nxt) <= last_ix:
                        break
                    try:
                        pc = float(df2.at[last_ix, "Close"])
                    except Exception:
                        pc = float(c)
                    slot_hhmm = ts_to_slot_hhmm(nxt)
                    dt_str = nxt.strftime("%Y-%m-%d %H:%M:%S")
                    new_row = {
                        "datetime": dt_str,
                        "time": slot_hhmm,
                        "Open": pc,
                        "High": pc,
                        "Low": pc,
                        "Close": pc,
                        "Volume": 0.0,
                        "cum_v_start": 0.0,
                    }
                    try:
                        nrd = pd.DataFrame([new_row])
                        nrd["datetime"] = pd.to_datetime(nrd["datetime"])
                        nrd.set_index("datetime", inplace=True)
                        df2 = pd.concat([df2, nrd]).tail(300)
                        bd[use_key] = df2
                        changed = True
                    except Exception:
                        break
            except Exception:
                break

        # 락 해제 후 콜백(DB upsert·eval 스폰이 락을 잡지 않도록)
        for cb_args in pending_cbs:
            try:
                _on_bar_closed_safe(app, *cb_args)
            except Exception:
                pass
        return changed
    except Exception:
        for cb_args in pending_cbs:
            try:
                _on_bar_closed_safe(app, *cb_args)
            except Exception:
                pass
        return changed


def _slot_roll_parallel_workers(app: Any) -> int:
    """슬롯 경계 롤 병렬 워커 수."""
    try:
        st = getattr(app, "settings", None) or {}
        v = int(float(st.get("realtime_15m_slot_roll_workers", 8)))
        return max(1, min(16, v))
    except Exception:
        return 8


def roll_needed_codes_parallel(app: Any, codes: List[str]) -> None:
    """롤이 필요한 종목만 병렬로 ``maybe_roll_15m_slot_on_clock`` 수행."""
    if not codes:
        return
    need: List[str] = []
    for nk in codes:
        try:
            if peek_15m_slot_roll_needed(app, nk):
                need.append(nk)
        except Exception:
            continue
    if not need:
        return
    workers = min(_slot_roll_parallel_workers(app), len(need))
    if workers <= 1 or len(need) == 1:
        for nk in need:
            try:
                maybe_roll_15m_slot_on_clock(app, nk)
            except Exception:
                pass
        return
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="Rt15mRoll"
        ) as ex:
            futs = [ex.submit(maybe_roll_15m_slot_on_clock, app, nk) for nk in need]
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception:
                    pass
    except Exception:
        for nk in need:
            try:
                maybe_roll_15m_slot_on_clock(app, nk)
            except Exception:
                pass


def sync_forming_15m_last_row_from_official_m1(app: Any, code: str) -> bool:
    """
    [옵션 A] M1 집계 경로 제거. 하위 호환용 스텁(항상 False).
    형성봉 OHLCV는 공식 15분 교체로만 갱신합니다.
    """
    return False


def _resolve_monitor_price(app: Any, nk: str, fallback: float) -> float:
    try:
        mi = getattr(app, "monitor_info", None) or {}
        for key, info in mi.items():
            try:
                if str(key).split("_")[0].zfill(6) == nk:
                    for fld in ("price", "last_price", "Close"):
                        v = info.get(fld)
                        if v is not None and float(v) > 0:
                            return float(v)
            except Exception:
                continue
    except Exception:
        pass
    return float(fallback or 0.0)


def _latest_bar_close(app: Any, nk: str) -> float:
    """
    형성 중(또는 마지막) 15분봉 Close 반환.

    WS 틱 가격이 0일 때 신호/UI 트리거용 폴백 가격원천.
    """
    try:
        bd = getattr(app, "bars_data", None)
        if not isinstance(bd, dict):
            return 0.0
        lk = getattr(app, "_bars_data_lock", None)

        def _read() -> float:
            _k, df = resolve_bars_data_frame(bd, nk, migrate_alias=True)
            if df is None or getattr(df, "empty", True):
                return 0.0
            try:
                return float(df["Close"].iloc[-1])
            except Exception:
                return 0.0

        if lk is not None:
            with lk:
                return _read()
        return _read()
    except Exception:
        return 0.0


def iter_slot_roll_watch_codes(app: Any) -> List[str]:
    """15분 슬롯 롤 감시 대상 종목(감시·스트림·보유)을 중복 없이 반환."""
    seen: Set[str] = set()
    out: List[str] = []

    def _add(raw: Any) -> None:
        try:
            nk = canonical_stock_code(str(raw).strip())
        except Exception:
            return
        if not nk or nk in seen:
            return
        seen.add(nk)
        out.append(nk)

    try:
        tc = getattr(app, "trading_controller", None)
        if tc is not None:
            for c in getattr(tc, "monitor_codes", None) or []:
                _add(c)
    except Exception:
        pass
    try:
        for c in getattr(app, "_active_stream_codes", None) or []:
            _add(c)
    except Exception:
        pass
    try:
        mi = getattr(app, "monitor_info", None) or {}
        for key in mi:
            try:
                _add(str(key).split("_")[0])
            except Exception:
                continue
    except Exception:
        pass
    try:
        vp = getattr(app, "virtual_positions", None) or {}
        for c in vp:
            _add(c)
    except Exception:
        pass
    return out


_slot_roll_tick_last: Dict[str, float] = {}
_slot_roll_tick_lock = threading.Lock()
_SLOT_ROLL_TICK_MIN_INTERVAL_SEC = 0.05


def try_roll_15m_slot_on_tick(app: Any, code: str) -> bool:
    """
    틱 수신 시 슬롯 롤이 필요하면 즉시 수행(종목별 스로틀).
    워커 큐 적체와 무관하게 15분 확정 콜백을 앞당깁니다.
    """
    nk = canonical_stock_code(str(code).strip())
    if not nk:
        return False
    if not peek_15m_slot_roll_needed(app, nk):
        return False
    try:
        now_m = time.monotonic()
        with _slot_roll_tick_lock:
            last = float(_slot_roll_tick_last.get(nk, 0.0))
            if now_m - last < _SLOT_ROLL_TICK_MIN_INTERVAL_SEC:
                return False
            _slot_roll_tick_last[nk] = now_m
    except Exception:
        pass
    try:
        return bool(maybe_roll_15m_slot_on_clock(app, nk))
    except Exception:
        return False


def peek_15m_slot_roll_needed(app: Any, code: str) -> bool:
    """슬롯 롤 필요 여부(읽기 전용)."""
    nk = canonical_stock_code(str(code).strip())
    if not nk:
        return False
    lk = getattr(app, "_bars_data_lock", None)
    bd = getattr(app, "bars_data", None)
    if lk is None or not isinstance(bd, dict):
        return False
    try:
        with lk:
            _k, df = resolve_bars_data_frame(bd, nk, migrate_alias=True)
            if df is None or getattr(df, "empty", True):
                return False
            last_ix = pd.Timestamp(df.index[-1])
            want = stream_slot_end_datetime(datetime.now())
            return bool(last_ix < want)
    except Exception:
        return False


_slot_sync_queue: "queue.SimpleQueue[Tuple[Any, str, float, int, bool]]" = (
    queue.SimpleQueue()
)
_slot_sync_pending: Set[str] = set()
_slot_sync_pending_lock = threading.Lock()
_slot_sync_thread: Optional[threading.Thread] = None
_slot_sync_thread_lock = threading.Lock()


def _run_official_sync_and_signal_eval_job(
    app: Any,
    nk: str,
    *,
    now_price_fallback: float = 0.0,
    volume_fallback: int = 0,
    skip_roll: bool = False,
) -> None:
    """슬롯 롤·신호 재평가(전용 워커 스레드). 봉 OHLCV는 공식 15분 교체가 권위."""
    try:
        if not skip_roll:
            maybe_roll_15m_slot_on_clock(app, nk)
        px = _resolve_monitor_price(app, nk, now_price_fallback)
        if px <= 0:
            px = _latest_bar_close(app, nk)
        if px <= 0:
            return
        disp = getattr(app, "tick_dispatch_service", None)
        if disp is not None:
            disp.on_tick_received(
                nk,
                float(px),
                int(volume_fallback),
                0.0,
                _official_sync_eval=True,
            )
            return
        eng = getattr(app, "signal_engine", None)
        if eng is not None:
            try:
                from core.krx_market_halt_slots import should_suppress_s1_eval_for_code

                suppressed, _halt_msg = should_suppress_s1_eval_for_code(app, nk)
            except Exception:
                suppressed = False
            if not suppressed:
                eng.on_tick_received(
                    nk,
                    px,
                    int(volume_fallback),
                    0.0,
                    defer_main_callbacks=True,
                    _official_sync_eval=True,
                )
    except Exception:
        pass


def _slot_sync_worker_loop() -> None:
    while True:
        try:
            app, nk, px_fb, vol_fb, skip_roll = _slot_sync_queue.get()
        except Exception:
            continue
        try:
            _run_official_sync_and_signal_eval_job(
                app,
                nk,
                now_price_fallback=float(px_fb),
                volume_fallback=int(vol_fb),
                skip_roll=bool(skip_roll),
            )
        except Exception:
            pass
        finally:
            try:
                with _slot_sync_pending_lock:
                    _slot_sync_pending.discard(nk)
            except Exception:
                pass


def _ensure_slot_sync_worker_started() -> None:
    global _slot_sync_thread
    with _slot_sync_thread_lock:
        if _slot_sync_thread is not None and _slot_sync_thread.is_alive():
            return
        _slot_sync_thread = threading.Thread(
            target=_slot_sync_worker_loop,
            name="Rt15mOfficialSyncWorker",
            daemon=True,
        )
        _slot_sync_thread.start()


def enqueue_official_sync_and_signal_eval(
    app: Any,
    code: str,
    *,
    now_price_fallback: float = 0.0,
    volume_fallback: int = 0,
    skip_roll: bool = False,
) -> None:
    nk = canonical_stock_code(str(code).strip())
    if not nk:
        return
    # 워커 큐 적체·pending 중복 차단과 무관하게 슬롯 롤은 즉시 수행
    if not skip_roll:
        try:
            if peek_15m_slot_roll_needed(app, nk):
                maybe_roll_15m_slot_on_clock(app, nk)
        except Exception:
            pass
    try:
        with _slot_sync_pending_lock:
            if nk in _slot_sync_pending:
                return
            _slot_sync_pending.add(nk)
        _slot_sync_queue.put(
            (
                app,
                nk,
                float(now_price_fallback),
                int(volume_fallback),
                bool(skip_roll),
            )
        )
        _ensure_slot_sync_worker_started()
    except Exception:
        try:
            with _slot_sync_pending_lock:
                _slot_sync_pending.discard(nk)
        except Exception:
            pass


def schedule_official_sync_and_signal_eval(
    app: Any,
    code: str,
    *,
    now_price_fallback: float = 0.0,
    volume_fallback: int = 0,
    skip_roll: bool = False,
) -> None:
    """
    슬롯 롤 → 신호 재평가(전용 워커 큐). 완성봉 권위는 공식 15분 교체.
    """
    enqueue_official_sync_and_signal_eval(
        app,
        code,
        now_price_fallback=now_price_fallback,
        volume_fallback=volume_fallback,
        skip_roll=skip_roll,
    )


class Rt15mSlotBoundaryWatcher:
    """
    감시 중 15분 슬롯 경계를 벽시계에 맞춰 감지합니다.

    - 경계 근처: 짧은 폴링(기본 0.1s)
    - 경계에서 멀면: 다음 경계 직전까지 sleep
    - 롤은 워커 큐에 맡기지 않고 **즉시 병렬** 수행한 뒤,
      공식 M1 동기화·형성봉 평가는 조용 구간 뒤로 미룹니다.
    """

    def __init__(self, app: Any, log: Optional[Callable[[str], None]] = None) -> None:
        self._app = app
        self._log = log
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()

    def start(self) -> None:
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="Rt15mSlotBoundaryWatcher", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            th = self._thread
            if th is not None and th.is_alive():
                th.join(timeout=2.0)
        except Exception:
            pass

    def _emit(self, msg: str) -> None:
        if self._log is None:
            return
        try:
            self._log(msg)
        except Exception:
            pass

    def _idle_watch_sec(self) -> float:
        """경계에서 멀 때의 최대 sleep(초)."""
        try:
            st = getattr(self._app, "settings", None) or {}
            v = float(st.get("realtime_15m_slot_watch_sec", 1.0))
            return max(0.5, min(5.0, v))
        except Exception:
            return 1.0

    def _near_poll_sec(self) -> float:
        """경계 근처 폴링 간격(초)."""
        try:
            st = getattr(self._app, "settings", None) or {}
            v = float(st.get("realtime_15m_slot_near_poll_sec", 0.1))
            return max(0.05, min(0.5, v))
        except Exception:
            return 0.1

    def _near_window_sec(self) -> float:
        """경계 직전 고주기 폴링 창(초)."""
        try:
            st = getattr(self._app, "settings", None) or {}
            v = float(st.get("realtime_15m_slot_near_window_sec", 2.0))
            return max(0.5, min(10.0, v))
        except Exception:
            return 2.0

    def _next_sleep_sec(self) -> float:
        """
        다음 wake까지 sleep.

        경계 ``near_window`` 이내면 ``near_poll``, 아니면 경계−창까지
        (단 idle_watch 상한) 만큼 쉽니다.
        """
        try:
            sec_to = float(seconds_until_next_krx_15m_boundary())
        except Exception:
            sec_to = 1.0
        near_w = self._near_window_sec()
        near_p = self._near_poll_sec()
        idle = self._idle_watch_sec()
        if sec_to <= near_w:
            # 경계까지 남은 시간보다 길지 않게(과슬립 방지)
            return max(0.05, min(near_p, max(0.05, sec_to)))
        sleep_to_near = sec_to - near_w
        return max(0.05, min(idle, sleep_to_near))

    def _roll_and_schedule_sync(self) -> None:
        """롤 필요 종목 병렬 확정. UI/신호 위생 틱은 조용 구간 뒤로 미룹니다."""
        codes = iter_slot_roll_watch_codes(self._app)
        if not codes:
            return
        need: List[str] = []
        for nk in codes:
            try:
                if peek_15m_slot_roll_needed(self._app, nk):
                    need.append(nk)
            except Exception:
                continue
        if not need:
            return
        roll_needed_codes_parallel(self._app, need)

        def _deferred_official_hygiene() -> None:
            for nk in need:
                try:
                    schedule_official_sync_and_signal_eval(
                        self._app, nk, skip_roll=True
                    )
                except Exception as e:
                    try:
                        self._emit(f"⚠️ [RT15mWatch] {nk!r}: {e!r}")
                    except Exception:
                        pass
            try:
                from core.krx_market_halt_slots import (
                    maybe_schedule_halt_slot_official_retries,
                )

                maybe_schedule_halt_slot_official_retries(self._app)
            except Exception:
                pass

        # 로컬 확정 평가·주문이 공식 REST/동기화와 CPU·락을 다투지 않도록 잠시 후순위
        try:
            from core.closed_15m_fast_path import (
                mark_rt15_official_quiet,
                schedule_after_rt15_official_quiet,
            )

            mark_rt15_official_quiet(self._app)
            schedule_after_rt15_official_quiet(
                self._app,
                _deferred_official_hygiene,
                name="Rt15mDeferOfficialSync",
            )
        except Exception:
            _deferred_official_hygiene()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                tc = getattr(self._app, "trading_controller", None)
                if tc is None or not getattr(tc, "is_monitoring", False):
                    if self._stop.wait(self._idle_watch_sec()):
                        break
                    continue
                # 먼저 밀린 롤을 처리(경계 wake 직후·늦은 wake 모두 커버)
                self._roll_and_schedule_sync()
            except Exception as e:
                try:
                    self._emit(f"⚠️ [RT15mWatch] 루프 예외: {e!r}")
                except Exception:
                    pass
            try:
                wait_sec = self._next_sleep_sec()
            except Exception:
                wait_sec = 1.0
            if self._stop.wait(wait_sec):
                break
