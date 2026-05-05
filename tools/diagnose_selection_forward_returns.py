# -*- coding: utf-8 -*-
"""
선정 레이어 진단: `prepare_dynamic_universe` 와 동일한 일봉 로드·`_score_dynamic_universe_daily_ohlcv`
산출물을 사용해, **선정일 T 종가 기준 H거래일 후 선행 수익률**과 `Score_A`/`Valid_A` 등의 관계를 요약한다.

사용 예:
  python tools/diagnose_selection_forward_returns.py --start 2024-01-01 --end 2025-06-30
  python tools/diagnose_selection_forward_returns.py --start 2024-01-01 --end 2025-06-30 --horizons 1,5,20 --csv out_sel_fwd.csv

주의: `bars_1d` 가 채워진 `trading_data.db` 가 필요하다. 실시간 FDR 전용 선정과는 데이터 소스가 다를 수 있다.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

# 프로젝트 루트
_ABS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ABS_ROOT not in sys.path:
    sys.path.insert(0, _ABS_ROOT)

from core.defaults import DEFAULT_SETTINGS  # noqa: E402
from data_manager_v2 import DataManagerV2  # noqa: E402
from strategy_selector import (  # noqa: E402
    _df_top_by_score_slot_band,
    _score_dynamic_universe_daily_ohlcv,
    load_kodex200_universe_codes,
    load_kospi_universe_codes,
)

logger = logging.getLogger(__name__)


def _load_merged_settings(path: Optional[str]) -> dict[str, Any]:
    """DEFAULT_SETTINGS 위에 JSON 을 얹는다."""
    s: dict[str, Any] = dict(DEFAULT_SETTINGS)
    try:
        p = path or os.path.join(_ABS_ROOT, "settings.json")
        if os.path.isfile(p):
            with open(p, "r", encoding="utf-8") as f:
                user = json.load(f)
            if isinstance(user, dict):
                s.update(user)
    except Exception as e:
        try:
            logger.warning("설정 로드 실패, DEFAULT만 사용: %s", e)
        except Exception:
            pass
    return s


def _resolve_union_tickers(
    start_s: str,
    end_s: str,
    universe_index_choice: str,
    kospi_top_n: int,
) -> Tuple[List[str], int]:
    """`prepare_dynamic_universe` 와 동일한 유니버스 누적 범위(Lean vs 월별)."""
    all_days = pd.date_range(start=start_s, end=end_s, freq="B")
    duration_days = int((all_days[-1] - all_days[0]).days) if len(all_days) > 1 else 0
    union_tickers: set[str] = set()
    idx_choice = str(universe_index_choice).strip().upper()
    if idx_choice not in ("KODEX200", "KOSPI200"):
        idx_choice = "KODEX200"
    if duration_days < 180:
        try:
            tickers = (
                load_kospi_universe_codes(logger_callback=None, top_n=int(kospi_top_n))
                if idx_choice == "KOSPI200"
                else load_kodex200_universe_codes(logger_callback=None)
            ) or []
        except Exception:
            tickers = []
        union_tickers = {str(t).zfill(6) for t in tickers}
    else:
        last_m = ""
        for d in all_days:
            curr_m = d.strftime("%Y%m")
            if curr_m != last_m or not union_tickers:
                try:
                    tickers = (
                        load_kospi_universe_codes(
                            logger_callback=None,
                            top_n=int(kospi_top_n),
                        )
                        if idx_choice == "KOSPI200"
                        else load_kodex200_universe_codes(logger_callback=None)
                    ) or []
                except Exception:
                    tickers = []
                union_tickers.update(str(t).zfill(6) for t in tickers)
                last_m = curr_m
    if not union_tickers:
        candidate_files = ["kodex200.csv", "kodex_200.csv", "kospi_top_500.csv"]
        if idx_choice == "KOSPI200":
            candidate_files = ["kospi_universe.csv", "kospi_top_500.csv"]

        for f_name in candidate_files:
            fp = os.path.join(_ABS_ROOT, f_name)
            if not os.path.isfile(fp):
                continue
            try:
                df_k = pd.read_csv(fp, dtype=str)
                c_col = (
                    "Code"
                    if "Code" in df_k.columns
                    else ("종목코드" if "종목코드" in df_k.columns else None)
                )
                if c_col:
                    union_tickers = set(
                        df_k[c_col].apply(lambda x: str(x).zfill(6)).tolist()
                    )
                    break
            except Exception:
                pass
    return sorted(union_tickers), duration_days


def _get_monthly_universe(
    dt: pd.Timestamp,
    cache: Dict[str, frozenset[str]],
    universe_index_choice: str,
) -> frozenset[str]:
    mk = pd.Timestamp(dt).strftime("%Y%m")
    if mk not in cache:
        idx_choice = str(universe_index_choice).strip().upper()
        if idx_choice not in ("KODEX200", "KOSPI200"):
            idx_choice = "KODEX200"
        try:
            tlist = (
                load_kospi_universe_codes(
                    logger_callback=None,
                    top_n=int(settings.get("kospi_top_n", 200)),
                )
                if idx_choice == "KOSPI200"
                else load_kodex200_universe_codes(logger_callback=None)
            ) or []
        except Exception:
            tlist = []
        cache[mk] = frozenset(str(t).zfill(6) for t in tlist)
    return cache[mk]


def load_scored_panel(
    settings: dict[str, Any],
    start_date: str,
    end_date: str,
    data_manager: DataManagerV2,
) -> Tuple[pd.DataFrame, List[str], int]:
    """
    bars_1d 조회 → `_score_dynamic_universe_daily_ohlcv` 적용 → 분석 구간(`>= start_date`) 서브프레임.

    Returns
    -------
    df_sim :
        `time`이 start 이상인 행(지표·Valid_*·Score_* 포함).
    all_codes, duration_days :
        유니버스 리스트 및 Lean 여부 판단용 일수.
    """
    start_s = pd.to_datetime(start_date).strftime("%Y-%m-%d")
    end_s = pd.to_datetime(end_date).strftime("%Y-%m-%d")
    all_codes, duration_days = _resolve_union_tickers(
        start_s,
        end_s,
        settings.get("universe_index_choice", "KODEX200"),
        int(settings.get("kospi_top_n", 200)),
    )
    if not all_codes:
        raise RuntimeError("유니버스 종목이 비었습니다. kodex200.csv 등을 확인하세요.")

    fetch_start_dt = pd.to_datetime(start_s) - timedelta(days=120)
    fetch_start = fetch_start_dt.strftime("%Y-%m-%d")
    placeholders = ",".join(["?"] * len(all_codes))
    query = f"""
        SELECT code, datetime as time, open, high, low, close, volume
        FROM bars_1d
        WHERE code IN ({placeholders}) AND datetime BETWEEN ? AND ?
        ORDER BY code, time
    """
    params = list(all_codes) + [fetch_start, end_s]
    try:
        with data_manager._get_connection() as conn:
            df_all = pd.read_sql_query(query, conn, params=params)
    except Exception as e:
        raise RuntimeError(f"bars_1d 조회 실패: {e}") from e

    if df_all is None or df_all.empty:
        raise RuntimeError("bars_1d 에 해당 구간 데이터가 없습니다.")

    df_all["time"] = pd.to_datetime(df_all["time"], errors="coerce")
    df_all = df_all.dropna(subset=["time"])
    df_all["code"] = df_all["code"].astype(str).str.zfill(6)
    df_all["close"] = pd.to_numeric(df_all["close"], errors="coerce")

    _score_dynamic_universe_daily_ohlcv(df_all, settings, with_b_debug=False)

    df_sim = df_all[df_all["time"] >= pd.to_datetime(start_s)].copy()
    return df_sim, all_codes, duration_days


def add_forward_returns(
    df: pd.DataFrame, horizons: Sequence[int]
) -> pd.DataFrame:
    """종목별 정렬 후 `close` 기준 H행 뒤 수익률(%)."""
    out = df.sort_values(["code", "time"]).copy()
    g = out.groupby("code", sort=False)["close"]
    for h in horizons:
        col = f"fwd_ret_{h}d"
        try:
            out[col] = (g.transform(lambda s, hh=h: s.shift(-hh)) / out["close"] - 1.0) * 100.0
        except Exception:
            out[col] = np.nan
    return out


try:
    from scipy.stats import spearmanr as _spearmanr
except Exception:  # pragma: no cover
    _spearmanr = None


def _build_schedule_a_b(
    df_sim: pd.DataFrame,
    settings: dict[str, Any],
    all_codes: List[str],
    duration_days: int,
    univ_cache: Dict[str, frozenset[str]],
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """`prepare_dynamic_universe` 와 동일한 Track A/B 일별 상위 스케줄."""
    schedule_a: Dict[str, List[str]] = {}
    schedule_b: Dict[str, List[str]] = {}
    max_s_a = int(settings.get("max_slots_a", 20))
    max_s_b = int(settings.get("max_slots_b", 10))
    floor_sa = max(0.0, float(settings.get("sel_track_min_score_a", 0.0) or 0.0))
    floor_sb = max(0.0, float(settings.get("sel_track_min_score_b", 0.0) or 0.0))
    u_all = {str(c).zfill(6) for c in all_codes}
    for d_str in df_sim["time"].dt.strftime("%Y-%m-%d").drop_duplicates().sort_values():
        if duration_days < 180:
            u_set = u_all
        else:
            u_set = set(_get_monthly_universe(pd.to_datetime(d_str), univ_cache))
        day_mask = (df_sim["time"] == pd.to_datetime(d_str)) & (df_sim["code"].isin(u_set))
        day_a = df_sim.loc[day_mask & df_sim["Valid_A"]]
        if not day_a.empty:
            schedule_a[d_str] = (
                _df_top_by_score_slot_band(
                    day_a,
                    score_col="Score_A",
                    code_col="code",
                    max_n=max_s_a,
                    settings=settings,
                    min_score_floor=floor_sa,
                )["code"]
                .astype(str)
                .str.zfill(6)
                .tolist()
            )
        day_b = df_sim.loc[day_mask & df_sim["Valid_B"]]
        if not day_b.empty:
            schedule_b[d_str] = (
                _df_top_by_score_slot_band(
                    day_b,
                    score_col="Score_B",
                    code_col="code",
                    max_n=max_s_b,
                    settings=settings,
                    min_score_floor=floor_sb,
                )["code"]
                .astype(str)
                .str.zfill(6)
                .tolist()
            )
    return schedule_a, schedule_b


def decile_table(
    df: pd.DataFrame, score_col: str, ret_col: str
) -> Optional[pd.DataFrame]:
    """점수 순위를 10분위로 나누고 각 구간의 표본 수·평균·중앙 수익률."""
    sub = df[[score_col, ret_col]].replace([np.inf, -np.inf], np.nan).dropna()
    if len(sub) < 50:
        return None
    try:
        ranks = sub[score_col].rank(method="average", pct=True)
        sub = sub.assign(_dec=np.minimum((ranks * 10).astype(int), 9))
    except Exception:
        return None
    grp = sub.groupby("_dec", observed=True)[ret_col]
    out = pd.DataFrame(
        {
            "n": grp.size(),
            "mean_pct": grp.mean(),
            "median_pct": grp.median(),
        }
    )
    out.index.name = "decile_0_low_9_high"
    return out


def top_bottom_spread(df: pd.DataFrame, score_col: str, ret_col: str) -> Optional[float]:
    """상위 20% 점수 vs 하위 20% 점수 그룹의 평균 수익률 차이(%p)."""
    sub = df[[score_col, ret_col]].replace([np.inf, -np.inf], np.nan).dropna()
    if len(sub) < 30:
        return None
    hi = sub[score_col] >= sub[score_col].quantile(0.8)
    lo = sub[score_col] <= sub[score_col].quantile(0.2)
    try:
        return float(sub.loc[hi, ret_col].mean() - sub.loc[lo, ret_col].mean())
    except Exception:
        return None


def random_same_n_baseline(
    df_day: pd.DataFrame,
    valid_mask: pd.Series,
    ret_col: str,
    univ_mask: pd.Series,
    rng: np.random.Generator,
    n_draw: int = 1,
) -> Optional[float]:
    """
    해당 일자에서 **Valid 표본 수 n** 과 같게, 유니버스(`univ_mask`) 안에서 무작위로 n종목을
    뽑아 동일 `ret_col` 평균을 낸다(당일 Valid 평균과 비교용 베이스라인).
    """
    va = df_day.loc[valid_mask]
    if va.empty or ret_col not in df_day.columns:
        return None
    n = len(va)
    pool = df_day.loc[univ_mask & df_day[ret_col].notna()]
    if pool.empty or len(pool) < 2:
        return None
    means: List[float] = []
    codes = pool["code"].astype(str).str.zfill(6).tolist()
    arr = pool[ret_col].to_numpy(dtype=float)
    for _ in range(max(1, n_draw)):
        if n >= len(codes):
            pick = np.arange(len(codes))
        else:
            pick = rng.choice(len(codes), size=n, replace=False)
        means.append(float(np.nanmean(arr[pick])))
    return float(np.mean(means)) if means else None


def run_diagnosis(
    settings: dict[str, Any],
    start_date: str,
    end_date: str,
    horizons: List[int],
    csv_path: Optional[str],
    random_draws: int,
    seed: int,
) -> None:
    idx_choice = str(settings.get("universe_index_choice", "KODEX200")).strip().upper()
    if idx_choice not in ("KODEX200", "KOSPI200"):
        idx_choice = "KODEX200"
    dm = DataManagerV2()
    df_sim, all_codes, duration_days = load_scored_panel(
        settings, start_date, end_date, dm
    )
    df_sim = add_forward_returns(df_sim, horizons)
    univ_cache: Dict[str, frozenset[str]] = {}
    # 행별 유니버스(장기 백테 시 월 구성원 필터)
    if duration_days >= 180:
        mk = df_sim["time"].dt.strftime("%Y%m")
        for mkey in mk.unique():
            _get_monthly_universe(pd.Timestamp(mkey + "01"), univ_cache, idx_choice)
        df_sim["_in_univ"] = [
            str(c).zfill(6) in _get_monthly_universe(t, univ_cache, idx_choice)
            for c, t in zip(df_sim["code"], df_sim["time"])
        ]
    else:
        df_sim["_in_univ"] = True

    schedule_a, schedule_b = _build_schedule_a_b(
        df_sim, settings, all_codes, duration_days, univ_cache
    )

    # 스케줄 상위 여부
    da = {d: set(codes) for d, codes in schedule_a.items()}
    db = {d: set(codes) for d, codes in schedule_b.items()}
    ds = df_sim["time"].dt.strftime("%Y-%m-%d")
    df_sim["_in_sched_a"] = [
        str(c).zfill(6) in da.get(d, set()) for c, d in zip(df_sim["code"], ds)
    ]
    df_sim["_in_sched_b"] = [
        str(c).zfill(6) in db.get(d, set()) for c, d in zip(df_sim["code"], ds)
    ]

    rng = np.random.default_rng(seed)

    print("\n" + "=" * 60)
    print("[선정 진단] 선행 수익률 vs Score / Valid (`prepare_dynamic_universe` 동일 스코어)")
    print(f"  기간: {start_date} ~ {end_date}  |  유니버스 종목 수: {len(all_codes)}")
    print(f"  Lean(<180일 단일 유니버스): {duration_days < 180}  |  H일: {horizons}")
    print("=" * 60)

    def summarize_track(
        label: str,
        mask: pd.Series,
        score_col: str,
        valid_col: str,
        sched_col: str,
    ) -> None:
        base = df_sim.loc[mask].copy()
        try:
            base["_sched"] = df_sim.loc[base.index, sched_col].astype(bool)
        except Exception:
            base["_sched"] = False
        print(f"\n--- {label} (행 수: {len(base)}) ---")
        if base.empty:
            print("  (표본 없음)")
            return
        for h in horizons:
            ret_col = f"fwd_ret_{h}d"
            valid = base[ret_col].notna()
            sub = base.loc[valid]
            if sub.empty:
                print(f"  [{ret_col}] 유효 표본 없음(말미 NaN)")
                continue
            mu = float(sub[ret_col].mean())
            med = float(sub[ret_col].median())
            print(f"  [{ret_col}] 평균 {mu:+.3f}%  |  중앙값 {med:+.3f}%  | n={len(sub)}")
            dec = decile_table(sub, score_col, ret_col)
            if dec is not None:
                spread = float(dec["mean_pct"].iloc[-1] - dec["mean_pct"].iloc[0])
                print(
                    f"    분위(점수 낮음→높음): 최저분위 평균 {dec['mean_pct'].iloc[0]:+.3f}% "
                    f"→ 최고분위 {dec['mean_pct'].iloc[-1]:+.3f}% (스프레드 {spread:+.3f}%p)"
                )
            sp = top_bottom_spread(sub, score_col, ret_col)
            if sp is not None:
                print(f"    상위20% vs 하위20% 점수 평균수익 차: {sp:+.3f}%p")

            # 일별 랜덤 베이스라인 (동일 일·동일 n)
            rnd_means: List[float] = []
            for _d_str, chunk in df_sim.groupby(df_sim["time"].dt.strftime("%Y-%m-%d")):
                m_valid = chunk[valid_col] & chunk["_in_univ"]
                rm = random_same_n_baseline(
                    chunk,
                    m_valid,
                    ret_col,
                    chunk["_in_univ"],
                    rng,
                    n_draw=random_draws,
                )
                if rm is not None:
                    rnd_means.append(rm)
            if rnd_means:
                print(
                    f"    동일 일·동일 n 무작위(유니버스) 평균수익: {float(np.mean(rnd_means)):+.3f}% "
                    f"(일자 수 {len(rnd_means)}, draw×일={random_draws})"
                )

            ss = base.loc[valid & base["_sched"]]
            if not ss.empty:
                print(
                    f"    [스케줄 상위만] 평균 {float(ss[ret_col].mean()):+.3f}%  n={len(ss)}"
                )

        # 연도별 Spearman (Score vs fwd)
        h5 = "fwd_ret_5d"
        if h5 in base.columns:
            base_y = base.loc[base[h5].notna(), ["time", score_col, h5]].copy()
            base_y["year"] = base_y["time"].dt.year
            print(f"\n  [{label}] 연도별 Score↔{h5} Spearman (표본 충분할 때만):")
            for yr, gy in base_y.groupby("year"):
                if len(gy) < 40:
                    continue
                sp5 = top_bottom_spread(gy, score_col, h5)
                extra = f", 상하20%스프레드 {sp5:+.3f}%p" if sp5 is not None else ""
                if _spearmanr is not None:
                    try:
                        r, p = _spearmanr(gy[score_col], gy[h5], nan_policy="omit")
                        print(
                            f"    {yr}: rho={float(r):+.4f} p={float(p):.4g} n={len(gy)}{extra}"
                        )
                    except Exception:
                        try:
                            rho = gy[[score_col, h5]].corr(method="spearman").iloc[0, 1]
                            print(
                                f"    {yr}: rho={float(rho):+.4f} n={len(gy)} (보조 경로){extra}"
                            )
                        except Exception as ex:
                            print(f"    {yr}: 상관 계산 생략 ({ex})")
                else:
                    try:
                        rho = gy[[score_col, h5]].corr(method="spearman").iloc[0, 1]
                        print(f"    {yr}: rho={float(rho):+.4f} n={len(gy)} (pandas){extra}")
                    except Exception as ex:
                        print(f"    {yr}: 상관 계산 생략 ({ex})")

    mask_a = df_sim["Valid_A"] & df_sim["_in_univ"]
    summarize_track(
        "Track A (Valid_A)",
        mask_a,
        "Score_A",
        "Valid_A",
        "_in_sched_a",
    )
    mask_b = df_sim["Valid_B"] & df_sim["_in_univ"]
    summarize_track(
        "Track B (Valid_B)",
        mask_b,
        "Score_B",
        "Valid_B",
        "_in_sched_b",
    )

    if csv_path:
        try:
            cols = [
                "time",
                "code",
                "Valid_A",
                "Valid_B",
                "Score_A",
                "Score_B",
                "_in_univ",
                "_in_sched_a",
                "_in_sched_b",
            ] + [f"fwd_ret_{h}d" for h in horizons]
            use = [c for c in cols if c in df_sim.columns]
            df_sim[use].to_csv(csv_path, index=False, encoding="utf-8-sig")
            print(f"\n[저장] {csv_path} ({len(df_sim)}행)")
        except Exception as e:
            print(f"\n[경고] CSV 저장 실패: {e}")

    print("\n" + "=" * 60)
    print("해석 힌트: 분위 스프레드·상관이 0에 가깝고 랜덤과 비슷하면 선정 점수가")
    print("선행 수익에 정보를 거의 주지 않는 것으로 볼 수 있다.")
    print("=" * 60 + "\n")


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    p = argparse.ArgumentParser(
        description="선정일 기준 일봉 선행 수익률 vs Score/Valid 진단"
    )
    p.add_argument("--start", required=True, help="YYYY-MM-DD (분석 구간 시작)")
    p.add_argument("--end", required=True, help="YYYY-MM-DD (분석 구간 끝)")
    p.add_argument(
        "--horizons",
        default="1,5,20",
        help="거래일 수익률 지평(쉼표). 예: 1,5,20",
    )
    p.add_argument(
        "--settings",
        default=None,
        help="settings.json 경로(미지정 시 프로젝트 루트)",
    )
    p.add_argument("--csv", default=None, help="일별·종목별 상세 CSV 저장 경로")
    p.add_argument(
        "--random-draws",
        type=int,
        default=1,
        help="일별 무작위 베이스라인 반복 추출 횟수(평균)",
    )
    p.add_argument("--seed", type=int, default=42, help="무작위 시드")
    args = p.parse_args()
    try:
        hs = [int(x.strip()) for x in str(args.horizons).split(",") if x.strip()]
    except Exception:
        hs = [1, 5, 20]
    hs = [h for h in hs if h > 0]
    if not hs:
        hs = [1, 5, 20]

    settings = _load_merged_settings(args.settings)
    try:
        run_diagnosis(
            settings,
            args.start,
            args.end,
            hs,
            args.csv,
            max(1, int(args.random_draws)),
            int(args.seed),
        )
    except Exception as e:
        print(f"[오류] {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
