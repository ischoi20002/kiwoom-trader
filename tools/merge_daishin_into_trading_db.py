# -*- coding: utf-8 -*-
"""
daishin_data.db(1분봉) → trading_data.db 로 보완 병합.

- 대신 수집본은 종목코드가 'A005930' 형태인 경우가 많음 → trading 쪽 6자리(005930)로 맞춤.
- 기존 trading 행과 (code, datetime) 이 겹치면 INSERT OR IGNORE 로 trading 데이터를 유지.
- 병합 후 종목별로 bars_15m 전체를 bars_1m 기준으로 재생성.

사용 예:
  python tools/merge_daishin_into_trading_db.py
  python tools/merge_daishin_into_trading_db.py --daishin C:/data/daishin_data.db --trading C:/vibe/trading_data.db
  python tools/merge_daishin_into_trading_db.py --only 005930,000660 --dry-run
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import FrozenSet, List, Optional, Set

# 프로젝트 루트를 path 에 추가
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils.bar_resample import refresh_resampled_data_at_path  # noqa: E402
from utils.normalization import normalize_code  # noqa: E402


def daishin_code_to_trading_code(code: str) -> str:
    """대신 DB 코드 → trading_data.db 에 쓰는 코드 (주로 6자리)."""
    try:
        c = str(code).strip()
        if len(c) == 7 and c[0].upper() == "A" and c[1:].isdigit():
            return c[1:].zfill(6)
        if c.isdigit():
            return c.zfill(6)
        return normalize_code(c)
    except Exception:
        return normalize_code(str(code))


def _collect_source_codes(daishin_path: str, only: Optional[FrozenSet[str]]) -> List[str]:
    """대신 DB의 DISTINCT code 목록 (필터 적용)."""
    out: Set[str] = set()
    try:
        with sqlite3.connect(f"file:{daishin_path}?mode=ro", uri=True, timeout=60) as src:
            cur = src.cursor()
            cur.execute("SELECT DISTINCT code FROM bars_1m")
            for (raw,) in cur.fetchall():
                if raw is None:
                    continue
                tc = daishin_code_to_trading_code(str(raw))
                if only is not None and tc not in only:
                    continue
                out.add(tc)
    except sqlite3.Error as e:
        raise RuntimeError(f"대신 DB 읽기 실패: {e}") from e
    return sorted(out)


def _expand_only_filter(codes_csv: Optional[str]) -> Optional[FrozenSet[str]]:
    if not codes_csv or not str(codes_csv).strip():
        return None
    parts = [p.strip().zfill(6) for p in str(codes_csv).split(",") if p.strip()]
    return frozenset(parts)


_INSERT_ONE_CODE_SQL = """
    INSERT OR IGNORE INTO main.bars_1m (code, datetime, open, high, low, close, volume)
    SELECT
      CASE
        WHEN length(ddb.bars_1m.code) = 7 AND upper(substr(ddb.bars_1m.code, 1, 1)) = 'A'
             AND substr(ddb.bars_1m.code, 2) GLOB '[0-9][0-9][0-9][0-9][0-9][0-9]'
          THEN substr(ddb.bars_1m.code, 2)
        WHEN ddb.bars_1m.code GLOB '[0-9]*' AND length(ddb.bars_1m.code) > 0
          THEN printf('%06d', CAST(ddb.bars_1m.code AS INTEGER))
        ELSE ddb.bars_1m.code
      END AS ncode,
      ddb.bars_1m.datetime,
      ddb.bars_1m.open,
      ddb.bars_1m.high,
      ddb.bars_1m.low,
      ddb.bars_1m.close,
      ddb.bars_1m.volume
    FROM ddb.bars_1m
    WHERE ddb.bars_1m.code IN (?, ?, ?)
    """


def _attach_daishin_db(conn: sqlite3.Connection, daishin_path: str, log: callable) -> Optional[str]:
    """
    대신 DB attach. 잠금 시 임시 파일로 복사 후 attach.
    반환: 삭제해야 할 임시 복사 경로(직접 attach 성공 시 None).
    """
    resolved = str(Path(daishin_path).resolve())
    tmp_path: Optional[str] = None
    try:
        conn.execute("ATTACH DATABASE ? AS ddb", (resolved,))
        return None
    except sqlite3.Error as e1:
        try:
            log(f"[경고] 대신 DB 직접 attach 실패({e1}) → 임시 복사본으로 재시도…")
        except Exception:
            pass
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="daishin_merge_", suffix=".db")
        os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        # WAL 포함 일관 스냅샷(단순 파일 복사는 -wal 미반영 위험)
        src_conn: Optional[sqlite3.Connection] = None
        dst_conn: Optional[sqlite3.Connection] = None
        try:
            src_conn = sqlite3.connect(resolved, timeout=300)
            dst_conn = sqlite3.connect(tmp_path, timeout=300)
            src_conn.backup(dst_conn)
        finally:
            if dst_conn is not None:
                try:
                    dst_conn.close()
                except sqlite3.Error:
                    pass
            if src_conn is not None:
                try:
                    src_conn.close()
                except sqlite3.Error:
                    pass
        conn.execute("ATTACH DATABASE ? AS ddb", (tmp_path,))
        return tmp_path
    except Exception as e2:
        if tmp_path and os.path.isfile(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        raise RuntimeError(
            f"대신 DB attach 불가: {e2} (CREON/다른 툴에서 daishin_data.db 를 닫은 뒤 재시도)"
        ) from e2


def _reconcile_sync_meta_from_bars_1m(trading_path: str, log: callable) -> None:
    """bars_1m MIN/MAX 로 sync_meta 시각 컬럼만 일괄 보정 (status 유지)."""
    try:
        with sqlite3.connect(trading_path, timeout=300) as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                INSERT INTO sync_meta (code, last_sync_dt, earliest_sync_dt)
                SELECT code, MAX(datetime), MIN(datetime)
                FROM bars_1m
                GROUP BY code
                ON CONFLICT(code) DO UPDATE SET
                    last_sync_dt = excluded.last_sync_dt,
                    earliest_sync_dt = excluded.earliest_sync_dt
                """
            )
            conn.commit()
        try:
            log("[메타] sync_meta ← bars_1m MIN/MAX 보정 완료 (하베스팅 로그 범위 정확도 개선)")
        except Exception:
            pass
    except sqlite3.Error as e:
        try:
            log(f"[경고] sync_meta 보정 실패: {e}")
        except Exception:
            pass


