# -*- coding: utf-8 -*-
"""trading_controller.py 에서 OrderExecutionService 로 이전된 메서드 제거·위임 추가."""
from pathlib import Path

tc_path = Path(__file__).resolve().parents[1] / "core" / "trading_controller.py"
lines = tc_path.read_text(encoding="utf-8").splitlines()

narrow_start = next(
    i
    for i, ln in enumerate(lines)
    if ln.startswith("    def _narrow_monitor_sel_track_after_s1_buy")
)
narrow_end = narrow_start + 1
while narrow_end < len(lines):
    if lines[narrow_end].startswith("    def ") and narrow_end != narrow_start:
        break
    narrow_end += 1

supply_start = next(
    i for i, ln in enumerate(lines) if ln.startswith("    def _supply_log_fields")
)
handle_timeout_start = next(
    i for i, ln in enumerate(lines) if ln.startswith("    def _handle_limit_order_timeout")
)
handle_timeout_end = handle_timeout_start + 1
while handle_timeout_end < len(lines):
    if lines[handle_timeout_end].startswith("    def "):
        break
    handle_timeout_end += 1

delegates = '''
    def execute_buy_signal(self, code, name, price, reason="TYPE_A") -> bool:
        """OrderExecutionService 위임 — S1 매수 집행."""
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is not None:
                return bool(oes.execute_buy_signal(code, name, price, reason=reason))
        except Exception:
            pass
        return False

    def handle_strategy_action(self, code, action, reason, **kwargs):
        """OrderExecutionService 위임 — EXIT/STAGE2 집행."""
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is not None:
                oes.handle_strategy_action(code, action, reason, **kwargs)
        except Exception:
            pass

    @property
    def limit_order_watch_service(self):
        """호환: 미체결 감시는 OrderExecutionService 가 소유."""
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is not None:
                return oes.limit_order_watch_service
        except Exception:
            pass
        return None

    @property
    def _limit_order_meta(self):
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is not None:
                return oes._limit_order_meta
        except Exception:
            pass
        return {}

    @property
    def _s1_buy_order_pending_codes(self):
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is not None:
                return oes.s1_buy_order_pending_codes
        except Exception:
            pass
        return set()

    def clear_limit_order_watch(self, order_no: Optional[str]) -> None:
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is not None:
                oes.clear_limit_order_watch(order_no)
        except Exception:
            pass

    def _register_limit_order_watch(self, *args, **kwargs) -> None:
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is not None:
                oes._register_limit_order_watch(*args, **kwargs)
        except Exception:
            pass

    def _handle_limit_order_timeout(self, order_no: str) -> None:
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is not None:
                oes._handle_limit_order_timeout(order_no)
        except Exception:
            pass
'''.strip("\n").splitlines()

new_lines = (
    lines[:supply_start]
    + lines[narrow_start:narrow_end]
    + delegates
    + lines[handle_timeout_end:]
)
tc_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
print(f"Patched {tc_path}: removed order block, kept narrow at {narrow_start + 1}-{narrow_end}")
