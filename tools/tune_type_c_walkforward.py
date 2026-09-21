# -*- coding: utf-8 -*-
"""TYPE_C 진입·청산 파라미터를 캐시 기반 워크포워드로 제한 탐색합니다."""

from __future__ import annotations

import gc
import gzip
import json
import os
import pickle
import sys
import time
import traceback
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from backtest_engine import TradingStrategy, settings_for_backtest  # noqa: E402
from core.defaults import DEFAULT_SETTINGS  # noqa: E402
from dynamic_backtest import DynamicBacktester  # noqa: E402

FULL_START = "2024-06-03"
FULL_END = "2026-09-18"
TRAIN = ("2024-06-03", "2025-06-30")
VALID = ("2025-07-01", "2026-03-31")
HOLDOUT = ("2026-04-01", "2026-09-18")

OUT = ROOT / "tools" / "bt_type_c_tuning_v2"
CONTEXT_PATH = ROOT / "tools" / "bt_type_c_tuning" / "type_c_context_v1.pkl.gz"
SUMMARY_PATH = OUT / "summary.json"
CONTEXT_VERSION = 1

ENTRY_CANDIDATES: list[Dict[str, Any]] = [
    {"id": "BASE", "overrides": {}},
    {
        "id": "CONFIRM2",
        "overrides": {"type_c_min_candle_confirmations": 2},
    },
    {
        "id": "LOW_ENTRY25",
        "overrides": {"type_c_max_box_position_pct": 25.0},
    },
    {
        "id": "RECENT_RECLAIM",
        "overrides": {
            "type_c_touch_lookback_bars": 2,
            "type_c_reclaim_atr_buffer": 0.2,
        },
    },
    {
        "id": "QUALITY",
        "overrides": {
            "type_c_min_candle_confirmations": 2,
            "type_c_max_box_position_pct": 30.0,
            "type_c_min_volume_ratio": 1.0,
            "type_c_touch_lookback_bars": 2,
            "type_c_reclaim_atr_buffer": 0.15,
        },
    },
]

EXIT_CANDIDATES: list[Dict[str, Any]] = [
    {"id": "EXIT_BASE", "overrides": {}},
    {
        "id": "TARGET04",
        "overrides": {"type_c_target_atr_buffer": 0.4},
    },
    {
        "id": "STOP07",
        "overrides": {"type_c_stop_atr_tolerance": 0.7},
    },
    {
        "id": "TARGET04_STOP07",
        "overrides": {
            "type_c_target_atr_buffer": 0.4,
            "type_c_stop_atr_tolerance": 0.7,
        },
    },
    {
        "id": "TARGET04_HOLD7",
        "overrides": {
            "type_c_target_atr_buffer": 0.4,
            "type_c_max_holding_days": 7,
        },
    },
]

SELECTION_SIGNATURE_KEYS = (
    "universe_index_choice",
    "kospi_universe_refresh",
    "kospi_top_n",
    "bt_universe_indicator_lookback_days",
    "bt_universe_schedule_warmup_trading_days",
    "use_transition_gate",
    "transition_ignore_soft",
    "transition_block_s1_on_soft",
    "transition_block_s2_on_soft",
    "box_sel_lookback",
    "box_sel_min_history_bars",
    "box_sel_atr_period",
    "box_sel_adx_period",
    "box_sel_ma_period",
    "box_sel_ma_slope_days",
    "box_sel_min_avg_trde_value_krw",
    "box_sel_min_price",
    "box_sel_min_box_width_pct",
    "box_sel_max_box_width_pct",
    "box_sel_max_adx",
    "box_sel_max_ma_slope_pct_per_day",
    "box_sel_min_lower_touches",
    "box_sel_min_upper_touches",
    "box_sel_touch_atr_tolerance",
    "box_sel_breakout_atr_tolerance",
    "box_sel_max_breakout_closes",
    "box_sel_max_volume_ratio",
    "box_sel_max_atr_expansion_ratio",
    "box_sel_lower_quantile",
    "box_sel_upper_quantile",
    "max_slots_c",
    # 15분 지표 캐시를 바꾸는 설정도 동일 서명으로 보호합니다.
    "type_c_atr_period",
    "bb_period",
    "use_synthetic_realtime",
)


