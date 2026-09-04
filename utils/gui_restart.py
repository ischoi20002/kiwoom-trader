# -*- coding: utf-8 -*-
"""
GUI 재시작 상태 저장·복원.

재시작 직전 감시 여부를 기록하고, 새 프로세스가 같은 상태로 기동하도록 한다.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from typing import Any, Dict, Optional

from utils.server_mode_paths import project_root

_STATE_FILENAME = "gui_restart_state.json"


def _state_roots() -> tuple[str, ...]:
    """재시작 상태 파일을 찾/쓸 루트 (운영 k-trader 우선)."""
    seen: set[str] = set()
    ordered: list[str] = []
    for root in (r"c:\k-trader", project_root(), r"c:\vibe"):
        try:
            abs_root = os.path.abspath(root)
        except Exception:
            continue
        if abs_root in seen:
            continue
        seen.add(abs_root)
        ordered.append(abs_root)
    return tuple(ordered)


def state_file_path() -> str:
    """재시작 상태 JSON 기본 경로 (쓰기용)."""
    roots = _state_roots()
    if roots:
        return os.path.join(roots[0], _STATE_FILENAME)
    return os.path.join(project_root(), _STATE_FILENAME)


def _state_file_candidates() -> tuple[str, ...]:
    return tuple(os.path.join(root, _STATE_FILENAME) for root in _state_roots())


def is_monitoring_active(app: Any) -> bool:
    """앱·트레이딩 컨트롤러 기준으로 실시간 감시 중인지 판별."""
    try:
        if bool(getattr(app, "is_monitoring", False)):
            return True
        tc = getattr(app, "trading_controller", None)
        if tc is not None and bool(getattr(tc, "is_monitoring", False)):
            return True
    except Exception:
        pass
    return False


def write_restart_state(*, resume_monitoring: bool) -> None:
    """재시작 직전 감시 재개 여부를 저장한다."""
    record = {
        "resume_monitoring": bool(resume_monitoring),
        "requested_at": datetime.now().isoformat(timespec="seconds"),
    }
    path = state_file_path()
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass


def read_and_clear_restart_state() -> Optional[Dict[str, Any]]:
    """
    저장된 재시작 상태를 읽고 파일을 삭제한다.

    Returns
    -------
    Optional[dict]
        ``resume_monitoring`` 키를 포함한 dict. 없으면 None.
    """
    data: Optional[Dict[str, Any]] = None
    for path in _state_file_candidates():
        try:
            if not os.path.isfile(path):
                continue
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except Exception:
            continue
        finally:
            try:
                if os.path.isfile(path):
                    os.remove(path)
            except Exception:
                pass
    return data


def resolve_run_script() -> Optional[str]:
    """run_k_trader.bat 절대 경로 (운영 c:\\k-trader 우선)."""
    root = project_root()
    candidates = (
        os.path.join(r"c:\k-trader", "run_k_trader.bat"),
        os.path.join(root, "run_k_trader.bat"),
        os.path.join(r"c:\vibe", "run_k_trader.bat"),
    )
    for path in candidates:
        try:
            if os.path.isfile(path):
                return os.path.abspath(path)
        except Exception:
            continue
    return None


def spawn_gui_restart(*, resume_monitoring: bool) -> bool:
    """
    새 GUI 프로세스를 띄운다.

    Windows 에서 ``cmd /c bat`` 만 쓰면 콘솔이 바로 닫히거나
    인자 전달이 깨지는 경우가 있어, ``start "title" /D ... bat`` 을 사용한다.

    Returns
    -------
    bool
        성공 여부.
    """
    script = resolve_run_script()
    if not script:
        return False
    cwd = os.path.dirname(script)
    try:
        write_restart_state(resume_monitoring=resume_monitoring)
        # START ["title"] [/D path] command [args]
        # title 은 인용 경로와 구분하기 위해 필수.
        cmd = f'start "K-Trader" /D "{cwd}" "{script}"'
        if resume_monitoring:
            cmd += " --auto-start"
        subprocess.Popen(
            cmd,
            shell=True,
            cwd=cwd,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10),
        )
        return True
    except Exception:
        # 폴백: venv pythonw 직접 기동
        try:
            pyw = os.path.join(cwd, ".venv", "Scripts", "pythonw.exe")
            py = os.path.join(cwd, ".venv", "Scripts", "python.exe")
            exe = pyw if os.path.isfile(pyw) else (py if os.path.isfile(py) else None)
            if not exe:
                return False
            args = [exe, "-u", "k_trader_gui.py"]
            if resume_monitoring:
                args.append("--auto-start")
            flags = int(
                getattr(subprocess, "DETACHED_PROCESS", 0x8)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
            )
            subprocess.Popen(
                args,
                cwd=cwd,
                creationflags=flags,
                close_fds=True,
            )
            return True
        except Exception:
            return False
