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
_FOURMONTH_CACHE_DIR = Path("data") / "universe_marcap_fourmonth"
_SEMIANNUAL_CACHE_DIR = Path("data") / "universe_marcap_semiannual"
_DAILY_CACHE_DIR = Path("data") / "universe_marcap_daily"
_MARCAP_RAW_URL = (
    "https://raw.githubusercontent.com/FinanceData/marcap/master/data/marcap-{year}.parquet"
)

_year_df_cache: dict[int, pd.DataFrame] = {}

# 로컬 parquet 가 요청 as-of 보다 뒤처질 때 재다운로드 (백테스트·실시간 공통 경로).
# 업스트림 자체가 며칠 늦게 갱신되므로, 파일이 이 시간보다 새것이면 재시도하지 않는다.
_LAG_REFRESH_MIN_AGE_HOURS = 6.0
# 프로세스당 연도별 1회만 시도 (daily 모드 등 반복 조회 시 다운로드 폭주 방지)
_lag_refresh_attempted: set[int] = set()


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
    force: bool = False,
) -> Optional[Path]:
    """연도 marcap parquet 를 확보한다. 성공 시 경로, 실패 시 None.

    ``force=True`` 면 연도와 무관하게 재다운로드한다 (실패 시 기존 파일 유지).
    """
    try:
        y = int(year)
    except (TypeError, ValueError):
        return None

    out = marcap_year_path(y)
    stale = False
    try:
        if out.is_file() and out.stat().st_size > 1000:
            if force:
                stale = True
            elif (
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
        engine_errors: list[str] = []
        # pyarrow 우선. fastparquet 는 일부 PC에서 WDAC/정책으로 DLL 차단됨.
        for engine in ("pyarrow", "fastparquet"):
            try:
                if engine == "pyarrow":
                    import pyarrow  # noqa: F401
                else:
                    import fastparquet  # noqa: F401
            except Exception as e:
                last_err = e
                try:
                    engine_errors.append(f"{engine} import: {e}")
                except Exception:
                    pass
                continue
            try:
                df = pd.read_parquet(path, engine=engine)
                last_err = None
                break
            except Exception as e:
                last_err = e
                try:
                    engine_errors.append(f"{engine} read: {e}")
                except Exception:
                    pass
                continue
        if df is None:
            detail = " | ".join(engine_errors) if engine_errors else str(last_err)
            raise RuntimeError(
                f"parquet 실패 ({detail}). "
                f"64bit .venv 로 실행하세요: c:\\k-trader\\run_k_trader.bat "
                f"(필요 시 install_k_trader.bat)"
            ) from last_err
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


def _year_df_max_date(df: pd.DataFrame) -> Optional[pd.Timestamp]:
    """연도 DataFrame 의 최신 Date (없으면 None)."""
    try:
        if df is None or df.empty or "Date" not in df.columns:
            return None
        return pd.Timestamp(df["Date"].max()).normalize()
    except Exception:
        return None


def refresh_marcap_if_lagging(
    asof: date | datetime | str | pd.Timestamp,
    *,
    logger_callback: Optional[Callable[[str], None]] = None,
    min_age_hours: Optional[float] = None,
) -> bool:
    """
    요청 as-of(미래면 오늘) 가 로컬 연도 parquet 최신 Date 보다 뒤면 재다운로드한다.

    - 파일이 ``min_age_hours`` 보다 새것이면 생략 (업스트림 지연 시 매 실행 다운로드 방지)
    - 프로세스당 연도별 1회만 시도
    - 갱신(다운로드 성공) 시 True
    """
    target = _normalize_asof(asof)
    today = pd.Timestamp.now().normalize()
    need = min(target, today)
    # 주말이면 직전 금요일까지만 요구 (휴장일 판정은 하지 않음)
    while need.weekday() >= 5:
        need = need - pd.Timedelta(days=1)
    y = int(need.year)
    if y in _lag_refresh_attempted:
        return False

    path = marcap_year_path(y)
    if not path.is_file():
        # 파일이 없으면 _load_marcap_year_df 가 최초 다운로드한다
        return False

    max_d = _year_df_max_date(_load_marcap_year_df(y, logger_callback=logger_callback))
    if max_d is not None and max_d >= need:
        return False

    try:
        age_h = (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds() / 3600.0
    except OSError:
        age_h = float("inf")
    try:
        min_age = float(_LAG_REFRESH_MIN_AGE_HOURS if min_age_hours is None else min_age_hours)
    except (TypeError, ValueError):
        min_age = _LAG_REFRESH_MIN_AGE_HOURS
    if age_h < min_age:
        return False

    _lag_refresh_attempted.add(y)
    _log_cb(
        logger_callback,
        f"  [Marcap] {y}년 로컬 데이터가 {max_d.strftime('%Y-%m-%d') if max_d is not None else '없음'}까지 "
        f"→ as-of {need.strftime('%Y-%m-%d')} 필요, 재다운로드",
    )
    out = ensure_marcap_year(y, logger_callback=logger_callback, force=True)
    _year_df_cache.pop(y, None)
    if out is None:
        return False
    try:
        new_max = _year_df_max_date(_load_marcap_year_df(y, logger_callback=logger_callback))
        _log_cb(
            logger_callback,
            f"  [Marcap] {y}년 갱신 후 최신 {new_max.strftime('%Y-%m-%d') if new_max is not None else '?'}",
        )
    except Exception:
        pass
    return True


def _normalize_asof(asof: date | datetime | str | pd.Timestamp) -> pd.Timestamp:
    return pd.to_datetime(asof).normalize()


def _normalize_market_id(market: str) -> str:
    """유니버스 시장 키(KOSPI/KOSDAQ) 정규화."""
    try:
        m = str(market or "KOSPI").strip().upper()
    except Exception:
        m = "KOSPI"
    return "KOSDAQ" if m in ("KOSDAQ", "KSQ", "KQ") else "KOSPI"


def _filter_common_stocks(df: pd.DataFrame, market: str = "KOSPI") -> pd.DataFrame:
    """보통주(코드 끝 0) 필터. KOSPI=STK, KOSDAQ=KSQ(GLOBAL 포함)."""
    if df.empty:
        return df
    out = df.copy()
    mkt = _normalize_market_id(market)
    try:
        if "MarketId" in out.columns:
            mid = "KSQ" if mkt == "KOSDAQ" else "STK"
            out = out[out["MarketId"].astype(str).str.upper() == mid]
        elif "Market" in out.columns:
            needle = "KOSDAQ" if mkt == "KOSDAQ" else "KOSPI"
            out = out[out["Market"].astype(str).str.upper().str.contains(needle, na=False)]
        if "Code" in out.columns:
            out["Code"] = out["Code"].astype(str).str.zfill(6)
            out = out[out["Code"].str.fullmatch(r"\d{6}", na=False)]
            out = out[out["Code"].str.endswith("0")]
    except Exception:
        return pd.DataFrame(columns=list(df.columns))
    return out


def _filter_kospi_common(df: pd.DataFrame) -> pd.DataFrame:
    """KOSPI 보통주(코드 끝 0) 필터."""
    return _filter_common_stocks(df, "KOSPI")


def _top_n_from_day_df(
    day_df: pd.DataFrame,
    top_n: int,
    *,
    market: str = "KOSPI",
) -> pd.DataFrame:
    """일자 스냅샷에서 시총 상위 N."""
    if day_df.empty:
        return pd.DataFrame(columns=["Code", "Name", "Marcap", "Rank"])
    stk = _filter_common_stocks(day_df, market)
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
    try:
        refresh_marcap_if_lagging(target, logger_callback=logger_callback)
    except Exception as e:
        try:
            logging.warning("marcap 지연 갱신 확인 실패 %s: %s", target, e, exc_info=True)
        except Exception:
            pass
    for offset in range(max(1, int(lookback_days))):
        check = target - pd.Timedelta(days=offset)
        df_y = _load_marcap_year_df(check.year, logger_callback=logger_callback)
        if df_y.empty:
            continue
        day = df_y[df_y["Date"] == check]
        if not day.empty:
            return day, check
    return pd.DataFrame(), None


def load_market_top_n_asof(
    asof: date | datetime | str | pd.Timestamp,
    top_n: int,
    *,
    market: str = "KOSPI",
    logger_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[list[str], Optional[str], pd.DataFrame]:
    """
    as-of 시장별 시총 상위 N 종목코드.

    Returns
    -------
    (codes, asof_str, detail_df)
    """
    try:
        n = max(1, int(top_n))
    except (TypeError, ValueError):
        n = 150

    mkt = _normalize_market_id(market)
    day_df, actual = load_marcap_day(asof, logger_callback=logger_callback)
    detail = _top_n_from_day_df(day_df, n, market=mkt)
    if detail.empty or actual is None:
        return [], None, detail

    codes: list[str] = []
    try:
        for c in detail["Code"].tolist():
            s = str(c).strip().zfill(6)
            if len(s) == 6 and s.isdigit():
                codes.append(s)
    except Exception:
        codes = []
    asof_str = actual.strftime("%Y-%m-%d")
    return codes, asof_str, detail


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
    return load_market_top_n_asof(
        asof, top_n, market="KOSPI", logger_callback=logger_callback
    )


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
                # 전월 as-of 가 당월 파일에 남은 경우 폐기
                if asof_val:
                    try:
                        if (
                            pd.Timestamp(asof_val).normalize().strftime("%Y%m")
                            != ym
                        ):
                            _log_cb(
                                logger_callback,
                                f"  [Marcap] {ym} 캐시 as-of {asof_val} 불일치 → 재생성",
                            )
                            raise ValueError("stale monthly asof month mismatch")
                    except ValueError:
                        raise
                    except Exception:
                        pass
                return codes[:n], asof_val
        except ValueError:
            pass
        except Exception as e:
            try:
                logging.info("월별 marcap 캐시 읽기 재시도: %s", e)
            except Exception:
                pass

    asof_guess = _first_month_asof(ym)
    codes, asof_str, detail = load_kospi_top_n_asof(
        asof_guess, n, logger_callback=logger_callback
    )

    # lookback 이 전월로 떨어지면, 당월 안에서 첫 가용 거래일을 앞으로 탐색
    try:
        asof_ym = (
            pd.Timestamp(asof_str).normalize().strftime("%Y%m")
            if asof_str
            else ""
        )
    except Exception:
        asof_ym = ""
    if (not codes) or (asof_ym and asof_ym != ym):
        codes, asof_str, detail = [], None, pd.DataFrame()
        try:
            y = int(ym[:4])
            m = int(ym[4:6])
            start = pd.Timestamp(year=y, month=m, day=1)
            if m == 12:
                end = pd.Timestamp(year=y + 1, month=1, day=1) - pd.Timedelta(days=1)
            else:
                end = pd.Timestamp(year=y, month=m + 1, day=1) - pd.Timedelta(days=1)
            d = max(asof_guess, start)
            while d <= end:
                if int(d.weekday()) < 5:
                    c2, a2, det2 = load_kospi_top_n_asof(
                        d, n, logger_callback=None
                    )
                    if c2 and a2:
                        try:
                            if pd.Timestamp(a2).normalize().strftime("%Y%m") == ym:
                                codes, asof_str, detail = c2, a2, det2
                                break
                        except Exception:
                            pass
                d = d + pd.Timedelta(days=1)
        except Exception as e:
            try:
                logging.warning("당월 marcap 전진 탐색 실패 %s: %s", ym, e, exc_info=True)
            except Exception:
                pass

    if not codes:
        return [], None

    try:
        if asof_str and pd.Timestamp(asof_str).normalize().strftime("%Y%m") != ym:
            return [], None
    except Exception:
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


def _fourmonth_cache_path(period_key: str, top_n: int) -> Path:
    return _FOURMONTH_CACHE_DIR / f"kospi_top{int(top_n)}_{period_key}.csv"


def _semiannual_cache_path(period_key: str, top_n: int) -> Path:
    return _SEMIANNUAL_CACHE_DIR / f"kospi_top{int(top_n)}_{period_key}.csv"


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


def _fourmonth_key(asof: date | datetime | str | pd.Timestamp) -> str:
    """4개월 키 (예: 2025FM01 = Jan-Apr, FM02 = May-Aug, FM03 = Sep-Dec)."""
    ts = _normalize_asof(asof)
    block = (int(ts.month) - 1) // 4 + 1
    return f"{int(ts.year)}FM{block:02d}"


def _first_fourmonth_asof(asof: date | datetime | str | pd.Timestamp) -> pd.Timestamp:
    """4개월 구간의 첫 영업일."""
    ts = _normalize_asof(asof)
    start_month = ((int(ts.month) - 1) // 4) * 4 + 1
    start = pd.Timestamp(year=int(ts.year), month=start_month, day=1)
    for offset in range(12):
        d = start + pd.Timedelta(days=offset)
        if d.weekday() < 5:
            return d
    return start


def get_fourmonth_kospi_top_n(
    asof: date | datetime | str | pd.Timestamp,
    top_n: int,
    *,
    logger_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[list[str], Optional[str]]:
    """4개월마다 KOSPI 시총 상위 N."""
    try:
        n = max(1, int(top_n))
    except (TypeError, ValueError):
        n = 150
    try:
        key = _fourmonth_key(asof)
    except Exception:
        return [], None

    cache_path = _fourmonth_cache_path(key, n)
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

    asof_guess = _first_fourmonth_asof(asof)
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
            logging.warning("4개월 marcap 캐시 저장 실패: %s", e, exc_info=True)
        except Exception:
            pass
    return codes, asof_str


def _semiannual_key(asof: date | datetime | str | pd.Timestamp) -> str:
    """반기 키 (예: 2025H1 = Jan-Jun, 2025H2 = Jul-Dec)."""
    ts = _normalize_asof(asof)
    half = 1 if int(ts.month) <= 6 else 2
    return f"{int(ts.year)}H{half}"


def _first_semiannual_asof(asof: date | datetime | str | pd.Timestamp) -> pd.Timestamp:
    """반기 구간의 첫 영업일."""
    ts = _normalize_asof(asof)
    start_month = 1 if int(ts.month) <= 6 else 7
    start = pd.Timestamp(year=int(ts.year), month=start_month, day=1)
    for offset in range(12):
        d = start + pd.Timedelta(days=offset)
        if d.weekday() < 5:
            return d
    return start


def get_semiannual_kospi_top_n(
    asof: date | datetime | str | pd.Timestamp,
    top_n: int,
    *,
    logger_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[list[str], Optional[str]]:
    """6개월(반기)마다 KOSPI 시총 상위 N."""
    try:
        n = max(1, int(top_n))
    except (TypeError, ValueError):
        n = 150
    try:
        key = _semiannual_key(asof)
    except Exception:
        return [], None

    cache_path = _semiannual_cache_path(key, n)
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

    asof_guess = _first_semiannual_asof(asof)
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
            logging.warning("반기 marcap 캐시 저장 실패: %s", e, exc_info=True)
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
    "fourmonth",
    "semiannual",
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
    if m == "fourmonth":
        return _fourmonth_key(ts)
    if m == "semiannual":
        return _semiannual_key(ts)
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
    if m == "fourmonth":
        return get_fourmonth_kospi_top_n(asof, top_n, logger_callback=logger_callback)
    if m == "semiannual":
        return get_semiannual_kospi_top_n(asof, top_n, logger_callback=logger_callback)
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


def _first_asof_for_refresh(
    asof: date | datetime | str | pd.Timestamp,
    mode: str,
) -> pd.Timestamp:
    """갱신 주기 구간의 첫 영업일(as-of 후보)."""
    m = normalize_refresh_mode(mode)
    ts = _normalize_asof(asof)
    try:
        if m == "daily":
            # 전일 as-of는 get_daily 쪽에서 처리. 여기선 당일 정규화.
            return ts
        if m == "weekly":
            return _first_week_asof(ts)
        if m == "biweekly":
            return _first_biweek_asof(ts)
        if m == "bimonthly":
            return _first_bimonth_asof(ts)
        if m == "quarterly":
            return _first_quarter_asof(ts)
        if m == "fourmonth":
            return _first_fourmonth_asof(ts)
        if m == "semiannual":
            return _first_semiannual_asof(ts)
        return _first_month_asof(ts.strftime("%Y%m"))
    except Exception:
        return ts


def ensure_realtime_kospi_universe_csv(
    *,
    kospi_universe_path: str = "kospi_universe.csv",
    target_n: int = 400,
    logger_callback: Optional[Callable[[str], None]] = None,
    marcap_cache_max_age_days: int = 3,
    refresh_mode: Optional[str] = None,
) -> Tuple[list[str], Optional[str]]:
    """
    실시간 감시용: ``kospi_universe_refresh`` 주기의 첫 영업일 marcap as-of 로
    ``kospi_universe.csv`` 갱신.

    CSV as-of 가 **현재 주기 키**와 같으면 스킵.
    기본 갱신주기는 ``quarterly`` (달력 분기).
    """
    try:
        tn = max(1, int(target_n))
    except (TypeError, ValueError):
        tn = 400

    mode = normalize_refresh_mode(refresh_mode or "quarterly")
    now = pd.Timestamp.now().normalize()
    try:
        period_key = universe_period_key(now, mode)
    except Exception:
        period_key = now.strftime("%Y%m")

    cached_codes, cached_asof, _ = _read_kospi_universe_csv_asof(kospi_universe_path)

    # 주기 전환 직후엔 로컬 marcap 연도 파일이 오래되면 이전 as-of 폴백될 수 있어 선행 갱신
    try:
        y_now = int(now.year)
        ensure_marcap_year(
            y_now,
            logger_callback=logger_callback,
            max_cache_age_days=max(1, int(marcap_cache_max_age_days)),
        )
        _year_df_cache.pop(y_now, None)
    except Exception as e:
        try:
            logging.warning("실시간 marcap 연도 선행 갱신 실패: %s", e, exc_info=True)
        except Exception:
            pass

    try:
        codes, asof_str = get_kospi_top_n_for_refresh(
            now, tn, mode, logger_callback=logger_callback
        )
    except Exception as e:
        try:
            logging.warning(
                "실시간 marcap 로드 실패 (mode=%s): %s", mode, e, exc_info=True
            )
        except Exception:
            pass
        _log_cb(
            logger_callback,
            f"  [Warn] 유니버스({mode}/{period_key}) 실패 → 기존 CSV as-of {cached_asof or '?'} 유지",
        )
        return cached_codes[:tn], cached_asof

    if not codes:
        _log_cb(
            logger_callback,
            f"  [Warn] 유니버스({mode}/{period_key}) 비어 있음 → 기존 CSV as-of {cached_asof or '?'} 유지",
        )
        return cached_codes[:tn], cached_asof

    # 당기 as-of 와 CSV as-of 가 동일하면 스킵 (월별→분기 전환 시 같은 분기라도 재동기화)
    try:
        if (
            cached_asof
            and asof_str
            and len(cached_codes) >= min(tn, 50)
            and str(cached_asof)[:10] == str(asof_str)[:10]
        ):
            return cached_codes[:tn], cached_asof
    except Exception:
        pass

    # kospi_universe.csv 동기화 (감시목록이 CSV를 읽음)
    try:
        period_asof = _first_asof_for_refresh(now, mode)
        refreshed = refresh_kospi_universe_csv_from_marcap(
            kospi_universe_path=kospi_universe_path,
            target_n=tn,
            asof=period_asof,
            logger_callback=logger_callback,
            refresh_mode=mode,
        )
        if refreshed:
            return refreshed[:tn], asof_str
    except Exception as e:
        try:
            logging.warning(
                "kospi_universe.csv 동기화 실패 (mode=%s): %s", mode, e, exc_info=True
            )
        except Exception:
            pass

    return codes[:tn], asof_str


def refresh_kospi_universe_csv_from_marcap(
    *,
    kospi_universe_path: str = "kospi_universe.csv",
    target_n: int = 400,
    asof: Optional[date | datetime | str] = None,
    logger_callback: Optional[Callable[[str], None]] = None,
    refresh_mode: Optional[str] = None,
) -> list[str]:
    """
    지정(또는 갱신주기 첫 영업일) marcap 시총 상위 target_n 을 ``kospi_universe.csv`` 에 저장.
    실시간 감시용 — FDR 현재 시총 스냅샷 대신 marcap 사용.
    """
    try:
        tn = max(1, int(target_n))
    except (TypeError, ValueError):
        tn = 400

    mode = normalize_refresh_mode(refresh_mode or "quarterly")
    if asof is None:
        asof_ts = _first_asof_for_refresh(pd.Timestamp.now().normalize(), mode)
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
            f"  [Marcap] kospi_universe.csv 갱신 "
            f"(mode={mode}, as-of {asof_str}, {len(codes)}종목)",
        )
    except Exception as e:
        try:
            logging.warning("kospi_universe.csv marcap 저장 실패: %s", e, exc_info=True)
        except Exception:
            pass

    return codes
