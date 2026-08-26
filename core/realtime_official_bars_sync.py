# -*- coding: utf-8 -*-
"""
실시간 **신호용** 15분봉: 틱 1분 선적재 + 공식 soft replace 후 S1·S2·청산 평가.

- 완성봉 OHLCV: ``compose_local_15m_ohlcv`` (공식 우선, 결측은 틱).
- 말분은 ``realtime_15m_last_minute_from_ticks`` 로 틱 우선 가능.
- 슬롯 앞구간(말분 제외)에 공식 1분이 없으면 틱만으로 완성15를 만들지 않음
  (장중 재시작 직후 틱전용봉 방지). 공식 catchup 후 재시도.
- 말분 틱이 중간부터면 부분 틱으로 평가하지 않고 공식 말분 1분을 기다림.
- head REST는 soft catchup(선택)이며 평가 게이트가 아님.
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pandas as pd

from core.rt_closed_15m_official_replace import ts_to_slot_hhmm
from core.rt_ui_responsiveness import clamp_slot_roll_workers, yield_gil_for_ui
from utils.bars_data_keys import resolve_bars_data_frame
from utils.krx_bars import (
    is_krx_15m_slot_end,
    is_krx_last_15m_slot,
    iter_krx_15m_slot_ends_for_day,
    krx_stream_slot_end_datetime,
    m1_bar_open_label_for_just_completed_minute,
    m1_closing_auction_ts_for_15m_end,
    m1_continuous_end_exclusive_for_15m_end,
    m1_window_start_for_15m_end,
    next_krx_15m_slot_end,
    seconds_until_next_krx_15m_boundary,
)
from utils.normalization import canonical_stock_code


def stream_slot_end_datetime(now: datetime) -> pd.Timestamp:
    """KRX 장중 15분봉 **좌측 라벨**(야간·장외 슬롯 생성 없음)."""
    return krx_stream_slot_end_datetime(now)


def _last_minute_from_ticks_enabled(app: Any) -> bool:
    """마지막 1분을 틱 버킷으로 보정할지(기본 ON — 공식14+틱1 합성)."""
    try:
        st = getattr(app, "settings", None) or {}
        return bool(st.get("realtime_15m_last_minute_from_ticks", True))
    except Exception:
        return True


def _tick_fill_on_api_miss_enabled(app: Any) -> bool:
    """
    공식 1분이 없는 분을 틱 seal로 채울지(기본 ON).

    ON이면 결측 분을 틱으로 채우되, 슬롯 앞구간에 공식 1분이
    하나도 없으면 ``_require_official_head_enabled`` 가 합성을 보류한다.
    """
    try:
        st = getattr(app, "settings", None) or {}
        return bool(st.get("realtime_15m_compose_tick_fill_on_api_miss", True))
    except Exception:
        return True


def _require_official_head_enabled(app: Any) -> bool:
    """
    완성 15분 합성 시 슬롯 앞구간(말분 제외)에 공식 1분이 필요한지(기본 ON).

    장중 재시작 직후 M1 스토어에 틱만 있으면 워밍업 1m 합산봉을
    틱전용 합성으로 덮지 않고, 공식 catchup 까지 평가를 미룬다.
    """
    try:
        st = getattr(app, "settings", None) or {}
        return bool(st.get("realtime_15m_compose_require_official_head", True))
    except Exception:
        return True


def _wait_official_last_if_tick_incomplete_enabled(app: Any) -> bool:
    """말분 틱이 중간부터면 공식 말분 1분을 기다릴지(기본 ON)."""
    try:
        st = getattr(app, "settings", None) or {}
        return bool(
            st.get("realtime_15m_wait_official_last_if_tick_incomplete", True)
        )
    except Exception:
        return True


def mark_tick_stream_ready(app: Any, when: Optional[datetime] = None) -> None:
    """웹소켓 틱 수신이 시작된 시각을 한 번만 기록합니다."""
    try:
        if getattr(app, "rt_tick_stream_ready_at", None) is not None:
            return
        app.rt_tick_stream_ready_at = when if when is not None else datetime.now()
    except Exception:
        pass


def clear_tick_stream_session(app: Any) -> None:
    """감시 중단 시 말분 틱 완전성 판별용 세션 상태를 지웁니다."""
    try:
        app.rt_tick_stream_ready_at = None
    except Exception:
        pass
    try:
        app.rt_monitor_inprogress_slot = None
    except Exception:
        pass
    try:
        m1s = getattr(app, "rt_tick_m1_store", None)
        if m1s is not None and hasattr(m1s, "reset_tick_session"):
            m1s.reset_tick_session()
    except Exception:
        pass


def remember_monitor_inprogress_slot(app: Any, when: Optional[datetime] = None) -> None:
    """감시 시작(워밍업) 시점의 형성 중 15분 슬롯 라벨을 기록합니다."""
    try:
        wall = when if when is not None else datetime.now()
        app.rt_monitor_inprogress_slot = stream_slot_end_datetime(wall)
    except Exception:
        try:
            app.rt_monitor_inprogress_slot = None
        except Exception:
            pass


def tick_stream_covers_minute_open(app: Any, last_open: pd.Timestamp) -> bool:
    """
    틱 스트림이 해당 분 시작 시점부터 살아 있었는지(전역).

    워밍업 중이거나 재시작 직후면 False.
    """
    try:
        ready = getattr(app, "rt_tick_stream_ready_at", None)
        if ready is None:
            return False
        return pd.Timestamp(ready) <= pd.Timestamp(last_open).floor("min")
    except Exception:
        return False


def last_minute_ticks_complete_for_eval(
    app: Any, code: str, last_open: Any
) -> bool:
    """
    말분 틱으로 평가해도 되는가.

    말분 시작 전에 틱을 받고 있었으면 True.
    재시작으로 말분 중간에 붙었으면 False → 공식 1분을 기다린다.
    """
    try:
        if not _wait_official_last_if_tick_incomplete_enabled(app):
            return True
        lab = pd.Timestamp(last_open).floor("min")
        m1s = getattr(app, "rt_tick_m1_store", None)
        first = None
        if m1s is not None and hasattr(m1s, "first_tick_at"):
            try:
                first = m1s.first_tick_at(code)
            except Exception:
                first = None
        if isinstance(first, datetime):
            # 말분 시작 전에 이 종목 틱이 있었으면 완전
            if pd.Timestamp(first) < lab:
                return True
        # 말분에야 첫 체결이 온 종목(유동성 낮음)은 전역 스트림 시작 시각으로 판별
        return tick_stream_covers_minute_open(app, lab)
    except Exception:
        return False


def has_official_minute(m1s: Any, nk: str, open_lab: pd.Timestamp) -> bool:
    """공식 completed 에 해당 1분이 있는지."""
    try:
        ol = pd.Timestamp(open_lab).floor("min")
        df = m1s.dataframe_for_window(nk, ol, ol, include_open_bucket=False)
        return df is not None and not getattr(df, "empty", True)
    except Exception:
        return False


def slot_has_official_head_m1(
    m1s: Any,
    nk: str,
    t0: pd.Timestamp,
    last_open: pd.Timestamp,
) -> bool:
    """
    슬롯 앞구간 ``[t0, last_open)`` 에 공식 completed 1분이 하나라도 있는지.

    말분은 틱 우선이므로 검사에서 제외한다. 앞구간이 없는 1분 슬롯은 True.
    """
    try:
        head_hi = pd.Timestamp(last_open).floor("min") - pd.Timedelta(minutes=1)
        t0s = pd.Timestamp(t0).floor("min")
        if head_hi < t0s:
            return True
        head = m1s.dataframe_for_window(
            nk, t0s, head_hi, include_open_bucket=False
        )
        return head is not None and not getattr(head, "empty", True)
    except Exception:
        return False


def _minute_ohlcv_official_or_tick(
    m1s: Any,
    nk: str,
    open_lab: pd.Timestamp,
    *,
    prefer_tick: bool = False,
    allow_tick_fill: bool = True,
) -> Optional[Tuple[float, float, float, float, float]]:
    """
    단일 1분 OHLCV.

    - ``prefer_tick``: 틱 우선(말분·15:30).
    - 그 외: 공식 completed 우선, 없으면 틱(``allow_tick_fill``).
    """
    tick_bar: Optional[Tuple[float, float, float, float, float]] = None
    if prefer_tick or allow_tick_fill:
        try:
            tick_bar = m1s.tick_minute_ohlcv(nk, open_lab)
        except Exception:
            tick_bar = None

    if prefer_tick and tick_bar is not None:
        to, th, tl, tc, tv = tick_bar
        if float(tc) > 0:
            return (
                float(to),
                float(th),
                float(tl),
                float(tc),
                max(0.0, float(tv)),
            )

    try:
        off = m1s.dataframe_for_window(
            nk, open_lab, open_lab, include_open_bucket=False
        )
    except TypeError:
        try:
            off = m1s.dataframe_for_window(nk, open_lab, open_lab)
        except Exception:
            off = None
    except Exception:
        off = None
    if off is not None and not getattr(off, "empty", True):
        try:
            o = float(off["Open"].iloc[-1])
            h = float(off["High"].iloc[-1])
            l_ = float(off["Low"].iloc[-1])
            c = float(off["Close"].iloc[-1])
            v = max(0.0, float(off["Volume"].iloc[-1]))
            if c > 0:
                return o, h, l_, c, v
        except Exception:
            pass

    if allow_tick_fill and tick_bar is not None:
        to, th, tl, tc, tv = tick_bar
        if float(tc) > 0:
            return (
                float(to),
                float(th),
                float(tl),
                float(tc),
                max(0.0, float(tv)),
            )
    return None


def compose_local_15m_ohlcv(
    app: Any, code: str, slot_end: Any
) -> Optional[Tuple[float, float, float, float, float]]:
    """
    로컬 15분 OHLCV 합성(좌측 라벨 창).

    - 일반: ``[T, T+15)`` 의 완료 1분
    - ``15:15``: ``[15:15, 15:20)``
    - ``15:30``: 단일가 1분만
    - ``realtime_15m_last_minute_from_ticks`` ON 이면 말분은 틱 우선
    - ``realtime_15m_compose_tick_fill_on_api_miss`` ON 이면 공식 결측 분을 틱으로 채움
    - ``realtime_15m_compose_require_official_head`` ON 이면 앞구간에 공식 1분이
      없을 때 None (틱만으로 완성15 금지). 폴러 catchup 후 재시도.
    - 말분 틱이 중간부터(장중 재시작)이면 공식 말분 1분이 올 때까지 None.
      도착 후에는 공식 1분만으로 합성한다.
    """
    try:
        nk = canonical_stock_code(str(code).strip())
        if not nk:
            return None
        slot = pd.Timestamp(slot_end).floor("min")
        m1s = getattr(app, "rt_tick_m1_store", None)
        if m1s is None:
            return None

        use_tick_tail = _last_minute_from_ticks_enabled(app)
        allow_fill = _tick_fill_on_api_miss_enabled(app)

        # 15:30 단일가 — 틱 우선, 없으면 공식.
        # 말분 틱이 중간부터면 공식 1분만 사용(없으면 None → 대기).
        if is_krx_last_15m_slot(slot):
            auc = m1_closing_auction_ts_for_15m_end(slot)
            if auc is None:
                return None
            incomplete_tail = not last_minute_ticks_complete_for_eval(app, nk, auc)
            if incomplete_tail:
                bar = _minute_ohlcv_official_or_tick(
                    m1s,
                    nk,
                    auc,
                    prefer_tick=False,
                    allow_tick_fill=False,
                )
                return bar
            bar = _minute_ohlcv_official_or_tick(
                m1s,
                nk,
                auc,
                prefer_tick=bool(use_tick_tail),
                allow_tick_fill=bool(use_tick_tail or allow_fill),
            )
            return bar

        t0 = m1_window_start_for_15m_end(slot)
        t_end_excl = m1_continuous_end_exclusive_for_15m_end(slot)
        last_open = t_end_excl - pd.Timedelta(minutes=1)
        if last_open < t0:
            return None

        # 말분 틱 불완전(재시작 등): 공식 말분 1분이 올 때까지 합성 거부, 오면 공식 1분만.
        incomplete_tail = not last_minute_ticks_complete_for_eval(
            app, nk, last_open
        )
        if incomplete_tail:
            use_tick_tail = False
            allow_fill = False
            try:
                if not has_official_minute(m1s, nk, last_open):
                    return None
            except Exception:
                return None

        # 장중 재시작: 앞구간 공식 1분이 없으면 틱전용 합성 거부
        try:
            if _require_official_head_enabled(app) and not slot_has_official_head_m1(
                m1s, nk, t0, last_open
            ):
                return None
        except Exception:
            return None

        # API miss 틱 채움: 슬롯 전 분을 공식우선(+틱결측)으로 순회
        if allow_fill:
            opens: list[float] = []
            highs: list[float] = []
            lows: list[float] = []
            closes: list[float] = []
            vols: list[float] = []
            cur = pd.Timestamp(t0).floor("min")
            end_last = pd.Timestamp(last_open).floor("min")
            while cur <= end_last:
                is_tail = cur == end_last
                bar = _minute_ohlcv_official_or_tick(
                    m1s,
                    nk,
                    cur,
                    prefer_tick=bool(use_tick_tail and is_tail),
                    allow_tick_fill=True,
                )
                if bar is not None:
                    o, h, l_, c, v = bar
                    opens.append(float(o))
                    highs.append(float(h))
                    lows.append(float(l_))
                    closes.append(float(c))
                    vols.append(max(0.0, float(v)))
                cur = cur + pd.Timedelta(minutes=1)
            if not closes:
                return None
            o0 = float(opens[0])
            h0 = float(max(highs)) if highs else o0
            l0 = (
                float(min(x for x in lows if x > 0))
                if any(x > 0 for x in lows)
                else o0
            )
            c0 = float(closes[-1])
            v0 = float(sum(max(0.0, x) for x in vols))
            if c0 <= 0:
                return None
            return o0, h0, l0, c0, v0

        # fill OFF: 기존 동작 — head는 공식만, 말분만 틱 우선
        tick_bar = None
        if use_tick_tail:
            try:
                tick_bar = m1s.tick_minute_ohlcv(nk, last_open)
            except Exception:
                tick_bar = None

        head_hi = last_open - pd.Timedelta(minutes=1)
        try:
            if head_hi >= t0:
                head = m1s.dataframe_for_window(
                    nk, t0, head_hi, include_open_bucket=False
                )
            else:
                head = None
        except Exception:
            head = None

        opens2: list[float] = []
        highs2: list[float] = []
        lows2: list[float] = []
        closes2: list[float] = []
        vols2: list[float] = []
        if head is not None and not getattr(head, "empty", True):
            try:
                opens2.extend([float(x) for x in head["Open"].tolist()])
                highs2.extend([float(x) for x in head["High"].tolist()])
                lows2.extend([float(x) for x in head["Low"].tolist()])
                closes2.extend([float(x) for x in head["Close"].tolist()])
                vols2.extend([float(x) for x in head["Volume"].tolist()])
            except Exception:
                pass

        if tick_bar is not None:
            to, th, tl, tc, tv = tick_bar
            opens2.append(float(to))
            highs2.append(float(th))
            lows2.append(float(tl))
            closes2.append(float(tc))
            vols2.append(max(0.0, float(tv)))
        else:
            try:
                tail = m1s.dataframe_for_window(
                    nk, last_open, last_open, include_open_bucket=False
                )
                if tail is not None and not getattr(tail, "empty", True):
                    opens2.append(float(tail["Open"].iloc[-1]))
                    highs2.append(float(tail["High"].iloc[-1]))
                    lows2.append(float(tail["Low"].iloc[-1]))
                    closes2.append(float(tail["Close"].iloc[-1]))
                    vols2.append(max(0.0, float(tail["Volume"].iloc[-1])))
            except Exception:
                pass

        if not closes2:
            try:
                full = m1s.dataframe_for_window(
                    nk, t0, last_open, include_open_bucket=False
                )
                if full is None or getattr(full, "empty", True):
                    return None
                o = float(full["Open"].iloc[0])
                h = float(full["High"].max())
                l_ = float(full["Low"].min())
                c = float(full["Close"].iloc[-1])
                v = float(full["Volume"].sum())
                if c <= 0:
                    return None
                return o, h, l_, c, max(0.0, v)
            except Exception:
                return None

        o = float(opens2[0])
        h = float(max(highs2)) if highs2 else o
        l_ = float(min(x for x in lows2 if x > 0)) if any(x > 0 for x in lows2) else o
        c = float(closes2[-1])
        v = float(sum(max(0.0, x) for x in vols2))
        if c <= 0:
            return None
        return o, h, l_, c, v
    except Exception:
        return None


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

            # 락 밖: 공식 1분 합성(가능하면)
            o, h, l_, c, v = raw_o, raw_h, raw_l, raw_c, raw_v
            try:
                composed = compose_local_15m_ohlcv(app, use_key, last_ix)
                if composed is not None:
                    o, h, l_, c, v = composed
            except Exception:
                pass

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
    """슬롯 경계 롤 병렬 워커 수. UI GIL 기아 방지를 위해 상한 2."""
    try:
        st = getattr(app, "settings", None) or {}
        return clamp_slot_roll_workers(st.get("realtime_15m_slot_roll_workers", 2))
    except Exception:
        return clamp_slot_roll_workers(2)


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
            yield_gil_for_ui()
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
                yield_gil_for_ui()
    except Exception:
        for nk in need:
            try:
                maybe_roll_15m_slot_on_clock(app, nk)
            except Exception:
                pass
            yield_gil_for_ui()


def sync_forming_15m_last_row_from_official_m1(app: Any, code: str) -> bool:
    """
    현재 슬롯의 마지막 15분 행 OHLCV를 공식 1분 집계로 재작성합니다.

    ``realtime_15m_last_minute_from_ticks`` ON 이면 마지막 1분은 틱 버킷 우선.
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

            composed = compose_local_15m_ohlcv(app, use_key, slot_end)
            if composed is not None:
                o, h, l_, c, v = composed
            else:
                t0 = m1_window_start_for_15m_end(slot_end)
                try:
                    wall = datetime.now()
                    closed_open = m1_bar_open_label_for_just_completed_minute(wall)
                    t_hi = min(
                        m1_continuous_end_exclusive_for_15m_end(slot_end)
                        - pd.Timedelta(minutes=1),
                        closed_open,
                    )
                except Exception:
                    wall2 = datetime.now()
                    t_hi = min(
                        m1_continuous_end_exclusive_for_15m_end(slot_end)
                        - pd.Timedelta(minutes=1),
                        pd.Timestamp(wall2).floor("min"),
                    )
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


