# -*- coding: utf-8 -*-
"""S1 매수 콜백 — 확정봉 스냅샷 우선 사용·형성봉 캐시 덮어쓰기 오판 방지 검증 (443060 사례)."""
from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import pandas as pd

from core.signal_callbacks_service import (
    CONFIRMED_S1_SIG_KEY,
    SignalCallbacksService,
)


class _Logger:
    def __init__(self) -> None:
        self.events: List[Tuple[str, Dict[str, Any]]] = []

    def log(self, kind: str, payload: Dict[str, Any]) -> None:
        self.events.append((kind, dict(payload)))


class _Controller:
    def __init__(self) -> None:
        self.buys: List[Tuple[str, str, float, str]] = []

    def execute_buy_signal(self, code: str, name: str, price: float, reason: str = "") -> bool:
        self.buys.append((code, name, float(price), str(reason)))
        return True


def _sig_row(signal: bool, sig_type: str) -> Dict[str, Any]:
    return {
        "Signal": signal,
        "Sig_Type": sig_type,
        "Met_Count": 7 if signal else 5,
        "Total_Count": 7,
        "Met_Ratio": 1.0 if signal else 5 / 7,
        "S1_Simultaneous_AB": False,
    }


def _make_app(info: Dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        settings={
            "use_type_a": True,
            "use_type_b": True,
            "enforce_track_signal_match": True,
        },
        portfolio=SimpleNamespace(virtual_positions={}),
        monitor_info={"443060": info},
        session_logger=_Logger(),
        trading_controller=_Controller(),
    )


def _forming_detailed() -> pd.DataFrame:
    """틱 경로가 형성 중인 10:15 봉으로 덮어쓴 상태(신호 없음)."""
    return pd.DataFrame([_sig_row(False, "NONE")], index=[pd.Timestamp("2026-09-28 10:15")])


def _call(svc: SignalCallbacksService) -> bool:
    return svc.on_buy_signal_received(
        code="443060",
        now_price=242000.0,
        rsi_sig=48.9,
        macd_val=100.0,
        osc_val=191.7,
        ma60_ratio=0.4,
    )


def _skip_reasons(app: SimpleNamespace) -> List[str]:
    return [
        str(p.get("reason"))
        for k, p in app.session_logger.events
        if k == "signal_buy_skipped"
    ]


def test_confirmed_snapshot_wins_over_overwritten_cache() -> None:
    info: Dict[str, Any] = {
        "name": "HD현대마린솔루션",
        "sel_track": "A",
        "last_detailed_sig": _forming_detailed(),
        "last_sig_cache": _sig_row(False, "NONE"),
    }
    SignalCallbacksService.store_confirmed_s1_signal(
        info, "2026-09-28 10:00:00", _sig_row(True, "TYPE_A")
    )
    app = _make_app(info)
    svc = SignalCallbacksService(app, log_func=lambda *_a, **_k: None)

    assert _call(svc) is True
    assert app.trading_controller.buys == [("443060", "HD현대마린솔루션", 242000.0, "TYPE_A")]
    accepted = [p for k, p in app.session_logger.events if k == "signal_buy_accepted"]
    assert accepted and accepted[0]["row_src"] == "confirmed"


def test_stale_cache_without_snapshot_is_not_misread_as_type_b() -> None:
    info: Dict[str, Any] = {
        "name": "HD현대마린솔루션",
        "sel_track": "A",
        "last_detailed_sig": _forming_detailed(),
    }
    app = _make_app(info)
    svc = SignalCallbacksService(app, log_func=lambda *_a, **_k: None)

    assert _call(svc) is False
    assert app.trading_controller.buys == []
    reasons = _skip_reasons(app)
    assert reasons == ["stale_signal_cache"]
    assert "track_signal_mismatch" not in reasons


def test_expired_snapshot_falls_back_to_cache() -> None:
    info: Dict[str, Any] = {
        "name": "HD현대마린솔루션",
        "sel_track": "A",
        "last_detailed_sig": _forming_detailed(),
    }
    SignalCallbacksService.store_confirmed_s1_signal(
        info, "2026-09-28 10:00:00", _sig_row(True, "TYPE_A")
    )
    info[CONFIRMED_S1_SIG_KEY]["at"] = time.monotonic() - 10_000.0
    app = _make_app(info)
    svc = SignalCallbacksService(app, log_func=lambda *_a, **_k: None)

    assert _call(svc) is False
    assert _skip_reasons(app) == ["stale_signal_cache"]


def test_valid_detailed_row_without_snapshot_still_buys() -> None:
    detailed = pd.DataFrame(
        [_sig_row(True, "TYPE_A")], index=[pd.Timestamp("2026-09-28 10:00")]
    )
    info: Dict[str, Any] = {
        "name": "HD현대마린솔루션",
        "sel_track": "A",
        "last_detailed_sig": detailed,
    }
    app = _make_app(info)
    svc = SignalCallbacksService(app, log_func=lambda *_a, **_k: None)

    assert _call(svc) is True
    assert app.trading_controller.buys[0][3] == "TYPE_A"


def test_real_track_mismatch_still_blocked() -> None:
    info: Dict[str, Any] = {"name": "X", "sel_track": "A"}
    SignalCallbacksService.store_confirmed_s1_signal(
        info, "2026-09-28 10:00:00", _sig_row(True, "TYPE_B")
    )
    app = _make_app(info)
    svc = SignalCallbacksService(app, log_func=lambda *_a, **_k: None)

    assert _call(svc) is False
    assert _skip_reasons(app) == ["track_signal_mismatch"]
