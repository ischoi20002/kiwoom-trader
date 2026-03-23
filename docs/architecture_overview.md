# Architecture Overview - Modularized Backtest Engine

This document provides a high-level overview of the modularized architecture of the backtesting system.

## 1. System Diagram

```mermaid
graph TD
    subgraph "Main Application (GUI)"
        GUI["Get_Price_GUI.py"]
    end

    subgraph "Core Engine Layer"
        Engine["backtest_engine.py<br/>(Backtest Engine)"]
        DataManager["data_manager_v2.py<br/>(Data Sync & DB)"]
        Strategy["strategy_selector.py<br/>(Strategy Definition)"]
    end

    subgraph "Modularized Packages"
        subgraph "reporting/"
            SignalRep["signal_report.py"]
            TradeRep["trade_report.py"]
            Charts["charts.py<br/>(Plotly Visualization)"]
        end

        subgraph "strategy/"
            DataLoader["data_loader.py<br/>(BacktestDataLoader)"]
        end

        subgraph "utils/"
            Norm["normalization.py<br/>(normalize_code)"]
        end
    end

    %% Relationships
    GUI --> Engine
    GUI --> DataManager
    
    Engine --> Strategy
    Engine --> DataManager
    Engine --> DataLoader
    Engine --> SignalRep
    Engine --> TradeRep
    Engine --> Charts
    Engine --> Norm

    DataManager --> Norm
    SignalRep --> Norm
    TradeRep --> Norm
    Charts --> Norm
```

## 2. Component Descriptions

### Main Application (GUI)
- **Get_Price_GUI.py**: The entry point of the application. Handles user input, parameter loading, and triggers both data synchronization and backtesting.

### Core Engine Layer
- **backtest_engine.py**: The central logic for the backtest. Manages the simulation loop, parameter optimization, and multi-threaded execution.
- **data_manager_v2.py**: Manages database connections, data harvesting (Kiwoom API), and caching.
- **strategy_selector.py**: Contains strategy metadata, default parameters, and the `TradingStrategy` class for signal calculation logic.

### Modularized Packages
- **reporting/**: Handles all output generation. Segregated from the engine to allow for independent updates to visualization and reporting styles.
- **strategy/**: Houses reusable strategy components. `BacktestDataLoader` encapsulates the complexity of fetching and merging 15-minute and 1-minute data.
- **utils/**: Project-wide utility functions. `normalization.py` ensures all stock codes are formatted consistently throughout the app.
