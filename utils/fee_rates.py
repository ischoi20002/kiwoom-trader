# -*- coding: utf-8 -*-
"""
전략 설정(settings)과 서버 모드(config.USE_MOCK)에 따른 수수료율(소수) 조회.

- 설정값은 모두 「체결금액 대비 %」 숫자(예: 0.015 = 0.015%)이며, 반환값은 곱셈용 소수(÷100).
"""
from __future__ import annotations

from typing import Any, Mapping, Tuple

# settings 키 누락 시 폴백(% 값). core/defaults.py DEFAULT_SETTINGS 와 동기화.
_FALLBACK_MOCK_BUY_PCT = 0.35
_FALLBACK_MOCK_SELL_PCT = 0.5
_FALLBACK_LIVE_BUY_PCT = 0.015
_FALLBACK_LIVE_SELL_PCT = 0.215


def fee_decimal_rates_live_trading(settings: Mapping[str, Any]) -> Tuple[float, float]:
    """
    실시간 가상 체결(PortfolioManager)·종료내역 수수료 컬럼·일일 결산 표용.

    - config.USE_MOCK=True: **buy_fee_mock** / **sell_fee_mock** (모의 비율이 실전보다 큼 — 별도 키)
    - USE_MOCK=False: **buy_fee_live** / **sell_fee_live** (없으면 buy_fee_bt / sell_fee_bt)

    반환값은 체결금액에 곱하는 소수(예: 0.35% → 0.0035).
    """
    try:
        import config as _cfg

        use_mock = bool(getattr(_cfg, "USE_MOCK", True))
    except Exception:
        use_mock = True
    try:
        if use_mock:
            bp = float(
                settings.get("buy_fee_mock", _FALLBACK_MOCK_BUY_PCT)
            )
            sp = float(
                settings.get("sell_fee_mock", _FALLBACK_MOCK_SELL_PCT)
            )
        else:
            bp = float(
                settings.get(
                    "buy_fee_live",
                    settings.get("buy_fee_bt", _FALLBACK_LIVE_BUY_PCT),
                )
            )
            sp = float(
                settings.get(
                    "sell_fee_live",
                    settings.get("sell_fee_bt", _FALLBACK_LIVE_SELL_PCT),
                )
            )
    except (TypeError, ValueError):
        bp, sp = (
            (_FALLBACK_MOCK_BUY_PCT, _FALLBACK_MOCK_SELL_PCT)
            if use_mock
            else (_FALLBACK_LIVE_BUY_PCT, _FALLBACK_LIVE_SELL_PCT)
        )
    return bp / 100.0, sp / 100.0


def unrealized_at_price_net_of_sell_fee(
    pos: Mapping[str, Any],
    curr_p: float,
    qty: int,
    avg_price: float,
    settings: Mapping[str, Any],
) -> float:
    """
    전량 매도 가정 시 **매도 수수료를 뺀 매수대(invested 우선)** 대비 미실현 손익(원).

    포지션 관리 탭(`VirtualTab._unrealized_at_price_net_of_sell_fee`)과 동일 식 —
    종합 리포트 등 다른 모듈에서도 재사용합니다.
    """
    try:
        q = int(qty or 0)
    except (TypeError, ValueError):
        q = 0
    try:
        px = float(curr_p)
    except (TypeError, ValueError):
        px = 0.0
    if q <= 0 or px <= 0:
        return 0.0
    try:
        _, sell_r = fee_decimal_rates_live_trading(settings)
        sr = float(sell_r)
    except Exception:
        try:
            _, sell_r = fee_decimal_rates_live_trading({})
            sr = float(sell_r)
        except Exception:
            sr = 0.005
    try:
        sr = max(0.0, min(float(sr), 0.5))
    except Exception:
        sr = 0.005
    try:
        inv = float(pos.get("invested", 0) or 0)
    except (TypeError, ValueError):
        inv = 0.0
    if inv <= 1e-9:
        try:
            ap = float(avg_price or 0)
        except (TypeError, ValueError):
            ap = 0.0
        inv = max(0.0, ap * float(q))
    gross = px * float(q)
    net_proceeds = gross * (1.0 - sr)
    return float(net_proceeds - inv)


def fee_decimal_rates_backtest(settings: Mapping[str, Any]) -> Tuple[float, float]:
    """백테스트(SimpleBacktester, DynamicBacktester): buy_fee_bt, sell_fee_bt."""
    try:
        bp = float(settings.get("buy_fee_bt", settings.get("buy_fee_rate", 0.015)))
        sp = float(settings.get("sell_fee_bt", settings.get("sell_fee_rate", 0.215)))
    except (TypeError, ValueError):
        bp, sp = 0.015, 0.215
    return bp / 100.0, sp / 100.0
