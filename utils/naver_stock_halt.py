# -*- coding: utf-8 -*-
"""
네이버 금융 실시간 시세 API 기반 거래정지(HALTED) 여부 조회.

- 엔드포인트: GET .../api/realtime/domestic/stock/{code1},{code2},...
- 응답의 tradeStopType.name == 'HALTED' 이면 거래정지로 본다.
- API 오류 시 빈 집합 반환(선정 필터는 생략 — 전 종목 차단 방지).
"""

from __future__ import annotations

from typing import Iterable, Set

import requests

_NAVER_BATCH_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/"


def _trade_stop_is_halted(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    name = str(obj.get("name", "") or "").strip().upper()
    return name == "HALTED"


def _item_is_halted(item: dict) -> bool:
    if _trade_stop_is_halted(item.get("tradeStopType")):
        return True
    omp = item.get("overMarketPriceInfo")
    if isinstance(omp, dict) and _trade_stop_is_halted(omp.get("tradeStopType")):
        return True
    return False


def fetch_naver_halted_codes(
    codes: Iterable[str],
    *,
    chunk_size: int = 40,
    timeout: float = 18.0,
) -> Set[str]:
    """
    6자리 종목코드 목록에 대해 네이버 실시간 API로 거래정지(HALTED) 종목 코드 집합을 반환합니다.

    Parameters
    ----------
    codes : 종목코드(문자열, 6자리 권장)
    chunk_size : 요청당 최대 종목 수(URL 길이·서버 제한 완화)
    timeout : HTTP 타임아웃(초)
    """
    halted: Set[str] = set()
    norm = [str(c).strip().zfill(6) for c in codes if c is not None and str(c).strip()]
    if not norm:
        return halted

    for i in range(0, len(norm), max(1, int(chunk_size))):
        chunk = norm[i : i + max(1, int(chunk_size))]
        url = _NAVER_BATCH_URL + ",".join(chunk)
        try:
            resp = requests.get(url, timeout=timeout)
            if resp.status_code != 200:
                continue
            data = resp.json()
            rows = data.get("datas")
            if not isinstance(rows, list):
                continue
            for item in rows:
                if not isinstance(item, dict):
                    continue
                code = str(item.get("itemCode", "") or "").zfill(6)
                if not code or code == "000000":
                    continue
                if _item_is_halted(item):
                    halted.add(code)
        except Exception:
            continue
    return halted
