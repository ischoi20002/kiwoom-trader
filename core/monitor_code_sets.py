# -*- coding: utf-8 -*-
"""
실시간 감시 UI(`monitor_tab`)와 틱 처리(`signal_engine`)가 동일 규칙으로 쓰는
종목 코드 집합 계산 — **관심목록 전용(wl_only)** vs **감시 선정(monitor_codes)** 구분.

과거에는 한쪽만 `split('_')[0]`를 쓰고 한쪽은 `zfill`만 쓰는 등 미세 차이로
화면과 시그널 동작이 어긋날 수 있어, 여기서만 정규화·차집합을 정의한다.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Set


def normalize_krx_symbol(code: Any) -> str:
    """
    종목 코드를 6자리 문자열로 통일한다.
    키움/내부에서 `003490_1` 형태가 섞여 들어와도 접미사 앞만 사용한다.
    """
    try:
        s = str(code).strip()
        if not s:
            return ""
        base = str(s.split("_")[0]).strip()
        if not base:
            return ""
        return base.zfill(6)
    except Exception:
        return ""


def monitor_selection_codes_set(monitor_codes: Optional[Iterable[Any]]) -> Set[str]:
    """
    `trading_controller.monitor_codes` 등 **실시간 감시 선정** 종목의 6자리 집합.

    Parameters
    ----------
    monitor_codes :
        선정 코드 시퀀스. None 이면 빈 집합.
    """
    out: Set[str] = set()
    try:
        for c in monitor_codes or []:
            nk = normalize_krx_symbol(c)
            if nk:
                out.add(nk)
    except Exception:
        pass
    return out


def watchlist_only_codes_set(
    watchlist_codes: Optional[Iterable[Any]],
    monitor_codes: Optional[Iterable[Any]],
) -> Set[str]:
    """
    관심목록에는 있으나 감시 선정(`monitor_codes`)에는 **없는** 6자리 코드 집합.

    감시 선정 종목이 관심목록에도 있으면 여기에 포함되지 않는다
    (감시·보유 트리에 정상 표시, 틱 트리 큐잉도 동일).
    """
    mon = monitor_selection_codes_set(monitor_codes)
    out: Set[str] = set()
    try:
        for c in watchlist_codes or []:
            nk = normalize_krx_symbol(c)
            if nk and nk not in mon:
                out.add(nk)
    except Exception:
        pass
    return out
