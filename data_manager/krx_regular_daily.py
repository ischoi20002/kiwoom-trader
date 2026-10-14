# -*- coding: utf-8 -*-
"""
애프터마켓 시행(2026-09-14) 이후 일봉을 **정규장 기준**으로 덧씌우는 오버레이.

배경:
    FDR(네이버)·키움 일봉(ka10081)은 애프터(16:00~20:00) 마지막 체결가를 종가로, 애프터 고저를
    포함한 고가·저가를 준다. KRX 공식 종가는 정규장(15:30) 종가다. ``use_aftermarket_bars``
    정책이 OFF(장마감 후 주문 금지)면 15분봉에서 애프터를 빼는 것과 맞추어 일봉도
    정규장 기준을 쓴다.

구성:
    - 정규장 기준값 소스 (``source``):
        * ``"m15"`` (백테스트·과거일 선정): 로컬 ``bars_15m`` 의 정규장 봉(종가 단일가 라벨까지)을
          일별 집계. 하베스팅이 일봉·15분봉을 함께 갱신하므로 별도 조회가 필요 없다.
        * ``"kiwoom"`` (실시간 선정·레짐): 키움 ka10080(15분봉)을 직접 조회해 같은 방식으로 집계하고
          ``bars_1d_regular_kiwoom`` 에 캐시한다. 하베스팅 테이블과 섞지 않으며, 종목별로
          마지막 확보일 이후만 다시 받는다(1페이지 ≈ 21거래일). ``RETENTION_DAYS`` 초과분은 삭제.
      ``bars_1d`` 원본(네이버)은 건드리지 않는다(30일 겹침 수정주가 검증과 무관).
    - 읽는 시점에 수정 비율 = (원본 일봉 시가) / (기준값 시가) 를 구해
      고가·저가·종가 = 기준값 × 비율. 시가는 09:00 확정이라 애프터 영향이 없으므로
      비율은 데이터 소스 간 수정주가 계수 차이와 같다(분할·병합 소급 반영 자동 추종).
    - 시가·거래량은 원본 유지(거래량은 애프터 포함 하루 합계).
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

import numpy as np
import pandas as pd

M15_TABLE: str = "bars_15m"
KIWOOM_TABLE: str = "bars_1d_regular_kiwoom"
KIWOOM_META_TABLE: str = "bars_1d_regular_kiwoom_meta"

SOURCE_KIWOOM: str = "kiwoom"
SOURCE_M15: str = "m15"

# 당일 정규장 값은 종가 단일가(15:30) 봉 확정 후 이 시간(분)이 지나야 받는다
FETCH_AFTER_CLOSE_MINUTES: int = 10
# 키움 캐시 보관 기간(일) — 선정 지표 lookback(기본 280일)보다 넉넉히
RETENTION_DAYS: int = 400
# 종목당 ka10080 최대 페이지(1페이지 900봉 ≈ 21거래일)
KIWOOM_MAX_PAGES: int = 40
# 원본 종가와 덧씌운 종가 차이가 이 비율을 넘으면 매칭 오류(수정 비율 오판 등)로 보고 원본 유지.
# 정규장 하한(-30%) 후 애프터 상한(+30%)이면 실제 차이가 약 86% 까지 날 수 있어 그보다 크게 둔다.
MAX_CLOSE_DEVIATION: float = 0.9
# 저장 테이블 재확인 주기(초) — 다른 프로세스가 새 날짜를 저장했을 때 따라가기 위함
CACHE_RECHECK_SEC: float = 30.0

LogFn = Optional[Callable[..., None]]
TokenProvider = Callable[[], Optional[str]]
# (code, token, next_key) → (봉 레코드 리스트 또는 None, 다음 next_key)
PageFetcher = Callable[[str, str, str], Tuple[Optional[List[Any]], str]]

_lock = threading.RLock()
# 키움 조회는 한 번에 하나만 (선정·레짐·백그라운드 사전조회 중복 호출 방지)
_ensure_lock = threading.Lock()
_warned: Set[str] = set()
# 15분봉 집계 메모 (최근 1건: (DB 상태, 날짜 목록) → 집계 프레임)
_m15_cache: Dict[Tuple[Any, ...], pd.DataFrame] = {}
# 키움 캐시 메모
_kw_df: Optional[pd.DataFrame] = None
_kw_sig: Optional[Tuple[Any, ...]] = None
_kw_checked_at: float = 0.0
# 경로별 종목 → 정규장 값 확보 완료일(YYYY-MM-DD)
_kw_covered: Dict[str, Dict[str, str]] = {}
_kw_pruned_on: Dict[str, str] = {}
_token_provider: Optional[TokenProvider] = None
_fallback_auth: Any = None


def _log(log: LogFn, msg: str) -> None:
    try:
        if log is not None:
            log(msg)
        else:
            print(msg)
    except Exception:
        pass


def _warn_once(log: LogFn, key: str, msg: str) -> None:
    """같은 경고는 프로세스당 1회만."""
    with _lock:
        if key in _warned:
            return
        _warned.add(key)
    _log(log, msg)


def regular_daily_overlay_enabled(settings: Optional[Mapping[str, Any]]) -> bool:
    """애프터 봉 정책이 OFF 이면 True (일봉을 정규장 기준으로 덧씌움)."""
    try:
        from utils.aftermarket_policy import aftermarket_bars_enabled_for_settings

        return not bool(aftermarket_bars_enabled_for_settings(settings))
    except Exception:
        return False


def resolve_db_path(data_manager: Any = None, db_path: Optional[str] = None) -> str:
    """오버레이 저장 DB 경로 (``trading_data.db``)."""
    if db_path:
        return os.path.abspath(str(db_path))
    try:
        p = getattr(data_manager, "db_path", None)
        if p:
            return os.path.abspath(str(p))
    except Exception:
        pass
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "trading_data.db")


def _aftermarket_start() -> date:
    try:
        from reporting.krx_trading_calendar import KRX_AFTERMARKET_START_DATE

        return KRX_AFTERMARKET_START_DATE
    except Exception:
        return date(2026, 9, 14)


def _close_minutes(d: date) -> int:
    """해당 일 정규장 종가 단일가 15분봉 라벨(분, 기본 15:30 — 수능일 등 지연 반영)."""
    try:
        from utils.krx_session_hours import get_session_hours

        return int(get_session_hours(d).close_minutes)
    except Exception:
        return 15 * 60 + 30


def _close_label(day: str) -> str:
    """해당 일 정규장 종가 단일가 15분봉 라벨 ``HH:MM``."""
    m = _close_minutes(pd.Timestamp(day).date())
    return f"{m // 60:02d}:{m % 60:02d}"


def _day_available_now(d: date, now: datetime) -> bool:
    """d 의 정규장 확정값을 지금 받아도 되는지 (당일은 종가 단일가 + 버퍼 이후)."""
    if d < now.date():
        return True
    if d > now.date():
        return False
    m = _close_minutes(d) + FETCH_AFTER_CLOSE_MINUTES
    return now >= datetime(d.year, d.month, d.day) + timedelta(minutes=m)


def target_days(start: Any, end: Any, now: Optional[datetime] = None) -> List[str]:
    """[start, end] 중 애프터 시행 이후 + 거래일 + 지금 확정된 날짜 (YYYY-MM-DD)."""
    try:
        s = pd.Timestamp(start).date()
        e = pd.Timestamp(end).date()
    except Exception:
        return []
    s = max(s, _aftermarket_start())
    if e < s:
        return []
    t_now = now or datetime.now()
    try:
        from reporting.krx_trading_calendar import is_trading_day
    except Exception:
        is_trading_day = None  # type: ignore[assignment]
    out: List[str] = []
    d = s
    while d <= e:
        try:
            ok = d.weekday() < 5 and (is_trading_day is None or bool(is_trading_day(d)))
        except Exception:
            ok = d.weekday() < 5
        if ok and _day_available_now(d, t_now):
            out.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return out


def latest_complete_day(now: Optional[datetime] = None) -> Optional[str]:
    """지금 정규장 값이 확정된 가장 최근 거래일 (애프터 시행 이전이면 None)."""
    t_now = now or datetime.now()
    days = target_days(t_now.date() - timedelta(days=20), t_now.date(), now=t_now)
    return days[-1] if days else None


def is_prefetch_quiet_time(now: Optional[datetime] = None) -> bool:
    """
    사전조회해도 되는 시간인지 (장중 ka10080 유량을 감시 폴링과 나눠 쓰지 않도록).

    거래일 08:50 ~ 종가 단일가+버퍼 사이는 False, 그 외(장전·장마감 후·휴장일)는 True.
    """
    t = now or datetime.now()
    try:
        from reporting.krx_trading_calendar import is_trading_day

        if not bool(is_trading_day(t.date())):
            return True
    except Exception:
        if t.weekday() >= 5:
            return True
    start = datetime(t.year, t.month, t.day, 8, 50)
    end = datetime(t.year, t.month, t.day) + timedelta(
        minutes=_close_minutes(t.date()) + FETCH_AFTER_CLOSE_MINUTES
    )
    return not (start <= t < end)


# ---------------------------------------------------------------------------
# 백테스트: 로컬 bars_15m 집계
# ---------------------------------------------------------------------------


def _db_state_sig(path: str) -> Tuple[Any, ...]:
    """DB 본파일·WAL 의 수정시각·크기 (15분봉 갱신 감지용)."""
    out: List[Any] = [path]
    for p in (path, path + "-wal"):
        try:
            st = os.stat(p)
            out += [st.st_mtime_ns, st.st_size]
        except OSError:
            out += [0, 0]
    return tuple(out)


def _load_m15_regular(path: str, days: List[str]) -> pd.DataFrame:
    """
    ``bars_15m`` 정규장 봉(종가 단일가 라벨까지)을 일별 OHLC 로 집계.

    Returns:
        ``code, date, k_open, k_high, k_low, k_close``. 실패 시 빈 프레임.
    """
    cols = ["code", "date", "k_open", "k_high", "k_low", "k_close"]
    empty = pd.DataFrame(columns=cols)
    days = sorted({str(d) for d in days if d})
    if not days:
        return empty
    key = (_db_state_sig(path), tuple(days))
    with _lock:
        hit = _m15_cache.get(key)
    if hit is not None:
        return hit

    by_close: Dict[str, List[str]] = {}
    for d in days:
        by_close.setdefault(_close_label(d), []).append(d)
    try:
        conn = sqlite3.connect(path, timeout=30)
    except Exception:
        return empty
    parts: List[pd.DataFrame] = []
    try:
        for close_hm, ds in by_close.items():
            hi = (pd.Timestamp(ds[-1]) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            ph = ",".join("?" * len(ds))
            where = (
                f" FROM {M15_TABLE} WHERE datetime >= ? AND datetime < ? "
                f"AND substr(datetime, 1, 10) IN ({ph}) AND substr(datetime, 12, 5) <= ? "
                "GROUP BY code, substr(datetime, 1, 10)"
            )
            params = [ds[0], hi, *ds, close_hm]
            hl = pd.read_sql_query(
                "SELECT code, substr(datetime, 1, 10) AS date, MAX(high) AS k_high, "
                "MIN(low) AS k_low" + where,
                conn,
                params=params,
            )
            # SQLite: 집계가 MIN()/MAX() 하나뿐이면 bare 열은 그 행(첫 봉·마지막 봉)의 값
            op = pd.read_sql_query(
                "SELECT code, substr(datetime, 1, 10) AS date, MIN(datetime) AS t0, "
                "open AS k_open" + where,
                conn,
                params=params,
            )
            cl = pd.read_sql_query(
                "SELECT code, substr(datetime, 1, 10) AS date, MAX(datetime) AS t1, "
                "close AS k_close" + where,
                conn,
                params=params,
            )
            parts.append(
                hl.merge(op[["code", "date", "k_open"]], on=["code", "date"]).merge(
                    cl[["code", "date", "k_close"]], on=["code", "date"]
                )
            )
    except Exception:
        return empty
    finally:
        try:
            conn.close()
        except Exception:
            pass
    if not parts:
        return empty
    df = pd.concat(parts, ignore_index=True)
    df["code"] = df["code"].astype(str).str.strip().str.zfill(6)
    for c in ("k_open", "k_high", "k_low", "k_close"):
        df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    df = df[(df["k_open"] > 0) & (df["k_high"] > 0) & (df["k_low"] > 0) & (df["k_close"] > 0)]
    df = df.drop_duplicates(["code", "date"]).reset_index(drop=True)[cols]
    with _lock:
        _m15_cache.clear()
        _m15_cache[key] = df
    return df


# ---------------------------------------------------------------------------
# 실시간: 키움 ka10080(15분봉) 조회 + 캐시
# ---------------------------------------------------------------------------


def register_kiwoom_token_provider(provider: Optional[TokenProvider]) -> None:
    """앱 인증 토큰 공급자 등록 (GUI 의 ``auth_manager.get_auth_token``)."""
    global _token_provider
    _token_provider = provider


def _get_token(log: LogFn = None) -> Optional[str]:
    """등록된 공급자 → 없으면 현재 서버 모드(모의/실전) 기준 AuthManager 로 발급."""
    global _fallback_auth
    try:
        if _token_provider is not None:
            tok = _token_provider()
            if tok:
                return str(tok)
    except Exception:
        pass
    try:
        if _fallback_auth is None:
            from core.auth_manager import AuthManager

            _fallback_auth = AuthManager(logger_callback=log)
        tok = _fallback_auth.get_auth_token()
        return str(tok) if tok else None
    except Exception:
        return None


def _default_page_fetcher(data_manager: Any) -> Optional[PageFetcher]:
    """앱 공용 ka10080 조회(전역 유량 1회/초 공유)를 쓰는 페이지 조회 함수."""
    dm = data_manager
    if not callable(getattr(dm, "fetch_kiwoom_minute_chart_page", None)):
        try:
            from data_manager_v2 import DataManagerV2

            dm = DataManagerV2(DataManagerV2.DEFAULT_DB_PATH)
        except Exception:
            return None

    def _fetch(code: str, token: str, next_key: str) -> Tuple[Optional[List[Any]], str]:
        return dm.fetch_kiwoom_minute_chart_page(
            code, token, next_key, tic_scope="15", fetch_context="regular_daily"
        )

    return _fetch


def _signed_abs(x: Any) -> float:
    try:
        return abs(float(str(x).strip().replace(",", "").replace("+", "")))
    except (TypeError, ValueError):
        return float("nan")


def _rows_to_m15_frame(rows: Sequence[Any]) -> pd.DataFrame:
    """ka10080 레코드 → ``datetime, open, high, low, close`` (cntr_tm = 봉 시작 시각)."""
    recs: List[Tuple[pd.Timestamp, float, float, float, float]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        tm = str(r.get("cntr_tm") or "").strip()
        if len(tm) < 12:
            continue
        try:
            ts = pd.Timestamp(datetime.strptime(tm[:12], "%Y%m%d%H%M"))
        except ValueError:
            continue
        recs.append(
            (
                ts,
                _signed_abs(r.get("open_pric")),
                _signed_abs(r.get("high_pric")),
                _signed_abs(r.get("low_pric")),
                _signed_abs(r.get("cur_prc")),
            )
        )
    df = pd.DataFrame(recs, columns=["datetime", "open", "high", "low", "close"])
    return df.drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)


def _aggregate_regular_daily(m15: pd.DataFrame, days: Set[str]) -> pd.DataFrame:
    """15분봉 → 일별 정규장 OHLC (``date, open, high, low, close``), ``days`` 만."""
    cols = ["date", "open", "high", "low", "close"]
    if m15 is None or m15.empty or not days:
        return pd.DataFrame(columns=cols)
    df = m15.copy()
    df["date"] = df["datetime"].dt.strftime("%Y-%m-%d")
    df = df[df["date"].isin(days)]
    if df.empty:
        return pd.DataFrame(columns=cols)
    hm = df["datetime"].dt.hour * 60 + df["datetime"].dt.minute
    close_m = df["date"].map({d: _close_minutes(pd.Timestamp(d).date()) for d in df["date"].unique()})
    df = df[(hm <= close_m) & (df["open"] > 0) & (df["close"] > 0)]
    if df.empty:
        return pd.DataFrame(columns=cols)
    g = df.groupby("date", sort=True)
    out = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
        }
    ).reset_index()
    return out[cols]


def fetch_kiwoom_regular_daily(
    code: str,
    since: str,
    until: str,
    *,
    token: str,
    page_fetcher: PageFetcher,
    max_pages: int = KIWOOM_MAX_PAGES,
) -> Optional[pd.DataFrame]:
    """
    키움 15분봉을 최신부터 ``since`` 이전까지 거슬러 받아 [since, until] 정규장 일봉을 만든다.

    Returns:
        ``date, open, high, low, close`` (거래 없는 날은 행 없음). 조회 실패 시 None.
    """
    rows_all: List[Any] = []
    nk = ""
    for _ in range(max(1, int(max_pages))):
        rows, nk_out = page_fetcher(code, token, nk)
        if rows is None:
            return None
        rows_all.extend(rows)
        oldest = min((str(r.get("cntr_tm") or "")[:8] for r in rows if isinstance(r, dict)), default="")
        nk = str(nk_out or "").strip()
        if not rows or not nk or (oldest and oldest < since.replace("-", "")):
            break
    days = set(target_days(since, until))
    return _aggregate_regular_daily(_rows_to_m15_frame(rows_all), days)


def _kw_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {KIWOOM_TABLE} (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            PRIMARY KEY (code, date)
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {KIWOOM_META_TABLE} (
            code TEXT PRIMARY KEY,
            covered_to TEXT,
            fetched_at TEXT
        )
        """
    )
    return conn


