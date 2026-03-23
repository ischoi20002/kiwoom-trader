import sys
import os
from datetime import datetime

# backtest_engine 경로 추가
sys.path.append(os.getcwd())
from backtest_engine import BacktestDataLoader

def test_cache_logic():
    # 실제 캐시 디렉토리를 사용하거나 임시 디렉토리를 사용
    loader = BacktestDataLoader(cache_dir="backtest_data")
    
    # 1. 2025년 데이터 (이미 캐시에 있어야 함)
    code = "005930" # 삼성전자
    start_date = "2025-04-01"
    end_date = "2026-03-31"
    
    print(f"--- First Request: {start_date} ~ {end_date} ---")
    df1 = loader.get_data(code, start_date, end_date, interval='15m')
    if df1 is not None:
        print(f"SUCCESS: Got {len(df1)} bars.")
    
    # 2. 2024년 데이터 (API 한계 밖의 날짜 요청)
    start_date_old = "2024-01-01"
    print(f"\n--- Second Request (Old Date): {start_date_old} ~ {end_date} ---")
    df2 = loader.get_data(code, start_date_old, end_date, interval='15m')
    if df2 is not None:
        print(f"SUCCESS: Got {len(df2)} bars.")
        print(f"First bar date in result: {df2.index.min()}")

if __name__ == "__main__":
    test_cache_logic()
