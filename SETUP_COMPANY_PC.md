# 회사 PC 동일 환경 세팅

## 1. 저장소

```text
git clone https://github.com/ischoi20002/kiwoom-trader.git
cd kiwoom-trader
```

## 2. Python

이 PC에서 동작 확인된 버전: **Python 3.12** (3.10+ 권장).

```text
py -3.12 -m venv venv
venv\Scripts\activate
```

## 3. 의존성

- **최소 설치(프로젝트 명시 의존성만)**: `pip install -r requirements.txt`
- **이 개발 PC의 pip 환경을 그대로 맞추려면**: `pip install -r requirements-lock.txt`  
  (전역 환경 기준 `pip freeze`이므로 패키지 수가 많습니다. 동일 재현이 목적일 때만 사용하세요.)

## 4. Git에 포함되지 않는 것(대용량 DB 등)

- `trading_data.db` — 하베스팅·시세 DB. 별도 USB/네트워크 복사 또는 회사 PC에서 하베스팅 재실행.
- `realtime_15m*.db`, `KTrader.db`, `trading_state_live.db`, `trading_state_mock.db` 등 — `.gitignore` 참고.
- `trading_state.db`는 소형이라 저장소에 포함될 수 있습니다(브랜치·민감도에 따라 회사 정책에 맞게 조정).

## 5. 설정

- `settings.json`, `config.py`(또는 `USE_MOCK` 등) — 저장소에 맞춰 두고, **API 키·토큰**은 회사 정책에 따라 환경변수나 로컬 전용 파일로 분리하세요.
