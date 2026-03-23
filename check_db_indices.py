import sqlite3
conn = sqlite3.connect('trading_data.db')
cursor = conn.cursor()
for table in ['bars_15m', 'bars_1d']:
    print(f"\nIndices for {table}:")
    cursor.execute(f"PRAGMA index_list({table})")
    indices = cursor.fetchall()
    for idx in indices:
        print(idx)
        cursor.execute(f"PRAGMA index_info({idx[1]})")
        print(cursor.fetchall())
conn.close()
