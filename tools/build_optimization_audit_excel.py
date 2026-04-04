# -*- coding: utf-8 -*-
"""
최적화 관련 파라미터 감사용 엑셀 생성.
실행: 프로젝트 루트에서  python tools/build_optimization_audit_excel.py
출력: tools/optimization_params_audit.xlsx
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    print("pandas 가 필요합니다: pip install pandas openpyxl", file=sys.stderr)
    sys.exit(1)

# 프로젝트 루트를 path 에 추가
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.defaults import DEFAULT_SETTINGS, get_allowed_keys  # noqa: E402
from parameter_optimizer import SIGNAL_ONLY_PARAMS, _INDICATOR_PARAMS  # noqa: E402
from tools.settings_tab_ui_catalog import KEY_TO_UI_ROW  # noqa: E402

_MISSING_UI_SORT = 99_999_999


def _parse_opt_keys(gui_path: Path) -> list[str]:
    text = gui_path.read_text(encoding="utf-8")
    m = re.search(r"opt_keys\s*=\s*\[(.*?)\]\s*\n\s*for key in opt_keys", text, re.DOTALL)
    if not m:
        raise RuntimeError("k_trader_gui.py 에서 opt_keys 블록을 찾지 못했습니다.")
    body = m.group(1)
    return re.findall(r"'([a-z0-9_]+)'", body)


def _parse_param_info_keys(bt_path: Path) -> set[str]:
    text = bt_path.read_text(encoding="utf-8")
    start = text.find("self.PARAM_INFO = {")
    if start < 0:
        raise RuntimeError("backtest_tab.py 에서 PARAM_INFO 를 찾지 못했습니다.")
    depth = 0
    i = start + len("self.PARAM_INFO = ")
    while i < len(text) and text[i] != "{":
        i += 1
    start_brace = i
    depth = 1
    i = start_brace + 1
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    block = text[start_brace:i]
    return set(re.findall(r"'([a-z0-9_]+)'\s*:\s*\(", block))


def _parse_search_space_keys(bt_path: Path) -> set[str]:
    text = bt_path.read_text(encoding="utf-8")
    start = text.find("def _get_dynamic_search_space")
    if start < 0:
        raise RuntimeError("_get_dynamic_search_space 를 찾지 못했습니다.")
    ret = text.find("return {", start)
    if ret < 0:
        raise RuntimeError("return { 를 찾지 못했습니다.")
    depth = 1
    i = ret + len("return {")
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    block = text[ret + len("return ") : i]
    return set(re.findall(r"'([a-z0-9_]+)'\s*:", block))


def main() -> None:
    gui_path = ROOT / "k_trader_gui.py"
    bt_path = ROOT / "ui" / "tabs" / "backtest_tab.py"

    opt_keys = set(_parse_opt_keys(gui_path))
    param_info_keys = _parse_param_info_keys(bt_path)
    search_keys = _parse_search_space_keys(bt_path)

    allowed = get_allowed_keys()
    default_keys = set(DEFAULT_SETTINGS.keys())

    union = (
        default_keys
        | opt_keys
        | param_info_keys
        | search_keys
        | set(SIGNAL_ONLY_PARAMS)
        | set(_INDICATOR_PARAMS)
    )

    def _ui_sort(k: str) -> tuple[int, str]:
        u = KEY_TO_UI_ROW.get(k)
        return (u.sort_key if u is not None else _MISSING_UI_SORT, k)

    union_sorted = sorted(union, key=_ui_sort)

    rows: list[dict[str, object]] = []
    yn = lambda s: "Y" if s else "N"  # noqa: E731
    for idx, key in enumerate(union_sorted, start=1):
        in_def = key in default_keys
        in_allowed = key in allowed
        in_ui = key in opt_keys
        in_space = key in search_keys
        in_pinfo = key in param_info_keys
        in_sig = key in SIGNAL_ONLY_PARAMS
        in_ind = key in _INDICATOR_PARAMS

        ui_row = KEY_TO_UI_ROW.get(key)
        if ui_row is not None:
            sk = ui_row.sort_key
            sec = ui_row.section_sidebar
            panel = ui_row.panel_title
            block = ui_row.block_header
            screen = ui_row.screen_label
        else:
            sk = _MISSING_UI_SORT
            sec = "(설정탭에 없음 · 레거시/내부전용)"
            panel = ""
            block = ""
            screen = ""

        mismatch_parts: list[str] = []
        if in_ui and not in_space:
            mismatch_parts.append("UI토글O_탐색공간X")
        if in_space and not in_ui:
            mismatch_parts.append("탐색공간O_UI토글X")
        if in_ui and not in_pinfo:
            mismatch_parts.append("UI토글O_PARAM_INFOX")
        if in_ind and in_sig:
            mismatch_parts.append("지표파라미터와_signal_only_동시포함(버그)")
        if in_def and not in_allowed:
            mismatch_parts.append("기본값있음_allowed_keys밖")

        rows.append(
            {
                "순번": idx,
                "설정탭_정렬키": sk if sk < _MISSING_UI_SORT else "",
                "설정탭_대분류_사이드바": sec,
                "설정탭_중분류_패널제목": panel,
                "설정탭_소분류_구역헤더": block,
                "설정탭_화면항목명": screen,
                "parameter_key": key,
                "DEFAULT_SETTINGS_포함": yn(in_def),
                "기본값_샘플": DEFAULT_SETTINGS.get(key, ""),
                "allowed_keys_포함": yn(in_allowed),
                "01_UI최적화토글_k_trader_gui": yn(in_ui),
                "02_탐색공간_backtest_tab": yn(in_space),
                "03_결과표_PARAM_INFO": yn(in_pinfo),
                "04_signal_only_화이트리스트_optimizer": yn(in_sig),
                "05_지표계산_INDICATOR_PARAMS": yn(in_ind),
                "자동_불일치_힌트": "|".join(mismatch_parts),
                "최적화대상_사용자지정_Y만입력": "",
                "비고_사용자": "",
            }
        )

    df = pd.DataFrame(rows)

    out_path = ROOT / "tools" / "optimization_params_audit.xlsx"
    try:
        with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
            guide = pd.DataFrame(
                {
                    "안내": [
                        "이 파일은 코드에서 자동 생성되었습니다.",
                        "행 순서는 설정 탭(좌측 ①~⑦)과 동일한 흐름이며, 설정탭_* 열이 카테고리·화면 문구입니다.",
                        "설정 UI 매핑 원본: tools/settings_tab_ui_catalog.py (settings_tab.py 변경 시 동기화)",
                        "「최적화대상_사용자지정_Y만입력」 열에 최적화에 넣을 키만 Y 를 입력해 주세요.",
                        "작업 후 저장한 파일을 프로젝트에 두고, 채팅에서 해당 엑셀을 기준으로 반영 요청하면 됩니다.",
                        "",
                        "열 설명:",
                        "01: 설정 탭 ○/● 최적화 토글이 붙는 키 (k_trader_gui opt_keys)",
                        "02: 백테스트 탭 Bayesian/Grid 시 trial 값 후보를 만드는 키 (_get_dynamic_search_space)",
                        "03: 최적화 결과 트리뷰 컬럼·툴팁용 PARAM_INFO",
                        "04: 지표 캐시 재사용 허용(신호만 변경) 화이트리스트 — 여기 없으면 지표 매 trial 재계산",
                        "05: calculate_all_indicators 에 직접 영향 → 04 와 동시 Y 이면 코드 점검 필요",
                    ]
                }
            )
            guide.to_excel(writer, sheet_name="README", index=False)
            df.to_excel(writer, sheet_name="parameters", index=False)
    except ImportError:
        print("openpyxl 이 필요합니다: pip install openpyxl", file=sys.stderr)
        sys.exit(1)

    print(f"작성 완료: {out_path} (행 수: {len(df)})")


if __name__ == "__main__":
    main()
