# -*- coding: utf-8 -*-
"""active_positions 저장 시 max_p 를 DataManager로 전달하는지."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

from core.portfolio_manager import PortfolioManager


class _FakeDM:
    def __init__(self) -> None:
        self.calls: List[Tuple[tuple, Dict[str, Any]]] = []

    def save_active_position(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, dict(kwargs)))


def test_save_active_position_forwards_max_p_to_dm() -> None:
    dm = _FakeDM()
    app = SimpleNamespace(settings={})
    pf = PortfolioManager(app, dm, order_manager=None, log_func=lambda m: None)
    pf.virtual_positions["377300"] = {
        "name": "카카오페이",
        "avg_price": 50000.0,
        "qty": 10,
        "max_p": 52000.0,
        "max_price": 52000.0,
        "max_p_high": 53000.0,
        "max_now": 51000.0,
    }
    pf._save_active_position(
        "377300",
        "카카오페이",
        50000.0,
        10,
        1,
        500000.0,
        "2026-08-25T12:00:00",
        "SIGNAL",
    )
    assert len(dm.calls) == 1
    args, kwargs = dm.calls[0]
    assert args[0] == "377300"
    assert kwargs.get("max_p") == 52000.0
    assert kwargs.get("max_p_high") == 53000.0


if __name__ == "__main__":
    test_save_active_position_forwards_max_p_to_dm()
    print("ok")
