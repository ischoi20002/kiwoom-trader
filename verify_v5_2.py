import os
import sys
# 현재 디렉토리를 경로에 추가
sys.path.append(os.getcwd())

from backtest_engine import BacktestDataLoader
from datetime import datetime, timedelta

def test_kiwoom_data_fetch():
    loader = BacktestDataLoader(cache_dir="test_backtest_data")
    
    # 약 6개월 전부터 현재까지 15분봉 데이터 요청 (최소 20~30개 배치 필요)
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")
    code = "005930" # 삼성전자
    
    print(f"Testing Kiwoom Data Fetch for {code} from {start_date} to {end_date}...")
    df = loader.get_data(code, start_date, end_date, interval='15m')
    
    if df is not None and not df.empty:
        print(f"SUCCESS: Fetched {len(df)} bars.")
        print(f"First bar: {df.index[0]}")
        print(f"Last bar: {df.index[-1]}")
        
        # 연속성 체크 (날짜가 60일보다 앞서는지 확인)
        first_dt = df.index[0]
        days_diff = (datetime.now() - first_dt).days
        print(f"Earliest data is {days_diff} days old.")
        
        if days_diff > 60:
            print("VERIFIED: Successfully bypassed yfinance 60-day limit using Kiwoom REST API!")
        else:
            print("WARNING: Data fetched is within 60 days. Increase start_date for a better test.")
    else:
        print("FAILURE: No data fetched or empty DataFrame.")

if __name__ == "__main__":
    test_kiwoom_data_fetch()