def _base_settings() -> Dict[str, Any]:
    """기본값과 사용자 설정을 합친 TYPE_C 단독 설정을 반환합니다."""
    raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    merged = dict(DEFAULT_SETTINGS)
    merged.update(raw)
    settings = settings_for_backtest(merged)
    settings.update(
        {
            "bt_high_precision": False,
            "use_type_a": False,
            "use_type_b": False,
            "use_type_c": True,
            "use_sel_track_a": False,
            "use_sel_track_b": False,
            "use_sel_track_c": True,
            "max_slots_a": 0,
            "max_slots_b": 0,
            "max_slots_c": 15,
            "use_s2_master_a": False,
            "use_s2_master_b": False,
            "enforce_track_signal_match": True,
            # 기존 장기 백테스트와 완전히 같은 인과 이력을 유지합니다.
            "type_c_signal_history_bars": 400,
        }
    )
    return settings


def _selection_signature(settings: Dict[str, Any]) -> Dict[str, Any]:
    """컨텍스트 재사용 가능 여부를 판단할 선정 설정 서명을 만듭니다."""
    return {key: settings.get(key) for key in SELECTION_SIGNATURE_KEYS}


def _build_context(settings: Dict[str, Any]) -> Dict[str, Any]:
    """전체 구간의 선정 스케줄과 15분 지표를 한 번 계산합니다."""
    print("[Context] 전체 TYPE_C 스케줄·15분 지표 생성 시작")
    started = time.time()
    tester = DynamicBacktester(deepcopy(settings))
    tester.high_precision = False
    tester.start_date = FULL_START
    tester.end_date = FULL_END
    tester._cache_run_hot_settings()
    tester.stock_name_map = TradingStrategy.get_stock_name_map(
        settings.get("universe_index_choice", "KODEX200")
    )
    if not tester._bootstrap_portfolio_data(FULL_START, FULL_END, None):
        raise RuntimeError("TYPE_C 튜닝 컨텍스트 생성 실패")

    context = {
        "version": CONTEXT_VERSION,
        "start_date": FULL_START,
        "end_date": FULL_END,
        "selection_signature": _selection_signature(settings),
        "schedule_a": tester.schedule_a,
        "schedule_b": tester.schedule_b,
        "schedule_c": tester.schedule_c,
        "schedule_c_meta": tester.schedule_c_meta,
        "transition_regime_by_day": tester.transition_regime_by_day,
        "index_dd_block_by_day": tester.index_dd_block_by_day,
        "cached_15m_data": tester.cached_15m_data,
        "build_elapsed_sec": round(time.time() - started, 1),
    }
    temp_path = CONTEXT_PATH.with_suffix(CONTEXT_PATH.suffix + ".tmp")
    try:
        with gzip.open(temp_path, "wb", compresslevel=1) as stream:
            pickle.dump(context, stream, protocol=pickle.HIGHEST_PROTOCOL)
        temp_path.replace(CONTEXT_PATH)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
    print(
        f"[Context] 생성 완료: {context['build_elapsed_sec']}초, "
        f"{len(context['cached_15m_data'])}종목"
    )
    return context


def _load_or_build_context(settings: Dict[str, Any]) -> Dict[str, Any]:
    """유효한 디스크 컨텍스트를 불러오고, 없으면 새로 생성합니다."""
    if CONTEXT_PATH.is_file():
        try:
            print(f"[Context] 캐시 로드: {CONTEXT_PATH}")
            with gzip.open(CONTEXT_PATH, "rb") as stream:
                context = pickle.load(stream)
            valid = (
                isinstance(context, dict)
                and context.get("version") == CONTEXT_VERSION
                and context.get("start_date") == FULL_START
                and context.get("end_date") == FULL_END
                and context.get("selection_signature")
                == _selection_signature(settings)
                and isinstance(context.get("cached_15m_data"), dict)
            )
            if valid:
                print(
                    f"[Context] 재사용: {len(context['cached_15m_data'])}종목"
                )
                return context
            print("[Context] 설정 서명 불일치로 재생성합니다.")
        except (OSError, EOFError, pickle.PickleError, TypeError, ValueError):
            traceback.print_exc()
    return _build_context(settings)


