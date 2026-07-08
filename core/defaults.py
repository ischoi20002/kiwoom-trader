# -*- coding: utf-8 -*-

"""
[v4.10.0] Vibe Project DNA — 전략 파라미터 기본값(DEFAULT_SETTINGS).

- 신규 설치·공장 초기화·settings.json 누락 시 이 값이 사용됩니다.
- 설정 탭 값을 코드 디폴트로 반영하려면 settings.json 저장 후
  프로젝트 루트에서:  python tools/sync_defaults_from_settings.py
"""

from typing import Any, Mapping, Optional

# 주력 전략 기본 파라미터 (settings.json + tools/sync_defaults_from_settings.py 동기화, skip 키는 기존 파일 값 유지)
DEFAULT_SETTINGS = {
    "backtest_initial_capital": 10000000,
    "buy_fee_live": 0.015,
    "sell_fee_live": 0.215,
    "buy_fee_mock": 0.35,
    "sell_fee_mock": 0.55,
    "buy_fee_bt": 0.015,
    "sell_fee_bt": 0.215,
    "max_slots": 30,
    "stage1_amt": 20.0,
    "s1_equity_pct": 20.0,
    "s1_sizing_unit": "%",
    "stage2_amt": 30.0,
    "s2_equity_pct": 25.0,
    "s2_sizing_unit": "%",
    "use_type_balance_gate": False,
    "type_balance_a_pct": 40.0,
    "min_15m_bars_for_signal": 60,
    # 고정밀 백테 인과 S1: ``calculate_indicators`` 재실행 시 사용할 15분봉 룩백 행 수(너무 작으면 장기 지표 불일치).
    "high_precision_causal_lookback_bars": 400,
    # 고정밀 인과 S1: 전역 15분 봉 인덱스 i 가 이 값 이상일 때만 Ready(벡터 루프와 동일하게 기본 19).
    "hp_causal_ready_min_bar_index": 19,
    "s1_buy_signal_cooldown_sec": 1800.0,
    "realtime_warmup_ohlcv_source": "realtime_15m_db_then_api",
    "realtime_warmup_15m_query_limit": 7000,
    # realtime_15m_*.db 에서 오래된 15분봉을 삭제할 때만 사용(워밍업 과거 채움과 무관).
    "realtime_15m_calendar_retention_days": 365,
    "realtime_warmup_persist_to_rt15m_db": True,
    "realtime_bars_max_keep": 300,
    # 15분 슬롯 확정 직후 ka10080(15분)으로 완성 봉 OHLCV 덮어쓰기(틱 누락 완화, 항상 동작)
    # [튜닝] 슬롯 직후 2.5s 일괄 조회는 M1(1분) 폴러와 ka10080 경쟁 → stagger 로 M1 이후 분산
    "realtime_closed_15m_official_replace_delay_sec": 2.5,
    "realtime_closed_15m_official_replace_stagger": True,
    # 0=자동(m1_spread+guard). 15분 REST는 슬롯 확정 후 최소 이 시각까지 대기
    "realtime_closed_15m_official_replace_min_delay_sec": 0.0,
    # 15분 REST는 min_delay 이후 spread 구간에 종목 분산(15분 슬롯 내 완료)
    "realtime_closed_15m_official_replace_spread_per_code_sec": 22.5,
    "realtime_closed_15m_official_replace_spread_min_sec": 90.0,
    "realtime_closed_15m_official_replace_spread_max_sec": 840.0,
    "realtime_closed_15m_official_replace_spread_sec": 360.0,
    "realtime_m1_ka10080_guard_sec": 12.0,
    "realtime_closed_15m_official_replace_max_retries": 3,
    "realtime_closed_15m_official_replace_max_pages": 8,
    "realtime_closed_15m_official_replace_inter_retry_sec": 0.6,
    # ka10080 REST 전역 최소 호출 간격(초). 워밍업·M1폴러·15분확정 등 병렬 호출이 429 나지 않게 직렬화.
    # [튜닝] 22종목 동시 폴링 시 0.35s 는 M1 분산 간격과 충돌해 429 상시 발생 → 0.5s 로 여유 확보.
    "realtime_ka10080_global_min_interval_sec": 0.5,
    # ka10080 실패 로그: 동일 (경로|종목|원인) UI 스로틀(초), JSONL 요약 주기(초)
    "realtime_ka10080_fail_log_ui_throttle_sec": 90.0,
    "realtime_ka10080_fail_summary_interval_sec": 300.0,
    # M1 ka10080 분산(종목 수 × per_code, cap 55s/분). 16종목≈40s, 40종목=55s
    "realtime_watchlist_m1_api_spread_per_code_sec": 2.5,
    "realtime_watchlist_m1_api_spread_min_sec": 12.0,
    "realtime_watchlist_m1_api_spread_max_sec": 55.0,
    "realtime_watchlist_m1_api_spread_sec": 40.0,
    "realtime_watchlist_m1_api_max_retries": 3,
    "realtime_watchlist_m1_api_backoff_base_sec": 0.6,
    "realtime_watchlist_m1_api_cache_max_entries": 4000,
    # 인트라바 미러: RtTickM1Store 비었을 때 bars_1m DB 병합(장후·API 실패 정합)
    "realtime_intrabar_m1_db_fallback": True,
    # ka10080 성공 시 응답 차트 전체를 M1 store에 catch-up
    "realtime_m1_poller_apply_chart_catchup": True,
    # ka10080 실패(502 등) 시에도 슬롯·15m 동기화 후 신호 평가
    "realtime_m1_poller_eval_on_fetch_fail": True,
    # 동일 분 캐시 히트 시에도 평가 재트리거(이전 eval 누락 복구)
    "realtime_m1_poller_eval_on_cache_hit": True,
    # 서킷브레이커·시장중단 15분 슬롯: 공식 REST 보정·S1 평가 억제
    "realtime_market_halt_slot_detection": True,
    "realtime_market_halt_skip_official_replace": True,
    "realtime_market_halt_suppress_s1_eval": True,
    "realtime_market_halt_m1_quiet_ratio": 0.70,
    "realtime_market_halt_m1_max_volume": 50.0,
    "realtime_market_halt_m1_min_codes": 6,
    "realtime_market_halt_official_miss_min_codes": 6,
    "realtime_market_halt_official_miss_window_sec": 180.0,
    # 시장중단 슬롯: 즉시 REST 스킵 후 지연 공식봉(ka10080) 재조회
    "realtime_market_halt_official_retry_enabled": True,
    "realtime_market_halt_official_retry_first_delay_sec": 180.0,
    "realtime_market_halt_official_retry_min_interval_sec": 600.0,
    "realtime_market_halt_official_retry_max_attempts": 8,
    "realtime_market_halt_official_retry_min_codes_to_clear": 3,
    "realtime_market_halt_official_retry_fail_cooldown_sec": 60.0,
    "use_api_warmup_backfill": True,
    # True: 감시 시작 워밍업마다 DB 검증 통과 여부와 무관하게 ka10080(15분) 조회·병합
    "realtime_warmup_force_api_refresh": True,
    "realtime_warmup_ka10080_tic_scope": "15",
    "realtime_warmup_ka10080_max_pages": 40,
    "realtime_warmup_indicator_calc_max_rows": 2000,
    "realtime_warmup_ka10080_page_delay_min": 0.2,
    "realtime_warmup_ka10080_page_delay_max": 0.55,
    "realtime_warmup_retry_max": 3,
    "realtime_warmup_retry_delay_sec": 30.0,
    "use_intraday_api_backfill": True,
    "require_full_warmup_for_monitoring": False,
    # 연속 감시: 장마감(15:30) 후 buffer 분 경과 시 자동 중단→일일선정→재시작
    "continuous_monitor_eod_buffer_minutes": 15,
    "continuous_monitor_stop_settle_sec": 5.0,
    # bars_1d 소스: fdr(하베스팅·백테 선정 기본, 감시 api_only와 동일) |
    # from_1m_aggregate(레거시 1m 집계), kiwoom_ka10081(키움 REST 일봉 직접)
    "monitor_pre_selection_bars_1d_source": "fdr",
    "monitor_pre_selection_rebuild_sleep_sec": 0.02,
    "incremental_warmup_stream": False,
    # 실시간 틱·UI: 메인스레드 부하 완화 (settings.json 과 동기 권장)
    "realtime_tick_flush_ms": 150,
    "realtime_tick_flush_max_codes_per_slice": 8,
    "realtime_tick_flush_slice_yield_ms": 2,
    "use_realtime_tick_worker_thread": True,
    "realtime_tick_main_queue_burst": 48,
    "realtime_tick_main_poller_ms": 16,
    "ws_reg_max_codes_per_chunk": 80,
    "realtime_tree_first_refresh_delay_ms": 48,
    "realtime_rsi_calc_min_interval_sec": 1.0,
    # S1 감시 진단: 신호=True·미매수 시 최소 기록 간격(초). 틱마다 기록 시 UI·JSONL 폭주.
    "realtime_s1_watch_diag_signal_no_buy_min_sec": 30.0,
    # 감시 트리 렌더 1프레임 최대 점유(초) — 초과 시 나머지는 다음 프레임으로 이월
    "realtime_monitor_render_frame_budget_sec": 0.045,
    # (레거시) 예전에는 S1 풀계산 최소 간격으로 사용. 현재는 15m 꼬리·M1 리비전 트리거가 우선.
    "realtime_s1_full_calc_min_interval_sec": 1.0,
    # S1 풀계산: 15m/M1 지문이 안 바뀌어도 이 간격(초)마다 한 번은 재계산(안전망).
    "realtime_s1_full_calc_fallback_sec": 120.0,
    # True: 미보유 S1·보유 청산·S2 를 동적 고정밀과 동일 `evaluate_bt_hp_intrabar_unified` 경로로 판정(공식 1분·합성 수급).
    "realtime_s1_mirror_dynamic_intrabar": True,
    # True: 같은 M1에서 forming OHLCV+intrabar 지표로 get_buy_signals 재판정(정적 Signal 복사 제거)
    "realtime_intrabar_close_now_signals": True,
    # 보유 청산·S2: ``evaluate_position`` / 인트라바 미러 호출 최소 간격(초). 너무 짧으면 매수 직후 과민 매도.
    "realtime_eval_position_min_interval_sec": 15.0,
    # 봉 부족 시 보유용 마지막 지표 행 재계산 최소 간격(초) — eval 과 동기 권장.
    "realtime_exit_indicator_min_interval_sec": 15.0,
    "realtime_monitor_atr_display_min_interval_sec": 0.5,
    # 보유 직후 monitor_info 스텁 유지(초) — 첫 틱에 즉시 해제하지 않아 메인 REST·UI 멈춤 완화
    "vp_price_stub_grace_sec": 45.0,
    # 실시간 인트라바 미러 GAP_DOWN: M1 저가만으로 청산하지 않고 틱 현재가도 한도 이하여야 함
    "realtime_gap_down_tick_confirm": True,
    # 진입 직후 N초 동안 미러 GAP_DOWN 억제(매수 직후 M1 저가 오판 방지)
    "realtime_gap_down_entry_grace_sec": 90.0,
    # 감시 트리 보유행 ATR 지표행 캐시(초) — calculate_indicators 메인 호출 최소화
    "realtime_monitor_atr_ir_cache_min_sec": 12.0,
    # 유니버스 인덱스 소스: KODEX200(기존 kodex200.csv) 또는 KOSPI200(신규 kospi_universe.csv + topN 슬라이스)
    "universe_index_choice": "KODEX200",
    # KOSPI200 모드에서 사용할 상위 종목 N (kospi_universe.csv head(N) 슬라이스)
    "kospi_top_n": 200,
    "kodex200_universe_priority": "csv_only",
    "kodex200_export_csv_on_etf_success": False,
    "kodex200_constituents_url": "",
    "limit_order_price_guard_enabled": True,
    "limit_order_max_tick_deviation": 3,
    "limit_order_cache_vs_tree_mismatch_ticks": 10,
    "realtime_use_market_orders": False,
    # True: 실시간 S1/EXIT/S2 를 시장가 대신 **매도1호가(매수)·매수1호가(매도)** 지정가로 접수하고,
    # 미체결 시 ka10004 재조회 후 kt10002 정정(최대 touch_quote_order_max_amends 회).
    # 실시간 REST는 터치호가 전용(매수=매도1·매도=매수1 + 정정 루프).
    "realtime_touch_quote_orders": True,
    "touch_quote_order_poll_sec": 30.0,
    "touch_quote_order_max_amends": 3,
    # kt00007 체결확인이 계속 pending(미반영)일 때 confirm 재예약을 무한 반복하지 않도록 누적 상한.
    # 상한 초과 시 미체결 타임아웃으로 흡수 → exit_pending/s1 매수 pending 영구 잠금 방지.
    "touch_pending_confirm_max": 20,
    "reconcile_remove_grace_seconds": 180,
    "reconcile_remove_confirm_syncs": 2,
    "sync_ghost_suppress_hours": 12.0,
    "sync_ghost_suppress_minutes": 15.0,
    "sync_ghost_new_synced_hours": 1.0,
    "exclude_trading_halt_on_selection": True,
    "min_trde_val": 30.0,
    "vol_ratio": 1.15,
    "vol_lookback": 3.0,
    "prev_vol_lookback": 7.0,
    "prev_vol_lookback_b": 5.0,
    "prev_vol_lookback_s1_a": 6.0,
    "prev_vol_lookback_s1_b": 5.0,
    "prev_vol_lookback_s2_a": 7.0,
    "prev_vol_lookback_s2_b": 5.0,
    "ma_convergence": 1.2,
    "sel_rsi_max": 70.0,
    "sel_rsi_min": 30.0,
    "macd_fast_daily": 12,
    "macd_slow_daily": 26,
    "macd_sig_daily": 9,
    "use_rsi_min": True,
    "buy_rsi_min": 40.0,
    "use_macd_signal": True,
    "macd_fast_entry": 12,
    "macd_slow_entry": 26,
    "macd_sig_entry": 9,
    "use_rsi_sig_filter": True,
    "rsi_sig_period": 9.0,
    "use_type_a": True,
    "buy_rsi_max_a": 100.0,
    "use_smart_buy_a": False,
    "use_gc": True,
    "gc_window": 90,
    "use_rsi_signal": True,
    "rsi_window": 60,
    "use_ma60_safe": True,
    "ma60_min": 100.0,
    "ma60_max": 108.0,
    "use_vol_surge": True,
    "vol_surge": 1.3,
    "use_atr_stop_loss_a": True,
    "use_atr_trailing_stop_a": False,
    "use_type_b": True,
    "same_bar_sig_priority": "prefer_a",
    "buy_rsi_max_b": 100.0,
    "use_smart_buy_b": False,
    "use_type_b_vol": True,
    "vol_surge_b": 2.5,
    "use_trend_aligned_b": True,
    "use_macd_gc_b": True,
    "use_atr_stop_loss_b": True,
    "use_atr_trailing_stop_b": True,
    "use_s2_ma20": True,
    "s2_ma20_gap": 0.0,
    "use_s2_ma5": True,
    "s2_ma5_gap": -0.5,
    "use_s2_rsi_sig": True,
    "s2_rsi_gap": 0.0,
    "use_s2_macd_gc": True,
    "s2_macd_gap": 0.0,
    "use_s2_vol_surge": True,
    "s2_vol_ratio": 1.5,
    "use_s2_profit": True,
    "s2_profit_limit": 1.5,
    "use_smart_buy_s2": False,
    "use_smart_buy_s2_b": False,
    "use_s2_master_a": True,
    "use_s2_master_b": True,
    "use_s2_delay": True,
    "s2_delay_hours": 24.0,
    "use_atr_stop_loss_s2": False,
    "atr_multiplier_sl_s2": 3.0,
    "use_atr_trailing_stop_s2": True,
    "atr_multiplier_trail_s2": 4.5,
    "smart_buy_vp": 110.0,
    "smart_buy_ba_ratio": 1.5,
    "use_smart_exit": False,
    "smart_exit_vp": 95.0,
    "smart_exit_ba_ratio": 1.1,
    "smart_exit_price_threshold": 1.0,
    "use_gap_down_exit": True,
    "gap_down_limit": -5.0,
    "use_gap_grace": False,
    "gap_grace_mins": 15,
    "use_atr_exit": True,
    "atr_period": 100,
    "use_gap_down_exit_a": True,
    "gap_down_limit_a": -5.0,
    "use_gap_grace_a": False,
    "gap_grace_mins_a": 40,
    "use_atr_exit_a": True,
    "atr_period_a": 100,
    "use_atr_trail_only_after_tp_a": False,
    "use_atr_trail_only_after_tp_s1_a": False,
    "use_atr_trail_only_after_tp_s2_a": True,
    "use_gap_down_exit_b": True,
    "gap_down_limit_b": -5.0,
    "use_gap_grace_b": False,
    "gap_grace_mins_b": 15,
    "use_atr_exit_b": True,
    "atr_period_b": 100,
    "use_atr_trail_only_after_tp_b": True,
    "use_atr_trail_only_after_tp_s1_b": True,
    "use_atr_trail_only_after_tp_s2_b": True,
    "use_s1_a_tp": True,
    "s1_a_take_profit": 4.5,
    "use_s1_a_stop_loss": True,
    "s1_a_stop_loss": 3.0,
    "use_s1_b_tp": True,
    "s1_b_take_profit": 4.0,
    "use_s1_b_stop_loss": True,
    "s1_b_stop_loss": 3.0,
    "use_s2_tp": True,
    "s2_take_profit": 5.0,
    "use_s2_stop_loss": True,
    "s2_stop_loss": 2.5,
    "use_s1_a_flash_exit": True,
    "s1_a_flash_exit_pct": 4.0,
    "use_s1_b_flash_exit": True,
    "s1_b_flash_exit_pct": 2.0,
    "use_s2_flash_exit": True,
    "s2_flash_exit_pct": 2.5,
    "use_s2_b_tp": True,
    "s2_b_take_profit": 5.0,
    "use_s2_b_stop_loss": True,
    "s2_b_stop_loss": 3.0,
    "use_s2_b_flash_exit": True,
    "s2_b_flash_exit_pct": 2.0,
    "use_atr_stop_loss_s2_b": True,
    "atr_multiplier_sl_s2_b": 1.5,
    "use_atr_trailing_stop_s2_b": True,
    "atr_multiplier_trail_s2_b": 7.0,
    "use_atr_trail_only_after_tp": True,
    "atr_multiplier_sl_a": 2.0,
    "atr_multiplier_sl_b": 2.0,
    "atr_multiplier_trail_a": 4.5,
    "atr_multiplier_trail_b": 5.0,
    "use_sel_track_a": True,
    "use_sel_track_b": True,
    "max_slots_a": 20,
    "max_slots_b": 15,
    "max_slots_score_rel_pad": 0.0,
    "max_slots_band_extra_cap": 12,
    "sel_track_min_score_a": 0.0,
    "sel_track_min_score_b": 0.0,
    "sel_track_min_score_fallback_topn": False,
    "sel_a_use_new_stack": True,
    "sel_a_rs_lookback": 4.0,
    "sel_a_rs_rank_min": 0.33,
    "sel_a_rs_rank_max": 0.9,
    "sel_a_pb_lookback": 15.0,
    "sel_a_pb_min_pct": 5.0,
    "sel_a_pb_max_pct": 20.0,
    "sel_a_ma20_near_pct": 0.045,
    "sel_a_require_close_ge_ma5": True,
    "sel_a_ma5_eps_pct": 0.004,
    "sel_a_atr_period": 14.0,
    "sel_a_atr_pct_min": 0.006,
    "sel_a_atr_pct_max": 0.085,
    "sel_a_gap_abs_max_pct": 6.0,
    "sel_a_require_osc_positive": True,
    "sel_a_require_ma60": True,
    "sel_a_require_vol_burst": False,
    "sel_a_require_sel_rsi_band": False,
    "sel_a_min_history_bars": 65.0,
    "sel_b_min_trde_val": 55.0,
    "sel_b_rsi_min": 40.0,
    "sel_b_rsi_max": 100.0,
    "sel_b_vol_ratio": 1.5,
    "sel_b_vol_lookback": 6.0,
    "sel_b_use_new_stack": True,
    "sel_b_rs_lookback": 4.0,
    "sel_b_rs_rank_min": 0.0,
    "sel_b_rs_rank_max": 1.0,
    "sel_b_hi_lookback": 20.0,
    "sel_b_hi_min_pct": 92.0,
    "sel_b_hi_max_pct": 99.7,
    "sel_b_ma20_slope_lookback": 5.0,
    "sel_b_ma20_slope_min": 0.001,
    "sel_b_require_close_ge_ma5": False,
    "sel_b_ma5_eps_pct": 0.005,
    "sel_b_ma5_min_excess_ratio": 0.005,
    "sel_b_atr_period": 14.0,
    "sel_b_atr_pct_min": 0.005,
    "sel_b_atr_pct_max": 0.09,
    "sel_b_gap_abs_max_pct": 6.0,
    "sel_b_require_osc_positive": True,
    "sel_b_require_osc_rising": True,
    "sel_b_require_ma60": False,
    "sel_b_require_vol_burst": True,
    "sel_b_require_sel_rsi_band": False,
    "sel_b_min_history_bars": 65.0,
    "use_s2_b_ma20": True,
    "s2_b_ma20_gap": 0.0,
    "use_s2_b_ma5": True,
    "s2_b_ma5_gap": -0.5,
    "use_s2_b_rsi_sig": True,
    "s2_b_rsi_gap": 0.0,
    "use_s2_b_macd_gc": True,
    "s2_b_macd_gap": 0.0,
    "use_s2_b_vol_surge": True,
    "s2_b_vol_ratio": 1.8,
    "use_s2_b_profit": True,
    "s2_b_profit_limit": 1.5,
    "use_s2_b_delay": True,
    "s2_b_delay_hours": 24.0,
    "ma60_safe_ratio": 15.0,
    "macd_fast": 12,
    "macd_sig": 9,
    "macd_slow": 26,
    "daily_report_session_buffer_minutes": 10,
    "daily_settlement_batch_start_date": "2026-04-05",
    "min_ask_bid_ratio_entry": 1.5,
    "min_vol_power_entry": 120.0,
    "power_sustained_min": 2,
    "smart_exit_bid_ratio": 1.5,
    "limit_order_unfilled_timeout_sec": 45.0,
    "exchange_fill_sync_lookback_days": 7,
    "exchange_fill_confirm_max_attempts": 8,
    "exchange_fill_confirm_retry_interval_sec": 2.5,
    "exchange_fill_late_reconfirm_sec": 35.0,
    # kt00007 병합 최소 간격(초) — 체결 폴링·터치호가가 동시에 돌 때 REST 폭주 완화
    "exchange_fill_kt00007_sync_min_interval_sec": 5.0,
    # per-order kt00007 긴급 sync 최소 간격(초) — 터치 poll 전용
    "exchange_fill_kt00007_per_order_min_sec": 1.2,
    # RC4033·타임아웃 등 경량 체결 확인 최대 횟수
    "exchange_fill_boost_max_attempts": 6,
    # RC4033 즉시확정·boost 폴링 간격(초)
    "exchange_fill_boost_interval_sec": 1.5,
    "exchange_fill_no_open_confirm_attempts": 8,
    "exchange_fill_no_open_confirm_interval_sec": 0.8,
    # 터치 pending N회 후 취소 API로 전량체결(RC4033) 조기 탐지
    "touch_pending_cancel_probe_after": 2,
    "touch_exit_pending_probe_after": 1,
    "exchange_fill_trust_no_open_qty": True,
    # on_fill 연속 시 포지션 탭 gui_callback 디바운스(ms)
    "portfolio_gui_callback_debounce_ms": 1200,
    "virtual_tab_orders_refresh_debounce_ms": 800,
    "virtual_tab_light_refresh_debounce_ms": 450,
    "post_fill_sync_first_delay_ms": 8500,
    "post_fill_sync_follow_delay_ms": 28000,
    "post_fill_sync_enable_followup": True,
    # 미확정 주문 존재 시 post-fill 계좌동기화 생략
    "post_fill_sync_skip_while_pending": True,
    "sync_run_kt00007_on_silent_sync": False,
    "bt_start_date": "2026-04-01",
    "bt_end_date": "2026-04-24",
    "mock_account_id": "81220142",
}

