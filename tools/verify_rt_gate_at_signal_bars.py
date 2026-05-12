# -*- coding: utf-8 -*-
"""
백테스트와 동일한 DB 15분봉 히스토리로 Signal==True 인 봉을 찾은 뒤,
각 슬롯에 대해 틱 재구성·게이트(수정 후 SignalEngine 로직)를 검증합니다.

사용 예:
  python tools/verify_rt_gate_at_signal_bars.py 004170 2026-05-12
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def slot_end_ts(dt: datetime) -> datetime:
    h, m = dt.hour, dt.minute
    tgt_m = ((m // 15) + 1) * 15
    th = h
    if tgt_m == 60:
        tgt_m = 0
        th = h + 1
    return dt.replace(hour=th, minute=tgt_m, second=0, microsecond=0)


def load_settings() -> dict:
    with open(ROOT / "settings.json", "r", encoding="utf-8") as f:
        return json.load(f)


def load_db_bars_upto(code: str, end_ts: str) -> pd.DataFrame:
    con = sqlite3.connect(str(ROOT / "trading_data.db"))
    try:
        df = pd.read_sql_query(
            """
            SELECT datetime AS dt, open AS Open, high AS High, low AS Low,
                   close AS Close, volume AS Volume
            FROM bars_15m
            WHERE code = ? AND datetime <= ?
            ORDER BY datetime
            """,
            con,
            params=(code, end_ts),
        )
    finally:
        con.close()
    if df.empty:
        return pd.DataFrame()
    df["datetime"] = pd.to_datetime(df["dt"], errors="coerce")
    df = df.dropna(subset=["datetime"]).set_index("datetime").drop(columns=["dt"])
    return df


def ticks_in_slot(
    code: str, day: str, slot_end: pd.Timestamp, tick_path: Path
) -> list[dict]:
    slot_start = slot_end - pd.Timedelta(minutes=15)
    out: list[dict] = []
    if not tick_path.is_file():
        return out
    for line in tick_path.open("r", encoding="utf-8"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if str(o.get("code") or "").zfill(6) != code:
            continue
        ts = datetime.strptime(str(o["ts_wall"])[:19], "%Y-%m-%d %H:%M:%S")
        if ts < slot_start.to_pydatetime() or ts >= slot_end.to_pydatetime():
            continue
        out.append(o)
    out.sort(key=lambda x: x["ts_wall"])
    return out


def ticks_to_partial_bar(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    prev_cum = None
    o = h = l = c = None
    vol = 0.0
    for r in rows:
        price = float(r.get("price") or 0)
        cum = float(r.get("cum_volume") or 0)
        if o is None:
            o = h = l = c = price
            if prev_cum is not None and cum >= prev_cum:
                vol += cum - prev_cum
            elif prev_cum is not None and cum < prev_cum:
                vol += max(0.0, cum)
        else:
            h = max(h, price)
            l = min(l, price)
            c = price
            if prev_cum is not None and cum >= prev_cum:
                vol += cum - prev_cum
            elif prev_cum is not None and cum < prev_cum:
                vol += max(0.0, cum)
        prev_cum = cum
    end = pd.Timestamp(
        slot_end_ts(
            datetime.strptime(str(rows[0]["ts_wall"])[:19], "%Y-%m-%d %H:%M:%S")
        )
    )
    return pd.DataFrame(
        [
            {
                "datetime": end,
                "Open": o,
                "High": h,
                "Low": l,
                "Close": c,
                "Volume": max(0.0, round(vol)),
            }
        ]
    ).set_index("datetime")


def find_signal_bars_on_day(
    code: str, day: str, settings: dict, market_up: str = "UP"
) -> list[pd.Timestamp]:
    """해당 일자(장중)에서 Signal==True 인 15분봉 라벨 시각 목록."""
    from backtest_engine import TradingStrategy

    hist = load_db_bars_upto(code, f"{day} 23:59:59")
    if hist.empty:
        return []
    df = TradingStrategy.calculate_indicators(hist, settings)
    sig = TradingStrategy.get_buy_signals_detailed(df, settings, market_up)
    sig = sig.copy()
    sig.index = df.index
    d0 = pd.Timestamp(day).date()
    hits: list[pd.Timestamp] = []
    for ts in sig.index:
        try:
            if ts.date() != d0:
                continue
        except Exception:
            continue
        try:
            if bool(sig.loc[ts, "Signal"]):
                hits.append(pd.Timestamp(ts))
        except Exception:
            continue
    return sorted(set(hits))


def verify_one_slot(
    code: str,
    day: str,
    bar_close: pd.Timestamp,
    settings: dict,
    tick_path: Path,
) -> None:
    from backtest_engine import TradingStrategy
    from core.defaults import effective_min_15m_bars_for_monitoring
    from core.signal_engine import SignalEngine

    class _MiniApp:
        def __init__(self, st: dict) -> None:
            self.settings = st

    eng = SignalEngine(_MiniApp(settings))
    min_bars = int(effective_min_15m_bars_for_monitoring(settings))

    hist = load_db_bars_upto(code, f"{day} 23:59:59")
    if bar_close not in hist.index:
        print(f"  [스킵] DB에 봉 없음: {bar_close}")
        return

    hist_before = hist[hist.index < bar_close]
    bar_db = hist.loc[bar_close]
    n_hist = len(hist_before)
    rows = ticks_in_slot(code, day, bar_close, tick_path)
    partial = ticks_to_partial_bar(rows)

    df_bt = pd.concat([hist_before, pd.DataFrame([bar_db])]).sort_index()
    df_bt = TradingStrategy.calculate_indicators(df_bt, settings)
    sig_bt = TradingStrategy.get_buy_signals_detailed(df_bt, settings, "UP")
    last_bt = sig_bt.iloc[-1]

    print(f"--- 슬롯 라벨 {bar_close} ---")
    print(f"  확정봉만 개수: {n_hist} (min {min_bars}) → bars_ready: {n_hist >= min_bars}")
    print(f"  틱 수(해당 15분): {len(rows)}")
    print(
        "  DB완성 OHLCV:",
        int(bar_db["Open"]),
        int(bar_db["High"]),
        int(bar_db["Low"]),
        int(bar_db["Close"]),
        int(bar_db["Volume"]),
    )
    if partial.empty:
        print("  틱 부분봉: 없음 (틱 파일 없거나 해당 구간 틱 없음)")
        df_rt = df_bt
        last_rt = last_bt
    else:
        pr = partial.iloc[0]
        print(
            "  틱부분 OHLCV:",
            int(pr["Open"]),
            int(pr["High"]),
            int(pr["Low"]),
            int(pr["Close"]),
            int(pr["Volume"]),
        )
        df_rt = pd.concat([hist_before, partial]).sort_index()
        df_rt = TradingStrategy.calculate_indicators(df_rt, settings)
        sig_rt = TradingStrategy.get_buy_signals_detailed(df_rt, settings, "UP")
        last_rt = sig_rt.iloc[-1]

    for label, ser in (("DB완성", last_bt), ("틱부분" if not partial.empty else "틱없음→DB와동일", last_rt)):
        ls = ser.to_dict() if hasattr(ser, "to_dict") else dict(ser)
        sig_b = bool(ls.get("Signal", False))
        old_gate = sig_b and bool(ls.get("Ready", False))
        new_ready = bool(eng._s1_presignal_ready_for_gate(ls))
        new_gate = sig_b and new_ready
        print(
            f"  [{label}] Signal={sig_b} Sig_Type={ls.get('Sig_Type')} "
            f"옛게이트_is_buy={old_gate} 수정후_ready={new_ready} 수정후_is_buy={new_gate}"
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="백테 Signal 봉별 실시간 게이트 검증")
    parser.add_argument("code", help="종목코드 6자리")
    parser.add_argument("day", help="YYYY-MM-DD")
    parser.add_argument(
        "--bar",
        type=str,
        default="",
        help="특정 15분 라벨만 검증 (예: 2026-05-12 14:00:00). 비우면 당일 Signal True 전부",
    )
    args = parser.parse_args()
    code = str(args.code).zfill(6)
    day = str(args.day).strip()
    settings = load_settings()
    tick_path = ROOT / "realtime_tick_logs" / f"{day}_trades_mock.jsonl"

    print(f"=== 실시간 게이트 검증 code={code} day={day} ===")
    print(f"틱 파일: {tick_path.name} 존재={tick_path.is_file()}")
    print()

    try:
        if args.bar.strip():
            bar_close = pd.Timestamp(args.bar.strip())
            verify_one_slot(code, day, bar_close, settings, tick_path)
            return

        bars = find_signal_bars_on_day(code, day, settings)
        if not bars:
            print("당일 Signal==True 인 15분봉이 없습니다 (히스토리·설정·시장추세 UP 확인).")
            return
        print(f"백테(당일·UP) Signal True 슬롯 {len(bars)}개:", [str(b) for b in bars])
        print()
        for bar_close in bars:
            verify_one_slot(code, day, bar_close, settings, tick_path)
        print("요약: 각 행에서 수정 후 is_buy=True 이면 고정 프리시그널 게이트는 매수를 막지 않습니다.")
    except Exception as exc:
        print("오류:", exc)
        raise


if __name__ == "__main__":
    main()
