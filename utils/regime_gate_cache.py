# -*- coding: utf-8 -*-
"""
Transition 레짐(FDR 일봉) 계산 결과 디스크 캐시.

동일 완료 거래일(as_of) + 동일 설정이면 GUI 재시작 시 FDR 재조회를 생략한다.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Any, Dict, Optional

from utils.server_mode_paths import project_root

_CACHE_FILENAME = "regime_gate_cache.json"

# 레짐 판정에 영향을 주는 설정 키
_FINGERPRINT_KEYS = (
    "use_transition_gate",
    "regime_display_eod_buffer_minutes",
    "transition_prior_share_min",
    "transition_share_delta5_max",
    "transition_share_delta3_max",
    "transition_rvol_min",
    "transition_xs_ret_std_min",
    "transition_soft_slots_a",
    "transition_soft_slots_b",
    "transition_block_s1_on_soft",
    "transition_block_s2_on_soft",
    "transition_ignore_soft",
    "hard_suppress_on_index_up",
    "hard_suppress_index_up_pct",
    "hard_suppress_to_off_only",
    "chop_valid_a_min",
    "chop_valid_b_max",
    "chop_valid_b_share_max_pct",
    "chop_valid_a_floor",
    "max_slots_a",
    "max_slots_b",
    "kodex200_universe_priority",
    # R5_EXIT60 (레짐 조회와 동시 판정)
    "use_rs_persist_guard",
    "rs_persist_corr_max",
    "rs_persist_exit_flash_a",
    "rs_persist_exit_atr_sl_a",
    "use_rotation_a_guard_g1",
    "rotation_a_guard_a_share_min",
    "rotation_a_guard_idx5_min",
    "rotation_a_guard_xs_pct",
)


def cache_file_path() -> str:
    """캐시 파일 절대 경로."""
    return os.path.join(project_root(), _CACHE_FILENAME)


def clear_regime_gate_cache() -> bool:
    """디스크 레짐 캐시를 삭제한다. 성공·파일이 없어도 True."""
    path = cache_file_path()
    try:
        if os.path.isfile(path):
            os.remove(path)
        return True
    except Exception:
        return False


def transition_gate_settings_fingerprint(settings: Optional[dict[str, Any]]) -> str:
    """레짐 판정 관련 설정만 해시한다."""
    st = settings or {}
    payload: Dict[str, Any] = {}
    for key in _FINGERPRINT_KEYS:
        if key in st:
            payload[key] = st.get(key)
    try:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        raw = str(sorted(payload.items()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def load_regime_gate_cache(
    *,
    as_of: str,
    settings: Optional[dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    디스크 캐시를 읽는다.

    as_of·설정 지문이 일치할 때만 payload(dict)를 반환한다.
    """
    path = cache_file_path()
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        if str(data.get("as_of", "")).strip()[:10] != str(as_of or "").strip()[:10]:
            return None
        fp = transition_gate_settings_fingerprint(settings)
        if str(data.get("settings_fingerprint", "")) != fp:
            return None
        payload = data.get("payload")
        if not isinstance(payload, dict):
            return None
        regime = str(payload.get("regime", "")).strip().upper()
        if regime not in ("OFF", "TRANSITION", "HARD"):
            return None
        return dict(payload)
    except Exception:
        return None


def save_regime_gate_cache(
    *,
    as_of: str,
    settings: Optional[dict[str, Any]],
    payload: Dict[str, Any],
) -> None:
    """성공한 레짐 계산 결과를 디스크에 저장한다."""
    path = cache_file_path()
    record = {
        "as_of": str(as_of or "").strip()[:10],
        "settings_fingerprint": transition_gate_settings_fingerprint(settings),
        "computed_at": datetime.now().isoformat(timespec="seconds"),
        "payload": dict(payload),
    }
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