def _metrics(
    tester: DynamicBacktester,
    trades: Optional[pd.DataFrame],
) -> Dict[str, Any]:
    """목적함수에 필요한 비용 반영 성과를 계산합니다."""
    equity = pd.DataFrame(tester.daily_equity)
    if equity.empty:
        return {
            "return_pct": -100.0,
            "max_dd_pct": -100.0,
            "n_exits": 0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
        }
    equity["equity"] = pd.to_numeric(equity["equity"], errors="coerce")
    equity.dropna(subset=["equity"], inplace=True)
    initial = float(tester.initial_cash)
    end_equity = float(equity.iloc[-1]["equity"])
    drawdown = equity["equity"] / equity["equity"].cummax() - 1.0

    frame = trades if isinstance(trades, pd.DataFrame) else pd.DataFrame()
    exits = (
        frame[frame["Action"].isin(["SELL", "HOLD"])].copy()
        if not frame.empty
        else pd.DataFrame()
    )
    returns = (
        pd.to_numeric(exits["Return"], errors="coerce").dropna()
        if not exits.empty
        else pd.Series(dtype=float)
    )
    positive = float(returns[returns > 0].sum())
    negative = abs(float(returns[returns < 0].sum()))
    return {
        "return_pct": round((end_equity / initial - 1.0) * 100.0, 4),
        "max_dd_pct": round(float(drawdown.min()) * 100.0, 4),
        "end_equity": round(end_equity, 0),
        "n_buy1": int((frame.get("Action") == "BUY1").sum()) if not frame.empty else 0,
        "n_exits": int(len(exits)),
        "win_rate_pct": round(
            float((returns > 0).mean() * 100.0) if len(returns) else 0.0,
            2,
        ),
        "avg_exit_return_pct": round(
            float(returns.mean()) if len(returns) else 0.0,
            4,
        ),
        "profit_factor": round(
            positive / negative if negative > 0 else (999.0 if positive > 0 else 0.0),
            4,
        ),
        "fees_krw": round(
            float(pd.to_numeric(frame.get("Fee"), errors="coerce").fillna(0).sum())
            if not frame.empty and "Fee" in frame
            else 0.0,
            0,
        ),
    }


def _run_period(
    run_id: str,
    period_name: str,
    period: tuple[str, str],
    settings: Dict[str, Any],
    context: Dict[str, Any],
    signal_cache: Dict[Any, pd.DataFrame],
) -> Dict[str, Any]:
    """한 파라미터 조합·한 구간을 실행하며 완료 결과는 재사용합니다."""
    run_dir = OUT / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    result_path = run_dir / f"{period_name}.json"
    if result_path.is_file():
        try:
            cached = json.loads(result_path.read_text(encoding="utf-8"))
            if cached.get("start") == period[0] and cached.get("end") == period[1]:
                print(f"[Run] 캐시 재사용 {run_id}/{period_name}")
                return cached
        except (OSError, ValueError, TypeError):
            pass

    tester = DynamicBacktester(deepcopy(settings))
    tester._precomputed_bt_context = context
    tester._type_c_signal_cache = signal_cache
    tester._suppress_backtest_artifacts = True
    tester._quiet_backtest = True
    started = time.time()
    print(f"[Run] {run_id}/{period_name} {period[0]}~{period[1]}")
    trades = tester.run(
        period[0],
        period[1],
        interval="15m",
        callback=None,
        high_precision=False,
    )
    result = {
        "run_id": run_id,
        "period": period_name,
        "start": period[0],
        "end": period[1],
        "elapsed_sec": round(time.time() - started, 1),
        **_metrics(tester, trades if isinstance(trades, pd.DataFrame) else None),
    }
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"  ret={result['return_pct']:+.2f}% "
        f"DD={result['max_dd_pct']:.2f}% "
        f"N={result['n_exits']} PF={result['profit_factor']:.2f} "
        f"({result['elapsed_sec']:.1f}s)"
    )
    return result


