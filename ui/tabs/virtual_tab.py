import tkinter as tk
import threading
import os
from tkinter import ttk, messagebox
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import config

class VirtualTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        # 전체 렌더 시 계산된 금일 실현 손익 — 틱 요약 갱신에 재사용
        self._cached_day_realized_pnl: float = 0.0
        self.vars = {}
        # [v4.5.3] 위젯 소유권 이전 (캡슐화)
        # 실시간 보유는 Treeview 대신 Canvas+Frame+Label 그리드 (행 소실/깜빡임 방지)
        self.tree_active = None
        self._active_grid_inner: Optional[tk.Frame] = None
        self._active_hold_canvas: Optional[tk.Canvas] = None
        self._active_grid_inner_win: Optional[int] = None
        self._active_row_frames: Dict[str, tk.Frame] = {}
        self._active_row_labels: Dict[str, List[tk.Label]] = {}
        self.tree_history = None
        self.tree_orders = None
        self.lbl_mode_badge = None
        self.btn_env = None
        self.lbl_mock_result = None
        
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
            ("금일 손익", "day", "#BF616A"),
            ("누적 수익", "cum", "#A3BE8C")
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
        ttk.Button(btn_group, text="📋 일일결산", width=10, command=self.generate_daily_settlement).pack(side=tk.LEFT, padx=2)
        ttk.Button(btn_group, text="⭐ 관심목록", width=10, command=self._open_watchlist_dialog).pack(side=tk.LEFT, padx=2)
        
        # [v3.9.5] 버튼명 '서버 전환'으로 변경
        self.btn_env = ttk.Button(btn_group, text="서버 전환", width=10, command=self._toggle_environment)
        self.btn_env.pack(side=tk.LEFT, padx=2)

        # --- [2] 메인 레이아웃 (3단 수직 분할) ---
        v_paned = ttk.PanedWindow(main_container, orient=tk.VERTICAL)
        v_paned.pack(fill=tk.BOTH, expand=True)

        # 1. 보유 현황
        active_f = ttk.LabelFrame(v_paned, text=" 💰 실시간 보유 현황 (Active) ", padding="5")
        v_paned.add(active_f, weight=4)
        self._build_active_holdings_grid(active_f)

        # 2. 매매 종료
        history_f = ttk.LabelFrame(v_paned, text=" ✅ 매매 종료 내역 (History) ", padding="5")
        v_paned.add(history_f, weight=3)
        self.tree_history = self._build_history_tree(history_f)

        # 3. 주문 내역 (통합된 주문 로그)
        order_f = ttk.LabelFrame(v_paned, text=" 📄 실시간 주문 로그 (Order Log) ", padding="5")
        v_paned.add(order_f, weight=3)
        self.tree_orders = self._build_order_tree(order_f)

        self._setup_styles()

    def _build_active_holdings_grid(self, parent: ttk.Widget) -> None:
        """실시간 보유: Treeview 대신 스크롤 가능한 라벨 그리드 (틱 갱신 시 행 단위 destroy 최소화)."""
        try:
            outer = ttk.Frame(parent)
            outer.pack(fill=tk.BOTH, expand=True)
            canvas = tk.Canvas(outer, highlightthickness=0, bg="#2E3440")
            sb = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=canvas.yview)
            inner = tk.Frame(canvas, bg="#2E3440")
            inner_win = canvas.create_window((0, 0), window=inner, anchor="nw")
            canvas.configure(yscrollcommand=sb.set)
            canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            sb.pack(side=tk.RIGHT, fill=tk.Y)

            def _on_inner_cfg(_evt: Any = None) -> None:
                try:
                    canvas.configure(scrollregion=canvas.bbox("all"))
                except Exception:
                    pass

            def _on_canvas_cfg(evt: tk.Event) -> None:
                try:
                    canvas.itemconfig(inner_win, width=int(evt.width))
                except Exception:
                    pass

            inner.bind("<Configure>", _on_inner_cfg)
            canvas.bind("<Configure>", _on_canvas_cfg)

            def _wheel(evt: tk.Event) -> None:
                try:
                    canvas.yview_scroll(int(-1 * (evt.delta / 120)), "units")
                except Exception:
                    pass

            canvas.bind("<MouseWheel>", _wheel)
            inner.bind("<MouseWheel>", _wheel)

            hdr_bg = "#3B4252"
            hf = tk.Frame(inner, bg=hdr_bg)
            hf.pack(fill=tk.X)
            headers = [
                ("코드", 9),
                ("종목명", 14),
                ("수량", 6),
                ("평단가", 10),
                ("매입가", 12),
                ("현재가", 10),
                ("평가금", 12),
                ("손익금", 12),
                ("수익률", 8),
                ("진입일시", 16),
            ]
            for i, (text, wch) in enumerate(headers):
                tk.Label(
                    hf,
                    text=text,
                    bg=hdr_bg,
                    fg="#ECEFF4",
                    font=("Malgun Gothic", 8, "bold"),
                    width=wch,
                    anchor="w",
                ).grid(row=0, column=i, padx=2, pady=3, sticky="w")

            self._active_grid_inner = inner
            self._active_hold_canvas = canvas
            self._active_grid_inner_win = inner_win
        except Exception as e:
            try:
                print(f"[UI Error] _build_active_holdings_grid: {e}")
            except Exception:
                pass
            self._active_grid_inner = None
            self._active_hold_canvas = None
            self._active_grid_inner_win = None

    def _build_history_tree(self, parent):
        cols = ("Time", "Code", "Name", "Qty", "BuyP", "SellP", "PnL", "Profit", "Reason")
        tree = ttk.Treeview(parent, columns=cols, show="headings", selectmode="browse")
        heads = ["종료일시", "코드", "종목명", "수량", "매수가", "매도가", "손익금", "수익률", "사유"]
        for col, text in zip(cols, heads):
            tree.heading(col, text=text)
            tree.column(col, width=85, anchor=tk.CENTER)
        tree.column("Time", width=152, anchor=tk.CENTER)
        tree.column("Name", width=110, anchor=tk.W)
        tree.column("PnL", width=90, anchor=tk.E)
        tree.column("Reason", width=120, anchor=tk.W)
        
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
        tree.column("Reason", width=120, anchor=tk.W)
        tree.column("BAOrd", anchor=tk.E)

        # 호가(매도/매수/비율) 열이 넓어 가로 스크롤 제공
        scroll_y = ttk.Scrollbar(wrap, orient=tk.VERTICAL, command=tree.yview)
        scroll_x = ttk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=tree.xview)
        tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        tree.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        return tree

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

    def _watchlist_hide_codes(self) -> set:
        """관심목록 종목은 가상포지션 탭에서 숨김 (감시목록과 별도, 전체 관심 코드)."""
        try:
            return self.app.watchlist_store.codes_set()
        except Exception:
            return set()

    def _watchlist_report_exclude_codes(self) -> set:
        """나러티브 리포트에서만 제외: 관심만·비감시. 감시와 겹치면 리포트에 포함."""
        try:
            mc = getattr(self.app.trading_controller, "monitor_codes", None) or []
        except Exception:
            mc = []
        try:
            return self.app.watchlist_store.codes_report_exclusion_set(mc)
        except Exception:
            return set()

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
        """보유 행 표시 순서를 종목코드 기준으로 고정(줄무늬 태그·인덱스 흔들림 방지)."""
        try:
            merged = self._vp_items_by_normalized()
            wl = self._watchlist_hide_codes()
            rows = [(nk, p) for nk, p in merged.items() if nk not in wl]
            return sorted(rows, key=lambda x: x[0])
        except Exception:
            return []

    def _active_row_vals_and_tags(
        self, row_idx: int, nk: str, pos: Dict[str, Any], info: Dict[str, Any]
    ) -> Tuple[tuple, Tuple[str, ...], float]:
        """실시간 보유 한 행의 values·tags·수익률% (그리드/요약 공통)."""
        try:
            raw_p = info.get("price", pos.get("avg_price", 0))
            curr_p = float(raw_p) if raw_p not in (None, "", 0) else float(pos.get("avg_price", 0) or 0)
        except (TypeError, ValueError):
            curr_p = float(pos.get("avg_price", 0) or 0)
        try:
            ap = float(pos.get("avg_price", 0) or 0)
        except (TypeError, ValueError):
            ap = 0.0
        qty = int(pos.get("qty", 0) or 0)
        if ap <= 0:
            ap = curr_p
        profit_pct = (curr_p / ap - 1) * 100 if ap else 0.0
        eval_amt = curr_p * qty
        unrealized = (curr_p - ap) * qty
        buy_in_amt = int(round(float(ap) * float(qty)))
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
            f"{int(eval_amt):+,}",
            f"{int(unrealized):+,}",
            p_str,
            entry_disp,
        )
        # 전체 렌더와 동일하게 항상 (줄무늬, 방향) 2튜플
        return vals, (tag_base, p_tag), float(profit_pct)

    def _active_grid_ready(self) -> bool:
        return self._active_grid_inner is not None

    def _profit_label_fg(self, profit_pct: float) -> str:
        if profit_pct > 0:
            return "#E35F5F"
        if profit_pct < 0:
            return "#81A1C3"
        return "#ECEFF4"

    def _remove_active_grid_row(self, nk: str) -> None:
        """보유 그리드에서 종목 행 제거."""
        try:
            fr = self._active_row_frames.pop(nk, None)
            self._active_row_labels.pop(nk, None)
            if fr is not None:
                fr.destroy()
        except Exception:
            pass

    def _upsert_active_grid_row(
        self, nk: str, row_idx: int, vals: Tuple[Any, ...], profit_pct: float
    ) -> None:
        """종목별 행 프레임이 없으면 생성, 라벨 텍스트·색만 갱신."""
        try:
            inner = self._active_grid_inner
            if inner is None or len(vals) < 10:
                return
            bg_even = "#242933"
            bg_odd = "#3B4252"
            row_bg = bg_even if row_idx % 2 == 0 else bg_odd
            # 수익률 방향색: 코드~진입까지 동일 적용 (Treeview 태그와 동일한 행 단위 강조)
            fg_row = self._profit_label_fg(profit_pct)
            anchors = ("w", "w", "e", "e", "e", "e", "e", "e", "e", "w")

            labels = self._active_row_labels.get(nk)
            row_fr = self._active_row_frames.get(nk)
            if row_fr is None or labels is None or len(labels) != 10:
                try:
                    if row_fr is not None:
                        row_fr.destroy()
                except Exception:
                    pass
                row_fr = tk.Frame(inner, bg=row_bg)
                row_fr.pack(fill=tk.X)
                labels = []
                for col in range(10):
                    lb = tk.Label(
                        row_fr,
                        text="",
                        bg=row_bg,
                        fg=fg_row,
                        font=("Malgun Gothic", 9),
                        anchor=anchors[col],
                    )
                    wch = (9, 14, 6, 10, 12, 10, 12, 12, 8, 16)[col]
                    lb.grid(row=0, column=col, padx=2, pady=1, sticky="ew")
                    try:
                        lb.configure(width=wch)
                    except Exception:
                        pass
                    labels.append(lb)
                self._active_row_frames[nk] = row_fr
                self._active_row_labels[nk] = labels

            # 배경(줄무늬)·텍스트·손익 색 동기화
            row_fr = self._active_row_frames[nk]
            row_fr.configure(bg=row_bg)
            texts = [str(vals[i]) for i in range(10)]
            for col, lb in enumerate(self._active_row_labels[nk]):
                try:
                    lb.configure(text=texts[col], bg=row_bg, fg=fg_row)
                except Exception:
                    pass
        except Exception as e:
            try:
                print(f"[UI Error] _upsert_active_grid_row: {e}")
            except Exception:
                pass

    def refresh_portfolio_light(self) -> None:
        """
        포지션 구조(종목·수량·평단)는 그대로일 때만 사용.
        히스토리·주문 트리 전체 delete/재삽입 없이 보유 행·상단 요약만 갱신합니다.
        (실시간 감시 탭의 틱 단위 item 갱신과 같은 패턴으로 깜빡임을 줄입니다.)
        """
        try:
            if not self._active_grid_ready():
                return
            merged = self._vp_items_by_normalized()
            wl = self._watchlist_hide_codes()
            for nk in merged:
                if nk in wl:
                    continue
                if nk not in self._active_row_frames:
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
            vals, _tags, profit_pct = self._active_row_vals_and_tags(idx, nk, pos, info)
            try:
                raw_cp = info.get("price", pos.get("avg_price", 0))
                curr_p = (
                    float(raw_cp)
                    if raw_cp not in (None, "", 0)
                    else float(pos.get("avg_price", 0) or 0)
                )
            except (TypeError, ValueError):
                curr_p = float(pos.get("avg_price", 0) or 0)
            try:
                ap = float(pos.get("avg_price", 0) or 0)
            except (TypeError, ValueError):
                ap = 0.0
            qty = int(pos.get("qty", 0) or 0)
            if ap <= 0:
                ap = curr_p
            total_eval += curr_p * qty
            total_invested += float(pos.get("invested", 0) or 0)
            total_unrealized_pnl += (curr_p - ap) * qty

            updated_nks.add(nk)
            if self._active_grid_ready():
                self._upsert_active_grid_row(nk, idx, vals, profit_pct)

        # 그리드에만 남은 행: VP 스냅샷에 종목이 없을 때만 제거
        if not skip_orphan_delete and self._active_grid_ready():
            for nk in list(self._active_row_frames.keys()):
                if nk in updated_nks:
                    continue
                if nk not in merged_vp:
                    self._remove_active_grid_row(nk)
            try:
                c = self._active_hold_canvas
                if c is not None:
                    c.update_idletasks()
                    c.configure(scrollregion=c.bbox("all"))
            except Exception:
                pass

        # 2. 종료 종목
        wl_hide = self._watchlist_hide_codes()
        total_realized_pnl = 0
        total_day_realized_pnl = 0
        today_str = datetime.now().strftime("%Y-%m-%d")
        
        self.tree_history.delete(*self.tree_history.get_children())
        hist_visible: List[Dict[str, Any]] = []
        for pos in self.app.portfolio.virtual_history:
            if str(pos.get("code", "")).zfill(6) in wl_hide:
                continue
            hist_visible.append(pos)
            pnl_amt = pos.get("realized_pnl_amt", 0)
            total_realized_pnl += pnl_amt
            if str(pos.get("entry_time", "")).startswith(today_str):
                total_day_realized_pnl += pnl_amt

        hist_visible.sort(key=self._history_exit_sort_ts, reverse=True)

        hist_row = 0
        for pos in hist_visible:
            last_event = pos["events"][-1] if pos.get("events") else {}
            pnl_amt = pos.get("realized_pnl_amt", 0)
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
                    f"{int(pnl_amt):+,}",
                    f"{pos.get('final_pnl_pct', 0):+.2f}%",
                    pos.get("reason", "SIGNAL"),
                ),
                tags=(tag_base, p_tag),
            )

        self._cached_day_realized_pnl = float(total_day_realized_pnl)

        # 3. 요약 정보 (Account Summary - REST API Synced) — 주문 로그는 위 0단계에서 이미 반영
        portfolio = self.app.portfolio
        current_cash = getattr(portfolio, "actual_deposit", 0)
        stock_eval = float(total_eval)  # 그리드 실시간 평가 합 (= '주식 평가금' 행)
        total_assets = self._compute_virtual_tab_total_assets(portfolio, stock_eval)

        day_pnl_total = total_day_realized_pnl + total_unrealized_pnl
        start_cap = float(
            str(self.app.settings.get("backtest_initial_capital", 10000000)).replace(",", "")
        )
        try:
            ta = float(total_assets)
        except (TypeError, ValueError):
            ta = 0.0
        # 금일 %: 장중 기준으로 '금일 손익 반영 전' 총자산 근사치 대비 (누적%와 분리)
        opening_eq = max(1.0, ta - float(day_pnl_total))
        day_pnl_pct = (float(day_pnl_total) / opening_eq) * 100.0

        # 누적 수익: 설정 초기자본 대비 (금액 + 초기자본 대비 %)
        cum_pnl_amt = ta - start_cap
        cum_pnl_pct = (cum_pnl_amt / start_cap) * 100.0 if start_cap > 1e-9 else 0.0

        self.vars["total"].set(f"{total_assets:,.0f}")
        self.vars["cash"].set(f"{current_cash:,.0f}")
        self.vars["stock"].set(f"{stock_eval:,.0f}")
        
        # [v4.5.3] D+1, D+2 예상 예수금 업데이트
        self.vars["d1_cash"].set(f"{getattr(portfolio, 'd1_deposit', 0):,.0f}")
        self.vars["d2_cash"].set(f"{getattr(portfolio, 'd2_deposit', 0):,.0f}")
            
        self.vars["day"].set(f"{int(day_pnl_total):+,} ({day_pnl_pct:+.2f}%)")
        self.vars["cum"].set(f"{int(cum_pnl_amt):+,} ({cum_pnl_pct:+.2f}%)")

    def _render_real_data(self):
        """실시간 틱·동기화 후 보유 그리드 라벨만 갱신 (Treeview 미사용)."""
        if not self._active_grid_ready():
            return
        try:
            mi = getattr(self.app, "monitor_info", None) or {}
            for idx, (nk, pos) in enumerate(self._sorted_active_holdings()):
                info = mi.get(nk, {}) or {}
                vals, _tags, profit_pct = self._active_row_vals_and_tags(idx, nk, pos, info)
                self._upsert_active_grid_row(nk, idx, vals, profit_pct)
            try:
                c = self._active_hold_canvas
                if c is not None:
                    c.configure(scrollregion=c.bbox("all"))
            except Exception:
                pass
        except Exception as e:
            try:
                print(f"[UI Error] _render_real_data: {e}")
            except Exception:
                pass

        self._update_summary_after_tick()

    def _update_summary_after_tick(self) -> None:
        """틱 직후 상단 요약(주식평가금·금일손익·총자산 등)만 재계산합니다."""
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

            wl_hide = self._watchlist_hide_codes()
            mi = getattr(self.app, "monitor_info", None) or {}
            total_eval = 0.0
            total_unrealized_pnl = 0.0
            for nk, pos in self._vp_items_by_normalized().items():
                if nk in wl_hide:
                    continue
                info = mi.get(nk, {}) or {}
                curr_p = float(info.get("price", pos.get("avg_price", 0)))
                if curr_p <= 0:
                    curr_p = float(pos.get("avg_price", 0))
                q = int(pos.get("qty", 0))
                ap = float(pos.get("avg_price", 0))
                total_eval += curr_p * q
                total_unrealized_pnl += (curr_p - ap) * q

            portfolio = self.app.portfolio
            current_cash = float(getattr(portfolio, "actual_deposit", 0) or 0)
            stock_eval = float(total_eval)
            total_assets = self._compute_virtual_tab_total_assets(portfolio, stock_eval)

            day_pnl_total = float(self._cached_day_realized_pnl) + total_unrealized_pnl
            start_cap = float(
                str(self.app.settings.get("backtest_initial_capital", 10000000)).replace(",", "")
            )
            try:
                ta = float(total_assets)
            except (TypeError, ValueError):
                ta = 0.0
            opening_eq = max(1.0, ta - float(day_pnl_total))
            day_pnl_pct = (float(day_pnl_total) / opening_eq) * 100.0 if opening_eq > 0 else 0.0

            cum_pnl_amt = ta - start_cap
            cum_pnl_pct = (cum_pnl_amt / start_cap) * 100.0 if start_cap > 1e-9 else 0.0

            _set_if_diff("total", f"{total_assets:,.0f}")
            _set_if_diff("cash", f"{current_cash:,.0f}")
            _set_if_diff("stock", f"{stock_eval:,.0f}")
            _set_if_diff("d1_cash", f"{getattr(portfolio, 'd1_deposit', 0):,.0f}")
            _set_if_diff("d2_cash", f"{getattr(portfolio, 'd2_deposit', 0):,.0f}")
            _set_if_diff("day", f"{int(day_pnl_total):+,} ({day_pnl_pct:+.2f}%)")
            _set_if_diff("cum", f"{int(cum_pnl_amt):+,} ({cum_pnl_pct:+.2f}%)")
        except Exception:
            pass

    def _compute_virtual_tab_total_assets(self, portfolio: Any, stock_eval: float) -> float:
        """
        가상포지션 탭 '총 자산' 표시.
        원칙: D+2 예수금(d2_deposit) + 주식 평가금(실시간 합) — 상단 두 숫자와 일치.
        D+2가 아직 0(미동기화 등)이면 API 총자산·예수금+평가 보정(_normalize)로 폴백.
        """
        try:
            d2 = float(getattr(portfolio, "d2_deposit", 0) or 0)
            se = float(stock_eval)
            if d2 > 0:
                return d2 + se
            te = float(getattr(portfolio, "total_equity", 0) or 0)
            cash = float(getattr(portfolio, "actual_deposit", 0) or 0)
            if te <= 0:
                return max(0.0, cash + se)
            return self._normalize_total_assets_display(te, cash, se)
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
        """DB 기반 실시간 주문 내역 동기화 (주문 시점 VP·호가만 표시)."""
        if not getattr(self, "tree_orders", None):
            return
        self.tree_orders.delete(*self.tree_orders.get_children())
        try:
            orders = self.app.data_manager.get_today_orders()
            wl_hide = self._watchlist_hide_codes()
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
                if str(code).zfill(6) in wl_hide:
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
                        result,
                        reason,
                    ),
                    tags=(tag_base, side_tag),
                )
        except Exception as e:
            try:
                print(f"[UI Error] _render_orders: {e}")
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
            messagebox.showinfo("설정 변경 완료", f"성공적으로 [{target_mode}] 모드로 설정되었습니다.\n\n재시작하면 {target_mode} 서버에서 시작합니다.")
            
            mode_bg = "#5E81AC" if new_mock else "#BF616A" 
            mode_text = " 모의투자모드 " if new_mock else " 실전모드 "
            self.lbl_mode_badge.config(text=mode_text, background=mode_bg)
            
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

    def generate_report(self):
        """[v4.5.3] 가상 매매 나러티브 리포트 생성 (UI 위임)"""
        try:
            from reporting.virtual_report import generate_virtual_narrative_report
            import webbrowser
            
            output_file = "virtual_trade_report.html"
            excl = self._watchlist_report_exclude_codes()
            hist_vis = [
                h
                for h in self.app.portfolio.virtual_history
                if str(h.get("code", "")).zfill(6) not in excl
            ]
            generate_virtual_narrative_report(hist_vis, output_file, self.app.settings)
            
            abs_path = os.path.abspath(output_file)
            if os.path.exists(abs_path):
                webbrowser.open(f"file://{abs_path}")
                self.app.log(f"[Sync] 가상 매매 나러티브 리포트 생성 완료: {output_file}")
            else:
                self.app.log("[Error] 리포트 파일 생성 실패")
        except Exception as e:
            self.app.log(f"[Error] 리포트 생성 중 오류: {e}")

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
        
        for t in [self.tree_history, self.tree_orders]:
            # [v4.8.4] TCheckbutton 스타일은 메인에서 관리하므로 태그에 집중
            t.tag_configure("even", background=bg_even)
            t.tag_configure("odd", background=bg_odd)
            
            # [v4.7.5] 글로벌 일관성을 위해 고밀도 시인성 컬러 적용
            t.tag_configure("price_up", foreground=up_col)
            t.tag_configure("price_down", foreground=dn_col)
