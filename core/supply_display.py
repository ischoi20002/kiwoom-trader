# -*- coding: utf-8 -*-
"""
실시간 감시·주문 로그·일일 결산 리포트에서 공유하는 체결강도·호가 표시.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


def format_bid_ask_monitor_display(total_ask: object, total_bid: object) -> str:
    """
    매도잔량 / 매수잔량 및 매도÷매수 비율(소수 1자리).
    Smart Buy 매도벽 비율과 동일한 분모(매수잔량) 기준.
    """
    try:
        ta = int(float(total_ask or 0))
        tb = int(float(total_bid or 0))
        if tb > 0:
            ratio = float(ta) / float(tb)
            r_s = f"{ratio:.1f}"
        else:
            r_s = "—"
        return f"{ta:,} / {tb:,} ({r_s})"
    except Exception:
        return "— / — (—)"


def format_vol_power_column(vol_power: object) -> str:
    """감시 화면 '체결강도' 열과 동일(소수 1자리 %)."""
    try:
        if vol_power is None or str(vol_power).strip() in ("", "—"):
            return "—"
        return f"{float(vol_power):.1f}%"
    except Exception:
        return "—"


def capture_supply_snapshot(app: Any, code: str) -> Dict[str, Optional[Any]]:
    """
    monitor_info 기준 스냅샷(주문·체결 시점 기록용).

    Returns
    -------
    dict
        vol_power: float 또는 None, bid_ask_text: 감시 Bid/Ask 열과 동일 문자열.
    """
    out: Dict[str, Optional[Any]] = {"vol_power": None, "bid_ask_text": "— / — (—)"}
    try:
        nk = str(code).split("_")[0].strip().zfill(6)
        raw = getattr(app, "monitor_info", None) or {}
        info = raw.get(nk)
        if not isinstance(info, dict):
            return out
        try:
            vp = info.get("vol_power")
            if vp is not None and str(vp).strip() not in ("", "—"):
                out["vol_power"] = float(vp)
        except (TypeError, ValueError):
            pass
        out["bid_ask_text"] = format_bid_ask_monitor_display(
            info.get("total_ask"), info.get("total_bid")
        )
    except Exception:
        pass
    return out