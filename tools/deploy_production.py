# -*- coding: utf-8 -*-
"""
개발(c:\\vibe) → 운영(c:\\k-trader) 최소 파일 배포.

- 실시간 매매 GUI 실행에 필요한 파일만 복사
- 백테스트/도구/백업/로그 산출물 제외
- 운영용 k_trader_gui.py 에서 전략검증 탭 import 제거
- GitHub Release 용 zip 패키징·적용 지원
"""
from __future__ import annotations

import argparse
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable, List, Optional, Set

SOURCE = Path(r"c:\vibe")
DEST = Path(r"c:\k-trader")

# 운영 업데이트 시 덮어쓰지 않을 런타임 설정 (이미 DEST에 있을 때만)
PRESERVE_RUNTIME_FILES: Set[str] = {
    "settings.json",
    "config.py",
    "license.key",
}

# Release zip 에도 넣지 않음 (비밀키·개인 설정 유출 방지, 수신측 유지)
ZIP_EXCLUDE_FILES: Set[str] = {
    "settings.json",
    "config.py",
    "license.key",
}
# 루트 필수 파일
ROOT_FILES: List[str] = [
    "k_trader_gui.py",
    "config.py",
    "backtest_engine.py",
    "data_manager_v2.py",
    "dynamic_backtest.py",
    "strategy_selector.py",
    "settings.json",
    "krx_special_sessions.json",
    "krx_closed_day_overrides.json",
    "kodex200.csv",
    "kodex_200.csv",
    "requirements.txt",
    "requirements-lock.txt",
    "run_k_trader.bat",
    "run_entry.py",
    "install_k_trader.bat",
    "remote_update.bat",
    "apply_release_update.py",
    "CHANGELOG.md",
]

