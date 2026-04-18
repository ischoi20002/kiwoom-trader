# -*- coding: utf-8 -*-
"""
영업일 종료 시점 총자산 스냅샷(로컬 JSON).

- 모의/실전은 ``eod_equity_snapshots_mock.json`` / ``eod_equity_snapshots_live.json`` 으로 분리됩니다.
- **금일 손익** = 현재 총자산 − 직전 영업일에 저장된 EOD 총자산 (일일 리포트·포지션 탭 공통).
- 직전 영업일 스냅이 없으면 금일 손익은 계산 불가(None)이며, 장 마감+버퍼 이후 자동 기록으로 채워집니다.
"""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from reporting.krx_trading_calendar import is_trading_day, previous_trading_day
from utils.server_mode_paths import eod_equity_snapshots_path


def _load_store() -> Dict[str, Any]:
    """스냅샷 JSON 전체(dict). 파손 시 빈 ``by_date``."""
    path = eod_equity_snapshots_path()
    if not os.path.isfile(path):
        return {"by_date": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"by_date": {}}
        bd = data.get("by_date")
        if not isinstance(bd, dict):
            data["by_date"] = {}
        return data
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"by_date": {}}
    except Exception:
        return {"by_date": {}}


def _save_store(data: Dict[str, Any]) -> None:
    """원자적 저장(임시 파일 후 replace)."""
    path = eod_equity_snapshots_path()
    try:
        base = os.path.dirname(path)
        if base:
            os.makedirs(base, exist_ok=True)
    except OSError:
        pass
    try:
        tmp = f"{path}.tmp"
        out = dict(data)
        out["by_date"] = dict(out.get("by_date") or {})
        out["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass


def read_eod_total_assets_for_day(trading_day_yyyy_mm_dd: str) -> Optional[float]:
    """특정 영업일 키(YYYY-MM-DD)에 저장된 EOD 총자산. 없거나 비정상이면 None."""
    try:
        key = str(trading_day_yyyy_mm_dd)[:10]
    except (TypeError, ValueError, AttributeError):
        return None
    try:
        raw = (_load_store().get("by_date") or {}).get(key)
        if raw is None:
            return None
        v = float(raw)
        if not math.isfinite(v) or v <= 0:
            return None
        return float(v)
    except (TypeError, ValueError, AttributeError):
        return None
    except Exception:
        return None


def write_eod_total_assets_for_day(trading_day_yyyy_mm_dd: str, total_assets: float) -> None:
    """해당 영업일의 EOD 총자산을 기록(덮어쓰기)."""
    try:
        key = str(trading_day_yyyy_mm_dd)[:10]
        v = float(total_assets)
    except (TypeError, ValueError, AttributeError):
        return
    if not math.isfinite(v) or v <= 0:
        return
    try:
        st = _load_store()
        bd = dict(st.get("by_date") or {})
        bd[key] = float(v)
        st["by_date"] = bd
        _save_store(st)
    except Exception:
        pass


def read_prev_eod_total_assets_for_calendar_day(cal: date) -> Optional[float]:
    """
    ``cal`` 달력일 기준 **직전 영업일**에 저장된 EOD 총자산.

    예: 월요일이면 직전 금요일 스냅샷.
    """
    try:
        prev = previous_trading_day(cal)
    except Exception:
        prev = None
    if prev is None:
        return None
    return read_eod_total_assets_for_day(prev.strftime("%Y-%m-%d"))


def compute_day_pnl_vs_eod_store(
    app: Any,
    *,
    calendar_day: date,
    current_total_assets: float,
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
    """
    금일 손익(원), 전일 대비(%), 분모로 사용한 전일 EOD 총자산, 전일 키(YYYY-MM-DD).

    Parameters
    ----------
    current_total_assets :
        이미 계산된 현재 총자산(포지션 탭·일일 결산과 동일 규칙). 중복 ``_compute_summary`` 호출 방지.
    """
    try:
        prev = previous_trading_day(calendar_day)
    except Exception:
        prev = None
    if prev is None:
        return None, None, None, None
    prev_key = prev.strftime("%Y-%m-%d")
    try:
        prev_assets = read_eod_total_assets_for_day(prev_key)
    except Exception:
        prev_assets = None
    if prev_assets is None or prev_assets <= 0:
        return None, None, None, prev_key
    try:
        cur = float(current_total_assets)
    except (TypeError, ValueError):
        return None, None, float(prev_assets), prev_key
    if not math.isfinite(cur):
        return None, None, float(prev_assets), prev_key
    try:
        d_amt = cur - float(prev_assets)
        d_pct = (d_amt / float(prev_assets)) * 100.0
        if not math.isfinite(d_pct):
            d_pct = 0.0
        return float(d_amt), float(d_pct), float(prev_assets), prev_key
    except (TypeError, ValueError, ZeroDivisionError):
        return None, None, float(prev_assets), prev_key
    except Exception:
        return None, None, float(prev_assets), prev_key


def _session_end_local(report_day: str, buffer_minutes: int) -> datetime:
    """KST 로컬 기준 장 마감(15:30) + 버퍼."""
    d = datetime.strptime(str(report_day)[:10], "%Y-%m-%d")
    return d.replace(hour=15, minute=30, second=0, microsecond=0) + timedelta(
        minutes=max(0, int(buffer_minutes))
    )


def maybe_record_eod_after_session(
    app: Any,
    total_assets: float,
    *,
    now: Optional[datetime] = None,
) -> None:
    """
    거래일이고 (장 마감 + ``daily_report_session_buffer_minutes``) 이후면
    **당일** 키로 총자산 스냅샷을 저장합니다.
    """
    try:
        t = now or datetime.now()
        d = t.date()
        if not is_trading_day(d):
            return
        try:
            buf = int((getattr(app, "settings", None) or {}).get("daily_report_session_buffer_minutes", 10))
        except (TypeError, ValueError, AttributeError):
            buf = 10
        buf = max(0, min(buf, 24 * 60))
        if t < _session_end_local(d.strftime("%Y-%m-%d"), buf):
            return
        write_eod_total_assets_for_day(d.strftime("%Y-%m-%d"), float(total_assets))
    except Exception:
        pass
