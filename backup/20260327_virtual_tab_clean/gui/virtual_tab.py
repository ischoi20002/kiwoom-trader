import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timedelta
import config
import os

class VirtualTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self.tree_active = None
        self.tree_history = None
        self.tree_events = None
        self.review_widgets = {}
        
        # UI Attributes [v3.7.8] Pre-define for linting
        self.vars = {}
        self.lbl_mode_badge = None
        self.btn_env = None
        self.lbl_mock_result = None
        self.snap_f = None
        self.snap_content = None
        self.lbl_empty = None
        self.tree_orders = None
        self.current_events = []
        
        # UI State
        self.selected_date = datetime.now()
        
        self._setup_ui()
        self._render_virtual_positions()

    def _setup_ui(self):
        """[v3.7.9] 프리미엄 대시보드 레이아웃 (Tabs-based & Clean Logic)"""
        main_container = ttk.Frame(self.parent, padding="15")
        main_container.pack(fill=tk.BOTH, expand=True)

        # --- [1] 상단: 계좌 요약 & 제어 통합 패널 ---
        header_f = ttk.Frame(main_container)
        header_f.pack(fill=tk.X, pady=(0, 15))
        
        # Summary Area (Left)
        summary_f = ttk.Frame(header_f)
        summary_f.pack(side=tk.LEFT, fill=tk.Y)
        
        self.vars.update({
            "total": tk.StringVar(value="0"),
            "cash": tk.StringVar(value="0"),
            "stock": tk.StringVar(value="0"),
            "day": tk.StringVar(value="0 (0.00%)"),
            "cum": tk.StringVar(value="0.00%")
        })

        for i, (label, var, color) in enumerate([
            ("총 자산", "total", self.app.colors['primary']),
            ("예수금", "cash", None),
            ("주식 평가금", "stock", None),
            ("금일 손익", "day", "#BF616A"),
            ("누적 수익률", "cum", "#A3BE8C")
        ]):
            f = ttk.Frame(summary_f)
            f.pack(side=tk.LEFT, padx=(0, 20))
            ttk.Label(f, text=label, font=("Malgun Gothic", 8), foreground=self.app.colors['secondary']).pack(anchor=tk.W)
            ttk.Label(f, textvariable=self.vars[var], font=("Malgun Gothic", 11, "bold"), 
                      foreground=color if color else self.app.colors['fg']).pack(anchor=tk.W)

        # Control Panel (Right)
        control_f = ttk.Frame(header_f)
        control_f.pack(side=tk.RIGHT, fill=tk.Y)
        
        use_mock = getattr(config, 'USE_MOCK', False)
        mode_text = " MOCK MODE " if use_mock else " REAL MODE "
        mode_bg = "#5E81AC" if use_mock else "#BF616A" 
        
        self.lbl_mode_badge = tk.Label(control_f, text=mode_text, font=("Malgun Gothic", 9, "bold"), 
                                      foreground="white", background=mode_bg, padx=10, pady=2)
        self.lbl_mode_badge.pack(side=tk.RIGHT, padx=(10, 0))
        
        # 버튼 그룹 (한 줄로 배치)
        btn_group = ttk.Frame(control_f)
        btn_group.pack(side=tk.RIGHT)
        
        ttk.Button(btn_group, text="🔄 동기화", width=10, command=lambda: self.app.sync_with_actual_account()).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_group, text="🧹 초기화", width=10, command=self.app._clear_virtual_positions).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_group, text="🚨 비상 매도", style="Accent.TButton", width=12, command=self.app._sell_all_virtual).pack(side=tk.LEFT, padx=5)
        self.btn_env = ttk.Button(btn_group, text="모드 전환", width=10, command=self._toggle_environment)
        self.btn_env.pack(side=tk.LEFT, padx=2)

        # --- [2] 메인 레이아웃 (PanedWindow) ---
        main_paned = ttk.PanedWindow(main_container, orient=tk.HORIZONTAL)
        main_paned.pack(fill=tk.BOTH, expand=True)

        # 좌측: 마스터 리브 (보유/종료/로그)
        master_f = ttk.Frame(main_paned)
        main_paned.add(master_f, weight=3)
        
        master_nb = ttk.Notebook(master_f)
        master_nb.pack(fill=tk.BOTH, expand=True)

        # [보유 중]
        active_f = ttk.Frame(master_nb, padding="5")
        master_nb.add(active_f, text=" 보유 현황 (Active) ")
        self.tree_active = self._build_active_tree(active_f)

        # [매매로그/종료]
        history_f = ttk.Frame(master_nb, padding="5")
        master_nb.add(history_f, text=" 매매 종료 (History) ")
        self.tree_history = self._build_history_tree(history_f)
        
        # [실시간 주문 로그] - 하단에서 이곳으로 이동 (Clutter 감소)
        order_log_tab = ttk.Frame(master_nb, padding="5")
        master_nb.add(order_log_tab, text=" 실시간 주문 (Orders) ")
        self.tree_orders = self._build_order_tree(order_log_tab)

        # 우측: 디테일 리브 (타임라인/스냅샷)
        detail_paned = ttk.PanedWindow(main_paned, orient=tk.VERTICAL)
        main_paned.add(detail_paned, weight=2)

        # 타임라인
        time_f = ttk.LabelFrame(detail_paned, text=" 🕒 매매 타임라인 (Timeline) ", padding="10")
        detail_paned.add(time_f, weight=1)
        self.tree_events = self._build_event_tree(time_f)

        # 스냅샷
        self.snap_f = ttk.LabelFrame(detail_paned, text=" 📸 스냅샷 복기 (Snapshot) ", padding="15")
        detail_paned.add(self.snap_f, weight=1)
        self._init_snap_ui()

        self._setup_styles()

    def _build_active_tree(self, parent):
        cols = ("Code", "Name", "Qty", "Avg", "Curr", "Profit")
        tree = ttk.Treeview(parent, columns=cols, show="headings", selectmode="browse")
        heads = {"Code": "코드", "Name": "종목명", "Qty": "수량", "Avg": "평단가", "Curr": "현재가", "Profit": "수익률(Day/Pos)"}
        for col, text in heads.items():
            tree.heading(col, text=text)
            tree.column(col, width=80, anchor=tk.CENTER)
        tree.column("Name", width=120, anchor=tk.W)
        tree.column("Profit", width=140, anchor=tk.CENTER)
        tree.pack(fill=tk.BOTH, expand=True)
        tree.bind("<<TreeviewSelect>>", self._on_stock_select)
        return tree

    def _build_history_tree(self, parent):
        cols = ("Code", "Name", "Result", "Time")
        tree = ttk.Treeview(parent, columns=cols, show="headings", selectmode="browse")
        for col, text in zip(cols, ["코드", "종목명", "수익률", "종료시간"]):
            tree.heading(col, text=text)
            tree.column(col, width=90, anchor=tk.CENTER)
        tree.column("Name", width=120, anchor=tk.W)
        tree.pack(fill=tk.BOTH, expand=True)
        tree.bind("<<TreeviewSelect>>", self._on_stock_select)
        return tree

    def _build_event_tree(self, parent):
        cols = ("Time", "Action", "Price", "Qty", "P/L", "Reason")
        tree = ttk.Treeview(parent, columns=cols, show="headings", selectmode="browse")
        for col, text in zip(cols, ["시간", "액션", "가격", "수량", "구간수익", "사유"]):
            tree.heading(col, text=text)
            tree.column(col, width=80, anchor=tk.CENTER)
        tree.column("Reason", width=150, anchor=tk.W)
        tree.pack(fill=tk.BOTH, expand=True)
        tree.bind("<<TreeviewSelect>>", self._on_event_select)
        return tree

    def _build_order_tree(self, parent):
        cols = ("Time", "Code", "Side", "Qty", "Price", "Status")
        tree = ttk.Treeview(parent, columns=cols, show="headings", height=5)
        for col, text in zip(cols, ["시간", "종목코드", "구분", "수량", "가격", "결과"]):
            tree.heading(col, text=text)
            tree.column(col, width=100, anchor=tk.CENTER)
        tree.pack(fill=tk.X, expand=True)
        return tree

    def _init_snap_ui(self):
        self.snap_content = ttk.Frame(self.snap_f)
        self.lbl_empty = ttk.Label(self.snap_f, text="이벤트를 선택하면\n그때의 지표 상태가 표시됩니다.", 
                                  foreground=self.app.colors['secondary'], justify=tk.CENTER)
        self.lbl_empty.pack(expand=True)
        grid = ttk.Frame(self.snap_content)
        grid.pack(fill=tk.X)
        fields = [("매매시간", "time"), ("매매가격", "price"), ("지수필터", "market"), 
                  ("RSI(14)", "rsi"), ("MA배열", "ma"), ("체결강도", "power"),
                  ("매매사유", "reason")]
        for i, (label, key) in enumerate(fields):
            ttk.Label(grid, text=label, font=("Malgun Gothic", 8), foreground=self.app.colors['secondary']).grid(row=i//2, column=(i%2)*2, sticky=tk.W, pady=3)
            val = ttk.Label(grid, text="-", font=("Malgun Gothic", 9, "bold"))
            val.grid(row=i//2, column=(i%2)*2+1, sticky=tk.W, padx=(10, 20), pady=3)
            self.review_widgets[key] = val

    def _render_virtual_positions(self):
        """실제 데이터(app.virtual_positions / history) 렌더링 - IID 기반 업데이트로 선택 유지"""
        # 1. 보유 종목 렌더링
        total_eval = 0
        total_invested = 0
        total_unrealized_pnl = 0
        
        # 현재 트리에 있는 항목들 (나중에 삭제할 항목 파악용)
        existing_active = set(self.tree_active.get_children())
        active_list = list(self.app.virtual_positions.items())
        
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
            tag_base = "even" if idx % 2 == 0 else "odd"
            p_tag = "price_up" if profit_pct > 0 else "price_down" if profit_pct < 0 else ""
            tags = (tag_base, p_tag)
            
            vals = (code, pos['name'], pos['qty'], f"{pos['avg_price']:,.0f}", f"{curr_p:,.0f}", p_str)
            
            if self.tree_active.exists(code):
                self.tree_active.item(code, values=vals, tags=tags)
                if code in existing_active: existing_active.remove(code)
            else:
                self.tree_active.insert("", tk.END, iid=code, values=vals, tags=tags)
        
        # 더 이상 없는 종목 삭제
        for old_id in existing_active:
            self.tree_active.delete(old_id)

        # 2. 종료 종목 렌더링 (히스토리는 보통 추가만 되므로 단순화)
        total_realized_pnl = 0
        total_day_realized_pnl = 0
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        # 히스토리는 개수가 적고 자주 전체가 바뀌지 않으므로 일단 기존 방식 유지하되, 
        # 필요시 iid 기반으로 변경 가능. 여기서는 텍스트만 업데이트.
        self.tree_history.delete(*self.tree_history.get_children())
        for idx, pos in enumerate(self.app.virtual_history):
            last_event = pos['events'][-1] if pos.get('events') else {}
            code = pos.get('code', '000000')
            pnl_amt = pos.get('realized_pnl_amt', 0)
            total_realized_pnl += pnl_amt
            
            if pos.get('entry_time', '').startswith(today_str):
                total_day_realized_pnl += pnl_amt
            
            tag_base = "even" if idx % 2 == 0 else "odd"
            p_tag = "price_up" if pos.get('final_pnl_pct', 0) > 0 else "price_down"
            self.tree_history.insert("", tk.END, values=(code, pos['name'], f"{pos.get('final_pnl_pct', 0):+.2f}%", last_event.get('time', '-')), tags=(tag_base, p_tag))

        # 4. 요약 정보 업데이트 [v2.2] 예수금 및 금일 손익 산출
        start_cap = float(str(self.app.settings.get('total_capital', 10000000)).replace(',', ''))
        current_cash = start_cap - total_invested + total_realized_pnl
        total_assets = current_cash + total_eval
        
        # 금일 손익 = 금일 실현 + 현재 미실현
        day_pnl_total = total_day_realized_pnl + total_unrealized_pnl
        day_pnl_pct = (day_pnl_total / start_cap) * 100
        
        self.vars["total"].set(f"{total_assets:,.0f}")
        self.vars["cash"].set(f"{current_cash:,.0f}")
        self.vars["stock"].set(f"{total_eval:,.0f}")
        self.vars["day"].set(f"{int(day_pnl_total):+,} ({day_pnl_pct:+.2f}%)")
        
        # 누적 수익률 계산
        total_profit_pct = (total_assets / start_cap - 1) * 100
        self.vars["cum"].set(f"{total_profit_pct:+.2f}%")

    def _on_stock_select(self, event):
        tree = event.widget
        sel = tree.selection()
        if not sel: return
        for i in self.tree_events.get_children(): self.tree_events.delete(i)
        
        values = tree.item(sel[0])['values']
        code = str(values[0]).zfill(6)
        
        # 데이터 찾기 (보유 또는 히스토리)
        target_pos = self.app.virtual_positions.get(code)
        if not target_pos:
            # 히스토리에서 찾기 (가장 최근 것)
            for h in reversed(self.app.virtual_history):
                if h.get('code') == code:
                    target_pos = h
                    break
        
        if target_pos and 'events' in target_pos:
            self.current_events = target_pos['events'] # 캐시
            for i, e in enumerate(target_pos['events']):
                self.tree_events.insert("", tk.END, iid=f"evt_{i}", values=(e['time'], e['action'], f"{e['price']:,.0f}", e['qty'], e['pnl'], e['reason']), tags=(e['action'],))

    def _on_event_select(self, event):
        sel = self.tree_events.selection()
        if not sel: return
        
        idx = int(sel[0].split('_')[1])
        if not hasattr(self, 'current_events') or idx >= len(self.current_events): return
        
        evt = self.current_events[idx]
        snap = evt.get('snapshot', {})
        
        if snap:
            self.lbl_empty.pack_forget()
            self.snap_content.pack(fill=tk.BOTH, expand=True)
            
            # 위젯 업데이트
            mapping = {
                "time": snap.get('time', '-'),
                "price": snap.get('price', '-'),
                "market": snap.get('market', '-'),
                "rsi": snap.get('rsi', '-'),
                "ma": snap.get('ma', '-'),
                "power": snap.get('power', '-'),
                "reason": snap.get('reason', '-')
            }
            # 추가 수급 지표 (ba_ratio) 표시 시도
            if "ba_ratio" in snap:
                mapping["power"] += f" (잔량:{snap['ba_ratio']})"
                
            for key, val in mapping.items():
                if key in self.review_widgets:
                    self.review_widgets[key].config(text=val)

    def _setup_styles(self):
        self.tree_events.tag_configure("BUY", foreground="#BF616A")
        self.tree_events.tag_configure("SELL", foreground="#81A1C1")
        self.tree_events.tag_configure("ADD", foreground="#EBCB8B")
        
        # [v2.2] 줄무늬(Zebra Striping) 명시적 색상 설정 (Nordic Theme)
        # MonitorTab과 동일한 고대비 색상 적용
        bg_even = "#242933" 
        bg_odd = "#3B4252"
        
        for t in [self.tree_active, self.tree_history, self.tree_events]:
            t.tag_configure("even", background=bg_even)
            t.tag_configure("odd", background=bg_odd)
            
        self.tree_active.tag_configure("price_up", foreground="#BF616A")
        self.tree_active.tag_configure("price_down", foreground="#81A1C1")
        self.tree_history.tag_configure("price_up", foreground="#BF616A")
        self.tree_history.tag_configure("price_down", foreground="#81A1C1")

    def _render_real_data(self):
        self._render_virtual_positions()

    def _toggle_environment(self):
        """[v3.7.4] 실전/모의 환경 설정(config.py)을 토글하고 재시작을 안내합니다."""
        import config
        import os
        from tkinter import messagebox
        
        current_mock = getattr(config, 'USE_MOCK', False)
        new_mock = not current_mock
        target_mode = "모의투자" if new_mock else "실전투자"
        
        if not messagebox.askyesno("환경 전환 확인", f"현재 설정을 [{target_mode}] 모드로 전환하시겠습니까?\n\n* 확인을 누르면 config.py 파일이 수정되며, 변경사항 적용을 위해 프로그램 재시작이 필요합니다."):
            return
            
        try:
            # config.py 경로 확인
            config_path = os.path.join(os.getcwd(), 'config.py')
            if not os.path.exists(config_path):
                # 파일이 없으면 상위 디렉토리 시도
                config_path = os.path.join(os.path.dirname(os.getcwd()), 'config.py')
            
            with open(config_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                
            new_lines = []
            found = False
            for line in lines:
                # USE_MOCK 라인 찾아서 교체
                if line.strip().startswith('USE_MOCK ='):
                    if '#' in line:
                        parts = line.split('#', 1)
                        new_lines.append(f'USE_MOCK = {new_mock} # {parts[1].strip()}\n')
                    else:
                        new_lines.append(f'USE_MOCK = {new_mock} # 환경 전환 버튼에 의해 자동 수정됨\n')
                    found = True
                else:
                    new_lines.append(line)
            
            if not found:
                new_lines.append(f'\nUSE_MOCK = {new_mock} # 환경 전환 버튼에 의해 추가됨\n')
                
            with open(config_path, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
                
            messagebox.showinfo("설정 변경 완료", f"성공적으로 [{target_mode}] 모드로 전환되었습니다.\n\n안전한 접속을 위해 프로그램을 종료하고 다시 실행해 주세요.")
            
            # 버튼 텍스트 즉시 업데이트 (사용자 피드백용)
            self.btn_env.config(text=f"환경 전환: {('실전' if new_mock else '모의')}")
            
        except Exception as e:
            messagebox.showerror("오류", f"설정 변경 중 오류가 발생했습니다: {e}")
