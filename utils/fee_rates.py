# -*- coding: utf-8 -*-
"""
전략 설정(settings)과 서버 모드(config.USE_MOCK)에 따른 수수료율(소수) 조회.

- 설정값은 모두 「체결금액 대비 %」 숫자(예: 0.015 = 0.015%)이며, 반환값은 곱셈용 소수(÷100).
"""
from __future__ import annotations

from typing import Any, Mapping, Tuple


def fee_decimal_rates_live_trading(settings: Mapping[str, Any]) -> Tuple[float, float]:
    """
    실시간 가상 체결(PortfolioManager)·일일 결산 리포트 표용.
    - 모의 서버(USE_MOCK=True): buy_fee_mock, sell_fee_mock
    - 실전 서버(USE_MOCK=False): buy_fee_live, sell_fee_live (미설정 시 buy_fee_bt/sell_fee_bt 폴백)
    """
    try:
        import config as _cfg

        use_mock = bool(getattr(_cfg, "USE_MOCK", True))
    except Exception:
        use_mock = True
    try:
        if use_mock:
            bp = float(settings.get("buy_fee_mock", 0.0))
            sp = float(settings.get("sell_fee_mock", 0.0))
        else:
            bp = float(
                settings.get("buy_fee_live", settings.get("buy_fee_bt", 0.015))
            )
            sp = float(
                settings.get("sell_fee_live", settings.get("sell_fee_bt", 0.215))
            )
    except (TypeError, ValueError):
        bp, sp = 0.015, 0.215
    return bp / 100.0, sp / 100.0


def fee_decimal_rates_backtest(settings: Mapping[str, Any]) -> Tuple[float, float]:
    """백테스트(SimpleBacktester, DynamicBacktester): buy_fee_bt, sell_fee_bt."""
    try:
        bp = float(settings.get("buy_fee_bt", settings.get("buy_fee_rate", 0.015)))
        sp = float(settings.get("sell_fee_bt", settings.get("sell_fee_rate", 0.215)))
    except (TypeError, ValueError):
        bp, sp = 0.015, 0.215
    return bp / 100.0, sp / 100.0
