# -*- coding: utf-8 -*-
"""영업일 캘린더·메인 DB 유지보수 믹스인."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, List, Optional, Set, Tuple

import sqlite3

import pandas as pd

from utils.normalization import normalize_code

from data_manager.fdr_daily_sync import MARKET_INDEX_BAR_CODES

# data_manager_v2 와 동일 (순환 import 방지)
_CALENDAR_REFERENCE_STOCK_CODES = (
    "005930",
    "000660",
    "005380",
    "035420",
    "051910",
)


class TradingDaysMaintenanceMixin:
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

