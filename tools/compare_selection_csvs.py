# -*- coding: utf-8 -*-
"""
감시 선정 결과 CSV 둘을 ``Code`` 열 기준으로 비교합니다.

기본 비교 대상(프로젝트 루트 기준)::

    selected_targets.csv
        GUI 「일일 감시목록 선정」버튼 → ``select_monitoring_targets`` 실행 후
        (``strategy_selector`` 가 ``db_only`` 가 아닐 때만 자동 저장.
         ``db_only`` 전용이면 이 파일이 갱신되지 않을 수 있음.)

    selected_targets_from_db.csv
        ``python tools/run_daily_selection_from_db.py`` 로 생성.
        동일 ``select_monitoring_targets`` + ``selection_ohlcv_source="db_only"``.
        (주석대로 **백테스트 엔진은 돌리지 않음** — 일봉 DB만으로 선정만 실행.)

사용::

    python tools/compare_selection_csvs.py
    python tools/compare_selection_csvs.py --a a.csv --b b.csv
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List


def _codes_from_csv(path: str) -> List[str]:
    import pandas as pd

    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    df = pd.read_csv(path, dtype=str)
    if "Code" not in df.columns:
        raise ValueError(f"{path}: 'Code' 열이 없습니다.")
    return df["Code"].astype(str).str.zfill(6).tolist()


def main() -> int:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    ap = argparse.ArgumentParser(description="선정 결과 CSV 두 개 Code 목록 비교")
    ap.add_argument(
        "--a",
        default=os.path.join(root, "selected_targets.csv"),
        metavar="PATH",
        help="기준 A (기본: GUI 선정 캐시 selected_targets.csv)",
    )
    ap.add_argument(
        "--b",
        default=os.path.join(root, "selected_targets_from_db.csv"),
        metavar="PATH",
        help="기준 B (기본: run_daily_selection_from_db 출력)",
    )
    args = ap.parse_args()

    try:
        a = _codes_from_csv(args.a)
        b = _codes_from_csv(args.b)
    except Exception as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 1

    sa, sb = set(a), set(b)
    print("=== 파일 ===")
    print("A:", os.path.abspath(args.a))
    print("B:", os.path.abspath(args.b))
    print("=== 요약 ===")
    print(f"A 개수: {len(a)}  B 개수: {len(b)}")
    print(f"집합 동일: {sa == sb}")
    print(f"순서 동일: {a == b}")
    if sa != sb:
        print("A 에만:", sorted(sa - sb))
        print("B 에만:", sorted(sb - sa))
    elif a != b:
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                print(f"첫 순서 불일치 인덱스 {i}: A={x} B={y}")
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
