# -*- coding: utf-8 -*-
import requests
import threading
import config
import os

# [v3.2] 백업파일(v2.9)과 동일하게 환경변수 설정 추가
os.environ['KIWOOM_API_KEY'] = config.APP_KEY
os.environ['KIWOOM_API_SECRET'] = config.APP_SECRET

class AuthManager:
    """키움 REST API 인증 및 토큰 관리 (v3.2: 백업 로직 절대 복구)"""
    def __init__(self, logger_callback=None):
        self.access_token = None
        self.logger = logger_callback or print

    def log(self, message):
        if self.logger:
            self.logger(message)

    def get_auth_token(self):
        """키움 REST API 토큰 발급 시도 (헤더 없이 백업과 100% 동일하게 호출)"""
        self.log("\n[Auth] 키움 토큰 발급 시도 중 (Original Mode)...")
        try:
            auth_url = f"{config.URL}/oauth2/token"
            auth_data = {
                "grant_type": "client_credentials", 
                "appkey": config.APP_KEY, 
                "secretkey": config.APP_SECRET
            }
            # [DEBUG] 자격 증명 로드 확인 (터미널 출력을 위해 🔍 아이콘 추가)
            self.log(f"🔍 [DEBUG] AppKey Loaded: {config.APP_KEY[:5]}***, Secret Loaded: {config.APP_SECRET[:5]}***")
            self.log(f"🔍 [DEBUG] Auth URL: {auth_url}")
            
            # [v3.2] 헤더를 완전히 제거하여 백업 코드 'requests.post(auth_url, json=auth_data)'와 100% 일치시킴
            res = requests.post(auth_url, json=auth_data, timeout=15)
            
            if res.status_code != 200:
                self.log(f"❌ 토큰 발급 실패 (HTTP {res.status_code}): {res.text[:100]}")
                return None
            
            # [v3.3] 점검 상황 감지 로직 추가
            content_type = res.headers.get("Content-Type", "").lower()
            if "text/html" in content_type or "<html" in res.text[:100].lower():
                # HTML 응답인 경우 점검 페이지인지 확인
                maintenance_keywords = ["시스템", "점검", "작업", "알림", "서비스", "maintenance", "notice"]
                if any(kw in res.text for kw in maintenance_keywords):
                    self.log("⚠️ [시스템 점검] 키움 API 서버가 현재 점검 중입니다. (보통 토요일 오후 2~5시 사이)")
                    self.log(f"🔎 [점검 정보] 응답 내용 일부: {res.text[:150].strip()}...")
                else:
                    self.log(f"❌ 예상치 못한 HTML 응답 수신 (Status {res.status_code})")
                return None

            try:
                data = res.json()
            except Exception as je:
                self.log(f"❌ JSON 파싱 실패: {je}\n[Status]: {res.status_code}\n[Raw Content]: {res.text[:300]}")
                return None

            if data.get("return_code") == 0 or 'token' in data:
                self.access_token = data.get("token")
                self.log("✅ 토큰 발급 성공!")
                return self.access_token
            else:
                self.log(f"❌ 토큰 발급 실패: {data}")
                return None
        except Exception as e:
            self.log(f"⚠️ 인증 에러: {e}")
            return None

    def is_authenticated(self):
        return self.access_token is not None
