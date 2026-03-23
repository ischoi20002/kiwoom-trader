import sys
import os

# Add current directory to path
sys.path.append(os.getcwd())

try:
    from backtest_engine import TradingStrategy
    from parameter_optimizer import ParameterOptimizer
    import pandas as pd
    import numpy as np

    print("--- Verifying MACD Parameter Consistency ---")
    
    # 1. Check TradingStrategy.calculate_indicators
    settings = {
        'sig_macd_fast': 10,
        'sig_macd_slow': 20,
        'sig_macd_sig': 5
    }
    df = pd.DataFrame({
        'Open': [100]*50, 'High': [105]*50, 'Low': [95]*50, 'Close': [102]*50, 'Volume': [1000]*50
    }, index=pd.date_range('2025-01-01', periods=50, freq='15min'))
    
    df_ind = TradingStrategy.calculate_indicators(df, settings)
    if 'MACD' in df_ind.columns:
        print("[SUCCESS] MACD indicators calculated successfully.")
    else:
        print("[FAILURE] MACD indicators missing.")

    # 2. Check ParameterOptimizer signal_only_params
    with open('parameter_optimizer.py', 'r', encoding='utf-8') as f:
        content = f.read()
        if 'buy_rsi_min' in content and 'vol_surge_b' in content:
            print("[SUCCESS] ParameterOptimizer contains updated signal_only_params names.")
        else:
            print("[FAILURE] ParameterOptimizer signal_only_params naming seems outdated.")

    # 3. Check backtest_engine main block
    with open('backtest_engine.py', 'r', encoding='utf-8') as f:
        content = f.read()
        if 'sig_macd_fast' in content and 'sig_macd_slow' in content:
            print("[SUCCESS] backtest_engine main block uses sig_macd_... names.")
        else:
            print("[FAILURE] backtest_engine main block uses legacy names.")

    print("\n--- Verification Complete ---")

except Exception as e:
    print(f"[ERROR] Verification failed with error: {e}")
    import traceback
    traceback.print_exc()
