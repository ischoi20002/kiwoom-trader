# -*- coding: utf-8 -*-
"""
운영 zip (및 선택적으로 Setup.exe) 을 만들어 GitHub Release 로 게시한다.

사용:
  py -3 -u tools/publish_production_release.py
  py -3 -u tools/publish_production_release.py --with-setup
  py -3 -u tools/publish_production_release.py --version 1.1.0

전제: ``gh`` 로그인(또는 GH_TOKEN), origin 이 GitHub 저장소.
      ``--with-setup`` 시 Inno Setup 6(ISCC) 및 포터블 런타임 빌드 가능해야 함.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

_VERSION_RE = re.compile(
    r"""APP_VERSION\s*:\s*str\s*=\s*["']([0-9]+(?:\.[0-9]+)*)["']"""
)


def _read_app_version() -> str:
    text = (ROOT / "core" / "version.py").read_text(encoding="utf-8")
    m = _VERSION_RE.search(text)
    if not m:
        raise RuntimeError("core/version.py 에서 APP_VERSION 파싱 실패")
    return str(m.group(1))


def _run(cmd: list[str], *, check: bool = True) -> Tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
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


def _detect_repo_slug() -> str:
    code, out, _ = _run(["git", "remote", "get-url", "origin"], check=False)
    if code != 0:
        return "ischoi20002/kiwoom-trader"
    url = (out or "").strip()
    m = re.search(r"github\.com[:/](?P<slug>[^/]+/[^/]+?)(?:\.git)?$", url)
    if m:
        return str(m.group("slug"))
    return "ischoi20002/kiwoom-trader"


def _upload_asset(slug: str, tag: str, path: Path) -> None:
    name = path.name
    _run(
        [
            "gh",
            "release",
            "delete-asset",
            tag,
            name,
            "--repo",
            slug,
            "--yes",
        ],
        check=False,
    )
    _run(
        [
            "gh",
            "release",
            "upload",
            tag,
            str(path),
            "--repo",
            slug,
            "--clobber",
        ],
        check=True,
    )


def publish(
    *,
    version: Optional[str] = None,
    repo: Optional[str] = None,
    draft: bool = False,
    skip_gh: bool = False,
    with_setup: bool = False,
    setup_path: Optional[Path] = None,
) -> Path:
    from deploy_production import build_production_zip

    ver = (version or _read_app_version()).strip()
    if not ver:
        raise RuntimeError("버전 문자열이 비어 있습니다")
    slug = (repo or _detect_repo_slug()).strip()
    tag = f"v{ver}"
    asset_name = f"k-trader-{ver}.zip"

    out_dir = Path(tempfile.mkdtemp(prefix="k-trader-release-"))
    zip_path = out_dir / asset_name
    print(f"[1/4] 운영 zip 생성: {zip_path}")
    build_production_zip(zip_path, source=ROOT, version=ver)
    print(f"      size={zip_path.stat().st_size} bytes")

    # Setup.exe 는 기본 로컬 전달만 — GitHub 업로드는 명시 옵션일 때만
    setup_exe: Optional[Path] = None
    if setup_path is not None and Path(setup_path).is_file():
        setup_exe = Path(setup_path)
        print(f"[2/4] Setup.exe 명시 업로드: {setup_exe}")
    elif with_setup:
        print("[2/4] Setup.exe 빌드 후 업로드…")
        from build_installer import build_installer

        setup_exe = build_installer(version=ver)
    else:
        print(
            "[2/4] Setup.exe 생략 (로컬 전용 — "
            "업로드하려면 --with-setup 또는 --setup 경로)"
        )

    if skip_gh:
        print("[skip] gh Release 게시 생략")
        return zip_path

    notes_lines = [
        f"K-Trader 운영 패키지 v{ver}",
        "",
        f"- zip: `{asset_name}` (업데이트용, 설정 미포함)",
    ]
    if setup_exe is not None:
        notes_lines.append(
            f"- Setup: `{setup_exe.name}` (신규 설치용, 포터블 Python 포함)"
        )
    notes_lines.extend(
        [
            "- 신규: Setup.exe → 첫 실행 API 설정 마법사",
            "- 업데이트: 대시보드「업데이트 확인」또는 `remote_update.bat`",
            "- `config.py` / `settings.json` / `license.key` 는 수신측 유지",
        ]
    )
    notes_file = out_dir / "notes.md"
    notes_file.write_text("\n".join(notes_lines) + "\n", encoding="utf-8")

    assets: List[Path] = [zip_path]
    if setup_exe is not None:
        assets.append(setup_exe)

    print(f"[3/4] Release 존재 여부 확인: {slug} {tag}")
    code, _, _ = _run(
        ["gh", "release", "view", tag, "--repo", slug],
        check=False,
    )
    if code == 0:
        print(f"[4/4] 기존 Release에 자산 업로드/교체: {tag}")
        for asset in assets:
            print(f"      upload {asset.name}")
            _upload_asset(slug, tag, asset)
    else:
        print(f"[4/4] Release 생성: {tag}")
        cmd = [
            "gh",
            "release",
            "create",
            tag,
            *[str(a) for a in assets],
            "--repo",
            slug,
            "--title",
            f"K-Trader {tag}",
            "--notes-file",
            str(notes_file),
        ]
        if draft:
            cmd.append("--draft")
        _run(cmd, check=True)

    names = ", ".join(a.name for a in assets)
    print(f"[OK] GitHub Release 게시 완료: {slug} {tag} / {names}")
    return zip_path


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="운영 zip/Setup GitHub Release 게시")
    p.add_argument("--version", default="", help="미지정 시 core/version.py")
    p.add_argument("--repo", default="", help="owner/name (미지정 시 origin)")
    p.add_argument("--draft", action="store_true", help="draft Release")
    p.add_argument(
        "--skip-gh",
        action="store_true",
        help="zip 만 만들고 Release 업로드 생략",
    )
    p.add_argument(
        "--with-setup",
        action="store_true",
        help="(비권장) Setup.exe 를 빌드해 Release 에 함께 업로드. 기본은 zip만 — Setup은 로컬 전달",
    )
    p.add_argument(
        "--setup",
        default="",
        help="이미 빌드된 Setup.exe 경로 (있으면 --with-setup 대신 사용)",
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    try:
        args = _parse_args()
        publish(
            version=str(args.version or "").strip() or None,
            repo=str(args.repo or "").strip() or None,
            draft=bool(args.draft),
            skip_gh=bool(args.skip_gh),
            with_setup=bool(args.with_setup),
            setup_path=Path(args.setup) if str(args.setup or "").strip() else None,
        )
    except Exception as ex:
        print(f"[오류] {ex}")
        raise SystemExit(1)
