# 🎯 종목 선정 및 감시 등록 로직 (Selection Logic)

본 문서는 K-Trader의 핵심 기능 중 하나인 종목 선정(Stock Selection) 프로세스의 두 가지 운영 모드 및 기술적 차이점에 대해 기술합니다.

## 1. 운영 모드 비교 (Operational Modes)

| 구분 | 수동 모드 (GUI Button) | 원격 모드 (Telegram /select) |
| :--- | :--- | :--- |
| **트리거** | 대시보드 "종목 선정 실행" 버튼 | 텔레그램 `/select` 명령어 |
| **확인 절차** | **팝업창(Popup) 확인 필수** | **자동 확정 (Auto-Confirm)** |
| **사용자 개입** | 최종 선정 리스트를 보고 직접 선택 가능 | 시스템이 상위 N개 종목을 즉시 등록 |
| **결과 피드백** | GUI 화면 로그 및 팝업 표시 | 텔레그램 메시지로 선정 결과 전송 |
| **주요 용도** | 장 시작 전 정밀 검토 및 수동 개입 | 외부 활동 중 원격 유니버스 갱신 |

---

## 2. 프로세스 상세 (Process Flow)

### 📂 공통 분석 단계
- `strategy_selector.select_monitoring_targets` 호출
- KOSPI 200 유니버스 로드 (CSV 또는 FDR)
- 10개 스레드를 통한 병렬 분석 (거래대금, 이평수렴, RSI, 거래량 분출)
- `get_strategy_score`를 통한 최종 점수 산정 및 정렬

### 💻 수동 선정 (Manual Path)
1. **Thread**: 별도의 Background Thread에서 분석 수행 (UI 프리징 방지)
2. **Display**: 분석 완료 시 `_display_selected_targets` 호출하여 팝업 생성
3. **Confirm**: 사용자가 [확정] 클릭 시 `TradingController.set_monitor_targets` 호출
4. **Sync**: `MonitorTab`의 워치리스트 갱신

### 📱 원격 선정 (Remote Path)
1. **Polling**: `RemoteCommander`가 DB의 원격 명령 테이블에서 `/select` 감지
2. **Auto-Confirm**: `auto_confirm=True` 옵션과 함께 선정 프로세스 실행
3. **Bypass**: GUI 팝업 생성을 건너뛰고 `TradingController`에 즉시 데이터 등록
4. **Notify**: 선정된 상위 5개 종목 정보를 포함한 텔레그램 메시지 발송

---

## 3. 기술적 주의사항 (Technical Notes)

> [!IMPORTANT]
> **상태 동기화**: 두 모드 모두 최종적으로 `TradingController`를 통해 종목을 등록하므로, GUI 내부 상태(`self.monitor_codes`, `self.monitor_info`)와 엔진 상태가 항상 동일하게 유지되어야 합니다.

> [!TIP]
> **데이터 캐잉**: 선정 결과는 `selected_targets.csv`에 자동 저장됩니다. 이는 프로그램 재시작 시 `TradingController`가 마지막 선정 리스트를 복구하는 기준 데이터로 활용됩니다.
