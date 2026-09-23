# -*- coding: utf-8 -*-
"""백테/동적유니버스용 트랙 A/B/C 배치 스코어링.

``strategy_selector`` 에서 분리. 기존 이름은 strategy_selector re-export 로 호환.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Set

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


def _apply_sel_track_a_batch_v2(
    df_all: pd.DataFrame,
    settings: dict,
    min_억: float,
    conv: float,
) -> None:
    """
    ``prepare_dynamic_universe`` 일괄 패널용 Track A v2.
    상대강도(일자 교차섹션 분위) + 고가대비 되돌림 + 이평근접 + ATR%·갭 + 대금.
    """
    try:
        g = df_all.groupby("code", sort=False)
        if "open" not in df_all.columns:
            df_all["open"] = df_all["close"]
        for col in ("high", "low"):
            if col not in df_all.columns:
                df_all[col] = df_all["close"]

        rs_n = max(1, int(float(settings.get("sel_a_rs_lookback", 5))))
        clag = g["close"].shift(rs_n)
        df_all["_sel_ret5_raw"] = (df_all["close"] / clag.replace(0, np.nan) - 1.0) * 100.0

        pb_n = max(5, int(float(settings.get("sel_a_pb_lookback", 20))))
        hh = g["high"].transform(lambda x: x.rolling(pb_n, min_periods=pb_n).max())
        df_all["_sel_pb_pct"] = (hh - df_all["close"]) / hh.replace(0, np.nan) * 100.0

        prev_c = g["close"].shift(1)
        tr = np.maximum(
            df_all["high"] - df_all["low"],
            np.maximum((df_all["high"] - prev_c).abs(), (df_all["low"] - prev_c).abs()),
        )
        atr_w = max(5, int(float(settings.get("sel_a_atr_period", 14))))
        df_all["_sel_tr"] = tr
        atr_roll = g["_sel_tr"].transform(
            lambda x: x.rolling(atr_w, min_periods=atr_w).mean()
        )
        df_all["_sel_atr_pct"] = atr_roll / df_all["close"].replace(0, np.nan)

        o0 = df_all["open"].astype(float)
        pc = g["close"].shift(1)
        df_all["_sel_gap_pct"] = ((o0 - pc) / pc.replace(0, np.nan) * 100.0).replace(
            [np.inf, -np.inf], np.nan
        )

        ma5 = df_all["MA5"].astype(float)
        ma20 = df_all["MA20"].astype(float)
        lc = df_all["close"].astype(float)
        m20n = float(settings.get("sel_a_ma20_near_pct", 0.045))
        eps = float(settings.get("sel_a_ma5_eps_pct", 0.004))
        near20 = ma20 > 0
        near20 = near20 & ((lc / ma20.replace(0, np.nan) - 1.0).abs() <= m20n)
        if _settings_bool_sel(settings, "sel_a_require_close_ge_ma5", True):
            ge5 = (ma5 > 0) & (lc >= ma5 * (1.0 - eps))
            df_all["_sel_ma_ok"] = near20 | ge5
        else:
            df_all["_sel_ma_ok"] = near20

        c1 = df_all["AvgVal5"] >= (min_억 * 100000000)
        c5 = df_all["OSC"] > 0 if _settings_bool_sel(settings, "sel_a_require_osc_positive", True) else pd.Series(True, index=df_all.index)
        c_ma60 = df_all["MA60"].notna() & (df_all["MA60"].astype(float) > 0)
        if not _settings_bool_sel(settings, "sel_a_require_ma60", True):
            c_ma60 = pd.Series(True, index=df_all.index)

        burst_ok = (df_all["VolBurstActive_A"] == 1) if _settings_bool_sel(
            settings, "sel_a_require_vol_burst", False
        ) else pd.Series(True, index=df_all.index)

        rmin = float(settings.get("sel_rsi_min", 35.0))
        rmax = float(settings.get("sel_rsi_max", 70.0))
        rsi_ok = (df_all["RSI"] >= rmin) & (df_all["RSI"] <= rmax)
        if not _settings_bool_sel(settings, "sel_a_require_sel_rsi_band", False):
            rsi_ok = pd.Series(True, index=df_all.index)

        rk_lo = float(settings.get("sel_a_rs_rank_min", 0.28))
        rk_hi = float(settings.get("sel_a_rs_rank_max", 0.92))
        df_all["_sel_rs_pct"] = df_all.groupby("time")["_sel_ret5_raw"].rank(
            pct=True, ascending=True
        )
        pb_lo = float(settings.get("sel_a_pb_min_pct", 1.5))
        pb_hi = float(settings.get("sel_a_pb_max_pct", 16.0))
        atr_lo = float(settings.get("sel_a_atr_pct_min", 0.006))
        atr_hi = float(settings.get("sel_a_atr_pct_max", 0.085))
        gap_mx = float(settings.get("sel_a_gap_abs_max_pct", 6.0))

        m_rs = df_all["_sel_ret5_raw"].notna() & df_all["_sel_rs_pct"].between(
            rk_lo, rk_hi, inclusive="both"
        )
        m_pb = df_all["_sel_pb_pct"].notna() & df_all["_sel_pb_pct"].between(
            pb_lo, pb_hi, inclusive="both"
        )
        m_atr = df_all["_sel_atr_pct"].notna() & df_all["_sel_atr_pct"].between(
            atr_lo, atr_hi, inclusive="both"
        )
        m_gap = df_all["_sel_gap_pct"].notna() & (df_all["_sel_gap_pct"].abs() <= gap_mx)

        va = (
            m_rs
            & m_pb
            & m_atr
            & m_gap
            & df_all["_sel_ma_ok"]
            & c1
            & c5
            & c_ma60
            & burst_ok
            & rsi_ok
        )
        df_all["Valid_A"] = va.fillna(False)

        mid_pb = (pb_lo + pb_hi) / 2.0
        half_w = max((pb_hi - pb_lo) / 2.0, 1e-6)
        pb_score = (
            (1.0 - (df_all["_sel_pb_pct"] - mid_pb).abs() / half_w).clip(lower=0.0, upper=1.0)
            * 100.0
        )
        rs_part = df_all["_sel_rs_pct"].fillna(0.0) * 55.0
        pb_part = pb_score * 0.28
        ma_part = df_all["_sel_ma_ok"].astype(float) * 18.0
        liq_part = (
            (df_all["AvgVal5"] / 50000000000.0 * 100.0).clip(upper=100.0) * 0.12
        )
        osc_b = (df_all["OSC"] > df_all["OSC_Prev"]).astype(float) * 10.0
        mode = _sel_score_mode(settings)
        if _use_score_a_s1_pnl_aligned(mode):
            sc = _score_a_s1_pnl_aligned_series(df_all, settings)
        elif mode == "fwd_aligned_v1":
            sc = _score_a_fwd_aligned_series(df_all, settings)
        else:
            sc = (rs_part + pb_part + ma_part + liq_part + osc_b).clip(lower=0.0, upper=100.0)
        df_all["Score_A"] = np.where(df_all["Valid_A"], np.round(sc, 1), 0.0)
    except Exception as e:
        try:
            logging.warning("_apply_sel_track_a_batch_v2 실패, 레거시로 폴백: %s", e, exc_info=True)
        except Exception:
            pass
        c1 = df_all["AvgVal5"] >= (min_억 * 100000000)
        c5 = df_all["OSC"] > 0
        c_ma60 = df_all["MA60"].notna()
        use_a = bool(settings.get("use_sel_track_a", True))
        c2_a = (df_all["MA_Diff"] <= conv) if use_a else pd.Series(False, index=df_all.index)
        c3_a = (df_all["RSI"] >= float(settings.get("sel_rsi_min", 35.0))) & (
            df_all["RSI"] <= float(settings.get("sel_rsi_max", 70.0))
        )
        c4_a = df_all["VolBurstActive_A"] == 1
        df_all["Valid_A"] = c1 & c4_a & c5 & c_ma60 & c2_a & c3_a
        df_all["Score_A"] = (
            ((1 - (df_all["MA_Diff"] / conv)) * 100).clip(lower=0) * 0.4
            + (df_all["RSI"] / 70 * 100).clip(upper=100) * 0.3
            + (df_all["AvgVal5"] / 50000000000 * 100).clip(upper=100) * 0.2
            + ((1 - (df_all["Pos_Diff"] / 0.03)) * 100).clip(lower=0) * 0.1
        )
        df_all.loc[df_all["OSC"] > df_all["OSC_Prev"], "Score_A"] += 10.0


def _apply_sel_track_b_batch_v2(
    df_all: pd.DataFrame,
    settings: dict,
    *,
    with_b_debug: bool = False,
) -> None:
    """
    ``prepare_dynamic_universe`` 일괄 패널용 Track B v2.
    단기 RS(일자 분위) + 고가 대비 종가 비율 + MA20 기울기 + 정배열 + ATR%·갭 + 대금(B) + 분출·RSI·OSC.
    """
    try:
        g = df_all.groupby("code", sort=False)
        if "open" not in df_all.columns:
            df_all["open"] = df_all["close"]
        for col in ("high", "low"):
            if col not in df_all.columns:
                df_all[col] = df_all["close"]

        rs_n = max(1, int(float(settings.get("sel_b_rs_lookback", 5))))
        clag = g["close"].shift(rs_n)
        df_all["_sel_b_ret_raw"] = (df_all["close"] / clag.replace(0, np.nan) - 1.0) * 100.0

        hi_n = max(5, int(float(settings.get("sel_b_hi_lookback", 20))))
        hh = g["high"].transform(lambda x: x.rolling(hi_n, min_periods=hi_n).max())
        df_all["_sel_b_hi_pct"] = (
            df_all["close"] / hh.replace(0, np.nan) * 100.0
        ).replace([np.inf, -np.inf], np.nan)

        prev_c = g["close"].shift(1)
        tr = np.maximum(
            df_all["high"] - df_all["low"],
            np.maximum((df_all["high"] - prev_c).abs(), (df_all["low"] - prev_c).abs()),
        )
        atr_w = max(5, int(float(settings.get("sel_b_atr_period", 14))))
        df_all["_sel_b_tr"] = tr
        atr_roll = g["_sel_b_tr"].transform(
            lambda x: x.rolling(atr_w, min_periods=atr_w).mean()
        )
        df_all["_sel_b_atr_pct"] = atr_roll / df_all["close"].replace(0, np.nan)

        o0 = df_all["open"].astype(float)
        pc = g["close"].shift(1)
        df_all["_sel_b_gap_pct"] = ((o0 - pc) / pc.replace(0, np.nan) * 100.0).replace(
            [np.inf, -np.inf], np.nan
        )

        slope_lb = max(2, int(float(settings.get("sel_b_ma20_slope_lookback", 5))))
        ma20_s = g["MA20"].shift(slope_lb)
        df_all["_sel_b_ma20_slope"] = (
            df_all["MA20"].astype(float) / ma20_s.replace(0, np.nan) - 1.0
        )
        slope_min = float(settings.get("sel_b_ma20_slope_min", 0.001))
        df_all["_sel_b_slope_ok"] = df_all["_sel_b_ma20_slope"] >= slope_min

        lc = df_all["close"].astype(float)
        ma20 = df_all["MA20"].astype(float)
        ma60 = df_all["MA60"].astype(float)
        ma5 = df_all["MA5"].astype(float)
        df_all["_sel_b_trend_ok"] = (ma20 > ma60) & (lc > ma20) & (ma20 > 0) & (ma60 > 0)
        if _settings_bool_sel(settings, "sel_b_require_close_ge_ma5", False):
            mult_b2 = _sel_b_ma5_close_mult(settings)
            ge5 = (ma5 > 0) & (lc >= ma5 * mult_b2)
            df_all["_sel_b_trend_ok"] = df_all["_sel_b_trend_ok"] & ge5

        sel_b_min_val = float(settings.get("sel_b_min_trde_val", 45.0)) * 100000000
        c1_b = df_all["AvgVal5"] >= sel_b_min_val

        c5 = df_all["OSC"] > 0 if _settings_bool_sel(settings, "sel_b_require_osc_positive", True) else pd.Series(
            True, index=df_all.index
        )
        c_ma60 = df_all["MA60"].notna() & (df_all["MA60"].astype(float) > 0)
        if not _settings_bool_sel(settings, "sel_b_require_ma60", True):
            c_ma60 = pd.Series(True, index=df_all.index)

        burst_ok = (df_all["VolBurstActive_B"] == 1) if _settings_bool_sel(
            settings, "sel_b_require_vol_burst", True
        ) else pd.Series(True, index=df_all.index)

        rmin = float(settings.get("sel_b_rsi_min", 45.0))
        rmax = float(settings.get("sel_b_rsi_max", 75.0))
        rsi_ok = (df_all["RSI"] >= rmin) & (df_all["RSI"] <= rmax)
        if not _settings_bool_sel(settings, "sel_b_require_sel_rsi_band", True):
            rsi_ok = pd.Series(True, index=df_all.index)

        osc_rise_ok = (df_all["OSC"] > df_all["OSC_Prev"]) if _settings_bool_sel(
            settings, "sel_b_require_osc_rising", False
        ) else pd.Series(True, index=df_all.index)

        rk_lo = float(settings.get("sel_b_rs_rank_min", 0.38))
        rk_hi = float(settings.get("sel_b_rs_rank_max", 0.97))
        df_all["_sel_b_rs_pct"] = df_all.groupby("time")["_sel_b_ret_raw"].rank(
            pct=True, ascending=True
        )
        hi_lo = float(settings.get("sel_b_hi_min_pct", 92.0))
        hi_hi = float(settings.get("sel_b_hi_max_pct", 99.7))
        atr_lo = float(settings.get("sel_b_atr_pct_min", 0.005))
        atr_hi = float(settings.get("sel_b_atr_pct_max", 0.09))
        gap_mx = float(settings.get("sel_b_gap_abs_max_pct", 7.0))

        m_rs = df_all["_sel_b_ret_raw"].notna() & df_all["_sel_b_rs_pct"].between(
            rk_lo, rk_hi, inclusive="both"
        )
        m_hi = df_all["_sel_b_hi_pct"].notna() & df_all["_sel_b_hi_pct"].between(
            hi_lo, hi_hi, inclusive="both"
        )
        m_atr = df_all["_sel_b_atr_pct"].notna() & df_all["_sel_b_atr_pct"].between(
            atr_lo, atr_hi, inclusive="both"
        )
        m_gap = df_all["_sel_b_gap_pct"].notna() & (df_all["_sel_b_gap_pct"].abs() <= gap_mx)

        vb = (
            m_rs
            & m_hi
            & m_atr
            & m_gap
            & df_all["_sel_b_trend_ok"]
            & df_all["_sel_b_slope_ok"]
            & c1_b
            & c5
            & c_ma60
            & burst_ok
            & rsi_ok
            & osc_rise_ok
        )
        df_all["Valid_B"] = vb.fillna(False)

        half_hi = max((hi_hi - hi_lo) / 2.0, 1e-6)
        hip = df_all["_sel_b_hi_pct"]
        hi_score = (
            (1.0 - (hip - (hi_lo + hi_hi) / 2.0).abs() / half_hi).clip(lower=0.0, upper=1.0) * 100.0
        )
        rs_part = df_all["_sel_b_rs_pct"].fillna(0.0) * 52.0
        hi_part = hi_score * 0.28
        tr_st = ((lc / ma20.replace(0, np.nan) - 1.0) * 100.0).clip(lower=0.0, upper=10.0) * 10.0
        tr_part = tr_st * 0.15
        slope_bonus = df_all["_sel_b_slope_ok"].astype(float) * 10.0
        liq_part = (
            (df_all["AvgVal5"] / 50000000000.0 * 100.0).clip(upper=100.0) * 0.12
        )
        osc_b = (df_all["OSC"] > df_all["OSC_Prev"]).astype(float) * 10.0
        mode = _sel_score_mode(settings)
        if _use_score_b_s1_pnl_aligned(mode):
            sc = _score_b_s1_pnl_aligned_series(df_all, settings)
        elif mode == "fwd_aligned_v1":
            sc = _score_b_fwd_aligned_series(df_all, settings)
        else:
            sc = (rs_part + hi_part + tr_part + slope_bonus + liq_part + osc_b).clip(
                lower=0.0, upper=100.0
            )
        df_all["Score_B"] = np.where(df_all["Valid_B"], np.round(sc, 1), 0.0)
        if with_b_debug:
            try:
                df_all["Dbg_B1_avg5_ge_selB"] = c1_b.astype(bool)
                df_all["Dbg_B2_trend_mas"] = df_all["_sel_b_trend_ok"].astype(bool)
                df_all["Dbg_B3_rsi_range"] = rsi_ok.astype(bool)
                df_all["Dbg_B4_volburst1"] = burst_ok.astype(bool)
                df_all["Dbg_C5_osc_pos"] = c5.astype(bool)
                df_all["Dbg_C_ma60_ok"] = c_ma60.astype(bool)
            except Exception:
                pass
    except Exception as e:
        try:
            logging.warning("_apply_sel_track_b_batch_v2 실패, 레거시로 폴백: %s", e, exc_info=True)
        except Exception:
            pass
        use_b = bool(settings.get("use_sel_track_b", True))
        c2_b = ((df_all["MA20"] > df_all["MA60"]) & (df_all["close"] > df_all["MA20"])) if use_b else pd.Series(
            False, index=df_all.index
        )
        if use_b and _settings_bool_sel(settings, "sel_b_require_close_ge_ma5", False):
            mult_fb = _sel_b_ma5_close_mult(settings)
            m5f = df_all["MA5"].astype(float)
            lcf = df_all["close"].astype(float)
            c2_b = c2_b & (m5f > 0) & (lcf >= m5f * mult_fb)
        sel_b_rsi_min = float(settings.get("sel_b_rsi_min", 45.0))
        sel_b_rsi_max = float(settings.get("sel_b_rsi_max", 75.0))
        sel_b_min_val = float(settings.get("sel_b_min_trde_val", 60.0)) * 100000000
        c3_b = (df_all["RSI"] >= sel_b_rsi_min) & (df_all["RSI"] <= sel_b_rsi_max)
        c1_b = df_all["AvgVal5"] >= sel_b_min_val
        c4_b = df_all["VolBurstActive_B"] == 1
        c5 = df_all["OSC"] > 0
        c_ma60 = df_all["MA60"].notna()
        df_all["Valid_B"] = c1_b & c2_b & c3_b & c4_b & c5 & c_ma60
        trend_strength = ((df_all["close"] / df_all["MA20"] - 1) * 100).clip(lower=0, upper=10) * 10
        df_all["Score_B"] = (
            trend_strength * 0.4
            + (df_all["RSI"] / 70 * 100).clip(upper=100) * 0.3
            + (df_all["AvgVal5"] / 50000000000 * 100).clip(upper=100) * 0.2
            + (df_all["OSC"].clip(lower=0) / (df_all["OSC"].abs().max() + 1e-10) * 100) * 0.1
        )
        df_all.loc[df_all["OSC"] > df_all["OSC_Prev"], "Score_B"] += 10.0


def _resync_batch_valid_to_period_universe(
    df_all: pd.DataFrame,
    get_period_universe: Callable[[Any], list[str]],
    settings: dict,
) -> None:
    """
    일자별 **당기 유니버스** 안에서만 RS 분위·Valid/Score 를 다시 맞춘다.

    패널(유니온)에 타월 종목이 섞여도, 같은 날짜의 레짐·스케줄이
    BT 기간에 따라 바뀌지 않게 한다.
    """
    if df_all is None or df_all.empty or "time" not in df_all.columns:
        return
    if "code" not in df_all.columns:
        return
    try:
        day_keys = df_all["time"].dt.strftime("%Y-%m-%d")
    except Exception:
        try:
            day_keys = pd.to_datetime(df_all["time"]).dt.strftime("%Y-%m-%d")
        except Exception:
            return

    # 일자별 유니버스 멤버십 — 행 단위 Python 루프 대신 일자별 벡터 isin
    univ_cache: Dict[str, set[str]] = {}
    in_period = np.zeros(len(df_all), dtype=bool)
    try:
        codes_arr = df_all["code"].astype(str).str.zfill(6).to_numpy()
        days_arr = np.asarray(day_keys, dtype=object)
        for d_str in pd.unique(days_arr):
            try:
                d_key = str(d_str)
                if d_key not in univ_cache:
                    univ_cache[d_key] = {
                        str(c).zfill(6) for c in (get_period_universe(d_key) or [])
                    }
                univ_list = list(univ_cache[d_key])
                if not univ_list:
                    continue
                day_m = days_arr == d_str
                in_period[day_m] = np.isin(codes_arr[day_m], univ_list)
            except Exception:
                continue
    except Exception:
        return

    mask = pd.Series(in_period, index=df_all.index)
    if not bool(mask.any()):
        return

    # 당기 유니버스 행만 RS 분위에 참여 (기간 불변)
    try:
        if "_sel_ret5_raw" in df_all.columns:
            tmp = df_all["_sel_ret5_raw"].where(mask)
            df_all["_sel_rs_pct"] = tmp.groupby(df_all["time"]).rank(
                pct=True, ascending=True
            )
    except Exception:
        pass
    try:
        if "_sel_b_ret_raw" in df_all.columns:
            tmpb = df_all["_sel_b_ret_raw"].where(mask)
            df_all["_sel_b_rs_pct"] = tmpb.groupby(df_all["time"]).rank(
                pct=True, ascending=True
            )
    except Exception:
        pass

    # Valid_A 재적용 (행 단위 조건 + 재계산된 RS)
    try:
        if bool(settings.get("use_sel_track_a", True)) and "_sel_rs_pct" in df_all.columns:
            rk_lo = float(settings.get("sel_a_rs_rank_min", 0.28))
            rk_hi = float(settings.get("sel_a_rs_rank_max", 0.92))
            pb_lo = float(settings.get("sel_a_pb_min_pct", 1.5))
            pb_hi = float(settings.get("sel_a_pb_max_pct", 16.0))
            atr_lo = float(settings.get("sel_a_atr_pct_min", 0.006))
            atr_hi = float(settings.get("sel_a_atr_pct_max", 0.085))
            gap_mx = float(settings.get("sel_a_gap_abs_max_pct", 6.0))
            min_억 = float(settings.get("min_trde_val", 40.0))
            m_rs = df_all["_sel_ret5_raw"].notna() & df_all["_sel_rs_pct"].between(
                rk_lo, rk_hi, inclusive="both"
            )
            m_pb = (
                df_all["_sel_pb_pct"].notna()
                & df_all["_sel_pb_pct"].between(pb_lo, pb_hi, inclusive="both")
                if "_sel_pb_pct" in df_all.columns
                else pd.Series(False, index=df_all.index)
            )
            m_atr = (
                df_all["_sel_atr_pct"].notna()
                & df_all["_sel_atr_pct"].between(atr_lo, atr_hi, inclusive="both")
                if "_sel_atr_pct" in df_all.columns
                else pd.Series(False, index=df_all.index)
            )
            m_gap = (
                df_all["_sel_gap_pct"].notna()
                & (df_all["_sel_gap_pct"].abs() <= gap_mx)
                if "_sel_gap_pct" in df_all.columns
                else pd.Series(False, index=df_all.index)
            )
            ma_ok = (
                df_all["_sel_ma_ok"].astype(bool)
                if "_sel_ma_ok" in df_all.columns
                else pd.Series(True, index=df_all.index)
            )
            c1 = df_all["AvgVal5"] >= (min_억 * 100000000)
            c5 = (
                (df_all["OSC"] > 0)
                if _settings_bool_sel(settings, "sel_a_require_osc_positive", True)
                else pd.Series(True, index=df_all.index)
            )
            c_ma60 = df_all["MA60"].notna() & (df_all["MA60"].astype(float) > 0)
            if not _settings_bool_sel(settings, "sel_a_require_ma60", True):
                c_ma60 = pd.Series(True, index=df_all.index)
            burst_ok = (
                (df_all["VolBurstActive_A"] == 1)
                if _settings_bool_sel(settings, "sel_a_require_vol_burst", False)
                else pd.Series(True, index=df_all.index)
            )
            rmin = float(settings.get("sel_rsi_min", 35.0))
            rmax = float(settings.get("sel_rsi_max", 70.0))
            rsi_ok = (df_all["RSI"] >= rmin) & (df_all["RSI"] <= rmax)
            if not _settings_bool_sel(settings, "sel_a_require_sel_rsi_band", False):
                rsi_ok = pd.Series(True, index=df_all.index)
            va = (
                mask
                & m_rs
                & m_pb
                & m_atr
                & m_gap
                & ma_ok
                & c1
                & c5
                & c_ma60
                & burst_ok
                & rsi_ok
            )
            df_all["Valid_A"] = va.fillna(False)
            # Score_A: Valid 아니면 0, 유효하면 RS 반영해 재계산(레거시 가중)
            mid_pb = (pb_lo + pb_hi) / 2.0
            half_w = max((pb_hi - pb_lo) / 2.0, 1e-6)
            pb_score = (
                (1.0 - (df_all["_sel_pb_pct"] - mid_pb).abs() / half_w).clip(
                    lower=0.0, upper=1.0
                )
                * 100.0
                if "_sel_pb_pct" in df_all.columns
                else 0.0
            )
            rs_part = df_all["_sel_rs_pct"].fillna(0.0) * 55.0
            pb_part = pb_score * 0.28 if not isinstance(pb_score, float) else 0.0
            ma_part = ma_ok.astype(float) * 18.0
            liq_part = (
                (df_all["AvgVal5"] / 50000000000.0 * 100.0).clip(upper=100.0) * 0.12
            )
            osc_b = (df_all["OSC"] > df_all["OSC_Prev"]).astype(float) * 10.0
            sc = (rs_part + pb_part + ma_part + liq_part + osc_b).clip(lower=0.0, upper=100.0)
            df_all["Score_A"] = np.where(df_all["Valid_A"], np.round(sc, 1), 0.0)
        elif "Valid_A" in df_all.columns:
            df_all.loc[~mask, "Valid_A"] = False
            if "Score_A" in df_all.columns:
                df_all.loc[~mask, "Score_A"] = 0.0
    except Exception as e:
        try:
            logging.warning("period-univ Valid_A 재동기화 실패: %s", e, exc_info=True)
        except Exception:
            pass

    # Valid_B 재적용
    try:
        if bool(settings.get("use_sel_track_b", True)) and "_sel_b_rs_pct" in df_all.columns:
            rk_lo = float(settings.get("sel_b_rs_rank_min", 0.38))
            rk_hi = float(settings.get("sel_b_rs_rank_max", 0.97))
            hi_lo = float(settings.get("sel_b_hi_min_pct", 92.0))
            hi_hi = float(settings.get("sel_b_hi_max_pct", 99.7))
            atr_lo = float(settings.get("sel_b_atr_pct_min", 0.005))
            atr_hi = float(settings.get("sel_b_atr_pct_max", 0.09))
            gap_mx = float(settings.get("sel_b_gap_abs_max_pct", 7.0))
            sel_b_min_val = float(settings.get("sel_b_min_trde_val", 45.0)) * 100000000
            m_rs = df_all["_sel_b_ret_raw"].notna() & df_all["_sel_b_rs_pct"].between(
                rk_lo, rk_hi, inclusive="both"
            )
            m_hi = (
                df_all["_sel_b_hi_pct"].notna()
                & df_all["_sel_b_hi_pct"].between(hi_lo, hi_hi, inclusive="both")
                if "_sel_b_hi_pct" in df_all.columns
                else pd.Series(False, index=df_all.index)
            )
            m_atr = (
                df_all["_sel_b_atr_pct"].notna()
                & df_all["_sel_b_atr_pct"].between(atr_lo, atr_hi, inclusive="both")
                if "_sel_b_atr_pct" in df_all.columns
                else pd.Series(False, index=df_all.index)
            )
            m_gap = (
                df_all["_sel_b_gap_pct"].notna()
                & (df_all["_sel_b_gap_pct"].abs() <= gap_mx)
                if "_sel_b_gap_pct" in df_all.columns
                else pd.Series(False, index=df_all.index)
            )
            trend_ok = (
                df_all["_sel_b_trend_ok"].astype(bool)
                if "_sel_b_trend_ok" in df_all.columns
                else pd.Series(False, index=df_all.index)
            )
            slope_ok = (
                df_all["_sel_b_slope_ok"].astype(bool)
                if "_sel_b_slope_ok" in df_all.columns
                else pd.Series(True, index=df_all.index)
            )
            c1_b = df_all["AvgVal5"] >= sel_b_min_val
            c5 = (
                (df_all["OSC"] > 0)
                if _settings_bool_sel(settings, "sel_b_require_osc_positive", True)
                else pd.Series(True, index=df_all.index)
            )
            c_ma60 = df_all["MA60"].notna() & (df_all["MA60"].astype(float) > 0)
            if not _settings_bool_sel(settings, "sel_b_require_ma60", True):
                c_ma60 = pd.Series(True, index=df_all.index)
            burst_ok = (
                (df_all["VolBurstActive_B"] == 1)
                if _settings_bool_sel(settings, "sel_b_require_vol_burst", True)
                else pd.Series(True, index=df_all.index)
            )
            rmin = float(settings.get("sel_b_rsi_min", 45.0))
            rmax = float(settings.get("sel_b_rsi_max", 75.0))
            rsi_ok = (df_all["RSI"] >= rmin) & (df_all["RSI"] <= rmax)
            if not _settings_bool_sel(settings, "sel_b_require_sel_rsi_band", True):
                rsi_ok = pd.Series(True, index=df_all.index)
            osc_rise_ok = (
                (df_all["OSC"] > df_all["OSC_Prev"])
                if _settings_bool_sel(settings, "sel_b_require_osc_rising", False)
                else pd.Series(True, index=df_all.index)
            )
            vb = (
                mask
                & m_rs
                & m_hi
                & m_atr
                & m_gap
                & trend_ok
                & slope_ok
                & c1_b
                & c5
                & c_ma60
                & burst_ok
                & rsi_ok
                & osc_rise_ok
            )
            df_all["Valid_B"] = vb.fillna(False)
            half_hi = max((hi_hi - hi_lo) / 2.0, 1e-6)
            hip = df_all["_sel_b_hi_pct"] if "_sel_b_hi_pct" in df_all.columns else None
            if hip is not None:
                hi_score = (
                    (1.0 - (hip - (hi_lo + hi_hi) / 2.0).abs() / half_hi)
                    .clip(lower=0.0, upper=1.0)
                    * 100.0
                )
            else:
                hi_score = 0.0
            rs_part = df_all["_sel_b_rs_pct"].fillna(0.0) * 52.0
            hi_part = hi_score * 0.28 if not isinstance(hi_score, float) else 0.0
            lc = df_all["close"].astype(float)
            ma20 = df_all["MA20"].astype(float)
            tr_st = ((lc / ma20.replace(0, np.nan) - 1.0) * 100.0).clip(
                lower=0.0, upper=10.0
            ) * 10.0
            tr_part = tr_st * 0.15
            slope_bonus = slope_ok.astype(float) * 10.0
            liq_part = (
                (df_all["AvgVal5"] / 50000000000.0 * 100.0).clip(upper=100.0) * 0.12
            )
            osc_b = (df_all["OSC"] > df_all["OSC_Prev"]).astype(float) * 10.0
            sc = (rs_part + hi_part + tr_part + slope_bonus + liq_part + osc_b).clip(
                lower=0.0, upper=100.0
            )
            df_all["Score_B"] = np.where(df_all["Valid_B"], np.round(sc, 1), 0.0)
        elif "Valid_B" in df_all.columns:
            df_all.loc[~mask, "Valid_B"] = False
            if "Score_B" in df_all.columns:
                df_all.loc[~mask, "Score_B"] = 0.0
    except Exception as e:
        try:
            logging.warning("period-univ Valid_B 재동기화 실패: %s", e, exc_info=True)
        except Exception:
            pass

    # Track C는 교차섹션 순위를 사용하지 않으므로 당기 유니버스 마스크만 적용합니다.
    try:
        if "Valid_C" in df_all.columns:
            df_all.loc[~mask, "Valid_C"] = False
        if "Score_C" in df_all.columns:
            df_all.loc[~mask, "Score_C"] = 0.0
    except Exception as e:
        try:
            logging.warning("period-univ Valid_C 재동기화 실패: %s", e, exc_info=True)
        except Exception:
            pass

    try:
        valid_columns = [
            column for column in ("Valid_A", "Valid_B", "Valid_C") if column in df_all
        ]
        score_columns = [
            column for column in ("Score_A", "Score_B", "Score_C") if column in df_all
        ]
        if valid_columns:
            df_all["Valid"] = df_all[valid_columns].fillna(False).any(axis=1)
        if score_columns:
            df_all["Score"] = df_all[score_columns].fillna(0.0).max(axis=1)
    except Exception:
        pass


def _apply_sel_track_c_batch_latest(
    df_all: pd.DataFrame,
    settings: dict,
    *,
    codes: Optional[Set[str]] = None,
) -> None:
    """
    슬라이딩 선정 창에서 종목별 마지막 일봉에 Track C 판정을 기록합니다.

    호출부는 평가일 행만 스케줄에 사용하므로 과거 행을 매번 재평가하지 않아도
    미래참조 없이 실시간 선정기와 동일한 결과를 얻습니다.

    codes 가 있으면 해당 종목만 평가한다(당기 유니버스). Track C는 교차섹션이
    없어 비유니버스 스킵 후에도 유니버스 종목 결과는 동일하다.
    """
    df_all["Valid_C"] = pd.Series(False, index=df_all.index, dtype=bool)
    df_all["Score_C"] = pd.Series(0.0, index=df_all.index, dtype=float)
    if not _settings_bool_sel(settings, "use_sel_track_c", True):
        return

    config = BoxReversionSelectionConfig.from_mapping(settings)
    code_filter: Optional[Set[str]] = None
    if codes is not None:
        try:
            code_filter = {str(c).zfill(6) for c in codes}
        except Exception:
            code_filter = None

    ohlcv_cols = [
        c
        for c in ("time", "datetime", "open", "high", "low", "close", "volume")
        if c in df_all.columns
    ]
    if "close" not in ohlcv_cols:
        return

    try:
        grouped = df_all.groupby("code", sort=False)
    except (KeyError, TypeError, ValueError):
        return

    metric_map = {
        "BoxLower_C": "BoxLower",
        "BoxUpper_C": "BoxUpper",
        "BoxWidthPct_C": "BoxWidthPct",
        "BoxPositionPct_C": "BoxPositionPct",
        "ADX_C": "ADX",
    }

    for raw_code, group in grouped:
        try:
            code = str(raw_code).zfill(6)
            if code_filter is not None and code not in code_filter:
                continue
            if "time" in group.columns:
                try:
                    if group["time"].is_monotonic_increasing:
                        ordered = group
                    else:
                        ordered = group.sort_values("time")
                except Exception:
                    ordered = group.sort_values("time")
            else:
                ordered = group
            if ordered.empty:
                continue
            # 스코어 열이 아닌 OHLCV 만 전달 → 정규화/복사 비용 축소
            slim = ordered.loc[:, [c for c in ohlcv_cols if c in ordered.columns]]
            candidate = evaluate_box_reversion_candidate(
                slim,
                code=code,
                config=config,
                assume_clean=True,
            )
            if candidate is None:
                continue
            last_index = ordered.index[-1]
            df_all.at[last_index, "Valid_C"] = True
            df_all.at[last_index, "Score_C"] = float(candidate.get("Score", 0.0))
            for target_column, source_key in metric_map.items():
                if target_column not in df_all.columns:
                    df_all[target_column] = np.nan
                df_all.at[last_index, target_column] = candidate.get(source_key, np.nan)
        except Exception as e:
            try:
                logging.warning(
                    "Track C 배치 판정 실패(%s): %s", str(raw_code).zfill(6), e
                )
            except Exception:
                pass

