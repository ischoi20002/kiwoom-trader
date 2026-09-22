import sqlite3
import pandas as pd
import os
from datetime import datetime, timedelta, date
from typing import Any, Dict, List, Optional, Set, Tuple

from utils.normalization import normalize_code, canonical_stock_code
from utils.telegram_sender import send_telegram_message
from data_manager.fdr_daily_sync import FdrDailySyncMixin
from data_manager.volume_profile_cache import VolumeProfileCacheMixin
from data_manager.universe_listing import UniverseListingMixin
from data_manager.trading_days_maintenance import TradingDaysMaintenanceMixin
from data_manager.active_positions_store import ActivePositionsStoreMixin
from data_manager.order_history_store import OrderHistoryStoreMixin
from data_manager.kiwoom_chart_fetch import KiwoomChartFetchMixin

# ka10080 헬퍼 — data_manager.kiwoom_chart_fetch 로 이전 (호환 re-export)
from data_manager.kiwoom_chart_fetch import (  # noqa: E402
    _log_ka10080_rate_limit_throttled,
    _ka10080_global_acquire_slot,
    _ka10080_global_bump_cooldown,
    _ka10080_response_diag,
    _ka10080_diag_detail,
)


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
    refresh_resampled_data_at_path_range,
    aggregate_daily_ohlcv_from_1m_db_range,
)
from utils.server_mode_paths import (
    migrate_legacy_trading_state_db_files,
    state_db_path,
    state_db_path_for_live,
    state_db_path_for_mock,
)
import random
import threading
import requests
import config
import time
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

# ka10080 전역 페이싱(워밍업·M1폴러·15분 확정 보정 등 병렬 스레드가 동시에 때리는 429 완화)












