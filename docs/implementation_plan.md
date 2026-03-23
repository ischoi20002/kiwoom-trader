# Implementation Plan - Phase 1 Modularization

Refactor `backtest_engine.py` to improve maintainability by extracting reporting, data loading, and utility logic into dedicated packages.

## Proposed Changes

### [New Package] Reporting (`reporting/`)
Extract all HTML report generation and chart visualization logic.
- **[NEW] `reporting/trade_report.py`**: 
    - Move `generate_trade_report` function and its large HTML template string.
- **[NEW] `reporting/signal_report.py`**:
    - Move `generate_signal_report` function and its HTML template.
- **[NEW] `reporting/charts.py`**:
    - Move `generate_stock_market_chart` and `generate_single_backtest_chart` (Plotly logic).

### [New Package] Strategy/Data (`strategy/`)
Extract core data loading and shared strategy components.
- **[NEW] `strategy/data_loader.py`**:
    - Move `BacktestDataLoader` class.
- **[NEW] `strategy/indicators.py`**:
    - (Future) Move `TradingStrategy` indicator calculations.

### [New Package] Utilities (`utils/`)
- **[NEW] `utils/normalization.py`**:
    - Move `normalize_code` helper.

### [Component] Backtest Engine
#### [MODIFY] [backtest_engine.py](file:///c:/바이브/backtest_engine.py)
- Import extracted modules and update internal calls.
- Remove redundant code blocks to reduce file size from ~2600 lines to ~1000 lines.

## Verification Plan

### Automated Tests
- Run `python -m py_compile` on all new and modified files.
- Execute a sample backtest to ensure the report generation still works identically.
- Verify that `dynamic_trade_report.html` is generated correctly.

### Manual Verification
- Confirm that the `Naver Finance` link in the report still functions.
- Ensure the Plotly charts in the report are still interactive.
