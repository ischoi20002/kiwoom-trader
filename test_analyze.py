import pandas as pd
import os

trade_csv = 'c:/바이브/backtest_trades.csv'
if not os.path.exists(trade_csv):
    print("No backtest_trades.csv found.")
else:
    df = pd.read_csv(trade_csv)
    
    buys = df[df['Action'].str.contains('BUY')]
    sells = df[df['Action'].str.contains('SELL|HOLD')]
    
    print("====== Type A 비활성화 백테스트 결과 분석 ======\n")
    print(f"총 매매 진입 횟수: {len(buys)}건")
    if len(buys) > 0:
        print("\n--- 진입(매수) 사유 분포 ---")
        print(buys['Reason'].str.split('|').str[0].value_counts().to_string())
        
    print(f"\n총 청산(매도/보유) 횟수: {len(sells)}건")
    if len(sells) > 0:
        wins = len(sells[sells['Return'] > 0])
        total = len(sells)
        win_rate = (wins / total) * 100
        avg_ret = sells['Return'].mean()
        
        print(f"승률: {wins}/{total} ({win_rate:.2f}%)")
        print(f"평균 수익률(건당): {avg_ret:.2f}%")
        
        print("\n--- 청산(매도) 사유 분포 ---")
        print(sells['Reason'].str.split('|').str[0].value_counts().to_string())
    
    # Check if there are any TYPE_A leftovers
    type_a_leftovers = buys[buys['Reason'].str.contains('TYPE_A')]
    if len(type_a_leftovers) > 0:
        print(f"\n[경고] TYPE_A 진입이 {len(type_a_leftovers)}건 검출되었습니다 (비활성화 상태여야 함).")
        print(type_a_leftovers[['Date', 'Code', 'Reason']].head())
    else:
        print("\n[정상] TYPE_A 진입 건수가 0건으로 깔끔하게 비활성화 되었습니다.")
