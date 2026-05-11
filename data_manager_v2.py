import sqlite3
import pandas as pd
import os
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional, Tuple

from utils.normalization import normalize_code
from utils.telegram_sender import send_telegram_message

# 시장지수·VIX 등 — 하베스팅/메인 DB에서 제외·정리 대상 코드 (bars_* / sync_meta)
MARKET_INDEX_BAR_CODES: Tuple[str, ...] = (
    "001",
    "101",
    "^VIX",
    "KS11",
    "KQ11",
    "^KS11",
    "^KQ11",
)

# 영업일 캘린더용 기준 종목(지수 대신). 하베스팅 유니버스에 자주 포함되는 대형주 순.
_CALENDAR_REFERENCE_STOCK_CODES: Tuple[str, ...] = (
    "005930",
    "000660",
    "005380",
    "035420",
    "051910",
)
from utils.bar_resample import (
    refresh_resampled_data_at_path,
    aggregate_daily_ohlcv_from_1m_db,
    refresh_resampled_data_at_path_range,
    aggregate_daily_ohlcv_from_1m_db_range,
)
from utils.server_mode_paths import (
    migrate_legacy_trading_state_db_files,
    state_db_path,
    state_db_path_for_live,
    state_db_path_for_mock,
)
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
    # trading_data.db: 백테스트·동적백테·최적화용 캔들/메타 저장(실시간 대시보드 종목 선정은 API 일봉 권장)
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
        # 구 trading_state.db → trading_state_mock.db 1회 이관 후, 모의/실전은 별도 파일
        try:
            migrate_legacy_trading_state_db_files(self.db_path)
        except Exception:
            pass
        if state_db_path:
            self.state_db_path = os.path.abspath(state_db_path)
        else:
            # 호환: 예전 단일 경로를 쓰던 코드용(실제 연결은 state_db_path() 동적)
            self.state_db_path = os.path.join(
                os.path.dirname(self.db_path), self.DEFAULT_STATE_DB_BASENAME
            )
        print(f"DEBUG [DataManagerV2]: Main DB at {self.db_path}")
        try:
            print(
                f"DEBUG [DataManagerV2]: State DB (현재 모드) at {state_db_path()}"
            )
        except Exception:
            print(f"DEBUG [DataManagerV2]: State DB (레거시) at {self.state_db_path}")
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
        """주문·포지션·원격명령 — config.USE_MOCK 에 따라 mock/live DB 분리."""
        try:
            p = state_db_path()
        except Exception:
            p = self.state_db_path
        return self._get_state_connection_for_path(p)

    def _get_state_connection_for_path(self, abs_path: str):
        """지정 경로의 상태 DB 연결 (스키마 부트스트랩·구버전 이전용)."""
        try:
            conn = sqlite3.connect(abs_path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL;")
            return conn
        except sqlite3.DatabaseError as e:
            if "malformed" in str(e).lower():
                print(
                    f"CRITICAL [DataManagerV2]: State DB malformed at {abs_path}: {e}"
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
            # PRIMARY KEY (code, datetime) 가 이미 동일 순서 인덱스 역할.
            # 보조 인덱스 (code, datetime) 는 중복 저장만 유발 → 생성하지 않음.
            # 기존 DB에 남아 있으면 제거(용량·freelist 정리; 파일 축소는 VACUUM).
            for _ix in (
                "idx_bars_1m_code_dt",
                "idx_bars_15m_code_dt",
                "idx_bars_1d_code_dt",
                "idx_bars_1d_code_date",
            ):
                try:
                    conn.execute(f"DROP INDEX IF EXISTS {_ix}")
                except sqlite3.Error:
                    pass
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
        # 지수·VIX 등 레거니 봉/ sync_meta 정리(멱등). 영업일은 get_trading_days → KRX 캘린더.
        try:
            purged = self.purge_market_index_bars_from_main_db()
            if any(purged.values()):
                try:
                    print(f"[DataManagerV2] 시장지수·VIX 레거니 행 정리: {purged}")
                except Exception:
                    pass
        except Exception:
            pass

    def _ensure_state_schema_at_path(self, abs_path: str) -> None:
        """주문·활성포지션 등 상태 테이블 스키마를 지정 파일에 생성."""
        with self._get_state_connection_for_path(abs_path) as conn:
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
                "ALTER TABLE order_history ADD COLUMN order_source TEXT DEFAULT ''",
                "ALTER TABLE order_history ADD COLUMN fill_row_key TEXT DEFAULT ''",
            ):
                try:
                    conn.execute(_oh_mig)
                except sqlite3.OperationalError:
                    pass
            try:
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_order_history_fillkey "
                    "ON order_history(order_source, fill_row_key)"
                )
            except sqlite3.Error:
                pass
            conn.commit()

    def _init_state_tables(self) -> None:
        """모의·실전 state DB 각각에 스키마 생성(파일 분리)."""
        try:
            self._ensure_state_schema_at_path(state_db_path_for_mock())
            self._ensure_state_schema_at_path(state_db_path_for_live())
        except Exception as e:
            try:
                print(f"[DataManagerV2] 상태 DB 스키마 초기화 경고: {e}")
            except Exception:
                pass

    def _migrate_legacy_state_tables_from_main_db(self) -> None:
        """
        예전 단일 trading_data.db 에 있던 주문·포지션 테이블을
        trading_state_mock.db 로 한 번만 이전하고 메인 DB 에서는 제거합니다.
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

            with self._get_state_connection_for_path(
                state_db_path_for_mock()
            ) as st:
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

            with self._get_state_connection_for_path(
                state_db_path_for_mock()
            ) as st:
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
                    "[DataManagerV2] 구버전 주문·포지션 데이터를 trading_state_mock.db 로 이전했습니다."
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
            try:
                self.rebuild_daily_bars_from_1m(code)
            except Exception as e:
                try:
                    import logging
                    logging.warning("update_historical_data: rebuild_daily_bars_from_1m %s: %s", code, e, exc_info=True)
                except Exception:
                    pass
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

    def refresh_resampled_data_range(
        self,
        code: str,
        start_day: str,
        end_day: str,
    ) -> None:
        """
        bars_1m 기준으로 해당 종목의 bars_15m을 날짜 범위(start_day~end_day)에 대해서만 재생성합니다.
        """
        err = refresh_resampled_data_at_path_range(
            self.db_path, code, start_day=start_day, end_day=end_day
        )
        if err:
            try:
                print(f"[DataManagerV2] refresh_resampled_data_range {code}: {err}")
            except Exception:
                pass

    def rebuild_daily_bars_from_1m(self, code: str) -> bool:
        """
        bars_1m 전 구간을 일(캘린더) 단위로 집계해 ``bars_1d``에 저장.
        prepare_dynamic_universe / 선정이 참조하는 일봉 테이블을 분봉 DB와 맞춘다.
        """
        try:
            ncode = normalize_code(code)
        except Exception:
            return False
        try:
            daily = aggregate_daily_ohlcv_from_1m_db(self.db_path, ncode)
        except Exception as e:
            try:
                import logging

                logging.warning(
                    "rebuild_daily_bars_from_1m 조회/집계 실패 %s: %s",
                    ncode,
                    e,
                    exc_info=True,
                )
            except Exception:
                pass
            return False
        if daily is None or daily.empty:
            return False
        try:
            self.save_daily_bars(ncode, daily)
            return True
        except Exception as e:
            try:
                import logging

                logging.warning(
                    "rebuild_daily_bars_from_1m 저장 실패 %s: %s", ncode, e, exc_info=True
                )
            except Exception:
                pass
            return False

    def rebuild_daily_bars_from_1m_range(
        self,
        code: str,
        start_day: str,
        end_day: str,
    ) -> bool:
        """
        bars_1m 중 날짜 범위(start_day~end_day)만 읽어 bars_1d를 부분 갱신합니다.
        """
        try:
            ncode = normalize_code(code)
        except Exception:
            return False

        try:
            daily = aggregate_daily_ohlcv_from_1m_db_range(
                self.db_path,
                ncode,
                start_day=start_day,
                end_day=end_day,
            )
        except Exception:
            return False

        if daily is None or daily.empty:
            return False

        try:
            self.save_daily_bars(ncode, daily)
            return True
        except Exception:
            return False

    def is_daily_bars_behind_1m(self, code: str) -> bool:
        """
        bars_1d의 마지막 일자가 bars_1m 최종 분봉이 속한 영업일보다 오래되면 True.
        (1분봉만 갱신되고 일봉 집계가 누락된 경우 일괄 보강 대상)
        """
        try:
            ncode = normalize_code(code)
        except Exception:
            return True
        try:
            with self._get_connection() as conn:
                row_1m = conn.execute(
                    "SELECT MAX(datetime) FROM bars_1m WHERE code = ?",
                    (ncode,),
                ).fetchone()
                row_1d = conn.execute(
                    "SELECT MAX(datetime) FROM bars_1d WHERE code = ?",
                    (ncode,),
                ).fetchone()
        except sqlite3.Error as e:
            try:
                import logging

                logging.warning(
                    "is_daily_bars_behind_1m 조회 실패 %s: %s", ncode, e, exc_info=True
                )
            except Exception:
                pass
            return True
        except Exception as e:
            try:
                import logging

                logging.warning(
                    "is_daily_bars_behind_1m 오류 %s: %s", ncode, e, exc_info=True
                )
            except Exception:
                pass
            return True

        max_1m = row_1m[0] if row_1m else None
        max_1d = row_1d[0] if row_1d else None
        if not max_1m:
            return False
        try:
            d_1m = datetime.strptime(str(max_1m).strip()[:10], "%Y-%m-%d").date()
        except Exception:
            return True
        if not max_1d:
            return True
        try:
            d_1d = datetime.strptime(str(max_1d).strip()[:10], "%Y-%m-%d").date()
        except Exception:
            return True
        return d_1d < d_1m

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

    def _ensure_order_history_exchange_columns_current(self) -> None:
        """기존 state DB 에 order_source / fill_row_key 컬럼이 없을 때 보강."""
        try:
            with self._get_state_connection() as conn:
                for stmt in (
                    "ALTER TABLE order_history ADD COLUMN order_source TEXT DEFAULT ''",
                    "ALTER TABLE order_history ADD COLUMN fill_row_key TEXT DEFAULT ''",
                ):
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError:
                        pass
                try:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_order_history_fillkey "
                        "ON order_history(order_source, fill_row_key)"
                    )
                except sqlite3.Error:
                    pass
                conn.commit()
        except Exception:
            pass

    def merge_exchange_fill_order_rows(self, rows: List[Dict[str, Any]]) -> int:
        """
        키움 kt00007 등에서 가져온 체결 행을 order_history 에 삽입합니다.
        (order_source, fill_row_key) 동일 건은 스킵 — 실전/모의 state DB 각각에 반영됩니다.
        """
        if not rows:
            return 0
        self._ensure_order_history_exchange_columns_current()
        added = 0
        try:
            with self._lock, self._get_state_connection() as conn:
                for r in rows:
                    try:
                        fk = str((r or {}).get("fill_row_key") or "").strip()
                        if not fk:
                            continue
                        dup = conn.execute(
                            "SELECT 1 FROM order_history WHERE order_source = ? AND fill_row_key = ? LIMIT 1",
                            ("KT00007", fk),
                        ).fetchone()
                        if dup:
                            continue
                        conn.execute(
                            """
                            INSERT INTO order_history (
                                code, side, qty, price, order_type, order_no, timestamp, result,
                                name, reason, filled_price, filled_qty,
                                snap_vol_power, snap_bid_ask,
                                order_source, fill_row_key
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(r.get("code") or "").zfill(6),
                                str(r.get("side") or "buy"),
                                int(r.get("qty") or 0),
                                int(r.get("price") or 0),
                                str(r.get("order_type") or "00"),
                                str(r.get("order_no") or ""),
                                str(r.get("timestamp") or ""),
                                str(r.get("result") or "FILLED"),
                                str(r.get("name") or ""),
                                str(r.get("reason") or ""),
                                int(r.get("filled_price") or 0),
                                int(r.get("filled_qty") or 0),
                                r.get("snap_vol_power"),
                                str(r.get("snap_bid_ask") or ""),
                                "KT00007",
                                fk,
                            ),
                        )
                        added += 1
                    except Exception as e:
                        try:
                            print(f"[DataManagerV2] merge_exchange_fill_order_rows 행 스킵: {e}")
                        except Exception:
                            pass
                conn.commit()
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] merge_exchange_fill_order_rows 실패: {e}")
            except Exception:
                pass
            return added
        except Exception as e:
            try:
                print(f"[DataManagerV2] merge_exchange_fill_order_rows 오류: {e}")
            except Exception:
                pass
            return added
        return added

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

    def get_orders_for_calendar_date(self, day: date) -> List[Tuple[Any, ...]]:
        """
        캘린더 하루치 주문 이력. 오늘은 get_today_orders()와 동일(당일 0시~현재 시각).
        과거 날짜는 해당일 00:00:00 ~ 23:59:59 (표시는 최신순).
        """
        try:
            if day == date.today():
                return self.get_today_orders()
            start_dt = day.strftime("%Y-%m-%d 00:00:00")
            end_dt = day.strftime("%Y-%m-%d 23:59:59")
            with self._get_state_connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT timestamp, code, side, qty, price, filled_price, filled_qty, result, name, reason,
                           snap_vol_power, snap_bid_ask, fill_snap_vol_power, fill_snap_bid_ask
                    FROM order_history
                    WHERE timestamp >= ? AND timestamp <= ?
                    ORDER BY timestamp DESC
                    """,
                    (start_dt, end_dt),
                )
                return list(cursor.fetchall())
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] get_orders_for_calendar_date 실패: {e}")
            except Exception:
                pass
            return []
        except Exception as e:
            try:
                print(f"[DataManagerV2] get_orders_for_calendar_date 오류: {e}")
            except Exception:
                pass
            return []

    def get_distinct_order_calendar_dates(self) -> List[date]:
        """
        order_history 에 레코드가 있는 캘린더 날짜 목록(오름차순, 중복 없음).
        주문 로그 날짜 네비에서 빈 날짜 건너뛰기용.
        """
        try:
            with self._get_state_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT DISTINCT substr(timestamp, 1, 10) AS d
                    FROM order_history
                    WHERE timestamp IS NOT NULL AND length(trim(timestamp)) >= 10
                    ORDER BY d ASC
                    """
                )
                rows = cur.fetchall()
            out: List[date] = []
            for row in rows:
                try:
                    s = str(row[0]).strip()[:10]
                    out.append(datetime.strptime(s, "%Y-%m-%d").date())
                except (TypeError, ValueError, IndexError):
                    continue
            return out
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] get_distinct_order_calendar_dates 실패: {e}")
            except Exception:
                pass
            return []
        except Exception as e:
            try:
                print(f"[DataManagerV2] get_distinct_order_calendar_dates 오류: {e}")
            except Exception:
                pass
            return []

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

    def _normalize_order_stock_code(self, raw: Any) -> str:
        """order_history.code 와 포트폴리오 6자리 키를 맞추기 위한 정규화."""
        try:
            s = str(raw or "").strip()
            if not s:
                return ""
            if "_" in s:
                s = s.split("_", 1)[0]
            return s.zfill(6)
        except Exception:
            return ""

    def get_last_successful_buy_for_code(self, code: str) -> Optional[Dict[str, Any]]:
        """
        동일 종목의 가장 최근 매수 성공(또는 체결) 건 — 로컬 보유 없이 매도만 반영될 때 원가 추정용.
        """
        try:
            target = self._normalize_order_stock_code(code)
            if not target:
                return None
            with self._get_state_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT timestamp, code, qty, price, filled_price, filled_qty, result, name, reason
                    FROM order_history
                    WHERE lower(side) = 'buy'
                      AND (
                          upper(result) IN ('SUCCESS', 'FILLED', 'MOCK')
                          OR result = 'MOCK'
                      )
                    ORDER BY timestamp DESC
                    LIMIT 120
                    """
                )
                rows = cur.fetchall()
            for row in rows:
                (
                    ts,
                    c,
                    q,
                    p,
                    fp,
                    fq,
                    _res,
                    nm,
                    rs,
                ) = row
                if self._normalize_order_stock_code(c) != target:
                    continue
                try:
                    fq_i = int(fq or 0)
                except (TypeError, ValueError):
                    fq_i = 0
                try:
                    q_i = int(q or 0)
                except (TypeError, ValueError):
                    q_i = 0
                try:
                    fp_i = int(fp or 0)
                except (TypeError, ValueError):
                    fp_i = 0
                try:
                    p_i = int(p or 0)
                except (TypeError, ValueError):
                    p_i = 0
                buy_px = float(fp_i if fp_i > 0 else p_i)
                if buy_px <= 0:
                    continue
                qty_eff = fq_i if fq_i > 0 else q_i
                if qty_eff <= 0:
                    continue
                return {
                    "timestamp": str(ts or ""),
                    "code": str(c or ""),
                    "qty": int(qty_eff),
                    "buy_price": buy_px,
                    "name": str(nm or ""),
                    "reason": str(rs or ""),
                }
            return None
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] get_last_successful_buy_for_code 실패: {e}")
            except Exception:
                pass
            return None
        except Exception as e:
            try:
                print(f"[DataManagerV2] get_last_successful_buy_for_code 오류: {e}")
            except Exception:
                pass
            return None

    def get_latest_buy_reason_with_sig_type(self, code: str) -> Optional[str]:
        """
        매수 이력 중 reason 에 TYPE_A / TYPE_B 가 명시된 가장 최근 건.
        (kt00007 FILLED 등이 최상단이어도 실제 전략 구분이 있는 행을 찾기 위함)
        """
        try:
            target = self._normalize_order_stock_code(code)
            if not target:
                return None
            with self._get_state_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT reason
                    FROM order_history
                    WHERE lower(side) = 'buy'
                      AND (
                          upper(result) IN ('SUCCESS', 'FILLED', 'MOCK')
                          OR result = 'MOCK'
                      )
                    ORDER BY timestamp DESC
                    LIMIT 120
                    """,
                )
                rows = cur.fetchall()
            for (rs,) in rows:
                try:
                    r = str(rs or "").upper()
                except Exception:
                    continue
                if "TYPE_B" in r or "TYPE_A" in r:
                    return str(rs or "")
            return None
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] get_latest_buy_reason_with_sig_type 실패: {e}")
            except Exception:
                pass
            return None
        except Exception as e:
            try:
                print(f"[DataManagerV2] get_latest_buy_reason_with_sig_type 오류: {e}")
            except Exception:
                pass
            return None

    def get_sell_order_meta_by_order_no(self, order_no: str) -> Optional[Dict[str, Any]]:
        """주문번호로 매도 접수 행 조회 — CHE 경로에서 종목명·사유 보강."""
        try:
            oid = str(order_no or "").strip()
            if not oid:
                return None
            with self._get_state_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT name, reason, qty, price, code
                    FROM order_history
                    WHERE order_no = ?
                      AND lower(side) = 'sell'
                    ORDER BY timestamp DESC
                    LIMIT 1
                    """,
                    (oid,),
                )
                row = cur.fetchone()
            if not row:
                return None
            return {
                "name": str(row[0] or ""),
                "reason": str(row[1] or ""),
                "qty": int(row[2] or 0),
                "price": int(row[3] or 0),
                "code": str(row[4] or ""),
            }
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] get_sell_order_meta_by_order_no 실패: {e}")
            except Exception:
                pass
            return None
        except Exception as e:
            try:
                print(f"[DataManagerV2] get_sell_order_meta_by_order_no 오류: {e}")
            except Exception:
                pass
            return None

    def get_order_history_row_by_order_no(
        self, order_no: str
    ) -> Optional[Dict[str, Any]]:
        """order_history에서 order_no 1건을 조회합니다.

        반환 필드(대략):
        - code, side, name, reason
        - price, filled_price, filled_qty
        - result
        - timestamp
        """
        try:
            oid = str(order_no or "").strip()
            if not oid:
                return None
            with self._get_state_connection() as conn:
                row = conn.execute(
                    """
                    SELECT
                        code,
                        side,
                        name,
                        reason,
                        price,
                        filled_price,
                        filled_qty,
                        result,
                        timestamp
                    FROM order_history
                    WHERE order_no = ?
                    ORDER BY timestamp DESC, id DESC
                    LIMIT 1
                    """,
                    (oid,),
                ).fetchone()
            if not row:
                return None
            return {
                "code": str(row[0] or ""),
                "side": str(row[1] or ""),
                "name": str(row[2] or ""),
                "reason": str(row[3] or ""),
                "price": float(row[4] or 0),
                "filled_price": float(row[5] or 0),
                "filled_qty": int(row[6] or 0),
                "result": str(row[7] or ""),
                "timestamp": row[8],
            }
        except Exception:
            return None

    def get_order_history_row_for_telegram(
        self, code: str, order_no: str
    ) -> Optional[Dict[str, Any]]:
        """
        텔레그램 주문/체결 메시지를 '포지션관리탭 주문·체결 내역' 컬럼과 동일한 값으로 만들기 위한 조회.

        반환 Dict 필드(대략):
        - timestamp, side, name, reason
        - price(주문가), filled_price(체결가), filled_qty(체결수량)
        - snap_vol_power(VP), snap_bid_ask(호가)
        """
        try:
            ncode = normalize_code(str(code or "").strip())
            oid = str(order_no or "").strip()
            if not ncode or not oid:
                return None

            with self._get_state_connection() as conn:
                row = conn.execute(
                    """
                    SELECT
                        timestamp,
                        side,
                        name,
                        reason,
                        price,
                        filled_price,
                        filled_qty,
                        snap_vol_power,
                        snap_bid_ask
                    FROM order_history
                    WHERE code = ?
                      AND order_no = ?
                    ORDER BY timestamp DESC, id DESC
                    LIMIT 1
                    """,
                    (ncode, oid),
                ).fetchone()

            if not row:
                return None

            return {
                "timestamp": row[0],
                "side": row[1],
                "name": row[2],
                "reason": row[3],
                "price": row[4],
                "filled_price": row[5],
                "filled_qty": row[6],
                "snap_vol_power": row[7],
                "snap_bid_ask": row[8],
            }
        except Exception:
            return None

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

    def patch_buy_filled_price_after_reconcile(
        self,
        code: str,
        broker_avg_price: float,
        broker_qty: int,
    ) -> None:
        """
        계좌 동기화(ka10085) 직후 증권사 매입평균과 order_history.filled_price 를 맞춥니다.
        동일 종목 매수 행이 DB에 1건일 때만 갱신합니다(추매 등 복수 건은 혼합 평단이므로 생략).
        """
        try:
            if broker_qty <= 0 or broker_avg_price <= 0:
                return
            raw = str(code or "").strip()
            if not raw:
                return
            nc = normalize_code(raw)
            if nc.isdigit() and len(nc) < 6:
                nc = nc.zfill(6)
            fp_int = int(round(float(broker_avg_price)))
            candidates: List[str] = []
            for c in (nc, raw.zfill(6) if raw.isdigit() else "", raw):
                if c and c not in candidates:
                    candidates.append(c)
            with self._lock, self._get_state_connection() as conn:
                rows: List[Tuple[Any, ...]] = []
                for cand in candidates:
                    try:
                        cur = conn.execute(
                            """
                            SELECT id, COALESCE(filled_qty, 0), COALESCE(qty, 0), COALESCE(filled_price, 0)
                            FROM order_history
                            WHERE code = ?
                              AND LOWER(TRIM(COALESCE(side, ''))) = 'buy'
                              AND (
                                UPPER(COALESCE(result, '')) IN ('FILLED', 'SUCCESS', 'MOCK')
                                OR COALESCE(filled_qty, 0) > 0
                              )
                              AND UPPER(COALESCE(result, '')) NOT IN ('CANCELLED', 'REJECTED', 'FAILED')
                            ORDER BY timestamp DESC, id DESC
                            """,
                            (cand,),
                        )
                        rows = cur.fetchall()
                        if rows:
                            break
                    except sqlite3.Error:
                        continue
                if len(rows) != 1:
                    return
                rid, fq, oq, old_fp = rows[0]
                eff_q = int(fq) if fq else int(oq) if oq else 0
                if eff_q > 0 and eff_q != int(broker_qty):
                    return
                try:
                    if int(old_fp) == fp_int:
                        return
                except (TypeError, ValueError):
                    pass
                conn.execute(
                    "UPDATE order_history SET filled_price = ? WHERE id = ?",
                    (fp_int, int(rid)),
                )
                conn.commit()
        except Exception as e:
            try:
                print(f"[DataManagerV2] patch_buy_filled_price_after_reconcile: {e}")
            except Exception:
                pass

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
        if kiwoom_code in MARKET_INDEX_BAR_CODES:
            return False
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
        bars_1d에 저장된 일봉 중 가장 최근 일자(시장지수·VIX 코드 제외).
        """
        try:
            ph = ",".join("?" * len(MARKET_INDEX_BAR_CODES))
            with self._get_connection() as conn:
                row = conn.execute(
                    f"SELECT MAX(datetime) FROM bars_1d WHERE code NOT IN ({ph})",
                    MARKET_INDEX_BAR_CODES,
                ).fetchone()
                s = row[0] if row and row[0] else None
            if not s:
                return None
            return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
        except Exception:
            return None

    def main_db_has_any_intraday_rows(self) -> bool:
        """
        trading_data.db 에 분봉·15분봉이 한 건이라도 있는지.
        배포본처럼 빈 DB(또는 미하베스팅)일 때 실시간 워밍업이 DB 경로에 의존하지 않도록 판별에 사용.
        """
        try:
            with self._get_connection() as conn:
                if conn.execute("SELECT 1 FROM bars_15m LIMIT 1").fetchone():
                    return True
                if conn.execute("SELECT 1 FROM bars_1m LIMIT 1").fetchone():
                    return True
            return False
        except Exception:
            try:
                print("[DataManagerV2] main_db_has_any_intraday_rows 조회 실패 → False 처리")
            except Exception:
                pass
            return False

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

    def vacuum_main_db(self) -> bool:
        """
        메인 DB 파일을 재작성해 DELETE로 비운 페이지(freelist)를 OS에 반환한다.
        대용량 DB는 수 분 걸릴 수 있으며, 실행 중 짧게 배타 락이 걸린다.
        """
        try:
            try:
                print(
                    "[DataManagerV2] VACUUM 실행 중… (파일 크기 반영, 완료까지 시간이 걸릴 수 있음)"
                )
            except Exception:
                pass
            with self._lock, self._get_connection() as conn:
                conn.execute("VACUUM")
            try:
                print("[DataManagerV2] VACUUM 완료")
            except Exception:
                pass
            return True
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] VACUUM 실패: {e}")
            except Exception:
                pass
            return False
        except Exception as e:
            try:
                print(f"[DataManagerV2] VACUUM 오류: {e}")
            except Exception:
                pass
            return False

    def purge_market_index_bars_from_main_db(
        self, *, run_vacuum_if_deleted: bool = False
    ) -> Dict[str, int]:
        """
        시장지수·코스닥 지수·VIX 등 레거니 시계열을 trading_data.db(메인)에서 삭제한다.
        bars_1m / bars_15m / bars_1d / sync_meta 만 대상. 멱등 호출 가능.

        SQLite는 DELETE만으로 파일 크기가 줄지 않는다. 디스크 반환은 ``vacuum_main_db()`` 또는
        ``run_vacuum_if_deleted=True`` 로 VACUUM 실행(대용량 DB는 수십 분·기동 시 자동 호출 비권장).

        기본값 ``run_vacuum_if_deleted=False`` — 삭제만 하고, 필요 시 수동으로 VACUUM 한다.
        """
        out: Dict[str, int] = {
            "bars_1m": 0,
            "bars_15m": 0,
            "bars_1d": 0,
            "sync_meta": 0,
        }
        ph = ",".join("?" * len(MARKET_INDEX_BAR_CODES))
        try:
            with self._lock, self._get_connection() as conn:
                for tbl in ("bars_1m", "bars_15m", "bars_1d"):
                    try:
                        cur = conn.execute(
                            f"DELETE FROM {tbl} WHERE code IN ({ph})",
                            MARKET_INDEX_BAR_CODES,
                        )
                        out[str(tbl)] = int(cur.rowcount or 0)
                    except sqlite3.Error as e:
                        try:
                            print(
                                f"[DataManagerV2] purge_market_index: {tbl} DELETE 실패: {e}"
                            )
                        except Exception:
                            pass
                try:
                    cur2 = conn.execute(
                        f"DELETE FROM sync_meta WHERE code IN ({ph})",
                        MARKET_INDEX_BAR_CODES,
                    )
                    out["sync_meta"] = int(cur2.rowcount or 0)
                except sqlite3.Error as e:
                    try:
                        print(
                            f"[DataManagerV2] purge_market_index: sync_meta DELETE 실패: {e}"
                        )
                    except Exception:
                        pass
                conn.commit()
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] purge_market_index_bars_from_main_db 실패: {e}")
            except Exception:
                pass
        except Exception as e:
            try:
                print(f"[DataManagerV2] purge_market_index_bars_from_main_db 오류: {e}")
            except Exception:
                pass
            return out

        n_del = sum(out.values())
        if n_del > 0 and not run_vacuum_if_deleted:
            try:
                print(
                    "[DataManagerV2] 지수/VIX 행이 삭제되었습니다. "
                    "파일 크기는 VACUUM 전까지 거의 변하지 않을 수 있습니다. "
                    "여유 있을 때 DataManagerV2().vacuum_main_db() 를 실행하세요."
                )
            except Exception:
                pass
        if run_vacuum_if_deleted and n_del > 0:
            self.vacuum_main_db()
        return out

    def _distinct_bars_1d_dates_for_code(
        self, code: str, start_date: str, end_date: str
    ) -> List[str]:
        """한 종목의 bars_1d 에 실제로 있는 거래일 문자열 목록."""
        try:
            nc = normalize_code(code)
        except Exception:
            nc = str(code).strip().zfill(6)
        s, e = str(start_date)[:10], str(end_date)[:10]
        try:
            with self._get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT DISTINCT datetime FROM bars_1d
                    WHERE code = ? AND datetime BETWEEN ? AND ?
                    ORDER BY datetime ASC
                    """,
                    (nc, s, e),
                ).fetchall()
            return [str(row[0])[:10] for row in rows if row and row[0]]
        except sqlite3.Error as e:
            try:
                import logging

                logging.warning(
                    "distinct_bars_1d_dates_for_code 실패 %s: %s", nc, e, exc_info=True
                )
            except Exception:
                pass
            return []
        except Exception:
            return []

    def _trading_days_from_reference_equity_bars_1d(
        self, start_date: str, end_date: str
    ) -> List[str]:
        """기준 종목 중 첫 번째로 일봉이 있는 종목의 거래일 집합(구 001 일봉 역할 대체)."""
        for raw in _CALENDAR_REFERENCE_STOCK_CODES:
            got = self._distinct_bars_1d_dates_for_code(raw, start_date, end_date)
            if got:
                return got
        return []

    def get_trading_days(self, start_date: str, end_date: str) -> List[str]:
        """
        기간 내 백테·유니버스용 거래일(YYYY-MM-DD).

        - 예전에는 001 지수 일봉 날짜에 맞췄고, 지수 제거 후에는 KRX만 쓰면
          DB에 분봉이 없는 날까지 루프가 돌아 일별 자산·체결이 깨질 수 있다.
        - 따라서 **KRX 영업일 ∩ 기준 종목(005930 등) bars_1d 실제 일자** 교집합을 우선 사용한다.
        """
        s, e = str(start_date)[:10], str(end_date)[:10]
        krx_list: List[str] = []
        try:
            from reporting.krx_trading_calendar import trading_days_inclusive

            krx_list = [d.strftime("%Y-%m-%d") for d in trading_days_inclusive(s, e)]
        except Exception as e:
            try:
                import logging

                logging.warning(
                    "get_trading_days: KRX 캘린더 실패 — %s", e, exc_info=True
                )
            except Exception:
                pass

        db_ref = self._trading_days_from_reference_equity_bars_1d(s, e)
        if krx_list and db_ref:
            krx_set = set(krx_list)
            merged = sorted(krx_set.intersection(db_ref))
            if merged:
                return merged

        if db_ref:
            return sorted(db_ref)

        if krx_list:
            try:
                import logging

                logging.warning(
                    "get_trading_days: 기준 종목 일봉 없음 — KRX 일자만 사용(데이터 공백일 가능)"
                )
            except Exception:
                pass
            return krx_list

        try:
            ph = ",".join("?" * len(MARKET_INDEX_BAR_CODES))
            q = (
                f"SELECT DISTINCT datetime FROM bars_1d WHERE datetime BETWEEN ? AND ? "
                f"AND code NOT IN ({ph}) ORDER BY datetime ASC"
            )
            params: Tuple[Any, ...] = (s, e, *MARKET_INDEX_BAR_CODES)
            with self._get_connection() as conn:
                rows = conn.execute(q, params).fetchall()
            return [str(row[0])[:10] for row in rows if row and row[0]]
        except sqlite3.Error as e:
            try:
                import logging

                logging.warning("get_trading_days: DB DISTINCT 폴백 실패 — %s", e, exc_info=True)
            except Exception:
                pass
            return []
        except Exception:
            return []

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

    def fetch_kiwoom_minute_chart_page(
        self,
        code: str,
        token: str,
        next_key: str = "",
        *,
        tic_scope: str = "1",
    ) -> Tuple[Optional[List[Any]], str]:
        """
        키움 ka10080 주식분봉 1페이지 조회.
        tic_scope: 봉 주기(문자열). 예) "1"=1분, "15"=15분 — 키움 스펙에 따름.
        next_key 가 비어 있으면 최초 조회, 있으면 연속조회(cont-yn=Y).
        반환: (분봉 레코드 리스트 또는 오류 시 None, 다음 페이지용 next_key)
        """
        base = config.MOCK_URL if getattr(config, "USE_MOCK", False) else config.URL
        url = f"{base}/api/dostk/chart"
        ts = str(tic_scope).strip() if tic_scope is not None else "1"
        if not ts:
            ts = "1"
        body: Dict[str, Any] = {"stk_cd": code, "tic_scope": ts, "upd_stkpc_tp": "1"}
        nk_in = (next_key or "").strip()
        max_retries = 3
        for attempt in range(max_retries):
            try:
                headers = {
                    "Content-Type": "application/json;charset=UTF-8",
                    "authorization": f"Bearer {token}",
                    "cont-yn": "Y" if nk_in else "N",
                    "next-key": nk_in,
                    "api-id": "ka10080",
                }
                res = requests.post(url, headers=headers, json=body, timeout=15)
                if res.status_code == 200:
                    try:
                        data = res.json() if res.content else {}
                    except Exception:
                        data = {}
                    raw_list = data.get(
                        "stk_min_pole_chart_qry",
                        data.get("output", data.get("data", [])),
                    )
                    if raw_list is None:
                        raw_list = []
                    nk_out = (res.headers.get("next-key") or "").strip()
                    if not nk_out:
                        try:
                            nk_out = str(data.get("next-key") or "").strip()
                        except Exception:
                            nk_out = ""
                    return raw_list, nk_out
                if res.status_code == 429:
                    if attempt + 1 < max_retries:
                        time.sleep((attempt + 1) * 2.0)
                        continue
                    print(f"  [Error] ka10080 429 ({code})")
                    return None, ""
                print(f"  [Error] ka10080 Status {res.status_code} ({code})")
                return None, ""
            except Exception as e:
                if attempt + 1 < max_retries:
                    time.sleep(1.0)
                    continue
                print(f"  [Error] ka10080 예외 ({code}): {e}")
                return None, ""
        return None, ""

    def fetch_15m_bars_for_trading_day(
        self,
        code: str,
        day_yyyy_mm_dd: str,
        *,
        logger: Optional[Any] = None,
        max_pages: int = 80,
    ) -> Any:
        """
        일일결산·가상 종합리포트용 15분봉.

        1) ``trading_data.db`` 의 ``bars_15m`` 구간 조회
        2) 비어 있으면 동일 일자 ``bars_1m`` 를 15분봉으로 리샘플
        3) 그래도 없으면 :meth:`fetch_15m_bars_for_trading_day_rest_only` (키움 REST)

        반환 형식은 :meth:`get_bars` 15m 과 동일(time, Open, High, Low, Close, Volume).
        가상 리포트 스레드에서 REST pacing 판별용으로 ``_last_report_15m_source`` 에
        ``db15`` / ``db1m`` / ``rest`` 를 설정합니다.
        """
        def _log(msg: str) -> None:
            if logger is not None:
                try:
                    logger(msg)
                except Exception:
                    pass

        # pacing 판별용(캐시 미스 시 호출 직후 조회). 실패·조기 반환 시 빈 문자열.
        self._last_report_15m_source = ""

        try:
            n_code = normalize_code(str(code).strip())
        except Exception:
            n_code = str(code).strip().zfill(6)
        day = str(day_yyyy_mm_dd).strip()[:10]
        day_start = f"{day} 00:00:00"
        day_end = f"{day} 23:59:59"

        df15: Optional[pd.DataFrame] = None
        try:
            df15 = self.get_bars(
                n_code, start_dt=day_start, end_dt=day_end, interval="15m"
            )
        except Exception as e:
            _log(f"[리포트15m] {n_code} {day} DB bars_15m 조회 예외: {e}")
            df15 = None

        if df15 is not None and not df15.empty:
            try:
                nrows = len(df15)
            except Exception:
                nrows = -1
            self._last_report_15m_source = "db15"
            _log(f"[리포트15m] {n_code} {day} trading_data.db bars_15m {nrows}건")
            return df15

        df1: Optional[pd.DataFrame] = None
        try:
            df1 = self.get_bars(
                n_code, start_dt=day_start, end_dt=day_end, interval="1m"
            )
        except Exception as e:
            _log(f"[리포트15m] {n_code} {day} DB bars_1m 조회 예외: {e}")
            df1 = None

        if df1 is not None and not df1.empty:
            try:
                w = df1.copy()
                w["time"] = pd.to_datetime(w["time"], errors="coerce")
                w = w.dropna(subset=["time"])
                if w.empty:
                    raise ValueError("time 파싱 후 비어 있음")
                w = w.set_index("time").sort_index()
                agg = {
                    "Open": "first",
                    "High": "max",
                    "Low": "min",
                    "Close": "last",
                    "Volume": "sum",
                }
                miss = [k for k in agg if k not in w.columns]
                if miss:
                    raise ValueError(f"필수 열 없음: {miss}")
                rs = (
                    w.resample("15min", closed="right", label="right")
                    .agg(agg)
                    .dropna(how="any")
                )
                rs = rs.reset_index()
                if rs.empty:
                    raise ValueError("리샘플 결과 없음")
                self._last_report_15m_source = "db1m"
                _log(
                    f"[리포트15m] {n_code} {day} trading_data.db bars_1m→15m "
                    f"{len(rs)}건"
                )
                return rs
            except Exception as e:
                _log(f"[리포트15m] {n_code} {day} bars_1m→15m 리샘플 실패: {e}")

        self._last_report_15m_source = "rest"
        _log(f"[리포트15m] {n_code} {day} DB에 해당일 봉 없음 → REST 폴백")
        try:
            return self.fetch_15m_bars_for_trading_day_rest_only(
                code, day, logger=logger, max_pages=max_pages
            )
        except Exception as e:
            _log(f"[리포트15m] {n_code} {day} REST 폴백 예외: {e}")
            return pd.DataFrame()

    def fetch_15m_bars_for_trading_day_rest_only(
        self,
        code: str,
        day_yyyy_mm_dd: str,
        *,
        logger: Optional[Any] = None,
        max_pages: int = 80,
    ) -> Any:
        """
        리포트용 **REST 전용** 폴백: trading_data.db 를 읽지 않습니다.
        키움 ka10080 을 **15분봉(tic_scope=15)** 연속조회만으로 해당 일자(00:00~23:59) 구간을 채웁니다.
        반환 형식은 get_bars(..., interval='15m') 와 유사(time, Open, High, Low, Close, Volume).
        일반적으로는 :meth:`fetch_15m_bars_for_trading_day` 가 DB 우선으로 이 메서드를 호출합니다.
        """
        import pandas as pd
        from kiwoom_rest_api.auth.token import TokenManager

        def _log(msg: str) -> None:
            if logger is not None:
                try:
                    logger(msg)
                except Exception:
                    pass

        try:
            n_code = normalize_code(str(code).strip())
        except Exception:
            n_code = str(code).strip().zfill(6)
        day = str(day_yyyy_mm_dd).strip()[:10]
        day_start = f"{day} 00:00:00"
        day_end = f"{day} 23:59:59"

        try:
            token = TokenManager().get_token()
        except Exception:
            token = None
        if not token:
            _log(f"[일일결산] {n_code} 토큰 없음 — 15분봉 REST 생략")
            return pd.DataFrame()

        def _clean_px(v: Any) -> int:
            try:
                return int(
                    float(
                        str(v)
                        .replace("+", "")
                        .replace("-", "")
                        .replace(",", "")
                        .strip()
                        or 0
                    )
                )
            except Exception:
                return 0

        next_key_in = ""
        rows_acc: List[Dict[str, Any]] = []
        seen_dt: set = set()
        for page in range(max(1, int(max_pages))):
            raw_list, next_key_out = self.fetch_kiwoom_minute_chart_page(
                n_code, token, next_key_in, tic_scope="15"
            )
            if not raw_list:
                break
            page_has_in_day = False
            for b in raw_list:
                if not isinstance(b, dict):
                    continue
                tm = b.get("cntr_tm") or b.get("date_time") or b.get("datetime")
                if not tm:
                    continue
                s = str(tm).strip()
                if len(s) < 14:
                    continue
                try:
                    dt_fmt = f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}:{s[12:14]}"
                except Exception:
                    continue
                if dt_fmt < day_start or dt_fmt > day_end:
                    continue
                page_has_in_day = True
                if dt_fmt in seen_dt:
                    continue
                seen_dt.add(dt_fmt)
                rows_acc.append(
                    {
                        "datetime": dt_fmt,
                        "open": _clean_px(
                            b.get("open_pric", b.get("open", 0))
                        ),
                        "high": _clean_px(
                            b.get("high_pric", b.get("high", 0))
                        ),
                        "low": _clean_px(b.get("low_pric", b.get("low", 0))),
                        "close": _clean_px(
                            b.get("cur_prc", b.get("close", b.get("stck_prpr", 0)))
                        ),
                        "volume": _clean_px(
                            b.get("trde_qty", b.get("volume", b.get("cntg_vol", 0)))
                        ),
                    }
                )
            next_key_in = (next_key_out or "").strip()
            if not next_key_in:
                break
            try:
                time.sleep(0.22)
            except Exception:
                pass
            if not page_has_in_day:
                try:
                    first_tm = str(
                        raw_list[0].get("cntr_tm") or raw_list[0].get("date_time") or ""
                    )
                    if len(first_tm) >= 8 and first_tm[:8] < day.replace("-", ""):
                        break
                except Exception:
                    pass

        if not rows_acc:
            _log(f"[일일결산] {n_code} {day} ka10080(15분) 구간 봉 없음(REST 연속조회 종료)")
            return pd.DataFrame()

        try:
            df15 = pd.DataFrame(rows_acc)
            df15["datetime"] = pd.to_datetime(df15["datetime"], errors="coerce")
            df15 = df15.dropna(subset=["datetime"])
            df15 = df15.sort_values("datetime").drop_duplicates(
                subset=["datetime"], keep="last"
            )
            df15 = df15.reset_index(drop=True)
            df15.rename(
                columns={
                    "datetime": "time",
                    "open": "Open",
                    "high": "High",
                    "low": "Low",
                    "close": "Close",
                    "volume": "Volume",
                },
                inplace=True,
            )
            return df15
        except Exception as e:
            _log(f"[일일결산] {n_code} 15분 가공 실패: {e}")
            return pd.DataFrame()

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
        """
        [v4.5.91] 감시 종목별 시간대 누적·분봉 거래량 프로필 구축.
        sync_meta·bars_data 가 비어 있으면 키움 분봉 API만 사용(trading_data.db 캔들 미필수).
        """
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

    def send_telegram_message(
        self, message: str, parse_mode: Optional[str] = "HTML"
    ) -> bool:
        """
        텔레그램 Bot API로 메시지 전송 (DB 로직과 분리된 공용 유틸 위임).
        """
        try:
            return send_telegram_message(
                message,
                parse_mode=parse_mode,
                log_func=print,
            )
        except Exception:
            return False

    def get_active_positions(self):
        with self._get_state_connection() as conn:
            cursor = conn.execute(
                "SELECT code, name, avg_price, qty, stage, invested, entry_time, reason FROM active_positions WHERE status='OPEN'"
            )
            return cursor.fetchall()

    def get_kospi_list(self, count=200, force=False, settings=None, universe_log=None):
        """
        KODEX200/KOSPI200 유니버스 확보.

        count<=200: ``core.kodex200_universe.resolve_kodex200_universe`` —
        ``kodex200_universe_priority`` (예: ``csv_only``=kodex200.csv 전용, ``etf_api_first`` 등).
        단, settings['universe_index_choice']=='KOSPI200' 이면 ``kospi_universe.csv``를 사용합니다.
        - ``kospi_universe.csv``는 상위 큰 리스트(예: top 400)를 저장
        - 반환 시에는 ``min(count, kospi_top_n)`` 또는 기존 count 파라미터를 head로 슬라이스

        count>200: FDR 코스피 시총 상위 ``count`` (기존과 동일).
        """
        import os

        import FinanceDataReader as fdr
        import pandas as pd

        idx_choice = "KODEX200"
        if isinstance(settings, dict):
            idx_choice = str(settings.get("universe_index_choice", "KODEX200")).strip().upper()
        if idx_choice not in ("KODEX200", "KOSPI200"):
            idx_choice = "KODEX200"

        kospi_top_n = 200
        effective_count = int(count)
        try:
            if isinstance(settings, dict):
                kospi_top_n = int(settings.get("kospi_top_n", 200))
        except Exception:
            kospi_top_n = 200
        if idx_choice == "KOSPI200":
            effective_count = min(effective_count, kospi_top_n)

        cache_file = (
            "kospi_universe.csv" if (count <= 200 and idx_choice == "KOSPI200") else "kodex_200.csv"
        ) if count <= 200 else f"kospi_top_{count}.csv"

        def _ulog(msg: str) -> None:
            try:
                if universe_log is not None:
                    universe_log(msg)
                else:
                    print(msg)
            except Exception:
                pass

        try:
            if count <= 200:
                if idx_choice == "KOSPI200":
                    # 1) kospi_universe.csv 우선 로드
                    try:
                        if os.path.exists(cache_file) and (os.path.getsize(cache_file) > 10) and not force:
                            df = pd.read_csv(cache_file, dtype=str)
                            if "종목코드" in df.columns and "Code" not in df.columns:
                                df.rename(columns={"종목코드": "Code", "종목명": "Name"}, inplace=True)
                            if "Code" not in df.columns:
                                return pd.DataFrame()
                            if "Name" not in df.columns:
                                df["Name"] = df["Code"]
                            df["Code"] = df["Code"].astype(str).str.zfill(6)
                            df_out = df[["Code", "Name"]].head(effective_count).copy()
                            return df_out
                    except Exception:
                        pass

                    # 2) kospi_universe.csv 없으면 strategy_selector 로더를 호출해 생성
                    try:
                        from strategy_selector import _maybe_generate_kospi_universe_csv
                        _maybe_generate_kospi_universe_csv(
                            logger_callback=_ulog if universe_log is not None else None,
                            kospi_universe_path=cache_file,
                            target_n=max(400, effective_count),
                            force_refresh=force,
                        )
                        if os.path.exists(cache_file):
                            df = pd.read_csv(cache_file, dtype=str)
                            if "종목코드" in df.columns and "Code" not in df.columns:
                                df.rename(columns={"종목코드": "Code", "종목명": "Name"}, inplace=True)
                            if "Code" not in df.columns:
                                return pd.DataFrame()
                            if "Name" not in df.columns:
                                df["Name"] = df["Code"]
                            df["Code"] = df["Code"].astype(str).str.zfill(6)
                            return df[["Code", "Name"]].head(effective_count).copy()
                    except Exception:
                        return pd.DataFrame()

                    return pd.DataFrame()

                # KODEX200: 기존 resolve_kodex200_universe 로딩 유지
                merged: dict = {}
                try:
                    from core.defaults import DEFAULT_SETTINGS

                    merged.update(DEFAULT_SETTINGS)
                except Exception:
                    pass
                if isinstance(settings, dict):
                    merged.update(settings)
                from core.kodex200_universe import resolve_kodex200_universe

                df = resolve_kodex200_universe(merged, count=200, log=_ulog)
                if df is not None and not getattr(df, "empty", True):
                    try:
                        df.to_csv(cache_file, index=False, encoding="utf-8-sig")
                    except Exception as ee:
                        try:
                            print(
                                f"DEBUG ERROR [DataManagerV2]: kodex_200.csv 쓰기 실패: {ee}"
                            )
                        except Exception:
                            pass
                    return df
                return pd.DataFrame()

            df = fdr.StockListing("KOSPI")
            df = df[df["Code"].astype(str).str.endswith("0")].sort_values(
                by="Marcap", ascending=False
            ).head(effective_count)[["Code", "Name"]]
            if df is not None and not getattr(df, "empty", True):
                df.to_csv(cache_file, index=False, encoding="utf-8-sig")
            return df
        except Exception as e:
            print(f"  [Error] 종목 리스트 확보 중 오류: {e}")
            if os.path.exists(cache_file):
                try:
                    return pd.read_csv(cache_file, dtype={"Code": str})
                except Exception:
                    pass
            return pd.DataFrame()

    def get_remote_command(self):
        """[v4.6.9] 원격 명령 테이블에서 최신 명령어를 하나 가져오고 삭제합니다 (Pop-and-Delete)."""
        import sqlite3
        conn = None
        try:
            try:
                _sp = state_db_path()
            except Exception:
                _sp = self.state_db_path
            conn = sqlite3.connect(_sp, timeout=10)
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
