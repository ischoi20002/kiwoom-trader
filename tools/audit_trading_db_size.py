# -*- coding: utf-8 -*-
"""trading_data.db 용량 감사: 페이지·인덱스·freelist·sqlite_stat1 (일회 실행용).

사용법:
  python tools/audit_trading_db_size.py
  python tools/audit_trading_db_size.py --count   # COUNT(*) (대용량이면 매우 느림)
"""
from __future__ import annotations

import os
import sqlite3
import sys
from typing import Any, List, Optional, Tuple


def _pk_columns(cur: sqlite3.Cursor, table: str) -> Optional[Tuple[str, ...]]:
    """PRIMARY KEY 복합 컬럼 순서."""
    try:
        rows = cur.execute(f"PRAGMA table_info({table})").fetchall()
        pk_rows = [(str(r[1]), int(r[5])) for r in rows if int(r[5]) > 0]
        pk_rows.sort(key=lambda x: x[1])
        return tuple(c for c, _ in pk_rows) if pk_rows else None
    except sqlite3.Error:
        return None


def _index_columns(cur: sqlite3.Cursor, index_name: str) -> Tuple[str, ...]:
    rows = cur.execute(f"PRAGMA index_info({index_name})").fetchall()
    cols = [str(r[2]) for r in sorted(rows, key=lambda x: int(x[0]))]
    return tuple(cols)


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db = os.path.join(root, "trading_data.db")
    if not os.path.isfile(db):
        print(f"파일 없음: {db}")
        sys.exit(1)
    sz = os.path.getsize(db)
    print(f"파일 크기: {sz:,} bytes ({sz / 1e9:.3f} GB)")
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    page_size = int(cur.execute("PRAGMA page_size").fetchone()[0])
    page_count = int(cur.execute("PRAGMA page_count").fetchone()[0])
    freelist = int(cur.execute("PRAGMA freelist_count").fetchone()[0])
    print(f"page_size: {page_size}, page_count: {page_count:,}, freelist_count: {freelist:,}")
    used_pages = page_count - freelist
    print(f"추정 사용 페이지: {used_pages:,}, 빈 페이지(freelist): {freelist:,}")
    if freelist > 0:
        print(
            f"  → freelist 로 회수 가능한 대략 용량: {freelist * page_size / 1e9:.3f} GB "
            "(VACUUM 후 파일 축소 가능)"
        )
    print(f"page_count×page_size = {page_count * page_size:,} (파일과 차이는 정상 범위)")
    print()

    try:
        rows = cur.execute(
            """
            SELECT name, SUM(pgsize) AS b
            FROM dbstat
            GROUP BY name
            ORDER BY b DESC
            """
        ).fetchall()
        print("--- dbstat: 객체별 페이지 바이트 합 (상위 50) ---")
        for i, (name, b) in enumerate(rows[:50]):
            print(f"{i+1:2d}. {name!r}: {b:,} bytes ({b / 1e9:.4f} GB)")
        total = sum(r[1] for r in rows)
        print(f"dbstat 합계(모든 객체): {total:,} bytes ({total / 1e9:.4f} GB)")
    except sqlite3.Error as e:
        print(f"dbstat 실패 (SQLite 빌드에 dbstat 없을 수 있음): {e}")

    print()
    print("--- 테이블·인덱스 목록 (sqlite_master) ---")
    cur.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE type IN ('table','index') ORDER BY type, name"
    )
    all_objs: List[Tuple[Any, ...]] = cur.fetchall()
    for typ, name, tbl_name, sql in all_objs:
        head = (sql or "")[:120].replace("\n", " ")
        print(f"{typ:6s} {name!s:40s} tbl={tbl_name!s}")
        if sql and len(sql) > 120:
            print(f"       {head}...")

    print()
    print("--- sqlite_stat1 (ANALYZE 실행 시 행 수 추정) ---")
    try:
        cur.execute(
            "SELECT tbl, idx, stat FROM sqlite_stat1 ORDER BY stat DESC LIMIT 30"
        )
        for row in cur.fetchall():
            print(row)
    except sqlite3.Error as e:
        print(f"sqlite_stat1 없음/오류: {e}")

    do_count = "--count" in sys.argv
    big_tables = ["bars_1m", "bars_15m", "bars_1d", "sync_meta"]
    print()
    if do_count:
        print("--- 주요 테이블 COUNT(*) (느릴 수 있음) ---")
        for t in big_tables:
            try:
                n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                print(f"{t}: {n:,} rows")
            except sqlite3.Error as e:
                print(f"{t}: {e}")
    else:
        print("전체 COUNT(*)는 생략. 필요 시: python tools/audit_trading_db_size.py --count")

    print()
    print("--- PK와 컬럼 순서가 동일한 보조 인덱스 (중복 가능성) ---")
    for tbl in ("bars_1m", "bars_15m", "bars_1d"):
        try:
            pk_cols = _pk_columns(cur, tbl)
            if not pk_cols:
                continue
            idxs = cur.execute(
                "SELECT name FROM pragma_index_list(?) WHERE origin='c'",
                (tbl,),
            ).fetchall()
            for (iname,) in idxs:
                if str(iname).startswith("sqlite_autoindex"):
                    continue
                icols = _index_columns(cur, str(iname))
                if icols == pk_cols:
                    print(
                        f"  [중복 의심] {tbl}: 인덱스 {iname!r} == PK 컬럼 {pk_cols}"
                    )
        except sqlite3.Error as e:
            print(f"  {tbl}: {e}")

    conn.close()


if __name__ == "__main__":
    main()