# [Zombie Parameter Prevention] 유효 키 목록
ALLOWED_EXTRA_KEYS = {
    'bt_start_date', 'bt_end_date', 
    's1_sizing_unit', 's2_sizing_unit', 
    'mock_account_id', 'backtest_initial_capital',
    'buy_fee_live', 'sell_fee_live',
    'buy_fee_bt', 'sell_fee_bt',
    'buy_fee_mock', 'sell_fee_mock',
    'daily_report_session_buffer_minutes',
    'daily_settlement_batch_start_date',
    'exchange_fill_sync_lookback_days',
}

def get_allowed_keys(current_settings=None):
    """설정 저장 시 필터링에 사용할 전체 유효 키 셋 반환"""
    base = set(DEFAULT_SETTINGS.keys()).union(ALLOWED_EXTRA_KEYS)
    if current_settings:
        # [v16.62] opt_ 로 시작하는 최적화 상태 키들도 허용 목록에 포함
        opt_keys = {k for k in current_settings.keys() if k.startswith('opt_')}
        return base.union(opt_keys)
    return base


def effective_min_15m_bars_for_monitoring(
    settings: Optional[Mapping[str, Any]] = None,
) -> int:
    """
    실시간 감시·워밍업 검증 공통: 필요한 최소 15분봉 개수.
    `min_15m_bars_for_signal` 과 설정 탭 ATR 주기(atr_period_a, atr_period_b, 폴백 atr_period) 중 최댓값.
    """
    s: Mapping[str, Any] = settings if settings is not None else DEFAULT_SETTINGS
    try:
        base = max(1, int(float(s.get("min_15m_bars_for_signal", 60))))
    except (TypeError, ValueError):
        base = 60
    try:
        atr_legacy = max(1, int(float(s.get("atr_period", 100))))
    except (TypeError, ValueError):
        atr_legacy = 100
    try:
        atr_a = max(1, int(float(s.get("atr_period_a", atr_legacy))))
    except (TypeError, ValueError):
        atr_a = atr_legacy
    try:
        atr_b = max(1, int(float(s.get("atr_period_b", atr_legacy))))
    except (TypeError, ValueError):
        atr_b = atr_legacy
    atr_need = max(atr_a, atr_b)
    return max(base, atr_need)
