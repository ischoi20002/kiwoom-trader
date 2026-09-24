# -*- coding: utf-8 -*-
"""공식 상장·KOSPI 유니버스 리스팅 믹스인."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Set

import os
import sqlite3

import pandas as pd

from utils.normalization import normalize_code

class UniverseListingMixin:
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


    def get_kospi_list(self, count=200, force=False, settings=None, universe_log=None):
        """
        KOSPI 시총 상위 유니버스 확보 (KODEX200 구성 유니버스 폐기).

        count<=200: ``kospi_universe.csv`` / marcap as-of
        """
        import os

        import FinanceDataReader as fdr
        import pandas as pd

        try:
            from strategy.universe_codes import normalize_universe_index_choice

            # KODEX200 구성 유니버스 폐기 — 항상 KOSPI200
            _ = normalize_universe_index_choice(
                (settings or {}).get("universe_index_choice")
                if isinstance(settings, dict)
                else None
            )
        except Exception:
            pass

        kospi_top_n = 200
        effective_count = int(count)
        try:
            if isinstance(settings, dict):
                kospi_top_n = int(settings.get("kospi_top_n", 200))
        except Exception:
            kospi_top_n = 200
        effective_count = min(effective_count, kospi_top_n)

        cache_file = (
            "kospi_universe.csv" if count <= 200 else f"kospi_top_{count}.csv"
        )

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
                # kospi_universe_refresh 주기(기본 분기) marcap as-of → kospi_universe.csv
                try:
                    _rmode = "quarterly"
                    try:
                        if isinstance(settings, dict):
                            _rmode = str(
                                settings.get("kospi_universe_refresh", "quarterly")
                            )
                    except Exception:
                        _rmode = "quarterly"
                    if force:
                        from core.marcap_universe import refresh_kospi_universe_csv_from_marcap

                        refresh_kospi_universe_csv_from_marcap(
                            kospi_universe_path=cache_file,
                            target_n=max(400, effective_count),
                            logger_callback=_ulog if universe_log is not None else None,
                            refresh_mode=_rmode,
                        )
                    else:
                        from core.marcap_universe import ensure_realtime_kospi_universe_csv

                        ensure_realtime_kospi_universe_csv(
                            kospi_universe_path=cache_file,
                            target_n=max(400, effective_count),
                            logger_callback=_ulog if universe_log is not None else None,
                            refresh_mode=_rmode,
                        )
                    if os.path.exists(cache_file) and os.path.getsize(cache_file) > 10:
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

