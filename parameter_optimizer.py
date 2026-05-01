import pandas as pd
import os
import itertools
import math
from collections import Counter
from datetime import datetime, timedelta
import numpy as np
from concurrent.futures import ThreadPoolExecutor as TPE, ProcessPoolExecutor as PPE, as_completed
import backtest_engine as bt
import random
OPTUNA_IMPORT_ERROR: str | None = None
try:
    import optuna

    HAS_OPTUNA = True
    optuna.logging.set_verbosity(optuna.logging.WARNING)
except ImportError as _opt_ie:
    HAS_OPTUNA = False
    OPTUNA_IMPORT_ERROR = str(_opt_ie)
except Exception as _opt_e:
    # 의존 패키지 불일치 등으로 import 자체가 실패할 수 있음
    HAS_OPTUNA = False
    OPTUNA_IMPORT_ERROR = f"{type(_opt_e).__name__}: {_opt_e}"
from backtest_engine import SimpleBacktester, TradingStrategy, SIGNAL_CSV, TRADE_CSV, BASE_DIR
from strategy.data_loader import BacktestDataLoader
import reporting.trade_report
import importlib
importlib.reload(reporting.trade_report)
from reporting.trade_report import generate_trade_report
import pickle


def _finite_optuna_objective_value(val: object) -> float:
    """Optuna에 반환할 목적값은 유한한 float 이어야 한다 (NaN/Inf 시 trial FAIL로 줄어듦)."""
    try:
        x = float(val)
        if math.isfinite(x):
            return x
    except (TypeError, ValueError):
        pass
    return -999.0


# [v30.0] 멀티프로세싱 오버헤드 최적화를 위한 전역 변수 (Windows 피클링 방지)
_G_DATA = {}
_G_M1 = {}
# [Perf] 지표 계산 결과 캐시 (지표에 영향을 주는 파라미터가 같으면 재사용)
_G_IND_CACHE: dict = {}

# 지표 계산에 영향을 주는 파라미터 집합 (calculate_all_indicators 에서 실제로 사용하는 값들)
# - 15분 MACD 진입·RSI 시그널 주기는 strategy.indicator_constants 고정 (설정 키 무시)
# - atr_period / atr_period_a / atr_period_b → ATR / ATR_A / ATR_B
# - vol_lookback             → Prev_Day_Vol
# - prev_vol_lookback_s1_a/b, s2_a/b → Prev_Day_Vol_S* (구 prev_vol_lookback 폴백)
_INDICATOR_PARAMS = frozenset({
    'atr_period', 'atr_period_a', 'atr_period_b',
    'vol_lookback',
    'prev_vol_lookback',
    'prev_vol_lookback_b',
    'prev_vol_lookback_s1_a',
    'prev_vol_lookback_s1_b',
    'prev_vol_lookback_s2_a',
    'prev_vol_lookback_s2_b',
})

