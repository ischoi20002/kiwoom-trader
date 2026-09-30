# -*- coding: utf-8 -*-
"""장마감 후 주문 허용(order_after_close_enabled) 상위 스위치 테스트."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, List

from utils.krx_bars import (
    after_close_orders_blocked_now,
    is_krx_after_close_15m_slot,
    settings_allow_after_close_orders,
)


def _engine(settings: Dict[str, Any], queued: List[str] | None = None) -> Any:
    from core.signal_engine import SignalEngine

    q = queued if queued is not None else []

    class _Nop:
        def queue_buy_signal(self, **kwargs: Any) -> bool:
            q.append("buy")
            return True

        def queue_strategy_action(self, **kwargs: Any) -> bool:
            q.append(str(kwargs.get("action") or ""))
            return True

    app = SimpleNamespace(
        settings=dict(settings),
        next_open_pending_action_service=_Nop(),
        order_execution_service=None,
        log=lambda *_: None,
    )
    return SignalEngine(app)


def test_after_close_slot_labels() -> None:
    """15:30 슬롯·애프터 16:00~19:45는 마감 이후, 정규장 슬롯은 아님."""
    assert is_krx_after_close_15m_slot("2026-09-14 15:30:00")
    assert is_krx_after_close_15m_slot("2026-09-14 16:00:00")
    assert is_krx_after_close_15m_slot("2026-09-14 17:15:00")
    assert is_krx_after_close_15m_slot("2026-09-14 19:45:00")
    assert not is_krx_after_close_15m_slot("2026-09-14 15:15:00")
    assert not is_krx_after_close_15m_slot("2026-09-14 09:00:00")
    assert not is_krx_after_close_15m_slot("2026-09-14 14:45:00")


def test_settings_default_on_and_string_values() -> None:
    assert settings_allow_after_close_orders({}) is True
    assert settings_allow_after_close_orders(None) is True
    assert settings_allow_after_close_orders({"order_after_close_enabled": False}) is False
    assert settings_allow_after_close_orders({"order_after_close_enabled": "false"}) is False
    assert settings_allow_after_close_orders({"order_after_close_enabled": "true"}) is True


def test_wall_clock_block() -> None:
    off = {"order_after_close_enabled": False}
    on = {"order_after_close_enabled": True}
    # 월요일 거래일
    assert after_close_orders_blocked_now(off, datetime(2026, 9, 14, 15, 29, 59)) is False
    assert after_close_orders_blocked_now(off, datetime(2026, 9, 14, 15, 30, 0)) is True
    assert after_close_orders_blocked_now(off, datetime(2026, 9, 14, 17, 0, 0)) is True
    assert after_close_orders_blocked_now(on, datetime(2026, 9, 14, 17, 0, 0)) is False
    # 주말은 대상 아님
    assert after_close_orders_blocked_now(off, datetime(2026, 9, 19, 17, 0, 0)) is False


def test_master_off_disables_1530_orders() -> None:
    se = _engine({"order_after_close_enabled": False, "order_1530_enabled": True})
    assert se._order_1530_enabled() is False
    se_on = _engine({"order_after_close_enabled": True, "order_1530_enabled": True})
    assert se_on._order_1530_enabled() is True


def test_master_off_skips_aftermarket_and_1945_routes() -> None:
    queued: List[str] = []
    se = _engine(
        {
            "order_after_close_enabled": False,
            "next_open_1945_enabled": True,
            "order_1530_enabled": True,
        },
        queued,
    )
    s_after = datetime(2026, 9, 14, 17, 0, 0)
    s_1945 = datetime(2026, 9, 14, 19, 45, 0)
    s_mid = datetime(2026, 9, 14, 14, 0, 0)
    assert se._after_close_order_blocked(s_after) is True
    assert se._after_close_order_blocked(s_mid) is False
    assert se._route_1530_strategy_execution(
        "005930", "EXIT", reason="SL", limit_price=100.0, bar_ts=s_after
    ) == "skipped"
    assert se._route_1530_strategy_execution(
        "005930", "EXIT", reason="SL", limit_price=100.0, bar_ts=s_1945
    ) == "skipped"
    assert se._route_1945_strategy_execution(
        "005930", "EXIT", reason="SL", limit_price=100.0, bar_ts=s_1945
    ) == "skipped"
    # 정규장 슬롯은 영향 없음
    assert se._route_1530_strategy_execution(
        "005930", "EXIT", reason="SL", limit_price=100.0, bar_ts=s_mid
    ) == "none"
    assert queued == []


def test_master_on_keeps_existing_routes() -> None:
    queued: List[str] = []
    se = _engine(
        {"order_after_close_enabled": True, "next_open_1945_enabled": True},
        queued,
    )
    s_after = datetime(2026, 9, 14, 17, 0, 0)
    assert se._after_close_order_blocked(s_after) is False
    assert se._route_1530_strategy_execution(
        "005930", "EXIT", reason="SL", limit_price=100.0, bar_ts=s_after
    ) == "none"
    assert se._route_1945_strategy_execution(
        "005930", "EXIT", reason="SL", limit_price=100.0,
        bar_ts=datetime(2026, 9, 14, 19, 45, 0),
    ) == "queued"
    assert queued == ["EXIT"]


def test_oes_guard_skips_next_open() -> None:
    from unittest.mock import patch

    from core.execution.order_execution_service import OrderExecutionService

    app = SimpleNamespace(settings={"order_after_close_enabled": False})
    svc = OrderExecutionService(app, log_func=lambda *_a, **_k: None)
    with patch(
        "utils.krx_bars.after_close_orders_blocked_now", return_value=True
    ):
        assert svc._after_close_order_blocked_now(None) is True
        assert svc._after_close_order_blocked_now({"limit_price": 1000}) is False


def test_dynamic_bt_fill_skipped_after_close() -> None:
    from dynamic_backtest import DynamicBacktester

    bt = DynamicBacktester.__new__(DynamicBacktester)
    bt.bt_after_close_orders = False
    bt.bt_1530_signal_next_open_fill = False
    bt.bt_1945_signal_next_open_fill = True
    got = bt._resolve_auction_market_fill(
        "005930", "2026-09-14 17:00:00", None, 100.0, "SIG"
    )
    assert got is None
    got_mid = bt._resolve_auction_market_fill(
        "005930", "2026-09-14 14:00:00", None, 100.0, "SIG"
    )
    assert got_mid is not None and float(got_mid[0]) == 100.0

    bt.bt_after_close_orders = True
    got_on = bt._resolve_auction_market_fill(
        "005930", "2026-09-14 17:00:00", None, 100.0, "SIG"
    )
    assert got_on is not None and float(got_on[0]) == 100.0


def test_settings_allow_1530_orders() -> None:
    from utils.krx_bars import settings_allow_1530_orders

    assert settings_allow_1530_orders({}) is False
    assert settings_allow_1530_orders({"order_1530_enabled": True}) is True
    assert settings_allow_1530_orders(
        {"order_1530_enabled": True, "order_after_close_enabled": False}
    ) is False


def test_dynamic_bt_1530_fill_follows_order_1530() -> None:
    """15:30 주문 허용 OFF면 BT도 15:30 체결(익일 시가 이월 포함) 생략."""
    from dynamic_backtest import DynamicBacktester

    bt = DynamicBacktester.__new__(DynamicBacktester)
    bt.bt_after_close_orders = True
    bt.bt_1530_orders = False
    bt.bt_1530_signal_next_open_fill = True
    bt.bt_1945_signal_next_open_fill = True
    assert bt._resolve_auction_market_fill(
        "005930", "2026-09-14 15:30:00", None, 100.0, "SIG"
    ) is None
    # 정규장·애프터 슬롯은 영향 없음
    got = bt._resolve_auction_market_fill(
        "005930", "2026-09-14 17:00:00", None, 100.0, "SIG"
    )
    assert got is not None and float(got[0]) == 100.0


def test_simple_bt_1530_slot_skipped_when_order_off() -> None:
    """SimpleBacktester: 15:30 신호평가 허용이어도 주문 OFF면 스텝 스킵(포지션 불변)."""
    import numpy as np
    import pandas as pd

    from backtest_engine import SimpleBacktester

    bt = SimpleBacktester(
        {"next_open_1530_enabled": True, "order_1530_enabled": False}
    )
    bt.bt_1530_orders = False
    ts = pd.Timestamp("2026-09-14 15:30:00")
    params = {"position": None, "balance": 1_000_000.0}
    out = bt._sim_loop_step_v3(
        0,
        np.array([100.0]),
        np.array([101.0]),
        np.array([99.0]),
        [ts],
        np.array([15]),
        np.array([30]),
        np.array([True]),
        {},
        "005930",
        None,
        False,
        params,
    )
    assert out["position"] is None
    assert float(out["balance"]) == 1_000_000.0


if __name__ == "__main__":
    test_settings_allow_1530_orders()
    test_dynamic_bt_1530_fill_follows_order_1530()
    test_simple_bt_1530_slot_skipped_when_order_off()
    test_after_close_slot_labels()
    test_settings_default_on_and_string_values()
    test_wall_clock_block()
    test_master_off_disables_1530_orders()
    test_master_off_skips_aftermarket_and_1945_routes()
    test_master_on_keeps_existing_routes()
    test_oes_guard_skips_next_open()
    test_dynamic_bt_fill_skipped_after_close()
    print("ok")
