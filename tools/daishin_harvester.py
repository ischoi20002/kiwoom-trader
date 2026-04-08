import sys
import os
import win32com.client
import pandas as pd
import time
from datetime import datetime

# Add current directory to path for core module imports
if os.getcwd() not in sys.path:
    sys.path.append(os.getcwd())

from core.daishin_data_manager import DaishinDataManager

# This script MUST be run with 32-bit Python.
# py -3.10-32 tools/daishin_harvester.py

class DaishinHarvester:
    def __init__(self, db_path="daishin_data.db"):
        self.db = DaishinDataManager(db_path)
        try:
            self.obj_stock_chart = win32com.client.Dispatch("CpSysDib.StockChart")
            self.obj_util = win32com.client.Dispatch("CpUtil.CpCybos")
        except Exception as e:
            print(f"Error initializing CREON COM objects: {e}")
            print("Make sure CREON PLUS terminal is running and logged in.")
            sys.exit(1)

    def check_connection(self):
        if self.obj_util.IsConnect == 0:
            print("CREON Plus is not connected.")
            return False
        return True

    def log(self, msg):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now}] {msg}", flush=True)

    def harvest_code(self, code, target_count=50000):
        """
        Harvest historical 1-minute bars for a specific code.
        code: String like 'A005930'
        target_count: Total number of bars to try to collect.
        """
        if not self.check_connection():
            return

        self.log(f"Starting harvest for {code}...")
        
        # Check current progress
        earliest_dt = self.db.get_earliest_sync_time(code)
        last_dt = self.db.get_last_sync_time(code)
        status = self.db.get_sync_status(code)

        if status == "COMPLETE":
            self.log(f"Code {code} is already marked as COMPLETE. Skipping.")
            return

        total_collected = 0
        
        # Set Inputs for StockChart
        self.obj_stock_chart.SetInputValue(0, code)
        self.obj_stock_chart.SetInputValue(1, ord('2')) # 2: By count
        # self.obj_stock_chart.SetInputValue(2, 20240101) # End date (optional)
        self.obj_stock_chart.SetInputValue(4, 2000)      # Request 2000 bars at a time
        self.obj_stock_chart.SetInputValue(5, [0, 1, 2, 3, 4, 5, 8]) # Date, Time, O, H, L, C, Vol
        self.obj_stock_chart.SetInputValue(6, ord('m')) # 'm': Minute
        self.obj_stock_chart.SetInputValue(9, ord('1')) # '1': Modified price

        while True:
            # Request
            self.obj_stock_chart.BlockRequest()

            # Check status
            status_code = self.obj_stock_chart.GetDibStatus()
            msg = self.obj_stock_chart.GetDibMsg1()
            if status_code != 0:
                self.log(f"Error in request: {msg}")
                break

            # Parse data
            num_data = self.obj_stock_chart.GetHeaderValue(3)
            if num_data == 0:
                self.log("No data returned from server.")
                break

            bars = []
            for i in range(num_data):
                d = self.obj_stock_chart.GetDataValue(0, i)
                t = self.obj_stock_chart.GetDataValue(1, i)
                
                # Format: YYYY-MM-DD HH:MM:SS
                dt_str = str(d)
                tm_str = str(t).zfill(4)
                dt_fmt = f"{dt_str[:4]}-{dt_str[4:6]}-{dt_str[6:8]} {tm_str[:2]}:{tm_str[2:4]}:00"
                
                bars.append({
                    'datetime': dt_fmt,
                    'open': self.obj_stock_chart.GetDataValue(2, i),
                    'high': self.obj_stock_chart.GetDataValue(3, i),
                    'low': self.obj_stock_chart.GetDataValue(4, i),
                    'close': self.obj_stock_chart.GetDataValue(5, i),
                    'volume': self.obj_stock_chart.GetDataValue(6, i),
                })
            
            df = pd.DataFrame(bars)
            self.db.save_bars(code, df)
            
            total_collected += len(bars)
            self.log(f"Collected {len(bars)} bars (Total: {total_collected}) - Earliest: {bars[-1]['datetime']}")

            # Rate limiting check: StockChart has a limit of 60 requests per 15 seconds.
            # We'll sleep slightly to be safe.
            time.sleep(0.3)

            # Check if we should continue
            if not self.obj_stock_chart.Continue:
                self.log(f"No more data available for {code} on server.")
                self.db.set_sync_status(code, "COMPLETE")
                break
            
            if total_collected >= target_count:
                self.log(f"Reached target count {target_count} for {code}.")
                break

if __name__ == "__main__":
    harvester = DaishinHarvester()
    
    # [Universe] Try to load all KOSPI codes from the existing list
    universe_file = 'kospi_top_100000.csv'
    all_codes = []
    
    if os.path.exists(universe_file):
        try:
            df_universe = pd.read_csv(universe_file, dtype={'Code': str})
            all_codes = ['A' + code.strip() for code in df_universe['Code'].tolist()]
            print(f"Loaded {len(all_codes)} codes from {universe_file}")
        except Exception as e:
            print(f"Error reading universe file: {e}")
    
    if not all_codes:
        # Fallback: manually specified or via API
        # test_codes = ['A005930'] 
        print("Universe is empty. Please check kospi_top_100000.csv")
        sys.exit(1)
    
    for idx, code in enumerate(all_codes):
        print(f"[{idx+1}/{len(all_codes)}] Processing {code}...")
        try:
            # Set target_count to a high value for deep historical harvesting (e.g., 200,000 bars)
            harvester.harvest_code(code, target_count=200000)
        except Exception as e:
            print(f"Fatal error processing {code}: {e}")
            # Try to continue with next stock
            continue

    print("KOSPI Harvesting session finished.")
