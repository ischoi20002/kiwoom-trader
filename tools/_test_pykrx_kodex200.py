# -*- coding: utf-8 -*-
"""일회성: kodex200_universe._try_pykrx_kodex200_etf 로 KODEX200(069500) 조회 검증."""
import sys

def main() -> int:
    sys.path.insert(0, ".")
    try:
        from core.kodex200_universe import _try_pykrx_kodex200_etf
    except Exception as e:
        print("import 실패:", e)
        return 1

    def _log(msg: str) -> None:
        print(msg)

    print("--- _try_pykrx_kodex200_etf(log=...) ---")
    try:
        df = _try_pykrx_kodex200_etf(log=_log)
    except Exception as e:
        print("EXCEPTION:", type(e).__name__, e)
        import traceback

        traceback.print_exc()
        return 2

    if df is None or getattr(df, "empty", True):
        print("결과: 실패(None 또는 빈 DF)")
        return 3

    print("성공: 행 수", len(df))
    print(df.head(3))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
