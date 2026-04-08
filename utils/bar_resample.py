# -*- coding: utf-8 -*-
"""bars_1m → bars_15m 재샘플 (CLI·배치용, 무거운 의존성 없음)."""
from __future__ import annotations

import sqlite3
from typing import Optional

import pandas as pd

from utils.normalization import normalize_code


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
            df_1m["datetime"] = pd.to_datetime(df_1m["datetime"])
            df_1m.set_index("datetime", inplace=True)
            df_15m = df_1m.resample("15min").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
            ).dropna()
            df_15m.reset_index(inplace=True)
            df_15m["datetime"] = df_15m["datetime"].dt.strftime("%Y-%m-%d %H:%M:%S")
            df_15m["code"] = code
            conn.execute("DELETE FROM bars_15m WHERE code = ?", (code,))
            df_15m.to_sql("bars_15m", conn, if_exists="append", index=False)
            conn.commit()
        return None
    except Exception as e:
        try:
            return str(e)
        except Exception:
            return "unknown error"
