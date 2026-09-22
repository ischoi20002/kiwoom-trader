# -*- coding: utf-8 -*-
"""가상포트 탭 non-UI 헬퍼 — 트리 열 폭·주문로그·보유 표시 포맷 (Tk 비의존)."""
from __future__ import annotations

import math
from typing import Any, Dict, Literal, Optional, Tuple

# 포지션 탭 트리 컬럼 기본 폭(px). 사용자 조절·세로 분할은 virtual_portfolio_ui.json
DEFAULT_VIRTUAL_ACTIVE_COL_WIDTHS: Dict[str, int] = {
    "Entry": 140,
    "Code": 72,
    "Name": 120,
    "Qty": 56,
    "PnL": 100,
    "PnLPct": 72,
    "Eval": 100,
    "BuyIn": 96,
    "CurP": 88,
    "AvgP": 88,
    "DayChg": 88,
    "WtPct": 72,
}
DEFAULT_VIRTUAL_ORDER_COL_WIDTHS: Dict[str, int] = {
    "Time": 138,
    "Code": 88,
    "Name": 100,
    "Side": 88,
    "Qty": 88,
    "FQty": 72,
    "OrderP": 88,
    "FilledP": 88,
    "VPOrd": 72,
    "BAOrd": 200,
    "Status": 88,
    "Reason": 120,
}
VIRTUAL_ACTIVE_COLS: Tuple[str, ...] = tuple(DEFAULT_VIRTUAL_ACTIVE_COL_WIDTHS.keys())
VIRTUAL_ORDER_COLS: Tuple[str, ...] = tuple(DEFAULT_VIRTUAL_ORDER_COL_WIDTHS.keys())
VIRTUAL_COL_MIN: Dict[str, int] = {c: 40 for c in VIRTUAL_ACTIVE_COLS}
VIRTUAL_COL_MIN.update(
    {c: 40 for c in VIRTUAL_ORDER_COLS if c not in VIRTUAL_COL_MIN}
)


def format_entry_datetime(entry_raw: object) -> str:
    """진입 시각 ISO 등 → 'YYYY-MM-DD HH:MM' 형태로 표시."""
    try:
        et = str(entry_raw or "").strip()
        if not et:
            return "-"
        if "T" in et:
            d, tpart = et.split("T", 1)
            return f"{d} {tpart[:5]}"
        if len(et) >= 16 and et[4] == "-" and et[7] == "-":
            return et[:16].replace("T", " ")
    except Exception:
        pass
    return (str(entry_raw)[:16] if entry_raw else "-") or "-"


def order_log_status_display(result: object, f_qty: object = None) -> str:
    """주문 로그 '상태': 체결 완료는 사용자 표기 '성공'."""
    try:
        s = str(result or "").strip()
    except Exception:
        return "-"
    su = s.upper()
    if su == "FILLED":
        return "성공"
    if su == "PARTIAL":
        try:
            fq = int(f_qty or 0)
        except (TypeError, ValueError):
            fq = 0
        return f"부분체결({fq})" if fq > 0 else "부분체결"
    if su in ("CANCELLED", "CANCELED", "CANCEL", "CNCL"):
        try:
            if int(f_qty or 0) > 0:
                return f"취소(부분체결 {int(f_qty)}주)"
        except (TypeError, ValueError):
            pass
        return "취소(미체결)"
    if su == "SUCCESS":
        try:
            if int(f_qty or 0) > 0:
                return f"부분체결({int(f_qty)})"
        except (TypeError, ValueError):
            pass
        return "접수"
    return s


def normalize_order_side_raw(side_raw: object) -> Literal["buy", "sell", "unknown"]:
    """DB·코어에서 오는 side 값을 buy/sell로 정규화(필터·표시용)."""
    try:
        s = str(side_raw or "").strip().lower()
    except Exception:
        return "unknown"
    if s in ("buy", "b", "매수", "bid"):
        return "buy"
    if s in ("sell", "s", "매도", "ask"):
        return "sell"
    return "unknown"


