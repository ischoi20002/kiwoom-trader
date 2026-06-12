# -*- coding: utf-8 -*-
"""RT15m DB 15분봉 단절(갭) 점검 — 감시 종목 기준."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "realtime_15m_mock.db")
TARGETS = os.path.join(ROOT, "selected_targets.csv")


def _load_codes() -> List[str]:
    try:
        df = pd.read_csv(TARGETS, dtype={"Code": str})
        return [str(c).zfill(6) for c in df["Code"].tolist()]
    except Exception as e:
        print(f"selected_targets.csv 오류: {e}")
        return []


def _is_krx_15m_slot(ts: pd.Timestamp) -> bool:
    """09:00~15:30 KRX 15분 슬롯(right-label)인지."""
    try:
        t = pd.Timestamp(ts)
        if t.weekday() >= 5:
            return False
        hm = t.hour * 60 + t.minute
        # right-label: 09:15, 09:30, ... 15:30
        if hm < 9 * 60 + 15 or hm > 15 * 60 + 30:
            return False
        return (t.minute % 15) == 0 and t.second == 0
    except Exception:
        return False


def _expected_prev_slot(ts: pd.Timestamp) -> Optional[pd.Timestamp]:
    """직전 영업 슬롯(right-label)."""
    try:
        t = pd.Timestamp(ts)
        prev = t - pd.Timedelta(minutes=15)
        if prev.time() < time(9, 15):
            # 전일 15:30으로 (주말은 단순 -1일, 휴장은 별도)
            d = t.date()
            from datetime import timedelta

            d2 = d - timedelta(days=1)
            while pd.Timestamp(d2).weekday() >= 5:
                d2 = d2 - timedelta(days=1)
            return pd.Timestamp(datetime.combine(d2, time(15, 30)))
        return prev
    except Exception:
        return None


def analyze_code(con: sqlite3.Connection, code: str) -> Dict[str, Any]:
    cur = con.cursor()
    rows = cur.execute(
        "SELECT datetime FROM bars_15m_rt WHERE code=? ORDER BY datetime",
        (code,),
    ).fetchall()
    if not rows:
        return {"code": code, "n": 0, "intraday_gaps": [], "bad_slots": []}

    dts = [pd.Timestamp(r[0]) for r in rows]
    intraday_gaps: List[Tuple[str, str, float]] = []
    bad_slots: List[str] = []

    for ts in dts:
        if not _is_krx_15m_slot(ts):
            bad_slots.append(str(ts))

    for i in range(1, len(dts)):
        a, b = dts[i - 1], dts[i]
        delta_min = (b - a).total_seconds() / 60.0
        if delta_min <= 15.01:
            continue
        # 같은 날 장중 갭
        if a.date() == b.date() and a.time() >= time(9, 0) and b.time() <= time(15, 30):
            intraday_gaps.append((str(a), str(b), delta_min))
        elif delta_min < 24 * 60:  # overnight but not multi-day
            intraday_gaps.append((str(a), str(b), delta_min))

    return {
        "code": code,
        "n": len(dts),
        "min_dt": str(dts[0]),
        "max_dt": str(dts[-1]),
        "intraday_gaps": intraday_gaps,
        "bad_slots": bad_slots[:5],
        "bad_slot_count": len(bad_slots),
    }


def _same_day_session_gaps(dts: List[pd.Timestamp]) -> List[Tuple[str, str, float]]:
    """같은 거래일 장중(09~15:30)에서 15분 초과 공백."""
    from datetime import time

    out: List[Tuple[str, str, float]] = []
    for i in range(1, len(dts)):
        a, b = dts[i - 1], dts[i]
        if a.date() != b.date():
            continue
        dm = (b - a).total_seconds() / 60.0
        if dm <= 15.01:
            continue
        if a.time() >= time(9, 0) and b.time() <= time(15, 30):
            out.append((str(a), str(b), dm))
    return out


def main() -> None:
    codes = _load_codes()
    print(f"감시 종목: {len(codes)}")
    if not os.path.exists(DB):
        print(f"DB 없음: {DB}")
        return
    print(f"DB: {DB} ({os.path.getsize(DB):,} bytes)")

    now = datetime.now()
    in_session = (
        now.weekday() < 5
        and time(9, 0) <= now.time() <= time(15, 30)
    )
    print(f"현재: {now.strftime('%Y-%m-%d %H:%M:%S')}  장중={in_session}")

    con = sqlite3.connect(DB)
    total_gaps = 0
    codes_with_gaps: List[str] = []

    targets_df = pd.read_csv(TARGETS, dtype={"Code": str})

    print("\n=== 요약 (장중 같은날 15분 초과 공백) ===")
    print(f"{'종목':<22} {'DB봉수':>7} {'마지막봉':<20} {'경과일':>5} {'장중갭':>5}")
    print("-" * 65)

    for code in codes:
        r = analyze_code(con, code)
        rows = con.execute(
            "SELECT datetime FROM bars_15m_rt WHERE code=? ORDER BY datetime",
            (code,),
        ).fetchall()
        dts = [pd.Timestamp(x[0]) for x in rows]
        session_gaps = _same_day_session_gaps(dts)
        ng = len(session_gaps)
        total_gaps += ng
        if ng:
            codes_with_gaps.append(code)
        nm = ""
        try:
            row = targets_df[targets_df["Code"].astype(str).str.zfill(6) == code]
            if not row.empty:
                nm = str(row.iloc[0].get("Name", ""))[:10]
        except Exception:
            pass
        label = f"{nm}({code})" if nm else code
        last = r.get("max_dt", "?")
        try:
            days_ago = (now.date() - pd.Timestamp(last).date()).days
        except Exception:
            days_ago = -1
        print(
            f"{label:<22} {r.get('n',0):>7} {str(last)[:19]:<20} {days_ago:>5} {ng:>5}"
        )
        for g in session_gaps[:2]:
            print(f"   gap {g[0]} -> {g[1]} ({g[2]:.0f}m)")

    print(f"\n장중 갭 있는 종목: {len(codes_with_gaps)}/{len(codes)}")
    print(
        "\n[워밍업 검증 참고] _validate_warmup_base 는 "
        "(1) 봉수>=min (2) 장중일 때 마지막 봉이 최신 슬롯인지만 검사합니다. "
        "중간 단절은 검사하지 않습니다."
    )
    if not in_session:
        print(
            "[장외] _needs_intraday_api_backfill=False → "
            "당일 갭 검사 자체가 꺼져 DB만으로 통과 가능합니다."
        )
    con.close()


if __name__ == "__main__":
    main()
