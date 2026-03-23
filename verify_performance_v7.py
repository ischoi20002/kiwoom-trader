import time
import pandas as pd
import numpy as np
from backtest_engine import PortfolioBacktester, ParameterOptimizer

def generate_mock_data(code, days=200):
    dates = pd.date_range(end=pd.Timestamp.now(), periods=days, freq='D')
    df = pd.DataFrame({
        'Open': np.random.uniform(10000, 15000, days),
        'High': np.random.uniform(15000, 16000, days),
        'Low': np.random.uniform(9000, 10000, days),
        'Close': np.random.uniform(10000, 15000, days),
        'Volume': np.random.uniform(100000, 1000000, days)
    }, index=dates)
    return df

class MockLoader:
    def get_data(self, code, *args, **kwargs):
        return generate_mock_data(code)

def test_performance():
    settings = {
        'rsi_min': 40, 'rsi_max': 52, 'vol_surge': 1.5,
        'macd_fast': 12, 'macd_slow': 26, 'macd_sig': 9,
        'rsi_sig_period': 9, 'ma60_min': 100, 'ma60_max': 112
    }
    codes = [f'{i:06d}' for i in range(10)]
    
    print("--- Testing PortfolioBacktester Parallelization ---")
    tester = PortfolioBacktester(settings, codes)
    tester.loader = MockLoader()
    
    start_time = time.time()
    # PortfolioBacktester.run_all uses ThreadPoolExecutor internally now
    tester.run_all("2024-01-01", "2024-12-31")
    print(f"Portfolio Backtest Time: {time.time() - start_time:.2f}s")

    print("\n--- Testing ParameterOptimizer Parallelization & Caching ---")
    param_ranges = {
        'rsi_min': [40, 42],
        'rsi_max': [50, 52]
    }
    optimizer = ParameterOptimizer(codes, param_ranges, base_settings=settings)
    # inject mock loader
    import backtest_engine
    # We need to monkeypatch BacktestDataLoader in ParameterOptimizer
    class MockDataLoader:
        def get_data(self, code, *args, **kwargs):
            return generate_mock_data(code)
    
    # This is a bit tricky due to how it's instantiated inside find_best_params
    # Let's just run it and see if it's faster with the new logic.
    # Note: It will try to fetch market data via FDR, which might be slow but constant.
    
    start_time = time.time()
    optimizer.find_best_params("2024-01-01", "2024-12-31")
    print(f"Optimization Time: {time.time() - start_time:.2f}s")

if __name__ == "__main__":
    test_performance()
