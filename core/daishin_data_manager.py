import sqlite3
import os
import pandas as pd
import threading
from datetime import datetime

class DaishinDataManager:
    def __init__(self, db_path="daishin_data.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self):
        with self._get_connection() as conn:
            # 1-minute bars table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bars_1m (
                    code TEXT,
                    datetime TEXT,
                    open INTEGER,
                    high INTEGER,
                    low INTEGER,
                    close INTEGER,
                    volume INTEGER,
                    PRIMARY KEY (code, datetime)
                )
            """)
            # Sync metadata table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_meta (
                    code TEXT PRIMARY KEY,
                    earliest_sync_dt TEXT,
                    last_sync_dt TEXT,
                    status TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bars_1m_code_dt ON bars_1m (code, datetime)")
            conn.commit()

    def save_bars(self, code, df):
        if df.empty:
            return
        
        # Ensure correct column order and types
        data = df.copy()
        if 'datetime' not in data.columns:
            # Standardize column name if needed
            for col in ['time', 'Date', 'DateTime']:
                if col in data.columns:
                    data.rename(columns={col: 'datetime'}, inplace=True)
                    break
        
        data['code'] = code
        cols = ['code', 'datetime', 'open', 'high', 'low', 'close', 'volume']
        data = data[cols]

        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            rows = data.values.tolist()
            cursor.executemany("""
                INSERT OR IGNORE INTO bars_1m (code, datetime, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, rows)
            
            # Update sync meta
            first_dt = data['datetime'].min()
            last_dt = data['datetime'].max()
            
            cursor.execute("""
                INSERT INTO sync_meta (code, earliest_sync_dt, last_sync_dt) VALUES (?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET 
                    earliest_sync_dt = CASE 
                        WHEN excluded.earliest_sync_dt < COALESCE(earliest_sync_dt, '9999') THEN excluded.earliest_sync_dt 
                        ELSE earliest_sync_dt END,
                    last_sync_dt = CASE 
                        WHEN excluded.last_sync_dt > COALESCE(last_sync_dt, '') THEN excluded.last_sync_dt 
                        ELSE last_sync_dt END
            """, (code, first_dt, last_dt))
            conn.commit()

    def get_earliest_sync_time(self, code):
        with self._get_connection() as conn:
            row = conn.execute("SELECT earliest_sync_dt FROM sync_meta WHERE code = ?", (code,)).fetchone()
            return row[0] if row else None

    def get_last_sync_time(self, code):
        with self._get_connection() as conn:
            row = conn.execute("SELECT last_sync_dt FROM sync_meta WHERE code = ?", (code,)).fetchone()
            return row[0] if row else None

    def set_sync_status(self, code, status):
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO sync_meta (code, status) VALUES (?, ?)
                ON CONFLICT(code) DO UPDATE SET status = excluded.status
            """, (code, status))
            conn.commit()

    def get_sync_status(self, code):
        with self._get_connection() as conn:
            row = conn.execute("SELECT status FROM sync_meta WHERE code = ?", (code,)).fetchone()
            return row[0] if row else None
