# -*- coding: utf-8 -*-
"""
운영 zip 을 만들어 GitHub Release 로 게시한다.

사용:
  py -3 -u tools/publish_production_release.py
  py -3 -u tools/publish_production_release.py --version 0.19.9

전제: ``gh`` 로그인(또는 GH_TOKEN), origin 이 GitHub 저장소.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional, Tuple

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
    # nina.v@example.com:owner/repo.git / https://github.com/owner/repo.git
    m = re.search(r"github\.com[:/](?P<slug>[^/]+/[^/]+?)(?:\.git)?$", url)
    if m:
        return str(m.group("slug"))
    return "ischoi20002/kiwoom-trader"


def publish(
    *,
    version: Optional[str] = None,
    repo: Optional[str] = None,
    draft: bool = False,
    skip_gh: bool = False,
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
    print(f"[1/3] 운영 zip 생성: {zip_path}")
    build_production_zip(zip_path, source=ROOT, version=ver)
    print(f"      size={zip_path.stat().st_size} bytes")

    if skip_gh:
        print("[skip] gh Release 게시 생략")
        return zip_path

    notes = (
        f"K-Trader 운영 패키지 v{ver}\n\n"
        f"- 자산: `{asset_name}`\n"
        "- `config.py` / `settings.json` 미포함 (수신측 유지)\n"
        "- 적용: 대시보드「업데이트 확인」또는 `remote_update.bat`\n"
    )
    notes_file = out_dir / "notes.md"
    notes_file.write_text(notes, encoding="utf-8")

    print(f"[2/3] Release 존재 여부 확인: {slug} {tag}")
    code, _, _ = _run(
        ["gh", "release", "view", tag, "--repo", slug],
        check=False,
    )
    if code == 0:
        print(f"[3/3] 기존 Release에 자산 업로드/교체: {tag}")
        # 동일 이름 자산이 있으면 삭제 후 업로드
        _run(
            [
                "gh",
                "release",
                "delete-asset",
                tag,
                asset_name,
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
                str(zip_path),
                "--repo",
                slug,
                "--clobber",
            ],
            check=True,
        )
    else:
        print(f"[3/3] Release 생성: {tag}")
        cmd = [
            "gh",
            "release",
            "create",
            tag,
            str(zip_path),
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

    print(f"[OK] GitHub Release 게시 완료: {slug} {tag} / {asset_name}")
    return zip_path


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="운영 zip GitHub Release 게시")
    p.add_argument("--version", default="", help="미지정 시 core/version.py")
    p.add_argument("--repo", default="", help="owner/name (미지정 시 origin)")
    p.add_argument("--draft", action="store_true", help="draft Release")
    p.add_argument(
        "--skip-gh",
        action="store_true",
        help="zip 만 만들고 Release 업로드 생략",
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
        )
    except Exception as ex:
        print(f"[오류] {ex}")
        raise SystemExit(1)
