import sqlite3
import pandas as pd

# Test SQLite comparison
conn = sqlite3.connect(':memory:')
d1 = '2026-03-20'
d2 = '2026-3-19'
res = conn.execute(f"SELECT '{d1}' <= '{d2}'").fetchone()[0]
print(f"SQLite check: '{d1}' <= '{d2}' is {bool(res)}")

# Test pd.to_datetime logic
print(f"Pandas check: pd.to_datetime('{d1}') <= pd.to_datetime('{d2}') is {pd.to_datetime(d1) <= pd.to_datetime(d2)}")
conn.close()