def merge_1m_in_batches(
    trading_path: str,
    daishin_path: str,
    codes: List[str],
    log: callable,
    log_every: int = 25,
) -> None:
    """
    종목별로 INSERT OR IGNORE 후 즉시 COMMIT — 한 번에 수억 행을 묶지 않아 장시간 잠금을 피합니다.
    """
    trading_path = str(Path(trading_path).resolve())
    daishin_path = str(Path(daishin_path).resolve())
    if not os.path.isfile(daishin_path):
        raise FileNotFoundError(f"대신 DB 없음: {daishin_path}")
    if not os.path.isfile(trading_path):
        raise FileNotFoundError(f"trading DB 없음: {trading_path}")

    n = len(codes)
    tmp_attach: Optional[str] = None
    with sqlite3.connect(trading_path, timeout=300) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            conn.execute("PRAGMA busy_timeout=600000;")
        except sqlite3.Error:
            pass
        try:
            conn.execute("PRAGMA synchronous=NORMAL;")
        except sqlite3.Error:
            pass
        tmp_attach = _attach_daishin_db(conn, daishin_path, log)
        try:
            for i, code_str in enumerate(codes, 1):
                params = [code_str, f"A{code_str}", f"a{code_str}"]
                conn.execute(_INSERT_ONE_CODE_SQL, params)
                conn.commit()
                if log_every and (i % log_every == 0 or i == n):
                    try:
                        log(f"[병합] 1분봉 … {i}/{n} 종목 반영")
                    except Exception:
                        pass
        finally:
            try:
                conn.execute("DETACH DATABASE ddb;")
            except Exception:
                pass
            try:
                conn.execute("PRAGMA synchronous=FULL;")
            except sqlite3.Error:
                pass
    if tmp_attach and os.path.isfile(tmp_attach):
        try:
            os.unlink(tmp_attach)
        except OSError:
            try:
                log(f"[경고] 임시 대신 복사본 삭제 실패: {tmp_attach}")
            except Exception:
                pass
    try:
        log(f"[병합] 1분봉 종목별 병합 완료 ({n}종목)")
    except Exception:
        pass


