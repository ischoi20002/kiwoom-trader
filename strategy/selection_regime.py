# -*- coding: utf-8 -*-
"""시장 breadth·전이 레짐·G1/R5 게이트.

``strategy_selector`` 에서 분리. re-export 로 호환.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import FinanceDataReader as fdr
import numpy as np
import pandas as pd

from strategy.selection_dynamic_score import (
    _score_dynamic_universe_daily_ohlcv,
    _selection_daily_lookback_calendar_days,
)
from strategy.selection_scoring import _settings_bool_sel
from strategy.universe_codes import load_kodex200_universe_codes, resolve_universe_codes


def compute_market_breadth(
    rows_or_df: Union[List[dict], pd.DataFrame],
) -> Dict[str, float]:
    """
    유니버스 스냅샷(선정 스캔 결과 행 목록 또는 일자별 DataFrame)에서 시장 브레드스를 계산한다.

    외부 지수 데이터 없이, 선정 단계에서 이미 계산된 종가·MA20·MA60만으로 시장 폭을 측정한다.

    입력:
        - list[dict]: 실시간 선정 경로. 각 행은 ``Price``(또는 ``close``)·``MA20``·``MA60`` 보유.
        - pd.DataFrame: 백테스트 일별 패널. ``close``·``MA20``·``MA60`` 컬럼 보유.

    반환:
        - ``ma20_above``: 종가 ≥ MA20 인 종목 비율(0~1). 표본 없으면 ``float('nan')``.
        - ``ma20_gt_ma60``: MA20 > MA60 비율(0~1).
        - ``n``: 유효 표본 종목 수.
    """
    nan = float("nan")
    result: Dict[str, float] = {"ma20_above": nan, "ma20_gt_ma60": nan, "n": 0.0}
    try:
        if isinstance(rows_or_df, pd.DataFrame):
            df = rows_or_df
            if df is None or df.empty:
                return result
            close_col = (
                "close"
                if "close" in df.columns
                else ("Price" if "Price" in df.columns else None)
            )
            if close_col is None or "MA20" not in df.columns:
                return result
            close = pd.to_numeric(df[close_col], errors="coerce")
            ma20 = pd.to_numeric(df["MA20"], errors="coerce")
            ma60 = (
                pd.to_numeric(df["MA60"], errors="coerce")
                if "MA60" in df.columns
                else pd.Series(np.nan, index=df.index)
            )
            valid = close.notna() & ma20.notna() & (ma20 > 0)
            n = int(valid.sum())
            if n <= 0:
                return result
            above = float((close[valid] >= ma20[valid]).mean())
            v60 = valid & ma60.notna() & (ma60 > 0)
            gt60 = float((ma20[v60] > ma60[v60]).mean()) if int(v60.sum()) > 0 else nan
            return {"ma20_above": above, "ma20_gt_ma60": gt60, "n": float(n)}

        # list[dict] 경로
        rows = rows_or_df or []
        above_cnt = 0
        gt60_cnt = 0
        gt60_den = 0
        n = 0
        for r in rows:
            try:
                price = r.get("Price", r.get("close", None))
                ma20 = r.get("MA20", None)
                ma60 = r.get("MA60", None)
                if price is None or ma20 is None:
                    continue
                price_f = float(price)
                ma20_f = float(ma20)
                if not (np.isfinite(price_f) and np.isfinite(ma20_f)) or ma20_f <= 0:
                    continue
                n += 1
                if price_f >= ma20_f:
                    above_cnt += 1
                if ma60 is not None:
                    ma60_f = float(ma60)
                    if np.isfinite(ma60_f) and ma60_f > 0:
                        gt60_den += 1
                        if ma20_f > ma60_f:
                            gt60_cnt += 1
            except (TypeError, ValueError):
                continue
        if n <= 0:
            return result
        return {
            "ma20_above": above_cnt / n,
            "ma20_gt_ma60": (gt60_cnt / gt60_den) if gt60_den > 0 else nan,
            "n": float(n),
        }
    except Exception:
        return result


def _clamp_nonneg_int(value: Any, default: int) -> int:
    """설정값을 0 이상 정수로 안전 변환."""
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return max(0, int(default))


def is_chop_regime_day(
    valid_a: int,
    valid_b: int,
    settings: Optional[dict] = None,
) -> bool:
    """
    A 과밀·B 고갈 횡보(chop) 레짐 여부.

    조건 (OR):
      1) valid_a >= chop_valid_a_min AND valid_b <= chop_valid_b_max
      2) valid_b_share <= chop_valid_b_share_max_pct AND valid_a >= chop_valid_a_floor

    선정 스케줄 생성 시 A 슬롯만 축소하는 데 사용한다.
    """
    s = settings or {}
    try:
        va = max(0, int(valid_a))
        vb = max(0, int(valid_b))
        a_min = max(0, int(float(s.get("chop_valid_a_min", 15) or 15)))
        b_max = max(0, int(float(s.get("chop_valid_b_max", 3) or 3)))
        share_max = float(s.get("chop_valid_b_share_max_pct", 15.0) or 15.0)
        a_floor = max(0, int(float(s.get("chop_valid_a_floor", 10) or 10)))
        total = va + vb
        share = (vb / total * 100.0) if total > 0 else 100.0
        if va >= a_min and vb <= b_max:
            return True
        if va >= a_floor and share <= share_max:
            return True
        return False
    except Exception:
        return False


def _fetch_daily_ohlcv_batch_fdr(
    data_manager: Any,
    codes: List[str],
    fetch_start: str,
    end_date: str,
    logger_callback: Optional[Callable[..., None]] = None,
    *,
    max_workers: int = 12,
) -> pd.DataFrame:
    """
    레짐·전환 게이트용: 유니버스 종목 일봉을 FDR에서 병렬 조회한다.
    ``bars_1d`` DB 동기화 없이도 동작한다.
    """
    empty = pd.DataFrame(
        columns=["code", "time", "open", "high", "low", "close", "volume"]
    )
    log = logger_callback or (lambda *_a, **_k: None)
    if data_manager is None or not codes:
        return empty
    fetch_fn = getattr(data_manager, "fetch_fdr_daily_dataframe", None)
    if not callable(fetch_fn):
        try:
            log("⚠️ [Transition] data_manager.fetch_fdr_daily_dataframe 없음")
        except Exception:
            pass
        return empty

    norm_codes = [str(c).strip().zfill(6) for c in codes if str(c).strip()]
    if not norm_codes:
        return empty

    def _one(code: str) -> Optional[pd.DataFrame]:
        try:
            raw = fetch_fn(code, fetch_start, end_date)
            if raw is None or raw.empty:
                return None
            part = raw.copy()
            part["code"] = code
            part["time"] = pd.to_datetime(part["datetime"], errors="coerce")
            for col in ("open", "high", "low", "close", "volume"):
                if col not in part.columns:
                    if col == "volume":
                        part[col] = 0
                    else:
                        part[col] = part.get("close", np.nan)
                part[col] = pd.to_numeric(part[col], errors="coerce")
            part = part.dropna(subset=["time", "close"])
            if part.empty:
                return None
            return part[["code", "time", "open", "high", "low", "close", "volume"]]
        except Exception:
            return None

    parts: List[pd.DataFrame] = []
    workers = max(1, min(int(max_workers or 12), 24))
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for df_part in pool.map(_one, norm_codes):
                if df_part is not None and not df_part.empty:
                    parts.append(df_part)
    except Exception as ex:
        try:
            log(f"⚠️ [Transition] FDR 일괄 조회 실패: {ex}")
        except Exception:
            pass
        return empty

    if not parts:
        try:
            log(
                f"⚠️ [Transition] FDR 일봉 0건 ({fetch_start}~{end_date}, "
                f"요청 {len(norm_codes)}종목)"
            )
        except Exception:
            pass
        return empty

    out = pd.concat(parts, ignore_index=True)
    out = out.sort_values(["code", "time"]).reset_index(drop=True)
    try:
        log(
            f"[Transition] FDR 일봉 {len(out)}행 / "
            f"{out['code'].nunique()}종목 ({fetch_start}~{end_date})"
        )
    except Exception:
        pass
    return out


def _fdr_index_daily_close_df(
    data_manager: Any,
    index_code: str,
    fetch_start: str,
    end_date: str,
) -> pd.DataFrame:
    """지수·ETF 일봉 close 시계열 (FDR 우선)."""
    empty = pd.DataFrame(columns=["time", "close"])
    if data_manager is None:
        return empty
    fetch_fn = getattr(data_manager, "fetch_fdr_daily_dataframe", None)
    if callable(fetch_fn):
        try:
            raw = fetch_fn(index_code, fetch_start, end_date)
            if raw is not None and not raw.empty:
                tmp = raw.copy()
                tmp["time"] = pd.to_datetime(tmp["datetime"], errors="coerce")
                tmp["close"] = pd.to_numeric(tmp["close"], errors="coerce")
                tmp = tmp.dropna(subset=["time", "close"])
                if not tmp.empty:
                    return tmp[["time", "close"]].copy()
        except Exception:
            pass
    try:
        df = data_manager.get_daily_bars(index_code, fetch_start, end_date)
        if df is not None and not df.empty:
            tmp = df.copy()
            if "Close" in tmp.columns:
                tmp.rename(columns={"Close": "close"}, inplace=True)
            tmp["time"] = pd.to_datetime(tmp["time"], errors="coerce")
            tmp["close"] = pd.to_numeric(tmp["close"], errors="coerce")
            tmp = tmp.dropna(subset=["time", "close"])
            if not tmp.empty:
                return tmp[["time", "close"]].copy()
    except Exception:
        pass
    return empty


def _build_index_vol_by_day(
    data_manager: Any,
    fetch_start: str,
    end_date: str,
) -> Dict[str, Dict[str, float]]:
    """
    KODEX200(069500) 일봉 기준 일별 맵 — FDR 우선.

    - ``rvol_5`` / ``rvol_20``: 연환산 변동성(%)
    - ``ret_pct``: 일수익률(%) — HARD 상승일 억제 등에 사용
    """
    out: Dict[str, Dict[str, float]] = {}
    try:
        df = _fdr_index_daily_close_df(data_manager, "069500", fetch_start, end_date)
        if df is None or df.empty:
            return out
        tmp = df.copy()
        tmp["d"] = pd.to_datetime(tmp["time"], errors="coerce").dt.normalize()
        close = pd.to_numeric(tmp["close"], errors="coerce")
        ret = close.pct_change()
        tmp["ret_pct"] = ret * 100.0
        tmp["rvol_20"] = ret.rolling(20).std() * np.sqrt(252.0) * 100.0
        tmp["rvol_5"] = ret.rolling(5).std() * np.sqrt(252.0) * 100.0
        for _, row in tmp.iterrows():
            d_raw = row.get("d")
            if pd.isna(d_raw):
                continue
            d_str = pd.Timestamp(d_raw).strftime("%Y-%m-%d")
            entry: Dict[str, float] = {}
            for key in ("rvol_20", "rvol_5", "ret_pct"):
                val = row.get(key)
                if val is not None and np.isfinite(float(val)):
                    entry[key] = float(val)
            if entry:
                out[d_str] = entry
    except Exception:
        return out
    return out


def _sliding_valid_counts_by_day(
    df_ohlcv: pd.DataFrame,
    settings: dict,
    days: List[str],
    lookback_days: int,
) -> Dict[str, Dict[str, float]]:
    """
    평가일 D마다 ``[D-N, D]`` 창으로 일봉 스코어를 다시 계산해 Valid_A/B 개수·B비중을 구한다.

    RSI·MACD 가 ``ewm(adjust=False)`` 라 창 시작일에 따라 Valid 판정이 달라지므로,
    ``prepare_dynamic_universe`` (백테) 와 같은 슬라이딩 창을 써야 레짐이 일치한다.

    반환: ``{YYYY-MM-DD: {"va", "vb", "share"}}`` (계산 실패 일자는 제외)
    """
    out: Dict[str, Dict[str, float]] = {}
    if df_ohlcv is None or df_ohlcv.empty or not days:
        return out
    try:
        base = df_ohlcv.sort_values("time", kind="mergesort").reset_index(drop=True)
        ts_arr = pd.DatetimeIndex(pd.to_datetime(base["time"])).asi8
    except Exception:
        return out
    lb = max(1, int(lookback_days))
    for d_str in days:
        try:
            d_ts = pd.Timestamp(d_str).normalize()
            left_ts = d_ts - timedelta(days=lb)
            right_ts = d_ts + timedelta(hours=23, minutes=59, seconds=59)
            li = int(np.searchsorted(ts_arr, left_ts.value, side="left"))
            ri = int(np.searchsorted(ts_arr, right_ts.value, side="right"))
            if ri <= li:
                continue
            win = base.iloc[li:ri].copy()
            win.sort_values(["code", "time"], kind="mergesort", inplace=True)
            win.reset_index(drop=True, inplace=True)
            _score_dynamic_universe_daily_ohlcv(win, settings, with_b_debug=False)
            day = win[win["time"].dt.normalize() == d_ts]
            if day.empty:
                continue
            va = int(day["Valid_A"].astype(bool).sum()) if "Valid_A" in day.columns else 0
            vb = int(day["Valid_B"].astype(bool).sum()) if "Valid_B" in day.columns else 0
            tot = va + vb
            out[d_str] = {
                "va": float(va),
                "vb": float(vb),
                "share": (vb / tot * 100.0) if tot > 0 else 0.0,
            }
        except Exception:
            continue
    return out


def _compute_transition_regime_map_fdr(
    settings: dict,
    data_manager: Any,
    start_date: str,
    end_date: str,
    logger_callback: Optional[Callable[..., None]] = None,
    *,
    eval_last_days: Optional[int] = None,
    detail_out: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, str]:
    """
    FDR 일봉 기반 일별 transition 레짐 맵.
    ``prepare_dynamic_universe`` 의 레짐 분류와 동일 로직, DB ``bars_1d`` 미사용.

    Valid_A/B 는 평가일마다 ``[D-N, D]`` (N=``bt_universe_indicator_lookback_days``)
    슬라이딩 창으로 재계산한다 (백테·실시간 헤더와 동일).
    ``eval_last_days`` 지정 시 마지막 N영업일만 평가한다 (share delta 용 5일은 자동 추가).
    ``detail_out`` 에 dict 를 넘기면 일별 G1·R5 판정(``_compute_g1_r5_by_day``)을 채운다.
    """
    log = logger_callback or (lambda *_a, **_k: None)
    regime_map: Dict[str, str] = {}
    try:
        start_s = pd.to_datetime(start_date).strftime("%Y-%m-%d")
        end_s = pd.to_datetime(end_date).strftime("%Y-%m-%d")
        codes = resolve_universe_codes(settings, logger_callback=log)
        if not codes:
            return regime_map

        lookback_n = _selection_daily_lookback_calendar_days(settings)
        fetch_start = (pd.to_datetime(start_s) - timedelta(days=lookback_n)).strftime(
            "%Y-%m-%d"
        )
        df_all = _fetch_daily_ohlcv_batch_fdr(
            data_manager, codes, fetch_start, end_s, log
        )
        if df_all is None or df_all.empty:
            return regime_map

        df_sim = df_all[df_all["time"] >= pd.to_datetime(start_s)].copy()
        if df_sim.empty:
            return regime_map

        trading_days_list = sorted(df_sim["time"].dt.strftime("%Y-%m-%d").unique().tolist())
        if eval_last_days is not None:
            try:
                keep = max(1, int(eval_last_days)) + 5
                trading_days_list = trading_days_list[-keep:]
                df_sim = df_sim[
                    df_sim["time"] >= pd.to_datetime(trading_days_list[0])
                ].copy()
            except Exception:
                pass

        counts_by_day = _sliding_valid_counts_by_day(
            df_all, settings, trading_days_list, lookback_n
        )

        # 종가 수익률 분산은 창과 무관 → 전체 로드분으로 계산 (첫 평가일 NaN 방지)
        xs_ret_std_by_day = _xs_ret_std_by_day(df_all)

        fetch_start_iv = (
            pd.to_datetime(trading_days_list[0]) - timedelta(days=60)
        ).strftime("%Y-%m-%d")
        index_vol_by_day = _build_index_vol_by_day(data_manager, fetch_start_iv, end_s)

        transition_stats_by_day: Dict[str, Dict[str, float]] = {}
        try:
            _t_rows: List[Dict[str, Any]] = []
            for _d_str in trading_days_list:
                _c = counts_by_day.get(_d_str)
                if _c is None:
                    continue
                _t_rows.append(
                    {
                        "d": _d_str,
                        "va": int(_c["va"]),
                        "vb": int(_c["vb"]),
                        "share": float(_c["share"]),
                    }
                )
            _tdf = pd.DataFrame(_t_rows)
            if not _tdf.empty:
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

        for d_str in trading_days_list:
            try:
                _ts = transition_stats_by_day.get(d_str, {})
                _va = int(_ts.get("va", 0))
                _vb = int(_ts.get("vb", 0))
                _share = float(_ts.get("share", 0.0))
                _d5 = _ts.get("share_delta5")
                _d3 = _ts.get("share_delta3")
                _xs = xs_ret_std_by_day.get(d_str)
                _iv = index_vol_by_day.get(d_str, {})
                regime_map[d_str] = classify_transition_regime(
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
            except Exception:
                regime_map[d_str] = "OFF"

        if detail_out is not None:
            try:
                detail_out.update(
                    _compute_g1_r5_by_day(
                        settings,
                        data_manager,
                        df_all,
                        trading_days_list,
                        regime_map,
                        counts_by_day,
                        xs_ret_std_by_day,
                    )
                )
            except Exception as ex:
                try:
                    log(f"⚠️ [Transition] G1/R5 이력 계산 실패: {ex}")
                except Exception:
                    pass
    except Exception as ex:
        try:
            log(f"⚠️ [Transition] FDR 레짐 맵 실패: {ex}")
        except Exception:
            pass
    return regime_map


def classify_transition_regime(
    valid_a: int,
    valid_b: int,
    vb_share: float,
    vb_share_delta5: Optional[float],
    vb_share_delta3: Optional[float],
    settings: Optional[dict] = None,
    index_rvol_20: Optional[float] = None,
    xs_ret_std: Optional[float] = None,
    index_day_ret: Optional[float] = None,
) -> str:
    """
    B-share 급락(레짐 전환) + 변동성 확인.

    반환: ``OFF`` / ``TRANSITION`` / ``HARD``
      - TRANSITION/HARD: 신규 S1 차단 + S2 중단 (런타임)
      - ``hard_suppress_on_index_up``: 지수 일수익률(%)이 임계 이상이면
        chop→HARD를 건너뜀 (폭등일 A과밀 오발동 완화)
      - ``hard_suppress_to_off_only``: 억제 시 TRANSITION 폴백 없이 즉시 ``OFF``
    """
    s = settings or {}
    try:
        chop = is_chop_regime_day(valid_a, valid_b, settings)
        if chop:
            suppress = bool(s.get("hard_suppress_on_index_up", False))
            try:
                thr = float(s.get("hard_suppress_index_up_pct", 2.0) or 2.0)
            except (TypeError, ValueError):
                thr = 2.0
            day_ret: Optional[float] = None
            try:
                if index_day_ret is not None and np.isfinite(float(index_day_ret)):
                    day_ret = float(index_day_ret)
            except Exception:
                day_ret = None
            suppressed = bool(
                suppress and day_ret is not None and day_ret >= thr
            )
            if not suppressed:
                return "HARD"
            # 강한 상승일로 HARD 억제 — OFF만 허용하면 TRANSITION 폴백 금지
            if bool(s.get("hard_suppress_to_off_only", False)):
                return "OFF"

        va = max(0, int(valid_a))
        vb = max(0, int(valid_b))
        share = float(vb_share)
        d5 = float(vb_share_delta5) if vb_share_delta5 is not None and np.isfinite(vb_share_delta5) else None
        d3 = float(vb_share_delta3) if vb_share_delta3 is not None and np.isfinite(vb_share_delta3) else None

        delta5_thr = float(s.get("transition_share_delta5_max", -15.0) or -15.0)
        delta3_thr = float(s.get("transition_share_delta3_max", -10.0) or -10.0)
        prior_min = float(s.get("transition_prior_share_min", 45.0) or 45.0)
        rvol_min = float(s.get("transition_rvol_min", 35.0) or 35.0)
        xs_min = float(s.get("transition_xs_ret_std_min", 3.5) or 3.5)

        r20 = (
            float(index_rvol_20)
            if index_rvol_20 is not None and np.isfinite(float(index_rvol_20))
            else None
        )
        xs = (
            float(xs_ret_std)
            if xs_ret_std is not None and np.isfinite(float(xs_ret_std))
            else None
        )

        def _vol_ok() -> bool:
            if r20 is not None and r20 >= rvol_min:
                return True
            if xs is not None and xs >= xs_min:
                return True
            # 지수 rvol 결측(2026-04~) 구간: B-share 급락만으로 전환 허용
            if r20 is None and d5 is not None and d5 <= delta5_thr - 5.0:
                return True
            return False

        if d5 is not None and d5 <= delta5_thr:
            prior = share - d5
            if prior >= prior_min and _vol_ok():
                return "TRANSITION"

        if d3 is not None and d3 <= delta3_thr:
            prior3 = share - d3
            if prior3 >= prior_min and _vol_ok():
                return "TRANSITION"

        # 지수 없는 구간 fallback: A 과밀 + B 고갈
        if va >= 15 and vb <= 4 and share <= 20.0:
            return "TRANSITION"
        return "OFF"
    except Exception:
        return "OFF"


def is_rotation_a_guard_g1_day(
    *,
    regime: str,
    idx5: Optional[float],
    xs_ret_std: Optional[float],
    valid_a: int,
    valid_b: int,
    xs_threshold: float,
    a_share_min: float = 0.60,
    idx5_min: float = -1.0,
) -> bool:
    """
    G1 로테이션 A 가드 (전일 통계 기준).

    OFF · 지수 5일 수익률≥idx5_min · 종목분산 xs≥임계 · A비중≥a_share_min
    → 그날 신규 Type A S1 억제에 사용.
    """
    try:
        if str(regime or "OFF").strip().upper() != "OFF":
            return False
        if idx5 is None or not np.isfinite(float(idx5)):
            return False
        if float(idx5) < float(idx5_min):
            return False
        if xs_ret_std is None or not np.isfinite(float(xs_ret_std)):
            return False
        if float(xs_ret_std) < float(xs_threshold):
            return False
        va = max(0, int(valid_a))
        vb = max(0, int(valid_b))
        tot = va + vb
        if tot <= 0:
            return False
        if (va / float(tot)) < float(a_share_min):
            return False
        return True
    except Exception:
        return False


def _spearman_corr_pair(a: pd.Series, b: pd.Series, *, min_n: int = 40) -> Optional[float]:
    """scipy 없이 Spearman 상관 (순위→피어슨)."""
    try:
        both = pd.concat([a, b], axis=1).dropna()
        if len(both) < int(min_n):
            return None
        ra = both.iloc[:, 0].rank()
        rb = both.iloc[:, 1].rank()
        v = float(ra.corr(rb, method="pearson"))
        return v if np.isfinite(v) else None
    except Exception:
        return None


def compute_rs_rank_corr_by_day(
    df_all: pd.DataFrame,
    *,
    lookback: int = 5,
    rs_window: int = 20,
    min_names: int = 40,
) -> Dict[str, float]:
    """
    일별 RS 순위 지속성: ``rs_window``일 상대강도 순위의 ``lookback``영업일 전 대비 Spearman.

    반환 키=YYYY-MM-DD, 값=상관(낮을수록 리더십 교체·순환매).
    ``df_all`` 필요 컬럼: ``code``, ``time``, ``close``.
    """
    out: Dict[str, float] = {}
    try:
        if df_all is None or df_all.empty:
            return out
        need = {"code", "time", "close"}
        if not need.issubset(set(df_all.columns)):
            return out
        d = df_all[["code", "time", "close"]].copy()
        d["time"] = pd.to_datetime(d["time"], errors="coerce")
        d["close"] = pd.to_numeric(d["close"], errors="coerce")
        d["code"] = d["code"].astype(str).str.zfill(6)
        d = d.dropna(subset=["time", "close"])
        if d.empty:
            return out
        d["d"] = d["time"].dt.strftime("%Y-%m-%d")
        px = d.pivot_table(index="d", columns="code", values="close", aggfunc="last")
        px = px.sort_index()
        px.index = px.index.astype(str)
        lb = max(1, int(lookback))
        rw = max(2, int(rs_window))
        rs = px / px.shift(rw) - 1.0
        days = list(rs.index)
        for i, day in enumerate(days):
            if i < lb:
                continue
            try:
                corr = _spearman_corr_pair(
                    rs.iloc[i - lb], rs.iloc[i], min_n=int(min_names)
                )
                if corr is not None:
                    out[str(day)] = float(corr)
            except Exception:
                continue
        return out
    except Exception:
        return out


def is_rs_persist_guard_day(
    rs_rank_corr: Optional[float],
    corr_max: float,
) -> bool:
    """
    R5(RS 지속성) 가드: 상관 ≤ corr_max 이면 청산 타이트 대상일.
    """
    try:
        if rs_rank_corr is None or not np.isfinite(float(rs_rank_corr)):
            return False
        return float(rs_rank_corr) <= float(corr_max)
    except Exception:
        return False


def resolve_rs_persist_corr_threshold(
    corr_by_day: Dict[str, float],
    settings: Optional[dict] = None,
) -> float:
    """R5 임계: ``rs_persist_corr_max`` 절대값 (기본 0.60)."""
    s = settings or {}
    try:
        return float(s.get("rs_persist_corr_max", 0.60) or 0.60)
    except (TypeError, ValueError):
        return 0.60


def resolve_transition_regime_policy(
    level: str,
    day_max_a: int,
    day_max_b: int,
    settings: Optional[dict] = None,
) -> Tuple[int, int]:
    """
    Transition 레짐별 슬롯 상한.

    - HARD: 0/0 (신규 감시 없음)
    - TRANSITION: transition_soft_slots (기본 6/6) — tiered 모드용
    """
    s = settings or {}
    try:
        lv = str(level or "OFF").strip().upper()
        if lv == "HARD":
            return 0, 0
        if lv == "TRANSITION":
            sa = _clamp_nonneg_int(s.get("transition_soft_slots_a", 6), 6)
            sb = _clamp_nonneg_int(s.get("transition_soft_slots_b", 6), 6)
            return min(day_max_a, sa), min(day_max_b, sb)
        return day_max_a, day_max_b
    except Exception:
        return day_max_a, day_max_b


def resolve_transition_runtime_blocks(
    regime: str,
    settings: Optional[dict] = None,
) -> Tuple[bool, bool]:
    """
    레짐별 실시간·백테스트 S1/S2 차단 플래그.

    - HARD: S1·S2 모두 차단
    - TRANSITION: ``transition_block_s1_on_soft`` / ``transition_block_s2_on_soft`` 를 그대로 따름
      (슬롯 축소는 ``resolve_transition_regime_policy`` 가 별도로 담당)
    - 그 외: 차단 없음
    """
    s = settings or {}
    try:
        r = str(regime or "OFF").strip().upper()
        if r == "HARD":
            return True, True
        if r == "TRANSITION":
            block_s1 = bool(s.get("transition_block_s1_on_soft", False))
            block_s2 = bool(s.get("transition_block_s2_on_soft", True))
            return block_s1, block_s2
        return False, False
    except Exception:
        return False, False


def _load_kodex200_daily_close(
    data_manager: Any,
    fetch_start: str,
    end_date: str,
) -> pd.DataFrame:
    """
    KODEX200(069500) 일봉 종가. DB ``bars_1d`` + FDR 병합(최근 구간 보강).
    반환 컬럼: ``d``(YYYY-MM-DD), ``close``.
    """
    frames: List[pd.DataFrame] = []
    try:
        if data_manager is not None and hasattr(data_manager, "_get_connection"):
            with data_manager._get_connection() as conn:
                df = pd.read_sql_query(
                    """
                    SELECT datetime as time, close
                    FROM bars_1d
                    WHERE code = '069500' AND datetime BETWEEN ? AND ?
                    ORDER BY datetime
                    """,
                    conn,
                    params=[fetch_start, str(end_date) + " 23:59:59"],
                )
            if df is not None and not df.empty:
                tmp = df.copy()
                tmp["time"] = pd.to_datetime(tmp["time"], errors="coerce")
                tmp["close"] = pd.to_numeric(tmp["close"], errors="coerce")
                tmp = tmp.dropna(subset=["time", "close"])
                if not tmp.empty:
                    tmp["d"] = tmp["time"].dt.strftime("%Y-%m-%d")
                    frames.append(
                        tmp.groupby("d", as_index=False).last()[["d", "close"]]
                    )
    except Exception:
        pass
    try:
        fdr = _fdr_index_daily_close_df(data_manager, "069500", fetch_start, end_date)
        if fdr is not None and not fdr.empty:
            tmp = fdr.copy()
            tmp["d"] = pd.to_datetime(tmp["time"], errors="coerce").dt.strftime(
                "%Y-%m-%d"
            )
            tmp["close"] = pd.to_numeric(tmp["close"], errors="coerce")
            tmp = tmp.dropna(subset=["d", "close"])
            if not tmp.empty:
                frames.append(tmp.groupby("d", as_index=False).last()[["d", "close"]])
    except Exception:
        pass
    if not frames:
        return pd.DataFrame(columns=["d", "close"])
    merged = pd.concat(frames, ignore_index=True)
    # 같은 날은 FDR(뒤쪽) 우선
    merged = merged.drop_duplicates(subset=["d"], keep="last").sort_values("d")
    return merged.reset_index(drop=True)


def _xs_ret_std_by_day(df_ohlcv: pd.DataFrame) -> Dict[str, float]:
    """일별 교차섹션 1일 수익률 표준편차(%) — 종가만 사용하므로 창 길이와 무관."""
    out: Dict[str, float] = {}
    try:
        if df_ohlcv is None or df_ohlcv.empty:
            return out
        _df = df_ohlcv.sort_values(["code", "time"]).copy()
        _df["ret1"] = _df.groupby("code")["close"].pct_change()
        _s = _df.groupby("time")["ret1"].std() * 100.0
        for t_idx, val in _s.items():
            if val is not None and np.isfinite(float(val)):
                out[pd.Timestamp(t_idx).strftime("%Y-%m-%d")] = float(val)
    except Exception:
        return out
    return out


def _compute_g1_r5_by_day(
    settings: dict,
    data_manager: Any,
    df_ohlcv: pd.DataFrame,
    days: List[str],
    regime_by_day: Dict[str, str],
    counts_by_day: Dict[str, Dict[str, float]],
    xs_by_day: Optional[Dict[str, float]] = None,
) -> Dict[str, Dict[str, Any]]:
    """
    일별 G1(로테이션 A 가드)·R5(RS 지속성 가드) 판정.

    설정 ON/OFF 와 무관하게 **판정값**을 계산한다 (적용 여부는 호출부가 결정).
      - G1: 레짐 OFF · KODEX200 5일 수익률 ≥ idx5_min · xs ≥ 최근 120일 xs 분위 · A비중 ≥ 임계
      - R5: 20일 RS 순위의 5영업일 Spearman ≤ ``rs_persist_corr_max``

    반환: ``{YYYY-MM-DD: {"g1", "r5", "rs5", "idx5", "xs", "xs_thr"}}``
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not days:
        return out
    days_sorted = sorted(days)
    try:
        xs_map = xs_by_day if xs_by_day is not None else _xs_ret_std_by_day(df_ohlcv)
    except Exception:
        xs_map = {}

    try:
        xs_pct = float(settings.get("rotation_a_guard_xs_pct", 50.0) or 50.0)
    except (TypeError, ValueError):
        xs_pct = 50.0
    try:
        a_min = float(settings.get("rotation_a_guard_a_share_min", 0.60) or 0.60)
    except (TypeError, ValueError):
        a_min = 0.60
    try:
        # idx5_min=0 허용 → `or` 폴백 사용 금지
        _i5_raw = settings.get("rotation_a_guard_idx5_min", -1.0)
        i5_min = float(-1.0 if _i5_raw is None else _i5_raw)
    except (TypeError, ValueError):
        i5_min = -1.0
    ignore_soft = bool(settings.get("transition_ignore_soft", False))

    # KODEX200 종가 (idx5)
    closes: Optional[pd.Series] = None
    try:
        _ix = _load_kodex200_daily_close(
            data_manager,
            (pd.to_datetime(days_sorted[0]) - timedelta(days=40)).strftime("%Y-%m-%d"),
            days_sorted[-1],
        )
        if _ix is not None and not _ix.empty and {"d", "close"}.issubset(_ix.columns):
            _ix = _ix.drop_duplicates("d").sort_values("d")
            closes = _ix.set_index("d")["close"].astype(float)
    except Exception:
        closes = None

    # R5 상관 (종가 패널 전체에서 1회)
    corr_map: Dict[str, float] = {}
    r5_thr = 0.60
    try:
        corr_map = compute_rs_rank_corr_by_day(
            df_ohlcv, lookback=5, rs_window=20, min_names=40
        )
        r5_thr = resolve_rs_persist_corr_threshold(corr_map, settings)
    except Exception:
        corr_map = {}

    try:
        panel_days = sorted(
            pd.Timestamp(d).strftime("%Y-%m-%d")
            for d in pd.to_datetime(df_ohlcv["time"]).dt.normalize().unique()
        )
    except Exception:
        panel_days = sorted(xs_map.keys())

    for d in days_sorted:
        try:
            idx5: Optional[float] = None
            if closes is not None and d in closes.index:
                _keys = list(closes.index)
                _ii = _keys.index(d)
                if _ii >= 5:
                    _c0 = float(closes.iloc[_ii - 5])
                    _c1 = float(closes.iloc[_ii])
                    if _c0 > 0:
                        idx5 = (_c1 / _c0 - 1.0) * 100.0

            # 실시간과 동일: [D-120일, D] 창의 xs 분위 (창 첫날은 pct_change NaN 이라 제외)
            xs_thr = 3.05
            try:
                left = (pd.to_datetime(d) - timedelta(days=120)).strftime("%Y-%m-%d")
                win_days = [k for k in panel_days if left <= k <= d]
                vals = [xs_map[k] for k in win_days[1:] if k in xs_map]
                if vals:
                    xs_thr = float(np.nanpercentile(vals, xs_pct))
            except Exception:
                xs_thr = 3.05

            xs_d = xs_map.get(d)
            reg = str(regime_by_day.get(d, "OFF") or "OFF").strip().upper()
            if reg == "TRANSITION" and ignore_soft:
                reg = "OFF"
            cnt = counts_by_day.get(d, {})
            g1 = bool(
                is_rotation_a_guard_g1_day(
                    regime=reg,
                    idx5=idx5,
                    xs_ret_std=xs_d,
                    valid_a=int(cnt.get("va", 0)),
                    valid_b=int(cnt.get("vb", 0)),
                    xs_threshold=xs_thr,
                    a_share_min=a_min,
                    idx5_min=i5_min,
                )
            )
            rs5 = corr_map.get(d)
            r5 = bool(is_rs_persist_guard_day(rs5, r5_thr))
            out[d] = {
                "g1": g1,
                "r5": r5,
                "rs5": rs5,
                "idx5": idx5,
                "xs": xs_d,
                "xs_thr": xs_thr,
            }
        except Exception:
            out[d] = {"g1": False, "r5": False, "rs5": None, "idx5": None, "xs": None, "xs_thr": None}
    return out


