import sqlite3
import pandas as pd
import numpy as np

def check_gaps():
    db_path = 'trading_data.db'
    conn = sqlite3.connect(db_path)
    
    print("Scanning for 3%~5% Gap Downs in 2025 (Day-to-Day)...")
    
    # We'll use 15m data but only look at the first bar of each day (09:00)
    # compared to the last bar of the previous day (15:30).
    # Since the DB is large, we'll process stock by stock for a sample of KOSPI 200.
    
    # Get some sample codes (KOSPI 200)
    query_codes = "SELECT DISTINCT code FROM bars_15m LIMIT 100"
    codes = pd.read_sql_query(query_codes, conn)['code'].tolist()
    
    gap_cases = []
    
    for code in codes:
        query = f"""
            SELECT code, datetime as time, open, close, high, low 
            FROM bars_15m 
            WHERE code = '{code}' AND datetime BETWEEN '2025-01-01' AND '2025-12-31'
            ORDER BY datetime
        """
        df = pd.read_sql_query(query, conn)
        if df.empty: continue
        
        df['time'] = pd.to_datetime(df['time'])
        df['date'] = df['time'].dt.date
        
        # Group by date to get Day Open and Previous Day Close
        # Also get high of the FIRST 15m bar
        day_first_15m = df.groupby('date').first()
        day_all = df.groupby('date').agg({'open': 'first', 'close': 'last', 'high': 'max', 'low': 'min'})
        day_all['first_15m_high'] = day_first_15m['high']
        day_all['prev_close'] = day_all['close'].shift(1)
        
        # Calculate gap
        day_all['gap_pct'] = (day_all['open'] / day_all['prev_close'] - 1) * 100
        
        # Filter gaps between -5% and -3%
        target_gaps = day_all[(day_all['gap_pct'] <= -3.0) & (day_all['gap_pct'] > -5.0)].copy()
        
        if not target_gaps.empty:
            for d, row in target_gaps.iterrows():
                # Check recovery within 15 min (to -3% line)
                recovered_15m = (row['first_15m_high'] / row['prev_close'] - 1) >= -0.03
                
                # Check total day recovery to prev close
                max_reach = row['high']
                min_reach = row['low']
                rebound_pct = (max_reach / row['prev_close'] - 1) * 100
                further_fall_pct = (min_reach / row['open'] - 1) * 100
                
                gap_cases.append({
                    'Code': code,
                    'Date': d,
                    'Gap': row['gap_pct'],
                    'Recovered_15m_Negative3Pct': recovered_15m,
                    'DayHighRatio': rebound_pct,
                    'DayLowRatio': further_fall_pct,
                    'RecoveredToPrevClose': max_reach >= row['prev_close']
                })
    
    conn.close()
    
    if not gap_cases:
        print("No matches found in the 50-stock sample.")
    else:
        results = pd.DataFrame(gap_cases)
        print(f"\nFound {len(results)} cases of 3~5% gap downs.")
        print(results.head(20))
        
        recovery_rate = results['RecoveredToPrevClose'].mean() * 100
        recovery_15m_rate = results['Recovered_15m_Negative3Pct'].mean() * 100
        print(f"\n15-min Recovery Rate (to -3% line): {recovery_15m_rate:.1f}%")
        print(f"Total Day Recovery Rate (to prev close): {recovery_rate:.1f}%")
        
        avg_further_fall = results['DayLowRatio'].mean()
        print(f"Avg Further Fall from Open: {avg_further_fall:.1f}%")

if __name__ == "__main__":
    check_gaps()
