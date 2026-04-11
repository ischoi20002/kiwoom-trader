# 변경 이력

`core/version.py`의 `APP_VERSION`과 맞춥니다.  
버전 숫자를 올릴 때 **같은 작업**에서 여기에 해당 버전 섹션을 추가합니다.

---

## 0.1.1

- `CHANGELOG.md` 신설: 이후 버전 갱신 시 수정 요약을 누적 기록
- `core/version.py` 관례에「버전 변경 시 본 파일 반영」문구 보강

## 0.1.0

- 앱 표시 버전을 `0.N.M` 체계로 정리 (`core/version.py`의 `APP_VERSION`, 메인 창 제목 연동)
- 실전 반영 전 선행 `0`·`N`/`M` 증가 규칙을 `core/version.py` 주석으로 명시
