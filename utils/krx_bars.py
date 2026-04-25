# -*- coding: utf-8 -*-
"""
KRX 장중 15분봉·1분 창의 시간 정의.

pandas 기본 ``resample('15min')`` 은 label=left, closed=left 로
「09:45」 행이 [09:45, 10:00) 1분봉을 집계해, (09:30,09:45] 및 인트라바(09:31~09:45)와
저가/체결 정합이 깨진다. DB ``bars_15m`` 는 ``label=right, closed=right`` 집계 뒤,
첫 09:00~09:15 는 1m [09:00, 09:15] 로 병합해 보정한다.
"""
from __future__ import annotations

from datetime import datetime
from typing import Union

import pandas as pd

from utils.normalization import normalize_code

__all__ = [
    "m1_window_start_for_15m_end",
    "resample_1m_ohlcv_to_15m_krx",
    "KRX_15M_RESAMPLE_KWARGS",
]

# DB·백텐·키움 REST resample(r/r)과 동일
KRX_15M_RESAMPLE_KWARGS: dict = {"label": "right", "closed": "right"}


def m1_window_start_for_15m_end(
    end_ts: Union[pd.Timestamp, datetime, str],
) -> pd.Timestamp:
    """
    15분봉 행 시각(구간 **종가 시각**에 해당)일 때, 동일 15m 을 구성하는 1분봉 **시작(포함)**.

    - [09:00, 09:15] (첫 봉) → 시작 09:00 (T-15분, 최대 16개 1m)
    - 이후 15m → 시작 end-14분 (T-14, 15개 1m; 예: 09:45봉 → 09:31~09:45)
    """
    t = pd.Timestamp(end_ts)
    if t.hour == 9 and t.minute == 15:
        return t - pd.Timedelta(minutes=15)
    return t - pd.Timedelta(minutes=14)


def resample_1m_ohlcv_to_15m_krx(
    df_1m: pd.DataFrame, code: str
) -> pd.DataFrame:
    """
    1m OHLCV(열: open, high, low, close, volume, index=datetime)을
    KRX 정의에 맞는 15m DataFrame(열 datetime+OHLCV+code)로 집계한다.
    """
    ncode = normalize_code(code)
    _empty = pd.DataFrame(
        columns=["datetime", "open", "high", "low", "close", "volume", "code"]
    )
    if df_1m is None or len(df_1m) == 0:
        return _empty

    try:
        s = df_1m.copy()
        s = s.sort_index()
        s.index = pd.to_datetime(s.index, errors="coerce")
        s = s[s.index.notna()]
        for c in ("open", "high", "low", "close", "volume"):
            if c not in s.columns:
                return _empty
            s[c] = pd.to_numeric(s[c], errors="coerce")
        s = s.dropna(subset=["open", "high", "low", "close", "volume"])
        if s.empty:
            return _empty
    except (TypeError, ValueError, KeyError) as e:
        print(f"[krx_bars] 1m 전처리 실패: {e!r}", flush=True)
        return _empty

    try:
        ohlc = s.resample("15min", **KRX_15M_RESAMPLE_KWARGS).agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        ohlc = ohlc.dropna()
    except (TypeError, ValueError) as e:
        print(f"[krx_bars] resample 실패: {e!r}", flush=True)
        return _empty
    except (KeyboardInterrupt, SystemExit) as e:
        raise
    except Exception as e:  # noqa: BLE001
        try:
            print(f"[krx_bars] resample 예기치 않은 실패: {e!r}", flush=True)
        except Exception:  # noqa: BLE001
            pass
        return _empty

    # 날짜마다: (8:45,9:0] 를 달리 쓰는 09:00 15m 행 제거, 09:15 는 [9:0,9:15] 1m 로 재집계
    for day, _ in s.groupby(s.index.date):
        d0 = pd.Timestamp(day)
        t00 = d0 + pd.Timedelta(hours=9)
        t915 = d0 + pd.Timedelta(hours=9, minutes=15)
        mseg = s.loc[(s.index >= t00) & (s.index <= t915)]
        if t00 in ohlc.index:
            ohlc = ohlc.drop(t00)
        if mseg.empty or t915 not in ohlc.index:
            continue
        try:
            ohlc.loc[t915, "open"] = float(mseg["open"].iloc[0])
            ohlc.loc[t915, "high"] = float(mseg["high"].max())
            ohlc.loc[t915, "low"] = float(mseg["low"].min())
            ohlc.loc[t915, "close"] = float(mseg["close"].iloc[-1])
            ohlc.loc[t915, "volume"] = float(mseg["volume"].sum())
        except (TypeError, ValueError, KeyError) as e:
            try:
                print(f"[krx_bars] 09:15 병합 경고(해당일 스킵): {e!r}", flush=True)
            except (OSError, TypeError) as e2:  # noqa: BLE001
                pass

    try:
        out = ohlc.reset_index()
        if out.columns[0] != "datetime":
            out = out.rename(columns={out.columns[0]: "datetime"})
    except (KeyError, AttributeError, ValueError) as e:
        print(f"[krx_bars] reset_index 실패: {e!r}", flush=True)
        return _empty

    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    out["code"] = ncode
    return out
