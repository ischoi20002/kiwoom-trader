import pandas as pd
import numpy as np

def analyze_buy1_only_failure_rate(csv_path):
    try:
        df = pd.read_csv(csv_path)
        df['Code'] = df['Code'].astype(str).str.zfill(6)
        df['Date'] = pd.to_datetime(df['Date'])
        df = df.sort_values(['Code', 'Date'])
        
        round_trips = []
        current_trip = {}
        
        for _, row in df.iterrows():
            action = row['Action']
            code = row['Code']
            
            if 'BUY1' in action:
                current_trip[code] = {'code': code, 'has_buy1': True, 'has_buy2': False, 'return': 0, 'completed': False, 'sig_type': row['Reason'].split(' ')[0]}
            elif 'BUY2' in action:
                if code in current_trip:
                    current_trip[code]['has_buy2'] = True
            elif 'SELL' in action or 'HOLD' in action:
                if code in current_trip:
                    trip = current_trip.pop(code)
                    trip['return'] = float(row['Return'])
                    trip['completed'] = True
                    round_trips.append(trip)
        
        if not round_trips:
            print("No completed round-trips found.")
            return
            
        trips_df = pd.DataFrame(round_trips)
        
        print(f"--- Analysis Results ---")
        print(f"Total Completed Trips: {len(trips_df)}")
        
        for sig in ['TYPE_A', 'TYPE_B']:
            sig_df = trips_df[trips_df['sig_type'] == sig]
            if sig_df.empty: continue
            
            sig_buy1_only = sig_df[~sig_df['has_buy2']]
            total_sig = len(sig_buy1_only)
            loss_sig = len(sig_buy1_only[sig_buy1_only['return'] < 0])
            loss_rate = (loss_sig / total_sig) * 100 if total_sig > 0 else 0
            avg_loss = sig_buy1_only[sig_buy1_only['return'] < 0]['return'].mean()
            
            print(f"\n[{sig} BUY1-only]")
            print(f"  - Total Count: {total_sig}")
            print(f"  - Loss-making: {loss_sig}")
            print(f"  - Failure Rate: {loss_rate:.2f}%")
            if not pd.isna(avg_loss):
                print(f"  - Avg Loss: {avg_loss:.2f}%")

    except Exception as e:
        print(f"Error during analysis: {e}")

if __name__ == "__main__":
    analyze_buy1_only_failure_rate('backtest_trades.csv')
