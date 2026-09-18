# -*- coding: utf-8 -*-
"""체결 텔레그램: fallback·주문번호 dedup."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, List, Optional


def test_fill_telegram_uses_fallback_when_db_empty() -> None:
    from core.execution.fill_reconciliation_service import FillReconciliationService

    sent: List[str] = []

    class _DM:
        def get_order_history_row_for_telegram(
            self, code: str, order_no: str
        ) -> Optional[dict]:
            return None

    app = SimpleNamespace(
        data_manager=_DM(),
        send_telegram_message=lambda msg, parse_mode="HTML": sent.append(msg),
    )
    frs = FillReconciliationService(app, log_func=lambda *_: None)
    frs._maybe_send_buy_sell_telegram(
        "006260",
        "0060544",
        "buy",
        fallback_qty=7,
        fallback_price=320000.0,
        fallback_name="LS",
        fallback_reason="STAGE2",
    )
    # 스레드 기동 직후이므로 짧게 join
    import time

    time.sleep(0.2)
    assert len(sent) == 1
    assert "LS" in sent[0]
    assert "7" in sent[0]
    assert "320,000" in sent[0]

    # dedup
    frs._maybe_send_buy_sell_telegram(
        "006260",
        "0060544",
        "buy",
        fallback_qty=7,
        fallback_price=320000.0,
        fallback_name="LS",
        fallback_reason="STAGE2",
    )
    time.sleep(0.1)
    assert len(sent) == 1


def test_fill_telegram_waits_when_no_qty() -> None:
    from core.execution.fill_reconciliation_service import FillReconciliationService

    sent: List[Any] = []

    class _DM:
        def get_order_history_row_for_telegram(
            self, code: str, order_no: str
        ) -> Optional[dict]:
            return {"side": "buy", "name": "X", "filled_qty": 0, "filled_price": 0}

    app = SimpleNamespace(
        data_manager=_DM(),
        send_telegram_message=lambda *a, **k: sent.append(1),
    )
    frs = FillReconciliationService(app, log_func=lambda *_: None)
    frs._maybe_send_buy_sell_telegram("000660", "1", "buy")
    import time

    time.sleep(0.1)
    assert sent == []
    # 이후 fallback 으로 재시도 가능
    frs._maybe_send_buy_sell_telegram(
        "000660", "1", "buy", fallback_qty=1, fallback_price=1000.0, fallback_name="X"
    )
    time.sleep(0.2)
    assert len(sent) == 1


if __name__ == "__main__":
    test_fill_telegram_uses_fallback_when_db_empty()
    test_fill_telegram_waits_when_no_qty()
    print("ok")
