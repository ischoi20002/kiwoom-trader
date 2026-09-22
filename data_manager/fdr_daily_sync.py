# -*- coding: utf-8 -*-
"""FDR 일봉 동기화 믹스인 (DataManagerV2)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Optional

import pandas as pd

from utils.normalization import normalize_code

# data_manager_v2.MARKET_INDEX_BAR_CODES 와 동일 (순환 import 방지용 로컬 정의)
MARKET_INDEX_BAR_CODES = (
    "001",
    "101",
    "^VIX",
    "KS11",
    "KQ11",
    "^KS11",
    "^KQ11",
)


class FdrDailySyncMixin:
    def _fdr_ticker_for_daily(self, code: str) -> Optional[str]:
        """FDR 일봉 조회용 티커 (지수 코드 매핑)."""
        try:
            kiwoom_code = normalize_code(code)
        except Exception:
            return None
        if kiwoom_code in MARKET_INDEX_BAR_CODES:
            return None
        mapping = {
            "001": "KS11",
            "101": "KQ11",
            "KS11": "KS11",
            "KQ11": "KQ11",
            "^VIX": "^VIX",
        }
        return mapping.get(kiwoom_code, kiwoom_code)


    def get_max_daily_bar_date_for_code(self, code: str) -> Optional[date]:
        """종목별 bars_1d 최신 일자 (YYYY-MM-DD)."""
        try:
            ncode = normalize_code(code)
        except Exception:
            return None
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT MAX(datetime) FROM bars_1d WHERE code = ?",
                    (ncode,),
                ).fetchone()
            s = row[0] if row and row[0] else None
            if not s:
                return None
            return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
        except Exception:
            return None


    def fetch_fdr_daily_dataframe(
        self,
        code: str,
        start_day: str,
        end_day: str,
    ) -> pd.DataFrame:
        """
        FDR 일봉 DataFrame (datetime, open, high, low, close, volume).
        실패·빈 결과 시 빈 DataFrame.
        """
        import FinanceDataReader as fdr

        empty = pd.DataFrame(
            columns=["datetime", "open", "high", "low", "close", "volume"]
        )
        fdr_code = self._fdr_ticker_for_daily(code)
        if not fdr_code:
            return empty
        try:
            start_s = str(start_day)[:10]
            end_s = str(end_day)[:10]
        except Exception:
            return empty
        try:
            raw = fdr.DataReader(fdr_code, start_s, end_s)
        except Exception as e:
            try:
                print(f"[DataManagerV2] FDR 일봉 조회 실패 {code} ({start_s}~{end_s}): {e}")
            except Exception:
                pass
            return empty
        if raw is None or raw.empty:
            return empty
        try:
            df = raw.reset_index()
            df.columns = [str(c).lower() for c in df.columns]
            if "date" in df.columns:
                df.rename(columns={"date": "datetime"}, inplace=True)
            elif "index" in df.columns:
                df.rename(columns={"index": "datetime"}, inplace=True)
            rename_map = {
                "시가": "open",
                "고가": "high",
                "저가": "low",
                "종가": "close",
                "거래량": "volume",
            }
            df.rename(columns=rename_map, inplace=True)
            for col in ("open", "high", "low", "close", "volume"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce").dt.strftime(
                "%Y-%m-%d"
            )
            df = df.dropna(subset=["datetime", "close"])
            if df.empty:
                return empty
            out_cols = ["datetime", "open", "high", "low", "close", "volume"]
            for c in out_cols:
                if c not in df.columns:
                    if c == "volume":
                        df[c] = 0
                    else:
                        df[c] = df["close"]
            return df[out_cols].copy()
        except Exception as e:
            try:
                print(f"[DataManagerV2] FDR 일봉 정규화 실패 {code}: {e}")
            except Exception:
                pass
            return empty


    def sync_daily_bars_from_fdr_range(
        self,
        code: str,
        start_day: str,
        end_day: str,
    ) -> bool:
        """FDR 일봉 구간을 bars_1d에 INSERT OR REPLACE."""
        try:
            df = self.fetch_fdr_daily_dataframe(code, start_day, end_day)
            if df is None or df.empty:
                return False
            self.save_daily_bars(code, df)
            return True
        except Exception as e:
            try:
                print(f"[DataManagerV2] sync_daily_bars_from_fdr_range {code}: {e}")
            except Exception:
                pass
            return False


    def sync_daily_bars_from_fdr(
        self,
        code: str,
        start_day: Optional[str] = None,
        end_day: Optional[str] = None,
    ) -> bool:
        """
        FDR → bars_1d 증분 동기화.
        start_day 미지정 시: 기존 일봉 MAX-1일 또는 (end-400일)부터.
        end_day 미지정 시: required_eod_bar_date_asof_local().
        """
        try:
            from reporting.krx_trading_calendar import required_eod_bar_date_asof_local

            if end_day:
                end_d = datetime.strptime(str(end_day)[:10], "%Y-%m-%d").date()
            else:
                end_d = required_eod_bar_date_asof_local()
            if start_day:
                start_d = datetime.strptime(str(start_day)[:10], "%Y-%m-%d").date()
            else:
                max_1d = self.get_max_daily_bar_date_for_code(code)
                if max_1d is not None:
                    start_d = max_1d - timedelta(days=1)
                else:
                    start_d = end_d - timedelta(days=400)
            if start_d > end_d:
                start_d = end_d
            return self.sync_daily_bars_from_fdr_range(
                code,
                start_d.strftime("%Y-%m-%d"),
                end_d.strftime("%Y-%m-%d"),
            )
        except Exception as e:
            try:
                print(f"[DataManagerV2] sync_daily_bars_from_fdr {code}: {e}")
            except Exception:
                pass
            return False


    def sync_daily_bars_from_fdr_to_required_eod(self, code: str) -> bool:
        """마지막 완결 거래일(EOD)까지 FDR 일봉을 bars_1d에 반영."""
        try:
            from reporting.krx_trading_calendar import required_eod_bar_date_asof_local

            req = required_eod_bar_date_asof_local()
            max_1d = self.get_max_daily_bar_date_for_code(code)
            if max_1d is not None and max_1d >= req:
                return True
            return self.sync_daily_bars_from_fdr(code, end_day=req.strftime("%Y-%m-%d"))
        except Exception as e:
            try:
                print(
                    f"[DataManagerV2] sync_daily_bars_from_fdr_to_required_eod {code}: {e}"
                )
            except Exception:
                pass
            return False


    def is_daily_bars_behind_required_eod(
        self,
        code: str,
        required: Optional[date] = None,
    ) -> bool:
        """bars_1d 최신일이 KRX 기준 마지막 완결 일봉보다 오래됐으면 True."""
        try:
            from reporting.krx_trading_calendar import required_eod_bar_date_asof_local

            req = required if required is not None else required_eod_bar_date_asof_local()
            max_1d = self.get_max_daily_bar_date_for_code(code)
            if max_1d is None:
                return True
            return max_1d < req
        except Exception:
            return True

