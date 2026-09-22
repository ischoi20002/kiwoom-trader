# -*- coding: utf-8 -*-
"""감시 트리 '전략유형' 표시 문자열 — UI/신호엔진 공통 (Tk 비의존)."""
from typing import Any, Dict, Optional


def format_strategy_type_label(sel_track: object) -> str:
    """
    선정 트랙(A/B/C 조합) → 실시간 감시 '전략유형' 컬럼 표시문자열.
    SignalEngine 등에서 동일 문구를 쓰려면 이 함수를 import 해 사용합니다.
    """
    try:
        tr = str(sel_track if sel_track is not None else "").strip().upper()
        normalized = "".join(track for track in ("A", "B", "C") if track in tr)
        if normalized:
            return "전략 " + "+".join(normalized)
        return "전략 A+B"
    except Exception:
        return "전략 A+B"


def _open_holding_pos(pos: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """수량>0 인 실제 보유 dict만 반환. 미체결·유령 dict는 선정 트랙 표시를 오염시키지 않습니다."""
    try:
        if not isinstance(pos, dict):
            return None
        qty = int(pos.get("qty") or pos.get("quantity") or 0)
        if qty > 0:
            return pos
    except Exception:
        return None
    return None


def format_entry_signal_type_label(
    pos: Optional[Dict[str, Any]],
    sel_track: object,
    *,
    code: Optional[str] = None,
    app: Any = None,
) -> str:
    """
    보유 종목은 실제 TYPE_A/B/C를, 비보유 종목은 선정 트랙을 표시합니다.

    code·app 이 있으면 PortfolioManager.infer_sig_type_for_ui 로 단일 A/B 를
    (DB 복구 후 sig_type 누락·SYNCED 등) 보강합니다.
    """
    try:
        pos = _open_holding_pos(pos)
        # 보유(dict)일 때만 포트폴리오 추정 — 미보유면 과거 매수 reason 으로 오표시될 수 있음
        if app is not None and code and isinstance(pos, dict):
            pf = getattr(app, "portfolio", None)
            if pf is not None and hasattr(pf, "infer_sig_type_for_ui"):
                eff = pf.infer_sig_type_for_ui(
                    str(code).split("_")[0].zfill(6), pos
                )
                if eff == "TYPE_B":
                    return "전략 B"
                if eff == "TYPE_A":
                    return "전략 A"
                if eff == "TYPE_C":
                    return "전략 C"
        if isinstance(pos, dict):
            st = str(pos.get("sig_type") or "").strip().upper()
            if st == "TYPE_C" or "TYPE_C" in st:
                return "전략 C"
            if st == "TYPE_B" or "TYPE_B" in st:
                return "전략 B"
            if st == "TYPE_A" or "TYPE_A" in st:
                return "전략 A"
            rsn = str(pos.get("reason") or "").upper()
            if "TYPE_C" in rsn:
                return "전략 C"
            if "TYPE_B" in rsn:
                return "전략 B"
            if "TYPE_A" in rsn and "TYPE_B" not in rsn:
                return "전략 A"
    except Exception:
        pass
    return format_strategy_type_label(sel_track)


__all__ = [
    "format_strategy_type_label",
    "format_entry_signal_type_label",
]
