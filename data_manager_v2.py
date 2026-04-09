import sqlite3
import pandas as pd
import os
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional

from utils.normalization import normalize_code
from utils.bar_resample import refresh_resampled_data_at_path
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
    # [v4.12] 주문·체결·활성포지션 등 PC 간 백업·Git 동기화용 소형 DB (캔들·sync_meta 는 trading_data.db)
    DEFAULT_STATE_DB_BASENAME = "trading_state.db"

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(DataManagerV2, cls).__new__(cls)
            return cls._instance

    def __init__(self, db_path=None, state_db_path=None):
        if DataManagerV2._initialized:
            return
        mp = db_path if db_path else self.DEFAULT_DB_PATH
        self.db_path = os.path.abspath(mp)
        if state_db_path:
            self.state_db_path = os.path.abspath(state_db_path)
        else:
            self.state_db_path = os.path.join(
                os.path.dirname(self.db_path), self.DEFAULT_STATE_DB_BASENAME
            )
        print(f"DEBUG [DataManagerV2]: Main DB at {self.db_path}")
        print(f"DEBUG [DataManagerV2]: State DB at {self.state_db_path}")
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

    def _get_state_connection(self):
        """주문·포지션·원격명령 등 소형 상태 DB 연결 (백업·Git 동기화 대상)."""
        try:
            conn = sqlite3.connect(self.state_db_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL;")
            return conn
        except sqlite3.DatabaseError as e:
            if "malformed" in str(e).lower():
                print(
                    f"CRITICAL [DataManagerV2]: State DB malformed at {self.state_db_path}: {e}"
                )
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

    def _init_db(self) -> None:
        """메인(캔들·하베스팅) DB + 상태(주문·포지션) DB 초기화 및 구버전 단일 DB 이전."""
        self._init_main_data_tables()
        self._init_state_tables()
        self._migrate_legacy_state_tables_from_main_db()

    def _init_main_data_tables(self) -> None:
        """대용량 trading_data.db: 봉·sync_meta·실시간스냅샷·상장유니버스 (하베스팅 전용)."""
        with self._get_connection() as conn:
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
            # [v4.12] 예전 실시간 스냅샷 테이블 — 적재 경로 없음, 백테는 합성 지표로 처리. 잔여 파일 정리.
            try:
                conn.execute("DROP INDEX IF EXISTS idx_realtime_snap_code_dt")
                conn.execute("DROP TABLE IF EXISTS realtime_snapshots")
            except sqlite3.Error:
                pass
            try:
                conn.execute("ALTER TABLE sync_meta ADD COLUMN last_daily_sync TEXT")
            except sqlite3.OperationalError:
                pass
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

    def _init_state_tables(self) -> None:
        """소형 trading_state.db: 주문·활성포지션·원격명령·헤더 시장추세 (Git/백업 동기화 권장)."""
        with self._get_state_connection() as conn:
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS remote_commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    command TEXT,
                    timestamp TEXT,
                    status TEXT DEFAULT 'PENDING'
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS system_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            try:
                conn.execute("ALTER TABLE order_history ADD COLUMN filled_price INTEGER DEFAULT 0")
                conn.execute("ALTER TABLE order_history ADD COLUMN filled_qty INTEGER DEFAULT 0")
                conn.execute("ALTER TABLE order_history ADD COLUMN name TEXT DEFAULT ''")
                conn.execute("ALTER TABLE order_history ADD COLUMN reason TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            for _oh_mig in (
                "ALTER TABLE order_history ADD COLUMN snap_vol_power REAL",
                "ALTER TABLE order_history ADD COLUMN snap_bid_ask TEXT DEFAULT ''",
                "ALTER TABLE order_history ADD COLUMN fill_snap_vol_power REAL",
                "ALTER TABLE order_history ADD COLUMN fill_snap_bid_ask TEXT DEFAULT ''",
            ):
                try:
                    conn.execute(_oh_mig)
                except sqlite3.OperationalError:
                    pass
            conn.commit()

    def _migrate_legacy_state_tables_from_main_db(self) -> None:
        """
        예전 단일 trading_data.db 에 있던 주문·포지션 테이블을 trading_state.db 로 한 번만 이전하고
        메인 DB 에서는 제거합니다. 다른 PC 의 trading_state.db 를 복사해 온 경우에도 메인 쪽 유령 테이블을 정리합니다.
        """
        state_tables = (
            "order_history",
            "active_positions",
            "remote_commands",
            "system_config",
        )
        try:
            if not os.path.exists(self.db_path):
                return

            with self._get_connection() as main:
                main_has_leg = bool(
                    main.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_history'"
                    ).fetchone()
                )

            with self._get_state_connection() as st:
                n_state_orders = st.execute("SELECT COUNT(*) FROM order_history").fetchone()[0]

            # state 에 이미 이력이 있고 메인에 구 테이블만 남은 경우 → 복사 없이 메인에서만 DROP
            if main_has_leg and n_state_orders > 0:
                with self._get_connection() as main:
                    for tbl in state_tables:
                        try:
                            main.execute(f"DROP TABLE IF EXISTS {tbl}")
                        except sqlite3.Error:
                            pass
                    main.commit()
                return

            if n_state_orders > 0:
                return

            if not main_has_leg:
                return

            with self._get_state_connection() as st:
                st.execute("ATTACH DATABASE ? AS leg", (self.db_path,))
                try:
                    for tbl in state_tables:
                        try:
                            exists = st.execute(
                                "SELECT 1 FROM leg.sqlite_master WHERE type='table' AND name=?",
                                (tbl,),
                            ).fetchone()
                            if not exists:
                                continue
                            st.execute(f"INSERT INTO {tbl} SELECT * FROM leg.{tbl}")
                        except sqlite3.Error as ex:
                            try:
                                print(
                                    f"[DataManagerV2] 상태 테이블 이전 건너뜀 ({tbl}): {ex}"
                                )
                            except Exception:
                                pass
                    st.commit()
                finally:
                    try:
                        st.execute("DETACH DATABASE leg")
                    except sqlite3.Error:
                        pass
            with self._get_connection() as main:
                for tbl in state_tables:
                    try:
                        main.execute(f"DROP TABLE IF EXISTS {tbl}")
                    except sqlite3.Error:
                        pass
                main.commit()
            try:
                print(
                    "[DataManagerV2] 구버전 주문·포지션 데이터를 trading_state.db 로 이전했습니다."
                )
            except Exception:
                pass
        except Exception as e:
            try:
                print(f"[DataManagerV2] 상태 DB 이전 경고(무시 가능): {e}")
            except Exception:
                pass

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
        """bars_1m 전체를 기준으로 해당 종목의 bars_15m을 재생성합니다."""
        err = refresh_resampled_data_at_path(self.db_path, code)
        if err:
            try:
                print(f"[DataManagerV2] refresh_resampled_data {code}: {err}")
            except Exception:
                pass


    def save_active_position(self, code, name, avg_price, qty, stage, invested, entry_time, reason):
        with self._lock, self._get_state_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO active_positions (code, name, avg_price, qty, stage, invested, entry_time, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (code, name, avg_price, qty, stage, invested, entry_time, reason))
            conn.commit()

    def delete_active_position(self, code):
        with self._lock, self._get_state_connection() as conn:
            conn.execute("DELETE FROM active_positions WHERE code = ?", (code,))
            conn.commit()

    def log_order(
        self,
        code,
        side,
        qty,
        price,
        order_type,
        order_no,
        result,
        name="",
        reason="",
        snap_vol_power=None,
        snap_bid_ask=None,
    ):
        """주문 접수 DB 기록. snap_* 는 감시 화면과 동일한 체결강도·호가 문자열 스냅샷."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, self._get_state_connection() as conn:
            conn.execute(
                """
                INSERT INTO order_history (
                    code, side, qty, price, order_type, order_no, timestamp, result, name, reason,
                    snap_vol_power, snap_bid_ask
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    side,
                    qty,
                    price,
                    order_type,
                    order_no,
                    now,
                    result,
                    name,
                    reason,
                    snap_vol_power,
                    (snap_bid_ask if snap_bid_ask is not None else ""),
                ),
            )
            conn.commit()

    def get_today_orders(self):
        today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
        with self._get_state_connection() as conn:
            cursor = conn.execute("""
                SELECT timestamp, code, side, qty, price, filled_price, filled_qty, result, name, reason,
                       snap_vol_power, snap_bid_ask, fill_snap_vol_power, fill_snap_bid_ask
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
            with self._get_state_connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT timestamp, code, side, qty, price, filled_price, filled_qty, result, name, reason, order_no, order_type,
                           snap_vol_power, snap_bid_ask, fill_snap_vol_power, fill_snap_bid_ask
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
        with self._get_state_connection() as conn:
            cursor = conn.execute("""
                SELECT name, reason 
                FROM order_history 
                WHERE code = ? AND (result = 'SUCCESS' OR result = 'MOCK')
                ORDER BY timestamp DESC LIMIT 1
            """, (code,))
            res = cursor.fetchone()
            return res if res else (None, None)

    def update_order_status(
        self,
        code,
        order_no,
        new_status,
        filled_price=0,
        filled_qty=0,
        fill_snap_vol_power=None,
        fill_snap_bid_ask=None,
    ):
        """체결 반영 시 fill_snap_* 로 체결 시점 수급 스냅샷을 함께 저장할 수 있습니다."""
        sets = ["result = ?", "filled_price = ?", "filled_qty = ?"]
        params: List[Any] = [new_status, filled_price, filled_qty]
        if fill_snap_vol_power is not None:
            sets.append("fill_snap_vol_power = ?")
            params.append(fill_snap_vol_power)
        if fill_snap_bid_ask is not None:
            sets.append("fill_snap_bid_ask = ?")
            params.append(fill_snap_bid_ask)
        params.extend(
            [
                code,
                order_no,
                datetime.now().strftime("%Y-%m-%d 00:00:00"),
            ]
        )
        sql = f"""
                UPDATE order_history 
                SET {", ".join(sets)}
                WHERE code = ? AND (order_no = ? OR order_no = 'MOCK_ID')
                AND timestamp >= ?
            """
        with self._lock, self._get_state_connection() as conn:
            conn.execute(sql, tuple(params))
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

    def reconcile_sync_meta_from_bars_1m(self, code: Optional[str] = None) -> bool:
        """
        sync_meta 의 last_sync_dt / earliest_sync_dt 를 bars_1m 실제 MIN·MAX 와 일치시킵니다.
        대신 DB 병합 등으로 bars_1m 만 갱신된 경우 메타가 옛날로 남는 문제를 보정합니다.
        status 등 다른 컬럼은 유지합니다.
        """
        try:
            with self._get_connection() as conn:
                if code:
                    ncode = normalize_code(code)
                    row = conn.execute(
                        "SELECT MIN(datetime), MAX(datetime) FROM bars_1m WHERE code = ?",
                        (ncode,),
                    ).fetchone()
                    if not row or row[0] is None:
                        return True
                    conn.execute(
                        """
                        INSERT INTO sync_meta (code, last_sync_dt, earliest_sync_dt)
                        VALUES (?, ?, ?)
                        ON CONFLICT(code) DO UPDATE SET
                            last_sync_dt = excluded.last_sync_dt,
                            earliest_sync_dt = excluded.earliest_sync_dt
                        """,
                        (ncode, row[1], row[0]),
                    )
                else:
                    conn.execute(
                        """
                        INSERT INTO sync_meta (code, last_sync_dt, earliest_sync_dt)
                        SELECT code, MAX(datetime), MIN(datetime)
                        FROM bars_1m
                        GROUP BY code
                        ON CONFLICT(code) DO UPDATE SET
                            last_sync_dt = excluded.last_sync_dt,
                            earliest_sync_dt = excluded.earliest_sync_dt
                        """
                    )
                conn.commit()
            return True
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] reconcile_sync_meta_from_bars_1m 실패: {e}")
            except Exception:
                pass
            return False

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

    def get_latest_daily_bar_date_in_db(self) -> Optional[date]:
        """
        bars_1d에 저장된 일봉 중 가장 최근 영업일.
        코스피 지수(001) 우선, 없으면 전체 MAX(datetime).
        """
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT MAX(datetime) FROM bars_1d WHERE code = ?",
                    ("001",),
                ).fetchone()
                s = row[0] if row and row[0] else None
                if not s:
                    row2 = conn.execute("SELECT MAX(datetime) FROM bars_1d").fetchone()
                    s = row2[0] if row2 and row2[0] else None
            if not s:
                return None
            return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
        except Exception:
            return None

    def check_daily_bars_fresh_for_selection(self, now: Optional[datetime] = None) -> tuple[bool, str]:
        """
        종목 선정 전: 로컬 일봉이 '최신 장마감' 기준일까지 반영됐는지 검사.
        (True, "") 이면 선정 진행 가능.
        """
        try:
            from reporting.krx_trading_calendar import required_eod_bar_date_asof_local

            req = required_eod_bar_date_asof_local(now)
            latest = self.get_latest_daily_bar_date_in_db()
            if latest is None:
                return (
                    False,
                    "일봉(bars_1d) 데이터가 없습니다. 하베스팅으로 DB를 채운 뒤 다시 시도하세요.",
                )
            if latest < req:
                return (
                    False,
                    f"DB 최신 일봉: {latest} / 필요(최신 장마감 반영): {req} 이상.",
                )
            return (True, "")
        except Exception as e:
            return (
                False,
                f"일봉 최신일 확인 중 오류: {e}",
            )

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

    def send_telegram_message(self, message: str, parse_mode: Optional[str] = "HTML") -> bool:
        """
        텔레그램 Bot API로 메시지 전송.
        parse_mode 가 None 이면 HTML 파싱 없음(종목명 특수문자·긴 목록에 안전).
        Returns
        -------
        bool
            전송 성공 True, 스킵/실패 False
        """
        token = getattr(config, "TELEGRAM_BOT_TOKEN", "") or ""
        cid = getattr(config, "TELEGRAM_CHAT_ID", "") or ""
        if not token or not cid or "발급" in token:
            try:
                print("[Telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 를 config.py 에 설정하세요.")
            except Exception:
                pass
            return False
        try:
            payload: Dict[str, Any] = {"chat_id": cid, "text": message}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            res = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json=payload,
                timeout=10,
            )
            if res.status_code != 200:
                try:
                    print(f"[Telegram Error] HTTP {res.status_code}: {res.text[:500]}")
                except Exception:
                    pass
                return False
            return True
        except Exception as e:
            try:
                print(f"[Telegram Exception] {e}")
            except Exception:
                pass
            return False

    def load_settings_dict_for_market(self) -> dict:
        """실시간 지수 레짐에 사용할 settings.json (없으면 빈 dict → 모듈 기본 임계값)."""
        path = os.path.join(self.ABS_DIR, "settings.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def get_market_indices(self, settings=None):
        """
        KOSPI/KOSDAQ 일봉 기반 MSI·ADX·VIX로 4단계 레짐 산출 (백테·다이나믹과 동일 분기).
        settings: dict 미전달 시 프로젝트 루트 settings.json 로드 시도.
        """
        import FinanceDataReader as fdr
        from strategy import indicators
        from strategy.market_regime import classify_market_regime_row, regime_reason_korean

        eff_settings = settings if isinstance(settings, dict) else self.load_settings_dict_for_market()
        indices = {"KS11": "KOSPI", "KQ11": "KOSDAQ"}
        results: dict = {}
        # VIX: 당일(마지막 행)이 NaN이면 전일 등 직전 유효 종가 사용 — 동적 백테 VIX ffill과 동일 의도
        vix_val = 15.0
        try:
            vix_df = fdr.DataReader("VIX", (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d"))
            if not vix_df.empty and "Close" in vix_df.columns:
                last_valid = None
                for x in pd.to_numeric(vix_df["Close"], errors="coerce").values:
                    try:
                        fx = float(x)
                        if fx == fx and abs(fx) != float("inf"):
                            last_valid = fx
                    except (TypeError, ValueError):
                        continue
                if last_valid is not None:
                    vix_val = float(last_valid)
        except Exception:
            pass
        for ticker, name in indices.items():
            try:
                df = fdr.DataReader(ticker, (datetime.now() - timedelta(days=300)).strftime("%Y-%m-%d"))
                if df is not None and len(df) >= 120:
                    df.columns = [c.capitalize() if c.lower() == "close" else c for c in df.columns]
                    df = indicators.calculate_all_indicators(df, eff_settings)
                    last = df.iloc[-1]
                    msi = float(last.get("MSI", 0))
                    adx = float(last.get("ADX", 0))
                    regime = classify_market_regime_row(msi, adx, vix_val, eff_settings)
                    reason = regime_reason_korean(regime, msi, adx, vix_val, name)
                    results[name] = {
                        "val": float(last["Close"]),
                        "ratio": float((last["Close"] / df.iloc[-2]["Close"] - 1) * 100),
                        "trend": regime,
                        "regime": regime,
                        "reason": reason,
                        "msi": msi,
                        "adx": adx,
                        "vix": vix_val,
                    }
            except Exception:
                continue
        return results

    def get_realtime_indices(self):
        try:
            resp = requests.get("https://polling.finance.naver.com/api/realtime/domestic/index/KOSPI,KOSDAQ", timeout=5)
            if resp.status_code == 200:
                return {item['itemCode']: {'val': float(item['closePriceRaw']), 'ratio': float(item['fluctuationsRatioRaw'])} for item in resp.json().get('datas', [])}
        except: return {}
        return {}

    def get_active_positions(self):
        with self._get_state_connection() as conn:
            cursor = conn.execute(
                "SELECT code, name, avg_price, qty, stage, invested, entry_time, reason FROM active_positions WHERE status='OPEN'"
            )
            return cursor.fetchall()

    def get_market_trend_label(self):
        with self._get_state_connection() as conn:
            row = conn.execute("SELECT value FROM system_config WHERE key='market_trend'").fetchone()
            return row[0] if row else "NEUTRAL"

    def set_market_trend_label(self, label):
        with self._lock, self._get_state_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO system_config (key, value) VALUES ('market_trend', ?)",
                (label,),
            )
            conn.commit()

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
            conn = sqlite3.connect(self.state_db_path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL;")
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
