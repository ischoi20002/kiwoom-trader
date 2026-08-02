# -*- coding: utf-8 -*-
"""
FinanceData marcap 기반 KOSPI 시총 상위 N 유니버스 (과거 시점 PIT).

- 연도별 ``marcap-YYYY.parquet`` 온디맨드 다운로드 → ``data/marcap/`` 캐시
- 월별 topN → ``data/universe_marcap_monthly/`` CSV 캐시
"""
from __future__ import annotations

import io
import logging
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, Tuple

import pandas as pd

# 프로젝트 루트 기준 상대 경로
_MARCAP_CACHE_DIR = Path("data") / "marcap"
_MONTHLY_CACHE_DIR = Path("data") / "universe_marcap_monthly"
_WEEKLY_CACHE_DIR = Path("data") / "universe_marcap_weekly"
_BIWEEKLY_CACHE_DIR = Path("data") / "universe_marcap_biweekly"
_BIMONTHLY_CACHE_DIR = Path("data") / "universe_marcap_bimonthly"
_QUARTERLY_CACHE_DIR = Path("data") / "universe_marcap_quarterly"
_DAILY_CACHE_DIR = Path("data") / "universe_marcap_daily"
_MARCAP_RAW_URL = (
    "https://raw.githubusercontent.com/FinanceData/marcap/master/data/marcap-{year}.parquet"
)

_year_df_cache: dict[int, pd.DataFrame] = {}


def _log_cb(
    logger_callback: Optional[Callable[[str], None]],
    msg: str,
) -> None:
    try:
        if logger_callback is not None:
            logger_callback(msg)
    except Exception:
        pass


def marcap_year_path(year: int) -> Path:
    """연도 parquet 로컬 캐시 경로."""
    return _MARCAP_CACHE_DIR / f"marcap-{int(year)}.parquet"


def ensure_marcap_year(
    year: int,
    *,
    logger_callback: Optional[Callable[[str], None]] = None,
    max_cache_age_days: Optional[int] = None,
) -> Optional[Path]:
    """연도 marcap parquet 를 확보한다. 성공 시 경로, 실패 시 None."""
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None

    out = marcap_year_path(y)
    stale = False
    try:
        if out.is_file() and out.stat().st_size > 1000:
            if (
                max_cache_age_days is not None
                and y == datetime.now().year
            ):
                try:
                    mtime = datetime.fromtimestamp(out.stat().st_mtime)
                    if (datetime.now() - mtime).days >= int(max_cache_age_days):
                        stale = True
                        _log_cb(
                            logger_callback,
                            f"  [Marcap] {y}년 캐시 만료 → 재다운로드",
                        )
                except OSError:
                    pass
            if not stale:
                return out
    except OSError:
        pass

    if stale:
        _year_df_cache.pop(y, None)

    url = _MARCAP_RAW_URL.format(year=y)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        _log_cb(logger_callback, f"  [Marcap] {y}년 데이터 다운로드 중…")
        with urllib.request.urlopen(url, timeout=120) as resp:
            data = resp.read()
        if not data:
            return None
        out.write_bytes(data)
        _log_cb(logger_callback, f"  [Marcap] {y}년 캐시 저장: {out}")
        return out
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        try:
            logging.warning("marcap 연도 다운로드 실패 %s: %s", y, e, exc_info=True)
        except Exception:
            pass
        _log_cb(logger_callback, f"  [Warn] marcap {y}년 다운로드 실패: {e}")
        return None


