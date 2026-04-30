# -*- coding: utf-8 -*-
"""
실시간 15분 캐시 DB(realtime_15m_mock.db)와 백테 DB(trading_data.db)의
15분봉/신호를 같은 슬롯 기준으로 비교한다.
"""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Any, Dict, List, Optional

import pandas as pd

from backtest_engine import TradingStrategy

ROOT = os.path.dirname(os.path.abspath(__file__))
TARGET_DAY = "2026-04-30"
TARGET_CODES = {
    "352820": "하이브",
    "001430": "세아베스틸지주",
}
HINT_SLOTS = {
    "352820": ["2026-04-30 09:15:00"],
    "001430": ["2026-04-30 09:45:00"],
}


def load_settings() -> Dict[str, Any]:
    path = os.path.join(ROOT, "settings.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_rt15m(code: str) -> Optional[pd.DataFrame]:
    db_path = os.path.join(ROOT, "realtime_15m_mock.db")
    sql = """
    SELECT code, datetime, open, high, low, close, volume
    FROM bars_15m_rt
    WHERE code = ? AND datetime BETWEEN ? AND ?
    ORDER BY datetime
    """
    start = "2026-03-01 00:00:00"
    end = f"{TARGET_DAY} 23:59:59"
    with sqlite3.connect(db_path) as con:
        df = pd.read_sql_query(sql, con, params=[code, start, end])
    if df.empty:
        return None
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )
    return df.set_index("datetime")[["Open", "High", "Low", "Close", "Volume"]]


def load_td15m(code: str) -> Optional[pd.DataFrame]:
    db_path = os.path.join(ROOT, "trading_data.db")
    sql = """
    SELECT code, datetime, open, high, low, close, volume
    FROM bars_15m
    WHERE code = ? AND datetime BETWEEN ? AND ?
    ORDER BY datetime
    """
    start = "2026-03-01 00:00:00"
    end = f"{TARGET_DAY} 23:59:59"
    with sqlite3.connect(db_path) as con:
        df = pd.read_sql_query(sql, con, params=[code, start, end])
    if df.empty:
        return None
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.rename(
        columns={
            "open": "Open",
            "high": "High",
            "low": "Low",
            "close": "Close",
            "volume": "Volume",
        }
    )
    return df.set_index("datetime")[["Open", "High", "Low", "Close", "Volume"]]


def add_signals(df: pd.DataFrame, settings: Dict[str, Any]) -> pd.DataFrame:
    ind = TradingStrategy.calculate_indicators(df, settings)
    sig = TradingStrategy.get_buy_signals_detailed(ind, settings, market_trend="UP")
    out = ind.join(sig[["Signal", "Sig_Type", "Met_Ratio"]], how="left")
    return out


def fmt_bool(v: Any) -> str:
    return "Y" if bool(v) else "N"


def print_hint_rows(
    code: str, name: str, rt_df: pd.DataFrame, td_df: pd.DataFrame
) -> None:
    print(f"\n[{code} {name}] 실시간 매수시각 대응 슬롯 비교")
    for s in HINT_SLOTS.get(code, []):
        ts = pd.Timestamp(s)
        rt = rt_df.loc[ts] if ts in rt_df.index else None
        td = td_df.loc[ts] if ts in td_df.index else None
        print(f"- 슬롯 {s}")
        if rt is None:
            print("  RT: 행 없음")
        else:
            print(
                "  RT: "
                f"O={int(rt['Open'])} H={int(rt['High'])} L={int(rt['Low'])} C={int(rt['Close'])} V={int(rt['Volume'])} "
                f"| RSI={rt.get('RSI', 0):.2f} MACD={rt.get('MACD', 0):.2f} OSC={rt.get('OSC', 0):.2f} "
                f"| Signal={fmt_bool(rt.get('Signal'))} ({rt.get('Sig_Type', '-')})"
            )
        if td is None:
            print("  TD: 행 없음")
        else:
            print(
                "  TD: "
                f"O={int(td['Open'])} H={int(td['High'])} L={int(td['Low'])} C={int(td['Close'])} V={int(td['Volume'])} "
                f"| RSI={td.get('RSI', 0):.2f} MACD={td.get('MACD', 0):.2f} OSC={td.get('OSC', 0):.2f} "
                f"| Signal={fmt_bool(td.get('Signal'))} ({td.get('Sig_Type', '-')})"
            )


def print_day_summary(code: str, name: str, rt_df: pd.DataFrame, td_df: pd.DataFrame) -> None:
    rt_day = rt_df.loc[TARGET_DAY : TARGET_DAY + " 23:59:59"].copy()
    td_day = td_df.loc[TARGET_DAY : TARGET_DAY + " 23:59:59"].copy()
    rt_sig = rt_day[rt_day["Signal"] == True]  # noqa: E712
    td_sig = td_day[td_day["Signal"] == True]  # noqa: E712
    print(f"\n[{code} {name}] {TARGET_DAY} 하루 요약")
    print(f"- RT rows={len(rt_day)}, Signal=True={len(rt_sig)}")
    print(f"- TD rows={len(td_day)}, Signal=True={len(td_sig)}")
    if not rt_sig.empty:
        print("- RT 신호 슬롯:", ", ".join(rt_sig.index.strftime("%H:%M")))
    if not td_sig.empty:
        print("- TD 신호 슬롯:", ", ".join(td_sig.index.strftime("%H:%M")))


def print_ohlcv_mismatch(code: str, name: str, rt_df: pd.DataFrame, td_df: pd.DataFrame) -> None:
    cols = ["Open", "High", "Low", "Close", "Volume"]
    rt_day = rt_df.loc[TARGET_DAY : TARGET_DAY + " 23:59:59", cols]
    td_day = td_df.loc[TARGET_DAY : TARGET_DAY + " 23:59:59", cols]
    common = rt_day.index.intersection(td_day.index)
    if len(common) == 0:
        print(f"\n[{code} {name}] 공통 슬롯 없음")
        return
    diffs: List[str] = []
    for ts in common:
        r = rt_day.loc[ts]
        t = td_day.loc[ts]
        changed = [c for c in cols if float(r[c]) != float(t[c])]
        if changed:
            diffs.append(f"{ts.strftime('%H:%M')}({','.join(changed)})")
    print(f"\n[{code} {name}] OHLCV 불일치 슬롯 수: {len(diffs)}/{len(common)}")
    if diffs:
        print("- 불일치 슬롯:", ", ".join(diffs[:12]) + (" ..." if len(diffs) > 12 else ""))


def main() -> None:
    settings = load_settings()
    print("=== RT15M vs TradingData 15M 비교 ===")
    for code, name in TARGET_CODES.items():
        rt = load_rt15m(code)
        td = load_td15m(code)
        if rt is None or td is None:
            print(f"\n[{code} {name}] 데이터 없음 (rt={rt is not None}, td={td is not None})")
            continue
        rt_sig = add_signals(rt, settings)
        td_sig = add_signals(td, settings)
        print_day_summary(code, name, rt_sig, td_sig)
        print_ohlcv_mismatch(code, name, rt_sig, td_sig)
        print_hint_rows(code, name, rt_sig, td_sig)
    print("\n완료")


if __name__ == "__main__":
    main()
