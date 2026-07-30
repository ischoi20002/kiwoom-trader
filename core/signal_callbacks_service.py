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

    @staticmethod
    def format_track_mismatch_status(sel_track: Any, sig_type: Any) -> str:
        """
        감시 트리용: 신호는 떴으나 선정 트랙과 불일치해 진입하지 않을 때 표시.
        예) ``타점(A) · 트랙B 제외``
        """
        try:
            st = str(sig_type or "").strip().upper()
            if st == "TYPE_A" or st.endswith("_A"):
                sig_lbl = "A"
            elif st == "TYPE_B" or st.endswith("_B"):
                sig_lbl = "B"
            else:
                sig_lbl = "?"
            tr = str(sel_track or "AB").strip().upper() or "AB"
            return f"타점({sig_lbl}) · 트랙{tr} 제외"
        except Exception:
            return "타점 · 트랙 불일치"

    @staticmethod
    def log_track_signal_mismatch(
        app: Any,
        *,
        code: str,
        name: str = "",
        sel_track: Any = "AB",
        sig_type: Any = "",
        raw_sig_type: Any = None,
        price: Optional[float] = None,
    ) -> None:
        """트랙·신호 불일치로 S1 매수를 건너뛸 때 세션 로그."""
        try:
            sl = getattr(app, "session_logger", None)
            if not sl:
                return
            payload: dict = {
                "code": str(code).zfill(6),
                "name": str(name or ""),
                "reason": "track_signal_mismatch",
                "sel_track": str(sel_track or "").strip().upper(),
                "sig_type": str(sig_type or ""),
            }
            if raw_sig_type is not None:
                payload["raw_sig_type"] = str(raw_sig_type)
            if price is not None:
                try:
                    payload["price"] = float(price)
                except (TypeError, ValueError):
                    pass
            sl.log("signal_buy_skipped", payload)
        except Exception:
            pass

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
        - (선택) 선정 트랙(sel_track) 교차 검증 후 주문
        """
        try:
            code = str(code).zfill(6)
            mi = getattr(self.app, "monitor_info", None) or {}
            info = mi.get(code, {})
            name = str(info.get("name", "Unknown"))

            detailed = info.get("last_detailed_sig")
            row: dict = {}
            if detailed is not None and not getattr(detailed, "empty", True):
                last_ser = detailed.iloc[-1]
                row = (
                    last_ser.to_dict()
                    if hasattr(last_ser, "to_dict")
                    else dict(last_ser)
                )
            else:
                cached = info.get("last_sig_cache")
                if isinstance(cached, dict) and cached:
                    row = dict(cached)
                else:
                    self.log(f"⚠️ [Skip] {code} 신호 캐시 없음")
                    try:
                        sl = getattr(self.app, "session_logger", None)
                        if sl:
                            sl.log(
                                "signal_buy_skipped",
                                {
                                    "code": code,
                                    "name": name,
                                    "reason": "no_signal_cache",
                                },
                            )
                    except Exception:
                        pass
                    return False
            # CSV/선정 단계에서 SelTrack(A/B/AB)을 넣은 경우에만 백테 트랙 검증과 동일
            tr = str(
                info.get("sel_track", info.get("SelTrack", info.get("seltrack", "AB")))
            ).strip().upper()

            sig_type = TradingStrategy.effective_s1_sig_type_for_buy(
                self.app.settings, row, self.app.portfolio.virtual_positions, tr
            )

            # 선정 트랙과 확정 신호 타입 불일치: 의도적 무시(백테와 동일)
            if not TradingStrategy.track_signal_match_allows(
                self.app.settings, sig_type, tr
            ):
                try:
                    raw_sig = ""
                    try:
                        raw_sig = str(row.get("Sig_Type", "") or "")
                    except Exception:
                        raw_sig = ""
                    self.log_track_signal_mismatch(
                        self.app,
                        code=code,
                        name=name,
                        sel_track=tr,
                        sig_type=sig_type,
                        raw_sig_type=raw_sig,
                        price=float(now_price),
                    )
                except Exception:
                    pass
                try:
                    status = self.format_track_mismatch_status(tr, sig_type)
                    if isinstance(info, dict):
                        info["confluence"] = status
                        info["status_text"] = status
                except Exception:
                    pass
                return False

            # 동시신호에서 확정 타입이 TYPE_A 이면 차단(TYPE_B 경로는 현행 유지)
            if TradingStrategy.simultaneous_type_a_blocked(
                self.app.settings, row, sig_type
            ):
                try:
                    if isinstance(info, dict):
                        info["confluence"] = "동시신호 A 차단"
                        info["status_text"] = "동시신호 A 차단"
                except Exception:
                    pass
                try:
                    sl = getattr(self.app, "session_logger", None)
                    if sl:
                        sl.log(
                            "signal_buy_skipped",
                            {
                                "code": code,
                                "name": name,
                                "reason": "simul_type_a_blocked",
                                "sig_type": str(sig_type),
                                "sel_track": tr,
                                "price": float(now_price),
                            },
                        )
                except Exception:
                    pass
                return False

            if bool(self.app.settings.get("transition_runtime_block_s1", False)):
                try:
                    sl = getattr(self.app, "session_logger", None)
                    if sl:
                        sl.log(
                            "signal_buy_skipped",
                            {
                                "code": code,
                                "name": name,
                                "reason": "transition_block_s1",
                                "regime": str(
                                    self.app.settings.get("transition_runtime_regime", "")
                                ),
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