# 선택적 UI/설정 (있으면 복사)
ROOT_OPTIONAL: List[str] = [
    "monitor_column_widths.json",
    "watchlist_favorites.json",
    "config.example.py",
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


def _copy_mobile_web(src_dir: Path, dst_dir: Path) -> int:
    """모바일 PWA 정적 파일 복사 (html/js/css/manifest)."""
    count = 0
    if not src_dir.is_dir():
        print(f"[경고] mobile_web 없음: {src_dir}")
        return 0
    allow_suffix = {".html", ".js", ".css", ".webmanifest", ".svg", ".png", ".ico", ".json"}
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in sorted(src_dir.rglob("*")):
        if any(p in SKIP_DIR_NAMES for p in item.parts):
            continue
        if not item.is_file():
            continue
        if item.suffix.lower() not in allow_suffix and item.name not in (
            "manifest.webmanifest",
        ):
            continue
        rel = item.relative_to(src_dir)
        target = dst_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        count += 1
    return count


def _patch_build_flavor_production(path: Path) -> None:
    """운영 트리: BUILD_FLAVOR 를 production 으로 고정."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as ex:
        raise RuntimeError(f"build_flavor 읽기 실패: {path}: {ex}") from ex
    new_text, n = re.subn(
        r'(BUILD_FLAVOR:\s*str\s*=\s*)["\']dev["\']',
        r'\1"production"',
        text,
        count=1,
    )
    if n != 1:
        # 이미 production 이거나 형식 변경 — 강제 치환 시도
        new_text, n2 = re.subn(
            r'(BUILD_FLAVOR:\s*str\s*=\s*)["\'][^"\']*["\']',
            r'\1"production"',
            text,
            count=1,
        )
        if n2 != 1:
            raise RuntimeError(f"BUILD_FLAVOR 패치 실패: {path}")
    path.write_text(new_text, encoding="utf-8", newline="\n")


def _scrub_license_enforce_from_config_texts(dest: Path) -> None:
    """
    운영본 config / config.example 에서 LICENSE_ENFORCE_REAL 줄·관련 주석 제거.
    (고객이 스위치를 보지 않도록)
    """
    targets = [dest / "config.example.py", dest / "config.py"]
    drop_re = re.compile(
        r"^\s*(#\s*)?LICENSE_ENFORCE_REAL\b.*$",
        re.IGNORECASE,
    )
    note_re = re.compile(
        r"^\s*#.*LICENSE_ENFORCE_REAL.*$",
        re.IGNORECASE,
    )
    for path in targets:
        try:
            if not path.is_file():
                continue
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            kept: List[str] = []
            for line in lines:
                if drop_re.match(line) or note_re.match(line):
                    continue
                kept.append(line)
            path.write_text("".join(kept), encoding="utf-8", newline="\n")
        except Exception as ex:
            print(f"[경고] LICENSE_ENFORCE_REAL 제거 실패 ({path.name}): {ex}")


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
    portable_py = dest / "python" / "python.exe"
    venv_py = dest / ".venv" / "Scripts" / "python.exe"
    py = portable_py if portable_py.is_file() else venv_py
    if not py.is_file():
        print(
            "\n[경고] [운영] 포터블 python\\ / .venv 없음 — Setup.exe 또는 install_k_trader.bat"
        )
        print(f"     → {dest}\\install_k_trader.bat")
        return
    try:
        import subprocess

        r = subprocess.run(
            [str(py), "-c", "import pandas"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        label = "python\\" if py == portable_py else ".venv"
        if r.returncode != 0:
            print(
                f"\n[경고] [운영] {label} 에 pandas 등 패키지 없음 — install_k_trader.bat 재실행"
            )
            print(f"     → {dest}\\install_k_trader.bat")
            return
        print(f"\n[OK] [운영] {label} + pandas 확인됨 (run_k_trader.bat 우선 사용)")
    except Exception as ex:
        print(f"\n[경고] [운영] 런타임 검사 생략: {ex}")


def stage_production(
    dest: Path,
    *,
    source: Optional[Path] = None,
    preserve_runtime: bool = False,
    exclude_files: Optional[Set[str]] = None,
    quiet: bool = False,
) -> dict:
    """
    운영 트리를 ``dest`` 에 구성한다.

    Returns:
        복사 요약 dict
    """
    src_root = Path(source) if source is not None else SOURCE
    if not src_root.is_dir():
        raise FileNotFoundError(f"소스 없음: {src_root}")

    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    skip_names = set(exclude_files or ())
    copied: List[str] = []
    skipped_preserve: List[str] = []

    for name in ROOT_FILES + ROOT_OPTIONAL:
        if name in skip_names:
            continue
        if (
            preserve_runtime
            and name in PRESERVE_RUNTIME_FILES
            and (dest / name).is_file()
        ):
            skipped_preserve.append(name)
            continue
        src = src_root / name
        if _copy_file(src, dest / name):
            copied.append(name)

    dirs_plan: Iterable[tuple[str, Set[str]]] = [
        ("core", CORE_EXCLUDE),
        ("core/execution", set()),
        ("core/realtime", set()),
        ("data_manager", set()),
        ("ui", set()),
        ("ui/tabs", UI_TABS_EXCLUDE),
        ("strategy", set()),
        ("reporting", REPORTING_EXCLUDE),
        ("utils", UTILS_EXCLUDE),
    ]
    py_count = 0
    for rel, exc in dirs_plan:
        n = _copy_tree(src_root / rel, dest / rel, exc)
        py_count += n

    mobile_n = _copy_mobile_web(src_root / "mobile_web", dest / "mobile_web")

    gui_path = dest / "k_trader_gui.py"
    if gui_path.is_file():
        _patch_k_trader_gui_for_production(gui_path)

    flavor_path = dest / "core" / "build_flavor.py"
    if flavor_path.is_file():
        _patch_build_flavor_production(flavor_path)

    _scrub_license_enforce_from_config_texts(dest)

    for sub in ("realtime_session_logs", "equity_snapshots_mock", "equity_snapshots_live"):
        (dest / sub).mkdir(parents=True, exist_ok=True)

    summary = {
        "copied": copied,
        "skipped_preserve": skipped_preserve,
        "py_count": py_count,
        "mobile_n": mobile_n,
        "dest": str(dest),
    }
    if not quiet:
        print(f"[배포 완료] {src_root} -> {dest}")
        print(f"  루트 파일: {len(copied)}개")
        if skipped_preserve:
            print(f"  런타임 유지(미복사): {', '.join(skipped_preserve)}")
        print(f"  Python 모듈: {py_count}개")
        print(f"  mobile_web 파일: {mobile_n}개")
        print("  백테스트 탭 제거 패치 적용됨 (k_trader_gui.py)")
        print("  BUILD_FLAVOR=production · LICENSE_ENFORCE_REAL 운영본에서 제거")
    return summary


def deploy(*, preserve_runtime: bool = False) -> None:
    """개발 소스 → ``c:\\k-trader`` 배포."""
    stage_production(DEST, preserve_runtime=preserve_runtime, quiet=False)
    print(f"\n실행: cd {DEST} && run_k_trader.bat")
    print(f"  (신규 PC: Setup.exe 또는 {DEST}\\install_k_trader.bat)")
    print(f"  (Setup 빌드: py -3 -u tools\\build_installer.py)")
    print(f"  (다음 배포: {SOURCE}\\deploy_production.bat)")
    _verify_op_venv(DEST)


def build_production_zip(
    zip_path: Path,
    *,
    source: Optional[Path] = None,
    version: str = "",
) -> Path:
    """
    운영 배포 트리를 임시 폴더에 만들고 zip 으로 묶는다.

    ``config.py`` / ``settings.json`` 은 zip 에 포함하지 않는다.
    """
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="k-trader-stage-") as td:
        stage = Path(td) / "k-trader"
        stage_production(
            stage,
            source=source,
            preserve_runtime=False,
            exclude_files=ZIP_EXCLUDE_FILES,
            quiet=True,
        )
        # 수신측 안내
        readme = stage / "README_UPDATE.txt"
        try:
            ver_line = f"버전: {version}\n" if version else ""
            readme.write_text(
                (
                    "K-Trader 운영 패키지\n"
                    f"{ver_line}"
                    "\n"
                    "1) 신규 설치: 로컬 전달용 K-Trader-Setup-*.exe 권장 "
                    "(GitHub 미게시, Python 포함, 첫 실행 API 설정 마법사).\n"
                    "2) zip 만 쓰는 경우: 이 폴더를 c:\\k-trader 에 두고 "
                    "install_k_trader.bat → run_k_trader.bat.\n"
                    "3) config.py / settings.json / license.key 는 패키지에 없습니다.\n"
                    "   - 최초: 실행 시 설정 마법사 (또는 대시보드「API·계정 설정」).\n"
                    "   - 업데이트: 기존 설정·라이선스·DB 는 유지됩니다.\n"
                    "4) 이후 대시보드「업데이트 확인」으로 Release zip 을 받습니다.\n"
                ),
                encoding="utf-8",
            )
        except Exception:
            pass
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(
            zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zf:
            for item in sorted(stage.rglob("*")):
                if not item.is_file():
                    continue
                arc = item.relative_to(stage).as_posix()
                zf.write(item, arcname=arc)
    return zip_path


def apply_production_tree(
    staged: Path,
    dest: Path,
    *,
    preserve_runtime: bool = True,
) -> None:
    """
    스테이징(또는 zip 해제) 트리를 ``dest`` 에 병합 복사한다.

    ``preserve_runtime=True`` 이면 dest 의 settings.json·config.py 를 유지한다.
    """
    staged = Path(staged)
    dest = Path(dest)
    if not staged.is_dir():
        raise FileNotFoundError(f"스테이징 없음: {staged}")
    dest.mkdir(parents=True, exist_ok=True)

    preserved: dict[str, bytes] = {}
    if preserve_runtime:
        for name in PRESERVE_RUNTIME_FILES:
            p = dest / name
            if p.is_file():
                try:
                    preserved[name] = p.read_bytes()
                except Exception:
                    pass

    # staged 파일을 dest 로 복사 (.venv 등은 staged 에 없으므로 유지)
    for item in staged.rglob("*"):
        if not item.is_file():
            continue
        if any(p in SKIP_DIR_NAMES for p in item.parts):
            continue
        rel = item.relative_to(staged)
        # 루트 런타임 파일은 zip/stage 에 있어도 덮지 않음
        if preserve_runtime and rel.parts == (rel.name,) and rel.name in PRESERVE_RUNTIME_FILES:
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)

    for name, data in preserved.items():
        try:
            (dest / name).write_bytes(data)
        except Exception:
            pass


def apply_production_zip(
    zip_path: Path,
    dest: Path,
    *,
    preserve_runtime: bool = True,
) -> None:
    """운영 zip 을 ``dest`` 에 적용 (settings/config 유지)."""
    zip_path = Path(zip_path)
    if not zip_path.is_file():
        raise FileNotFoundError(f"zip 없음: {zip_path}")
    with tempfile.TemporaryDirectory(prefix="k-trader-unzip-") as td:
        extract_root = Path(td)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_root)
        # zip 루트가 곧 파일이거나 k-trader/ 한 단계일 수 있음
        staged = extract_root
        candidates = [
            p for p in extract_root.iterdir() if p.is_dir() and (p / "k_trader_gui.py").is_file()
        ]
        if (extract_root / "k_trader_gui.py").is_file():
            staged = extract_root
        elif len(candidates) == 1:
            staged = candidates[0]
        elif (extract_root / "k-trader" / "k_trader_gui.py").is_file():
            staged = extract_root / "k-trader"
        else:
            # gui 탐색
            found = list(extract_root.rglob("k_trader_gui.py"))
            if not found:
                raise FileNotFoundError("zip 안에 k_trader_gui.py 없음")
            staged = found[0].parent
        apply_production_tree(staged, dest, preserve_runtime=preserve_runtime)


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="개발→운영 최소 파일 배포")
    p.add_argument(
        "--preserve-runtime",
        action="store_true",
        help="DEST에 이미 있는 settings.json·config.py 는 덮어쓰지 않음",
    )
    p.add_argument(
        "--zip",
        type=str,
        default="",
        help="운영 zip 경로로 패키징만 수행 (c:\\k-trader 배포 안 함)",
    )
    p.add_argument(
        "--version",
        type=str,
        default="",
        help="--zip 시 README 에 기록할 버전",
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    try:
        args = _parse_args()
        if str(args.zip or "").strip():
            out = build_production_zip(
                Path(str(args.zip).strip()),
                version=str(args.version or "").strip(),
            )
            print(f"[OK] 운영 zip: {out} ({out.stat().st_size} bytes)")
        else:
            deploy(preserve_runtime=bool(args.preserve_runtime))
    except Exception as ex:
        print(f"[오류] {ex}")
        raise
