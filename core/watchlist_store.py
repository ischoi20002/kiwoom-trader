# -*- coding: utf-8 -*-
"""
텔레그램 알림 전용 관심목록.
감시목록과 독립(등록해도 감시목록에서 제거하지 않음).
UI·가상포지션에서는 관심 종목 숨김. 리포트는 '관심만·비감시' 종목만 제외(감시와 겹치면 리포트 포함).
"""
from __future__ import annotations

import json
import os
import threading
from typing import Callable, Dict, Iterable, List, Optional

_DEFAULT_FILE = "watchlist_favorites.json"


class WatchlistStore:
    """관심 종목 code/name 을 JSON으로 보관합니다."""

    def __init__(
        self,
        path: Optional[str] = None,
        logger_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._path = path or os.path.join(os.getcwd(), _DEFAULT_FILE)
        self._log = logger_callback or (lambda _m: None)
        self._lock = threading.Lock()
        self._items: List[Dict[str, str]] = []
        self._load()

    def _load(self) -> None:
        try:
            if os.path.isfile(self._path):
                with open(self._path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                raw = data.get("items", []) if isinstance(data, dict) else []
                out: List[Dict[str, str]] = []
                for it in raw:
                    if not isinstance(it, dict):
                        continue
                    c = str(it.get("code", "")).strip().zfill(6)
                    if len(c) == 6 and c.isdigit():
                        out.append(
                            {
                                "code": c,
                                "name": str(it.get("name", "") or "").strip() or c,
                            }
                        )
                self._items = out
        except Exception as e:
            self._log(f"[Watchlist] 로드 실패: {e}")
            self._items = []

    def save(self) -> bool:
        try:
            with self._lock:
                payload = {"items": list(self._items)}
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            self._log(f"[Watchlist] 저장 실패: {e}")
            return False

    def get_items(self) -> List[Dict[str, str]]:
        with self._lock:
            return [dict(x) for x in self._items]

    def get_codes(self) -> List[str]:
        with self._lock:
            return [x["code"] for x in self._items]

    def codes_set(self) -> set:
        """정규화된 6자리 코드 집합 (감시 화면·가상포지션 탭에서 숨길 때 사용)."""
        with self._lock:
            return {str(x["code"]).zfill(6) for x in self._items}

    def codes_report_exclusion_set(
        self, monitor_codes: Optional[Iterable[str]] = None
    ) -> set:
        """
        HTML/나러티브 리포트에서 제외할 코드 = 관심목록 \\ 감시목록.
        감시에도 있으면 리포트에 그대로 포함(예전과 동일).
        """
        wl = self.codes_set()
        try:
            mon = {str(c).zfill(6) for c in (monitor_codes or [])}
        except Exception:
            mon = set()
        return wl - mon

    def get_name(self, code: str) -> str:
        c = str(code).strip().zfill(6)
        with self._lock:
            for it in self._items:
                if it["code"] == c:
                    return it.get("name", c)
        return c

    def add(self, code: str, name: str = "") -> bool:
        c = str(code).strip().zfill(6)
        if len(c) != 6 or not c.isdigit():
            return False
        nm = (name or "").strip() or c
        with self._lock:
            for it in self._items:
                if it["code"] == c:
                    it["name"] = nm
                    return True
            self._items.append({"code": c, "name": nm})
        return True

    def remove(self, code: str) -> bool:
        c = str(code).strip().zfill(6)
        with self._lock:
            before = len(self._items)
            self._items = [x for x in self._items if x["code"] != c]
            return len(self._items) < before
