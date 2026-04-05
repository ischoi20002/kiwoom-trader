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
from concurrent.futures import ThreadPoolExecutor, as_completed

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
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL;")  # 동시성 확보를 위한 WAL 모드
            return conn
        except sqlite3.DatabaseError as e:
            if "malformed" in str(e).lower():
                print(f"CRITICAL [DataManagerV2]: Database at {self.db_path} is malformed.")
                self._handle_corrupted_db()
                # Re-try once after handling
                conn = sqlite3.connect(self.db_path, timeout=30)
                conn.execute("PRAGMA journal_mode=WAL;")
                return conn
            else:
                raise e

    def run_analyze(self) -> bool:
        """
        SQLite 통계(sqlite_stat*) 갱신으로 쿼리 플래너 품질 개선.
        대량 INSERT 후(하베스팅 종료 등) 한 번 호출하는 것을 권장.
        """
        try:
            with self._get_connection() as conn:
                conn.execute("ANALYZE")
                conn.commit()
            return True
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] ANALYZE 실패: {e}")
            except Exception:
                pass
            return False
        except Exception as e:
            try:
                print(f"[DataManagerV2] ANALYZE 예외: {e}")
            except Exception:
                pass
            return False

    def _handle_corrupted_db(self):
        """손상된 데이터베이스 파일을 백업하고 새로운 파일을 생성할 수 있도록 준비"""
        import time
        timestamp = int(time.time())
        backup_path = f"{self.db_path}.corrupted_{timestamp}"
        print(f"CRITICAL [DataManagerV2]: Renaming corrupted DB to {backup_path}")
        try:
            # Handle potential file lock issues by trying to rename
            if os.path.exists(self.db_path):
                os.rename(self.db_path, backup_path)
        except Exception as e:
            print(f"CRITICAL [DataManagerV2]: Failed to rename corrupted DB: {e}")
            # If rename fails, we might need manual intervention or just try to delete if permitted
            raise e

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
            # PK(code, datetime)로 (code, 구간) 조회가 이미 최적화됨. 아래 idx_bars_15m_code_dt는
            # 구 스키마/마이그레이션 호환 및 EXPLAIN 일관성용 CREATE INDEX IF NOT EXISTS.
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

            # 8. 원격 명령 테이블 [v3.8]
            conn.execute("""
                CREATE TABLE IF NOT EXISTS remote_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT,
                    timestamp TEXT,
                    status TEXT DEFAULT 'PENDING'
                )
            """)

            # 9. 시스템 설정/상태 테이블 [v3.8] 
            conn.execute("""
                CREATE TABLE IF NOT EXISTS system_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
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
                pass
            
            # [Migration] order_history 테이블에 체결 정보 및 신호 메타데이터 컬럼 추가 [v3.8]
            try:
                conn.execute("ALTER TABLE order_history ADD COLUMN filled_price INTEGER DEFAULT 0")
                conn.execute("ALTER TABLE order_history ADD COLUMN filled_qty INTEGER DEFAULT 0")
                conn.execute("ALTER TABLE order_history ADD COLUMN name TEXT DEFAULT ''")
                conn.execute("ALTER TABLE order_history ADD COLUMN reason TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass

            # [v4.10] 공공데이터 KRX 상장 스냅샷 반영용 (하베스팅 유니버스·탈락 추적)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS listing_universe_state (
                    code TEXT PRIMARY KEY,
                    state TEXT NOT NULL DEFAULT 'ACTIVE',
                    name TEXT,
                    market_id TEXT,
                    last_official_bas_dt TEXT,
                    updated_at TEXT,
                    exited_at TEXT
                )
            """)
            
            conn.commit()


    def merge_official_krx_listing(self, df) -> dict:
        """
        공공데이터포털 KRX KOSPI 스냅샷을 반영합니다.

        - 스냅샷에 포함된 종목: state=ACTIVE, exited_at=NULL
        - 기존 ACTIVE였으나 스냅샷에 없음: state=EXITED, exited_at 기록
        - bars_1m/bars_15m 등 가격 데이터는 삭제하지 않습니다 (과거 백테스트 보존).

        Returns
        -------
        dict
            active_count, snapshot_rows, newly_exited_count
        """
        import pandas as pd
        from datetime import datetime

        if df is None or (hasattr(df, "empty") and df.empty):
            return {"active_count": 0, "snapshot_rows": 0, "newly_exited_count": 0}

        if not isinstance(df, pd.DataFrame):
            raise TypeError("df는 pandas.DataFrame 이어야 합니다.")

        now_iso = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if "Code" not in df.columns:
            raise ValueError("DataFrame에 Code 컬럼이 필요합니다.")

        work = df.copy()
        work["Code"] = work["Code"].astype(str).str.strip().str.zfill(6)
        current_codes = set(work["Code"].tolist())

        newly_exited = 0
        with self._lock, self._get_connection() as conn:
            prev = conn.execute(
                "SELECT code FROM listing_universe_state WHERE state = 'ACTIVE'"
            ).fetchall()
            active_before = {str(r[0]).zfill(6) for r in prev}
            to_exit = active_before - current_codes
            for code in to_exit:
                conn.execute(
                    """
                    UPDATE listing_universe_state
                    SET state = 'EXITED', exited_at = ?, updated_at = ?
                    WHERE code = ? AND state = 'ACTIVE'
                    """,
                    (now_iso, now_iso, code),
                )
            newly_exited = len(to_exit)

            for _, row in work.iterrows():
                code = str(row["Code"]).zfill(6)
                name = ""
                if "Name" in work.columns:
                    try:
                        name = str(row.get("Name", "") or "")
                    except Exception:
                        name = ""
                mkt = ""
                if "mrktCtg" in work.columns:
                    try:
                        mkt = str(row.get("mrktCtg", "") or "")
                    except Exception:
                        mkt = ""
                bas = ""
                if "basDt" in work.columns:
                    try:
                        bas = str(row.get("basDt", "") or "")
                    except Exception:
                        bas = ""
                conn.execute(
                    """
                    INSERT INTO listing_universe_state
                        (code, state, name, market_id, last_official_bas_dt, updated_at, exited_at)
                    VALUES (?, 'ACTIVE', ?, ?, ?, ?, NULL)
                    ON CONFLICT(code) DO UPDATE SET
                        state = 'ACTIVE',
                        name = excluded.name,
                        market_id = excluded.market_id,
                        last_official_bas_dt = excluded.last_official_bas_dt,
                        updated_at = excluded.updated_at,
                        exited_at = NULL
                    """,
                    (code, name, mkt, bas, now_iso),
                )
            conn.commit()

            n_active = conn.execute(
                "SELECT COUNT(*) FROM listing_universe_state WHERE state = 'ACTIVE'"
            ).fetchone()[0]

        return {
            "active_count": int(n_active),
            "snapshot_rows": len(work),
            "newly_exited_count": newly_exited,
        }

    def get_active_official_kospi_codes(self):
        """listing_universe_state 에서 ACTIVE 인 종목코드 리스트 (정렬)."""
        try:
            with self._get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT code FROM listing_universe_state
                    WHERE state = 'ACTIVE'
                    ORDER BY code
                    """
                ).fetchall()
            return [str(r[0]).zfill(6) for r in rows]
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] get_active_official_kospi_codes: {e}")
            except Exception:
                pass
            return []

    def update_historical_data(self, code, logger=None):
        """[Legacy Support] 1분/15분봉 및 일봉(bars_1d) 데이터를 서버에서 가져와 업데이트"""
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
        if last_sync:
            last_dt = datetime.strptime(last_sync, "%Y-%m-%d %H:%M:%S")
            now = datetime.now()
            if last_dt.date() == now.date() and last_dt.hour >= 15 and last_dt.minute >= 30:
                if status == "COMPLETE":
                    log(f"  [Skip] {code}: 이미 오늘 데이터까지 최신 상태(COMPLETE)입니다.")
                    return True
        
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
                    dt_fmt = f"{tm[:4]}-{tm[4:6]}-{tm[6:8]} {tm[8:10]}:{tm[10:12]}:{tm[12:14]}"
                    
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
                
                if chunk:
                    df = pd.DataFrame(chunk)
                    self.save_bars(code, df)
                
                next_key = data.get('next-key', "")
                if stop: break
                if not next_key:
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
            if stop: log(f"  [Info] {code} 이미 모든 구간이 동기화되어 있습니다.")
            else: log(f"  [Info] {code} API 서버에서 반환된 데이터가 없습니다.")
        
        return True

    def save_bars(self, code, df):
        if df.empty: return
        code = normalize_code(code)
        data = df.copy()
        if 'time' in data.columns: data.rename(columns={'time': 'datetime'}, inplace=True)
        elif 'Date' in data.columns: data.rename(columns={'Date': 'datetime'}, inplace=True)
        data.columns = [c.lower() for c in data.columns]
        data['code'] = code
        cols = ['code', 'datetime', 'open', 'high', 'low', 'close', 'volume']
        data = data[cols]

        with self._lock, self._get_connection() as conn:
            cursor = conn.cursor()
            rows = data.values.tolist()
            cursor.executemany("""
                INSERT OR IGNORE INTO bars_1m (code, datetime, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, rows)
            
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
            
            conn.execute("DELETE FROM bars_15m WHERE code = ?", (code,))
            df_15m.to_sql('bars_15m', conn, if_exists='append', index=False)
            conn.commit()

    def save_realtime_snapshot(self, code, dt, price, vol_power, total_ask, total_bid):
        code = normalize_code(code)
        with self._lock, self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO realtime_snapshots (code, datetime, price, vol_power, total_ask, total_bid)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (code, dt, price, vol_power, total_ask, total_bid))
            conn.commit()

    def save_active_position(self, code, name, avg_price, qty, stage, invested, entry_time, reason):
        with self._get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO active_positions (code, name, avg_price, qty, stage, invested, entry_time, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (code, name, avg_price, qty, stage, invested, entry_time, reason))
            conn.commit()

    def delete_active_position(self, code):
        with self._get_connection() as conn:
            conn.execute("DELETE FROM active_positions WHERE code = ?", (code,))
            conn.commit()

    def log_order(self, code, side, qty, price, order_type, order_no, result, name="", reason=""):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO order_history (code, side, qty, price, order_type, order_no, timestamp, result, name, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (code, side, qty, price, order_type, order_no, now, result, name, reason))
            conn.commit()

    def get_today_orders(self):
        today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT timestamp, code, side, qty, price, filled_price, filled_qty, result, name, reason
                FROM order_history 
                WHERE timestamp >= ? 
                ORDER BY timestamp DESC
            """, (today_start,))
            return cursor.fetchall()

    def get_orders_between(self, start_dt: str, end_dt: str):
        """
        날짜·시간 범위의 주문 이력을 시간순으로 반환합니다.
        (일일 결산 리포트 등 분석용)
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT timestamp, code, side, qty, price, filled_price, filled_qty, result, name, reason, order_no, order_type
                    FROM order_history
                    WHERE timestamp >= ? AND timestamp <= ?
                    ORDER BY timestamp ASC
                    """,
                    (start_dt, end_dt),
                )
                return cursor.fetchall()
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] get_orders_between 실패: {e}")
            except Exception:
                pass
            return []

    def get_latest_order_meta(self, code):
        with self._get_connection() as conn:
            cursor = conn.execute("""
                SELECT name, reason 
                FROM order_history 
                WHERE code = ? AND (result = 'SUCCESS' OR result = 'MOCK')
                ORDER BY timestamp DESC LIMIT 1
            """, (code,))
            res = cursor.fetchone()
            return res if res else (None, None)

    def update_order_status(self, code, order_no, new_status, filled_price=0, filled_qty=0):
        with self._get_connection() as conn:
            conn.execute("""
                UPDATE order_history 
                SET result = ?, filled_price = ?, filled_qty = ?
                WHERE code = ? AND (order_no = ? OR order_no = 'MOCK_ID')
                AND timestamp >= ?
            """, (new_status, filled_price, filled_qty, code, order_no, datetime.now().strftime("%Y-%m-%d 00:00:00")))
            conn.commit()

    def get_bars(self, code, start_dt=None, end_dt=None, interval='1m', limit=None):
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
            if not df.empty: df.columns = [c.capitalize() if c.lower() != 'time' else 'time' for c in df.columns]
            return df

    def get_last_sync_time(self, code):
        code = normalize_code(code)
        with self._get_connection() as conn:
            row = conn.execute("SELECT last_sync_dt FROM sync_meta WHERE code = ?", (code,)).fetchone()
            return row[0] if row else None

    def get_earliest_sync_time(self, code):
        code = normalize_code(code)
        with self._get_connection() as conn:
            row = conn.execute("SELECT earliest_sync_dt FROM sync_meta WHERE code = ?", (code,)).fetchone()
            return row[0] if row else None

    def get_sync_status(self, code):
        code = normalize_code(code)
        with self._get_connection() as conn:
            row = conn.execute("SELECT status FROM sync_meta WHERE code = ?", (code,)).fetchone()
            return row[0] if row else None

    def update_daily_data(self, code, start_date="2020-01-01"):
        import FinanceDataReader as fdr
        kiwoom_code = normalize_code(code)
        mapping = {'001': 'KS11', '101': 'KQ11', 'KS11': 'KS11', 'KQ11': 'KQ11', '^VIX': '^VIX'}
        fdr_code = mapping.get(kiwoom_code, kiwoom_code)
        last_sync = self.get_last_daily_sync(kiwoom_code)
        fetch_start = (datetime.strptime(last_sync, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d") if last_sync else start_date
        print(f"  [Sync-1D] {kiwoom_code}({fdr_code}) ({fetch_start} ~ )")
        try:
            df = fdr.DataReader(fdr_code, fetch_start)
            if df is None or df.empty: return False
            df = df.reset_index()
            df.columns = [str(c).lower() for c in df.columns]
            if 'date' in df.columns: df.rename(columns={'date': 'datetime'}, inplace=True)
            elif 'index' in df.columns: df.rename(columns={'index': 'datetime'}, inplace=True)
            rename_map = {'시가': 'open', '고가': 'high', '저가': 'low', '종가': 'close', '거래량': 'volume'}
            df.rename(columns=rename_map, inplace=True)
            df['datetime'] = pd.to_datetime(df['datetime']).dt.strftime('%Y-%m-%d')
            self.save_daily_bars(kiwoom_code, df)
            return True
        except Exception as e:
            print(f"  [Error] {kiwoom_code} 일봉 동기화 중 오류: {e}")
            return False

    def save_daily_bars(self, code, df):
        if df.empty: return
        code = normalize_code(code); data = df[['datetime', 'open', 'high', 'low', 'close', 'volume']].copy(); data['code'] = code
        with self._lock, self._get_connection() as conn:
            rows = data[['code', 'datetime', 'open', 'high', 'low', 'close', 'volume']].values.tolist()
            conn.executemany("INSERT OR REPLACE INTO bars_1d (code, datetime, open, high, low, close, volume) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
            last_dt = data['datetime'].max()
            conn.execute("INSERT INTO sync_meta (code, last_daily_sync) VALUES (?, ?) ON CONFLICT(code) DO UPDATE SET last_daily_sync = excluded.last_daily_sync", (code, last_dt))
            conn.commit()

    def get_daily_bars(self, code, start_dt=None, end_dt=None, limit=None):
        code = normalize_code(code); query = "SELECT datetime as time, open, high, low, close, volume FROM bars_1d WHERE code = ?"
        params = [code]
        if start_dt: query += " AND datetime >= ?"; params.append(start_dt[:10])
        if end_dt: query += " AND datetime <= ?"; params.append(end_dt[:10])
        query += " ORDER BY datetime ASC"
        if limit: query += f" LIMIT {limit}"
        with self._get_connection() as conn:
            df = pd.read_sql_query(query, conn, params=params)
            if not df.empty: df['time'] = pd.to_datetime(df['time']); df.columns = [c.capitalize() if c.lower() != 'time' else 'time' for c in df.columns]
            return df

    def get_last_daily_sync(self, code):
        code = normalize_code(code)
        with self._get_connection() as conn:
            row = conn.execute("SELECT last_daily_sync FROM sync_meta WHERE code = ?", (code,)).fetchone()
            return row[0] if row and row[0] else None

    def get_trading_days(self, start_date, end_date):
        query = "SELECT DISTINCT datetime FROM bars_1d WHERE code = '001' AND datetime BETWEEN ? AND ? ORDER BY datetime ASC"
        with self._get_connection() as conn:
            return [row[0] for row in conn.execute(query, (start_date, end_date)).fetchall()]

    def set_sync_status(self, code, status):
        code = normalize_code(code)
        with self._get_connection() as conn:
            conn.execute("INSERT INTO sync_meta (code, status) VALUES (?, ?) ON CONFLICT(code) DO UPDATE SET status = excluded.status", (code, status))
            conn.commit()

    def fetch_kiwoom_minute_data(self, code, token, logger=None):
        url = f"{config.URL}/api/dostk/chart"
        headers = {"Content-Type": "application/json;charset=UTF-8", "authorization": f"Bearer {token}", "cont-yn": "N", "next-key": "", "api-id": "ka10080"}
        body = {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "1"}
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                res = requests.post(url, headers=headers, json=body, timeout=10)
                if res.status_code == 200: return res.json().get('stk_min_pole_chart_qry', res.json().get('output', res.json().get('data', [])))
                elif res.status_code == 429:
                    if attempt < max_retries: time.sleep((attempt + 1) * 3); continue
                print(f"  [Error] API Status {res.status_code} ({code})"); return None
            except Exception as e:
                if attempt < max_retries: time.sleep(1); continue
                print(f"  [Error] API Fetch Exception ({code}): {e}"); return None
        return None

    def fetch_and_parse_15m_bars(self, code, token):
        n_code = normalize_code(code); query = "SELECT datetime, open, high, low, close, volume FROM bars_1m WHERE code = ? ORDER BY datetime DESC LIMIT 5000"
        with self._get_connection() as conn: df_db = pd.read_sql_query(query, conn, params=[n_code])
        df_api = pd.DataFrame()
        if token:
            time.sleep(0.5) # Lite spread
            raw_api = self.fetch_kiwoom_minute_data(code, token)
            if raw_api:
                df_api = pd.DataFrame(raw_api); f_cl='cur_prc'; f_op='open_pric'; f_hg='high_pric'; f_lw='low_pric'; f_tm='cntr_tm'; f_vl='trde_qty'
                for c in [f_cl, f_op, f_hg, f_lw, f_vl]: df_api[c] = pd.to_numeric(df_api[c].astype(str).str.replace('+', '').str.replace('-', ''), errors='coerce').fillna(0)
                df_api['datetime'] = pd.to_datetime(df_api[f_tm], format='%Y%m%d%H%M%S', errors='coerce').dt.strftime('%Y-%m-%d %H:%M:%S')
                df_api.rename(columns={f_op: 'open', f_hg: 'high', f_lw: 'low', f_cl: 'close', f_vl: 'volume'}, inplace=True)
                df_api = df_api[['datetime', 'open', 'high', 'low', 'close', 'volume']]
        df_combined = pd.concat([df_db, df_api]).drop_duplicates('datetime').sort_values('datetime')
        if df_combined.empty: return pd.DataFrame()
        df_final = df_combined.tail(5000).copy(); df_final['datetime'] = pd.to_datetime(df_final['datetime']); df_final.set_index('datetime', inplace=True)
        resampled = df_final.resample('15min', closed='right', label='right').agg({'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}).dropna()
        resampled['time'] = resampled.index.strftime('%H%M'); resampled.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'}, inplace=True)
        return resampled.tail(300)

    def _fetch_and_build_profile(self, code):
        """[v4.5.9] 429 방지용 순차 프로필 빌더"""
        from kiwoom_rest_api.auth.token import TokenManager
        token = TokenManager().get_token()
        if not token: return
        raw = self.fetch_kiwoom_minute_data(code, token)
        if raw:
            prof, min_prof = self.build_volume_profile(raw)
            return prof, min_prof
        return {}, {}

    def build_volume_profile(self, minute_data):
        if not minute_data: return {}, {}
        try:
            df = pd.DataFrame(minute_data); f_vl='trde_qty' if 'trde_qty' in df.columns else ('cntg_vol' if 'cntg_vol' in df.columns else 'volume')
            f_tm='cntr_tm' if 'cntr_tm' in df.columns else ('data_dt' if 'data_dt' in df.columns else 'time')
            df['vol'] = pd.to_numeric(df[f_vl].astype(str).str.replace('+', '').str.replace('-', ''), errors='coerce').fillna(0); df['dt_str'] = df[f_tm].astype(str)
            if len(df['dt_str'].iloc[0]) >= 12: df['day']=df['dt_str'].str[:8]; df['time_slot']=df['dt_str'].str[8:12]
            else: df['day']=datetime.now().strftime('%Y%m%d'); df['time_slot']=df['dt_str'].str[:4]
            df = df.sort_values(by=['day', 'time_slot']); df['cum_vol'] = df.groupby('day')['vol'].cumsum()
            return df.groupby('time_slot')['cum_vol'].mean().to_dict(), df.groupby('time_slot')['vol'].mean().to_dict()
        except: return {}, {}

    def parse_to_bars(self, minute_data, codes):
        """[v4.5.9] 429 에러 방지용 리샘플링 통합 모듈"""
        if not minute_data: return pd.DataFrame()
        try:
            df = pd.DataFrame(minute_data); f_cl='cur_prc'; f_op='open_pric'; f_hg='high_pric'; f_lw='low_pric'; f_tm='cntr_tm'; f_vl='trde_qty'
            for c in [f_cl, f_op, f_hg, f_lw, f_vl]: df[c] = pd.to_numeric(df[c].astype(str).str.replace('+', '').str.replace('-', ''), errors='coerce').fillna(0)
            df['time_str'] = df[f_tm].astype(str); df['datetime'] = pd.to_datetime(df['time_str'], format='%Y%m%d%H%M%S', errors='coerce').fillna(pd.to_datetime(datetime.now().strftime('%Y%m%d')+df['time_str'], format='%Y%m%d%H%M%S', errors='coerce'))
            df.set_index('datetime', inplace=True); df = df.sort_index(); resampled = df.resample('15min', closed='right', label='right').agg({f_op: 'first', f_hg: 'max', f_lw: 'min', f_cl: 'last', f_vl: 'sum'}).dropna()
            
            # [Warmup-Loop] 순차 처리 및 429 에러 복원 로직
            total = len(codes)
            with ThreadPoolExecutor(max_workers=1) as executor:
                for i, code in enumerate(codes):
                    try:
                        print(f"  [Warmup-Bars] {code} 분석 중... ({i+1}/{total})")
                        self._fetch_and_build_profile(code)
                        import random
                        time.sleep(random.uniform(2.0, 4.0)) # Stable spread
                    except Exception as e:
                        print(f"  [Status] {code} Warmup Skipping: {e}")
                        if "429" in str(e): time.sleep(60)
            resampled.columns = ['Open', 'High', 'Low', 'Close', 'Volume']; resampled['time'] = resampled.index.strftime('%H%M')
            return resampled.tail(100)
        except: return pd.DataFrame()

    def fetch_nxt_status(self, code, token):
        if not token: return False
        try:
            res = requests.post(f"{config.URL}/api/dostk/stkinfo", headers={'Content-Type': 'application/json;charset=UTF-8', 'authorization': f'Bearer {token}', 'api-id': 'ka10100'}, json={'stk_cd': code}, timeout=3)
            if res.status_code == 200: return res.json().get('nxtEnable') == 'Y'
        except: pass
        return False

    def prepare_volume_profiles(self, monitor_codes, token, bars_data, volume_profiles, min_volume_profiles):
        """[v4.5.91] DB-First Smart Warmup (API 부하 최소화 및 속도 향상)"""
        if not token: return
        total = len(monitor_codes)
        
        for i, code in enumerate(monitor_codes):
            try:
                # 1. DB/캐시 데이터 최신성 확인
                last_dt_str = self.get_last_sync_time(code)
                is_fresh = False
                if last_dt_str:
                    last_dt = datetime.strptime(last_dt_str, "%Y-%m-%d %H:%M:%S")
                    # 마지막 데이터가 20분 이내라면 최신으로 간주
                    if (datetime.now() - last_dt).total_seconds() < 1200:
                        is_fresh = True

                # 2. 로컬 데이터가 충분하면 API 건너뛰기
                if is_fresh and code in bars_data and not bars_data[code].empty:
                    print(f"  [Warmup-Local] {code} DB 데이터 활용 중... ({i+1}/{total})")
                    df = bars_data[code]
                    # DataFrame을 소스 리스트 형태로 변환하여 프로필 빌드
                    raw = df.reset_index().to_dict('records')
                    prof, min_p = self.build_volume_profile(raw)
                    volume_profiles[code] = prof
                    min_volume_profiles[code] = min_p
                    continue

                # 3. 데이터가 없거나 오래된 경우만 API 호출
                print(f"  [Warmup-API] {code} 최신 데이터 수집 중... ({i+1}/{total})")
                raw = self.fetch_kiwoom_minute_data(code, token)
                if raw:
                    prof, min_p = self.build_volume_profile(raw)
                    volume_profiles[code] = prof
                    min_volume_profiles[code] = min_p
                
                # API 호출 시에만 안전 지연 시간 적용
                import random
                time.sleep(random.uniform(2.0, 4.0))
                
            except Exception as e:
                print(f"[System] {code} Profile Skipping: {e}")
                if "429" in str(e): time.sleep(60)

    def save_bars_cache(self, bars_data, cache_file):
        try:
            cache = {c: df.to_dict(orient='records') for c, df in bars_data.items() if not df.empty}
            with open(cache_file, "w", encoding="utf-8") as f: json.dump(cache, f, ensure_ascii=False, indent=2)
        except: pass

    def load_bars_cache(self, cache_file):
        if not os.path.exists(cache_file): return {}
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f)
                return {c: pd.DataFrame(recs) for c, recs in cache.items()}
        except: return {}

    def send_telegram_message(self, message):
        token = getattr(config, "TELEGRAM_BOT_TOKEN", ""); cid = getattr(config, "TELEGRAM_CHAT_ID", "")
        if token and cid and "발급" not in token:
            try: requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": cid, "text": message, "parse_mode": "HTML"}, timeout=5)
            except: pass

    @staticmethod
    @staticmethod
    def judge_market_trend(msi, vix_val, adx, name="KOSPI"):
        """[v4.5.10] Unified Market Trend Logic (DNA Standard)"""
        # 0. Global Kill-Switch (VIX Panic)
        if vix_val > 30: return "DOWN", f"시장 공포 확산 (VIX:{vix_val:.1f})"
        
        # 1. Sideways / Range-bound (ADX Low)
        if adx < 15: return "NEUTRAL", f"횡보/박스권 장세 (ADX:{adx:.1f})"
        
        # 2. MSI Based Trend (Momentum)
        if msi < -0.10: 
            return "DOWN", f"지수 모멘텀 붕괴 (MSI:{msi:.2f})"
        elif msi > 0.05:
            # Additional safety for UP: VIX should be relatively stable
            if vix_val < 30: return "UP", f"추세 상승장 (MSI:{msi:.2f})"
            else: return "NEUTRAL", "상승 중 변동성 주의"
        
        return "NEUTRAL", f"추세 탐색 중 (MSI:{msi:.2f})"

    def get_market_indices(self):
        import FinanceDataReader as fdr; from strategy import indicators
        indices = {"KS11": "KOSPI", "KQ11": "KOSDAQ"}
        results = {}
        vix_val = 0.0
        try:
            vix_df = fdr.DataReader('VIX', (datetime.now() - timedelta(days=10)).strftime('%Y-%m-%d'))
            if not vix_df.empty: vix_val = float(vix_df.iloc[-1]['Close'])
        except: pass
        for ticker, name in indices.items():
            try:
                df = fdr.DataReader(ticker, (datetime.now() - timedelta(days=300)).strftime('%Y-%m-%d'))
                if df is not None and len(df) >= 120:
                    df.columns = [c.capitalize() if c.lower() == 'close' else c for c in df.columns]
                    df = indicators.calculate_all_indicators(df, {})
                    last = df.iloc[-1]; msi = float(last.get('MSI', 0)); adx = float(last.get('ADX', 0))
                    trend, reason = self.judge_market_trend(msi, vix_val, adx, name)
                    results[name] = {"val": float(last['Close']), "ratio": float((last['Close']/df.iloc[-2]['Close']-1)*100), "trend": trend, "reason": reason, "msi": msi, "adx": adx, "vix": vix_val}
            except: continue
        return results

    def get_realtime_indices(self):
        try:
            resp = requests.get("https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI,KOSDAQ", timeout=5)
            if resp.status_code == 200:
                return {item['itemCode']: {'val': float(item['closePriceRaw']), 'ratio': float(item['fluctuationsRatioRaw'])} for item in resp.json().get('datas', [])}
        except: return {}
        return {}

    def get_active_positions(self):
        with self._get_connection() as conn: cursor = conn.execute("SELECT code, name, avg_price, qty, stage, invested, entry_time, reason FROM active_positions WHERE status='OPEN'"); return cursor.fetchall()

    def get_market_trend_label(self):
        with self._get_connection() as conn:
            row = conn.execute("SELECT value FROM system_config WHERE key='market_trend'").fetchone()
            return row[0] if row else "NEUTRAL"

    def set_market_trend_label(self, label):
        with self._get_connection() as conn: conn.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES ('market_trend', ?)", (label,)); conn.commit()

    def get_kospi_list(self, count=200, force=False):
        """
        [v17.2] KODEX 200 유니버스 확보 (CSV 우선 -> Kiwoom API -> pykrx)
        """
        import FinanceDataReader as fdr
        from pykrx import stock
        import pandas as pd
        import os
        import time

        cache_file = "kodex_200.csv" if count <= 200 else f"kospi_top_{count}.csv"
        
        try:
            # 1. kodex200.csv 파일 확인 (KODEX 200 전용 최우선 소스)
            if count <= 200:
                csv_path = os.path.join(os.getcwd(), 'kodex200.csv')
                if os.path.exists(csv_path):
                    try:
                        # 인코딩 문제 대응 (utf-8-sig or cp949)
                        try:
                            df_csv = pd.read_csv(csv_path, dtype={'종목코드': str}, encoding='utf-8-sig')
                        except:
                            df_csv = pd.read_csv(csv_path, dtype={'종목코드': str}, encoding='cp949')
                        
                        if '종목코드' in df_csv.columns:
                            # 컬럼명 통일 (Code, Name)
                            df_res = df_csv[['종목코드', '종목명']].copy()
                            df_res.columns = ['Code', 'Name']
                            if len(df_res) >= 190:
                                print(f"DEBUG: Universe loaded from kodex200.csv ({len(df_res)} stocks)")
                                # 내부 캐시 파일로도 복사하여 일관성 유지
                                df_res.to_csv(cache_file, index=False, encoding='utf-8-sig')
                                return df_res
                    except Exception as e:
                        print(f"DEBUG ERROR [DataManagerV2]: Failed to read kodex200.csv: {e}")

            # 2. 데이터 확보 (KODEX 200 전용 Fallback)
            df = None
            if count <= 200:
                # [v17.3 Fallback] KODEX 200 ETF(069500)의 PDF 구성 종목 직접 조회
                print("  [Info] CSV 파일이 없어 pykrx를 통한 KODEX 200 ETF(069500) 구성을 요청합니다.")
                try:
                    tickers_df = stock.get_etf_portfolio_deposit_file("069500")
                    if tickers_df is not None and not tickers_df.empty:
                        tickers = tickers_df.index.tolist()
                        tickers = [t for t in tickers if len(t) == 6 and t.isdigit()]
                        names = [stock.get_market_ticker_name(t) for t in tickers]
                        df = pd.DataFrame({'Code': tickers, 'Name': names})
                except: pass
            
            if df is None or df.empty:
                # 최종 Fallback: FDR 시총순 추출 (KOSPI 상위)
                print("  [Info] 최종 Fallback으로 FDR 시총 상위 종목을 추출합니다.")
                df = fdr.StockListing('KOSPI')
                df = df[df['Code'].str.endswith('0')].sort_values(by='Marcap', ascending=False).head(count)
                df = df[['Code', 'Name']]
            
            # 3. CSV 저장 (최종 고정 리스트)
            if df is not None and not df.empty:
                df.to_csv(cache_file, index=False, encoding='utf-8-sig')
            return df
        except Exception as e:
            print(f"  [Error] 종목 리스트 확보 중 오류: {e}")
            if os.path.exists(cache_file): return pd.read_csv(cache_file, dtype={'Code': str})
            return pd.DataFrame()

    def get_remote_command(self):
        """[v4.6.9] 원격 명령 테이블에서 최신 명령어를 하나 가져오고 삭제합니다 (Pop-and-Delete)."""
        import sqlite3
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            cursor = conn.cursor()
            
            # 1. 테이블 존재 여부 확인 (Defensive)
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='remote_commands'")
            if not cursor.fetchone():
                return None
            
            # 2. 가장 오래된 명령(먼저 들어온 것) 하나 가져오기
            cursor.execute("SELECT id, command FROM remote_commands ORDER BY id ASC LIMIT 1")
            row = cursor.fetchone()
            
            if row:
                cmd_id, command = row
                # 3. 가져온 명령은 즉시 삭제 (중복 실행 방지)
                cursor.execute("DELETE FROM remote_commands WHERE id = ?", (cmd_id,))
                conn.commit()
                return command
            
            return None
        except Exception as e:
            # Polling 중이므로 조용히 로깅만 수행 (User 로깅은 지양)
            if "database is locked" not in str(e).lower():
                print(f"[DataManager] Remote Polling Error: {e}")
            return None
        finally:
            if conn: conn.close()
