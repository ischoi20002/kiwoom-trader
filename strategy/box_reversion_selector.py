"""Box Reversion(박스 회귀) 전략의 일봉 기반 종목 선정기."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Optional

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class BoxReversionSelectionConfig:
    """박스권 후보 선정 설정."""

    lookback: int = 20
    min_history_bars: int = 60
    atr_period: int = 14
    adx_period: int = 14
    ma_period: int = 20
    ma_slope_days: int = 5
    min_avg_trde_value_krw: float = 20_000_000_000.0
    min_price: float = 3_000.0
    min_box_width_pct: float = 6.0
    max_box_width_pct: float = 12.0
    max_adx: float = 22.0
    max_ma_slope_pct_per_day: float = 0.15
    min_lower_touches: int = 2
    min_upper_touches: int = 2
    touch_atr_tolerance: float = 0.5
    breakout_atr_tolerance: float = 0.5
    max_breakout_closes: int = 1
    max_volume_ratio: float = 2.0
    max_atr_expansion_ratio: float = 1.5
    lower_quantile: float = 0.20
    upper_quantile: float = 0.80
    max_candidates: int = 15

    @classmethod
    def from_mapping(
        cls, settings: Optional[Mapping[str, Any]] = None
    ) -> "BoxReversionSelectionConfig":
        """설정 딕셔너리에서 안전하게 구성합니다."""
        if not settings:
            return cls()

        defaults = cls()

        def _number(key: str, default: float, caster: type) -> Any:
            try:
                return caster(settings.get(key, default))
            except (TypeError, ValueError):
                logging.warning("Box Reversion 설정값 오류: %s=%r", key, settings.get(key))
                return caster(default)

        return cls(
            lookback=max(10, _number("box_sel_lookback", defaults.lookback, int)),
            min_history_bars=max(
                30,
                _number("box_sel_min_history_bars", defaults.min_history_bars, int),
            ),
            atr_period=max(2, _number("box_sel_atr_period", defaults.atr_period, int)),
            adx_period=max(2, _number("box_sel_adx_period", defaults.adx_period, int)),
            ma_period=max(2, _number("box_sel_ma_period", defaults.ma_period, int)),
            ma_slope_days=max(
                1,
                _number("box_sel_ma_slope_days", defaults.ma_slope_days, int),
            ),
            min_avg_trde_value_krw=max(
                0.0,
                _number(
                    "box_sel_min_avg_trde_value_krw",
                    defaults.min_avg_trde_value_krw,
                    float,
                ),
            ),
            min_price=max(0.0, _number("box_sel_min_price", defaults.min_price, float)),
            min_box_width_pct=max(
                0.0,
                _number(
                    "box_sel_min_box_width_pct", defaults.min_box_width_pct, float
                ),
            ),
            max_box_width_pct=max(
                0.0,
                _number(
                    "box_sel_max_box_width_pct", defaults.max_box_width_pct, float
                ),
            ),
            max_adx=max(0.0, _number("box_sel_max_adx", defaults.max_adx, float)),
            max_ma_slope_pct_per_day=max(
                0.0,
                _number(
                    "box_sel_max_ma_slope_pct_per_day",
                    defaults.max_ma_slope_pct_per_day,
                    float,
                ),
            ),
            min_lower_touches=max(
                1,
                _number(
                    "box_sel_min_lower_touches", defaults.min_lower_touches, int
                ),
            ),
            min_upper_touches=max(
                1,
                _number(
                    "box_sel_min_upper_touches", defaults.min_upper_touches, int
                ),
            ),
            touch_atr_tolerance=max(
                0.0,
                _number(
                    "box_sel_touch_atr_tolerance",
                    defaults.touch_atr_tolerance,
                    float,
                ),
            ),
            breakout_atr_tolerance=max(
                0.0,
                _number(
                    "box_sel_breakout_atr_tolerance",
                    defaults.breakout_atr_tolerance,
                    float,
                ),
            ),
            max_breakout_closes=max(
                0,
                _number(
                    "box_sel_max_breakout_closes",
                    defaults.max_breakout_closes,
                    int,
                ),
            ),
            max_volume_ratio=max(
                0.0,
                _number(
                    "box_sel_max_volume_ratio", defaults.max_volume_ratio, float
                ),
            ),
            max_atr_expansion_ratio=max(
                0.0,
                _number(
                    "box_sel_max_atr_expansion_ratio",
                    defaults.max_atr_expansion_ratio,
                    float,
                ),
            ),
            lower_quantile=float(
                np.clip(
                    _number(
                        "box_sel_lower_quantile", defaults.lower_quantile, float
                    ),
                    0.0,
                    0.49,
                )
            ),
            upper_quantile=float(
                np.clip(
                    _number(
                        "box_sel_upper_quantile", defaults.upper_quantile, float
                    ),
                    0.51,
                    1.0,
                )
            ),
            max_candidates=max(
                1,
                _number("box_sel_max_candidates", defaults.max_candidates, int),
            ),
        )


def _normalize_daily_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """일봉 컬럼과 인덱스를 표준화하고 유효하지 않은 행을 제거합니다."""
    if df is None or df.empty:
        return pd.DataFrame()

    normalized = df.copy()
    normalized.columns = [str(column).strip().lower() for column in normalized.columns]
    if "time" in normalized.columns and "datetime" not in normalized.columns:
        normalized.rename(columns={"time": "datetime"}, inplace=True)
    if "date" in normalized.columns and "datetime" not in normalized.columns:
        normalized.rename(columns={"date": "datetime"}, inplace=True)

    required = {"open", "high", "low", "close", "volume"}
    missing = required.difference(normalized.columns)
    if missing:
        raise ValueError(f"필수 일봉 컬럼 누락: {', '.join(sorted(missing))}")

    for column in required:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")
    normalized.dropna(subset=list(required), inplace=True)
    normalized = normalized[
        (normalized["high"] > 0)
        & (normalized["low"] > 0)
        & (normalized["close"] > 0)
        & (normalized["volume"] >= 0)
    ].copy()

    if "datetime" in normalized.columns:
        normalized["datetime"] = pd.to_datetime(
            normalized["datetime"], errors="coerce"
        )
        normalized.dropna(subset=["datetime"], inplace=True)
        normalized.sort_values("datetime", inplace=True)
    else:
        try:
            normalized.sort_index(inplace=True)
        except (TypeError, ValueError):
            pass
    return normalized


def _wilder_ewm(series: pd.Series, period: int) -> pd.Series:
    """Wilder 방식의 지수 이동평균을 계산합니다."""
    return series.ewm(alpha=1.0 / float(period), adjust=False, min_periods=period).mean()


def _add_atr_adx(df: pd.DataFrame, atr_period: int, adx_period: int) -> pd.DataFrame:
    """ATR과 ADX를 미래참조 없이 계산합니다."""
    result = df.copy()
    previous_close = result["close"].shift(1)
    true_range = pd.concat(
        [
            result["high"] - result["low"],
            (result["high"] - previous_close).abs(),
            (result["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    result["atr"] = _wilder_ewm(true_range, atr_period)

    up_move = result["high"].diff()
    down_move = -result["low"].diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=result.index,
        dtype=float,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=result.index,
        dtype=float,
    )
    smoothed_tr = _wilder_ewm(true_range, adx_period).replace(0.0, np.nan)
    plus_di = 100.0 * _wilder_ewm(plus_dm, adx_period) / smoothed_tr
    minus_di = 100.0 * _wilder_ewm(minus_dm, adx_period) / smoothed_tr
    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    result["adx"] = _wilder_ewm(dx, adx_period)
    return result


def _bounded_score(value: float, best: float, worst: float) -> float:
    """작을수록 좋은 지표를 0~1 점수로 변환합니다."""
    if not np.isfinite(value):
        return 0.0
    if worst <= best:
        return 1.0 if value <= best else 0.0
    return float(np.clip((worst - value) / (worst - best), 0.0, 1.0))


def evaluate_box_reversion_candidate(
    daily_ohlcv: pd.DataFrame,
    *,
    code: str,
    name: Optional[str] = None,
    config: Optional[BoxReversionSelectionConfig] = None,
    as_of: Optional[date | datetime | pd.Timestamp] = None,
) -> Optional[dict[str, Any]]:
    """한 종목이 박스권 후보인지 평가하고, 유효 후보만 반환합니다."""
    cfg = config or BoxReversionSelectionConfig()
    try:
        df = _normalize_daily_ohlcv(daily_ohlcv)
        if as_of is not None and not df.empty:
            cutoff = pd.Timestamp(as_of)
            if "datetime" in df.columns:
                df = df[df["datetime"] <= cutoff].copy()
            elif isinstance(df.index, pd.DatetimeIndex):
                df = df[df.index <= cutoff].copy()

        required_bars = max(
            cfg.min_history_bars,
            cfg.lookback + 1,
            cfg.ma_period + cfg.ma_slope_days,
            (cfg.adx_period * 2) + 1,
        )
        if len(df) < required_bars:
            return None

        df = _add_atr_adx(df, cfg.atr_period, cfg.adx_period)
        df["ma"] = df["close"].rolling(cfg.ma_period).mean()
        df["trde_value"] = df["close"] * df["volume"]
        df["avg_trde_value"] = df["trde_value"].rolling(cfg.lookback).mean()

        last = df.iloc[-1]
        atr = float(last["atr"])
        adx = float(last["adx"])
        price = float(last["close"])
        avg_trde_value = float(last["avg_trde_value"])
        if not all(np.isfinite(value) for value in (atr, adx, price, avg_trde_value)):
            return None
        if atr <= 0 or price < cfg.min_price:
            return None

        box = df.iloc[-cfg.lookback :].copy()
        lower = float(box["low"].quantile(cfg.lower_quantile))
        upper = float(box["high"].quantile(cfg.upper_quantile))
        center = (lower + upper) / 2.0
        if center <= 0 or upper <= lower:
            return None
        box_width_pct = ((upper - lower) / center) * 100.0

        ma_now = float(last["ma"])
        ma_then = float(df["ma"].iloc[-1 - cfg.ma_slope_days])
        if ma_now <= 0 or ma_then <= 0:
            return None
        ma_slope_pct_per_day = (
            ((ma_now / ma_then) ** (1.0 / cfg.ma_slope_days)) - 1.0
        ) * 100.0

        touch_distance = atr * cfg.touch_atr_tolerance
        lower_touches = int((box["low"] <= lower + touch_distance).sum())
        upper_touches = int((box["high"] >= upper - touch_distance).sum())

        breakout_distance = atr * cfg.breakout_atr_tolerance
        outside = (box["close"] < lower - breakout_distance) | (
            box["close"] > upper + breakout_distance
        )
        breakout_closes = int(outside.sum())

        prior_volume = float(box["volume"].iloc[:-1].mean())
        volume_ratio = (
            float(last["volume"]) / prior_volume if prior_volume > 0 else float("inf")
        )
        prior_atr = float(df["atr"].iloc[-cfg.lookback - 1 : -1].mean())
        atr_expansion_ratio = atr / prior_atr if prior_atr > 0 else float("inf")

        valid = all(
            (
                avg_trde_value >= cfg.min_avg_trde_value_krw,
                cfg.min_box_width_pct <= box_width_pct <= cfg.max_box_width_pct,
                adx <= cfg.max_adx,
                abs(ma_slope_pct_per_day) <= cfg.max_ma_slope_pct_per_day,
                lower_touches >= cfg.min_lower_touches,
                upper_touches >= cfg.min_upper_touches,
                breakout_closes <= cfg.max_breakout_closes,
                volume_ratio <= cfg.max_volume_ratio,
                atr_expansion_ratio <= cfg.max_atr_expansion_ratio,
            )
        )
        if not valid:
            return None

        stability_score = 15.0 * _bounded_score(adx, 10.0, cfg.max_adx)
        stability_score += 15.0 * _bounded_score(
            abs(ma_slope_pct_per_day), 0.0, cfg.max_ma_slope_pct_per_day
        )
        touch_score = 12.5 * min(lower_touches / cfg.min_lower_touches, 1.0)
        touch_score += 12.5 * min(upper_touches / cfg.min_upper_touches, 1.0)
        trend_score = 10.0 * _bounded_score(volume_ratio, 1.0, cfg.max_volume_ratio)
        trend_score += 10.0 * _bounded_score(
            atr_expansion_ratio, 1.0, cfg.max_atr_expansion_ratio
        )
        liquidity_score = 15.0 * min(
            avg_trde_value / max(cfg.min_avg_trde_value_krw * 5.0, 1.0), 1.0
        )
        ideal_width = (cfg.min_box_width_pct + cfg.max_box_width_pct) / 2.0
        half_width = max((cfg.max_box_width_pct - cfg.min_box_width_pct) / 2.0, 0.01)
        width_score = 10.0 * max(
            0.0, 1.0 - abs(box_width_pct - ideal_width) / half_width
        )
        total_score = min(
            100.0,
            stability_score
            + touch_score
            + trend_score
            + liquidity_score
            + width_score,
        )
        box_position_pct = float(
            np.clip((price - lower) / (upper - lower) * 100.0, 0.0, 100.0)
        )

        return {
            "Code": str(code).zfill(6),
            "Name": str(name or code),
            "Strategy": "box_reversion",
            "Score": round(total_score, 2),
            "Price": round(price, 2),
            "BoxLower": round(lower, 2),
            "BoxUpper": round(upper, 2),
            "BoxWidthPct": round(box_width_pct, 2),
            "BoxPositionPct": round(box_position_pct, 2),
            "ADX": round(adx, 2),
            "MASlopePctPerDay": round(ma_slope_pct_per_day, 4),
            "LowerTouches": lower_touches,
            "UpperTouches": upper_touches,
            "AvgTrdeValueEok": round(avg_trde_value / 100_000_000.0, 1),
            "VolumeRatio": round(volume_ratio, 2),
            "ATRExpansionRatio": round(atr_expansion_ratio, 2),
        }
    except (KeyError, TypeError, ValueError, ArithmeticError, IndexError) as exc:
        logging.warning(
            "Box Reversion 후보 평가 실패(%s): %s", str(code).zfill(6), exc
        )
        return None
    except Exception:
        logging.exception("Box Reversion 후보 평가 중 예외(%s)", str(code).zfill(6))
        return None


def select_box_reversion_candidates(
    ohlcv_by_code: Mapping[str, pd.DataFrame],
    *,
    names: Optional[Mapping[str, str]] = None,
    settings: Optional[Mapping[str, Any]] = None,
    as_of: Optional[date | datetime | pd.Timestamp] = None,
) -> pd.DataFrame:
    """여러 종목을 평가하여 점수순 Box Reversion 후보를 반환합니다."""
    config = BoxReversionSelectionConfig.from_mapping(settings)
    candidates: list[dict[str, Any]] = []
    for raw_code, daily_ohlcv in ohlcv_by_code.items():
        code = str(raw_code).zfill(6)
        try:
            candidate = evaluate_box_reversion_candidate(
                daily_ohlcv,
                code=code,
                name=(names or {}).get(raw_code, (names or {}).get(code, code)),
                config=config,
                as_of=as_of,
            )
            if candidate is not None:
                candidates.append(candidate)
        except Exception:
            logging.exception("Box Reversion 종목 선정 실패(%s)", code)

    if not candidates:
        return pd.DataFrame(
            columns=[
                "Code",
                "Name",
                "Strategy",
                "Score",
                "Price",
                "BoxLower",
                "BoxUpper",
                "BoxWidthPct",
                "BoxPositionPct",
            ]
        )

    result = pd.DataFrame(candidates)
    result.sort_values(
        ["Score", "BoxPositionPct", "AvgTrdeValueEok"],
        ascending=[False, True, False],
        inplace=True,
        kind="stable",
    )
    return result.head(config.max_candidates).reset_index(drop=True)
