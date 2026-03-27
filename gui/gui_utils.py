# -*- coding: utf-8 -*-
import tkinter as tk
import sys
import threading
import calendar
from datetime import datetime

class GuiUtils:
    """GUI 관련 유틸리티 및 전용 로깅 클래스"""
    def __init__(self, log_area=None, root=None):
        self.log_area = log_area
        self.root = root
        self._log_msg_count = 0

    def log(self, message):
        """방대해진 로그를 처리하기 위한 고성능 스레드 안전 로깅"""
        if threading.current_thread() is not threading.main_thread():
            if self.root:
                self.root.after(0, lambda: self._do_log_safe(message))
        else:
            self._do_log_safe(message)

    def _do_log_safe(self, message):
        if not self.log_area or not self.log_area.winfo_exists():
            try:
                print(message)
            except UnicodeEncodeError:
                safe_msg = message.replace("🔍", "[DEBUG]").replace("❌", "[ERROR]").replace("⚠️", "[WARN]").replace("📊", "[STATS]").replace("✅", "[OK]")
                try: print(safe_msg.encode(sys.stdout.encoding, errors='ignore').decode(sys.stdout.encoding))
                except: print(message.encode('ascii', errors='ignore').decode('ascii'))
            return
        
        try:
            # [v3.6] 타임스탬프 및 메시지 정돈 (이모지 제거 포함)
            now = datetime.now().strftime("%H:%M:%S")
            prefix = ""
            msg_str = str(message)
            if msg_str.startswith("\n"):
                prefix = "\n"
                msg_str = msg_str.lstrip("\n")
            
            # 이모지 및 특수 기호 제거 (텍스트형으로 변환)
            icons = ["🚀", "🔍", "✅", "❌", "⚠️", "📊", "ℹ️", "🎯", "🔎", "⏹️", "⚙️"]
            for icon in icons:
                msg_str = msg_str.replace(icon, "").strip()
            
            clean_msg = f"{prefix}[{now}] {msg_str}"
            
            self.log_area.insert(tk.END, f"{clean_msg}\n")
            
            # 중요 디버그 메시지는 터미널에도 출력
            icon_triggers = ["🔍", "❌", "⚠️", "📊", "✅", "🚀", "ℹ️", "🎯"]
            if any(icon in message for icon in icon_triggers):
                try:
                    print(message)
                except UnicodeEncodeError:
                    # 유니코드 에러 시 대체 출력
                    safe_msg = message.replace("🔍", "[DEBUG]").replace("❌", "[ERROR]").replace("⚠️", "[WARN]").replace("📊", "[STATS]").replace("✅", "[OK]")
                    print(safe_msg.encode('ascii', 'ignore').decode('ascii'))
            
            # 성능: 대략 50건마다 길이 체크
            self._log_msg_count += 1
            if self._log_msg_count % 50 == 0:
                last_line = int(self.log_area.index('end-1c').split('.')[0])
                if last_line > 2000:
                    self.log_area.delete("1.0", "500.0")
            
            self.log_area.see(tk.END)
        except:
            print(message)

    def get_criteria_text(self, settings, format_type="ui"):
        s = settings
        b = "▶ " if format_type == "ui" else "■ "
        line_break = "\n" if format_type == "ui" else "\n"
        return (
            f"{b}KOSPI 200  |  {b}{s.get('min_trde_val', 40)}억+  |  {b}수렴 {int(s.get('ma_convergence', 0.03)*100)}%  |  {b}선정RSI {s.get('sel_rsi_min', 40)}~{s.get('sel_rsi_max', 70)}{line_break}"
            f"{b}분출 {int(s.get('vol_lookback', 10))}일내 {int(s.get('vol_ratio', 1.5)*100)}%+  |  {b}매수RSI {s.get('buy_rsi_min', 40)} ~ [A:{s.get('buy_rsi_max_a', 58.0)} / B:{s.get('buy_rsi_max_b', 65.0)}]"              
        )

