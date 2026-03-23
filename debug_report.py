import pandas as pd
import os

TRADE_CSV = "backtest_trades.csv"
REPORT_HTML = "trade_report.html"

print(f"--- Checking {TRADE_CSV} ---")
if os.path.exists(TRADE_CSV):
    try:
        df = pd.read_csv(TRADE_CSV, dtype={'Code': str})
        print(f"Total Rows: {len(df)}")
        print(f"Unique Codes: {df['Code'].unique()}")
        print(f"Sample Rows (First 5):\n{df.head()}")
    except Exception as e:
        print(f"Error reading CSV: {e}")
else:
    print("CSV not found.")

print(f"\n--- Checking {REPORT_HTML} ---")
if os.path.exists(REPORT_HTML):
    try:
        with open(REPORT_HTML, "r", encoding="utf-8") as f:
            content = f.read()
            # Count occurrences of stock groups or names
            import re
            stocks = re.findall(r'id="stock-([0-9]{6})"', content)
            print(f"Stocks in HTML: {len(stocks)}")
            print(f"Stock Codes in HTML: {stocks}")
            
            # Check for hardcoded feeling
            if "005930" in content: print("(!) Samsung Electronics (005930) found in HTML")
            if "000660" in content: print("(!) SK Hynix (000660) found in HTML")
    except Exception as e:
        print(f"Error reading HTML: {e}")
else:
    print("HTML not found.")
