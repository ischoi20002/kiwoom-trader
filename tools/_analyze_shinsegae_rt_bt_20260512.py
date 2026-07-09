# -*- coding: utf-8 -*-
"""일회성: 신세계(004170) 2026-05-12 실시간 매수 시각 틱 vs DB 봉·S1 신호 비교."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

CODE = "004170"
DAY = "2026-05-12"
SIGNAL_TS = "2026-05-12 14:05:08"
ROOT = Path(__file__).resolve().parents[1]
TICK_PATH = ROOT / "realtime_tick_logs" / f"{DAY}_trades_mock.jsonl"
SETTINGS_PATH = ROOT / "settings.json"
TRADING_DB = ROOT / "trading_data.db"
RT_DB = ROOT / "realtime_15m_mock.db"


def slot_end_ts(dt: datetime) -> datetime:
    """StreamProcessor._update_realtime_bar_impl 과 동일한 슬롯 라벨."""
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
    """틱 → 15분봉(라벨=구간 우측). 거래량=누적 차분(역행 시 재시작으로 간주)."""
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
        end = slot_end_ts(ts)
        ix = pd.Timestamp(end)
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
                "_last_cum": cum,
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
            b["_last_cum"] = cum
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


def read_ticks_until(cutoff: datetime) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    try:
        fp = TICK_PATH.open("r", encoding="utf-8")
    except OSError as e:
        print("틱 파일 없음:", e)
        return out
    with fp:
        for line in fp:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("code") != CODE:
                continue
            try:
                ts = datetime.strptime(str(o["ts_wall"])[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if ts <= cutoff:
                out.append(o)
    return out


def fetch_sqlite_15m(db_path: Path) -> pd.DataFrame:
    if not db_path.is_file():
        return pd.DataFrame()
    day_start = f"{DAY} 00:00:00"
    day_end = f"{DAY} 23:59:59"
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
            params=(CODE, day_start, day_end),
        )
    finally:
        con.close()
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["dt"], errors="coerce")
    df = df.dropna(subset=["datetime"]).set_index("datetime").drop(columns=["dt"])
    return df


def fetch_rt15m() -> pd.DataFrame:
    if not RT_DB.is_file():
        return pd.DataFrame()
    day_start = f"{DAY} 00:00:00"
    day_end = f"{DAY} 23:59:59"
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
            params=(CODE, day_start, day_end),
        )
    finally:
        con.close()
    if df.empty:
        return df
    df["datetime"] = pd.to_datetime(df["dt"], errors="coerce")
    df = df.dropna(subset=["datetime"]).set_index("datetime").drop(columns=["dt"])
    return df


def load_prev_day_bars_for_warmup() -> pd.DataFrame:
    """전일(또는 근접 영업일) 15m — attach_prev_day_volume_quartet 워밍업용."""
    for back in range(1, 12):
        p = (datetime.strptime(DAY, "%Y-%m-%d") - timedelta(days=back)).strftime("%Y-%m-%d")
        con = sqlite3.connect(str(TRADING_DB))
        try:
            prev = pd.read_sql_query(
                """
                SELECT datetime AS dt, open AS Open, high AS High, low AS Low, close AS Close, volume AS Volume
                FROM bars_15m WHERE code = ? AND datetime >= ? AND datetime < ?
                ORDER BY datetime
                """,
                con,
                params=(CODE, f"{p} 00:00:00", f"{p} 23:59:59"),
            )
        finally:
            con.close()
        if len(prev) > 20:
            prev["datetime"] = pd.to_datetime(prev["dt"], errors="coerce")
            prev = prev.dropna(subset=["datetime"]).set_index("datetime").drop(columns=["dt"])
            return prev
    return pd.DataFrame()


def attach_indicators(df: pd.DataFrame, settings: Dict[str, Any]) -> pd.DataFrame:
    from strategy.indicators import calculate_all_indicators

    prev = load_prev_day_bars_for_warmup()
    combo = pd.concat([prev, df]).sort_index()
    return calculate_all_indicators(combo, settings)


def slice_day(df: pd.DataFrame) -> pd.DataFrame:
    d0 = pd.Timestamp(f"{DAY} 00:00:00")
    d1 = pd.Timestamp(f"{DAY} 23:59:59")
    return df.loc[(df.index >= d0) & (df.index <= d1)]


def eval_signals_tail(df_ind: pd.DataFrame, settings: Dict[str, Any], title: str) -> None:
    from strategy.signals import get_buy_signals_detailed

    print(f"\n===== {title} =====")
    if df_ind.empty or len(df_ind) < 3:
        print("DF 부족", len(df_ind))
        return
    det = get_buy_signals_detailed(df_ind, settings, market_trend="UP")
    last_i = -1
    row = det.iloc[last_i]
    raw = df_ind.iloc[last_i]
    print("마지막 봉:", df_ind.index[last_i])
    print(
        "OHLCV:",
        float(raw["Open"]),
        float(raw["High"]),
        float(raw["Low"]),
        float(raw["Close"]),
        float(raw["Volume"]),
    )
    print(
        "RSI / RSI_Sig / MACD / OSC:",
        round(float(raw.get("RSI", float("nan"))), 4),
        round(float(raw.get("RSI_Sig", float("nan"))), 4),
        round(float(raw.get("MACD", float("nan"))), 4),
        round(float(raw.get("OSC", float("nan"))), 4),
    )
    print(
        "Signal:",
        bool(row["Signal"]),
        "Sig_Type:",
        row["Sig_Type"],
        "Met_Ratio:",
        round(float(row["Met_Ratio"]), 4),
        "Met_B/Tot_B:",
        int(row["Met_B"]),
        "/",
        int(row["Tot_B"]),
    )


def main() -> None:
    settings = load_settings()
    cutoff = datetime.strptime(SIGNAL_TS, "%Y-%m-%d %H:%M:%S")
    eod = datetime.strptime(f"{DAY} 23:59:59", "%Y-%m-%d %H:%M:%S")

    ticks_eod = read_ticks_until(eod)
    ticks_cut = read_ticks_until(cutoff)
    print("틱 건수 전체:", len(ticks_eod), "/ 14:05:08까지:", len(ticks_cut))

    df_tick_all = ticks_to_15m_ohlcv(ticks_eod)
    df_tick_cut = ticks_to_15m_ohlcv(ticks_cut)
    df_bt = fetch_sqlite_15m(TRADING_DB)
    df_rt = fetch_rt15m()

    day_mask = df_tick_all.index.strftime("%Y-%m-%d") == DAY
    print("\n[틱 재구성 당일 tail]")
    print(df_tick_all.loc[day_mask].tail(10).to_string())

    print("\n[trading_data.db 당일 tail] n=", len(df_bt))
    if not df_bt.empty:
        print(df_bt.tail(10).to_string())

    print("\n[realtime_15m_mock.db 당일 tail] n=", len(df_rt))
    if not df_rt.empty:
        print(df_rt.tail(10).to_string())

    key = pd.Timestamp(f"{DAY} 14:15:00")
    print("\n=== 라벨 14:15 봉 OHLCV (틱전체 vs DB vs RT저장) ===")

    def pick(df: pd.DataFrame, k: pd.Timestamp) -> str:
        if df.empty or k not in df.index:
            return "(없음)"
        r = df.loc[k]
        return f"O={r['Open']:.0f} H={r['High']:.0f} L={r['Low']:.0f} C={r['Close']:.0f} V={r['Volume']:.0f}"

    print("틱전체:", pick(df_tick_all, key))
    print("백테DB:", pick(df_bt, key))
    print("실시간DB:", pick(df_rt, key))

    # 인과: 14:15 이전 확정봉은 DB + 형성중 14:15는 틱_cut
    hist_bt = df_bt[df_bt.index < key].copy()
    if df_tick_cut.empty:
        print("틱_cut 비어 있음")
        return
    last_partial = df_tick_cut.loc[[df_tick_cut.index.max()]].copy()
    causal = pd.concat([hist_bt, last_partial]).sort_index()
    causal = causal[~causal.index.duplicated(keep="last")]

    causal_i = slice_day(attach_indicators(causal, settings))
    eval_signals_tail(causal_i, settings, "인과 스냅샷(14:05까지) — 마지막=형성중 14:15 라벨")

    full_i = slice_day(attach_indicators(df_bt, settings))
    eval_signals_tail(full_i, settings, "백테스트 DB 당일 전체(완성봉) — 마지막 봉")

    # 틱으로만 완성된 당일 전체
    tick_day = df_tick_all.loc[day_mask].copy()
    if len(tick_day) > 0:
        tick_i = slice_day(attach_indicators(tick_day, settings))
        eval_signals_tail(tick_i, settings, "틱만으로 재구성한 당일 전체봉(장중 의미) — 마지막 봉")

    if key in df_tick_all.index and key in df_bt.index:
        print("\n=== 14:15 봉 숫자 차이 (틱재구성 − DB) ===")
        a, b = df_tick_all.loc[key], df_bt.loc[key]
        for c in ["Open", "High", "Low", "Close", "Volume"]:
            print(c, float(a[c]) - float(b[c]))


if __name__ == "__main__":
    main()
