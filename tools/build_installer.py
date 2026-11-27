# -*- coding: utf-8 -*-
"""
운영 Setup.exe 빌드 오케스트레이션.

1) stage_production → dist/installer_stage (config/settings/license 제외)
2) 포터블 Python → dist/runtime/python (없으면 빌드)
3) Inno Setup ISCC → dist/K-Trader-Setup-{ver}.exe

사용:
  py -3 -u tools/build_installer.py
  py -3 -u tools/build_installer.py --skip-runtime
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Set

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

STAGE = ROOT / "dist" / "installer_stage"
RUNTIME = ROOT / "dist" / "runtime" / "python"
OUT_DIR = ROOT / "dist"
ISS = ROOT / "tools" / "installer" / "k_trader.iss"

_INSTALLER_EXCLUDE: Set[str] = {
    "config.py",
    "settings.json",
    "license.key",
}


def _read_version() -> str:
    import re

    text = (ROOT / "core" / "version.py").read_text(encoding="utf-8")
    m = re.search(
        r"""APP_VERSION\s*:\s*str\s*=\s*["']([0-9]+(?:\.[0-9]+)*)["']""",
        text,
    )
    if not m:
        raise RuntimeError("APP_VERSION 파싱 실패")
    return str(m.group(1))


def _find_iscc() -> Optional[Path]:
    env = (os.environ.get("ISCC") or os.environ.get("INNO_SETUP_ISCC") or "").strip()
    if env:
        p = Path(env)
        if p.is_file():
            return p
    home = Path.home()
    candidates = [
        home / r"AppData\Local\Programs\Inno Setup 6\ISCC.exe",
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    # PATH
    which = shutil.which("ISCC") or shutil.which("ISCC.exe")
    if which:
        return Path(which)
    return None


def _stage_app(*, version: str) -> Path:
    from deploy_production import stage_production, ZIP_EXCLUDE_FILES

    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True, exist_ok=True)
    exclude = set(ZIP_EXCLUDE_FILES) | set(_INSTALLER_EXCLUDE)
    print(f"[1/3] stage_production → {STAGE}")
    stage_production(
        STAGE,
        source=ROOT,
        preserve_runtime=False,
        exclude_files=exclude,
        quiet=False,
    )
    # 안전망: 민감 파일 제거
    for name in _INSTALLER_EXCLUDE:
        p = STAGE / name
        try:
            if p.is_file():
                p.unlink()
        except Exception:
            pass
    readme = STAGE / "README_UPDATE.txt"
    try:
        readme.write_text(
            (
                "K-Trader 운영 패키지 (Setup 설치본)\n"
                f"버전: {version}\n\n"
                "1) 설치 후 run_k_trader.bat (또는 바로가기) 실행\n"
                "2) 첫 실행 시 API·계정 설정 마법사에서 키를 입력\n"
                "3) 이후 대시보드「업데이트 확인」으로 zip 업데이트\n"
                "4) config.py / settings.json / license.key 는 업데이트해도 유지\n"
            ),
            encoding="utf-8",
        )
    except Exception:
        pass
    return STAGE


def _ensure_runtime(*, skip: bool, force: bool) -> Path:
    py = RUNTIME / "python.exe"
    if skip:
        if not py.is_file():
            raise FileNotFoundError(
                f"--skip-runtime 이지만 런타임 없음: {RUNTIME}\n"
                "먼저: py -3 -u tools/build_portable_runtime.py"
            )
        print(f"[2/3] 기존 런타임 사용: {RUNTIME}")
        return RUNTIME
    if py.is_file() and not force:
        print(f"[2/3] 캐시 런타임 사용 (재빌드: --force-runtime): {RUNTIME}")
        return RUNTIME
    print("[2/3] 포터블 런타임 빌드…")
    from build_portable_runtime import build_portable_runtime

    return build_portable_runtime(out_dir=RUNTIME)


def _compile_iss(*, version: str, iscc: Path) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_name = f"K-Trader-Setup-{version}.exe"
    out_path = OUT_DIR / out_name
    if out_path.exists():
        try:
            out_path.unlink()
        except Exception:
            pass
    cmd: List[str] = [
        str(iscc),
        f"/DMyAppVersion={version}",
        f"/DStageDir={STAGE}",
        f"/DRuntimeDir={RUNTIME}",
        f"/DOutputDir={OUT_DIR}",
        str(ISS),
    ]
    print(f"[3/3] ISCC 컴파일…")
    print("     ", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(ISS.parent))
    if proc.returncode != 0:
        raise RuntimeError(f"ISCC 실패 (code={proc.returncode})")
    if not out_path.is_file():
        # Inno 가 OutputBaseFilename 으로 만들었는지 재확인
        raise FileNotFoundError(f"Setup.exe 생성 실패: {out_path}")
    print(f"[OK] {out_path} ({out_path.stat().st_size} bytes)")
    return out_path


def build_installer(
    *,
    version: Optional[str] = None,
    skip_runtime: bool = False,
    force_runtime: bool = False,
) -> Path:
    ver = (version or _read_version()).strip()
    if not ISS.is_file():
        raise FileNotFoundError(f"iss 없음: {ISS}")
    iscc = _find_iscc()
    if iscc is None:
        raise RuntimeError(
            "Inno Setup 6 의 ISCC.exe 를 찾을 수 없습니다.\n"
            "https://jrsoftware.org/isdl.php 에서 설치하거나 "
            "환경변수 ISCC 에 ISCC.exe 전체 경로를 지정하세요."
        )
    _stage_app(version=ver)
    _ensure_runtime(skip=skip_runtime, force=force_runtime)
    return _compile_iss(version=ver, iscc=iscc)


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="K-Trader Setup.exe 빌드")
    p.add_argument("--version", default="")
    p.add_argument("--skip-runtime", action="store_true")
    p.add_argument("--force-runtime", action="store_true")
    return p.parse_args(argv)


if __name__ == "__main__":
    try:
        args = _parse_args()
        build_installer(
            version=str(args.version or "").strip() or None,
            skip_runtime=bool(args.skip_runtime),
            force_runtime=bool(args.force_runtime),
        )
    except Exception as ex:
        print(f"[오류] {ex}", file=sys.stderr)
        raise SystemExit(1)
