import tkinter as tk
from tkinter import ttk
import pandas as pd

class BacktestResultTable:
    """[v25.7] 백테스트 결과 리스트 및 카테고리 헤더 관리 클래스"""
    def __init__(self, parent_frame, app, backtest_tab):
        self.parent = parent_frame
        self.app = app
        self.backtest_tab = backtest_tab
        self.tree = None
        
        self.table_assembly = ttk.Frame(self.parent)
        self.table_assembly.pack(pady=10)
        
        self._setup_ui()

    def _setup_ui(self):
        # 1. 카테고리 헤더 바 (Nord Palette)
        cat_frame = tk.Frame(self.table_assembly, height=30, bg="#3B4252")
        cat_frame.pack(side=tk.TOP, fill=tk.X) 

        categories = [
            ("📊 성과", 275, "#434C5E"),
            ("🚀 매수 진입", 520, "#4C566A"),
            ("🎯 청산/추매", 545, "#2E3440")
        ]
        
        for name, width, color in categories:
            f = tk.Frame(cat_frame, width=width, height=28, bg=color)
            f.pack_propagate(False)
            f.pack(side=tk.LEFT)
            lbl = tk.Label(f, text=name, font=("Malgun Gothic", 9, "bold"), 
                         bg=color, fg="#D8DEE9", relief=tk.FLAT)
            lbl.pack(fill=tk.BOTH, expand=True)

        # 2. Treeview 설정
        cols = ["rank", "avg_ret", "avg_mdd", "win_rate", "trades", 
                "gc_w", "vol_l", "vol_a", "vol_b", "rsi_min", "rsi_a", "rsi_b", "rsi_w",
                "a_tp", "a_sl", "b_tp", "b_sl", "s2_tp", "s2_sl", "rsi_ex", "s2", "s2_d"]
        
        self.tree = ttk.Treeview(self.table_assembly, columns=cols, show="headings", height=15)
        
        headers = {
            "rank": "순위", "avg_ret": "수익률", "avg_mdd": "MDD", "win_rate": "승률", "trades": "횟수",
            "gc_w": "☑️ GC_W", "vol_l": "☑️ V_L", "vol_a": "☑️ V_A", "vol_b": "☑️ V_B", 
            "rsi_min": "☑️ R_Min", "rsi_a": "☑️ R_A", "rsi_b": "☑️ R_B", "rsi_w": "☑️ R_W", 
            "a_tp": "☑️ A_TP", "a_sl": "☑️ A_SL", "b_tp": "☑️ B_TP", "b_sl": "☑️ B_SL",
            "s2_tp": "☑️ S2_TP", "s2_sl": "☑️ S2_SL", "rsi_ex": "☑️ R_Ex", "s2": "☑️ S2", "s2_d": "☑️ S2_D"
        }
        
        col_widths = {
            "rank": 40, "avg_ret": 60, "avg_mdd": 60, "win_rate": 60, "trades": 55,
            "gc_w": 65, "vol_l": 65, "vol_a": 65, "vol_b": 65, "rsi_min": 65, "rsi_a": 65, "rsi_b": 65, "rsi_w": 65,
            "a_tp": 60, "a_sl": 60, "b_tp": 60, "b_sl": 60, "s2_tp": 60, "s2_sl": 60,
            "rsi_ex": 65, "s2": 60, "s2_d": 60
        }

        for col in cols:
            self.tree.heading(col, text=headers.get(col, col), command=lambda c=col: self._on_header_click(c))
            self.tree.column(col, width=col_widths.get(col, 60), anchor=tk.CENTER, stretch=False)

        # Scrollbar
        scrollbar = ttk.Scrollbar(self.table_assembly, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Tooltips & Events
        self.tree.bind("<Motion>", self._on_tree_motion)
        self.tree.bind("<Leave>", self._on_tree_leave)
        self.tree.bind("<Double-1>", self.backtest_tab._on_tree_double_click)

    def _on_header_click(self, col_name):
        """[v15.7] 헤더 클릭 시 해당 파라미터 최적화 포함 여부 토글 (실시간 아이콘 업데이트)"""
        if col_name in self.backtest_tab.col_to_params:
            params = self.backtest_tab.col_to_params[col_name]
            is_active = self.backtest_tab.opt_params_vars[params[0]].get()
            new_state = not is_active
            
            for p in params:
                if p in self.backtest_tab.opt_params_vars:
                    self.backtest_tab.opt_params_vars[p].set(new_state)
            
            self.refresh_header_icons()

    def refresh_header_icons(self):
        """현재 상태에 맞춰 헤더 아이콘(☑️/⬜) 일괄 업데이트"""
        for col_name, params in self.backtest_tab.col_to_params.items():
            is_active = self.backtest_tab.opt_params_vars[params[0]].get()
            icon = "☑️" if is_active else "⬜"
            
            cols = list(self.tree["columns"])
            if col_name in cols:
                col_id = f"#{cols.index(col_name) + 1}"
                current_text = self.tree.heading(col_id, "text")
                base_text = current_text.replace("☑️", "").replace("⬜", "").strip()
                self.tree.heading(col_id, text=f"{icon} {base_text}")

    def render_results(self, df):
        """데이터프레임 결과를 테이블에 표시"""
        for item in self.tree.get_children():
            self.tree.delete(item)
            
        for i, (idx, row) in enumerate(df.iterrows(), 1):
            # i is now the 1-based rank
            perf_tag = 'positive' if row.avg_ret > 0 else 'negative'
            row_tag = 'even' if i % 2 == 0 else 'odd'

            gc_w = int(row.gc_window) if 'gc_window' in row else "-"
            vol_l = int(row.vol_lookback) if 'vol_lookback' in row else "-"
            vol_a = f"{row.vol_surge:.1f}" if 'vol_surge' in row else "-"
            vol_b = f"{row.vol_surge_b:.1f}" if 'vol_surge_b' in row else "-"
            rsi_min = int(row.buy_rsi_min) if 'buy_rsi_min' in row else "-"
            rsi_a = int(row.buy_rsi_max_a) if 'buy_rsi_max_a' in row else "-"
            rsi_b = int(row.buy_rsi_max_b) if 'buy_rsi_max_b' in row else "-"
            rsi_w = int(row.rsi_window) if 'rsi_window' in row else "-"
            
            a_tp = row.s1_a_take_profit if 's1_a_take_profit' in row else 0
            a_sl = row.s1_a_stop_loss if 's1_a_stop_loss' in row else 0
            b_tp = row.s1_b_take_profit if 's1_b_take_profit' in row else 0
            b_sl = row.s1_b_stop_loss if 's1_b_stop_loss' in row else 0
            s2_tp = row.s2_take_profit if 's2_take_profit' in row else 0
            s2_sl = row.s2_stop_loss if 's2_stop_loss' in row else 0
            
            rsi_ex = row.rsi_exit_limit if 'rsi_exit_limit' in row else "-"
            s2 = row.stage2_trigger if 'stage2_trigger' in row else "-"
            s2_d = int(row.s2_min_days) if 's2_min_days' in row else "-"

            self.tree.insert("", tk.END, values=(
                i, f"{row.avg_ret:+.2f}", f"{row.avg_mdd:.2f}", f"{row.avg_win:.2f}", 
                int(row.total_trades), gc_w, vol_l, vol_a, vol_b, rsi_min, rsi_a, rsi_b, rsi_w,
                f"{a_tp:.1f}", f"{a_sl:.1f}", f"{b_tp:.1f}", f"{b_sl:.1f}", 
                f"{s2_tp:.1f}", f"{s2_sl:.1f}", rsi_ex, s2, s2_d
            ), tags=(perf_tag, row_tag))

        self.tree.tag_configure('positive', foreground="#A3BE8C")
        self.tree.tag_configure('negative', foreground="#BF616A")
        self.tree.tag_configure('even', background="#3B4252")
        self.tree.tag_configure('odd', background="#2E3440")

    def _on_tree_motion(self, event):
        region = self.tree.identify_region(event.x, event.y)
        if region == "heading":
            col_id = self.tree.identify_column(event.x)
            try:
                col_name = self.tree["columns"][int(col_id.replace("#", "")) - 1]
                tip = self.backtest_tab.header_tooltips.get(col_name, "")
                if tip:
                    from gui import gui_utils
                    gui_utils.show_tooltip(event, tip)
            except:
                pass

    def _on_tree_leave(self, event):
        from gui import gui_utils
        gui_utils.hide_tooltip()
