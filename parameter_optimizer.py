import pandas as pd
import os
import itertools
from datetime import datetime, timedelta
import numpy as np
from concurrent.futures import ThreadPoolExecutor as TPE, ProcessPoolExecutor as PPE, as_completed
import backtest_engine as bt
import random
try:
    import optuna
    HAS_OPTUNA = True
except ImportError:
    HAS_OPTUNA = False
from backtest_engine import SimpleBacktester, TradingStrategy, SIGNAL_CSV, TRADE_CSV, BASE_DIR
from strategy.data_loader import BacktestDataLoader
from reporting.signal_report import generate_signal_report
import reporting.trade_report
import importlib
importlib.reload(reporting.trade_report)
from reporting.trade_report import generate_trade_report

# [v23.0] 멀티프로세싱을 위해 최상위 레벨로 이동된 실행 함수
def run_single_combination(args):
    """단일 파라미터 조합에 대해 모든 종목의 백테스트를 수행 (별도 프로세스에서 실행됨)"""
    idx, settings, data_cache, data_cache_m1, market_df, vix_df, is_indicator_constant, high_precision = args
    
    summaries = []
    for code, data in data_cache.items():
        if data is not None:
            # [v23.0] 가벼운 시뮬레이터를 사용하여 처리 (내부 쓰레드 제거로 오버헤드 최소화)
            local_tester = SimpleBacktester(settings)
            m1_pre = data_cache_m1.get(code)
            try:
                summary = local_tester.run(data, code, market_df=market_df, vix_df=vix_df, log_to_csv=False, 
                                      skip_indicators=is_indicator_constant, high_precision=high_precision, m1_df_preloaded=m1_pre)
                if summary:
                    summaries.append(summary)
            except Exception as e: 
                print(f"  (!) Process Run Error ({code}): {e}")
                
    if summaries:
        df_sum = pd.DataFrame(summaries)
        result = settings.copy()
        result.update({
            'avg_ret': df_sum['final_ret'].mean(), 'avg_mdd': df_sum['mdd'].mean(), 
            'avg_win': df_sum['win_rate'].mean(), 'total_trades': df_sum['trades'].sum(),
            'avg_s2': df_sum['s2_trades'].mean()
        })
        return result
    return None