def resolve_transition_gate_for_live(
    settings: dict,
    data_manager: Any,
    as_of: Optional[datetime] = None,
    logger_callback: Optional[Callable[..., None]] = None,
    *,
    skip_cache: bool = False,
) -> Dict[str, Any]:
    """
    실시간 선정용 transition 레짐·슬롯·차단 플래그.

    FDR 일봉으로 최근 구간을 스코어해 당일 레짐을 판정한다 (``bars_1d`` DB 불필요).

    백테와 같이 **완료된 거래일** 기준만 사용한다.
    ``as_of`` 가 오늘(또는 미래)이면서 아직 장마감+버퍼 전이면 직전 완료 거래일로 스냅한다.
    과거 ``target_date`` 명시 시에는 그 날짜를 그대로 쓴다.

    ``skip_cache=True`` 이면 디스크 캐시를 읽지 않고 FDR을 다시 조회한다.
    """
    log = logger_callback or (lambda *_a, **_k: None)
    max_a = int(settings.get("max_slots_a", 20))
    max_b = int(settings.get("max_slots_b", 15))
    result: Dict[str, Any] = {
        "regime": "OFF",
        "slots_a": max_a,
        "slots_b": max_b,
        "block_s1": False,
        "block_s2": False,
    }
    if not _settings_bool_sel(settings, "use_transition_gate", False):
        # Transition 슬롯 게이트는 꺼져 있어도 G1/R5만 켤 수 있음 → 아래 FDR 경로 유지
        if not (
            _settings_bool_sel(settings, "use_rotation_a_guard_g1", False)
            or _settings_bool_sel(settings, "use_rs_persist_guard", False)
        ):
            return result
    if data_manager is None:
        log("⚠️ [Transition] data_manager 없음 — 게이트 OFF")
        return result
    try:
        as_of_dt = pd.to_datetime(as_of or datetime.now()).normalize()
        # 미완성 당일 일봉으로 레짐이 바뀌지 않도록 직전 완료 거래일로 고정
        try:
            from reporting.krx_trading_calendar import completed_trading_day_asof_local

            try:
                buf = int(float(settings.get("regime_display_eod_buffer_minutes", 30) or 30))
            except (TypeError, ValueError):
                buf = 30
            now_d = datetime.now().date()
            completed = completed_trading_day_asof_local(
                datetime.now(), eod_buffer_minutes=buf
            )
            req_d = as_of_dt.date()
            # 실시간/오늘 이후만 스냅 (과거 선정일은 유지)
            if req_d >= now_d and req_d != completed:
                as_of_dt = pd.Timestamp(completed)
        except Exception:
            pass
        end_s = as_of_dt.strftime("%Y-%m-%d")
        if not skip_cache:
            try:
                from utils.regime_gate_cache import load_regime_gate_cache

                cached = load_regime_gate_cache(as_of=end_s, settings=settings)
                if cached:
                    result.update(cached)
                    log(
                        f"ℹ️ [Transition] {end_s} 레짐 캐시 재사용 "
                        f"(regime={cached.get('regime', 'OFF')}, FDR 재조회 생략)"
                    )
                    return result
            except Exception:
                pass
        else:
            try:
                log(f"ℹ️ [Transition] {end_s} 레짐 강제 재계산 (캐시 무시)")
            except Exception:
                pass
        codes = resolve_universe_codes(settings, logger_callback=log)
        if not codes:
            return result
        # 평가일·직전 5영업일 각각 [D-N, D] 창이 필요 → 연휴 대비 30일 여유
        lookback_n = _selection_daily_lookback_calendar_days(settings)
        fetch_start = (as_of_dt - timedelta(days=lookback_n + 30)).strftime("%Y-%m-%d")
        df_all = _fetch_daily_ohlcv_batch_fdr(
            data_manager, codes, fetch_start, end_s, log
        )
        if df_all is None or df_all.empty:
            log("⚠️ [Transition] FDR 일봉 부족 — 게이트 OFF")
            return result
        df_all = df_all[df_all["time"].dt.normalize() <= as_of_dt]
        if df_all.empty:
            log("⚠️ [Transition] 기준일 이전 FDR 일봉 없음 — 게이트 OFF")
            return result
        day_keys = sorted(
            pd.Timestamp(d).strftime("%Y-%m-%d")
            for d in df_all["time"].dt.normalize().unique()
        )
        if end_s not in day_keys:
            end_s = day_keys[-1]
            as_of_dt = pd.Timestamp(end_s).normalize()
        tgt_idx = day_keys.index(end_s)
        eval_days = day_keys[max(0, tgt_idx - 5) : tgt_idx + 1]
        counts_by_day = _sliding_valid_counts_by_day(
            df_all, settings, eval_days, lookback_n
        )
        tgt = counts_by_day.get(end_s)
        if tgt is None:
            log(f"⚠️ [Transition] {end_s} 슬라이딩 스코어 실패 — 게이트 OFF")
            return result
        va = int(tgt["va"])
        vb = int(tgt["vb"])
        share = float(tgt["share"])
        # share delta: 평가일 기준 3·5영업일 전 (각 날짜도 자기 창으로 재계산)
        d5 = None
        d3 = None
        if len(eval_days) >= 6 and eval_days[-6] in counts_by_day:
            d5 = share - float(counts_by_day[eval_days[-6]]["share"])
        if len(eval_days) >= 4 and eval_days[-4] in counts_by_day:
            d3 = share - float(counts_by_day[eval_days[-4]]["share"])
        xs_by_day = _xs_ret_std_by_day(df_all)
        _xs_v = xs_by_day.get(end_s)
        xs = float(_xs_v) if _xs_v is not None else np.nan
        iv = _build_index_vol_by_day(
            data_manager,
            (as_of_dt - timedelta(days=60)).strftime("%Y-%m-%d"),
            end_s,
        ).get(end_s, {})
        regime = classify_transition_regime(
            va,
            vb,
            share,
            d5,
            d3,
            settings,
            index_rvol_20=iv.get("rvol_20"),
            xs_ret_std=xs if np.isfinite(xs) else None,
            index_day_ret=iv.get("ret_pct"),
        )
        if regime == "TRANSITION" and bool(settings.get("transition_ignore_soft", False)):
            regime = "OFF"
        slots_a, slots_b = max_a, max_b
        if regime == "HARD":
            slots_a, slots_b = 0, 0
        elif regime == "TRANSITION":
            slots_a, slots_b = resolve_transition_regime_policy(
                "TRANSITION", max_a, max_b, settings
            )
        block_s1, block_s2 = resolve_transition_runtime_blocks(regime, settings)
        # G1(Type A 신규 억제)·R5(Type A 청산 타이트) — 이력 창과 같은 헬퍼로 판정
        _g1_on = _settings_bool_sel(settings, "use_rotation_a_guard_g1", False)
        _r5_enabled = _settings_bool_sel(settings, "use_rs_persist_guard", False)
        block_type_a = False
        _r5_on = False
        _r5_corr: Optional[float] = None
        if _g1_on or _r5_enabled:
            try:
                _gr = _compute_g1_r5_by_day(
                    settings,
                    data_manager,
                    df_all,
                    [end_s],
                    {end_s: regime},
                    {end_s: tgt},
                    xs_by_day,
                ).get(end_s, {})
                if _g1_on:
                    block_type_a = bool(_gr.get("g1", False))
                if _r5_enabled:
                    _r5_on = bool(_gr.get("r5", False))
                    _r5_corr = _gr.get("rs5")
            except Exception:
                block_type_a = False
                _r5_on = False
        _ret_s = iv.get("ret_pct")
        _ret_txt = (
            f"{float(_ret_s):+.2f}%"
            if _ret_s is not None and np.isfinite(float(_ret_s))
            else "n/a"
        )
        _r5_txt = (
            f"{float(_r5_corr):.3f}"
            if _r5_corr is not None and np.isfinite(float(_r5_corr))
            else "n/a"
        )
        log(
            f"[Transition] {end_s} 레짐={regime} Valid_A={va} Valid_B={vb} "
            f"share={share:.1f}% d5={d5 if d5 is not None else 'n/a'} "
            f"idx_ret={_ret_txt} "
            f"→ 슬롯 A={slots_a}/B={slots_b} S1차단={block_s1} S2차단={block_s2} "
            f"G1_A차단={block_type_a} R5청산타이트={_r5_on}(rs5={_r5_txt})"
        )
        result.update(
            {
                "regime": regime,
                "slots_a": slots_a,
                "slots_b": slots_b,
                "block_s1": block_s1,
                "block_s2": block_s2,
                "block_type_a": block_type_a,
                "r5_exit_tighten": bool(_r5_on),
                "rs5_corr": (
                    float(_r5_corr)
                    if _r5_corr is not None and np.isfinite(float(_r5_corr))
                    else None
                ),
                "as_of": end_s,
            }
        )
        try:
            from utils.regime_gate_cache import save_regime_gate_cache

            save_regime_gate_cache(as_of=end_s, settings=settings, payload=result)
        except Exception:
            pass
        return result
    except Exception as ex:
        try:
            log(f"⚠️ [Transition] 레짐 판정 실패(OFF): {ex}")
        except Exception:
            pass
        return result


