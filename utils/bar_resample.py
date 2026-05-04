# -*- coding: utf-8 -*-
"""bars_1m → bars_15m 재샘플, bars_1m → bars_1d 일봉 집계 (CLI·배치·하베스팅)."""
from __future__ import annotations

import sqlite3
from typing import Optional

import pandas as pd

from utils.krx_bars import resample_1m_ohlcv_to_15m_krx
from utils.normalization import normalize_code


def aggregate_daily_ohlcv_from_1m_dataframe(df_1m: pd.DataFrame) -> pd.DataFrame:
    """
    1분봉 DataFrame(datetime, open, high, low, close, volume)을
    캘린더 일자별 OHLCV로 집계한다. ``save_daily_bars``에 넣을 수 있게
    ``datetime`` 은 'YYYY-MM-DD' 문자열 열로 둔다.
    """
    if df_1m is None or df_1m.empty:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    try:
        d = df_1m.copy()
        d["datetime"] = pd.to_datetime(d["datetime"], errors="coerce")
        d = d.dropna(subset=["datetime"])
        if d.empty:
            return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        for c in ("open", "high", "low", "close", "volume"):
            d[c] = pd.to_numeric(d[c], errors="coerce")
        d = d.dropna(subset=["open", "high", "low", "close", "volume"])
        if d.empty:
            return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        d = d.sort_values("datetime")
        d["d"] = d["datetime"].dt.normalize()
        daily = (
            d.groupby("d", as_index=False)
            .agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
            )
        )
        daily["datetime"] = daily["d"].dt.strftime("%Y-%m-%d")
        out = daily[["datetime", "open", "high", "low", "close", "volume"]].copy()
        for c in ("open", "high", "low", "close", "volume"):
            out[c] = out[c].round(0).astype("int64")
        return out
    except (ValueError, TypeError, KeyError):
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])


def aggregate_daily_ohlcv_from_1m_db(db_path: str, code: str) -> pd.DataFrame:
    """
    SQLite ``bars_1m`` 에서 해당 종목의 전체 1분봉을 읽어 일봉 집계.
    DB 오류·빈 데이터 시 빈 DataFrame.
    """
    code = normalize_code(code)
    try:
        with sqlite3.connect(db_path, timeout=120) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            df_1m = pd.read_sql_query(
                "SELECT datetime, open, high, low, close, volume FROM bars_1m WHERE code = ?",
                conn,
                params=[code],
            )
    except (sqlite3.Error, OSError):
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    return aggregate_daily_ohlcv_from_1m_dataframe(df_1m)


def aggregate_daily_ohlcv_from_1m_db_range(
    db_path: str,
    code: str,
    start_day: str,
    end_day: str,
) -> pd.DataFrame:
    """
    SQLite ``bars_1m``에서 해당 종목의 (start_day~end_day) 범위만 읽어
    일봉(bars_1d)으로 집계합니다.

    주의사항
    --------
    - ``datetime``은 'YYYY-MM-DD HH:MM:SS' 문자열이며, 사전식 비교가 안전합니다.
    - 결과 ``datetime``은 일봉용 'YYYY-MM-DD' 문자열입니다.
    """
    code = normalize_code(code)
    try:
        s_day = str(start_day)[:10]
        e_day = str(end_day)[:10]
        start_ts = f"{s_day} 00:00:00"
        end_ts = f"{e_day} 23:59:59"

        with sqlite3.connect(db_path, timeout=120) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            df_1m = pd.read_sql_query(
                """
                SELECT datetime, open, high, low, close, volume
                FROM bars_1m
                WHERE code = ?
                  AND datetime >= ?
                  AND datetime <= ?
                """,
                conn,
                params=[code, start_ts, end_ts],
            )
    except (sqlite3.Error, OSError):
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])

    return aggregate_daily_ohlcv_from_1m_dataframe(df_1m)


def refresh_resampled_data_at_path(db_path: str, code: str) -> Optional[str]:
    """
    지정 경로 SQLite trading DB에서 한 종목의 bars_15m을 bars_1m 전체 구간 기준으로 재계산합니다.
    성공 시 None, 실패 시 오류 메시지 문자열 반환.
    """
    code = normalize_code(code)
    try:
        with sqlite3.connect(db_path, timeout=120) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            df_1m = pd.read_sql_query(
                "SELECT datetime, open, high, low, close, volume FROM bars_1m WHERE code = ?",
                conn,
                params=[code],
            )
            if df_1m.empty:
                return None
            df_1m["datetime"] = pd.to_datetime(df_1m["datetime"], errors="coerce")
            df_1m = df_1m.dropna(subset=["datetime"])
            for c in ("open", "high", "low", "close", "volume"):
                df_1m[c] = pd.to_numeric(df_1m[c], errors="coerce")
            df_1m = df_1m.dropna(subset=["open", "high", "low", "close", "volume"])
            if df_1m.empty:
                return None
            df_1m = df_1m.set_index("datetime").sort_index()
            df_15m = resample_1m_ohlcv_to_15m_krx(df_1m, code)
            if df_15m is None or len(df_15m) == 0:
                return None
            conn.execute("DELETE FROM bars_15m WHERE code = ?", (code,))
            df_15m.to_sql("bars_15m", conn, if_exists="append", index=False)
            conn.commit()
        return None
    except Exception as e:
        try:
            return str(e)
        except Exception:
            return "unknown error"


def refresh_resampled_data_at_path_range(
    db_path: str,
    code: str,
    start_day: str,
    end_day: str,
) -> Optional[str]:
    """
    trading DB의 ``bars_15m``을 'start_day~end_day' 날짜 구간에 대해서만
    bars_1m 기준으로 재계산합니다.
    """
    code = normalize_code(code)
    try:
        s_day = str(start_day)[:10]
        e_day = str(end_day)[:10]
        start_ts = f"{s_day} 00:00:00"
        end_ts = f"{e_day} 23:59:59"

        with sqlite3.connect(db_path, timeout=120) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")

            df_1m = pd.read_sql_query(
                """
                SELECT datetime, open, high, low, close, volume
                FROM bars_1m
                WHERE code = ?
                  AND datetime >= ?
                  AND datetime <= ?
                """,
                conn,
                params=[code, start_ts, end_ts],
            )

            if df_1m.empty:
                return None

            df_1m["datetime"] = pd.to_datetime(df_1m["datetime"], errors="coerce")
            df_1m = df_1m.dropna(subset=["datetime"])
            for c in ("open", "high", "low", "close", "volume"):
                df_1m[c] = pd.to_numeric(df_1m[c], errors="coerce")
            df_1m = df_1m.dropna(subset=["open", "high", "low", "close", "volume"])
            if df_1m.empty:
                return None

            df_1m = df_1m.set_index("datetime").sort_index()
            df_15m = resample_1m_ohlcv_to_15m_krx(df_1m, code)
            if df_15m is None or len(df_15m) == 0:
                return None

            conn.execute(
                """
                DELETE FROM bars_15m
                WHERE code = ?
                  AND datetime >= ?
                  AND datetime <= ?
                """,
                (code, start_ts, end_ts),
            )
            df_15m.to_sql("bars_15m", conn, if_exists="append", index=False)
            conn.commit()
        return None
    except Exception as e:  # noqa: BLE001
        try:
            return str(e)
        except Exception:
            return "unknown error"
