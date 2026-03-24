import sqlite3
import pandas as pd
import numpy as np

def analyze_macd_impact_slope():
    db_path = r'c:\바이브\trading_data.db'
    conn = sqlite3.connect(db_path)
    
    # 1. 최근 3개월 데이터 샘플링 (2024-12-01 ~ 2025-02-28)
    query = """
        SELECT code, datetime as time, open, high, low, close, volume 
        FROM bars_1d 
        WHERE datetime BETWEEN '2024-12-01' AND '2025-02-28'
        ORDER BY code, datetime
    """
    df = pd.read_sql_query(query, conn)
    conn.close()
    
    if df.empty:
        print("No data found.")
        return

    df['close'] = df['close'].astype(float)
    df['volume'] = df['volume'].astype(float)
    df['trde_val'] = df['close'] * df['volume']
    
    # 2. 지표 계산 (MA, RSI, MACD)
    df['MA5'] = df.groupby('code')['close'].transform(lambda x: x.rolling(5).mean())
    df['MA20'] = df.groupby('code')['close'].transform(lambda x: x.rolling(20).mean())
    
    # RSI
    delta = df.groupby('code')['close'].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    ema_up = gain.groupby(df['code']).transform(lambda x: x.ewm(com=13, adjust=False).mean())
    ema_down = loss.groupby(df['code']).transform(lambda x: x.ewm(com=13, adjust=False).mean())
    df['RSI'] = 100 - (100 / (1 + ema_up / (ema_down + 1e-10)))
    
    # MACD (12/26/9)
    df['EMA_F'] = df.groupby('code')['close'].transform(lambda x: x.ewm(span=12, adjust=False).mean())
    df['EMA_S'] = df.groupby('code')['close'].transform(lambda x: x.ewm(span=26, adjust=False).mean())
    df['MACD'] = df['EMA_F'] - df['EMA_S']
    df['MACD_S'] = df.groupby('code')['MACD'].transform(lambda x: x.ewm(span=9, adjust=False).mean())
    df['OSC'] = df['MACD'] - df['MACD_S']
    df['OSC_Prev'] = df.groupby('code')['OSC'].shift(1)
    
    # 3. 시나리오 비교
    df['AvgVal5'] = df.groupby('code')['trde_val'].transform(lambda x: x.rolling(5).mean())
    df['MA_Diff'] = (df['MA5'] - df['MA20']).abs() / df['MA20']
    
    c1 = df['AvgVal5'] >= (40 * 100000000)
    c2 = df['MA_Diff'] <= 0.03
    c3 = (df['RSI'] >= 40.0) & (df['RSI'] <= 70.0)
    
    df['Scenario_A'] = c1 & c2 & c3
    
    # Scenario B: 추가 필터 (MACD >= MACD_Sig AND OSC > OSC_Prev)
    # [v26.14] Adding slope check for the selection filter
    df['Scenario_B'] = df['Scenario_A'] & (df['OSC'] > 0) & (df['OSC'] > df['OSC_Prev'])
    
    # 4. 결과 집계
    stats = df.groupby('time').agg({
        'Scenario_A': 'sum',
        'Scenario_B': 'sum'
    })
    
    stats['Reduction'] = (1 - stats['Scenario_B'] / stats['Scenario_A']) * 100
    avg_reduction = stats['Reduction'].mean()
    
    print("\n--- MACD 필터 격상 + 우상향(기울기) 추가 시 영향 분석 ---")
    print(stats.tail(10))
    print(f"\n평균 후보 종목 수 (Scenario A - 현재 필터): {stats['Scenario_A'].mean():.1f}개")
    print(f"평균 후보 종목 수 (Scenario B - MACD 정배열 + 우상향): {stats['Scenario_B'].mean():.1f}개")
    print(f"평균 종목 감소율: {avg_reduction:.1f}%")
    print("-----------------------------------------------------")

if __name__ == "__main__":
    analyze_macd_impact_slope()
