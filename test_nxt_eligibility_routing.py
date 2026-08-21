# -*- coding: utf-8 -*-
"""NXT 거래가능 캐시 스모크 테스트."""
from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from core.nxt_eligibility_service import NxtEligibilityService


def test_nxt_cache_roundtrip_same_day() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "_nxt_eligibility.json")
        app = SimpleNamespace(
            auth_manager=None,
            data_manager=None,
            monitor_codes=["005930"],
            portfolio=SimpleNamespace(virtual_positions={}),
            trading_controller=None,
        )
        with patch(
            "core.nxt_eligibility_service._cache_path", return_value=path
        ), patch(
            "core.nxt_eligibility_service.NxtEligibilityService._today_key",
            return_value="2099-01-02",
        ):
            svc = NxtEligibilityService(app, log_func=lambda *_: None)
            with svc._lock:
                svc._day = "2099-01-02"
                svc._enabled = {"005930"}
                svc._disabled = {"000660"}
                svc._updated_at = "2099-01-02T08:00:00"
            svc._persist()
            svc2 = NxtEligibilityService(app, log_func=lambda *_: None)
            assert svc2.is_nxt_tradable("005930", fetch_if_unknown=False) is True
            assert svc2.is_nxt_tradable("000660", fetch_if_unknown=False) is False
            assert svc2.is_nxt_tradable("035420", fetch_if_unknown=False) is False


def test_collect_codes_includes_holdings() -> None:
    app = SimpleNamespace(
        auth_manager=None,
        data_manager=None,
        monitor_codes=["005930"],
        portfolio=SimpleNamespace(
            virtual_positions={"000660": {"qty": 1}, "035420_AL": {"qty": 2}}
        ),
        trading_controller=SimpleNamespace(monitor_codes=["005380"]),
    )
    svc = NxtEligibilityService(app, log_func=lambda *_: None)
    got = set(svc.collect_codes_for_refresh())
    assert "005930" in got
    assert "000660" in got
    assert "035420" in got
    assert "005380" in got


if __name__ == "__main__":
    test_nxt_cache_roundtrip_same_day()
    test_collect_codes_includes_holdings()
    print("ok")
