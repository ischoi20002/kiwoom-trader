# -*- coding: utf-8 -*-
"""
분봉(bars_1m / bars_15m) 수정주가 변경 감지·키움 재수신 교체 공통 로직.

키움 ka10080(upd_stkpc_tp=1)은 분할·병합 등 이벤트 후 과거 분봉을 소급 수정해 내려주지만,
하베스팅 증분 저장(``INSERT OR IGNORE``)은 기존 행을 바꾸지 않아 DB 에 수정 전 가격이 남는다.
증분 수집 중 DB 와 겹치는 분봉 종가를 비교해 변경을 감지하고, 해당 종목 분봉을
API 바닥(키움 제공 최초 시각)까지 다시 받아 그 구간을 교체한다.
"""
from __future__ import annotations

import sqlite3
import statistics
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# 겹치는 분봉 비교 기준: 최소 비교 개수, 불일치 비율, 불일치 봉의 상대 오차 중앙값
OVERLAP_MIN_BARS = 5
OVERLAP_MISMATCH_FRAC = 0.5
OVERLAP_MEDIAN_REL_TOL = 0.003
PAGE_SLEEP_SEC = 0.35
RETRY_WAITS = (2.0, 5.0, 15.0)

FetchPage = Callable[..., Tuple[Any, str]]
ParseBars = Callable[[Any], List[Dict[str, Any]]]


