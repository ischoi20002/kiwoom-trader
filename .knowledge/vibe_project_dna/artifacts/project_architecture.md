# 🗺️ Vibe 프로젝트 아키텍처 지형도

본 문서는 Vibe 자동매매 시스템의 전체 모듈 구성과 데이터 흐름을 정의합니다. 안티그래비티는 새로운 기능을 추가하거나 기존 로직을 수정할 때 이 지형도를 참조하여 중복 개발을 방지하고 계층 구조를 준수해야 합니다.

## 1. 모듈별 역할 정의 (Core Modules)

| 모듈명 | 역할 (Role) | 주요 파일 |
| :--- | :--- | :--- |
| **Data Layer** | 데이터 로딩 및 DB 관리 (SQLite) | `data_manager_v2.py` |
| **Strategy Layer** | 매수/매도 시그널 및 기술적 지표 산출 | `strategy/signals.py`, `strategy/simulation_models.py` |
| **Scout Layer** | 일봉 기반 종목 스크리닝 및 점수화 | `strategy_selector.py` |
| **Execution Layer** | 백테스팅 엔진 및 실시간 매매 시뮬레이션 | `dynamic_backtest.py`, `backtest_engine.py` |
| **UI/Reporting** | GUI 환경 및 매매 결과 리포트 생성 | `k_trader_gui.py`, `reporting/trade_report.py` |

## 2. 데이터 파이프라인 (Data Flow)

1.  **Ingestion**: `data_manager_v2.py`가 증권사 API 또는 원격 서버에서 데이터를 가져와 로컬 DB(`trading_data.db`)에 저장.
2.  **Selection (Scout)**: `strategy_selector.py`가 DB에서 일봉 데이터를 읽어 RSI, MA 수렴도 등을 기준으로 상위 N개 종목 선정.
3.  **Simulation/Execution**: `dynamic_backtest.py`가 선정된 종목에 대해 15분봉/1분봉 정밀 시뮬레이션 수행. (`signals.py`의 로직 준수)
4.  **Reporting**: 매매 기록이 발생하면 `trade_report.py`가 HTML 형태의 가독성 높은 결과 리포트 생성.

## 3. 안티그래비티 준수 사항 (Developer Mantra)
- **중복 금지**: 새로운 지표 계산이 필요할 경우 `strategy_selector.py`나 `signals.py`에 이미 존재하는 함수가 있는지 반드시 확인하라.
- **의존성 유지**: UI 레이어(`gui/`)에서 직접 DB 쿼리를 날리지 말고, 가급적 `DataManager`를 통해 데이터를 요청하라.
- **동기화**: `dynamic_backtest.py`의 로직은 실시간 매매(`k_trader_gui.py`)와 100% 동기화되어야 함을 명심하라.
