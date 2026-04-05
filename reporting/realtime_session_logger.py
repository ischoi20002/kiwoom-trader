# -*- coding: utf-8 -*-
"""
실시간 감시 세션 일별 JSONL 로거.
매수/매도 신호, 주문, 체결, 스킵 사유 등을 한 줄 JSON으로 누적하여 사후 분석·원인 추적에 사용합니다.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from typing import Any, Dict, Optional

import numpy as np


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _json_safe(obj: Any) -> Any:
    """JSON 직렬화 가능한 기본 타입으로 정규화합니다."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, bool)):
        return obj
    if isinstance(obj, float):
        return float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


class RealtimeSessionLogger:
    """
    날짜별 폴더 `realtime_session_logs/YYYY-MM-DD/session_events.jsonl`에 이벤트를 추가합니다.
    """

    def __init__(self, base_dir: Optional[str] = None) -> None:
        self._base = base_dir or os.path.join(_project_root(), "realtime_session_logs")
        self._lock = threading.Lock()
        try:
            os.makedirs(self._base, exist_ok=True)
        except OSError:
            pass

    def _daily_dir(self, day: Optional[str] = None) -> str:
        d = day or datetime.now().strftime("%Y-%m-%d")
        path = os.path.join(self._base, d)
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass
        return path

    def jsonl_path(self, day: Optional[str] = None) -> str:
        """해당 영업일의 JSONL 파일 경로."""
        return os.path.join(self._daily_dir(day), "session_events.jsonl")

    def log(
        self,
        kind: str,
        payload: Optional[Dict[str, Any]] = None,
        *,
        day: Optional[str] = None,
    ) -> None:
        """
        한 건의 이벤트를 JSONL에 추가합니다.

        Parameters
        ----------
        kind : str
            예: signal_s1_buy, signal_buy_skipped, order_submit, order_reject, fill, strategy_exit, ...
        payload : dict, optional
            부가 정보(종목코드, 가격, 사유 등).
        day : str, optional
            강제 일자(YYYY-MM-DD). None이면 오늘.
        """
        try:
            rec: Dict[str, Any] = {
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "kind": kind,
                "data": _json_safe(payload) if payload else {},
            }
            line = json.dumps(rec, ensure_ascii=False) + "\n"
            path = self.jsonl_path(day)
            with self._lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            # 로깅 실패가 매매 경로를 막지 않도록 삼킵니다.
            pass

    def read_events(self, day: Optional[str] = None) -> list:
        """해당 일의 JSONL을 파싱한 리스트 (리포트·분석용)."""
        path = self.jsonl_path(day)
        out: list = []
        if not os.path.isfile(path):
            return out
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass
        return out
