# -*- coding: utf-8 -*-
import requests
import threading
import config
import os

# [v3.7] 환경변수는 메인 GUI(k_trader_gui.py)에서 USE_MOCK 설정에 따라 동적으로 설정됩니다.

class AuthManager:
    """키움 REST API 인증 및 토큰 관리 (v3.2: 백업 로직 절대 복구)"""
    def __init__(self, logger_callback=None):
        self.access_token = None
        self.logger = logger_callback or print

    def get_access_token(self):
        """[v3.7.8] 현재 저장된 액세스 토큰 반환"""
        return self.access_token

    def get_token(self):
        """[COMPAT] kiwoom_rest_api 패키지 호환용"""
        return self.get_auth_token()

    def log(self, message):
        if self.logger:
            self.logger(message)

    def get_auth_token(self):
        """키움 REST API 토큰 발급 시도 (v3.7: 모의투자 환경 대응)"""
        self.log(f"[Auth] 키움 토큰 발급 시도 중 ({'모의' if getattr(config, 'USE_MOCK', False) else '실전'} 모드)...")
        try:
            # 설정에 따른 URL 및 키 선택
            use_mock = getattr(config, 'USE_MOCK', False)
            base_url = config.MOCK_URL if use_mock else config.URL
            auth_url = f"{base_url}/oauth2/token"
            
            # 모의투자용 키가 있는 경우 우선 사용
            m_key = getattr(config, 'MOCK_APP_KEY', "")
            m_sec = getattr(config, 'MOCK_APP_SECRET', "")
            
            app_key = m_key if (use_mock and m_key) else config.APP_KEY
            app_sec = m_sec if (use_mock and m_sec) else config.APP_SECRET
            
            auth_data = {
                "grant_type": "client_credentials", 
                "appkey": app_key, 
                "secretkey": app_sec
            }
            # [DEBUG] 자격 증명 로드 확인 (DEACTIVATE FOR USER PRIVACY)
            # self.log(f"[Debug] AppKey Loaded: {config.APP_KEY[:5]}***, Secret Loaded: {config.APP_SECRET[:5]}***")
            # self.log(f"[Debug] Auth URL: {auth_url}")
            
            # [v3.2] 헤더를 완전히 제거하여 백업 코드 'requests.post(auth_url, json=auth_data)'와 100% 일치시킴
            res = requests.post(auth_url, json=auth_data, timeout=15)
            
            if res.status_code != 200:
                self.log(f"[Error] 토큰 발급 실패 (HTTP {res.status_code}): {res.text[:100]}")
                return None
            
            # [v3.3] 점검 상황 감지 로직 추가
            content_type = res.headers.get("Content-Type", "").lower()
            if "text/html" in content_type or "<html" in res.text[:100].lower():
                # HTML 응답인 경우 점검 페이지인지 확인
                maintenance_keywords = ["시스템", "점검", "작업", "알림", "서비스", "maintenance", "notice"]
                if any(kw in res.text for kw in maintenance_keywords) or res.status_code == 302:
                    self.log("[System] 키움 API 서버가 현재 점검 중이거나 접근이 제한되었습니다 (302/HTML).")
                    self.log("[Tip] 매주 토요일은 정기 점검 시간(보통 14~17시)이므로 API 기능이 제한됩니다. 로컬 데이터로 계속 진행합니다.")
                else:
                    self.log(f"[Error] 예상치 못한 응답 수신 (Status {res.status_code})")
                return None

            try:
                data = res.json()
            except Exception as je:
                self.log(f"[Error] JSON 파싱 실패: {je}\n[Status]: {res.status_code}\n[Raw Content]: {res.text[:300]}")
                return None

            if data.get("return_code") == 0 or 'token' in data:
                self.access_token = data.get("token")
                self.log("[Ok] 토큰 발급 성공!")
                return self.access_token
            else:
                self.log(f"[Error] 토큰 발급 실패: {data}")
                return None
        except Exception as e:
            self.log(f"[Error] 인증 에러: {e}")
            return None

    def is_authenticated(self):
        return self.access_token is not None