def compute_transition_regime_history(
    settings: dict,
    data_manager: Any,
    *,
    lookback_trading_days: int = 22,
    end_date: Optional[Union[str, datetime]] = None,
) -> List[Dict[str, Any]]:
    """
    최근 N영업일 일별 transition 레짐 이력.

    ``prepare_dynamic_universe`` 와 동일 분류 로직(게이트 ON으로 일시 강제).
    GUI 이력 차트용 — 실전 ``use_transition_gate`` 와 무관하게 분류 결과를 반환한다.

    레짐은 **완료 거래일(data_as_of)** 일봉으로 판정하고, 반환 ``date``/``apply_on`` 은
    그 레짐이 적용되는 **다음 영업일**이다 (헤더 레짐 표시와 동일).
    마지막 데이터일은 **완료된 거래일**로 캡해 미완성 당일 일봉이 섞이지 않게 한다.
    """
    out: List[Dict[str, Any]] = []
    try:
        n = max(5, min(int(lookback_trading_days or 22), 60))
        st = dict(settings or {})
        st["use_transition_gate"] = True

        end_s = pd.to_datetime(end_date or datetime.now()).strftime("%Y-%m-%d")
        if end_date is None:
            try:
                from reporting.krx_trading_calendar import (
                    completed_trading_day_asof_local,
                )

                try:
                    _buf = int(
                        float(st.get("regime_display_eod_buffer_minutes", 30) or 30)
                    )
                except (TypeError, ValueError):
                    _buf = 30
                end_s = completed_trading_day_asof_local(
                    datetime.now(), eod_buffer_minutes=_buf
                ).strftime("%Y-%m-%d")
            except Exception:
                pass
        # 표시 N영업일 + share delta 5영업일을 덮는 달력일 (연휴 여유 포함)
        span_days = max(75, int((n + 6) * 1.6) + 14)
        start_s = (pd.to_datetime(end_s) - timedelta(days=span_days)).strftime("%Y-%m-%d")

        def _silent(*_a: Any, **_k: Any) -> None:
            pass

        guard_by_day: Dict[str, Dict[str, Any]] = {}
        _result = _compute_transition_regime_map_fdr(
            st,
            data_manager,
            start_s,
            end_s,
            _silent,
            eval_last_days=n,
            detail_out=guard_by_day,
        )
        regime_map: Dict[str, str] = dict(_result or {})
        # 헤더(resolve_transition_gate_for_live)와 동일하게 약화 무시 옵션 반영
        if bool(st.get("transition_ignore_soft", False)):
            regime_map = {
                k: ("OFF" if str(v).upper() == "TRANSITION" else v)
                for k, v in regime_map.items()
            }

        # KRX 영업일과의 교집합만 사용 (FDR 이상치 일자 방어). 캘린더 실패 시 맵 그대로.
        try:
            from reporting.krx_trading_calendar import trading_days_inclusive

            krx_set = {d.strftime("%Y-%m-%d") for d in trading_days_inclusive(start_s, end_s)}
        except Exception:
            krx_set = set()

        cand = sorted(k for k in regime_map.keys() if k <= end_s)
        if krx_set:
            filtered = [d for d in cand if d in krx_set]
            if filtered:
                cand = filtered
        display_days = cand[-n:]

        labels = {
            "OFF": "유지",
            "TRANSITION": "약화",
            "HARD": "붕괴",
        }
        try:
            from reporting.krx_trading_calendar import next_trading_day as _next_td
        except Exception:
            _next_td = None  # type: ignore

        for d in display_days:
            reg = str(regime_map.get(d, "OFF")).strip().upper()
            if reg not in ("OFF", "TRANSITION", "HARD"):
                reg = "OFF"
            # UI 표시는 적용일(다음 영업일). 데이터 판정일은 data_as_of 에 보관.
            apply_on = d
            try:
                if _next_td is not None:
                    nxt = _next_td(d)
                    if nxt is not None:
                        apply_on = nxt.strftime("%Y-%m-%d")
            except Exception:
                apply_on = d
            _gd = guard_by_day.get(d, {})
            _rs5 = _gd.get("rs5")
            out.append(
                {
                    "date": apply_on,
                    "data_as_of": d,
                    "apply_on": apply_on,
                    "regime": reg,
                    "label": labels.get(reg, reg),
                    # 판정값 (실전 적용 여부는 use_rotation_a_guard_g1 / use_rs_persist_guard)
                    "g1": bool(_gd.get("g1", False)),
                    "r5": bool(_gd.get("r5", False)),
                    "rs5": (
                        float(_rs5)
                        if _rs5 is not None and np.isfinite(float(_rs5))
                        else None
                    ),
                }
            )
    except Exception:
        return out
    return out

