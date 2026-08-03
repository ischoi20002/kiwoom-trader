# -*- coding: utf-8 -*-
"""
완성 15분 신호/판정 후 주문 직전 가격 확인 게이트.

S1·S2·EXIT 공통. RT는 라이브가, BT(저정밀)는 다음 1분봉 종가를 확인가로 씁니다.
임계값은 ``realtime_s1_live_*`` 설정을 사용합니다.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from utils.krx_bars import next_m1_close_after_15m_end


def s1_price_confirm_gate(
    bar_close: float,
    confirm_px: float,
    ma20: float = 0.0,
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    완성봉 Close 대비 확인가(라이브 또는 다음 1분 종가) 게이트.

    Returns
    -------
    (ok, reason)
        ok=False 이면 주문/체결 스킵. reason 은 실패 사유(성공 시 빈 문자열).
    """
    st = settings if isinstance(settings, dict) else {}

    try:
        bc = float(bar_close or 0.0)
    except (TypeError, ValueError):
        bc = 0.0
    if bc <= 0:
        return False, "no_bar_close"

    try:
        px = float(confirm_px or 0.0)
    except (TypeError, ValueError):
        px = 0.0
    if px <= 0:
        return False, "no_confirm_price"

    try:
        chase = float(st.get("realtime_s1_live_max_chase_pct", 0.8) or 0.8)
    except (TypeError, ValueError):
        chase = 0.8
    try:
        fade = float(st.get("realtime_s1_live_max_fade_pct", 1.0) or 1.0)
    except (TypeError, ValueError):
        fade = 1.0
    chase = max(0.0, min(chase, 10.0))
    fade = max(0.0, min(fade, 10.0))

    if px > bc * (1.0 + chase / 100.0):
        return False, f"chase>{chase:.1f}% conf={px:.0f} close={bc:.0f}"
    if px < bc * (1.0 - fade / 100.0):
        return False, f"fade>{fade:.1f}% conf={px:.0f} close={bc:.0f}"

    try:
        ma_side = bool(st.get("realtime_s1_live_ma20_side_confirm", True))
    except Exception:
        ma_side = True
    if ma_side:
        try:
            m20 = float(ma20 or 0.0)
        except (TypeError, ValueError):
            m20 = 0.0
        if m20 > 0:
            bar_above = bc >= m20
            conf_above = px >= m20
            if bar_above and not conf_above:
                return False, f"ma20_break conf={px:.0f} ma20={m20:.0f}"
            if (not bar_above) and conf_above:
                return False, f"ma20_cross_up conf={px:.0f} ma20={m20:.0f}"

    return True, ""


def bt_m1_confirm_fill(
    m1_df: Any,
    slot_end: Any,
    bar_close: float,
    ma20: float = 0.0,
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, float, str]:
    """
    BT 저정밀: 다음 1분 종가 조회 + 게이트.

    Returns
    -------
    (ok, fill_px, reason)
        ok=True 이면 fill_px 로 체결. 실패 시 fill_px=0.
    """
    st = settings if isinstance(settings, dict) else {}
    try:
        if not bool(st.get("bt_s1_m1_confirm_gate_enabled", True)):
            try:
                bc = float(bar_close or 0.0)
            except (TypeError, ValueError):
                bc = 0.0
            return (bc > 0, bc, "")
    except Exception:
        pass

    try:
        conf = next_m1_close_after_15m_end(m1_df, slot_end)
    except Exception:
        conf = None
    if conf is None or float(conf) <= 0:
        return False, 0.0, "no_confirm_1m"

    ok, reason = s1_price_confirm_gate(bar_close, float(conf), ma20, st)
    if not ok:
        return False, float(conf), reason
    return True, float(conf), ""
