import sqlite3
import os

db_path = 'trading_data.db'
if not os.path.exists(db_path):
    print(f"Error: {db_path} not found.")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

try:
    cursor.execute("SELECT COUNT(*) FROM bars_1m")
    total = cursor.fetchone()[0]
    print(f"Total bars_1m rows: {total}")
    cursor.execute(
        "SELECT code, COUNT(*) AS n FROM bars_1m GROUP BY code ORDER BY n DESC LIMIT 10"
    )
    print("\nTop 10 codes by 1m bar count:")
    for row in cursor.fetchall():
        print(f"  Code {row[0]}: {row[1]} rows")
except sqlite3.OperationalError as e:
    print(f"Error: {e}")

conn.close()