# 최적화 시 지표 재계산 스킵 판정용(신호·매매 로직만 영향). _INDICATOR_PARAMS 와 교집합 없어야 함.
SIGNAL_ONLY_PARAMS = frozenset({
    'buy_rsi_min', 'buy_rsi_max_a', 'buy_rsi_max_b', 'vol_surge', 'vol_surge_b',
    's1_a_stop_loss', 's1_b_stop_loss', 's2_stop_loss', 's2_b_stop_loss',
    's1_a_take_profit', 's1_b_take_profit', 's2_take_profit', 's2_b_take_profit',
    'take_profit', 'stop_loss',
    'gap_down_limit', 'gap_down_limit_a', 'gap_down_limit_b',
    'use_gap_down_exit', 'use_gap_down_exit_a', 'use_gap_down_exit_b',
    'use_gap_grace', 'use_gap_grace_a', 'use_gap_grace_b',
    'gap_grace_mins', 'gap_grace_mins_a', 'gap_grace_mins_b',
    'use_atr_exit', 'use_atr_exit_a', 'use_atr_exit_b',
    'use_atr_trail_only_after_tp', 'use_atr_trail_only_after_tp_a', 'use_atr_trail_only_after_tp_b',
    'use_atr_trail_only_after_tp_s1_a', 'use_atr_trail_only_after_tp_s2_a',
    'use_atr_trail_only_after_tp_s1_b', 'use_atr_trail_only_after_tp_s2_b',
    's2_delay_hours', 's2_profit_limit',
    's2_b_delay_hours', 's2_b_profit_limit',
    's2_ma20_gap', 's2_vol_ratio', 's2_rsi_gap', 's2_macd_gap',
    's2_b_ma20_gap', 's2_b_vol_ratio', 's2_b_rsi_gap', 's2_b_macd_gap',
    'use_s1_a_flash_exit', 'use_s1_b_flash_exit', 'use_s2_flash_exit', 'use_s2_b_flash_exit',
    'use_s1_a_stop_loss', 'use_s1_b_stop_loss', 'use_s2_stop_loss', 'use_s2_b_stop_loss',
    'use_s1_a_tp', 'use_s1_b_tp', 'use_s2_tp', 'use_s2_b_tp',
    's1_a_flash_exit_pct', 's1_b_flash_exit_pct', 's2_flash_exit_pct', 's2_b_flash_exit_pct',
    'use_s2_ma5', 'use_s2_profit', 'use_s2_delay', 'gc_window', 'rsi_window',
    'atr_multiplier_sl_a', 'atr_multiplier_sl_b', 'atr_multiplier_sl_s2', 'atr_multiplier_sl_s2_b',
    'atr_multiplier_trail_a', 'atr_multiplier_trail_b', 'atr_multiplier_trail_s2', 'atr_multiplier_trail_s2_b',
    'ma_convergence', 'use_rsi_min',
    's1_equity_pct', 's2_equity_pct', 's1_sizing_unit', 's2_sizing_unit',
    'use_smart_exit', 'smart_exit_ba_ratio', 'smart_exit_vp', 'smart_exit_price_threshold',
    'use_smart_buy_a', 'use_smart_buy_b', 'use_smart_buy_s2', 'use_smart_buy_s2_b',
    'use_s2_master_a', 'use_s2_master_b',
    'smart_buy_ba_ratio', 'smart_buy_vp', 'use_synthetic_realtime',
    'macd_fast_daily', 'macd_slow_daily', 'macd_sig_daily',
    'macd_fast_entry', 'macd_slow_entry', 'macd_sig_entry', 'rsi_sig_period',
    'backtest_initial_capital', 'sel_rsi_min', 'sel_rsi_max',
    'min_trde_val', 'vol_ratio',
    'stage1_amt', 'stage2_amt',
    'ma60_min', 'ma60_max',
    's2_ma5_gap', 'use_s2_ma20', 'use_s2_vol_surge', 'use_s2_rsi_sig', 'use_s2_macd_gc',
    'use_s2_b_ma20', 'use_s2_b_ma5', 'use_s2_b_vol_surge', 'use_s2_b_rsi_sig', 'use_s2_b_macd_gc',
    'use_s2_b_profit', 'use_s2_b_delay',
    'use_atr_stop_loss_s2_b', 'use_atr_trailing_stop_s2_b',
    'use_macd_signal',
    'sel_b_rsi_min', 'sel_b_rsi_max', 'sel_b_min_trde_val',
})

def init_worker(data_path):
    """프로세스 생성 시 데이터를 한 번만 로드하여 메모리에 상주"""
    global _G_DATA, _G_M1, _G_IND_CACHE
    try:
        with open(data_path, 'rb') as f:
            cache = pickle.load(f)
            _G_DATA = cache.get('data', {})
            _G_M1 = cache.get('m1', {})
        _G_IND_CACHE = {}  # 워커마다 독립적인 지표 캐시 초기화
    except Exception as e:
        print(f"  (!) Worker Init Error: {e}")