def order_log_row_is_filled(result: object, f_price: object, f_qty: object) -> bool:
    """체결 여부: FILLED이거나 체결 수량 1주 이상이면 체결로 간주."""
    try:
        if str(result or "").strip().upper() == "FILLED":
            return True
    except Exception:
        pass
    try:
        if int(f_qty or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    _ = f_price
    return False


def order_log_vp_cell(v: object) -> str:
    """주문 로그용 체결강도 셀(소수 1자리 %)."""
    try:
        if v is None:
            return "-"
        return f"{float(v):.1f}%"
    except Exception:
        return "-"


def parse_virtual_sash_rel(raw: Dict[str, Any]) -> Optional[float]:
    """저장된 상·하단 분할 비율(첫 번째 sash 위치 / Paned 높이)."""
    try:
        v = raw.get("sash0_rel")
        if v is None:
            return None
        x = float(v)
        if x != x:
            return None
        return max(0.08, min(0.88, x))
    except (TypeError, ValueError):
        return None


def merge_virtual_col_widths(
    keys: Tuple[str, ...],
    defaults: Dict[str, int],
    saved: Any,
    col_min: Optional[Dict[str, int]] = None,
) -> Dict[str, int]:
    """저장된 폭이 있으면 병합(없는 열·잘못된 값은 기본값)."""
    mins = col_min if isinstance(col_min, dict) else VIRTUAL_COL_MIN
    out = {k: int(defaults.get(k, 64)) for k in keys}
    if not isinstance(saved, dict):
        return out
    for k, v in saved.items():
        if k not in out:
            continue
        try:
            iw = int(v)
            lo = int(mins.get(k, 32))
            if lo <= iw <= 4000:
                out[k] = iw
        except (TypeError, ValueError):
            pass
    return out


def fmt_active_vs_prev(info: Dict[str, Any]) -> str:
    """전일 종가 대비 등락폭(원). monitor_info['diff']와 감시 탭과 동일 규칙."""
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


def holding_cost_ex_fee(ap: float, qty: int) -> float:
    """매수 수수료를 제외한 매입금액(평균단가×수량)."""
    try:
        if qty <= 0:
            return 0.0
        a = float(ap)
        if abs(a) <= 1e-12:
            return 0.0
        return float(a) * float(qty)
    except (TypeError, ValueError):
        return 0.0


def finite_tree_float(x: object, fallback: float = 0.0) -> float:
    """Treeview 숫자 표시용: nan·inf·변환 실패 시 fallback."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return float(fallback)
    if not math.isfinite(v):
        return float(fallback)
    return float(v)


_DEFAULT_VIRTUAL_ACTIVE_COL_WIDTHS = DEFAULT_VIRTUAL_ACTIVE_COL_WIDTHS
_DEFAULT_VIRTUAL_ORDER_COL_WIDTHS = DEFAULT_VIRTUAL_ORDER_COL_WIDTHS
_VIRTUAL_ACTIVE_COLS = VIRTUAL_ACTIVE_COLS
_VIRTUAL_ORDER_COLS = VIRTUAL_ORDER_COLS
_VIRTUAL_COL_MIN = VIRTUAL_COL_MIN
_format_entry_datetime = format_entry_datetime
_order_log_status_display = order_log_status_display
_normalize_order_side_raw = normalize_order_side_raw
_order_log_row_is_filled = order_log_row_is_filled
_order_log_vp_cell = order_log_vp_cell
_parse_virtual_sash_rel = parse_virtual_sash_rel
_merge_virtual_col_widths = merge_virtual_col_widths
_fmt_active_vs_prev = fmt_active_vs_prev
_holding_cost_ex_fee = holding_cost_ex_fee
_finite_tree_float = finite_tree_float

__all__ = [
    "DEFAULT_VIRTUAL_ACTIVE_COL_WIDTHS",
    "DEFAULT_VIRTUAL_ORDER_COL_WIDTHS",
    "VIRTUAL_ACTIVE_COLS",
    "VIRTUAL_ORDER_COLS",
    "VIRTUAL_COL_MIN",
    "format_entry_datetime",
    "order_log_status_display",
    "normalize_order_side_raw",
    "order_log_row_is_filled",
    "order_log_vp_cell",
    "parse_virtual_sash_rel",
    "merge_virtual_col_widths",
    "fmt_active_vs_prev",
    "holding_cost_ex_fee",
    "finite_tree_float",
]
