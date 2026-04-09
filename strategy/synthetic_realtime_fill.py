# -*- coding: utf-8 -*-
"""
백테스트 시 체결강도·호가비율 컬럼이 없을 때의 결측 보간.
고정 계수만 사용하며 JSON 저장·온라인 튜닝(자가학습)은 없음.
"""
from __future__ import annotations

import numpy as np

# 기존 SimulatedRealtimeMetrics 기본 파라미터와 동일 (학습 제거)
_VP_MULT = 5000.0
_VP_BIAS = 100.0
_BA_MULT = 10.0
_BA_BIAS = 1.0


def synthesize_vol_power_vectorized(
    price_changes: np.ndarray,
    vol_ratios: np.ndarray,
) -> np.ndarray:
    """가격 변화·거래량 비율로 체결강도 후보를 벡터 생성 (30~500 클램프)."""
    try:
        base_vps = _VP_BIAS + np.asarray(price_changes, dtype=np.float64) * _VP_MULT
        pc = np.asarray(price_changes, dtype=np.float64)
        vr = np.asarray(vol_ratios, dtype=np.float64)
        base_vps = base_vps + np.where((vr > 1.5) & (pc > 0), 20.0, 0.0)
        return np.clip(base_vps, 30.0, 500.0)
    except Exception:
        return np.full(np.asarray(price_changes).shape, 100.0, dtype=np.float64)


def synthesize_bid_ask_ratio_vectorized(
    price_changes: np.ndarray,
    vol_ratios: np.ndarray,
    vol_powers: np.ndarray | None = None,
) -> np.ndarray:
    """매도잔량/매수잔량 비(호가 비율) 후보를 벡터 생성 (0.3~3.0 클램프)."""
    try:
        pc = np.asarray(price_changes, dtype=np.float64)
        vr = np.asarray(vol_ratios, dtype=np.float64)
        base_ratios = _BA_BIAS + pc * _BA_MULT
        base_ratios = base_ratios + np.where(
            (vr > 2.0) & (pc > 0),
            0.3,
            np.where((vr > 2.0) & (pc <= 0), -0.3, 0.0),
        )
        if vol_powers is not None:
            vp = np.asarray(vol_powers, dtype=np.float64)
            base_ratios = base_ratios + np.where(
                vp > 150, 0.2, np.where(vp < 70, -0.2, 0.0)
            )
        return np.clip(base_ratios, 0.3, 3.0)
    except Exception:
        return np.full(np.asarray(price_changes).shape, 1.0, dtype=np.float64)
