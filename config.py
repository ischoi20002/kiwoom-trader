# config.py
# 키움증권 REST API 공용 설정 파일

# 1. 본인의 정보를 입력하세요 (따옴표 안에 넣으세요)
APP_KEY = "3qV0WGDfHHTDrievzvsl0Yvt-7u6VLSOwS469J_AYP0"
APP_SECRET = "-TZiGwo1WpQSTPDnoDJAAblpcbkuCqpPpCC9mPlFA80"

# 2. 거래 서버 설정 (실전/모의)
REAL_URL = "https://api.kiwoom.com"
MOCK_URL = "https://mockapi.kiwoom.com"
USE_MOCK = True # 서버 전환 버튼에 의해 자동 수정됨

# [Fix] USE_MOCK 값에 따라 최종 접속 URL을 결정합니다.
URL = MOCK_URL if USE_MOCK else REAL_URL

MOCK_APP_KEY = "5-_eJyH9IVD8SFmAqXMHas0jaTCS0O9nJ-_ivKx4KLI"                     # 모의투자용 App Key
MOCK_APP_SECRET = "NFlzCETCLsvRQ_BwghdIlLW_h0p39YlZZANiimKeF-E"                  # 모의투자용 Secret Key
ALLOW_MOCK_ORDER = True # 모의투자 주문 허용 여부
ALLOW_REAL_ORDER = False # 실전투자 주문 허용 여부 (매우 주의!)

# 3. 텔레그램 알림 설정 (본인의 봇 토큰과 채팅방 ID를 입력하세요)
TELEGRAM_BOT_TOKEN = "8696479782:AAFIpWSn7BmeYfXgv3EypV1kyJ51LdCjtn8"
TELEGRAM_CHAT_ID = "8293995996"
