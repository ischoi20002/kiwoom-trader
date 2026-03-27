import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timedelta

class VirtualTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self.tree_active = None
        self.tree_history = None
        self.tree_events = None
        self.review_widgets = {}
        
        # UI State
        self.selected_date = datetime.now()
        
        self._setup_ui()
        self._render_virtual_positions()

    def _setup_ui(self):
        """고밀도 & 실데이터 연동 대시보드 레이아웃"""
        main_container = ttk.Frame(self.parent, padding="15")
        main_container.pack(fill=tk.BOTH, expand=True)

        # --- [1] 상단 계좌 요약 ---
        summary_f = ttk.LabelFrame(main_container, text=" 💰 계좌 요약 (Account Summary) ", padding="10")
        summary_f.pack(fill=tk.X, pady=(0, 15))
        
        self.vars = {
            "total": tk.StringVar(value="0"),
            "cash": tk.StringVar(value="0"),
            "stock": tk.StringVar(value="0"),
            "day": tk.StringVar(value="0 (0.00%)"),
            "cum": tk.StringVar(value="0.00%")
        }

        for i, (label, var, color) in enumerate([
            ("총 자산", "total", self.app.colors['primary']),
            ("보유 현금", "cash", None),
            ("주식 평가금", "stock", None),
            ("금일 손익", "day", "#BF616A"),
            ("누적 수익률", "cum", "#A3BE8C")
        ]):
            f = ttk.Frame(summary_f)
            f.pack(side=tk.LEFT, padx=15)
            ttk.Label(f, text=label, font=("Malgun Gothic", 8), foreground=self.app.colors['secondary']).pack(anchor=tk.W)
            ttk.Label(f, textvariable=self.vars[var], font=("Malgun Gothic", 10, "bold"), 
                      foreground=color if color else self.app.colors['fg']).pack(anchor=tk.W)

        # --- [2] 메인 드릴 다운 (Tri-Pane) ---
        h_paned = ttk.PanedWindow(main_container, orient=tk.HORIZONTAL)
        h_paned.pack(fill=tk.BOTH, expand=True)

        master_nb = ttk.Notebook(h_paned)
        h_paned.add(master_nb, weight=2)

        # [보유 중]
        active_f = ttk.Frame(master_nb, padding="5")
        master_nb.add(active_f, text=" 보유 중 (Active) ")
        self.tree_active = self._build_active_tree(active_f)

        # [종료/복기]
        history_f = ttk.Frame(master_nb, padding="5")
        master_nb.add(history_f, text=" 종료/복기 (History) ")
        self.tree_history = self._build_history_tree(history_f)

        detail_paned = ttk.PanedWindow(h_paned, orient=tk.VERTICAL)
        h_paned.add(detail_paned, weight=3)

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
        tree.column("Profit", width=130, anchor=tk.CENTER)
        tree.pack(fill=tk.BOTH, expand=True)
        tree.bind("<<TreeviewSelect>>", self._on_stock_select)
        return tree

    def _build_history_tree(self, parent):
        cols = ("Code", "Name", "Result", "Time")
        tree = ttk.Treeview(parent, columns=cols, show="headings", selectmode="browse")
        for col, text in zip(cols, ["코드", "종목명", "수익률", "종료시간"]):
            tree.heading(col, text=text)
            tree.column(col, width=90, anchor=tk.CENTER)
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

    def _init_snap_ui(self):
        self.snap_content = ttk.Frame(self.snap_f)
        self.lbl_empty = ttk.Label(self.snap_f, text="이벤트를 선택하면\n그때의 지표 상태가 표시됩니다.", 
                                  foreground=self.app.colors['secondary'], justify=tk.CENTER)
        self.lbl_empty.pack(expand=True)
        grid = ttk.Frame(self.snap_content)
        grid.pack(fill=tk.X)
        fields = [("매매시간", "time"), ("매매가격", "price"), ("지수필터", "market"), 
                  ("RSI(14)", "rsi"), ("MA배열", "ma"), ("체결강도", "power")]
        for i, (label, key) in enumerate(fields):
            ttk.Label(grid, text=label, font=("Malgun Gothic", 8), foreground=self.app.colors['secondary']).grid(row=i//2, column=(i%2)*2, sticky=tk.W, pady=3)
            val = ttk.Label(grid, text="-", font=("Malgun Gothic", 9, "bold"))
            val.grid(row=i//2, column=(i%2)*2+1, sticky=tk.W, padx=(10, 20), pady=3)
            self.review_widgets[key] = val

    def _render_virtual_positions(self):
        """실제 데이터(app.virtual_positions / history) 렌더링"""
        # 1. 트리 초기화
        for t in [self.tree_active, self.tree_history]:
            for i in t.get_children(): t.delete(i)
            
        # 2. 보유 종목 렌더링
        total_eval = 0
        for code, pos in self.app.virtual_positions.items():
            curr_p = self.app.realtime_data.get(code, {}).get('Price', pos['avg_price'])
            profit_pct = (curr_p / pos['avg_price'] - 1) * 100
            day_total_pct = pos.get('realized_pnl', 0.0) + profit_pct # 간소화된 합계
            eval_amt = curr_p * pos['qty']
            total_eval += eval_amt
            
            p_str = f"{day_total_pct:+.2f}% ({profit_pct:+.2f}%)"
            self.tree_active.insert("", tk.END, values=(code, pos['name'], pos['qty'], f"{pos['avg_price']:,}", f"{curr_p:,}", p_str))

        # 3. 종료 종목 렌더링
        for pos in self.app.virtual_history:
            last_event = pos['events'][-1] if pos.get('events') else {}
            self.tree_history.insert("", tk.END, values=(pos.get('code', '000000'), pos['name'], f"{pos.get('final_pnl_pct', 0):+.2f}%", last_event.get('time', '-')))

        # 4. 요약 정보 업데이트
        base_cash = 100000000 # Mock base
        total_assets = base_cash + total_eval
        self.vars["total"].set(f"{total_assets:,.0f}")
        self.vars["cash"].set(f"{base_cash:,.0f}")
        self.vars["stock"].set(f"{total_eval:,.0f}")

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
            for e in target_pos['events']:
                self.tree_events.insert("", tk.END, values=(e['time'], e['action'], f"{e['price']:,}", e['qty'], e['pnl'], e['reason']), tags=(e['action'],))

    def _on_event_select(self, event):
        sel = self.tree_events.selection()
        if not sel: return
        
        # 선택된 이벤트의 스냅샷 찾기
        tree = self.tree_events
        vals = tree.item(sel[0])['values']
        time_str = vals[0]
        
        # 현재 선택된 종목의 전체 이벤트에서 해당 시간의 스냅샷 추출
        # (실제 구현 시 데이터 멤버에 현재 선택된 종목 정보를 저장해두면 더 빠름)
        snapshot = None
        # ... (이벤트 데이터에서 스냅샷 추출 로직)
        # Mocking for now from values if data structure is complex, 
        # but in real use we pass the snapshot dict directly.
        
        self.lbl_empty.pack_forget()
        self.snap_content.pack(fill=tk.BOTH, expand=True)
        # 렌더링은 데이터 구조에 맞춰 연결

    def _setup_styles(self):
        self.tree_events.tag_configure("BUY", foreground="#BF616A")
        self.tree_events.tag_configure("SELL", foreground="#81A1C1")
        self.tree_events.tag_configure("ADD", foreground="#EBCB8B")

    def _render_real_data(self):
        self._render_virtual_positions()
