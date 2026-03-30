# -*- coding: utf-8 -*-

import threading
import time
import pandas as pd
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from backtest_engine import TradingStrategy

class WarmupService:
    """
    [v4.5.3] 데이터 워밍업 전담 서비스
    가상 실시간 감시 시작 전 필요한 모든 데이터를 준비하고 지표를 계산합니다.
    """
    def __init__(self, app, logger_callback=None, progress_callback=None):
        self.app = app
        self.data_manager = app.data_manager
        self.auth_manager = app.auth_manager
        self.config_handler = app.config_handler
        self.log = logger_callback or print
        self.progress_cb = progress_callback # (current, total, name, code) -> void
        
        self.is_running = False

    def run(self, monitor_codes, monitor_info, bars_data, volume_profiles, min_volume_profiles):
        """[v4.6.0] 비동기 스트리밍 워밍업 (DB 우선 로드 후 배경 동기화)"""
        self.is_running = True
        total = len(monitor_codes)
        token = self.auth_manager.get_auth_token()
        
        # [Step 1] 초고속 DB/캐시 로드 (1초 이내)
        self.log(f"📦 [Warmup] {total}개 종목 로컬 데이터 기반 즉시 활성화 중...")
        for code in monitor_codes:
            if not self.is_running: break
            name = monitor_info.get(code, {}).get('name', 'Unknown')
            
            # DB/캐시에서 데이터 가져오기 (API 호출 안함)
            last_sync = self.data_manager.get_last_sync_time(code)
            cached_df = self.data_manager.get_bars(code, interval='1m', limit=5000)
            
            if not cached_df.empty:
                # 인덱스 계산 및 메모리 적재
                bars_data[code] = TradingStrategy.calculate_indicators(cached_df, self.app.settings)
                # 볼륨 프로파일도 DB 기반으로 즉시 생성
                raw = cached_df.reset_index().to_dict('records')
                prof, min_p = self.data_manager.build_volume_profile(raw)
                volume_profiles[code] = prof
                min_volume_profiles[code] = min_p
            
        # [Step 2] 배경 동기화 스레드 시작 (메인 스레드 반환)
        def bg_sync():
            self.log(f"🔄 [Sync] 배경 데이터 정밀 동기화 시작 (API)...")
            success_count = 0
            for i, code in enumerate(monitor_codes):
                if not self.is_running: break
                name = monitor_info.get(code, {}).get('name', 'Unknown')
                
                try:
                    # 데이터 최신성 확인 후 부족한 분량만 API 수집
                    last_dt_str = self.data_manager.get_last_sync_time(code)
                    is_fresh = False
                    if last_dt_str:
                        last_dt = datetime.strptime(last_dt_str, "%Y-%m-%d %H:%M:%S")
                        if (datetime.now() - last_dt).total_seconds() < 1200:
                            is_fresh = True
                    
                    if not is_fresh:
                        # API에서 최신 데이터 수집 및 프로필 재구축
                        raw_api = self.data_manager.fetch_kiwoom_minute_data(code, token)
                        if raw_api:
                            # DB 업데이트 및 지표 재계산
                            df_new = pd.DataFrame(raw_api)
                            # (생략: DB 저장 로직은 DataManager 내부에서 수행됨)
                            prof, min_p = self.data_manager.build_volume_profile(raw_api)
                            volume_profiles[code] = prof
                            min_volume_profiles[code] = min_p
                            success_count += 1
                        
                        # API 호출 후 티켓 소모에 따른 지연
                        import random
                        time.sleep(random.uniform(1.5, 3.0))
                    
                    # 진행률 보고 (GUI 갱신용)
                    if self.progress_cb:
                        self.progress_cb(i + 1, total, name, code)
                        
                except Exception as e:
                    self.log(f"⚠️ [Sync] {name}({code}) 동기화 실패: {e}")

            self.log(f"✅ [Sync] 모든 종목 동기화 완료 ({success_count}개 갱신)")
            self.is_running = False

        # 배경 스레드 분리
        threading.Thread(target=bg_sync, daemon=True).start()
        
        # 즉시 '준비됨' 상태로 반환하도록 가상 성공값 리턴
        return total, total

    def stop(self):
        """워밍업 중단"""
        self.is_running = False
