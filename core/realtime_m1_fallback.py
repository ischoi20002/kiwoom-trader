# -*- coding: utf-8 -*-
"""
실시간 인트라바 미러용 1분봉 확보 — 메모리(RtTickM1Store) + DB(bars_1m) 폴백.

ka10080(REST) 실패·502 시 M1 저장소가 비어 ``evaluate_bt_hp_intrabar_unified`` 가
동작하지 않아 BT 대비 RT 매매가 0건에 가깝게 나오는 문제를 완화합니다.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from utils.normalization import canonical_stock_code


def iter_m1_open_labels_inclusive(
    start_incl: pd.Timestamp, end_incl: pd.Timestamp
) -> List[pd.Timestamp]:
    """``[start_incl, end_incl]`` 1분 open 라벨 목록(포함)."""
    out: List[pd.Timestamp] = []
    try:
        cur = pd.Timestamp(start_incl).floor("min")
        end = pd.Timestamp(end_incl).floor("min")
        if end < cur:
            return out
        while cur <= end:
            out.append(cur)
            cur = cur + pd.Timedelta(minutes=1)
    except Exception:
        return []
    return out


def chart_1m_open_labels_in_range(
    df_1m: Optional[pd.DataFrame],
    start_incl: pd.Timestamp,
    end_incl: pd.Timestamp,
) -> Set[pd.Timestamp]:
    """
    ka10080 차트 인덱스 → open 라벨 집합.

    연속매매: cntr_tm = 봉 종가 → open = 종가 − 1분.
    종가 단일가(15:30): cntr_tm = 15:30 → open = 15:30.
    """
    found: Set[pd.Timestamp] = set()
    try:
        if df_1m is None or getattr(df_1m, "empty", True):
            return found
        lo = pd.Timestamp(start_incl).floor("min")
        hi = pd.Timestamp(end_incl).floor("min")
        from utils.krx_session_hours import get_session_hours

        for end_ts in df_1m.index:
            try:
                end_lab = pd.Timestamp(end_ts).floor("min")
                try:
                    h = get_session_hours(end_lab)
                    hm = int(end_lab.hour) * 60 + int(end_lab.minute)
                    if hm == int(h.close_minutes):
                        open_lab = end_lab
                    else:
                        open_lab = end_lab - pd.Timedelta(minutes=1)
                except Exception:
                    open_lab = end_lab - pd.Timedelta(minutes=1)
                if lo <= open_lab <= hi:
                    found.add(open_lab)
            except Exception:
                continue
    except Exception:
        return found
    return found


def official_m1_open_labels_in_store(
    app: Any,
    code: str,
    start_incl: pd.Timestamp,
    end_incl: pd.Timestamp,
) -> Set[pd.Timestamp]:
    """RtTickM1Store 공식 completed 구간의 open 라벨 집합."""
    found: Set[pd.Timestamp] = set()
    try:
        nk = canonical_stock_code(code)
        if not nk:
            return found
        m1s = getattr(app, "rt_tick_m1_store", None)
        if m1s is None:
            return found
        df = m1s.dataframe_for_window(
            nk,
            pd.Timestamp(start_incl).floor("min"),
            pd.Timestamp(end_incl).floor("min"),
            include_open_bucket=False,
        )
        if df is None or getattr(df, "empty", True):
            return found
        for ts in df.index:
            try:
                found.add(pd.Timestamp(ts).floor("min"))
            except Exception:
                continue
    except Exception:
        return found
    return found


def build_m1_head_catchup_report(
    app: Any,
    code: str,
    *,
    slot: Any,
    head_start: pd.Timestamp,
    head_last_open: pd.Timestamp,
    chart_df: Optional[pd.DataFrame],
    applied: int,
    status: str,
    detail: str = "",
) -> Dict[str, Any]:
    """
    헤드 soft catchup 커버리지 리포트.

    - ``expected_n``: 헤드 구간 기대 1분 수(일반 슬롯 14)
    - ``chart_n``: 차트 페이지에 있던 헤드 분 수
    - ``store_n``: catchup 후 store 공식 completed 수
    - ``missing``: store에 없는 open 라벨(HH:MM, 최대 14)
    """
    nk = canonical_stock_code(code) or str(code)
    expected = iter_m1_open_labels_inclusive(head_start, head_last_open)
    exp_set = set(expected)
    chart_set = chart_1m_open_labels_in_range(chart_df, head_start, head_last_open)
    store_set = official_m1_open_labels_in_store(
        app, nk, head_start, head_last_open
    )
    missing = sorted(exp_set - store_set)
    missing_hm = [t.strftime("%H:%M") for t in missing[:14]]
    chart_miss = sorted(exp_set - chart_set)
    chart_miss_hm = [t.strftime("%H:%M") for t in chart_miss[:14]]
    try:
        slot_s = pd.Timestamp(slot).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        slot_s = str(slot)
    complete = bool(expected) and len(missing) == 0
    return {
        "code": nk,
        "slot": slot_s,
        "head_start": pd.Timestamp(head_start).strftime("%Y-%m-%d %H:%M:%S"),
        "head_last_open": pd.Timestamp(head_last_open).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "expected_n": int(len(expected)),
        "chart_n": int(len(chart_set & exp_set)),
        "store_n": int(len(store_set & exp_set)),
        "applied_n": int(applied),
        "complete": bool(complete),
        "missing": missing_hm,
        "chart_missing": chart_miss_hm,
        "status": str(status),
        "detail": str(detail or "")[:200],
    }


def build_m1_last_official_report(
    app: Any,
    code: str,
    *,
    closed_lab: pd.Timestamp,
    chart_df: Optional[pd.DataFrame],
    status: str,
    detail: str = "",
    source: str = "official",
) -> Dict[str, Any]:
    """
    신호평가용 슬롯 말분 공식 1분 조회 커버리지.

    - ``chart_has_last``: ka10080 차트에 말분 open 존재
    - ``store_has_last``: RtTickM1Store 공식 completed에 말분 존재
    - ``complete``: store에 말분이 있으면 True (조회·DB·캐시 반영 성공)
    """
    nk = canonical_stock_code(code) or str(code)
    try:
        lab = pd.Timestamp(closed_lab).floor("min")
    except Exception:
        lab = pd.Timestamp(closed_lab)
    chart_set = chart_1m_open_labels_in_range(chart_df, lab, lab)
    store_set = official_m1_open_labels_in_store(app, nk, lab, lab)
    chart_has = lab in chart_set
    store_has = lab in store_set
    slot_s = ""
    try:
        from utils.krx_bars import krx_15m_label_for_m1_open

        slot = krx_15m_label_for_m1_open(lab)
        if slot is not None:
            slot_s = pd.Timestamp(slot).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        slot_s = ""
    st = str(status or "")
    # 조회 자체가 성공했는지는 status로, 반영 여부는 store_has_last로 본다.
    # no_trade_skip/flat_no_trade: 증권사 무거래분 생략 — 실패가 아님.
    fetch_ok = st in (
        "ok",
        "cache_ok",
        "db_fallback",
        "flat_no_trade",
        "no_trade_skip",
    )
    is_no_trade = st in ("flat_no_trade", "no_trade_skip")
    return {
        "code": nk,
        "last_open": lab.strftime("%Y-%m-%d %H:%M:%S"),
        "slot": slot_s,
        "status": st,
        "detail": str(detail or "")[:200],
        "source": str(source or "official"),
        "chart_has_last": bool(chart_has),
        "store_has_last": bool(store_has),
        "fetch_ok": bool(fetch_ok),
        # 무거래 생략은 store에 말분이 없어도 의도된 정상 처리
        "complete": bool(store_has) or bool(is_no_trade),
        "no_trade": bool(is_no_trade),
    }


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
        from utils.krx_session_hours import get_session_hours

        for end_ts, row in df_1m.sort_index().iterrows():
            try:
                end_lab = pd.Timestamp(end_ts).floor("min")
                try:
                    h = get_session_hours(end_lab)
                    hm = int(end_lab.hour) * 60 + int(end_lab.minute)
                    if hm == int(h.close_minutes):
                        open_lab = end_lab
                    else:
                        open_lab = end_lab - pd.Timedelta(minutes=1)
                except Exception:
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