def run(
    trading_path: str,
    daishin_path: str,
    *,
    only_codes: Optional[str] = None,
    dry_run: bool = False,
    skip_15m: bool = False,
    log: Optional[callable] = None,
) -> None:
    _log = log or (lambda m: print(m, flush=True))
    only = _expand_only_filter(only_codes)

    codes = _collect_source_codes(daishin_path, only)
    if not codes:
        _log("[종료] 병합 대상 종목이 없습니다 (--only 필터 또는 대신 DB 비어 있음).")
        return

    _log(f"[정보] 대상 종목 수: {len(codes)} (대신 DB 기준, 코드는 trading 6자리로 정규화)")

    if dry_run:
        try:
            with sqlite3.connect(f"file:{daishin_path}?mode=ro", uri=True) as src:
                n = src.execute("SELECT COUNT(*) FROM bars_1m").fetchone()[0]
            _log(f"[드라이런] 대신 bars_1m 총 행 수 ≈ {n:,}")
        except Exception as e:
            _log(f"[드라이런] 행 수 조회 실패: {e}")
        _log("[드라이런] 실제 병합·15분 재생성은 수행하지 않았습니다.")
        return

    t0 = time.perf_counter()
    merge_1m_in_batches(trading_path, daishin_path, codes, _log)
    _log(f"[병합] 1분봉 병합 소요: {time.perf_counter() - t0:.1f}s")

    _reconcile_sync_meta_from_bars_1m(trading_path, _log)

    if skip_15m:
        _log("[건너뜀] --skip-15m 으로 15분 재생성 생략")
        return

    _log(f"[15분] {len(codes)}개 종목 bars_15m 재생성 시작…")
    for i, code in enumerate(codes, 1):
        err = refresh_resampled_data_at_path(trading_path, code)
        if err:
            _log(f"  [경고] {code} 15분 재생성 실패: {err}")
        elif i % 25 == 0 or i == len(codes):
            _log(f"  … {i}/{len(codes)} ({code})")

    try:
        with sqlite3.connect(trading_path, timeout=60) as conn:
            conn.execute("ANALYZE")
            conn.commit()
        _log("[완료] ANALYZE 실행함.")
    except Exception as e:
        _log(f"[경고] ANALYZE 실패(무시 가능): {e}")

    _log(f"[완료] 전체 경과: {time.perf_counter() - t0:.1f}s")


def main() -> None:
    p = argparse.ArgumentParser(description="daishin_data.db 1분봉 → trading_data.db 보완 + 15분 재생성")
    p.add_argument(
        "--trading",
        default=str(_ROOT / "trading_data.db"),
        help="trading_data.db 경로 (기본: 프로젝트 루트)",
    )
    p.add_argument(
        "--daishin",
        default=str(_ROOT / "daishin_data.db"),
        help="daishin_data.db 경로 (기본: 프로젝트 루트)",
    )
    p.add_argument("--only", default=None, help="쉼표 구분 6자리 종목코드만 (예: 005930,000660)")
    p.add_argument("--dry-run", action="store_true", help="행 수·종목 수만 확인")
    p.add_argument("--skip-15m", action="store_true", help="1분만 병합, 15분 재생성 생략")
    args = p.parse_args()
    try:
        run(
            args.trading,
            args.daishin,
            only_codes=args.only,
            dry_run=args.dry_run,
            skip_15m=args.skip_15m,
        )
    except Exception as e:
        print(f"[오류] {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
