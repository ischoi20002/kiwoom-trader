# 회사 PC / 운영 PC 세팅

## A. 운영 PC 신규 설치 (권장)

Setup.exe 는 **GitHub에 올리지 않습니다.** 개발 PC에서 빌드한 뒤 USB·NAS·OneDrive 등으로 전달하세요.

1. `K-Trader-Setup-{버전}.exe` 실행 → 기본 경로 `C:\k-trader` (포터블 Python 포함)
2. 바로가기 또는 `run_k_trader.bat` 실행
3. **첫 실행 설정 마법사**에서 모의/실전 API 키·계좌·(실전) `license.key` 입력
4. 이후 업데이트는 대시보드 **「업데이트 확인」** → GitHub Release **zip** (설정·DB 유지)

> Setup.exe 는 코드 서명 전이면 SmartScreen 경고가 날 수 있습니다.  
> 빌드: `py -3 -u tools/build_installer.py` (Inno Setup 6 필요)  
> 산출: `dist/K-Trader-Setup-*.exe` (로컬 보관, Release 업로드 안 함)

Setup 없이 zip만 쓰는 경우: `C:\k-trader` 에 풀고 `install_k_trader.bat` → `run_k_trader.bat`  
(시스템 Python 3.12 필요). 설정은 마찬가지로 첫 실행 마법사.

---

## B. 개발 PC (저장소 클론)

### 1. 저장소

```text
git clone https://github.com/ischoi20002/kiwoom-trader.git
cd kiwoom-trader
```

### 2. Python

이 PC에서 동작 확인된 버전: **Python 3.12** (3.10+ 권장).

```text
py -3.12 -m venv venv
venv\Scripts\activate
```

### 3. 의존성

- **최소 설치(프로젝트 명시 의존성만)**: `pip install -r requirements.txt`
- **이 개발 PC의 pip 환경을 그대로 맞추려면**: `pip install -r requirements-lock.txt`  
  (전역 환경 기준 `pip freeze`이므로 패키지 수가 많습니다. 동일 재현이 목적일 때만 사용하세요.)

### 4. Git에 포함되지 않는 것(대용량 DB 등)

- `trading_data.db` — 하베스팅·시세 DB. 별도 USB/네트워크 복사 또는 회사 PC에서 하베스팅 재실행.
- `realtime_15m*.db`, `KTrader.db`, `trading_state_live.db`, `trading_state_mock.db` 등 — `.gitignore` 참고.
- `trading_state.db`는 소형이라 저장소에 포함될 수 있습니다(브랜치·민감도에 따라 회사 정책에 맞게 조정).

### 5. 설정

- 실행 시 **API·계정 설정 마법사** 또는 대시보드「API·계정 설정」
- 또는 `config.example.py` → `config.py` 수동 복사
- **API 키·토큰**은 회사 정책에 따라 환경변수나 로컬 전용 파일로 분리하세요.

### 6. 배포 산출물

```text
# 운영 zip → GitHub Release (업데이트용)
py -3 -u tools/publish_production_release.py

# Setup.exe → 로컬 dist/ 만 (GitHub 업로드하지 않음)
py -3 -u tools/build_portable_runtime.py
py -3 -u tools/build_installer.py --skip-runtime
```
