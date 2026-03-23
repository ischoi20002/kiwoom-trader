# Walkthrough - Recovery and Logic Restoration

Successfully recovered the core GUI from a backup and restored advanced features added post-backup.

## Key Accomplishments

### 1. GUI Recovery & Stabilization
- Restored `Get_Price_GUI.py` from a stable backup (2026-03-18).
- Fixed encoding issues and redundant escape characters in f-strings.
- Re-implemented the Type A/B split RSI logic and summarized monitoring labels.
- Verified syntax with `py_compile`.

### 2. Performance Optimizations
- **Harvester Optimization**: Re-enabled incremental sync and simplified the completeness logic. All existing stocks are now marked as `COMPLETE` in the database, and the legacy March 4th boundary has been removed to skip redundant historical downloads.
- **Startup Logic**: Removed the expensive `_auto_sync_data` call on startup to ensure instant application availability.
- **Reporting**: Integrated the modern `dynamic_trade_report.html` workflow into the GUI.
- **Strategy UI Reorganization**: Refactored the Strategy Settings tab to group conditions by trading phase:
    - **Stage 1: Primary Entry**: Type A, Type B, and Common requirements.
    - **Stage 2: Add-on Buy**: Confirmation logic for secondary entries.
    - **Exit & Money Management**: Consolidated TP/SL and capital allocation settings.
- **UI Logic & Dependencies**: 
    - Implemented a **toggle mechanism** where "Type A/B Activation" enables/disables sub-conditions (Golden Cross, Volume Surge, etc.).
    - Added missing **Type B parameters** (`vol_surge_b`, `use_macd_gc_b`, etc.) to the UI and synchronized them with the strategy engine.
    - Integrated **"Common" filters** (Volume Spike, Market Filter, etc.) across both the GUI and the backtest engine.
    - **Label Correction**: Renamed the Volume Spike label from "전일비" (Previous Day) to **"평균 대비" (Average)** to correctly reflect the underlying logic (comparison vs. 20-bar Simple Moving Average).
    - **Filter Exemption & UI Relocation**: 
        - Exempted **Type B** from the "RSI 50 상향돌파" filter.
        - Relocated this setting from the **Common section to the Type A section**, as it now exclusively serves as a secondary confirmation for pullbacks.
    - **Stage 1 (Entry) Exit Split (A/B)**: Implemented dual-column parameters for Type A and Type B scouting positions.
    - **Stage 2 Dynamic Scale-out (Scale-out)**:
        - **Precision Harvesting**: Implemented **RSI Overheat Alert** (default 75). Once triggered, the engine monitors for a **user-configurable price drop** (default 1.0%) from peak before selling **50% of the position**.
        - **Partial Sell Support**: Refactored `_execute_sell` to handle fractional liquidations. Partial sells are recorded as `PARTIAL_SELL` in history and `P_SELL` in CSV reports.
    - **Money Management GUI**: 
        - **Full Control**: Exposed **Total Capital (총 운용 자산)** and **Max Monitoring Slots (최대 감시 종목 수)** in the settings tab. 
        - **Budget Synchronization**: These settings directly feed into the `DynamicBacktester` to control overall script load and portfolio concentration.
- **Engine Stability Fixes**:
    - **Datetime Conversion**: Fixed `AttributeError: 'Index' object has no attribute 'time'` by ensuring all batch-loaded data- [x] **고정밀 백테스트 데이터 정합성 강화**: `DatetimeIndex` 변환 및 `Code` 키 매칭 버그 수정.
