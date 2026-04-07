# -*- coding: utf-8 -*-
"""
시장 4단계 레짐 (DOWN / NEUTRAL / UP / AGGRESSIVE) 단일 정의.

DynamicBacktester·SimpleBacktester·리포트·실시간 UI가 동일 분기·settings 임계값을 사용한다.
"""
from __future__ import annotations

from typing import Any, Mapping

import numpy as np
import pandas as pd


def _thresholds(settings: Mapping[str, Any] | None) -> tuple[float, float, float, float, float]:
    """settings에서 MSI/ADX/VIX 임계값 로드 (파싱 실패 시 백테·다이나믹과 동일한 기본값)."""
    s = settings or {}
    try:
        msi_down = float(s.get("msi_down_threshold", -0.10))
        msi_up = float(s.get("msi_up_threshold", 0.10))
        adx_min = float(s.get("adx_min_threshold", 15))
        adx_down = float(s.get("adx_down_threshold", 20))
        vix_down = float(s.get("vix_down_threshold", 30))
        return msi_down, msi_up, adx_min, adx_down, vix_down
    except (TypeError, ValueError):
        return -0.10, 0.10, 15.0, 20.0, 30.0


def classify_market_regime_row(
    msi: float,
    adx: float,
    v_val: float,
    settings: Mapping[str, Any] | None,
) -> str:
    """
    단일 시점 MSI/ADX/VIX로 4단계 레짐 문자열 반환.
    dynamic_backtest.py 의 v4.7 분기와 동일.
    """
    msi_down, msi_up, adx_min, adx_down, vix_down = _thresholds(settings)
    try:
        m = float(msi)
        a = float(adx)
        v = float(v_val)
    except (TypeError, ValueError):
        return "UP"
    try:
        if m < msi_down or v > vix_down or (m < 0 and a > adx_down):
            return "DOWN"
        if a < adx_min and m <= msi_up:
            return "NEUTRAL"
        if m > msi_up and a > adx_down:
            return "AGGRESSIVE"
        return "UP"
    except Exception:
        return "UP"


def classify_market_regime_series(
    msi_s: pd.Series,
    adx_s: pd.Series,
    vix_s: pd.Series,
    settings: Mapping[str, Any] | None,
) -> pd.Series:
    """일별 시계열에 벡터화 적용 (인덱스는 msi_s와 동일)."""
    msi_down, msi_up, adx_min, adx_down, vix_down = _thresholds(settings)
    try:
        m = pd.to_numeric(msi_s, errors="coerce").astype(float)
        a = pd.to_numeric(adx_s, errors="coerce").astype(float)
        v = pd.to_numeric(vix_s, errors="coerce").astype(float)
        m = m.fillna(0.0)
        a = a.fillna(0.0)
        v = v.fillna(15.0)
        mask_down = (m < msi_down) | (v > vix_down) | ((m < 0) & (a > adx_down))
        mask_neutral = (~mask_down) & (a < adx_min) & (m <= msi_up)
        mask_aggr = (~mask_down) & (~mask_neutral) & (m > msi_up) & (a > adx_down)
        out = np.where(mask_down, "DOWN", np.where(mask_neutral, "NEUTRAL", np.where(mask_aggr, "AGGRESSIVE", "UP")))
        return pd.Series(out, index=msi_s.index, dtype=object)
    except Exception:
        return pd.Series("UP", index=msi_s.index, dtype=object)


def regime_to_trend_status(regime: str | None) -> tuple[str, str]:
    """
    derive_market_regime_scalar(Market_Trend, Market_Status)와 역호환되는 (Trend, Status).
    """
    try:
        r = str(regime or "UP").strip().upper()
    except Exception:
        return "UP", "SAFE"
    if r == "DOWN":
        return "DOWN", "RISK"
    if r == "NEUTRAL":
        return "NEUTRAL", "WATCH"
    if r == "AGGRESSIVE":
        return "UP", "AGGRESSIVE"
    return "UP", "SAFE"


def regime_display_tag(regime: str | None) -> str:
    """상단 티커 [태그]. 백테 4단계 레짐 코드가 그대로 보이도록 순풍/역풍 3단계 문구는 쓰지 않는다."""
    try:
        r = str(regime or "").strip().upper()
    except Exception:
        return "[확인중]"
    return {
        "DOWN": "[역풍]",
        "NEUTRAL": "[중립]",
        "UP": "[UP]",
        "AGGRESSIVE": "[강세]",
    }.get(r, "[확인중]")


def regime_display_emoji(regime: str | None) -> str:
    """로그 한 줄 요약용."""
    try:
        r = str(regime or "").strip().upper()
    except Exception:
        return "⚪확인중"
    return {
        "DOWN": "⚠️역풍",
        "NEUTRAL": "⚪중립",
        "UP": "🟢UP",
        "AGGRESSIVE": "🔥강세",
    }.get(r, "⚪확인중")


def regime_reason_korean(
    regime: str,
    msi: float,
    adx: float,
    vix: float,
    index_name: str = "",
) -> str:
    """지수별 상세 reason (설정 임계값은 문자열에 숫자만 반영)."""
    try:
        nm = f" ({index_name})" if index_name else ""
        r = str(regime).strip().upper()
        if r == "DOWN":
            return f"4단계 레짐: 역풍{nm} — MSI {msi:+.2f} / ADX {adx:.1f} / VIX {vix:.1f}"
        if r == "NEUTRAL":
            return f"4단계 레짐: 중립{nm} — 횡보·Type B 제한 구간 (MSI {msi:+.2f}, ADX {adx:.1f})"
        if r == "AGGRESSIVE":
            return f"4단계 레짐: 공격적 강세{nm} — MSI·ADX 동시 충족 (MSI {msi:+.2f}, ADX {adx:.1f})"
        if r == "UP":
            return f"4단계 레짐: UP(일반 강세){nm} — (MSI {msi:+.2f}, ADX {adx:.1f}, VIX {vix:.1f})"
        return f"4단계 레짐: UP{nm} — (MSI {msi:+.2f}, ADX {adx:.1f}, VIX {vix:.1f})"
    except Exception:
        return "시장 레짐 판정"
