import json
import time
import pandas as pd
from backtest_engine import PortfolioBacktester
from data_manager_v2 import DataManagerV2

def main():
    with open('settings.json', 'r', encoding='utf-8') as f:
        settings = json.load(f)
    
    # KODEX 200 종목 중 상위 40개 선정
    codes = []
    try:
        df_kodex = pd.read_csv('kodex200.csv')
        codes = df_kodex['Code'].iloc[:40].tolist()
    except:
        # 폴백: 직접 지정 (일부 종목)
        codes = ['005930', '000660', '035420', '035720', '005380', '005490', '055550', '012330'] * 5
    
    print(f"Benchmark: PortfolioBacktester with {len(codes)} stocks")
    print(f"Period: 2025-01-01 ~ 2025-03-31")
    
    tester = PortfolioBacktester(settings, codes)
    
    start_time = time.time()
    # 고정밀 모드 off로 먼저 테스트 (충분한 효과 확인 가능)
    tester.run_all('2025-01-01', '2025-03-31', high_precision=False)
    end_time = time.time()
    
    print(f"\nPortfolio Backtest Duration: {end_time - start_time:.2f} seconds")

if __name__ == "__main__":
    main()
