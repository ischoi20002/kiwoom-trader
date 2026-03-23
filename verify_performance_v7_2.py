import time
import pandas as pd
import numpy as np
import os
import backtest_engine
from backtest_engine import PortfolioBacktester, ParameterOptimizer, SimpleBacktester

def generate_mock_data(code, days=100):
    dates = pd.date_range(end=pd.Timestamp.now(), periods=days, freq='D')
    df = pd.DataFrame({
        'Open': np.random.uniform(10000, 11000, days),
        'High': np.random.uniform(11000, 12000, days),
        'Low': np.random.uniform(9000, 10000, days),
        'Close': np.random.uniform(10000, 11000, days),
        'Volume': np.random.uniform(100000, 1000000, days)
    }, index=dates)
    # Add dummy MA/MACD/RSI to avoid recalculation if needed or just let it calculate once
    return df

class MockLoader:
    def get_data(self, code, *args, **kwargs):
        return generate_mock_data(code)

def test_results_and_performance():
    settings = {
        'rsi_min': 40, 'rsi_max': 52, 'vol_surge': 1.1,
        'macd_fast': 12, 'macd_slow': 26, 'macd_sig': 9,
        'rsi_sig_period': 9, 'ma60_min': 90, 'ma60_max': 120,
        'stop_loss': 3.0, 'take_profit': 5.0, 'flash_exit_pct': 3.0
    }
    codes = ['005930', '000660'] # Use real-ish looking codes
    
    # Cleanup previous logs
    for f in ["backtest_signals.csv", "backtest_trades.csv"]:
        if os.path.exists(f): os.remove(f)

    print("--- Testing PortfolioBacktester (Result Consistency) ---")
    tester = PortfolioBacktester(settings, codes)
    # Monkeypatch the loader instance
    tester.loader = MockLoader()
    
    start_time = time.time()
    tester.run_all("2024-01-01", "2024-12-31")
    duration = time.time() - start_time
    print(f"Portfolio Backtest Time: {duration:.2f}s")
    
    # Check if logs exist
    if os.path.exists("backtest_trades.csv"):
        trades = pd.read_csv("backtest_trades.csv")
        print(f"Successfully recorded {len(trades)} trades via batch logging.")
    else:
        print("Error: backtest_trades.csv not found!")

    print("\n--- Testing ParameterOptimizer (Logic & Speed) ---")
    param_ranges = {
        'rsi_min': [35, 40, 45],
        'ma60_min': [95, 100]
    }
    # We need to monkeypatch the BacktestDataLoader class itself to avoid real API calls
    original_loader = backtest_engine.BacktestDataLoader
    backtest_engine.BacktestDataLoader = MockLoader
    
    try:
        optimizer = ParameterOptimizer(codes, param_ranges, base_settings=settings)
        start_time = time.time()
        results = optimizer.find_best_params("2024-01-01", "2024-12-31")
        duration = time.time() - start_time
        print(f"Optimization Time (6 combos): {duration:.2f}s")
        if results is not None:
            print(f"Best avg_ret: {results['avg_ret'].iloc[0]:.2f}%")
    finally:
        backtest_engine.BacktestDataLoader = original_loader

if __name__ == "__main__":
    test_results_and_performance()
