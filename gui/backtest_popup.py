import tkinter as tk
from tkinter import ttk, messagebox
import threading
from datetime import datetime
import traceback
import importlib.util

class BacktestParamPopup:
    """[v25.7] 백테스트 결과 상세 확인 및 개별 실행용 팝업 클래스"""
    def __init__(self, parent_root, app, params):
        self.root = parent_root
        self.app = app
        self.params = params
        self.edit_vars = {}
        self.popup = tk.Toplevel(self.root)
        self.popup.title("⚙️ 백테스트 반영 파라미터 확인")
        self._setup_ui()

    def _setup_ui(self):
        # 화면 중앙 배치 로직
        w, h = 580, 620
        self.root.update_idletasks()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()
        root_x = self.root.winfo_x()
        root_y = self.root.winfo_y()
        
        pos_x = root_x + (root_w // 2) - (w // 2)
        pos_y = root_y + (root_h // 2) - (h // 2)
        
        self.popup.geometry(f"{w}x{h}+{pos_x}+{pos_y}")
        self.popup.configure(bg="#2E3440")
        self.popup.transient(self.root)
        self.popup.grab_set()

        # Header
        header_frame = tk.Frame(self.popup, bg="#3B4252", height=50)
        header_frame.pack(fill=tk.X)
        rank_val = self.params.get('rank', self.params.get('Rank', '-'))
        ret_val = self.params.get('avg_ret', 0)
        ttk.Label(header_frame, text=f"🏆 Optimization Rank #{rank_val} | Avg Return: {ret_val:+.2f}%", 
                  font=("Malgun Gothic", 10, "bold"), foreground="#81A1C1", background="#3B4252").pack(pady=10)

        main_container = tk.Frame(self.popup, bg="#2E3440", padx=15, pady=10)
        main_container.pack(fill=tk.BOTH, expand=True)

        style = ttk.Style()
        style.configure("Nord.TLabelframe", background="#2E3440", foreground="#D8DEE9")
        style.configure("Nord.TLabelframe.Label", font=("Malgun Gothic", 9, "bold"), foreground="#88C0D0", background="#2E3440")

        # Category Definition (17 parameters)
        categories = [
            (" 🏁 1. 매수 진입 조건 (Entry) ", {
                'buy_rsi_min': 'RSI 하한', 'buy_rsi_max_a': 'RSI 상한(A)', 
                'buy_rsi_max_b': 'RSI 상한(B)', 'vol_surge': '거래량(A)', 'vol_surge_b': '거래량(B)',
                'rsi_window': 'RSI 윈도우', 'vol_lookback': '거래량룩백(L)', 'gc_window': '골든크로스(W)'
            }),
            (" 🎯 2. Type A/B 청산 (Exit) ", {
                's1_a_take_profit': 'A-익절 (%)', 's1_a_stop_loss': 'A-손절 (%)',
                's1_b_take_profit': 'B-익절 (%)', 's1_b_stop_loss': 'B-손절 (%)',
                'rsi_exit_limit': 'RSI 이탈기준'
            }),
            (" 💠 3. Stage 2 추매/청산 (S2) ", {
                'stage2_trigger': 'S2 추매기준', 's2_take_profit': 'S2-익절 (%)', 
                's2_stop_loss': 'S2-손절 (%)', 's2_min_days': 'S2-대기일(D)'
            })
        ]

        self.edit_vars = {}
        for title, fields in categories:
            lf = ttk.LabelFrame(main_container, text=title, padding="10", style="Nord.TLabelframe")
            lf.pack(fill=tk.X, pady=5)
            
            inner_col = 0
            for key, label in fields.items():
                row = inner_col // 3
                col = (inner_col % 3) * 2
                
                ttk.Label(lf, text=label, font=("Malgun Gothic", 8), foreground="#D8DEE9", background="#2E3440").grid(row=row, column=col, sticky="w", padx=2, pady=2)
                
                # [v25.2] 소수점 2자리까지 유연하게 표시
                orig_val = self.params.get(key, 0)
                try:
                    val_str = f"{float(orig_val):g}"
                except:
                    val_str = str(orig_val)
                    
                var = tk.StringVar(value=val_str)
                self.edit_vars[key] = var
                
                # 최적화 대상이었던 항목은 강조 (색상 등)
                is_optimized = key in self.app.backtest_tab.opt_params_vars
                entry_color = "#EBCB8B" if is_optimized else "#ECEFF4"
                
                entry = tk.Entry(lf, textvariable=var, width=8, bg="#3B4252", fg=entry_color, 
                               insertbackground="white", font=("Consolas", 9), borderwidth=0)
                entry.grid(row=row, column=col+1, padx=(0, 10), pady=2)
                inner_col += 1

        # Footer Actions
        footer = tk.Frame(main_container, bg="#2E3440")
        footer.pack(fill=tk.X, pady=(15, 0))
        
        ttk.Button(footer, text="🚀 정밀 백테스트 시작 (High-Precision)", style="Accent.TButton", 
                   command=self._on_btn_run_popup_backtest).pack(side=tk.RIGHT, padx=5)
        ttk.Button(footer, text="취소", command=self.popup.destroy).pack(side=tk.RIGHT)

    def _on_btn_run_popup_backtest(self):
        """[v25.6] 팝업에서 수정한 값으로 고정밀 일별 백테스트 실행"""
        try:
            full_params = {}
            for k, var in self.edit_vars.items():
                full_params[k] = float(var.get())
            
            # Start and End Date from Main App
            start_date = self.app.backtest_tab.start_date_picked.get()
            end_date = self.app.backtest_tab.end_date_picked.get()
            
            # Use DynamicBacktest as before
            from dynamic_backtest import DynamicBacktester
            tester = DynamicBacktester(self.app.settings, full_params)
            
            def run_task():
                try:
                    tester.run(start_date, end_date)
                    messagebox.showinfo("완료", "정밀 백테스트가 완료되었습니다. (로그 확인 요망)")
                except Exception as e:
                    messagebox.showerror("오류", f"백테스트 실행 중 오류 발생:\n{e}")
                    traceback.print_exc()
            
            threading.Thread(target=run_task, daemon=True).start()
            self.popup.destroy()
            
        except ValueError:
            messagebox.showwarning("입력 오류", "모든 파라미터는 숫자여야 합니다.")
        except Exception as e:
            messagebox.showerror("오류", f"백테스트 준비 중 오류 발생:\n{e}")