- [x] **현금 비중 및 자금 운용 문제 해결**: 'Ghost Filters' 제거 및 기본값 완화 (수렴도 7%, 거래대금 10억, 이격도 20%).
- [x] **2단계 부분 익절 로직 정상화**: 'RSI Alert Mode' 도입으로 분할 매도 집행 성공.
- [x] **성과 지표 및 리포트 정확도 개선**: 부분 익절을 통계에 포함하고, 누적 수익금을 **실현손익 방식**으로 변경함. 또한 리포트 헤더를 **Gross Profit, Total Fees(-), Net Profit(강조)** 세 단계로 구분하여 수익 구조를 명확히 함.
- [x] **리포트 외부 차트 연동**: '인터랙티브 분석 차트' 버튼을 **'네이버 증권 차트'**로 변경하고, 클릭 시 해당 종목의 네이버 금융 상세 페이지로 직접 연결되도록 개선함.
- [x] **1차 매수(S1) 전용 일봉 추세 필터 도입**: Type A(눌림목) 진입 시 **일봉 기준 5/20 골든크로스(정배열)** 상태를 필수 조건으로 추가하여 하락 추세에서의 역추세 진입 위험을 낮춤. GUI 설정 탭에도 관련 안내 문구를 추가함.
- [x] **1차 매수(S1) 생존율 최적화 (재복구)**: 파일 복구 과정에서 누락되었던 **RSI 진입 하한선(40.0)** 및 **Type A 상한선(52.0)**을 다시 강제 적용함. 또한 `Get_Price_GUI.py`의 기본 손절폭을 **4%**로 다시 상향 조정하여 최적의 상태를 유지함.
Fixed `KeyError: 'Code'` by restoring the logic that capitalizes database columns, ensuring compatibility between SQLite results and the engine's grouping logic.
        - **Trend Follow (Remainder)**: The remaining 50% is managed via **Flash Exit (S2)**, **RSI Trend Exit** (default 50), and **MA20 Break**.
- **RSI Signal Correction**:
    - Removed the **hidden RSI lower bound (40.0)** from the real-time entry logic.
    - Signals now trigger solely based on the **Upper Bound (상한선)** visible in the GUI (e.g., RSI <= 58 for Type A).
    - This correction allows the strategy to capture deep pullbacks (RSI 20, 30, etc.) that were previously blocked by the selection filter's floor.
- **Volume Surge Refinement**:
    - Changed the logic from "20-bar average" to **"Previous Day Same Time Volume"**.
    - Signals now trigger when current volume exceeds **1.5x** of the same time slot from the previous trading day.
    - This provides a more accurate reflection of relative supply/demand spikes within the intra-day cycle.
- **Bug Fixes**:
    - Resolved a **`KeyError: 'use_vol_surge'`** that occurred when clicking the "Factory Reset" button in the Strategy Settings tab.
    - Hardened the reset logic to use a safe `.get()` method, preventing future crashes if new settings are added.

### 3. Feature Restoration & UI Refinement
- Added the **[🚀 포트폴리오 백테스트]** button for high-precision validation.
- **UI Reorganization**:
    - **Group 1**: Optimization + Result Reports (Signal/Trade Reports).
    - **Group 2**: Portfolio Backtest (Single Run).
    - **Group 3**: On-demand Chart Viewer.
- Connected `DynamicBacktester` to automatically open `dynamic_trade_report.html`.

---

## Verification Results

### Backtest Reporting
- [x] "Portfolio Backtest" button is visible and active.
- [x] Clicking the button triggers the simulation thread.
- [x] Status label updates progress (e.g., "데이터 스캔 및 매매 시뮬레이션 중...").
- [x] `dynamic_trade_report.html` is generated and opened in the browser.

### Harvesting Efficiency

---

## Phase 1: Modularization Completed (2026-03-20)

Successfully refactored the backtesting engine by extracting core functionalities into separate modules.

### Key Changes:
- **Reporting Package**: Created `reporting/` with `signal_report.py`, `trade_report.py`, and `charts.py` for all visualization logic.
- **Strategy Package**: Moved `BacktestDataLoader` to `strategy/data_loader.py` to decouple data ingestion from the engine core.
- **Utils Package**: Centralized `normalize_code` in `utils/normalization.py`, ensuring consistent stock code handling across all modules.
- **Engine Cleanup**: Reduced `backtest_engine.py` from ~2150 to ~1680 lines. Updated it to import modularized components.
- **Stability**: Fixed corruption in the `find_best_params` method and ensured robust reporting integration with absolute paths.

### Verification:
- All new packages are functional and correctly imported.
- `find_best_params` correctly triggers final backtests and generates all reports.
- `data_manager_v2.py` successfully uses the centralized normalization utility.
