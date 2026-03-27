import FinanceDataReader as fdr
import pandas as pd
import os

# [v3.5.1] Use dynamic path detection
vix_path = os.path.join(os.path.dirname(__file__), 'backtest_data', 'vix_master.csv')
if os.path.exists(vix_path):
    df_vix = pd.read_csv(vix_path, index_col=0, parse_dates=True)
    last_date = df_vix.index.max().strftime('%Y-%m-%d')
    print(f"Current VIX last date: {last_date}")
    
    # Fetch new data
    df_new = fdr.DataReader('VIX', last_date, '2026-03-17')
    if not df_new.empty:
        df_combined = pd.concat([df_vix, df_new])
        df_combined = df_combined[~df_combined.index.duplicated(keep='last')].sort_index()
        df_combined.to_csv(vix_path)
        print(f"Updated VIX last date: {df_combined.index.max().strftime('%Y-%m-%d')}")
    else:
        print("No new VIX data found.")
else:
    print("VIX master not found.")
