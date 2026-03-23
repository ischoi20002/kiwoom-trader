import sqlite3
import pandas as pd

db_path = r"C:\바이브\trading_data.db"
conn = sqlite3.connect(db_path)

print("--- 1m Index Counts ---")
try:
    df1 = pd.read_sql_query("SELECT code, COUNT(*) as count FROM bars_1m WHERE code IN ('001', '101') GROUP BY code", conn)
    print(df1)
except Exception as e:
    print(f"Error 1m: {e}")

print("\n--- 15m Index Counts ---")
try:
    df2 = pd.read_sql_query("SELECT code, COUNT(*) as count FROM bars_15m WHERE code IN ('001', '101') GROUP BY code", conn)
    print(df2)
except Exception as e:
    print(f"Error 15m: {e}")

conn.close()
