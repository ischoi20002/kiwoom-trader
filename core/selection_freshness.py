# -*- coding: utf-8 -*-
"""
일일 감시목록(``selected_targets.csv``) 신선도 판정.

직전(또는 당일 EOD 이후에는 당일) 장마감+버퍼 이후에 저장된 선정만
당일 세션용으로 유효합니다. 그보다 오래되면 재선정이 필요합니다.
"""
from __future__ import annotations

import os
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Dict, Optional, Tuple


DEFAULT_TARGETS_CSV = "selected_targets.csv"


def selected_targets_csv_path(cwd: Optional[str] = None) -> str:
    """작업 디렉터리 기준 ``selected_targets.csv`` 절대 경로."""
    try:
        base = cwd if cwd is not None else os.getcwd()
        return os.path.join(str(base), DEFAULT_TARGETS_CSV)
    except Exception:
        return DEFAULT_TARGETS_CSV


def _eod_buffer_minutes(settings: Optional[Dict[str, Any]]) -> int:
    try:
        st = settings or {}
        v = int(
            float(
                st.get(
                    "continuous_monitor_eod_buffer_minutes",
                    st.get("daily_report_session_buffer_minutes", 30),
                )
            )
        )
        return max(5, min(v, 120))
    except (TypeError, ValueError):
        return 30


def selection_fresh_after_threshold(
    now: Optional[datetime] = None,
    *,
    settings: Optional[Dict[str, Any]] = None,
) -> datetime:
    """
    이 시각(포함) 이후에 저장된 CSV 만 신선.

    - 거래일·장마감+버퍼 **이전**: 직전 거래일 15:30+버퍼
    - 거래일·장마감+버퍼 **이후**: 당일 15:30+버퍼
    - 비거래일: 직전 거래일 15:30+버퍼
    """
    t = now if now is not None else datetime.now()
    buf = _eod_buffer_minutes(settings)
    today_d = t.date()

    try:
        from reporting.krx_trading_calendar import is_trading_day, previous_trading_day
    except Exception:
        # 폴백: 평일만
        def is_trading_day(d: date) -> bool:  # type: ignore[misc]
            return d.weekday() < 5

        def previous_trading_day(d: date, max_lookback: int = 10) -> Optional[date]:
            cur = d
            for _ in range(max_lookback):
                cur = cur - timedelta(days=1)
                if cur.weekday() < 5:
                    return cur
            return None

    def _due(day_d: date) -> datetime:
        try:
            from utils.krx_session_hours import get_session_hours

            return get_session_hours(day_d).close_datetime() + timedelta(minutes=buf)
        except Exception:
            return datetime.combine(day_d, dtime(15, 30, 0)) + timedelta(minutes=buf)

    try:
        if is_trading_day(today_d):
            due_today = _due(today_d)
            if t >= due_today:
                return due_today
            prev = previous_trading_day(today_d)
            if prev is not None:
                return _due(prev)
            return due_today
        prev = previous_trading_day(today_d)
        if prev is not None:
            return _due(prev)
    except Exception:
        pass
    return datetime.combine(today_d, dtime(0, 0, 0))


def selected_targets_mtime(path: Optional[str] = None) -> Optional[datetime]:
    """CSV 수정 시각. 없거나 실패 시 None."""
    p = path or selected_targets_csv_path()
    try:
        if not os.path.isfile(p):
            return None
        return datetime.fromtimestamp(os.path.getmtime(p))
    except Exception:
        return None


def count_selected_targets(path: Optional[str] = None) -> int:
    """``selected_targets.csv`` 의 종목 수. 없거나 비면 0."""
    p = path or selected_targets_csv_path()
    try:
        if not os.path.isfile(p):
            return 0
        import pandas as pd

        df = pd.read_csv(p, dtype={"Code": str})
        if df is None or getattr(df, "empty", True):
            return 0
        if "Code" not in df.columns:
            return 0
        codes = [
            str(c).strip()
            for c in df["Code"].tolist()
            if str(c).strip() and str(c).strip().lower() != "nan"
        ]
        return len(codes)
    except Exception:
        return 0


def is_selected_targets_fresh(
    path: Optional[str] = None,
    *,
    now: Optional[datetime] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> bool:
    """일일 감시목록 CSV 가 현재 세션용으로 유효한지."""
    p = path or selected_targets_csv_path()
    mtime = selected_targets_mtime(p)
    if mtime is None:
        return False
    try:
        thr = selection_fresh_after_threshold(now, settings=settings)
        return mtime >= thr
    except Exception:
        return False


def selection_freshness_status(
    path: Optional[str] = None,
    *,
    now: Optional[datetime] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """(신선 여부, 로그용 설명)."""
    p = path or selected_targets_csv_path()
    mtime = selected_targets_mtime(p)
    if mtime is None:
        return False, f"선정 파일 없음: {p}"
    try:
        thr = selection_fresh_after_threshold(now, settings=settings)
        if mtime >= thr:
            return True, (
                f"선정 신선 (mtime={mtime.strftime('%Y-%m-%d %H:%M')}, "
                f"기준≥{thr.strftime('%Y-%m-%d %H:%M')})"
            )
        return False, (
            f"선정 만료 (mtime={mtime.strftime('%Y-%m-%d %H:%M')}, "
            f"필요≥{thr.strftime('%Y-%m-%d %H:%M')})"
        )
    except Exception as e:
        return False, f"신선도 판정 실패: {e}"
