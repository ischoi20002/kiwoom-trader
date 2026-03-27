import pandas as pd
import sqlite3
import os
import threading
from datetime import datetime, timedelta

class BacktestDataLoader:
    """[v11.5] SQLite로부터 15분봉 및 1분봉 데이터를 로드하고 캐싱하는 전용 클래스"""
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(BacktestDataLoader, cls).__new__(cls)
                # DB 경로는 Get_Price_GUI.py와 동일하게 설정
                cls._instance.db_path = os.path.join(os.getcwd(), "trading_data.db")
                cls._instance.cache_dir = os.path.join(os.getcwd(), "backtest_data")
                cls._instance.cache = {}
            return cls._instance

    def get_data(self, code, start_date, end_date, interval='15m', force_refresh=False):
        """종목 코드와 기간에 맞는 데이터를 캐시 또는 DB에서 로드"""
        cache_key = f"{code}_{interval}_{start_date}_{end_date}"
        if force_refresh and cache_key in self.cache:
            del self.cache[cache_key]
            
        if cache_key in self.cache:
            return self.cache[cache_key]
            
        try:
            conn = sqlite3.connect(self.db_path)
            # [v15.3.8] Robust Table Name mapping
            # [v31.3] Join with realtime_snapshots to get VolPower and Bid/Ask metrics
            table_name = f"bars_{interval}"
            if interval == '1m':
                query = f"""
                    SELECT b.*, r.vol_power, r.total_ask, r.total_bid
                    FROM {table_name} b
                    LEFT JOIN realtime_snapshots r ON b.code = r.code AND b.datetime = r.datetime
                    WHERE b.code = ? AND b.datetime BETWEEN ? AND ?
                """
            else:
                # 15m or others: standard query (simulated metrics will be added in Engine if missing)
                query = f"SELECT * FROM {table_name} WHERE code = ? AND datetime BETWEEN ? AND ?"
                
            df = pd.read_sql(query, conn, params=(code, f"{start_date} 00:00:00", f"{end_date} 23:59:59"))
            conn.close()
            
            if df.empty: return None
            
            # [v17.5] Normalize column names to match expected format
            df.columns = [c.capitalize() if c.lower() != 'time' else 'time' for c in df.columns]
            if 'Datetime' in df.columns:
                df.rename(columns={'Datetime': 'Date'}, inplace=True)
            
            df['Date'] = pd.to_datetime(df['Date'])
            df.set_index('Date', inplace=True)
            df.sort_index(inplace=True)
            
            self.cache[cache_key] = df
            return df
        except Exception as e:
            print(f"  (!) Data load error ({code}): {e}")
            return None
