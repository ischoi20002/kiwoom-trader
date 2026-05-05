# -*- coding: utf-8 -*-
"""
텔레그램 알림·실시간 신호 테스트용 관심목록.
감시목록과 독립(등록해도 감시목록에서 제거하지 않음).
포지션 관리·일일 결산 등 계좌 집계와는 별개이며, 해당 화면/리포트에서 관심 여부로 종목을 빼거나 넣지 않습니다.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional

from core.monitor_code_sets import watchlist_only_codes_set

_DEFAULT_FILE = "watchlist_favorites.json"


def _parse_avg_price(raw: Any) -> Optional[float]:
    """JSON/입력에서 평단(기준가) 파싱. 없음·0 이하·비정상은 None."""
    if raw is None or raw == "":
        return None
    try:
        v = float(raw)
        if v <= 0 or v != v or abs(v) == float("inf"):
            return None
        return v
    except (TypeError, ValueError):
        return None


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
                                "avg_price": _parse_avg_price(it.get("avg_price")),
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

    def preload_from_kodex200_csv(
        self,
        csv_path: Optional[str] = None,
        *,
        logger_callback: Optional[Callable[[str], None]] = None,
    ) -> int:
        """
        `kodex200.csv`에서 Code/Name을 읽어 watchlist_store에 선적재합니다.

        목적:
        실시간 감시에서 종목명 표시를 위해 `pykrx`를 호출하지 않게 하여
        (특히) `KRX_ID/KRX_PW` 환경변수가 없을 때의 인증 실패 로그를 줄입니다.
        """
        log = logger_callback or self._log
        path = csv_path or os.path.join(os.getcwd(), "kodex200.csv")

        try:
            if not os.path.isfile(path):
                return 0

            # 프로젝트 내 pandas 사용을 전제로, 동적 import로 의존 지점을 제한합니다.
            import pandas as pd  # type: ignore

            df = pd.read_csv(path, dtype=str)
        except Exception as e:
            try:
                log(f"[Watchlist] kodex200.csv 선적재 실패: {e}")
            except Exception:
                pass
            return 0

        code_col: Optional[str] = None
        name_col: Optional[str] = None
        if "종목코드" in df.columns and "종목명" in df.columns:
            code_col, name_col = "종목코드", "종목명"
        elif "Code" in df.columns:
            if "Name" in df.columns:
                code_col, name_col = "Code", "Name"
            elif "종목명" in df.columns:
                code_col, name_col = "Code", "종목명"

        if not code_col or not name_col:
            return 0

        try:
            existing = self.codes_set()
            added = 0
            sub = df[[code_col, name_col]].copy()

            for _, row in sub.iterrows():
                try:
                    raw_code = row.get(code_col)
                    raw_name = row.get(name_col)
                    if raw_code is None:
                        continue

                    c = str(raw_code).strip()
                    if not c or not c.isdigit():
                        continue
                    c = c.zfill(6)
                    if c in existing:
                        continue

                    nm = "" if raw_name is None else str(raw_name).strip()
                    if not nm:
                        nm = c

                    if self.add(c, nm, avg_price=None):
                        existing.add(c)
                        added += 1
                except Exception:
                    continue

            if added > 0:
                try:
                    log(f"[Watchlist] kodex200.csv 선적재: {added}개 추가됨")
                except Exception:
                    pass
            return added
        except Exception as e:
            try:
                log(f"[Watchlist] kodex200.csv 선적재 처리 실패: {e}")
            except Exception:
                pass
            return 0

    def codes_set(self) -> set:
        """정규화된 6자리 코드 집합 (감시 화면·포지션 관리 탭에서 숨길 때 사용)."""
        with self._lock:
            return {str(x["code"]).zfill(6) for x in self._items}

    def codes_report_exclusion_set(
        self, monitor_codes: Optional[Iterable[str]] = None
    ) -> set:
        """
        (레거시) 관심만·비감시 코드 집합. 포지션/결산 리포트는 더 이상 이 집합으로 필터하지 않습니다.
        스크립트·외부 연동 호환용으로 메서드는 유지합니다.
        감시 선정과의 차집합은 `core.monitor_code_sets.watchlist_only_codes_set` 과 동일 규칙이다.
        """
        try:
            wl = self.codes_set()
            return watchlist_only_codes_set(wl, monitor_codes or [])
        except Exception:
            return set()

    def get_name(self, code: str) -> str:
        c = str(code).strip().zfill(6)
        with self._lock:
            for it in self._items:
                if it["code"] == c:
                    return it.get("name", c)
        return c

    def get_avg_price(self, code: str) -> Optional[float]:
        """매도 기준(평단)가. 없으면 None — 매수 신호만 감시."""
        c = str(code).strip().zfill(6)
        with self._lock:
            for it in self._items:
                if it["code"] == c:
                    return _parse_avg_price(it.get("avg_price"))
        return None

    def _resolve_name_from_market(self, code: str) -> str:
        """코드만 있을 때 시장 표지명 조회 (pykrx). 실패 시 코드 반환."""
        try:
            # pykrx의 일부 경로(시장 정보 스크래핑/인증)는 KRX_ID/KRX_PW 환경변수를 요구합니다.
            # 실시간 감시는 시작 즉시 많은 종목에 대해 이름이 필요할 수 있으므로,
            # 환경변수가 없으면 즉시 스킵(종목코드 그대로 반환)해서 불필요한 인증 실패 로그를 줄입니다.
            krx_id = os.environ.get("KRX_ID", "").strip()
            krx_pw = os.environ.get("KRX_PW", "").strip()
            if not krx_id or not krx_pw:
                return code

            from pykrx import stock

            n = stock.get_market_ticker_name(code)
            if n and str(n).strip():
                return str(n).strip()
        except Exception:
            pass
        return code

    def get_display_name(self, code: str) -> str:
        """텔레그램/UI용: 저장된 이름이 없거나 코드와 같으면 시장명 조회."""
        c = str(code).strip().zfill(6)
        nm = self.get_name(c)
        if nm and nm != c and len(str(nm).strip()) >= 1:
            return str(nm).strip()
        return self._resolve_name_from_market(c)

    def add(self, code: str, name: str = "", avg_price: Optional[float] = None) -> bool:
        c = str(code).strip().zfill(6)
        if len(c) != 6 or not c.isdigit():
            return False
        nm = (name or "").strip()
        if not nm or nm == c:
            nm = self._resolve_name_from_market(c)
        ap = _parse_avg_price(avg_price)
        with self._lock:
            for it in self._items:
                if it["code"] == c:
                    it["name"] = nm
                    it["avg_price"] = ap
                    return True
            self._items.append({"code": c, "name": nm, "avg_price": ap})
        return True

    def remove(self, code: str) -> bool:
        c = str(code).strip().zfill(6)
        with self._lock:
            before = len(self._items)
            self._items = [x for x in self._items if x["code"] != c]
            return len(self._items) < before
