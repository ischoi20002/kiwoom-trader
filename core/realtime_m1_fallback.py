# -*- coding: utf-8 -*-
"""
실시간 인트라바 미러용 1분봉 확보 — 메모리(RtTickM1Store) + DB(bars_1m) 폴백.

ka10080(REST) 실패·502 시 M1 저장소가 비어 ``evaluate_bt_hp_intrabar_unified`` 가
동작하지 않아 BT 대비 RT 매매가 0건에 가깝게 나오는 문제를 완화합니다.
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

import pandas as pd

from utils.normalization import canonical_stock_code


def _settings_bool(settings: Any, key: str, default: bool = True) -> bool:
    try:
        if settings is None:
            return default
        v = settings.get(key, default)
        if isinstance(v, bool):
            return v
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return default


def closed_minute_ohlcv_from_db(
    app: Any,
    code: str,
    closed_open: pd.Timestamp,
) -> Optional[Tuple[float, float, float, float, float]]:
    """
    ``bars_1m`` 에서 닫힌 1분(오픈 라벨 = ``closed_open``) OHLCV 를 읽습니다.
  live 당일 분은 하베스팅 전엔 없을 수 있으나, API 실패·장후 정합에 사용합니다.
    """
    try:
        nk = canonical_stock_code(code)
        if not nk:
            return None
        dm = getattr(app, "data_manager", None)
        if dm is None:
            return None
        ol = pd.Timestamp(closed_open).floor("min")
        ds = ol.strftime("%Y-%m-%d %H:%M:%S")
        df = dm.get_bars(nk, start_dt=ds, end_dt=ds, interval="1m")
        if df is None or getattr(df, "empty", True):
            return None
        row = df.iloc[-1]
        o = float(row["Open"])
        h = float(row["High"])
        l_ = float(row["Low"])
        c = float(row["Close"])
        v = float(row.get("Volume", row.get("volume", 0)) or 0)
        return o, h, l_, c, max(0.0, v)
    except Exception:
        return None


def apply_chart_catchup_to_m1_store(
    app: Any,
    code: str,
    df_1m: pd.DataFrame,
    upto_closed_open: pd.Timestamp,
    *,
    from_open: Optional[pd.Timestamp] = None,
) -> int:
    """
    ka10080 차트에서 ``upto_closed_open`` 까지의 **완료 1분**을 RtTickM1Store 에 누락 없이 반영.

    ka10080 인덱스는 ``cntr_tm`` = **봉 종가 시각**(분 끝)이므로 오픈 라벨 = 종가 시각 − 1분.
    ``from_open`` 이 있으면 그 시각(포함)부터만 반영(슬롯 head 일괄용).
    없으면 RtTickM1Store 보관 길이(최근 N분)로 하한을 잡는다.
    반환: 새로 반영한 분 수.
    """
    try:
        nk = canonical_stock_code(code)
        if not nk or df_1m is None or getattr(df_1m, "empty", True):
            return 0
        m1s = getattr(app, "rt_tick_m1_store", None)
        if m1s is None:
            return 0
        upto = pd.Timestamp(upto_closed_open).floor("min")
        if from_open is not None:
            from_open_ts = pd.Timestamp(from_open).floor("min")
        else:
            try:
                max_completed = int(getattr(m1s, "_max_c", 24) or 24)
            except (TypeError, ValueError):
                max_completed = 24
            max_completed = max(8, min(max_completed, 64))
            from_open_ts = upto - pd.Timedelta(minutes=max_completed - 1)

        existing = pd.DataFrame()
        try:
            existing = m1s.dataframe_for_window(
                nk, from_open_ts, upto, include_open_bucket=False
            )
            if existing is None:
                existing = pd.DataFrame()
            else:
                existing = existing.copy()
                existing.index = pd.to_datetime(existing.index).floor("min")
        except Exception:
            existing = pd.DataFrame()

        def _same_existing_row(
            open_lab: pd.Timestamp,
            o: float,
            h: float,
            l_: float,
            c: float,
            v: float,
        ) -> bool:
            """이미 같은 공식 1분 OHLCV가 있으면 재반영하지 않는다."""
            try:
                if existing is None or existing.empty or open_lab not in existing.index:
                    return False
                erow = existing.loc[open_lab]
                if isinstance(erow, pd.DataFrame):
                    erow = erow.iloc[-1]
                vals = (
                    float(erow["Open"]),
                    float(erow["High"]),
                    float(erow["Low"]),
                    float(erow["Close"]),
                    float(erow.get("Volume", 0) or 0),
                )
                want = (o, h, l_, c, max(0.0, v))
                return all(abs(vals[i] - want[i]) < 1e-9 for i in range(5))
            except Exception:
                return False

        applied = 0
        for end_ts, row in df_1m.sort_index().iterrows():
            try:
                end_lab = pd.Timestamp(end_ts).floor("min")
                open_lab = end_lab - pd.Timedelta(minutes=1)
                if open_lab < from_open_ts or open_lab > upto:
                    continue
                o = float(row["Open"])
                h = float(row["High"])
                l_ = float(row["Low"])
                c = float(row["Close"])
                v = float(row.get("Volume", 0) or 0)
                if _same_existing_row(open_lab, o, h, l_, c, v):
                    continue
                before = int(m1s.revision_for_code(nk))
                m1s.apply_official_closed_minute(nk, open_lab, o, h, l_, c, v)
                after = int(m1s.revision_for_code(nk))
                if after > before:
                    applied += 1
            except Exception:
                continue
        return applied
    except Exception:
        return 0


def load_intrabar_m1_window(
    app: Any,
    code: str,
    t0: pd.Timestamp,
    t_hi: pd.Timestamp,
    *,
    include_open_bucket: bool = False,
) -> pd.DataFrame:
    """
    인트라바 미러용 ``[t0, t_hi]`` 1분 DataFrame.
    1) RtTickM1Store  2) 부족 시 bars_1m DB 병합.
    """
    out_empty = pd.DataFrame()
    try:
        nk = canonical_stock_code(code)
        if not nk:
            return out_empty
        t0s = pd.Timestamp(t0)
        this = pd.Timestamp(t_hi)
        if this < t0s:
            return out_empty

        m1s = getattr(app, "rt_tick_m1_store", None)
        df_store = out_empty
        if m1s is not None:
            try:
                df_store = m1s.dataframe_for_window(
                    nk, t0s, this, include_open_bucket=include_open_bucket
                )
            except TypeError:
                df_store = m1s.dataframe_for_window(nk, t0s, this)

        st = getattr(app, "settings", None) or {}
        if not _settings_bool(st, "realtime_intrabar_m1_db_fallback", True):
            return df_store if df_store is not None else out_empty

        dm = getattr(app, "data_manager", None)
        if dm is None:
            return df_store if df_store is not None else out_empty

        start_s = t0s.strftime("%Y-%m-%d %H:%M:%S")
        end_s = this.strftime("%Y-%m-%d %H:%M:%S")
        df_db = dm.get_bars(nk, start_dt=start_s, end_dt=end_s, interval="1m")
        if df_db is None or getattr(df_db, "empty", True):
            return df_store if df_store is not None else out_empty

        try:
            df_db = df_db.copy()
            if "time" in df_db.columns:
                df_db = df_db.set_index("time")
            elif "datetime" in df_db.columns:
                df_db = df_db.set_index("datetime")
            df_db.index = pd.to_datetime(df_db.index)
            df_db = df_db.rename(
                columns={
                    "open": "Open",
                    "high": "High",
                    "low": "Low",
                    "close": "Close",
                    "volume": "Volume",
                }
            )
            keep = [c for c in ("Open", "High", "Low", "Close", "Volume") if c in df_db.columns]
            df_db = df_db[keep]
        except Exception:
            return df_store if df_store is not None else out_empty

        if df_store is None or getattr(df_store, "empty", True):
            return df_db.sort_index()

        try:
            merged = pd.concat([df_store, df_db])
            merged = merged[~merged.index.duplicated(keep="last")].sort_index()
            return merged.loc[t0s:this]
        except Exception:
            return df_store
    except Exception:
        return out_empty