class ParameterOptimizer:
    """자동화된 파라미터 최적화 엔진 (v5.1)"""
    def __init__(self, codes, param_ranges, base_settings=None):
        self.codes = codes
        self.param_ranges = param_ranges # {'rsi_min': [35, 40, 45], ...}
        self.base_settings = base_settings if base_settings else {}
        
    def find_best_params(self, start_date, end_date, interval='1d', callback=None, force_refresh=False, high_precision=False, method='grid', n_trials=30):
        # [v18.8] Date Normalization (YYYY-MM-DD)
        start_date = pd.to_datetime(start_date).strftime('%Y-%m-%d')
        end_date = pd.to_datetime(end_date).strftime('%Y-%m-%d')
        
        # 신호 기록 초기화 (v6.3)
        for f in [SIGNAL_CSV, TRADE_CSV]:
            if os.path.exists(f): 
                try: os.remove(f)
                except:
                    try: open(f, 'w').close()
                    except: pass

        # 1. 파라미터 조합 생성 전략 결정 (v30.0)
        all_combinations = []
        keys = list(self.param_ranges.keys())
        values = list(self.param_ranges.values())
        
        if method == 'grid':
            for v in itertools.product(*values):
                comb = self.base_settings.copy()
                comb.update(dict(zip(keys, v)))
                all_combinations.append(comb)
            total_comb = len(all_combinations)
            print(f"\nOptimization Started (Grid): {total_comb} combinations to test ({interval}).")
        elif method == 'random':
            print(f"\nOptimization Started (Random): {n_trials} trials to test ({interval}).")
            for _ in range(n_trials):
                comb = self.base_settings.copy()
                for k, v_list in self.param_ranges.items():
                    comb[k] = random.choice(v_list)
                all_combinations.append(comb)
            total_comb = n_trials
        elif method == 'bayesian' and HAS_OPTUNA:
            print(f"\nOptimization Started (Bayesian/Optuna): {n_trials} trials to test ({interval}).")
            # Bayesian은 별도 루프(study.optimize)에서 처리하므로 여기서 combinations를 채우지 않음
            total_comb = n_trials
        else:
            # Fallback to random if optuna not available but bayesian requested
            method = 'random'
            print(f"\nOptuna not available or invalid method. Falling back to Random: {n_trials} trials.")
            for _ in range(n_trials):
                comb = self.base_settings.copy()
                for k, v_list in self.param_ranges.items():
                    comb[k] = random.choice(v_list)
                all_combinations.append(comb)
            total_comb = n_trials
        
        all_results = []
        total_stocks = len(self.codes)
        
        loader = BacktestDataLoader()
        data_cache = {}
        total_finished = 0
        
        market_cache_path = os.path.join(loader.cache_dir, "market_master.csv")
        vix_cache_path = os.path.join(loader.cache_dir, "vix_master.csv")
        
        market_df, vix_df = None, None
        w_start_dt = (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=200)).strftime("%Y-%m-%d")
        
        def load_master_index(path, symbol, start, end):
            df_m = None
            if os.path.exists(path):
                try:
                    df_m = pd.read_csv(path, index_col=0, parse_dates=True)
                    if not df_m.empty:
                        c_start, c_end = df_m.index.min(), df_m.index.max()
                        if c_start.strftime("%Y-%m-%d") <= start and c_end.strftime("%Y-%m-%d") >= end:
                            print(f"  [Cache] {symbol} index hit! ({c_start.date()} ~ {c_end.date()})")
                            return df_m
                except: pass
            
            print(f"  [API] Fetching {symbol} data...")
            try:
                import FinanceDataReader as fdr
                df_new = fdr.DataReader(symbol, start, end)
                if df_new is not None and not df_new.empty:
                    if df_m is not None:
                        df_m = pd.concat([df_m, df_new])
                        df_m = df_m[~df_m.index.duplicated(keep='last')].sort_index()
                    else:
                        df_m = df_new
                    df_m.to_csv(path)
                    return df_m
            except Exception as e:
                print(f"  (!) {symbol} Load Error: {e}")
            return df_m

        try:
            market_df = load_master_index(market_cache_path, 'KS11', w_start_dt, end_date)
            if os.path.exists(vix_cache_path):
                try:
                    vix_df = pd.read_csv(vix_cache_path, index_col=0, parse_dates=True)
                    v_start, v_end = vix_df.index.min(), vix_df.index.max()
                    if v_start.strftime("%Y-%m-%d") <= w_start_dt and v_end.strftime("%Y-%m-%d") >= end_date:
                        print(f"  [Cache] VIX hit! ({v_start.date()} ~ {v_end.date()})")
                    else:
                        vix_df = None
                except: vix_df = None

            if vix_df is None:
                print("  [API] Fetching VIX data...")
                import FinanceDataReader as fdr
                try: vix_df = fdr.DataReader('VIX', w_start_dt, end_date)
                except:
                    try: vix_df = fdr.DataReader('FRED:VIXCLS', w_start_dt, end_date)
                    except: vix_df = pd.DataFrame(index=market_df.index if market_df is not None else [])
                
                if not vix_df.empty:
                    vix_df = vix_df[~vix_df.index.duplicated(keep='last')].sort_index()
                    if 'Close' not in vix_df: 
                        if not vix_df.empty: vix_df.columns = ['Close'] + list(vix_df.columns[1:])
                        else: vix_df['Close'] = np.nan
                    vix_df.to_csv(vix_cache_path)
        except Exception as e:
            print(f"  (!) Market Data Caching Error: {e}")

        self.market_df = market_df
        self.vix_df = vix_df

        data_cache_m1 = {}
        
        with TPE(max_workers=3) as executor:
            def load_with_m1(code):
                nonlocal total_finished
                res_15m = loader.get_data(code, start_date, end_date, interval, force_refresh=force_refresh)
                res_1m = None
                if high_precision:
                    m1_start = res_15m.index.min().strftime('%Y-%m-%d') if res_15m is not None else start_date
                    m1_end = (pd.to_datetime(end_date) + timedelta(days=1)).strftime('%Y-%m-%d')
                    res_1m = loader.get_data(code, m1_start, m1_end, interval='1m', force_refresh=force_refresh)
                
                total_finished += 1
                msg = f"데이터 준비 완료 ({total_finished}/{total_stocks}): {code}"
                if callback: callback(msg, (total_finished / total_stocks) * 50)
                return code, res_15m, res_1m

            results = list(executor.map(load_with_m1, self.codes))
            for code, res_15m, res_1m in results:
                data_cache[code] = res_15m
                if res_1m is not None:
                    data_cache_m1[code] = res_1m

        # [v23.1] 지표 재계산 필요 여부 판단 (보수적 화이트리스트 방식)
        # 아래 파라미터들은 지표(Indicators) 계산에는 영향을 주지 않고, 신호(Signals)나 매매 로직에만 관여함
        signal_only_params = {
            'buy_rsi_min', 'buy_rsi_max_a', 'buy_rsi_max_b', 'vol_surge', 'vol_surge_b',
            's1_a_stop_loss', 's1_b_stop_loss', 's2_stop_loss', 
            's1_a_take_profit', 's1_b_take_profit', 's2_take_profit',
            'take_profit', 'stop_loss', 'gap_down_limit', 's2_min_days', 'stage2_trigger',
            'use_s1_flash_exit', 'use_s2_flash_exit', 'use_s1_stop_loss', 'use_s2_stop_loss',
            's1_a_flash_exit_pct', 's1_b_flash_exit_pct', 's2_flash_exit_pct',
            'use_s2_ma5', 'use_s2_profit', 'use_s2_osc', 'gc_window', 'rsi_window'
        }
        
        # 최적화 대상 파라미터 중 하나라도 지표에 영향을 줄 가능성이 있으면 False
        is_indicator_constant = all(k in signal_only_params for k in keys)

        if is_indicator_constant:
            print("  [Perf] Technical indicators are constant across combinations. Pre-calculating...")
            for code, data in data_cache.items():
                if data is not None:
                    data_cache[code] = TradingStrategy.calculate_indicators(data, self.base_settings)
        else:
            print("  [Perf] Parameters affect indicators. Recalculating indicators for each trial.")

        # [v23.0] 최적화: 모든 종목 데이터와 지수 데이터를 인자로 준비
        # (주의: Windows에서는 피클링 오버헤드가 있으나, 시뮬레이션 연산이 이를 상쇄함)
        common_args = (data_cache, data_cache_m1, market_df, vix_df, is_indicator_constant, high_precision)
        
        def prepare_args(idx_settings):
            idx, settings = idx_settings
            return (idx, settings) + common_args

        # 3. 실제 최적화 수행 (v23.0 Optimized: ProcessPoolExecutor 도입)
        
        if method == 'bayesian' and HAS_OPTUNA:
            # Optuna는 자체적으로 병렬화가 가능하지만, 여기서는 기존 run_comb 구조를 유지하며 최적화
            def objective(trial):
                settings = self.base_settings.copy()
                for k, v_list in self.param_ranges.items():
                    if len(v_list) >= 2 and all(isinstance(x, (int, float)) for x in v_list):
                        vmin, vmax = min(v_list), max(v_list)
                        if all(isinstance(x, int) for x in v_list):
                            settings[k] = trial.suggest_int(k, vmin, vmax)
                        else:
                            settings[k] = trial.suggest_float(k, vmin, vmax)
                    else:
                        settings[k] = trial.suggest_categorical(k, v_list)
                
                res = run_single_combination(prepare_args((trial.number, settings)))
                if res:
                    all_results.append(res)
                    if callback: callback(f"베이지안 최적화 중 ({trial.number+1}/{n_trials})", 50 + (trial.number/n_trials)*50)
                    return res['avg_ret']
                return -999.0

            study = optuna.create_study(direction='maximize')
            study.optimize(objective, n_trials=n_trials)
        else:
            # [v23.0] 일반 Grid Search의 경우 모든 코어 동원
            num_workers = min(os.cpu_count() or 4, 12)
            print(f"  [Perf] Starting Multiprocessing (PPE) with {num_workers} processes...")
            
            if callback: callback(f"멀티프로세싱 최적화 시작 (코어: {num_workers})...", 50)
            
            with PPE(max_workers=num_workers) as p_executor:
                # [v23.0] 최상위 레벨 함수인 run_single_combination 사용
                tasks = [prepare_args((i, comb)) for i, comb in enumerate(all_combinations)]
                all_results = list(p_executor.map(run_single_combination, tasks))
                all_results = [r for r in all_results if r is not None]

        if all_results:
            df_results = pd.DataFrame(all_results).sort_values(by='avg_ret', ascending=False)
            df_results.to_csv("optimization_results.csv", index=False, encoding='utf-8-sig')
            
            if not df_results.empty:
                best = df_results.iloc[0]
                best_settings = self.base_settings.copy()
                for k in keys:
                    val = best[k]
                    if k in ['vol_lookback', 'macd_fast', 'macd_slow', 'macd_sig']:
                        try: val = int(float(val))
                        except: pass
                    best_settings[k] = val
                
                self.best_settings = best_settings
                
                for f in [SIGNAL_CSV, TRADE_CSV]:
                    if os.path.exists(f): 
                        try: os.remove(f)
                        except:
                            try: open(f, 'w').close()
                            except: pass
                
                def _run_final(code_data):
                    code, data = code_data
                    if data is not None:
                        local_final_tester = SimpleBacktester(best_settings)
                        m1_pre = data_cache_m1.get(code)
                        local_final_tester.run(data, code, market_df=self.market_df, vix_df=self.vix_df, log_to_csv=True,
                                               high_precision=high_precision, m1_df_preloaded=m1_pre)

                with TPE(max_workers=4) as executor:
                    futures = {executor.submit(_run_final, item): item[0] for item in data_cache.items()}
                    finished_pass = 0
                    total_pass = len(data_cache)
                    for future in as_completed(futures):
                        finished_pass += 1
                        msg = f"최종 백테스트 수행 중 ({finished_pass}/{total_pass})..."
                        if callback: callback(msg, 50 + (finished_pass/total_pass)*10)
                        try: future.result()
                        except: pass
            
            sig_path = os.path.join(BASE_DIR, "signal_report.html")
            trd_path = os.path.join(BASE_DIR, "trade_report.html")
            
            generate_signal_report(output_path=sig_path, target_codes=self.codes)
            trades_df = pd.DataFrame()
            total_trades_count, win_rate_calc, avg_ret_calc = 0, 0, 0
            
            if os.path.exists(TRADE_CSV):
                try:
                    trades_df = pd.read_csv(TRADE_CSV, dtype={'Code': str})
                    if not trades_df.empty:
                        trades_df = trades_df[trades_df['Date'] != 'Date'].copy()
                        trades_df['Date'] = pd.to_datetime(trades_df['Date'], errors='coerce')
                        trades_df = trades_df.dropna(subset=['Date'])
                        
                        sells = trades_df[trades_df['Action'].isin(['SELL', 'HOLD', 'PARTIAL_SELL'])]
                        total_trades_count = len(sells)
                        win_rate_calc = (len(sells[sells['Return'] > 0]) / total_trades_count * 100) if total_trades_count > 0 else 0
                        
                        if total_trades_count > 0 and 'Amount' in sells.columns and sells['Amount'].sum() > 0:
                            avg_ret_calc = (sells['Return'] * sells['Amount']).sum() / sells['Amount'].sum()
                        else:
                            avg_ret_calc = sells['Return'].mean() if total_trades_count > 0 else 0
                except: pass

            generate_trade_report(trades_df, interval, trd_path, total_trades_count, 
                                  win_rate_calc, avg_ret_calc, best_settings, market_df=self.market_df, vix_df=self.vix_df, 
                                  callback=callback, target_codes=self.codes, daily_equity=None, high_precision=high_precision)

            return df_results
        return None
