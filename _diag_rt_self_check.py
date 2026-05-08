"""RT DB(realtime_15m_mock.db.bars_15m_rt) 자체 정합성 검증 — 외부 비교 기준 없을 때 1차 sanity.

장중에 trading_data.db 가 비어있거나 0B periodic 로그가 없을 때,
RT DB 봉 자체의 _내부 일관성_ 만이라도 빠르게 확인합니다.

체크 항목
1. OHLC sanity:  L ≤ min(O,C), H ≥ max(O,C), O>0, V≥0
2. 슬롯별 적재 종목 수 / 합계 거래량 / 평균 거래량 추이
3. Volume ‘0’ 봉 및 의심스럽게 작은 봉 (직전 봉 대비 1% 미만) 카운트
4. 슬롯 마감 시각 vs 현재 시각 (진행 중 슬롯 식별)
"""
from __future__ import annotations
import sqlite3
from datetime import date, datetime
from collections import defaultdict
import sys

DB = "realtime_15m_mock.db"
DAY = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y-%m-%d")


def main() -> None:
    con = sqlite3.connect(DB)
    cur = con.cursor()

    # 슬롯별 집계
    cur.execute(
        "SELECT datetime, COUNT(*), SUM(volume), AVG(volume), "
        "  MIN(volume), MAX(volume) "
        "FROM bars_15m_rt WHERE substr(datetime,1,10)=? "
        "GROUP BY datetime ORDER BY datetime",
        (DAY,),
    )
    rows = cur.fetchall()
    print(f"[{DAY}] 슬롯별 RT DB 적재 현황")
    print(f"  {'slot':<22} {'종목':>4} {'합계 V':>14} {'평균 V':>10} "
          f"{'min V':>10} {'max V':>10}")
    for ts, n, vsum, vavg, vmin, vmax in rows:
        print(
            f"  {ts:<22} {n:>4d} {int(vsum or 0):>14,d} "
            f"{int(vavg or 0):>10,d} {int(vmin or 0):>10,d} "
            f"{int(vmax or 0):>10,d}"
        )

    # OHLC sanity check
    cur.execute(
        "SELECT code, datetime, open, high, low, close, volume "
        "FROM bars_15m_rt WHERE substr(datetime,1,10)=?",
        (DAY,),
    )
    bars = cur.fetchall()
    print(f"\n총 봉 수: {len(bars)}")

    bad_sanity: list[tuple] = []
    zero_v: list[tuple] = []
    by_code: dict[str, list[tuple]] = defaultdict(list)
    for b in bars:
        code, ts, o, h, l, c, v = b
        try:
            o, h, l, c = float(o), float(h), float(l), float(c)
            v = int(v or 0)
        except Exception:
            bad_sanity.append((code, ts, "TYPE_ERR"))
            continue
        if o <= 0 or h <= 0 or l <= 0 or c <= 0:
            bad_sanity.append((code, ts, f"NEG O={o} H={h} L={l} C={c}"))
        elif l > min(o, c) + 1e-6:
            bad_sanity.append((code, ts, f"L>min(O,C) O={o} H={h} L={l} C={c}"))
        elif h < max(o, c) - 1e-6:
            bad_sanity.append((code, ts, f"H<max(O,C) O={o} H={h} L={l} C={c}"))
        if v == 0:
            zero_v.append((code, ts))
        by_code[code].append(b)

    print(f"OHLC sanity 위반: {len(bad_sanity)}")
    for code, ts, msg in bad_sanity[:20]:
        print(f"  {code}  {ts}  {msg}")
    if len(bad_sanity) > 20:
        print(f"  ... 외 {len(bad_sanity) - 20}건")

    print(f"\nVolume=0 봉: {len(zero_v)}")
    if zero_v[:10]:
        for c, t in zero_v[:10]:
            print(f"  {c}  {t}")

    # 종목별 슬롯간 거래량 추이 — 의심 케이스(이전 슬롯 대비 1% 미만, but >0)
    print("\n[의심 봉] 직전 슬롯 대비 거래량 1% 미만 (마지막 슬롯 제외)")
    last_slot = rows[-1][0] if rows else None
    suspect = 0
    for code in sorted(by_code):
        seq = sorted(by_code[code], key=lambda r: r[1])
        prev_v = None
        for b in seq:
            ts, v = b[1], int(b[6] or 0)
            if ts == last_slot:
                continue
            if prev_v is not None and prev_v > 0 and 0 < v < prev_v * 0.01:
                print(f"  {code}  {ts}  v={v:,d}  (prev={prev_v:,d})")
                suspect += 1
            prev_v = v
    print(f"  → 합계: {suspect}건")

    # 진행 중 슬롯 식별
    now_dt = datetime.now()
    if rows:
        last_ts = datetime.strptime(rows[-1][0], "%Y-%m-%d %H:%M:%S")
        delta_min = (now_dt - last_ts).total_seconds() / 60.0
        print(
            f"\n현재 시각: {now_dt.strftime('%H:%M:%S')}  "
            f"마지막 슬롯: {last_ts.strftime('%H:%M:%S')}  "
            f"({delta_min:+.1f}분)"
        )
        if -15 < delta_min <= 0:
            print("  → 마지막 슬롯은 _진행 중_ 으로 보임 (검증 시 마지막 슬롯 제외 권장)")

    con.close()


if __name__ == "__main__":
    main()
