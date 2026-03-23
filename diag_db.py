import time
import pandas as pd
import sqlite3
import os
from data_manager_v2 import DataManagerV2

def diag():
    db_path = "trading_data.db"
    if not os.path.exists(db_path):
        print(f"Error: {db_path} not found.")
        return

    db = DataManagerV2(db_path)
    code = '005930'
    
    print(f"[*] Measuring get_bars for {code} (Full fetch)...")
    start_t = time.time()
    df = db.get_bars(code)
    end_t = time.time()
    
    print(f"[Result] Rows: {len(df)}")
    print(f"[Result] Time taken: {end_t - start_t:.4f} seconds")
    
    with sqlite3.connect(db_path) as conn:
        print("\n[*] Table Schema:")
        schema = conn.execute("PRAGMA table_info('bars_1m')").fetchall()
        for s in schema: print(f"  - {s}")

        print("\n[*] Index List:")
        indexes = conn.execute("PRAGMA index_list('bars_1m')").fetchall()
        for idx in indexes:
            print(f"  - {idx}")
            info = conn.execute(f"PRAGMA index_info('{idx[1]}')").fetchall()
            print(f"    - Columns: {info}")

        print("\n[*] EXPLAIN QUERY PLAN (By Code):")
        plan = conn.execute(f"EXPLAIN QUERY PLAN SELECT * FROM bars_1m WHERE code = '{code}'").fetchall()
        for p in plan: print(f"  - {p}")

        print("\n[*] Row count for 005930 via raw SQL:")
        start_t = time.time()
        cnt = conn.execute(f"SELECT COUNT(*) FROM bars_1m WHERE code = '{code}'").fetchone()[0]
        end_t = time.time()
        print(f"  - Count: {cnt}, Time: {end_t - start_t:.4f}s")

if __name__ == "__main__":
    diag()
