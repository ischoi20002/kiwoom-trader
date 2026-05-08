# -*- coding: utf-8 -*-
"""
``bars_data`` dict 의 종목코드 키 정규화·별칭 승격.

워밍업은 ``005930`` 로 저장했는데 일부 경로에서 ``5930`` 등으로 조회하면
``StreamProcessor`` 가 봉이 없다고 판단해 1행짜리 DF를 새로 만들어
합치도·봉수가 초기화되는 문제가 생길 수 있다.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from utils.normalization import canonical_stock_code


def resolve_bars_data_frame(
    bars_data: Optional[Dict[str, Any]],
    code: object,
    *,
    migrate_alias: bool = True,
) -> Tuple[str, Optional[Any]]:
    """
    canonical 6자리 키와 해당 DataFrame 을 반환합니다.

    ``migrate_alias=True`` 이면 동일 종목으로 판단되는 옛 키를 canonical 로 합칩니다.
    """
    try:
        nk = canonical_stock_code(code)
        bd: Dict[str, Any] = bars_data if isinstance(bars_data, dict) else {}
        if not nk:
            return "", None
        if nk in bd:
            return nk, bd.get(nk)
        for k in list(bd.keys()):
            try:
                kk = canonical_stock_code(k)
            except Exception:
                continue
            if kk != nk:
                continue
            df = bd.get(k)
            if migrate_alias:
                try:
                    if nk not in bd and df is not None:
                        bd[nk] = df
                    if k != nk:
                        bd.pop(k, None)
                except Exception:
                    pass
            return nk, bd.get(nk)
        return nk, None
    except Exception:
        try:
            nk = canonical_stock_code(code)
            if nk:
                return nk, (bars_data or {}).get(nk)
            return "", None
        except Exception:
            return "", None
