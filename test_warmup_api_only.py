# -*- coding: utf-8 -*-
"""워밍업은 저장된 15분 DB를 읽지 않고 API만 사용한다."""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from core.warmup_service import WarmupService


def _svc() -> WarmupService:
    app = SimpleNamespace(
        settings={
            "realtime_warmup_ohlcv_source": "realtime_15m_db_then_api",
            "use_api_warmup_backfill": True,
        },
        data_manager=SimpleNamespace(),
        auth_manager=SimpleNamespace(),
        config_handler=SimpleNamespace(),
    )
    return WarmupService(app, logger_callback=lambda _m: None)


def test_warmup_source_is_always_api_only() -> None:
    svc = _svc()
    assert svc._warmup_ohlcv_source() == "api_only"


def test_warmup_load_does_not_read_db() -> None:
    svc = _svc()
    called = {"n": 0}

    def _fetch(*_a, **_k):
        called["n"] += 1
        return pd.DataFrame()

    svc._load_from_realtime_monitor_db = _fetch  # type: ignore[method-assign]
    out = svc._load_15m_ohlcv_frame("005930")
    assert out is not None
    assert getattr(out, "empty", True)
    assert called["n"] == 0


def test_api_backfill_discards_stored_base() -> None:
    svc = _svc()
    idx = pd.DatetimeIndex(["2026-09-10 09:00:00"])
    stored = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [1.0], "Volume": [1.0]},
        index=idx,
    )
    got = svc._maybe_api_backfill_15m(
        "005930",
        "삼성전자",
        stored,
        token=None,
    )
    assert got is not None
    assert getattr(got, "empty", True)


if __name__ == "__main__":
    test_warmup_source_is_always_api_only()
    test_warmup_load_does_not_read_db()
    test_api_backfill_discards_stored_base()
    print("ok")