# [v23.0] 멀티프로세싱을 위해 최상위 레벨로 이동된 실행 함수
def run_single_combination(args):
    """단일 파라미터 조합에 대해 모든 종목의 백테스트를 수행 (별도 프로세스에서 실행됨)"""
    # [v30.0] args에서 거대한 데이터를 제거하고 전역 변수 사용
    idx, settings, is_indicator_constant, high_precision = args
    
    # [v30.0] 워커 프로세스에서 데이터가 로드되지 않은 경우 지연 로딩 (Windows 최적화)
    global _G_DATA, _G_M1, _G_IND_CACHE
    if not _G_DATA:
        tmp_cache_path = os.path.join(BASE_DIR, "bt_opt_cache.tmp")
        if os.path.exists(tmp_cache_path):
            init_worker(tmp_cache_path)
    
    summaries = []
    data_cache = _G_DATA
    data_cache_m1 = _G_M1

    # [Perf] 지표 재계산이 필요한 경우, 같은 지표 파라미터 조합이면 캐시 재사용
    if not is_indicator_constant:
        ind_key = tuple(sorted((k, settings.get(k)) for k in _INDICATOR_PARAMS if k in settings))
        if ind_key not in _G_IND_CACHE:
            # 지표 계산 후 캐시 저장 (원본 데이터 보호를 위해 copy 사용)
            _G_IND_CACHE[ind_key] = {
                code: TradingStrategy.calculate_indicators(data.copy(), settings)
                for code, data in data_cache.items() if data is not None
            }
        effective_data = _G_IND_CACHE[ind_key]
        skip_ind = True  # 이미 지표 계산 완료
    else:
        effective_data = data_cache
        skip_ind = is_indicator_constant

    for code, data in effective_data.items():
        if data is not None:
            # [v23.0] 가벼운 시뮬레이터를 사용하여 처리 (내부 쓰레드 제거로 오버헤드 최소화)
            tester = SimpleBacktester(settings)
            m1_pre = data_cache_m1.get(code) if data_cache_m1 else None
            try:
                summary = tester.run(data, code, market_df=None, vix_df=None, log_to_csv=False, 
                                      skip_indicators=skip_ind, high_precision=high_precision, m1_df_preloaded=m1_pre)
                if summary:
                    summaries.append(summary)
            except Exception as e: 
                print(f"  (!) Process Run Error ({code}): {e}")
                
    if summaries:
        df_sum = pd.DataFrame(summaries)
        result = settings.copy()
        
        # [v4.0] Accurate Global Aggregation for Stage 1 Metrics
        all_s1_a = [r for s in summaries for r in s.get('s1_a_rets', [])]
        all_s1_b = [r for s in summaries for r in s.get('s1_b_rets', [])]
        
        avg_s1_a = sum(all_s1_a) / len(all_s1_a) if all_s1_a else 0
        avg_s1_b = sum(all_s1_b) / len(all_s1_b) if all_s1_b else 0
        
        result.update({
            'avg_ret': df_sum['final_ret'].mean(), 'avg_mdd': df_sum['mdd'].mean(), 
            'avg_win': df_sum['win_rate'].mean(), 'total_trades': df_sum['trades'].sum(),
            'avg_s2': df_sum['s2_trades'].mean(),
            'avg_s1_a': avg_s1_a,
            'avg_s1_b': avg_s1_b
        })
        return result
    return None

