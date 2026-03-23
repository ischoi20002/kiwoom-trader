import sqlite3
import pandas as pd
import os

db_path = r"C:\바이브\trading_data.db"

def analyze():
    if not os.path.exists(db_path):
        print(f"DATABASE NOT FOUND: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    
    # 1. Schema check
    print("--- SCHEMA CHECK ---")
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(bars_1m)")
    cols = cursor.fetchall()
    for col in cols:
        print(f"Column: {col[1]}, Type: {col[2]}")

    # 2. Row count by code
    print("\n--- ROW COUNT CHECK ---")
    query = "SELECT code, COUNT(*) as count, MIN(datetime), MAX(datetime) FROM bars_1m GROUP BY code"
    df_counts = pd.read_sql_query(query, conn)
    print(df_counts)

    # 3. Leading zero check in DB
    print("\n--- LEADING ZERO CHECK ---")
    cursor.execute("SELECT DISTINCT code FROM bars_1m")
    codes = [row[0] for row in cursor.fetchall()]
    print(f"Unique Codes in DB: {codes}")

    # 4. Market Data Check
    print("\n--- MARKET DATA CHECK (bars_1m) ---")
    cursor.execute("SELECT code, COUNT(*) FROM bars_1m WHERE code IN ('001', '101') GROUP BY code")
    print(f"Index Rows (1m): {cursor.fetchall()}")

    print("\n--- MARKET DATA CHECK (bars_15m) ---")
    cursor.execute("SELECT code, COUNT(*) FROM bars_15m WHERE code IN ('001', '101') GROUP BY code")
    print(f"Index Rows (15m): {cursor.fetchall()}")

    conn.close()

analyze()
