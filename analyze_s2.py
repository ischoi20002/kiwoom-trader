import pandas as pd
import numpy as np

def analyze_s2_types():
    try:
        df = pd.read_csv('backtest_trades.csv')
        df = df[df['Action'] != 'REJECTED_SIG']
        
        codes = df['Code'].unique()
        a_s2_count = 0
        b_s2_count = 0
        
        for c in codes:
            s_df = df[df['Code'] == c]
            entry_type = None
            is_s2_triggered = False
            
            for _, r in s_df.iterrows():
                act = str(r['Action'])
                reason = str(r.get('Reason', ''))
                
                # 진입 타입 판별
                if 'BUY' in act:
                    if 'TYPE_A' in reason: entry_type = 'A'
                    elif 'TYPE_B' in reason: entry_type = 'B'
                    
                    if 'Stage 2' in reason or 'S2' in reason:
                        is_s2_triggered = True
                
                # 청산 시점의 S2 여부 확인 (또는 하단에서 카운트)
                elif act in ['SELL', 'HOLD']:
                    if is_s2_triggered:
                        if entry_type == 'A': a_s2_count += 1
                        elif entry_type == 'B': b_s2_count += 1
                    
                    # 초기화
                    entry_type = None
                    is_s2_triggered = False
        
        print("\n=== STAGE 2 (추가매수) 근원별 건수 분석 ===")
        print(f"Type A(눌림목) 진입 후 추매: {a_s2_count} 건")
        print(f"Type B(추세돌파) 진입 후 추매: {b_s2_count} 건")
        print(f"합계: {a_s2_count + b_s2_count} 건")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    analyze_s2_types()
