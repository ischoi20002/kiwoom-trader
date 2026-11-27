# -*- coding: utf-8 -*-
"""
운영 설치용 포터블 Python 런타임 스테이징.

빌드 PC의 Python 3.12 전체 설치(Tcl/Tk 포함)를 ``dist/runtime/python/`` 로
복사한 뒤 requirements 를 설치한다.
(공식 quiet TargetDir 은 이미 Python 이 있으면 빈 폴더가 되고,
 NuGet 패키지는 tkinter 가 없어 이 방식을 사용한다.)

사용:
  py -3 -u tools/build_portable_runtime.py
  py -3 -u tools/build_portable_runtime.py --donor "C:\\Path\\To\\Python312"
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "dist" / "runtime" / "python"

_SKIP_DIR_NAMES = {
    "__pycache__",
    "Doc",
    "docs",
    "test",
    "tests",
    ".git",
}


def _run(cmd: Sequence[str], *, cwd: Optional[Path] = None, check: bool = True) -> Tuple[int, str, str]:
    proc = subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"명령 실패 ({proc.returncode}): {' '.join(cmd)}\n"
            f"{proc.stdout}\n{proc.stderr}"
        )
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _find_donor(explicit: Optional[Path] = None) -> Path:
    """Tcl/Tk 포함 전체 Python 루트를 찾는다."""
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit))
    try:
        base = Path(getattr(sys, "base_prefix", None) or sys.prefix)
        candidates.append(base)
        candidates.append(Path(sys.executable).resolve().parent)
    except Exception:
        pass
    try:
        home = Path.home()
        candidates.append(home / r"AppData\Local\Programs\Python\Python312")
        candidates.append(home / r"AppData\Local\Programs\Python\Python313")
    except Exception:
        pass
    candidates.extend(
        [
            Path(r"C:\Program Files\Python312"),
            Path(r"C:\Program Files\Python313"),
            Path(r"C:\Python312"),
        ]
    )
    seen: set[str] = set()
    for root in candidates:
        try:
            key = str(root.resolve()).lower()
        except Exception:
            key = str(root).lower()
        if key in seen:
            continue
        seen.add(key)
        py = root / "python.exe"
        tk_pyd = root / "DLLs" / "_tkinter.pyd"
        tcl_dir = root / "tcl"
        if py.is_file() and tk_pyd.is_file() and tcl_dir.is_dir():
            # 버전 확인 (3.12 권장)
            try:
                code, out, _ = _run(
                    [str(py), "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
                    check=True,
                )
                ver = (out or "").strip()
                if ver.startswith("3.12") or ver.startswith("3.13") or ver.startswith("3.11"):
                    return root
            except Exception:
                continue
    raise RuntimeError(
        "포터블 런타임용 Python(3.11~3.13, tkinter 포함) 설치를 찾지 못했습니다. "
        "--donor 로 python.exe 가 있는 폴더를 지정하세요."
    )


def _copy_python_tree(src: Path, dest: Path) -> None:
    """전체 Python 트리를 dest 로 복사 (불필요 폴더 제외)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    def _ignore(directory: str, names: list[str]) -> set[str]:
        skipped: set[str] = set()
        for n in names:
            if n in _SKIP_DIR_NAMES:
                skipped.add(n)
            elif n.endswith(".pyc"):
                skipped.add(n)
        return skipped

    # shutil.copytree 는 dest 가 없어야 함 → 위에서 mkdir 했으므로 직접 순회
    for item in src.iterdir():
        if item.name in _SKIP_DIR_NAMES:
            continue
        target = dest / item.name
        try:
            if item.is_dir():
                shutil.copytree(item, target, ignore=_ignore, dirs_exist_ok=False)
            else:
                shutil.copy2(item, target)
        except Exception as ex:
            raise RuntimeError(f"복사 실패 {item} → {target}: {ex}") from ex


def _ensure_pip(python_exe: Path) -> None:
    code, _, _ = _run([str(python_exe), "-m", "pip", "--version"], check=False)
    if code == 0:
        return
    print("    pip 없음 → ensurepip")
    _run([str(python_exe), "-m", "ensurepip", "--upgrade"], check=False)
    code2, _, _ = _run([str(python_exe), "-m", "pip", "--version"], check=False)
    if code2 != 0:
        raise RuntimeError("pip 준비 실패")


