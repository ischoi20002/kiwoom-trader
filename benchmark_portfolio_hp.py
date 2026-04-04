import json
import time
import pandas as pd
from backtest_engine import PortfolioBacktester

def main():
    with open('settings.json', 'r', encoding='utf-8') as f:
        settings = json.load(f)
    
    # KODEX 200 상위 10개만 (고정밀은 데이터 로드가 많음)
    codes = []
    try:
        df_kodex = pd.read_csv('kodex200.csv')
        codes = df_kodex['Code'].iloc[:10].tolist()
    except:
        codes = ['005930', '000660', '035420', '035720', '005380'] * 2
    
    print(f"Benchmark: PortfolioBacktester [High-Precision] with {len(codes)} stocks")
    print(f"Period: 2025-01-01 ~ 2025-01-15") # 2주간
    
    tester = PortfolioBacktester(settings, codes)
    
    start_time = time.time()
    tester.run_all('2025-01-01', '2025-01-15', high_precision=True)
    end_time = time.time()
    
    print(f"\n[High-Precision] Portfolio Backtest Duration: {end_time - start_time:.2f} seconds")

if __name__ == "__main__":
    main()
