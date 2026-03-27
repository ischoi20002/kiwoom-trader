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

            # 5. 실시간 데이터 스냅샷 테이블 (체결강도, 호가잔량 로깅용)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS realtime_snapshots (
                    code TEXT,
                    datetime TEXT,
                    price INTEGER,
                    vol_power REAL,
                    total_ask INTEGER,
                    total_bid INTEGER,
                    PRIMARY KEY (code, datetime)
                )
            """)

            # 6. 활성 포지션 테이블 (실전/모의 매매 상태 유지용) [v3.7.8]
            conn.execute("""
                CREATE TABLE IF NOT EXISTS active_positions (
                    code TEXT PRIMARY KEY,
                    name TEXT,
                    avg_price REAL,
                    qty INTEGER,
                    stage INTEGER,
                    invested INTEGER,
                    entry_time TEXT,
                    reason TEXT,
                    status TEXT DEFAULT 'OPEN'
                )
            """)

            # 7. 주문 이력 테이블 [v3.7.8]
            conn.execute("""
                CREATE TABLE IF NOT EXISTS order_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT,
                    side TEXT,
                    qty INTEGER,
                    price INTEGER,
                    order_type TEXT,
                    order_no TEXT,
                    timestamp TEXT,
                    result TEXT
                )
            """)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_bars_1m_code_dt ON bars_1m (code, datetime)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bars_15m_code_dt ON bars_15m (code, datetime)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_bars_1d_code_dt ON bars_1d (code, datetime)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_realtime_snap_code_dt ON realtime_snapshots (code, datetime)")

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

    def save_realtime_snapshot(self, code, dt, price, vol_power, total_ask, total_bid):
        """[v35.7] 실시간 데이터 스케줄 스냅샷 저장 (모델 학습용)"""
        code = normalize_code(code)
        with self._lock, self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO realtime_snapshots (code, datetime, price, vol_power, total_ask, total_bid)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (code, dt, price, vol_power, total_ask, total_bid))
            conn.commit()

    def save_active_position(self, code, name, avg_price, qty, stage, invested, entry_time, reason):
        """[v3.7.8] 활성 포지션 정보 저장"""
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO active_positions (code, name, avg_price, qty, stage, invested, entry_time, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (code, name, avg_price, qty, stage, invested, entry_time, reason))
            conn.commit()

    def delete_active_position(self, code):
        """[v3.7.8] 포지션 종료 시 삭제"""
        with self._get_connection() as conn:
            conn.execute("DELETE FROM active_positions WHERE code = ?", (code,))
            conn.commit()

    def log_order(self, code, side, qty, price, order_type, order_no, result):
        """[v3.7.8] 주문 이력 기록"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO order_history (code, side, qty, price, order_type, order_no, timestamp, result)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (code, side, qty, price, order_type, order_no, now, result))
            conn.commit()

    def sync_historical_vol_power(self, code, token):
        """[v31.2] ka10046 API를 사용하여 과거 체결강도 데이터를 수집 및 저장"""
        code = normalize_code(code)
        url = f"{config.URL}/api/dostk/mrkcond"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "api-id": "ka10046"
        }
        
        # 현재 날짜로부터 역순으로 날짜 추정 (ka10046은 날짜를 명시적으로 주지 않으므로)
        current_date = datetime.now()
        
        next_key = ""
        total_saved = 0
        last_tm = "999999"
        
        # [v31.2] 최신 데이터부터 과거로 수집
        while True:
            body = {
                "stk_cd": code,
                "tic_scope": "1",
                "vol_pow_tp": "1"
            }
            if next_key:
                headers["cont-yn"] = "Y"
                headers["next-key"] = next_key

            try:
                res = requests.post(url, headers=headers, json=body, timeout=10)
                if res.status_code != 200: break
                
                data = res.json()
                raw_list = data.get('stk_cntg_hour_trnd_qry', data.get('output', []))
                next_key = res.headers.get('next-key', "")
                
                if not raw_list: break
                
                snapshots = []
                for item in raw_list:
                    tm = str(item.get('cntg_tm', '000000')).strip()
                    if len(tm) < 6: tm = tm.zfill(6)
                    
                    # 날짜 경계 감지 (시간이 갑자기 커지면 전일로 이동)
                    if tm > last_tm:
                        # 주말/공휴일 제외 로직은 생략(단순화), 실제 거래일 기반으로 날짜 차감
                        current_date -= timedelta(days=1)
                        # 금요일에서 목요일이 아니라 금요일에서 월요일 등으로 건너뛰는 것은 실제 데이터 흐름에 맡김
                    
                    last_tm = tm
                    dt_str = f"{current_date.strftime('%Y-%m-%d')} {tm[:2]}:{tm[2:4]}:{tm[4:6]}"
                    price = int(str(item.get('cur_prc', '0')).replace('+', '').replace('-', '').strip())
                    vol_pow = float(item.get('cntr_str', '0'))
                    
                    snapshots.append((code, dt_str, price, vol_pow))
                
                with self._lock, self._get_connection() as conn:
                    conn.executemany("""
                        INSERT OR REPLACE INTO realtime_snapshots (code, datetime, price, vol_power)
                        VALUES (?, ?, ?, ?)
                    """, snapshots)
                    conn.commit()
                
                total_saved += len(snapshots)
                if not next_key: break
                time.sleep(0.2)
                
            except Exception as e:
                print(f"  (!) VolPower Sync Error ({code}): {e}")
                break
                
        return total_saved

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

    def fetch_kiwoom_minute_data(self, code, token, logger=None):
        """키움 주식분봉차트조회(ka10080) 호출 - [v3.5.2] Retry 로직 추가"""
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
        
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                res = requests.post(url, headers=headers, json=body, timeout=10)
                
                if res.status_code == 200:
                    data = res.json()
                    raw_list = data.get('stk_min_pole_chart_qry', data.get('output', data.get('data', [])))
                    return raw_list
                elif res.status_code == 429:
                    if attempt < max_retries:
                        wait = (attempt + 1) * 2
                        time.sleep(wait)
                        continue
                
                if res.status_code != 200:
                    # [v3.5.2] 에러 발생 시 로그 출력 (429 차단 등 대응)
                    if logger: logger(f"  [Error] API Status {res.status_code} ({code})")
                    else: print(f"  [Error] API Status {res.status_code} ({code})")
                return None
            except Exception as e:
                if attempt < max_retries:
                    time.sleep(1)
                    continue
                print(f"  [Error] API Fetch Exception ({code}): {e}")
                return None
        return None

    def fetch_and_parse_15m_bars(self, code, token):
        """[v3.5] DB와 API 데이터를 무조건 병합하여 15분봉 워밍업 수행 (29/60 부족 현상 방지)"""
        # 1. DB에서 가장 최신의 1분봉 5000개 조회 (약 10거래일분)
        n_code = normalize_code(code)
        query = "SELECT datetime, open, high, low, close, volume FROM bars_1m WHERE code = ? ORDER BY datetime DESC LIMIT 5000"
        with self._get_connection() as conn:
            df_db = pd.read_sql_query(query, conn, params=[n_code])
        
        
        # 2. API에서 최신 데이터 보충 (오늘 데이터 확보)
        df_api = pd.DataFrame()
        if token:
            # [v3.5.2] Throttling 강화: 0.1~0.3 -> 0.5~1.0
            import random
            time.sleep(random.uniform(0.5, 1.0))
            raw_api = self.fetch_kiwoom_minute_data(code, token)
            if raw_api:
                df_api = pd.DataFrame(raw_api)
                # 컬럼 파싱 및 정규화
                f_cl = 'cur_prc'; f_op = 'open_pric'; f_hg = 'high_pric'; f_lw = 'low_pric'; f_tm = 'cntr_tm'; f_vl = 'trde_qty'
                for c in [f_cl, f_op, f_hg, f_lw, f_vl]:
                    if c in df_api.columns:
                        df_api[c] = pd.to_numeric(df_api[c].astype(str).str.replace('+', '').str.replace('-', ''), errors='coerce').fillna(0)
                
                df_api['datetime'] = pd.to_datetime(df_api[f_tm], format='%Y%m%d%H%M%S', errors='coerce').dt.strftime('%Y-%m-%d %H:%M:%S')
                df_api.rename(columns={f_op: 'open', f_hg: 'high', f_lw: 'low', f_cl: 'close', f_vl: 'volume'}, inplace=True)
                df_api = df_api[['datetime', 'open', 'high', 'low', 'close', 'volume']]

        # 3. 데이터 결합 (DB + API) 및 최신 5000개 유지
        df_combined = pd.concat([df_db, df_api]).drop_duplicates('datetime').sort_values('datetime')
        if df_combined.empty: return pd.DataFrame()
        
        df_final = df_combined.tail(5000).copy() # [Fix] SettingWithCopyWarning 방지
        

        try:
            # 4. 1분봉 -> 15분봉 리샘플링 (DatetimeIndex 보존)
            df_final['datetime'] = pd.to_datetime(df_final['datetime'])
            df_final.set_index('datetime', inplace=True)
            
            resampled = df_final.resample('15min', closed='right', label='right').agg({
                'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'
            }).dropna()
            
            # [v3.5] 전략 엔진 규격 (CamelCase & DatetimeIndex)
            resampled['time'] = resampled.index.strftime('%H%M')
            resampled.rename(columns={
                'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'
            }, inplace=True)
            
            # [Merge Debug]
            # print(f"  [Merge] {code}: Final Bars {len(resampled)}/60")
            
            return resampled.tail(300) # 충분한 버퍼 (300개) 유지
        except Exception as e:
            print(f"  [Error] 15m DB Warmup Parse Error ({code}): {e}")
            return pd.DataFrame()

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
            
            resampled.columns = ['Open', 'High', 'Low', 'Close', 'Volume']
            resampled['time'] = resampled.index.strftime('%H%M')
            
            resampled['ma5'] = resampled['Close'].rolling(window=5).mean()
            resampled['ma20'] = resampled['Close'].rolling(window=20).mean()
            resampled['ma60'] = resampled['Close'].rolling(window=60).mean()
            
            return resampled[['time', 'Open', 'High', 'Low', 'Close', 'Volume', 'ma5', 'ma20', 'ma60']].tail(100)
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
            # [v3.5.2] Throttling 강화: 0.1~0.5 -> 0.5~1.5
            import random
            time.sleep(random.uniform(0.5, 1.5))
            fresh_data = self.fetch_kiwoom_minute_data(code, token)
            if not fresh_data: return
            
            prof, min_prof = self.build_volume_profile(fresh_data)
            volume_profiles[code] = prof
            min_volume_profiles[code] = min_prof
            
            # [v3.5 Fix] 이미 워밍업된 bars_data를 여기서 다시 업데이트하면
            # time('0915' 등) 기준 중복 제거로 인해 과거 데이터가 파괴됨.
            # 따라서 프로필만 계산하고 데이터 업데이트는 생략함.
            pass

        # [v3.5.2] API 호출 제한 방지를 위해 병렬 허용치 축소 (10 -> 2)
        with ThreadPoolExecutor(max_workers=2) as executor:
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
        """[v27.0] 백테스팅 엔진과 동일한 Triple-Lock (MSI, ADX, VIX) 기반 시장 지수 조회 (헤비/2분 주기)"""
        import FinanceDataReader as fdr
        from strategy import indicators
        indices = {"KS11": "KOSPI", "KQ11": "KOSDAQ"}
        results = {}
        
        # 1. VIX(변동성 지수) 수집
        vix_val = 15.0
        try:
            vix_df = fdr.DataReader('VIX', (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d'))
            if not vix_df.empty: vix_val = float(vix_df.iloc[-1]['Close'])
        except:
            try:
                vix_df = fdr.DataReader('FRED:VIXCLS', (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d'))
                if not vix_df.empty: vix_val = float(vix_df.iloc[-1].iloc[0])
            except: pass

        # 2. 각 지수별 추세 판단 (Triple-Lock)
        for ticker, name in indices.items():
            try:
                # [v27.0] MSI/ADX 산출을 위해 200일치 데이터 수집
                df = fdr.DataReader(ticker, (datetime.now() - timedelta(days=300)).strftime('%Y-%m-%d'))
                if df is not None and len(df) >= 120:
                    df.columns = [c.capitalize() if c.lower() == 'close' else c for c in df.columns]
                    df = indicators.calculate_all_indicators(df, {})
                    last = df.iloc[-1]
                    
                    price = float(last['Close'])
                    ratio = float((price / df.iloc[-2]['Close'] - 1) * 100)
                    msi = float(last.get('MSI', 0))
                    adx = float(last.get('ADX', 0))
                    
                    # Triple-Lock 판단 로직
                    lock_msi = msi < -0.05
                    lock_vix = vix_val > 30
                    lock_adx = adx < 15
                    triple_lock_down = lock_msi | lock_vix | lock_adx
                    
                    if triple_lock_down: trend = "DOWN"
                    elif msi > 0: trend = "UP"
                    else: trend = "NEUTRAL"
                    
                    results[name] = {
                        "val": price, "ratio": ratio, "trend": trend,
                        "msi": msi, "adx": adx, "vix": vix_val
                    }
            except Exception as e:
                print(f"  [Error] Market Index ({name}) calculation error: {e}")
                continue
        return results

    def get_realtime_indices(self):
        """[v27.4] 네이버 금융 Polling API를 이용한 초실시간 지수 가격 수집 (라이트/10초 주기)"""
        url = "https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI,KOSDAQ"
        headers = {'User-Agent': 'Mozilla/5.0'}
        results = {}
        try:
            resp = requests.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                for item in data.get('datas', []):
                    code = item['itemCode'] 
                    results[code] = {
                        'val': float(item['closePriceRaw']),
                        'ratio': float(item['fluctuationsRatioRaw'])
                    }
        except Exception as e:
            print(f"[DataManager] Realtime Index Fetch Error: {e}")
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
