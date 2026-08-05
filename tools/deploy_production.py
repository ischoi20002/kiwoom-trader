# -*- coding: utf-8 -*-
"""
개발(c:\\vibe) → 운영(c:\\k-trader) 최소 파일 배포.

- 실시간 매매 GUI 실행에 필요한 파일만 복사
- 백테스트/도구/백업/로그 산출물 제외
- 운영용 k_trader_gui.py 에서 전략검증 탭 import 제거
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Iterable, List, Set

SOURCE = Path(r"c:\vibe")
DEST = Path(r"c:\k-trader")

# 루트 필수 파일
ROOT_FILES: List[str] = [
    "k_trader_gui.py",
    "config.py",
    "backtest_engine.py",
    "data_manager_v2.py",
    "strategy_selector.py",
    "settings.json",
    "krx_special_sessions.json",
    "kodex200.csv",
    "kodex_200.csv",
    "requirements.txt",
    "requirements-lock.txt",
    "run_k_trader.bat",
    "install_k_trader.bat",
    "CHANGELOG.md",
]

# 선택적 UI/설정 (있으면 복사)
ROOT_OPTIONAL: List[str] = [
    "monitor_column_widths.json",
    "watchlist_favorites.json",
]

# 디렉터리별 제외 (백테스트·개발 전용)
REPORTING_EXCLUDE: Set[str] = {
    "backtest_trade_analytics.py",
    "charts.py",
}

UI_TABS_EXCLUDE: Set[str] = {
    "backtest_tab.py",
}

CORE_EXCLUDE: Set[str] = {
    "daishin_data_manager.py",
}

UTILS_EXCLUDE: Set[str] = {
    "krx_public_listing.py",
}

SKIP_DIR_NAMES: Set[str] = {
    "__pycache__",
    ".git",
    ".cursor",
    ".vscode",
    ".knowledge",
}

# 모든 하위 경로에서 제외할 파일명
GLOBAL_FILE_EXCLUDE: Set[str] = {
    "backtest_tab.py",
}


def _copy_tree(src_dir: Path, dst_dir: Path, exclude_files: Set[str]) -> int:
    """하위 .py 및 __init__ 복사 (__pycache__ 제외)."""
    count = 0
    if not src_dir.is_dir():
        return 0
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in sorted(src_dir.rglob("*")):
        if any(p in SKIP_DIR_NAMES for p in item.parts):
            continue
        if not item.is_file():
            continue
        rel = item.relative_to(src_dir)
        if item.name in exclude_files or item.name in GLOBAL_FILE_EXCLUDE:
            continue
        if item.suffix not in (".py",):
            continue
        target = dst_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        count += 1
    return count


def _copy_file(src: Path, dst: Path) -> bool:
    if not src.is_file():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _patch_k_trader_gui_for_production(path: Path) -> None:
    """운영 빌드: 백테스트 탭·미사용 import 제거."""
    text = path.read_text(encoding="utf-8")
    replacements = [
        ("from strategy.data_loader import BacktestDataLoader\n", ""),
        ("from ui.tabs.backtest_tab import BacktestTab\n", ""),
        (
            "            'virtual': ttk.Frame(self.notebook),\n"
            "            'backtest': ttk.Frame(self.notebook),\n"
            "            'settings': ttk.Frame(self.notebook)",
            "            'virtual': ttk.Frame(self.notebook),\n"
            "            'settings': ttk.Frame(self.notebook)",
        ),
        (
            "        self.notebook.add(self._tab_frames['backtest'], text=\" 전략검증 \")\n\n",
            "\n",
        ),
        ("        self.backtest_tab = BacktestTab(self._tab_frames['backtest'], self)\n", ""),
        (
            "        if \"전략검증\" in tab_text and hasattr(self, \"backtest_tab\"):\n"
            "            # [v26.1] 탭 전환 시 현재 선택된 최적화 항목만 컬럼으로 표시하도록 즉시 리프레시\n"
            "            self.backtest_tab._refresh_columns_from_settings()\n",
            "",
        ),
    ]
    for old, new in replacements:
        if old not in text:
            continue
        text = text.replace(old, new)
    # 제목에 운영 표시 (선택)
    text = re.sub(
        r'(\[Remote Monitoring\])',
        r'[Operation]',
        text,
        count=1,
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def _verify_op_venv(dest: Path) -> None:
    """
    운영 폴더 .venv 상태 확인.

    deploy 는 .venv 를 복사/삭제하지 않는다. 다만 run_k_trader.bat 등은
    매 배포마다 덮어쓰므로, venv 가 없거나 패키지가 빠지면 실행이 실패한다.
    """
    venv_py = dest / ".venv" / "Scripts" / "python.exe"
    if not venv_py.is_file():
        print(
            "\n[경고] [운영] .venv 없음 — 최초 1회 install_k_trader.bat 실행 필요"
        )
        print(f"     → {dest}\\install_k_trader.bat")
        return
    try:
        import subprocess

        r = subprocess.run(
            [str(venv_py), "-c", "import pandas"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if r.returncode != 0:
            print(
                "\n[경고] [운영] .venv 에 pandas 등 패키지 없음 — install_k_trader.bat 재실행"
            )
            print(f"     → {dest}\\install_k_trader.bat")
            return
        print("\n[OK] [운영] .venv + pandas 확인됨 (run_k_trader.bat 가 .venv 우선 사용)")
    except Exception as ex:
        print(f"\n[경고] [운영] .venv 검사 생략: {ex}")


def deploy() -> None:
    if not SOURCE.is_dir():
        raise FileNotFoundError(f"소스 없음: {SOURCE}")

    DEST.mkdir(parents=True, exist_ok=True)
    copied: List[str] = []

    for name in ROOT_FILES + ROOT_OPTIONAL:
        src = SOURCE / name
        if _copy_file(src, DEST / name):
            copied.append(name)

    dirs_plan: Iterable[tuple[str, Set[str]]] = [
        ("core", CORE_EXCLUDE),
        ("core/execution", set()),
        ("ui", set()),
        ("ui/tabs", UI_TABS_EXCLUDE),
        ("strategy", set()),
        ("reporting", REPORTING_EXCLUDE),
        ("utils", UTILS_EXCLUDE),
    ]
    py_count = 0
    for rel, exc in dirs_plan:
        n = _copy_tree(SOURCE / rel, DEST / rel, exc)
        py_count += n

    gui_path = DEST / "k_trader_gui.py"
    if gui_path.is_file():
        _patch_k_trader_gui_for_production(gui_path)

    # 런타임 출력 폴더(빈 디렉터리)
    for sub in ("realtime_session_logs", "equity_snapshots_mock", "equity_snapshots_live"):
        (DEST / sub).mkdir(parents=True, exist_ok=True)

    print(f"[배포 완료] {SOURCE} -> {DEST}")
    print(f"  루트 파일: {len(copied)}개")
    print(f"  Python 모듈: {py_count}개")
    print("  백테스트 탭 제거 패치 적용됨 (k_trader_gui.py)")
    print(f"\n실행: cd {DEST} && run_k_trader.bat")
    print(f"  (최초/패키지 오류: {DEST}\\install_k_trader.bat)")
    print(f"  (다음 배포: {SOURCE}\\deploy_production.bat)")
    _verify_op_venv(DEST)


if __name__ == "__main__":
    try:
        deploy()
    except Exception as ex:
        print(f"[오류] {ex}")
        raise