def detect_minute_overlap_mismatch(
    db_path: str,
    table: str,
    code: str,
    bars: Sequence[Dict[str, Any]],
    upto: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    API 분봉 중 DB 에도 있는 시각(``upto`` 이하)의 종가를 비교.

    겹치는 봉의 절반 이상이 다르고 그 상대 오차 중앙값이 0.3% 를 넘으면 수정주가 변경으로 보고
    dict 반환. 비교 불가·일치면 None. (체결 보정 등 소수 봉 차이는 무시)
    """
    try:
        cand = [b for b in bars if b.get("time") and (not upto or str(b["time"]) <= str(upto))]
        if len(cand) < OVERLAP_MIN_BARS:
            return None
        api_close = {str(b["time"]): float(b["close"]) for b in cand}
        times = sorted(api_close)
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        try:
            rows = conn.execute(
                f"SELECT datetime, close FROM {table} WHERE code=? AND datetime BETWEEN ? AND ?",
                (code, times[0], times[-1]),
            ).fetchall()
        finally:
            conn.close()
        pairs = [(float(c), api_close[str(d)]) for d, c in rows if str(d) in api_close and c]
        if len(pairs) < OVERLAP_MIN_BARS:
            return None
        rels = [abs(db_c - api_c) / api_c for db_c, api_c in pairs if api_c > 0 and db_c != api_c]
        if len(rels) < len(pairs) * OVERLAP_MISMATCH_FRAC:
            return None
        med = statistics.median(rels)
        if med <= OVERLAP_MEDIAN_REL_TOL:
            return None
        return {"n_compared": len(pairs), "n_mismatch": len(rels), "median_rel": med}
    except Exception as e:
        try:
            print(f"[MinuteAdj] {code} 겹침 비교 실패: {e}")
        except Exception:
            pass
        return None


def fetch_all_minute_pages(
    fetch_page: FetchPage,
    parse_bars: ParseBars,
    code: str,
    token: str,
    scope: str,
    cap: Optional[str] = None,
    should_continue: Optional[Callable[[], bool]] = None,
    page_sleep: float = PAGE_SLEEP_SEC,
) -> Optional[List[Dict[str, Any]]]:
    """
    ka10080 을 API 바닥(next-key 없음)까지 전부 조회해 시각순 봉 리스트 반환.
    첫 페이지가 비면 [] (데이터 없음), 중간 실패(재시도 소진)·중단 시 None.
    """
    nk = ""
    by_time: Dict[str, Dict[str, Any]] = {}
    pages = 0
    while True:
        if should_continue is not None and not should_continue():
            return None
        raw: Any = None
        nk_new = ""
        for wait in (0.0,) + RETRY_WAITS:
            if wait:
                time.sleep(wait)
            raw, nk_new = fetch_page(code, token, nk, tic_scope=scope)
            if raw:
                break
            if pages == 0 and raw is not None:
                return []
        if not raw:
            try:
                print(f"[MinuteAdj] {code} tic={scope} page {pages + 1} 조회 실패 (재시도 소진)")
            except Exception:
                pass
            return None
        pages += 1
        try:
            bars = parse_bars(raw)
        except Exception as e:
            try:
                print(f"[MinuteAdj] {code} tic={scope} 파싱 실패: {e}")
            except Exception:
                pass
            return None
        for b in bars:
            t = str(b.get("time") or "")
            if len(t) < 19 or (cap and t > cap):
                continue
            by_time[t] = b
        nk = nk_new
        if not nk:
            break
        time.sleep(page_sleep)
    return [by_time[t] for t in sorted(by_time)]


def replace_minute_range(
    db_path: str,
    table: str,
    code: str,
    bars: Sequence[Dict[str, Any]],
    backup_db: Optional[str] = None,
) -> Tuple[int, int, int]:
    """
    [bars 최초, 최종] 구간 DB 행을 삭제 후 bars 로 원자적 교체.
    ``backup_db`` 지정 시 삭제 전 구간을 같은 테이블명으로 복사.
    반환: (삭제 행, 저장 행, 종가가 바뀐 기존 행).
    """
    if table not in ("bars_1m", "bars_15m"):
        raise ValueError(f"지원하지 않는 테이블: {table}")
    if not bars:
        return 0, 0, 0
    lo, hi = str(bars[0]["time"]), str(bars[-1]["time"])
    rows = [
        (code, str(b["time"]), int(b["open"]), int(b["high"]), int(b["low"]), int(b["close"]), int(b["volume"]))
        for b in bars
    ]
    new_close = {str(b["time"]): int(b["close"]) for b in bars}
    conn = sqlite3.connect(db_path, timeout=60)
    attached = False
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        old = conn.execute(
            f"SELECT datetime, close FROM {table} WHERE code=? AND datetime BETWEEN ? AND ?",
            (code, lo, hi),
        ).fetchall()
        changed = sum(1 for d, c in old if str(d) in new_close and int(c or 0) != new_close[str(d)])
        if backup_db:
            conn.execute("ATTACH DATABASE ? AS bk", (backup_db,))
            attached = True
        conn.execute("BEGIN")
        if attached:
            conn.execute(f"CREATE TABLE IF NOT EXISTS bk.{table} AS SELECT * FROM main.{table} WHERE 0")
            conn.execute(
                f"INSERT INTO bk.{table} SELECT * FROM main.{table} WHERE code=? AND datetime BETWEEN ? AND ?",
                (code, lo, hi),
            )
        cur = conn.execute(f"DELETE FROM {table} WHERE code=? AND datetime BETWEEN ? AND ?", (code, lo, hi))
        deleted = int(cur.rowcount or 0)
        conn.executemany(
            f"INSERT OR REPLACE INTO {table} (code, datetime, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        if table == "bars_1m":
            conn.execute(
                "INSERT INTO sync_meta (code, last_sync_dt, earliest_sync_dt) VALUES (?, ?, ?) "
                "ON CONFLICT(code) DO UPDATE SET "
                "last_sync_dt = MAX(COALESCE(last_sync_dt, ''), excluded.last_sync_dt), "
                "earliest_sync_dt = MIN(COALESCE(earliest_sync_dt, '9999-12-31'), excluded.earliest_sync_dt)",
                (code, hi, lo),
            )
        conn.commit()
        return deleted, len(rows), changed
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        if attached:
            try:
                conn.execute("DETACH DATABASE bk")
            except Exception:
                pass
        conn.close()


def refetch_and_replace_code(
    fetch_page: FetchPage,
    parse_bars: ParseBars,
    db_path: str,
    code: str,
    token: str,
    cap: Optional[str] = None,
    backup_db: Optional[str] = None,
    should_continue: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    한 종목의 1분봉·15분봉을 키움에서 전부 다시 받아 교체.
    테이블별 결과 {"status": replaced|no_data|fetch_failed|error, ...} 를 담은 dict 반환.
    """
    rec: Dict[str, Any] = {"code": code}
    for table, scope in (("bars_1m", "1"), ("bars_15m", "15")):
        try:
            bars = fetch_all_minute_pages(fetch_page, parse_bars, code, token, scope, cap, should_continue)
            if bars is None:
                rec[table] = {"status": "fetch_failed"}
                continue
            if not bars:
                rec[table] = {"status": "no_data"}
                continue
            deleted, saved, changed = replace_minute_range(db_path, table, code, bars, backup_db)
            rec[table] = {
                "status": "replaced",
                "from": bars[0]["time"],
                "to": bars[-1]["time"],
                "n": saved,
                "deleted": deleted,
                "changed": changed,
            }
        except Exception as e:
            rec[table] = {"status": "error", "error": str(e)}
    return rec
