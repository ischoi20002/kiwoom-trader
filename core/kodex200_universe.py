# -*- coding: utf-8 -*-
"""
KODEX 200 유니버스 로딩.

``kodex200_universe_priority``:
- ``csv_only``: 루트 ``kodex200.csv`` 만 읽고 pykrx·원격 URL·FDR 폴백 없음(당분간 고정 리스트 유지용).
- ``etf_api_first`` / ``local_csv_first``: ETF(PDF)·URL·로컬 CSV·FDR 순 조합(아래 ``resolve_kodex200_universe`` 참고).
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from io import StringIO
from typing import Any, Callable, Mapping, Optional

import pandas as pd

# pykrx/거래소 응답에 따라 200 미만일 수 있어 완화(과도하게 짧은 리스트는 거부)
_MIN_KODEX200_ACCEPT: int = 175
# ETF PDF는 영업일·공시 시차로 당일이 비는 경우가 있어, 오늘부터 과거로 이 만큼 캘린더 일자 시도
_MAX_ETF_PDF_DATE_BACKDAYS: int = 60
# KODEX 코스피200 ETF(069500) ISIN — pykrx ``get_etx_isin`` 은 ETF 전종목 테이블(EtxTicker) 로딩에 의존하는데,
# 그 테이블이 비면 PDF 조회가 끊기므로 KRX PDF API 호출 시 ISIN 을 직접 사용한다.
_KODEX200_ETF_ISIN_FALLBACK: str = "KR7069500007"


def _log(log: Optional[Callable[..., None]], msg: str) -> None:
    try:
        if log is not None:
            log(msg)
    except Exception:
        pass


def _codes_to_name_dataframe(codes: list[str], stock: Any) -> pd.DataFrame:
    """6자리 종목코드 리스트 → Code/Name DataFrame."""
    names: list[str] = []
    for t in codes:
        try:
            names.append(stock.get_market_ticker_name(t) or t)
        except Exception:
            names.append(t)
    return pd.DataFrame({"Code": codes, "Name": names})


def _etf_pdf_to_code_name_df(tickers_df: pd.DataFrame, stock: Any) -> Optional[pd.DataFrame]:
    """pykrx ``get_etf_portfolio_deposit_file`` 결과 DataFrame → Code/Name. 종목 수 부족 시 None."""
    try:
        if tickers_df is None or getattr(tickers_df, "empty", True):
            return None
        tickers = tickers_df.index.tolist()
        tickers = [str(t).strip() for t in tickers if str(t).strip()]
        tickers = [t for t in tickers if len(t) == 6 and t.isdigit()]
        if len(tickers) < _MIN_KODEX200_ACCEPT:
            return None
        return _codes_to_name_dataframe(tickers, stock)
    except Exception:
        return None


def _krx_mdcstat05001_via_http(trd_dd: str, isin: str) -> Optional[pd.DataFrame]:
    """
    KRX MDCSTAT05001(ETF PDF) — pykrx ``Post`` 와 동일 엔드포인트이나
    최소 User-Agent 만으로는 응답이 비는 환경이 있어 브라우저에 가깝게 요청한다.
    """
    try:
        import requests
    except Exception:
        return None
    url = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
    bld = "dbms/MDC/STAT/standard/MDCSTAT05001"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://data.krx.co.kr",
        "Referer": (
            "https://data.krx.co.kr/contents/MDC/MDI/mdiLoader/index.cmd?menuId=MDC0201"
        ),
        "X-Requested-With": "XMLHttpRequest",
    }
    data = {"bld": bld, "trdDd": trd_dd, "isuCd": isin}
    try:
        sess = requests.Session()
        sess.headers.update(headers)
        try:
            sess.get("https://data.krx.co.kr/", timeout=12)
        except Exception:
            pass
        resp = sess.post(url, data=data, timeout=45)
        resp.raise_for_status()
        js = resp.json()
    except Exception:
        return None
    out = js.get("output")
    if not out:
        return None
    try:
        return pd.DataFrame(out)
    except Exception:
        return None


def _kodex200_etf_isin_resolved() -> str:
    """가능하면 pykrx EtxTicker 로 ISIN 을 쓰고, 실패 시 고정 ISIN."""
    try:
        from pykrx.website.krx.etx.ticker import get_etx_isin

        v = get_etx_isin("069500")
        if v is not None and str(v).strip():
            return str(v).strip()
    except Exception:
        pass
    return _KODEX200_ETF_ISIN_FALLBACK


def _krx_pdf_raw_to_kodex200_dataframe(
    raw: pd.DataFrame,
    stock: Any,
) -> Optional[pd.DataFrame]:
    """
    KRX MDCSTAT05001 원시 PDF 행 → Code/Name.

    ``pykrx.website.krx.etx.wrap.get_etf_portfolio_deposit_file`` 과 동일하게
    ``COMPST_ISU_CD`` 가 ISIN 혼합일 때 7~12자리 구간을 종목코드로 쓴다.
    종목명은 ``COMPST_ISU_NM`` 우선, 없으면 ``get_market_ticker_name``.
    """
    try:
        if raw is None or getattr(raw, "empty", True):
            return None
        if "COMPST_ISU_CD" not in raw.columns:
            return None
        codes: list[str] = []
        names: list[str] = []
        seen: set[str] = set()
        nm_col = "COMPST_ISU_NM" if "COMPST_ISU_NM" in raw.columns else None
        for _, row in raw.iterrows():
            try:
                val = str(row["COMPST_ISU_CD"]).strip()
                if not val or val.lower() == "nan":
                    continue
                if len(val) > 6:
                    val = val[3:9]
                if len(val) != 6 or not val.isdigit():
                    continue
                if val in seen:
                    continue
                seen.add(val)
                codes.append(val)
                nm = ""
                if nm_col is not None:
                    try:
                        nm = str(row[nm_col]).strip()
                    except Exception:
                        nm = ""
                if not nm or nm.lower() == "nan":
                    try:
                        nm = stock.get_market_ticker_name(val) or val
                    except Exception:
                        nm = val
                names.append(nm)
            except Exception:
                continue
        if len(codes) < _MIN_KODEX200_ACCEPT:
            return None
        return pd.DataFrame({"Code": codes, "Name": names})
    except Exception:
        return None


def _try_pykrx_kodex200_etf(
    log: Optional[Callable[..., None]] = None,
) -> Optional[pd.DataFrame]:
    """
    KODEX 200 ETF(069500) PDF/구성 종목 → Code, Name DataFrame.

    - ``stock.get_etf_portfolio_deposit_file`` 는 ``get_etx_isin`` → EtxTicker(ETF 전종목)
      에 의존한다. 전종목 테이블이 비면 ISIN 을 못 받아 **PDF 요청 전에 실패**한다.
    - 그래서 KRX ``PDF(MDCSTAT05001).fetch(trdDd, isin)`` 를 **ISIN 고정·동적 병행**으로
      직접 호출한다(``_kodex200_etf_isin_resolved``).
    - 영업일 자동 추정 없이 **YYYYMMDD** 를 오늘부터 과거로 시도한다.
    """
    try:
        from pykrx import stock
        from pykrx.website.krx.etx.core import PDF
    except Exception:
        return None

    isin = _kodex200_etf_isin_resolved()

    for i in range(_MAX_ETF_PDF_DATE_BACKDAYS):
        d = date.today() - timedelta(days=i)
        ds = d.strftime("%Y%m%d")
        raw: Optional[pd.DataFrame] = None
        try:
            raw = PDF().fetch(ds, isin)
        except Exception:
            raw = None
        if raw is None or getattr(raw, "empty", True):
            raw = _krx_mdcstat05001_via_http(ds, isin)
        out: Optional[pd.DataFrame] = None
        if raw is not None and not getattr(raw, "empty", True):
            out = _krx_pdf_raw_to_kodex200_dataframe(raw, stock)
        if out is None or out.empty:
            try:
                tickers_df = stock.get_etf_portfolio_deposit_file("069500", ds)
            except Exception:
                continue
            out = _etf_pdf_to_code_name_df(tickers_df, stock)
        if out is not None and not out.empty:
            if i > 0:
                _log(
                    log,
                    f"ℹ️ [KODEX200] ETF 구성표 기준일 {ds} "
                    f"(당일 PDF 없음 등으로 과거일 탐색, {i}일 전)",
                )
            return out
    return None


def _normalize_kodex200_holdings_df(
    df_csv: pd.DataFrame,
    *,
    min_rows: int,
    log: Optional[Callable[..., None]] = None,
    short_warn: str = "CSV",
) -> Optional[pd.DataFrame]:
    """kodex200.csv 동일 스키마 DataFrame → Code/Name. 행 수 부족 시 None."""
    try:
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
        if len(df_res) < min_rows:
            _log(
                log,
                f"⚠️ [KODEX200] {short_warn} 행 수가 부족합니다({len(df_res)}).",
            )
            return None
        return df_res
    except Exception as e:
        _log(log, f"⚠️ [KODEX200] {short_warn} 파싱 실패: {e}")
        return None


def _try_kodex200_constituents_url(
    settings: Mapping[str, Any],
    *,
    log: Optional[Callable[..., None]] = None,
) -> Optional[pd.DataFrame]:
    """
    ``settings['kodex200_constituents_url']`` 에서 CSV 를 받아 유니버스로 쓴다.
    로컬 ``kodex200.csv`` 와 동일 열(종목코드·종목명 또는 Code·Name).
    """
    try:
        url = str(settings.get("kodex200_constituents_url") or "").strip()
        if not url:
            return None
        import requests

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/csv,text/plain,*/*;q=0.9",
        }
        r = requests.get(url, headers=headers, timeout=45)
        r.raise_for_status()
        raw = r.content
        try:
            text = raw.decode("utf-8-sig")
        except Exception:
            text = raw.decode("cp949", errors="replace")
        df_csv = pd.read_csv(StringIO(text), dtype=str)
        out = _normalize_kodex200_holdings_df(
            df_csv, min_rows=190, log=log, short_warn="원격 CSV"
        )
        if out is not None and not out.empty:
            _log(log, "[KODEX200] 설정 kodex200_constituents_url 원격 CSV를 사용합니다.")
        return out
    except Exception as e:
        _log(log, f"⚠️ [KODEX200] 원격 CSV URL 실패: {e}")
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
        out = _normalize_kodex200_holdings_df(
            df_csv, min_rows=190, log=log, short_warn="로컬 kodex200.csv"
        )
        return out
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
        - ``csv_only``: 로컬 ``kodex200.csv`` 만 (외부 편입 조회·폴백 없음).
        - ``etf_api_first``: pykrx/KRX PDF(069500) → ``kodex200_constituents_url`` →
          로컬 ``kodex200.csv`` → FDR
        - ``local_csv_first``: 로컬 CSV → ETF 경로 → URL → FDR
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

    if priority in ("csv_only", "file_only", "local_csv_only", "kodex200_csv_only"):
        try:
            df_csv = _try_read_kodex200_csv(log=log)
            if df_csv is not None and not df_csv.empty:
                _log(
                    log,
                    "[KODEX200] kodex200.csv 전용 모드: ETF·원격·FDR 조회를 생략합니다.",
                )
                return df_csv
            _log(
                log,
                "⚠️ [KODEX200] kodex200.csv 가 없거나(또는 유효 행 수 부족) "
                "csv_only 모드에서는 폴백하지 않습니다. 빈 목록을 반환합니다.",
            )
            return pd.DataFrame()
        except Exception as e:
            _log(log, f"❌ [KODEX200] csv_only 로드 예외: {e}")
            return pd.DataFrame()

    etf_first = priority in ("etf_api_first", "etf", "pykrx", "auto", "live")

    def _chain_etf_then_csv_then_fdr() -> pd.DataFrame:
        df_etf = _try_pykrx_kodex200_etf(log=log)
        if df_etf is not None and not df_etf.empty:
            _log(
                log,
                f"[KODEX200] pykrx KODEX200 ETF(069500) 구성 {len(df_etf)}종목을 사용합니다.",
            )
            _maybe_export_kodex200_csv(df_etf, settings, log=log)
            return df_etf
        _log(
            log,
            "ℹ️ [KODEX200] ETF PDF·HTTP 조회 실패 또는 종목 수 부족 — "
            "원격 URL·로컬 kodex200.csv·FDR 순으로 시도합니다.",
        )
        df_url = _try_kodex200_constituents_url(settings, log=log)
        if df_url is not None and not df_url.empty:
            _maybe_export_kodex200_csv(df_url, settings, log=log)
            return df_url
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
        df_etf = _try_pykrx_kodex200_etf(log=log)
        if df_etf is not None and not df_etf.empty:
            _log(
                log,
                f"[KODEX200] pykrx KODEX200 ETF(069500) 구성 {len(df_etf)}종목을 사용합니다.",
            )
            _maybe_export_kodex200_csv(df_etf, settings, log=log)
            return df_etf
        df_url = _try_kodex200_constituents_url(settings, log=log)
        if df_url is not None and not df_url.empty:
            _maybe_export_kodex200_csv(df_url, settings, log=log)
            return df_url
        _log(log, "[KODEX200] FDR 코스피 시총 상위 200으로 폴백합니다.")
        return _fallback_fdr_kospi_top(200, log)

    try:
        if etf_first:
            return _chain_etf_then_csv_then_fdr()
        return _chain_csv_then_etf_then_fdr()
    except Exception as e:
        _log(log, f"❌ [KODEX200] 유니버스 확보 중 예외: {e}")
        return pd.DataFrame()
