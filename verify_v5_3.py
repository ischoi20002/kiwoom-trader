import pandas as pd
from backtest_engine import BacktestDataLoader, SimpleBacktester
from datetime import datetime, timedelta

def test_long_term_fetch():
    loader = BacktestDataLoader(cache_dir="test_backtest_data_long")
    
    start_date = "2025-04-01"
    end_date = "2026-03-31"
    code = "005935" # 삼성전자우
    
    print(f"Testing Long-term Fetch for {code} from {start_date} to {end_date}...")
    
    # 1. 데이터 수집 테스트 (50,000개 제한 확인)
    df = loader.get_data(code, start_date, end_date, interval='15m')
    
    if df is not None and not df.empty:
        print(f"SUCCESS: Fetched {len(df)} bars.")
        print(f"First bar: {df.index[0]}")
        print(f"Last bar: {df.index[-1]}")
        
        # 2. 신호 발생 테스트
        settings = {
            'rsi_min': 30, 'rsi_max': 70, # 유저가 설정한 넓은 범위
            'macd_fast': 12, 'macd_slow': 26, 'macd_sig': 9
        }
        tester = SimpleBacktester(settings)
        result_df, summary = tester.run(df, code)
        
        if summary and summary['trades'] > 0:
            print(f"SUCCESS: Generated {summary['trades']} trades.")
        else:
            print("FAILURE: Still no trades generated. Check debug logs above.")
    else:
        print("FAILURE: No data fetched or empty DataFrame.")

if __name__ == "__main__":
    test_long_term_fetch()
