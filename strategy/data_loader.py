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
                # DB 경로는 k_trader_gui.py와 동일하게 설정
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
            # 1m·15m 모두 bars_* 만 사용 (구 realtime_snapshots 는 미적재 레거시로 제거됨).
            table_name = f"bars_{interval}"
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
