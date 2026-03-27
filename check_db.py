import sqlite3
import os

db_path = 'trading_data.db'
if not os.path.exists(db_path):
    print(f"Error: {db_path} not found.")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

try:
    cursor.execute("SELECT COUNT(*) FROM realtime_snapshots")
    total = cursor.fetchone()[0]
    print(f"Total Snapshots: {total}")

    cursor.execute("SELECT code, COUNT(*) FROM realtime_snapshots GROUP BY code ORDER BY COUNT(*) DESC LIMIT 20")
    print("\nTop 20 Stocks with Volume Power Data:")
    for row in cursor.fetchall():
        print(f"  Code {row[0]}: {row[1]} records")
except sqlite3.OperationalError as e:
    print(f"Error: {e}")

conn.close()
