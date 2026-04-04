# -*- coding: utf-8 -*-
import json
import os
import pandas as pd

class ConfigHandler:
    """설정 파일 및 데이터 캐시 관리 전용 클래스"""
    def __init__(self, settings_file="settings.json", cache_file="bars_cache.json", logger_callback=None):
        self.settings_file = settings_file
        self.cache_file = cache_file
        self.logger = logger_callback or print

    def log(self, message):
        if self.logger:
            self.logger(message)

    def load_settings(self, default_settings):
        """설정 파일 로드"""
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    loaded_keys = set(loaded.keys())
                    # 기본 설정과 병합 (새로운 설정 항목 대응)
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
                self.log(f"⚠️ 설정 로드 에러: {e}")
        return default_settings.copy()

    def save_settings(self, settings):
        """[v3.5.5] 설정 파일 저장 (유효 키 필터링 포함)"""
        try:
            # [v4.5.3 Modularization] core.defaults에서 허용된 키 가져오기
            from core.defaults import get_allowed_keys
            allowed = get_allowed_keys(settings)
            
            # 필터링 수행 (DEFAULT_SETTINGS에 없는 좀비 파라미터 제거)
            filtered_settings = {k: v for k, v in settings.items() if k in allowed}
            
            with open(self.settings_file, "w", encoding="utf-8") as f:
                json.dump(filtered_settings, f, indent=4, ensure_ascii=False)
            return True
        except Exception as e:
            self.log(f"❌ 설정 저장 실패: {e}")
            return False

    def load_bars_cache(self):
        """분봉 데이터 캐시 로드"""
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    cache_data = json.load(f)
                    bars_data = {}
                    for code, data_list in cache_data.items():
                        bars_data[code] = pd.DataFrame(data_list)
                    self.log(f"✅ 캐시에서 {len(bars_data)}종목 데이터를 로드했습니다.")
                    return bars_data
            except Exception as e:
                self.log(f"⚠️ 캐시 로드 에러: {e}")
        return {}

    def save_bars_cache(self, bars_data):
        """분봉 데이터 캐시 저장 (최근 100개만)"""
        try:
            cache_to_save = {}
            for code, df in bars_data.items():
                # 리스트로 변환하여 저장
                cache_to_save[code] = df.tail(100).to_dict('records')
            
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_to_save, f, ensure_ascii=False)
            return True
        except Exception as e:
            self.log(f"⚠️ 캐시 저장 에러: {e}")
            return False