class CustomDatePicker(tk.Toplevel):
    """[v3.1] Nordic 테마 기반 커스텀 날짜 선택 팝업"""
    def __init__(self, parent, target_entry, initial_date=None, colors=None):
        super().__init__(parent)
        self.target_entry = target_entry
        self.colors = colors or {'bg': '#2E3440', 'card': '#3B4252', 'fg': '#ECEFF4', 'primary': '#88C0D0'}
        
        self.title("날짜 선택")
        self.configure(bg=self.colors['bg'])
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        # 위치 최적화 (엔트리 바로 아래)
        try:
            x = target_entry.winfo_rootx()
            y = target_entry.winfo_rooty() + target_entry.winfo_height() + 2
            self.geometry(f"+{x}+{y}")
        except: pass

        # 초기 날짜 설정
        if initial_date:
            try: self.curr_date = datetime.strptime(initial_date, "%Y-%m-%d")
            except: self.curr_date = datetime.now()
        else:
            self.curr_date = datetime.now()

        self.year = self.curr_date.year
        self.month = self.curr_date.month
        calendar.setfirstweekday(calendar.SUNDAY)
        self._setup_ui()

    def _setup_ui(self):
        main_frame = tk.Frame(self, bg=self.colors['bg'], padx=10, pady=10)
        main_frame.pack()

        # 헤더 (년/월 네비게이션)
        header = tk.Frame(main_frame, bg=self.colors['bg'])
        header.pack(fill=tk.X, pady=(0, 10))
        
        btn_style = {"bg": "#4C566A", "fg": "white", "relief": tk.FLAT, "padx": 5, "font": ("Malgun Gothic", 8, "bold")}
        
        tk.Button(header, text="◀", command=self._prev_month, **btn_style).pack(side=tk.LEFT)
        self.lbl_month = tk.Label(header, text=f"{self.year}년 {self.month}월", bg=self.colors['bg'], 
                                 fg=self.colors['primary'], font=("Malgun Gothic", 10, "bold"), width=12)
        self.lbl_month.pack(side=tk.LEFT, expand=True)
        tk.Button(header, text="▶", command=self._next_month, **btn_style).pack(side=tk.LEFT)
        
        # 요일 헤더
        days_frame = tk.Frame(main_frame, bg=self.colors['bg'])
        days_frame.pack()
        days = ["일", "월", "화", "수", "목", "금", "토"]
        day_colors = ["#BF616A", "#D8DEE9", "#D8DEE9", "#D8DEE9", "#D8DEE9", "#D8DEE9", "#81A1C1"]
        
        for i, d in enumerate(days):
            tk.Label(days_frame, text=d, width=4, bg=self.colors['bg'], font=("Malgun Gothic", 9),
                    fg=day_colors[i]).grid(row=0, column=i, pady=5)
            
        self.body_frame = tk.Frame(main_frame, bg=self.colors['bg'])
        self.body_frame.pack()
        self._draw_calendar()

    def _draw_calendar(self):
        for widget in self.body_frame.winfo_children():
            widget.destroy()
            
        self.lbl_month.config(text=f"{self.year}년 {self.month}월")
        
        cal = calendar.monthcalendar(self.year, self.month)
        today = datetime.now()
        
        for r, week in enumerate(cal):
            for c, day in enumerate(week):
                if day == 0: continue
                
                # 오늘 날짜 강조
                is_today = (self.year == today.year and self.month == today.month and day == today.day)
                fg_color = "#ECEFF4"
                if c == 0: fg_color = "#BF616A" # 일요일
                elif c == 6: fg_color = "#81A1C1" # 토요일
                
                bg_color = "#3B4252"
                if is_today: bg_color = "#5E81AC" # 오늘 하이라이트
                
                btn = tk.Button(self.body_frame, text=str(day), width=3, bg=bg_color, fg=fg_color,
                               relief=tk.FLAT, font=("Malgun Gothic", 9),
                               command=lambda d=day: self._select_date(d))
                btn.grid(row=r, column=c, padx=1, pady=1)
                
                # 호버 효과
                btn.bind("<Enter>", lambda e, b=btn: b.config(bg="#4C566A"))
                btn.bind("<Leave>", lambda e, b=btn, bg=bg_color: b.config(bg=bg))

    def _prev_month(self):
        self.month -= 1
        if self.month == 0:
            self.month = 12
            self.year -= 1
        self._draw_calendar()

    def _next_month(self):
        self.month += 1
        if self.month == 13:
            self.month = 1
            self.year += 1
        self._draw_calendar()

    def _select_date(self, day):
        date_str = f"{self.year}-{self.month:02d}-{day:02d}"
        self.target_entry.delete(0, tk.END)
        self.target_entry.insert(0, date_str)
        self.destroy()

class ToolTip:
    """[v3.5] GUI 요소에 툴팁을 표시하는 전용 클래스"""
    def __init__(self, widget, text_func):
        self.widget = widget
        self.text_func = text_func
        self.tip_window = None
        self.widget.bind("<Enter>", self.show_tip)
        self.widget.bind("<Leave>", self.hide_tip)
        self.widget.bind("<Motion>", self.update_tip)

    def show_tip(self, event=None):
        if self.tip_window: return
        self.tip_window = tk.Toplevel(self.widget)
        self.tip_window.wm_overrideredirect(1)
        self.update_tip(event)

    def update_tip(self, event=None):
        text = self.text_func(event) if callable(self.text_func) else self.text_func
        if not text:
            self.hide_tip()
            return
            
        if not self.tip_window:
            self.tip_window = tk.Toplevel(self.widget)
            self.tip_window.wm_overrideredirect(1)
            
        for child in self.tip_window.winfo_children(): child.destroy()
        label = tk.Label(self.tip_window, text=text, justify=tk.LEFT,
                      background="#FFFFE0", relief=tk.SOLID, borderwidth=1,
                      font=("Malgun Gothic", "9", "normal"), padx=5, pady=3)
        label.pack()
        
        x = self.widget.winfo_pointerx() + 15
        y = self.widget.winfo_pointery() + 15
        self.tip_window.wm_geometry(f"+{x}+{y}")

    def hide_tip(self, event=None):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None
