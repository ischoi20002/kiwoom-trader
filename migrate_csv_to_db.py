import os
import pandas as pd
from data_manager_v2 import DataManagerV2
import glob

def migrate():
    # 데이터 폴더 경로 (기존 CSV 저장 위치)
    data_dir = "backtest_data"
    db_manager = DataManagerV2("trading_data.db")
    
    # 1분봉 마스터 파일들 찾기 (*_1m_master.csv)
    csv_files = glob.glob(os.path.join(data_dir, "*_1m_master.csv"))
    
    if not csv_files:
        print("❌ 마이그레이션할 CSV 파일을 찾지 못했습니다.")
        return

    print(f"🚀 총 {len(csv_files)}개 종목의 데이터 이관을 시작합니다.")

    for file_path in csv_files:
        try:
            filename = os.path.basename(file_path)
            code = filename.split('_')[0]
            
            print(f"📦 {code} 이관 중...", end="\r")
            
            # CSV 로드
            df = pd.read_csv(file_path)
            
            # 필요한 컬럼만 필터링 및 타입 확인
            # time, open, high, low, close, volume
            if 'time' not in df.columns:
                print(f"\n⚠️ {code}: 'time' 컬럼이 없습니다. 건너뜁니다.")
                continue
                
            # DB 저장
            db_manager.save_bars(code, df)
            
        except Exception as e:
            print(f"\n❌ {file_path} 처리 중 오류: {e}")

    print("\n✅ 모든 데이터 이관이 완료되었습니다.")

if __name__ == "__main__":
    migrate()