def _objective(train: Dict[str, Any], valid: Dict[str, Any]) -> float:
    """홀드아웃을 보지 않고 학습·검증 안정성을 점수화합니다."""
    if int(train.get("n_exits", 0)) < 25 or int(valid.get("n_exits", 0)) < 15:
        return -1000.0
    score = (
        float(valid.get("return_pct", -100.0))
        + 0.25 * float(train.get("return_pct", -100.0))
        - 0.30 * abs(float(valid.get("max_dd_pct", -100.0)))
    )
    valid_pf = float(valid.get("profit_factor", 0.0))
    if valid_pf < 1.10:
        score -= (1.10 - valid_pf) * 5.0
    if float(train.get("return_pct", -100.0)) < 0:
        # 학습 손실을 검증 우연으로 덮는 후보는 진입 상위 후보에서 제외합니다.
        score -= 100.0
    return round(score, 6)


def _passes_pre_holdout(
    train: Dict[str, Any],
    valid: Dict[str, Any],
) -> bool:
    """홀드아웃 확인 전에 충족해야 하는 최소 강건성 조건입니다."""
    return bool(
        float(train.get("return_pct", -100.0)) > 0
        and float(valid.get("return_pct", -100.0)) > 0
        and float(valid.get("profit_factor", 0.0)) >= 1.20
        and int(train.get("n_exits", 0)) + int(valid.get("n_exits", 0)) >= 70
        and float(valid.get("max_dd_pct", -100.0)) >= -8.0
    )


def _evaluate_configuration(
    run_id: str,
    settings: Dict[str, Any],
    context: Dict[str, Any],
    signal_cache: Dict[Any, pd.DataFrame],
) -> Dict[str, Any]:
    """학습·검증 구간을 실행하고 목적함수를 반환합니다."""
    train = _run_period(run_id, "train", TRAIN, settings, context, signal_cache)
    valid = _run_period(run_id, "valid", VALID, settings, context, signal_cache)
    return {
        "id": run_id,
        "settings": {
            key: settings.get(key)
            for key in (
                "type_c_min_candle_confirmations",
                "type_c_max_box_position_pct",
                "type_c_min_volume_ratio",
                "type_c_touch_lookback_bars",
                "type_c_reclaim_atr_buffer",
                "type_c_target_atr_buffer",
                "type_c_stop_atr_tolerance",
                "type_c_max_holding_days",
            )
        },
        "train": train,
        "valid": valid,
        "objective": _objective(train, valid),
        "passes_pre_holdout": _passes_pre_holdout(train, valid),
    }


def _write_summary(payload: Dict[str, Any]) -> None:
    """중단 후 재개할 수 있도록 현재 요약을 원자적으로 저장합니다."""
    temp = SUMMARY_PATH.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp.replace(SUMMARY_PATH)


def _entry_by_id(entry_id: str) -> Dict[str, Any]:
    """ID로 진입 후보를 반환합니다."""
    for candidate in ENTRY_CANDIDATES:
        if candidate["id"] == entry_id:
            return candidate
    raise KeyError(f"알 수 없는 진입 후보: {entry_id}")


