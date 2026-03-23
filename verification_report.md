# Strategy Verification: Market Filter and Signal(9)

## Verification Goal
Confirm if the "Market Index Filter" and "Signal (9)" indicators are consistently applied to both real-time monitoring and backtesting systems.

## 1. Market Index Filter Analysis
- **Status**: ✅ **Fully Integrated**
- **Real-time (`Get_Price_GUI.py`)**: 
  - Monitoring loop (Lines 961-965) fetches real-time market status.
  - If any index is in a `DOWN` trend, the `market_trend` variable is updated.
  - This trend is passed to the strategy engine, which pauses buy signals during downtrends.
- **Backtesting (`backtest_engine.py`)**:
  - `SimpleBacktester.run` (Lines 342-350) calculates the historical 20-day MA of KOSPI.
  - It filters out buy signals on dates where the index closed below its 20-day MA.

## 2. Signal (9) Indicators Analysis
Depending on the specific indicator referenced:

### RSI Signal (9)
- **Status**: ⚠️ **Visual/Logging Only**
- **Unified Engine**: Both systems calculate `RSI_Sig` using a 9-period rolling mean.
- **Usage**:
  - **Real-time**: Displays in the monitoring table and triggers a "Green Highlight" (`rsi_signal` tag) when `RSI > RSI_Sig`.
  - **Backtesting**: Recorded in `backtest_signals.csv` but currently **not** a mandatory condition for the final buy signal (`is_buy`).

### MACD Signal (9)
- **Status**: ✅ **Fully Integrated into Buy Logic**
- **Unified Engine**: Both systems use a 9-period MACD signal.
- **Usage**: The condition `MACD >= MACD_Sig` is a mandatory requirement for the "Integrated Strategy" buy signal in both real-time and backtesting.

## Conclusion
The core engines are highly synchronized (sharing the same `TradingStrategy` code). The Market Filter is active in both, and "Signal (9)" is fully utilized depending on which indicator (MACD vs RSI) is prioritized as a trigger.
