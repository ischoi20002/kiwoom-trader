# -*- coding: utf-8 -*-
"""
완성 15분 신호평가 직후 → 슬롯+N초(기본 120)에 ka10080(tic=15)로
공식 15분봉을 조회해, 평가에 쓰인 합성봉과 차이를 로그합니다.

차이 나는 종목만 OHLCV를 출력하고, 슬롯 단위로 요약을 남깁니다.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from utils.normalization import canonical_stock_code

ComposeOhlcv = Tuple[float, float, float, float, float]

_LOCK = threading.RLock()
# slot_key → SlotBatch
_BATCHES: Dict[str, "SlotBatch"] = {}


@dataclass
class SlotBatch:
    """한 15분 슬롯의 합성 스냅샷·타이머."""

    slot_ts: pd.Timestamp
    slot_key: str
    # code → 평가 시점 합성 OHLCV
    composed: Dict[str, ComposeOhlcv] = field(default_factory=dict)
    timer: Optional[threading.Timer] = None
    fired: bool = False


def _log(app: Any, msg: str) -> None:
    try:
        fn = getattr(app, "log", None)
        if callable(fn):
            fn(msg)
    except Exception:
        pass


def _settings(app: Any) -> Dict[str, Any]:
    try:
        st = getattr(app, "settings", None)
        return st if isinstance(st, dict) else {}
    except Exception:
        return {}


def _enabled(app: Any) -> bool:
    try:
        v = _settings(app).get("realtime_compose_vs_api15_diag_enabled", True)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return True


def _delay_sec(app: Any) -> float:
    try:
        v = float(
            _settings(app).get("realtime_compose_vs_api15_diag_delay_sec", 120.0)
        )
    except (TypeError, ValueError):
        v = 120.0
    return max(30.0, min(v, 600.0))


def _gap_sec(app: Any) -> float:
    """종목 간 ka10080 호출 간격(유량=1 완화)."""
    try:
        v = float(
            _settings(app).get("realtime_compose_vs_api15_diag_gap_sec", 1.1)
        )
    except (TypeError, ValueError):
        v = 1.1
    return max(0.5, min(v, 5.0))


def _slot_key(bar_ts: Any) -> str:
    try:
        return pd.Timestamp(bar_ts).floor("min").strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(bar_ts)


def _norm_ohlcv(o: Any, h: Any, l_: Any, c: Any, v: Any) -> Optional[ComposeOhlcv]:
    try:
        oo = float(o)
        hh = float(h)
        ll = float(l_)
        cc = float(c)
        vv = max(0.0, float(v or 0))
        if cc <= 0:
            return None
        return (oo, hh, ll, cc, vv)
    except (TypeError, ValueError):
        return None


def _read_composed_from_bars(app: Any, code: str, bar_ts: Any) -> Optional[ComposeOhlcv]:
    """bars_data / RT DB 에서 해당 슬롯 합성봉 읽기."""
    try:
        slot = pd.Timestamp(bar_ts).floor("min")
    except Exception:
        return None
    # 1) bars_data
    try:
        bd = getattr(app, "bars_data", None)
        df = bd.get(code) if isinstance(bd, dict) else None
        if df is not None and not getattr(df, "empty", True):
            try:
                idx = pd.DatetimeIndex(pd.to_datetime(df.index)).floor("min")
            except Exception:
                idx = df.index
            try:
                mask = idx == slot
                if hasattr(mask, "any") and bool(mask.any()):
                    row = df.loc[mask].iloc[-1]
                    return _norm_ohlcv(
                        row.get("Open", row.get("open")),
                        row.get("High", row.get("high")),
                        row.get("Low", row.get("low")),
                        row.get("Close", row.get("close")),
                        row.get("Volume", row.get("volume")),
                    )
            except Exception:
                pass
            try:
                last_ix = pd.Timestamp(df.index[-1]).floor("min")
                if last_ix == slot:
                    row = df.iloc[-1]
                    return _norm_ohlcv(
                        row.get("Open", row.get("open")),
                        row.get("High", row.get("high")),
                        row.get("Low", row.get("low")),
                        row.get("Close", row.get("close")),
                        row.get("Volume", row.get("volume")),
                    )
            except Exception:
                pass
    except Exception:
        pass
    # 2) realtime_monitor_bars_store
    try:
        store = getattr(app, "realtime_monitor_bars_store", None)
        if store is not None and hasattr(store, "fetch_15m_bars_as_dataframe"):
            df2 = store.fetch_15m_bars_as_dataframe(code, limit=64)
            if df2 is not None and not getattr(df2, "empty", True):
                try:
                    # time 컬럼 또는 index
                    if "time" in df2.columns:
                        times = pd.to_datetime(df2["time"])
                        sub = df2.loc[times.dt.floor("min") == slot]
                    else:
                        idx2 = pd.DatetimeIndex(pd.to_datetime(df2.index)).floor("min")
                        sub = df2.loc[idx2 == slot]
                    if sub is not None and not getattr(sub, "empty", True):
                        row = sub.iloc[-1]
                        return _norm_ohlcv(
                            row.get("Open", row.get("open")),
                            row.get("High", row.get("high")),
                            row.get("Low", row.get("low")),
                            row.get("Close", row.get("close")),
                            row.get("Volume", row.get("volume")),
                        )
                except Exception:
                    pass
    except Exception:
        pass
    return None


def _ohlcv_tuple_str(t: ComposeOhlcv) -> str:
    try:
        return (
            f"O={int(round(t[0]))} H={int(round(t[1]))} "
            f"L={int(round(t[2]))} C={int(round(t[3]))} V={int(round(t[4]))}"
        )
    except Exception:
        return str(t)


def _diff_fields(a: ComposeOhlcv, b: ComposeOhlcv) -> List[str]:
    names = ("O", "H", "L", "C", "V")
    out: List[str] = []
    for i, n in enumerate(names):
        try:
            if abs(float(a[i]) - float(b[i])) > 1e-6:
                out.append(n)
        except Exception:
            out.append(n)
    return out


def note_after_closed_15m_eval(
    app: Any,
    code: Any,
    bar_ts: Any,
    *,
    composed: Optional[ComposeOhlcv] = None,
) -> None:
    """
    완성 15분 신호평가가 끝난 뒤 호출.

    슬롯 시각 + delay(기본 120초)에 해당 종목들의 합성 vs API 15분 비교를 예약합니다.
    """
    try:
        if not _enabled(app):
            return
        nk = canonical_stock_code(str(code or "").strip())
        if not nk:
            return
        slot = pd.Timestamp(bar_ts).floor("min")
        sk = _slot_key(slot)
        snap = composed
        if snap is None:
            snap = _read_composed_from_bars(app, nk, slot)
        if snap is None:
            return

        delay = _delay_sec(app)
        fire_at = slot.to_pydatetime() + pd.Timedelta(seconds=delay)
        try:
            now = datetime.now()
        except Exception:
            now = datetime.now()
        wait = (fire_at - now).total_seconds()
        if wait < 0.5:
            wait = 0.5

        with _LOCK:
            batch = _BATCHES.get(sk)
            if batch is None:
                batch = SlotBatch(slot_ts=slot, slot_key=sk)
                _BATCHES[sk] = batch
            if batch.fired:
                return
            batch.composed[nk] = snap
            if batch.timer is None:
                t = threading.Timer(
                    float(wait),
                    _run_slot_compare,
                    args=(app, sk),
                )
                t.daemon = True
                batch.timer = t
                try:
                    t.start()
                except Exception:
                    batch.timer = None
                    return
                try:
                    _log(
                        app,
                        f"ℹ️ [ComposeVsAPI15] {sk} 비교 예약 "
                        f"(+{delay:.0f}s, 현재등록 {len(batch.composed)}종목)",
                    )
                except Exception:
                    pass
    except Exception as e:
        try:
            _log(app, f"⚠️ [ComposeVsAPI15] 예약 실패: {e}")
        except Exception:
            pass


def _get_token(app: Any) -> str:
    try:
        am = getattr(app, "auth_manager", None)
        if am is not None and hasattr(am, "get_auth_token"):
            tok = am.get_auth_token()
            return str(tok or "").strip()
    except Exception:
        pass
    return ""


def _run_slot_compare(app: Any, slot_key: str) -> None:
    """슬롯 배치: 등록 종목 순회하며 API 15분 vs 합성 비교."""
    batch: Optional[SlotBatch] = None
    try:
        with _LOCK:
            batch = _BATCHES.get(slot_key)
            if batch is None or batch.fired:
                return
            batch.fired = True
            items = list(batch.composed.items())
            slot = batch.slot_ts
            batch.timer = None
    except Exception:
        return
    if not items:
        return

    try:
        if not _enabled(app):
            return
    except Exception:
        return

    dm = getattr(app, "data_manager", None)
    if dm is None:
        _log(app, f"⚠️ [ComposeVsAPI15] {slot_key} data_manager 없음 — 비교 생략")
        return

    token = _get_token(app)
    if not token:
        _log(app, f"⚠️ [ComposeVsAPI15] {slot_key} 토큰 없음 — 비교 생략")
        return

    gap = _gap_sec(app)
    n_ok = 0
    n_diff = 0
    n_fail = 0
    n_match = 0

    _log(
        app,
        f"🔎 [ComposeVsAPI15] {slot_key} 합성 vs API(15분) 비교 시작 "
        f"({len(items)}종목, gap={gap:.1f}s)",
    )

    from core.rt_closed_15m_official_replace import fetch_official_15m_row_for_timestamp

    for i, (nk, comp) in enumerate(sorted(items, key=lambda x: x[0])):
        if i > 0:
            try:
                time.sleep(gap)
            except Exception:
                pass
        try:
            tc = getattr(app, "trading_controller", None)
            if tc is not None and not bool(getattr(tc, "is_monitoring", True)):
                _log(app, f"ℹ️ [ComposeVsAPI15] {slot_key} 감시 중단 — 비교 중단")
                break
        except Exception:
            pass

        try:
            token = _get_token(app) or token
            api_row = fetch_official_15m_row_for_timestamp(
                dm,
                nk,
                token,
                slot,
                tic_scope="15",
                max_pages=4,
                max_retries=2,
                inter_retry_sec=0.6,
                app=app,
            )
        except Exception as e:
            n_fail += 1
            try:
                _log(app, f"⚠️ [ComposeVsAPI15] {nk} {slot_key} API 조회 실패: {e}")
            except Exception:
                pass
            continue

        if api_row is None:
            n_fail += 1
            try:
                _log(
                    app,
                    f"⚠️ [ComposeVsAPI15] {nk} {slot_key} API 15분봉 없음",
                )
            except Exception:
                pass
            continue

        n_ok += 1
        try:
            api_t = (
                float(api_row[0]),
                float(api_row[1]),
                float(api_row[2]),
                float(api_row[3]),
                max(0.0, float(api_row[4])),
            )
        except Exception:
            n_fail += 1
            continue

        fields = _diff_fields(comp, api_t)
        if not fields:
            n_match += 1
            continue

        n_diff += 1
        try:
            _log(
                app,
                f"⚠️ [ComposeVsAPI15] DIFF {nk} {slot_key} fields={''.join(fields)} "
                f"| 합성 {_ohlcv_tuple_str(comp)} "
                f"| API {_ohlcv_tuple_str(api_t)}",
            )
        except Exception:
            pass

    try:
        _log(
            app,
            f"ℹ️ [ComposeVsAPI15] {slot_key} 완료 "
            f"match={n_match} diff={n_diff} api_fail={n_fail} "
            f"compared={n_ok}/{len(items)}",
        )
    except Exception:
        pass

    try:
        with _LOCK:
            _BATCHES.pop(slot_key, None)
            if len(_BATCHES) > 32:
                keys = sorted(_BATCHES.keys())
                for k in keys[:-16]:
                    b = _BATCHES.get(k)
                    if b is not None and b.fired:
                        _BATCHES.pop(k, None)
    except Exception:
        pass
