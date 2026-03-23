
import FinanceDataReader as fdr
import pandas as pd
from datetime import datetime

def check_gc():
    print("Fetching KOSPI data for 2024-2025...")
    # Get plenty of data for warming up MAs
    df = fdr.DataReader('KS11', '2024-01-01', '2025-12-31')
    
    if df.empty:
        print("Failed to fetch data.")
        return

    df['MA20'] = df['Close'].rolling(20).mean()
    df['MA60'] = df['Close'].rolling(60).mean()
    df['MA120'] = df['Close'].rolling(120).mean()
    
    # Golden Cross: Prev day MA_fast < MA_slow AND Curr day MA_fast > MA_slow
    
    # 20/60 GC
    df['GC_20_60'] = (df['MA20'] > df['MA60']) & (df['MA20'].shift(1) <= df['MA60'].shift(1))
    
    # 60/120 GC
    df['GC_60_120'] = (df['MA60'] > df['MA120']) & (df['MA60'].shift(1) <= df['MA120'].shift(1))
    
    print("\n[KOSPI 2025 Moving Average Analysis]")
    
    gc_20_60_dates = df[(df.index >= '2025-01-01') & df['GC_20_60']].index
    print("\n--- 20일선 / 60일선 골든크로스 (단기/중기 추세 전환) ---")
    if not gc_20_60_dates.empty:
        for d in gc_20_60_dates:
            print(f"- {d.strftime('%Y-%m-%d')}")
    else:
        print("2025년에 발생하지 않았거나 아직 발생 전입니다.")
        
    gc_60_120_dates = df[(df.index >= '2025-01-01') & df['GC_60_120']].index
    print("\n--- 60일선 / 120일선 골든크로스 (중기/장기 대세 전환) ---")
    if not gc_60_120_dates.empty:
        for d in gc_60_120_dates:
            print(f"- {d.strftime('%Y-%m-%d')}")
    else:
        print("2025년에 발생하지 않았거나 아직 발생 전입니다.")

    # Check current status
    last_row = df.iloc[-1]
    print(f"\n[현재 상태 ({df.index[-1].strftime('%Y-%m-%d')})]")
    print(f"- 종가: {last_row['Close']:,.2f}")
    print(f"- MA20: {last_row['MA20']:,.2f}")
    print(f"- MA60: {last_row['MA60']:,.2f}")
    print(f"- MA120: {last_row['MA120']:,.2f}")
    
    if last_row['MA20'] > last_row['MA60'] > last_row['MA120']:
        print(">> 현재 시장은 [정배열] 상태인 탄탄한 상승세입니다.")
    elif last_row['Close'] > last_row['MA20']:
        print(">> 현재 종가가 20일선 위에 있으나 완벽한 정배열은 아닙니다.")
    else:
        print(">> 현재 시장 추세가 다소 꺾여 있는 상태입니다.")

if __name__ == "__main__":
    check_gc()
