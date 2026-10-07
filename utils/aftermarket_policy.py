# -*- coding: utf-8 -*-
"""
애프터마켓(16:00~19:45) 15분봉을 **신호·지표 시계열**에 쓸지 결정하는 정책.

- 설정 ``use_aftermarket_bars``: ``"auto"``(기본) | True | False
  - ``"auto"``: ``order_after_close_enabled`` 를 따른다. 장마감 후 주문을 막으면
    애프터 봉은 주문에 쓰이지 않으면서 지표(MA·ATR·거래량 평균 등)만 왜곡하므로 제외.
- 데이터 수집(하베스팅 ``bars_1m``→``bars_15m`` 리샘플)은 정책과 무관하게 애프터 봉을 보존한다.
  정책은 **사용 시점**(실시간 슬롯 격자·워밍업·백테 로딩)에만 적용한다.

실시간 슬롯 격자(``utils.krx_bars``)는 ``register_live_settings_provider`` 로 등록한
설정을 매 호출 조회한다. 등록이 없으면(백테 워커·도구·테스트) 달력 기준(애프터 포함)으로 동작하고,
백테는 ``drop_aftermarket_rows`` 로 행을 직접 거른다.
"""
from __future__ import annotations

import contextlib
import contextvars
from typing import Any, Callable, Dict, Iterator, Mapping, Optional

import numpy as np
import pandas as pd

AFTERMARKET_BARS_SETTING_KEY: str = "use_aftermarket_bars"

_LiveProvider = Callable[[], Optional[Mapping[str, Any]]]
_live_provider: Optional[_LiveProvider] = None

# 스레드(컨텍스트)별 강제값 — None 이면 미지정
_grid_override: contextvars.ContextVar[Optional[bool]] = contextvars.ContextVar(
    "aftermarket_grid_override", default=None
)

_FALSE_STRS = ("0", "false", "off", "no", "n")
_TRUE_STRS = ("1", "true", "on", "yes", "y")


def _order_after_close_enabled(settings: Mapping[str, Any]) -> bool:
    """``order_after_close_enabled``(기본 True) 해석 — ``krx_bars.settings_allow_after_close_orders`` 와 동일."""
    try:
        raw = settings.get("order_after_close_enabled", True)
        if isinstance(raw, str):
            return raw.strip().lower() not in _FALSE_STRS + ("",)
        return bool(raw)
    except Exception:
        return True


def aftermarket_bars_enabled_for_settings(settings: Optional[Mapping[str, Any]]) -> bool:
    """
    설정 기준 애프터 15분봉 사용 여부.

    ``use_aftermarket_bars`` 가 명시 bool/문자열이면 그 값, ``"auto"``·미지정이면
    ``order_after_close_enabled`` 를 따른다. 해석 실패 시 True(기존 동작).
    """
    try:
        st: Mapping[str, Any] = settings or {}
        raw = st.get(AFTERMARKET_BARS_SETTING_KEY, "auto")
        if raw is None:
            return _order_after_close_enabled(st)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        s = str(raw).strip().lower()
        if s in _TRUE_STRS:
            return True
        if s in _FALSE_STRS:
            return False
        return _order_after_close_enabled(st)
    except Exception:
        return True


def register_live_settings_provider(provider: Optional[_LiveProvider]) -> None:
    """
    실시간 앱 설정 조회 함수를 등록한다(``None`` 이면 해제).

    등록 후 ``utils.krx_bars`` 슬롯 격자가 애프터 슬롯 포함 여부를 이 설정으로 판정한다.
    """
    global _live_provider
    _live_provider = provider


def live_aftermarket_grid_enabled() -> bool:
    """
    슬롯 격자에서 애프터 슬롯을 쓸지(달력상 애프터 시행일 판정은 호출부 책임).

    우선순위: 컨텍스트 강제값 → 등록된 실시간 설정 → True(미등록).
    """
    try:
        ov = _grid_override.get()
        if ov is not None:
            return bool(ov)
    except Exception:
        pass
    prov = _live_provider
    if prov is None:
        return True
    try:
        return aftermarket_bars_enabled_for_settings(prov())
    except Exception:
        return True


