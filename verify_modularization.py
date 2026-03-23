import os
import sys

# Add current directory to path
sys.path.append(os.getcwd())

try:
    print("Testing dynamic_backtest import...")
    import dynamic_backtest
    print("SUCCESS: dynamic_backtest imported.")
    
    print("Testing parameter_optimizer import...")
    import parameter_optimizer
    print("SUCCESS: parameter_optimizer imported.")
    
    # Simple instantiation test
    settings = {'total_capital': 10000000}
    print("Testing DynamicBacktester instantiation...")
    tester = dynamic_backtest.DynamicBacktester(settings)
    print("SUCCESS: DynamicBacktester instantiated.")
    
    print("Testing ParameterOptimizer instantiation...")
    optimizer = parameter_optimizer.ParameterOptimizer([], {})
    print("SUCCESS: ParameterOptimizer instantiated.")
    
    print("\n--- ALL MODULARIZATION TESTS PASSED ---")
except Exception as e:
    print(f"\n--- TEST FAILED ---")
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
