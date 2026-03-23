import pandas as pd
import time
import os
import sys
from datetime import datetime

# Add current directory to path
sys.path.append(os.getcwd())

from backtest_engine import DynamicBacktester
import strategy_selector

def test_dynamic_opt_v3():
    settings = {
        'total_capital': 10000000,
        'max_slots': 10,
        'stage1_amt': 1000000,
        'stage2_amt': 2000000,
        'buy_fee_rate': 0.015,
        'sell_fee_rate': 0.195,
        'min_trde_val': 40.0,
        'ma_convergence': 0.03,
        'rsi_min': 40.0,
        'rsi_max': 70.0,
        'vol_ratio': 1.5,
        'vol_lookback': 10,
        'macd_fast': 12,
        'macd_slow': 26,
        'macd_sig': 9,
        'take_profit': 5.0,
        'stop_loss': 3.0,
        'rsi_exit': 50.0
    }
    
    tester = DynamicBacktester(settings)
    
    start_date = "2024-01-01"
    end_date = "2024-01-31" 
    
    print(f"\n[Measurement] Starting Phase 1 Fix verification ({start_date} ~ {end_date})...")
    
    # 1. Measure prepare_dynamic_universe
    print("\n[Step 1] Measuring prepare_dynamic_universe...")
    s1 = time.time()
    universe_schedule = strategy_selector.prepare_dynamic_universe(
        settings, start_date, end_date, tester.db, print
    )
    e1 = time.time()
    print(f"[Step 1 Duration] {e1 - s1:.2f} seconds")

    # 2. Measure the loop speed (approx)
    print("\n[Step 2] Measuring Loop Speed (Total duration of run)...")
    s2 = time.time()
    tester.run(start_date, end_date, interval='15m')
    e2 = time.time()
    print(f"[Step 2 Duration] {e2 - s2:.2f} seconds")

if __name__ == "__main__":
    test_dynamic_opt_v3()
