import time
import pandas as pd
from datetime import datetime
from backtest_engine import DynamicBacktester, DataManagerV2

def run_benchmark():
    import json
    import os
    if os.path.exists('backtest_trades.csv'):
        os.remove('backtest_trades.csv')
        
    with open('settings.json', 'r', encoding='utf-8') as f:
        settings = json.load(f)
    
    # 19-day period (approx 3 weeks) with 15m data (2025-03-04 ~ 2026-03-19)
    start_date = "2026-02-20"
    end_date = "2026-03-15" # approx 16 trading days
    
    print(f"--- Starting Benchmark: {start_date} ~ {end_date} ---")
    tester = DynamicBacktester(settings)
    
    t0 = time.time()
    def benchmark_callback(msg, progress=None):
        if progress is not None:
            print(f"[{progress:4.1f}%] {msg}")
        else:
            print(f"[  -  ] {msg}")

    results = tester.run(start_date, end_date, interval='15m', callback=benchmark_callback)
    t1 = time.time()
    
    duration = t1 - t0
    print(f"\n--- Benchmark Completed ---")
    print(f"Total Duration: {duration:.2f} seconds")
    
    if results is not None:
        print(f"Total Trades: {len(results)}")
        if not results.empty:
            final_equity = tester.daily_equity[-1]['equity']
            ret = (final_equity / 10_000_000 - 1) * 100
            print(f"Final Equity: {final_equity:,.0f} ({ret:+.2f}%)")
    else:
        print("Backtest failed or returned no results.")

if __name__ == "__main__":
    run_benchmark()
