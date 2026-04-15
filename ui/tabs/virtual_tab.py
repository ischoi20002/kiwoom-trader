import tkinter as tk
import threading
import os
import importlib
import time
from tkinter import ttk, messagebox
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
import config
from utils.fee_rates import fee_decimal_rates_live_trading

class VirtualTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self.vars = {}
        # [v4.5.3] 실시간 보유: ttk.Treeview (행 iid=6자리 코드, 종료/주문과 동일 패턴)
        self.tree_active: Optional[ttk.Treeview] = None
        self.tree_history = None
        self.tree_orders = None
        self.lbl_mode_badge = None
        self.btn_env = None  # 운영 버튼은 대시보드로 이전됨(레거시 참조용)
        self.lbl_mock_result = None
        # 종목별 REST 시세 폴백 캐시 (API 과다·429 방지)
        self._fallback_quote_cache: Dict[str, Tuple[float, float]] = {}
        # 주문 로그: 조회 중인 날짜(네비게이션). 기본은 오늘.
        self._order_log_view_date: date = date.today()
        self._order_log_date_var: Optional[tk.StringVar] = None
        self._btn_order_log_prev: Optional[ttk.Button] = None
        self._btn_order_log_next: Optional[ttk.Button] = None
        self.btn_narrative_report: Optional[ttk.Button] = None
        self._narrative_report_busy: bool = False

        self._setup_ui()
        self._render_virtual_positions()
        try:
            self.app.trading_controller._ensure_virtual_positions_monitor_info()
            self.app.monitor_info = dict(self.app.trading_controller.monitor_info or {})
        except Exception:
            pass
        try:
            self.app._maybe_start_account_sync_timer()
        except Exception:
            pass

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
            "cum": tk.StringVar(value="0 (+0.00%)")
        })

        for label, var, color in [
            ("총 자산", "total", self.app.colors['primary']),
            ("예수금", "cash", None),
            ("D+1 예상", "d1_cash", self.app.colors['primary']),
            ("D+2 예상", "d2_cash", self.app.colors['primary']),
            ("주식 평가금", "stock", None),
            ("보유주식 손익(미청산)", "day", "#BF616A"),
            ("누적 수익(종료+보유)", "cum", "#A3BE8C")
        ]:
            f = ttk.Frame(summary_f)
            f.pack(side=tk.LEFT, padx=(0, 15))
            ttk.Label(f, text=label, font=("Malgun Gothic", 8), foreground=self.app.colors['primary']).pack(anchor=tk.W)
            ttk.Label(f, textvariable=self.vars[var], font=("Malgun Gothic", 10, "bold"), 
                      foreground=color if color else self.app.colors['fg']).pack(anchor=tk.W)

        # [v3.9.1] 모의투자 잔고 조회 결과 라벨
        self.lbl_mock_result = ttk.Label(summary_f, text="", font=("Malgun Gothic", 9), foreground="#A3BE8C")
        self.lbl_mock_result.pack(side=tk.LEFT, padx=10, pady=(15, 0))

        # 동기화·리포트·서버 전환 등은 대시보드 탭 「운영 · 계좌」로 이전했습니다.
        ttk.Label(
            header_f,
            text="계좌 동기화·종합리포트·일일결산·관심목록·서버 전환 → 대시보드 탭",
            font=("Malgun Gothic", 8),
            foreground="#787c99",
        ).pack(side=tk.RIGHT, padx=(8, 0), pady=(12, 0))

        # --- [2] 메인 레이아웃 (3단 수직 분할) ---
        v_paned = ttk.PanedWindow(main_container, orient=tk.VERTICAL)
        v_paned.pack(fill=tk.BOTH, expand=True)

        # 모의/실전별 저장 파일 분리 — config.USE_MOCK 과 표시 라벨 일치
        try:
            _pm = "모의" if getattr(config, "USE_MOCK", True) else "실전"
        except Exception:
            _pm = "모의"

        # 1. 보유 현황
        active_f = ttk.LabelFrame(
            v_paned,
            text=f" 💰 실시간 보유 현황 · {_pm} ",
            padding="5",
        )
        v_paned.add(active_f, weight=4)
        self._build_active_holdings_tree(active_f)

        # 2. 매매 종료
        history_f = ttk.LabelFrame(
            v_paned,
            text=f" ✅ 매매 종료 내역 · {_pm} ",
            padding="5",
        )
        v_paned.add(history_f, weight=3)
        self.tree_history = self._build_history_tree(history_f)

        # 3. 주문 내역 — 제목·날짜 네비 한 줄(세로 공간 절약)
        v_paned.add(self._build_order_log_section(v_paned, _pm), weight=3)

        self._setup_styles()

    def _build_active_holdings_tree(self, parent: ttk.Widget) -> None:
        """실시간 보유: 매매종료·주문과 동일한 Treeview 패턴 (스크롤·줄무늬·태그)."""
        try:
            cols = (
                "Code",
                "Name",
                "Qty",
                "AvgP",
                "BuyIn",
                "CurP",
                "Eval",
                "PnL",
                "PnLPct",
                "Entry",
            )
            tree = ttk.Treeview(
                parent, columns=cols, show="headings", selectmode="browse"
            )
            heads = [
                "코드",
                "종목명",
                "수량",
                "평단가",
                "매입가",
                "현재가",
                "평가금",
                "손익금",
                "수익률",
                "진입일시",
            ]
            widths = (72, 120, 56, 88, 96, 88, 100, 100, 72, 140)
            # 진입일시(Entry)는 중앙 정렬
            anchors = (tk.W, tk.W, tk.E, tk.E, tk.E, tk.E, tk.E, tk.E, tk.E, tk.CENTER)
            for col, text, w, an in zip(cols, heads, widths, anchors):
                tree.heading(col, text=text)
                tree.column(col, width=w, anchor=an)

            sb = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=tree.yview)
            tree.configure(yscrollcommand=sb.set)
            tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            sb.pack(side=tk.RIGHT, fill=tk.Y)
            self.tree_active = tree
        except Exception as e:
            try:
                print(f"[UI Error] _build_active_holdings_tree: {e}")
            except Exception:
                pass
            self.tree_active = None

    def _build_history_tree(self, parent):
        cols = (
            "Time",
            "Code",
            "Name",
            "Qty",
            "BuyP",
            "SellP",
            "Fees",
            "PnL",
            "Profit",
            "Reason",
        )
        tree = ttk.Treeview(parent, columns=cols, show="headings", selectmode="browse")
        heads = [
            "종료일시",
            "코드",
            "종목명",
            "수량",
            "매수가",
            "매도가",
            "수수료",
            "손익금",
            "수익률",
            "사유",
        ]
        for col, text in zip(cols, heads):
            tree.heading(col, text=text)
            tree.column(col, width=85, anchor=tk.CENTER)
        tree.column("Time", width=152, anchor=tk.CENTER)
        tree.column("Name", width=110, anchor=tk.W)
        tree.column("BuyP", anchor=tk.E)
        tree.column("SellP", anchor=tk.E)
        tree.column("Fees", width=78, anchor=tk.E)
        tree.column("PnL", width=90, anchor=tk.E)
        tree.column("Profit", anchor=tk.E)
        tree.column("Reason", width=120, anchor=tk.CENTER)
        
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        return tree

    def _build_order_tree(self, parent):
        cols = (
            "Time",
            "Code",
            "Name",
            "Side",
            "Qty",
            "OrderP",
            "FilledP",
            "VPOrd",
            "BAOrd",
            "Status",
            "Reason",
        )
        # LabelFrame 등 상위는 pack — 트리+스크롤은 내부 전용 프레임에서만 grid (pack/grid 혼용 금지)
        wrap = ttk.Frame(parent)
        wrap.pack(fill=tk.BOTH, expand=True)
        wrap.grid_rowconfigure(0, weight=1)
        wrap.grid_columnconfigure(0, weight=1)

        tree = ttk.Treeview(wrap, columns=cols, show="headings")
        display_names = [
            "일시",
            "코드",
            "종목명",
            "구분",
            "수량",
            "주문가",
            "체결가",
            "VP",
            "호가",
            "상태",
            "사유",
        ]
        for col, text in zip(cols, display_names):
            tree.heading(col, text=text)
            w = 88
            if col == "Time":
                w = 138
            elif col == "BAOrd":
                w = 200
            elif col == "VPOrd":
                w = 72
            elif col == "Reason":
                w = 120
            tree.column(col, width=w, anchor=tk.CENTER)
        tree.column("Name", width=100, anchor=tk.W)
        tree.column("OrderP", anchor=tk.E)
        tree.column("FilledP", anchor=tk.E)
        tree.column("VPOrd", anchor=tk.E)
        tree.column("Reason", width=120, anchor=tk.CENTER)
        tree.column("BAOrd", anchor=tk.E)

        # 호가(매도/매수/비율) 열이 넓어 가로 스크롤 제공
        scroll_y = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=tree.yview)
        scroll_x = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        return tree

    def _build_order_log_section(
        self, paned_parent: ttk.Widget, mode_label: str
    ) -> ttk.LabelFrame:
        """
        주문 로그: 상단 한 줄에 왼쪽 제목 + 오른쪽 끝 날짜 네비(이전/다음/오늘).
        LabelFrame 라벨 슬롯(labelwidget)은 가로로 늘어나지 않아 본문 상단 프레임에 배치한다.
        """
        # text 비움: labelwidget은 라벨 슬롯만 차지해 가로로 늘어나지 않아 네비가 우측 끝에 못 붙음.
        # 본문 상단에 fill=X 헤더를 두고 제목은 왼쪽, 날짜 네비는 오른쪽 끝(pack RIGHT)으로 둔다.
        order_f = ttk.LabelFrame(paned_parent, padding="5", text="")
        try:
            hdr = ttk.Frame(order_f)
            hdr.pack(fill=tk.X)
            # 매매 종료 내역 등 상단 LabelFrame 제목과 동일: TLabelframe.Label (styles.py)
            ttk.Label(
                hdr,
                text=f" 📄 주문 로그 · {mode_label} ",
                style="TLabelframe.Label",
            ).pack(side=tk.LEFT)
            nav = ttk.Frame(hdr)
            self._btn_order_log_prev = ttk.Button(
                nav,
                text="◀ 이전",
                width=9,
                command=self._order_log_prev_day,
            )
            self._btn_order_log_prev.pack(side=tk.LEFT, padx=(0, 4))
            self._order_log_date_var = tk.StringVar(value="")
            ttk.Label(
                nav,
                textvariable=self._order_log_date_var,
                style="TLabelframe.Label",
            ).pack(side=tk.LEFT, padx=8)
            self._btn_order_log_next = ttk.Button(
                nav,
                text="다음 ▶",
                width=9,
                command=self._order_log_next_day,
            )
            self._btn_order_log_next.pack(side=tk.LEFT, padx=(4, 0))
            ttk.Button(
                nav,
                text="오늘",
                width=8,
                command=self._order_log_today,
            ).pack(side=tk.LEFT, padx=(12, 0))
            nav.pack(side=tk.RIGHT)
            self.tree_orders = self._build_order_tree(order_f)
            self._sync_order_log_date_label()
        except Exception as e:
            try:
                print(f"[UI Error] _build_order_log_section: {e}")
            except Exception:
                pass
            try:
                self.tree_orders = self._build_order_tree(order_f)
            except Exception:
                self.tree_orders = None
        return order_f

    def _order_log_calendar_dates_with_orders(self) -> List[date]:
        """DB에 주문 이력이 있는 날짜(오름차순)."""
        try:
            dm = getattr(self.app, "data_manager", None)
            if dm is None:
                return []
            fn = getattr(dm, "get_distinct_order_calendar_dates", None)
            if callable(fn):
                return list(fn() or [])
            return []
        except Exception:
            return []

    def _order_log_prev_date_with_orders(self, view_d: date) -> Optional[date]:
        """view_d 보다 이전 중 주문이 있는 가장 최근 캘린더 날."""
        try:
            ds = self._order_log_calendar_dates_with_orders()
            older = [x for x in ds if x < view_d]
            return max(older) if older else None
        except Exception:
            return None

    def _order_log_next_date_with_orders(self, view_d: date) -> Optional[date]:
        """view_d 보다 이후이면서 오늘까지 — 주문이 있는 가장 이른 캘린더 날."""
        try:
            today = date.today()
            ds = self._order_log_calendar_dates_with_orders()
            newer = [x for x in ds if view_d < x <= today]
            return min(newer) if newer else None
        except Exception:
            return None

    def _sync_order_log_date_label(self) -> None:
        """선택 날짜 표시 및 이전/다음(빈 날짜 미포함·양끝에서 비활성)."""
        try:
            if self._order_log_date_var is None:
                return
            d = self._order_log_view_date
            today = date.today()
            if d == today:
                self._order_log_date_var.set(f"{d.strftime('%Y-%m-%d')} (오늘)")
            else:
                self._order_log_date_var.set(d.strftime("%Y-%m-%d"))
            can_prev = self._order_log_prev_date_with_orders(d) is not None
            can_next = self._order_log_next_date_with_orders(d) is not None
            if self._btn_order_log_prev is not None:
                self._btn_order_log_prev.config(
                    state=("normal" if can_prev else "disabled")
                )
            if self._btn_order_log_next is not None:
                self._btn_order_log_next.config(
                    state=("normal" if can_next else "disabled")
                )
        except Exception:
            pass

    def _kick_order_log_exchange_sync_for_view_date(self) -> None:
        """주문 로그에 보이는 일자의 증권사 체결(kt00007)을 백그라운드로 보강 후 트리만 재그리기."""
        try:
            app = self.app
            d = self._order_log_view_date

            def _job() -> None:
                try:
                    om = getattr(app, "order_manager", None)
                    dm = getattr(app, "data_manager", None)
                    if om is None or dm is None:
                        return
                    om.sync_exchange_fills_into_database(
                        dm,
                        dates=[d],
                        silent=True,
                        logger=getattr(app, "log", print),
                    )
                except Exception:
                    pass
                try:
                    root = getattr(app, "root", None)
                    if root is not None and hasattr(
                        app, "refresh_virtual_tab_orders_only"
                    ):
                        root.after(0, app.refresh_virtual_tab_orders_only)
                except Exception:
                    pass

            threading.Thread(target=_job, daemon=True).start()
        except Exception:
            pass

    def _order_log_prev_day(self) -> None:
        """주문 로그: 이전(주문 내역이 있는 가장 가까운 과거 일자)."""
        try:
            nd = self._order_log_prev_date_with_orders(self._order_log_view_date)
            if nd is None:
                return
            self._order_log_view_date = nd
            self._sync_order_log_date_label()
            self._kick_order_log_exchange_sync_for_view_date()
            self._render_orders()
        except Exception as e:
            try:
                print(f"[UI Error] _order_log_prev_day: {e}")
            except Exception:
                pass

    def _order_log_next_day(self) -> None:
        """주문 로그: 다음(주문 내역이 있는 가장 가까운 이후 일자, 오늘 이하)."""
        try:
            nd = self._order_log_next_date_with_orders(self._order_log_view_date)
            if nd is None:
                return
            self._order_log_view_date = nd
            self._sync_order_log_date_label()
            self._kick_order_log_exchange_sync_for_view_date()
            self._render_orders()
        except Exception as e:
            try:
                print(f"[UI Error] _order_log_next_day: {e}")
            except Exception:
                pass

    def _order_log_today(self) -> None:
        """주문 로그: 오늘 날짜로 이동."""
        try:
            self._order_log_view_date = date.today()
            self._sync_order_log_date_label()
            self._kick_order_log_exchange_sync_for_view_date()
            self._render_orders()
        except Exception as e:
            try:
                print(f"[UI Error] _order_log_today: {e}")
            except Exception:
                pass

    def _open_watchlist_dialog(self) -> None:
        """텔레그램 전용 관심목록 편집 (추가·삭제·저장)."""
        try:
            from ui.watchlist_dialog import open_watchlist_dialog

            open_watchlist_dialog(self.app.root, self.app)
        except Exception as e:
            try:
                messagebox.showerror("오류", f"관심목록 창을 열 수 없습니다: {e}")
            except Exception:
                pass

    def _vp_items_by_normalized(self) -> Dict[str, Dict[str, Any]]:
        """
        virtual_positions 키를 6자리로 통일한 뷰 (3490 vs 003490 이중 키 시 첫 항목 유지).
        계좌 동기화 중 잠깐 dict가 비어 보이는 레이스로 보유 행 전체가 삭제되는 것을 막기 위해
        portfolio.lock으로 스냅샷을 복사한 뒤 처리합니다.
        """
        try:
            portfolio = self.app.portfolio
            try:
                with portfolio.lock:
                    raw = dict(getattr(portfolio, "virtual_positions", None) or {})
            except Exception:
                raw = dict(getattr(portfolio, "virtual_positions", None) or {})
            out: Dict[str, Dict[str, Any]] = {}
            for k, p in raw.items():
                nk = str(k).split("_")[0].zfill(6)
                if nk not in out:
                    out[nk] = p
            return out
        except Exception:
            return {}

    @staticmethod
    def _format_entry_datetime(entry_raw: object) -> str:
        """진입 시각 ISO 등 → 'YYYY-MM-DD HH:MM' 형태로 표시."""
        try:
            et = str(entry_raw or "").strip()
            if not et:
                return "-"
            if "T" in et:
                d, tpart = et.split("T", 1)
                return f"{d} {tpart[:5]}"
            if len(et) >= 16 and et[4] == "-" and et[7] == "-":
                return et[:16].replace("T", " ")
        except Exception:
            pass
        return (str(entry_raw)[:16] if entry_raw else "-") or "-"

    @staticmethod
    def _format_history_exit_display(pos: Dict[str, Any], last_event: Dict[str, Any]) -> str:
        """
        매매 종료일시 표시.
        exit_datetime(신규 저장) → exit_date+exit_time → 구버전(시각만)은 entry_time 날짜와 결합 시도.
        """
        try:
            ex_dt = str(pos.get("exit_datetime") or "").strip()
            if ex_dt:
                return ex_dt[:19] if len(ex_dt) >= 19 else ex_dt
            ed = str(pos.get("exit_date") or "").strip()
            et = str(pos.get("exit_time") or "").strip()
            le_t = str(last_event.get("time") or "").strip()
            if not et:
                et = le_t
            if ed and et and " " not in et:
                return f"{ed} {et}"
            if et and len(et) >= 10 and et[4] == "-" and " " in et:
                return et[:19] if len(et) >= 19 else et
            if ed and le_t and " " not in le_t:
                return f"{ed} {le_t}"
            if et:
                entry_iso = str(pos.get("entry_time") or "").strip()
                if "T" in entry_iso:
                    d0 = entry_iso.split("T", 1)[0]
                    if len(d0) == 10 and d0[4] == "-":
                        return f"{d0} {et}"
                return et
            return le_t or "-"
        except Exception:
            return "-"

    @staticmethod
    def _history_closed_reason_display(pos: Dict[str, Any]) -> str:
        """
        매매 종료 내역 '사유': 매도·정리 사유(주문 로그 사유와 동일 계열).
        `reason` 필드는 진입(TYPE A/B 등)용이므로 표시에 쓰지 않는다.
        """
        try:
            er = str(pos.get("exit_reason") or "").strip()
        except Exception:
            er = ""
        if er:
            return er
        try:
            for ev in reversed(list(pos.get("events") or [])):
                if not isinstance(ev, dict):
                    continue
                act = str(ev.get("action") or "").upper()
                if "SELL" not in act and act != "SYNC_EXIT":
                    continue
                r_ev = str(ev.get("reason") or "").strip()
                snap = ev.get("snapshot")
                r_snap = ""
                if isinstance(snap, dict):
                    try:
                        r_snap = str(snap.get("reason") or "").strip()
                    except Exception:
                        r_snap = ""
                for cand in (r_snap, r_ev):
                    if cand:
                        return cand
                break
        except Exception:
            pass
        try:
            r0 = str(pos.get("reason") or "").strip()
            if r0.upper() in ("RECONCILIATION", "SYNCED"):
                return r0
        except Exception:
            pass
        return "MANUAL"

    @staticmethod
    def _order_log_status_display(result: object) -> str:
        """주문 로그 '상태': 체결 완료는 사용자 표기 '성공'."""
        try:
            s = str(result or "").strip()
        except Exception:
            return "-"
        if s.upper() == "FILLED":
            return "성공"
        return s

    @staticmethod
    def _history_exit_sort_ts(pos: Dict[str, Any]) -> float:
        """매매 종료 내역 정렬 키: 종료 시각(epoch). 클수록 최신."""
        try:
            s = str(pos.get("exit_datetime") or "").strip()
            if len(s) >= 19:
                return float(
                    datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").timestamp()
                )
            ed = str(pos.get("exit_date") or "").strip()
            et = str(pos.get("exit_time") or "").strip()
            if ed and et:
                if " " in et and len(et) >= 16 and et[4] == "-":
                    frag = et[:19].replace("T", " ")
                    return float(
                        datetime.strptime(frag[:19], "%Y-%m-%d %H:%M:%S").timestamp()
                    )
                frag2 = f"{ed} {et.strip()[:8]}"
                return float(
                    datetime.strptime(frag2, "%Y-%m-%d %H:%M:%S").timestamp()
                )
            if et and len(et) <= 10 and ":" in et:
                entry_iso = str(pos.get("entry_time") or "").strip()
                if "T" in entry_iso:
                    d0 = entry_iso.split("T", 1)[0]
                    if len(d0) == 10 and d0[4] == "-":
                        frag3 = f"{d0} {et.strip()[:8]}"
                        return float(
                            datetime.strptime(frag3, "%Y-%m-%d %H:%M:%S").timestamp()
                        )
        except (TypeError, ValueError, OSError):
            pass
        return 0.0

    def _sorted_active_holdings(self) -> List[Tuple[str, Dict[str, Any]]]:
        """
        보유 행 표시 순서를 종목코드 기준으로 고정(줄무늬 태그·인덱스 흔들림 방지).
        관심목록은 신호 테스트용으로만 쓰이며, 포지션 관리 탭 집계·표시와는 연동하지 않습니다.
        """
        try:
            merged = self._vp_items_by_normalized()
            rows = list(merged.items())
            return sorted(rows, key=lambda x: x[0])
        except Exception:
            return []

    def _rest_quote_fallback_close(self, nk: str) -> float:
        """실시간 미수신 시 키움 REST(ka10004) 현재가 — trading_data.db 미사용."""
        try:
            now_m = time.monotonic()
            cached = self._fallback_quote_cache.get(nk)
            if cached is not None and (now_m - cached[0]) < 55.0:
                return float(cached[1])
            om = getattr(self.app, "order_manager", None)
            fn = getattr(om, "fetch_stock_cur_prc_rest", None) if om is not None else None
            if not callable(fn):
                return 0.0
            px = fn(nk)
            if px is not None:
                v = float(px)
                if v > 0:
                    self._fallback_quote_cache[nk] = (now_m, v)
                    return v
        except Exception:
            pass
        return 0.0

    def _monitor_live_price(self, nk: str, pos: Dict[str, Any]) -> float:
        """
        6자리 종목코드 기준 현재가(청산 가정 시 단가).
        틱의 price → (스텁이 아닐 때) last_price → 키움 REST 현재가(ka10004) → 평단 순으로 폴백합니다.
        """
        try:
            mi = getattr(self.app, "monitor_info", None) or {}
            info = mi.get(nk, {}) or {}
        except Exception:
            info = {}
        stub_flag = bool(info.get("vp_price_stub"))
        try:
            ap = float(pos.get("avg_price", 0) or 0)
        except (TypeError, ValueError):
            ap = 0.0
        try:
            raw_p = info.get("price")
            if raw_p not in (None, "", 0):
                v = float(raw_p)
                if v > 0:
                    return v
        except (TypeError, ValueError):
            pass
        if not stub_flag:
            try:
                raw_lp = info.get("last_price")
                if raw_lp not in (None, "", 0):
                    v = float(raw_lp)
                    if v > 0:
                        # 구버전 스텁: last만 평단이고 틱 캐시 price 없음 → 실시간으로 보지 않음
                        if (
                            ap > 0
                            and abs(v - ap) < 1e-6
                            and "price" not in info
                        ):
                            pass
                        else:
                            return v
            except (TypeError, ValueError):
                pass
        api_px = self._rest_quote_fallback_close(nk)
        if api_px > 0:
            return api_px
        try:
            if ap > 0:
                return ap
        except (TypeError, ValueError):
            pass
        return 0.0

    def _has_live_tick_price(self, nk: str) -> bool:
        """실시간 틱 `price`가 유효하면 True — 장중 스트림 우선(잔고 API 스냅샷보다 앞섬)."""
        try:
            mi = getattr(self.app, "monitor_info", None) or {}
            row = mi.get(nk, {}) or {}
            if row.get("vp_price_stub"):
                return False
            raw_p = row.get("price")
            if raw_p in (None, "", 0):
                return False
            v = float(raw_p)
            return v > 0
        except Exception:
            return False

    def _row_has_ka_snapshot(self, pos: Dict[str, Any]) -> bool:
        """마지막 계좌 동기화(ka10085)에서 저장한 손익·평가 스냅샷이 있으면 True."""
        try:
            if not pos.get("ka_sync_iso"):
                return False
            if float(pos.get("ka_evlu_amt") or 0) > 0:
                return True
            if abs(float(pos.get("ka_evlu_pfls_amt") or 0)) > 1e-6:
                return True
            if float(pos.get("ka_cur_prc") or 0) > 0:
                return True
            if float(pos.get("ka_pur_amt") or 0) > 0:
                return True
        except Exception:
            pass
        return False

    def _unrealized_at_price_net_of_sell_fee(
        self,
        pos: Dict[str, Any],
        curr_p: float,
        qty: int,
        avg_price: float,
    ) -> float:
        """
        현재가(전량 매도 가정 단가)로 청산한다고 할 때 **매도 수수료를 차감한** 미실현 손익(원).
        원가는 청산 실현과 동일하게 `invested`(매수 수수료 포함)를 우선 사용합니다.
        """
        try:
            q = int(qty or 0)
        except (TypeError, ValueError):
            q = 0
        try:
            px = float(curr_p)
        except (TypeError, ValueError):
            px = 0.0
        if q <= 0 or px <= 0:
            return 0.0
        try:
            _, sell_r = fee_decimal_rates_live_trading(self.app.settings)
            sr = float(sell_r)
        except Exception:
            try:
                _, sell_r = fee_decimal_rates_live_trading({})
                sr = float(sell_r)
            except Exception:
                sr = 0.005
        try:
            sr = max(0.0, min(float(sr), 0.5))
        except Exception:
            sr = 0.005
        try:
            inv = float(pos.get("invested", 0) or 0)
        except (TypeError, ValueError):
            inv = 0.0
        if inv <= 1e-9:
            try:
                ap = float(avg_price or 0)
            except (TypeError, ValueError):
                ap = 0.0
            inv = max(0.0, ap * float(q))
        gross = px * float(q)
        net_proceeds = gross * (1.0 - sr)
        return float(net_proceeds - inv)

    def _history_realized_pnl_amt(self, pos: Dict[str, Any]) -> float:
        """종료 포지션 실현손익(원). `realized_pnl_amt` 누락·null 시 수익률·투입금으로 근사."""
        try:
            raw = pos.get("realized_pnl_amt")
            if raw is not None and raw != "":
                return float(raw)
        except (TypeError, ValueError):
            pass
        try:
            inv = float(pos.get("invested", 0) or 0)
            fp = float(pos.get("final_pnl_pct", 0) or 0)
            if inv > 0 and abs(fp) > 1e-12:
                return inv * fp / 100.0
        except (TypeError, ValueError):
            pass
        return 0.0

    def _sum_visible_history_realized_pnl(self) -> float:
        """
        `virtual_history` 전체에 대해 종료 실현손익(`_history_realized_pnl_amt`) 합계.
        누적 수익의 청산분에 사용합니다(관심목록과 무관).
        """
        out = 0.0
        try:
            hist = list(getattr(self.app.portfolio, "virtual_history", None) or [])
        except Exception:
            hist = []
        for pos in hist:
            if not isinstance(pos, dict):
                continue
            try:
                out += float(self._history_realized_pnl_amt(pos))
            except Exception:
                pass
        return float(out)

    def _history_total_fees_amt(self, pos: Dict[str, Any]) -> float:
        """
        종료 1건의 매수+매도 수수료 합계(원).
        BUY_S1/S2·SELL 이벤트가 있으면 체결금액 기준 누적, 없으면 invested·exit_price로 근사합니다.
        """
        try:
            buy_r, sell_r = fee_decimal_rates_live_trading(self.app.settings)
            br, sr = float(buy_r), float(sell_r)
        except Exception:
            # 설정 손상 시에도 모의/실전에 맞는 폴백(빈 dict → fee_rates 내부 USE_MOCK 기본값)
            try:
                br, sr = fee_decimal_rates_live_trading({})
            except Exception:
                br, sr = 0.0035, 0.005
        try:
            qty = int(pos.get("qty", 0) or 0)
        except (TypeError, ValueError):
            qty = 0
        evs = pos.get("events") or []
        if not isinstance(evs, list):
            evs = []
        buy_gross = 0.0
        fee_buy = 0.0
        fee_sell = 0.0
        for e in evs:
            try:
                if not isinstance(e, dict):
                    continue
                act = str(e.get("action", "") or "").upper()
                if act in ("BUY_S1", "BUY_S2"):
                    p = float(e.get("price", 0) or 0)
                    q = int(e.get("qty", 0) or 0)
                    if p > 0 and q > 0:
                        g = p * float(q)
                        buy_gross += g
                        fee_buy += g * br
                elif act == "SELL":
                    p = float(e.get("price", 0) or 0)
                    q = int(e.get("qty", 0) or 0) or qty
                    if p > 0 and q > 0:
                        fee_sell += p * float(q) * sr
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        if buy_gross <= 1e-9 and qty > 0:
            try:
                inv = float(pos.get("invested", 0) or 0)
            except (TypeError, ValueError):
                inv = 0.0
            if inv > 0 and (1.0 + br) > 1e-12:
                gross = inv / (1.0 + br)
                fee_buy = max(0.0, inv - gross)
        if fee_sell <= 1e-9 and qty > 0:
            try:
                ex = float(pos.get("exit_price", 0) or 0)
            except (TypeError, ValueError):
                ex = 0.0
            if ex > 0:
                fee_sell = ex * float(qty) * sr
        return float(fee_buy + fee_sell)

    def _active_row_vals_and_tags(
        self, row_idx: int, nk: str, pos: Dict[str, Any], info: Dict[str, Any]
    ) -> Tuple[tuple, Tuple[str, ...], float, float, float]:
        """
        실시간 보유 한 행의 values·tags·수익률% (트리/요약 공통).
        반환 (vals, tags, profit_pct, unrealized_raw, eval_amt_raw):
        상단 요약 합계는 **트리와 동일**한 unrealized·eval을 쓰려면 이 값을 그대로 더하면 됩니다.
        """
        try:
            live_p = float(self._monitor_live_price(nk, pos))
        except (TypeError, ValueError):
            live_p = float(pos.get("avg_price", 0) or 0)
        try:
            ap = float(pos.get("avg_price", 0) or 0)
        except (TypeError, ValueError):
            ap = 0.0
        qty = int(pos.get("qty", 0) or 0)
        if ap <= 0:
            ap = live_p if live_p > 0 else ap

        use_tick = self._has_live_tick_price(nk)
        use_ka = (not use_tick) and self._row_has_ka_snapshot(pos)

        if use_tick and live_p > 0:
            curr_p = live_p
            eval_amt = curr_p * qty
            buy_in_amt = int(round(float(ap) * float(qty)))
        elif use_ka:
            try:
                ka_cur = float(pos.get("ka_cur_prc") or 0)
                ka_ev = float(pos.get("ka_evlu_amt") or 0)
                ka_pur = float(pos.get("ka_pur_amt") or 0)
            except (TypeError, ValueError):
                ka_cur = ka_ev = ka_pur = 0.0
            if ka_cur > 0:
                curr_p = ka_cur
            elif ka_ev > 0 and qty > 0:
                curr_p = ka_ev / float(qty)
            else:
                curr_p = live_p if live_p > 0 else ap
            if ka_ev > 0:
                eval_amt = ka_ev
            else:
                eval_amt = curr_p * float(qty)
            if ka_pur > 0:
                buy_in_amt = int(round(ka_pur))
            else:
                buy_in_amt = int(round(float(ap) * float(qty)))
        else:
            curr_p = live_p
            if ap <= 0:
                ap = curr_p
            eval_amt = curr_p * qty
            buy_in_amt = int(round(float(ap) * float(qty)))

        unrealized = self._unrealized_at_price_net_of_sell_fee(pos, float(curr_p), qty, ap)
        try:
            inv_pf = float(pos.get("invested", 0) or 0)
        except (TypeError, ValueError):
            inv_pf = 0.0
        if inv_pf <= 1e-9 and ap > 0 and qty > 0:
            inv_pf = float(ap) * float(qty)
        if inv_pf > 1e-9:
            profit_pct = (unrealized / inv_pf) * 100.0
        elif ap > 0 and float(curr_p) > 0:
            profit_pct = (float(curr_p) / ap - 1.0) * 100.0
        else:
            profit_pct = 0.0
        p_str = f"{profit_pct:+.2f}%"
        entry_disp = self._format_entry_datetime(pos.get("entry_time", ""))
        tag_base = "even" if row_idx % 2 == 0 else "odd"
        p_tag = (
            "price_up"
            if profit_pct > 0
            else ("price_down" if profit_pct < 0 else "")
        )
        nm = pos.get("name", nk)
        vals = (
            nk,
            nm,
            f"{qty:,}",
            f"{ap:,.0f}",
            f"{buy_in_amt:,}",
            f"{curr_p:,.0f}",
            f"{int(eval_amt):,}",
            f"{int(unrealized):+,}",
            p_str,
            entry_disp,
        )
        # 전체 렌더와 동일하게 항상 (줄무늬, 방향) 2튜플 + 요약 합산용 원시 금액
        return (
            vals,
            (tag_base, p_tag),
            float(profit_pct),
            float(unrealized),
            float(eval_amt),
        )

    def _active_tree_ready(self) -> bool:
        return self.tree_active is not None

    def _remove_active_tree_row(self, nk: str) -> None:
        """보유 트리에서 종목 행 제거."""
        try:
            tr = self.tree_active
            if tr is not None and tr.exists(nk):
                tr.delete(nk)
        except Exception:
            pass

    def _reorder_active_tree_rows(self) -> None:
        """종목코드 정렬 순서와 Treeview 행 순서를 맞춘다."""
        try:
            tr = self.tree_active
            if tr is None:
                return
            ordered = [nk for nk, _ in self._sorted_active_holdings()]
            for i, nk in enumerate(ordered):
                if not tr.exists(nk):
                    continue
                try:
                    tr.move(nk, "", i)
                except Exception:
                    pass
        except Exception as e:
            try:
                print(f"[UI Error] _reorder_active_tree_rows: {e}")
            except Exception:
                pass

    def _upsert_active_tree_row(
        self,
        nk: str,
        vals: Tuple[Any, ...],
        tags: Tuple[str, ...],
    ) -> None:
        """보유 트리: 동일 iid(종목코드)면 values/tags만 갱신, 없으면 삽입."""
        try:
            tr = self.tree_active
            if tr is None or len(vals) < 10:
                return
            tag_list = tuple(t for t in tags if t)
            v10 = tuple(str(vals[i]) for i in range(10))
            if tr.exists(nk):
                tr.item(nk, values=v10, tags=tag_list)
            else:
                tr.insert("", tk.END, iid=nk, values=v10, tags=tag_list)
        except Exception as e:
            try:
                print(f"[UI Error] _upsert_active_tree_row: {e}")
            except Exception:
                pass

    def refresh_portfolio_light(self) -> None:
        """
        포지션 구조(종목·수량·평단)는 그대로일 때만 사용.
        히스토리·주문 트리 전체 delete/재삽입 없이 보유 행·상단 요약만 갱신합니다.
        (실시간 감시 탭의 틱 단위 item 갱신과 같은 패턴으로 깜빡임을 줄입니다.)
        """
        try:
            if not self._active_tree_ready():
                return
            merged = self._vp_items_by_normalized()
            tr = self.tree_active
            for nk in merged:
                if tr is not None and not tr.exists(nk):
                    self._render_virtual_positions()
                    try:
                        self.app._last_vp_render_sig = self.app._vp_structure_signature()
                    except Exception:
                        pass
                    return
            self._render_real_data()
        except Exception as e:
            try:
                print(f"[UI Error] refresh_portfolio_light: {e}")
            except Exception:
                pass
            try:
                self._render_virtual_positions()
                self.app._last_vp_render_sig = self.app._vp_structure_signature()
            except Exception:
                pass

    def _render_virtual_positions(self):
        """데이터(app.virtual_positions / history / today_orders) 동기화"""
        # 0. 실시간 주문 로그 — 보유/히스토리 처리 전에 먼저 갱신(체결강도·호가 열 포함)
        try:
            self._render_orders()
        except Exception:
            pass
        # 1. 보유 종목
        total_eval = 0
        total_invested = 0
        total_unrealized_pnl = 0
        
        merged_vp = self._vp_items_by_normalized()
        active_list = self._sorted_active_holdings()
        skip_orphan_delete = False
        updated_nks: set = set()
        # 스냅샷이 잠깐 비어 보이면 보유 트리를 통째로 지우지 않음 (수초~십수 초 공백 방지)
        existing_active = False
        try:
            with self.app.portfolio.lock:
                existing_active = (
                    len(getattr(self.app.portfolio, "virtual_positions", None) or {}) > 0
                )
        except Exception:
            existing_active = False
        if not merged_vp and existing_active:
            merged_vp = self._vp_items_by_normalized()
            active_list = self._sorted_active_holdings()
            if not merged_vp:
                skip_orphan_delete = True

        mi = getattr(self.app, "monitor_info", None) or {}
        for idx, (nk, pos) in enumerate(active_list):
            info = mi.get(nk, {}) or {}
            vals, row_tags, profit_pct, ur_row, ev_row = self._active_row_vals_and_tags(
                idx, nk, pos, info
            )
            total_eval += ev_row
            total_invested += float(pos.get("invested", 0) or 0)
            total_unrealized_pnl += ur_row

            updated_nks.add(nk)
            if self._active_tree_ready():
                self._upsert_active_tree_row(nk, vals, row_tags)

        # 트리에만 남은 행: VP 스냅샷에 종목이 없을 때만 제거
        if not skip_orphan_delete and self._active_tree_ready():
            tr = self.tree_active
            if tr is not None:
                for nk in list(tr.get_children()):
                    if nk in updated_nks:
                        continue
                    if nk not in merged_vp:
                        self._remove_active_tree_row(nk)
        if self._active_tree_ready():
            self._reorder_active_tree_rows()

        # 2. 종료 종목
        self.tree_history.delete(*self.tree_history.get_children())
        try:
            _raw_hist = list(getattr(self.app.portfolio, "virtual_history", None) or [])
        except Exception:
            _raw_hist = []
        hist_visible: List[Dict[str, Any]] = [
            p for p in _raw_hist if isinstance(p, dict)
        ]
        hist_visible.sort(key=self._history_exit_sort_ts, reverse=True)
        total_closed_pnl = self._sum_visible_history_realized_pnl()

        hist_row = 0
        for pos in hist_visible:
            last_event = pos["events"][-1] if pos.get("events") else {}
            pnl_amt = self._history_realized_pnl_amt(pos)
            try:
                fee_sum = float(self._history_total_fees_amt(pos))
            except Exception:
                fee_sum = 0.0
            tag_base = "even" if hist_row % 2 == 0 else "odd"
            hist_row += 1
            p_tag = "price_up" if pos.get("final_pnl_pct", 0) > 0 else "price_down"
            self.tree_history.insert(
                "",
                tk.END,
                values=(
                    self._format_history_exit_display(pos, last_event),
                    pos.get("code"),
                    pos["name"],
                    f"{pos.get('qty', 0):,}",
                    f"{pos.get('avg_price', 0):,.0f}",
                    f"{pos.get('exit_price', 0):,.0f}",
                    f"{int(round(fee_sum)):,}",
                    f"{int(round(pnl_amt)):+,}",
                    f"{pos.get('final_pnl_pct', 0):+.2f}%",
                    self._history_closed_reason_display(pos),
                ),
                tags=(tag_base, p_tag),
            )

        # 3. 요약 정보 (Account Summary - REST API Synced) — 주문 로그는 위 0단계에서 이미 반영
        portfolio = self.app.portfolio
        current_cash = getattr(portfolio, "actual_deposit", 0)
        # 상단 '주식 평가금': 잔고 API(ka10085) 합산, 미동기화 시 그리드 실시간 합 폴백
        stock_eval = self._stock_eval_for_summary(portfolio, float(total_eval))
        total_assets = self._compute_virtual_tab_total_assets(portfolio, stock_eval)

        # 보유주식 손익(미청산): 금일 청산 실현은 제외, 현재 보유 종목 손익금(미실현) 합만.
        # %는 보유 매입금(invested, 매수수수료 포함) 합 대비.
        day_pnl_total = float(total_unrealized_pnl)
        try:
            inv_den = float(total_invested)
        except (TypeError, ValueError):
            inv_den = 0.0
        if inv_den > 1e-6:
            day_pnl_pct = (day_pnl_total / inv_den) * 100.0
        else:
            day_pnl_pct = 0.0
        try:
            ta = float(total_assets)
        except (TypeError, ValueError):
            ta = 0.0

        # 누적 수익(종료+보유): 매매종료 손익금 합 + 실시간 보유 손익금(미실현) 합.
        # % 분모는 총자산(백테스트 초기자본은 실시간 요약에 사용하지 않음).
        cum_pnl_amt = float(total_closed_pnl) + float(total_unrealized_pnl)
        if ta > 1e-6:
            cum_pnl_pct = (cum_pnl_amt / ta) * 100.0
        else:
            cum_pnl_pct = 0.0

        self.vars["total"].set(f"{total_assets:,.0f}")
        self.vars["cash"].set(f"{current_cash:,.0f}")
        self.vars["stock"].set(f"{stock_eval:,.0f}")
        
        # [v4.5.3] D+1, D+2 예상 예수금 업데이트
        self.vars["d1_cash"].set(f"{getattr(portfolio, 'd1_deposit', 0):,.0f}")
        self.vars["d2_cash"].set(f"{getattr(portfolio, 'd2_deposit', 0):,.0f}")
            
        self.vars["day"].set(f"{int(day_pnl_total):+,} ({day_pnl_pct:+.2f}%)")
        self.vars["cum"].set(f"{int(cum_pnl_amt):+,} ({cum_pnl_pct:+.2f}%)")

    def _render_real_data(self):
        """실시간 틱·동기화 후 보유 트리 값만 갱신."""
        if not self._active_tree_ready():
            return
        try:
            mi = getattr(self.app, "monitor_info", None) or {}
            for idx, (nk, pos) in enumerate(self._sorted_active_holdings()):
                info = mi.get(nk, {}) or {}
                vals, row_tags, profit_pct, _, _ = self._active_row_vals_and_tags(
                    idx, nk, pos, info
                )
                self._upsert_active_tree_row(nk, vals, row_tags)
            self._reorder_active_tree_rows()
        except Exception as e:
            try:
                print(f"[UI Error] _render_real_data: {e}")
            except Exception:
                pass

        self._update_summary_after_tick()

    def _update_summary_after_tick(self) -> None:
        """틱 직후 상단 요약(주식평가금·보유주식 손익·총자산 등)만 재계산합니다."""
        try:
            def _set_if_diff(key: str, text: str) -> None:
                try:
                    v = self.vars.get(key)
                    if v is None:
                        return
                    if v.get() != text:
                        v.set(text)
                except Exception:
                    try:
                        self.vars[key].set(text)
                    except Exception:
                        pass

            mi = getattr(self.app, "monitor_info", None) or {}
            total_eval = 0.0
            total_unrealized_pnl = 0.0
            total_invested_open = 0.0
            for idx, (nk, pos) in enumerate(self._sorted_active_holdings()):
                info = mi.get(nk, {}) or {}
                _v, _t, _p, ur_row, ev_row = self._active_row_vals_and_tags(
                    idx, nk, pos, info
                )
                total_eval += ev_row
                total_unrealized_pnl += ur_row
                try:
                    total_invested_open += float(pos.get("invested", 0) or 0)
                except (TypeError, ValueError):
                    pass

            portfolio = self.app.portfolio
            current_cash = float(getattr(portfolio, "actual_deposit", 0) or 0)
            stock_eval = self._stock_eval_for_summary(portfolio, float(total_eval))
            total_assets = self._compute_virtual_tab_total_assets(portfolio, stock_eval)

            day_pnl_total = float(total_unrealized_pnl)
            if total_invested_open > 1e-6:
                day_pnl_pct = (day_pnl_total / total_invested_open) * 100.0
            else:
                day_pnl_pct = 0.0
            try:
                ta = float(total_assets)
            except (TypeError, ValueError):
                ta = 0.0

            closed_h = self._sum_visible_history_realized_pnl()
            cum_pnl_amt = float(closed_h) + float(total_unrealized_pnl)
            if ta > 1e-6:
                cum_pnl_pct = (cum_pnl_amt / ta) * 100.0
            else:
                cum_pnl_pct = 0.0

            _set_if_diff("total", f"{total_assets:,.0f}")
            _set_if_diff("cash", f"{current_cash:,.0f}")
            _set_if_diff("stock", f"{stock_eval:,.0f}")
            _set_if_diff("d1_cash", f"{getattr(portfolio, 'd1_deposit', 0):,.0f}")
            _set_if_diff("d2_cash", f"{getattr(portfolio, 'd2_deposit', 0):,.0f}")
            _set_if_diff("day", f"{int(day_pnl_total):+,} ({day_pnl_pct:+.2f}%)")
            _set_if_diff("cum", f"{int(cum_pnl_amt):+,} ({cum_pnl_pct:+.2f}%)")
        except Exception:
            pass

    @staticmethod
    def _stock_eval_for_summary(portfolio: Any, local_total_eval: float) -> float:
        """
        상단 '주식 평가금': 잔고 API(ka10085)에서 합산한 보유 평가액 우선.
        미동기화·API 0이면 로컬 실시간 합(local_total_eval) 폴백.
        """
        try:
            api_mv = float(getattr(portfolio, "holdings_stocks_mv_api", 0) or 0)
            if api_mv > 0:
                return api_mv
            return float(local_total_eval)
        except Exception:
            try:
                return float(local_total_eval)
            except Exception:
                return 0.0

    @staticmethod
    def _compute_virtual_tab_total_assets(portfolio: Any, _stock_eval: float) -> float:
        """
        포지션 관리 탭 '총 자산': D+2 예수 + 화면과 동일한 주식평가(stock_eval) 합이 기준.
        last_tot_evlu_amt(ka10085 단일 총평가)는 D+2 보정 전 값일 수 있어, 합계와 ±2.5% 안에서만 동일 처리.
        """
        try:
            d2 = float(getattr(portfolio, "d2_deposit", 0) or 0)
            api_mv = float(getattr(portfolio, "holdings_stocks_mv_api", 0) or 0)
            se = float(_stock_eval) if float(_stock_eval or 0) > 0 else api_mv
            combined = d2 + se
            raw_te = float(getattr(portfolio, "last_tot_evlu_amt", 0) or 0)
            pte = float(getattr(portfolio, "total_equity", 0) or 0)
            if abs(combined) > 1e-6 or se > 0 or d2 != 0:
                if raw_te > 0:
                    den = max(abs(raw_te), abs(combined), 1.0)
                    if abs(raw_te - combined) / den <= 0.025:
                        return float(raw_te)
                return float(combined)
            if raw_te > 0:
                return float(raw_te)
            if pte > 0:
                return float(pte)
            cash = float(getattr(portfolio, "actual_deposit", 0) or 0)
            if cash > 0:
                return float(cash)
            if api_mv > 0:
                return float(api_mv)
            return 0.0
        except Exception:
            try:
                return float(getattr(portfolio, "total_equity", 0) or 0)
            except Exception:
                return 0.0

    def _normalize_total_assets_display(
        self, total_assets: float, current_cash: float, stock_eval: float
    ) -> float:
        """
        tot_evlu_amt 가 주식 평가액만 담는 경우 총자산이 예수금보다 작게 보일 수 있어,
        (예수금 + 주식평가) 합이 더 크면 그 값을 총자산 표시에 사용합니다.
        """
        try:
            computed = float(current_cash) + float(stock_eval)
            if computed <= 0:
                return float(total_assets)
            ta = float(total_assets)
            if ta <= 0:
                return computed
            if ta < computed * 0.88:
                return computed
            return ta
        except Exception:
            return float(total_assets)

    def _order_log_vp_cell(self, v: object) -> str:
        """주문 로그용 체결강도 셀(소수 1자리 %)."""
        try:
            if v is None:
                return "-"
            return f"{float(v):.1f}%"
        except Exception:
            return "-"

    def _render_orders(self) -> None:
        """DB 기반 주문 내역 동기화. 선택 날짜(네비게이션) 기준, 주문 시점 VP·호가 표시."""
        if not getattr(self, "tree_orders", None):
            return
        self.tree_orders.delete(*self.tree_orders.get_children())
        try:
            try:
                view_d = self._order_log_view_date
            except Exception:
                view_d = date.today()
            orders = self.app.data_manager.get_orders_for_calendar_date(view_d)
            row_i = 0
            for ord_data in orders:
                # snap_* = 주문 시점, fill_snap_* = 체결 시점 (감시 Bid/Ask 열과 동일 호가 문자열)
                try:
                    od = list(ord_data)
                    while len(od) < 14:
                        od.append(None)
                    (
                        ts,
                        code,
                        side,
                        qty,
                        price,
                        f_price,
                        f_qty,
                        result,
                        name,
                        reason,
                        snap_vp,
                        snap_ba,
                        _fill_vp,
                        _fill_ba,
                    ) = od[:14]
                except Exception:
                    continue
                try:
                    tm_raw = str(ts or "").strip()
                    tm = tm_raw if tm_raw else "-"
                except Exception:
                    tm = "-"
                side_kr = "매수" if side == "buy" else "매도"
                f_p_str = f"{f_price:,}" if f_price > 0 else "-"
                ba_o = (snap_ba or "").strip() or "-"
                tag_base = "even" if row_i % 2 == 0 else "odd"
                row_i += 1
                side_tag = "price_up" if side == "buy" else "price_down"

                self.tree_orders.insert(
                    "",
                    tk.END,
                    values=(
                        tm,
                        code,
                        name,
                        side_kr,
                        f"{qty:,}",
                        f"{price:,}",
                        f_p_str,
                        self._order_log_vp_cell(snap_vp),
                        ba_o,
                        self._order_log_status_display(result),
                        reason,
                    ),
                    tags=(tag_base, side_tag),
                )
        except Exception as e:
            try:
                print(f"[UI Error] _render_orders: {e}")
            except Exception:
                pass
        try:
            self._sync_order_log_date_label()
        except Exception:
            pass

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
            messagebox.showinfo(
                "설정 변경 완료",
                f"성공적으로 [{target_mode}] 모드로 설정되었습니다.\n\n"
                f"재시작하면 {target_mode} 서버에서 시작합니다.\n\n"
                "※ 모의·실전은 포지션·종료내역·주문 로그가 서로 다른 파일에 저장되므로\n"
                "   기록이 섞이지 않습니다.",
            )
            
            mode_bg = "#5E81AC" if new_mock else "#BF616A"
            mode_text = " 모의투자모드 " if new_mock else " 실전모드 "
            _db = getattr(self.app, "dashboard_tab", None)
            _badge = getattr(_db, "lbl_mode_badge", None) if _db is not None else None
            if _badge is not None:
                try:
                    _badge.config(text=mode_text, background=mode_bg)
                except Exception:
                    pass

        except Exception as e:
            messagebox.showerror("오류", f"설정 변경 중 오류가 발생했습니다: {e}")

    def log_order(self, code, side, qty, price, order_type, result, reason=""):
        """[v4.5.3] DB 기록 및 신규 3판넬 UI 갱신 (캡슐화)"""
        try:
            from core.supply_display import capture_supply_snapshot

            _sn = capture_supply_snapshot(self.app, code)
        except Exception:
            _sn = {"vol_power": None, "bid_ask_text": ""}
        # 기존 호환: positional 8번째 인자가 name 컬럼으로 전달되던 패턴 유지
        self.app.data_manager.log_order(
            code,
            side,
            qty,
            price,
            order_type,
            "MOCK_ID",
            result,
            name=reason,
            reason="",
            snap_vol_power=_sn.get("vol_power"),
            snap_bid_ask=_sn.get("bid_ask_text") or "",
        )
        try:
            self.app.refresh_virtual_tab_full()
        except Exception:
            try:
                self._render_orders()
            except Exception:
                pass
            try:
                self._render_virtual_positions()
            except Exception:
                pass

    def _narrative_report_buttons(self) -> List[Any]:
        """종합 리포트 생성 중 비활성화할 버튼(포지션 탭·대시보드)."""
        out: List[Any] = []
        try:
            if self.btn_narrative_report is not None:
                out.append(self.btn_narrative_report)
        except Exception:
            pass
        try:
            _db = getattr(self.app, "dashboard_tab", None)
            _b = getattr(_db, "btn_narrative_report", None) if _db is not None else None
            if _b is not None:
                out.append(_b)
        except Exception:
            pass
        return out

    def generate_report(self):
        """가상 매매 종합 리포트 생성 (백그라운드 스레드 + 진행률 표시)."""
        if self._narrative_report_busy:
            try:
                messagebox.showinfo(
                    "종합 리포트",
                    "이미 리포트를 생성 중입니다. 완료될 때까지 잠시만 기다려 주세요.",
                    parent=self.parent,
                )
            except Exception:
                pass
            return
        try:
            import webbrowser

            # 디스크에서 virtual_report.py를 수정해도 프로세스 재시작 없이 반영되도록 reload
            try:
                import reporting.virtual_report as _vr_mod

                importlib.reload(_vr_mod)
                gen_fn: Callable[..., None] = _vr_mod.generate_virtual_narrative_report
            except Exception as _e_reload:
                from reporting.virtual_report import generate_virtual_narrative_report

                gen_fn = generate_virtual_narrative_report
                try:
                    self.app.log(
                        f"[Warn] virtual_report reload 실패, 기존 모듈 사용: {_e_reload}"
                    )
                except Exception:
                    pass

            output_file = "virtual_trade_report.html"
            hist_vis = list(getattr(self.app.portfolio, "virtual_history", None) or [])
            open_vis: list = []
            try:
                try:
                    self.app.trading_controller._ensure_virtual_positions_monitor_info()
                    self.app.monitor_info = dict(
                        self.app.trading_controller.monitor_info or {}
                    )
                except Exception:
                    pass
                vp = getattr(self.app.portfolio, "virtual_positions", None) or {}
                for c_key, p in vp.items():
                    if not isinstance(p, dict):
                        continue
                    try:
                        nc = str(p.get("code") or c_key).zfill(6)
                    except Exception:
                        nc = str(c_key).zfill(6)
                    row = dict(p)
                    row.setdefault("code", nc)
                    try:
                        row["current_price"] = float(self._monitor_live_price(nc, row))
                    except (TypeError, ValueError):
                        try:
                            row["current_price"] = float(row.get("avg_price") or 0)
                        except (TypeError, ValueError):
                            row["current_price"] = 0.0
                    open_vis.append(row)
            except Exception as _e_open:
                self.app.log(f"[Warn] 리포트용 보유 포지션 수집 실패: {_e_open}")

            root = self.parent.winfo_toplevel()
            dlg = tk.Toplevel(root)
            dlg.title("종합 리포트 생성 중")
            dlg.resizable(False, False)
            try:
                dlg.transient(root)
                dlg.grab_set()
            except Exception:
                pass
            frm = ttk.Frame(dlg, padding=16)
            frm.pack(fill=tk.BOTH, expand=True)
            status_var = tk.StringVar(value="준비 중…")
            ttk.Label(frm, textvariable=status_var, wraplength=360).pack(anchor=tk.W)
            prog = ttk.Progressbar(frm, mode="determinate", maximum=100, length=360)
            prog.pack(pady=(10, 0), fill=tk.X)
            ttk.Label(
                frm,
                text="종목별 REST 15분봉 조회로 수 분 걸릴 수 있습니다.",
                font=("Malgun Gothic", 8),
                foreground="#787c99",
            ).pack(anchor=tk.W, pady=(8, 0))

            def _block_close() -> None:
                try:
                    messagebox.showinfo(
                        "종합 리포트",
                        "생성이 끝날 때까지 이 창을 닫을 수 없습니다.",
                        parent=dlg,
                    )
                except Exception:
                    pass

            try:
                dlg.protocol("WM_DELETE_WINDOW", _block_close)
            except Exception:
                pass

            holder: Dict[str, Any] = {"err": None, "path": None}

            def on_progress(done: int, total: int, msg: str) -> None:
                try:
                    tt = max(1, int(total))
                    dd = max(0, min(int(done), tt))
                    pct = int(round(100.0 * float(dd) / float(tt)))

                    def apply_ui() -> None:
                        try:
                            status_var.set(msg)
                            prog.configure(maximum=100)
                            prog["value"] = pct
                        except tk.TclError:
                            pass

                    root.after(0, apply_ui)
                except Exception:
                    pass

            def worker() -> None:
                try:
                    gen_fn(
                        hist_vis,
                        output_file,
                        self.app.settings,
                        open_positions=open_vis,
                        data_manager=getattr(self.app, "data_manager", None),
                        progress_callback=on_progress,
                    )
                    holder["path"] = os.path.abspath(output_file)
                except Exception as e:
                    holder["err"] = str(e)
                finally:

                    def finish() -> None:
                        self._narrative_report_busy = False
                        try:
                            dlg.grab_release()
                        except Exception:
                            pass
                        try:
                            dlg.destroy()
                        except Exception:
                            pass
                        try:
                            for _nb in self._narrative_report_buttons():
                                _nb.state(["!disabled"])
                        except Exception:
                            pass
                        err = holder.get("err")
                        pth = holder.get("path")
                        if err:
                            try:
                                self.app.log(f"[Error] 리포트 생성 중 오류: {err}")
                            except Exception:
                                pass
                            try:
                                messagebox.showerror(
                                    "종합 리포트",
                                    str(err),
                                    parent=self.parent,
                                )
                            except Exception:
                                pass
                        elif pth and os.path.exists(pth):
                            try:
                                webbrowser.open(f"file://{pth}")
                            except Exception as _wb:
                                try:
                                    self.app.log(
                                        f"[Warn] 브라우저 열기 실패: {_wb}"
                                    )
                                except Exception:
                                    pass
                            try:
                                self.app.log(
                                    f"[Sync] 가상 매매 종합 리포트 생성 완료: {output_file}"
                                )
                            except Exception:
                                pass
                        else:
                            try:
                                self.app.log("[Error] 리포트 파일 생성 실패")
                            except Exception:
                                pass

                    root.after(0, finish)

            self._narrative_report_busy = True
            try:
                for _nb in self._narrative_report_buttons():
                    _nb.state(["disabled"])
            except Exception:
                pass
            try:
                dlg.update_idletasks()
                px = root.winfo_rootx() + (root.winfo_width() // 2) - (dlg.winfo_width() // 2)
                py = root.winfo_rooty() + (root.winfo_height() // 2) - (dlg.winfo_height() // 2)
                dlg.geometry(f"+{max(0, px)}+{max(0, py)}")
            except Exception:
                pass
            threading.Thread(target=worker, daemon=True).start()
        except Exception as e:
            self._narrative_report_busy = False
            try:
                for _nb in self._narrative_report_buttons():
                    _nb.state(["!disabled"])
            except Exception:
                pass
            self.app.log(f"[Error] 리포트 생성 준비 오류: {e}")

    def generate_daily_settlement(self):
        """거래일 배치(빈·미완료 일자 채움) 후 당일 리포트 HTML 열기. 시작일은 UI에서 지정·기억."""
        try:
            top = tk.Toplevel(self.parent)
            top.title("일일 결산")
            top.resizable(False, False)
            frm = ttk.Frame(top, padding=12)
            frm.pack(fill=tk.BOTH, expand=True)

            default_start = self.app.settings.get("daily_settlement_batch_start_date") or ""
            if not default_start or len(str(default_start).strip()) < 10:
                default_start = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

            ttk.Label(frm, text="배치 시작일 (YYYY-MM-DD)").grid(row=0, column=0, sticky=tk.W)
            var = tk.StringVar(value=str(default_start)[:10])
            ent = ttk.Entry(frm, textvariable=var, width=16)
            ent.grid(row=0, column=1, padx=8)
            ttk.Label(
                frm,
                text="시작일~오늘, 거래일만, 최대 31일 범위",
                font=("Malgun Gothic", 8),
            ).grid(row=1, column=0, columnspan=2, sticky=tk.W)

            def on_ok() -> None:
                try:
                    s = var.get().strip()[:10]
                    datetime.strptime(s, "%Y-%m-%d")
                except Exception:
                    messagebox.showerror("오류", "날짜 형식은 YYYY-MM-DD 입니다.")
                    return
                self.app.settings["daily_settlement_batch_start_date"] = s
                try:
                    self.app.config_handler.save_settings(self.app.settings)
                except Exception as e:
                    self.app.log(f"[Warn] 설정 저장 실패: {e}")
                top.destroy()
                self._run_daily_settlement_batch_worker(s)

            ttk.Button(frm, text="실행", command=on_ok).grid(row=2, column=1, sticky=tk.E, pady=(10, 0))
            ttk.Button(frm, text="취소", command=top.destroy).grid(row=2, column=0, sticky=tk.W, pady=(10, 0))
            ent.focus_set()
        except Exception as e:
            self.app.log(f"[Error] 일일 결산 UI 오류: {e}")

    def _run_daily_settlement_batch_worker(self, start_s: str) -> None:
        """백그라운드에서 일괄 생성 후 브라우저로 열기."""
        def job() -> None:
            try:
                import webbrowser
                from pathlib import Path

                from reporting.daily_settlement_report import run_daily_settlement_batch

                def log(msg: str) -> None:
                    try:
                        self.app.log(msg)
                    except Exception:
                        pass

                res = run_daily_settlement_batch(self.app, start_s, log_callback=log)
                if not res.get("ok"):
                    err = res.get("error") or "알 수 없는 오류"
                    self.app.log(f"[일일결산] {err}")

                    def _show_err(msg: str) -> None:
                        try:
                            messagebox.showerror("일일 결산", msg)
                        except Exception:
                            pass

                    try:
                        self.parent.after(0, lambda m=err: _show_err(m))
                    except Exception:
                        _show_err(err)
                    return
                op = res.get("opened_path")
                if op and os.path.isfile(op):
                    webbrowser.open(Path(op).as_uri())
                gen_n = len(res.get("generated", []) or [])
                sk_n = len(res.get("skipped", []) or [])
                self.app.log(f"[일일결산] 완료 — 생성 {gen_n}건, 스킵 {sk_n}건, 열기: {op or '없음'}")
            except Exception as e:
                self.app.log(f"[Error] 일일결산 배치: {e}")
                try:
                    self.parent.after(0, lambda: messagebox.showerror("일일 결산", str(e)))
                except Exception:
                    pass

        threading.Thread(target=job, daemon=True).start()

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
            if t is None:
                continue
            # [v4.8.4] TCheckbutton 스타일은 메인에서 관리하므로 태그에 집중
            t.tag_configure("even", background=bg_even)
            t.tag_configure("odd", background=bg_odd)
            
            # [v4.7.5] 글로벌 일관성을 위해 고밀도 시인성 컬러 적용
            t.tag_configure("price_up", foreground=up_col)
            t.tag_configure("price_down", foreground=dn_col)
