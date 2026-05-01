# -*- coding: utf-8 -*-
"""
운영용 포트폴리오 자산 추이 그래프 원천 데이터.

요구사항
- 일자별 총자산 스냅샷을 저장/덮어쓰기(같은 날짜는 override)
- 운영/모의 분리 디렉토리 사용
- 외부 의존 없음 (daily_settlement/trading_data.db 비의존)
"""

from __future__ import annotations

import json
import os
from datetime import date
from typing import Any, Dict, Optional, Tuple

from utils.server_mode_paths import equity_snapshots_root_dir, project_root


def _ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        # 저장 실패가 매매 흐름을 막지 않도록 삼킵
        pass


def snapshot_file_path(day: date | str) -> str:
    d = day if isinstance(day, date) else str(day)[:10]
    root = equity_snapshots_root_dir()
    return os.path.join(root, f"{d}.json")


def snapshot_file_path_under_root(root_dir: str, day: date | str) -> str:
    d = day if isinstance(day, date) else str(day)[:10]
    return os.path.join(root_dir, f"{d}.json")


def read_equity_snapshot_from_root(
    root_dir: str, day: date | str
) -> Optional[Dict[str, Any]]:
    path = snapshot_file_path_under_root(root_dir, day)
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def equity_snapshots_root_candidates() -> Tuple[str, str]:
    """
    (mock_root, live_root)

    UI가 모드 토글로 인해 현재 mode과 스냅샷 저장 mode이 어긋나는 경우를 방지하기 위해
    두 폴더를 모두 후보로 둡니다.
    """
    base = project_root()
    return (
        os.path.join(base, "equity_snapshots_mock"),
        os.path.join(base, "equity_snapshots_live"),
    )


def upsert_equity_snapshot(day: date | str, record: Dict[str, Any]) -> str:
    """
    동일 날짜에 대해 덮어쓰기(override)합니다.

    Returns
    -------
    str
        저장된 파일 경로(절대 경로). 실패 시 "".
    """
    path = snapshot_file_path(day)
    _ensure_dir(os.path.dirname(path))

    tmp_path = f"{path}.tmp"
    out = dict(record or {})
    out["date"] = str(day)[:10]

    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        # 임의 실패 시에도 tmp 남기지 않음
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return ""

    return os.path.abspath(path)


def read_equity_snapshot(day: date | str) -> Optional[Dict[str, Any]]:
    path = snapshot_file_path(day)
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def load_equity_snapshots_range(
    start_day: date | str, end_day: date | str
) -> Tuple[list[str], list[Dict[str, Any]]]:
    """
    그래프 생성용: 날짜 범위 내 존재하는 스냅샷만 로드합니다.
    """
    s = start_day if isinstance(start_day, date) else str(start_day)[:10]
    e = end_day if isinstance(end_day, date) else str(end_day)[:10]
    try:
        lo = min(s, e)
        hi = max(s, e)
    except Exception:
        lo, hi = s, e

    root = equity_snapshots_root_dir()
    if not os.path.isdir(root):
        return [], []

    days: list[str] = []
    recs: list[Dict[str, Any]] = []
    try:
        for fn in sorted(os.listdir(root)):
            if not fn.endswith(".json"):
                continue
            d = fn[:10]
            if d < lo or d > hi:
                continue
            data = read_equity_snapshot(d)
            if data is None:
                continue
            days.append(d)
            recs.append(data)
    except Exception:
        pass

    return days, recs

