import sqlite3
import pandas as pd
import os
from datetime import datetime, timedelta

from utils.normalization import normalize_code
import threading
import requests
import config
import time
import json
from concurrent.futures import ThreadPoolExecutor

class DataManagerV2:
    _instance = None
    _initialized = False
    _lock = threading.Lock()
    # [v10.2] Absolute path for the database to ensure consistency across process CWD
    ABS_DIR = os.path.dirname(os.path.abspath(__file__))
    DEFAULT_DB_PATH = os.path.join(ABS_DIR, "trading_data.db")

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(DataManagerV2, cls).__new__(cls)
            return cls._instance

    def __init__(self, db_path=None):
        if DataManagerV2._initialized:
            return
        self.db_path = db_path if db_path else self.DEFAULT_DB_PATH
        print(f"DEBUG [DataManagerV2]: Using DB at {self.db_path}")
        self._init_db()
        DataManagerV2._initialized = True

    def _get_connection(self):
        """SQLite 연결 확보 및 WAL 모드 활성화"""
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL;")  # 동시성 확보를 위한 WAL 모드
        return conn

    def _init_db(self):
        """데이터베이스 테이블 초기화"""
        with self._get_connection() as conn:
            # 1. 1분봉 마스터 테이블
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bars_1m (
                    code TEXT,
                    datetime TEXT,
                    open INTEGER,
                    high INTEGER,
                    low INTEGER,
                    close INTEGER,
                    volume INTEGER,
                    PRIMARY KEY (code, datetime)
                )
            """)
            
            # 2. 15분봉 테이블 (조회 성능을 위해 별도 관리)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bars_15m (
                    code TEXT,
                    datetime TEXT,
                    open INTEGER,
                    high INTEGER,
                    low INTEGER,
                    close INTEGER,
                    volume INTEGER,
                    PRIMARY KEY (code, datetime)
                )
            """)

            # 3. 일봉 테이블 (전략 종목 선정 가속화용)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bars_1d (
                    code TEXT,
                    datetime TEXT,
                    open INTEGER,
                    high INTEGER,
                    low INTEGER,
                    close INTEGER,
                    volume INTEGER,
                    PRIMARY KEY (code, datetime)
                )
            """)

            # 4. 동기화 메타 정보 테이블
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_meta (
                    code TEXT PRIMARY KEY,
                    last_sync_dt TEXT,
                    earliest_sync_dt TEXT,
                    last_daily_sync TEXT,
                    status TEXT
                )
            """)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_bars_1m_code_dt ON bars_1m (code, datetime)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bars_15m_code_dt ON bars_15m (code, datetime)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bars_1d_code_dt ON bars_1d (code, datetime)")

            # [Migration] sync_meta 테이블에 last_daily_sync 컬럼이 없는 경우 추가
            try:
                conn.execute("ALTER TABLE sync_meta ADD COLUMN last_daily_sync TEXT")
            except sqlite3.OperationalError:
                pass # 이미 존재함
            
            conn.commit()

    def update_historical_data(self, code, logger=None):
        """[Legacy Support] 1분/15분봉 및 일봉(bars_1d) 데이터를 서버에서 가져와 업데이트"""
        # [v16.0] 일봉 데이터 동기화 병행 (FinanceDataReader)
        self.update_daily_data(code)
        from kiwoom_rest_api.auth.token import TokenManager
        
        def log(msg):
            if logger: logger(msg)
            else: print(msg)

        token = TokenManager().get_token()
        if not token:
            log(f"  [Error] {code} 토큰 발급 실패")
            return False

        last_sync = self.get_last_sync_time(code)
        earliest_sync = self.get_earliest_sync_time(code)
        status = self.get_sync_status(code)
        # [v16.9] 최신성 체크: 당일 데이터 존재 여부 확인
        if last_sync:
            last_dt = datetime.strptime(last_sync, "%Y-%m-%d %H:%M:%S")
            now = datetime.now()
            # 오늘 날짜이고 15:30 이후 데이터가 있다면 (완료된 종목은 스킵)
            if last_dt.date() == now.date() and last_dt.hour >= 15 and last_dt.minute >= 30:
                if status == "COMPLETE":
                    log(f"  [Skip] {code}: 이미 오늘 데이터까지 최신 상태(COMPLETE)입니다.")
                    return True
        
        # 1. API 수집 (간소화된 Harvest 로직)
        all_bars = []
        next_key = ""
        url = f"{config.URL}/api/dostk/chart"
        headers = {"Content-Type": "application/json;charset=UTF-8", "authorization": f"Bearer {token}", "api-id": "ka10080"}
        
        log(f"  [Sync] {code} 시작 (DB 범위: {earliest_sync or '미상'} ~ {last_sync or '없음'})")
        
        while True:
            headers["next-key"] = next_key
            headers["cont-yn"] = "Y" if next_key else "N"
            body = {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "1"}
            
            try:
                res = requests.post(url, headers=headers, json=body, timeout=10)
                if res.status_code != 200: break
                
                data = res.json()
                raw_list = data.get('stk_min_pole_chart_qry', data.get('output', []))
                if not raw_list: break
                
                chunk = []
                stop = False
                for b in raw_list:
                    tm = b.get('cntr_tm', "")
                    if not tm: continue
                    # YYYYMMDDHHMMSS -> YYYY-MM-DD HH:MM:SS
                    dt_fmt = f"{tm[:4]}-{tm[4:6]}-{tm[6:8]} {tm[8:10]}:{tm[10:12]}:{tm[12:14]}"
                    
                    # [v16.9] Simplified: 기존 데이터와 연결되면 즉시 종료 (Incremental Sync)
                    if last_sync and dt_fmt <= last_sync:
                        stop = True
                        break
                    

                    def clean(v): return int(str(v).replace('+', '').replace('-', '').strip())
                    chunk.append({
                        'code': code, 'datetime': dt_fmt,
                        'open': clean(b.get('open_pric', 0)), 'high': clean(b.get('high_pric', 0)),
                        'low': clean(b.get('low_pric', 0)), 'close': clean(b.get('cur_prc', 0)),
                        'volume': clean(b.get('trde_qty', 0))
                    })
                
                all_bars.extend(chunk)
                
                # [v6.7] chunk 단위로 즉시 저장하여 메모리 부담 완화 및 안전성 확보
                if chunk:
                    df = pd.DataFrame(chunk)
                    self.save_bars(code, df)
                
                next_key = data.get('next-key', "")
                if stop: break
                if not next_key:
                    # [v6.8] API가 더 이상 과거 데이터를 주지 않음 -> 이 종목은 이게 끝임 (무결점 마크)
                    self.set_sync_status(code, "COMPLETE")
                    break
                time.sleep(0.25)
            except Exception as e:
                log(f"  [Error] {code} 루프 중 예외: {e}")
                break

        if all_bars:
            log(f"  [Success] {code} {len(all_bars)}개 봉 업데이트 완료")
            self.refresh_resampled_data(code)
        else:
            if stop:
                log(f"  [Info] {code} 이미 모든 구간이 동기화되어 있습니다.")
            else:
                log(f"  [Info] {code} API 서버에서 반환된 데이터가 없습니다.")
        
        return True

    def save_bars(self, code, df):
        """Pandas DataFrame 데이터를 DB에 저장 (중복 무시)"""
        if df.empty: return
        
        # [v12.5] Normalize code consistently
        code = normalize_code(code)
        
        data = df.copy()
        # 컬럼명 통일 (time/Date -> datetime)
        if 'time' in data.columns:
            data.rename(columns={'time': 'datetime'}, inplace=True)
        elif 'Date' in data.columns:
            data.rename(columns={'Date': 'datetime'}, inplace=True)
            
        # 소문자 통일 (Open -> open 등)
        data.columns = [c.lower() for c in data.columns]
        
        # code 컬럼 추가
        data['code'] = code
        
        # 필요한 컬럼만 추출/순서 정렬
        cols = ['code', 'datetime', 'open', 'high', 'low', 'close', 'volume']
        data = data[cols]

        with self._lock, self._get_connection() as conn:
            # [v6.5.6] INSERT OR IGNORE 로직 (PK 충돌 무시)
            cursor = conn.cursor()
            rows = data.values.tolist()
            cursor.executemany("""
                INSERT OR IGNORE INTO bars_1m (code, datetime, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, rows)
            
            # 메타 정보 업데이트 (최신/최고 과거 날짜 갱신)
            last_dt = data['datetime'].max()
            first_dt = data['datetime'].min()
            
            cursor.execute("""
                INSERT INTO sync_meta (code, last_sync_dt, earliest_sync_dt) VALUES (?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET 
                    last_sync_dt = MAX(COALESCE(last_sync_dt, ''), excluded.last_sync_dt),
                    earliest_sync_dt = MIN(COALESCE(earliest_sync_dt, '9999-12-31'), excluded.earliest_sync_dt)
            """, (code, last_dt, first_dt))
            conn.commit()

    def refresh_resampled_data(self, code):
        """1분봉을 기반으로 15분봉 테이블 갱신"""
        with self._get_connection() as conn:
            df_1m = pd.read_sql_query("SELECT datetime, open, high, low, close, volume FROM bars_1m WHERE code = ?", conn, params=[code])
            if df_1m.empty: return
            
            df_1m['datetime'] = pd.to_datetime(df_1m['datetime'])
            df_1m.set_index('datetime', inplace=True)
            
            df_15m = df_1m.resample('15min').agg({
                'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
            }).dropna()
            
            df_15m.reset_index(inplace=True)
            df_15m['datetime'] = df_15m['datetime'].dt.strftime('%Y-%m-%d %H:%M:%S')
            df_15m['code'] = code
            
            # 15분봉 저장 (중복 제거를 위해 REPLACE 또는 DELETE 후 INSERT)
            conn.execute("DELETE FROM bars_15m WHERE code = ?", (code,))
            df_15m.to_sql('bars_15m', conn, if_exists='append', index=False)
            conn.commit()

    def get_bars(self, code, start_dt=None, end_dt=None, interval='1m', limit=None):
        """특정 구간의 데이터를 DataFrame으로 반환 (v9.5: C-level date parsing)"""
        code = normalize_code(code)
        table = 'bars_1m' if interval == '1m' else 'bars_15m'
        query = f"SELECT datetime as time, open, high, low, close, volume FROM {table} WHERE code = ?"
        params = [code]
        if start_dt: query += " AND datetime >= ?"; params.append(start_dt)
        if end_dt: query += " AND datetime <= ?"; params.append(end_dt)
        query += " ORDER BY datetime ASC"
        if limit: query += f" LIMIT {limit}"

        with self._get_connection() as conn:
            df = pd.read_sql_query(query, conn, params=params, parse_dates=['time'])
            if not df.empty:
                df.columns = [c.capitalize() if c.lower() != 'time' else 'time' for c in df.columns]
            return df

    def get_last_sync_time(self, code):
        code = normalize_code(code)
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT last_sync_dt FROM sync_meta WHERE code = ?", (code,))
            row = cursor.fetchone()
            return row[0] if row else None

    def get_earliest_sync_time(self, code):
        code = normalize_code(code)
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT earliest_sync_dt FROM sync_meta WHERE code = ?", (code,))
            row = cursor.fetchone()
            return row[0] if row else None

    def get_sync_status(self, code):
        """특정 종목의 동기화 상태(COMPLETE 등) 조회"""
        code = normalize_code(code)
        with self._get_connection() as conn:
            row = conn.execute("SELECT status FROM sync_meta WHERE code = ?", (code,)).fetchone()
            return row[0] if row else None

    def update_daily_data(self, code, start_date="2020-01-01"):
        """[Selection Logic] FinanceDataReader를 통해 일봉 데이터를 DB에 동기화"""
        import FinanceDataReader as fdr
        kiwoom_code = normalize_code(code)
        
        # [v16.1] 지수 코드 매핑 (키움 -> FDR/Yahoo)
        mapping = {
            '001': 'KS11',   # KOSPI
            '101': 'KQ11',   # KOSDAQ
            'KS11': 'KS11',
            'KQ11': 'KQ11',
            '^VIX': '^VIX'
        }
        fdr_code = mapping.get(kiwoom_code, kiwoom_code)

        # 마지막 동기화 일자 확인
        last_sync = self.get_last_daily_sync(kiwoom_code)
        if last_sync:
            fetch_start = (datetime.strptime(last_sync, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        else:
            fetch_start = start_date

        print(f"  [Sync-1D] {kiwoom_code}({fdr_code}) ({fetch_start} ~ )")
        try:
            df = fdr.DataReader(fdr_code, fetch_start)
            if df is None or df.empty:
                return False
            
            # [v16.1] 강건한 컬럼 파싱 (Date/index -> datetime)
            df = df.reset_index()
            # 모든 컬럼명을 소문자로
            df.columns = [str(c).lower() for c in df.columns]
            
            # 'date' 또는 'index' 컬럼을 'datetime'으로 변경
            if 'date' in df.columns:
                df.rename(columns={'date': 'datetime'}, inplace=True)
            elif 'index' in df.columns:
                df.rename(columns={'index': 'datetime'}, inplace=True)
            
            # 필수 데이터 한글명 -> 소문자 영문명 매핑 (데이터 소스에 따라 다름)
            rename_map = {
                'open': 'open', 'high': 'high', 'low': 'low', 'close': 'close', 'volume': 'volume',
                '시가': 'open', '고가': 'high', '저가': 'low', '종가': 'close', '거래량': 'volume'
            }
            df.rename(columns=rename_map, inplace=True)
            
            # 날짜 포맷팅
            df['datetime'] = pd.to_datetime(df['datetime']).dt.strftime('%Y-%m-%d')
            
            self.save_daily_bars(kiwoom_code, df)
            return True
        except Exception as e:
            print(f"  [Error] {kiwoom_code} 일봉 동기화 중 오류: {e}")
            return False

    def save_daily_bars(self, code, df):
        """일봉 데이터를 DB에 저장 (bars_1d)"""
        if df.empty: return
        code = normalize_code(code)
        
        # 필요한 컬럼만 추출/순서 정렬
        cols = ['datetime', 'open', 'high', 'low', 'close', 'volume']
        data = df[cols].copy()
        data['code'] = code

        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            rows = data[['code', 'datetime', 'open', 'high', 'low', 'close', 'volume']].values.tolist()
            cursor.executemany("""
                INSERT OR REPLACE INTO bars_1d (code, datetime, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, rows)
            
            # 메타 정보 업데이트
            last_dt = data['datetime'].max()
            cursor.execute("""
                INSERT INTO sync_meta (code, last_daily_sync) VALUES (?, ?)
                ON CONFLICT(code) DO UPDATE SET last_daily_sync = excluded.last_daily_sync
            """, (code, last_dt))
            conn.commit()

    def get_daily_bars(self, code, start_dt=None, end_dt=None, limit=None):
        """특정 구간의 일봉 데이터를 DataFrame으로 반환"""
        code = normalize_code(code)
        query = "SELECT datetime as time, open, high, low, close, volume FROM bars_1d WHERE code = ?"
        params = [code]
        if start_dt: query += " AND datetime >= ?"; params.append(start_dt[:10])
        if end_dt: query += " AND datetime <= ?"; params.append(end_dt[:10])
        query += " ORDER BY datetime ASC"
        if limit: query += f" LIMIT {limit}"

        with self._get_connection() as conn:
            df = pd.read_sql_query(query, conn, params=params)
            if not df.empty:
                df['time'] = pd.to_datetime(df['time'])
                df.columns = [c.capitalize() if c.lower() != 'time' else 'time' for c in df.columns]
            return df

    def get_last_daily_sync(self, code):
        code = normalize_code(code)
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT last_daily_sync FROM sync_meta WHERE code = ?", (code,))
            row = cursor.fetchone()
            return row[0] if row and row[0] else None

    def get_trading_days(self, start_date, end_date):
        """KOSPI(KS11) 데이터를 기준으로 거래소 영업일 리스트 반환"""
        query = "SELECT DISTINCT datetime FROM bars_1d WHERE code = 'KS11' AND datetime BETWEEN ? AND ? ORDER BY datetime ASC"
        with self._get_connection() as conn:
            cursor = conn.execute(query, (start_date, end_date))
            return [row[0] for row in cursor.fetchall()]

    def set_sync_status(self, code, status):
        """특정 종목의 동기화 상태 저장"""
        code = normalize_code(code)
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO sync_meta (code, status) VALUES (?, ?)
                ON CONFLICT(code) DO UPDATE SET status = excluded.status
            """, (code, status))
            conn.commit()

    def fetch_kiwoom_minute_data(self, code, token):
        """키움 주식분봉차트조회(ka10080) 호출"""
        try:
            url = f"{config.URL}/api/dostk/chart"
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "authorization": f"Bearer {token}",
                "cont-yn": "N",
                "next-key": "",
                "api-id": "ka10080"
            }
            body = {
                "stk_cd": code,
                "tic_scope": "1", 
                "upd_stkpc_tp": "1"
            }
            res = requests.post(url, headers=headers, json=body, timeout=10)
            
            if res.status_code == 200:
                data = res.json()
                raw_list = data.get('stk_min_pole_chart_qry', data.get('output', data.get('data', [])))
                return raw_list
            return None
        except Exception as e:
            print(f"  [Error] API Fetch Exception ({code}): {e}")
            return None

    def build_volume_profile(self, minute_data):
        """분봉 데이터를 기반으로 시간대별 '누적 거래량 평균' 프로필 생성"""
        if not minute_data: return {}, {}
        try:
            df = pd.DataFrame(minute_data)
            f_vl = 'trde_qty' if 'trde_qty' in df.columns else ('cntg_vol' if 'cntg_vol' in df.columns else 'volume')
            f_tm = 'cntr_tm' if 'cntr_tm' in df.columns else ('data_dt' if 'data_dt' in df.columns else 'time')
            
            df['vol'] = pd.to_numeric(df[f_vl].astype(str).str.replace('+', '').str.replace('-', ''), errors='coerce').fillna(0)
            df['dt_str'] = df[f_tm].astype(str)
            
            if len(df['dt_str'].iloc[0]) >= 12: 
                df['day'] = df['dt_str'].str[:8]
                df['time_slot'] = df['dt_str'].str[8:12]
            else: 
                df['day'] = datetime.now().strftime('%Y%m%d')
                df['time_slot'] = df['dt_str'].str[:4]
            
            df = df.sort_values(by=['day', 'time_slot'])
            df['cum_vol'] = df.groupby('day')['vol'].cumsum()
            
            profile = df.groupby('time_slot')['cum_vol'].mean().to_dict()
            min_profile = df.groupby('time_slot')['vol'].mean().to_dict()
            
            return profile, min_profile
        except Exception as e:
            print(f"  [Error] Profile build error: {e}")
            return {}, {}

    def parse_to_bars(self, minute_data, code=""):
        """API 원본 데이터를 Pandas DataFrame으로 변환 (15분봉 리샘플링)"""
        if not minute_data: return pd.DataFrame()
        try:
            first_item = minute_data[0]
            f_cl = 'cur_prc' if 'cur_prc' in first_item else ('stck_prpr' if 'stck_prpr' in first_item else ('stck_clpr' if 'stck_clpr' in first_item else 'close'))
            f_op = 'open_pric' if 'open_pric' in first_item else ('stck_oprc' if 'stck_oprc' in first_item else 'open')
            f_hg = 'high_pric' if 'high_pric' in first_item else ('stck_hgpr' if 'stck_hgpr' in first_item else 'high')
            f_lw = 'low_pric' if 'low_pric' in first_item else ('stck_lwpr' if 'stck_lwpr' in first_item else 'low')
            f_tm = 'cntr_tm' if 'cntr_tm' in first_item else ('stck_cntg_hour' if 'stck_cntg_hour' in first_item else 'time')
            f_vl = 'trde_qty' if 'trde_qty' in first_item else ('cntg_vol' if 'cntg_vol' in first_item else 'volume')

            df = pd.DataFrame(minute_data)
            for c in [f_cl, f_op, f_hg, f_lw, f_vl]:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c].astype(str).str.replace('+', '').str.replace('-', ''), errors='coerce').fillna(0)
            
            df['time_str'] = df[f_tm].astype(str)
            if len(df['time_str'].iloc[0]) >= 12:
                df['datetime'] = pd.to_datetime(df['time_str'], format='%Y%m%d%H%M%S', errors='coerce')
            elif len(df['time_str'].iloc[0]) == 6:
                today_prefix = datetime.now().strftime('%Y%m%d')
                df['datetime'] = pd.to_datetime(today_prefix + df['time_str'], format='%Y%m%d%H%M%S', errors='coerce')
            else:
                df['datetime'] = pd.to_datetime(df['time_str'], errors='coerce')

            df = df.dropna(subset=['datetime'])
            df.set_index('datetime', inplace=True)
            df = df.sort_index()
            
            resampled = df.resample('15min', closed='right', label='right').agg({
                f_op: 'first', f_hg: 'max', f_lw: 'min', f_cl: 'last', f_vl: 'sum'
            }).dropna()
            
            resampled.columns = ['o', 'h', 'l', 'c', 'v']
            resampled['time'] = resampled.index.strftime('%H%M')
            
            resampled['ma5'] = resampled['c'].rolling(window=5).mean()
            resampled['ma20'] = resampled['c'].rolling(window=20).mean()
            resampled['ma60'] = resampled['c'].rolling(window=60).mean()
            
            return resampled[['time', 'o', 'h', 'l', 'c', 'v', 'ma5', 'ma20', 'ma60']].tail(100)
        except Exception as e:
            print(f"  [Error] Bar parsing error ({code}): {e}")
            return pd.DataFrame()

    def fetch_nxt_status(self, code, token):
        """ka10100 API를 사용하여 NXT 거래 가능 여부 확인"""
        if not token: return False
        try:
            url = f"{config.URL}/api/dostk/stkinfo"
            headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'authorization': f'Bearer {token}',
                'api-id': 'ka10100',
            }
            params = {'stk_cd': code}
            res = requests.post(url, headers=headers, json=params, timeout=3)
            if res.status_code == 200:
                data = res.json()
                return data.get('nxtEnable') == 'Y'
        except: pass
        return False

    def prepare_volume_profiles(self, monitor_codes, token, bars_data, volume_profiles, min_volume_profiles):
        """실시간 감시를 위한 프로필 데이터 빌드 및 bars_data 업데이트"""
        if not token: return
        
        def process(code):
            fresh_data = self.fetch_kiwoom_minute_data(code, token)
            if not fresh_data: return
            
            prof, min_prof = self.build_volume_profile(fresh_data)
            volume_profiles[code] = prof
            min_volume_profiles[code] = min_prof
            
            new_df = self.parse_to_bars(fresh_data, code)
            if code in bars_data and not bars_data[code].empty:
                old_df = bars_data[code]
                combined = pd.concat([old_df, new_df]).drop_duplicates(subset=['time'], keep='last')
                bars_data[code] = combined.sort_values('time').tail(100)
            else:
                bars_data[code] = new_df

        with ThreadPoolExecutor(max_workers=10) as executor:
            list(executor.map(process, monitor_codes))

    def save_bars_cache(self, bars_data, cache_file):
        """RSI 봉 데이터를 JSON 파일로 캐싱"""
        try:
            cache_data = {}
            for code, df in bars_data.items():
                if not df.empty:
                    cache_data[code] = df.to_dict(orient='records')
            
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"  [Error] Cache save error: {e}")

    def load_bars_cache(self, cache_file):
        """JSON 파일에서 RSI 봉 데이터 복구"""
        if not os.path.exists(cache_file): return {}
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
            
            bars_data = {}
            for code, records in cache_data.items():
                bars_data[code] = pd.DataFrame(records)
            return bars_data
        except Exception as e:
            print(f"  [Error] Cache load error: {e}")
            return {}

    def send_telegram_message(self, message):
        import importlib
        importlib.reload(config)
        token, cid = getattr(config, "TELEGRAM_BOT_TOKEN", ""), getattr(config, "TELEGRAM_CHAT_ID", "")
        if token and cid and "발급" not in token:
            try:
                res = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", 
                                   json={"chat_id": cid, "text": message, "parse_mode": "HTML"}, timeout=5)
                if res.status_code != 200:
                    print(f"  [Telegram Error] Status: {res.status_code}, Body: {res.text}")
            except Exception as e:
                print(f"  [Telegram Exception] {e}")
        else:
            print(f"  [Telegram Skip] Token or Chat ID not properly set in config.py")

    def get_market_indices(self):
        """ FinanceDataReader를 사용하여 시장 지수(KOSPI, KOSDAQ)를 조회 """
        import FinanceDataReader as fdr
        indices = {"KS11": "KOSPI", "KQ11": "KOSDAQ"}
        results = {}
        
        for ticker, name in indices.items():
            try:
                df = fdr.DataReader(ticker, (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d'))
                if df is not None and not df.empty:
                    last = df.iloc[-1]
                    prev = df.iloc[-2]
                    price = float(last['Close'])
                    ratio = float((price / prev['Close'] - 1) * 100)
                    
                    ma5_series = df['Close'].rolling(5).mean()
                    ma5 = ma5_series.iloc[-1]
                    ma5_prev = ma5_series.iloc[-2]
                    slope = ma5 - ma5_prev
                    
                    if price >= ma5 and slope > 0: trend = "UP"
                    elif abs(price - ma5) / ma5 < 0.005: trend = "NEUTRAL"
                    else: trend = "DOWN"
                    
                    results[name] = {"val": price, "ratio": ratio, "trend": trend}
            except: continue
        return results

    def _final_save_settings(self, settings_file, settings):
        """설정 파일에 최종 저장"""
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)

    def get_kospi_list(self, count=200, force=False):
        """KOSPI 종목 리스트 수집/확인 (count에 따라 200/500/전체 등 지원)"""
        import FinanceDataReader as fdr
        cache_file = "kospi_200.csv" if count <= 200 else f"kospi_top_{count}.csv"
        try:
            is_expired = False
            if os.path.exists(cache_file):
                file_age_days = (time.time() - os.path.getmtime(cache_file)) / (24 * 3600)
                if file_age_days > 30: is_expired = True

            if not force and os.path.exists(cache_file) and not is_expired:
                return pd.read_csv(cache_file, dtype={'Code': str})
            else:
                df = fdr.StockListing('KOSPI')
                df = df[df['Code'].str.endswith('0')]
                df_sorted = df.sort_values(by='Marcap', ascending=False).head(count)
                df_sorted.to_csv(cache_file, index=False, encoding='utf-8-sig')
                return df_sorted
        except Exception as e:
            print(f"  [Error] KOSPI {count} list fetch error: {e}")
            return None

# 싱글톤 인스턴스 (필요 시)
# data_manager = DataManagerV2()
