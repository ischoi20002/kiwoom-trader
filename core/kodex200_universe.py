# -*- coding: utf-8 -*-
"""
KODEX 200 유니버스 로딩.

편입은 연 2회 등으로 바뀌므로, 수동 ``kodex200.csv`` 만 고집하면 구버전이 남습니다.
``kodex200_universe_priority`` 설정으로 **pykrx ETF(069500) 구성 자동 조회**를 최우선할 수 있습니다.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Mapping, Optional

import pandas as pd

# pykrx/거래소 응답에 따라 200 미만일 수 있어 완화(과도하게 짧은 리스트는 거부)
_MIN_KODEX200_ACCEPT: int = 175


def _log(log: Optional[Callable[..., None]], msg: str) -> None:
    try:
        if log is not None:
            log(msg)
    except Exception:
        pass


def _try_pykrx_kodex200_etf() -> Optional[pd.DataFrame]:
    """KODEX 200 ETF(069500) PDF/구성 종목 → Code, Name DataFrame."""
    try:
        from pykrx import stock

        tickers_df = stock.get_etf_portfolio_deposit_file("069500")
        if tickers_df is None or getattr(tickers_df, "empty", True):
            return None
        tickers = tickers_df.index.tolist()
        tickers = [str(t).strip() for t in tickers if str(t).strip()]
        tickers = [t for t in tickers if len(t) == 6 and t.isdigit()]
        if len(tickers) < _MIN_KODEX200_ACCEPT:
            return None
        names: list = []
        for t in tickers:
            try:
                names.append(stock.get_market_ticker_name(t) or t)
            except Exception:
                names.append(t)
        return pd.DataFrame({"Code": tickers, "Name": names})
    except Exception:
        return None


def _try_read_kodex200_csv(
    csv_path: Optional[str] = None,
    *,
    log: Optional[Callable[..., None]] = None,
) -> Optional[pd.DataFrame]:
    """프로젝트 루트 ``kodex200.csv`` (종목코드·종목명)."""
    path = csv_path or os.path.join(os.getcwd(), "kodex200.csv")
    if not os.path.isfile(path):
        return None
    try:
        try:
            df_csv = pd.read_csv(
                path, dtype={"종목코드": str, "Code": str}, encoding="utf-8-sig"
            )
        except Exception:
            df_csv = pd.read_csv(
                path, dtype={"종목코드": str, "Code": str}, encoding="cp949"
            )
        if "종목코드" in df_csv.columns and "종목명" in df_csv.columns:
            df_res = df_csv[["종목코드", "종목명"]].copy()
            df_res.columns = ["Code", "Name"]
        elif "Code" in df_csv.columns:
            nm = "Name" if "Name" in df_csv.columns else (
                "종목명" if "종목명" in df_csv.columns else None
            )
            if nm is None:
                return None
            df_res = df_csv[["Code", nm]].copy()
            df_res.columns = ["Code", "Name"]
        else:
            return None
        df_res["Code"] = df_res["Code"].astype(str).str.zfill(6)
        if len(df_res) < 190:
            _log(
                log,
                f"⚠️ [KODEX200] 로컬 CSV 행 수가 부족합니다({len(df_res)}). 다른 소스를 시도합니다.",
            )
            return None
        return df_res
    except Exception as e:
        _log(log, f"⚠️ [KODEX200] kodex200.csv 읽기 실패: {e}")
        return None


def _fallback_fdr_kospi_top(
    count: int, log: Optional[Callable[..., None]] = None
) -> pd.DataFrame:
    """최종 폴백: FDR 코스피 시총 상위."""
    try:
        import FinanceDataReader as fdr

        df = fdr.StockListing("KOSPI")
        df = df[df["Code"].astype(str).str.endswith("0")].sort_values(
            by="Marcap", ascending=False
        )
        return df[["Code", "Name"]].head(int(count)).copy()
    except Exception as e:
        _log(log, f"❌ [KODEX200] FDR 폴백 실패: {e}")
        return pd.DataFrame()


def _maybe_export_kodex200_csv(
    df: pd.DataFrame,
    settings: Mapping[str, Any],
    *,
    log: Optional[Callable[..., None]] = None,
) -> None:
    """ETF 기준으로 받은 유니버스를 레거시 CSV 형식으로 저장(선택)."""
    try:
        if not bool(settings.get("kodex200_export_csv_on_etf_success", False)):
            return
        if df is None or getattr(df, "empty", True) or len(df) < _MIN_KODEX200_ACCEPT:
            return
        out = os.path.join(os.getcwd(), "kodex200.csv")
        export = df.copy()
        export["종목코드"] = export["Code"].astype(str).str.zfill(6)
        export["종목명"] = export["Name"].astype(str)
        export[["종목코드", "종목명"]].to_csv(out, index=False, encoding="utf-8-sig")
        _log(
            log,
            f"[KODEX200] ETF 기준 유니버스를 kodex200.csv 에 저장했습니다 "
            f"(kodex200_export_csv_on_etf_success=True, {len(export)}종목).",
        )
    except Exception as e:
        _log(log, f"⚠️ [KODEX200] kodex200.csv 자동 저장 실패: {e}")


def resolve_kodex200_universe(
    settings: Mapping[str, Any],
    *,
    count: int = 200,
    log: Optional[Callable[..., None]] = None,
) -> pd.DataFrame:
    """
    KODEX200에 가까운 ``Code``/``Name`` 목록을 반환합니다.

    ``kodex200_universe_priority``:
        - ``etf_api_first`` (기본): pykrx 069500 구성 → 실패 시 kodex200.csv → FDR
        - ``local_csv_first``: kodex200.csv → ETF → FDR (기존과 동일한 우선순위)
    """
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 200
    if n != 200:
        _log(
            log,
            f"ℹ️ [KODEX200] count={n} 은 KODEX200 전용이 아니어 FDR 시총 상위로 채웁니다.",
        )
        return _fallback_fdr_kospi_top(n, log)

    try:
        priority = str(
            settings.get("kodex200_universe_priority", "etf_api_first") or ""
        ).strip().lower()
    except Exception:
        priority = "etf_api_first"

    etf_first = priority in ("etf_api_first", "etf", "pykrx", "auto", "live")

    def _chain_etf_then_csv_then_fdr() -> pd.DataFrame:
        df_etf = _try_pykrx_kodex200_etf()
        if df_etf is not None and not df_etf.empty:
            _log(
                log,
                f"[KODEX200] pykrx KODEX200 ETF(069500) 구성 {len(df_etf)}종목을 사용합니다.",
            )
            _maybe_export_kodex200_csv(df_etf, settings, log=log)
            return df_etf
        _log(
            log,
            "ℹ️ [KODEX200] ETF 구성 조회 실패 또는 종목 수 부족 — kodex200.csv 또는 FDR을 시도합니다.",
        )
        df_csv = _try_read_kodex200_csv(log=log)
        if df_csv is not None and not df_csv.empty:
            _log(log, "[KODEX200] 로컬 kodex200.csv 를 사용합니다.")
            return df_csv
        _log(log, "[KODEX200] FDR 코스피 시총 상위 200으로 폴백합니다.")
        return _fallback_fdr_kospi_top(200, log)

    def _chain_csv_then_etf_then_fdr() -> pd.DataFrame:
        df_csv = _try_read_kodex200_csv(log=log)
        if df_csv is not None and not df_csv.empty:
            _log(log, "[KODEX200] 로컬 kodex200.csv 를 사용합니다.")
            return df_csv
        df_etf = _try_pykrx_kodex200_etf()
        if df_etf is not None and not df_etf.empty:
            _log(
                log,
                f"[KODEX200] pykrx ETF(069500) 구성 {len(df_etf)}종목을 사용합니다.",
            )
            _maybe_export_kodex200_csv(df_etf, settings, log=log)
            return df_etf
        _log(log, "[KODEX200] FDR 코스피 시총 상위 200으로 폴백합니다.")
        return _fallback_fdr_kospi_top(200, log)

    try:
        if etf_first:
            return _chain_etf_then_csv_then_fdr()
        return _chain_csv_then_etf_then_fdr()
    except Exception as e:
        _log(log, f"❌ [KODEX200] 유니버스 확보 중 예외: {e}")
        return pd.DataFrame()
