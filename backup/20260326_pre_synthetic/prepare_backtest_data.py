import sys
import io
import pandas as pd
import os
import time
from datetime import datetime
from data_manager_v2 import DataManagerV2
from concurrent.futures import ThreadPoolExecutor
import sqlite3

# 콘솔 출력 인코딩 설정 (Windows CP949 등 대응)
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.detach(), encoding='utf-8', errors='replace')

def prepare_backtest_data():
    """
    1. trading_data.db의 1분봉으로 15분봉 생성 (bars_15m 테이블 채우기)
    2. backtest_data/ 폴더에 1m, 15m CSV로 익스포트
    """
    db = DataManagerV2("trading_data.db")
    target_list_path = "kospi_top_500.csv"
    cache_dir = "backtest_data"
    
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)
        
    if not os.path.exists(target_list_path):
        print(f"[ERROR] 대상 리스트 파일({target_list_path})이 없습니다.")
        return

    df_list = pd.read_csv(target_list_path, dtype={'Code': str})
    codes = df_list['Code'].tolist()
    
    print(f"[START] 총 {len(codes)}개 종목에 대한 15분봉 생성 및 CSV 내보내기를 시작합니다.")

    def process_stock(code):
        try:
            # 1. 15분봉 리샘플링 및 DB 저장
            db.refresh_resampled_data(code)
            
            # 2. CSV 내보내기 (1m)
            df_1m = db.get_bars(code)
            if not df_1m.empty:
                # 백테스트 엔진 형식에 맞게 컬럼명 조정 (open -> Open 등)
                df_1m.columns = [c.capitalize() for c in df_1m.columns]
                # Index를 Datetime으로 설정 (엔진 요구사항)
                df_1m.set_index('Time', inplace=True)
                csv_1m_path = os.path.join(cache_dir, f"{code}_1m_master.csv")
                df_1m.to_csv(csv_1m_path)
            
            # 3. CSV 내보내기 (15m)
            with db._get_connection() as conn:
                df_15m = pd.read_sql_query("SELECT datetime as time, open, high, low, close, volume FROM bars_15m WHERE code = ?", conn, params=[code])
            
            if not df_15m.empty:
                df_15m.columns = [c.capitalize() for c in df_15m.columns]
                df_15m.set_index('Time', inplace=True)
                csv_15m_path = os.path.join(cache_dir, f"{code}_15m_master.csv")
                df_15m.to_csv(csv_15m_path)
                
            return True
        except Exception as e:
            print(f"[ERROR] {code} 처리 중 오류: {e}")
            return False

    total_count = len(codes)
    success_count = 0
    
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(process_stock, codes))
        success_count = sum(1 for r in results if r)

    print(f"\n[OK] 완료! (성공: {success_count} / 총: {total_count})")
    print(f"[INFO] 데이터가 {cache_dir}/ 폴더에 저장되었습니다. 이제 백테스팅을 즉시 실행할 수 있습니다.")

if __name__ == "__main__":
    start_time = time.time()
    prepare_backtest_data()
    end_time = time.time()
    print(f"[TIME] 총 소요 시간: {end_time - start_time:.1f}초")
