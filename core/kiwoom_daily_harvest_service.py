from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence

import pandas as pd

from core.kiwoom_daily_bars import fetch_kiwoom_daily_ohlcv_dataframe
from reporting.krx_trading_calendar import required_eod_bar_date_asof_local


@dataclass
class DailyHarvestIntegrityResult:
    """일봉 하베스팅 후 DB 완결성 점검 결과."""

    total_codes: int
    ok_codes: int
    missing_codes: List[str]
    stale_codes: List[str]
    required_date: str

    @property
    def is_complete(self) -> bool:
        return not self.missing_codes and not self.stale_codes


class KiwoomDailyHarvestService:
    """
    감시 선정 직전 ``bars_1d`` 준비 + 완결성 점검.

    - 기본(``monitor_pre_selection_bars_1d_source`` = ``from_1m_aggregate``):
      ``bars_1m`` → ``rebuild_daily_bars_from_1m`` 로만 ``bars_1d`` 를 맞춤.
      동적 백테·``prepare_dynamic_universe`` 가 쓰는 일봉과 동일 파이프라인.
    - ``kiwoom_ka10081``: 키움 REST 일봉을 직접 저장(레거시·증권사 일봉 정의).
      이 경우 1분 집계 일봉과 수치가 달라질 수 있음.
    """

    def __init__(
        self,
        data_manager: Any,
        logger_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.data_manager = data_manager
        self.log = logger_callback or (lambda _msg: None)

    def _safe_log(self, msg: str) -> None:
        try:
            self.log(msg)
        except Exception:
            pass

    def harvest_daily_bars_for_codes(
        self,
        *,
        codes: Sequence[str],
        token: str,
        settings: Optional[Dict[str, Any]] = None,
        lookback_days: int = 365,
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> DailyHarvestIntegrityResult:
        """
        codes 전체에 대해 ``bars_1d`` 를 준비한 뒤 완결성을 반환.

        ``monitor_pre_selection_bars_1d_source`` 가 ``from_1m_aggregate``(기본)이면
        ``token`` 은 사용하지 않습니다.
        """
        cfg: Dict[str, Any] = settings or {}
        total = len(codes)
        if total == 0:
            req = required_eod_bar_date_asof_local(datetime.now()).strftime("%Y-%m-%d")
            return DailyHarvestIntegrityResult(0, 0, [], [], req)

        mode_raw = str(
            cfg.get("monitor_pre_selection_bars_1d_source", "from_1m_aggregate")
            or "from_1m_aggregate"
        ).strip().lower()
        use_kiwoom_daily = mode_raw in ("kiwoom_ka10081", "kiwoom", "ka10081")
        if not use_kiwoom_daily:
            return self._rebuild_bars_1d_from_1m_for_codes(
                codes=codes,
                cfg=cfg,
                progress_callback=progress_callback,
            )

        end_dt = datetime.now()
        start_dt = end_dt - timedelta(days=max(lookback_days, 120))
        min_date = start_dt.strftime("%Y-%m-%d")
        max_date = end_dt.strftime("%Y-%m-%d")
        upd_tp = str(cfg.get("kiwoom_daily_upd_stkpc_tp", "1") or "1").strip()
        # KODEX200 전체 순차 호출 시 429 완화: 설정값과 무관하게 하한을 둠.
        try:
            min_interval = float(cfg.get("kiwoom_daily_min_request_interval_sec", 1.15))
        except (TypeError, ValueError):
            min_interval = 1.15
        # 대량 순차 호출 시 429 방지 — 설정이 낮아도 하한 유지
        min_interval = max(min_interval, 0.95)
        try:
            max_429_retry = int(cfg.get("kiwoom_daily_max_429_retries_per_page", 8))
        except (TypeError, ValueError):
            max_429_retry = 8
        max_429_retry = max(max_429_retry, 4)
        try:
            max_pages = int(cfg.get("kiwoom_daily_max_pages", 30))
        except (TypeError, ValueError):
            max_pages = 30
        try:
            page_delay = float(cfg.get("kiwoom_daily_page_delay_sec", 0.38))
        except (TypeError, ValueError):
            page_delay = 0.38
        page_delay = max(page_delay, 0.22)
        try:
            sleep_between_codes = float(
                cfg.get("kiwoom_daily_harvest_sleep_between_codes_sec", 0.55)
            )
        except (TypeError, ValueError):
            sleep_between_codes = 0.55
        sleep_between_codes = max(sleep_between_codes, 0.0)

        self._safe_log(
            f"📥 [일봉 하베스팅] KODEX200 {total}종목 대상, {min_date}~{max_date} "
            f"(키움 ka10081, 요청간격≥{min_interval:.2f}s, 종목간격≥{sleep_between_codes:.2f}s)"
        )
        ok_count = 0
        for idx, code in enumerate(codes, start=1):
            ncode = str(code).strip().zfill(6)
            step_msg = f"{ncode} 일봉 수집 중..."
            if progress_callback is not None:
                try:
                    progress_callback(idx - 1, total, step_msg)
                except Exception:
                    pass
            try:
                df = fetch_kiwoom_daily_ohlcv_dataframe(
                    ncode,
                    token,
                    min_date=min_date,
                    max_date=max_date,
                    upd_stkpc_tp=upd_tp,
                    max_pages=max_pages,
                    page_delay_sec=page_delay,
                    min_request_interval_sec=min_interval,
                    max_429_retries_per_page=max_429_retry,
                    logger_callback=self.log,
                )
                if df is None or df.empty:
                    self._safe_log(f"  [Warn] {ncode} 일봉 수집 결과가 비어 있습니다.")
                    continue
                save_df = self._to_save_frame(df)
                if save_df.empty:
                    self._safe_log(f"  [Warn] {ncode} 일봉 정규화 결과가 비어 있습니다.")
                    continue
                self.data_manager.save_daily_bars(ncode, save_df)
                ok_count += 1
            except Exception as e:
                self._safe_log(f"  [Warn] {ncode} 일봉 하베스팅 실패: {e}")
            finally:
                if progress_callback is not None:
                    try:
                        progress_callback(idx, total, f"{ncode} 처리 완료 ({idx}/{total})")
                    except Exception:
                        pass
                if sleep_between_codes > 0 and idx < total:
                    try:
                        time.sleep(sleep_between_codes)
                    except Exception:
                        pass

        result = self.check_daily_integrity(codes)
        self._safe_log(
            f"✅ [일봉 하베스팅] 저장 성공 {ok_count}/{total}, "
            f"완결성 OK {result.ok_codes}/{result.total_codes}"
        )
        return result

    def _rebuild_bars_1d_from_1m_for_codes(
        self,
        *,
        codes: Sequence[str],
        cfg: Dict[str, Any],
        progress_callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> DailyHarvestIntegrityResult:
        """``bars_1m`` 전 구간 집계로 ``bars_1d`` 를 재작성(백테·선정 단일 소스)."""
        total = len(codes)
        try:
            sleep_between = float(
                cfg.get("monitor_pre_selection_rebuild_sleep_sec", 0.02)
            )
        except (TypeError, ValueError):
            sleep_between = 0.02
        sleep_between = max(sleep_between, 0.0)

        self._safe_log(
            f"📥 [일봉 준비] KODEX200 {total}종목 — bars_1m → bars_1d 재집계 "
            f"(동적 백테·선정 동일 파이프라인, 종목 간격 {sleep_between:.3f}s)"
        )
        ok_count = 0
        for idx, code in enumerate(codes, start=1):
            ncode = str(code).strip().zfill(6)
            if progress_callback is not None:
                try:
                    progress_callback(idx - 1, total, f"{ncode} 1m→1d 집계 중…")
                except Exception:
                    pass
            try:
                if self.data_manager.rebuild_daily_bars_from_1m(ncode):
                    ok_count += 1
                else:
                    self._safe_log(
                        f"  [Warn] {ncode} 1m→1d 재집계 실패(1분봉 없음 또는 집계 결과 없음)"
                    )
            except Exception as e:
                self._safe_log(f"  [Warn] {ncode} 1m→1d 재집계 예외: {e}")
            finally:
                if progress_callback is not None:
                    try:
                        progress_callback(
                            idx, total, f"{ncode} 처리 완료 ({idx}/{total})"
                        )
                    except Exception:
                        pass
                if sleep_between > 0 and idx < total:
                    try:
                        time.sleep(sleep_between)
                    except Exception:
                        pass

        result = self.check_daily_integrity(codes)
        self._safe_log(
            f"✅ [일봉 준비] 1m→1d 성공 {ok_count}/{total}, "
            f"완결성 OK {result.ok_codes}/{result.total_codes}"
        )
        return result

    def check_daily_integrity(self, codes: Sequence[str]) -> DailyHarvestIntegrityResult:
        """
        하베스팅 로직 관점의 완결성 점검:
        - bars_1d 가 존재하는지
        - 최신 일자가 required_eod_bar_date_asof_local 이상인지
        """
        required_dt = required_eod_bar_date_asof_local(datetime.now())
        required_s = required_dt.strftime("%Y-%m-%d")
        missing: List[str] = []
        stale: List[str] = []
        ok_count = 0
        for code in codes:
            ncode = str(code).strip().zfill(6)
            try:
                # get_daily_bars(..., limit=1) 은 ORDER BY ASC 이라 **최초** 일자만 온다.
                # 최신 장마감 반영 여부는 MAX(datetime) 기준으로 본다.
                with self.data_manager._get_connection() as conn:
                    row = conn.execute(
                        "SELECT MAX(datetime) FROM bars_1d WHERE code = ?",
                        (ncode,),
                    ).fetchone()
                raw_max = row[0] if row else None
                if not raw_max:
                    missing.append(ncode)
                    continue
                last_time = datetime.strptime(str(raw_max)[:10], "%Y-%m-%d").date()
                if last_time < required_dt:
                    stale.append(ncode)
                    continue
                ok_count += 1
            except Exception:
                missing.append(ncode)
        return DailyHarvestIntegrityResult(
            total_codes=len(codes),
            ok_codes=ok_count,
            missing_codes=missing,
            stale_codes=stale,
            required_date=required_s,
        )

    @staticmethod
    def _to_save_frame(df: pd.DataFrame) -> pd.DataFrame:
        """ka10081 DataFrame을 DataManagerV2.save_daily_bars 입력 형식으로 변환."""
        if df is None or df.empty:
            return pd.DataFrame()
        d = df.copy()
        cols = {str(c).lower(): c for c in d.columns}
        req_map = {
            "time": cols.get("time"),
            "open": cols.get("open"),
            "high": cols.get("high"),
            "low": cols.get("low"),
            "close": cols.get("close"),
            "volume": cols.get("volume"),
        }
        if any(v is None for v in req_map.values()):
            return pd.DataFrame()
        out = pd.DataFrame(
            {
                "datetime": pd.to_datetime(d[req_map["time"]]).dt.strftime("%Y-%m-%d"),
                "open": pd.to_numeric(d[req_map["open"]], errors="coerce").fillna(0.0),
                "high": pd.to_numeric(d[req_map["high"]], errors="coerce").fillna(0.0),
                "low": pd.to_numeric(d[req_map["low"]], errors="coerce").fillna(0.0),
                "close": pd.to_numeric(d[req_map["close"]], errors="coerce").fillna(0.0),
                "volume": pd.to_numeric(d[req_map["volume"]], errors="coerce").fillna(0.0),
            }
        )
        out = out.dropna(subset=["datetime"]).drop_duplicates(subset=["datetime"], keep="last")
        out = out.sort_values("datetime").reset_index(drop=True)
        return out
