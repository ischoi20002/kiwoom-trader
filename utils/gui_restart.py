# -*- coding: utf-8 -*-
"""
GUI 재시작 상태 저장·복원.

재시작 직전 감시 여부를 기록하고, 새 프로세스가 같은 상태로 기동하도록 한다.
재기동은 콘솔 창 없이 ``pythonw`` 를 우선 사용한다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

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


def resolve_gui_workdir() -> Optional[str]:
    """GUI 기동 작업 디렉터리 (운영 k-trader 우선)."""
    script = resolve_run_script()
    if script:
        return os.path.dirname(script)
    try:
        prod = r"c:\k-trader"
        if os.path.isfile(os.path.join(prod, "k_trader_gui.py")):
            return prod
    except Exception:
        pass
    return None


def resolve_gui_python(*, prefer_windowed: bool = True) -> Optional[str]:
    """
    GUI 실행용 파이썬 경로.

    prefer_windowed=True 이면 콘솔 없는 pythonw 를 우선한다.
    """
    cwd = resolve_gui_workdir()
    if not cwd:
        return None
    if prefer_windowed:
        names: Sequence[str] = (
            os.path.join("python", "pythonw.exe"),
            os.path.join("python", "python.exe"),
            os.path.join(".venv", "Scripts", "pythonw.exe"),
            os.path.join(".venv", "Scripts", "python.exe"),
        )
    else:
        names = (
            os.path.join("python", "python.exe"),
            os.path.join("python", "pythonw.exe"),
            os.path.join(".venv", "Scripts", "python.exe"),
            os.path.join(".venv", "Scripts", "pythonw.exe"),
        )
    for rel in names:
        try:
            cand = os.path.join(cwd, rel)
            if os.path.isfile(cand):
                return os.path.abspath(cand)
        except Exception:
            continue
    return None


def spawn_gui_restart(*, resume_monitoring: bool) -> bool:
    """
    새 GUI 프로세스를 콘솔 없이 띄운다 (pythonw 우선).

    Returns
    -------
    bool
        성공 여부.
    """
    cwd = resolve_gui_workdir()
    if not cwd:
        return False
    try:
        write_restart_state(resume_monitoring=resume_monitoring)
    except Exception:
        pass

    exe = resolve_gui_python(prefer_windowed=True)
    if exe:
        try:
            entry = "run_entry.py"
            try:
                if not os.path.isfile(os.path.join(cwd, entry)):
                    entry = "k_trader_gui.py"
            except Exception:
                entry = "k_trader_gui.py"
            args: List[str] = [exe, "-u", entry]
            if resume_monitoring:
                args.append("--auto-start")
            flags = 0
            if sys.platform == "win32":
                flags = int(
                    getattr(subprocess, "DETACHED_PROCESS", 0x8)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
                    | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                )
            subprocess.Popen(
                args,
                cwd=cwd,
                creationflags=flags,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            pass

    # 최후 폴백: bat (콘솔이 뜰 수 있음)
    script = resolve_run_script()
    if not script:
        return False
    try:
        cmd = f'start "K-Trader" /D "{cwd}" "{script}"'
        if resume_monitoring:
            cmd += " --auto-start"
        subprocess.Popen(cmd, shell=True, cwd=cwd)
        return True
    except Exception:
        return False
