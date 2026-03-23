import sqlite3
import os

db_path = "trading_data.db"
if not os.path.exists(db_path):
    print(f"Error: {db_path} not found.")
else:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print("--- bars_1d (KS11/001) ---")
    cursor.execute("SELECT code, MAX(datetime) FROM bars_1d WHERE code IN ('KS11', '001') GROUP BY code")
    print(cursor.fetchall())
    
    print("--- bars_15m (KS11/001) ---")
    cursor.execute("SELECT code, MAX(datetime) FROM bars_15m WHERE code IN ('KS11', '001') GROUP BY code")
    print(cursor.fetchall())
    
    print("--- sync_meta ---")
    cursor.execute("SELECT code, last_sync_dt FROM sync_meta WHERE code IN ('KS11', '001')")
    print(cursor.fetchall())
    
    conn.close()
