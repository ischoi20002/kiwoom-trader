import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime
import config
import os

class VirtualTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self.vars = {}
        # [v4.5.3] 위젯 소유권 이전 (캡슐화)
        self.tree_active = None
        self.tree_history = None
        self.tree_orders = None
        self.lbl_mode_badge = None
        self.btn_env = None
        self.lbl_mock_result = None
        
        self._setup_ui()
        self._render_virtual_positions()

    def _setup_ui(self):
        """[v3.9] 3-판넬 수직 레이아웃 (보유/종료/주문 통합)"""
        main_container = ttk.Frame(self.parent, padding="10")
        main_container.pack(fill=tk.BOTH, expand=True)

        # --- [1] 상단: 계좌 요약 및 제어 패널 ---
        header_f = ttk.Frame(main_container)
        header_f.pack(fill=tk.X, pady=(0, 10))
        
        # 요약 정보 (좌측)
        summary_f = ttk.Frame(header_f)
        summary_f.pack(side=tk.LEFT, fill=tk.Y)
        
        self.vars.update({
            "total": tk.StringVar(value="0"),
            "cash": tk.StringVar(value="0"),
            "stock": tk.StringVar(value="0"),
            "d1_cash": tk.StringVar(value="0"),
            "d2_cash": tk.StringVar(value="0"),
            "day": tk.StringVar(value="0 (0.00%)"),
            "cum": tk.StringVar(value="0.00%")
        })

        for label, var, color in [
            ("총 자산", "total", self.app.colors['primary']),
            ("예수금", "cash", None),
            ("D+1 예상", "d1_cash", self.app.colors['primary']),
            ("D+2 예상", "d2_cash", self.app.colors['primary']),
            ("주식 평가금", "stock", None),
            ("금일 손익", "day", "#BF616A"),
            ("누적 수익률", "cum", "#A3BE8C")
        ]:
            f = ttk.Frame(summary_f)
            f.pack(side=tk.LEFT, padx=(0, 15))
            ttk.Label(f, text=label, font=("Malgun Gothic", 8), foreground=self.app.colors['primary']).pack(anchor=tk.W)
            ttk.Label(f, textvariable=self.vars[var], font=("Malgun Gothic", 10, "bold"), 
                      foreground=color if color else self.app.colors['fg']).pack(anchor=tk.W)

        # [v3.9.1] 모의투자 잔고 조회 결과 라벨
        self.lbl_mock_result = ttk.Label(summary_f, text="", font=("Malgun Gothic", 9), foreground="#A3BE8C")
        self.lbl_mock_result.pack(side=tk.LEFT, padx=10, pady=(15, 0))

        # 제어 버튼 (우측)
        control_f = ttk.Frame(header_f)
        control_f.pack(side=tk.RIGHT, fill=tk.Y)
        
        # [v3.9.5] 서버 상태 배지 (모의투자/실전)
        use_mock = getattr(config, 'USE_MOCK', False)
        mode_text = " 모의투자모드 " if use_mock else " 실전모드 "
        mode_bg = "#5E81AC" if use_mock else "#BF616A" 
        self.lbl_mode_badge = tk.Label(control_f, text=mode_text, font=("Malgun Gothic", 9, "bold"), 
                                      foreground="white", background=mode_bg, padx=10, pady=2)
        self.lbl_mode_badge.pack(side=tk.RIGHT, padx=(10, 0))

        btn_group = ttk.Frame(control_f)
        btn_group.pack(side=tk.RIGHT)
        
        ttk.Button(btn_group, text="🔄 동기화", width=8, command=self.app._sync_account_deposit).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_group, text="📊 리포트", width=8, command=self.generate_report).pack(side=tk.LEFT, padx=2)
        
        # [v3.9.5] 버튼명 '서버 전환'으로 변경
        self.btn_env = ttk.Button(btn_group, text="서버 전환", width=10, command=self._toggle_environment)
        self.btn_env.pack(side=tk.LEFT, padx=2)

        # --- [2] 메인 레이아웃 (3단 수직 분할) ---
        v_paned = ttk.PanedWindow(main_container, orient=tk.VERTICAL)
        v_paned.pack(fill=tk.BOTH, expand=True)

        # 1. 보유 현황
        active_f = ttk.LabelFrame(v_paned, text=" 💰 실시간 보유 현황 (Active) ", padding="5")
        v_paned.add(active_f, weight=4)
        self.tree_active = self._build_active_tree(active_f)

        # 2. 매매 종료
        history_f = ttk.LabelFrame(v_paned, text=" ✅ 매매 종료 내역 (History) ", padding="5")
        v_paned.add(history_f, weight=3)
        self.tree_history = self._build_history_tree(history_f)

        # 3. 주문 내역 (통합된 주문 로그)
        order_f = ttk.LabelFrame(v_paned, text=" 📄 실시간 주문 로그 (Order Log) ", padding="5")
        v_paned.add(order_f, weight=3)
        self.tree_orders = self._build_order_tree(order_f)

        self._setup_styles()

    def _build_active_tree(self, parent):
        cols = ("Code", "Name", "Qty", "Avg", "Curr", "Eval", "PnL", "Profit", "Entry")
        tree = ttk.Treeview(parent, columns=cols, show="headings", selectmode="browse")
        heads = {
            "Code": "코드", "Name": "종목명", "Qty": "수량", "Avg": "평단가", 
            "Curr": "현재가", "Eval": "평가금", "PnL": "손익금", 
            "Profit": "수익률", "Entry": "진입시간"
        }
        for col, text in heads.items():
            tree.heading(col, text=text)
            tree.column(col, width=80, anchor=tk.CENTER)
        tree.column("Name", width=110, anchor=tk.W)
        tree.column("Eval", width=90, anchor=tk.E)
        tree.column("PnL", width=90, anchor=tk.E)
        tree.column("Profit", width=80, anchor=tk.CENTER)
        tree.column("Entry", width=70, anchor=tk.CENTER)
        
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        return tree

    def _build_history_tree(self, parent):
        cols = ("Time", "Code", "Name", "Qty", "BuyP", "SellP", "PnL", "Profit", "Reason")
        tree = ttk.Treeview(parent, columns=cols, show="headings", selectmode="browse")
        heads = ["종료시간", "코드", "종목명", "수량", "매수가", "매도가", "손익금", "수익률", "사유"]
        for col, text in zip(cols, heads):
            tree.heading(col, text=text)
            tree.column(col, width=85, anchor=tk.CENTER)
        tree.column("Name", width=110, anchor=tk.W)
        tree.column("PnL", width=90, anchor=tk.E)
        tree.column("Reason", width=120, anchor=tk.W)
        
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        return tree

    def _build_order_tree(self, parent):
        cols = ("Time", "Code", "Name", "Side", "Qty", "OrderP", "FilledP", "Status", "Reason")
        tree = ttk.Treeview(parent, columns=cols, show="headings")
        display_names = ["시간", "코드", "종목명", "구분", "수량", "주문가", "체결가", "상태", "사유"]
        for col, text in zip(cols, display_names):
            tree.heading(col, text=text)
            tree.column(col, width=85, anchor=tk.CENTER)
        tree.column("Name", width=110, anchor=tk.W)
        tree.column("Reason", width=150, anchor=tk.W)
        
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        return tree

    def _render_virtual_positions(self):
        """데이터(app.virtual_positions / history / today_orders) 동기화"""
        # 1. 보유 종목
        total_eval = 0
        total_invested = 0
        total_unrealized_pnl = 0
        
        existing_active = set(self.tree_active.get_children())
        active_list = list(self.app.portfolio.virtual_positions.items())
        
        for idx, (code, pos) in enumerate(active_list):
            info = self.app.monitor_info.get(code, {})
            curr_p = info.get('price', pos['avg_price'])
            profit_pct = (curr_p / pos['avg_price'] - 1) * 100
            eval_amt = curr_p * pos['qty']
            unrealized = (curr_p - pos['avg_price']) * pos['qty']
            
            total_eval += eval_amt
            total_invested += pos.get('invested', 0)
            total_unrealized_pnl += unrealized
            
            p_str = f"{profit_pct:+.2f}%"
            entry_tm = pos.get('entry_time', '').split('T')[-1][:5] if 'T' in pos.get('entry_time', '') else '-'
            tag_base = "even" if idx % 2 == 0 else "odd"
            p_tag = "price_up" if profit_pct > 0 else "price_down" if profit_pct < 0 else ""
            
            vals = (
                code, pos['name'], f"{pos['qty']:,}", f"{pos['avg_price']:,.0f}", f"{curr_p:,.0f}",
                f"{int(eval_amt):+,}", f"{int(unrealized):+,}", p_str, entry_tm
            )
            
            if self.tree_active.exists(code):
                self.tree_active.item(code, values=vals, tags=(tag_base, p_tag))
                if code in existing_active: existing_active.remove(code)
            else:
                self.tree_active.insert("", tk.END, iid=code, values=vals, tags=(tag_base, p_tag))
        
        for old_id in existing_active:
            self.tree_active.delete(old_id)

        # 2. 종료 종목
        total_realized_pnl = 0
        total_day_realized_pnl = 0
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        self.tree_history.delete(*self.tree_history.get_children())
        for idx, pos in enumerate(self.app.portfolio.virtual_history):
            last_event = pos['events'][-1] if pos.get('events') else {}
            pnl_amt = pos.get('realized_pnl_amt', 0)
            total_realized_pnl += pnl_amt
            
            if pos.get('entry_time', '').startswith(today_str):
                total_day_realized_pnl += pnl_amt
            
            tag_base = "even" if idx % 2 == 0 else "odd"
            p_tag = "price_up" if pos.get('final_pnl_pct', 0) > 0 else "price_down"
            self.tree_history.insert("", tk.END, values=(
                pos.get('exit_time', last_event.get('time', '-')), 
                pos.get('code'), pos['name'], f"{pos.get('qty', 0):,}",
                f"{pos.get('avg_price', 0):,.0f}", f"{pos.get('exit_price', 0):,.0f}",
                f"{int(pnl_amt):+,}", f"{pos.get('final_pnl_pct', 0):+.2f}%", 
                pos.get('reason', 'SIGNAL')
            ), tags=(tag_base, p_tag))

        # 3. 주문 내역
        self._render_orders()

        # 4. 요약 정보 (Account Summary - REST API Synced)
        # [v4.8.6 Asset Sync] 추정치가 아닌 실제 계좌 동기화 데이터를 우선 사용
        portfolio = self.app.portfolio
        total_assets = getattr(portfolio, 'total_equity', 0)
        current_cash = getattr(portfolio, 'actual_deposit', 0)
        stock_eval = total_eval # 현재 트리뷰의 평가금액 합계
        
        # [v4.8.6] API에서 총자산이 0으로 잡히는 경우(종목미보유 등)를 대비한 최종 보정
        if total_assets == 0:
            total_assets = current_cash + stock_eval
            
        day_pnl_total = total_day_realized_pnl + total_unrealized_pnl
        # 수익률 계산을 위한 기준 자산 (설정값 or 실제자산)
        start_cap = float(str(self.app.settings.get('backtest_initial_capital', 10000000)).replace(',', ''))
        safe_cap = max(1, start_cap if total_assets == 0 else (total_assets - day_pnl_total))
        
        day_pnl_pct = (day_pnl_total / safe_cap) * 100
        
        self.vars["total"].set(f"{total_assets:,.0f}")
        self.vars["cash"].set(f"{current_cash:,.0f}")
        self.vars["stock"].set(f"{stock_eval:,.0f}")
        
        # [v4.5.3] D+1, D+2 예상 예수금 업데이트
        self.vars["d1_cash"].set(f"{getattr(portfolio, 'd1_deposit', 0):,.0f}")
        self.vars["d2_cash"].set(f"{getattr(portfolio, 'd2_deposit', 0):,.0f}")
            
        self.vars["day"].set(f"{int(day_pnl_total):+,} ({day_pnl_pct:+.2f}%)")
        
        total_profit_pct = (total_assets / safe_cap - 1) * 100
        self.vars["cum"].set(f"{total_profit_pct:+.2f}%")

    def _render_real_data(self):
        """[v4.5.3] 실시간 틱 수신 시 보유 종목 수익률만 부분 렌더링 (최적화)"""
        if not self.tree_active: return
        
        for code in self.app.portfolio.virtual_positions:
            if self.tree_active.exists(code):
                pos = self.app.portfolio.virtual_positions[code]
                info = self.app.monitor_info.get(code, {})
                curr_p = info.get('price', pos['avg_price'])
                profit_pct = (curr_p / pos['avg_price'] - 1) * 100
                eval_amt = curr_p * pos['qty']
                unrealized = (curr_p - pos['avg_price']) * pos['qty']
                
                p_str = f"{profit_pct:+.2f}%"
                p_tag = "price_up" if profit_pct > 0 else "price_down" if profit_pct < 0 else ""
                
                # 기존 값 가져와서 수익률/평가금만 교체
                curr_vals = list(self.tree_active.item(code, 'values'))
                curr_vals[4] = f"{curr_p:,.0f}"      # 현재가
                curr_vals[5] = f"{int(eval_amt):+,}" # 평가금
                curr_vals[6] = f"{int(unrealized):+,}" # 손익금
                curr_vals[7] = p_str                # 수익률
                
                # 태그 유지하며 색상만 변경
                tags = list(self.tree_active.item(code, 'tags'))
                if "price_up" in tags: tags.remove("price_up")
                if "price_down" in tags: tags.remove("price_down")
                if p_tag: tags.append(p_tag)
                
                self.tree_active.item(code, values=tuple(curr_vals), tags=tuple(tags))

    def _render_orders(self):
        """DB 기반 실시간 주문 내역 동기화"""
        self.tree_orders.delete(*self.tree_orders.get_children())
        try:
            orders = self.app.data_manager.get_today_orders()
            for i, ord_data in enumerate(orders):
                # ts, code, side, qty, price, f_price, f_qty, result, name, reason
                ts, code, side, qty, price, f_price, f_qty, result, name, reason = ord_data
                tm = ts.split(" ")[1] if " " in ts else ts
                side_kr = "매수" if side == "buy" else "매도"
                f_p_str = f"{f_price:,}" if f_price > 0 else "-"
                
                tag_base = "even" if i % 2 == 0 else "odd"
                side_tag = "price_up" if side == "buy" else "price_down"
                
                self.tree_orders.insert("", tk.END, values=(
                    tm, code, name, side_kr, f"{qty:,}", f"{price:,}", f_p_str, result, reason
                ), tags=(tag_base, side_tag))
        except: pass

    def _toggle_environment(self):
        """[v3.9.6] 실전/모의 서버 설정(config.py)을 토글하고 재시작 안내를 강화합니다.
        파일에서 직접 현재 상태를 읽어 연속적인 토글이 가능하게 조치합니다."""
        import os
        
        try:
            config_path = os.path.join(os.getcwd(), 'config.py')
            if not os.path.exists(config_path):
                config_path = os.path.join(os.path.dirname(os.getcwd()), 'config.py')
            
            with open(config_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                
            # 파일 내용에서 현재 USE_MOCK 값 추출
            current_file_mock = True
            for line in lines:
                if line.strip().startswith('USE_MOCK ='):
                    current_file_mock = 'True' in line.split('=')[1]
                    break
            
            new_mock = not current_file_mock
            target_mode = "모의투자" if new_mock else "실전투자"
            
            # 감시 중일 경우 경고
            if getattr(self.app, 'is_monitoring', False):
                if not messagebox.askyesno("감시 중 전환", "현재 실시간 감시가 진행 중입니다. 지금 설정을 변경해도 재시작 전까지는 현재 모드가 유지됩니다.\n\n계속하시겠습니까?"):
                    return

            if not messagebox.askyesno("서버 전환 확인", f"현재 설정을 [{target_mode}] 모드로 전환하시겠습니까?"):
                return
                
            new_lines = []
            found = False
            for line in lines:
                if line.strip().startswith('USE_MOCK ='):
                    new_lines.append(f'USE_MOCK = {new_mock} # 서버 전환 버튼에 의해 자동 수정됨\n')
                    found = True
                else:
                    new_lines.append(line)
            
            if not found:
                new_lines.append(f'\nUSE_MOCK = {new_mock} # 서버 전환 버튼에 의해 추가됨\n')
                
            with open(config_path, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
                
            # 성공 메시지 및 UI 업데이트
            messagebox.showinfo("설정 변경 완료", f"성공적으로 [{target_mode}] 모드로 설정되었습니다.\n\n재시작하면 {target_mode} 서버에서 시작합니다.")
            
            mode_bg = "#5E81AC" if new_mock else "#BF616A" 
            mode_text = " 모의투자모드 " if new_mock else " 실전모드 "
            self.lbl_mode_badge.config(text=mode_text, background=mode_bg)
            
        except Exception as e:
            messagebox.showerror("오류", f"설정 변경 중 오류가 발생했습니다: {e}")

    def log_order(self, code, side, qty, price, order_type, result, reason=""):
        """[v4.5.3] DB 기록 및 신규 3판넬 UI 갱신 (캡슐화)"""
        self.app.data_manager.log_order(code, side, qty, price, order_type, "MOCK_ID", result, reason)
        self._render_orders()
        self._render_virtual_positions()

    def generate_report(self):
        """[v4.5.3] 가상 매매 나러티브 리포트 생성 (UI 위임)"""
        try:
            from reporting.virtual_report import generate_virtual_narrative_report
            import webbrowser
            
            output_file = "virtual_trade_report.html"
            generate_virtual_narrative_report(self.app.portfolio.virtual_history, output_file, self.app.settings)
            
            abs_path = os.path.abspath(output_file)
            if os.path.exists(abs_path):
                webbrowser.open(f"file://{abs_path}")
                self.app.log(f"[Sync] 가상 매매 나러티브 리포트 생성 완료: {output_file}")
            else:
                self.app.log("[Error] 리포트 파일 생성 실패")
        except Exception as e:
            self.app.log(f"[Error] 리포트 생성 중 오류: {e}")

    def open_report_folder(self):
        """리포트 저장 폴더 열기"""
        try:
            os.startfile(os.getcwd())
        except Exception as e:
            self.app.log(f"[Error] 폴더 열기 실패: {e}")

    def _setup_styles(self):
        import tkinter as tk
        from tkinter import ttk
        style = ttk.Style()
        bg_even = "#242933" 
        bg_odd = "#3B4252"
        up_col = "#E35F5F" # Nord Red variant
        dn_col = "#81A1C3" # Nord Blue variant
        
        for t in [self.tree_active, self.tree_history, self.tree_orders]:
            # [v4.8.4] TCheckbutton 스타일은 메인에서 관리하므로 태그에 집중
            t.tag_configure("even", background=bg_even)
            t.tag_configure("odd", background=bg_odd)
            
            # [v4.7.5] 글로벌 일관성을 위해 고밀도 시인성 컬러 적용
            t.tag_configure("price_up", foreground=up_col)
            t.tag_configure("price_down", foreground=dn_col)
