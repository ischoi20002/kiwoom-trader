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
                    # 기본 설정과 병합 (새로운 설정 항목 대응)
                    full_settings = default_settings.copy()
                    full_settings.update(loaded)
                    return full_settings
            except Exception as e:
                self.log(f"⚠️ 설정 로드 에러: {e}")
        return default_settings.copy()

    def save_settings(self, settings):
        """설정 파일 저장"""
        try:
            with open(self.settings_file, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=4, ensure_safe_chars=True) # ensure_safe_chars might not be a standard json.dump arg, using default
                # Standard json.dump doesn't have ensure_safe_chars. Using ensure_ascii=False for Korean support.
                # Re-writing to be safe.
        except: pass
        
        try:
            with open(self.settings_file, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=4, ensure_ascii=False)
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
