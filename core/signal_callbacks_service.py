# -*- coding: utf-8 -*-
import traceback
from typing import Any, Optional

from backtest_engine import TradingStrategy


class SignalCallbacksService:
    """
    SignalEngine/StrategyEngine 콜백을 처리하는 서비스.

    - Tk GUI 오케스트레이션과 전략 판정 로직을 분리하기 위함
    - KiwoomApp은 콜백 진입점으로서 최소한만 유지
    """

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print

    def on_buy_signal_received(
        self,
        code: str,
        now_price: float,
        rsi_sig: float,
        macd_val: float,
        osc_val: float,
        ma60_ratio: float,
    ) -> bool:
        """
        SignalEngine 1차 매수 포착 콜백.

        DynamicBacktester 와 동일:
        - 확정 Sig_Type
        - TypeBalanceGate
        - (선택) 선정 트랙(sel_track) 교차 검증 후 주문
        """
        try:
            code = str(code).zfill(6)
            mi = getattr(self.app, "monitor_info", None) or {}
            info = mi.get(code, {})
            name = str(info.get("name", "Unknown"))

            detailed = info.get("last_detailed_sig")
            if detailed is None or getattr(detailed, "empty", True):
                self.log(f"⚠️ [Skip] {code} 신호 캐시 없음")
                try:
                    sl = getattr(self.app, "session_logger", None)
                    if sl:
                        sl.log(
                            "signal_buy_skipped",
                            {"code": code, "name": name, "reason": "no_signal_cache"},
                        )
                except Exception:
                    pass
                return False

            last_sig = detailed.iloc[-1]
            row = (
                last_sig.to_dict()
                if hasattr(last_sig, "to_dict")
                else dict(last_sig)
            )
            sig_type = TradingStrategy.effective_s1_sig_type_for_buy(
                self.app.settings, row, self.app.portfolio.virtual_positions
            )

            # CSV/선정 단계에서 SelTrack(A/B/AB)을 넣은 경우에만 백테 트랙 검증과 동일
            tr = str(
                info.get("sel_track", info.get("SelTrack", info.get("seltrack", "AB")))
            ).strip().upper()

            # 선정 트랙과 확정 신호 타입 불일치: 의도적 무시(백테와 동일)
            if tr == "A" and sig_type == "TYPE_B":
                return False
            if tr == "B" and sig_type == "TYPE_A":
                return False

            if not TradingStrategy.type_balance_allows_new_s1(
                self.app.settings, sig_type, self.app.portfolio.virtual_positions
            ):
                self.log(f"⚠️ [Skip] {name}({code}) TypeBalanceGate (A/B 비중)")
                try:
                    sl = getattr(self.app, "session_logger", None)
                    if sl:
                        sl.log(
                            "signal_buy_skipped",
                            {
                                "code": code,
                                "name": name,
                                "reason": "type_balance_gate",
                                "sig_type": sig_type,
                            },
                        )
                except Exception:
                    pass
                return False

            try:
                sl = getattr(self.app, "session_logger", None)
                if sl:
                    sl.log(
                        "signal_buy_accepted",
                        {
                            "code": code,
                            "name": name,
                            "price": float(now_price),
                            "sig_type": sig_type,
                            "sel_track": tr,
                            "rsi_sig": float(rsi_sig),
                            "macd": float(macd_val),
                            "osc": float(osc_val),
                            "ma60_ratio": float(ma60_ratio),
                        },
                    )
            except Exception:
                pass

            return self.app.trading_controller.execute_buy_signal(
                code, name, now_price, reason=sig_type
            )
        except Exception as e:
            try:
                self.log(f"❌ [BuySignal] {code} 처리 실패: {e}")
            except Exception:
                pass
            try:
                sl = getattr(self.app, "session_logger", None)
                if sl:
                    sl.log(
                        "signal_buy_error", {"code": code, "error": str(e)}
                    )
            except Exception:
                pass
            try:
                traceback.print_exc()
            except Exception:
                pass
            return False

    def on_strategy_action_needed(
        self,
        code: str,
        action: str,
        reason: str,
        **kwargs: Any,
    ) -> Any:
        """StrategyEngine의 액션(EXIT/STAGE2)을 TradingController로 위임."""
        try:
            return self.app.trading_controller.handle_strategy_action(
                code, action, reason, **kwargs
            )
        except Exception:
            return None

