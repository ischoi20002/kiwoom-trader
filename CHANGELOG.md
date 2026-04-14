# 변경 이력

`core/version.py`의 `APP_VERSION`과 맞춥니다.  
버전 숫자를 올릴 때 **같은 작업**에서 여기에 해당 버전 섹션을 추가합니다.

---

## 0.2.1 (2026-04-14)

- 예수금: 출금가능금액(pymn) 기준 표시, 주문가능·D+2와 분리; `get_withdrawable_deposit()` 추가
- S1 매수: `invest_amt < 1주 가격`일 때 `max(1,…)`로 한도 초과 매수하던 문제 제거 (D+2 미수 방지)
- 실시간 감시 ATR 기준가: NaN·`ATR미사용` 보강(ADX 실패 시에도 ATR 계산, TR 근사 폴백, Type B·sel_track·워밍업 시계열 연동)
- 기타: `strategy/indicators` ADX/ATR 분리, 포지션 동기화 시 `sel_track=B`이면 `sig_type` TYPE_B 등

## 0.2.0 (2026-04-13)

- 가상 매매 종합 리포트: 미청산 보유 종목을 현재가 기준으로 평가, 매도 시 수수료를 반영한 순입금 추정을 종목별·상단 요약에 표시
- 코어·UI·리포팅·수수료/경로 유틸 등 누적 개선 (실시간 감시, 가상 탭, 일일 결산·거래 리포트 등)

## 0.1.1

- `CHANGELOG.md` 신설: 이후 버전 갱신 시 수정 요약을 누적 기록
- `core/version.py` 관례에「버전 변경 시 본 파일 반영」문구 보강

## 0.1.0

- 앱 표시 버전을 `0.N.M` 체계로 정리 (`core/version.py`의 `APP_VERSION`, 메인 창 제목 연동)
- 실전 반영 전 선행 `0`·`N`/`M` 증가 규칙을 `core/version.py` 주석으로 명시
