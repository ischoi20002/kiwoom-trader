import pandas as pd
import numpy as np

def analyze_a_entry_quality():
    try:
        df = pd.read_csv('backtest_trades.csv')
        df = df[df['Action'] != 'REJECTED_SIG']
        
        codes = df['Code'].unique()
        results = []
        
        for c in codes:
            s_df = df[df['Code'] == c]
            entry_row = None
            has_s2 = False
            
            for _, r in s_df.iterrows():
                act = str(r['Action'])
                reason = str(r.get('Reason', ''))
                
                if 'BUY' in act and 'TYPE_A' in reason and not ('Stage 2' in reason or 'S2' in reason):
                    entry_row = r
                
                if entry_row is not None and ('Stage 2' in reason or 'S2' in reason):
                    has_s2 = True
                
                if entry_row is not None and act in ['SELL', 'HOLD']:
                    # Extract Smart metrics from Reason string if possible
                    # Reason example: 1M_SmartBuy|[TYPE_A] RSI_CROSS|RSI:42.5|BA:1.1|VP:130
                    reason_str = str(entry_row.get('Reason', ''))
                    ba = 0.0
                    vp = 0.0
                    try:
                        if 'BA:' in reason_str:
                            ba = float(reason_str.split('BA:')[1].split('|')[0])
                        if 'VP:' in reason_str:
                            vp = float(reason_str.split('VP:')[1].split('|')[0])
                    except: pass
                    
                    results.append({
                        'Has_S2': has_s2,
                        'BA': ba,
                        'VP': vp,
                        'Profit': (float(r['Price'])*float(r['Qty'])) - float(r.get('Fee',0))
                    })
                    entry_row = None
                    has_s2 = False

        res_df = pd.DataFrame(results)
        if res_df.empty:
            print("No data found.")
            return

        print("\n=== Type A Entry Quality Analysis (BA/VP at Entry) ===")
        for s2 in [False, True]:
            sub = res_df[res_df['Has_S2'] == s2]
            label = "S1+S2 (Profit Engine)" if s2 else "S1 Only (Mediocre)"
            print(f"\nGroup: {label} ({len(sub)} samples)")
            print(f"  Avg BA: {sub['BA'].mean():.2f}")
            print(f"  Avg VP: {sub['VP'].mean():.1f}")
            print(f"  BA Range: {sub['BA'].min():.2f} ~ {sub['BA'].max():.2f}")
            print(f"  VP Range: {sub['VP'].min():.1f} ~ {sub['VP'].max():.1f}")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    analyze_a_entry_quality()