def _covered_map(path: str, conn: Optional[sqlite3.Connection] = None) -> Dict[str, str]:
    """종목별 확보 완료일 (프로세스 메모, 최초 1회 DB 로드)."""
    with _lock:
        hit = _kw_covered.get(path)
        if hit is not None:
            return hit
    own = conn is None
    try:
        c = conn or _kw_connect(path)
    except Exception:
        return {}
    try:
        mp = {
            str(r[0]): str(r[1] or "")
            for r in c.execute(f"SELECT code, covered_to FROM {KIWOOM_META_TABLE}").fetchall()
        }
    except Exception:
        mp = {}
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass
    with _lock:
        _kw_covered[path] = mp
    return mp


def _prune_old(conn: sqlite3.Connection, path: str, log: LogFn) -> None:
    """보관 기간 초과 행 삭제 (경로당 하루 1회)."""
    today = datetime.now().strftime("%Y-%m-%d")
    with _lock:
        if _kw_pruned_on.get(path) == today:
            return
        _kw_pruned_on[path] = today
    cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    try:
        n = conn.execute(f"DELETE FROM {KIWOOM_TABLE} WHERE date < ?", (cutoff,)).rowcount
        conn.commit()
        if n and n > 0:
            _log(log, f"[정규장일봉] 보관기간({RETENTION_DAYS}일) 초과 {n}행 삭제")
    except Exception:
        pass