def _load_marcap_year_df(
    year: int,
    *,
    logger_callback: Optional[Callable[[str], None]] = None,
) -> pd.DataFrame:
    """연도 marcap DataFrame (메모리 캐시)."""
    y = int(year)
    if y in _year_df_cache:
        return _year_df_cache[y]

    path = ensure_marcap_year(y, logger_callback=logger_callback)
    if path is None or not path.is_file():
        _year_df_cache[y] = pd.DataFrame()
        return _year_df_cache[y]

    try:
        df = None
        last_err: Optional[BaseException] = None
        for engine in ("pyarrow", "fastparquet"):
            try:
                df = pd.read_parquet(path, engine=engine)
                last_err = None
                break
            except Exception as e:
                last_err = e
                continue
        if df is None:
            raise last_err if last_err is not None else RuntimeError("parquet 엔진 없음")
    except Exception as e:
        try:
            # DLL 정책 차단 등은 traceback 전체 출력 대신 한 줄로
            logging.warning("marcap parquet 읽기 실패 %s: %s", path, e)
        except Exception:
            pass
        _year_df_cache[y] = pd.DataFrame()
        return _year_df_cache[y]

    if "Date" not in df.columns:
        _year_df_cache[y] = pd.DataFrame()
        return _year_df_cache[y]

    try:
        df = df.copy()
        df["Date"] = pd.to_datetime(df["Date"]).dt.normalize()
        if "Code" in df.columns:
            df["Code"] = df["Code"].astype(str).str.zfill(6)
    except Exception:
        _year_df_cache[y] = pd.DataFrame()
        return _year_df_cache[y]

    _year_df_cache[y] = df
    return df


def _normalize_asof(asof: date | datetime | str | pd.Timestamp) -> pd.Timestamp:
    return pd.to_datetime(asof).normalize()


def _filter_kospi_common(df: pd.DataFrame) -> pd.DataFrame:
    """KOSPI 보통주(코드 끝 0) 필터."""
    if df.empty:
        return df
    out = df.copy()
    if "MarketId" in out.columns:
        out = out[out["MarketId"].astype(str).str.upper() == "STK"]
    elif "Market" in out.columns:
        out = out[out["Market"].astype(str).str.upper().str.contains("KOSPI", na=False)]
    if "Code" in out.columns:
        out["Code"] = out["Code"].astype(str).str.zfill(6)
        out = out[out["Code"].str.endswith("0")]
    return out


def _top_n_from_day_df(day_df: pd.DataFrame, top_n: int) -> pd.DataFrame:
    """일자 스냅샷에서 시총 상위 N."""
    if day_df.empty:
        return pd.DataFrame(columns=["Code", "Name", "Marcap", "Rank"])
    stk = _filter_kospi_common(day_df)
    if stk.empty:
        return pd.DataFrame(columns=["Code", "Name", "Marcap", "Rank"])
    if "Marcap" in stk.columns:
        stk = stk.sort_values("Marcap", ascending=False)
    elif "Rank" in stk.columns:
        stk = stk.sort_values("Rank", ascending=True)
    cols = [c for c in ("Code", "Name", "Marcap", "Rank") if c in stk.columns]
    return stk[cols].head(int(top_n)).copy()


def load_marcap_day(
    asof: date | datetime | str | pd.Timestamp,
    *,
    logger_callback: Optional[Callable[[str], None]] = None,
    lookback_days: int = 7,
) -> Tuple[pd.DataFrame, Optional[pd.Timestamp]]:
    """
    as-of 일자(또는 직전 거래일) marcap 전종목 스냅샷.

    Returns
    -------
    (day_df, actual_asof)
    """
    target = _normalize_asof(asof)
    for offset in range(max(1, int(lookback_days))):
        check = target - pd.Timedelta(days=offset)
        df_y = _load_marcap_year_df(check.year, logger_callback=logger_callback)
        if df_y.empty:
            continue
        day = df_y[df_y["Date"] == check]
        if not day.empty:
            return day, check
    return pd.DataFrame(), None


