# -*- coding: utf-8 -*-
"""
실시간 감시 전용 15분봉 SQLite (하베스팅 trading_data.db 와 분리).

- 모의/실전별 파일: realtime_15m_mock.db / realtime_15m_live.db
- 워밍업 시 로컬 우선 조회, 검증 통과 후 병합 OHLCV 일괄 저장(bulk_upsert_ohlcv_index_frame)
- 장중에는 슬롯이 바뀔 때 확정 15분봉 1행씩 INSERT OR REPLACE
- ``realtime_15m_calendar_retention_days``(기본 365)일보다 오래된 봉은 종목별 DELETE
  (워밍업이 과거를 채우는 목적이 아니라 **디스크 롤링**용)
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import pandas as pd

from utils.server_mode_paths import realtime_monitor_15m_db_path


def _norm_code(raw: Any) -> str:
    try:
        s = str(raw or "").strip()
        if "_" in s:
            s = s.split("_", 1)[0]
        return s.zfill(6)
    except Exception:
        return ""


class RealtimeMonitorBarsStore:
    """실시간 감시용 15분봉 전용 저장소 (배포 시 하베스팅 DB와 별도 파일)."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        logger_callback: Optional[Callable[[str], None]] = None,
        retention_calendar_days: Optional[int] = None,
    ) -> None:
        self._db_path = db_path or realtime_monitor_15m_db_path()
        self._log = logger_callback or print
        self._lock = threading.RLock()
        try:
            d = int(retention_calendar_days) if retention_calendar_days is not None else 365
        except (TypeError, ValueError):
            d = 365
        self._retention_days: int = max(1, min(d, 3650))
        self._ensure_schema()

    def _retention_cutoff_str(self, days: Optional[int] = None) -> str:
        """현재 시각 기준 `days`일 이전 시각 (로컬 타임존). 문자열 비교용 ISO 형식."""
        try:
            n = int(days) if days is not None else int(self._retention_days)
        except (TypeError, ValueError):
            n = 365
        n = max(1, min(n, 3650))
        cut = datetime.now() - timedelta(days=n)
        return cut.strftime("%Y-%m-%d %H:%M:%S")

    def _purge_old_bars_for_code(
        self, conn: sqlite3.Connection, code: str, days: Optional[int] = None
    ) -> None:
        """같은 커넥션에서 종목별로 보존 일수 이전 봉 삭제."""
        try:
            cs = self._retention_cutoff_str(days)
            conn.execute(
                "DELETE FROM bars_15m_rt WHERE code = ? AND datetime < ?",
                (code, cs),
            )
        except sqlite3.Error as e:
            try:
                self._log(f"⚠️ [RT15mDB] 롤링 삭제 실패 ({code}): {e}")
            except Exception:
                pass

    def _ensure_schema(self) -> None:
        try:
            with self._lock:
                with sqlite3.connect(self._db_path) as conn:
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS bars_15m_rt (
                            code TEXT NOT NULL,
                            datetime TEXT NOT NULL,
                            open INTEGER NOT NULL,
                            high INTEGER NOT NULL,
                            low INTEGER NOT NULL,
                            close INTEGER NOT NULL,
                            volume INTEGER NOT NULL,
                            PRIMARY KEY (code, datetime)
                        )
                        """
                    )
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS symbol_meta_rt (
                            code TEXT PRIMARY KEY,
                            last_seen TEXT NOT NULL
                        )
                        """
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_bars_15m_rt_code_dt "
                        "ON bars_15m_rt (code, datetime)"
                    )
                    self._migrate_symbol_meta_rt_columns(conn)
                    conn.commit()
        except sqlite3.Error as e:
            try:
                self._log(f"⚠️ [RT15mDB] 스키마 초기화 실패: {e}")
            except Exception:
                pass

    def _migrate_symbol_meta_rt_columns(self, _conn: sqlite3.Connection) -> None:
        """레거시 호출 호환용 훅(현재 마이그레이션 없음)."""
        pass

    def touch_last_seen(self, code: str) -> None:
        """감시 대상으로 올라온 시각 기록(정리·메타용)."""
        nc = _norm_code(code)
        if not nc:
            return
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self._lock:
                with sqlite3.connect(self._db_path) as conn:
                    conn.execute(
                        """
                        INSERT INTO symbol_meta_rt (code, last_seen)
                        VALUES (?, ?)
                        ON CONFLICT(code) DO UPDATE SET last_seen = excluded.last_seen
                        """,
                        (nc, now),
                    )
                    conn.commit()
        except sqlite3.Error as e:
            try:
                self._log(f"⚠️ [RT15mDB] last_seen 갱신 실패 ({nc}): {e}")
            except Exception:
                pass

    def upsert_completed_bar(
        self,
        code: str,
        bar_ts: Any,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
    ) -> None:
        """
        확정된 한 개의 15분봉을 저장합니다.
        bar_ts: pandas Timestamp 또는 datetime 호환.
        """
        nc = _norm_code(code)
        if not nc:
            return
        try:
            ts = pd.Timestamp(bar_ts)
            dt_str = ts.strftime("%Y-%m-%d %H:%M:%S")
            o = int(round(float(open_)))
            h = int(round(float(high)))
            l = int(round(float(low)))
            c = int(round(float(close)))
            v = int(round(float(volume)))
        except Exception as e:
            try:
                self._log(f"⚠️ [RT15mDB] 봉 값 정규화 실패 ({nc}): {e}")
            except Exception:
                pass
            return
        try:
            with self._lock:
                with sqlite3.connect(self._db_path) as conn:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO bars_15m_rt
                        (code, datetime, open, high, low, close, volume)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (nc, dt_str, o, h, l, c, v),
                    )
                    conn.execute(
                        """
                        INSERT INTO symbol_meta_rt (code, last_seen)
                        VALUES (?, ?)
                        ON CONFLICT(code) DO UPDATE SET last_seen = excluded.last_seen
                        """,
                        (nc, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    )
                    self._purge_old_bars_for_code(conn, nc)
                    conn.commit()
        except sqlite3.Error as e:
            try:
                self._log(f"⚠️ [RT15mDB] 봉 INSERT 실패 ({nc} {dt_str}): {e}")
            except Exception:
                pass

    def bulk_upsert_ohlcv_index_frame(
        self,
        code: str,
        ohlcv: pd.DataFrame,
        *,
        max_rows: Optional[int] = None,
    ) -> int:
        """
        워밍업 완료 시: 병합된 15분 OHLCV를 한 트랜잭션으로 저장합니다.
        인덱스는 DatetimeIndex, 열은 Open/High/Low/Close/Volume(대소문자 무관)이어야 합니다.
        저장 후 캘린더 보존 일수 이전 봉을 해당 종목에서 삭제합니다.
        max_rows: 비 None 이면 상한만큼 tail 후 저장(테스트·비상용). 기본은 전량 저장 후 롤링.
        반환: 저장한 행 수(실패 시 0).
        """
        nc = _norm_code(code)
        if not nc or ohlcv is None or ohlcv.empty:
            return 0
        try:
            d = ohlcv.copy()
            if not isinstance(d.index, pd.DatetimeIndex):
                return 0
            d = d.sort_index()
            d = d[~d.index.duplicated(keep="last")]
            if max_rows is not None and int(max_rows) > 0:
                d = d.tail(int(max_rows))
            if d.empty:
                return 0
            col_map = {str(c).lower(): c for c in d.columns}
            need = ("open", "high", "low", "close", "volume")
            if not all(k in col_map for k in need):
                return 0
            rows: list[tuple[Any, ...]] = []
            now_meta = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for ts, row in d.iterrows():
                try:
                    t = pd.Timestamp(ts)
                    dt_str = t.strftime("%Y-%m-%d %H:%M:%S")
                    o = int(round(float(row[col_map["open"]])))
                    h = int(round(float(row[col_map["high"]])))
                    l = int(round(float(row[col_map["low"]])))
                    c = int(round(float(row[col_map["close"]])))
                    v = int(round(float(row[col_map["volume"]])))
                    rows.append((nc, dt_str, o, h, l, c, v))
                except Exception:
                    continue
            if not rows:
                return 0
            sql_bar = """
                INSERT OR REPLACE INTO bars_15m_rt
                (code, datetime, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """
            sql_meta = """
                INSERT INTO symbol_meta_rt (code, last_seen)
                VALUES (?, ?)
                ON CONFLICT(code) DO UPDATE SET last_seen = excluded.last_seen
            """
            with self._lock:
                with sqlite3.connect(self._db_path) as conn:
                    conn.executemany(sql_bar, rows)
                    conn.execute(sql_meta, (nc, now_meta))
                    self._purge_old_bars_for_code(conn, nc)
                    conn.commit()
            return len(rows)
        except sqlite3.Error as e:
            try:
                self._log(f"⚠️ [RT15mDB] 워밍업 배치 저장 실패 ({nc}): {e}")
            except Exception:
                pass
            return 0
        except Exception as e:
            try:
                self._log(f"⚠️ [RT15mDB] 워밍업 배치 저장 예외 ({nc}): {e}")
            except Exception:
                pass
            return 0

    def fetch_15m_bars_as_dataframe(
        self,
        code: str,
        limit: int,
        *,
        retention_days: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        워밍업용: get_bars(15m)와 동일한 형태의 DataFrame (time, Open, High, Low, Close, Volume).
        retention_days: None 이면 인스턴스 보존 일수로 `datetime >= cutoff` 필터.
        """
        nc = _norm_code(code)
        if not nc or limit <= 0:
            return pd.DataFrame()
        lim = max(1, int(limit))
        try:
            rd = self._retention_days if retention_days is None else int(retention_days)
            rd = max(1, min(rd, 3650))
            cutoff = self._retention_cutoff_str(rd)
        except Exception:
            cutoff = self._retention_cutoff_str()
        try:
            with self._lock:
                with sqlite3.connect(self._db_path) as conn:
                    df = pd.read_sql_query(
                        """
                        SELECT datetime AS time, open AS Open, high AS High,
                               low AS Low, close AS Close, volume AS Volume
                        FROM bars_15m_rt
                        WHERE code = ? AND datetime >= ?
                        ORDER BY datetime DESC
                        LIMIT ?
                        """,
                        conn,
                        params=(nc, cutoff, lim),
                    )
            if not df.empty:
                df = df.iloc[::-1].reset_index(drop=True)
            if df.empty:
                return pd.DataFrame()
            df["time"] = pd.to_datetime(df["time"], errors="coerce")
            df = df.dropna(subset=["time"])
            return df
        except Exception as e:
            try:
                self._log(f"⚠️ [RT15mDB] 조회 실패 ({nc}): {e}")
            except Exception:
                pass
            return pd.DataFrame()
