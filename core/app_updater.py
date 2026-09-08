# -*- coding: utf-8 -*-
"""
GitHub Release(운영 zip) 대비 업데이트 확인·적용.

운영 PC(``c:\\k-trader``)만 있어도 동작한다.
확인: Releases API 최신 태그(APP_VERSION) 비교
적용: zip 다운로드 → settings/config 유지 병합 → GUI 재시작
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

PROD_ROOT = Path(r"c:\k-trader")
DEV_ROOT = Path(r"c:\vibe")
DEFAULT_REPO = "ischoi20002/kiwoom-trader"
DEFAULT_ASSET_PREFIX = "k-trader-"
_VERSION_RE = re.compile(
    r"""APP_VERSION\s*:\s*str\s*=\s*["']([0-9]+(?:\.[0-9]+)*)["']"""
)
_TAG_VERSION_RE = re.compile(r"^v?(?P<ver>[0-9]+(?:\.[0-9]+)*)$", re.I)


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
    asset_name: str = ""
    asset_id: int = 0
    asset_url: str = ""
    release_tag: str = ""


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


def resolve_prod_root() -> Path:
    """운영 루트. cwd 가 k-trader 이면 그걸 우선."""
    try:
        cwd = Path(os.getcwd()).resolve()
        if (cwd / "k_trader_gui.py").is_file():
            return cwd
    except Exception:
        pass
    try:
        here = Path(__file__).resolve().parents[1]
        if (here / "k_trader_gui.py").is_file():
            return here
    except Exception:
        pass
    return PROD_ROOT


