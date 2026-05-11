# -*- coding: utf-8 -*-
"""
로컬 ``trading_data.db`` 의 bars_1d 만으로 종목 선정을 실행합니다.

- GUI 「일일 감시목록 선정」과 동일한 ``select_monitoring_targets`` 로직
- ``selection_ohlcv_source="db_only"`` → FDR 일봉 폴백 없음(하베스팅 데이터와 정합 비교용)
- 백테스트 엔진은 돌리지 않습니다.

사용 예::

    python tools/run_daily_selection_from_db.py
    python tools/run_daily_selection_from_db.py --out my_pick.csv
    python tools/run_daily_selection_from_db.py --db C:/data/trading_data.db

GUI 로 저장된 ``selected_targets.csv`` 와 코드 목록만 빠르게 비교하려면::

    python tools/compare_selection_csvs.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, NoReturn


def _die(msg: str, code: int = 1) -> NoReturn:
    try:
        print(msg, file=sys.stderr)
    except Exception:
        pass
    raise SystemExit(code)


def _load_merged_settings(root: str) -> Dict[str, Any]:
    try:
        from core.defaults import DEFAULT_SETTINGS
    except Exception as e:
        _die(f"[오류] DEFAULT_SETTINGS 로드 실패: {e}")

    merged: Dict[str, Any] = dict(DEFAULT_SETTINGS)
    cfg = os.path.join(root, "settings.json")
    if os.path.isfile(cfg):
        try:
            with open(cfg, encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                merged.update(user)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            _die(f"[오류] settings.json 읽기 실패: {e}")
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(
        description="trading_data.db 일봉만으로 감시 종목 선정 (FDR 미사용)"
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help="trading_data.db 절대/상대 경로 (미지정 시 data_manager 기본 경로)",
    )
    parser.add_argument(
        "--out",
        metavar="CSV",
        default="selected_targets_from_db.csv",
        help="선정 결과 CSV (기본: 프로젝트 루트 selected_targets_from_db.csv)",
    )
    parser.add_argument(
        "--also-selected-targets",
        action="store_true",
        help="추가로 selected_targets.csv 에도 동일 내용 저장(GUI 캐시 파일명)",
    )
    args = parser.parse_args()

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)

    settings = _load_merged_settings(root)

    try:
        from data_manager_v2 import DataManagerV2
    except Exception as e:
        _die(f"[오류] DataManagerV2 임포트 실패: {e}")

    try:
        import strategy_selector
    except Exception as e:
        _die(f"[오류] strategy_selector 임포트 실패: {e}")

    db_path: str | None = args.db
    if db_path:
        db_path = os.path.abspath(db_path)

    def _log(line: str) -> None:
        try:
            print(line)
        except Exception:
            pass

    try:
        dm = DataManagerV2(db_path=db_path) if db_path else DataManagerV2()
    except Exception as e:
        _die(f"[오류] DataManagerV2 초기화 실패: {e}")

    try:
        df = strategy_selector.select_monitoring_targets(
            settings=settings,
            logger_callback=_log,
            target_date=None,
            data_manager=dm,
            selection_ohlcv_source="db_only",
        )
    except strategy_selector.SelectionDbStaleError as e:
        _die(f"[중단] 일봉 DB가 최신 장마감 기준으로 충분하지 않습니다: {e}", code=2)
    except Exception as e:
        _die(f"[오류] 선정 실행 실패: {e}")

    if df is None or df.empty:
        print("[결과] 조건에 맞는 종목이 없거나 DB 일봉이 부족합니다.")
        return 0

    out_path = os.path.abspath(args.out) if not os.path.isabs(args.out) else args.out
    try:
        df.to_csv(out_path, index=False, encoding="utf-8-sig")
    except Exception as e:
        _die(f"[오류] CSV 저장 실패 ({out_path}): {e}")

    print(f"[완료] {len(df)}개 종목 → {out_path}")

    if args.also_selected_targets:
        alt = os.path.join(root, "selected_targets.csv")
        try:
            df.to_csv(alt, index=False, encoding="utf-8-sig")
            print(f"[완료] 동일 내용 → {alt}")
        except Exception as e:
            _die(f"[오류] selected_targets.csv 저장 실패: {e}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
