import pandas as pd
import time
import os
import sys
from datetime import datetime

# Add current directory to path
sys.path.append(os.getcwd())

from backtest_engine import DynamicBacktester

def test_dynamic_opt_v2():
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
    
    # Test over 1 month to see the impact
    start_date = "2024-01-01"
    end_date = "2024-01-31" 
    
    print(f"Starting True Batching (Phase 1 Fix) verification ({start_date} ~ {end_date})...")
    start_time = time.time()
    
    # Run the backtest
    tester.run(start_date, end_date, interval='15m')
    
    end_time = time.time()
    duration = end_time - start_time
    print(f"\nOptimization Phase 1 Fix (True Batching) Complete.")
    print(f"Total Duration for {end_date} (1 month): {duration:.2f} seconds")

if __name__ == "__main__":
    test_dynamic_opt_v2()
