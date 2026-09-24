# -*- coding: utf-8 -*-
import queue
import tkinter as tk
import sys
import threading
import calendar
from collections import deque
from datetime import datetime
from typing import Any, Deque, List, Optional, Union

class GuiUtils:
    """GUI 관련 유틸리티 및 전용 로깅 클래스"""
    # after 한 건당 ScrolledText 에 몇 줄까지 한꺼번에 붙일지 (Tk 메인루프 점유 완화)
    _LOG_DRAIN_MAX_LINES: int = 200
    # 첫 로그 이후 UI에 반영하기까지 최대 대기(ms) — 너무 짧으면 after 폭주, 너무 길면 체감 지연
    _LOG_DEBOUNCE_MS: int = 90
    # 꼬리 처리만 할 때 추가 스케줄 간격(ms)
    _LOG_TAIL_POLL_MS: int = 40
    # 모바일 Status API용 최근 로그 줄 수
    _RECENT_LOG_MAX: int = 400

    def __init__(self, log_area=None, root=None):
        self.log_area = log_area
        self.root = root
        self._main_thread_id = threading.get_ident()
        self._log_msg_count = 0
        self._log_q: "queue.SimpleQueue[str]" = queue.SimpleQueue()
        self._log_flush_after_id: Optional[Union[str, int]] = None
        self._recent_log_lock = threading.Lock()
        self._recent_log_lines: Deque[str] = deque(maxlen=self._RECENT_LOG_MAX)
        self._icons_strip = (
            "🚀",
            "🔍",
            "✅",
            "❌",
            "⚠️",
            "📊",
            "ℹ️",
            "🎯",
            "🔎",
            "⏹️",
            "⚙️",
            "📡",
            "🔄",
            "🛡️",
            "📦",
            "📥",
            "🔥",
            "🚨",
            "🔔",
            "💿",
            "💰",
            "📈",
            "👁️",
            "📅",
            "🟢",
            "⚪",
        )
        self._icon_triggers = ("🔍", "❌", "⚠️", "📊", "✅", "🚀", "ℹ️", "🎯")
        # 메인 스레드에서 주기적으로 큐를 배출하는 폴러를 시작합니다.
        # (비메인 스레드는 Tk API를 직접 호출하지 않음)
        try:
            self._ensure_log_poller()
        except Exception:
            pass

    def log(self, message: Any) -> None:
        """
        스레드 안전 로깅.

        기존: 비메인 스레드마다 root.after(0, insert 1줄) → 실시간 틱 시 after 폭주로
        탭 전환·클릭이 한동안 먹지 않는 현상 발생.
        개선: 메시지는 큐에 쌓고, 메인 스레드에서 일정 간격으로 묶어서 insert (see 1회).

        비메인 스레드는 큐에만 적재합니다. 메인 스레드 폴러(90ms)가 배출하며,
        빈 큐에서도 폴러를 유지해 M1Poll 말분 요약 등 백그라운드 로그가
        대시보드에 남도록 합니다.
        """
        try:
            self._log_q.put(str(message))
        except Exception:
            try:
                print(message)
            except Exception:
                pass
            return

        try:
            is_main_thread = threading.get_ident() == self._main_thread_id
        except Exception:
            is_main_thread = True

        if not is_main_thread:
            # 백그라운드에서는 Tk를 전혀 호출하지 않습니다.
            # 메인 스레드의 주기 폴러가 큐를 배출하므로, 여기서 root.after를
            # 호출하면 메인 스레드가 portfolio.lock을 기다릴 때 교착될 수 있습니다.
            return

        self._request_log_flush()

    def _ensure_log_poller(self) -> None:
        """메인 스레드 주기 폴러를 1건 유지합니다."""
        try:
            r = self.root
            if r is None:
                return
            if threading.get_ident() != self._main_thread_id:
                return
            if self._log_flush_after_id is None:
                self._log_flush_after_id = r.after(
                    self._LOG_DEBOUNCE_MS, self._drain_log_queue_on_main
                )
        except Exception:
            pass

    def _request_log_flush(self) -> None:
        """Tk 메인 루프에 로그 배출 작업을 한 건만 예약합니다."""
        try:
            r = self.root
            if r is None:
                self._drain_log_queue_sync()
                return
            try:
                if not r.winfo_exists():
                    self._drain_log_queue_sync()
                    return
            except Exception:
                self._drain_log_queue_sync()
                return
            if self._log_flush_after_id is not None:
                return
            self._log_flush_after_id = r.after(
                self._LOG_DEBOUNCE_MS, self._drain_log_queue_on_main
            )
        except Exception:
            try:
                self._drain_log_queue_sync()
            except Exception:
                pass

    def _drain_log_queue_on_main(self) -> None:
        """메인 스레드: 큐에서 최대 N건 꺼내 한 번에 위젯에 반영 후, 남으면 짧게 재예약."""
        self._log_flush_after_id = None
        try:
            raw_batch: List[str] = []
            for _ in range(self._LOG_DRAIN_MAX_LINES):
                try:
                    raw_batch.append(self._log_q.get_nowait())
                except queue.Empty:
                    break
            if not raw_batch:
                # 빈 큐에서 조기 return 시 폴러가 끊기면 백그라운드 로그가 영구히 큐에만 남음
                try:
                    self._ensure_log_poller()
                except Exception:
                    pass
                return
            self._apply_log_batch(raw_batch)
        except Exception as e:
            try:
                print(f"[GuiUtils] 로그 배출 실패: {e}")
            except Exception:
                pass
        try:
            if self._log_q.empty():
                # 큐가 비어도 폴러는 유지(비메인 로그 enqueue를 놓치지 않기 위함)
                self._ensure_log_poller()
                return
            r = self.root
            if r is not None:
                try:
                    if r.winfo_exists() and self._log_flush_after_id is None:
                        self._log_flush_after_id = r.after(
                            self._LOG_TAIL_POLL_MS, self._drain_log_queue_on_main
                        )
                except Exception:
                    pass
        except Exception:
            pass

    def _drain_log_queue_sync(self) -> None:
        """root 없음/종료 시: 큐를 콘솔·링버퍼로 비웁니다."""
        while True:
            try:
                m = self._log_q.get_nowait()
            except queue.Empty:
                break
            try:
                line = self._format_log_line(m)
                self._remember_log_line(line)
                print(line.rstrip("\n"))
            except Exception:
                try:
                    print(m)
                except Exception:
                    pass

    def _remember_log_line(self, formatted: str) -> None:
        """대시보드와 동일 포맷 줄을 링버퍼에 보관(모바일 API용)."""
        try:
            line = str(formatted).rstrip("\r\n")
            if not line.strip():
                return
            with self._recent_log_lock:
                self._recent_log_lines.append(line)
        except Exception:
            pass

    def get_recent_log_lines(self, limit: int = 100) -> List[str]:
        """최근 로그 줄(오래된 → 최신). 스레드 안전 복사."""
        try:
            lim = max(1, min(int(limit), int(self._RECENT_LOG_MAX)))
        except (TypeError, ValueError):
            lim = 100
        try:
            with self._recent_log_lock:
                lines = list(self._recent_log_lines)
            if len(lines) > lim:
                return lines[-lim:]
            return lines
        except Exception:
            return []

    def _format_log_line(self, message: str) -> str:
        """타임스탬프·이모지 제거 등 한 줄 포맷(위젯 삽입용)."""
        now = datetime.now().strftime("%H:%M:%S")
        prefix = ""
        msg_str = str(message)
        if msg_str.startswith("\n"):
            prefix = "\n"
            msg_str = msg_str.lstrip("\n")
        for icon in self._icons_strip:
            msg_str = msg_str.replace(icon, "").strip()
        return f"{prefix}[{now}] {msg_str}\n"

    def _maybe_mirror_to_console(self, raw: str) -> None:
        try:
            if any(icon in raw for icon in self._icon_triggers):
                try:
                    print(raw)
                except UnicodeEncodeError:
                    safe_msg = (
                        raw.replace("🔍", "[DEBUG]")
                        .replace("❌", "[ERROR]")
                        .replace("⚠️", "[WARN]")
                        .replace("📊", "[STATS]")
                        .replace("✅", "[OK]")
                    )
                    print(safe_msg.encode("ascii", "ignore").decode("ascii"))
        except Exception:
            pass

    def _apply_log_batch(self, raw_batch: List[str]) -> None:
        """포맷된 여러 줄을 한 번의 insert 로 반영하고, 길이 상한 시 상단 삭제."""
        formatted: List[str] = []
        for raw in raw_batch:
            try:
                line = self._format_log_line(raw)
                self._remember_log_line(line)
                formatted.append(line)
            except Exception:
                try:
                    self._remember_log_line(str(raw))
                except Exception:
                    pass
                formatted.append(str(raw) + ("\n" if not str(raw).endswith("\n") else ""))

        if not self.log_area or not self.log_area.winfo_exists():
            for raw in raw_batch:
                self._maybe_mirror_to_console(raw)
            for line in formatted:
                try:
                    print(line.rstrip("\n"))
                except Exception:
                    pass
            return
        try:
            block = "".join(formatted)
            self.log_area.insert(tk.END, block)
            for raw in raw_batch:
                self._maybe_mirror_to_console(raw)

            self._log_msg_count += len(raw_batch)
            # 줄 수가 많아지면 상단 삭제(배치마다 검사 — index 조회는 가벼운 편)
            try:
                last_line = int(self.log_area.index("end-1c").split(".")[0])
                if last_line > 2000:
                    self.log_area.delete("1.0", "500.0")
            except Exception:
                pass
            self.log_area.see(tk.END)
        except Exception:
            for raw in raw_batch:
                try:
                    print(raw)
                except Exception:
                    pass

    def _do_log_safe(self, message: str) -> None:
        """
        레거시·직접 호출 호환: 단일 메시지 즉시 반영(가능하면 log() 경유 권장).
        """
        self._apply_log_batch([str(message)])

    def get_criteria_text(self, settings, format_type="ui"):
        s = settings
        univ_label = "KOSPI"
        b = "▶ " if format_type == "ui" else "■ "
        line_break = "\n" if format_type == "ui" else "\n"
        return (
            f"{b}{univ_label} 편입  |  {b}{s.get('min_trde_val', 40)}억+  |  {b}수렴 {int(s.get('ma_convergence', 3.0))}%  |  {b}선정RSI {s.get('sel_rsi_min', 40)}~{s.get('sel_rsi_max', 70)}{line_break}"
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
