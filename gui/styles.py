import tkinter as tk
from tkinter import ttk

class NordicStyle:
    """[v25.7] Nordic Professional 테마 및 위젯 스타일 중앙 관리 클래스"""
    def __init__(self, root):
        self.root = root
        self.colors = {
            'bg': '#2E3440',      # 전체 배경 (Polar Night)
            'card': '#3B4252',    # 카드/탭 배경 (Lighter Slate)
            'fg': '#ECEFF4',      # 기본 텍스트 (Snow Storm)
            'primary': '#88C0D0', # 포인트 컬러 (Frost Blue)
            'secondary': '#81A1C1', # 보조 컬러 (Slate Blue)
            'success': '#A3BE8C', # 상승/성공 (Moss Green)
            'danger': '#BF616A',  # 하락/위험 (Aurora Red)
            'border': '#4C566A',  # 경계선
            'white': '#FFFFFF'    # 백색
        }
        self.apply()

    def apply(self):
        style = ttk.Style()
        style.theme_use('clam')
        
        # 메인 배경 및 전연 폰트
        self.root.configure(bg=self.colors['bg'])
        style.configure(".", font=("Malgun Gothic", 9), 
                        background=self.colors['bg'], foreground=self.colors['fg'])
        
        # Notebook 스타일
        style.configure("TNotebook", background=self.colors['bg'], borderwidth=0)
        style.configure("TNotebook.Tab", padding=[22, 12], font=("Malgun Gothic", 10, "bold"), 
                        background="#434C5E", foreground="#81A1C1", borderwidth=1, bordercolor=self.colors['bg'])
        style.map("TNotebook.Tab", 
                  background=[("selected", self.colors['card'])],
                  foreground=[("selected", self.colors['primary'])])

        # LabelFrame 스타일
        style.configure("TLabelframe", background=self.colors['card'], bordercolor=self.colors['border'], 
                        relief=tk.FLAT, borderwidth=1)
        style.configure("TLabelframe.Label", font=("Malgun Gothic", 10, "bold"), 
                        foreground=self.colors['primary'], background=self.colors['card'])

        # 버튼 스타일
        style.configure("TButton", padding=[14, 8], font=("Malgun Gothic", 9, "bold"),
                        background="#4C566A", foreground=self.colors['fg'], borderwidth=1, relief=tk.RAISED)
        style.map("TButton", 
                  background=[("active", "#5E81AC"), ("pressed", "#434C5E")],
                  relief=[("pressed", tk.FLAT)])
        
        style.configure("Action.TButton", foreground=self.colors['bg'], background=self.colors['primary'], borderwidth=1)
        style.map("Action.TButton", background=[("active", "#B3D9E6"), ("disabled", "#4C566A")])
        
        style.configure("Success.TButton", foreground=self.colors['bg'], background=self.colors['success'], borderwidth=1)
        style.map("Success.TButton", background=[("active", "#B4CF9E")])
        
        style.configure("Accent.TButton", foreground=self.colors['bg'], background=self.colors['primary'], borderwidth=1)
        style.map("Accent.TButton", background=[("active", "#B3D9E6"), ("disabled", "#4C566A")])
                   
        style.configure("Toggle.TButton", foreground=self.colors['fg'], background=self.colors['danger'], borderwidth=1)
        style.map("Toggle.TButton", background=[("active", "#D08770")])
                   
        style.configure("Subtle.TButton", foreground=self.colors['secondary'], background=self.colors['card'], borderwidth=1)
        style.map("Subtle.TButton", background=[("active", self.colors['bg'])], foreground=[("active", self.colors['primary'])])

        # 트리뷰 스타일
        style.configure("Treeview", background="#242933", foreground=self.colors['fg'], 
                        fieldbackground="#242933", rowheight=32, borderwidth=0)
        style.configure("Treeview.Heading", background="#434C5E", foreground=self.colors['primary'], 
                        font=("Malgun Gothic", 9, "bold"), borderwidth=1, relief=tk.FLAT)
        style.map("Treeview", background=[("selected", "#88C0D0")], foreground=[("selected", "#2E3440")])

        # 입력 위젯 스타일
        style.configure("TEntry", fieldbackground="#242933", foreground=self.colors['fg'], 
                        insertcolor=self.colors['primary'], borderwidth=1, relief=tk.FLAT)
        style.map("TEntry", fieldbackground=[("readonly", "#2E3440")], foreground=[("readonly", "#81A1C1")])
        
        style.configure("TCheckbutton", background=self.colors['bg'], foreground=self.colors['fg'], font=("Malgun Gothic", 10))
        style.map("TCheckbutton", background=[("active", self.colors['bg'])], foreground=[("active", self.colors['primary'])])

        # 스크롤바 스타일링
        style.layout("Vertical.TScrollbar", 
                     [('Vertical.Scrollbar.trough', {'children': [('Vertical.Scrollbar.thumb', {'expand': '1', 'sticky': 'nswe'})], 'sticky': 'ns'})])
        style.configure("Vertical.TScrollbar", gripcount=0, background="#434C5E", 
                        troughcolor=self.colors['bg'], bordercolor=self.colors['bg'], arrowcolor="#81A1C1", thickness=12)
        style.map("Vertical.TScrollbar", background=[("active", "#4C566A")])
