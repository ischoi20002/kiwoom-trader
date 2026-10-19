# -*- coding: utf-8 -*-
"""M1 gap-fill: 기존 15m 행 compose 덮어쓰기 + 안정화 동결."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pandas as pd

from core.realtime.realtime_official_bars_sync import (
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


def _slot_ohlcv_from_store(store: RtTickM1Store, code: str, slot: pd.Timestamp) -> tuple:
    """스토어 15개 1분으로 기대 15분 OHLCV 계산(테스트 기준값)."""
    df = store.dataframe_for_window(
        code, slot, slot + pd.Timedelta(minutes=14), include_open_bucket=False
    )
    return (
        float(df["Open"].iloc[0]),
        float(df["High"].max()),
        float(df["Low"].min()),
        float(df["Close"].iloc[-1]),
        float(df["Volume"].sum()),
    )


def _make_app_with_bar(code: str, slot: pd.Timestamp, store: RtTickM1Store, bar: tuple) -> Any:
    o, h, l_, c, v = bar
    app = _make_app(code, slot, store, open_=o)
    df = app.bars_data[code]
    df.loc[slot, ["Open", "High", "Low", "Close", "Volume"]] = [o, h, l_, c, v]
    return app


def _chart_for_minutes(rows: dict) -> pd.DataFrame:
    """{open_label: (o,h,l,c,v)} → ka10080 형태 차트(인덱스 = open 라벨)."""
    idx = pd.DatetimeIndex(sorted(rows.keys()))
    vals = [rows[t] for t in idx]
    return pd.DataFrame(
        {
            "Open": [x[0] for x in vals],
            "High": [x[1] for x in vals],
            "Low": [x[2] for x in vals],
            "Close": [x[3] for x in vals],
            "Volume": [x[4] for x in vals],
        },
        index=idx,
    )


def test_gap_fill_first_match_does_not_freeze() -> None:
    """기존 봉과 첫 합성이 같아도 1회 확인만으로는 동결하지 않음(미완 말분 교정 여지)."""
    code = "005930"
    slot = pd.Timestamp("2026-08-18 10:00:00")
    store = RtTickM1Store(max_completed_per_code=24)
    _seed_full_slot_m1(store, code, slot)
    app = _make_app_with_bar(code, slot, store, _slot_ohlcv_from_store(store, code, slot))

    rep1 = _run_fill(app, code, slot)
    assert not rep1.get("filled_overwrite"), rep1
    st = getattr(app, "_rt15_compose_stable", {})
    assert st and not any(bool(v.get("frozen")) for v in st.values()), st

    rep2 = _run_fill(app, code, slot)
    assert not rep2.get("filled_overwrite"), rep2
    assert any(bool(v.get("frozen")) for v in st.values()), st


def test_revised_official_last_minute_unfreezes_and_corrects() -> None:
    """동결 후 catchup 이 말분 1분을 다른 값(최종 확정)으로 바꾸면 동결 해제 → 15분 교정."""
    from core.realtime_m1_fallback import apply_chart_catchup_to_m1_store

    code = "005930"
    slot = pd.Timestamp("2026-08-18 10:00:00")
    last = slot + pd.Timedelta(minutes=14)
    store = RtTickM1Store(max_completed_per_code=24)
    _seed_full_slot_m1(store, code, slot)
    # 미완 말분: 거래량·저가가 덜 반영된 상태
    store.apply_official_closed_minute(code, last, 69214.0, 69214.0, 69214.0, 69214.0, 5.0)
    partial = _slot_ohlcv_from_store(store, code, slot)
    app = _make_app_with_bar(code, slot, store, partial)

    _run_fill(app, code, slot)
    _run_fill(app, code, slot)
    st = getattr(app, "_rt15_compose_stable", {})
    assert any(bool(v.get("frozen")) for v in st.values()), st

    final_last = (69214.0, 69214.0, 68800.0, 68800.0, 40.0)
    chart = _chart_for_minutes({last: final_last})
    applied = apply_chart_catchup_to_m1_store(app, code, chart, last, from_open=slot)
    assert applied == 1
    assert not any(bool(v.get("frozen")) for v in st.values()), st

    rep = _run_fill(app, code, slot)
    assert rep.get("filled_overwrite"), rep
    out = app.bars_data[code].loc[slot]
    assert float(out["Close"]) == 68800.0
    assert float(out["Low"]) == 68800.0
    assert float(out["Volume"]) == partial[4] - 5.0 + 40.0


def test_catchup_new_minute_does_not_unfreeze() -> None:
    """스토어에 없던 분(축출 재적재 등) 신규 반영은 동결을 풀지 않음."""
    from core.realtime_m1_fallback import apply_chart_catchup_to_m1_store

    code = "005930"
    slot = pd.Timestamp("2026-08-18 10:00:00")
    store = RtTickM1Store(max_completed_per_code=24)
    _seed_full_slot_m1(store, code, slot)
    app = _make_app_with_bar(code, slot, store, _slot_ohlcv_from_store(store, code, slot))
    _run_fill(app, code, slot)
    _run_fill(app, code, slot)
    st = getattr(app, "_rt15_compose_stable", {})
    assert any(bool(v.get("frozen")) for v in st.values()), st

    store2 = RtTickM1Store(max_completed_per_code=24)
    app.rt_tick_m1_store = store2
    first = slot
    chart = _chart_for_minutes({first: (69000.0, 69050.0, 68950.0, 69010.0, 100.0)})
    assert apply_chart_catchup_to_m1_store(app, code, chart, first, from_open=slot) == 1
    assert any(bool(v.get("frozen")) for v in st.values()), st


if __name__ == "__main__":
    test_row_ohlcv_differs_detects_open_change()
    test_gap_fill_overwrites_wrong_existing_bar()
    test_gap_fill_freezes_after_identical_compose()
    test_gap_fill_freeze_disabled_allows_flat_overwrite()
    test_gap_fill_first_match_does_not_freeze()
    test_revised_official_last_minute_unfreezes_and_corrects()
    test_catchup_new_minute_does_not_unfreeze()
    print("ok")
