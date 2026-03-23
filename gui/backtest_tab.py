import tkinter as tk
from tkinter import ttk, messagebox
import threading
import pandas as pd
import numpy as np
import traceback
from datetime import datetime, timedelta
from gui.backtest_popup import BacktestParamPopup
from gui.backtest_table import BacktestResultTable

class BacktestTab:
    """[v25.7] 백테스팅 탭 (모듈화 버전: Table과 Popup을 분리)"""
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self.last_opt_df = None
        self.opt_params_vars = {}
        self.col_to_params = {}
        self.result_table = None
        self.header_tooltips = {}
        self.start_date_picked = None
        self.end_date_picked = None
        
        self._init_vars()
        self._setup_ui()

    def _init_vars(self):
        """최적화 대상 파라미터 및 매핑 초기화"""
        self.opt_params_vars = {
            'buy_rsi_min': tk.BooleanVar(value=True),
            'buy_rsi_max_a': tk.BooleanVar(value=True),
            'buy_rsi_max_b': tk.BooleanVar(value=True),
            'vol_surge': tk.BooleanVar(value=True),
            'vol_surge_b': tk.BooleanVar(value=True),
            'rsi_window': tk.BooleanVar(value=True),
            's1_a_take_profit': tk.BooleanVar(value=True),
            's1_b_take_profit': tk.BooleanVar(value=True),
            's1_a_stop_loss': tk.BooleanVar(value=True),
            's1_b_stop_loss': tk.BooleanVar(value=True),
            's2_take_profit': tk.BooleanVar(value=True),
            's2_stop_loss': tk.BooleanVar(value=True),
            'rsi_exit_limit': tk.BooleanVar(value=True),
            'stage2_trigger': tk.BooleanVar(value=True),
            'vol_lookback': tk.BooleanVar(value=True),
            'gc_window': tk.BooleanVar(value=True),
            's2_min_days': tk.BooleanVar(value=True)
        }
        self.col_to_params = {
            "vol_a": ["vol_surge"], "vol_b": ["vol_surge_b"],
            "rsi_b": ["buy_rsi_max_b"], "rsi_a": ["buy_rsi_max_a"], "rsi_min": ["buy_rsi_min"],
            "rsi_w": ["rsi_window"], "vol_l": ["vol_lookback"], "gc_w": ["gc_window"],
            "a_tp": ["s1_a_take_profit"], "a_sl": ["s1_a_stop_loss"],
            "b_tp": ["s1_b_take_profit"], "b_sl": ["s1_b_stop_loss"],
            "s2_tp": ["s2_take_profit"], "s2_sl": ["s2_stop_loss"],
            "rsi_ex": ["rsi_exit_limit"], "s2": ["stage2_trigger"], "s2_d": ["s2_min_days"]
        }
        self._sync_opt_toggles_with_settings()

    def _sync_opt_toggles_with_settings(self):
        s = self.app.settings
        sync_map = {
            "use_type_a": ["vol_surge", "buy_rsi_max_a", "s1_a_take_profit", "s1_a_stop_loss"],
            "use_type_b": ["vol_surge_b", "buy_rsi_max_b", "s1_b_take_profit", "s1_b_stop_loss"],
            "use_rsi_min": ["buy_rsi_min"],
            "use_s2_tp": ["s2_take_profit"],
            "use_s2_stop_loss": ["s2_stop_loss"],
            "use_s2_rsi_exit": ["rsi_exit_limit"]
        }
        for key, opt_keys in sync_map.items():
            is_enabled = s.get(key, True)
            for k in opt_keys:
                if k in self.opt_params_vars:
                    self.opt_params_vars[k].set(is_enabled)
        
        if hasattr(self, 'result_table'):
            self.result_table.refresh_header_icons()

    def _setup_ui(self):
        top_grid_frame = ttk.Frame(self.parent)
        top_grid_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(20, 10))
        
        # 1. Date Range
        date_lf = ttk.LabelFrame(top_grid_frame, text=" 📅 1. 기간 설정 ", padding="15")
        date_lf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        
        self.start_date_picked = tk.StringVar(value=self.app.settings.get('bt_start_date', "20250304"))
        self.end_date_picked = tk.StringVar(value=self.app.settings.get('bt_end_date', datetime.now().strftime("%Y%m%d")))
        
        tk.Label(date_lf, text="시작일(YYYYMMDD):").grid(row=0, column=0, sticky="w")
        tk.Entry(date_lf, textvariable=self.start_date_picked, width=12).grid(row=0, column=1, padx=5, pady=2)
        tk.Label(date_lf, text="종료일(YYYYMMDD):").grid(row=1, column=0, sticky="w")
        tk.Entry(date_lf, textvariable=self.end_date_picked, width=12).grid(row=1, column=1, padx=5, pady=2)

        # 2. Optimization Targets
        opt_lf = ttk.LabelFrame(top_grid_frame, text=" 🛠️ 2. 최적화 대상 ", padding="15")
        opt_lf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        
        targets = [
            ('buy_rsi_min', 'R_Min'), ('buy_rsi_max_a', 'R_Max(A)'), ('buy_rsi_max_b', 'R_Max(B)'),
            ('vol_surge', 'V_Surge(A)'), ('vol_surge_b', 'V_Surge(B)'), ('rsi_window', 'RSI_W'),
            ('vol_lookback', 'V_Lookback(L)'), ('gc_window', 'GC_W(W)'), ('s2_min_days', 'S2_Delay(D)')
        ]
        for i, (key, label) in enumerate(targets):
            row, col = divmod(i, 3)
            ttk.Checkbutton(opt_lf, text=label, variable=self.opt_params_vars[key]).grid(row=row, column=col, sticky="w")

        # 3. Actions
        action_lf = ttk.LabelFrame(top_grid_frame, text=" ⚡ 3. 실행 ", padding="15")
        action_lf.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)
        ttk.Button(action_lf, text="🔍 파라미터 최적화 시작", command=self._on_btn_start_optimize).pack(fill=tk.X, pady=2)
        ttk.Button(action_lf, text="📊 결과 리포트 열기", command=self._open_opt_trade_report).pack(fill=tk.X, pady=2)

        # Result Table (Modular)
        res_frame = ttk.LabelFrame(self.parent, text=" 💎 최적화 결과 랭킹 ")
        res_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        self.result_table = BacktestResultTable(res_frame, self.app, self)
        
        self.header_tooltips = {
            "rank": "순위: 수익률 기준 최적화 결과 순위",
            "avg_ret": "수익률: 백테스트 대상 종목들의 평균 수익률 (%)",
            "avg_mdd": "MDD: 평균 최대 낙폭",
            "win_rate": "승률 (%)", "trades": "매매 횟수",
            "vol_a": "Type A 거래량 급증 기준", "vol_b": "Type B 거래량 급증 기준",
            "rsi_b": "RSI 상한(B)", "rsi_a": "RSI 상한(A)", "rsi_min": "RSI 하한",
            "a_tp": "A 익절 (%)", "a_sl": "A 손절 (%)", "b_tp": "B 익절 (%)", "b_sl": "B 손절 (%)",
            "s2_tp": "S2 익절 (%)", "s2_sl": "S2 손절 (%)", "rsi_ex": "RSI 이탈기준",
            "s2": "S2 추매기준", "rsi_w": "RSI 윈도우(분)", "vol_l": "거래량 룩백(일)",
            "gc_w": "골든크로스 윈도우(분)", "s2_d": "S2 최소 대기일(일)"
        }

    def _on_btn_start_optimize(self):
        import parameter_optimizer
        search_space = {}
        for key, var in self.opt_params_vars.items():
            if var.get():
                # Get range from settings or dynamic generator
                full_space = self._get_dynamic_search_space(self.app.settings)
                search_space[key] = full_space[key]

        if not search_space:
            messagebox.showwarning("주의", "최소 하나 이상의 파라미터를 선택하세요.")
            return

        def run_opt():
            try:
                results_df = parameter_optimizer.find_best_params(
                    self.app.settings, search_space,
                    self.start_date_picked.get(), self.end_date_picked.get()
                )
                self.last_opt_df = results_df
                self.app.root.after(0, lambda: self.result_table.render_results(results_df))
            except Exception as e:
                messagebox.showerror("오류", f"최적화 중 오류 발생: {e}")
                traceback.print_exc()

        threading.Thread(target=run_opt, daemon=True).start()

    def _on_tree_double_click(self, event):
        selection = self.result_table.tree.selection()
        if not selection: return
        rank = int(self.result_table.tree.item(selection[0], "values")[0])
        if self.last_opt_df is not None:
            # iloc is 0-indexed, rank is 1-indexed in the table display
            params = self.last_opt_df.iloc[rank-1].to_dict()
            params['rank'] = rank
            BacktestParamPopup(self.app.root, self.app, params)

    def _get_dynamic_search_space(self, s):
        def range_for(val, step, count=3, min_val=None):
            import numpy as np
            if isinstance(val, (int, np.integer)):
                res = [int(val + (i - count//2) * step) for i in range(count)]
            else:
                res = [round(float(val) + (i - count//2) * step, 2) for i in range(count)]
            if min_val is not None: res = [max(min_val, x) for x in res]
            return sorted(list(set(res)))

        return {
            'buy_rsi_min': range_for(s.get('buy_rsi_min', 40.0), 5.0, min_val=0),
            'buy_rsi_max_b': range_for(s.get('buy_rsi_max_b', 65.0), 5.0),
            'buy_rsi_max_a': range_for(s.get('buy_rsi_max_a', 58.0), 5.0),
            'vol_surge': range_for(s.get('vol_surge', 1.5), 0.3, min_val=0.1),
            'vol_surge_b': range_for(s.get('vol_surge_b', 1.5), 0.3, min_val=0.1),
            's1_a_take_profit': range_for(s.get('s1_a_take_profit', 5.0), 2.0, min_val=0.5),
            's1_b_take_profit': range_for(s.get('s1_b_take_profit', 5.0), 2.0, min_val=0.5),
            's1_a_stop_loss': range_for(s.get('s1_a_stop_loss', 3.0), 1.0, min_val=0.5),
            's1_b_stop_loss': range_for(s.get('s1_b_stop_loss', 3.0), 1.0, min_val=0.5),
            's2_take_profit': range_for(s.get('s2_take_profit', 5.0), 2.0, min_val=0.5),
            's2_stop_loss': range_for(s.get('s2_stop_loss', 2.0), 1.0, min_val=0.5),
            'rsi_exit_limit': range_for(s.get('rsi_exit_limit', 50.0), 5.0),
            'stage2_trigger': range_for(s.get('stage2_trigger', 1.0), 0.5),
            'rsi_window': range_for(s.get('rsi_window', 45), 15, min_val=15),
            'vol_lookback': range_for(s.get('vol_lookback', 10), 5, min_val=1),
            'gc_window': range_for(s.get('gc_window', 15), 15, min_val=15),
            's2_min_days': range_for(s.get('s2_min_days', 1), 1, min_val=0)
        }

    def _open_opt_trade_report(self):
        import os, webbrowser
        path = os.path.abspath("trade_report.html")
        if os.path.exists(path):
            webbrowser.open(f"file://{path}")
        else:
            messagebox.showinfo("정보", "리포트 파일이 없습니다.")

    def _on_btn_backtest_with_selected(self):
        selection = self.result_table.tree.selection()
        if not selection:
            messagebox.showwarning("주의", "조합을 선택해주세요.")
            return
        rank = int(self.result_table.tree.item(selection[0], "values")[0])
        if self.last_opt_df is not None:
            selected_params = self.last_opt_df.iloc[rank-1].to_dict()
            from dynamic_backtest import DynamicBacktester
            tester = DynamicBacktester(self.app.settings, selected_params)
            def run():
                tester.run(self.start_date_picked.get(), self.end_date_picked.get())
                self.app.root.after(0, lambda: messagebox.showinfo("완료", "백테스트 완료"))
            threading.Thread(target=run, daemon=True).start()
