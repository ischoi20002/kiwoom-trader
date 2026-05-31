# -*- coding: utf-8 -*-
"""
기존 bars_1d(1m 집계)를 FDR 일봉으로 백필·교체합니다.

- 하베스팅·백테 선정·감시 선정(FDR)과 동일 일봉 소스로 맞추기 위한 1회(또는 주기) 작업
- bars_1m / bars_15m 은 건드리지 않음

사용 예::

    python tools/backfill_bars_1d_from_fdr.py
    python tools/backfill_bars_1d_from_fdr.py --start 2023-01-01 --end 2026-05-29
    python tools/backfill_bars_1d_from_fdr.py --sleep 0.15
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, NoReturn, Optional


def _die(msg: str, code: int = 1) -> NoReturn:
    try:
        print(msg, file=sys.stderr)
    except Exception:
        pass
    raise SystemExit(code)


def _load_settings(root: str) -> Dict[str, Any]:
    sys.path.insert(0, root)
    from core.defaults import DEFAULT_SETTINGS

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


def _load_universe(settings: Dict[str, Any], log: Callable[[str], None]) -> List[str]:
    from strategy_selector import (
        load_kodex200_universe_codes,
        load_kospi_universe_codes,
    )

    choice = str(settings.get("universe_index_choice", "KODEX200")).strip().upper()
    if choice == "KOSPI200":
        try:
            top_n = int(settings.get("kospi_top_n", 200))
        except (TypeError, ValueError):
            top_n = 200
        codes = load_kospi_universe_codes(logger_callback=log, top_n=top_n)
    else:
        codes = load_kodex200_universe_codes(logger_callback=log)
    out: List[str] = []
    seen: set[str] = set()
    for c in codes or []:
        s = str(c).strip().zfill(6)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def main() -> int:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)

    parser = argparse.ArgumentParser(
        description="bars_1d 를 FDR 일봉으로 백필 (1m 집계 대체)"
    )
    parser.add_argument(
        "--db",
        default=None,
        help="trading_data.db 경로 (미지정 시 DataManagerV2 기본)",
    )
    parser.add_argument(
        "--start",
        default=None,
        metavar="YYYY-MM-DD",
        help="시작일 (미지정: end - lookback)",
    )
    parser.add_argument(
        "--end",
        default=None,
        metavar="YYYY-MM-DD",
        help="종료일 (미지정: required_eod_bar_date_asof_local)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=400,
        help="start 미지정 시 end 기준 lookback 일수 (기본 400, 지표 warm-up)",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.12,
        help="종목 간 FDR 호출 간격(초)",
    )
    parser.add_argument(
        "--codes",
        nargs="*",
        default=None,
        help="6자리 종목코드 (미지정 시 settings 유니버스)",
    )
    args = parser.parse_args()

    settings = _load_settings(root)
    from data_manager_v2 import DataManagerV2
    from reporting.krx_trading_calendar import required_eod_bar_date_asof_local

    dm = DataManagerV2(args.db) if args.db else DataManagerV2()

    if args.end:
        try:
            end_d = datetime.strptime(str(args.end)[:10], "%Y-%m-%d").date()
        except ValueError as e:
            _die(f"[오류] --end 형식: {e}")
    else:
        end_d = required_eod_bar_date_asof_local()

    if args.start:
        try:
            start_d = datetime.strptime(str(args.start)[:10], "%Y-%m-%d").date()
        except ValueError as e:
            _die(f"[오류] --start 형식: {e}")
    else:
        lb = max(60, int(args.lookback_days))
        start_d = end_d - timedelta(days=lb)

    if start_d > end_d:
        _die("[오류] start > end")

    codes: List[str]
    if args.codes:
        codes = [str(c).strip().zfill(6) for c in args.codes if c]
    else:
        codes = _load_universe(settings, lambda m: print(m))

    if not codes:
        _die("[오류] 유니버스 종목이 없습니다.")

    start_s = start_d.strftime("%Y-%m-%d")
    end_s = end_d.strftime("%Y-%m-%d")
    print(f"[백필] FDR → bars_1d | {start_s} ~ {end_s} | {len(codes)}종목")
    print(f"[백필] DB: {dm.db_path}")

    ok_n = 0
    fail_n = 0
    for i, code in enumerate(codes, 1):
        try:
            ok = dm.sync_daily_bars_from_fdr_range(code, start_s, end_s)
            if ok:
                ok_n += 1
            else:
                fail_n += 1
                if fail_n <= 10:
                    print(f"  ⚠️ [{i}/{len(codes)}] {code}: FDR 데이터 없음/실패")
        except Exception as e:
            fail_n += 1
            print(f"  ❌ [{i}/{len(codes)}] {code}: {e}")
        if args.sleep > 0 and i < len(codes):
            time.sleep(float(args.sleep))

    print(f"[완료] 성공 {ok_n}, 실패/빈데이터 {fail_n}, 합계 {len(codes)}")
    try:
        dm.run_analyze()
    except Exception:
        pass
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