def ensure_kiwoom_regular(
    codes: Sequence[str],
    *,
    until: Optional[str] = None,
    data_manager: Any = None,
    db_path: Optional[str] = None,
    log: LogFn = None,
    token: Optional[str] = None,
    page_fetcher: Optional[PageFetcher] = None,
    stop_event: Optional[threading.Event] = None,
    progress_every: int = 0,
) -> Dict[str, Any]:
    """
    종목별로 확보 완료일 이후의 정규장 일봉을 키움 15분봉으로 받아 캐시한다.

    Returns:
        ``{"needed": n, "fetched": n, "failed": [code...], "until": YYYY-MM-DD|None}``
    """
    summary: Dict[str, Any] = {"needed": 0, "fetched": 0, "failed": [], "until": None}
    end = str(until or latest_complete_day() or "")
    if not end:
        return summary
    summary["until"] = end
    path = resolve_db_path(data_manager, db_path)
    norm = sorted({str(c).strip().zfill(6) for c in codes if str(c).strip()})
    covered = _covered_map(path)
    todo = [c for c in norm if covered.get(c, "") < end]
    summary["needed"] = len(todo)
    if not todo:
        return summary

    with _ensure_lock:
        covered = _covered_map(path)
        todo = [c for c in norm if covered.get(c, "") < end]
        summary["needed"] = len(todo)
        if not todo:
            return summary
        tok = token or _get_token(log)
        if not tok:
            _warn_once(
                log,
                "kw:token",
                "⚠️ [정규장일봉] 키움 토큰 없음 — 정규장 일봉 조회 생략(원본 일봉 사용)",
            )
            summary["failed"] = todo
            return summary
        fetcher = page_fetcher or _default_page_fetcher(data_manager)
        if fetcher is None:
            summary["failed"] = todo
            return summary
        floor = max(
            _aftermarket_start(),
            (datetime.now() - timedelta(days=RETENTION_DAYS)).date(),
        ).strftime("%Y-%m-%d")
        try:
            conn = _kw_connect(path)
        except Exception as e:
            _warn_once(log, f"kw:db:{path}", f"⚠️ [정규장일봉] DB 연결 실패({path}): {e}")
            summary["failed"] = todo
            return summary
        t0 = time.time()
        _log(log, f"[정규장일봉] 키움 15분봉으로 정규장 일봉 확보 시작: {len(todo)}종목 (~{end})")
        try:
            for i, code in enumerate(todo, 1):
                if stop_event is not None and stop_event.is_set():
                    break
                prev = covered.get(code, "")
                since = floor
                if prev:
                    since = max(floor, (pd.Timestamp(prev) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
                try:
                    df = fetch_kiwoom_regular_daily(
                        code, since, end, token=tok, page_fetcher=fetcher
                    )
                except Exception:
                    df = None
                if df is None:
                    summary["failed"].append(code)
                    continue
                try:
                    conn.executemany(
                        f"INSERT OR REPLACE INTO {KIWOOM_TABLE} "
                        "(code, date, open, high, low, close) VALUES (?, ?, ?, ?, ?, ?)",
                        [
                            (code, r.date, float(r.open), float(r.high), float(r.low), float(r.close))
                            for r in df.itertuples(index=False)
                        ],
                    )
                    conn.execute(
                        f"INSERT OR REPLACE INTO {KIWOOM_META_TABLE} (code, covered_to, fetched_at) "
                        "VALUES (?, ?, ?)",
                        (code, end, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                    )
                    conn.commit()
                    with _lock:
                        covered[code] = end
                    summary["fetched"] += 1
                except Exception as e:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    summary["failed"].append(code)
                    _log(log, f"⚠️ [정규장일봉] {code} 저장 실패: {e}")
                if progress_every and i % int(progress_every) == 0:
                    _log(log, f"[정규장일봉] 진행 {i}/{len(todo)} ({time.time() - t0:.0f}s)")
            _prune_old(conn, path, log)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        _log(
            log,
            f"[정규장일봉] 완료: {summary['fetched']}/{len(todo)}종목 {time.time() - t0:.0f}s"
            + (f", 실패 {len(summary['failed'])}" if summary["failed"] else ""),
        )
    if summary["failed"]:
        miss = summary["failed"]
        _warn_once(
            log,
            f"kw:failed:{end}:{len(miss)}",
            f"⚠️ [정규장일봉] {len(miss)}종목 키움 조회 실패 (예: {', '.join(miss[:5])}) "
            "— 해당 종목은 원본(애프터 포함) 일봉 사용, 다음 조회 때 재시도",
        )
    return summary


def _load_kiwoom_table(path: str) -> pd.DataFrame:
    """키움 정규장 일봉 캐시 전체 (프로세스 메모, 메타가 바뀌면 재로드)."""
    global _kw_df, _kw_sig, _kw_checked_at
    empty = pd.DataFrame(columns=["code", "date", "k_open", "k_high", "k_low", "k_close"])
    with _lock:
        if (
            _kw_df is not None
            and _kw_sig is not None
            and _kw_sig[0] == path
            and time.time() - _kw_checked_at < CACHE_RECHECK_SEC
        ):
            return _kw_df
        try:
            conn = _kw_connect(path)
        except Exception:
            return empty
        try:
            row = conn.execute(
                f"SELECT COUNT(*), MAX(fetched_at) FROM {KIWOOM_META_TABLE}"
            ).fetchone()
            sig = (path, int(row[0] or 0), str(row[1] or ""))
            _kw_checked_at = time.time()
            if _kw_df is not None and _kw_sig == sig:
                return _kw_df
            df = pd.read_sql_query(
                f"SELECT code, date, open AS k_open, high AS k_high, low AS k_low, "
                f"close AS k_close FROM {KIWOOM_TABLE}",
                conn,
            )
            _kw_df = df
            _kw_sig = sig
            _kw_covered.pop(path, None)
            return df
        except Exception:
            return empty
        finally:
            try:
                conn.close()
            except Exception:
                pass


def clear_memory_cache() -> None:
    """메모 캐시 초기화(테스트용)."""
    global _kw_df, _kw_sig, _kw_checked_at
    with _lock:
        _kw_df = None
        _kw_sig = None
        _kw_checked_at = 0.0
        _kw_covered.clear()
        _kw_pruned_on.clear()
        _warned.clear()
        _m15_cache.clear()


# ---------------------------------------------------------------------------
# 덧씌우기
# ---------------------------------------------------------------------------


def _find_col(df: pd.DataFrame, name: str) -> Optional[str]:
    for c in df.columns:
        if str(c).lower() == name:
            return c
    return None


def _date_keys(df: pd.DataFrame) -> Optional[pd.Series]:
    """행별 YYYY-MM-DD 키 (time/datetime/date 열 또는 DatetimeIndex)."""
    for c in ("time", "datetime", "date", "Date", "Datetime"):
        if c in df.columns:
            ts = pd.to_datetime(df[c], errors="coerce")
            return pd.Series(ts.dt.strftime("%Y-%m-%d").to_numpy(), index=df.index)
    if isinstance(df.index, pd.DatetimeIndex):
        return pd.Series(df.index.strftime("%Y-%m-%d"), index=df.index)
    return None


def apply_regular_session_overlay(
    df: Optional[pd.DataFrame],
    settings: Optional[Mapping[str, Any]] = None,
    *,
    code: Optional[str] = None,
    data_manager: Any = None,
    db_path: Optional[str] = None,
    enabled: Optional[bool] = None,
    source: str = SOURCE_KIWOOM,
    log: LogFn = None,
) -> Optional[pd.DataFrame]:
    """
    정책 OFF 이면 애프터 시행일 이후 행의 고가·저가·종가를 정규장 기준으로 바꾼다.

    Args:
        df: 일봉. ``code`` 열(일괄) 또는 ``code`` 인자(단일 종목). 날짜는 time/datetime/date 열
            또는 DatetimeIndex. OHLC 열 이름은 대소문자 무관.
        settings: 정책 판정 설정(``enabled`` 미지정 시).
        enabled: 덧씌우기 여부를 직접 지정.
        source: ``"kiwoom"``(실시간: 키움 15분봉 조회·캐시) 또는 ``"m15"``(백테: 로컬 ``bars_15m``).

    Returns:
        덧씌운 사본(변경 없으면 원본 객체). 실패 시 원본.
    """
    try:
        if df is None or getattr(df, "empty", True):
            return df
        on = bool(enabled) if enabled is not None else regular_daily_overlay_enabled(settings)
        if not on:
            return df
        c_open, c_high, c_low, c_close = (_find_col(df, k) for k in ("open", "high", "low", "close"))
        if not (c_open and c_high and c_low and c_close):
            return df
        keys = _date_keys(df)
        if keys is None:
            return df
        ah0 = _aftermarket_start().strftime("%Y-%m-%d")
        sel = keys.notna() & (keys >= ah0)
        if not bool(sel.any()):
            return df

        if "code" in df.columns:
            codes = df["code"].astype(str).str.strip().str.zfill(6)
        elif code:
            codes = pd.Series(str(code).strip().zfill(6), index=df.index)
        else:
            return df

        d_min = str(keys[sel].min())
        d_max = str(keys[sel].max())
        path = resolve_db_path(data_manager, db_path)
        use_m15 = str(source or SOURCE_KIWOOM).strip().lower() == SOURCE_M15
        src_name = "15분봉DB" if use_m15 else "키움15분봉"
        if use_m15:
            k = _load_m15_regular(path, keys[sel].unique().tolist())
        else:
            # 사전조회가 끝났으면 메모 확인만 하고 즉시 반환
            ensure_kiwoom_regular(
                codes[sel].unique().tolist(), data_manager=data_manager, db_path=path, log=log
            )
            k = _load_kiwoom_table(path)
        if k is None or k.empty:
            _warn_once(
                log,
                f"empty:{src_name}:{d_min}:{d_max}",
                f"⚠️ [정규장일봉] 정규장 기준값({src_name}) 없음 ({d_min}~{d_max}) — 원본(애프터 포함) 일봉 사용",
            )
            return df

        pos = np.flatnonzero(sel.to_numpy())
        sub = pd.DataFrame(
            {
                "code": codes.to_numpy()[pos],
                "date": keys.to_numpy()[pos],
                "open": pd.to_numeric(df[c_open].iloc[pos], errors="coerce").to_numpy(dtype="float64"),
                "close": pd.to_numeric(df[c_close].iloc[pos], errors="coerce").to_numpy(dtype="float64"),
            }
        )
        # left merge 는 왼쪽 순서·길이 유지 ((code, date) 는 테이블 PK 라 중복 없음)
        m = sub.merge(k, on=["code", "date"], how="left")
        if len(m) != len(sub):
            return df
        # 거래정지(원본 시가 0)·아직 확정 전인 당일 행은 정규장 기준값이 없어도 정상
        lc = latest_complete_day() or ""
        n_miss = int((m["k_close"].isna() & m["open"].gt(0) & m["date"].le(lc)).sum())
        if n_miss:
            _warn_once(
                log,
                f"miss:{src_name}:{d_min}:{d_max}:{n_miss}",
                f"⚠️ [정규장일봉] 정규장 기준값({src_name}) 없는 일봉 {n_miss}건 ({d_min}~{d_max}) "
                "— 해당 행은 원본(애프터 포함) 사용",
            )
        if m["k_close"].notna().sum() == 0:
            return df

        ratio = m["open"] / m["k_open"]
        ratio = ratio.where(np.isfinite(ratio) & (ratio > 0))
        # 시가 결측일은 같은 종목 인접일 비율로 보완(수정 계수는 이벤트 사이 구간에서 일정)
        ratio = ratio.groupby(m["code"]).transform(lambda s: s.ffill().bfill())
        ratio = ratio.fillna(1.0)

        new_h = np.round(m["k_high"] * ratio)
        new_l = np.round(m["k_low"] * ratio)
        new_c = np.round(m["k_close"] * ratio)
        ok = m["k_close"].notna() & new_c.gt(0) & m["close"].gt(0)
        dev = (m["close"] / new_c - 1.0).abs()
        ok &= dev.le(MAX_CLOSE_DEVIATION)
        n_bad = int((m["k_close"].notna() & ~ok).sum())
        if n_bad:
            _warn_once(
                log,
                f"bad:{d_min}:{d_max}:{n_bad}",
                f"⚠️ [정규장일봉] 원본과 차이가 커 덧씌우기 생략 {n_bad}건 ({d_min}~{d_max})",
            )
        okv = ok.to_numpy()
        if not bool(okv.any()):
            return df

        pos_ok = pos[okv]
        o = m["open"].to_numpy()[okv]
        c_new = new_c.to_numpy()[okv]
        # 반올림으로 시가·종가가 고저 밖으로 나가지 않게 보정 (시가 결측은 무시)
        h_new = np.fmax(np.fmax(new_h.to_numpy()[okv], o), c_new)
        l_new = np.fmin(np.fmin(new_l.to_numpy()[okv], o), c_new)

        out = df.copy()
        for col, vals in ((c_close, c_new), (c_high, h_new), (c_low, l_new)):
            if not pd.api.types.is_float_dtype(out[col]):
                out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
            j = out.columns.get_loc(col)
            out.iloc[pos_ok, j] = vals
        return out
    except Exception as e:
        _warn_once(log, f"apply:{type(e).__name__}", f"⚠️ [정규장일봉] 일봉 덧씌우기 실패(원본 사용): {e}")
        return df
