# -*- coding: utf-8 -*-
"""워밍업 UI 브리지: prep_ok 가 다음 종목 이벤트에 덮이지 않는지."""
from __future__ import annotations

from types import SimpleNamespace

from core.warmup_stock_prep_ui_service import WarmupStockPrepUiService


class _FakeTab:
    def __init__(self) -> None:
        self.events: list[tuple] = []

    def update_warmup_stock_prep(
        self,
        event: str,
        cur: int,
        total: int,
        name: str,
        code: str,
        reason: str = "",
    ) -> None:
        self.events.append((event, cur, name, str(code).zfill(6), reason))


def test_prep_ok_not_dropped_by_next_stock() -> None:
    tab = _FakeTab()
    app = SimpleNamespace(monitor_tab=tab, root=None, lbl_monitor_status=None, status_label=None)
    svc = WarmupStockPrepUiService(app)
    svc.on_warmup_stock_prep("prep_progress", 1, 4, "GS", "078930", "15분봉 API 1/40페이지 (보충)")
    svc.on_warmup_stock_prep("prep_ok", 1, 4, "GS", "078930", "")
    svc.on_warmup_stock_prep("prep_start", 2, 4, "다음", "005930", "")
    kinds = [e[0] for e in tab.events]
    assert "prep_ok" in kinds
    last_gs = [e for e in tab.events if e[3] == "078930"][-1]
    assert last_gs[0] == "prep_ok"


if __name__ == "__main__":
    test_prep_ok_not_dropped_by_next_stock()
    print("ok")
