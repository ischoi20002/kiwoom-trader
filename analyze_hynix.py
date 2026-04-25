import pandas as pd
from backtest_engine import SimpleBacktester, BacktestDataLoader
import os

settings = {
    "rsi_min": 35.0,
    "rsi_max": 65.0,
    "vol_surge": 1.2,
    "ma60_min": 95.0,
    "ma60_max": 115.0,
    "use_gc": True,
    "use_rsi_signal": True,
    "rsi_window": 45,
    "use_macd_signal": True,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_sig": 9,
    "rsi_sig_period": 9,
    "flash_exit_pct": 3.0,
    "gap_down_limit": -5.0,
    "stop_loss": 3.0,
    "take_profit": 4.0
}

target_log = "hynix_v36_trades.csv"
if os.path.exists(target_log): os.remove(target_log)

loader = BacktestDataLoader()
code = "000660"
start_date = "2025-01-01"
end_date = "2026-03-10"

data = loader.get_data(code, start_date, end_date, interval='15m')

# Monkey patch log_trade to write to specific file
tester = SimpleBacktester(settings)
orig_log = tester.run # We can't easily monkey patch the inner function, 
# so we'll just let it write to backtest_trades.csv and copy it

if os.path.exists("backtest_trades.csv"): os.remove("backtest_trades.csv")
df, summary = tester.run(data, code, market_df=None, log_to_csv=True)
print(f"Summary: {summary}")

if os.path.exists("backtest_trades.csv"):
    os.rename("backtest_trades.csv", target_log)
    print(f"Trade log saved to {target_log}")
