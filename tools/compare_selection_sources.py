# -*- coding: utf-8 -*-
"""
``bars_1d`` 기반 선정(``db_only``)과 키움 ``ka10081`` 기반 선정(``api_only`` + 토큰) 결과를 비교합니다.

환경 변수 ``KIWOOM_ACCESS_TOKEN`` 에 Bearer 토큰을 넣거나 ``--token`` 으로 전달하세요.

예::

    set KIWOOM_ACCESS_TOKEN=여기에_토큰
    python tools/compare_selection_sources.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple


def _root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _load_settings(root: str) -> Dict[str, Any]:
    sys.path.insert(0, root)
    from core.defaults import DEFAULT_SETTINGS

    merged: Dict[str, Any] = dict(DEFAULT_SETTINGS)
    cfg = os.path.join(root, "settings.json")
    if os.path.isfile(cfg):
        with open(cfg, encoding="utf-8") as f:
            u = json.load(f)
        if isinstance(u, dict):
            merged.update(u)
    return merged


def _codes(df: Any) -> List[str]:
    import pandas as pd

    if df is None or (isinstance(df, pd.DataFrame) and df.empty):
        return []
    return df["Code"].astype(str).str.zfill(6).tolist()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--token",
        default=os.environ.get("KIWOOM_ACCESS_TOKEN", "").strip(),
        help="키움 REST Bearer 토큰 (미입력 시 환경변수 KIWOOM_ACCESS_TOKEN)",
    )
    args = ap.parse_args()
    root = _root()
    os.chdir(root)
    if root not in sys.path:
        sys.path.insert(0, root)

    settings = _load_settings(root)
    from data_manager_v2 import DataManagerV2
    import strategy_selector

    dm = DataManagerV2()

    def _log(_: str) -> None:
        pass

    try:
        df_db = strategy_selector.select_monitoring_targets(
            settings=settings,
            logger_callback=_log,
            data_manager=dm,
            selection_ohlcv_source="db_only",
        )
    except strategy_selector.SelectionDbStaleError as e:
        print(f"[DB-only] 중단: {e}", file=sys.stderr)
        return 2

    tok = (args.token or "").strip()
    if not tok:
        print(
            "[비교] KIWOOM_ACCESS_TOKEN 또는 --token 이 없어 키움 경로는 생략합니다.",
            file=sys.stderr,
        )
        print("[DB-only]", _codes(df_db))
        return 1

    try:
        df_kw = strategy_selector.select_monitoring_targets(
            settings=settings,
            logger_callback=_log,
            data_manager=None,
            selection_ohlcv_source="api_only",
            kiwoom_access_token=tok,
        )
    except Exception as e:
        print(f"[Kiwoom] 실패: {e}", file=sys.stderr)
        return 3

    a = _codes(df_db)
    b = _codes(df_kw)
    sa, sb = set(a), set(b)
    print("=== 요약 ===")
    print(f"db_only 개수: {len(a)}")
    print(f"kiwoom   개수: {len(b)}")
    print(f"집합 동일: {sa == sb}")
    print(f"순서 동일: {a == b}")
    if sa != sb:
        print("db_only 에만:", sorted(sa - sb))
        print("kiwoom 에만:", sorted(sb - sa))
    if a != b and sa == sb:
        for i, (x, y) in enumerate(zip(a, b)):
            if x != y:
                print(f"첫 순서 불일치 인덱스 {i}: db={x} kw={y}")
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
