# -*- coding: utf-8 -*-
"""
종목 선정 파사드 — 구현은 strategy/selection_*.py 로 분리.

기존 ``import strategy_selector`` / ``from strategy_selector import X`` 호환을 유지합니다.
"""
from __future__ import annotations

from strategy.selection_errors import SelectionDbStaleError
from strategy.universe_codes import (
    load_kodex200_universe_codes,
    load_kospi_universe_codes,
    normalize_universe_index_choice,
    resolve_universe_codes,
    _maybe_generate_kospi200_csv,
    _maybe_generate_kospi_universe_csv,
)
from strategy.selection_scoring import (
    calculate_rsi,
    calculate_technical_indicators,
    get_strategy_score,
    _add_vol_burst_ab_columns,
    _settings_bool_sel,
    _sel_b_ma5_close_mult,
    _sel_score_mode,
    _use_score_a_s1_pnl_aligned,
    _use_score_b_s1_pnl_aligned,
    _sel_s1_pnl_signs,
    _signed_band_score,
    _score_a_s1_pnl_aligned_series,
    _score_b_s1_pnl_aligned_series,
    _band_low_is_better,
    _band_high_is_better,
    _score_a_fwd_aligned_series,
    _score_b_fwd_aligned_series,
)
from strategy.selection_live_aux import (
    _compute_sel_a_aux_from_daily_tail,
    _apply_sel_a_live_cross_section,
    _compute_sel_b_aux_from_daily_tail,
    _apply_sel_b_live_cross_section,
    _sorted_dict_rows_top_with_slot_band,
    _df_top_by_score_slot_band,
    _merge_dual_track_picks,
)
from strategy.selection_regime import (
    compute_market_breadth,
    _clamp_nonneg_int,
    is_chop_regime_day,
    _fetch_daily_ohlcv_batch_fdr,
    _fdr_index_daily_close_df,
    _build_index_vol_by_day,
    _compute_transition_regime_map_fdr,
    classify_transition_regime,
    is_rotation_a_guard_g1_day,
    resolve_transition_regime_policy,
    resolve_transition_runtime_blocks,
    _load_kodex200_daily_close,
    resolve_transition_gate_for_live,
    compute_transition_regime_history,
)
from strategy.selection_track_batch import (
    _apply_sel_track_a_batch_v2,
    _apply_sel_track_b_batch_v2,
    _resync_batch_valid_to_period_universe,
    _apply_sel_track_c_batch_latest,
)
from strategy.selection_dynamic_score import (
    _score_dynamic_universe_daily_ohlcv,
    _selection_daily_lookback_calendar_days,
    _universe_batch_indicator_lookback_days,
    _universe_batch_indicator_left_clip,
)
from strategy.selection_live import select_monitoring_targets
from strategy.selection_dynamic_universe import (
    prepare_dynamic_universe,
    collect_dynamic_schedule_codes,
    diagnose_dynamic_universe_selection_by_trading_day,
)

__all__ = [
    "SelectionDbStaleError",
    "load_kodex200_universe_codes",
    "load_kospi_universe_codes",
    "normalize_universe_index_choice",
    "resolve_universe_codes",
    "calculate_rsi",
    "calculate_technical_indicators",
    "get_strategy_score",
    "compute_market_breadth",
    "is_chop_regime_day",
    "classify_transition_regime",
    "is_rotation_a_guard_g1_day",
    "resolve_transition_regime_policy",
    "resolve_transition_runtime_blocks",
    "resolve_transition_gate_for_live",
    "compute_transition_regime_history",
    "select_monitoring_targets",
    "prepare_dynamic_universe",
    "collect_dynamic_schedule_codes",
    "diagnose_dynamic_universe_selection_by_trading_day",
]
