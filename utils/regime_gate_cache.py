# -*- coding: utf-8 -*-
"""
Transition 레짐(FDR 일봉) 계산 결과 디스크 캐시.

동일 완료 거래일(as_of) + 동일 설정 + 동일 유니버스면 GUI 재시작 시 FDR 재조회를 생략한다.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

from utils.server_mode_paths import project_root

_CACHE_FILENAME = "regime_gate_cache.json"

# 판정 로직이 바뀌면 올려서 이전 캐시를 무효화한다
_CALC_VERSION = "sliding_lookback_v1"

# 레짐 판정에 영향을 주는 설정 키
_FINGERPRINT_KEYS = (
    "use_transition_gate",
    "regime_display_eod_buffer_minutes",
    "bt_universe_indicator_lookback_days",
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
    payload: Dict[str, Any] = {"_calc_version": _CALC_VERSION}
    for key in _FINGERPRINT_KEYS:
        if key in st:
            payload[key] = st.get(key)
    # 애프터 정책 OFF 면 일봉을 정규장 기준으로 덧씌우므로 레짐 결과가 달라진다 (ON 은 기존 지문 유지)
    try:
        from utils.aftermarket_policy import aftermarket_bars_enabled_for_settings

        if not aftermarket_bars_enabled_for_settings(st):
            payload["_aftermarket_daily"] = "regular_kiwoom"
    except Exception:
        pass
    try:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        raw = str(sorted(payload.items()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def universe_codes_fingerprint(codes: Optional[Iterable[Any]]) -> str:
    """레짐 판정 유니버스(종목코드 집합) 해시. 비어 있으면 빈 문자열."""
    try:
        norm = sorted(
            {str(c).strip().zfill(6) for c in (codes or []) if str(c).strip()}
        )
    except Exception:
        return ""
    if not norm:
        return ""
    return hashlib.sha256(",".join(norm).encode("utf-8")).hexdigest()[:16]


def load_regime_gate_cache(
    *,
    as_of: str,
    settings: Optional[dict[str, Any]],
    universe_codes: Optional[Iterable[Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    디스크 캐시를 읽는다.

    as_of·설정 지문·유니버스 지문이 일치할 때만 payload(dict)를 반환한다.
    ``universe_codes`` 가 비면(유니버스 확인 불가) 캐시를 쓰지 않는다 —
    분기 전환 등으로 유니버스가 바뀐 뒤 이전 유니버스 레짐을 재사용하지 않기 위함.
    """
    u_fp = universe_codes_fingerprint(universe_codes)
    if not u_fp:
        return None
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
        if str(data.get("universe_fingerprint", "")) != u_fp:
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
    universe_codes: Optional[Iterable[Any]] = None,
) -> None:
    """성공한 레짐 계산 결과를 디스크에 저장한다 (계산에 쓴 유니버스 지문 포함)."""
    path = cache_file_path()
    record = {
        "as_of": str(as_of or "").strip()[:10],
        "settings_fingerprint": transition_gate_settings_fingerprint(settings),
        "universe_fingerprint": universe_codes_fingerprint(universe_codes),
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
