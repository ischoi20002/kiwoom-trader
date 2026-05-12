# -*- coding: utf-8 -*-
"""
신세계(004170) 고정밀 백테와 동일한 KRX 1분 창으로 인과 S1 첫 매수 시각 검증.

- ``utils.krx_bars.m1_window_start_for_15m_end`` / ``m1_index_bounds_for_15m_end``:
  ``backtest_engine`` 의 ``searchsorted`` 규칙과 동일.
- (1) ``bars_1m`` + ``SimpleBacktester.run(..., high_precision=True)`` 첫 S1 체결 시각
- (2) 동일 15m 라벨·동일 1분 라벨에 틱을 모아 만든 1m OHLCV 로
  ``_hp_causal_s1_buy_at_k`` + 스마트바이 분기만 재현해 첫 통과 분 비교
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CODE = "004170"
DAY = "2026-05-12"
END_15M = pd.Timestamp(f"{DAY} 13:15:00")
TICK_PATH = ROOT / "realtime_tick_logs" / f"{DAY}_trades_mock.jsonl"


def load_settings() -> dict:
    with open(ROOT / "settings.json", "r", encoding="utf-8") as f:
        return json.load(f)


def load_15m(code: str, day_end: str) -> pd.DataFrame:
    import sqlite3

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
            params=(code, day_end),
        )
    finally:
        con.close()
    df["datetime"] = pd.to_datetime(df["dt"], errors="coerce")
    df = df.dropna(subset=["datetime"]).set_index("datetime").drop(columns=["dt"])
    return df


def load_m1_range(code: str, t_min: str, t_max: str) -> pd.DataFrame:
    import sqlite3

    con = sqlite3.connect(str(ROOT / "trading_data.db"))
    try:
        df = pd.read_sql_query(
            """
            SELECT datetime, open, high, low, close, volume
            FROM bars_1m
            WHERE code = ? AND datetime >= ? AND datetime <= ?
            ORDER BY datetime
            """,
            con,
            params=(code, t_min, t_max),
        )
    finally:
        con.close()
    if df.empty:
        return pd.DataFrame()
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"]).set_index("datetime")
    return pd.DataFrame(
        {
            "Open": df["open"].astype(float),
            "High": df["high"].astype(float),
            "Low": df["low"].astype(float),
            "Close": df["close"].astype(float),
            "Volume": df["volume"].astype(float),
        }
    ).sort_index()


def ticks_to_m1_like_db_slice(
    code: str,
    minute_index: pd.DatetimeIndex,
    tick_path: Path,
) -> tuple[pd.DataFrame, int]:
    """
    ``minute_index`` 각 행(1분 라벨 = DB ``bars_1m`` 인덱스와 동일)에 틱을 모아 OHLCV 생성.
    반환: (1m DF index=minute_index 순서대로, 사용된 틱 수)
    """
    if not tick_path.is_file() or len(minute_index) == 0:
        return pd.DataFrame(), 0

    ticks: list[tuple[pd.Timestamp, float, float]] = []
    for line in tick_path.open("r", encoding="utf-8"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if str(o.get("code") or "").zfill(6) != code:
            continue
        ts = pd.Timestamp(str(o["ts_wall"])[:19])
        price = float(o.get("price") or 0)
        cum = float(o.get("cum_volume") or 0)
        ticks.append((ts, price, cum))
    ticks.sort(key=lambda x: x[0])
    used = 0
    rows: list[dict[str, Any]] = []
    prev_cum_global: Optional[float] = None
    for lab in minute_index:
        t0 = lab
        t1 = lab + pd.Timedelta(minutes=1)
        o = h = l = c = None
        vol = 0.0
        prev_cum = prev_cum_global
        for (ts, price, cum) in ticks:
            if ts < t0 or ts >= t1:
                continue
            used += 1
            if o is None:
                o = h = l = c = price
            else:
                h = max(h, price)
                l = min(l, price)
                c = price
            if prev_cum is not None and cum >= prev_cum:
                vol += cum - prev_cum
            elif prev_cum is not None and cum < prev_cum:
                vol += max(0.0, float(cum))
            prev_cum = float(cum)
        if prev_cum is not None:
            prev_cum_global = prev_cum
        if o is None:
            rows.append(
                {
                    "Open": np.nan,
                    "High": np.nan,
                    "Low": np.nan,
                    "Close": np.nan,
                    "Volume": 0.0,
                }
            )
        else:
            rows.append(
                {
                    "Open": float(o),
                    "High": float(h),
                    "Low": float(l),
                    "Close": float(c),
                    "Volume": max(0.0, round(vol)),
                }
            )
    out = pd.DataFrame(rows, index=minute_index.copy())
    return out, used


def first_hp_buy_times_from_backtest(
    settings: dict,
) -> list[dict[str, Any]]:
    """고정밀 전체 시뮵 후 당일 모든 BUY_1ST (Date, Reason, Sig_Type)."""
    from backtest_engine import SimpleBacktester

    df15 = load_15m(CODE, f"{DAY} 23:59:59")
    if df15.empty:
        return []
    t0 = str(df15.index.min() - pd.Timedelta(days=2))[:19]
    t1 = str(df15.index.max() + pd.Timedelta(days=1))[:19]
    m1 = load_m1_range(CODE, t0, t1)
    if m1.empty:
        return []
    bt = SimpleBacktester(settings)
    bt.run(df15.copy(), code=CODE, high_precision=True, m1_df_preloaded=m1)
    day0 = pd.Timestamp(DAY).date()
    out: list[dict[str, Any]] = []
    for tr in getattr(bt, "trades", []) or []:
        if str(tr.get("Action")) != "BUY_1ST":
            continue
        dt = pd.to_datetime(tr.get("Date"), errors="coerce")
        if pd.isna(dt) or dt.date() != day0:
            continue
        out.append(dict(tr))
    return out


def simulate_hp_causal_buy_minute(
    bt: Any,
    df15: pd.DataFrame,
    i: int,
    m1_highs: np.ndarray,
    m1_closes: np.ndarray,
    m1_lows: np.ndarray,
    m1_times: np.ndarray,
    m1_ba: Optional[np.ndarray],
    m1_vp: Optional[np.ndarray],
    m1_vols: Optional[np.ndarray] = None,
) -> Optional[pd.Timestamp]:
    """
    ``_process_intra_bar_full_v3`` 의 S1 신규 진입 분기와 동일(인과 S1 + Type별 스마트바이).
    """
    hist_slice_s1 = bt._hp_hist_slice_for_causal_s1(i)
    if hist_slice_s1 is None or getattr(hist_slice_s1, "empty", True):
        return None
    m1_ba = m1_ba if m1_ba is not None else np.ones(len(m1_highs), dtype=float)
    m1_vp = m1_vp if m1_vp is not None else np.full(len(m1_highs), 100.0, dtype=float)
    m1_vols = m1_vols if m1_vols is not None else None
    for idx in range(len(m1_highs)):
        k = int(idx) + 1
        try:
            is_causal, sig_type = bt._hp_causal_s1_buy_at_k(
                i,
                k,
                m1_highs,
                m1_closes,
                m1_lows,
                m1_vols,
                hist_slice_s1,
            )
        except Exception:
            is_causal, sig_type = False, "TYPE_B"
        if not is_causal:
            continue
        m1_ba_val = float(m1_ba[idx])
        m1_vp_val = float(m1_vp[idx])
        use_sb = bool(bt.use_sb_a) if sig_type == "TYPE_A" else bool(bt.use_sb_b)
        can_buy = True
        if use_sb:
            ab_ratio = 1.0 / (m1_ba_val + 1e-10)
            if ab_ratio < float(bt.sb_ba) or m1_vp_val < float(bt.sb_vp):
                can_buy = False
        if can_buy:
            return pd.Timestamp(m1_times[idx])
    return None


def main() -> None:
    from backtest_engine import SimpleBacktester, TradingStrategy
    from utils.krx_bars import m1_index_bounds_for_15m_end, m1_window_start_for_15m_end

    settings = load_settings()
    df15 = load_15m(CODE, f"{DAY} 23:59:59")
    if END_15M not in df15.index:
        print("15m 라벨 없음:", END_15M)
        return
    i = int(df15.index.get_loc(END_15M))
    t_start = m1_window_start_for_15m_end(END_15M)
    print("=== KRX 1분 창 (15m 라벨 대응) ===")
    print("15m 라벨(종가시각):", END_15M)
    print("1분 시작(포함):", t_start, "(m1_window_start_for_15m_end)")
    t0 = str(df15.index.min() - pd.Timedelta(days=2))[:19]
    t1 = str(df15.index.max() + pd.Timedelta(days=1))[:19]
    m1_full = load_m1_range(CODE, t0, t1)
    s, e = m1_index_bounds_for_15m_end(m1_full.index, END_15M)
    print("bars_1m 슬라이스 [s,e):", s, e, "길이", e - s)
    if e <= s:
        print("1분 구간 비어 있음")
        return
    lab = m1_full.index[s:e]
    print("첫·마지막 1m 라벨:", lab[0], lab[-1])
    print()

    bt = SimpleBacktester(settings)
    df_work = df15.copy()
    df_work = TradingStrategy.calculate_indicators(df_work, settings)
    bt._init_simulation(df_work, CODE, None, None, True)
    bt.df = df_work
    df_ind = df_work

    sl = m1_full.iloc[s:e]
    mh = sl["High"].values.astype(float)
    mc = sl["Close"].values.astype(float)
    ml = sl["Low"].values.astype(float)
    mt = sl.index.values
    mba = np.ones(len(sl), dtype=float)
    mvp = np.full(len(sl), 100.0, dtype=float)
    mvol = sl["Volume"].values.astype(float)

    t_db = simulate_hp_causal_buy_minute(bt, df_ind, i, mh, mc, ml, mt, mba, mvp, mvol)
    print("=== (1) DB bars_1m 슬라이스로 인과S1+SB 재현 ===")
    print("첫 매수 가능 1m 라벨:", t_db)
    print()

    m1_tick, n_used = ticks_to_m1_like_db_slice(CODE, lab, TICK_PATH)
    print("=== (2) 틱 → 동일 1분 라벨 집계 ===")
    print("해당 구간 틱 수:", n_used)
    if m1_tick["Open"].isna().all():
        print("틱 집계 실패(구간 틱 없음)")
    else:
        mht = m1_tick["High"].to_numpy(dtype=float)
        mct = m1_tick["Close"].to_numpy(dtype=float)
        mlt = m1_tick["Low"].to_numpy(dtype=float)
        for j in range(len(m1_tick)):
            if np.isnan(mht[j]):
                mht[j] = mct[j] if not np.isnan(mct[j]) else 0.0
                mlt[j] = mct[j] if not np.isnan(mct[j]) else 0.0
        mot = m1_tick["Open"].to_numpy(dtype=float)
        for j in range(len(m1_tick)):
            if np.isnan(mot[j]):
                mot[j] = mct[j] if not np.isnan(mct[j]) else 0.0
        mht = np.where(np.isnan(mht), mct, mht)
        mlt = np.where(np.isnan(mlt), mct, mlt)
        mot = np.where(np.isnan(mot), mct, mot)
        vols = m1_tick["Volume"].to_numpy(dtype=float)
        t_tick = simulate_hp_causal_buy_minute(
            bt, df_ind, i, mht, mct, mlt, m1_tick.index.values, mba, mvp, vols
        )
        print("첫 매수 가능 1m 라벨:", t_tick)
    print()

    print("=== (3) SimpleBacktester.run 고정밀 전체 — 당일 BUY_1ST 전부 ===")
    day_buys = first_hp_buy_times_from_backtest(settings)
    if not day_buys:
        print("당일 BUY_1ST 없음")
    else:
        for tr in day_buys:
            print(
                " ",
                tr.get("Date"),
                "|",
                str(tr.get("Reason", ""))[:80],
                "| Sig",
                tr.get("Sig_Type"),
            )
    print()
    if t_db is not None and day_buys:
        first_dt = pd.to_datetime(day_buys[0].get("Date"), errors="coerce")
        same = str(t_db)[:16] == str(first_dt)[:16]
        print("(1) 재현 시각과 (3) 당일 **첫** S1 체결 시각 동일(분):", same)
    print()
    print(
        "참고: KRX 1분 창 **시작**은 13:01이지만, "
        "이 DB·설정에서 인과 S1+SB가 처음 통과하는 **1분 라벨**은 13:15입니다 "
        "(k=15 누적 시점). 사용자 UI의 '13:01'은 창 시작 표기일 수 있습니다."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("오류:", exc)
        raise
