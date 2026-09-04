# -*- coding: utf-8 -*-
"""
GitHub(origin) 대비 업데이트 확인·적용.

개발 루트 ``c:\\vibe`` 에서 ``git fetch`` 후 ``core/version.py`` 의
``APP_VERSION`` 을 비교하고, 적용 시 ``remote_update.bat`` 으로
pull → 운영 배포(``c:\\k-trader``) → 재기동한다.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

DEV_ROOT = Path(r"c:\vibe")
PROD_ROOT = Path(r"c:\k-trader")
DEFAULT_REMOTE = "origin"
DEFAULT_BRANCH = "main"
_VERSION_RE = re.compile(
    r"""APP_VERSION\s*:\s*str\s*=\s*["']([0-9]+(?:\.[0-9]+)*)["']"""
)


@dataclass(frozen=True)
class UpdateCheckResult:
    """업데이트 확인 결과."""

    ok: bool
    local_version: str
    remote_version: str
    update_available: bool
    local_commit: str = ""
    remote_commit: str = ""
    remote_ref: str = ""
    message: str = ""
    error: str = ""


def is_production_runtime(cwd: Optional[str] = None) -> bool:
    """현재 프로세스가 운영 폴더(``c:\\k-trader``)에서 기동 중인지."""
    try:
        base = Path(cwd or os.getcwd()).resolve()
        prod = PROD_ROOT.resolve()
        return base == prod or prod in base.parents or str(base).lower().startswith(
            str(prod).lower()
        )
    except Exception:
        return False


def resolve_dev_repo() -> Optional[Path]:
    """``c:\\vibe`` Git 저장소 경로. 없으면 None."""
    try:
        root = DEV_ROOT
        if (root / ".git").exists():
            return root
    except Exception:
        pass
    try:
        here = Path(__file__).resolve().parents[1]
        if (here / ".git").exists():
            return here
    except Exception:
        pass
    return None


def resolve_update_script() -> Optional[Path]:
    """``remote_update.bat`` 경로."""
    candidates = (
        DEV_ROOT / "remote_update.bat",
        Path(os.getcwd()) / "remote_update.bat",
        Path(__file__).resolve().parents[1] / "remote_update.bat",
    )
    for p in candidates:
        try:
            if p.is_file():
                return p
        except Exception:
            continue
    return None


def parse_version_tuple(version: str) -> Tuple[int, ...]:
    """``0.18.99`` → ``(0, 18, 99)``. 파싱 실패 시 빈 튜플."""
    try:
        parts = [int(x) for x in str(version).strip().split(".") if x != ""]
        return tuple(parts) if parts else ()
    except (TypeError, ValueError):
        return ()


def version_is_newer(remote: str, local: str) -> bool:
    """remote 가 local 보다 높은 버전인지."""
    rt = parse_version_tuple(remote)
    lt = parse_version_tuple(local)
    if not rt or not lt:
        return bool(remote) and remote != local
    # 길이 맞추기
    n = max(len(rt), len(lt))
    rt2 = rt + (0,) * (n - len(rt))
    lt2 = lt + (0,) * (n - len(lt))
    return rt2 > lt2


def _run_git(
    repo: Path,
    args: Sequence[str],
    *,
    timeout: float = 60.0,
) -> Tuple[int, str, str]:
    """git 실행. (returncode, stdout, stderr)."""
    cmd = ["git", "-C", str(repo), *list(args)]
    try:
        # Windows: CREATE_NO_WINDOW
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=creationflags,
        )
        return (
            int(proc.returncode),
            str(proc.stdout or "").strip(),
            str(proc.stderr or "").strip(),
        )
    except subprocess.TimeoutExpired:
        return 1, "", "git timeout"
    except FileNotFoundError:
        return 1, "", "git 실행 파일을 찾을 수 없습니다"
    except Exception as e:
        return 1, "", repr(e)


def _parse_app_version_from_text(text: str) -> str:
    m = _VERSION_RE.search(text or "")
    if m:
        return str(m.group(1))
    return ""


def get_local_version() -> str:
    """현재 프로세스에 로드된 APP_VERSION."""
    try:
        from core.version import APP_VERSION

        return str(APP_VERSION)
    except Exception:
        return ""


def get_local_commit(repo: Optional[Path] = None) -> str:
    """로컬 HEAD 짧은 해시."""
    root = repo or resolve_dev_repo()
    if root is None:
        return ""
    code, out, _ = _run_git(root, ["rev-parse", "--short", "HEAD"])
    return out if code == 0 else ""


