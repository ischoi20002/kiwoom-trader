# -*- coding: utf-8 -*-
"""설정 파일·분봉 캐시 관리 (GUI·core 단일 구현)."""
from __future__ import annotations

import io
import json
import os
from typing import Any, Callable, Dict, Optional

import pandas as pd


class ConfigHandler:
    """설정 JSON 및 bars_cache.json 입출력."""

    # 구 KOSDAQ150 하베스트 잔재 — 저장 시 제거
    _DROP_KEYS = {
        "harvest_kosdaq150_enabled",
        "kosdaq_top_n",
        "kosdaq_harvest_union_start",
    }

    def __init__(
        self,
        settings_file: str = "settings.json",
        cache_file: str = "bars_cache.json",
        logger_callback: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.settings_file = settings_file
        self.cache_file = cache_file
        self.log = logger_callback or print
        # 구 API 호환
        self.logger = self.log

    def load_settings(self, default_settings: Dict[str, Any]) -> Dict[str, Any]:
        """settings.json 로드 + 기본값 병합 + ATR 트레일 키 마이그레이션."""
        if not os.path.exists(self.settings_file):
            return default_settings.copy()
        try:
            with open(self.settings_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                return default_settings.copy()
            loaded_keys = set(loaded.keys())
            full_settings = default_settings.copy()
            full_settings.update(loaded)
            try:
                # ATR 트레일 무장: JSON에 S1/S2 분리 키가 없으면 구 트랙 단일 키로 양쪽 채움
                if (
                    "use_atr_trail_only_after_tp_s1_a" not in loaded_keys
                    and "use_atr_trail_only_after_tp_s2_a" not in loaded_keys
                ):
                    leg_a = bool(
                        loaded.get(
                            "use_atr_trail_only_after_tp_a",
                            full_settings.get("use_atr_trail_only_after_tp", False),
                        )
                    )
                    full_settings["use_atr_trail_only_after_tp_s1_a"] = leg_a
                    full_settings["use_atr_trail_only_after_tp_s2_a"] = leg_a
                if (
                    "use_atr_trail_only_after_tp_s1_b" not in loaded_keys
                    and "use_atr_trail_only_after_tp_s2_b" not in loaded_keys
                ):
                    leg_b = bool(
                        loaded.get(
                            "use_atr_trail_only_after_tp_b",
                            full_settings.get("use_atr_trail_only_after_tp", False),
                        )
                    )
                    full_settings["use_atr_trail_only_after_tp_s1_b"] = leg_b
                    full_settings["use_atr_trail_only_after_tp_s2_b"] = leg_b
            except Exception:
                pass
            return full_settings
        except Exception as e:
            try:
                self.log(f"⚠️ 설정 로드 에러: {e}")
            except Exception:
                pass
            return default_settings.copy()

    def save_settings(self, settings: Dict[str, Any]) -> bool:
        """
        설정 저장.
        1) KOSDAQ 잔재 키 제거·유니버스 정규화
        2) defaults 허용 키로 필터 (좀비 파라미터 제거)
        """
        try:
            if not isinstance(settings, dict):
                return False
            # 원본 dict를 직접 수정(GUI 기존 동작) — 호출부 settings 와 동기화
            for k in list(settings.keys()):
                try:
                    ks = str(k)
                except Exception:
                    continue
                if ks in self._DROP_KEYS or ks.startswith("kq_"):
                    settings.pop(k, None)
            try:
                choice = str(
                    settings.get("universe_index_choice", "") or ""
                ).strip().upper()
            except Exception:
                choice = ""
            if choice in ("KOSDAQ", "KOSDAQ150", "KQ150"):
                settings["universe_index_choice"] = "KOSPI200"

            from core.defaults import get_allowed_keys

            allowed = get_allowed_keys(settings)
            filtered = {k: v for k, v in settings.items() if k in allowed}
            # 필터 결과를 호출부 settings 에도 반영(좀비 키 제거)
            for k in list(settings.keys()):
                if k not in filtered:
                    settings.pop(k, None)

            with open(self.settings_file, "w", encoding="utf-8") as f:
                json.dump(filtered, f, indent=4, ensure_ascii=False)
            return True
        except Exception as e:
            try:
                self.log(f"❌ 설정 저장 실패: {e}")
            except Exception:
                pass
            return False

    def load_bars_cache(self) -> Dict[str, pd.DataFrame]:
        """
        분봉 캐시 로드.
        라이브 포맷: 종목코드 → DataFrame.to_json() 문자열.
        레거시 포맷: 종목코드 → records(list[dict]).
        """
        if not os.path.exists(self.cache_file):
            return {}
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            bars_data: Dict[str, pd.DataFrame] = {}
            for code, payload in data.items():
                try:
                    if isinstance(payload, str):
                        bars_data[code] = pd.read_json(io.StringIO(payload))
                    elif isinstance(payload, list):
                        bars_data[code] = pd.DataFrame(payload)
                    elif isinstance(payload, dict):
                        # orient='split' 등 dict JSON
                        bars_data[code] = pd.read_json(io.StringIO(json.dumps(payload)))
                except Exception:
                    continue
            if bars_data:
                try:
                    self.log(
                        f"✅ 캐시에서 {len(bars_data)}종목 데이터를 로드했습니다."
                    )
                except Exception:
                    pass
            return bars_data
        except Exception as e:
            try:
                self.log(f"⚠️ 캐시 로드 에러: {e}")
            except Exception:
                pass
            return {}

    def save_bars_cache(self, bars_data: Dict[str, Any]) -> bool:
        """분봉 캐시 저장 — 라이브와 동일하게 DataFrame.to_json() 문자열."""
        try:
            data: Dict[str, str] = {}
            for code, df in (bars_data or {}).items():
                try:
                    if df is None or getattr(df, "empty", True):
                        continue
                    data[str(code)] = df.to_json()
                except Exception:
                    continue
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f)
            return True
        except Exception as e:
            try:
                self.log(f"⚠️ 캐시 저장 에러: {e}")
            except Exception:
                pass
            return False