def resolve_update_script() -> Optional[Path]:
    """``remote_update.bat`` 경로 (운영 우선)."""
    candidates = (
        resolve_prod_root() / "remote_update.bat",
        PROD_ROOT / "remote_update.bat",
        Path(os.getcwd()) / "remote_update.bat",
        DEV_ROOT / "remote_update.bat",
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
    n = max(len(rt), len(lt))
    rt2 = rt + (0,) * (n - len(rt))
    lt2 = lt + (0,) * (n - len(lt))
    return rt2 > lt2


def _parse_app_version_from_text(text: str) -> str:
    m = _VERSION_RE.search(text or "")
    if m:
        return str(m.group(1))
    return ""


def version_from_release_tag(tag: str) -> str:
    """``v0.19.8`` / ``0.19.8`` → ``0.19.8``."""
    m = _TAG_VERSION_RE.match(str(tag or "").strip())
    if m:
        return str(m.group("ver"))
    # 느슨한 추출
    m2 = re.search(r"([0-9]+(?:\.[0-9]+)+)", str(tag or ""))
    return str(m2.group(1)) if m2 else ""


def get_local_version() -> str:
    """현재 프로세스에 로드된 APP_VERSION."""
    try:
        from core.version import APP_VERSION

        return str(APP_VERSION)
    except Exception:
        pass
    # 파일에서 직접
    for root in (resolve_prod_root(), DEV_ROOT, Path(__file__).resolve().parents[1]):
        try:
            p = root / "core" / "version.py"
            if p.is_file():
                return _parse_app_version_from_text(
                    p.read_text(encoding="utf-8", errors="replace")
                )
        except Exception:
            continue
    return ""


def _settings_dict() -> Dict[str, Any]:
    try:
        import config as _cfg  # type: ignore

        # settings 는 GUI 가 로드 — 파일 직접 읽기
    except Exception:
        pass
    for root in (resolve_prod_root(), DEV_ROOT, Path.cwd()):
        try:
            p = root / "settings.json"
            if not p.is_file():
                continue
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # 중첩 settings 키 또는 평면
                inner = data.get("settings")
                if isinstance(inner, dict):
                    return {**data, **inner}
                return data
        except Exception:
            continue
    return {}


def resolve_github_repo() -> str:
    """owner/name."""
    env = (
        os.environ.get("KTRADER_GITHUB_REPO")
        or os.environ.get("GITHUB_RELEASES_REPO")
        or ""
    ).strip()
    if env:
        return env
    try:
        s = _settings_dict()
        v = str(s.get("github_releases_repo") or "").strip()
        if v:
            return v
    except Exception:
        pass
    try:
        import config as _cfg  # type: ignore

        v = str(getattr(_cfg, "GITHUB_RELEASES_REPO", "") or "").strip()
        if v:
            return v
    except Exception:
        pass
    # git remote (개발 PC)
    try:
        proc = subprocess.run(
            ["git", "-C", str(DEV_ROOT), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0,
        )
        if proc.returncode == 0:
            url = (proc.stdout or "").strip()
            m = re.search(r"github\.com[:/](?P<slug>[^/]+/[^/]+?)(?:\.git)?$", url)
            if m:
                return str(m.group("slug"))
    except Exception:
        pass
    return DEFAULT_REPO


def resolve_github_token() -> str:
    """비공개 Release 다운로드용 토큰."""
    for key in (
        "KTRADER_GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    ):
        v = str(os.environ.get(key) or "").strip()
        if v:
            return v
    try:
        s = _settings_dict()
        v = str(s.get("github_releases_token") or "").strip()
        if v:
            return v
    except Exception:
        pass
    try:
        import config as _cfg  # type: ignore

        v = str(getattr(_cfg, "GITHUB_RELEASES_TOKEN", "") or "").strip()
        if v:
            return v
    except Exception:
        pass
    # Windows: git credential manager (push 에 쓰는 것과 동일, 토큰 출력 없음)
    try:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=creationflags,
        )
        if proc.returncode == 0:
            for line in (proc.stdout or "").splitlines():
                if line.startswith("password="):
                    tok = line.split("=", 1)[1].strip()
                    if tok:
                        return tok
    except Exception:
        pass
    # gh CLI 로그인 토큰
    try:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=creationflags,
        )
        if proc.returncode == 0:
            tok = str(proc.stdout or "").strip()
            if tok:
                return tok
    except Exception:
        pass
    return ""


def _safe_emit(log: Optional[Callable[[str], None]], msg: str) -> None:
    """로그 콜백 실패(콘솔 cp949 등)가 업데이트 확인을 막지 않게 함."""
    if not callable(log):
        return
    try:
        log(msg)
    except Exception:
        try:
            log(msg.encode("ascii", errors="replace").decode("ascii"))
        except Exception:
            pass


def _github_request(
    url: str,
    *,
    token: str = "",
    accept: str = "application/vnd.github+json",
    timeout: float = 60.0,
) -> Tuple[int, bytes, Dict[str, str]]:
    headers = {
        "Accept": accept,
        "User-Agent": "K-Trader-Updater",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            return int(getattr(resp, "status", 200) or 200), body, hdrs
    except urllib.error.HTTPError as e:
        try:
            body = e.read() or b""
        except Exception:
            body = b""
        return int(e.code), body, {}
    except Exception as e:
        raise RuntimeError(f"GitHub 요청 실패: {e!r}") from e


def _pick_release_asset(assets: List[Dict[str, Any]], version: str) -> Optional[Dict[str, Any]]:
    if not isinstance(assets, list):
        return None
    prefer = f"{DEFAULT_ASSET_PREFIX}{version}.zip".lower()
    named: List[Dict[str, Any]] = []
    for a in assets:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "")
        if not name.lower().endswith(".zip"):
            continue
        named.append(a)
        if name.lower() == prefer:
            return a
    for a in named:
        name = str(a.get("name") or "").lower()
        if name.startswith(DEFAULT_ASSET_PREFIX) and name.endswith(".zip"):
            return a
    return named[0] if named else None


def fetch_latest_release(
    *,
    repo: Optional[str] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """GitHub /releases/latest JSON."""
    slug = (repo or resolve_github_repo()).strip()
    tok = token if token is not None else resolve_github_token()
    url = f"https://api.github.com/repos/{slug}/releases/latest"
    code, body, _ = _github_request(url, token=tok)
    if code != 200:
        msg = body.decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"releases/latest HTTP {code}: {msg}")
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Release JSON 형식 오류")
    return data


def download_release_asset(
    asset: Dict[str, Any],
    dest_zip: Path,
    *,
    repo: Optional[str] = None,
    token: Optional[str] = None,
    log: Optional[Callable[[str], None]] = None,
) -> Path:
    """Release 자산을 ``dest_zip`` 으로 저장."""
    emit = lambda m: _safe_emit(log, m)
    tok = token if token is not None else resolve_github_token()
    slug = (repo or resolve_github_repo()).strip()
    asset_id = asset.get("id")
    name = str(asset.get("name") or "update.zip")
    dest_zip = Path(dest_zip)
    dest_zip.parent.mkdir(parents=True, exist_ok=True)

    # 비공개: API asset 엔드포인트 + octet-stream
    if asset_id is not None and tok:
        url = f"https://api.github.com/repos/{slug}/releases/assets/{int(asset_id)}"
        emit(f"[Update] 자산 다운로드(API): {name}")
        code, body, _ = _github_request(
            url,
            token=tok,
            accept="application/octet-stream",
            timeout=300.0,
        )
        if code != 200:
            raise RuntimeError(f"asset 다운로드 HTTP {code}")
        dest_zip.write_bytes(body)
        return dest_zip

    # 공개: browser_download_url
    url = str(asset.get("browser_download_url") or "").strip()
    if not url:
        raise RuntimeError("다운로드 URL 없음 (비공개면 github_releases_token/GH_TOKEN 필요)")
    emit(f"[Update] 자산 다운로드: {name}")
    code, body, _ = _github_request(
        url,
        token=tok,
        accept="application/octet-stream",
        timeout=300.0,
    )
    if code != 200:
        raise RuntimeError(f"asset 다운로드 HTTP {code}")
    dest_zip.write_bytes(body)
    return dest_zip


def check_for_update(
    *,
    remote: str = "",
    branch: str = "",
    fetch: bool = True,
    log: Optional[Callable[[str], None]] = None,
) -> UpdateCheckResult:
    """
    GitHub 최신 Release 와 로컬 APP_VERSION 을 비교한다.

    ``remote``/``branch``/``fetch`` 는 하위 호환용(무시).
    """
    del remote, branch, fetch  # 구 API 호환
    emit = lambda m: _safe_emit(log, m)
    local_ver = get_local_version()
    repo = resolve_github_repo()
    token = resolve_github_token()

    try:
        emit(f"[Update] GitHub Release 확인: {repo} ...")
        data = fetch_latest_release(repo=repo, token=token)
    except Exception as e:
        hint = ""
        if not token:
            hint = (
                " (비공개 저장소면 GH_TOKEN 또는 settings.github_releases_token 필요)"
            )
        return UpdateCheckResult(
            ok=False,
            local_version=local_ver,
            remote_version="",
            update_available=False,
            remote_ref=f"github:{repo}",
            error=f"{e}{hint}",
        )

    tag = str(data.get("tag_name") or "").strip()
    remote_ver = version_from_release_tag(tag)
    if not remote_ver:
        return UpdateCheckResult(
            ok=False,
            local_version=local_ver,
            remote_version="",
            update_available=False,
            remote_ref=f"github:{repo}",
            release_tag=tag,
            error=f"Release 태그에서 버전 파싱 실패: {tag!r}",
        )

    assets = data.get("assets") if isinstance(data.get("assets"), list) else []
    asset = _pick_release_asset(assets, remote_ver)
    if asset is None:
        return UpdateCheckResult(
            ok=False,
            local_version=local_ver,
            remote_version=remote_ver,
            update_available=False,
            remote_ref=f"github:{repo}",
            release_tag=tag,
            error=f"Release {tag} 에 k-trader-*.zip 자산 없음",
        )

    newer = version_is_newer(remote_ver, local_ver)
    # 동일 버전이어도 자산이 있고 로컬과 태그가 다르면(재게시) 갱신 제안하지 않음 — 버전만
    available = bool(newer)

    if newer:
        msg = f"새 버전 있음: {local_ver} → {remote_ver}"
    else:
        msg = f"최신입니다 (v{local_ver})"

    return UpdateCheckResult(
        ok=True,
        local_version=local_ver,
        remote_version=remote_ver,
        update_available=available,
        remote_ref=f"github:{repo}",
        release_tag=tag,
        asset_name=str(asset.get("name") or ""),
        asset_id=int(asset.get("id") or 0),
        asset_url=str(asset.get("browser_download_url") or ""),
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

    lines.append(f"로컬: v{result.local_version}")
    lines.append(
        f"원격({result.remote_ref} {result.release_tag}): v{result.remote_version}"
    )
    if result.asset_name:
        lines.append(f"자산: {result.asset_name}")
    lines.append(result.message or "")
    lines.append("")
    lines.append("적용 시: GitHub Release zip → 운영(c:\\k-trader) 배포 → 재시작")
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
    emit = lambda m: _safe_emit(log, m)
    script = resolve_update_script()
    if script is None:
        return False, f"remote_update.bat 없음 ({resolve_prod_root()})"
    try:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
        subprocess.Popen(
            ["cmd", "/c", str(script)],
            cwd=str(script.parent),
            creationflags=creationflags,
        )
        emit(f"[Update] 업데이트 스크립트 기동: {script}")
        return True, str(script)
    except Exception as e:
        return False, f"스크립트 기동 실패: {e!r}"


def _restart_production_gui(prod: Path, log: Callable[[str], None]) -> bool:
    """운영 GUI 재기동."""
    pyw = prod / ".venv" / "Scripts" / "pythonw.exe"
    py = prod / ".venv" / "Scripts" / "python.exe"
    bat = prod / "run_k_trader.bat"
    try:
        if pyw.is_file():
            subprocess.Popen(
                [str(pyw), "-u", "k_trader_gui.py", "--auto-start"],
                cwd=str(prod),
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0x8)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200),
            )
            log("[Update] pythonw 로 GUI 재시작")
            return True
        if py.is_file():
            subprocess.Popen(
                [str(py), "-u", "k_trader_gui.py", "--auto-start"],
                cwd=str(prod),
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
                if sys.platform == "win32"
                else 0,
            )
            log("[Update] python 로 GUI 재시작")
            return True
        if bat.is_file():
            subprocess.Popen(
                ["cmd", "/c", str(bat), "--auto-start"],
                cwd=str(prod),
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)
                if sys.platform == "win32"
                else 0,
            )
            log("[Update] run_k_trader.bat 로 GUI 재시작")
            return True
    except Exception as e:
        log(f"[Update] 재시작 실패: {e!r}")
        return False
    log("[Update] pythonw/python/run_k_trader.bat 없음")
    return False


