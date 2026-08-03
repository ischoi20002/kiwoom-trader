# -*- coding: utf-8 -*-
"""
bars_1m 이 있는 전 종목에 대해 bars_15m 을 KRX 규칙(utils.krx_bars)으로 재생성.

사용: 프로젝트 루트에서
  python tools/refresh_all_bars_15m_krx.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime

# 프로젝트 루트
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_manager_v2 import DataManagerV2  # noqa: E402
from utils.bar_resample import refresh_resampled_data_at_path  # noqa: E402


def main() -> int:
    t_all = time.time()
    try:
        dm = DataManagerV2()
        db_path = str(dm.db_path)
    except (OSError, TypeError) as e:
        print(f"[실패] DataManager: {e!r}", flush=True)
        return 1
    try:
        with sqlite3.connect(db_path, timeout=120) as conn:
            rows = conn.execute(
                "SELECT DISTINCT code FROM bars_1m ORDER BY code"
            ).fetchall()
    except (sqlite3.Error, OSError) as e:
        print(f"[실패] DB 조회: {e!r}", flush=True)
        return 1
    codes = [r[0] for r in rows]
    n = len(codes)
    print(
        f"[시작] {datetime.now().isoformat(timespec='seconds')}  "
        f"DB={db_path}  대상 {n}종목 (bars_1m 기준)",
        flush=True,
    )
    err_list: list[tuple[str, str]] = []
    for i, code in enumerate(codes, 1):
        t0 = time.time()
        try:
            err = refresh_resampled_data_at_path(db_path, code)
        except (KeyboardInterrupt, SystemExit) as e:
            raise
        except Exception as e:  # noqa: BLE001
            err = str(e)
            try:
                print(f"  [{i}/{n}] {code} 예외: {e!r}", flush=True)
            except (OSError, TypeError) as e2:
                print(f"  [{i}/{n}] {code} 예외(로그 실패): {e2!r}", flush=True)
        if err:
            err_list.append((code, err))
            try:
                print(f"  [{i}/{n}] {code} 오류: {err[:200]}", flush=True)
            except (OSError, TypeError) as e2:
                pass
        if i == 1 or i % 10 == 0 or i == n:
            elapsed = time.time() - t_all
            rate = elapsed / max(i, 1)
            eta = rate * (n - i)
            try:
                print(
                    f"  … 진행 {i}/{n}  누적 {elapsed:.0f}s  "
                    f"ETA ~{eta/60:.1f}분  최근 {code} {time.time()-t0:.2f}s",
                    flush=True,
                )
            except (OSError, TypeError) as e2:
                pass
    total = time.time() - t_all
    print(
        f"[완료] {datetime.now().isoformat(timespec='seconds')}  "
        f"총 {total:.1f}s  성공 {n - len(err_list)}/{n}  실패 {len(err_list)}",
        flush=True,
    )
    if err_list:
        p = os.path.join(_ROOT, "tools", "refresh_bars_15m_errors.log")
        try:
            with open(p, "w", encoding="utf-8") as f:
                for c, m in err_list:
                    f.write(f"{c}\t{m}\n")
            print(f"  실패 목록: {p}", flush=True)
        except (OSError, TypeError) as e:
            print(f"  실패 로그 쓰기 실패: {e!r}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[중단] KeyboardInterrupt", flush=True)
        raise SystemExit(130)
