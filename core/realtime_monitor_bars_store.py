# -*- coding: utf-8 -*-
"""
실시간 감시 전용 15분봉 SQLite (하베스팅 trading_data.db 와 분리).

- 모의/실전별 파일: realtime_15m_mock.db / realtime_15m_live.db
- 워밍업 시 로컬 우선 조회, 검증 통과 후 병합 OHLCV 일괄 저장(bulk_upsert_ohlcv_index_frame)
- 장중에는 슬롯이 바뀔 때 확정 15분봉 1행씩 INSERT OR REPLACE
- ``bars_15m_rt``: 공식/워밍업·신호용 15분봉
- ``bars_1m_compose_rt``: 15분 합성에 쓰인 1분봉 + 출처(official/tick/flat)
- ``realtime_15m_calendar_retention_days``(기본 365)일보다 오래된 봉은 종목별 DELETE
  (워밍업이 과거를 채우는 목적이 아니라 **디스크 롤링**용)
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, List, Optional, Sequence, Tuple

import pandas as pd

from utils.server_mode_paths import realtime_monitor_15m_db_path

# 합성 1분 출처 라벨
COMPOSE_M1_SOURCE_OFFICIAL = "official"
COMPOSE_M1_SOURCE_TICK = "tick"
COMPOSE_M1_SOURCE_FLAT = "flat"


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
        try:
            self.purge_non_trading_day_bars()
        except Exception:
            pass

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
            conn.execute(
                "DELETE FROM bars_1m_compose_rt WHERE code = ? AND slot_datetime < ?",
                (code, cs),
            )
        except sqlite3.Error as e:
            try:
                self._log(f"⚠️ [RT15mDB] 롤링 삭제 실패 ({code}): {e}")
            except Exception:
                pass

    def purge_non_trading_day_bars(self) -> int:
        """
        주말·휴장일 15분봉을 삭제한다.

        슬롯 롤이 대체공휴일에 직전 종가·거래량 0 행을 넣어 MA60이
        백테스트와 어긋나던 오염을 정리한다.
        """
        deleted = 0
        try:
            from reporting.krx_trading_calendar import is_trading_day
        except Exception:
            return 0
        closed: list[str] = []
        try:
            with self._lock:
                with sqlite3.connect(self._db_path) as conn:
                    days = [
                        str(r[0])
                        for r in conn.execute(
                            "SELECT DISTINCT substr(datetime, 1, 10) FROM bars_15m_rt"
                        ).fetchall()
                        if r and r[0]
                    ]
                    for day_s in days:
                        try:
                            if not is_trading_day(day_s):
                                closed.append(day_s)
                        except Exception:
                            continue
                    for day_s in closed:
                        try:
                            d0 = datetime.strptime(day_s, "%Y-%m-%d")
                            nxt = (d0 + timedelta(days=1)).strftime("%Y-%m-%d")
                            cur = conn.execute(
                                "DELETE FROM bars_15m_rt "
                                "WHERE datetime >= ? AND datetime < ?",
                                (f"{day_s} 00:00:00", f"{nxt} 00:00:00"),
                            )
                            deleted += int(cur.rowcount or 0)
                        except (sqlite3.Error, ValueError):
                            continue
                    if closed:
                        conn.commit()
            if deleted > 0:
                try:
                    self._log(
                        f"ℹ️ [RT15mDB] 비거래일 15분봉 {deleted}건 삭제 "
                        f"({len(closed)}일)"
                    )
                except Exception:
                    pass
        except sqlite3.Error as e:
            try:
                self._log(f"⚠️ [RT15mDB] 비거래일 봉 정리 실패: {e}")
            except Exception:
                pass
        except Exception as e:
            try:
                self._log(f"⚠️ [RT15mDB] 비거래일 봉 정리 예외: {e}")
            except Exception:
                pass
        return deleted

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
                    conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS bars_1m_compose_rt (
                            code TEXT NOT NULL,
                            slot_datetime TEXT NOT NULL,
                            datetime TEXT NOT NULL,
                            open INTEGER NOT NULL,
                            high INTEGER NOT NULL,
                            low INTEGER NOT NULL,
                            close INTEGER NOT NULL,
                            volume INTEGER NOT NULL,
                            source TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            PRIMARY KEY (code, slot_datetime, datetime)
                        )
                        """
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_bars_1m_compose_rt_code_dt "
                        "ON bars_1m_compose_rt (code, datetime)"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_bars_1m_compose_rt_slot "
                        "ON bars_1m_compose_rt (code, slot_datetime)"
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
        *,
        purge: bool = True,
    ) -> None:
        """
        확정된 한 개의 15분봉을 저장합니다.
        bar_ts: pandas Timestamp 또는 datetime 호환.
        purge=False 이면 롤링 DELETE 생략(배치 저장 후 1회 purge 용).
        """
        self._upsert_ohlcv_row(
            "bars_15m_rt",
            code,
            bar_ts,
            open_,
            high,
            low,
            close,
            volume,
            log_tag="RT15mDB",
            purge=purge,
        )

    def upsert_compose_m1_bars(
        self,
        code: str,
        slot_ts: Any,
        rows: Sequence[Tuple[Any, float, float, float, float, float, str]],
        *,
        purge: bool = True,
    ) -> int:
        """
        15분 합성에 사용된 1분봉을 출처와 함께 저장한다.

        rows: (m1_ts, o, h, l, c, v, source)
        source: ``official`` | ``tick`` | ``flat``
        반환: 저장 시도 행 수.
        """
        nc = _norm_code(code)
        if not nc or not rows:
            return 0
        try:
            slot = pd.Timestamp(slot_ts).floor("min")
            slot_s = slot.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return 0
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prepared: List[Tuple[Any, ...]] = []
        for item in rows:
            try:
                m1_ts = pd.Timestamp(item[0]).floor("min")
                dt_s = m1_ts.strftime("%Y-%m-%d %H:%M:%S")
                o = int(round(float(item[1])))
                h = int(round(float(item[2])))
                l_ = int(round(float(item[3])))
                c = int(round(float(item[4])))
                v = int(round(max(0.0, float(item[5]))))
                src = str(item[6] or "").strip().lower() or COMPOSE_M1_SOURCE_TICK
                if src not in (
                    COMPOSE_M1_SOURCE_OFFICIAL,
                    COMPOSE_M1_SOURCE_TICK,
                    COMPOSE_M1_SOURCE_FLAT,
                ):
                    src = COMPOSE_M1_SOURCE_TICK
                prepared.append((nc, slot_s, dt_s, o, h, l_, c, v, src, now_s))
            except Exception:
                continue
        if not prepared:
            return 0
        try:
            with self._lock:
                with sqlite3.connect(self._db_path) as conn:
                    # 같은 슬롯 재합성 시 이전 1분 구성이 남지 않게 교체
                    conn.execute(
                        "DELETE FROM bars_1m_compose_rt "
                        "WHERE code = ? AND slot_datetime = ?",
                        (nc, slot_s),
                    )
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO bars_1m_compose_rt
                        (code, slot_datetime, datetime, open, high, low, close,
                         volume, source, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        prepared,
                    )
                    if purge:
                        self._purge_old_bars_for_code(conn, nc)
                    conn.commit()
            return len(prepared)
        except sqlite3.Error as e:
            try:
                self._log(f"⚠️ [RT15mDB] 합성1분 저장 실패 ({nc} {slot_s}): {e}")
            except Exception:
                pass
            return 0

    def upsert_completed_bars_batch(
        self,
        code: str,
        rows: list[tuple[Any, float, float, float, float, float]],
    ) -> int:
        """
        여러 확정 15분봉을 한 트랜잭션으로 저장합니다.

        정렬교체처럼 종목당 수~수십 행을 넣을 때, 행마다 connect+purge 하면
        종목당 수 초가 걸려 배치 반영이 분 단위로 늘어납니다.
        반환: 실제 INSERT 시도 행 수(실패 시 0).
        """
        nc = _norm_code(code)
        if not nc or not rows:
            return 0
        prepared: list[tuple[Any, ...]] = []
        try:
            from reporting.krx_trading_calendar import is_trading_day
        except Exception:
            is_trading_day = None  # type: ignore[assignment]
        for item in rows:
            try:
                ts = pd.Timestamp(item[0])
                if is_trading_day is not None:
                    try:
                        if not is_trading_day(ts.date()):
                            continue
                    except Exception:
                        pass
                dt_str = ts.strftime("%Y-%m-%d %H:%M:%S")
                o = int(round(float(item[1])))
                h = int(round(float(item[2])))
                l = int(round(float(item[3])))
                c = int(round(float(item[4])))
                v = int(round(float(item[5])))
                prepared.append((nc, dt_str, o, h, l, c, v))
            except Exception:
                continue
        if not prepared:
            return 0
        now_meta = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with self._lock:
                with sqlite3.connect(self._db_path) as conn:
                    conn.executemany(
                        """
                        INSERT OR REPLACE INTO bars_15m_rt
                        (code, datetime, open, high, low, close, volume)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        prepared,
                    )
                    conn.execute(
                        """
                        INSERT INTO symbol_meta_rt (code, last_seen)
                        VALUES (?, ?)
                        ON CONFLICT(code) DO UPDATE SET last_seen = excluded.last_seen
                        """,
                        (nc, now_meta),
                    )
                    self._purge_old_bars_for_code(conn, nc)
                    conn.commit()
            return len(prepared)
        except sqlite3.Error as e:
            try:
                self._log(f"⚠️ [RT15mDB] 배치 봉 INSERT 실패 ({nc}): {e}")
            except Exception:
                pass
            return 0
        except Exception as e:
            try:
                self._log(f"⚠️ [RT15mDB] 배치 봉 INSERT 예외 ({nc}): {e}")
            except Exception:
                pass
            return 0

    def _upsert_ohlcv_row(
        self,
        table: str,
        code: str,
        bar_ts: Any,
        open_: float,
        high: float,
        low: float,
        close: float,
        volume: float,
        *,
        log_tag: str = "RT15mDB",
        touch_meta: bool = True,
        purge: bool = True,
    ) -> None:
        """공통 INSERT OR REPLACE (허용 테이블만)."""
        if table != "bars_15m_rt":
            return
        nc = _norm_code(code)
        if not nc:
            return
        try:
            ts = pd.Timestamp(bar_ts)
            try:
                from reporting.krx_trading_calendar import is_trading_day

                if not is_trading_day(ts.date()):
                    return
            except Exception:
                pass
            dt_str = ts.strftime("%Y-%m-%d %H:%M:%S")
            o = int(round(float(open_)))
            h = int(round(float(high)))
            l = int(round(float(low)))
            c = int(round(float(close)))
            v = int(round(float(volume)))
        except Exception as e:
            try:
                self._log(f"⚠️ [{log_tag}] 봉 값 정규화 실패 ({nc}): {e}")
            except Exception:
                pass
            return
        try:
            with self._lock:
                with sqlite3.connect(self._db_path) as conn:
                    conn.execute(
                        f"""
                        INSERT OR REPLACE INTO {table}
                        (code, datetime, open, high, low, close, volume)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (nc, dt_str, o, h, l, c, v),
                    )
                    if touch_meta:
                        conn.execute(
                            """
                            INSERT INTO symbol_meta_rt (code, last_seen)
                            VALUES (?, ?)
                            ON CONFLICT(code) DO UPDATE SET last_seen = excluded.last_seen
                            """,
                            (nc, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        )
                    if purge:
                        self._purge_old_bars_for_code(conn, nc)
                    conn.commit()
        except sqlite3.Error as e:
            try:
                self._log(f"⚠️ [{log_tag}] 봉 INSERT 실패 ({nc} {dt_str}): {e}")
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
            try:
                from reporting.krx_trading_calendar import filter_ohlcv_index_to_trading_days

                d = filter_ohlcv_index_to_trading_days(d)
            except Exception:
                pass
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
            try:
                from reporting.krx_trading_calendar import is_trading_day

                uniq = {pd.Timestamp(t).date() for t in df["time"]}
                ok = {d for d in uniq if is_trading_day(d)}
                if ok:
                    df = df[df["time"].map(lambda t: pd.Timestamp(t).date() in ok)]
                else:
                    df = df.iloc[0:0]
            except Exception:
                pass
            return df.reset_index(drop=True)
        except Exception as e:
            try:
                self._log(f"⚠️ [RT15mDB] 조회 실패 ({nc}): {e}")
            except Exception:
                pass
            return pd.DataFrame()
