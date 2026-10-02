# -*- coding: utf-8 -*-
"""FDR 일봉 동기화 믹스인 (DataManagerV2)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional

import pandas as pd

from utils.normalization import normalize_code

# 증분 동기화 시 기존 일봉과 겹쳐 받아 비교할 달력일 수 (FDR 수정주가 반영 지연 대비)
ADJ_VERIFY_OVERLAP_DAYS = 30
# DB 종가와 FDR 종가의 상대 오차가 이 값을 넘으면 수정주가 이벤트로 판단
ADJ_CLOSE_REL_TOL = 0.0005
# 저가주에서 FDR 수정주가 반올림(1원) 차이는 이벤트로 보지 않음
ADJ_CLOSE_ABS_TOL = 1.0

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


    def _get_daily_bar_date_bounds(self, code: str) -> tuple[Optional[str], Optional[str]]:
        """종목별 bars_1d (최초일, 최신일) 문자열. 없으면 (None, None)."""
        try:
            ncode = normalize_code(code)
            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT MIN(datetime), MAX(datetime) FROM bars_1d WHERE code = ?",
                    (ncode,),
                ).fetchone()
            if not row or not row[0]:
                return None, None
            return str(row[0])[:10], str(row[1])[:10]
        except Exception:
            return None, None


    def detect_daily_adjustment_mismatch(
        self,
        code: str,
        fdr_df: pd.DataFrame,
        rel_tol: float = ADJ_CLOSE_REL_TOL,
    ) -> Optional[Dict[str, Any]]:
        """
        FDR 일봉과 기존 bars_1d 를 겹치는 날짜끼리 종가 비교.

        FDR 은 배당·분할 등 이벤트 후 과거 가격을 소급 수정하므로, 겹치는 날의
        종가가 다르면 DB 과거 구간이 수정 전 주가로 남아 있다는 뜻이다.
        불일치 시 가장 오차가 큰 날 정보를 dict 로, 일치하거나 비교 불가면 None.
        """
        try:
            if fdr_df is None or fdr_df.empty:
                return None
            ncode = normalize_code(code)
            days = fdr_df["datetime"].astype(str).str[:10]
            lo, hi = str(days.min()), str(days.max())
            with self._get_connection() as conn:
                db = pd.read_sql_query(
                    "SELECT datetime, close FROM bars_1d WHERE code = ? AND datetime BETWEEN ? AND ?",
                    conn,
                    params=(ncode, lo, hi),
                )
            if db.empty:
                return None
            db["datetime"] = db["datetime"].astype(str).str[:10]
            f = pd.DataFrame({"datetime": days.values, "fdr": pd.to_numeric(fdr_df["close"], errors="coerce").values})
            m = db.merge(f, on="datetime", how="inner")
            m["close"] = pd.to_numeric(m["close"], errors="coerce")
            m = m[(m["close"] > 0) & (m["fdr"] > 0)]
            if m.empty:
                return None
            diff = (m["close"] - m["fdr"]).abs()
            m["rel"] = diff / m["fdr"]
            bad = m[(m["rel"] > float(rel_tol)) & (diff > ADJ_CLOSE_ABS_TOL)]
            if bad.empty:
                return None
            worst = bad.loc[bad["rel"].idxmax()]
            return {
                "date": str(worst["datetime"]),
                "db_close": float(worst["close"]),
                "fdr_close": float(worst["fdr"]),
                "rel": float(worst["rel"]),
                "n_mismatch": int(len(bad)),
                "n_compared": int(len(m)),
            }
        except Exception as e:
            try:
                print(f"[DataManagerV2] 수정주가 비교 실패 {code}: {e}")
            except Exception:
                pass
            return None


    def _resave_full_daily_history_from_fdr(self, code: str, end_day: str) -> bool:
        """
        bars_1d 최초일부터 end_day 까지 FDR(수정주가)로 다시 받아 덮어쓴다.
        FDR 이 DB 최초일까지 내려주지 못하면 저장하지 않고 False (부분 덮어쓰기로 새 불연속이 생기지 않게).
        """
        try:
            db_min, _ = self._get_daily_bar_date_bounds(code)
            if not db_min:
                return False
            full = self.fetch_fdr_daily_dataframe(code, db_min, end_day)
            if full is None or full.empty:
                return False
            fdr_min = str(full["datetime"].astype(str).str[:10].min())
            # 상장 첫날 등 휴장 차이 허용: 최초일 기준 7일 이내면 전 구간 커버로 간주
            gap = (
                datetime.strptime(fdr_min, "%Y-%m-%d") - datetime.strptime(db_min, "%Y-%m-%d")
            ).days
            if gap > 7:
                try:
                    print(
                        f"[DataManagerV2] {code} FDR 전체 이력이 DB 최초일({db_min})까지 없음"
                        f"(FDR 최초 {fdr_min}) → 수정주가 재저장 보류"
                    )
                except Exception:
                    pass
                return False
            self.save_daily_bars(code, full)
            return True
        except Exception as e:
            try:
                print(f"[DataManagerV2] {code} 수정주가 전체 재저장 실패: {e}")
            except Exception:
                pass
            return False


    def sync_daily_bars_from_fdr_range(
        self,
        code: str,
        start_day: str,
        end_day: str,
        verify_adjustment: bool = True,
    ) -> bool:
        """
        FDR 일봉 구간을 bars_1d에 INSERT OR REPLACE.

        ``verify_adjustment`` 이면 기존 일봉과 겹치는 날 종가를 비교해 수정주가 이벤트를
        감지하고, 감지 시 DB 최초일부터 전체를 FDR 로 다시 저장한다(수정 전/후 주가 혼재 방지).
        전체 재저장에 실패하면 겹치는 구간은 덮어쓰지 않고 새 날짜만 저장해 다음 동기화 때 재감지한다.
        """
        try:
            df = self.fetch_fdr_daily_dataframe(code, start_day, end_day)
            if df is None or df.empty:
                return False
            mismatch = self.detect_daily_adjustment_mismatch(code, df) if verify_adjustment else None
            if not mismatch:
                self.save_daily_bars(code, df)
                return True
            try:
                print(
                    f"[DataManagerV2] {code} 수정주가 변경 감지 ({mismatch['date']} "
                    f"DB {mismatch['db_close']:,.0f} vs FDR {mismatch['fdr_close']:,.0f}, "
                    f"{mismatch['n_mismatch']}/{mismatch['n_compared']}일 불일치) → 전체 일봉 재저장"
                )
            except Exception:
                pass
            if self._resave_full_daily_history_from_fdr(code, end_day):
                return True
            _, db_max = self._get_daily_bar_date_bounds(code)
            new_rows = df[df["datetime"].astype(str).str[:10] > str(db_max)] if db_max else df
            if not new_rows.empty:
                self.save_daily_bars(code, new_rows)
            return True
        except Exception as e:
            try:
                print(f"[DataManagerV2] sync_daily_bars_from_fdr_range {code}: {e}")
            except Exception:
                pass
            return False


    def repair_daily_adjustment_for_code(
        self,
        code: str,
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """
        bars_1d 전 구간을 FDR 과 비교해 수정 전/후 주가가 섞였으면 전체 재저장 (일괄 정리용).
        반환: {"code", "status": ok|mismatch|repaired|repair_failed|no_db|no_fdr|error, ...}
        """
        out: Dict[str, Any] = {"code": str(code)}
        try:
            db_min, db_max = self._get_daily_bar_date_bounds(code)
            if not db_min or not db_max:
                out["status"] = "no_db"
                return out
            fdr_df = self.fetch_fdr_daily_dataframe(code, db_min, db_max)
            if fdr_df is None or fdr_df.empty:
                out["status"] = "no_fdr"
                return out
            mismatch = self.detect_daily_adjustment_mismatch(code, fdr_df)
            if not mismatch:
                out["status"] = "ok"
                return out
            out.update(mismatch)
            if dry_run:
                out["status"] = "mismatch"
                return out
            out["status"] = (
                "repaired" if self._resave_full_daily_history_from_fdr(code, db_max) else "repair_failed"
            )
            return out
        except Exception as e:
            out["status"] = "error"
            out["error"] = str(e)
            return out


    def sync_daily_bars_from_fdr(
        self,
        code: str,
        start_day: Optional[str] = None,
        end_day: Optional[str] = None,
    ) -> bool:
        """
        FDR → bars_1d 증분 동기화.
        start_day 미지정 시: 기존 일봉 MAX-ADJ_VERIFY_OVERLAP_DAYS일(수정주가 비교 구간) 또는 (end-400일)부터.
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
                    start_d = max_1d - timedelta(days=ADJ_VERIFY_OVERLAP_DAYS)
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

