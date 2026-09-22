# -*- coding: utf-8 -*-
"""시장 breadth·전이 레짐·지수 DD 게이트.

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

from strategy.selection_dynamic_score import _score_dynamic_universe_daily_ohlcv
from strategy.universe_codes import load_kodex200_universe_codes


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


def _compute_transition_regime_map_fdr(
    settings: dict,
    data_manager: Any,
    start_date: str,
    end_date: str,
    logger_callback: Optional[Callable[..., None]] = None,
) -> Dict[str, str]:
    """
    FDR 일봉 기반 일별 transition 레짐 맵.
    ``prepare_dynamic_universe`` 의 레짐 분류와 동일 로직, DB ``bars_1d`` 미사용.
    """
    log = logger_callback or (lambda *_a, **_k: None)
    regime_map: Dict[str, str] = {}
    try:
        start_s = pd.to_datetime(start_date).strftime("%Y-%m-%d")
        end_s = pd.to_datetime(end_date).strftime("%Y-%m-%d")
        codes = resolve_universe_codes(settings, logger_callback=log)
        if not codes:
            return regime_map

        fetch_start = (pd.to_datetime(start_s) - timedelta(days=120)).strftime("%Y-%m-%d")
        df_all = _fetch_daily_ohlcv_batch_fdr(
            data_manager, codes, fetch_start, end_s, log
        )
        if df_all is None or df_all.empty:
            return regime_map

        _score_dynamic_universe_daily_ohlcv(df_all, settings, with_b_debug=False)
        df_sim = df_all[df_all["time"] >= pd.to_datetime(start_s)].copy()
        if df_sim.empty:
            return regime_map

        trading_days_list = df_sim["time"].dt.strftime("%Y-%m-%d").unique().tolist()

        xs_ret_std_by_day: Dict[str, float] = {}
        try:
            _xs_df = df_sim.sort_values(["code", "time"]).copy()
            _xs_df["ret1"] = _xs_df.groupby("code")["close"].pct_change()
            _xs_s = _xs_df.groupby("time")["ret1"].std() * 100.0
            for t_idx, val in _xs_s.items():
                if val is not None and np.isfinite(float(val)):
                    xs_ret_std_by_day[pd.Timestamp(t_idx).strftime("%Y-%m-%d")] = float(
                        val
                    )
        except Exception:
            xs_ret_std_by_day = {}

        fetch_start_iv = (pd.to_datetime(start_s) - timedelta(days=60)).strftime(
            "%Y-%m-%d"
        )
        index_vol_by_day = _build_index_vol_by_day(data_manager, fetch_start_iv, end_s)

        transition_stats_by_day: Dict[str, Dict[str, float]] = {}
        try:
            _t_rows: List[Dict[str, Any]] = []
            for _d_str in trading_days_list:
                _day = df_sim[df_sim["time"] == pd.to_datetime(_d_str)]
                _va = int(_day["Valid_A"].astype(bool).sum()) if "Valid_A" in _day.columns else 0
                _vb = int(_day["Valid_B"].astype(bool).sum()) if "Valid_B" in _day.columns else 0
                _tot = _va + _vb
                _share = (_vb / _tot * 100.0) if _tot > 0 else 0.0
                _t_rows.append({"d": _d_str, "va": _va, "vb": _vb, "share": _share})
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


def build_index_dd_s1_block_by_day(
    settings: Optional[dict],
    data_manager: Any,
    start_date: str,
    end_date: str,
    logger_callback: Optional[Callable[..., None]] = None,
) -> Dict[str, bool]:
    """
    지수 드로우다운+쿨다운 게이트: 일별 S1 차단 맵.

    규칙 (기본 DD5_5_CD5):
      KODEX200 N일 수익률(%) <= thr 인 **데이터일** D 종가 확정 후,
      다음 영업일부터 ``cooldown`` 영업일 동안 S1 신규 차단.
      S2·보유 청산은 막지 않는다.

    반환: ``{ 'YYYY-MM-DD': True/False }`` (적용일 기준, BT ``current_day`` 키).
    """
    log = logger_callback or (lambda *_a, **_k: None)
    out: Dict[str, bool] = {}
    s = settings or {}
    try:
        if not bool(s.get("use_index_dd_gate", False)):
            return out
        try:
            n = max(1, int(float(s.get("index_dd_lookback_days", 5) or 5)))
        except (TypeError, ValueError):
            n = 5
        try:
            thr = float(s.get("index_dd_ret_threshold_pct", -5.0) or -5.0)
        except (TypeError, ValueError):
            thr = -4.0
        try:
            cooldown = max(1, int(float(s.get("index_dd_cooldown_days", 5) or 5)))
        except (TypeError, ValueError):
            cooldown = 5

        start_s = pd.to_datetime(start_date).strftime("%Y-%m-%d")
        end_s = pd.to_datetime(end_date).strftime("%Y-%m-%d")
        fetch_start = (pd.to_datetime(start_s) - timedelta(days=max(40, n * 3))).strftime(
            "%Y-%m-%d"
        )
        idx = _load_kodex200_daily_close(data_manager, fetch_start, end_s)
        if idx is None or idx.empty:
            try:
                log("⚠️ [IndexDD] KODEX200 일봉 없음 — 게이트 미적용")
            except Exception:
                pass
            return out

        close = pd.to_numeric(idx["close"], errors="coerce")
        idx = idx.copy()
        idx["ret_nd"] = (close / close.shift(n) - 1.0) * 100.0
        days = idx["d"].astype(str).tolist()
        rets = idx["ret_nd"].tolist()

        block_until_i = -1
        for i, d in enumerate(days):
            blocked = i <= block_until_i
            if start_s <= d <= end_s:
                out[d] = bool(blocked)
            try:
                v = rets[i]
                if v is not None and np.isfinite(float(v)) and float(v) <= thr:
                    block_until_i = max(block_until_i, i + cooldown)
            except Exception:
                pass

        n_on = sum(1 for v in out.values() if v)
        try:
            log(
                f"[IndexDD] N={n} thr={thr}% cd={cooldown} → "
                f"차단일 {n_on}/{len(out)} ({start_s}~{end_s})"
            )
        except Exception:
            pass
    except Exception as ex:
        try:
            log(f"⚠️ [IndexDD] 맵 생성 실패: {ex}")
        except Exception:
            pass
        return {}
    return out


def resolve_index_dd_gate_for_live(
    settings: dict,
    data_manager: Any,
    as_of: Optional[datetime] = None,
    logger_callback: Optional[Callable[..., None]] = None,
) -> Dict[str, Any]:
    """
    실시간용 지수 DD 게이트. 완료 거래일 기준 맵을 만들고 당일(또는 다음 적용일) 차단 여부 반환.
    """
    log = logger_callback or (lambda *_a, **_k: None)
    result: Dict[str, Any] = {
        "block_s1": False,
        "active": False,
        "as_of": None,
        "lookback": None,
        "thr": None,
        "cooldown": None,
    }
    try:
        if not bool(settings.get("use_index_dd_gate", False)):
            return result
        end_dt = as_of or datetime.now()
        try:
            from reporting.krx_trading_calendar import completed_trading_day_asof_local

            try:
                buf = int(
                    float(settings.get("regime_display_eod_buffer_minutes", 30) or 30)
                )
            except (TypeError, ValueError):
                buf = 30
            end_s = completed_trading_day_asof_local(
                end_dt, eod_buffer_minutes=buf
            ).strftime("%Y-%m-%d")
        except Exception:
            end_s = pd.to_datetime(end_dt).strftime("%Y-%m-%d")

        start_s = (pd.to_datetime(end_s) - timedelta(days=90)).strftime("%Y-%m-%d")
        block_map = build_index_dd_s1_block_by_day(
            settings, data_manager, start_s, end_s, log
        )
        # 적용일: 완료일 다음 영업일(장중에는 전일 확정 레짐 사용과 동일하게 end_s 자체도 조회)
        apply_on = end_s
        try:
            from reporting.krx_trading_calendar import next_trading_day

            nxt = next_trading_day(end_s)
            if nxt is not None:
                apply_on = nxt.strftime("%Y-%m-%d")
        except Exception:
            pass
        # 장중: 오늘이 apply_on 이거나, 맵에 오늘 키가 있으면 오늘 기준
        today_s = pd.to_datetime(end_dt).strftime("%Y-%m-%d")
        blocked = bool(block_map.get(today_s) or block_map.get(apply_on) or False)
        result.update(
            {
                "block_s1": blocked,
                "active": blocked,
                "as_of": end_s,
                "apply_on": apply_on,
                "lookback": settings.get("index_dd_lookback_days", 5),
                "thr": settings.get("index_dd_ret_threshold_pct", -5.0),
                "cooldown": settings.get("index_dd_cooldown_days", 5),
            }
        )
        try:
            if blocked:
                log(
                    f"ℹ️ [IndexDD] S1 차단 활성 (as_of={end_s}, apply≈{apply_on})"
                )
        except Exception:
            pass
    except Exception as ex:
        try:
            log(f"⚠️ [IndexDD] 실시간 판정 실패: {ex}")
        except Exception:
            pass
    return result


def resolve_transition_gate_for_live(
    settings: dict,
    data_manager: Any,
    as_of: Optional[datetime] = None,
    logger_callback: Optional[Callable[..., None]] = None,
) -> Dict[str, Any]:
    """
    실시간 선정용 transition 레짐·슬롯·차단 플래그.

    FDR 일봉으로 최근 구간을 스코어해 당일 레짐을 판정한다 (``bars_1d`` DB 불필요).

    백테와 같이 **완료된 거래일** 기준만 사용한다.
    ``as_of`` 가 오늘(또는 미래)이면서 아직 장마감+버퍼 전이면 직전 완료 거래일로 스냅한다.
    과거 ``target_date`` 명시 시에는 그 날짜를 그대로 쓴다.
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
        try:
            from utils.regime_gate_cache import load_regime_gate_cache, save_regime_gate_cache

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
        start_s = (as_of_dt - timedelta(days=30)).strftime("%Y-%m-%d")
        codes = resolve_universe_codes(settings, logger_callback=log)
        if not codes:
            return result
        fetch_start = (as_of_dt - timedelta(days=120)).strftime("%Y-%m-%d")
        df_all = _fetch_daily_ohlcv_batch_fdr(
            data_manager, codes, fetch_start, end_s, log
        )
        if df_all is None or df_all.empty:
            log("⚠️ [Transition] FDR 일봉 부족 — 게이트 OFF")
            return result
        _score_dynamic_universe_daily_ohlcv(df_all, settings, with_b_debug=False)
        df_day = df_all[df_all["time"].dt.normalize() == as_of_dt]
        if df_day.empty:
            last_t = df_all["time"].max()
            df_day = df_all[df_all["time"].dt.normalize() == pd.Timestamp(last_t).normalize()]
            end_s = pd.Timestamp(last_t).strftime("%Y-%m-%d")
            as_of_dt = pd.Timestamp(last_t).normalize()
        va = int(df_day["Valid_A"].astype(bool).sum()) if "Valid_A" in df_day.columns else 0
        vb = int(df_day["Valid_B"].astype(bool).sum()) if "Valid_B" in df_day.columns else 0
        tot = va + vb
        share = (vb / tot * 100.0) if tot > 0 else 0.0
        # share delta: 최근 6영업일
        days = sorted(df_all["time"].dt.normalize().unique())
        share_by_d: Dict[str, float] = {}
        for d in days:
            g = df_all[df_all["time"].dt.normalize() == d]
            t2 = int(g["Valid_A"].astype(bool).sum()) + int(g["Valid_B"].astype(bool).sum())
            vb2 = int(g["Valid_B"].astype(bool).sum())
            share_by_d[pd.Timestamp(d).strftime("%Y-%m-%d")] = (
                (vb2 / t2 * 100.0) if t2 > 0 else 0.0
            )
        d5 = None
        d3 = None
        if end_s in share_by_d:
            keys = sorted(share_by_d.keys())
            idx = keys.index(end_s) if end_s in keys else len(keys) - 1
            if idx >= 5:
                d5 = share - share_by_d[keys[idx - 5]]
            if idx >= 3:
                d3 = share - share_by_d[keys[idx - 3]]
        xs = np.nan
        try:
            _xsdf = df_all.sort_values(["code", "time"]).copy()
            _xsdf["ret1"] = _xsdf.groupby("code")["close"].pct_change()
            xs_s = _xsdf.groupby("time")["ret1"].std() * 100.0
            xs = float(xs_s.loc[as_of_dt]) if as_of_dt in xs_s.index else float("nan")
        except Exception:
            xs = np.nan
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
        _ret_s = iv.get("ret_pct")
        _ret_txt = (
            f"{float(_ret_s):+.2f}%"
            if _ret_s is not None and np.isfinite(float(_ret_s))
            else "n/a"
        )
        log(
            f"[Transition] {end_s} 레짐={regime} Valid_A={va} Valid_B={vb} "
            f"share={share:.1f}% d5={d5 if d5 is not None else 'n/a'} "
            f"idx_ret={_ret_txt} "
            f"→ 슬롯 A={slots_a}/B={slots_b} S1차단={block_s1} S2차단={block_s2}"
        )
        result.update(
            {
                "regime": regime,
                "slots_a": slots_a,
                "slots_b": slots_b,
                "block_s1": block_s1,
                "block_s2": block_s2,
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
        start_s = (pd.to_datetime(end_s) - timedelta(days=75)).strftime("%Y-%m-%d")

        def _silent(*_a: Any, **_k: Any) -> None:
            pass

        _result = _compute_transition_regime_map_fdr(st, data_manager, start_s, end_s, _silent)
        regime_map: Dict[str, str] = dict(_result or {})

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
            out.append(
                {
                    "date": apply_on,
                    "data_as_of": d,
                    "apply_on": apply_on,
                    "regime": reg,
                    "label": labels.get(reg, reg),
                }
            )
    except Exception:
        return out
    return out

