import os
import time
import json
import requests
import pandas as pd
from datetime import datetime, timedelta
import config
from data_manager_v2 import DataManagerV2
from kiwoom_rest_api.auth.token import TokenManager
import sys
import io

# 콘솔 출력 인코딩 설정 (Windows CP949 등 대응)
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.detach(), encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.detach(), encoding='utf-8', errors='replace')

class HarvestService:
    def __init__(self, target_list_path="kospi_top_500.csv"):
        self.db = DataManagerV2("trading_data.db")
        self.token_manager = TokenManager()
        self.target_list_path = target_list_path
        self.running = True
        
        # [v16.9] 수집 완료 여부를 COMPLETE 마크와 당일 동기화여부로만 판단 (특정 날짜 수동 지정 제거)
        
        # Kiwoom API 환경 및 라이브러리 설정 추가
        os.environ['KIWOOM_API_KEY'] = config.APP_KEY
        os.environ['KIWOOM_API_SECRET'] = config.APP_SECRET
        
        try:
            from kiwoom_rest_api import config as kiwoom_config
            kiwoom_config.API_KEY = config.APP_KEY
            kiwoom_config.API_SECRET = config.APP_SECRET
        except:
            pass

    def log(self, msg):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now}] {msg}", flush=True)

    def _fetch_minute_data(self, code, token, next_key=""):
        """키움 REST API 분봉차트조회 호출 (v9.0: 인덱스 수집 보류 - 주식 전용 최적화)"""
        code = str(code).strip()
        # [v16.9.1] 지수 및 시장 지표는 전용 동기화(Daily/FDR)를 위해 REST 수집 생략 
        if len(code) <= 3 or code in ['KS11', 'KQ11', '^VIX', '^KQ11', '^KS11']:
            return None, "" 

        url = f"{config.URL}/api/dostk/chart"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "cont-yn": "Y" if next_key else "N",
            "next-key": next_key,
            "api-id": "ka10080"
        }
        body = {
            "stk_cd": code,
            "tic_scope": "1", # 1분봉
            "upd_stkpc_tp": "1"
        }
        try:
            res = requests.post(url, headers=headers, json=body, timeout=10)
            if res.status_code == 200:
                data = res.json()
                raw_list = data.get('stk_min_pole_chart_qry', data.get('output', data.get('data', [])))
                n_key = res.headers.get('next-key', "")
                return raw_list, n_key
            else:
                self.log(f"⚠️ API 에러 ({code}): {res.status_code} | Msg: {res.text[:100]}")
                return None, ""
        except Exception as e:
            self.log(f"❌ 요청 중 예외 ({code}): {e}")
            return None, ""

    def _parse_to_bars(self, raw_list):
        """API 원본 데이터를 정형화된 리스트로 변환"""
        if not raw_list: return []
        parsed = []
        for item in raw_list:
            # [v7.8] 지수용 키(idx_prc, cntg_tm 등)와 주식용 키 대응 강화
            cl = item.get('idx_prc', item.get('cur_prc', item.get('stck_prpr', '0')))
            op = item.get('idx_oprc', item.get('open_pric', item.get('stck_oprc', '0')))
            hg = item.get('idx_hgpr', item.get('high_pric', item.get('stck_hgpr', '0')))
            lw = item.get('idx_lwpr', item.get('low_pric', item.get('stck_lwpr', '0')))
            tm = str(item.get('cntg_tm', item.get('cntr_tm', item.get('stck_cntg_hour', '')))).strip()
            vl = item.get('trde_qty', item.get('cntg_vol', '0'))
            
            if not tm or len(tm) < 12: continue
            
            # YYYYMMDDHHMMSS -> YYYY-MM-DD HH:MM:SS
            dt_fmt = f"{tm[:4]}-{tm[4:6]}-{tm[6:8]} {tm[8:10]}:{tm[10:12]}:{tm[12:14]}"
            
            def clean(v): return int(str(v).replace('+', '').replace('-', '').strip())
            
            parsed.append({
                'time': dt_fmt,
                'open': clean(op),
                'high': clean(hg),
                'low': clean(lw),
                'close': clean(cl),
                'volume': clean(vl)
            })
        return parsed

    def harvest_code(self, code, token):
        """한 종목의 부족한 데이터를 모두 수집 (v6.6: 증분 즉시 저장 모델)"""
        # [v16.0] 일봉 데이터(FinanceDataReader) 동기화 선행
        self.db.update_daily_data(code)
        
        if self.is_data_complete(code):
            self.log(f"[SKIP] {code}: 이미 모든 데이터가 충분하고 구멍이 없습니다.")
            return

        last_sync = self.db.get_last_sync_time(code)
        earliest_sync = self.db.get_earliest_sync_time(code)
        self.log(f"[SYNC] {code} 시작 (DB 범위: {earliest_sync or '미상'} ~ {last_sync or '없음'})")
        
        next_key = ""
        total_fetched = 0
        
        while self.running:
            raw_data, next_key = self._fetch_minute_data(code, token, next_key)
            if not raw_data: break
            
            bars = self._parse_to_bars(raw_data)
            if not bars: break
            
            # [v16.9] 첫 번째 청산을 가져왔을 때, 가장 최근 데이터가 이미 DB에 있다면 즉시 조기 종료 처리
            if total_fetched == 0 and last_sync and bars[0]['time'] <= last_sync:
                self.log(f"   >> {code}: 이미 최신 상태입니다. (최신 데이터: {bars[0]['time']})")
                total_fetched = 0
                break

            # [v16.9] 즉시 DB 저장 후 마지막 동기화 시간 갱신 (Incremental Sync 정확도 향상)
            df_chunk = pd.DataFrame(bars)
            self.db.save_bars(code, df_chunk)
            last_sync = self.db.get_last_sync_time(code) # DB에서 실시간으로 다시 읽어옴
            
            total_fetched += len(bars)
            current_bottom_time = bars[-1]['time']
            
            # [v16.9] Simplified: 기존 데이터와 조금이라도 겹치면 종료 (Incremental Sync)
            if last_sync and current_bottom_time <= last_sync:
                self.log(f"   - {code}: 기존 데이터와 연결됨. 조기 종료 (Overlapped).")
                break
            
            self.log(f"   - {code}: {total_fetched}개 봉 누적 저장 중... ({current_bottom_time})")
            
            if not next_key:
                # [v6.8] API 바닥에 도달함 (종목별 무결성 마크)
                self.db.set_sync_status(code, "COMPLETE")
                break
            
            time.sleep(0.3)
            
        if total_fetched > 0:
            self.log(f"   - {code}: 15분봉 데이터 갱신 중...")
            self.db.refresh_resampled_data(code)
            self.log(f"   - {code}: 과거 체결강도(VolPower) 동기화 중...")
            vp_count = self.db.sync_historical_vol_power(code, token)
            self.log(f"[OK] {code} 처리 완료: 신규 봉 {total_fetched}개, 체결강도 {vp_count}개 저장됨")
        else:
            # [v16.9.1] 개선된 원인별 로그 세분화 (지수/최신/미수집)
            is_idx = (len(code) <= 3 or code in ['KS11', 'KQ11', '^VIX', '^KQ11', '^KS11'])
            
            if last_sync and 'bars' in locals() and bars and bars[0]['time'] <= last_sync:
                self.log(f"[OK] {code} 처리 완료: 이미 최신 상태입니다.")
            elif is_idx:
                self.log(f"[OK] {code} 처리 완료: 지수 데이터(Daily Sync) 유효성 확인됨")
            else:
                self.log(f"[OK] {code} 처리 완료: 서버에서 가져온 데이터가 없습니다.")

    def is_data_complete(self, code):
        """데이터의 충분성 및 구멍 유무를 판단 (v6.8: 종목별 한계 및 전역 경계 기준)"""
        try:
            # 1. DB의 수집 범위 및 상태 확인
            last_sync = self.db.get_last_sync_time(code)
            earliest_sync = self.db.get_earliest_sync_time(code)
            status = self.db.get_sync_status(code) # (DataManagerV2에서 로드)
            
            if not last_sync:
                return False
            
            # [v16.9] 'COMPLETE' 상태이면 역사적으로 완료된 것으로 간주
            is_historically_complete = (status == "COMPLETE")
            
            if not is_historically_complete:
                return False # 과거 데이터가 더 필요함 (바닥까지 가야함)
            
            # [Smart Check 2] 보충(Daily Sync) 확인: 오늘 날짜의 데이터가 있는지 확인
            last_dt = datetime.strptime(last_sync, "%Y-%m-%d %H:%M:%S")
            now = datetime.now()
            
            # [Fix] 오늘 장 개시 전(09:00 이전)이라면, 어제 장마감 데이터까지만 있어도 완료로 간주
            if now.hour < 9:
                # 어제(또는 가장 최근 평일) 데이터가 있다면 통과
                # (now - timedelta).replace(...) 형식으로 datetime 객체의 replace를 호출해야 함
                yesterday_limit = (now - timedelta(days=1)).replace(hour=15, minute=30, second=0, microsecond=0)
                if last_dt >= yesterday_limit:
                    return True
            
            if last_sync[:10] < now.strftime("%Y-%m-%d"):
                return False 
                
            return True 
        except Exception as e:
            return False

    def run(self):
        """메인 수집 루프"""
        if not os.path.exists(self.target_list_path):
            self.log(f"[ERROR] 대상 리스트 파일({self.target_list_path})이 없습니다.")
            return

        df_list = pd.read_csv(self.target_list_path, dtype={'Code': str})
        codes = df_list['Code'].tolist()
        
        # [v7.5] Ensure market indices are always synchronized for backtest context
        # [v16.0] 주요 지수(KOSPI, KOSDAQ, VIX)를 수집 대상에 고정 추가
        for idx_code in ['001', '101', 'KS11', 'KQ11', '^VIX']:
            if idx_code not in codes:
                codes.insert(0, idx_code) # 상단에 배치하여 우선 수집
                
        self.log(f"[START] 총 {len(codes)}개 종목 수집을 시작합니다. (지수 포함)")
        
        token = self.token_manager.get_token()
        if not token:
            self.log("[ERROR] 토큰을 가져올 수 없습니다. 종료합니다.")
            return

        for idx, code in enumerate(codes):
            if not self.running: break
            
            self.log(f"[{idx+1}/{len(codes)}] 종목 분석 중 ({code})...")
            try:
                # [v7.0] 수집 전 무결성/입수 완료 여부 판단하여 스킵
                if self.is_data_complete(code):
                    self.log(f"   >> {code}: 이미 모든 구간이 동기화되어 있습니다. 건너뜁니다.")
                    continue

                self.harvest_code(code, token)
            except Exception as e:
                self.log(f"[ERROR] {code} 처리 중 치명적 오류: {e}")
            
            # 종목 간 대기 (서버 부하 분산)
            time.sleep(0.5)

        self.log("[FINISH] 모든 수집 작업이 종료되었습니다.")

if __name__ == "__main__":
    service = HarvestService()
    try:
        service.run()
        print("\n" + "="*50)
        input("✅ 모든 작업이 완료되었습니다. 결과를 확인한 후 Enter를 누르면 창이 닫힙니다...")
    except KeyboardInterrupt:
        service.running = False
        print("\n🛑 사용자에 의해 중지되었습니다.")
        input("종료하려면 Enter를 누르세요...")
    except Exception as e:
        print(f"\n❌ 치명적 오류 발생: {e}")
        input("오류를 확인한 후 Enter를 누르세요...")
