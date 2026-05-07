"""
실시간 15m 봉 vs 백테스트 15m 봉 정합성 일반화 검증 스크립트.

- 특정 종목 하드코딩 없이 RT DB(realtime_15m_mock.db.bars_15m_rt)에 적재된
  당일(또는 지정일) _모든_ 종목을 자동으로 trading_data.db.bars_15m 과 비교합니다.
- 슬롯별 OHLCV 일치 비율, 거래량 차이 비율(rt_v / td_v), 마지막 봉 비교를
  종목별로 한 줄 요약하고, 가장 차이가 큰 종목 TOP-N 을 함께 표시합니다.

사용 예:
    py -3 _diag_today_check.py                   # 오늘
    py -3 _diag_today_check.py 2026-05-07        # 특정일
    py -3 _diag_today_check.py 2026-05-07 005380 # 특정일·특정 종목 상세
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import date
from typing import Any, Optional

RT_DB = "realtime_15m_mock.db"
TD_DB = "trading_data.db"


def list_rt_codes(day: str) -> list[str]:
    con = sqlite3.connect(RT_DB)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT DISTINCT code FROM bars_15m_rt "
            "WHERE substr(datetime,1,10)=? ORDER BY code",
            (day,),
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        con.close()


def fetch_bars(db: str, table: str, code: str, day: str) -> list[tuple[Any, ...]]:
    con = sqlite3.connect(db)
    try:
        cur = con.cursor()
        cur.execute(
            f"SELECT datetime, open, high, low, close, volume "
            f"FROM {table} WHERE code=? AND substr(datetime,1,10)=? "
            f"ORDER BY datetime",
            (code, day),
        )
        return cur.fetchall()
    finally:
        con.close()


def compare_one(code: str, day: str) -> dict[str, Any]:
    rt = fetch_bars(RT_DB, "bars_15m_rt", code, day)
    td = fetch_bars(TD_DB, "bars_15m", code, day)
    rt_d = {r[0]: r for r in rt}
    td_d = {r[0]: r for r in td}
    common = sorted(set(rt_d) & set(td_d))
    only_rt = sorted(set(rt_d) - set(td_d))
    only_td = sorted(set(td_d) - set(rt_d))

    ohlcv_diff_rows = 0
    sum_rt_v = 0
    sum_td_v = 0
    for ts in common:
        r = rt_d[ts]
        t = td_d[ts]
        if any(
            (a is None) ^ (b is None)
            or (a is not None and b is not None and abs(float(a) - float(b)) > 1e-6)
            for a, b in zip(r[1:6], t[1:6])
        ):
            ohlcv_diff_rows += 1
        sum_rt_v += int(r[5] or 0)
        sum_td_v += int(t[5] or 0)

    return {
        "code": code,
        "rt_bars": len(rt),
        "td_bars": len(td),
        "common": len(common),
        "only_rt": len(only_rt),
        "only_td": len(only_td),
        "ohlcv_diff": ohlcv_diff_rows,
        "sum_rt_v": sum_rt_v,
        "sum_td_v": sum_td_v,
        "v_ratio": (sum_rt_v / sum_td_v) if sum_td_v > 0 else None,
        "rt_last": rt[-1] if rt else None,
        "td_last": td[-1] if td else None,
    }


def print_detail(res: dict[str, Any], day: str) -> None:
    code = res["code"]
    rt = fetch_bars(RT_DB, "bars_15m_rt", code, day)
    td = fetch_bars(TD_DB, "bars_15m", code, day)
    rt_d = {r[0]: r for r in rt}
    td_d = {r[0]: r for r in td}
    print(
        f"=== {code}  rt={len(rt)}  td={len(td)}  common={res['common']}  "
        f"ohlcv_diff={res['ohlcv_diff']}  v_ratio={res['v_ratio']!r}"
    )
    for ts in sorted(set(rt_d) | set(td_d)):
        r = rt_d.get(ts)
        t = td_d.get(ts)
        if r is None:
            print(f"  [TD only] {ts}  TD V={t[5]}")
            continue
        if t is None:
            print(f"  [RT only] {ts}  RT V={r[5]}")
            continue
        rv = int(r[5] or 0)
        tv = int(t[5] or 0)
        ratio = (rv / tv) if tv > 0 else None
        print(
            f"  {ts}  RT(O={r[1]} H={r[2]} L={r[3]} C={r[4]} V={rv:>10})  "
            f"TD(O={t[1]} H={t[2]} L={t[3]} C={t[4]} V={tv:>10})  "
            f"v_ratio={ratio if ratio is None else f'{ratio:.2%}'}"
        )


def main(argv: list[str]) -> None:
    day = argv[1] if len(argv) > 1 else date.today().strftime("%Y-%m-%d")
    target_code: Optional[str] = argv[2] if len(argv) > 2 else None

    codes = list_rt_codes(day)
    print(f"[{day}] RT DB 적재 종목: {len(codes)}개")
    if not codes:
        print("RT DB에 해당일 봉이 없습니다.")
        return

    results = [compare_one(c, day) for c in codes]

    print(
        f"\n{'code':<8} {'rt':>4} {'td':>4} {'cmn':>4} {'diff':>4} "
        f"{'sum_rt_v':>11} {'sum_td_v':>11} {'v_ratio':>8}"
    )
    for r in results:
        ratio = r["v_ratio"]
        ratio_s = "  -  " if ratio is None else f"{ratio:.2%}"
        print(
            f"{r['code']:<8} {r['rt_bars']:>4} {r['td_bars']:>4} "
            f"{r['common']:>4} {r['ohlcv_diff']:>4} "
            f"{r['sum_rt_v']:>11d} {r['sum_td_v']:>11d} {ratio_s:>8}"
        )

    valid = [r for r in results if r["v_ratio"] is not None]
    if valid:
        ratios = [r["v_ratio"] for r in valid]
        avg = sum(ratios) / len(ratios)
        below_50 = sum(1 for r in ratios if r < 0.5)
        below_20 = sum(1 for r in ratios if r < 0.2)
        print(
            f"\n[요약] 종목 {len(valid)}개 평균 v_ratio={avg:.2%} | "
            f"50% 미만 {below_50}개 | 20% 미만 {below_20}개"
        )
        worst = sorted(valid, key=lambda r: (r["v_ratio"], -r["ohlcv_diff"]))[:10]
        print("[가장 차이 큰 종목 TOP-10]")
        for r in worst:
            print(
                f"  {r['code']}  v_ratio={r['v_ratio']:.2%}  ohlcv_diff={r['ohlcv_diff']}"
            )

    if target_code:
        chosen = next((r for r in results if r["code"] == target_code), None)
        if chosen is None:
            print(f"\n[!] {target_code} 는 RT DB에 없는 종목입니다.")
        else:
            print()
            print_detail(chosen, day)


if __name__ == "__main__":
    main(sys.argv)
