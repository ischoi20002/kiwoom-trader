# -*- coding: utf-8 -*-
"""
동적 백테스트 준비 캐시 (저정밀 튜닝 가속).

유니버스 선정·15분 OHLCV·신호 프레임을 설정 지문으로 재사용한다.
유니버스/OHLCV/신호 모두 메모리+디스크에 저장해 GUI 재시작 후에도 HIT 가능하다.
청산/S2/ATR 등 체결 파라미터만 바꾸면 준비 단계를 건너뛴다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import pickle
import threading
import time
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

CACHE_VERSION = 4
CACHE_DIR_NAME = "_local_bt_prep_cache"

# 선정 스케줄에 영향을 주는 키 접두어
_UNIVERSE_PREFIXES: Tuple[str, ...] = (
    "sel_",
    "use_sel_",
    "transition_",
    "use_transition",
    "rotation_a_guard_",
    "use_rotation_a_guard",
    "rs_persist_",
    "use_rs_persist",
    "kospi_",
    "universe_",
    "bt_universe_",
    "max_slots",
    "type_c_",
    "use_type_c",
    "box_sel_",
)

# 접두어에 안 걸리지만 선정에 쓰이는 키
_UNIVERSE_EXACT = frozenset(
    {
        "min_trde_val",
        "vol_ratio",
        "ma_convergence",
        "vol_lookback",
        "macd_fast_daily",
        "macd_slow_daily",
        "macd_sig_daily",
        "exclude_trading_halt_on_selection",
        "sel_score_mode",
        "sel_s1_pnl_signs",
        "use_sel_track_a",
        "use_sel_track_b",
        "use_sel_track_c",
        "max_slots_a",
        "max_slots_b",
        "max_slots_c",
        "max_slots",
        "max_slots_score_rel_pad",
        "max_slots_band_extra_cap",
        "regime_display_eod_buffer_minutes",
        "use_rotation_a_guard_g1",
        "use_rs_persist_guard",
        "use_type_c",
    }
)

# calculate_all_indicators / 합성 수급
_INDICATOR_KEYS = frozenset(
    {
        "atr_period",
        "atr_period_a",
        "atr_period_b",
        "vol_lookback",
        "prev_vol_lookback",
        "prev_vol_lookback_b",
        "prev_vol_lookback_s1_a",
        "prev_vol_lookback_s1_b",
        "prev_vol_lookback_s2_a",
        "prev_vol_lookback_s2_b",
        "bb_period",
        "use_synthetic_realtime",
    }
)

# get_buy_signals_detailed
_SIGNAL_KEYS = frozenset(
    {
        "buy_rsi_min",
        "buy_rsi_min_b",
        "buy_rsi_max_a",
        "buy_rsi_max_b",
        "use_rsi_min",
        "use_rsi_min_b",
        "use_rsi_sig_filter",
        "ma60_min",
        "ma60_max",
        "ma_convergence",
        "gc_window",
        "vol_surge",
        "vol_surge_b",
        "use_type_a",
        "use_gc",
        "use_vol_surge",
        "use_ma60_safe",
        "use_rsi_signal",
        "rsi_window",
        "use_type_b",
        "use_macd_gc_b",
        "use_trend_aligned_b",
        "use_type_b_vol",
        "use_bb_block_a_2s",
        "bb_block_a_z",
        "use_bb_block_all_3s",
        "bb_block_all_z",
        "use_macd_signal",
        "next_open_1530_enabled",
        "allow_1530_signal_eval",
        "_bt_allow_1530_signal_eval",
        "same_bar_sig_priority",
        "min_15m_bars_for_signal",
        "hp_causal_ready_min_bar_index",
        "use_type_c",
    }
)

_lock = threading.Lock()
_mem: Dict[str, Any] = {
    "univ_fp": None,
    "univ": None,
    "ohlcv_fp": None,
    "ohlcv": None,
    "sig_fp": None,
    "sig": None,
}


def cache_enabled(settings: Optional[Mapping[str, Any]]) -> bool:
    """설정으로 캐시를 끌 수 있다 (기본 ON)."""
    try:
        if settings is None:
            return True
        return bool(settings.get("bt_prep_cache_enabled", True))
    except Exception:
        return True


def cache_dir(base_dir: Optional[str] = None) -> str:
    """로컬 캐시 디렉터리 경로."""
    try:
        root = base_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        root = os.getcwd()
    return os.path.join(root, CACHE_DIR_NAME)


def _norm_val(v: Any) -> Any:
    """지문용 값 정규화."""
    try:
        if v is None:
            return None
        if isinstance(v, bool):
            return bool(v)
        if isinstance(v, (int,)):
            return int(v)
        if isinstance(v, float):
            if not math.isfinite(v):
                return None
            return round(float(v), 10)
        if isinstance(v, (list, tuple)):
            return [_norm_val(x) for x in v]
        if isinstance(v, dict):
            return {str(k): _norm_val(x) for k, x in sorted(v.items(), key=lambda kv: str(kv[0]))}
        if isinstance(v, (bytes, bytearray)):
            return bytes(v).hex()
        return str(v)
    except Exception:
        try:
            return str(v)
        except Exception:
            return None


def _pick_settings(
    settings: Optional[Mapping[str, Any]],
    keys: Iterable[str],
    prefixes: Tuple[str, ...] = (),
) -> Dict[str, Any]:
    """설정에서 지문에 넣을 키만 고른다."""
    out: Dict[str, Any] = {}
    try:
        src = dict(settings or {})
    except Exception:
        src = {}
    key_set = set(keys)
    try:
        for k, v in src.items():
            sk = str(k)
            if sk in key_set or (prefixes and sk.startswith(prefixes)):
                out[sk] = _norm_val(v)
    except Exception:
        pass
    return out


def _hash_blob(blob: Any) -> str:
    """안정 해시."""
    try:
        raw = json.dumps(blob, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        raw = str(blob)
    try:
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
    except Exception:
        return hashlib.sha1(repr(blob).encode("utf-8", errors="replace")).hexdigest()[:20]


def file_mtime_token(path: str) -> str:
    """파일 mtime+size. 없으면 빈 문자열."""
    try:
        st = os.stat(path)
        return f"{int(st.st_mtime)}:{int(st.st_size)}"
    except Exception:
        return ""


def universe_fingerprint(
    settings: Mapping[str, Any],
    *,
    sched_start: str,
    end_date: str,
    db_path: str,
    extra_files: Optional[Iterable[str]] = None,
) -> str:
    """유니버스 선정 지문. 일봉 애프터 정책(정규장 덧씌우기 여부)도 키에 포함."""
    picked = _pick_settings(settings, _UNIVERSE_EXACT, _UNIVERSE_PREFIXES)
    files = [file_mtime_token(db_path)]
    try:
        for p in extra_files or []:
            files.append(file_mtime_token(str(p)))
    except Exception:
        pass
    try:
        from utils.aftermarket_policy import aftermarket_bars_enabled_for_settings

        ah_on = bool(aftermarket_bars_enabled_for_settings(settings))
    except Exception:
        ah_on = True
    blob = {
        "v": CACHE_VERSION,
        "kind": "univ",
        "sched_start": str(sched_start),
        "end_date": str(end_date),
        "settings": picked,
        "files": files,
    }
    # 애프터 정책 OFF → 일봉 정규장 덧씌우기 (ON 은 기존 지문 유지)
    if not ah_on:
        blob["aftermarket_daily"] = "regular_m15"
    return _hash_blob(blob)


def ohlcv_fingerprint(
    univ_fp: str,
    settings: Mapping[str, Any],
    *,
    start_date: str,
    end_date: str,
) -> str:
    """15분 OHLCV(+합성 수급) 지문. 애프터 봉 포함 여부(정책 해석 결과)도 키에 포함."""
    picked = _pick_settings(settings, ("use_synthetic_realtime",))
    try:
        from utils.aftermarket_policy import aftermarket_bars_enabled_for_settings

        ah_on = bool(aftermarket_bars_enabled_for_settings(settings))
    except Exception:
        ah_on = True
    blob = {
        "v": CACHE_VERSION,
        "kind": "ohlcv",
        "univ": str(univ_fp),
        "start_date": str(start_date),
        "end_date": str(end_date),
        "settings": picked,
        "aftermarket_bars": ah_on,
    }
    return _hash_blob(blob)


def signal_fingerprint(
    ohlcv_fp: str,
    settings: Mapping[str, Any],
) -> str:
    """15분 지표+신호 지문."""
    picked = _pick_settings(
        settings,
        set(_INDICATOR_KEYS) | set(_SIGNAL_KEYS),
        ("type_c_",),
    )
    blob = {
        "v": CACHE_VERSION,
        "kind": "sig",
        "ohlcv": str(ohlcv_fp),
        "settings": picked,
    }
    return _hash_blob(blob)


def _ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass


def _univ_disk_path(fp: str, base_dir: Optional[str] = None) -> str:
    return os.path.join(cache_dir(base_dir), f"univ_{fp}.pkl")


def get_universe(fp: str, base_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """유니버스 스케줄 캐시 조회 (메모리 → 디스크)."""
    try:
        with _lock:
            if _mem.get("univ_fp") == fp and isinstance(_mem.get("univ"), dict):
                return _mem["univ"]
    except Exception:
        pass
    path = _univ_disk_path(fp, base_dir)
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            payload = pickle.load(f)
        if not isinstance(payload, dict):
            return None
        if int(payload.get("v", 0) or 0) != CACHE_VERSION:
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        with _lock:
            _mem["univ_fp"] = fp
            _mem["univ"] = data
        return data
    except Exception:
        return None


def put_universe(
    fp: str,
    data: Dict[str, Any],
    base_dir: Optional[str] = None,
) -> None:
    """유니버스 스케줄을 메모리·디스크에 저장."""
    try:
        with _lock:
            _mem["univ_fp"] = fp
            _mem["univ"] = data
    except Exception:
        pass
    try:
        d = cache_dir(base_dir)
        _ensure_dir(d)
        path = _univ_disk_path(fp, base_dir)
        tmp = path + ".tmp"
        payload = {"v": CACHE_VERSION, "saved_at": time.time(), "data": data}
        with open(tmp, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except Exception:
        try:
            tmp = _univ_disk_path(fp, base_dir) + ".tmp"
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass


def _ohlcv_disk_path(fp: str, base_dir: Optional[str] = None) -> str:
    return os.path.join(cache_dir(base_dir), f"ohlcv_{fp}.pkl")


def _sig_disk_path(fp: str, base_dir: Optional[str] = None) -> str:
    return os.path.join(cache_dir(base_dir), f"sig_{fp}.pkl")


def _load_disk_dict(
    path: str,
) -> Optional[Dict[str, Any]]:
    """디스크 pickle → data dict (버전 불일치·손상 시 None)."""
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            payload = pickle.load(f)
        if not isinstance(payload, dict):
            return None
        if int(payload.get("v", 0) or 0) != CACHE_VERSION:
            return None
        data = payload.get("data")
        if not isinstance(data, dict) or not data:
            return None
        return data
    except Exception:
        return None


def _save_disk_dict(
    path: str,
    data: Dict[str, Any],
) -> None:
    """원자적 pickle 저장 (실패 시 tmp 정리)."""
    tmp = path + ".tmp"
    try:
        _ensure_dir(os.path.dirname(path))
        payload = {"v": CACHE_VERSION, "saved_at": time.time(), "data": data}
        with open(tmp, "wb") as f:
            pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass


def get_ohlcv(fp: str, base_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """15분 OHLCV 캐시 조회 (메모리 → 디스크)."""
    try:
        with _lock:
            if _mem.get("ohlcv_fp") == fp and isinstance(_mem.get("ohlcv"), dict):
                return _mem["ohlcv"]
    except Exception:
        pass
    data = _load_disk_dict(_ohlcv_disk_path(fp, base_dir))
    if data is None:
        return None
    try:
        with _lock:
            _mem["ohlcv_fp"] = fp
            _mem["ohlcv"] = data
    except Exception:
        pass
    return data


def put_ohlcv(
    fp: str,
    data: Dict[str, Any],
    base_dir: Optional[str] = None,
) -> None:
    """15분 OHLCV를 메모리·디스크에 저장."""
    try:
        with _lock:
            _mem["ohlcv_fp"] = fp
            _mem["ohlcv"] = data
    except Exception:
        pass
    try:
        _save_disk_dict(_ohlcv_disk_path(fp, base_dir), data)
    except Exception:
        pass


def get_signals(fp: str, base_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """15분 지표+신호 캐시 조회 (메모리 → 디스크)."""
    try:
        with _lock:
            if _mem.get("sig_fp") == fp and isinstance(_mem.get("sig"), dict):
                return _mem["sig"]
    except Exception:
        pass
    data = _load_disk_dict(_sig_disk_path(fp, base_dir))
    if data is None:
        return None
    try:
        with _lock:
            _mem["sig_fp"] = fp
            _mem["sig"] = data
    except Exception:
        pass
    return data


def put_signals(
    fp: str,
    data: Dict[str, Any],
    base_dir: Optional[str] = None,
) -> None:
    """15분 지표+신호를 메모리·디스크에 저장."""
    try:
        with _lock:
            _mem["sig_fp"] = fp
            _mem["sig"] = data
    except Exception:
        pass
    try:
        _save_disk_dict(_sig_disk_path(fp, base_dir), data)
    except Exception:
        pass


def clear_memory_cache() -> None:
    """프로세스 메모리 캐시만 비운다 (테스트용)."""
    with _lock:
        _mem["univ_fp"] = None
        _mem["univ"] = None
        _mem["ohlcv_fp"] = None
        _mem["ohlcv"] = None
        _mem["sig_fp"] = None
        _mem["sig"] = None