@contextlib.contextmanager
def force_aftermarket_grid(enabled: bool) -> Iterator[None]:
    """
    현재 스레드(컨텍스트)에서 슬롯 격자의 애프터 포함 여부를 강제한다.

    하베스팅 리샘플(항상 포함)·백테 실행(백테 설정 기준) 등에서 실시간 설정과 분리할 때 사용.
    """
    token = _grid_override.set(bool(enabled))
    try:
        yield
    finally:
        try:
            _grid_override.reset(token)
        except Exception:
            pass


def _close_minutes_by_day(days: np.ndarray) -> Dict[Any, int]:
    """일자 → 정규장 종가 단일가 라벨(분). 실패 시 15:30."""
    out: Dict[Any, int] = {}
    try:
        from utils.krx_session_hours import get_session_hours
    except Exception:
        get_session_hours = None  # type: ignore[assignment]
    for d in pd.unique(days):
        key = pd.Timestamp(d)
        try:
            if get_session_hours is None:
                raise RuntimeError("get_session_hours 없음")
            out[key] = int(get_session_hours(key.date()).close_minutes)
        except Exception:
            out[key] = 15 * 60 + 30
    return out


def aftermarket_row_mask(ts: Any) -> np.ndarray:
    """
    타임스탬프 배열 중 **정규장 종가 단일가 라벨 이후**(애프터) 행이면 True.

    종가 단일가(기본 15:30) 라벨 자체는 정규장이므로 False.
    """
    try:
        idx = pd.DatetimeIndex(pd.to_datetime(ts, errors="coerce"))
        if len(idx) == 0:
            return np.zeros(0, dtype=bool)
        valid = np.asarray(~idx.isna(), dtype=bool)
        if not valid.any():
            return np.zeros(len(idx), dtype=bool)
        hm = (
            pd.Series(idx.hour, dtype="float64").fillna(-1).to_numpy() * 60
            + pd.Series(idx.minute, dtype="float64").fillna(0).to_numpy()
        )
        days = pd.Series(idx.normalize())
        close_map = _close_minutes_by_day(days[valid].to_numpy())
        close_arr = (
            days.map(close_map).astype("float64").fillna(24 * 60).to_numpy()
        )
        return valid & (hm > close_arr)
    except Exception:
        try:
            return np.zeros(len(ts), dtype=bool)
        except Exception:
            return np.zeros(0, dtype=bool)


def drop_aftermarket_rows(
    df: Optional[pd.DataFrame],
    settings: Optional[Mapping[str, Any]] = None,
    *,
    enabled: Optional[bool] = None,
    time_col: Optional[str] = None,
) -> Optional[pd.DataFrame]:
    """
    애프터 정책이 OFF 이면 15분 OHLCV 에서 애프터(정규 종가 이후) 행을 제거한다.

    Args:
        df: DatetimeIndex 또는 ``time_col``(미지정 시 time/datetime/Date 자동 탐색) 보유 프레임.
        settings: 정책 판정용 설정(``enabled`` 미지정 시 사용).
        enabled: 애프터 사용 여부를 직접 지정(True 면 원본 그대로 반환).
        time_col: 시각 열 이름(인덱스가 시각이 아닐 때).

    Returns:
        필터된 프레임(변경 없으면 원본 객체). 실패 시 원본.
    """
    try:
        if df is None or getattr(df, "empty", True):
            return df
        use_ah = (
            bool(enabled)
            if enabled is not None
            else aftermarket_bars_enabled_for_settings(settings)
        )
        if use_ah:
            return df
        if time_col is not None and time_col in df.columns:
            ts = df[time_col]
        elif isinstance(df.index, pd.DatetimeIndex):
            ts = df.index
        else:
            ts = None
            for c in ("time", "datetime", "Date", "Datetime"):
                if c in df.columns:
                    ts = df[c]
                    break
            if ts is None:
                return df
        mask = aftermarket_row_mask(ts)
        if not mask.any():
            return df
        return df.loc[~mask].copy()
    except Exception:
        return df
