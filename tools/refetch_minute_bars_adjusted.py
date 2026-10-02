# -*- coding: utf-8 -*-
"""
분봉(bars_1m / bars_15m)을 키움 ka10080 수정주가(upd_stkpc_tp=1)로 다시 받아 교체합니다.

분할·병합 등 이벤트 이후에도 DB 분봉은 수집 당시 가격(수정 전)으로 남아 15분봉 지표에
가짜 급등락이 생깁니다. 키움은 분봉을 최근 약 1년만 제공하므로 그 구간만 교체되며,
그 이전 분봉은 그대로 남습니다. (하베스팅은 겹침 비교로 자동 재수신 — 이 도구는 일괄/수동용)

- 종목별로 API 바닥(next-key 없음)까지 전부 받은 경우에만 [받은 최초 시각, 받은 최종 시각]
  구간의 DB 행을 삭제 후 새 값으로 저장 (중간 실패 시 해당 종목은 손대지 않음)
- 교체 전 해당 구간 분봉은 --backup-db 에 보관

사용 예::

    python tools/refetch_minute_bars_adjusted.py --detect-jumps --dry-run
    python tools/refetch_minute_bars_adjusted.py --detect-jumps
    python tools/refetch_minute_bars_adjusted.py --codes 010120 091810
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from typing import Any, Dict, List

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from data_manager.minute_adjust_refetch import (  # noqa: E402
    fetch_all_minute_pages,
    refetch_and_replace_code,
)

# 연속 거래일 종가 비율이 이 범위를 벗어나면 분봉 가격 점프(분할·병합 등)로 간주
JUMP_UP = 1.35
JUMP_DOWN = 0.70


def detect_jump_codes(db_path: str) -> List[str]:
    """bars_15m 일별 마지막 정규장 종가 기준으로 하루 점프가 있는 종목 코드."""
    import pandas as pd

    out: List[str] = []
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        codes = [r[0] for r in conn.execute("SELECT DISTINCT code FROM bars_15m").fetchall()]
        for code in codes:
            try:
                m = pd.read_sql_query(
                    "SELECT substr(datetime,1,10) d, close FROM bars_15m "
                    "WHERE code=? AND substr(datetime,12,5) <= '15:30' ORDER BY datetime",
                    conn,
                    params=(code,),
                )
                if m.empty:
                    continue
                last = m.groupby("d")["close"].last()
                r = last / last.shift(1)
                if ((r > JUMP_UP) | (r < JUMP_DOWN)).any():
                    out.append(str(code))
            except Exception as e:
                print(f"  ⚠️ {code} 점프 검사 실패: {e}")
    return out


def _dry_run_code(hs: Any, db_path: str, code: str, token: str, cap: Any) -> Dict[str, Any]:
    """DB 는 바꾸지 않고 키움 값과 다른 기존 행 수만 센다."""
    rec: Dict[str, Any] = {"code": code}
    for table, scope in (("bars_1m", "1"), ("bars_15m", "15")):
        bars = fetch_all_minute_pages(hs._fetch_minute_data, hs._parse_to_bars, code, token, scope, cap)
        if bars is None:
            rec[table] = {"status": "fetch_failed"}
            continue
        if not bars:
            rec[table] = {"status": "no_data"}
            continue
        info: Dict[str, Any] = {"status": "dry_run", "from": bars[0]["time"], "to": bars[-1]["time"], "n": len(bars)}
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            old = dict(conn.execute(
                f"SELECT datetime, close FROM {table} WHERE code=? AND datetime BETWEEN ? AND ?",
                (code, info["from"], info["to"]),
            ).fetchall())
        info["changed"] = sum(
            1 for b in bars if b["time"] in old and int(old[b["time"]] or 0) != int(b["close"])
        )
        rec[table] = info
    return rec


def main() -> int:
    os.chdir(_ROOT)
    parser = argparse.ArgumentParser(description="분봉 키움 수정주가 재수신·교체")
    parser.add_argument("--codes", nargs="*", default=None, help="6자리 종목코드")
    parser.add_argument("--detect-jumps", action="store_true", help="bars_15m 가격 점프 종목 자동 선정")
    parser.add_argument("--dry-run", action="store_true", help="조회·비교만 하고 DB 는 바꾸지 않음")
    parser.add_argument(
        "--backup-db",
        default=os.path.join(_ROOT, "data", f"backup_minute_before_adj_refetch_{datetime.now():%Y%m%d}.db"),
        help="교체 전 분봉 백업 DB 경로 ('' 이면 백업 안 함)",
    )
    parser.add_argument("--report", default=None, help="결과 JSON 경로")
    args = parser.parse_args()

    from data_manager_v2 import DataManagerV2
    from harvest_service import HarvestService

    db_path = os.path.abspath(DataManagerV2.DEFAULT_DB_PATH)
    codes: List[str] = [str(c).strip().zfill(6) for c in (args.codes or []) if str(c).strip()]
    if args.detect_jumps:
        found = detect_jump_codes(db_path)
        print(f"[점프 감지] {len(found)}종목")
        codes = sorted(set(codes) | set(found))
    if not codes:
        print("[오류] 대상 종목이 없습니다 (--codes 또는 --detect-jumps)", file=sys.stderr)
        return 1

    try:
        from reporting.krx_trading_calendar import minute_harvest_datetime_cap_iso

        cap = minute_harvest_datetime_cap_iso()
    except Exception:
        cap = None

    hs = HarvestService()
    token = hs.token_manager.get_auth_token()
    if not token:
        print("[오류] 키움 토큰 발급 실패", file=sys.stderr)
        return 1
    backup_db = (args.backup_db or "").strip() or None
    if backup_db and not args.dry_run:
        os.makedirs(os.path.dirname(backup_db), exist_ok=True)

    mode = "조회·비교만(dry-run)" if args.dry_run else "재수신+교체"
    print(f"[분봉 재수신] {mode} | {len(codes)}종목 | DB: {db_path} | cap={cap}")
    results: List[Dict[str, Any]] = []
    t_all = time.time()
    for i, code in enumerate(codes, 1):
        t0 = time.time()
        try:
            # 토큰 만료 대비 종목마다 재확인 (AuthManager 가 캐시·갱신)
            token = hs.token_manager.get_auth_token() or token
            if args.dry_run:
                rec = _dry_run_code(hs, db_path, code, token, cap)
            else:
                rec = refetch_and_replace_code(
                    hs._fetch_minute_data, hs._parse_to_bars, db_path, code, token, cap, backup_db
                )
        except Exception as e:
            rec = {"code": code, "error": str(e)}
        rec["sec"] = round(time.time() - t0, 1)
        results.append(rec)
        m1 = rec.get("bars_1m", {})
        m15 = rec.get("bars_15m", {})
        print(
            f"  [{i}/{len(codes)}] {code} 1m={m1.get('status')} 변경 {m1.get('changed', '-')}/{m1.get('n', '-')} "
            f"| 15m={m15.get('status')} 변경 {m15.get('changed', '-')}/{m15.get('n', '-')} "
            f"| {str(m1.get('from', ''))[:10]}~ | {rec['sec']}s{' | 오류 ' + rec['error'] if 'error' in rec else ''}",
            flush=True,
        )

    ok = sum(
        1 for r in results
        if "error" not in r and all(r.get(t, {}).get("status") in ("replaced", "dry_run", "no_data") for t in ("bars_1m", "bars_15m"))
    )
    print(f"[완료] 정상 {ok}/{len(results)} | {time.time() - t_all:.0f}s")
    report = args.report or os.path.join(
        _ROOT, "logs", f"refetch_minute_bars_adjusted_{datetime.now():%Y%m%d_%H%M%S}.json"
    )
    try:
        os.makedirs(os.path.dirname(report), exist_ok=True)
        with open(report, "w", encoding="utf-8") as f:
            json.dump({"dry_run": args.dry_run, "cap": cap, "results": results}, f, ensure_ascii=False, indent=1)
        print(f"[리포트] {report}")
    except OSError as e:
        print(f"[경고] 리포트 저장 실패: {e}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