def check_for_update(
    *,
    remote: str = DEFAULT_REMOTE,
    branch: str = DEFAULT_BRANCH,
    fetch: bool = True,
    log: Optional[Callable[[str], None]] = None,
) -> UpdateCheckResult:
    """
    GitHub(origin/branch) 의 ``core/version.py`` 와 로컬 버전을 비교한다.

    ``fetch=True`` 이면 ``git fetch`` 후 원격 추적 브랜치를 본다.
    """
    emit = log or (lambda _m: None)
    local_ver = get_local_version()
    repo = resolve_dev_repo()
    if repo is None:
        return UpdateCheckResult(
            ok=False,
            local_version=local_ver,
            remote_version="",
            update_available=False,
            error=f"개발 Git 저장소를 찾을 수 없습니다 ({DEV_ROOT})",
        )

    local_commit = get_local_commit(repo)
    ref = f"{remote}/{branch}"

    if fetch:
        emit(f"ℹ️ [Update] git fetch {remote} {branch} …")
        code, _out, err = _run_git(
            repo,
            ["fetch", remote, branch, "--prune"],
            timeout=120.0,
        )
        if code != 0:
            return UpdateCheckResult(
                ok=False,
                local_version=local_ver,
                remote_version="",
                update_available=False,
                local_commit=local_commit,
                remote_ref=ref,
                error=f"git fetch 실패: {err or 'unknown'}",
            )

    code, remote_commit, err = _run_git(repo, ["rev-parse", "--short", ref])
    if code != 0:
        return UpdateCheckResult(
            ok=False,
            local_version=local_ver,
            remote_version="",
            update_available=False,
            local_commit=local_commit,
            remote_ref=ref,
            error=f"원격 ref 없음 ({ref}): {err or 'fetch 후 재시도'}",
        )

    code, ver_text, err = _run_git(repo, ["show", f"{ref}:core/version.py"])
    if code != 0:
        return UpdateCheckResult(
            ok=False,
            local_version=local_ver,
            remote_version="",
            update_available=False,
            local_commit=local_commit,
            remote_commit=remote_commit,
            remote_ref=ref,
            error=f"원격 version.py 조회 실패: {err or 'unknown'}",
        )

    remote_ver = _parse_app_version_from_text(ver_text)
    if not remote_ver:
        return UpdateCheckResult(
            ok=False,
            local_version=local_ver,
            remote_version="",
            update_available=False,
            local_commit=local_commit,
            remote_commit=remote_commit,
            remote_ref=ref,
            error="원격 APP_VERSION 파싱 실패",
        )

    newer = version_is_newer(remote_ver, local_ver)
    # 버전 숫자가 같아도 원격에만 있는 커밋이 있으면 업데이트 대상
    remote_ahead_n = 0
    try:
        code_c, cnt_out, _ = _run_git(
            repo, ["rev-list", "--count", f"HEAD..{ref}"]
        )
        if code_c == 0:
            remote_ahead_n = int(str(cnt_out or "0").strip() or "0")
    except (TypeError, ValueError):
        remote_ahead_n = 0
    available = bool(newer or remote_ahead_n > 0)

    if newer:
        msg = f"새 버전 있음: {local_ver} → {remote_ver}"
    elif available:
        msg = (
            f"버전은 동일({local_ver})하나 원격에 새 커밋 "
            f"{remote_ahead_n}개 ({local_commit} → {remote_commit})"
        )
    else:
        msg = f"최신입니다 (v{local_ver})"

    return UpdateCheckResult(
        ok=True,
        local_version=local_ver,
        remote_version=remote_ver,
        update_available=available,
        local_commit=local_commit,
        remote_commit=remote_commit,
        remote_ref=ref,
        message=msg,
    )


def format_check_summary(result: UpdateCheckResult) -> str:
    """사용자용 요약 문구."""
    lines: List[str] = []
    if result.error:
        lines.append(f"확인 실패: {result.error}")
        if result.local_version:
            lines.append(f"로컬: v{result.local_version}")
        return "\n".join(lines)

    lines.append(f"로컬: v{result.local_version}" + (
        f" ({result.local_commit})" if result.local_commit else ""
    ))
    lines.append(f"원격({result.remote_ref}): v{result.remote_version}" + (
        f" ({result.remote_commit})" if result.remote_commit else ""
    ))
    lines.append(result.message or "")
    lines.append("")
    lines.append(
        "적용 시: GitHub → c:\\vibe pull → 운영(c:\\k-trader) 배포 → 재시작"
    )
    lines.append("(운영 settings.json·config.py 는 유지)")
    return "\n".join(lines).strip()


def start_update_process(
    *,
    log: Optional[Callable[[str], None]] = None,
) -> Tuple[bool, str]:
    """
    ``remote_update.bat`` 을 별도 프로세스로 기동한다.

    Returns:
        (성공 여부, 메시지)
    """
    emit = log or (lambda _m: None)
    script = resolve_update_script()
    if script is None:
        return False, f"remote_update.bat 없음 ({DEV_ROOT})"
    try:
        creationflags = 0
        if sys.platform == "win32":
            # 새 콘솔에서 진행 상황 표시
            creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
        subprocess.Popen(
            ["cmd", "/c", str(script)],
            cwd=str(script.parent),
            creationflags=creationflags,
        )
        emit(f"ℹ️ [Update] 업데이트 스크립트 기동: {script}")
        return True, str(script)
    except Exception as e:
        return False, f"스크립트 기동 실패: {e!r}"
