# -*- coding: utf-8 -*-
"""동적 유니버스 준비·스케줄·진단 (백테스트용)."""
from __future__ import annotations

import logging
import os
import traceback
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import numpy as np
import pandas as pd

from strategy.selection_dynamic_score import (
    _score_dynamic_universe_daily_ohlcv,
    _selection_daily_lookback_calendar_days,
    _universe_batch_indicator_left_clip,
)
from strategy.selection_live_aux import _df_top_by_score_slot_band
from strategy.selection_regime import (
    _build_index_vol_by_day,
    _load_kodex200_daily_close,
    classify_transition_regime,
    compute_rs_rank_corr_by_day,
    is_rotation_a_guard_g1_day,
    is_rs_persist_guard_day,
    resolve_rs_persist_corr_threshold,
    resolve_transition_regime_policy,
)
from strategy.selection_scoring import _settings_bool_sel
from strategy.selection_track_batch import _resync_batch_valid_to_period_universe
from strategy.universe_codes import load_kodex200_universe_codes

# 슬라이딩 창 복사 시 필요한 원시 OHLCV 열만 유지 (지표는 일자마다 재생성)
_SLIDING_OHLCV_COLS: Tuple[str, ...] = (
    "code",
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


def _sliding_score_workers(settings: Optional[dict]) -> int:
    """
    슬라이딩 일자 병렬 워커 수.

    Track C(박스) 가 CPU 바운드라 ProcessPool 이 유리하다.
    기본(0): min(8, CPU). 1 이면 순차.
    """
    try:
        raw = int((settings or {}).get("bt_sliding_score_workers", 0) or 0)
    except Exception:
        raw = 0
    cpu = max(1, int(os.cpu_count() or 1))
    if raw == 1:
        return 1
    if raw <= 0:
        return max(1, min(8, cpu))
    return max(1, min(int(raw), 16, cpu))


# ProcessPool 워커 전역 (initializer 로 주입)
_SLIDING_POOL_DF: Optional[pd.DataFrame] = None
_SLIDING_POOL_SETTINGS: Optional[dict] = None


def _sliding_pool_init(df: pd.DataFrame, settings: dict) -> None:
    """자식 프로세스에 OHLCV·설정을 1회만 적재."""
    global _SLIDING_POOL_DF, _SLIDING_POOL_SETTINGS
    _SLIDING_POOL_DF = df
    _SLIDING_POOL_SETTINGS = settings


def _sliding_pool_job(
    payload: Tuple[str, str, int, int, Tuple[str, ...]],
) -> Optional[Tuple[str, pd.DataFrame, int, int, float]]:
    """(d_str, d_ts_iso, li, ri, univ_tuple) → 스냅샷."""
    d_str, d_ts_iso, li, ri, univ_tuple = payload
    try:
        if _SLIDING_POOL_DF is None or _SLIDING_POOL_SETTINGS is None:
            return None
        return _score_one_sliding_day(
            _SLIDING_POOL_DF,
            li=int(li),
            ri=int(ri),
            d_str=str(d_str),
            d_ts=pd.Timestamp(d_ts_iso),
            settings=_SLIDING_POOL_SETTINGS,
            univ_set=set(univ_tuple),
        )
    except Exception as e:
        try:
            logging.warning(
                "슬라이딩 프로세스 스코어 실패 %s: %s", d_str, e, exc_info=True
            )
        except Exception:
            pass
        return None


def _score_one_sliding_day(
    df_ohlcv: pd.DataFrame,
    *,
    li: int,
    ri: int,
    d_str: str,
    d_ts: pd.Timestamp,
    settings: dict,
    univ_set: Set[str],
) -> Optional[Tuple[str, pd.DataFrame, int, int, float]]:
    """
    평가일 D에 대해 ``[D-N, D]`` 창을 스코어하고 당일·당기유니버스 스냅샷을 반환.

    - 지표는 창 전체에서 재계산 (실시간 as-of 와 동일 EWM 재시작)
    - RS/Valid resync 는 **당일 행만** 수행 (일자 cross-section 이라 전체창 resync 와 동치)
    """
    try:
        if ri <= li:
            return None
        cols = [c for c in _SLIDING_OHLCV_COLS if c in df_ohlcv.columns]
        win = df_ohlcv.iloc[li:ri].loc[:, cols].copy()
        if win.empty:
            return None
        # code 연속 구간이 groupby.rolling/ewm 에 유리 (값 자체는 불변)
        try:
            win.sort_values(["code", "time"], kind="mergesort", inplace=True)
            win.reset_index(drop=True, inplace=True)
        except Exception:
            pass

        _score_dynamic_universe_daily_ohlcv(
            win, settings, with_b_debug=False, track_c_codes=univ_set
        )

        try:
            day_mask = win["time"].dt.normalize() == d_ts
        except Exception:
            day_mask = win["time"].dt.strftime("%Y-%m-%d") == d_str
        day = win.loc[day_mask].copy()
        if day is None or day.empty:
            return None

        # 당일만 period-universe RS/Valid 재동기화 (전체 창 루프 제거)
        def _gp(_dt: Any) -> List[str]:
            return list(univ_set)

        try:
            _resync_batch_valid_to_period_universe(day, _gp, settings)
        except Exception as e:
            try:
                logging.warning(
                    "슬라이딩 당일 resync 실패 %s: %s", d_str, e, exc_info=True
                )
            except Exception:
                pass

        try:
            codes = day["code"].astype(str).str.zfill(6)
            day_u = day.loc[codes.isin(univ_set)].copy()
        except Exception:
            day_u = day

        try:
            _va = (
                int(day_u["Valid_A"].astype(bool).sum())
                if "Valid_A" in day_u.columns
                else 0
            )
            _vb = (
                int(day_u["Valid_B"].astype(bool).sum())
                if "Valid_B" in day_u.columns
                else 0
            )
            _tot = _va + _vb
            _share = (_vb / _tot * 100.0) if _tot > 0 else 0.0
        except Exception:
            _va, _vb, _share = 0, 0, 0.0

        return (d_str, day_u, _va, _vb, float(_share))
    except Exception as e:
        try:
            logging.warning(
                "슬라이딩 일봉 스코어 실패 %s: %s", d_str, e, exc_info=True
            )
        except Exception:
            pass
        return None


def prepare_dynamic_universe(
    settings,
    start_date,
    end_date,
    data_manager,
    logger_callback,
    *,
    include_track_c_schedule: bool = False,
):
    """
    [v17.7] True Batching: 전 기간 데이터를 한 번에 조회하고 Pandas 벡터 연산으로 스크리닝.

    일봉 지표는 평가일 D마다 ``[D-N, D]`` 슬라이딩 창에서 다시 계산한다
    (N = ``bt_universe_indicator_lookback_days``, 기본 280 — 실시간 선정과 동일).
    """
    # [v18.8] Date Normalization (YYYY-MM-DD)
    start_date = pd.to_datetime(start_date).strftime('%Y-%m-%d')
    end_date = pd.to_datetime(end_date).strftime('%Y-%m-%d')
    
    logger_callback("\n" + "!" * 50)
    logger_callback(f"[Batch-v2] 유니버스 사전 선정 시작: {start_date} ~ {end_date}")
    
    # 1. 대상 영업일 리스트 확보
    trading_days = data_manager.get_trading_days(start_date, end_date)
    if not trading_days:
        logger_callback("  [Error] 거래일 데이터를 찾을 수 없습니다.")
        return {}

    # 2. 주기별 코스피 유니버스 캐싱 (monthly|…|quarterly|fourmonth|semiannual)
    universe_cache = {}
    all_days = pd.date_range(start=start_date, end=end_date, freq='B')
    union_tickers = set()
    from core.marcap_universe import (
        get_kospi_top_n_for_refresh,
        normalize_refresh_mode,
        universe_period_key,
    )

    refresh_mode = normalize_refresh_mode(settings.get("kospi_universe_refresh"))
    # monthly 외에는 기간 내 주기 갱신 — lean 단일 유니버스 생략
    use_period_univ = refresh_mode != "monthly"

    def get_period_universe(dt):
        """유니버스: KOSPI200=marcap as-of(갱신주기), KODEX200=고정 CSV."""
        try:
            idx_raw = str(settings.get("universe_index_choice", "KODEX200") or "KODEX200").strip().upper()
        except Exception:
            idx_raw = "KODEX200"
        idx_choice = "KOSPI200" if idx_raw in ("KOSPI", "KOSPI200") else "KODEX200"
        d_ts = pd.Timestamp(dt).normalize()
        cache_key = universe_period_key(d_ts, refresh_mode)
        if cache_key in universe_cache:
            return universe_cache[cache_key]
        try:
            top_n = 200
            try:
                top_n = int(settings.get("kospi_top_n", 200) or 200)
            except Exception:
                top_n = 200
            if idx_choice == "KOSPI200":
                tickers, asof_str = get_kospi_top_n_for_refresh(
                    d_ts,
                    top_n,
                    refresh_mode,
                    logger_callback=(
                        None if refresh_mode == "daily" else logger_callback
                    ),
                )
                if (
                    tickers
                    and refresh_mode == "monthly"
                    and logger_callback is not None
                ):
                    try:
                        logger_callback(
                            f"    - [Marcap] {d_ts.strftime('%Y%m')} as-of {asof_str or '?'} "
                            f"KOSPI top{top_n} → {len(tickers)}종목"
                        )
                    except Exception:
                        pass
            else:
                tickers = load_kodex200_universe_codes(logger_callback=logger_callback)
        except Exception as e:
            try:
                logging.warning("배치 유니버스 로드 실패: %s", e, exc_info=True)
            except Exception:
                pass
            tickers = []
        if tickers:
            universe_cache[cache_key] = tickers
        return tickers

    # 월별(또는 주기) marcap as-of 순회.
    # (구 Lean 시작월 단일 유니버스는 기간에 따라 같은 날 레짐이 달라져 제거)
    duration_days = (all_days[-1] - all_days[0]).days if not all_days.empty else 0

    if refresh_mode == "daily":
        logger_callback(
            f"  [Batch] 일별 marcap as-of 유니버스 순회… ({duration_days}일)"
        )
        for d in all_days:
            tickers = get_period_universe(d)
            if tickers:
                union_tickers.update(tickers)
        logger_callback(f"    - 일별 유니버스 누적 {len(union_tickers)}종목")
    elif use_period_univ:
        logger_callback(
            f"  [Batch] {refresh_mode} marcap as-of 유니버스 순회… ({duration_days}일)"
        )
        last_key = ""
        for d in all_days:
            curr_key = universe_period_key(d, refresh_mode)
            if curr_key != last_key or not union_tickers:
                union_tickers.update(get_period_universe(d))
                last_key = curr_key
                logger_callback(
                    f"    - {curr_key} 유니버스 체크 완료 (누적 {len(union_tickers)}종목)"
                )
    else:
        logger_callback(
            f"  [Batch] 월별 marcap as-of 유니버스 순회… ({duration_days}일)"
        )
        last_m = ""
        for d in all_days:
            curr_m = d.strftime("%Y%m")
            if curr_m != last_m or not union_tickers:
                union_tickers.update(get_period_universe(d))
                last_m = curr_m
                logger_callback(
                    f"    - {d.strftime('%Y-%m')} 유니버스 체크 완료 "
                    f"(누적 {len(union_tickers)}종목)"
                )
    
    if not union_tickers:
        logger_callback(
            "  [Warning] 유니버스 파일에서 유니버스를 읽지 못해 추가 시도합니다."
        )
        try:
            idx_raw = str(settings.get("universe_index_choice", "KODEX200") or "KODEX200").strip().upper()
        except Exception:
            idx_raw = "KODEX200"
        idx_choice = "KOSPI200" if idx_raw in ("KOSPI", "KOSPI200") else "KODEX200"
        candidate_files = ["kodex200.csv", "kodex_200.csv", "kospi_top_500.csv"]
        if idx_choice == "KOSPI200":
            candidate_files = ["kospi_universe.csv", "kospi_top_500.csv"]
        for f_name in candidate_files:
                if os.path.exists(f_name):
                    try:
                        df_k = pd.read_csv(f_name, dtype=str)
                        c_col = 'Code' if 'Code' in df_k.columns else ('종목코드' if '종목코드' in df_k.columns else None)
                        if c_col:
                            top_n = 200
                            try:
                                top_n = int(settings.get("kospi_top_n", 200))
                            except Exception:
                                top_n = 200
                            vals = df_k[c_col].apply(lambda x: str(x).zfill(6)).tolist()
                            if idx_choice == "KOSPI200":
                                vals = vals[: int(top_n)]
                            union_tickers = set(vals)
                            logger_callback(f"    - {f_name}에서 {len(union_tickers)}종목 로드 완료")
                            break
                    except Exception:
                        pass

    if not union_tickers:
        logger_callback("  [Error] 유니버스 종목 확보 실패.")
        return {}

    # 3. 일봉 로드: 첫 평가일(start) 기준 lookback ~ end
    #    지표는 날짜마다 [D-N, D] 창에서 다시 계산(실시간 FDR과 동일 as-of)
    all_codes = list(union_tickers)
    lookback_n = _selection_daily_lookback_calendar_days(settings)
    fetch_start = (
        pd.to_datetime(start_date) - timedelta(days=lookback_n)
    ).strftime("%Y-%m-%d")

    logger_callback(f"  [Batch] {len(all_codes)}개 종목의 전체 일봉 데이터 로드 중...")
    try:
        logger_callback(
            f"  [Batch] 슬라이딩 lookback={lookback_n}일 "
            f"(fetch={fetch_start} ~ {end_date}, 평가일마다 D-{lookback_n}~D)"
        )
    except Exception:
        pass
    placeholders = ",".join(["?"] * len(all_codes))
    query = f"""
        SELECT code, datetime as time, open, high, low, close, volume 
        FROM bars_1d 
        WHERE code IN ({placeholders}) AND datetime BETWEEN ? AND ?
        ORDER BY code, datetime
    """
    params = all_codes + [fetch_start, end_date]
    
    with data_manager._get_connection() as conn:
        df_all = pd.read_sql_query(query, conn, params=params)
    
    if df_all.empty:
        logger_callback("  [Error] DB에서 데이터를 찾을 수 없습니다. (데이터 동기화 필요)")
        return {}

    # [Bugfix] Convert time to datetime to use .dt accessor later
    df_all['time'] = pd.to_datetime(df_all['time'])
    try:
        df_all["code"] = df_all["code"].astype(str).str.zfill(6)
    except Exception:
        pass

    # 6. 스케줄 생성 — 평가일마다 [D-N, D] 창으로 지표·Valid 재판정
    logger_callback(
        "  [Batch] 날짜별 슬라이딩 lookback 지표·상위 종목 추출 중 (Track A/B/C)..."
    )
    schedule_a = {}
    schedule_b = {}
    schedule_c = {}
    schedule_c_meta: Dict[str, Dict[str, Dict[str, float]]] = {}
    schedule = {}  # 기존 호환용 (합집합)
    max_s_a = int(settings.get('max_slots_a', 20))
    max_s_b = int(settings.get('max_slots_b', 10))
    max_s_c = int(settings.get('max_slots_c', 15))
    floor_sa = max(0.0, float(settings.get("sel_track_min_score_a", 0.0) or 0.0))
    floor_sb = max(0.0, float(settings.get("sel_track_min_score_b", 0.0) or 0.0))
    floor_sc = max(0.0, float(settings.get("sel_track_min_score_c", 0.0) or 0.0))

    # 스케줄 일자 = prepare 구간의 거래일 (DB 캘린더)
    try:
        trading_days_list = [
            pd.to_datetime(d).strftime("%Y-%m-%d") for d in (trading_days or [])
        ]
    except Exception:
        trading_days_list = sorted(
            df_all.loc[
                df_all["time"] >= pd.to_datetime(start_date), "time"
            ]
            .dt.strftime("%Y-%m-%d")
            .unique()
            .tolist()
        )

    # 일별 교차-section 수익률 표준편차(%) — 원시 종가(당기 유니버스)
    xs_ret_std_by_day: Dict[str, float] = {}
    try:
        _xs_df = df_all.sort_values(["code", "time"]).copy()
        _xs_df["ret1"] = _xs_df.groupby("code")["close"].pct_change()
        _xs_df["_day"] = _xs_df["time"].dt.strftime("%Y-%m-%d")
        _xs_in = np.zeros(len(_xs_df), dtype=bool)
        _xs_univ: Dict[str, set[str]] = {}
        _xs_codes = _xs_df["code"].astype(str).str.zfill(6).tolist()
        for _i, _d in enumerate(_xs_df["_day"].tolist()):
            try:
                if _d not in _xs_univ:
                    _xs_univ[_d] = {
                        str(c).zfill(6) for c in (get_period_universe(_d) or [])
                    }
                _xs_in[_i] = _xs_codes[_i] in _xs_univ[_d]
            except Exception:
                _xs_in[_i] = False
        _xs_df = _xs_df.loc[_xs_in]
        if not _xs_df.empty:
            _xs_s = _xs_df.groupby("time")["ret1"].std() * 100.0
            for t_idx, val in _xs_s.items():
                if val is not None and np.isfinite(float(val)):
                    xs_ret_std_by_day[pd.Timestamp(t_idx).strftime("%Y-%m-%d")] = float(
                        val
                    )
    except Exception:
        xs_ret_std_by_day = {}

    # [Transition] 백테스트: 일별 레짐 게이트
    transition_gate_on = _settings_bool_sel(settings, "use_transition_gate", False)
    g1_guard_on = _settings_bool_sel(settings, "use_rotation_a_guard_g1", False)
    r5_guard_on = _settings_bool_sel(settings, "use_rs_persist_guard", False)
    transition_days = 0
    transition_hard_days = 0
    transition_regime_by_day: Dict[str, str] = {}
    rotation_a_guard_g1_by_ref_day: Dict[str, bool] = {}
    rs_persist_guard_by_ref_day: Dict[str, bool] = {}
    rs_rank_corr_by_day: Dict[str, float] = {}
    _r5_thr = 0.60
    if r5_guard_on:
        try:
            rs_rank_corr_by_day = compute_rs_rank_corr_by_day(
                df_all, lookback=5, rs_window=20, min_names=40
            )
            _r5_thr = resolve_rs_persist_corr_threshold(rs_rank_corr_by_day, settings)
        except Exception:
            rs_rank_corr_by_day = {}
            _r5_thr = float(settings.get("rs_persist_corr_max", 0.60) or 0.60)

    index_vol_by_day: Dict[str, Dict[str, float]] = {}
    idx5_by_day: Dict[str, float] = {}
    if transition_gate_on or g1_guard_on:
        try:
            fetch_start_iv = fetch_start
            index_vol_by_day = _build_index_vol_by_day(
                data_manager, fetch_start_iv, end_date
            )
        except Exception:
            index_vol_by_day = {}
    if g1_guard_on:
        try:
            _ix = _load_kodex200_daily_close(data_manager, fetch_start, end_date)
            if _ix is not None and not _ix.empty and "d" in _ix.columns and "close" in _ix.columns:
                _ix = _ix.drop_duplicates("d").sort_values("d")
                _closes = _ix.set_index("d")["close"].astype(float)
                _days_ix = list(_closes.index)
                for _i, _d in enumerate(_days_ix):
                    if _i < 5:
                        continue
                    try:
                        _c0 = float(_closes.iloc[_i - 5])
                        _c1 = float(_closes.iloc[_i])
                        if _c0 > 0:
                            idx5_by_day[str(_d)] = (_c1 / _c0 - 1.0) * 100.0
                    except Exception:
                        continue
        except Exception:
            idx5_by_day = {}

    # 일자별 Valid 집계(슬라이딩 점수) → share_delta
    transition_stats_by_day: Dict[str, Dict[str, float]] = {}
    _t_va_list: List[int] = []
    _t_vb_list: List[int] = []
    _t_share_list: List[float] = []
    _t_day_list: List[str] = []
    _day_snapshots: List[Tuple[str, pd.DataFrame]] = []

    n_days = len(trading_days_list)
    # 시간순이면 searchsorted 로 창을 자를 수 있다 (일자마다 전체 boolean mask 제거)
    try:
        df_all = df_all.sort_values("time", kind="mergesort").reset_index(drop=True)
        _all_ts = pd.DatetimeIndex(pd.to_datetime(df_all["time"])).asi8
    except Exception:
        _all_ts = None

    # 당기 유니버스를 일자별로 미리 고정 (병렬 시 cache 경쟁 방지 + 반복 조회 제거)
    univ_by_day: Dict[str, Set[str]] = {}
    for _d_pre in trading_days_list:
        try:
            _u = get_period_universe(pd.Timestamp(_d_pre))
            univ_by_day[_d_pre] = {str(c).zfill(6) for c in (_u or [])}
        except Exception:
            univ_by_day[_d_pre] = set()

    # (d_str, d_ts, li, ri) 작업 목록
    _jobs: List[Tuple[str, pd.Timestamp, int, int]] = []
    for _di, d_str in enumerate(trading_days_list):
        try:
            d_ts = pd.Timestamp(d_str).normalize()
            left_ts = d_ts - timedelta(days=lookback_n)
            right_ts = d_ts + timedelta(hours=23, minutes=59, seconds=59)
            if _all_ts is not None:
                _li = int(
                    np.searchsorted(
                        _all_ts, pd.Timestamp(left_ts).value, side="left"
                    )
                )
                _ri = int(
                    np.searchsorted(
                        _all_ts, pd.Timestamp(right_ts).value, side="right"
                    )
                )
            else:
                _m = (df_all["time"] >= left_ts) & (df_all["time"] <= right_ts)
                _idx = np.flatnonzero(_m.to_numpy())
                if len(_idx) == 0:
                    continue
                _li, _ri = int(_idx[0]), int(_idx[-1]) + 1
            if _ri > _li:
                _jobs.append((d_str, d_ts, _li, _ri))
        except Exception:
            continue

    n_workers = _sliding_score_workers(settings if isinstance(settings, dict) else {})
    if n_days > 1:
        try:
            logger_callback(
                f"  [Batch] 슬라이딩 스코어 {len(_jobs)}일 "
                f"(workers={n_workers}, lookback={lookback_n}"
                f"{', process' if n_workers > 1 else ''})"
            )
        except Exception:
            pass

    _results_by_day: Dict[str, Tuple[pd.DataFrame, int, int, float]] = {}

    def _run_job(
        job: Tuple[str, pd.Timestamp, int, int],
    ) -> Optional[Tuple[str, pd.DataFrame, int, int, float]]:
        _ds, _dts, _li, _ri = job
        return _score_one_sliding_day(
            df_all,
            li=_li,
            ri=_ri,
            d_str=_ds,
            d_ts=_dts,
            settings=settings,
            univ_set=univ_by_day.get(_ds, set()),
        )

    def _ingest(got: Optional[Tuple[str, pd.DataFrame, int, int, float]]) -> None:
        if got is None:
            return
        _ds, day_u, _va, _vb, _share = got
        _results_by_day[_ds] = (day_u, _va, _vb, _share)

    _done = 0
    if n_workers <= 1 or len(_jobs) <= 1:
        for job in _jobs:
            _ingest(_run_job(job))
            _done += 1
            if n_days > 40 and _done % 40 == 0:
                try:
                    logger_callback(
                        f"  [Batch] 슬라이딩 스코어 진행 {_done}/{len(_jobs)}일…"
                    )
                except Exception:
                    pass
    else:
        # Track C 등 CPU 바운드 → ProcessPool (스레드는 GIL 때문에 오히려 느림)
        payloads: List[Tuple[str, str, int, int, Tuple[str, ...]]] = []
        for _ds, _dts, _li, _ri in _jobs:
            payloads.append(
                (
                    _ds,
                    _dts.isoformat(),
                    int(_li),
                    int(_ri),
                    tuple(sorted(univ_by_day.get(_ds, set()))),
                )
            )
        try:
            with ProcessPoolExecutor(
                max_workers=n_workers,
                initializer=_sliding_pool_init,
                initargs=(df_all, settings),
            ) as pool:
                for got in pool.map(
                    _sliding_pool_job,
                    payloads,
                    chunksize=max(1, len(payloads) // max(1, n_workers * 4)),
                ):
                    _done += 1
                    _ingest(got)
                    if n_days > 40 and _done % 40 == 0:
                        try:
                            logger_callback(
                                f"  [Batch] 슬라이딩 스코어 진행 "
                                f"{_done}/{len(payloads)}일…"
                            )
                        except Exception:
                            pass
        except Exception as e:
            # 병렬 실패 시 순차로 재시도 (결과 정합 우선)
            try:
                logging.warning(
                    "슬라이딩 프로세스 병렬 실패 → 순차 재시도: %s", e, exc_info=True
                )
            except Exception:
                pass
            try:
                logger_callback(
                    "  [Batch] 슬라이딩 프로세스 병렬 실패 → 순차 재시도"
                )
            except Exception:
                pass
            _results_by_day.clear()
            for job in _jobs:
                _ingest(_run_job(job))

    # 시간순 스냅샷·transition 집계 (병렬 완료 순서와 무관)
    for d_str in trading_days_list:
        packed = _results_by_day.get(d_str)
        if packed is None:
            continue
        day_u, _va, _vb, _share = packed
        _t_day_list.append(d_str)
        _t_va_list.append(int(_va))
        _t_vb_list.append(int(_vb))
        _t_share_list.append(float(_share))
        _day_snapshots.append((d_str, day_u))

    # transition share delta
    if (transition_gate_on or g1_guard_on) and _t_day_list:
        try:
            _tdf = pd.DataFrame(
                {
                    "d": _t_day_list,
                    "va": _t_va_list,
                    "vb": _t_vb_list,
                    "share": _t_share_list,
                }
            )
            _tdf["share_delta5"] = _tdf["share"] - _tdf["share"].shift(5)
            _tdf["share_delta3"] = _tdf["share"] - _tdf["share"].shift(3)
            for _, _tr in _tdf.iterrows():
                transition_stats_by_day[str(_tr["d"])] = {
                    "va": float(_tr["va"]),
                    "vb": float(_tr["vb"]),
                    "share": float(_tr["share"]),
                    "share_delta5": (
                        float(_tr["share_delta5"])
                        if pd.notna(_tr["share_delta5"])
                        else float("nan")
                    ),
                    "share_delta3": (
                        float(_tr["share_delta3"])
                        if pd.notna(_tr["share_delta3"])
                        else float("nan")
                    ),
                }
        except Exception:
            transition_stats_by_day = {}

    # 2-pass: transition 반영 후 Track A/B 스케줄
    # G1 xs 임계: 기간 내 교차섹션 std 분위(프로브와 동일 방식)
    _g1_xs_thr = 3.05
    if g1_guard_on:
        try:
            _xs_pct = float(settings.get("rotation_a_guard_xs_pct", 50.0) or 50.0)
            _xs_vals = [
                float(v)
                for v in xs_ret_std_by_day.values()
                if v is not None and np.isfinite(float(v))
            ]
            if _xs_vals:
                _g1_xs_thr = float(np.nanpercentile(_xs_vals, _xs_pct))
        except Exception:
            _g1_xs_thr = 3.05
    try:
        _g1_a_min = float(settings.get("rotation_a_guard_a_share_min", 0.60) or 0.60)
    except (TypeError, ValueError):
        _g1_a_min = 0.60
    try:
        # idx5_min=0 허용 → `or` 폴백 사용 금지
        _g1_i5_raw = settings.get("rotation_a_guard_idx5_min", -1.0)
        _g1_idx5_min = float(-1.0 if _g1_i5_raw is None else _g1_i5_raw)
    except (TypeError, ValueError):
        _g1_idx5_min = -1.0

    for d_str, day_u in _day_snapshots:
        day_max_a = max_s_a
        day_max_b = max_s_b
        _tl = "OFF"
        _va = 0
        _vb = 0
        if transition_gate_on or g1_guard_on:
            try:
                _ts = transition_stats_by_day.get(d_str, {})
                _va = int(_ts.get("va", 0))
                _vb = int(_ts.get("vb", 0))
                _share = float(_ts.get("share", 0.0))
                _d5 = _ts.get("share_delta5")
                _d3 = _ts.get("share_delta3")
                _xs = xs_ret_std_by_day.get(d_str)
                _iv = index_vol_by_day.get(d_str, {})
                _tl = classify_transition_regime(
                    _va,
                    _vb,
                    _share,
                    _d5,
                    _d3,
                    settings,
                    index_rvol_20=_iv.get("rvol_20"),
                    xs_ret_std=_xs,
                    index_day_ret=_iv.get("ret_pct"),
                )
                transition_regime_by_day[d_str] = _tl
                if transition_gate_on:
                    _ignore_soft = bool(settings.get("transition_ignore_soft", False))
                    if _tl == "HARD":
                        day_max_a, day_max_b = 0, 0
                        transition_hard_days += 1
                    elif _tl == "TRANSITION" and not _ignore_soft:
                        day_max_a, day_max_b = resolve_transition_regime_policy(
                            "TRANSITION", day_max_a, day_max_b, settings
                        )
                        transition_days += 1
            except Exception:
                transition_regime_by_day[d_str] = "OFF"
                _tl = "OFF"

        if g1_guard_on:
            try:
                _ignore_soft_g1 = bool(settings.get("transition_ignore_soft", False))
                _reg_g1 = "OFF" if (_tl == "TRANSITION" and _ignore_soft_g1) else _tl
                rotation_a_guard_g1_by_ref_day[d_str] = bool(
                    is_rotation_a_guard_g1_day(
                        regime=_reg_g1,
                        idx5=idx5_by_day.get(d_str),
                        xs_ret_std=xs_ret_std_by_day.get(d_str),
                        valid_a=_va,
                        valid_b=_vb,
                        xs_threshold=_g1_xs_thr,
                        a_share_min=_g1_a_min,
                        idx5_min=_g1_idx5_min,
                    )
                )
            except Exception:
                rotation_a_guard_g1_by_ref_day[d_str] = False

        if r5_guard_on:
            try:
                _r5 = bool(
                    is_rs_persist_guard_day(
                        rs_rank_corr_by_day.get(d_str),
                        _r5_thr,
                    )
                )
                rs_persist_guard_by_ref_day[d_str] = bool(_r5)
            except Exception:
                rs_persist_guard_by_ref_day[d_str] = False

        if (
            transition_gate_on
            and day_max_a <= 0
            and day_max_b <= 0
            and (
                max_s_c <= 0
                or not _settings_bool_sel(settings, "use_type_c", False)
            )
        ):
            continue

        try:
            day_a = (
                day_u.loc[day_u["Valid_A"].astype(bool)]
                if "Valid_A" in day_u.columns
                else day_u.iloc[:0]
            )
        except Exception:
            day_a = day_u.iloc[:0]
        try:
            day_b = (
                day_u.loc[day_u["Valid_B"].astype(bool)]
                if "Valid_B" in day_u.columns
                else day_u.iloc[:0]
            )
        except Exception:
            day_b = day_u.iloc[:0]
        try:
            day_c = (
                day_u.loc[day_u["Valid_C"].astype(bool)]
                if "Valid_C" in day_u.columns
                else day_u.iloc[:0]
            )
        except Exception:
            day_c = day_u.iloc[:0]

        # Valid_* 는 위에서 항상 계산됨. 감시 스케줄만 켜진 트랙에 편입.
        mon_a = _settings_bool_sel(settings, "use_sel_track_a", True)
        mon_b = _settings_bool_sel(settings, "use_sel_track_b", True)
        mon_c = _settings_bool_sel(settings, "use_sel_track_c", True)

        if mon_a and not day_a.empty and day_max_a > 0:
            schedule_a[d_str] = (
                _df_top_by_score_slot_band(
                    day_a,
                    score_col="Score_A",
                    code_col="code",
                    max_n=day_max_a,
                    settings=settings,
                    min_score_floor=floor_sa,
                )["code"]
                .tolist()
            )
        if mon_b and not day_b.empty and day_max_b > 0:
            schedule_b[d_str] = (
                _df_top_by_score_slot_band(
                    day_b,
                    score_col="Score_B",
                    code_col="code",
                    max_n=day_max_b,
                    settings=settings,
                    min_score_floor=floor_sb,
                )["code"]
                .tolist()
            )
        if mon_c and not day_c.empty and max_s_c > 0:
            picked_c = _df_top_by_score_slot_band(
                day_c,
                score_col="Score_C",
                code_col="code",
                max_n=max_s_c,
                settings=settings,
                min_score_floor=floor_sc,
            )
            schedule_c[d_str] = picked_c["code"].tolist()
            day_meta: Dict[str, Dict[str, float]] = {}
            for _, picked_row in picked_c.iterrows():
                try:
                    picked_code = str(picked_row["code"]).zfill(6)
                    lower = float(picked_row.get("BoxLower_C", np.nan))
                    upper = float(picked_row.get("BoxUpper_C", np.nan))
                    if np.isfinite(lower) and np.isfinite(upper) and upper > lower > 0:
                        day_meta[picked_code] = {
                            "box_lower": lower,
                            "box_upper": upper,
                        }
                except (KeyError, TypeError, ValueError):
                    continue
            if day_meta:
                schedule_c_meta[d_str] = day_meta
        # use_type_c=False 이면 C는 관찰 스케줄만 유지하고 합동 감시에는 넣지 않음
        active_c = (
            schedule_c.get(d_str, [])
            if mon_c and _settings_bool_sel(settings, "use_type_c", False)
            else []
        )
        combined = list(
            dict.fromkeys(
                schedule_a.get(d_str, [])
                + schedule_b.get(d_str, [])
                + active_c
            )
        )
        if combined:
            schedule[d_str] = combined

    if transition_gate_on and (transition_days or transition_hard_days):
        logger_callback(
            f"  [Transition] 레짐 전환: TRANSITION {transition_days}일, HARD {transition_hard_days}일"
        )
    if g1_guard_on:
        try:
            _g1_n = sum(1 for v in rotation_a_guard_g1_by_ref_day.values() if v)
            logger_callback(
                f"  [G1] 로테이션 A 가드 발동 {_g1_n}/{len(rotation_a_guard_g1_by_ref_day)}일 "
                f"(xs_thr={_g1_xs_thr:.2f})"
            )
        except Exception:
            pass
    if r5_guard_on:
        try:
            _r5_n = sum(1 for v in rs_persist_guard_by_ref_day.values() if v)
            logger_callback(
                f"  [R5] RS지속성 A 가드 발동 {_r5_n}/{len(rs_persist_guard_by_ref_day)}일 "
                f"(corr_thr={_r5_thr:.3f})"
            )
        except Exception:
            pass

    a_days = len(schedule_a)
    b_days = len(schedule_b)
    c_days = len(schedule_c)
    if not schedule:
        logger_callback("  [Warning] 조건에 맞는 종목이 한 건도 추출되지 않았습니다. 필터 설정을 확인하세요.")
    else:
        logger_callback(
            f"[Batch] 완료. Track A: {a_days}일, Track B: {b_days}일, "
            f"Track C(관찰): {c_days}일, 매수 유니버스: {len(schedule)}일"
        )
    if include_track_c_schedule:
        return (
            schedule,
            schedule_a,
            schedule_b,
            transition_regime_by_day,
            schedule_c,
            schedule_c_meta,
            rotation_a_guard_g1_by_ref_day,
            rs_persist_guard_by_ref_day,
        )
    return schedule, schedule_a, schedule_b, transition_regime_by_day


def collect_dynamic_schedule_codes(
    settings: dict,
    start_date: str,
    end_date: str,
    data_manager: Any,
    *,
    logger_callback: Optional[Callable[..., Any]] = None,
) -> List[str]:
    """
    동적백테와 동일한 ``prepare_dynamic_universe`` 스케줄에서
    기간 내 Track A/B 선정 종목의 **합집합**을 반환한다.

    전략최적화(SimpleBacktester)가 대표 10종 대신
    실제 감시·선정 풀과 맞추기 위한 용도.
    """
    log = logger_callback or (lambda *a, **k: None)
    try:
        start_s = pd.to_datetime(start_date).strftime("%Y-%m-%d")
        end_s = pd.to_datetime(end_date).strftime("%Y-%m-%d")
    except Exception:
        start_s, end_s = str(start_date)[:10], str(end_date)[:10]

    # DynamicBacktester.run 과 동일: 첫날 ref_day + 선정 워밍업 영업일
    sched_anchor = start_s
    try:
        sd = pd.to_datetime(start_s)
        prev_range_start = (sd - timedelta(days=30)).strftime("%Y-%m-%d")
        prev_range_end = (sd - timedelta(days=1)).strftime("%Y-%m-%d")
        prev_days = data_manager.get_trading_days(prev_range_start, prev_range_end) or []
        if prev_days:
            sched_anchor = pd.to_datetime(prev_days[-1]).strftime("%Y-%m-%d")
    except Exception:
        sched_anchor = start_s

    sched_start = str(sched_anchor)
    try:
        warm_n = int(settings.get("bt_universe_schedule_warmup_trading_days", 20) or 0)
        warm_n = max(0, min(warm_n, 60))
    except Exception:
        warm_n = 20
    if warm_n > 0:
        try:
            anchor_dt = pd.to_datetime(sched_anchor)
            warm_from = (anchor_dt - timedelta(days=max(45, warm_n * 3))).strftime(
                "%Y-%m-%d"
            )
            warm_to = (anchor_dt - timedelta(days=1)).strftime("%Y-%m-%d")
            warm_days = data_manager.get_trading_days(warm_from, warm_to) or []
            if warm_days:
                pick = (
                    warm_days[-warm_n:]
                    if len(warm_days) >= warm_n
                    else list(warm_days)
                )
                sched_start = pd.to_datetime(pick[0]).strftime("%Y-%m-%d")
        except Exception:
            sched_start = str(sched_anchor)

    try:
        log(
            f"[Opt] 선정 스케줄 종목 풀 수집: {sched_start} ~ {end_s} "
            f"(BT기간 {start_s}~{end_s})"
        )
    except Exception:
        pass

    try:
        result = prepare_dynamic_universe(
            settings, sched_start, end_s, data_manager, log
        )
    except Exception as e:
        try:
            log(f"[Opt] 선정 스케줄 수집 실패: {e}")
        except Exception:
            pass
        return []

    schedule: Dict[str, Any] = {}
    schedule_a: Dict[str, Any] = {}
    schedule_b: Dict[str, Any] = {}
    try:
        if isinstance(result, tuple) and len(result) >= 3:
            schedule = dict(result[0] or {})
            schedule_a = dict(result[1] or {})
            schedule_b = dict(result[2] or {})
        elif isinstance(result, dict):
            schedule = dict(result)
            schedule_a = schedule
    except Exception:
        return []

    codes: set[str] = set()
    for src in (schedule, schedule_a, schedule_b):
        try:
            for day_codes in (src or {}).values():
                if not day_codes:
                    continue
                for c in day_codes:
                    nc = str(c).strip().zfill(6)
                    if len(nc) == 6 and nc.isdigit():
                        codes.add(nc)
        except Exception:
            continue

    out = sorted(codes)
    try:
        log(f"[Opt] 선정 스케줄 합집합: {len(out)}종목")
    except Exception:
        pass
    return out


def diagnose_dynamic_universe_selection_by_trading_day(
    settings: dict,
    start_date: Union[str, datetime],
    end_date: Union[str, datetime],
    data_manager: Any,
) -> pd.DataFrame:
    """
    `prepare_dynamic_universe`와 동일한 1d 로드·스코어에 대해,
    **영업일(``get_trading_days``)별**로 (1) KODEX 유니버스 행 수, (2) ``Valid_B``/``Valid_A`` 건수,
    (3) Track B 부분조건(Dbg_*)을 만족한 행 수(일당 합)를 집계한다.
    4/10 이후 `schedule`에 없을 때, **'일봉 행 없음'** vs **'Valid_B 전부 False'** 를 구분하는 데 쓴다.
    """
    start_s = pd.to_datetime(start_date).strftime("%Y-%m-%d")
    end_s = pd.to_datetime(end_date).strftime("%Y-%m-%d")
    trading_days: List[str] = list(data_manager.get_trading_days(start_s, end_s) or [])

    def _log(_: str) -> None:
        pass

    all_days = pd.date_range(start=start_s, end=end_s, freq="B")
    union_cache: dict[str, list[str]] = {}
    union_tickers: set[str] = set()
    from core.marcap_universe import (
        get_kospi_top_n_for_refresh,
        normalize_refresh_mode,
        universe_period_key,
    )

    refresh_mode = normalize_refresh_mode(settings.get("kospi_universe_refresh"))
    use_period_univ = refresh_mode != "monthly"

    def get_period_universe(dt: Any) -> list[str]:
        d_ts = pd.Timestamp(dt).normalize()
        cache_key = universe_period_key(d_ts, refresh_mode)
        if cache_key in union_cache:
            return union_cache[cache_key]
        try:
            try:
                idx_raw = str(settings.get("universe_index_choice", "KODEX200") or "KODEX200").strip().upper()
            except Exception:
                idx_raw = "KODEX200"
            idx_choice = "KOSPI200" if idx_raw in ("KOSPI", "KOSPI200") else "KODEX200"
            top_n = 200
            try:
                top_n = int(settings.get("kospi_top_n", 200) or 200)
            except Exception:
                top_n = 200
            if idx_choice == "KOSPI200":
                tickers, _ = get_kospi_top_n_for_refresh(
                    d_ts, top_n, refresh_mode, logger_callback=None
                )
            else:
                tickers = load_kodex200_universe_codes(logger_callback=None)
        except Exception:
            tickers = []
        if tickers:
            union_cache[cache_key] = tickers
        return tickers

    try:
        if refresh_mode == "daily":
            for d in all_days:
                union_tickers.update(get_period_universe(d))
        elif use_period_univ:
            last_key = ""
            for d in all_days:
                curr_key = universe_period_key(d, refresh_mode)
                if curr_key != last_key or not union_tickers:
                    union_tickers.update(get_period_universe(d))
                    last_key = curr_key
        else:
            last_m = ""
            for d in all_days:
                curr_m = d.strftime("%Y%m")
                if curr_m != last_m or not union_tickers:
                    union_tickers.update(get_period_universe(d))
                    last_m = curr_m
    except Exception as e:
        try:
            logging.warning("diagnose union: %s", e, exc_info=True)
        except Exception:
            pass
        union_tickers = set()

    if not union_tickers:
        try:
            _raw = str(settings.get("universe_index_choice", "KODEX200") or "KODEX200").strip().upper()
        except Exception:
            _raw = "KODEX200"
        _idx = "KOSPI200" if _raw in ("KOSPI", "KOSPI200") else "KODEX200"
        files = ["kodex200.csv", "kodex_200.csv", "kospi_top_500.csv"]
        if _idx == "KOSPI200":
            files = ["kospi_universe.csv", "kospi_top_500.csv"]
        for f_name in files:
                if not os.path.isfile(f_name):
                    continue
                try:
                    df_k = pd.read_csv(f_name, dtype=str)
                    c_col = "Code" if "Code" in df_k.columns else (
                        "종목코드" if "종목코드" in df_k.columns else None
                    )
                    if c_col:
                        union_tickers = set(df_k[c_col].apply(lambda x: str(x).zfill(6)).tolist())
                        break
                except Exception:
                    pass

    if not union_tickers:
        return pd.DataFrame(
            columns=[
                "date",
                "kodex_rows_해당일",
                "Valid_A_합",
                "Valid_B_합",
                "B1_avg5",
                "B2_정배열",
                "B3_rsi",
                "B4_분출B",
                "C5_osc",
                "C_ma60",
                "nonempty_schedule",
                "비고",
            ]
        )

    all_codes = list(union_tickers)
    fetch_start = _universe_batch_indicator_left_clip(end_s, settings)
    ph = ",".join(["?"] * len(all_codes))
    q = f"""
        SELECT code, datetime as time, open, high, low, close, volume
        FROM bars_1d
        WHERE code IN ({ph}) AND datetime BETWEEN ? AND ?
        ORDER BY code, datetime
    """
    try:
        with data_manager._get_connection() as conn:
            df_all = pd.read_sql_query(q, conn, params=all_codes + [fetch_start, end_s])
    except Exception as e:
        try:
            logging.error("diagnose: bars_1d 조회 실패: %s", e, exc_info=True)
        except Exception:
            pass
        return pd.DataFrame()

    if df_all is None or df_all.empty:
        rows_err = []
        for d in trading_days:
            rows_err.append(
                {
                    "date": d,
                    "kodex_rows_해당일": 0,
                    "Valid_A_합": 0,
                    "Valid_B_합": 0,
                    "B1_avg5": 0,
                    "B2_정배열": 0,
                    "B3_rsi": 0,
                    "B4_분출B": 0,
                    "C5_osc": 0,
                    "C_ma60": 0,
                    "nonempty_schedule": 0,
                    "비고": "bars_1d 비어 있음(유니버스 구간)",
                }
            )
        return pd.DataFrame(rows_err)

    try:
        df_all["time"] = pd.to_datetime(df_all["time"])
        if "code" in df_all.columns:
            df_all["code"] = df_all["code"].astype(str).str.zfill(6)
    except Exception:
        pass

    _score_dynamic_universe_daily_ohlcv(df_all, settings, with_b_debug=True)
    try:
        _resync_batch_valid_to_period_universe(df_all, get_period_universe, settings)
    except Exception as e:
        try:
            logging.warning("diagnose period-univ 재동기화 생략: %s", e, exc_info=True)
        except Exception:
            pass
    df_sim = df_all[df_all["time"] >= pd.to_datetime(start_s)].copy()

    dbg_cols = [
        "Dbg_B1_avg5_ge_selB",
        "Dbg_B2_trend_mas",
        "Dbg_B3_rsi_range",
        "Dbg_B4_volburst1",
        "Dbg_C5_osc_pos",
        "Dbg_C_ma60_ok",
    ]
    for c in dbg_cols:
        if c not in df_sim.columns:
            df_sim[c] = False

    # ``prepare`` 와 동일: 일자별 top(max_slots) 스케줄 키 존재 여부(중복 `prepare` 호출 없음)
    schedule: dict[str, list[str]] = {}
    try:
        max_s_a = int(settings.get("max_slots_a", 20))
        max_s_b = int(settings.get("max_slots_b", 10))
        floor_sa = max(0.0, float(settings.get("sel_track_min_score_a", 0.0) or 0.0))
        floor_sb = max(0.0, float(settings.get("sel_track_min_score_b", 0.0) or 0.0))
        trading_days_list2 = (
            df_sim["time"].dt.strftime("%Y-%m-%d").drop_duplicates().sort_values().tolist()
        )
        for d_str in trading_days_list2:
            try:
                universe2 = get_period_universe(pd.to_datetime(d_str))
            except Exception:
                universe2 = all_codes
            dmask = (df_sim["time"] == pd.to_datetime(d_str)) & (
                df_sim["code"].astype(str).str.zfill(6).isin(
                    {str(x).zfill(6) for x in universe2}
                )
            )
            sa = (
                _df_top_by_score_slot_band(
                    df_sim.loc[dmask & df_sim["Valid_A"]],
                    score_col="Score_A",
                    code_col="code",
                    max_n=max_s_a,
                    settings=settings,
                    min_score_floor=floor_sa,
                )["code"]
                .tolist()
            )
            sb = (
                _df_top_by_score_slot_band(
                    df_sim.loc[dmask & df_sim["Valid_B"]],
                    score_col="Score_B",
                    code_col="code",
                    max_n=max_s_b,
                    settings=settings,
                    min_score_floor=floor_sb,
                )["code"]
                .tolist()
            )
            comb = list(dict.fromkeys([str(x) for x in (sa or [])] + [str(x) for x in (sb or [])]))
            if comb:
                schedule[d_str] = comb
    except Exception as e:
        try:
            logging.warning("diagnose schedule build: %s", e, exc_info=True)
        except Exception:
            pass
        schedule = {}

    out_rows: list[dict[str, Any]] = []
    for d in trading_days:
        try:
            sub = df_sim[df_sim["time"].dt.strftime("%Y-%m-%d") == d]
        except Exception:
            sub = pd.DataFrame()
        n = int(len(sub))
        try:
            n_va = int(sub["Valid_A"].sum()) if n and "Valid_A" in sub.columns else 0
        except Exception:
            n_va = 0
        try:
            n_vb = int(sub["Valid_B"].sum()) if n and "Valid_B" in sub.columns else 0
        except Exception:
            n_vb = 0
        bparts = {c: 0 for c in dbg_cols}
        if n > 0:
            for c in dbg_cols:
                try:
                    bparts[c] = int(sub[c].astype(bool).sum())
                except Exception:
                    bparts[c] = 0
        in_sched = 1 if bool(schedule.get(d)) else 0
        note = ""
        if n == 0:
            note = "해당 영업일·KODEX·bars_1d에 (code,일) 행 없음"
        elif n_vb == 0 and n > 0:
            note = "일봉은 있으나 Valid_B=0(부분조건 열 참고)"
        else:
            note = "Valid_B>0 (스케줄 상한은 max_slots_b 등 별도)"
        rec: dict[str, Any] = {
            "date": d,
            "kodex_rows_해당일": n,
            "Valid_A_합": n_va,
            "Valid_B_합": n_vb,
            "B1_avg5": bparts[dbg_cols[0]],
            "B2_정배열": bparts[dbg_cols[1]],
            "B3_rsi": bparts[dbg_cols[2]],
            "B4_분출B": bparts[dbg_cols[3]],
            "C5_osc": bparts[dbg_cols[4]],
            "C_ma60": bparts[dbg_cols[5]],
            "nonempty_schedule": in_sched,
            "비고": note,
        }
        out_rows.append(rec)

    try:
        return pd.DataFrame(out_rows)
    except Exception:
        return pd.DataFrame()

