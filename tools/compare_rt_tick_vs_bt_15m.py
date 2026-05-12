# -*- coding: utf-8 -*-
"""
실시간 틱 JSONL로 재구성한 15분봉 vs trading_data.db(bars_15m) 및
동일 settings 기준 S1 신호(get_buy_signals_detailed) 비교.

사용 예:
  python tools/compare_rt_tick_vs_bt_15m.py
  python tools/compare_rt_tick_vs_bt_15m.py --code 003550 --event-ts ""
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
# ``python tools/...`` 실행 시 cwd와 무관하게 ``strategy`` 패키지를 찾습니다.
_root_s = str(ROOT)
if _root_s not in sys.path:
    sys.path.insert(0, _root_s)
SETTINGS_PATH = ROOT / "settings.json"
TRADING_DB = ROOT / "trading_data.db"
RT_DB = ROOT / "realtime_15m_mock.db"
TICK_SUBDIR = ROOT / "realtime_tick_logs"


def slot_end_ts(dt: datetime) -> datetime:
    """StreamProcessor._update_realtime_bar_impl 과 동일한 15분 슬롯 라벨(구간 우측 정각)."""
    h, m = dt.hour, dt.minute
    tgt_m = ((m // 15) + 1) * 15
    th = h
    if tgt_m == 60:
        tgt_m = 0
        th = h + 1
    return dt.replace(hour=th, minute=tgt_m, second=0, microsecond=0)


def load_settings() -> Dict[str, Any]:
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def ticks_to_15m_ohlcv(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """틱 → 15분봉. 거래량=누적 체결량 차분(역행 시 세션 리셋으로 간주)."""
    if not rows:
        return pd.DataFrame()
    buckets: Dict[pd.Timestamp, Dict[str, Any]] = {}
    prev_cum: Optional[float] = None
    for r in rows:
        try:
            ts = datetime.strptime(str(r["ts_wall"])[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        price = float(r.get("price") or 0)
        cum = float(r.get("cum_volume") or 0)
        ix = pd.Timestamp(slot_end_ts(ts))
        b = buckets.get(ix)
        if b is None:
            init_v = 0.0
            if prev_cum is not None and cum >= prev_cum:
                init_v = cum - prev_cum
            elif prev_cum is not None and cum < prev_cum:
                init_v = max(0.0, cum)
            buckets[ix] = {
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "vol": float(init_v),
            }
        else:
            b["high"] = max(b["high"], price)
            b["low"] = min(b["low"], price)
            b["close"] = price
            if prev_cum is not None and cum >= prev_cum:
                dv = cum - prev_cum
            elif prev_cum is not None and cum < prev_cum:
                dv = max(0.0, cum)
            else:
                dv = 0.0
            b["vol"] += dv
        prev_cum = cum
    idx = sorted(buckets.keys())
    out = pd.DataFrame(
        [
            {
                "datetime": i,
                "Open": buckets[i]["open"],
                "High": buckets[i]["high"],
                "Low": buckets[i]["low"],
                "Close": buckets[i]["close"],
                "Volume": max(0.0, round(buckets[i]["vol"])),
            }
            for i in idx
        ]
    )
    return out.set_index("datetime")


def read_ticks_until(
    tick_path: Path, code: str, cutoff: Optional[datetime]
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        fp = tick_path.open("r", encoding="utf-8")
    except OSError as e:
        print(f"틱 파일 없음 {tick_path}: {e}")
        return out
    with fp:
        for line in fp:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if str(o.get("code") or "").zfill(6) != code.zfill(6):
                continue
            try:
                ts = datetime.strptime(str(o["ts_wall"])[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if cutoff is None or ts <= cutoff:
                out.append(o)
    return out


def fetch_sqlite_15m(db_path: Path, code: str, day: str) -> pd.DataFrame:
    if not db_path.is_file():
        return pd.DataFrame()
    day_start = f"{day} 00:00:00"
    day_end = f"{day} 23:59:59"
    con = sqlite3.connect(str(db_path))
    try:
        df = pd.read_sql_query(
            """
            SELECT datetime AS dt, open AS Open, high AS High, low AS Low, close AS Close, volume AS Volume
            FROM bars_15m
            WHERE code = ? AND datetime >= ? AND datetime <= ?
            ORDER BY datetime
            """,
            con,
            params=(code, day_start, day_end),
        )
    finally:
        con.close()
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["dt"], errors="coerce")
    return df.dropna(subset=["datetime"]).set_index("datetime").drop(columns=["dt"])


def fetch_sqlite_15m_history_upto(
    db_path: Path, code: str, day: str, max_rows: int = 3500
) -> pd.DataFrame:
    """
    ``day`` 장마감 시각 이하까지의 15m 봉을 최대 ``max_rows``개 로드(지표 워밍업용).
    당일만 넣어 MA60·전일거래량 컬럼이 왜곡되는 문제를 피합니다.
    """
    if not db_path.is_file():
        return pd.DataFrame()
    day_end = f"{day} 23:59:59"
    con = sqlite3.connect(str(db_path))
    try:
        df = pd.read_sql_query(
            """
            SELECT datetime AS dt, open AS Open, high AS High, low AS Low, close AS Close, volume AS Volume
            FROM bars_15m
            WHERE code = ? AND datetime <= ?
            ORDER BY datetime DESC
            LIMIT ?
            """,
            con,
            params=(code, day_end, int(max_rows)),
        )
    finally:
        con.close()
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["dt"], errors="coerce")
    df = df.dropna(subset=["datetime"]).set_index("datetime").drop(columns=["dt"])
    return df.sort_index()


def fetch_rt15m(code: str, day: str) -> pd.DataFrame:
    if not RT_DB.is_file():
        return pd.DataFrame()
    day_start = f"{day} 00:00:00"
    day_end = f"{day} 23:59:59"
    con = sqlite3.connect(str(RT_DB))
    try:
        df = pd.read_sql_query(
            """
            SELECT datetime AS dt, open AS Open, high AS High, low AS Low, close AS Close, volume AS Volume
            FROM bars_15m_rt
            WHERE code = ? AND datetime >= ? AND datetime <= ?
            ORDER BY datetime
            """,
            con,
            params=(code, day_start, day_end),
        )
    finally:
        con.close()
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["dt"], errors="coerce")
    return df.dropna(subset=["datetime"]).set_index("datetime").drop(columns=["dt"])


def load_prev_day_bars(code: str, day: str) -> pd.DataFrame:
    """지표 워밍업: 직전 영업일 근처 15m."""
    for back in range(1, 15):
        p = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=back)).strftime("%Y-%m-%d")
        con = sqlite3.connect(str(TRADING_DB))
        try:
            prev = pd.read_sql_query(
                """
                SELECT datetime AS dt, open AS Open, high AS High, low AS Low, close AS Close, volume AS Volume
                FROM bars_15m WHERE code = ? AND datetime >= ? AND datetime < ?
                ORDER BY datetime
                """,
                con,
                params=(code, f"{p} 00:00:00", f"{p} 23:59:59"),
            )
        finally:
            con.close()
        if len(prev) > 20:
            prev["datetime"] = pd.to_datetime(prev["dt"], errors="coerce")
            return prev.dropna(subset=["datetime"]).set_index("datetime").drop(columns=["dt"])
    return pd.DataFrame()


def attach_indicators(df: pd.DataFrame, settings: Dict[str, Any], code: str, day: str) -> pd.DataFrame:
    from strategy.indicators import calculate_all_indicators

    prev = load_prev_day_bars(code, day)
    combo = pd.concat([prev, df]).sort_index()
    return calculate_all_indicators(combo, settings)


def slice_day(df: pd.DataFrame, day: str) -> pd.DataFrame:
    d0 = pd.Timestamp(f"{day} 00:00:00")
    d1 = pd.Timestamp(f"{day} 23:59:59")
    return df.loc[(df.index >= d0) & (df.index <= d1)]


def describe_last_signal_row(df_ind: pd.DataFrame, settings: Dict[str, Any], title: str) -> None:
    from strategy.signals import get_buy_signals_detailed

    print(f"\n===== {title} =====")
    if df_ind.empty or len(df_ind) < 3:
        print("DF 부족:", len(df_ind))
        return
    det = get_buy_signals_detailed(df_ind, settings, market_trend="UP")
    raw = df_ind.iloc[-1]
    row = det.iloc[-1]
    print("마지막 봉 시각:", df_ind.index[-1])
    print(
        "OHLCV:",
        int(raw["Open"]),
        int(raw["High"]),
        int(raw["Low"]),
        int(raw["Close"]),
        int(raw["Volume"]),
    )
    print(
        "RSI / RSI_Sig / MACD / OSC:",
        round(float(raw.get("RSI", float("nan"))), 4),
        round(float(raw.get("RSI_Sig", float("nan"))), 4),
        round(float(raw.get("MACD", float("nan"))), 4),
        round(float(raw.get("OSC", float("nan"))), 4),
    )
    print(
        "Signal / Sig_Type / Met_Ratio / Met_B:",
        bool(row["Signal"]),
        row["Sig_Type"],
        round(float(row["Met_Ratio"]), 4),
        int(row["Met_B"]),
        "/",
        int(row["Tot_B"]),
    )


def list_signal_bars_on_day(
    df_day_indicated: pd.DataFrame, settings: Dict[str, Any], day: str
) -> None:
    """당일 구간에서 Signal==True 인 모든 15분봉 시각 나열."""
    from strategy.signals import get_buy_signals_detailed

    print(f"\n--- 당일({day}) Signal==True 인 15분봉(백테스트 관점) ---")
    if df_day_indicated.empty:
        print("(없음)")
        return
    det = get_buy_signals_detailed(df_day_indicated, settings, market_trend="UP")
    m = det["Signal"].astype(bool)
    if not m.any():
        print("해당일 매수 신호 없음")
        return
    for ts in df_day_indicated.index[m]:
        r = det.loc[ts]
        px = df_day_indicated.loc[ts, "Close"]
        print(
            f"  {ts}  Close={int(px)}  Sig_Type={r['Sig_Type']}  Met_Ratio={float(r['Met_Ratio']):.3f}"
        )


def pick_tick_path(day: str) -> Path:
    for suf in ("mock", "live"):
        p = TICK_SUBDIR / f"{day}_trades_{suf}.jsonl"
        if p.is_file():
            return p
    return TICK_SUBDIR / f"{day}_trades_mock.jsonl"


def analyze_one(
    code: str,
    day: str,
    event_ts: Optional[str],
    settings: Dict[str, Any],
) -> None:
    from strategy.indicators import calculate_all_indicators

    tick_path = pick_tick_path(day)
    print(f"\n{'='*60}\n종목 {code}  일자 {day}  틱파일 {tick_path.name}\n{'='*60}")

    cutoff = datetime.strptime(event_ts, "%Y-%m-%d %H:%M:%S") if event_ts else None
    eod = datetime.strptime(f"{day} 23:59:59", "%Y-%m-%d %H:%M:%S")

    ticks_eod = read_ticks_until(tick_path, code, eod)
    ticks_cut = read_ticks_until(tick_path, code, cutoff) if cutoff else ticks_eod
    print("틱 건수(장종료까지):", len(ticks_eod), end="")
    if cutoff:
        print(" / 이벤트 시각까지:", len(ticks_cut))
    else:
        print()

    df_tick_all = ticks_to_15m_ohlcv(ticks_eod)
    df_tick_cut = ticks_to_15m_ohlcv(ticks_cut)
    df_bt = fetch_sqlite_15m(TRADING_DB, code, day)
    df_rt = fetch_rt15m(code, day)
    df_hist = fetch_sqlite_15m_history_upto(TRADING_DB, code, day, max_rows=3500)

    day_mask = df_tick_all.index.strftime("%Y-%m-%d") == day
    tick_day = df_tick_all.loc[day_mask] if day_mask.any() else pd.DataFrame()
    print("\n[DB bars_15m 당일] n=", len(df_bt))
    if not df_bt.empty:
        print(df_bt.to_string())
    else:
        print("(없음 - 하베스팅/코드 확인)")

    if event_ts and cutoff:
        slot = pd.Timestamp(slot_end_ts(cutoff))
        print(f"\n이벤트 로컬시각 {event_ts} → 형성 중인 15분봉 라벨(우측) = {slot}")

        key = slot
        print("\n[라벨 봉 OHLCV: 틱(이벤트까지) / 틱(장종료) / DB / realtime_15m DB]")

        def one_line(name: str, df: pd.DataFrame, k: pd.Timestamp) -> None:
            if df.empty or k not in df.index:
                print(f"  {name}: (없음)")
                return
            r = df.loc[k]
            print(
                f"  {name}: O={int(r['Open'])} H={int(r['High'])} L={int(r['Low'])} "
                f"C={int(r['Close'])} V={int(r['Volume'])}"
            )

        one_line("틱(이벤트까지)", df_tick_cut, key)
        one_line("틱(장종료)", df_tick_all, key)
        one_line("trading_data", df_bt, key)
        one_line("realtime_15m", df_rt, key)

        hist_bt = df_hist[df_hist.index < key].copy()
        if df_tick_cut.empty:
            print("틱(컷오프) 없음 - 인과 DF 생략")
        else:
            last_partial = df_tick_cut.loc[[df_tick_cut.index.max()]].copy()
            causal = pd.concat([hist_bt, last_partial]).sort_index()
            causal = causal[~causal.index.duplicated(keep="last")]
            causal_full = calculate_all_indicators(causal, settings)
            causal_day = slice_day(causal_full, day)
            describe_last_signal_row(
                causal_day,
                settings,
                "인과 스냅샷(이벤트 시각까지) - 마지막 봉=형성 중",
            )

    if df_hist.empty:
        print("\n(DB 히스토리 없음 - bars_15m 지표/신호 목록 생략)")
    else:
        full_marked = calculate_all_indicators(df_hist.copy(), settings)
        day_df = slice_day(full_marked, day)
        describe_last_signal_row(day_df, settings, "백테스트 DB 당일(완성봉만) - 마지막 봉")
        list_signal_bars_on_day(day_df, settings, day)

    if not tick_day.empty and not df_hist.empty:
        day0 = pd.Timestamp(f"{day} 00:00:00")
        prev_hist = df_hist[df_hist.index < day0]
        tick_combo = pd.concat([prev_hist, tick_day]).sort_index()
        tick_marked = calculate_all_indicators(tick_combo, settings)
        td = slice_day(tick_marked, day)
        describe_last_signal_row(td, settings, "틱 재구성 당일 전체(장종료 기준) - 마지막 봉")


def main() -> None:
    parser = argparse.ArgumentParser(description="틱 vs DB 15m 및 S1 신호 비교")
    parser.add_argument("--day", default="2026-05-12")
    parser.add_argument(
        "--code",
        default="004170",
        help="6자리 종목코드 (기본: 신세계)",
    )
    parser.add_argument(
        "--event-ts",
        default="2026-05-12 14:05:08",
        help="실시간 매수 판정 시각(로컬). LG만 볼 때는 \"\" 로 틱 인과 생략",
    )
    args = parser.parse_args()
    settings = load_settings()
    event_ts: Optional[str] = args.event_ts.strip() or None

    analyze_one(args.code, args.day, event_ts, settings)

    # LG(003550) 기본 2차 비교: 세션 로그에 매수 시각이 없을 수 있음(감시종목만 반영된 경우 등)
    if args.code == "004170":
        print("\n\n[참고] LG(003550) - 백테스트 당일 신호 시각만(실시간 이벤트 시각 미지정)")
        analyze_one("003550", args.day, None, settings)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("분석 중 오류:", exc)
        raise