def apply_composed_closed_15m_from_m1(
    app: Any, code: str, slot_label: Any
) -> Optional[Tuple[float, float, float, float, float]]:
    """
    닫힌 15분 슬롯 행에 공식 1분 합성 OHLCV를 반영하고 (o,h,l,c,v)를 반환합니다.
    """
    nk = canonical_stock_code(str(code).strip())
    if not nk:
        return None
    try:
        slot = pd.Timestamp(slot_label).floor("min")
    except Exception:
        return None
    composed = compose_local_15m_ohlcv(app, nk, slot)
    if composed is None:
        return None
    o, h, l_, c, v = composed
    lk = getattr(app, "_bars_data_lock", None)
    bd = getattr(app, "bars_data", None)
    if lk is None or not isinstance(bd, dict):
        return composed
    try:
        with lk:
            _k, df = resolve_bars_data_frame(bd, nk, migrate_alias=True)
            use_key = _k or nk
            if df is None or getattr(df, "empty", True):
                return composed
            if slot not in df.index:
                # 롤이 아직이면 호출 측에서 roll 후 재시도
                return composed
            df.at[slot, "Open"] = o
            df.at[slot, "High"] = h
            df.at[slot, "Low"] = l_
            df.at[slot, "Close"] = c
            df.at[slot, "Volume"] = v
            if "time" in df.columns:
                try:
                    df.at[slot, "time"] = ts_to_slot_hhmm(slot)
                except Exception:
                    pass
            bd[use_key] = df
        return composed
    except Exception:
        return composed


