# config.py
# 키움증권 REST API 공용 설정 파일

import os

# 공공데이터포털 — 금융위 KRX 상장종목정보 API (선택).
# 발급: https://www.data.go.kr (환경변수 DATA_GO_KR_SERVICE_KEY 권장)
DATA_GO_KR_SERVICE_KEY = os.environ.get("DATA_GO_KR_SERVICE_KEY", "").strip()

# 1. 본인의 정보를 입력하세요 (따옴표 안에 넣으세요)
APP_KEY = "YR6wsaXU9xdjuQ8-1EBwpvZOnrsFsNszV7iVTYGZrGg"
APP_SECRET = "WrP8PBGLB2H_6nEQI7yUlhBbZLopPWirP-W-gGYIxQI"

# 2. 거래 서버 설정 (실전/모의)
REAL_URL = "https://api.kiwoom.com"
MOCK_URL = "https://mockapi.kiwoom.com"
USE_MOCK = True # 서버 전환 버튼에 의해 자동 수정됨

# [Fix] USE_MOCK 값에 따라 최종 접속 URL을 결정합니다.
URL = MOCK_URL if USE_MOCK else REAL_URL

# ka10080 REST: 과다 호출(HTTP 429) 방지 — DataManagerV2 가 전역 최소 간격으로 직렬화.
# 키움 ka10080 유량=1(초당 1회) 이므로 기본 1.05초.
# KiwoomApp 기동 시 settings 의 realtime_ka10080_global_min_interval_sec 로 덮어씁니다.
KA10080_GLOBAL_MIN_INTERVAL_SEC = 1.05

MOCK_APP_KEY = "3Z8lR7-KdASRK013UStJk2GfpHmT8YiN3sHZB9yq1sY"                     # 모의투자용 App Key 
MOCK_APP_SECRET = "ucLGu97i-ZvfrXSLGpOnemgzjfVCb7CxmDjElkxHgl8"                  # 모의투자용 Secret Key
ALLOW_MOCK_ORDER = True # 모의투자 주문 허용 여부
ALLOW_REAL_ORDER = False # 실전투자 주문 허용 여부 (매우 주의!)

# 3. 텔레그램 알림 설정 (본인의 봇 토큰과 채팅방 ID를 입력하세요)
TELEGRAM_BOT_TOKEN = "8696479782:AAFIpWSn7BmeYfXgv3EypV1kyJ51LdCjtn8"
TELEGRAM_CHAT_ID = "8293995996"
