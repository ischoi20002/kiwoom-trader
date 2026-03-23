import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
import config

class DataManager:
    """[v1.0] 1분봉 시드 데이터 생성, 증분 업데이트 및 15분봉 리샘플링 관리"""
    
    def __init__(self, cache_dir="backtest_data"):
        self.cache_dir = cache_dir
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir)
        self.seed_date = "2025-03-04"

    def get_token(self):
        """키움 REST API 토큰 발급"""
        auth_url = f"{config.URL}/oauth2/token"
        auth_body = {
            "grant_type": "client_credentials",
            "appkey": config.APP_KEY,
            "secretkey": config.APP_SECRET
        }
        try:
            res = requests.post(auth_url, json=auth_body, timeout=10)
            return res.json().get("token")
        except Exception as e:
            print(f"  (!) Token Error: {e}")
            return None

    def update_historical_data(self, code, logger=None):
        """특정 종목의 1분봉 데이터를 시드일(2025-03-04)부터 어제까지 업데이트하고 15분봉 생성"""
        def log(msg):
            if logger: logger(msg)
            else: print(msg)

        token = self.get_token()
        if not token:
            log(f"  [Error] {code} 토큰 발급 실패")
            return False

        master_path = os.path.join(self.cache_dir, f"{code}_1m_master.csv")
        last_dt = None
        
        # 1. 기존 데이터 확인 (증분 업데이트 여부 결정)
        if os.path.exists(master_path):
            try:
                df_old = pd.read_csv(master_path, index_col=0, parse_dates=True)
                if not df_old.empty:
                    last_dt = df_old.index.max()
                    log(f"  [Info] {code} 기존 데이터 발견: {last_dt}")
            except:
                log(f"  [Warn] {code} 기존 데이터 로드 에러. 새로 수집합니다.")

        # 시작 날짜 결정: 기존 데이터 끝 다음날 또는 시드일
        if last_dt:
            today = datetime.now()
            # 마지막 데이터 날짜와 어제 날짜 사이의 실질적인 영업일 차이 계산 (간단화: 주말 제외)
            # 0:월, 1:화, ..., 4:금, 5:토, 6:일
            last_date = last_dt.date()
            yesterday_date = (today - timedelta(days=1)).date()
            
            is_up_to_date = False
            if last_date >= yesterday_date:
                is_up_to_date = True
            elif today.weekday() == 0: # 오늘이 월요일인 경우
                if last_date >= (today - timedelta(days=3)).date(): # 마지막이 지난주 금요일이면 OK
                    is_up_to_date = True
            
            if is_up_to_date:
                log(f"  [Info] {code} 데이터가 이미 최신입니다. (마지막: {last_dt})")
                return True
            
            start_date = last_dt.strftime("%Y-%m-%d")
        else:
            log(f"  [Info] {code} 시드 데이터가 없습니다. 처음부터 수집을 시작합니다.")
            start_date = self.seed_date

        end_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        log(f"  [Sync] {code} 업데이트 확인: {start_date} ~ {end_date}")

        # 2. 데이터 수집 (과거로 역순 수집하므로 base_dt를 end_date로 설정)
        all_bars = []
        next_key = ""
        url = f"{config.URL}/api/dostk/chart"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "api-id": "ka10080"
        }
        
        target_dt = datetime.strptime(start_date, "%Y-%m-%d")

        while True:
            headers["next-key"] = next_key
            headers["cont-yn"] = "Y" if next_key else "N"
            
            body = {
                "stk_cd": code,
                "tic_scope": "1", # 1분봉
                "upd_stkpc_tp": "1",
                "base_dt": end_date.replace("-", "")
            }
            
            try:
                res = requests.post(url, headers=headers, json=body, timeout=10)
                if res.status_code != 200:
                    log(f"  [Error] API 호출 실패: {res.status_code}")
                    break
                
                data = res.json()
                raw_list = data.get('stk_min_pole_chart_qry', data.get('output', []))
                if not raw_list: break
                
                chunk = []
                last_bar_dt = None
                for b in raw_list:
                    dt_str = str(b.get('cntr_tm', "")).strip()
                    if not dt_str: continue
                    dt = datetime.strptime(dt_str, "%Y%m%d%H%M%S")
                    last_bar_dt = dt
                    
                    if dt < target_dt: break
                    
                    chunk.append({
                        'Date': dt,
                        'Open': abs(float(b.get('open_pric', 0))),
                        'High': abs(float(b.get('high_pric', 0))),
                        'Low': abs(float(b.get('low_pric', 0))),
                        'Close': abs(float(b.get('cur_prc', 0))),
                        'Volume': int(b.get('trde_qty', 0))
                    })
                
                all_bars.extend(chunk)
                log(f"    ... {last_bar_dt} 까지 수집 중 (누적 {len(all_bars)}개)")
                
                next_key = res.headers.get('next-key', "").strip()
                cont_yn = res.headers.get('cont-yn', "N").strip()
                
                if cont_yn != 'Y' or not next_key or (last_bar_dt and last_bar_dt <= target_dt):
                    break
                
                time.sleep(0.25) # 4 requests/sec safety
                
            except Exception as e:
                log(f"  [Error] 수집 중 예외 발생: {e}")
                break

        if not all_bars:
            log(f"  [Info] {code} 추가할 새로운 데이터가 없습니다. (기존 데이터가 최신입니다)")
            # 새로운 데이터는 없어도 15분봉 리샘플링은 정합성을 위해 한번 수행
            self.ensure_15m_resampled(code, logger)
            return True

        # 3. 데이터 병합 및 저장
        df_new = pd.DataFrame(all_bars).set_index('Date').sort_index()
        if os.path.exists(master_path):
            df_old = pd.read_csv(master_path, index_col=0, parse_dates=True)
            old_len = len(df_old)
            df_final = pd.concat([df_old, df_new])
            df_final = df_final[~df_final.index.duplicated(keep='last')].sort_index()
            new_len = len(df_final)
            
            if new_len > old_len:
                df_final.to_csv(master_path)
                log(f"  [Success] {code} 1분봉 마스터 업데이트 완료 ({old_len} -> {new_len}개 봉)")
                self.ensure_15m_resampled(code, logger)
            else:
                log(f"  [Info] {code} 중복 제외 후 새로 추가된 데이터가 없습니다.")
        else:
            df_final = df_new
            df_final.to_csv(master_path)
            log(f"  [Success] {code} 1분봉 마스터 생성 완료 ({len(df_final)}개 봉)")
            self.ensure_15m_resampled(code, logger)
            
        return True

    def ensure_15m_resampled(self, code, logger=None):
        """1분봉 마스터를 기반으로 15분봉 데이터를 생성/갱신"""
        def log(msg):
            if logger: logger(msg)
            else: print(msg)

        master_path = os.path.join(self.cache_dir, f"{code}_1m_master.csv")
        resample_path = os.path.join(self.cache_dir, f"{code}_15m_master.csv")
        
        if not os.path.exists(master_path):
            return False
            
        try:
            df = pd.read_csv(master_path, index_col=0, parse_dates=True)
            if df.empty: return False
            
            # 리샘플링 로직: Open(첫), High(최대), Low(최소), Close(마지막), Volume(합계)
            df_15m = df.resample('15min').agg({
                'Open': 'first',
                'High': 'max',
                'Low': 'min',
                'Close': 'last',
                'Volume': 'sum'
            }).dropna()
            
            df_15m.to_csv(resample_path)
            log(f"  [Success] {code} 15분봉 리샘플링 완료 ({len(df_15m)}개 봉)")
            return True
        except Exception as e:
            log(f"  [Error] 리샘플링 실패: {e}")
            return False

if __name__ == "__main__":
    # 간단 테스트: 삼성전자 1분봉 업데이트
    dm = DataManager()
    dm.update_historical_data("005930")
