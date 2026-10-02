# -*- coding: utf-8 -*-
"""
bars_1d 에 수정 전/후 주가가 섞인 종목을 찾아 FDR(수정주가) 전체 이력으로 재저장합니다.

증분 동기화가 최근 며칠만 덧붙이던 시절, 배당·분할 등 이벤트 이후 FDR 이 과거 가격을
소급 수정해도 DB 과거 구간은 수정 전 주가로 남았습니다. 종목별로 DB 전 구간을 FDR 과
비교해 불일치가 있으면 DB 최초일부터 다시 저장합니다. (bars_1m / bars_15m 은 건드리지 않음)

사용 예::

    python tools/repair_bars_1d_adjusted.py --dry-run
    python tools/repair_bars_1d_adjusted.py
    python tools/repair_bars_1d_adjusted.py --codes 207940 005930
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List


def _all_stock_codes(dm: Any) -> List[str]:
    """bars_1d 에 있는 종목 코드 (지수 코드 제외)."""
    with dm._get_connection() as conn:
        rows = conn.execute("SELECT DISTINCT code FROM bars_1d ORDER BY code").fetchall()
    out: List[str] = []
    for (c,) in rows:
        s = str(c).strip()
        if len(s) == 6 and s[0].isdigit():
            out.append(s)
    return out


def main() -> int:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)

    parser = argparse.ArgumentParser(description="bars_1d 수정주가 혼재 일괄 정리 (FDR 기준)")
    parser.add_argument("--db", default=None, help="trading_data.db 경로 (미지정 시 DataManagerV2 기본)")
    parser.add_argument("--codes", nargs="*", default=None, help="6자리 종목코드 (미지정 시 bars_1d 전 종목)")
    parser.add_argument("--dry-run", action="store_true", help="불일치 종목만 보고하고 저장하지 않음")
    parser.add_argument("--sleep", type=float, default=0.1, help="종목 간 FDR 호출 간격(초)")
    parser.add_argument("--report", default=None, help="결과 JSON 경로 (미지정 시 logs/ 아래 자동)")
    args = parser.parse_args()

    from data_manager_v2 import DataManagerV2

    try:
        dm = DataManagerV2(args.db) if args.db else DataManagerV2()
    except Exception as e:
        print(f"[오류] DB 열기 실패: {e}", file=sys.stderr)
        return 1

    try:
        codes = [str(c).strip().zfill(6) for c in args.codes if c] if args.codes else _all_stock_codes(dm)
    except Exception as e:
        print(f"[오류] 종목 목록 조회 실패: {e}", file=sys.stderr)
        return 1
    if not codes:
        print("[오류] 대상 종목이 없습니다.", file=sys.stderr)
        return 1

    mode = "점검만(dry-run)" if args.dry_run else "점검+재저장"
    print(f"[수정주가 정리] {mode} | {len(codes)}종목 | DB: {dm.db_path}")

    results: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    for i, code in enumerate(codes, 1):
        r = dm.repair_daily_adjustment_for_code(code, dry_run=args.dry_run)
        results.append(r)
        st = str(r.get("status"))
        counts[st] = counts.get(st, 0) + 1
        if st not in ("ok", "no_fdr", "no_db"):
            detail = ""
            if "date" in r:
                detail = (
                    f" {r['date']} DB {r['db_close']:,.0f} vs FDR {r['fdr_close']:,.0f}"
                    f" ({r['rel'] * 100:.2f}%, {r['n_mismatch']}/{r['n_compared']}일)"
                )
            print(f"  [{i}/{len(codes)}] {code} {st}{detail}{' ' + r.get('error', '') if st == 'error' else ''}")
        elif i % 100 == 0:
            print(f"  ... {i}/{len(codes)} 진행")
        if args.sleep > 0 and i < len(codes):
            time.sleep(float(args.sleep))

    print(f"[완료] {counts}")

    report = args.report or os.path.join(
        root, "logs", f"repair_bars_1d_adjusted_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    )
    try:
        os.makedirs(os.path.dirname(report), exist_ok=True)
        with open(report, "w", encoding="utf-8") as f:
            json.dump({"dry_run": args.dry_run, "counts": counts, "results": results}, f, ensure_ascii=False, indent=1)
        print(f"[리포트] {report}")
    except OSError as e:
        print(f"[경고] 리포트 저장 실패: {e}")

    if not args.dry_run:
        try:
            dm.run_analyze()
        except Exception:
            pass
    return 0 if counts.get("error", 0) == 0 and counts.get("repair_failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
