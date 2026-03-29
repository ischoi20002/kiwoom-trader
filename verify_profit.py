import pandas as pd
import numpy as np

def verify():
    if not pd.io.common.file_exists('backtest_trades.csv'):
        print("Error: backtest_trades.csv not found")
        return

    df = pd.read_csv('backtest_trades.csv')
    if df.empty:
        print("Error: backtest_trades.csv is empty")
        return

    # Results container
    # [Profit, InvestedSum, Count]
    stats = {
        'S1_A': [0.0, 0.0, 0],
        'S1_B': [0.0, 0.0, 0],
        'S2': [0.0, 0.0, 0],
        'Total_A': [0.0, 0.0, 0],
        'Total_B': [0.0, 0.0, 0]
    }

    codes = df['Code'].unique()
    for c in codes:
        s_df = df[df['Code'] == c]
        inv = 0.0
        has_s2 = False
        sig = 'NONE'
        
        for _, r in s_df.iterrows():
            act = str(r['Action'])
            p = float(r['Price'])
            q = float(r['Qty'])
            f = float(r.get('Fee', 0)) if not pd.isna(r.get('Fee', 0)) else 0
            
            if 'BUY' in act:
                if any(x in act for x in ['BUY2', 'ADDON', 'S2']):
                    has_s2 = True
                else:
                    sig = str(r.get('Sig_Type', 'NONE'))
                inv += (p * q) + f
            
            elif act in ['SELL', 'HOLD']:
                # PROFIT = (Price*Qty - SellFee) - (InvestedSum already including BuyFees)
                profit = (p * q) - f - inv
                
                # Attribute
                if has_s2:
                    stats['S2'][0] += profit
                    stats['S2'][1] += inv
                    stats['S2'][2] += 1
                else:
                    if 'TYPE_A' in sig:
                        stats['S1_A'][0] += profit
                        stats['S1_A'][1] += inv
                        stats['S1_A'][2] += 1
                    elif 'TYPE_B' in sig:
                        stats['S1_B'][0] += profit
                        stats['S1_B'][1] += inv
                        stats['S1_B'][2] += 1
                
                if 'TYPE_A' in sig:
                    stats['Total_A'][0] += profit
                    stats['Total_A'][1] += inv
                elif 'TYPE_B' in sig:
                    stats['Total_B'][0] += profit
                    stats['Total_B'][1] += inv
                
                # Reset
                inv = 0.0
                has_s2 = False
                sig = 'NONE'

    print("=== [v4.6] PROFIT VERIFICATION RESULTS ===")
    print(f"{'Category':<15} | {'Yield':>8} | {'Profit (KRW)':>15} | {'Trades':>6}")
    print("-" * 55)
    for k, v in stats.items():
        yld = (v[0]/v[1]*100) if v[1] > 0 else 0
        print(f"{k:<15} | {yld:>7.2f}% | {int(v[0]):>13,}원 | {v[2]:>6}")
    
    total_net = sum(v[0] for k, v in stats.items() if k in ['S1_A', 'S1_B', 'S2'])
    print("-" * 55)
    print(f"Grand Total Net Profit: {int(total_net):,}원")

if __name__ == "__main__":
    verify()
