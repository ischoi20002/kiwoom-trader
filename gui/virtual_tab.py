import tkinter as tk
from tkinter import ttk, messagebox

class VirtualTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self._setup_ui()

    def _setup_ui(self):
        """가상 포지션 탭 레이아웃 설정"""
        vi_main_frame = ttk.Frame(self.parent, padding="15")
        vi_main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 컨트롤 영역
        ctrl_frame = ttk.Frame(vi_main_frame)
        ctrl_frame.pack(fill=tk.X, pady=(0, 15))
        
        ttk.Button(ctrl_frame, text="🗑️ 모든 가상 포지션 초기화", command=self.app._clear_virtual_positions).pack(side=tk.RIGHT, padx=5)
        ttk.Button(ctrl_frame, text="🔄 즉시 갱신", command=self._render_virtual_positions).pack(side=tk.RIGHT, padx=5)

        # 트리뷰 영역
        tree_frame = ttk.Frame(vi_main_frame)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        
        columns = ("Code", "Name", "AvgPrice", "Qty", "Price", "Return", "Amt", "Stage", "Days", "Reason")
        self.app.tree_virtual = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        
        headings = {
            "Code": "코드", "Name": "종목명", "AvgPrice": "평균단가",
            "Qty": "보유량", "Price": "현재가", "Return": "수익률(%)", 
            "Amt": "평가금액", "Stage": "단계", "Days": "보유일", "Reason": "진입사유"
        }
        for col, text in headings.items():
            self.app.tree_virtual.heading(col, text=text)
            
        col_widths = {
            "Code": 70, "Name": 110, "AvgPrice": 90, "Qty": 80,
            "Price": 90, "Return": 85, "Amt": 100, "Stage": 60, "Days": 60, "Reason": 180
        }
        for col, width in col_widths.items():
            self.app.tree_virtual.column(col, width=width, anchor=tk.E if col not in ["Code", "Name", "Reason"] else tk.W)
        
        yscrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.app.tree_virtual.yview)
        self.app.tree_virtual.configure(yscroll=yscrollbar.set)
        yscrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        if self.app.tree_virtual: self.app.tree_virtual.pack(side=tk.LEFT, expand=True, fill=tk.BOTH)

    def _render_virtual_positions(self):
        """가상 포지션 리스트를 트리뷰에 렌더링"""
        if not self.app.tree_virtual: return
        
        # 기존 항목 삭제
        for item in self.app.tree_virtual.get_children():
            self.app.tree_virtual.delete(item)
            
        total_pnl = 0.0
        total_invested = 0.0
        
        for i, (code, pos) in enumerate(self.app.virtual_positions.items()):
            try:
                # 현재가 가져오기 (실시간 정보 또는 마지막 가격)
                current_price = pos.get('current_price', pos['avg_price'])
                ret = (current_price / pos['avg_price'] - 1) * 100
                amount = current_price * pos['qty']
                
                total_invested += (pos['avg_price'] * pos['qty'])
                total_pnl += (amount - (pos['avg_price'] * pos['qty']))
                
                values = (
                    code, pos['name'], f"{int(pos['avg_price']):,}",
                    f"{pos['qty']:,}", f"{int(current_price):,}",
                    f"{ret:.2f}%", f"{int(amount):,}",
                    f"ST{pos['stage']}", pos.get('hold_days', 0),
                    pos.get('reason', '-')
                )
                
                perf_tag = "profit" if ret > 0 else "loss"
                row_tag = 'even' if i % 2 == 0 else 'odd'
                self.app.tree_virtual.insert("", tk.END, values=values, tags=(perf_tag, row_tag))
            except Exception as e:
                print(f"Error rendering position {code}: {e}")
                
        # 태그 색상 (v6.2 노르딕 테마 반영: 수익=빨강, 손실=파랑)
        self.app.tree_virtual.tag_configure("profit", foreground="#BF616A") # Soft Red
        self.app.tree_virtual.tag_configure("loss", foreground="#81A1C1")   # Soft Blue
        self.app.tree_virtual.tag_configure('odd', background='#242933')
        self.app.tree_virtual.tag_configure('even', background='#2E3440')
