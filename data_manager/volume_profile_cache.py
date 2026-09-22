# -*- coding: utf-8 -*-
"""볼륨 프로파일·분봉 캐시·텔레그램 래퍼 믹스인."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import pandas as pd

from utils.telegram_sender import send_telegram_message as _send_telegram_message


class VolumeProfileCacheMixin:
    def _fetch_and_build_profile(self, code):
        """[v4.5.9] 429 방지용 순차 프로필 빌더"""
        from kiwoom_rest_api.auth.token import TokenManager
        token = TokenManager().get_token()
        if not token: return
        raw = self.fetch_kiwoom_minute_data(code, token)
        if raw:
            prof, min_prof = self.build_volume_profile(raw)
            return prof, min_prof
        return {}, {}


    def build_volume_profile(self, minute_data):
        if not minute_data: return {}, {}
        try:
            df = pd.DataFrame(minute_data); f_vl='trde_qty' if 'trde_qty' in df.columns else ('cntg_vol' if 'cntg_vol' in df.columns else 'volume')
            f_tm='cntr_tm' if 'cntr_tm' in df.columns else ('data_dt' if 'data_dt' in df.columns else 'time')
            df['vol'] = pd.to_numeric(df[f_vl].astype(str).str.replace('+', '').str.replace('-', ''), errors='coerce').fillna(0); df['dt_str'] = df[f_tm].astype(str)
            if len(df['dt_str'].iloc[0]) >= 12: df['day']=df['dt_str'].str[:8]; df['time_slot']=df['dt_str'].str[8:12]
            else: df['day']=datetime.now().strftime('%Y%m%d'); df['time_slot']=df['dt_str'].str[:4]
            df = df.sort_values(by=['day', 'time_slot']); df['cum_vol'] = df.groupby('day')['vol'].cumsum()
            return df.groupby('time_slot')['cum_vol'].mean().to_dict(), df.groupby('time_slot')['vol'].mean().to_dict()
        except: return {}, {}


    def parse_to_bars(self, minute_data, codes):
        """[v4.5.9] 429 에러 방지용 리샘플링 통합 모듈"""
        if not minute_data: return pd.DataFrame()
        try:
            df = pd.DataFrame(minute_data); f_cl='cur_prc'; f_op='open_pric'; f_hg='high_pric'; f_lw='low_pric'; f_tm='cntr_tm'; f_vl='trde_qty'
            for c in [f_cl, f_op, f_hg, f_lw, f_vl]: df[c] = pd.to_numeric(df[c].astype(str).str.replace('+', '').str.replace('-', ''), errors='coerce').fillna(0)
            df['time_str'] = df[f_tm].astype(str); df['datetime'] = pd.to_datetime(df['time_str'], format='%Y%m%d%H%M%S', errors='coerce').fillna(pd.to_datetime(datetime.now().strftime('%Y%m%d')+df['time_str'], format='%Y%m%d%H%M%S', errors='coerce'))
            df.set_index('datetime', inplace=True); df = df.sort_index(); resampled = df.resample('15min', closed='right', label='right').agg({f_op: 'first', f_hg: 'max', f_lw: 'min', f_cl: 'last', f_vl: 'sum'}).dropna()
        
            # [Warmup-Loop] 순차 처리 및 429 에러 복원 로직
            total = len(codes)
            with ThreadPoolExecutor(max_workers=1) as executor:
                for i, code in enumerate(codes):
                    try:
                        print(f"  [Warmup-Bars] {code} 분석 중... ({i+1}/{total})")
                        self._fetch_and_build_profile(code)
                        import random
                        time.sleep(random.uniform(2.0, 4.0)) # Stable spread
                    except Exception as e:
                        print(f"  [Status] {code} Warmup Skipping: {e}")
                        if "429" in str(e): time.sleep(60)
            resampled.columns = ['Open', 'High', 'Low', 'Close', 'Volume']; resampled['time'] = resampled.index.strftime('%H%M')
            return resampled.tail(100)
        except: return pd.DataFrame()


    def prepare_volume_profiles(self, monitor_codes, token, bars_data, volume_profiles, min_volume_profiles):
        """
        [v4.5.91] 감시 종목별 시간대 누적·분봉 거래량 프로필 구축.
        sync_meta·bars_data 가 비어 있으면 키움 분봉 API만 사용(trading_data.db 캔들 미필수).
        """
        if not token: return
        total = len(monitor_codes)
    
        for i, code in enumerate(monitor_codes):
            try:
                # 1. DB/캐시 데이터 최신성 확인
                last_dt_str = self.get_last_sync_time(code)
                is_fresh = False
                if last_dt_str:
                    last_dt = datetime.strptime(last_dt_str, "%Y-%m-%d %H:%M:%S")
                    # 마지막 데이터가 20분 이내라면 최신으로 간주
                    if (datetime.now() - last_dt).total_seconds() < 1200:
                        is_fresh = True

                # 2. 로컬 데이터가 충분하면 API 건너뛰기
                if is_fresh and code in bars_data and not bars_data[code].empty:
                    print(f"  [Warmup-Local] {code} DB 데이터 활용 중... ({i+1}/{total})")
                    df = bars_data[code]
                    # DataFrame을 소스 리스트 형태로 변환하여 프로필 빌드
                    raw = df.reset_index().to_dict('records')
                    prof, min_p = self.build_volume_profile(raw)
                    volume_profiles[code] = prof
                    min_volume_profiles[code] = min_p
                    continue

                # 3. 데이터가 없거나 오래된 경우만 API 호출
                print(f"  [Warmup-API] {code} 최신 데이터 수집 중... ({i+1}/{total})")
                raw = self.fetch_kiwoom_minute_data(code, token)
                if raw:
                    prof, min_p = self.build_volume_profile(raw)
                    volume_profiles[code] = prof
                    min_volume_profiles[code] = min_p
            
                # API 호출 시에만 안전 지연 시간 적용
                import random
                time.sleep(random.uniform(2.0, 4.0))
            
            except Exception as e:
                print(f"[System] {code} Profile Skipping: {e}")
                if "429" in str(e): time.sleep(60)


    def save_bars_cache(self, bars_data, cache_file):
        try:
            cache = {c: df.to_dict(orient='records') for c, df in bars_data.items() if not df.empty}
            with open(cache_file, "w", encoding="utf-8") as f: json.dump(cache, f, ensure_ascii=False, indent=2)
        except: pass


    def load_bars_cache(self, cache_file):
        if not os.path.exists(cache_file): return {}
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f)
                return {c: pd.DataFrame(recs) for c, recs in cache.items()}
        except: return {}


    def send_telegram_message(
        self, message: str, parse_mode: Optional[str] = "HTML"
    ) -> bool:
        """
        텔레그램 Bot API로 메시지 전송 (DB 로직과 분리된 공용 유틸 위임).
        """
        try:
            return _send_telegram_message(
                message,
                parse_mode=parse_mode,
                log_func=print,
            )
        except Exception:
            return False

