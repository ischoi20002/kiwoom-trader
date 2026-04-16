# -*- coding: utf-8 -*-
"""
모의/실전(config.USE_MOCK)별 로컬 저장 경로.

- 포지션 JSON: virtual_db_mock.json / virtual_db_live.json
- 주문·활성포지션 SQLite: trading_state_mock.db / trading_state_live.db

구 단일 파일(virtual_db.json, trading_state.db)은 최초 1회 모의 쪽으로 이관합니다.
"""
from __future__ import annotations

import os
import shutil
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def project_root() -> str:
    """프로젝트 루트(vibe)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_mock_mode() -> bool:
    try:
        import config as _cfg

        return bool(getattr(_cfg, "USE_MOCK", True))
    except Exception:
        return True


def virtual_db_path() -> str:
    """현재 서버 모드에 맞는 포지션 JSON 절대 경로."""
    base = project_root()
    name = "virtual_db_mock.json" if is_mock_mode() else "virtual_db_live.json"
    return os.path.join(base, name)


def state_db_path() -> str:
    """현재 서버 모드에 맞는 trading_state 절대 경로."""
    base = project_root()
    name = "trading_state_mock.db" if is_mock_mode() else "trading_state_live.db"
    return os.path.join(base, name)


def state_db_path_for_mock() -> str:
    """모의 전용 state DB 경로(스키마 부트스트랩·구버전 이전용)."""
    return os.path.join(project_root(), "trading_state_mock.db")


def state_db_path_for_live() -> str:
    """실전 전용 state DB 경로."""
    return os.path.join(project_root(), "trading_state_live.db")


def realtime_monitor_15m_db_path() -> str:
    """
    실시간 감시 전용 15분봉 SQLite (하베스팅 trading_data.db 와 분리).

    모의/실전에 따라 별도 파일로 두어 배포 시 대용량 캔들 DB와 독립적으로 동기화 가능.
    """
    base = project_root()
    name = "realtime_15m_mock.db" if is_mock_mode() else "realtime_15m_live.db"
    return os.path.join(base, name)


def migrate_legacy_virtual_db_files() -> None:
    """
    구 단일 virtual_db.json → virtual_db_mock.json 으로 1회 이관.
    실전(virtual_db_live.json)은 별도 기록만 사용(파일 없으면 빈 상태로 시작).
    """
    root = project_root()
    legacy = os.path.join(root, "virtual_db.json")
    mock_p = os.path.join(root, "virtual_db_mock.json")
    try:
        if os.path.isfile(legacy) and not os.path.isfile(mock_p):
            shutil.copy2(legacy, mock_p)
    except OSError:
        pass


def migrate_legacy_trading_state_db_files(main_db_path: str) -> None:
    """
    구 단일 trading_state.db → trading_state_mock.db 으로 1회 이관.
    실전 DB는 스키마만 생성 후 빈 상태로 시작합니다.
    """
    base = os.path.dirname(os.path.abspath(main_db_path))
    legacy = os.path.join(base, "trading_state.db")
    mock_p = os.path.join(base, "trading_state_mock.db")
    try:
        if os.path.isfile(legacy) and not os.path.isfile(mock_p):
            shutil.copy2(legacy, mock_p)
    except OSError:
        pass