def apply_latest_release_update(
    *,
    log: Optional[Callable[[str], None]] = None,
    restart: bool = True,
    prod_root: Optional[Path] = None,
) -> Tuple[bool, str]:
    """
    최신 Release zip 을 받아 운영 폴더에 적용한다.

    Returns:
        (성공, 메시지)
    """
    emit = lambda m: _safe_emit(log, m)
    prod = Path(prod_root) if prod_root is not None else resolve_prod_root()

    result = check_for_update(log=emit)
    if not result.ok:
        return False, result.error or "확인 실패"
    if not result.update_available:
        # GUI 는 이미 종료된 경우가 많으므로 최신이어도 재기동
        if restart:
            _restart_production_gui(prod, emit)
        return True, result.message or "이미 최신"

    repo = resolve_github_repo()
    token = resolve_github_token()
    try:
        data = fetch_latest_release(repo=repo, token=token)
        assets = data.get("assets") if isinstance(data.get("assets"), list) else []
        asset = _pick_release_asset(assets, result.remote_version)
        if asset is None:
            return False, "Release zip 자산 없음"
    except Exception as e:
        return False, f"Release 재조회 실패: {e}"

    try:
        # deploy_production 모듈 경로
        tools = DEV_ROOT / "tools"
        if tools.is_dir() and str(tools) not in sys.path:
            sys.path.insert(0, str(tools))
        # 운영만 있는 경우: 번들된 최소 적용 로직
        try:
            from deploy_production import apply_production_zip  # type: ignore
        except Exception:
            apply_production_zip = None  # type: ignore
    except Exception:
        apply_production_zip = None  # type: ignore

    with tempfile.TemporaryDirectory(prefix="k-trader-dl-") as td:
        zip_path = Path(td) / (result.asset_name or "k-trader-update.zip")
        try:
            download_release_asset(
                asset, zip_path, repo=repo, token=token, log=emit
            )
        except Exception as e:
            return False, f"다운로드 실패: {e}"

        emit(f"[Update] zip 적용 -> {prod} (settings/config 유지)")
        try:
            if apply_production_zip is not None:
                apply_production_zip(zip_path, prod, preserve_runtime=True)
            else:
                _apply_zip_fallback(zip_path, prod)
        except Exception as e:
            return False, f"적용 실패: {e}"

    if restart:
        # GUI 파일 잠금 해제 여유
        try:
            time.sleep(1.0)
        except Exception:
            pass
        if not _restart_production_gui(prod, emit):
            return True, (
                f"v{result.remote_version} 적용됨 — GUI 재시작은 수동으로 "
                f"run_k_trader.bat 실행"
            )
    return True, f"v{result.local_version} → v{result.remote_version} 적용 완료"


def _apply_zip_fallback(zip_path: Path, dest: Path) -> None:
    """deploy_production 모듈이 없을 때 최소 zip 병합."""
    import shutil
    import zipfile

    preserve = {"settings.json", "config.py"}
    saved: Dict[str, bytes] = {}
    for name in preserve:
        p = dest / name
        if p.is_file():
            saved[name] = p.read_bytes()

    with tempfile.TemporaryDirectory(prefix="k-trader-fb-") as td:
        root = Path(td)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(root)
        staged = root
        if not (staged / "k_trader_gui.py").is_file():
            found = list(root.rglob("k_trader_gui.py"))
            if not found:
                raise FileNotFoundError("zip 에 k_trader_gui.py 없음")
            staged = found[0].parent
        for item in staged.rglob("*"):
            if not item.is_file():
                continue
            rel = item.relative_to(staged)
            if rel.parts == (rel.name,) and rel.name in preserve:
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
    for name, data in saved.items():
        (dest / name).write_bytes(data)
