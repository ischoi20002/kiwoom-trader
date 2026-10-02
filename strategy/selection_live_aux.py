# -*- coding: utf-8 -*-
"""선정 라이브 cross-section·슬롯밴드·듀얼트랙 병합.

``strategy_selector`` 에서 분리. 기존 이름은 strategy_selector re-export 로 호환.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from strategy.box_reversion_selector import (
    BoxReversionSelectionConfig,
    evaluate_box_reversion_candidate,
)
from strategy.selection_scoring import (
    _add_vol_burst_ab_columns,
    _band_high_is_better,
    _band_low_is_better,
    _score_a_fwd_aligned_series,
    _score_a_s1_pnl_aligned_series,
    _score_b_fwd_aligned_series,
    _score_b_s1_pnl_aligned_series,
    _sel_b_ma5_close_mult,
    _sel_s1_pnl_signs,
    _sel_score_mode,
    _settings_bool_sel,
    _signed_band_score,
    _use_score_a_s1_pnl_aligned,
    _use_score_b_s1_pnl_aligned,
    calculate_rsi,
    calculate_technical_indicators,
    get_strategy_score,
)


def _live_liq_part(df: pd.DataFrame) -> pd.Series:
    """
    점수 유동성 항목 (백테 배치와 동일: 5일 평균 대금 / 500억, 상한 100, ×0.12).

    원 단위 ``_sel_avg_val5`` 우선, 없으면 억 단위 ``AvgTrdeVal`` 폴백.
    """
    try:
        if "_sel_avg_val5" in df.columns:
            won = pd.to_numeric(df["_sel_avg_val5"], errors="coerce")
            if "AvgTrdeVal" in df.columns:
                won = won.fillna(
                    pd.to_numeric(df["AvgTrdeVal"], errors="coerce") * 100000000.0
                )
        else:
            won = pd.to_numeric(df.get("AvgTrdeVal", 0), errors="coerce") * 100000000.0
        won = pd.Series(won, index=df.index).fillna(0.0)
        return (won / 50000000000.0 * 100.0).clip(upper=100.0) * 0.12
    except Exception:
        return pd.Series(0.0, index=df.index)


def _live_osc_rise_bonus(df: pd.DataFrame) -> pd.Series:
    """OSC 가 전일보다 상승했을 때만 +10 (백테 배치와 동일, 결측은 0)."""
    try:
        osc = pd.to_numeric(df.get("OSC"), errors="coerce")
        osc_p = (
            pd.to_numeric(df["OSC_Prev"], errors="coerce")
            if "OSC_Prev" in df.columns
            else osc
        )
        return (pd.Series(osc, index=df.index) > pd.Series(osc_p, index=df.index)).astype(float) * 10.0
    except Exception:
        return pd.Series(0.0, index=df.index)


def _compute_sel_a_aux_from_daily_tail(
    df_valid: pd.DataFrame,
    last_row: pd.Series,
    settings: dict,
) -> Optional[dict]:
    """
    실시간 선정용: 일봉 꼬리에서 Track A v2 지표(상대강도 원시·되돌림·ATR%·갭·이평근접)를 계산한다.
    교차섹션 순위는 호출부에서 부여한다.
    """
    try:
        min_b = max(30, int(float(settings.get("sel_a_min_history_bars", 65))))
        if df_valid is None or len(df_valid) < min_b:
            return None
        rs_n = max(1, int(float(settings.get("sel_a_rs_lookback", 5))))
        pb_n = max(5, int(float(settings.get("sel_a_pb_lookback", 20))))
        atr_w = max(5, int(float(settings.get("sel_a_atr_period", 14))))
        need = max(rs_n + 2, pb_n + 1, atr_w + 2, min_b)
        if len(df_valid) < need:
            return None

        dfv = df_valid.copy()
        if "high" not in dfv.columns or dfv["high"].isna().all():
            dfv["high"] = dfv["close"]
        if "low" not in dfv.columns or dfv["low"].isna().all():
            dfv["low"] = dfv["close"]
        if "open" not in dfv.columns:
            dfv["open"] = dfv["close"]

        lc = float(last_row["close"] or 0)
        if lc <= 0:
            return None

        cl_rs = float(dfv["close"].iloc[-(rs_n + 1)])
        if cl_rs <= 0:
            return None
        ret5_raw = (lc / cl_rs - 1.0) * 100.0

        hh = float(dfv["high"].tail(pb_n).max())
        if hh <= 0:
            return None
        pb_pct = (hh - lc) / hh * 100.0

        prev_c = dfv["close"].shift(1)
        tr = np.maximum(
            dfv["high"] - dfv["low"],
            np.maximum((dfv["high"] - prev_c).abs(), (dfv["low"] - prev_c).abs()),
        )
        atr = float(tr.tail(atr_w).mean())
        atr_pct = atr / lc if lc > 0 else 0.0

        o0 = float(last_row.get("open", lc) or lc)
        pc1 = float(dfv["close"].iloc[-2])
        gap_pct = ((o0 - pc1) / pc1 * 100.0) if pc1 > 1e-9 else 0.0

        ma5 = float(last_row.get("MA5", 0) or 0)
        ma20 = float(last_row.get("MA20", 0) or 0)
        m20n = float(settings.get("sel_a_ma20_near_pct", 0.045))
        eps = float(settings.get("sel_a_ma5_eps_pct", 0.004))
        near20 = ma20 > 0 and abs(lc / ma20 - 1.0) <= m20n
        if _settings_bool_sel(settings, "sel_a_require_close_ge_ma5", True):
            ge5 = ma5 > 0 and lc >= ma5 * (1.0 - eps)
            ma_ok = bool(near20 or ge5)
        else:
            ma_ok = bool(near20)

        min_억 = float(settings.get("min_trde_val", 40.0)) * 100000000
        avg5 = float(last_row.get("AvgVal5", 0) or 0)
        min_liq_ok = avg5 >= min_억

        osc = float(last_row.get("OSC", 0) or 0)
        osc_ok = (osc > 0) if _settings_bool_sel(settings, "sel_a_require_osc_positive", True) else True
        ma60 = float(last_row.get("MA60", 0) or 0)
        ma60_ok = (ma60 > 0) if _settings_bool_sel(settings, "sel_a_require_ma60", True) else True

        vba = bool(last_row.get("VolBurstActive_A", False))
        burst_ok = (vba) if _settings_bool_sel(settings, "sel_a_require_vol_burst", False) else True

        rsi = float(last_row.get("RSI", 0) or 0)
        rmin = float(settings.get("sel_rsi_min", 35.0))
        rmax = float(settings.get("sel_rsi_max", 70.0))
        rsi_ok = (rmin <= rsi <= rmax) if _settings_bool_sel(
            settings, "sel_a_require_sel_rsi_band", False
        ) else True

        return {
            "_sel_ret5_raw": float(ret5_raw),
            "_sel_pb_pct": float(pb_pct),
            "_sel_atr_pct": float(atr_pct),
            "_sel_gap_pct": float(gap_pct),
            "_sel_ma_ok": bool(ma_ok),
            "_sel_min_liq_ok": bool(min_liq_ok),
            "_sel_osc_ok": bool(osc_ok),
            "_sel_ma60_ok": bool(ma60_ok),
            "_sel_burst_ok": bool(burst_ok),
            "_sel_rsi_ok": bool(rsi_ok),
        }
    except Exception:
        return None


def _apply_sel_a_live_cross_section(rows: list, settings: dict) -> list:
    """
    유니버스 전체 행에 대해 `_sel_ret5_raw` 교차섹션 분위를 매기고 Valid_A·Score_A 를 확정한다.
    """
    if not rows:
        return rows
    try:
        if not _settings_bool_sel(settings, "use_sel_track_a", True):
            return rows
        df = pd.DataFrame(rows)
        if "_sel_ret5_raw" not in df.columns:
            return rows
        rk_lo = float(settings.get("sel_a_rs_rank_min", 0.28))
        rk_hi = float(settings.get("sel_a_rs_rank_max", 0.92))
        pb_lo = float(settings.get("sel_a_pb_min_pct", 1.5))
        pb_hi = float(settings.get("sel_a_pb_max_pct", 16.0))
        atr_lo = float(settings.get("sel_a_atr_pct_min", 0.006))
        atr_hi = float(settings.get("sel_a_atr_pct_max", 0.085))
        gap_mx = float(settings.get("sel_a_gap_abs_max_pct", 6.0))

        s = pd.to_numeric(df["_sel_ret5_raw"], errors="coerce")
        df["_sel_rs_pct"] = s.rank(pct=True, ascending=True)
        pb = pd.to_numeric(df["_sel_pb_pct"], errors="coerce")
        atrp = pd.to_numeric(df["_sel_atr_pct"], errors="coerce")
        gap = pd.to_numeric(df["_sel_gap_pct"], errors="coerce").abs()

        m_rs = s.notna() & df["_sel_rs_pct"].between(rk_lo, rk_hi, inclusive="both")
        m_pb = pb.notna() & pb.between(pb_lo, pb_hi, inclusive="both")
        m_atr = atrp.notna() & atrp.between(atr_lo, atr_hi, inclusive="both")
        m_gap = gap.notna() & (gap <= gap_mx)
        m_ma = df["_sel_ma_ok"].astype(bool)
        m_liq = df["_sel_min_liq_ok"].astype(bool)
        m_osc = df["_sel_osc_ok"].astype(bool)
        m60 = df["_sel_ma60_ok"].astype(bool)
        m_br = df["_sel_burst_ok"].astype(bool)
        m_rsi = df["_sel_rsi_ok"].astype(bool)

        va = m_rs & m_pb & m_atr & m_gap & m_ma & m_liq & m_osc & m60 & m_br & m_rsi

        mid_pb = (pb_lo + pb_hi) / 2.0
        half_w = max((pb_hi - pb_lo) / 2.0, 1e-6)
        pb_score = (1.0 - (pb - mid_pb).abs() / half_w).clip(lower=0.0, upper=1.0) * 100.0
        rs_part = df["_sel_rs_pct"].fillna(0.0) * 55.0
        pb_part = pb_score * 0.28
        ma_part = df["_sel_ma_ok"].astype(float) * 18.0
        liq_part = _live_liq_part(df)
        # 백테 배치(_resync_batch_valid_to_period_universe)와 동일: OSC 상승 시에만 +10
        osc_bonus = _live_osc_rise_bonus(df)
        mode = _sel_score_mode(settings)
        if _use_score_a_s1_pnl_aligned(mode):
            sc = _score_a_s1_pnl_aligned_series(df, settings)
        elif mode == "fwd_aligned_v1":
            sc = _score_a_fwd_aligned_series(df, settings)
        else:
            sc = (rs_part + pb_part + ma_part + liq_part).fillna(0.0) + osc_bonus
            sc = sc.clip(lower=0.0, upper=100.0)
        df["_valid_a"] = va.fillna(False).values
        df["_score_a"] = np.where(va.fillna(False), np.round(sc, 1), 0.0)
        return df.to_dict("records")
    except Exception:
        return rows


def _compute_sel_b_aux_from_daily_tail(
    df_valid: pd.DataFrame,
    last_row: pd.Series,
    settings: dict,
) -> Optional[dict]:
    """
    실시간 선정용: Track B v2 — 단기 수익률 원시, 고가 대비 종가 비율(%), ATR%, 갭,
    MA20 기울기·정배열, 대금(B), 분출·RSI·OSC.
    """
    try:
        min_b = max(30, int(float(settings.get("sel_b_min_history_bars", 65))))
        if df_valid is None or len(df_valid) < min_b:
            return None
        rs_n = max(1, int(float(settings.get("sel_b_rs_lookback", 5))))
        hi_n = max(5, int(float(settings.get("sel_b_hi_lookback", 20))))
        atr_w = max(5, int(float(settings.get("sel_b_atr_period", 14))))
        slope_lb = max(2, int(float(settings.get("sel_b_ma20_slope_lookback", 5))))
        need = max(rs_n + 2, hi_n + 1, atr_w + 2, slope_lb + 25, min_b)
        if len(df_valid) < need:
            return None

        dfv = df_valid.copy()
        if "high" not in dfv.columns or dfv["high"].isna().all():
            dfv["high"] = dfv["close"]
        if "low" not in dfv.columns or dfv["low"].isna().all():
            dfv["low"] = dfv["close"]
        if "open" not in dfv.columns:
            dfv["open"] = dfv["close"]

        lc = float(last_row["close"] or 0)
        if lc <= 0:
            return None

        cl_rs = float(dfv["close"].iloc[-(rs_n + 1)])
        if cl_rs <= 0:
            return None
        ret_raw = (lc / cl_rs - 1.0) * 100.0

        hh = float(dfv["high"].tail(hi_n).max())
        if hh <= 0:
            return None
        hi_pct = (lc / hh) * 100.0

        prev_c = dfv["close"].shift(1)
        tr = np.maximum(
            dfv["high"] - dfv["low"],
            np.maximum((dfv["high"] - prev_c).abs(), (dfv["low"] - prev_c).abs()),
        )
        atr = float(tr.tail(atr_w).mean())
        atr_pct = atr / lc if lc > 0 else 0.0

        o0 = float(last_row.get("open", lc) or lc)
        pc1 = float(dfv["close"].iloc[-2])
        gap_pct = ((o0 - pc1) / pc1 * 100.0) if pc1 > 1e-9 else 0.0

        ma20 = float(last_row.get("MA20", 0) or 0)
        ma60 = float(last_row.get("MA60", 0) or 0)
        ma5 = float(last_row.get("MA5", 0) or 0)
        trend_ok = (ma20 > ma60) and (lc > ma20) and ma20 > 0 and ma60 > 0

        m20_series = dfv["MA20"].astype(float)
        if len(m20_series) < slope_lb + 1:
            return None
        ma20_now = float(m20_series.iloc[-1])
        ma20_prev = float(m20_series.iloc[-(slope_lb + 1)])
        slope = (ma20_now / ma20_prev - 1.0) if ma20_prev > 1e-9 else 0.0
        slope_min = float(settings.get("sel_b_ma20_slope_min", 0.001))
        slope_ok = slope >= slope_min

        if _settings_bool_sel(settings, "sel_b_require_close_ge_ma5", False):
            mult = _sel_b_ma5_close_mult(settings)
            ma5_ok = ma5 > 0 and lc >= ma5 * mult
            trend_ok = bool(trend_ok and ma5_ok)

        sel_b_min_val = float(settings.get("sel_b_min_trde_val", 45.0)) * 100000000
        avg5 = float(last_row.get("AvgVal5", 0) or 0)
        min_liq_ok = avg5 >= sel_b_min_val

        osc = float(last_row.get("OSC", 0) or 0)
        osc_prev = float(last_row.get("OSC_Prev", osc) or 0)
        osc_ok = (osc > 0) if _settings_bool_sel(settings, "sel_b_require_osc_positive", True) else True
        osc_rise_ok = (osc > osc_prev) if _settings_bool_sel(
            settings, "sel_b_require_osc_rising", False
        ) else True

        ma60_ok = (ma60 > 0) if _settings_bool_sel(settings, "sel_b_require_ma60", True) else True

        vbb = bool(last_row.get("VolBurstActive_B", False))
        burst_ok = vbb if _settings_bool_sel(settings, "sel_b_require_vol_burst", True) else True

        rsi = float(last_row.get("RSI", 0) or 0)
        rmin = float(settings.get("sel_b_rsi_min", 45.0))
        rmax = float(settings.get("sel_b_rsi_max", 75.0))
        rsi_ok = (rmin <= rsi <= rmax) if _settings_bool_sel(
            settings, "sel_b_require_sel_rsi_band", True
        ) else True

        return {
            "_sel_b_ret_raw": float(ret_raw),
            "_sel_b_hi_pct": float(hi_pct),
            "_sel_b_atr_pct": float(atr_pct),
            "_sel_b_gap_pct": float(gap_pct),
            "_sel_b_ma20_slope": float(slope),
            "_sel_b_slope_ok": bool(slope_ok),
            "_sel_b_trend_ok": bool(trend_ok),
            "_sel_b_min_liq_ok": bool(min_liq_ok),
            "_sel_b_osc_ok": bool(osc_ok),
            "_sel_b_osc_rise_ok": bool(osc_rise_ok),
            "_sel_b_ma60_ok": bool(ma60_ok),
            "_sel_b_burst_ok": bool(burst_ok),
            "_sel_b_rsi_ok": bool(rsi_ok),
        }
    except Exception:
        return None


def _apply_sel_b_live_cross_section(rows: list, settings: dict) -> list:
    """일자 유니버스 전체에 대해 Track B v2 교차섹션 분위·필터 후 ``_valid_b``·``_score_b`` 확정."""
    if not rows:
        return rows
    try:
        if not _settings_bool_sel(settings, "use_sel_track_b", True):
            return rows
        df = pd.DataFrame(rows)
        if "_sel_b_ret_raw" not in df.columns:
            return rows

        rk_lo = float(settings.get("sel_b_rs_rank_min", 0.38))
        rk_hi = float(settings.get("sel_b_rs_rank_max", 0.97))
        hi_lo = float(settings.get("sel_b_hi_min_pct", 92.0))
        hi_hi = float(settings.get("sel_b_hi_max_pct", 99.7))
        atr_lo = float(settings.get("sel_b_atr_pct_min", 0.005))
        atr_hi = float(settings.get("sel_b_atr_pct_max", 0.09))
        gap_mx = float(settings.get("sel_b_gap_abs_max_pct", 7.0))

        s = pd.to_numeric(df["_sel_b_ret_raw"], errors="coerce")
        df["_sel_b_rs_pct"] = s.rank(pct=True, ascending=True)
        hip = pd.to_numeric(df["_sel_b_hi_pct"], errors="coerce")
        atrp = pd.to_numeric(df["_sel_b_atr_pct"], errors="coerce")
        gap = pd.to_numeric(df["_sel_b_gap_pct"], errors="coerce").abs()

        m_rs = s.notna() & df["_sel_b_rs_pct"].between(rk_lo, rk_hi, inclusive="both")
        m_hi = hip.notna() & hip.between(hi_lo, hi_hi, inclusive="both")
        m_atr = atrp.notna() & atrp.between(atr_lo, atr_hi, inclusive="both")
        m_gap = gap.notna() & (gap <= gap_mx)

        m_tr = df["_sel_b_trend_ok"].astype(bool)
        m_sl = df["_sel_b_slope_ok"].astype(bool)
        m_liq = df["_sel_b_min_liq_ok"].astype(bool)
        m_osc = df["_sel_b_osc_ok"].astype(bool)
        m_osr = df["_sel_b_osc_rise_ok"].astype(bool)
        m60 = df["_sel_b_ma60_ok"].astype(bool)
        m_br = df["_sel_b_burst_ok"].astype(bool)
        m_rsi = df["_sel_b_rsi_ok"].astype(bool)

        vb = m_rs & m_hi & m_atr & m_gap & m_tr & m_sl & m_liq & m_osc & m_osr & m60 & m_br & m_rsi

        half_hi = max((hi_hi - hi_lo) / 2.0, 1e-6)
        hi_score = (1.0 - (hip - (hi_lo + hi_hi) / 2.0).abs() / half_hi).clip(lower=0.0, upper=1.0) * 100.0
        rs_part = df["_sel_b_rs_pct"].fillna(0.0) * 52.0
        hi_part = hi_score * 0.28
        lc = pd.to_numeric(df.get("Price", df.get("close", 0)), errors="coerce")
        ma20r = pd.to_numeric(df.get("MA20", 0), errors="coerce")
        tr_st = ((lc / ma20r.replace(0, np.nan) - 1.0) * 100.0).clip(lower=0.0, upper=10.0) * 10.0
        tr_part = tr_st * 0.15
        slope_bonus = df["_sel_b_slope_ok"].astype(float) * 10.0
        liq_part = _live_liq_part(df)
        osc_bonus = _live_osc_rise_bonus(df)
        mode = _sel_score_mode(settings)
        if _use_score_b_s1_pnl_aligned(mode):
            sc = _score_b_s1_pnl_aligned_series(df, settings)
        elif mode == "fwd_aligned_v1":
            sc = _score_b_fwd_aligned_series(df, settings)
        else:
            sc = (rs_part + hi_part + tr_part + slope_bonus + liq_part + osc_bonus).fillna(0.0)
            sc = sc.clip(lower=0.0, upper=100.0)
        df["_valid_b"] = vb.fillna(False).values
        df["_score_b"] = np.where(vb.fillna(False), np.round(sc, 1), 0.0)
        return df.to_dict("records")
    except Exception:
        return rows


def _sorted_dict_rows_top_with_slot_band(
    rows: List[dict],
    score_key: str,
    max_n: int,
    settings: dict,
    *,
    min_score_floor: float = 0.0,
) -> List[dict]:
    """
    - ``min_score_floor`` > 0: 해당 점수 이상만 후보(N과 무관), 이후 ``max_n`` 상한만 적용.
      한 건도 없으면 기본은 빈 목록(그날 해당 트랙 감시 없음). ``sel_track_min_score_fallback_topn`` True면 상위 ``max_n`` 폴백.
    - ``min_score_floor`` <= 0: 점수 내림차순 상위 ``max_n`` 및 (옵션) 상대 밴드 ``max_slots_score_rel_pad``.
    """
    try:
        max_n = max(0, int(max_n))
        if max_n <= 0 or not rows:
            return []
        mfloor = max(0.0, float(min_score_floor or 0.0))

        def _sk(r: dict) -> tuple[float, str]:
            try:
                sc = float(r.get(score_key, 0) or 0)
            except (TypeError, ValueError):
                sc = 0.0
            try:
                cd = str(r.get("Code", r.get("code", ""))).zfill(6)
            except Exception:
                cd = ""
            return (-sc, cd)

        arr = sorted(rows, key=_sk)
        if mfloor > 1e-15:
            pool: List[dict] = []
            for r in arr:
                try:
                    v = float(r.get(score_key, 0) or 0)
                except (TypeError, ValueError):
                    v = 0.0
                if v >= mfloor - 1e-12:
                    pool.append(r)
            if not pool and _settings_bool_sel(
                settings, "sel_track_min_score_fallback_topn", False
            ):
                return arr[:max_n]
            return pool[:max_n]

        pad = max(0.0, float(settings.get("max_slots_score_rel_pad", 0.0) or 0.0))
        extra_cap = max(0, int(float(settings.get("max_slots_band_extra_cap", 12) or 0)))
        if len(arr) <= max_n:
            return arr
        if pad <= 0:
            return arr[:max_n]
        try:
            cut = float(arr[max_n - 1].get(score_key, 0) or 0)
        except (TypeError, ValueError, IndexError):
            cut = 0.0
        floor = cut * (1.0 - pad)
        hard = max_n + extra_cap
        out: List[dict] = []
        for r in arr:
            try:
                v = float(r.get(score_key, 0) or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v >= floor - 1e-12:
                out.append(r)
            if len(out) >= hard:
                break
        return out if out else arr[:max_n]
    except Exception:
        try:
            return sorted(rows, key=lambda x: -float(x.get(score_key, 0) or 0))[: max(0, int(max_n))]
        except Exception:
            return []


def _df_top_by_score_slot_band(
    df: pd.DataFrame,
    *,
    score_col: str,
    code_col: str,
    max_n: int,
    settings: dict,
    min_score_floor: float = 0.0,
) -> pd.DataFrame:
    """
    일괄 스케줄용: ``_sorted_dict_rows_top_with_slot_band`` 와 동일 규칙.
    ``min_score_floor`` > 0 이면 점수 임계 후 ``max_n`` 상한만 적용. 임계 통과 없으면 기본 빈 프레임.
    """
    try:
        max_n = max(0, int(max_n))
        if max_n <= 0 or df is None or df.empty or score_col not in df.columns:
            return df.iloc[:0] if df is not None else pd.DataFrame()
        mfloor = max(0.0, float(min_score_floor or 0.0))
        cc = code_col if code_col in df.columns else "code"
        sort_cols = [score_col, cc] if cc in df.columns else [score_col]
        asc_b = [False, True] if len(sort_cols) == 2 else [False]
        s = df.sort_values(by=sort_cols, ascending=asc_b, na_position="last").reset_index(drop=True)

        if mfloor > 1e-15:
            pool = s[s[score_col].astype(float) >= mfloor - 1e-12].copy()
            if pool.empty and _settings_bool_sel(
                settings, "sel_track_min_score_fallback_topn", False
            ):
                return s.head(max_n).copy()
            return pool.head(max_n).copy()

        pad = max(0.0, float(settings.get("max_slots_score_rel_pad", 0.0) or 0.0))
        extra_cap = max(0, int(float(settings.get("max_slots_band_extra_cap", 12) or 0)))
        if len(s) <= max_n:
            return s
        if pad <= 0:
            return s.head(max_n).copy()
        try:
            cut = float(s.iloc[max_n - 1][score_col])
        except Exception:
            cut = 0.0
        floor = cut * (1.0 - pad)
        hard = max_n + extra_cap
        cand = s[s[score_col].astype(float) >= floor - 1e-12]
        return cand.head(hard).copy()
    except Exception as e:
        try:
            logging.warning("_df_top_by_score_slot_band 폴백(head): %s", e)
        except Exception:
            pass
        try:
            return df.sort_values(by=score_col, ascending=False).head(max(0, int(max_n))).copy()
        except Exception:
            return df.iloc[:0]


def _merge_dual_track_picks(
    selected_rows: list,
    settings: dict,
    logger_callback,
    max_a: Optional[int] = None,
    max_b: Optional[int] = None,
    max_c: Optional[int] = None,
) -> list:
    """
    Track A/B/C 상위 후보를 합치고 중복 종목은 ``SelTrack=ABC``처럼 병합합니다.

    ``max_a`` / ``max_b`` 가 주어지면 브레드스 레짐 슬롯을 적용합니다.
    Valid 스코어는 전 트랙 계산 전제. ``use_sel_track_*`` OFF 트랙은 감시 편입에서 제외합니다.
    Track C는 TYPE_C가 구현되기 전까지 트랙 매칭 게이트에서 A/B 매수를 차단합니다.
    """
    mon_a = _settings_bool_sel(settings, "use_sel_track_a", True)
    mon_b = _settings_bool_sel(settings, "use_sel_track_b", True)
    mon_c = _settings_bool_sel(settings, "use_sel_track_c", True) and _settings_bool_sel(
        settings, "use_type_c", False
    )
    max_a_i = (
        max(0, int(max_a))
        if max_a is not None
        else max(0, int(settings.get("max_slots_a", 20)))
    )
    max_b_i = (
        max(0, int(max_b))
        if max_b is not None
        else max(0, int(settings.get("max_slots_b", 10)))
    )
    max_c_i = (
        max(0, int(max_c))
        if max_c is not None
        else max(0, int(settings.get("max_slots_c", 15)))
    )
    if not mon_a:
        max_a_i = 0
    if not mon_b:
        max_b_i = 0
    if not mon_c:
        max_c_i = 0
    floor_a = max(0.0, float(settings.get("sel_track_min_score_a", 0.0) or 0.0))
    floor_b = max(0.0, float(settings.get("sel_track_min_score_b", 0.0) or 0.0))
    floor_c = max(0.0, float(settings.get("sel_track_min_score_c", 0.0) or 0.0))
    cand_a = [d for d in selected_rows if d.get("_valid_a")] if mon_a else []
    cand_b = [d for d in selected_rows if d.get("_valid_b")] if mon_b else []
    cand_c = [d for d in selected_rows if d.get("_valid_c")] if mon_c else []
    picked_a = _sorted_dict_rows_top_with_slot_band(
        cand_a, "_score_a", max_a_i, settings, min_score_floor=floor_a
    )
    picked_b = _sorted_dict_rows_top_with_slot_band(
        cand_b, "_score_b", max_b_i, settings, min_score_floor=floor_b
    )
    picked_c = _sorted_dict_rows_top_with_slot_band(
        cand_c, "_score_c", max_c_i, settings, min_score_floor=floor_c
    )

    def strip_internal(d: dict) -> dict:
        return {k: v for k, v in d.items() if not str(k).startswith("_")}

    final_by_code: Dict[str, dict] = {}
    track_by_code: Dict[str, set[str]] = {}
    score_by_code: Dict[str, float] = {}
    for track, score_key, picked in (
        ("A", "_score_a", picked_a),
        ("B", "_score_b", picked_b),
        ("C", "_score_c", picked_c),
    ):
        for candidate in picked:
            try:
                code = str(candidate["Code"]).zfill(6)
                score = float(candidate.get(score_key, 0) or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if code not in final_by_code:
                final_by_code[code] = strip_internal(candidate)
                track_by_code[code] = set()
                score_by_code[code] = score
            track_by_code[code].add(track)
            score_by_code[code] = max(score_by_code[code], score)

    final: list[dict] = []
    for code, row in final_by_code.items():
        row["SelTrack"] = "".join(
            track for track in ("A", "B", "C") if track in track_by_code[code]
        )
        row["Score"] = round(score_by_code[code], 1)
        final.append(row)

    logger_callback(
        f"[Track] A후보 {len(cand_a)}→채택 {len(picked_a)}, "
        f"B후보 {len(cand_b)}→채택 {len(picked_b)}, "
        f"C후보 {len(cand_c)}→채택 {len(picked_c)}, 최종 행 {len(final)}"
    )
    return final