class ParameterOptimizer:
    """자동화된 파라미터 최적화 엔진 (v5.1)"""
    def __init__(self, codes, param_ranges, base_settings=None):
        self.codes = codes
        self.param_ranges = param_ranges # {'rsi_min': [35, 40, 45], ...}
        self.base_settings = base_settings if base_settings else {}
        
    def find_best_params(self, start_date, end_date, interval='1d', callback=None, force_refresh=False, high_precision=False, method='grid', n_trials=50, seed=42):
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
            print(f"\n[Opt] Grid Search: {total_comb} combinations ({interval})")
        elif method == 'random':
            print(f"\n[Opt] Random Search: {n_trials} trials ({interval})")
            for _ in range(n_trials):
                comb = self.base_settings.copy()
                for k, v_list in self.param_ranges.items():
                    comb[k] = random.choice(v_list)
                all_combinations.append(comb)
            total_comb = n_trials
        elif method == 'bayesian' and HAS_OPTUNA:
            print(f"\n[Opt] Bayesian Search: {n_trials} trials ({interval})")
            # Bayesian은 별도 루프(study.optimize)에서 처리하므로 여기서 combinations를 채우지 않음
            total_comb = n_trials
        else:
            # Fallback to random if optuna not available but bayesian requested
            method = 'random'
            _why = OPTUNA_IMPORT_ERROR or "unknown"
            print(
                f"\n  (!) Optuna을(를) 불러올 수 없어 베이지안 대신 **랜덤 탐색**으로 대체합니다 ({n_trials} trials).\n"
                f"      원인: {_why}\n"
                f"      조치: GUI와 동일한 Python으로 `pip install optuna>=4.0.0` 실행 (프로젝트 requirements.txt 참고)."
            )
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
                data_cache[code] = res_15m.sort_index() if res_15m is not None else None
                if res_1m is not None:
                    data_cache_m1[code] = res_1m.sort_index()

        # [v23.1] 지표 재계산 필요 여부: SIGNAL_ONLY_PARAMS 화이트리스트(모듈 상수)와 대조
        signal_only_params = SIGNAL_ONLY_PARAMS
        
        # 최적화 대상 파라미터 중 하나라도 지표에 영향을 줄 가능성이 있으면 False
        is_indicator_constant = all(k in signal_only_params for k in keys)

        if is_indicator_constant:
            print("  [Perf] Technical indicators are constant across combinations. Pre-calculating...")
            for code, data in data_cache.items():
                if data is not None:
                    data_cache[code] = TradingStrategy.calculate_indicators(data, self.base_settings)
        else:
            print("  [Perf] Parameters affect indicators. Recalculating indicators for each trial.")

        # [v30.0] 거대한 데이터를 임시 파일로 저장하여 워커 프로세스에서 개별 로드 (Windows 피클링 오버헤드 방지)
        cache_data = {'data': data_cache, 'm1': data_cache_m1}
        tmp_cache_path = os.path.join(BASE_DIR, "bt_opt_cache.tmp")
        try:
            with open(tmp_cache_path, 'wb') as f:
                pickle.dump(cache_data, f)
            # 메인 프로세스에서도 전역 변수 설정 (일관성)
            init_worker(tmp_cache_path)
        except Exception as e:
            print(f"  (!) Cache Write Error: {e}")

        # [v23.0] 최적화: 공통 설정값만 인자로 준비
        common_args = (is_indicator_constant, high_precision)
        
        def prepare_args(idx_settings):
            idx, settings = idx_settings
            return (idx, settings) + common_args

        # 3. 실제 최적화 수행 (v23.0 Optimized: ProcessPoolExecutor 도입)
        
        if method == 'bayesian' and HAS_OPTUNA:
            # Optuna는 자체적으로 병렬화가 가능하지만, 여기서는 기존 run_comb 구조를 유지하며 최적화
            def objective(trial):
                try:
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
                        # [v35.4] Performance: Strip large lists (history, signal_logs) before storing in user_attrs
                        # This dramatically reduces Optuna's internal serialization overhead per trial
                        summary_res = {
                            k: v for k, v in res.items()
                            if k not in ['history', 'signal_logs', 'equity_curve']
                        }
                        for k, v in summary_res.items():
                            try:
                                trial.set_user_attr(k, v)
                            except Exception as _ua_err:
                                print(f"  (!) user_attr 설정 실패 ({k}): {_ua_err}")
                        return _finite_optuna_objective_value(res.get('avg_ret', -999.0))
                    return -999.0
                except Exception as _obj_err:
                    print(f"  (!) Objective trial {trial.number} 오류: {_obj_err}")
                    return -999.0

            _terminal_states = frozenset({
                optuna.trial.TrialState.COMPLETE,
                optuna.trial.TrialState.FAIL,
                optuna.trial.TrialState.PRUNED,
            })

            def study_callback(study, trial):
                """진행률 갱신: 종료된 trial(COMPLETE/FAIL/PRUNED) 개수 기준 (병렬 시 COMPLETE만 세면 과소)."""
                try:
                    if callback:
                        n_done = sum(1 for t in study.trials if t.state in _terminal_states)
                        denom = max(int(n_trials), 1)
                        callback(
                            f"베이지안 최적화 중 ({n_done}/{n_trials})",
                            50 + (n_done / denom) * 50,
                        )
                except Exception as _cb_err:
                    print(f"  (!) study_callback 오류: {_cb_err}")

            def rows_from_terminal_trials(study) -> list[dict]:
                """종료된 trial마다 1행 (COMPLETE / FAIL / PRUNED). FAIL도 n_trials와 행 수 불일치 완화."""
                rows: list[dict] = []
                try:
                    for t in study.get_trials(deepcopy=False):
                        if t.state not in _terminal_states:
                            continue
                        row: dict = {}
                        try:
                            if t.state == optuna.trial.TrialState.COMPLETE and t.user_attrs:
                                row = dict(t.user_attrs)
                            else:
                                row = self.base_settings.copy()
                                try:
                                    row.update(dict(t.params) if t.params else {})
                                except Exception as _p_err:
                                    print(f"  (!) trial.params 병합 오류 (trial {t.number}): {_p_err}")
                                val = t.value
                                ar = _finite_optuna_objective_value(
                                    val if val is not None else -999.0
                                )
                                row.update({
                                    'avg_ret': ar,
                                    'avg_mdd': 0.0,
                                    'avg_win': 0.0,
                                    'total_trades': 0,
                                    'avg_s2': 0.0,
                                    'avg_s1_a': 0.0,
                                    'avg_s1_b': 0.0,
                                })
                                if t.state != optuna.trial.TrialState.COMPLETE:
                                    row['opt_trial_state'] = str(t.state)
                                    try:
                                        sa = getattr(t, 'system_attrs', None) or {}
                                        fr = sa.get('fail_reason') or sa.get('fail_message') or ''
                                        if fr:
                                            row['opt_fail_reason'] = str(fr)[:500]
                                    except Exception as _sa_err:
                                        print(f"  (!) system_attrs 조회 오류 (trial {t.number}): {_sa_err}")
                        except Exception as _build_err:
                            print(f"  (!) trial 행 구성 오류 (trial {getattr(t, 'number', '?')}): {_build_err}")
                            continue
                        for pk in keys:
                            if pk not in row:
                                try:
                                    row[pk] = self.base_settings.get(pk)
                                except Exception:
                                    row[pk] = None
                        rows.append(row)
                except Exception as _row_err:
                    print(f"  (!) rows_from_terminal_trials 오류: {_row_err}")
                return rows

            sampler = optuna.samplers.TPESampler(seed=seed)
            study = optuna.create_study(direction='maximize', sampler=sampler)

            # Optuna n_jobs>1 은 스레드 병렬이라, 전역 _G_DATA/_G_IND_CACHE·백테스트를 동시에 돌리면
            # trial FAIL·목적값 비정상으로 COMPLETE만 모을 때 행 수가 크게 줄어든다. 직렬이 안전.
            num_workers = 1

            def robust_objective(trial):
                try:
                    if not _G_DATA:
                        init_worker(tmp_cache_path)
                    return objective(trial)
                except Exception as _rob_err:
                    print(f"  (!) robust_objective trial {trial.number} 오류: {_rob_err}")
                    return -999.0

            try:
                study.optimize(
                    robust_objective,
                    n_trials=n_trials,
                    n_jobs=num_workers,
                    callbacks=[study_callback],
                    catch=(Exception,),
                )
            except Exception as _opt_err:
                print(f"  (!) study.optimize 중단/오류 (부분 결과만 사용): {_opt_err}")
            try:
                state_counts = Counter(str(t.state) for t in study.get_trials(deepcopy=False))
                print(
                    f"  [Opt] trial 종료 요약: {dict(state_counts)} "
                    f"(총 {len(study.trials)}개, 요청 n_trials={n_trials})"
                )
            except Exception as _cnt_err:
                print(f"  (!) trial 상태 요약 출력 실패: {_cnt_err}")
            all_results = rows_from_terminal_trials(study)
        else:
            # [v23.0] 일반 Grid Search의 경우 모든 코어 동원
            # [v31.0] 마찬가지로 하드코딩 리미트(12) 해제
            num_workers = os.cpu_count() or 4
            print(f"  [Perf] Parallel PPE: {num_workers} workers")
            
            if callback: callback(f"멀티프로세싱 최적화 시작 (코어: {num_workers})...", 50)
            
            # [v30.0] PPE 초기화 시 데이터 로드 (Windows 최적화)
            with PPE(max_workers=num_workers, initializer=init_worker, initargs=(tmp_cache_path,)) as p_executor:
                # [v23.0] 최상위 레벨 함수인 run_single_combination 사용
                tasks = [prepare_args((i, comb)) for i, comb in enumerate(all_combinations)]
                all_results = list(p_executor.map(run_single_combination, tasks))
                all_results = [r for r in all_results if r is not None]

        # [v30.0] 임시 캐시 파일 삭제
        if os.path.exists(tmp_cache_path):
            try: os.remove(tmp_cache_path)
            except: pass

        if all_results:
            df_results = pd.DataFrame(all_results).sort_values(by='avg_ret', ascending=False)
            df_results.to_csv("optimization_results.csv", index=False, encoding='utf-8-sig')
            
            if not df_results.empty:
                best = df_results.iloc[0]
                best_settings = self.base_settings.copy()
                for k in keys:
                    val = best[k]
                    if k in ['vol_lookback', 'macd_fast', 'macd_slow', 'macd_sig',
                            'macd_fast_daily', 'macd_slow_daily', 'macd_sig_daily',
                            'macd_fast_entry', 'macd_slow_entry', 'macd_sig_entry',
                            'gc_window', 'rsi_window', 'atr_period', 'atr_period_a', 'atr_period_b',
                            'gap_grace_mins', 'gap_grace_mins_a', 'gap_grace_mins_b', 'ma_convergence',
                            'ma60_safe_ratio', 'backtest_initial_capital',
                            'stage1_amt', 'stage2_amt',
                            'prev_vol_lookback', 'prev_vol_lookback_b',
                            'prev_vol_lookback_s1_a', 'prev_vol_lookback_s1_b',
                            'prev_vol_lookback_s2_a', 'prev_vol_lookback_s2_b']:
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
                        local_final_tester.run(data, code, market_df=None, vix_df=None, log_to_csv=True,
                                               high_precision=high_precision, m1_df_preloaded=m1_pre)

                with TPE(max_workers=4) as executor:
                    futures = {executor.submit(_run_final, item): item[0] for item in data_cache.items()}
                    finished_pass = 0
                    total_pass = len(data_cache)
                    for future in as_completed(futures):
                        finished_pass += 1
                        msg = f"최종 백테스트 수행 중 ({finished_pass}/{total_pass})..."
                        if callback: callback(msg, 50 + (finished_pass/total_pass)*10)
            trd_path = os.path.join(BASE_DIR, "trade_report.html")
            trades_df = pd.DataFrame()
            total_trades_count, win_rate_calc, avg_ret_calc = 0, 0, 0
            
            if os.path.exists(TRADE_CSV):
                try:
                    trades_df = pd.read_csv(TRADE_CSV, dtype={'Code': str})
                    if not trades_df.empty:
                        trades_df = trades_df[trades_df['Date'] != 'Date'].copy()
                        trades_df['Date'] = pd.to_datetime(trades_df['Date'], errors='coerce')
                        trades_df = trades_df.dropna(subset=['Date'])
                        
                        sells = trades_df[trades_df['Action'].isin(['SELL', 'HOLD'])]
                        total_trades_count = len(sells)
                        win_rate_calc = (len(sells[sells['Return'] > 0]) / total_trades_count * 100) if total_trades_count > 0 else 0
                        
                        if total_trades_count > 0 and 'Amount' in sells.columns and sells['Amount'].sum() > 0:
                            avg_ret_calc = (sells['Return'] * sells['Amount']).sum() / sells['Amount'].sum()
                        else:
                            avg_ret_calc = sells['Return'].mean() if total_trades_count > 0 else 0
                except: pass

            generate_trade_report(trades_df, interval, trd_path, total_trades_count, 
                                  win_rate_calc, avg_ret_calc, best_settings,
                                  callback=callback, target_codes=self.codes, daily_equity=None, high_precision=high_precision)

            return df_results
        return None
