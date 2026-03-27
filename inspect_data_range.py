import sqlite3
import os
import pandas as pd

# [v3.5.1] Use dynamic path detection instead of hardcoded c:\바이브
db_path = os.path.join(os.path.dirname(__file__), 'trading_data.db')

def inspect_data():
    if not os.path.exists(db_path):
        print(f"FAIL: {db_path} not found.")
        return

    conn = sqlite3.connect(db_path)
    
    # 1. Check sync_meta status
    print(f"\n{'='*50}\n[INSPECT] sync_meta Sample\n{'='*50}")
    df_meta = pd.read_sql_query("SELECT * FROM sync_meta LIMIT 20", conn)
    print(df_meta)
    
    # 2. Check bars_1m data for a few samples
    target_stocks = ['005930', '000660', '005380', '001', '101']
    print(f"\n{'='*50}\n[INSPECT] bars_1m Data Summary\n{'='*50}")
    
    for code in target_stocks:
        print(f"\n--- CODE: {code} ---")
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM bars_1m WHERE code = ?", (code,))
        cnt = cursor.fetchone()[0]
        print(f"Count: {cnt}")
        
        if cnt > 0:
            df_head = pd.read_sql_query(f"SELECT * FROM bars_1m WHERE code = '{code}' ORDER BY datetime ASC LIMIT 5", conn)
            print("Earliest 5 rows:")
            print(df_head)
            
            df_tail = pd.read_sql_query(f"SELECT * FROM bars_1m WHERE code = '{code}' ORDER BY datetime DESC LIMIT 5", conn)
            print("\nLatest 5 rows:")
            print(df_tail)
        else:
            print("No data found in bars_1m.")

    conn.close()

if __name__ == "__main__":
    inspect_data()