def load_kospi_top_n_asof(
    asof: date | datetime | str | pd.Timestamp,
    top_n: int,
    *,
    logger_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[list[str], Optional[str], pd.DataFrame]:
    """
    as-of KOSPI 시총 상위 N 종목코드.

    Returns
    -------
    (codes, asof_str, detail_df)
    """
    try:
        n = max(1, int(top_n))
    except (TypeError, ValueError):
        n = 150

    day_df, actual = load_marcap_day(asof, logger_callback=logger_callback)
    detail = _top_n_from_day_df(day_df, n)
    if detail.empty or actual is None:
        return [], None, detail

    codes: list[str] = []
    for c in detail["Code"].tolist():
        s = str(c).strip().zfill(6)
        if len(s) == 6 and s.isdigit():
            codes.append(s)
    asof_str = actual.strftime("%Y-%m-%d")
    return codes, asof_str, detail


def _monthly_cache_path(year_month: str, top_n: int) -> Path:
    ym = str(year_month).replace("-", "")[:6]
    return _MONTHLY_CACHE_DIR / f"kospi_top{int(top_n)}_{ym}.csv"


def _first_month_asof(year_month: str) -> pd.Timestamp:
    """YYYYMM → 해당 월 첫 영업일(월~금) 추정."""
    ym = str(year_month).replace("-", "")[:6]
    try:
        start = pd.Timestamp(f"{ym[:4]}-{ym[4:6]}-01")
    except Exception:
        start = pd.Timestamp.now().normalize().replace(day=1)
    for offset in range(12):
        d = start + pd.Timedelta(days=offset)
        if d.month != start.month:
            break
        if d.weekday() < 5:
            return d
    return start


def get_monthly_kospi_top_n(
    year_month: str | pd.Timestamp | datetime,
    top_n: int,
    *,
    logger_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[list[str], Optional[str]]:
    """
    월별 KOSPI 시총 상위 N (월 첫 영업일 as-of, 로컬 CSV 캐시).

    Returns
    -------
    (codes, asof_date_str)
    """
    try:
        n = max(1, int(top_n))
    except (TypeError, ValueError):
        n = 150

    if isinstance(year_month, (datetime, pd.Timestamp)):
        ym = pd.Timestamp(year_month).strftime("%Y%m")
    else:
        ym = str(year_month).replace("-", "")[:6]

    cache_path = _monthly_cache_path(ym, n)
    if cache_path.is_file():
        try:
            cached = pd.read_csv(cache_path, dtype=str)
            if "Code" in cached.columns and len(cached) >= min(n, 10):
                codes = [
                    str(c).strip().zfill(6)
                    for c in cached["Code"].tolist()
                    if str(c).strip().isdigit()
                ]
                asof_val = None
                if "asof_date" in cached.columns and len(cached):
                    asof_val = str(cached["asof_date"].iloc[0])
                return codes[:n], asof_val
        except Exception as e:
            try:
                logging.info("월별 marcap 캐시 읽기 재시도: %s", e)
            except Exception:
                pass

    asof_guess = _first_month_asof(ym)
    codes, asof_str, detail = load_kospi_top_n_asof(
        asof_guess, n, logger_callback=logger_callback
    )
    if not codes:
        return [], None

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        out = detail.copy()
        if "Name" not in out.columns:
            out["Name"] = out["Code"]
        out["asof_date"] = asof_str or asof_guess.strftime("%Y-%m-%d")
        out[["Code", "Name", "asof_date"]].to_csv(
            cache_path, index=False, encoding="utf-8-sig"
        )
        _log_cb(
            logger_callback,
            f"  [Marcap] {ym} 월 유니버스 캐시 ({asof_str}, top{n}, {len(codes)}종목)",
        )
    except Exception as e:
        try:
            logging.warning("월별 marcap 캐시 저장 실패: %s", e, exc_info=True)
        except Exception:
            pass

    return codes, asof_str


def _weekly_cache_path(year_week: str, top_n: int) -> Path:
    yw = str(year_week).replace("-", "").upper()
    return _WEEKLY_CACHE_DIR / f"kospi_top{int(top_n)}_{yw}.csv"


def _iso_week_key(asof: date | datetime | str | pd.Timestamp) -> str:
    """ISO 연·주 키 (예: 2025W22)."""
    ts = _normalize_asof(asof)
    iso = ts.isocalendar()
    return f"{int(iso.year)}W{int(iso.week):02d}"


def _first_week_asof(asof: date | datetime | str | pd.Timestamp) -> pd.Timestamp:
    """해당 주의 첫 영업일(월~금) — 보통 월요일."""
    ts = _normalize_asof(asof)
    monday = ts - pd.Timedelta(days=int(ts.weekday()))
    for offset in range(5):
        d = monday + pd.Timedelta(days=offset)
        if d.weekday() < 5:
            return d
    return monday


def get_weekly_kospi_top_n(
    asof: date | datetime | str | pd.Timestamp,
    top_n: int,
    *,
    logger_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[list[str], Optional[str]]:
    """
    주별 KOSPI 시총 상위 N (해당 주 첫 영업일 as-of, 로컬 CSV 캐시).

    Returns
    -------
    (codes, asof_date_str)
    """
    try:
        n = max(1, int(top_n))
    except (TypeError, ValueError):
        n = 150

    try:
        yw = _iso_week_key(asof)
    except Exception:
        return [], None

    cache_path = _weekly_cache_path(yw, n)
    if cache_path.is_file():
        try:
            cached = pd.read_csv(cache_path, dtype=str)
            if "Code" in cached.columns and len(cached) >= min(n, 10):
                codes = [
                    str(c).strip().zfill(6)
                    for c in cached["Code"].tolist()
                    if str(c).strip().isdigit()
                ]
                asof_val = None
                if "asof_date" in cached.columns and len(cached):
                    asof_val = str(cached["asof_date"].iloc[0])
                return codes[:n], asof_val
        except Exception as e:
            try:
                logging.info("주별 marcap 캐시 읽기 재시도: %s", e)
            except Exception:
                pass

    asof_guess = _first_week_asof(asof)
    codes, asof_str, detail = load_kospi_top_n_asof(
        asof_guess, n, logger_callback=logger_callback
    )
    if not codes:
        return [], None

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        out = detail.copy()
        if "Name" not in out.columns:
            out["Name"] = out["Code"]
        out["asof_date"] = asof_str or asof_guess.strftime("%Y-%m-%d")
        out[["Code", "Name", "asof_date"]].to_csv(
            cache_path, index=False, encoding="utf-8-sig"
        )
        _log_cb(
            logger_callback,
            f"  [Marcap] {yw} 주 유니버스 캐시 ({asof_str}, top{n}, {len(codes)}종목)",
        )
    except Exception as e:
        try:
            logging.warning("주별 marcap 캐시 저장 실패: %s", e, exc_info=True)
        except Exception:
            pass

    return codes, asof_str


def _biweekly_cache_path(period_key: str, top_n: int) -> Path:
    return _BIWEEKLY_CACHE_DIR / f"kospi_top{int(top_n)}_{period_key}.csv"


def _bimonthly_cache_path(period_key: str, top_n: int) -> Path:
    return _BIMONTHLY_CACHE_DIR / f"kospi_top{int(top_n)}_{period_key}.csv"


def _quarterly_cache_path(period_key: str, top_n: int) -> Path:
    return _QUARTERLY_CACHE_DIR / f"kospi_top{int(top_n)}_{period_key}.csv"


def _biweekly_key(asof: date | datetime | str | pd.Timestamp) -> str:
    """ISO 연·격주 키 (예: 2025BW11 — week//2)."""
    ts = _normalize_asof(asof)
    iso = ts.isocalendar()
    fortnight = (int(iso.week) - 1) // 2
    return f"{int(iso.year)}BW{fortnight:02d}"


def _first_biweek_asof(asof: date | datetime | str | pd.Timestamp) -> pd.Timestamp:
    """격주 구간의 첫 영업일(해당 ISO 짝수/홀수 주 묶음의 시작 주 월요일)."""
    ts = _normalize_asof(asof)
    iso = ts.isocalendar()
    week_start = int(iso.week) - ((int(iso.week) - 1) % 2)
    # 해당 ISO 주의 월요일로 맞춘 뒤, week_start 주로 이동
    monday = ts - pd.Timedelta(days=int(ts.weekday()))
    delta_weeks = int(iso.week) - week_start
    period_monday = monday - pd.Timedelta(weeks=delta_weeks)
    for offset in range(5):
        d = period_monday + pd.Timedelta(days=offset)
        if d.weekday() < 5:
            return d
    return period_monday


def get_biweekly_kospi_top_n(
    asof: date | datetime | str | pd.Timestamp,
    top_n: int,
    *,
    logger_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[list[str], Optional[str]]:
    """격주(2주) KOSPI 시총 상위 N."""
    try:
        n = max(1, int(top_n))
    except (TypeError, ValueError):
        n = 150
    try:
        key = _biweekly_key(asof)
    except Exception:
        return [], None

    cache_path = _biweekly_cache_path(key, n)
    if cache_path.is_file():
        try:
            cached = pd.read_csv(cache_path, dtype=str)
            if "Code" in cached.columns and len(cached) >= min(n, 10):
                codes = [
                    str(c).strip().zfill(6)
                    for c in cached["Code"].tolist()
                    if str(c).strip().isdigit()
                ]
                asof_val = None
                if "asof_date" in cached.columns and len(cached):
                    asof_val = str(cached["asof_date"].iloc[0])
                return codes[:n], asof_val
        except Exception:
            pass

    asof_guess = _first_biweek_asof(asof)
    codes, asof_str, detail = load_kospi_top_n_asof(
        asof_guess, n, logger_callback=logger_callback
    )
    if not codes:
        return [], None
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        out = detail.copy()
        if "Name" not in out.columns:
            out["Name"] = out["Code"]
        out["asof_date"] = asof_str or asof_guess.strftime("%Y-%m-%d")
        out[["Code", "Name", "asof_date"]].to_csv(
            cache_path, index=False, encoding="utf-8-sig"
        )
    except Exception as e:
        try:
            logging.warning("격주 marcap 캐시 저장 실패: %s", e, exc_info=True)
        except Exception:
            pass
    return codes, asof_str


def _bimonthly_key(asof: date | datetime | str | pd.Timestamp) -> str:
    """2개월 키 (예: 2025BM01 = Jan-Feb, BM02 = Mar-Apr)."""
    ts = _normalize_asof(asof)
    pair = (int(ts.month) - 1) // 2 + 1
    return f"{int(ts.year)}BM{pair:02d}"


def _first_bimonth_asof(asof: date | datetime | str | pd.Timestamp) -> pd.Timestamp:
    """2개월 구간의 첫 영업일."""
    ts = _normalize_asof(asof)
    start_month = ((int(ts.month) - 1) // 2) * 2 + 1
    start = pd.Timestamp(year=int(ts.year), month=start_month, day=1)
    for offset in range(12):
        d = start + pd.Timedelta(days=offset)
        if d.weekday() < 5:
            return d
    return start


def get_bimonthly_kospi_top_n(
    asof: date | datetime | str | pd.Timestamp,
    top_n: int,
    *,
    logger_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[list[str], Optional[str]]:
    """2개월마다 KOSPI 시총 상위 N."""
    try:
        n = max(1, int(top_n))
    except (TypeError, ValueError):
        n = 150
    try:
        key = _bimonthly_key(asof)
    except Exception:
        return [], None

    cache_path = _bimonthly_cache_path(key, n)
    if cache_path.is_file():
        try:
            cached = pd.read_csv(cache_path, dtype=str)
            if "Code" in cached.columns and len(cached) >= min(n, 10):
                codes = [
                    str(c).strip().zfill(6)
                    for c in cached["Code"].tolist()
                    if str(c).strip().isdigit()
                ]
                asof_val = None
                if "asof_date" in cached.columns and len(cached):
                    asof_val = str(cached["asof_date"].iloc[0])
                return codes[:n], asof_val
        except Exception:
            pass

    asof_guess = _first_bimonth_asof(asof)
    codes, asof_str, detail = load_kospi_top_n_asof(
        asof_guess, n, logger_callback=logger_callback
    )
    if not codes:
        return [], None
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        out = detail.copy()
        if "Name" not in out.columns:
            out["Name"] = out["Code"]
        out["asof_date"] = asof_str or asof_guess.strftime("%Y-%m-%d")
        out[["Code", "Name", "asof_date"]].to_csv(
            cache_path, index=False, encoding="utf-8-sig"
        )
    except Exception as e:
        try:
            logging.warning("2개월 marcap 캐시 저장 실패: %s", e, exc_info=True)
        except Exception:
            pass
    return codes, asof_str


def _quarterly_key(asof: date | datetime | str | pd.Timestamp) -> str:
    """분기 키 (예: 2025Q2)."""
    ts = _normalize_asof(asof)
    q = (int(ts.month) - 1) // 3 + 1
    return f"{int(ts.year)}Q{q}"


def _first_quarter_asof(asof: date | datetime | str | pd.Timestamp) -> pd.Timestamp:
    """분기 첫 영업일."""
    ts = _normalize_asof(asof)
    start_month = ((int(ts.month) - 1) // 3) * 3 + 1
    start = pd.Timestamp(year=int(ts.year), month=start_month, day=1)
    for offset in range(12):
        d = start + pd.Timedelta(days=offset)
        if d.weekday() < 5:
            return d
    return start


def get_quarterly_kospi_top_n(
    asof: date | datetime | str | pd.Timestamp,
    top_n: int,
    *,
    logger_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[list[str], Optional[str]]:
    """분기마다 KOSPI 시총 상위 N."""
    try:
        n = max(1, int(top_n))
    except (TypeError, ValueError):
        n = 150
    try:
        key = _quarterly_key(asof)
    except Exception:
        return [], None

    cache_path = _quarterly_cache_path(key, n)
    if cache_path.is_file():
        try:
            cached = pd.read_csv(cache_path, dtype=str)
            if "Code" in cached.columns and len(cached) >= min(n, 10):
                codes = [
                    str(c).strip().zfill(6)
                    for c in cached["Code"].tolist()
                    if str(c).strip().isdigit()
                ]
                asof_val = None
                if "asof_date" in cached.columns and len(cached):
                    asof_val = str(cached["asof_date"].iloc[0])
                return codes[:n], asof_val
        except Exception:
            pass

    asof_guess = _first_quarter_asof(asof)
    codes, asof_str, detail = load_kospi_top_n_asof(
        asof_guess, n, logger_callback=logger_callback
    )
    if not codes:
        return [], None
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        out = detail.copy()
        if "Name" not in out.columns:
            out["Name"] = out["Code"]
        out["asof_date"] = asof_str or asof_guess.strftime("%Y-%m-%d")
        out[["Code", "Name", "asof_date"]].to_csv(
            cache_path, index=False, encoding="utf-8-sig"
        )
    except Exception as e:
        try:
            logging.warning("분기 marcap 캐시 저장 실패: %s", e, exc_info=True)
        except Exception:
            pass
    return codes, asof_str


def _daily_cache_path(asof_ymd: str, top_n: int) -> Path:
    ymd = str(asof_ymd).replace("-", "")[:8]
    return _DAILY_CACHE_DIR / f"kospi_top{int(top_n)}_{ymd}.csv"


def get_daily_kospi_top_n(
    asof: date | datetime | str | pd.Timestamp,
    top_n: int,
    *,
    logger_callback: Optional[Callable[[str], None]] = None,
    use_prior_day: bool = True,
) -> Tuple[list[str], Optional[str]]:
    """
    일별 KOSPI 시총 상위 N (로컬 CSV 캐시).

    Parameters
    ----------
    asof
        백테 거래일. ``use_prior_day=True`` 이면 전일(또는 직전 가용 거래일) as-of.
    use_prior_day
        True: look-ahead 완화(당일 EOD 시총으로 당일 선정 방지).
    """
    try:
        n = max(1, int(top_n))
    except (TypeError, ValueError):
        n = 150

    try:
        day = _normalize_asof(asof)
    except Exception:
        return [], None

    # 당일 장중 선정에 당일 종가 시총을 쓰지 않도록 전일부터 탐색
    query = day - pd.Timedelta(days=1) if use_prior_day else day

    day_df, actual = load_marcap_day(query, logger_callback=logger_callback)
    if day_df.empty or actual is None:
        return [], None

    asof_str = actual.strftime("%Y-%m-%d")
    cache_path = _daily_cache_path(asof_str, n)
    if cache_path.is_file():
        try:
            cached = pd.read_csv(cache_path, dtype=str)
            if "Code" in cached.columns and len(cached) >= min(n, 10):
                codes = [
                    str(c).strip().zfill(6)
                    for c in cached["Code"].tolist()
                    if str(c).strip().isdigit()
                ]
                return codes[:n], asof_str
        except Exception as e:
            try:
                logging.info("일별 marcap 캐시 읽기 재시도: %s", e)
            except Exception:
                pass

    detail = _top_n_from_day_df(day_df, n)
    if detail.empty:
        return [], None

    codes = [
        str(c).strip().zfill(6)
        for c in detail["Code"].tolist()
        if str(c).strip().isdigit()
    ]
    if not codes:
        return [], None

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        out = detail.copy()
        if "Name" not in out.columns:
            out["Name"] = out["Code"]
        out["asof_date"] = asof_str
        out[["Code", "Name", "asof_date"]].to_csv(
            cache_path, index=False, encoding="utf-8-sig"
        )
    except Exception as e:
        try:
            logging.warning("일별 marcap 캐시 저장 실패: %s", e, exc_info=True)
        except Exception:
            pass

    return codes[:n], asof_str


# 백테 kospi_universe_refresh 허용값
REFRESH_MODES: tuple[str, ...] = (
    "daily",
    "weekly",
    "biweekly",
    "monthly",
    "bimonthly",
    "quarterly",
)


def normalize_refresh_mode(mode: Optional[str]) -> str:
    """알 수 없는 값은 monthly로 폴백."""
    m = str(mode or "monthly").strip().lower()
    return m if m in REFRESH_MODES else "monthly"


def universe_period_key(
    asof: date | datetime | str | pd.Timestamp,
    mode: str,
) -> str:
    """갱신 주기별 캐시/순회 키."""
    m = normalize_refresh_mode(mode)
    ts = _normalize_asof(asof)
    if m == "daily":
        return ts.strftime("%Y%m%d")
    if m == "weekly":
        return _iso_week_key(ts)
    if m == "biweekly":
        return _biweekly_key(ts)
    if m == "bimonthly":
        return _bimonthly_key(ts)
    if m == "quarterly":
        return _quarterly_key(ts)
    return ts.strftime("%Y%m")


def get_kospi_top_n_for_refresh(
    asof: date | datetime | str | pd.Timestamp,
    top_n: int,
    mode: str,
    *,
    logger_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[list[str], Optional[str]]:
    """kospi_universe_refresh 모드에 맞는 시총 상위 N."""
    m = normalize_refresh_mode(mode)
    if m == "daily":
        return get_daily_kospi_top_n(asof, top_n, logger_callback=logger_callback)
    if m == "weekly":
        return get_weekly_kospi_top_n(asof, top_n, logger_callback=logger_callback)
    if m == "biweekly":
        return get_biweekly_kospi_top_n(asof, top_n, logger_callback=logger_callback)
    if m == "bimonthly":
        return get_bimonthly_kospi_top_n(asof, top_n, logger_callback=logger_callback)
    if m == "quarterly":
        return get_quarterly_kospi_top_n(asof, top_n, logger_callback=logger_callback)
    return get_monthly_kospi_top_n(asof, top_n, logger_callback=logger_callback)


def get_latest_marcap_asof(
    *,
    logger_callback: Optional[Callable[[str], None]] = None,
    current_year_cache_days: int = 3,
) -> Optional[pd.Timestamp]:
    """marcap 캐시에서 사용 가능한 최신 거래일 (실시간 유니버스 as-of)."""
    year = datetime.now().year
    ensure_marcap_year(
        year,
        logger_callback=logger_callback,
        max_cache_age_days=current_year_cache_days,
    )
    df_y = _load_marcap_year_df(year, logger_callback=logger_callback)
    if df_y.empty:
        ensure_marcap_year(year - 1, logger_callback=logger_callback)
        df_y = _load_marcap_year_df(year - 1, logger_callback=logger_callback)
    if df_y.empty or "Date" not in df_y.columns:
        return None
    try:
        return pd.Timestamp(df_y["Date"].max()).normalize()
    except Exception:
        return None


def _read_kospi_universe_csv_asof(
    kospi_universe_path: str,
) -> Tuple[list[str], Optional[str], pd.DataFrame]:
    """``kospi_universe.csv`` (asof_date 컬럼 포함 가능) 읽기."""
    path = Path(kospi_universe_path)
    if not path.is_file() or path.stat().st_size <= 10:
        return [], None, pd.DataFrame()

    try:
        df = pd.read_csv(path, dtype=str)
    except Exception:
        return [], None, pd.DataFrame()

    if "종목코드" in df.columns and "Code" not in df.columns:
        df = df.rename(columns={"종목코드": "Code", "종목명": "Name"})
    if "Code" not in df.columns:
        return [], None, pd.DataFrame()

    df = df.copy()
    df["Code"] = df["Code"].astype(str).str.zfill(6)
    if "Name" not in df.columns:
        df["Name"] = df["Code"]

    asof_val: Optional[str] = None
    if "asof_date" in df.columns and len(df):
        try:
            asof_val = str(df["asof_date"].iloc[0]).strip()[:10]
            if not asof_val or asof_val.lower() == "nan":
                asof_val = None
        except Exception:
            asof_val = None

    codes = [
        str(c).strip().zfill(6)
        for c in df["Code"].tolist()
        if str(c).strip().isdigit() and len(str(c).strip()) <= 6
    ]
    return codes, asof_val, df


def ensure_realtime_kospi_universe_csv(
    *,
    kospi_universe_path: str = "kospi_universe.csv",
    target_n: int = 400,
    logger_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[list[str], Optional[str]]:
    """
    실시간 감시용: **당월 첫 영업일** marcap as-of 로 ``kospi_universe.csv`` 갱신.

    백테 ``get_monthly_kospi_top_n`` 과 동일. CSV가 이미 당월이면 스킵.
    (실시간 전용 갱신주기 설정 없음 — monthly 고정)
    """
    try:
        tn = max(1, int(target_n))
    except (TypeError, ValueError):
        tn = 400

    ym = pd.Timestamp.now().normalize().strftime("%Y%m")
    cached_codes, cached_asof, _ = _read_kospi_universe_csv_asof(kospi_universe_path)
    if cached_asof and len(cached_codes) >= min(tn, 50):
        try:
            if pd.Timestamp(cached_asof).normalize().strftime("%Y%m") == ym:
                return cached_codes[:tn], cached_asof
        except Exception:
            pass

    try:
        codes, asof_str = get_monthly_kospi_top_n(
            ym, tn, logger_callback=logger_callback
        )
    except Exception as e:
        try:
            logging.warning("실시간 월별 marcap 로드 실패: %s", e, exc_info=True)
        except Exception:
            pass
        return cached_codes[:tn], cached_asof

    if not codes:
        return cached_codes[:tn], cached_asof

    # kospi_universe.csv 동기화 (감시목록이 CSV를 읽음)
    try:
        month_asof = _first_month_asof(ym)
        refreshed = refresh_kospi_universe_csv_from_marcap(
            kospi_universe_path=kospi_universe_path,
            target_n=tn,
            asof=month_asof,
            logger_callback=logger_callback,
        )
        if refreshed:
            return refreshed[:tn], asof_str
    except Exception as e:
        try:
            logging.warning("kospi_universe.csv 월별 동기화 실패: %s", e, exc_info=True)
        except Exception:
            pass

    return codes[:tn], asof_str


def refresh_kospi_universe_csv_from_marcap(
    *,
    kospi_universe_path: str = "kospi_universe.csv",
    target_n: int = 400,
    asof: Optional[date | datetime | str] = None,
    logger_callback: Optional[Callable[[str], None]] = None,
) -> list[str]:
    """
    지정(또는 당월 첫 영업일) marcap 시총 상위 target_n 을 ``kospi_universe.csv`` 에 저장.
    실시간 감시용 — FDR 현재 시총 스냅샷 대신 marcap 사용.
    """
    try:
        tn = max(1, int(target_n))
    except (TypeError, ValueError):
        tn = 400

    if asof is None:
        # 실시간 기본: 당월 첫 영업일 (monthly 고정)
        ym = pd.Timestamp.now().normalize().strftime("%Y%m")
        asof_ts = _first_month_asof(ym)
    else:
        asof_ts = _normalize_asof(asof)

    codes, asof_str, detail = load_kospi_top_n_asof(
        asof_ts, tn, logger_callback=logger_callback
    )
    if not codes:
        return []

    try:
        out = detail.copy()
        if "Name" not in out.columns:
            out["Name"] = out["Code"]
        out["asof_date"] = asof_str or ""
        out_path = Path(kospi_universe_path)
        out[["Code", "Name", "asof_date"]].to_csv(
            out_path, index=False, encoding="utf-8-sig"
        )
        _log_cb(
            logger_callback,
            f"  [Marcap] kospi_universe.csv 갱신 (as-of {asof_str}, {len(codes)}종목)",
        )
    except Exception as e:
        try:
            logging.warning("kospi_universe.csv marcap 저장 실패: %s", e, exc_info=True)
        except Exception:
            pass

    return codes
