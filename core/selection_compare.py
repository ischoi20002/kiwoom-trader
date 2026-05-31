# -*- coding: utf-8 -*-
"""
일일 감시목록(api_only)과 백테스트·DB 일봉(db_only) 선정 결과 비교 유틸.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Tuple

import pandas as pd


def selection_codes_from_df(df: Optional[pd.DataFrame]) -> List[str]:
    """선정 DataFrame → 6자리 코드 목록(순서 유지)."""
    if df is None or df.empty:
        return []
    cols = {str(c).lower(): c for c in df.columns}
    code_col = cols.get("code")
    if code_col is None:
        return []
    try:
        return df[code_col].astype(str).str.strip().str.zfill(6).tolist()
    except Exception:
        return []


def load_selection_csv(path: str) -> pd.DataFrame:
    """선정 CSV 로드. Code 열 필수."""
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    df = pd.read_csv(path, dtype=str)
    if "Code" not in df.columns:
        raise ValueError(f"{path}: 'Code' 열이 없습니다.")
    return df


def compare_selection_code_lists(
    live_codes: List[str],
    bt_codes: List[str],
) -> Dict[str, Any]:
    """두 코드 목록 비교 요약."""
    sa, sb = set(live_codes), set(bt_codes)
    only_live = sorted(sa - sb)
    only_bt = sorted(sb - sa)
    common = [c for c in live_codes if c in sb]
    order_same = live_codes == bt_codes
    first_order_mismatch: Optional[Tuple[int, str, str]] = None
    if sa == sb and not order_same:
        for i, (a, b) in enumerate(zip(live_codes, bt_codes)):
            if a != b:
                first_order_mismatch = (i, a, b)
                break
    return {
        "live_count": len(live_codes),
        "bt_count": len(bt_codes),
        "set_equal": sa == sb,
        "order_equal": order_same,
        "only_live": only_live,
        "only_bt": only_bt,
        "common_count": len(common),
        "first_order_mismatch": first_order_mismatch,
    }


def _row_brief(df: pd.DataFrame, code: str) -> str:
    """비교 로그용 한 줄 요약."""
    try:
        cols = {str(c).lower(): c for c in df.columns}
        ccol = cols.get("code", "Code")
        sub = df[df[ccol].astype(str).str.zfill(6) == code]
        if sub.empty:
            return code
        row = sub.iloc[0]
        name = str(row.get(cols.get("name", "Name"), code))[:14]
        track = str(row.get(cols.get("seltrack", "SelTrack"), "-")).strip().upper() or "-"
        try:
            score = float(row.get(cols.get("score", "Score"), 0))
            sc_txt = f"{score:.1f}"
        except (TypeError, ValueError):
            sc_txt = "-"
        return f"{code} {name} [{track}] 가점={sc_txt}"
    except Exception:
        return code


def log_selection_comparison(
    logger: Callable[[str], None],
    *,
    live_df: Optional[pd.DataFrame],
    bt_df: Optional[pd.DataFrame],
    live_label: str,
    bt_label: str,
    db_eod_hint: str = "",
) -> Dict[str, Any]:
    """
    두 선정 결과를 로그창에 표 형태로 출력합니다.
    반환: compare_selection_code_lists 요약 dict.
    """
    live_codes = selection_codes_from_df(live_df)
    bt_codes = selection_codes_from_df(bt_df)
    summary = compare_selection_code_lists(live_codes, bt_codes)

    logger("\n" + "=" * 102)
    logger("📊 [선정 비교] 일일 감시목록 vs 백테스팅(DB 일봉) 선정")
    if db_eod_hint:
        logger(f"   DB 일봉 기준일: {db_eod_hint}")
    logger(f"   A — {live_label}: {summary['live_count']}종목")
    logger(f"   B — {bt_label}: {summary['bt_count']}종목")
    logger("-" * 102)
    logger(
        f"   집합 동일: {'✅' if summary['set_equal'] else '❌'}  |  "
        f"순서 동일: {'✅' if summary['order_equal'] else '❌'}  |  "
        f"공통: {summary['common_count']}종목"
    )

    if summary["only_live"]:
        logger(f"\n   ▶ A(감시)에만 있음 ({len(summary['only_live'])}개):")
        for code in summary["only_live"]:
            line = _row_brief(live_df, code) if live_df is not None else code
            logger(f"      · {line}")

    if summary["only_bt"]:
        logger(f"\n   ▶ B(백테·DB)에만 있음 ({len(summary['only_bt'])}개):")
        for code in summary["only_bt"]:
            line = _row_brief(bt_df, code) if bt_df is not None else code
            logger(f"      · {line}")

    mismatch = summary.get("first_order_mismatch")
    if mismatch:
        idx, a, b = mismatch
        logger(f"\n   ▶ 순서 불일치 — 인덱스 {idx}: A={a}  B={b}")

    if summary["set_equal"] and summary["order_equal"]:
        logger("\n   ✅ 두 선정 결과가 완전히 일치합니다.")
    elif summary["set_equal"]:
        logger("\n   ⚠️ 종목 집합은 같지만 순위(정렬)가 다릅니다.")

    logger("=" * 102 + "\n")
    return summary
