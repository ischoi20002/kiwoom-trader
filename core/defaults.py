# -*- coding: utf-8 -*-

"""
[v4.10.0] Vibe Project DNA - 전략 파라미터 기본값 및 설정 상수 관리
사용자의 현재 설정값(settings.json)을 기반으로 프로그램 기본값을 동기화했습니다.
"""

# 주력 전략 기본 파라미터 (Settings Tab에 표시되는 순서와 일치)
DEFAULT_SETTINGS = {
    "backtest_initial_capital": 10000000,
    "max_slots": 30,
    "stage1_amt": 20.0,
    "s1_equity_pct": 20.0,
    "s1_sizing_unit": "%",
    "stage2_amt": 30.0,
    "s2_equity_pct": 30.0,
    "s2_sizing_unit": "%",
    "use_auto_virtual": True,
    
    "min_trde_val": 50.0,
    "vol_ratio": 1.5,
    "vol_lookback": 10.0,
    "ma_convergence": 5.0,
    "sel_rsi_max": 70.0,
    "sel_rsi_min": 30.0,
    "macd_fast_daily": 12,
    "macd_slow_daily": 26,
    "macd_sig_daily": 9,
    
    "use_market_filter": True,
    "use_rsi_min": True,
    "buy_rsi_min": 30.0,
    "use_macd_signal": True,
    "macd_fast_entry": 12,
    "macd_slow_entry": 26,
    "macd_sig_entry": 9,
    "use_rsi_sig_filter": True,
    "rsi_sig_period": 9.0,
    
    "use_type_a": True,
    "buy_rsi_max_a": 65.0,
    "use_smart_buy_a": False,
    "use_gc": True,
    "gc_window": 90.0,
    "use_rsi_signal": True,
    "rsi_window": 45.0,
    "use_ma60_safe": True,
    "ma60_min": 100.0,
    "ma60_max": 110.0,
    "use_vol_surge": True,
    "vol_surge": 1.5,
    "use_atr_stop_loss_a": False,
    "use_atr_trailing_stop_a": False,
    
    "use_type_b": True,
    "buy_rsi_max_b": 70.0,
    "use_smart_buy_b": True,
    "use_type_b_vol": True,
    "vol_surge_b": 2.0,
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
    "s2_profit_limit": 1.0,
    "use_smart_buy_s2": True,
    "use_s2_delay": True,
    "s2_delay_hours": 24.0,
    "use_atr_stop_loss_s2": True,
    "atr_multiplier_sl_s2": 2.5,
    "use_atr_trailing_stop_s2": True,
    "atr_multiplier_trail_s2": 4.0,
    
    "smart_buy_vp": 125.0,
    "smart_buy_ba_ratio": 1.25,
    "use_smart_exit": True,
    "smart_exit_vp": 90.0,
    "smart_exit_ba_ratio": 1.3,
    "smart_exit_price_threshold": -0.5,
    
    "use_gap_down_exit": True,
    "gap_down_limit": -5.0,
    "use_gap_grace": True,
    "use_atr_exit": True,
    "atr_period": 100,
    
    "use_s1_a_tp": True,
    "s1_a_take_profit": 5.0,
    "use_s1_a_stop_loss": True,
    "s1_a_stop_loss": 3.0,
    "use_s1_b_tp": True,
    "s1_b_take_profit": 4.0,
    "use_s1_b_stop_loss": True,
    "s1_b_stop_loss": 3.0,
    "use_s2_tp": True,
    "s2_take_profit": 6.0,
    "use_s2_stop_loss": True,
    "s2_stop_loss": 3.0,
    "use_s1_a_flash_exit": True,
    "s1_a_flash_exit_pct": 3.0,
    "use_s1_b_flash_exit": True,
    "s1_b_flash_exit_pct": 3.0,
    "use_s2_flash_exit": True,
    "s2_flash_exit_pct": 3.0,
    
    # ATR Multipliers (Global)
    "atr_multiplier_sl_a": 2.5,
    "atr_multiplier_sl_b": 2.5,
    "atr_multiplier_trail_a": 5.0,
    "atr_multiplier_trail_b": 4.0,
}

# [Zombie Parameter Prevention] 유효 키 목록
ALLOWED_EXTRA_KEYS = {
    'bt_start_date', 'bt_end_date', 
    's1_sizing_unit', 's2_sizing_unit', 
    'mock_account_id', 'backtest_initial_capital',
    'use_auto_virtual',
    'buy_fee_bt', 'sell_fee_bt',
    'buy_fee_mock', 'sell_fee_mock'
}

def get_allowed_keys(current_settings=None):
    """설정 저장 시 필터링에 사용할 전체 유효 키 셋 반환"""
    base = set(DEFAULT_SETTINGS.keys()).union(ALLOWED_EXTRA_KEYS)
    if current_settings:
        # [v16.62] opt_ 로 시작하는 최적화 상태 키들도 허용 목록에 포함
        opt_keys = {k for k in current_settings.keys() if k.startswith('opt_')}
        return base.union(opt_keys)
    return base
