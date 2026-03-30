# 🗺️ Vibe 프로젝트 아키텍처 지형도

본 문서는 Vibe 자동매매 시스템의 전체 모듈 구성과 데이터 흐름을 정의합니다. 안티그래비티는 새로운 기능을 추가하거나 기존 로직을 수정할 때 이 지형도를 참조하여 중복 개발을 방지하고 계층 구조를 준수해야 합니다.

## 1. 모듈별 역할 정의 (Core Modules)

| 모듈명 | 역할 (Role) | 주요 파일 |
| :--- | :--- | :--- |
| **Data Layer** | 데이터 로딩 및 DB 관리 (SQLite) | `data_manager_v2.py` |
| **Strategy Layer** | 매수/매도 시그널 및 기술적 지표 산출 | `strategy/signals.py`, `strategy/simulation_models.py` |
| **Service Layer** | **비즈니스 로직 및 상태 관리 (Master)** | `core/portfolio_manager.py`, `core/trading_controller.py` |
| **Scout Layer** | 일봉 기반 종목 스크리닝 및 점수화 | `strategy_selector.py` |
| **Execution Layer** | 백테스팅 엔진 및 실시간 매매 시뮤레이션 | `dynamic_backtest.py`, `backtest_engine.py` |
| **UI Layer (View)** | **순수 뷰 (Thin GUI Layer)** | `k_trader_gui.py`, `ui/tabs/*` |

## 2. 데이터 파이프라인 (Data Pipeline v4.5)

1.  **Ingestion**: `data_manager_v2.py`가 데이터를 관리.
2.  **Selection (Scout)**: `strategy_selector.py`가 상위 종목 선정.
3.  **Signal Generation**: `SignalEngine`이 실시간 데이터를 받아 진입/청산 시그널을 `TradingController`에 전달.
4.  **Execution (Controller)**: `TradingController`에서 주문 수량을 계산하고 체결 API를 호출.
5.  **State Logic (Portfolio)**: `PortfolioManager`가 체결 데이터를 수신하여 계좌 상태와 포지션을 실시간으로 갱신.
6.  **Reporting**: 매매 결과가 발생하면 `trade_report.py`나 `virtual_report.py`가 리포트 생성.

## 3. 상태 관리 철학 (State Management)
본 프로젝트의 상태(Position, History, Balance)는 오직 **`PortfolioManager`**만이 소유하며 관리합니다.
- **Single Source of Truth**: 모든 UI 컴포넌트(`VirtualTab`, `MonitorTab`)는 직접 상태를 보유하지 않고 `app.portfolio`를 통해 데이터에 접근해야 합니다.
- **Hardening**: GUI 클래스에서 임의로 `virtual_positions` 등의 변수를 생성하여 사용하는 것을 엄격히 금지합니다.

## 4. 안티그래비티 준수 사항 (Developer Mantra)
- **중복 금지**: 새로운 지표 계산이 필요할 경우 공용 라이브러리 함수가 있는지 반드시 확인하라.
- **GUI는 View일 뿐이다**: 비즈니스 로직(Execution Flow, Risk Management)은 반드시 `core/` 서비스 레이어에 위치시켜라.
- **상태 고립**: UI 컴포넌트 간 통신이 필요한 경우 반드시 `PortfolioManager`나 `TradingController`를 중재자로 사용하여 결합도(Coupling)를 낮추어라.
- **동기화**: `dynamic_backtest.py`의 시뮬레이션 로직은 실전 매매 로직과 100% 일치해야 한다.