class DataManagerV2(
    FdrDailySyncMixin,
    VolumeProfileCacheMixin,
    UniverseListingMixin,
    TradingDaysMaintenanceMixin,
    ActivePositionsStoreMixin,
    OrderHistoryStoreMixin,
    KiwoomChartFetchMixin,
):
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
        self.last_ka10080_fetch_meta: Dict[str, Any] = {}
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
                    status TEXT DEFAULT 'OPEN',
                    max_p REAL DEFAULT 0,
                    max_p_high REAL DEFAULT 0,
                    box_lower REAL DEFAULT 0,
                    box_upper REAL DEFAULT 0,
                    box_atr_entry REAL DEFAULT 0,
                    box_breakdown_closes INTEGER DEFAULT 0,
                    box_last_breakdown_bar TEXT DEFAULT ''
                )
            """)
            for _ap_mig in (
                "ALTER TABLE active_positions ADD COLUMN max_p REAL DEFAULT 0",
                "ALTER TABLE active_positions ADD COLUMN max_p_high REAL DEFAULT 0",
                "ALTER TABLE active_positions ADD COLUMN box_lower REAL DEFAULT 0",
                "ALTER TABLE active_positions ADD COLUMN box_upper REAL DEFAULT 0",
                "ALTER TABLE active_positions ADD COLUMN box_atr_entry REAL DEFAULT 0",
                "ALTER TABLE active_positions ADD COLUMN box_breakdown_closes INTEGER DEFAULT 0",
                "ALTER TABLE active_positions ADD COLUMN box_last_breakdown_bar TEXT DEFAULT ''",
            ):
                try:
                    conn.execute(_ap_mig)
                except sqlite3.OperationalError:
                    pass
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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS order_fill_state (
                    order_no TEXT PRIMARY KEY,
                    code TEXT DEFAULT '',
                    side TEXT DEFAULT '',
                    order_qty INTEGER DEFAULT 0,
                    exchange_filled_qty INTEGER DEFAULT 0,
                    applied_qty INTEGER DEFAULT 0,
                    name TEXT DEFAULT '',
                    reason TEXT DEFAULT '',
                    updated_at TEXT DEFAULT ''
                )
            """)
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
                self.sync_daily_bars_from_fdr_to_required_eod(code)
            except Exception as e:
                try:
                    import logging
                    logging.warning("update_historical_data: FDR bars_1d %s: %s", code, e, exc_info=True)
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

    def save_bars_15m(self, code: str, df: Any) -> int:
        """
        ka10080(tic_scope=15) 등 공식 15분봉을 ``bars_15m``에 저장합니다.

        PRIMARY KEY(code, datetime) 기준 ``INSERT OR REPLACE`` —
        과거 1m→15m 리샘플 값이 있으면 동일 시각은 공식봉으로 덮어씁니다.
        반환: 시도한 행 수(0이면 저장 없음).
        """
        try:
            import pandas as pd
        except Exception:
            return 0
        try:
            if df is None or getattr(df, "empty", True):
                return 0
            ncode = normalize_code(code)
            data = df.copy()
            if "time" in data.columns:
                data.rename(columns={"time": "datetime"}, inplace=True)
            elif "Date" in data.columns:
                data.rename(columns={"Date": "datetime"}, inplace=True)
            data.columns = [str(c).lower() for c in data.columns]
            need = ("datetime", "open", "high", "low", "close", "volume")
            for c in need:
                if c not in data.columns:
                    return 0
            data["code"] = ncode
            data = data[["code", "datetime", "open", "high", "low", "close", "volume"]]
            # 문자열 시각 정규화
            data["datetime"] = data["datetime"].astype(str).str.strip()
            for c in ("open", "high", "low", "close", "volume"):
                data[c] = pd.to_numeric(data[c], errors="coerce").fillna(0).astype(int)
            data = data[data["datetime"].str.len() >= 19]
            if data.empty:
                return 0
            rows = data.values.tolist()
            with self._lock, self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.executemany(
                    """
                    INSERT OR REPLACE INTO bars_15m
                        (code, datetime, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                conn.commit()
            return int(len(rows))
        except Exception as e:
            try:
                print(f"[DataManagerV2] save_bars_15m {code}: {e}")
            except Exception:
                pass
            return 0

    def get_bars_15m_bounds(self, code: str) -> tuple[Optional[str], Optional[str]]:
        """``bars_15m`` MIN/MAX datetime. 없으면 (None, None)."""
        try:
            ncode = normalize_code(code)
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT MIN(datetime), MAX(datetime) FROM bars_15m WHERE code = ?",
                    (ncode,),
                ).fetchone()
            if not row or row[0] is None:
                return None, None
            return str(row[0]), str(row[1])
        except Exception:
            return None, None

    def refresh_resampled_data(self, code):
        """
        [레거시] bars_1m → bars_15m 로컬 리샘플 재생성.

        하베스팅은 ka10080(tic_scope=15) 직접 조회로 전환됨.
        도구/마이그레이션용으로만 유지합니다.
        """
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
        [레거시] bars_1m 기준 날짜 범위만 15m 리샘플.

        하베스팅은 공식 15분봉 직접 조회를 사용합니다.
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
        ``bars_1m``을 일(캘린더) 단위로 집계해 ``bars_1d``에 반영.

        일봉이 1분봉 일자보다 이미 최신이면 DB 작업 없이 True.
        그렇지 않으면 **뒤진 구간(마지막 일봉 다음날~1m 상한일)**만 읽어 부분 갱신해
        전 구간 1m 스캔을 피한다. 일봉이 전혀 없을 때만 1m MIN~MAX 전체를 집계한다.
        """
        try:
            ncode = normalize_code(code)
        except Exception:
            return False

        try:
            with self._get_connection() as conn:
                row_1m = conn.execute(
                    "SELECT MIN(datetime), MAX(datetime) FROM bars_1m WHERE code = ?",
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
                    "rebuild_daily_bars_from_1m 메타 조회 실패 %s: %s",
                    ncode,
                    e,
                    exc_info=True,
                )
            except Exception:
                pass
            return False
        except Exception as e:
            try:
                import logging

                logging.warning(
                    "rebuild_daily_bars_from_1m 메타 조회 예외 %s: %s", ncode, e, exc_info=True
                )
            except Exception:
                pass
            return False

        min_1m = row_1m[0] if row_1m else None
        max_1m = row_1m[1] if row_1m else None
        max_1d = row_1d[0] if row_1d else None

        if not max_1m:
            return False

        try:
            end_day = str(max_1m).strip()[:10]
            d_1m = datetime.strptime(end_day, "%Y-%m-%d").date()
        except Exception:
            return False

        try:
            min_day = str(min_1m).strip()[:10] if min_1m else end_day
        except Exception:
            min_day = end_day

        # 일봉 없음: 1m이 존재하는 전 구간 집계
        if not max_1d:
            return self.rebuild_daily_bars_from_1m_range(ncode, min_day, end_day)

        try:
            d_1d = datetime.strptime(str(max_1d).strip()[:10], "%Y-%m-%d").date()
        except Exception:
            return self.rebuild_daily_bars_from_1m_range(ncode, min_day, end_day)

        # 일봉 최종일이 1m 상한보다 미래로 잡힌 비정상 시 1m 기준으로 전 구간 재맞춤
        if d_1d > d_1m:
            return self.rebuild_daily_bars_from_1m_range(ncode, min_day, end_day)
        if d_1d == d_1m:
            return True

        start_cal = d_1d + timedelta(days=1)
        start_day = start_cal.strftime("%Y-%m-%d")

        if start_day > end_day:
            return self.rebuild_daily_bars_from_1m_range(ncode, min_day, end_day)

        return self.rebuild_daily_bars_from_1m_range(ncode, start_day, end_day)

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
        """[Legacy] FDR 일봉 → bars_1d (감시·백테 선정과 동일 소스)."""
        kiwoom_code = normalize_code(code)
        if kiwoom_code in MARKET_INDEX_BAR_CODES:
            return False
        try:
            return self.sync_daily_bars_from_fdr(code, start_day=str(start_date)[:10])
        except Exception as e:
            print(f"  [Error] {kiwoom_code} 일봉(FDR) 동기화 중 오류: {e}")
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






    def set_sync_status(self, code, status):
        code = normalize_code(code)
        with self._get_connection() as conn:
            conn.execute("INSERT INTO sync_meta (code, status) VALUES (?, ?) ON CONFLICT(code) DO UPDATE SET status = excluded.status", (code, status))
            conn.commit()
















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
