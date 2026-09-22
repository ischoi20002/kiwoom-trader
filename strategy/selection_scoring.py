# -*- coding: utf-8 -*-
"""선정 스코어·일봉 지표 헬퍼 (RSI / strategy score / band scorers).

``strategy_selector`` 에서 분리. 기존 이름은 strategy_selector re-export 로 호환.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


def calculate_rsi(series, period=14):
    """
    RSI(Relative Strength Index)를 계산합니다.
    """
    delta = series.diff()
    up, down = delta.clip(lower=0), -1 * delta.clip(upper=0)
    # ewm을 사용한 매끄러운 계산 (키움/시스템 트레이딩 방식)
    ema_up = up.ewm(com=period-1, adjust=False).mean()
    ema_down = down.ewm(com=period-1, adjust=False).mean()
    rs = ema_up / (ema_down + 1e-10)
    return 100 - (100 / (1+rs))

# =============================================================================
# [v18.5] 공용 전략 로직 (대시보드 & 백테스팅 공용)
# =============================================================================

def calculate_technical_indicators(df, settings):
    """
    공통 기술적 지표 계산 (MA, RSI, MACD, BB)
    df는 최소한 'close', 'volume' 컬럼을 포함해야 함 (소문자 기준)
    """
    # 기본 이평선
    df['MA5'] = df['close'].rolling(5).mean()
    df['MA20'] = df['close'].rolling(20).mean()
    df['MA60'] = df['close'].rolling(60).mean()
    
    
    # 거래대금
    df['TrdeVal'] = df['close'] * df['volume']
    
    # RSI
    df['RSI'] = calculate_rsi(df['close'], 14)
    
    # MACD
    m_f = int(settings.get('macd_fast_daily', 12))
    m_s = int(settings.get('macd_slow_daily', 26))
    m_sg = int(settings.get('macd_sig_daily', 9))
    df['EMA_Fast'] = df['close'].ewm(span=m_f, adjust=False).mean()
    df['EMA_Slow'] = df['close'].ewm(span=m_s, adjust=False).mean()
    df['MACD'] = df['EMA_Fast'] - df['EMA_Slow']
    df['MACD_Sig'] = df['MACD'].ewm(span=m_sg, adjust=False).mean()
    df['OSC'] = df['MACD'] - df['MACD_Sig']
    
    return df

def get_strategy_score(row, settings):
    """
    [Backtest v30.0 Priority] 단일 행(시점)에 대한 전략 점수 및 필터링 결과 반환
    """
    # 1. 환경 변수 로드
    min_억 = float(settings.get('min_trde_val', 40.0))
    conv_limit = float(settings.get('ma_convergence', 5.0)) / 100.0
    sel_rsi_min = float(settings.get('sel_rsi_min', 30.0))
    sel_rsi_max = float(settings.get('sel_rsi_max', 70.0))
    
    # 2. 필터링 판정 (Status Tracking)
    res = {"valid": False, "score": 0.0, "details": {}}
    
    # 2-1. 거래대금 (5일 평균 사용 여부는 호출부에서 판단하도록 유연성 부여, 여기선 row값 기준)
    # 실제 select_monitoring_targets에서는 tail(5).mean()을 사용하므로, 
    # row에 이미 계산된 'AvgVal5'가 있다고 가정하거나 직접 전달받아야 함.
    avg_val = row.get('AvgVal5', row.get('TrdeVal', 0))
    c1 = avg_val >= (min_억 * 100000000)
    
    # 2-2. 이평 수렴도 (Dual-Track Selection Logic v30.9)
    diff_rate = abs(row['MA5'] - row['MA20']) / row['MA20'] if row['MA20'] > 0 else 1.0
    c2_converged = (diff_rate <= conv_limit) if settings.get('use_sel_track_a', True) else False
    c2_trending = ((row['MA20'] > row['MA60']) and (row['close'] > row['MA20'])) if settings.get('use_sel_track_b', True) else False
    c2 = c2_converged or c2_trending
    
    # 2-3. RSI 범위
    c3 = (sel_rsi_min <= row['RSI'] <= sel_rsi_max)
    
    # 2-4. 거래량 분출 (VolBurst10 등 호출부 전달 필요)
    c4 = row.get('VolBurstActive', False)
    
    # 2-5. MACD/OSC 필터 (Hybrid v26.15: 양수 필터 + 우상향 가산점)
    osc_prev = row.get('OSC_Prev', row.get('OSC', 0))
    # 2-5. 일봉 MACD 필터 (OSC 양수 고정)
    c5 = (row['OSC'] > 0) 
    
    if not (c1 and c2 and c3 and c4 and c5):
        return res

    # 3. 점수 산정
    bonus = 0.0
    if row['OSC'] > osc_prev: bonus += 10.0   # 우상향 시 가산점
    
    avg_val_억 = avg_val / 100000000
    s1 = min(100, (row['RSI'] / 70) * 100) # 탄성 (RSI가 높을수록 가점)
    s2 = max(0, (1 - (diff_rate / conv_limit)) * 100) # 밀집도 (수렴할수록 가점)
    s3 = min(100, (avg_val_억 / 500) * 100) # 대금 가점 (500억 기준)
    
    pos_diff = abs(row['close'] - row['MA20']) / row['MA20'] if row['MA20'] > 0 else 1.0
    s4 = max(0, (1 - (pos_diff / 0.03)) * 100) # 20일선 근접도 (3%이내 가점)
    
    total_score = round((s2 * 0.4) + (s1 * 0.3) + (s3 * 0.2) + (s4 * 0.1) + bonus, 1)
    
    res.update({"valid": True, "score": total_score, "pos_diff": pos_diff})
    return res


def _add_vol_burst_ab_columns(df_valid: pd.DataFrame, settings: dict) -> pd.DataFrame:
    """
    prepare_dynamic_universe 와 동일: Track A/B 각각 거래량 분출(배율·룩백 분리).
    """
    vol_ratio_a = float(settings.get("vol_ratio", 1.5))
    lookback_a = int(settings.get("vol_lookback", 10))
    df_valid = df_valid.copy()
    df_valid["VolRatio"] = df_valid["volume"] / df_valid["volume"].shift(1)
    df_valid["VolBurst_A"] = (df_valid["VolRatio"] >= vol_ratio_a) & (
        df_valid["close"] > df_valid["close"].shift(1)
    )
    df_valid["VolBurstActive_A"] = df_valid["VolBurst_A"].rolling(lookback_a).max().astype(bool)
    vol_ratio_b = float(settings.get("sel_b_vol_ratio", vol_ratio_a))
    lookback_b = int(settings.get("sel_b_vol_lookback", lookback_a))
    df_valid["VolBurst_B"] = (df_valid["VolRatio"] >= vol_ratio_b) & (
        df_valid["close"] > df_valid["close"].shift(1)
    )
    df_valid["VolBurstActive_B"] = df_valid["VolBurst_B"].rolling(lookback_b).max().astype(bool)
    return df_valid


def _settings_bool_sel(settings: dict, key: str, default: bool = False) -> bool:
    try:
        v = settings.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return int(v) != 0
        if isinstance(v, str):
            t = v.strip().lower()
            if t in ("true", "1", "yes", "on"):
                return True
            if t in ("false", "0", "no", "off", ""):
                return False
        return bool(v) if v is not None else default
    except Exception:
        return default


def _sel_b_ma5_close_mult(settings: dict) -> float:
    """
    Track B ``종가 ≥ MA5`` 토글 ON일 때 ``lc >= MA5 * 반환값`` 에 쓰는 배율.
    ``(1 - sel_b_ma5_eps_pct) + sel_b_ma5_min_excess_ratio`` — 후자가 0이면 기존과 동일.
    (v2 다른 필터만으로는 이미 MA5 근처 조건이 충족되는 경우가 많아, 추가 이격으로 체감 가능.)
    """
    try:
        eps = float(settings.get("sel_b_ma5_eps_pct", 0.005))
        ex = float(settings.get("sel_b_ma5_min_excess_ratio", 0.0))
    except (TypeError, ValueError):
        eps, ex = 0.005, 0.0
    try:
        return max(1e-9, (1.0 - eps) + ex)
    except Exception:
        return max(1e-9, 1.0 - 0.005)


def _sel_score_mode(settings: Optional[dict]) -> str:
    """선정 Score 모드: ``legacy`` | ``fwd_aligned_v1`` | ``s1_pnl_aligned_v1`` | ``b_s1_pnl_aligned_v1``."""
    try:
        m = str((settings or {}).get("sel_score_mode", "legacy") or "legacy").strip().lower()
        if m in ("fwd_aligned_v1", "fwd_aligned", "aligned"):
            return "fwd_aligned_v1"
        if m in ("b_s1_pnl_aligned_v1", "b_only_s1", "b_s1_aligned"):
            return "b_s1_pnl_aligned_v1"
        if m in ("s1_pnl_aligned_v1", "s1_pnl", "s1_aligned"):
            return "s1_pnl_aligned_v1"
        return "legacy"
    except Exception:
        return "legacy"


def _use_score_a_s1_pnl_aligned(mode: str) -> bool:
    """Track A에 S1손익 정렬 스코어를 쓸지."""
    return mode == "s1_pnl_aligned_v1"


def _use_score_b_s1_pnl_aligned(mode: str) -> bool:
    """Track B에 S1손익 정렬 스코어를 쓸지 (B-only 모드 포함)."""
    return mode in ("s1_pnl_aligned_v1", "b_s1_pnl_aligned_v1")


def _sel_s1_pnl_signs(settings: Optional[dict]) -> Dict[str, float]:
    """S1 손익 정렬용 피처 부호 (+1=높을수록 유리, -1=낮을수록 유리)."""
    try:
        raw = (settings or {}).get("sel_s1_pnl_signs")
        if isinstance(raw, dict) and raw:
            out: Dict[str, float] = {}
            for k, v in raw.items():
                try:
                    out[str(k)] = float(v)
                except Exception:
                    out[str(k)] = 0.0
            return out
    except Exception:
        pass
    return {}


def _signed_band_score(val: Any, lo: float, hi: float, sign: float) -> float:
    """sign>0: 높을수록 고점, sign<0: 낮을수록 고점, sign=0: 중립 50."""
    try:
        s = float(sign)
    except Exception:
        s = 0.0
    if abs(s) < 1e-12:
        return 50.0
    if s > 0:
        return _band_high_is_better(val, lo, hi)
    return _band_low_is_better(val, lo, hi)


def _score_a_s1_pnl_aligned_series(df: pd.DataFrame, settings: dict) -> pd.Series:
    """Valid_A 순위 — S1 체결 손익 상관 부호(sel_s1_pnl_signs) 반영."""
    try:
        signs = _sel_s1_pnl_signs(settings)
        if not signs or all(abs(float(v)) < 1e-12 for v in signs.values()):
            return _score_a_fwd_aligned_series(df, settings)
        pb_lo = float(settings.get("sel_a_pb_min_pct", 5.0))
        pb_hi = float(settings.get("sel_a_pb_max_pct", 16.0))
        atr_lo = float(settings.get("sel_a_atr_pct_min", 0.01))
        atr_hi = float(settings.get("sel_a_atr_pct_max", 0.085))
        rk_lo = float(settings.get("sel_a_rs_rank_min", 0.35))
        rk_hi = float(settings.get("sel_a_rs_rank_max", 0.8))
        parts: List[pd.Series] = []
        weights: List[float] = []

        def _add(series: pd.Series, w: float) -> None:
            parts.append(series.fillna(50.0))
            weights.append(abs(float(w)))

        if abs(float(signs.get("a_pb", 0))) > 0:
            pb = pd.to_numeric(df.get("_sel_pb_pct"), errors="coerce")
            _add(
                pb.apply(lambda x: _signed_band_score(x, pb_lo, pb_hi, signs.get("a_pb", 0))),
                0.30,
            )
        if abs(float(signs.get("a_atr", 0))) > 0:
            atr = pd.to_numeric(df.get("_sel_atr_pct"), errors="coerce")
            _add(
                atr.apply(lambda x: _signed_band_score(x, atr_lo, atr_hi, signs.get("a_atr", 0))),
                0.20,
            )
        if abs(float(signs.get("a_ma_diff", 0))) > 0 and "MA_Diff" in df.columns:
            conv = max(float(settings.get("ma_convergence", 5.0)), 1e-6)
            md = pd.to_numeric(df["MA_Diff"], errors="coerce").abs()
            # sign+: 큰 MA_Diff 유리 → md/conv, sign-: 작은 유리
            if float(signs.get("a_ma_diff", 0)) > 0:
                ma_s = (md / conv).clip(0.0, 1.0) * 100.0
            else:
                ma_s = (1.0 - (md / conv)).clip(0.0, 1.0) * 100.0
            _add(ma_s, 0.20)
        if abs(float(signs.get("a_liq", 0))) > 0:
            if "AvgVal5" in df.columns:
                liq = pd.to_numeric(df["AvgVal5"], errors="coerce").fillna(0.0)
                liq_s = (liq / 50000000000.0 * 100.0).clip(upper=100.0)
            else:
                liq = pd.to_numeric(df.get("AvgTrdeVal", 0), errors="coerce").fillna(0.0)
                liq_s = (liq / 500.0 * 100.0).clip(upper=100.0)
            if float(signs.get("a_liq", 0)) < 0:
                liq_s = 100.0 - liq_s
            _add(liq_s, 0.15)
        if abs(float(signs.get("a_rs", 0))) > 0 and "_sel_rs_pct" in df.columns:
            rs = pd.to_numeric(df["_sel_rs_pct"], errors="coerce")
            _add(
                rs.apply(lambda x: _signed_band_score(x, rk_lo, rk_hi, signs.get("a_rs", 0))),
                0.15,
            )
        if not parts:
            return _score_a_fwd_aligned_series(df, settings)
        wsum = sum(weights) or 1.0
        sc = sum(p * (w / wsum) for p, w in zip(parts, weights))
        return sc.fillna(0.0).clip(0.0, 100.0)
    except Exception:
        return pd.Series(0.0, index=df.index)


def _score_b_s1_pnl_aligned_series(df: pd.DataFrame, settings: dict) -> pd.Series:
    """Valid_B 순위 — S1 체결 손익 상관 부호 반영."""
    try:
        signs = _sel_s1_pnl_signs(settings)
        if not signs or all(abs(float(v)) < 1e-12 for v in signs.values()):
            return _score_b_fwd_aligned_series(df, settings)
        hi_lo = float(settings.get("sel_b_hi_min_pct", 92.0))
        hi_hi = float(settings.get("sel_b_hi_max_pct", 99.7))
        atr_lo = float(settings.get("sel_b_atr_pct_min", 0.005))
        atr_hi = float(settings.get("sel_b_atr_pct_max", 0.09))
        parts: List[pd.Series] = []
        weights: List[float] = []

        def _add(series: pd.Series, w: float) -> None:
            parts.append(series.fillna(50.0))
            weights.append(abs(float(w)))

        if abs(float(signs.get("b_rs", 0))) > 0 and "_sel_b_rs_pct" in df.columns:
            rs = pd.to_numeric(df["_sel_b_rs_pct"], errors="coerce").fillna(0.0)
            if float(signs.get("b_rs", 0)) > 0:
                rs_s = rs * 100.0
            else:
                rs_s = (1.0 - rs) * 100.0
            _add(rs_s, 0.30)
        if abs(float(signs.get("b_hi", 0))) > 0:
            hip = pd.to_numeric(df.get("_sel_b_hi_pct"), errors="coerce")
            _add(
                hip.apply(lambda x: _signed_band_score(x, hi_lo, hi_hi, signs.get("b_hi", 0))),
                0.25,
            )
        if abs(float(signs.get("b_atr", 0))) > 0:
            atr = pd.to_numeric(df.get("_sel_b_atr_pct"), errors="coerce")
            _add(
                atr.apply(lambda x: _signed_band_score(x, atr_lo, atr_hi, signs.get("b_atr", 0))),
                0.15,
            )
        if abs(float(signs.get("b_liq", 0))) > 0:
            if "AvgVal5" in df.columns:
                liq = pd.to_numeric(df["AvgVal5"], errors="coerce").fillna(0.0)
                liq_s = (liq / 50000000000.0 * 100.0).clip(upper=100.0)
            else:
                liq = pd.to_numeric(df.get("AvgTrdeVal", 0), errors="coerce").fillna(0.0)
                liq_s = (liq / 500.0 * 100.0).clip(upper=100.0)
            if float(signs.get("b_liq", 0)) < 0:
                liq_s = 100.0 - liq_s
            _add(liq_s, 0.15)
        if abs(float(signs.get("b_osc", 0))) > 0:
            osc = pd.to_numeric(df.get("OSC", 0), errors="coerce").fillna(0.0)
            # 교차섹션 분위
            try:
                osc_pct = osc.rank(pct=True)
            except Exception:
                osc_pct = (osc > 0).astype(float)
            if float(signs.get("b_osc", 0)) > 0:
                osc_s = osc_pct * 100.0
            else:
                osc_s = (1.0 - osc_pct) * 100.0
            _add(osc_s, 0.15)
        if not parts:
            return _score_b_fwd_aligned_series(df, settings)
        wsum = sum(weights) or 1.0
        sc = sum(p * (w / wsum) for p, w in zip(parts, weights))
        return sc.fillna(0.0).clip(0.0, 100.0)
    except Exception:
        return pd.Series(0.0, index=df.index)


def _band_low_is_better(val: Any, lo: float, hi: float) -> float:
    """밴드 [lo,hi]에서 낮을수록 100점에 가깝게."""
    try:
        x = float(val)
        span = max(float(hi) - float(lo), 1e-9)
        return float(np.clip((float(hi) - x) / span, 0.0, 1.0) * 100.0)
    except Exception:
        return 0.0


def _band_high_is_better(val: Any, lo: float, hi: float) -> float:
    """밴드 [lo,hi]에서 높을수록 100점에 가깝게."""
    try:
        x = float(val)
        span = max(float(hi) - float(lo), 1e-9)
        return float(np.clip((x - float(lo)) / span, 0.0, 1.0) * 100.0)
    except Exception:
        return 0.0


def _score_a_fwd_aligned_series(df: pd.DataFrame, settings: dict) -> pd.Series:
    """
    Valid_A 후보 순위용 — 선행수익과 부호가 맞게(얕은 되돌림·낮은 ATR·낮은 MA갭·유동성).
    Valid 게이트는 호출 측에서 적용.
    """
    try:
        pb_lo = float(settings.get("sel_a_pb_min_pct", 5.0))
        pb_hi = float(settings.get("sel_a_pb_max_pct", 16.0))
        atr_lo = float(settings.get("sel_a_atr_pct_min", 0.01))
        atr_hi = float(settings.get("sel_a_atr_pct_max", 0.085))
        pb = pd.to_numeric(df.get("_sel_pb_pct"), errors="coerce")
        atr = pd.to_numeric(df.get("_sel_atr_pct"), errors="coerce")
        pb_s = ((pb_hi - pb) / max(pb_hi - pb_lo, 1e-9)).clip(0.0, 1.0) * 100.0
        atr_s = ((atr_hi - atr) / max(atr_hi - atr_lo, 1e-9)).clip(0.0, 1.0) * 100.0
        if "MA_Diff" in df.columns:
            conv = max(float(settings.get("ma_convergence", 5.0)), 1e-6)
            md = pd.to_numeric(df["MA_Diff"], errors="coerce").abs()
            ma_s = (1.0 - (md / conv)).clip(0.0, 1.0) * 100.0
        else:
            ma_s = pd.Series(50.0, index=df.index)
        if "AvgVal5" in df.columns:
            liq = pd.to_numeric(df["AvgVal5"], errors="coerce").fillna(0.0)
            liq_s = (liq / 50000000000.0 * 100.0).clip(upper=100.0)
        else:
            liq = pd.to_numeric(df.get("AvgTrdeVal", 0), errors="coerce").fillna(0.0)
            liq_s = (liq / 500.0 * 100.0).clip(upper=100.0)
        sc = pb_s * 0.40 + atr_s * 0.20 + ma_s * 0.20 + liq_s * 0.20
        return sc.fillna(0.0).clip(0.0, 100.0)
    except Exception:
        return pd.Series(0.0, index=df.index)


def _score_b_fwd_aligned_series(df: pd.DataFrame, settings: dict) -> pd.Series:
    """
    Valid_B 후보 순위용 — RS 추격 비중을 뒤집고(낮은 RS% 우대),
    고점 근접·유동성·OSC·낮은 ATR을 가점.
    """
    try:
        hi_lo = float(settings.get("sel_b_hi_min_pct", 92.0))
        hi_hi = float(settings.get("sel_b_hi_max_pct", 99.7))
        atr_lo = float(settings.get("sel_b_atr_pct_min", 0.005))
        atr_hi = float(settings.get("sel_b_atr_pct_max", 0.09))
        rs = pd.to_numeric(df.get("_sel_b_rs_pct"), errors="coerce").fillna(0.0)
        rs_inv = (1.0 - rs) * 100.0
        hip = pd.to_numeric(df.get("_sel_b_hi_pct"), errors="coerce")
        hi_s = ((hip - hi_lo) / max(hi_hi - hi_lo, 1e-9)).clip(0.0, 1.0) * 100.0
        atr = pd.to_numeric(df.get("_sel_b_atr_pct"), errors="coerce")
        atr_s = ((atr_hi - atr) / max(atr_hi - atr_lo, 1e-9)).clip(0.0, 1.0) * 100.0
        if "AvgVal5" in df.columns:
            liq = pd.to_numeric(df["AvgVal5"], errors="coerce").fillna(0.0)
            liq_s = (liq / 50000000000.0 * 100.0).clip(upper=100.0)
        else:
            liq = pd.to_numeric(df.get("AvgTrdeVal", 0), errors="coerce").fillna(0.0)
            liq_s = (liq / 500.0 * 100.0).clip(upper=100.0)
        osc = pd.to_numeric(df.get("OSC", 0), errors="coerce").fillna(0.0)
        if "OSC_Prev" in df.columns:
            osc_p = pd.to_numeric(df["OSC_Prev"], errors="coerce").fillna(osc)
            osc_s = (osc > osc_p).astype(float) * 70.0 + (osc > 0).astype(float) * 30.0
        else:
            osc_s = (osc > 0).astype(float) * 100.0
        sc = rs_inv * 0.40 + hi_s * 0.25 + atr_s * 0.15 + liq_s * 0.10 + osc_s * 0.10
        return sc.fillna(0.0).clip(0.0, 100.0)
    except Exception:
        return pd.Series(0.0, index=df.index)

