import os

paths = [
    r"C:\바이브\trade_report.html",
    r"C:\바이브\signal_report.html",
    r"C:\바이브\backtest_trades.csv",
    r"C:\바이브\backtest_signals.csv"
]

for p in paths:
    if os.path.exists(p):
        print(f"FOUND: {p} (Size: {os.path.getsize(p)} bytes)")
    else:
        print(f"MISSING: {p}")

print(f"CWD: {os.getcwd()}")
print(f"Script Dir: {os.path.dirname(os.path.abspath(__file__))}")
