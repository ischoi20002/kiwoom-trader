"""
042700(디버그 종목)의 0B 누적거래량(fid 13) 변화량과 RT 15m봉 거래량을 비교해
stream_processor의 차분 누적 로직이 정상 작동했는지를 검증합니다.

세션 로그 ws_real_0b_seen_periodic 의 'volume' 필드(=fid 13, 누적거래량) 시계열에서
각 15분 슬롯 (09:00~09:15, 09:15~09:30, ...) 동안의 max(volume) - min(volume) 을 계산해
RT DB / TD DB 의 같은 시간대 봉 거래량과 비교합니다.
"""
from __future__ import annotations
import json
import sqlite3
from datetime import datetime
from collections import defaultdict
from typing import Any

CODE = "042700"
DAY = "2026-05-07"
LOG_PATH = f"realtime_session_logs/{DAY}/session_events.jsonl"


def slot_label(ts: str) -> str:
    """timestamp(YYYY-MM-DD HH:MM:SS) → 다음 15분 슬롯 라벨(YYYY-MM-DD HH:MM:00)."""
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    m = dt.minute
    target_m = ((m // 15) + 1) * 15
    target_h = dt.hour
    if target_m == 60:
        target_m = 0
        target_h += 1
    return dt.strftime(f"%Y-%m-%d {target_h:02d}:{target_m:02d}:00")


def main() -> None:
    bucket: dict[str, list[int]] = defaultdict(list)
    seen_ts: list[str] = []
    with open(LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("kind") != "ws_real_0b_seen_periodic":
                continue
            d = j.get("data", {})
            if str(d.get("code", "")) != CODE:
                continue
            ts = j.get("ts", "")
            v = int(d.get("volume", 0) or 0)
            if v <= 0:
                continue
            slot = slot_label(ts)
            bucket[slot].append(v)
            seen_ts.append(ts)

    print(f"[{CODE}] 0B periodic samples: {len(seen_ts)}")
    if seen_ts:
        print(f"  range: {seen_ts[0]} ~ {seen_ts[-1]}")
    print()

    rt_con = sqlite3.connect("realtime_15m_mock.db")
    td_con = sqlite3.connect("trading_data.db")
    try:
        print(f"{'slot':<22}  {'log_diff':>10}  {'rt_v':>10}  {'td_v':>10}")
        for slot in sorted(bucket):
            vs = bucket[slot]
            log_diff = max(vs) - min(vs)
            cur = rt_con.cursor()
            cur.execute(
                "SELECT volume FROM bars_15m_rt WHERE code=? AND datetime=?",
                (CODE, slot),
            )
            r = cur.fetchone()
            rt_v = r[0] if r else None
            cur = td_con.cursor()
            cur.execute(
                "SELECT volume FROM bars_15m WHERE code=? AND datetime=?",
                (CODE, slot),
            )
            r = cur.fetchone()
            td_v = r[0] if r else None
            print(f"{slot:<22}  {log_diff:>10d}  {str(rt_v):>10}  {str(td_v):>10}")
    finally:
        rt_con.close()
        td_con.close()


if __name__ == "__main__":
    main()
