# -*- coding: utf-8 -*-
"""
감시 슬롯 수(N)는 고정한 채, **선정 품질**과 **Track↔Sig_Type 교차 거절**만 진단한다.

배경
----
실시간은 계산량·UI 때문에 max_slots_a/b 상한을 유지한다.
따라서 "슬롯을 늘리면?"이 아니라,
같은 N 안에서 Score 선정이 S1에 유리한 종목을 고르는지,
감시 목록 안에서 track_mismatch 로 신호가 얼마나 버려지는지를 본다.

구성
----
A) 일봉 선행수익 (빠름): Valid 풀에서
   - Score 상위 N (현행 스케줄)
   - 동일 N 무작위
   - Score 하위 N (안티스코어)
   의 H거래일 선행수익 평균을 비교한다.

B) 15분 Signal∧Ready (DynamicBacktester 캐시):
   - 감시 목록 안 S1 이벤트 중 track 일치 / 불일치 건수·종목일 수

사용 예
-------
  python tools/diagnose_selection_quality_fixed_n.py --start 2025-01-01 --end 2025-06-30
  python tools/diagnose_selection_quality_fixed_n.py --start 2025-04-01 --end 2025-06-30 --daily-only
  python tools/diagnose_selection_quality_fixed_n.py --start 2025-01-01 --end 2025-03-31 --out tools/bt_compare_out_sel_quality
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

_ABS_ROOT = Path(__file__).resolve().parent.parent
if str(_ABS_ROOT) not in sys.path:
    sys.path.insert(0, str(_ABS_ROOT))
os.chdir(_ABS_ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backtest_engine import TradingStrategy, settings_for_backtest  # noqa: E402
from core.defaults import DEFAULT_SETTINGS  # noqa: E402
from data_manager_v2 import DataManagerV2  # noqa: E402
from dynamic_backtest import (  # noqa: E402
    DynamicBacktester,
    _dual_track_new_candidates,
)
from strategy_selector import (  # noqa: E402
    _df_top_by_score_slot_band,
    load_kodex200_universe_codes,
    load_kospi_universe_codes,
)

# 동일 디렉터리 모듈 로드 (tools 패키지 여부 무관)
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "diagnose_selection_forward_returns",
    str(_ABS_ROOT / "tools" / "diagnose_selection_forward_returns.py"),
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("diagnose_selection_forward_returns.py 로드 실패")
_fwd = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_fwd)
_build_schedule_a_b = _fwd._build_schedule_a_b
add_forward_returns = _fwd.add_forward_returns
load_scored_panel = _fwd.load_scored_panel


def _build_schedule_a_b_safe(
    df_sim: pd.DataFrame,
    settings: dict[str, Any],
    all_codes: List[str],
    duration_days: int,
    univ_cache: Dict[str, frozenset[str]],
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]]]:
    """prepare_dynamic_universe와 동일 슬롯 스케줄(월 유니버스 인자 버그 회피)."""
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
            u_set = set(_monthly_univ(pd.to_datetime(d_str), univ_cache, settings))
        day_mask = (df_sim["time"] == pd.to_datetime(d_str)) & (
            df_sim["code"].astype(str).str.zfill(6).isin(u_set)
        )
        day_a = df_sim.loc[day_mask & df_sim["Valid_A"].astype(bool)]
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
        day_b = df_sim.loc[day_mask & df_sim["Valid_B"].astype(bool)]
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


def _monthly_univ(
    dt: pd.Timestamp,
    cache: Dict[str, frozenset[str]],
    settings: dict[str, Any],
) -> frozenset[str]:
    """월별 유니버스(기존 diagnose 헬퍼의 settings 미전달 버그 회피)."""
    mk = pd.Timestamp(dt).strftime("%Y%m")
    if mk in cache:
        return cache[mk]
    idx_choice = str(settings.get("universe_index_choice", "KODEX200")).strip().upper()
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

logger = logging.getLogger(__name__)


def _load_settings(path: Optional[str]) -> dict[str, Any]:
    """DEFAULT + settings.json + 백테용 Smart Buy/Exit OFF."""
    s: dict[str, Any] = dict(DEFAULT_SETTINGS)
    try:
        p = Path(path) if path else _ABS_ROOT / "settings.json"
        if p.is_file():
            user = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                s.update(user)
    except Exception as e:
        try:
            logger.warning("설정 로드 실패: %s", e)
        except Exception:
            pass
    try:
        s = settings_for_backtest(s)
    except Exception:
        pass
    # 진단은 레짐 게이트 끄고 슬롯 N만 본다
    s["use_market_breadth_gate"] = False
    return s


def _codes_from_top(
    day_df: pd.DataFrame,
    valid_col: str,
    score_col: str,
    max_n: int,
    settings: dict[str, Any],
    floor: float,
    *,
    ascending: bool = False,
) -> List[str]:
    """Valid 행에서 Score 상위(또는 하위) max_n 종목 코드."""
    try:
        sub = day_df.loc[day_df[valid_col].astype(bool)].copy()
        if sub.empty or max_n <= 0:
            return []
        if ascending:
            # 하위 N: 점수 오름차순 상위 N = 최악 Score
            sub = sub.sort_values(score_col, ascending=True, kind="mergesort")
            return (
                sub["code"]
                .astype(str)
                .str.zfill(6)
                .head(max_n)
                .tolist()
            )
        picked = _df_top_by_score_slot_band(
            sub,
            score_col=score_col,
            code_col="code",
            max_n=max_n,
            settings=settings,
            min_score_floor=floor,
        )
        if picked is None or picked.empty:
            return []
        return picked["code"].astype(str).str.zfill(6).tolist()
    except Exception as e:
        try:
            logger.warning("codes_from_top 실패: %s", e)
        except Exception:
            pass
        return []


def _mean_fwd(
    day_df: pd.DataFrame, codes: Sequence[str], ret_col: str
) -> Optional[float]:
    """해당 일자·코드들의 선행수익 평균(%)."""
    if not codes or ret_col not in day_df.columns:
        return None
    try:
        cset = {str(c).zfill(6) for c in codes}
        m = day_df["code"].astype(str).str.zfill(6).isin(cset)
        vals = day_df.loc[m, ret_col].replace([np.inf, -np.inf], np.nan).dropna()
        if vals.empty:
            return None
        return float(vals.mean())
    except Exception:
        return None


def _random_same_n(
    day_df: pd.DataFrame,
    valid_col: str,
    ret_col: str,
    n: int,
    rng: np.random.Generator,
    n_draw: int,
) -> Optional[float]:
    """Valid 풀에서 동일 n종 무작위 추출(여러 번) 후 선행수익 평균."""
    try:
        pool = day_df.loc[
            day_df[valid_col].astype(bool) & day_df[ret_col].notna()
        ].copy()
        if pool.empty or n <= 0:
            return None
        codes = pool["code"].astype(str).str.zfill(6).to_numpy()
        arr = pool[ret_col].to_numpy(dtype=float)
        nn = min(int(n), len(codes))
        means: List[float] = []
        for _ in range(max(1, int(n_draw))):
            if nn >= len(codes):
                pick = np.arange(len(codes))
            else:
                pick = rng.choice(len(codes), size=nn, replace=False)
            means.append(float(np.nanmean(arr[pick])))
        return float(np.mean(means)) if means else None
    except Exception:
        return None


def run_part_a_daily(
    settings: dict[str, Any],
    start: str,
    end: str,
    horizons: List[int],
    random_draws: int,
    seed: int,
) -> Tuple[pd.DataFrame, dict[str, Any]]:
    """
    N 고정 일봉 선정 품질.
    반환: 일자×트랙 상세 DF, 요약 dict.
    """
    dm = DataManagerV2()
    df_sim, all_codes, duration_days = load_scored_panel(settings, start, end, dm)
    df_sim = add_forward_returns(df_sim, horizons)
    univ_cache: Dict[str, frozenset[str]] = {}
    if duration_days >= 180:
        for mkey in df_sim["time"].dt.strftime("%Y%m").unique():
            _monthly_univ(pd.Timestamp(str(mkey) + "01"), univ_cache, settings)
        df_sim["_in_univ"] = [
            str(c).zfill(6) in _monthly_univ(t, univ_cache, settings)
            for c, t in zip(df_sim["code"], df_sim["time"])
        ]
        df_sim = df_sim.loc[df_sim["_in_univ"]].copy()

    max_sa = int(settings.get("max_slots_a", 20))
    max_sb = int(settings.get("max_slots_b", 15))
    floor_sa = max(0.0, float(settings.get("sel_track_min_score_a", 0.0) or 0.0))
    floor_sb = max(0.0, float(settings.get("sel_track_min_score_b", 0.0) or 0.0))
    schedule_a, schedule_b = _build_schedule_a_b_safe(
        df_sim, settings, all_codes, duration_days, univ_cache
    )
    rng = np.random.default_rng(int(seed))

    rows: List[dict[str, Any]] = []
    dates = sorted(df_sim["time"].dt.strftime("%Y-%m-%d").unique().tolist())
    for d_str in dates:
        day = df_sim.loc[df_sim["time"] == pd.to_datetime(d_str)]
        if day.empty:
            continue
        n_valid_a = int(day["Valid_A"].astype(bool).sum()) if "Valid_A" in day.columns else 0
        n_valid_b = int(day["Valid_B"].astype(bool).sum()) if "Valid_B" in day.columns else 0
        sel_a = list(schedule_a.get(d_str, []))
        sel_b = list(schedule_b.get(d_str, []))
        # 스케줄이 비면 Score로 재구성(동일 로직)
        if not sel_a and n_valid_a > 0:
            sel_a = _codes_from_top(day, "Valid_A", "Score_A", max_sa, settings, floor_sa)
        if not sel_b and n_valid_b > 0:
            sel_b = _codes_from_top(day, "Valid_B", "Score_B", max_sb, settings, floor_sb)
        bot_a = _codes_from_top(
            day, "Valid_A", "Score_A", max_sa, settings, 0.0, ascending=True
        )
        bot_b = _codes_from_top(
            day, "Valid_B", "Score_B", max_sb, settings, 0.0, ascending=True
        )

        row: dict[str, Any] = {
            "date": d_str,
            "n_valid_a": n_valid_a,
            "n_valid_b": n_valid_b,
            "n_sel_a": len(sel_a),
            "n_sel_b": len(sel_b),
            "n_slots_a": max_sa,
            "n_slots_b": max_sb,
        }
        for h in horizons:
            # diagnose_selection_forward_returns.add_forward_returns 컬럼명
            col = f"fwd_ret_{h}d"
            if col not in day.columns:
                continue
            top_a = _mean_fwd(day, sel_a, col)
            rnd_a = _random_same_n(day, "Valid_A", col, len(sel_a) or max_sa, rng, random_draws)
            bot_ma = _mean_fwd(day, bot_a, col)
            top_b = _mean_fwd(day, sel_b, col)
            rnd_b = _random_same_n(day, "Valid_B", col, len(sel_b) or max_sb, rng, random_draws)
            bot_mb = _mean_fwd(day, bot_b, col)
            row[f"a_top_{h}d"] = top_a
            row[f"a_rnd_{h}d"] = rnd_a
            row[f"a_bot_{h}d"] = bot_ma
            row[f"a_edge_vs_rnd_{h}d"] = (
                (top_a - rnd_a) if (top_a is not None and rnd_a is not None) else None
            )
            row[f"a_edge_vs_bot_{h}d"] = (
                (top_a - bot_ma) if (top_a is not None and bot_ma is not None) else None
            )
            row[f"b_top_{h}d"] = top_b
            row[f"b_rnd_{h}d"] = rnd_b
            row[f"b_bot_{h}d"] = bot_mb
            row[f"b_edge_vs_rnd_{h}d"] = (
                (top_b - rnd_b) if (top_b is not None and rnd_b is not None) else None
            )
            row[f"b_edge_vs_bot_{h}d"] = (
                (top_b - bot_mb) if (top_b is not None and bot_mb is not None) else None
            )
        rows.append(row)

    detail = pd.DataFrame(rows)
    summary: dict[str, Any] = {
        "max_slots_a": max_sa,
        "max_slots_b": max_sb,
        "n_days": int(len(detail)),
        "avg_n_sel_a": float(detail["n_sel_a"].mean()) if not detail.empty else 0.0,
        "avg_n_sel_b": float(detail["n_sel_b"].mean()) if not detail.empty else 0.0,
        "avg_n_valid_a": float(detail["n_valid_a"].mean()) if not detail.empty else 0.0,
        "avg_n_valid_b": float(detail["n_valid_b"].mean()) if not detail.empty else 0.0,
        "edges": {},
    }
    for h in horizons:
        for track, key in (("A", "a"), ("B", "b")):
            er = f"{key}_edge_vs_rnd_{h}d"
            eb = f"{key}_edge_vs_bot_{h}d"
            tr = f"{key}_top_{h}d"
            rr = f"{key}_rnd_{h}d"
            br = f"{key}_bot_{h}d"
            summary["edges"][f"{track}_H{h}"] = {
                "top_mean_pct": float(detail[tr].mean()) if tr in detail and detail[tr].notna().any() else None,
                "rnd_mean_pct": float(detail[rr].mean()) if rr in detail and detail[rr].notna().any() else None,
                "bot_mean_pct": float(detail[br].mean()) if br in detail and detail[br].notna().any() else None,
                "edge_vs_rnd_pctp": float(detail[er].mean()) if er in detail and detail[er].notna().any() else None,
                "edge_vs_bot_pctp": float(detail[eb].mean()) if eb in detail and detail[eb].notna().any() else None,
                "pct_days_top_beats_rnd": (
                    float((detail[er] > 0).mean() * 100.0)
                    if er in detail and detail[er].notna().any()
                    else None
                ),
            }
    return detail, summary


def _track_allows(sig_type: str, code_track: str) -> bool:
    """동적 백테와 동일: A↔B 교차만 거절, AB는 둘 다 허용."""
    st = str(sig_type or "").upper()
    ct = str(code_track or "A").upper()
    if st == "TYPE_A" and ct == "B":
        return False
    if st == "TYPE_B" and ct == "A":
        return False
    return True


def run_part_b_track_mismatch(
    settings: dict[str, Any],
    start: str,
    end: str,
) -> Tuple[pd.DataFrame, dict[str, Any]]:
    """
    감시 목록(N 고정) 안에서 Signal∧Ready 의 track 일치/불일치.
    """
    bt = DynamicBacktester(settings)
    t0 = time.time()

    def _cb(msg: str, pct: Any = None) -> None:
        try:
            if pct is not None:
                print(f"  [15m] {msg} ({pct}%)")
            else:
                print(f"  [15m] {msg}")
        except Exception:
            pass

    ok = bt._bootstrap_portfolio_data(start, end, _cb)
    if not ok:
        return pd.DataFrame(), {"error": "bootstrap 실패"}

    trading_days = list(getattr(bt, "_bt_trading_days", []) or [])
    max_sa = int(settings.get("max_slots_a", 20))
    max_sb = int(settings.get("max_slots_b", 15))
    day_rows: List[dict[str, Any]] = []
    # 이벤트 단위 집계
    tot_sr = 0
    tot_ok = 0
    tot_mis = 0
    # 종목-일 단위(같은 날 여러 봉 신호는 1회로)
    code_day_ok: set[Tuple[str, str]] = set()
    code_day_mis: set[Tuple[str, str]] = set()
    mis_by_pair: Dict[str, int] = {
        "TYPE_A_on_B": 0,
        "TYPE_B_on_A": 0,
    }

    for idx, current_day in enumerate(trading_days):
        prev_day_idx = idx - 1
        ref_day = (
            trading_days[prev_day_idx]
            if prev_day_idx >= 0
            else (getattr(bt, "_bt_ref_day_for_first", None) or current_day)
        )
        top_a = list(bt.schedule_a.get(ref_day, []) or [])
        top_b = list(bt.schedule_b.get(ref_day, []) or [])
        track_map: Dict[str, str] = {}
        for c in top_a:
            track_map[str(c).zfill(6)] = "A"
        for c in top_b:
            cc = str(c).zfill(6)
            if cc in track_map:
                track_map[cc] = "AB"
            else:
                track_map[cc] = "B"
        monitoring = _dual_track_new_candidates(top_a, top_b, [], max_sa, max_sb)
        monitoring = [str(c).zfill(6) for c in monitoring]

        n_sr = 0
        n_ok = 0
        n_mis = 0
        n_codes_sr = 0
        for code in monitoring:
            if code not in bt.cached_15m_data:
                continue
            base_df = bt.cached_15m_data[code]
            try:
                day_full = base_df.loc[current_day : current_day + " 23:59:59"]
            except Exception:
                continue
            if day_full is None or day_full.empty:
                continue
            if "Signal" not in day_full.columns or "Ready" not in day_full.columns:
                continue
            try:
                mask = day_full["Signal"].astype(bool) & day_full["Ready"].astype(bool)
            except Exception:
                continue
            if not bool(mask.any()):
                continue
            n_codes_sr += 1
            ct = track_map.get(code, "A")
            for _, row in day_full.loc[mask].iterrows():
                try:
                    sig_type = TradingStrategy.effective_s1_sig_type_for_buy(
                        settings, row, {}
                    )
                except Exception:
                    sig_type = str(row.get("Sig_Type", "TYPE_B"))
                n_sr += 1
                tot_sr += 1
                if _track_allows(sig_type, ct):
                    n_ok += 1
                    tot_ok += 1
                    code_day_ok.add((current_day, code))
                else:
                    n_mis += 1
                    tot_mis += 1
                    code_day_mis.add((current_day, code))
                    if str(sig_type).upper() == "TYPE_A" and ct == "B":
                        mis_by_pair["TYPE_A_on_B"] += 1
                    elif str(sig_type).upper() == "TYPE_B" and ct == "A":
                        mis_by_pair["TYPE_B_on_A"] += 1

        day_rows.append(
            {
                "date": current_day,
                "ref_day": ref_day,
                "n_monitor": len(monitoring),
                "n_codes_with_sr": n_codes_sr,
                "n_sr_events": n_sr,
                "n_sr_track_ok": n_ok,
                "n_sr_track_mismatch": n_mis,
                "mismatch_event_pct": (100.0 * n_mis / n_sr) if n_sr else 0.0,
            }
        )

    detail = pd.DataFrame(day_rows)
    elapsed = time.time() - t0
    # 종목-일: mismatch만 있고 ok 없는 경우 = 그날 그 종목 S1이 전부 교차거절
    only_mis = code_day_mis - code_day_ok
    both = code_day_mis & code_day_ok
    summary = {
        "elapsed_sec": round(float(elapsed), 1),
        "n_days": int(len(detail)),
        "avg_n_monitor": float(detail["n_monitor"].mean()) if not detail.empty else 0.0,
        "sr_events_total": int(tot_sr),
        "sr_events_track_ok": int(tot_ok),
        "sr_events_track_mismatch": int(tot_mis),
        "mismatch_event_pct": (100.0 * tot_mis / tot_sr) if tot_sr else 0.0,
        "code_days_with_ok": len(code_day_ok),
        "code_days_with_mismatch": len(code_day_mis),
        "code_days_mismatch_only": len(only_mis),
        "code_days_both_ok_and_mis": len(both),
        "mismatch_pairs": mis_by_pair,
        "avg_daily_mismatch_event_pct": (
            float(detail["mismatch_event_pct"].mean()) if not detail.empty else 0.0
        ),
    }
    return detail, summary


def _fmt(v: Any, digits: int = 3) -> str:
    try:
        if v is None:
            return "n/a"
        return f"{float(v):.{digits}f}"
    except Exception:
        return str(v)


def _print_part_a(summary: dict[str, Any]) -> None:
    print("\n" + "=" * 72)
    print("A) N 고정 · 일봉 선정 품질 (Valid 풀 안에서 Score 상위 vs 무작위 vs 하위)")
    print("=" * 72)
    print(
        f"  슬롯 N: A={summary.get('max_slots_a')} / B={summary.get('max_slots_b')}  "
        f"(일수={summary.get('n_days')})"
    )
    print(
        f"  평균 선정: A={_fmt(summary.get('avg_n_sel_a'), 1)} "
        f"(Valid≈{_fmt(summary.get('avg_n_valid_a'), 1)})  "
        f"B={_fmt(summary.get('avg_n_sel_b'), 1)} "
        f"(Valid≈{_fmt(summary.get('avg_n_valid_b'), 1)})"
    )
    sat_a = float(summary.get("avg_n_valid_a") or 0) <= float(summary.get("max_slots_a") or 1) * 0.95
    sat_b = float(summary.get("avg_n_valid_b") or 0) <= float(summary.get("max_slots_b") or 1) * 0.95
    if sat_a or sat_b:
        print(
            "  ※ Valid 수가 슬롯 N보다 작거나 비슷 → Score 순위 차별이 거의 없음 "
            f"(A포화={sat_a}, B포화={sat_b}). 병목은 랭킹이 아니라 Valid 필터."
        )
    edges = summary.get("edges") or {}
    for key, block in edges.items():
        if not isinstance(block, dict):
            continue
        print(f"\n  [{key}]")
        print(
            f"    Score상위: {_fmt(block.get('top_mean_pct'))}%  |  "
            f"무작위 동일N: {_fmt(block.get('rnd_mean_pct'))}%  |  "
            f"Score하위: {_fmt(block.get('bot_mean_pct'))}%"
        )
        print(
            f"    edge vs 무작위: {_fmt(block.get('edge_vs_rnd_pctp'))} %p  |  "
            f"edge vs 하위: {_fmt(block.get('edge_vs_bot_pctp'))} %p  |  "
            f"상위>무작위 일수: {_fmt(block.get('pct_days_top_beats_rnd'), 1)}%"
        )
    print(
        "\n  해석: Valid≪N 이면 Score edge는 의미 없음(전원 선정).\n"
        "        Valid≫N 인데 edge≈0 이면 같은 N에서 스코어 개선 여지.\n"
        "        edge≪0 이면 Score가 오히려 불리한 쪽을 고르는 중."
    )


def _print_part_b(summary: dict[str, Any]) -> None:
    print("\n" + "=" * 72)
    print("B) N 고정 · 감시목록 안 Track↔Sig_Type 교차 거절")
    print("=" * 72)
    if summary.get("error"):
        print(f"  실패: {summary['error']}")
        return
    print(
        f"  일수={summary.get('n_days')}  평균감시={summary.get('avg_n_monitor'):.1f}  "
        f"소요={summary.get('elapsed_sec')}s"
    )
    print(
        f"  Signal∧Ready 이벤트: {summary.get('sr_events_total')}  "
        f"(일치 {summary.get('sr_events_track_ok')} / "
        f"불일치 {summary.get('sr_events_track_mismatch')} = "
        f"{summary.get('mismatch_event_pct'):.1f}%)"
    )
    print(
        f"  종목-일: 일치≥1 {summary.get('code_days_with_ok')}  |  "
        f"불일치≥1 {summary.get('code_days_with_mismatch')}  |  "
        f"불일치만(그날 S1 전량 거절) {summary.get('code_days_mismatch_only')}"
    )
    pairs = summary.get("mismatch_pairs") or {}
    print(
        f"  불일치 내역: TYPE_A on TrackB={pairs.get('TYPE_A_on_B', 0)}  "
        f"TYPE_B on TrackA={pairs.get('TYPE_B_on_A', 0)}"
    )
    print(
        "\n  해석: mismatch 비율이 크면, N을 늘리지 않고도 "
        "트랙 게이트 완화/AB 비중 확대가 S1 기회를 살릴 수 있음."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="N고정 선정품질·track_mismatch 진단")
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2025-06-30")
    ap.add_argument("--horizons", default="1,5", help="선행수익 거래일 예: 1,5")
    ap.add_argument("--random-draws", type=int, default=40)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--settings", default=None)
    ap.add_argument(
        "--daily-only",
        action="store_true",
        help="일봉 Part A만 (15분 로드 생략)",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="결과 CSV/JSON 저장 디렉터리",
    )
    args = ap.parse_args()

    try:
        horizons = [int(x.strip()) for x in str(args.horizons).split(",") if x.strip()]
    except Exception:
        horizons = [1, 5]
    if not horizons:
        horizons = [1, 5]

    settings = _load_settings(args.settings)
    print(
        f"[진단] 기간 {args.start} ~ {args.end}  |  "
        f"slots A={settings.get('max_slots_a')} B={settings.get('max_slots_b')}  |  "
        f"daily_only={bool(args.daily_only)}"
    )

    t0 = time.time()
    detail_a, sum_a = run_part_a_daily(
        settings,
        args.start,
        args.end,
        horizons,
        int(args.random_draws),
        int(args.seed),
    )
    _print_part_a(sum_a)

    detail_b = pd.DataFrame()
    sum_b: dict[str, Any] = {}
    if not args.daily_only:
        print("\n[진단] 15분 캐시 로드 후 track_mismatch 집계 중…")
        detail_b, sum_b = run_part_b_track_mismatch(settings, args.start, args.end)
        _print_part_b(sum_b)

    out_dir = Path(args.out) if args.out else None
    if out_dir is None:
        out_dir = _ABS_ROOT / "tools" / "bt_compare_out_sel_quality"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        detail_a.to_csv(out_dir / "part_a_daily_edges.csv", index=False, encoding="utf-8-sig")
        (out_dir / "part_a_summary.json").write_text(
            json.dumps(sum_a, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if not detail_b.empty:
            detail_b.to_csv(
                out_dir / "part_b_track_mismatch_daily.csv",
                index=False,
                encoding="utf-8-sig",
            )
            (out_dir / "part_b_summary.json").write_text(
                json.dumps(sum_b, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(f"\n[저장] {out_dir}")
    except Exception as e:
        print(f"[경고] 결과 저장 실패: {e}")

    print(f"\n[완료] 총 소요 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("중단됨", file=sys.stderr)
        raise SystemExit(130)
    except Exception as e:
        print(f"[Error] {e}", file=sys.stderr)
        raise
