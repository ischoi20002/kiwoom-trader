"""
장중 RT 15m 봉 누적 상태 점검(수정 0.6.38 효과 1차 검증).

- RT DB(realtime_15m_mock.db.bars_15m_rt)에 오늘 적재된 종목·슬롯 현황
- 디버그 코드(042700, 454910)의 0B periodic 로그 차분(log_diff) vs RT DB 봉 거래량(rt_v) 비교
  - log_diff: 키움 0B 누적거래량(fid 13) max-min — 슬롯 동안의 실제 거래량 근사치
  - rt_v: stream_processor 차분 누적 결과
  - **rt_v / log_diff 가 90%+** 면 수정 효과 정상.
"""
from __future__ import annotations
import json
import sqlite3
from datetime import date, datetime
from collections import defaultdict
from typing import Any

DAY = date.today().strftime("%Y-%m-%d")
DEBUG_CODES = ("042700", "454910", "016360", "267250")


def slot_label(ts: str) -> str:
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    m = dt.minute
    target_m = ((m // 15) + 1) * 15
    target_h = dt.hour
    if target_m == 60:
        target_m = 0
        target_h += 1
    return dt.strftime(f"%Y-%m-%d {target_h:02d}:{target_m:02d}:00")


def rt_db_summary() -> None:
    con = sqlite3.connect("realtime_15m_mock.db")
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT DISTINCT code FROM bars_15m_rt WHERE substr(datetime,1,10)=? ORDER BY code",
            (DAY,),
        )
        codes = [r[0] for r in cur.fetchall()]
        print(f"[{DAY}] RT DB 적재 종목수: {len(codes)}")
        cur.execute(
            "SELECT datetime, COUNT(*), SUM(volume) FROM bars_15m_rt "
            "WHERE substr(datetime,1,10)=? GROUP BY datetime ORDER BY datetime",
            (DAY,),
        )
        print("\n[슬롯별 적재 현황]")
        print(f"{'slot':<22}  {'종목수':>5}  {'합계 거래량':>14}")
        for ts, n, vsum in cur.fetchall():
            vsum = int(vsum or 0)
            print(f"{ts:<22}  {n:>5}  {vsum:>14,d}")
    finally:
        con.close()


def fetch_rt_bars_for_code(code: str) -> dict[str, dict[str, Any]]:
    con = sqlite3.connect("realtime_15m_mock.db")
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT datetime, open, high, low, close, volume FROM bars_15m_rt "
            "WHERE code=? AND substr(datetime,1,10)=? ORDER BY datetime",
            (code, DAY),
        )
        out: dict[str, dict[str, Any]] = {}
        for r in cur.fetchall():
            out[r[0]] = {"O": r[1], "H": r[2], "L": r[3], "C": r[4], "V": r[5]}
        return out
    finally:
        con.close()


def debug_code_check() -> None:
    log_path = f"realtime_session_logs/{DAY}/session_events.jsonl"
    print(f"\n[0B periodic 로그 vs RT 봉 거래량 검증]  source={log_path}")
    try:
        bucket: dict[tuple[str, str], list[int]] = defaultdict(list)
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                if j.get("kind") != "ws_real_0b_seen_periodic":
                    continue
                d = j.get("data", {})
                code = str(d.get("code", ""))
                if code not in DEBUG_CODES:
                    continue
                ts = j.get("ts", "")
                v = int(d.get("volume", 0) or 0)
                if v <= 0:
                    continue
                bucket[(code, slot_label(ts))].append(v)
    except FileNotFoundError:
        print(f"  세션 로그 파일을 찾을 수 없음: {log_path}")
        return

    if not bucket:
        print("  디버그 코드(042700/454910 등)의 0B periodic 로그가 아직 없음.")
        return

    by_code: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for (code, slot), vs in bucket.items():
        by_code[code].append((slot, max(vs) - min(vs)))

    for code in sorted(by_code):
        rt_d = fetch_rt_bars_for_code(code)
        print(f"\n=== {code} ===")
        print(f"  {'slot':<22}  {'log_diff':>10}  {'rt_v':>10}  {'ratio':>8}")
        ratios: list[float] = []
        for slot, log_diff in sorted(by_code[code]):
            rt = rt_d.get(slot)
            rt_v = int(rt["V"]) if rt else None
            ratio_s = "  -  "
            if rt_v is not None and log_diff > 0:
                ratio = rt_v / log_diff
                ratios.append(ratio)
                ratio_s = f"{ratio:>8.2%}"
            print(
                f"  {slot:<22}  {log_diff:>10d}  "
                f"{(str(rt_v) if rt_v is not None else '-'):>10}  {ratio_s}"
            )
        if ratios:
            avg = sum(ratios) / len(ratios)
            print(f"  → 평균 ratio = {avg:.2%}  (수정 0.6.38 효과 지표)")


def main() -> None:
    rt_db_summary()
    debug_code_check()


if __name__ == "__main__":
    main()
