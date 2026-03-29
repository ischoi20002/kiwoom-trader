import json
import pandas as pd
from dynamic_backtest import DynamicBacktester
from data_manager_v2 import DataManagerV2

def main():
    # 1. 설정 로드
    with open('settings.json', 'r', encoding='utf-8') as f:
        settings = json.load(f)
    
    print(f"--- [Type A Optimization Test] ---")
    print(f"Target Period: {settings.get('bt_start_date')} ~ {settings.get('bt_end_date')}")
    print(f"Type A Settings: RSI Max={settings.get('buy_rsi_max_a')}, SB={settings.get('use_smart_buy_a')}")
    print(f"Exit Strategy: TP={settings.get('s1_a_take_profit')}%, SL={settings.get('s1_a_stop_loss')}%")
    
    # 2. 테스터 초기화
    tester = DynamicBacktester(settings)
    
    # 3. 백테스트 실행
    start_date = settings.get('bt_start_date', '2025-04-01')
    end_date = settings.get('bt_end_date', '2025-12-31')
    
    print("\nStarting Simulation...")
    tester.run(start_date, end_date, high_precision=True)
    
    # 4. 결과 분석 및 출력
    df = pd.DataFrame(tester.trades_history)
    if df.empty:
        print("No trades executed.")
        return

    sells = df[(df['Sig_Type'] == 'TYPE_A') & (df['Action'] == 'SELL')].copy()
    if not sells.empty:
        avg_ret = sells['Return'].mean()
        win_rate = (len(sells[sells['Return'] > 0]) / len(sells)) * 100
        print(f"\n[Result: Stage 1 Type A]")
        print(f"Total Trades: {len(sells)}")
        print(f"Avg Return: {avg_ret:.4f}%")
        print(f"Win Rate: {win_rate:.2f}%")
        
        # [v34.2] 성과 비교용 가이드
        if avg_ret > 0.41:
            print(f"\n>>> SUCCESS! New record: {avg_ret:.4f}% (Better than 0.41%)")
        else:
            print(f"\n>>> Needs more tuning. Current: {avg_ret:.4f}% (Baseline: 0.41%)")
    else:
        print("No Type A SELL trades recorded.")

if __name__ == "__main__":
    main()
