import pandas as pd
import numpy as np

def audit():
    df = pd.read_csv('backtest_trades.csv')
    df = df[df['Action'] != 'REJECTED_SIG'] # 거부된 신호 제외
    
    codes = df['Code'].unique()
    audit_log = []
    
    for c in codes:
        s_df = df[df['Code'] == c]
        name = s_df['Name'].iloc[0]
        inv_sum = 0.0
        buy_count = 0
        
        for _, r in s_df.iterrows():
            act = str(r['Action'])
            p = float(r['Price'])
            q = float(r['Qty'])
            f = float(r.get('Fee', 0)) if not pd.isna(r.get('Fee', 0)) else 0
            dt = r['Date']
            
            if 'BUY' in act or 'ADDON' in act:
                cost = (p * q) + f
                inv_sum += cost
                buy_count += 1
                audit_log.append(f"  [{dt}] BUY  | {name} ({c}) | Price: {p:,.0f} | Qty: {q:.0f} | Fee: {f:,.0f} | Cost: -{cost:,.0f}")
            
            elif act in ['SELL', 'HOLD']:
                proceeds = (p * q) - f
                profit = proceeds - inv_sum
                audit_log.append(f"  [{dt}] {act:<4} | {name} ({c}) | Price: {p:,.0f} | Qty: {q:.0f} | Fee: {f:,.0f} | Net: +{proceeds:,.0f}")
                audit_log.append(f"  >>> SEQUENCE PROFIT: {int(profit):,}원")
                inv_sum = 0.0
                buy_count = 0

    print("\n=== FULL TRADE AUDIT LOG (BUY -> SELL/HOLD) ===")
    for line in audit_log:
        print(line)
    
    total_raw_profit = 0
    # Report style calculation
    for _, r in df.iterrows():
        act = str(r['Action'])
        amt = float(r['Price']) * float(r['Qty'])
        fee = float(r.get('Fee', 0)) if not pd.isna(r.get('Fee', 0)) else 0
        if 'BUY' in act or 'ADDON' in act:
            total_raw_profit -= (amt + fee)
        elif act in ['SELL', 'HOLD']:
            total_raw_profit += (amt - fee)
            
    print("\n" + "="*50)
    print(f"CALCULATED TOTAL PROFIT: {int(total_raw_profit):,}원")
    print(f"TOTAL FILLED EXIT TRADES: {len(df[df['Action'].isin(['SELL', 'HOLD'])])}")
    print("="*50)

if __name__ == "__main__":
    audit()
