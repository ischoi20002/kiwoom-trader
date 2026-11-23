# config.example.py
# 키움증권 REST API 공용 설정 템플릿
# 최초 설치: 이 파일을 config.py 로 복사한 뒤 값을 채우세요.
# (업데이트 zip 에는 config.py 가 포함되지 않습니다.)

import os

# 공공데이터포털 — 금융위 KRX 상장종목정보 API (선택).
DATA_GO_KR_SERVICE_KEY = os.environ.get("DATA_GO_KR_SERVICE_KEY", "").strip()

# 1. 본인의 정보를 입력하세요
APP_KEY = ""
APP_SECRET = ""

# 2. 거래 서버 설정 (실전/모의)
REAL_URL = "https://api.kiwoom.com"
MOCK_URL = "https://mockapi.kiwoom.com"
USE_MOCK = True  # 서버 전환 버튼에 의해 자동 수정됨

URL = MOCK_URL if USE_MOCK else REAL_URL

KA10080_GLOBAL_MIN_INTERVAL_SEC = 1.05

MOCK_APP_KEY = ""
MOCK_APP_SECRET = ""
ALLOW_MOCK_ORDER = True
ALLOW_REAL_ORDER = False  # 실전투자 주문 허용 여부 (매우 주의!)

# 2-1. 실전 계좌 라이선스 (모의투자는 검사하지 않음)
# 발급자가 tools/issue_license.py 로 만든 license.key 를 프로그램 폴더에 두고,
# 아래 계좌번호를 라이선스에 등록된 실계좌와 동일하게 입력하세요.
LIVE_ACCOUNT_NO = ""  # 예: "1234567890"
LICENSE_ENFORCE_REAL = True  # False 면 실전 주문 시 라이선스 검사 생략(개발용)

# 3. 텔레그램 알림 설정
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""

# 4. KRX 정보데이터시스템(data.krx.co.kr) 계정 — pykrx 공식 시세 조회용 (선택)
# 환경변수 KRX_ID / KRX_PW 가 있으면 그쪽이 우선합니다.
KRX_ID = ""
KRX_PW = ""

# 4. (선택) GitHub Release 업데이트 — 비공개 저장소면 토큰 필요
# 환경변수 GH_TOKEN / GITHUB_TOKEN / KTRADER_GITHUB_TOKEN 권장
# GITHUB_RELEASES_REPO = "owner/repo"
