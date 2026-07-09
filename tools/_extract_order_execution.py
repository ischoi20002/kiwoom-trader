# -*- coding: utf-8 -*-
"""trading_controller.py 에서 주문 집행 메서드를 OrderExecutionService 로 추출."""
from pathlib import Path

tc_path = Path(__file__).resolve().parents[1] / "core" / "trading_controller.py"
lines = tc_path.read_text(encoding="utf-8").splitlines()

start_idx = next(
    i for i, ln in enumerate(lines) if ln.startswith("    def _supply_log_fields")
)
end_idx = len(lines)
for i in range(start_idx + 1, len(lines)):
    if lines[i].startswith("    def _handle_limit_order_timeout"):
        for j in range(i + 1, len(lines)):
            if lines[j].startswith("    def "):
                end_idx = j
                break
        else:
            end_idx = len(lines)
        break

block = lines[start_idx:end_idx]
new_block: list[str] = []
for ln in block:
    if "self._narrow_monitor_sel_track_after_s1_buy" in ln:
        new_block.append(
            ln.replace(
                "self._narrow_monitor_sel_track_after_s1_buy",
                "self._notify_s1_buy_submitted",
            )
        )
    else:
        new_block.append(ln)

header = '''# -*- coding: utf-8 -*-
"""
주문 집행(S1/S2/EXIT) 및 지정가 미체결 감시를 담당하는 독립 서비스.

TradingController·KiwoomApp 에서 분리 — 이후 전면 개편 시 이 모듈을 중심으로 교체.
"""
import threading
import time
import config
from datetime import datetime
from typing import Any, Dict, Optional, Set, Tuple

from core.execution.limit_order_watch_service import LimitOrderWatchService
from utils.normalization import canonical_stock_code


class OrderExecutionService:
    """실시간 매수·매도 주문 제출·미체결 감시 오케스트레이션."""

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print
        self.limit_order_watch_service = LimitOrderWatchService(
            self.app, log_func=self.log
        )
        self._limit_order_meta = self.limit_order_watch_service.limit_order_meta
        self._buy_skip_d2_one_lot_log_at: Dict[str, float] = {}
        self._exit_retry_not_before: Dict[str, float] = {}
        self._exit_retry_skip_log_at: Dict[str, float] = {}

    @property
    def s1_buy_order_pending_codes(self) -> Set[str]:
        return self.limit_order_watch_service.s1_buy_pending_codes

    def _notify_s1_buy_submitted(self, code: object, reason: object) -> None:
        """S1 매수 접수 후 감시 메타(sel_track) 보정 — TradingController에 위임."""
        try:
            tc = getattr(self.app, "trading_controller", None)
            if tc is not None and hasattr(tc, "_narrow_monitor_sel_track_after_s1_buy"):
                tc._narrow_monitor_sel_track_after_s1_buy(code, reason)
        except Exception:
            pass

'''

out = Path(__file__).resolve().parents[1] / "core" / "execution" / "order_execution_service.py"
out.write_text(header + "\n".join(new_block) + "\n", encoding="utf-8")
print(f"Wrote {out} ({len(new_block)} lines, src {start_idx + 1}-{end_idx})")
