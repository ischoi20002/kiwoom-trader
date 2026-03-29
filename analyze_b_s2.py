import pandas as pd
import numpy as np

def analyze_b_s2_profit():
    try:
        df = pd.read_csv('backtest_trades.csv')
        df = df[df['Action'] != 'REJECTED_SIG']
        
        codes = df['Code'].unique()
        b_s2_trades = []
        
        for c in codes:
            s_df = df[df['Code'] == c]
            entry_type = None
            is_s2_triggered = False
            inv_sum = 0
            
            for _, r in s_df.iterrows():
                act = str(r['Action'])
                p = float(r['Price'])
                q = float(r['Qty'])
                fee = float(r.get('Fee', 0))
                reason = str(r.get('Reason', ''))
                
                if 'BUY' in act:
                    if 'TYPE_B' in reason and not ('Stage 2' in reason or 'S2' in reason):
                        entry_type = 'B'
                    
                    if entry_type == 'B' and ('Stage 2' in reason or 'S2' in reason):
                        is_s2_triggered = True
                    
                    inv_sum += (p * q) + fee
                
                elif act in ['SELL', 'HOLD']:
                    if entry_type == 'B' and is_s2_triggered:
                        profit = (p * q) - fee - inv_sum
                        b_s2_trades.append({
                            'Date': r['Date'],
                            'Code': c,
                            'Name': s_df['Name'].iloc[0],
                            'Profit': profit,
                            'Reason': reason
                        })
                    
                    # Reset
                    entry_type = None
                    is_s2_triggered = False
                    inv_sum = 0
        
        print("\n=== Type B(추세돌파) -> Stage 2(추매) 상세 수익 분석 ===")
        total_p = 0
        for i, t in enumerate(b_s2_trades):
            print(f"{i+1}. [{t['Date']}] {t['Name']}({t['Code']}) | 수익: {int(t['Profit']):,}")
            total_p += t['Profit']
        
        print(f"\nType B S2 총 수익금: {int(total_p):,} 원")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    analyze_b_s2_profit()
