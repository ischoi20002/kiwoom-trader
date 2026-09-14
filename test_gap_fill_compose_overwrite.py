# -*- coding: utf-8 -*-
"""M1 gap-fill: 기존 15m 행 compose 덮어쓰기 + 안정화 동결."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pandas as pd

from core.realtime_official_bars_sync import (
    fill_missing_closed_15m_from_official_m1,
    _row_ohlcv_differs_from_composed,
)
from core.rt_tick_m1_store import RtTickM1Store


def _seed_full_slot_m1(store: RtTickM1Store, code: str, slot: pd.Timestamp) -> None:
    """슬롯 15개 분봉을 공식으로 채운다."""
    for i, o in enumerate((69000.0, 69100.0, 69200.0)):
        lab = slot + pd.Timedelta(minutes=int(i))
        store.apply_official_closed_minute(code, lab, o, o + 50, o - 50, o + 10, 100.0 + i)
    for i in range(3, 15):
        lab = slot + pd.Timedelta(minutes=int(i))
        px = 69200.0 + i
        store.apply_official_closed_minute(code, lab, px, px, px, px, 10.0)


def _seed_tail_only_m1(store: RtTickM1Store, code: str, slot: pd.Timestamp) -> None:
    """앞 6분 결측(축출) 상태 — 평봉 합성 시 Open/Volume 오염."""
    for i in range(6, 15):
        lab = slot + pd.Timedelta(minutes=int(i))
        px = 69200.0 + i
        store.apply_official_closed_minute(code, lab, px, px, px, px, 10.0)


def _base_settings(**extra: Any) -> dict:
    st = {
        "realtime_m1_head_fill_missing_15m_enabled": True,
        "realtime_m1_compose_overwrite_existing_15m": True,
        "realtime_m1_compose_stable_freeze_enabled": True,
        "realtime_m1_compose_stable_match_count": 2,
        "realtime_15m_compose_tick_fill_on_api_miss": True,
        "realtime_15m_compose_require_official_head": True,
        "realtime_15m_compose_flat_middle_no_trade": True,
        "realtime_15m_eval_on_official_last_m1": True,
    }
    st.update(extra)
    return st


def _make_app(code: str, slot: pd.Timestamp, store: RtTickM1Store, *, open_: float, settings: dict | None = None) -> Any:
    df = pd.DataFrame(
        [
            {
                "datetime": slot.strftime("%Y-%m-%d %H:%M:%S"),
                "time": "10:00",
                "Open": float(open_),
                "High": 69300.0,
                "Low": 68900.0,
                "Close": 69310.0,
                "Volume": 500.0,
            }
        ]
    )
    df["datetime"] = pd.to_datetime(df["datetime"])
    df.set_index("datetime", inplace=True)
    return SimpleNamespace(
        settings=settings or _base_settings(),
        rt_tick_m1_store=store,
        rt_tick_stream_ready_at=datetime(2026, 8, 18, 9, 0, 0),
        bars_data={code: df},
        _bars_data_lock=__import__("threading").Lock(),
    )


def _run_fill(app: Any, code: str, slot: pd.Timestamp) -> dict:
    return fill_missing_closed_15m_from_official_m1(
        app,
        code,
        max_bars=1,
        as_of=datetime(2026, 8, 18, 10, 20, 0),
        include_slots=[slot],
    )


def test_row_ohlcv_differs_detects_open_change() -> None:
    row = pd.Series({"Open": 100.0, "High": 110.0, "Low": 90.0, "Close": 105.0, "Volume": 10.0})
    assert _row_ohlcv_differs_from_composed(row, 101.0, 110.0, 90.0, 105.0, 10.0)
    assert not _row_ohlcv_differs_from_composed(row, 100.0, 110.0, 90.0, 105.0, 10.0)


def test_gap_fill_overwrites_wrong_existing_bar() -> None:
    code = "005930"
    slot = pd.Timestamp("2026-08-18 10:00:00")
    store = RtTickM1Store(max_completed_per_code=24)
    _seed_full_slot_m1(store, code, slot)
    app = _make_app(code, slot, store, open_=69100.0)  # skip-first 오염
    rep = _run_fill(app, code, slot)
    assert rep.get("filled_overwrite"), rep
    assert float(app.bars_data[code].loc[slot]["Open"]) == 69000.0


def test_gap_fill_freezes_after_identical_compose() -> None:
    """1회 합성 + 동일 재확인 → 동결 후, 스토어 결측에도 덮어쓰지 않음."""
    code = "005930"
    slot = pd.Timestamp("2026-08-18 10:00:00")
    store = RtTickM1Store(max_completed_per_code=24)
    _seed_full_slot_m1(store, code, slot)
    app = _make_app(code, slot, store, open_=69100.0)

    # 1회차: 잘못된 기존봉 → 올바른 합성으로 덮어쓰기 (hits=1)
    rep1 = _run_fill(app, code, slot)
    assert rep1.get("filled_overwrite"), rep1
    open_good = float(app.bars_data[code].loc[slot]["Open"])
    vol_good = float(app.bars_data[code].loc[slot]["Volume"])
    assert open_good == 69000.0

    # 2회차: 동일 결과 → 동결 (hits=2)
    rep2 = _run_fill(app, code, slot)
    assert not rep2.get("filled_overwrite"), rep2
    st = getattr(app, "_rt15_compose_stable", {})
    assert any(bool(v.get("frozen")) for v in st.values()), st

    # 스토어 앞구간 축출 후 3회차: 재합성 생략
    store2 = RtTickM1Store(max_completed_per_code=24)
    _seed_tail_only_m1(store2, code, slot)
    app.rt_tick_m1_store = store2
    rep3 = _run_fill(app, code, slot)
    slot_s = slot.strftime("%Y-%m-%d %H:%M:%S")
    assert slot_s in (rep3.get("skipped_stable") or []), rep3
    out = app.bars_data[code].loc[slot]
    assert float(out["Open"]) == open_good
    assert float(out["Volume"]) == vol_good


def test_gap_fill_freeze_disabled_allows_flat_overwrite() -> None:
    """동결 OFF 이면 결측 스토어 재합성이 기존 봉을 덮을 수 있음."""
    code = "005930"
    slot = pd.Timestamp("2026-08-18 10:00:00")
    store = RtTickM1Store(max_completed_per_code=24)
    _seed_full_slot_m1(store, code, slot)
    app = _make_app(
        code,
        slot,
        store,
        open_=69100.0,
        settings=_base_settings(realtime_m1_compose_stable_freeze_enabled=False),
    )
    assert _run_fill(app, code, slot).get("filled_overwrite")
    assert float(app.bars_data[code].loc[slot]["Open"]) == 69000.0

    store2 = RtTickM1Store(max_completed_per_code=24)
    _seed_tail_only_m1(store2, code, slot)
    app.rt_tick_m1_store = store2
    rep = _run_fill(app, code, slot)
    assert rep.get("filled_overwrite"), rep
    assert float(app.bars_data[code].loc[slot]["Open"]) != 69000.0


if __name__ == "__main__":
    test_row_ohlcv_differs_detects_open_change()
    test_gap_fill_overwrites_wrong_existing_bar()
    test_gap_fill_freezes_after_identical_compose()
    test_gap_fill_freeze_disabled_allows_flat_overwrite()
    print("ok")
