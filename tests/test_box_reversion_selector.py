"""Box Reversion 종목 선정기 테스트."""

from __future__ import annotations

import numpy as np
import pandas as pd

from strategy.box_reversion_selector import (
    BoxReversionSelectionConfig,
    evaluate_box_reversion_candidate,
    select_box_reversion_candidates,
)
from backtest_engine import TradingStrategy
from strategy_selector import _merge_dual_track_picks


def _make_range_ohlcv(periods: int = 100) -> pd.DataFrame:
    """충분한 유동성과 반복 접촉을 가진 박스권 일봉을 생성합니다."""
    dates = pd.bdate_range("2026-01-02", periods=periods)
    phase = np.arange(periods) * (2.0 * np.pi / 10.0)
    close = 10_000.0 + (450.0 * np.sin(phase))
    return pd.DataFrame(
        {
            "datetime": dates,
            "open": close - 20.0,
            "high": close + 100.0,
            "low": close - 100.0,
            "close": close,
            "volume": np.full(periods, 3_000_000.0),
        }
    )


def _relaxed_config() -> BoxReversionSelectionConfig:
    """합성 데이터의 지표 초기화 차이를 허용하는 테스트 설정입니다."""
    return BoxReversionSelectionConfig(
        max_adx=35.0,
        max_ma_slope_pct_per_day=0.3,
        min_box_width_pct=5.0,
        max_box_width_pct=15.0,
    )


def test_range_stock_is_selected() -> None:
    """반복 횡보 종목은 박스 후보로 선정되어야 합니다."""
    candidate = evaluate_box_reversion_candidate(
        _make_range_ohlcv(),
        code="5930",
        name="테스트전자",
        config=_relaxed_config(),
    )

    assert candidate is not None
    assert candidate["Code"] == "005930"
    assert candidate["Strategy"] == "box_reversion"
    assert candidate["LowerTouches"] >= 2
    assert candidate["UpperTouches"] >= 2
    assert 5.0 <= candidate["BoxWidthPct"] <= 15.0


def test_assume_clean_matches_normalize_path() -> None:
    """배치용 assume_clean 경로가 정규화 경로와 동일 결과를 내야 합니다."""
    df = _make_range_ohlcv()
    cfg = _relaxed_config()
    a = evaluate_box_reversion_candidate(df, code="005930", config=cfg)
    b = evaluate_box_reversion_candidate(
        df.rename(columns={"datetime": "time"}),
        code="005930",
        config=cfg,
        assume_clean=True,
    )
    assert a is not None and b is not None
    assert a["Score"] == b["Score"]
    assert a["BoxLower"] == b["BoxLower"]
    assert a["BoxUpper"] == b["BoxUpper"]
    assert a["ADX"] == b["ADX"]


def test_trending_stock_is_rejected() -> None:
    """방향성이 강한 상승 종목은 박스 후보에서 제외되어야 합니다."""
    df = _make_range_ohlcv()
    trend = np.linspace(8_000.0, 14_000.0, len(df))
    df["open"] = trend - 30.0
    df["high"] = trend + 100.0
    df["low"] = trend - 100.0
    df["close"] = trend

    candidate = evaluate_box_reversion_candidate(
        df,
        code="000001",
        config=_relaxed_config(),
    )

    assert candidate is None


def test_selector_sorts_and_limits_candidates() -> None:
    """선정 결과는 점수순 정렬과 최대 개수 제한을 지켜야 합니다."""
    settings = {
        "box_sel_max_candidates": 1,
        "box_sel_max_adx": 35.0,
        "box_sel_max_ma_slope_pct_per_day": 0.3,
        "box_sel_min_box_width_pct": 5.0,
        "box_sel_max_box_width_pct": 15.0,
    }
    result = select_box_reversion_candidates(
        {
            "005930": _make_range_ohlcv(),
            "000660": _make_range_ohlcv(),
        },
        names={"005930": "테스트전자", "000660": "테스트반도체"},
        settings=settings,
    )

    assert len(result) == 1
    assert result.iloc[0]["Strategy"] == "box_reversion"


def test_track_c_merge_and_buy_guard() -> None:
    """C 선정 결과를 병합하되 기존 A/B 신호 매수는 차단해야 합니다."""
    logs: list[str] = []
    rows = [
        {
            "Code": "005930",
            "Name": "C전용",
            "_valid_a": False,
            "_score_a": 0.0,
            "_valid_b": False,
            "_score_b": 0.0,
            "_valid_c": True,
            "_score_c": 88.0,
        },
        {
            "Code": "000660",
            "Name": "A와C",
            "_valid_a": True,
            "_score_a": 70.0,
            "_valid_b": False,
            "_score_b": 0.0,
            "_valid_c": True,
            "_score_c": 82.0,
        },
    ]
    merged = _merge_dual_track_picks(
        rows,
        {"max_slots_a": 10, "max_slots_b": 10, "max_slots_c": 10},
        logs.append,
    )
    by_code = {row["Code"]: row for row in merged}

    assert by_code["005930"]["SelTrack"] == "C"
    assert by_code["000660"]["SelTrack"] == "AC"
    assert not TradingStrategy.track_signal_match_allows(
        {"enforce_track_signal_match": False}, "TYPE_A", "C"
    )
    assert TradingStrategy.track_signal_match_allows(
        {"enforce_track_signal_match": True}, "TYPE_A", "AC"
    )
    assert not TradingStrategy.track_signal_match_allows(
        {"use_type_c": False}, "TYPE_C", "C"
    )