def _looks_like_stub_15m_row(row: Any) -> bool:
    """롤 직후 스텁(거래량 0·OHLC 동일)으로 보이면 True."""
    try:
        v = float(row.get("Volume", row.get("volume", 0)) or 0)
        if v <= 0:
            return True
        o = float(row["Open"])
        h = float(row["High"])
        l_ = float(row["Low"])
        c = float(row["Close"])
        if (
            abs(o - h) < 0.5
            and abs(h - l_) < 0.5
            and abs(l_ - c) < 0.5
            and v < 1.0
        ):
            return True
    except Exception:
        return False
    return False


def fill_missing_closed_15m_from_official_m1(
    app: Any,
    code: str,
    *,
    max_bars: int = 4,
    as_of: Optional[datetime] = None,
) -> Dict[str, Any]:
    """
    이미 끝난 최근 15분 슬롯 중 **누락·스텁**을 공식 1분 합성으로 채웁니다.

    공식 ka10080(15) 교체의 보조 경로입니다. 형성 중 슬롯은 건드리지 않습니다.
    """
    report: Dict[str, Any] = {
        "code": "",
        "checked": 0,
        "filled_missing": [],
        "filled_stub": [],
        "skipped": [],
        "failed": [],
    }
    try:
        nk = canonical_stock_code(str(code).strip())
    except Exception:
        nk = ""
    report["code"] = nk or str(code)
    if not nk:
        return report
    try:
        st = getattr(app, "settings", None) or {}
        if not bool(st.get("realtime_m1_head_fill_missing_15m_enabled", True)):
            report["skipped"].append("disabled")
            return report
    except Exception:
        pass
    try:
        max_n = max(1, min(int(max_bars), 16))
    except Exception:
        max_n = 4
    try:
        wall = as_of if as_of is not None else datetime.now()
        forming = stream_slot_end_datetime(wall)
    except Exception:
        return report
    try:
        day0 = pd.Timestamp(forming).normalize()
        slots = [
            pd.Timestamp(s).floor("min")
            for s in iter_krx_15m_slot_ends_for_day(day0)
        ]
        done = [s for s in slots if s < pd.Timestamp(forming).floor("min")]
        targets = done[-max_n:]
    except Exception:
        return report
    report["checked"] = int(len(targets))
    if not targets:
        return report

    lk = getattr(app, "_bars_data_lock", None)
    bd = getattr(app, "bars_data", None)
    if lk is None or not isinstance(bd, dict):
        report["skipped"].append("no_bars_data")
        return report

    for slot in targets:
        slot_s = slot.strftime("%Y-%m-%d %H:%M:%S")
        try:
            composed = compose_local_15m_ohlcv(app, nk, slot)
        except Exception:
            composed = None
        if composed is None:
            report["failed"].append(slot_s)
            continue
        o, h, l_, c, v = composed
        if float(c) <= 0:
            report["failed"].append(slot_s)
            continue
        try:
            with lk:
                _k, df = resolve_bars_data_frame(bd, nk, migrate_alias=True)
                use_key = _k or nk
                if df is None or getattr(df, "empty", True):
                    report["skipped"].append(f"{slot_s}:empty_df")
                    continue
                # 인덱스 정규화 조회
                hit = None
                try:
                    if slot in df.index:
                        hit = slot
                    else:
                        for ix in df.index:
                            if pd.Timestamp(ix).floor("min") == slot:
                                hit = ix
                                break
                except Exception:
                    hit = None

                if hit is None:
                    # 누락 행 삽입
                    try:
                        dt_str = slot.strftime("%Y-%m-%d %H:%M:%S")
                        new_row = {
                            "datetime": dt_str,
                            "time": ts_to_slot_hhmm(slot),
                            "Open": float(o),
                            "High": float(h),
                            "Low": float(l_),
                            "Close": float(c),
                            "Volume": float(v),
                        }
                        nrd = pd.DataFrame([new_row])
                        nrd["datetime"] = pd.to_datetime(nrd["datetime"])
                        nrd.set_index("datetime", inplace=True)
                        df2 = pd.concat([df, nrd])
                        try:
                            df2 = df2[~df2.index.duplicated(keep="last")]
                            df2 = df2.sort_index().tail(300)
                        except Exception:
                            pass
                        try:
                            df2["ma5"] = df2["Close"].rolling(window=5).mean()
                            df2["ma20"] = df2["Close"].rolling(window=20).mean()
                            df2["ma60"] = df2["Close"].rolling(window=60).mean()
                        except Exception:
                            pass
                        bd[use_key] = df2
                        report["filled_missing"].append(slot_s)
                    except Exception:
                        report["failed"].append(slot_s)
                    continue

                # 존재: 스텁만 공식1분 합성으로 교체 (이미 채워진 봉은 유지)
                try:
                    row = df.loc[hit]
                    if not _looks_like_stub_15m_row(row):
                        continue
                    df.at[hit, "Open"] = float(o)
                    df.at[hit, "High"] = float(h)
                    df.at[hit, "Low"] = float(l_)
                    df.at[hit, "Close"] = float(c)
                    df.at[hit, "Volume"] = float(v)
                    if "time" in df.columns:
                        df.at[hit, "time"] = ts_to_slot_hhmm(slot)
                    try:
                        df["ma5"] = df["Close"].rolling(window=5).mean()
                        df["ma20"] = df["Close"].rolling(window=20).mean()
                        df["ma60"] = df["Close"].rolling(window=60).mean()
                    except Exception:
                        pass
                    bd[use_key] = df
                    report["filled_stub"].append(slot_s)
                except Exception:
                    report["failed"].append(slot_s)
        except Exception:
            report["failed"].append(slot_s)

    # DB·지표 후처리(채운 경우만)
    if report["filled_missing"] or report["filled_stub"]:
        try:
            store = getattr(app, "realtime_monitor_bars_store", None)
            if store is not None:
                for slot_s in list(report["filled_missing"]) + list(
                    report["filled_stub"]
                ):
                    try:
                        ts = pd.Timestamp(slot_s)
                        composed2 = compose_local_15m_ohlcv(app, nk, ts)
                        if composed2 is None:
                            continue
                        o2, h2, l2, c2, v2 = composed2
                        if hasattr(store, "upsert_completed_bar"):
                            store.upsert_completed_bar(
                                nk, ts, o2, h2, l2, c2, v2
                            )
                    except Exception:
                        continue
        except Exception:
            pass
        try:
            from core.closed_15m_fast_path import enqueue_official_indicator_recalc

            enqueue_official_indicator_recalc(app, nk)
        except Exception:
            pass
    return report


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
    """슬롯 롤·M1 반영·신호 재평가(전용 워커 스레드)."""
    try:
        if not skip_roll:
            maybe_roll_15m_slot_on_clock(app, nk)
        sync_forming_15m_last_row_from_official_m1(app, nk)
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
    슬롯 롤 → 1분 집계 반영 → 신호 재평가(전용 워커 큐).
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
