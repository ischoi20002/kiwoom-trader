# -*- coding: utf-8 -*-

"""
[v4.10.0] Vibe Project DNA — 전략 파라미터 기본값(DEFAULT_SETTINGS).

- 신규 설치·공장 초기화·settings.json 누락 시 이 값이 사용됩니다.
- 설정 탭 값을 코드 디폴트로 반영하려면 settings.json 저장 후
  프로젝트 루트에서:  python tools/sync_defaults_from_settings.py
"""

from typing import Any, Mapping, Optional

# 주력 전략 기본 파라미터 (마지막 동기화: settings.json 기준, 키 순서는 기존 defaults 유지)
DEFAULT_SETTINGS = {
    "backtest_initial_capital": 10000000,
    # 수수료 요율(%): 체결금액 대비. 실전/모의는 config.USE_MOCK 과 짝을 이룸.
    "buy_fee_live": 0.015,
    "sell_fee_live": 0.215,
    "buy_fee_mock": 0.35,
    "sell_fee_mock": 0.5,
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
    # 실시간: 15분봉 하한(진행 중 가상 1봉 포함). 실제 임계는 ATR 주기와의 max → effective_min_15m_bars_for_monitoring
    "min_15m_bars_for_signal": 60,
    # 워밍업 OHLCV 출처:
    # - "api_only": DB 미사용, ka10080 만
    # - "realtime_15m_db_then_api": 실시간 전용 realtime_15m_*.db 우선(하베스팅 DB 미사용), 부족 시 API
    # - "db_then_api": 하베스팅 trading_data.db 의 bars_15m/1m 우선 — 없으면 api_only 로 강등
    # 전략 종목 선정(strategy_selector)은 별도 키로 api_only 를 쓸 수 있음
    "realtime_warmup_ohlcv_source": "realtime_15m_db_then_api",
    # 워밍업: DB에서 가져올 최대 15분봉 개수(bars_15m 부족 시 1m→15m 리샘플, db_then_api 일 때만 사용)
    # 워밍업·실시간 DB 조회 시 한 번에 가져올 15분봉 행 상한(1년 캐시·최대 약 7천 봉 전후)
    "realtime_warmup_15m_query_limit": 7000,
    # realtime_15m_*.db 에서 캘린더 기준 이전 일수만큼만 유지(롤링 DELETE)
    "realtime_15m_calendar_retention_days": 365,
    # 캘린더 깊이 판정 시 (보존 N일 − 슬랙)일 이상이면 충족. 주말·연휴로 첫 봉 날짜가 며칠 밀릴 수 있음.
    "realtime_warmup_depth_calendar_slack_days": 21,
    # True: 워밍업 검증 통과 시 병합 15분 OHLCV를 realtime_15m_*.db 에 배치 저장(다음 실행 캐시)
    "realtime_warmup_persist_to_rt15m_db": True,
    # 실시간 bars_data 메모리 상한(StreamProcessor.tail 과 맞춤)
    "realtime_bars_max_keep": 300,
    # DB·리샘플만으로 봉이 부족할 때 키움 분봉 API로 15분봉 메모리 보충
    "use_api_warmup_backfill": True,
    # 실시간 감시 워밍업·RT15m DB 보충 시 ka10080 봉 주기(문자열). "15"=15분봉만 수신(1분→15분 리샘플 없음)
    "realtime_warmup_ka10080_tic_scope": "15",
    # 워밍업 분봉 API(ka10080) 연속조회 최대 페이지 수(종목당 — 신호·당일갭만 채울 때)
    "realtime_warmup_ka10080_max_pages": 40,
    # 캘린더 과거 확보 모드일 때 연속조회 상한(종목당). 1년 분량은 API·페이지당 건수에 따라 더 필요할 수 있음
    "realtime_warmup_ka10080_max_pages_deep": 500,
    # True: 가장 오래된 15분봉이 (지금−realtime_15m_calendar_retention_days) 이전이 될 때까지 ka10080 연속조회
    "realtime_warmup_fetch_calendar_history": True,
    # 레거시 키(호환). 캘린더 깊이(1년)는 항상 백그라운드 큐에서만 처리 — 메인 워밍업은 감시 시작에 필요한 최소·당일만
    "realtime_warmup_quick_then_deep_background": True,
    # 워밍업·깊이보강 후 지표 계산 시 사용할 최대 15분봉 행 수(꼬리만). 수천 봉 전체 RSI/MACD 반복을 막아 CPU·GIL 부하 완화
    "realtime_warmup_indicator_calc_max_rows": 2000,
    # 연속조회 페이지 사이 대기(초) — 429 완화
    "realtime_warmup_ka10080_page_delay_min": 0.2,
    "realtime_warmup_ka10080_page_delay_max": 0.55,
    # 워밍업 검증/API 실패 시 동일 종목 재시도(최초 1회 + 아래 횟수만큼)
    "realtime_warmup_retry_max": 3,
    "realtime_warmup_retry_delay_sec": 30.0,
    # 장중: 하베스팅이 전일까지이므로 당일 09:00~현재 구간이 비었으면 봉 수와 무관 API 보충
    "use_intraday_api_backfill": True,
    # True: 한 종목이라도 워밍업 실패 시 전체 감시 미시작. False: 실패 종목만 제외하고 나머지 감시(권장).
    "require_full_warmup_for_monitoring": False,
    # 준비 완료 종목부터 웹소켓 구독(REG)·감시 시작(나머지는 이어서 합류).
    # require_full_warmup=True 이고 일부만 스트림이 켜진 뒤 다른 종목이 실패하면 감시를 중단합니다.
    "incremental_warmup_stream": True,
    # KODEX200 유니버스: 권장 ``csv_only``(KRX data.krx 비인증·차단 — 로컬 ``kodex200.csv`` 만).
    # (레거시) ``etf_api_first``·``local_csv_first`` 는 KRX/원격/ETF 경로를 쓰므로 운영에서 비권장.
    "kodex200_universe_priority": "csv_only",
    # True이고 ETF 조회에 성공하면 kodex200.csv 를 자동 덮어씀(백업 후 사용 권장).
    "kodex200_export_csv_on_etf_success": False,
    # 비어 있지 않으면: 해당 URL에서 kodex200.csv 와 동일 형식(종목코드·종목명) CSV 를 GET 해 유니버스로 사용
    # (GitHub raw·사내 서버 등). pykrx/KRX 실패 시 로컬 파일보다 우선 시도됨(etf_api_first 모드).
    "kodex200_constituents_url": "",
    # 지정가(00) 주문 전: 참고가 vs 실시간 캐시·화면 시세 일관성 검증
    "limit_order_price_guard_enabled": True,
    # 주문 기준가와 신뢰 시세(캐시 우선) 허용 차이(호가 단위 개수)
    "limit_order_max_tick_deviation": 3,
    # 화면 현재가 vs monitor_info 캐시 불일치 허용(더 넓게 — UI 갱신 지연 허용)
    "limit_order_cache_vs_tree_mismatch_ticks": 10,
    # 계좌 동기화 시 잔고 API 지연으로 로컬 포지션을 제거하지 않는 유예(초)
    "reconcile_remove_grace_seconds": 180,
    # 서버 잔고 목록에 종목이 연속으로 없을 때만 로컬 제거(동기화 주기마다 1씩 증가)
    "reconcile_remove_confirm_syncs": 2,
    # 실매도 종료 직후 잔고 API 유령(SYNCED) 제거 시 '직전 실매도' 판정 윈도우(레거시·시간 단위, 분으로 상한 변환됨)
    "sync_ghost_suppress_hours": 12.0,
    # SYNCED 유령 제거 전용(분). 너무 길면 동일 종목 당일 재매도 후 매매종료내역 누락 가능
    "sync_ghost_suppress_minutes": 15.0,
    # 동일 종목에 최근 실매도 종료 이력이 있을 때 신규 SYNCED 자동등록 생략(더 짧게 — 재매수 동기화 오판 완화)
    "sync_ghost_new_synced_hours": 1.0,
    # 종목 선정 시 네이버 실시간 API로 거래정지(HALTED) 종목 제외 (실시간 선정만)
    "exclude_trading_halt_on_selection": True,
    "min_trde_val": 50.0,
    "vol_ratio": 1.2,
    "vol_lookback": 7.0,
    "prev_vol_lookback": 7.0,
    "prev_vol_lookback_b": 5.0,
    "prev_vol_lookback_s1_a": 7.0,
    "prev_vol_lookback_s1_b": 5.0,
    "prev_vol_lookback_s2_a": 7.0,
    "prev_vol_lookback_s2_b": 5.0,
    "ma_convergence": 2.0,
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
    "buy_rsi_max_a": 70.0,
    "use_smart_buy_a": False,
    "use_gc": True,
    "gc_window": 90,
    "use_rsi_signal": True,
    "rsi_window": 60,
    "use_ma60_safe": True,
    "ma60_min": 100.0,
    "ma60_max": 108.0,
    "use_vol_surge": True,
    "vol_surge": 1.4,
    "use_atr_stop_loss_a": False,
    "use_atr_trailing_stop_a": False,
    "use_type_b": True,
    "same_bar_sig_priority": "confluence",
    "buy_rsi_max_b": 70.0,
    "use_smart_buy_b": True,
    "use_type_b_vol": True,
    "vol_surge_b": 2.5,
    "use_trend_aligned_b": True,
    "use_macd_gc_b": True,
    "use_atr_stop_loss_b": False,
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
    "use_atr_stop_loss_s2": True,
    "atr_multiplier_sl_s2": 2.5,
    "use_atr_trailing_stop_s2": True,
    "atr_multiplier_trail_s2": 6.0,
    "smart_buy_vp": 115.0,
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
    "use_gap_grace_a": True,
    "gap_grace_mins_a": 45,
    "use_atr_exit_a": True,
    "atr_period_a": 100,
    "use_atr_trail_only_after_tp_a": False,
    "use_atr_trail_only_after_tp_s1_a": False,
    "use_atr_trail_only_after_tp_s2_a": False,
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
    "s1_a_take_profit": 5.0,
    "use_s1_a_stop_loss": True,
    "s1_a_stop_loss": 2.0,
    "use_s1_b_tp": True,
    "s1_b_take_profit": 5.0,
    "use_s1_b_stop_loss": True,
    "s1_b_stop_loss": 2.0,
    "use_s2_tp": True,
    "s2_take_profit": 5.0,
    "use_s2_stop_loss": True,
    "s2_stop_loss": 3.0,
    "use_s1_a_flash_exit": True,
    "s1_a_flash_exit_pct": 5.0,
    "use_s1_b_flash_exit": True,
    "s1_b_flash_exit_pct": 2.0,
    "use_s2_flash_exit": True,
    "s2_flash_exit_pct": 2.0,
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
    "atr_multiplier_sl_a": 2.5,
    "atr_multiplier_sl_b": 2.5,
    "atr_multiplier_trail_a": 8.0,
    "atr_multiplier_trail_b": 5.0,
    "use_sel_track_a": True,
    "use_sel_track_b": True,
    "max_slots_a": 20,
    "max_slots_b": 15,
    "sel_b_min_trde_val": 45.0,
    "sel_b_rsi_min": 45.0,
    "sel_b_rsi_max": 75.0,
    "sel_b_vol_ratio": 1.5,
    "sel_b_vol_lookback": 7.0,
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
    # 일일결산: 장마감(15:30) + 버퍼(분) 이후 생성분을 '완료'로 판정, 배치 시작일 UI 기억용
    "daily_report_session_buffer_minutes": 10,
    "daily_settlement_batch_start_date": "2026-04-05",
    "min_ask_bid_ratio_entry": 1.5,
    "min_vol_power_entry": 120.0,
    "power_sustained_min": 2,
    "smart_exit_bid_ratio": 1.5,
    "limit_order_unfilled_timeout_sec": 5.0,
    "bt_start_date": "2024-04-01",
    "bt_end_date": "2024-12-31",
    "mock_account_id": "81220142",
    # 계좌 동기화 시 kt00007 체결내역을 주문 로그에 몇 일치 병합할지(1~60)
    "exchange_fill_sync_lookback_days": 7,
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
