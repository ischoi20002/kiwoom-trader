from data_manager_v2 import DataManagerV2 

db = DataManagerV2("trading_data.db")
days = db.get_trading_days("2025-04-01", "2025-04-30")
print(f"Trading days found: {len(days)}")
if days:
    print(f"First day: {days[0]}, Last day: {days[-1]}")
else:
    print("No trading days found. The fix might not be working or data is missing.")
