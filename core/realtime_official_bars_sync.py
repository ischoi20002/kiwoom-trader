# -*- coding: utf-8 -*-
"""
실시간 **신호용** 15분봉: 틱으로 OHLCV를 쌓지 않고, 공식 1분(ka10080)만으로
형성 중 마지막 15분 행을 갱신합니다. 15분 슬롯 전환은 벽시계 기준으로
``bars_data`` 행을 추가하고 확정 콜백(전용 DB·공식 15분 덮어쓰기 스케줄)을 호출합니다.

- 감시 트리·체결가 등 UI/주문용 틱 가격은 ``SignalEngine.on_tick_received`` 가 별도 처리합니다.
- 본 모듈은 ``_signal_skip_bar_update=True`` 로 신호 재평가만 트리거할 때 사용합니다.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

import pandas as pd

from core.rt_closed_15m_official_replace import ts_to_slot_hhmm
from utils.bars_data_keys import resolve_bars_data_frame
from utils.krx_bars import (
    is_krx_15m_slot_end,
    krx_stream_slot_end_datetime,
    m1_bar_open_label_for_just_completed_minute,
    m1_window_start_for_15m_end,
    next_krx_15m_slot_end,
)
from utils.normalization import canonical_stock_code


def stream_slot_end_datetime(now: datetime) -> pd.Timestamp:
    """KRX 장중 15분봉 종가 라벨(야간·장외 슬롯 생성 없음)."""
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
    try:
        with lk:
            _k, df = resolve_bars_data_frame(bd, nk, migrate_alias=True)
            use_key = _k or nk
            if df is None or getattr(df, "empty", True):
                return False
            want = stream_slot_end_datetime(datetime.now())
            guard = 0
            while guard < 64:
                guard += 1
                try:
                    last_ix = pd.Timestamp(df.index[-1])
                except Exception:
                    break
                if last_ix >= want:
                    break
                try:
                    o = float(df.at[last_ix, "Open"])
                    h = float(df.at[last_ix, "High"])
                    l_ = float(df.at[last_ix, "Low"])
                    c = float(df.at[last_ix, "Close"])
                    v = float(df.at[last_ix, "Volume"])
                except Exception:
                    o = h = l_ = c = v = 0.0
                # 장외·야간 등 비정상 인덱스는 공식 REST 대상이 아니므로 콜백 생략
                if is_krx_15m_slot_end(last_ix):
                    try:
                        from core.krx_market_halt_slots import (
                            probe_and_maybe_mark_halt_slot,
                        )

                        probe_and_maybe_mark_halt_slot(app, last_ix)
                    except Exception:
                        pass
                    _on_bar_closed_safe(app, use_key, last_ix, o, h, l_, c, v)
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
                    pc = float(df.at[last_ix, "Close"])
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
                    df = pd.concat([df, nrd]).tail(300)
                    bd[use_key] = df
                    changed = True
                except Exception:
                    break
        return changed
    except Exception:
        return changed


def sync_forming_15m_last_row_from_official_m1(app: Any, code: str) -> bool:
    """
    ``RtTickM1Store`` 에 쌓인 **완료 공식 1분**만으로 현재 슬롯의 마지막 15분 행 OHLCV를 재작성합니다.
    (진행 중 1분 버킷은 사용하지 않음)

    Returns:
        마지막 행이 갱신되었으면 True.
    """
    nk = canonical_stock_code(str(code).strip())
    if not nk:
        return False
    m1s = getattr(app, "rt_tick_m1_store", None)
    if m1s is None:
        return False
    lk = getattr(app, "_bars_data_lock", None)
    bd = getattr(app, "bars_data", None)
    if lk is None or not isinstance(bd, dict):
        return False
    try:
        with lk:
            _k, df = resolve_bars_data_frame(bd, nk, migrate_alias=True)
            use_key = _k or nk
            if df is None or getattr(df, "empty", True):
                return False
            slot_end = stream_slot_end_datetime(datetime.now())
            try:
                last_ix = pd.Timestamp(df.index[-1])
            except Exception:
                return False
            if last_ix != slot_end:
                return False
            t0 = m1_window_start_for_15m_end(slot_end)
            try:
                wall = datetime.now()
                closed_open = m1_bar_open_label_for_just_completed_minute(wall)
                t_hi = min(slot_end, closed_open + pd.Timedelta(minutes=1))
            except Exception:
                wall2 = datetime.now()
                t_hi = min(slot_end, pd.Timestamp(wall2).floor("min"))
            if t_hi < t0:
                return False
            try:
                m1df = m1s.dataframe_for_window(
                    nk, t0, t_hi, include_open_bucket=False
                )
            except TypeError:
                m1df = m1s.dataframe_for_window(nk, t0, t_hi)
            if m1df is None or getattr(m1df, "empty", True):
                return False
            o = float(m1df["Open"].iloc[0])
            h = float(m1df["High"].max())
            l_ = float(m1df["Low"].min())
            c = float(m1df["Close"].iloc[-1])
            v = float(m1df["Volume"].sum())
            li = df.index[-1]
            df.at[li, "Open"] = o
            df.at[li, "High"] = h
            df.at[li, "Low"] = l_
            df.at[li, "Close"] = c
            df.at[li, "Volume"] = v
            if "time" in df.columns:
                try:
                    df.at[li, "time"] = ts_to_slot_hhmm(li)
                except Exception:
                    pass
            bd[use_key] = df
            return True
    except Exception:
        return False


def _resolve_monitor_price(app: Any, nk: str, fallback: float) -> float:
    try:
        mi = getattr(app, "monitor_info", None) or {}
        for key, info in mi.items():
            try:
                if str(key).split("_")[0].zfill(6) == nk:
                    for fld in ("last_price", "price", "Close"):
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
    형성 중(또는 마지막) 15분봉 Close 반환. 공식 1분(ka10080) 동기화가 반영된 값이다.

    [버그픽스] WS 0B 틱이 없거나 끊긴 동안 ``monitor_info`` 의 틱 가격이 0 이면
    ``schedule_official_sync_and_signal_eval`` 의 ``on_tick_received`` 트리거가
    ``px>0`` 조건에 막혀 신호 평가가 통째로 스킵된다(M1 폴러가 공식 봉을 채워도 평가 안 됨).
    틱 가격이 없을 때 공식 봉 종가로 평가를 구동하기 위한 폴백 가격원이다.
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


def schedule_official_sync_and_signal_eval(
    app: Any,
    code: str,
    *,
    now_price_fallback: float = 0.0,
    volume_fallback: int = 0,
) -> None:
    """
    Tk 메인 스레드에서: 슬롯 롤 → 1분 집계 반영 → ``_signal_skip_bar_update`` 신호 재평가.
    """
    nk = canonical_stock_code(str(code).strip())
    if not nk:
        return

    def _job() -> None:
        try:
            maybe_roll_15m_slot_on_clock(app, nk)
            sync_forming_15m_last_row_from_official_m1(app, nk)
            px = _resolve_monitor_price(app, nk, now_price_fallback)
            # [버그픽스] 틱 가격이 0(WS 끊김·차단)이면 공식 M1 동기화된 마지막 봉 Close 로
            # 폴백해 평가를 구동한다(슬롯 워처·폴러 양쪽 경로 모두 보호).
            if px <= 0:
                px = _latest_bar_close(app, nk)
            eng = getattr(app, "signal_engine", None)
            if eng is not None and px > 0:
                try:
                    from core.krx_market_halt_slots import (
                        should_suppress_s1_eval_for_code,
                    )

                    suppressed, _halt_msg = should_suppress_s1_eval_for_code(
                        app, nk
                    )
                except Exception:
                    suppressed = False
                if not suppressed:
                    eng.on_tick_received(
                        nk,
                        px,
                        int(volume_fallback),
                        0.0,
                        defer_main_callbacks=False,
                    )
        except Exception:
            pass

    try:
        root = getattr(app, "root", None)
        if root is not None and hasattr(root, "after"):
            root.after(0, _job)
        else:
            _job()
    except Exception:
        try:
            _job()
        except Exception:
            pass


class Rt15mSlotBoundaryWatcher:
    """
    감시 중 5초마다 슬롯 전진을 확인합니다. 전진 시 공식 동기화·신호 재평가를 예약합니다.
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

    def _loop(self) -> None:
        while not self._stop.wait(5.0):
            try:
                tc = getattr(self._app, "trading_controller", None)
                if tc is None or not getattr(tc, "is_monitoring", False):
                    continue
                codes = list(getattr(self._app, "_active_stream_codes", None) or [])
                for c in codes:
                    try:
                        nk = canonical_stock_code(str(c).strip())
                        if not nk:
                            continue
                        if maybe_roll_15m_slot_on_clock(self._app, nk):
                            schedule_official_sync_and_signal_eval(self._app, nk)
                    except Exception as e:
                        try:
                            self._emit(f"⚠️ [RT15mWatch] {c!r}: {e!r}")
                        except Exception:
                            pass
                try:
                    from core.krx_market_halt_slots import (
                        maybe_schedule_halt_slot_official_retries,
                    )

                    maybe_schedule_halt_slot_official_retries(self._app)
                except Exception:
                    pass
            except Exception as e:
                try:
                    self._emit(f"⚠️ [RT15mWatch] 루프 예외: {e!r}")
                except Exception:
                    pass