def _top_unique(
    rows: Iterable[Dict[str, Any]],
    limit: int,
) -> list[Dict[str, Any]]:
    """목적함수 상위의 서로 다른 설정을 반환합니다."""
    selected: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for row in sorted(rows, key=lambda item: item["objective"], reverse=True):
        signature = json.dumps(row.get("settings", {}), sort_keys=True)
        if signature in seen:
            continue
        seen.add(signature)
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def main() -> None:
    """제한 탐색 후 상위 후보만 최종 홀드아웃에서 평가합니다."""
    OUT.mkdir(parents=True, exist_ok=True)
    base = _base_settings()
    context = _load_or_build_context(base)

    entry_rows: list[Dict[str, Any]] = []
    print("\n[Stage 1] 진입 조건 탐색")
    for candidate in ENTRY_CANDIDATES:
        settings = deepcopy(base)
        settings.update(candidate["overrides"])
        # 후보마다 별도 캐시를 써 설정 간 신호 오염을 방지합니다.
        signal_cache: Dict[Any, pd.DataFrame] = {}
        row = _evaluate_configuration(
            candidate["id"], settings, context, signal_cache
        )
        row["entry_id"] = candidate["id"]
        row["exit_id"] = "EXIT_BASE"
        entry_rows.append(row)
        signal_cache.clear()
        gc.collect()
        _write_summary({"status": "entry", "entry_results": entry_rows})

    top_entries = _top_unique(entry_rows, 2)
    print("\n[Stage 2] 상위 진입 후보의 청산 조건 탐색")
    tuned_rows: list[Dict[str, Any]] = []
    for entry_row in top_entries:
        entry_id = str(entry_row["entry_id"])
        entry_candidate = _entry_by_id(entry_id)
        signal_cache = {}
        for exit_candidate in EXIT_CANDIDATES:
            settings = deepcopy(base)
            settings.update(entry_candidate["overrides"])
            settings.update(exit_candidate["overrides"])
            run_id = f"{entry_id}__{exit_candidate['id']}"
            row = _evaluate_configuration(
                run_id, settings, context, signal_cache
            )
            row["entry_id"] = entry_id
            row["exit_id"] = exit_candidate["id"]
            tuned_rows.append(row)
            _write_summary(
                {
                    "status": "exit",
                    "entry_results": entry_rows,
                    "exit_results": tuned_rows,
                }
            )
        signal_cache.clear()
        gc.collect()

    ranked = _top_unique(tuned_rows, 3)
    print("\n[Stage 3] 상위 3개 최종 홀드아웃 평가")
    holdout_rows: list[Dict[str, Any]] = []
    for row in ranked:
        settings = deepcopy(base)
        settings.update(_entry_by_id(str(row["entry_id"]))["overrides"])
        exit_candidate = next(
            item for item in EXIT_CANDIDATES if item["id"] == row["exit_id"]
        )
        settings.update(exit_candidate["overrides"])
        signal_cache = {}
        holdout = _run_period(
            str(row["id"]),
            "holdout",
            HOLDOUT,
            settings,
            context,
            signal_cache,
        )
        holdout_rows.append(
            {
                "id": row["id"],
                "entry_id": row["entry_id"],
                "exit_id": row["exit_id"],
                "objective": row["objective"],
                "settings": row["settings"],
                "train": row["train"],
                "valid": row["valid"],
                "holdout": holdout,
                "passes_pre_holdout": bool(row.get("passes_pre_holdout", False)),
                "passes_target": bool(
                    row.get("passes_pre_holdout", False)
                    and
                    float(holdout.get("return_pct", -100.0)) > 0
                    and float(holdout.get("profit_factor", 0.0)) >= 1.20
                    and float(holdout.get("max_dd_pct", -100.0)) >= -8.0
                ),
            }
        )
        signal_cache.clear()
        gc.collect()

    final = {
        "status": "completed",
        "periods": {"train": TRAIN, "valid": VALID, "holdout": HOLDOUT},
        "selection_tuning": "고정된 기본 Track C 스케줄에서 진입·청산만 탐색",
        "entry_results": entry_rows,
        "exit_results": tuned_rows,
        "holdout_results": holdout_rows,
    }
    _write_summary(final)
    rows = []
    for row in holdout_rows:
        rows.append(
            {
                "id": row["id"],
                "objective": row["objective"],
                "train_return_pct": row["train"]["return_pct"],
                "valid_return_pct": row["valid"]["return_pct"],
                "holdout_return_pct": row["holdout"]["return_pct"],
                "holdout_max_dd_pct": row["holdout"]["max_dd_pct"],
                "holdout_profit_factor": row["holdout"]["profit_factor"],
                "holdout_n_exits": row["holdout"]["n_exits"],
                "passes_pre_holdout": row["passes_pre_holdout"],
                "passes_target": row["passes_target"],
            }
        )
    pd.DataFrame(rows).to_csv(
        OUT / "holdout_summary.csv", index=False, encoding="utf-8-sig"
    )
    print("\n[완료]")
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
