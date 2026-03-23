import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor as TPE
import time

class MockLoader:
    def get_data(self, code, start, end, interval, force_refresh=False):
        print(f"  [Mock] Fetching {code}...")
        time.sleep(0.5)
        print(f"  [API] Successfully fetched 100 bars from Kiwoom.")
        return pd.DataFrame()

def test_hang():
    codes = ['A', 'B', 'C']
    total_stocks = len(codes)
    total_finished = 0
    loader = MockLoader()
    
    def callback(msg, val):
        print(f"CALLBACK: {msg} ({val}%)")

    def load_stock_data(code):
        nonlocal total_finished
        res = loader.get_data(code, "2025-01-01", "2025-01-10", "15m")
        total_finished += 1
        msg = f"데이터 준비 완료 ({total_finished}/{total_stocks}): {code}"
        print(f"  -> {msg}")
        callback(msg, (total_finished / total_stocks) * 50)
        return code, res

    print("Starting TPE...")
    with TPE(max_workers=3) as executor:
        results = list(executor.map(load_stock_data, codes))
    print("FINISHED")

if __name__ == "__main__":
    test_hang()
