# -*- coding: utf-8 -*-
"""
SignalEngine 감시 트리 상태표시·라벨 헬퍼 (Tk 비의존).

합치도(confluence) ↔ status_text 동기화, S1 합치도 문구, 전략유형 라벨.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from core.monitor_strategy_labels import format_entry_signal_type_label


def _default_to_scalar(val: Any, default: Any = 0) -> Any:
    """SignalEngine._to_scalar 최소 대체 (pandas Series / numpy 스칼라)."""
    try:
        if val is None:
            return default
        if hasattr(val, "iloc"):
            if hasattr(val, "size") and val.size == 0:
                return default
            return _default_to_scalar(val.iloc[0], default)
        if hasattr(val, "item") and not hasattr(val, "shape"):
            return val.item()
        if isinstance(val, (list,)) and len(val) > 0:
            return _default_to_scalar(val[0], default)
        return val
    except Exception:
        return default


def format_s1_confluence_status_line(
    last_sig: Any,
    sel_track: str = "AB",
    *,
    to_scalar: Optional[Callable[[Any, Any], Any]] = None,
) -> str:
    """
    S1 합치도 표시: 눌림목(A)·추세돌파(B)·박스반등(C) 충족 수를 보여준다.
    """
    _ts = to_scalar or _default_to_scalar
    try:
        tr_raw = str(sel_track or "AB").strip().upper()
        tr = "".join(x for x in ("A", "B", "C") if x in tr_raw) or "AB"
        ma_c = int(_ts(last_sig.get("Met_A", 0), 0))
        ta_t = max(1, int(_ts(last_sig.get("Tot_A", 1), 1)))
        mb_c = int(_ts(last_sig.get("Met_B", 0), 0))
        tb_t = max(1, int(_ts(last_sig.get("Tot_B", 1), 1)))
        mc_c = int(_ts(last_sig.get("Met_C", 0), 0))
        tc_t = max(1, int(_ts(last_sig.get("Tot_C", 1), 1)))
        if tr == "A":
            return f"눌림A {ma_c}/{ta_t}"
        if tr == "B":
            return f"추세B {mb_c}/{tb_t}"
        if tr == "C":
            return f"박스C {mc_c}/{tc_t}"
        st = str(_ts(last_sig.get("Sig_Type", "NONE"), "NONE"))
        if st == "TYPE_C":
            return f"박스C {mc_c}/{tc_t}"
        if st == "TYPE_B":
            return f"추세B {mb_c}/{tb_t} (눌림A {ma_c}/{ta_t})"
        if st == "TYPE_A":
            return f"눌림A {ma_c}/{ta_t} (추세B {mb_c}/{tb_t})"
        parts = []
        if "A" in tr:
            parts.append(f"눌림A {ma_c}/{ta_t}")
        if "B" in tr:
            parts.append(f"추세B {mb_c}/{tb_t}")
        if "C" in tr:
            parts.append(f"박스C {mc_c}/{tc_t}")
        return " · ".join(parts) if parts else "—"
    except Exception:
        try:
            mc = int(_ts(last_sig["Met_Count"], 0))
            tc = max(1, int(_ts(last_sig["Total_Count"], 1)))
            return f"({mc}/{tc})"
        except Exception:
            return "—"


def clear_warmup_status_if_placeholder(info: Dict[str, Any]) -> None:
    """
    wl_only 등에서 status_text가 워밍업 완료 문구로 고정되지 않도록
    confluence 플레이스홀더일 때 '대기'로 되돌린다.
    """
    try:
        conf_s = str(info.get("confluence", "")).strip()
        prev_s = str(info.get("status_text", "")).strip()
        if prev_s.startswith("✅ 15분봉") and (not conf_s or conf_s in ("-", "—")):
            info["status_text"] = "대기"
    except Exception:
        pass


def sync_status_text_from_confluence(info: Dict[str, Any]) -> None:
    """
    신호상태 열용 status_text 동기화.
    confluence가 유효하면 복사하고, 플레이스홀더+워밍업 문구면 '대기'로 리셋.
    """
    try:
        conf_s = str(info.get("confluence", "")).strip()
        prev_s = str(info.get("status_text", "")).strip()
        if conf_s and conf_s not in ("-", "—"):
            info["status_text"] = conf_s
        else:
            if prev_s.startswith("✅ 15분봉"):
                info["status_text"] = "대기"
            elif not prev_s:
                info["status_text"] = "대기"
    except Exception:
        pass


def resolve_monitor_strat_label(
    *,
    pos: Any,
    sel_track: object,
    code: str,
    app: Any = None,
) -> str:
    """감시 트리 '전략유형' 셀 문자열."""
    try:
        return format_entry_signal_type_label(
            pos if isinstance(pos, dict) else None,
            sel_track,
            code=code,
            app=app,
        )
    except Exception:
        try:
            return format_entry_signal_type_label(
                None, sel_track, code=code, app=app
            )
        except Exception:
            return "—"


def format_monitor_vs_prev_cell(info: Dict[str, Any]) -> str:
    """감시 트리 '대비' 열: 전일 종가 대비 등락폭(원)."""
    try:
        raw = info.get("diff")
        if raw is None:
            return "—"
        s = str(raw).strip().replace(",", "")
        if s in ("", "-", "—"):
            return "—"
        iv = int(round(float(s)))
        if iv == 0:
            return "0"
        return f"{iv:+,}"
    except Exception:
        return "—"


__all__ = [
    "format_s1_confluence_status_line",
    "clear_warmup_status_if_placeholder",
    "sync_status_text_from_confluence",
    "resolve_monitor_strat_label",
    "format_monitor_vs_prev_cell",
]
