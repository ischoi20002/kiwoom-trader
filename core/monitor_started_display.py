# -*- coding: utf-8 -*-
"""
실시간 감시 ``STARTED`` 상태 콜백 표시 문자열.

워밍업·웹소켓 구독 대상은 ``일일 선정 ∪ 관심목록 ∪ 가상 보유`` 병합이므로,
종목 수만 표시하면 사용자가 '선정 수'와 혼동하기 쉽다.
"""
from __future__ import annotations

from typing import Any


def format_monitor_started_status_text(data: Any) -> str:
    """
    ``TradingController`` → ``status_cb("STARTED", data)`` 의 ``data`` 를
    상단 상태 라벨용 한 줄 문자열로 변환한다.

    - **dict** (권장): ``n_selected`` 일일 선정(``monitor_codes``),
      ``n_stream`` 워밍업 성공·틱 수신 대상(병합 목록 기준).
    - **int** (하위 호환): 단일 숫자만 표시.
    """
    try:
        if isinstance(data, dict):
            n_sel = int(data.get("n_selected", 0) or 0)
            n_stream = int(data.get("n_stream", 0) or 0)
            incremental = bool(data.get("incremental"))
            if incremental:
                return f"상태: 감시 중 (선정 {n_sel} · 연결 진행 {n_stream})"
            if n_stream <= 0 and n_sel <= 0:
                return "상태: 감시 중"
            if n_sel > 0 and n_stream > 0 and n_stream != n_sel:
                return (
                    f"상태: 감시 중 (일일 선정 {n_sel} · 실시간 수신 {n_stream})"
                )
            if n_stream > 0:
                return f"상태: 감시 중 ({n_stream}종목)"
            if n_sel > 0:
                return f"상태: 감시 중 ({n_sel}종목)"
            return "상태: 감시 중"
        n = int(data)
        return f"상태: 감시 중 ({n}종목)"
    except Exception:
        return "상태: 감시 중"