def _pip_install(python_exe: Path, requirements: Path, *, use_lock: bool) -> None:
    req = requirements
    if use_lock:
        lock = requirements.parent / "requirements-lock.txt"
        if lock.is_file():
            req = lock
    print(f"[3] pip install -r {req.name}")
    _ensure_pip(python_exe)
    _run([str(python_exe), "-m", "pip", "install", "--upgrade", "pip"], check=True)
    _run([str(python_exe), "-m", "pip", "install", "-r", str(req)], check=True)


def _smoke_test(python_exe: Path) -> None:
    print("[4] smoke test (tkinter / pandas / websockets / cryptography)")
    code = (
        "import tkinter; import pandas; import websockets; "
        "import cryptography; print('OK', pandas.__version__, 'tk', tkinter.TkVersion)"
    )
    _run([str(python_exe), "-c", code], check=True)
    code2, out, err = _run(
        [str(python_exe), "-c", "import kiwoom_rest_api; print('kiwoom OK')"],
        check=False,
    )
    if code2 != 0:
        print(f"    [경고] kiwoom_rest_api import 실패: {(err or out).strip()}")
    else:
        print("   ", (out or "").strip())
    print("    smoke OK")


def build_portable_runtime(
    *,
    out_dir: Path = DEFAULT_OUT,
    donor: Optional[Path] = None,
    requirements: Optional[Path] = None,
    use_lock: bool = False,
    # 하위 호환(무시)
    python_version: str = "3.12.10",
    skip_download_if_cached: bool = True,
) -> Path:
    """
    포터블 Python+의존성을 ``out_dir`` 에 구성한다.

    Returns
    -------
    Path
        python.exe 가 있는 디렉터리
    """
    _ = python_version, skip_download_if_cached
    out_dir = Path(out_dir)
    req = Path(requirements) if requirements else (ROOT / "requirements.txt")
    if not req.is_file():
        raise FileNotFoundError(f"requirements 없음: {req}")

    donor_root = _find_donor(Path(donor) if donor else None)
    print(f"[1] 도너 Python: {donor_root}")

    staging_parent = Path(tempfile.mkdtemp(prefix="ktrader-py-stage-"))
    staging = staging_parent / "python"
    try:
        print(f"[2] 트리 복사 → staging")
        _copy_python_tree(donor_root, staging)
        py = staging / "python.exe"
        if not py.is_file():
            raise RuntimeError(f"복사 후 python.exe 없음: {py}")
        # 복사본이 시스템 경로를 가리키지 않게 가벼운 확인
        _run([str(py), "-c", "import sys; print(sys.prefix)"], check=True)
        _pip_install(py, req, use_lock=use_lock)
        _smoke_test(py)

        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(out_dir))
    finally:
        try:
            if staging_parent.exists():
                shutil.rmtree(staging_parent, ignore_errors=True)
        except Exception:
            pass

    print(f"[OK] 포터블 런타임: {out_dir}")
    return out_dir


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="K-Trader 포터블 Python 런타임 빌드")
    p.add_argument("--out", default=str(DEFAULT_OUT), help="산출 디렉터리")
    p.add_argument("--donor", default="", help="도너 Python 루트 (python.exe 있는 폴더)")
    p.add_argument("--requirements", default="", help="requirements 경로")
    p.add_argument("--use-lock", action="store_true", help="requirements-lock.txt 사용")
    p.add_argument("--python-version", default="3.12.10", help="(호환용, 무시)")
    p.add_argument("--force-download", action="store_true", help="(호환용, 무시)")
    return p.parse_args(argv)


if __name__ == "__main__":
    try:
        args = _parse_args()
        build_portable_runtime(
            out_dir=Path(args.out),
            donor=Path(args.donor) if str(args.donor or "").strip() else None,
            requirements=Path(args.requirements) if args.requirements else None,
            use_lock=bool(args.use_lock),
        )
    except Exception as ex:
        print(f"[오류] {ex}", file=sys.stderr)
        raise SystemExit(1)
