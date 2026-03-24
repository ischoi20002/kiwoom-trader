import sys
import traceback
import pandas as pd
from parameter_optimizer import ParameterOptimizer
import json

def run_test():
    try:
        # Load default settings
        with open('settings.json', 'r', encoding='utf-8') as f:
            settings = json.load(f)
            
        print("====== 1. Preparing Search Space ======")
        space = {
            'buy_rsi_min': [20.0, 30.0],
            'buy_rsi_max_b': [60.0, 70.0],
            'vol_surge_b': [1.0, 2.0],
            's1_a_take_profit': [3.0, 5.0]
        }
        
        sample_codes = ['005930', '000660'] # Only 2 stocks for quick check
        print("====== 2. Initializing Optimizer ======")
        opt = ParameterOptimizer(sample_codes, space, settings)
        
        print("====== 3. Running Optimization (10 Trials, HighPrec) ======")
        res = opt.find_best_params("2025-05-01", "2025-08-01", interval='15m', 
                                   force_refresh=False, high_precision=True, 
                                   method='bayesian', n_trials=10, seed=42)
                                   
        print("====== 4. Optimization Completed ======")
        if res is not None:
            print(f"Result DataFrame Shape: {res.shape}")
            print(res.head(2))
        else:
            print("Result is None.")
            
    except Exception as e:
        print("\n" + "="*50)
        print(f"🔥🔥 ERROR CAPTURED 🔥🔥")
        print("="*50)
        traceback.print_exc()

if __name__ == '__main__':
    run_test()
