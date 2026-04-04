import tkinter as tk
from tkinter import ttk, messagebox
import json
import os
import pandas as pd
import typing as tp
from core.defaults import DEFAULT_SETTINGS

class SettingsTab:
    def __init__(self, parent, app, default_settings):
        self.parent = parent
        self.app = app
        self.default_settings = default_settings
        self.params_entries = {}
        self.conf_vars = {}
        self.type_a_widgets = []
        self.type_b_widgets = []
        self.s2_a_widgets = []
        self.s2_b_widgets = []
        self.mkt_gate_widgets = []
        self.grid_rows = {} 
        self.sizing_rows = []
        self.opt_sync_tasks = [] 
        self.window_id: tp.Any = None 
        
        self.colors = self.app.colors
        self._setup_ui()

    def setup_section_columns(self, frame):
        # [v4.16.16] 최적화 아이콘을 설명(Hint) 앞으로 이동하여 가시성 확보
        frame.columnconfigure(0, weight=0, minsize=40)   # 체크박스
        frame.columnconfigure(1, weight=0, minsize=220)  # 라벨
        frame.columnconfigure(2, weight=0, minsize=100)  # 입력창
        frame.columnconfigure(3, weight=0, minsize=60)   # 단위
        frame.columnconfigure(4, weight=0, minsize=80)   # 최적화 토글 (중간에 배치하여 잘림 방지)
        frame.columnconfigure(5, weight=1, minsize=200)  # 설명 (가로 확장)

    def _bind_opt_interaction(
        self,
        widget,
        key: str,
        grid_row: int | None = None,
        grid_parent: tp.Any = None,
        *,
        pack_in: tp.Any | None = None,
        peer_keys: tuple[str, ...] = (),
        bind_widget_rclick: bool = True,
    ) -> None:
        """[v16.50] 최적화 ○/● 아이콘. peer_keys: 동일 입력행 묶음 스타일. bind_widget_rclick=False 이면 입력창 우클릭 미연동(사이징 행 이중 키 시 덮어쓰기 방지)."""
        if not hasattr(self.app, 'opt_params_vars') or key not in self.app.opt_params_vars:
            return

        bt = getattr(self.app, 'backtest_tab', None)
        bg_col = self.colors.get('bg', '#2E3440')
        fg_normal = "#616E88"  # Eligible (Grey)
        fg_active = "#8FBCBB"  # Active (Mint)
        fg_hover = "#88C0D0"  # Hover (Bright Blue)

        opt_icon = tk.Label(
            pack_in if pack_in is not None else grid_parent,
            text="○",
            cursor="hand2",
            fg=fg_normal,
            bg=bg_col,
            font=('Malgun Gothic', 12),
        )
        if pack_in is not None:
            opt_icon.pack(side=tk.LEFT, padx=(6, 0))
        else:
            if grid_parent is None or grid_row is None:
                return
            opt_icon.grid(row=grid_row, column=4, padx=10)

        def _any_peer_active() -> bool:
            try:
                keys = (key,) + peer_keys
                for k in keys:
                    if k in self.app.opt_params_vars and self.app.opt_params_vars[k].get():
                        return True
            except (tk.TclError, AttributeError, KeyError):
                pass
            return False

        def sync_style() -> None:
            try:
                is_active = bool(self.app.opt_params_vars[key].get())
                if _any_peer_active():
                    widget.configure(style="Opt.TEntry")
                else:
                    widget.configure(style="TEntry")
                opt_icon.config(text="●" if is_active else "○", fg=fg_active if is_active else fg_normal)
            except (tk.TclError, AttributeError):
                pass

        def on_toggle(event: object | None = None) -> None:
            try:
                current = bool(self.app.opt_params_vars[key].get())
                self.app.opt_params_vars[key].set(not current)
                for t in self.opt_sync_tasks:
                    try:
                        t()
                    except Exception:
                        pass
                if bt and hasattr(bt, '_refresh_columns_from_settings'):
                    bt._refresh_columns_from_settings()
                self.app.settings[f"opt_{key}"] = not current
                state_str = "지정" if not current else "해제"
                self.status_label.config(text=f"[{key}] 최적화 대상 {state_str} 완료")
                self.app.config_handler.save_settings(self.app.settings)
            except Exception as e:
                try:
                    self.app.log(f"최적화 토글 오류({key}): {e}")
                except Exception:
                    pass

        opt_icon.bind("<Button-1>", on_toggle)
        opt_icon.bind("<Button-3>", on_toggle)
        opt_icon.bind("<Enter>", lambda e: opt_icon.config(fg=fg_hover))
        opt_icon.bind("<Leave>", lambda e: sync_style())
        if bind_widget_rclick:
            widget.bind("<Button-3>", on_toggle)

        self.parent.after(500, sync_style)
        self.opt_sync_tasks.append(sync_style)

    def clear_all_optimization(self):
        """[v26.0] 최적화 전체 해제 — opt_params_vars 전부 False."""
        # 1. 중앙 저장소의 모든 최적화 변수를 False로 리셋하고 설정 파일에 영구 등록
        for key, var in self.app.opt_params_vars.items():
            var.set(False)
            self.app.settings[f"opt_{key}"] = False
            
        # 2. UI 동기화 (원형 토글 버튼 ○ 형태로 전체 리셋)
        for task in self.opt_sync_tasks: 
            try: task()
            except: pass
            
        # 3. 백테스팅 탭 컬럼 즉시 비우기 (기본 지표만 남김)
        bt = getattr(self.app, 'backtest_tab', None)
        if bt and hasattr(bt, '_refresh_columns_from_settings'):
            bt._refresh_columns_from_settings()
            
        # 4. 사용자 안내 및 업데이트
        self.status_label.config(text="🧹 모든 최적화 선택이 해제되었습니다.")
        self.save_settings(sync_to_backtest=False)

    def _setup_ui(self) -> None:
        """설정 탭: 좌측 사이드바(전략 흐름) + 우측 섹션별 패널(스크롤)."""
        bg_col = self.colors.get('bg', '#2E3440')
        txt_col = self.colors.get('fg', '#ECEFF4')
        primary_col = self.colors.get('primary', '#88C0D0')
        secondary_col = self.colors.get('secondary', '#81A1C1')
        nord_box = '#3B4252'
        nord_deep = '#242933'

        style = ttk.Style()
        style.configure('Dimmed.TLabel', foreground='#666666')
        style.configure('Dimmed.TEntry', foreground='#666666')
        style.map('Dimmed.TEntry', fieldbackground=[('disabled', '#3B4252')])

        # ── 하단 버튼 (먼저 pack → 상단 본문이 남은 공간 채움) ──
        btn_frame = ttk.Frame(self.parent, padding='20')
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(btn_frame, text='💾 설정 저장 및 즉시 적용', command=self.save_settings, width=30, style='Action.TButton').pack(side=tk.RIGHT, padx=10)
        ttk.Button(btn_frame, text='🔄 공장 초기화', command=self.reset_to_defaults, width=20).pack(side=tk.RIGHT, padx=10)

        # ── 헤더 ──
        header_sticky = ttk.Frame(self.parent, padding=(25, 12, 25, 8))
        header_sticky.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(header_sticky, text='⚙️ 전략 파라미터', font=('Malgun Gothic', 18, 'bold'), foreground=primary_col).pack(side=tk.LEFT)

        guide_f = ttk.Frame(header_sticky)
        guide_f.pack(side=tk.LEFT, padx=24)
        ttk.Label(
            guide_f,
            text='💡 좌측에서 단계 선택 · ○/● 최적화는 입력창 우클릭',
            font=('Malgun Gothic', 9, 'italic'),
            foreground=secondary_col,
        ).pack(side=tk.LEFT)

        ttk.Button(header_sticky, text='🧹 최적화 전체 해제', width=18, command=self.clear_all_optimization).pack(side=tk.LEFT, padx=10)

        self.status_label = ttk.Label(header_sticky, text='', font=('Malgun Gothic', 10), foreground=secondary_col)
        self.status_label.pack(side=tk.RIGHT)

        # ── 본문: Paned (사이드바 | 스크롤 콘텐츠) ──
        main_body = ttk.Frame(self.parent)
        main_body.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        paned = ttk.Panedwindow(main_body, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        sidebar_outer = ttk.Frame(paned, padding=(10, 8, 6, 8))
        paned.add(sidebar_outer, weight=0)

        scroll_host = ttk.Frame(paned, padding=0)
        paned.add(scroll_host, weight=1)

        try:
            paned.pane(sidebar_outer, width=248)
        except tk.TclError:
            try:
                paned.paneconfigure(sidebar_outer, width=248)
            except tk.TclError:
                pass

        # 사이드바: 파이프라인 안내 + 목록
        flow_wrap = tk.Frame(sidebar_outer, bg=nord_deep, highlightthickness=1, highlightbackground=nord_box)
        flow_wrap.pack(fill=tk.X, pady=(0, 10))
        tk.Label(
            flow_wrap,
            text='전략 파이프라인',
            fg=primary_col,
            bg=nord_deep,
            font=('Malgun Gothic', 9, 'bold'),
        ).pack(anchor='w', padx=10, pady=(8, 2))
        tk.Label(
            flow_wrap,
            text=(
                'KODEX200 유니버스\n'
                '  ↓ ② 유니버스 종목선정 (A/B)\n'
                '  ↓ ③ S1 (공통필터 → A → B)\n'
                '  ↓ ④ Stage 2 추매 (A/B)\n'
                '  ↓ ⑤⑥ 수급·시장\n'
                '  ↓ ⑦ 청산'
            ),
            fg='#D8DEE9',
            bg=nord_deep,
            font=('Malgun Gothic', 9),
            justify=tk.LEFT,
        ).pack(anchor='w', padx=10, pady=(0, 8))

        sb_list = tk.Listbox(
            sidebar_outer,
            height=12,
            font=('Malgun Gothic', 10),
            bg=nord_box,
            fg=txt_col,
            selectbackground='#5E81AC',
            selectforeground=txt_col,
            activestyle='none',
            highlightthickness=0,
            relief='flat',
            bd=0,
            exportselection=False,
        )
        sb_list.pack(fill=tk.BOTH, expand=True)

        section_entries: tp.List[tp.Tuple[str, str]] = [
            ('fund', '① 자금 · 사이징'),
            ('univ', '② 유니버스 · 종목선정'),
            ('s1', '③ Stage 1 진입 (공통/A/B)'),
            ('s2', '④ Stage 2 추매 (A/B)'),
            ('smart', '⑤ Smart Wall'),
            ('mkt', '⑥ 시장 게이트'),
            ('exit', '⑦ 청산 · 리스크 (A/B)'),
        ]
        self._settings_section_keys: list[str] = [k for k, _ in section_entries]
        self._section_by_key: dict[str, ttk.Frame] = {}
        for sid, label in section_entries:
            sb_list.insert(tk.END, f'  {label}')
        sb_list.selection_set(0)

        # 우측 스크롤 영역
        sb_right = ttk.Scrollbar(scroll_host, orient=tk.VERTICAL)
        canvas = tk.Canvas(scroll_host, background=bg_col, highlightthickness=0)
        sb_right.config(command=canvas.yview)
        canvas.configure(yscrollcommand=sb_right.set)
        sb_right.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        content_inner = ttk.Frame(canvas, padding=(16, 12, 28, 24))
        self.window_id = canvas.create_window((0, 0), window=content_inner, anchor='nw')

        def _on_canvas_configure(event: tk.Event) -> None:
            try:
                w = max(int(event.width) - 8, 520)
                canvas.itemconfig(self.window_id, width=w)
            except (tk.TclError, ValueError):
                pass

        canvas.bind('<Configure>', _on_canvas_configure)

        def _update_scrollregion(_event: tk.Event | None = None) -> None:
            try:
                canvas.configure(scrollregion=canvas.bbox('all'))
            except tk.TclError:
                pass

        content_inner.bind('<Configure>', _update_scrollregion)

        def _on_mousewheel(event: tk.Event) -> None:
            try:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')
            except tk.TclError:
                pass

        def _bind_wheel(_e: tk.Event | None = None) -> None:
            try:
                canvas.bind_all('<MouseWheel>', _on_mousewheel)
            except tk.TclError:
                pass

        def _unbind_wheel(_e: tk.Event | None = None) -> None:
            try:
                canvas.unbind_all('<MouseWheel>')
            except tk.TclError:
                pass

        scroll_host.bind('<Enter>', _bind_wheel)
        scroll_host.bind('<Leave>', _unbind_wheel)

        for sid, _lbl in section_entries:
            self._section_by_key[sid] = ttk.Frame(content_inner)

        def _show_settings_section(section_id: str) -> None:
            try:
                for fr in self._section_by_key.values():
                    fr.pack_forget()
                fr = self._section_by_key.get(section_id)
                if fr is not None:
                    fr.pack(fill=tk.BOTH, expand=True)
                content_inner.update_idletasks()
                _update_scrollregion()
                canvas.yview_moveto(0)
            except (tk.TclError, KeyError):
                pass

        def _on_sidebar_select(_event: tk.Event | None = None) -> None:
            try:
                sel = sb_list.curselection()
                if not sel:
                    return
                idx = int(sel[0])
                if 0 <= idx < len(self._settings_section_keys):
                    _show_settings_section(self._settings_section_keys[idx])
            except (tk.TclError, ValueError, IndexError):
                pass

        sb_list.bind('<<ListboxSelect>>', _on_sidebar_select)

        # 각 섹션별로 UI 빌드 (부모 프레임만 분리)
        def _sec_parent(sid: str) -> ttk.Frame:
            return self._section_by_key[sid]

        # --- Helper Methods ---
        def get_next_row(parent):
            if parent not in self.grid_rows:
                self.grid_rows[parent] = 0
                self.setup_section_columns(parent)
            curr = self.grid_rows[parent]
            self.grid_rows[parent] += 1
            return curr

        def add_header(parent, text):
            r = get_next_row(parent)
            ttk.Label(parent, text=text, font=('Malgun Gothic', 10, 'bold'), foreground=primary_col).grid(row=r, column=0, columnspan=6, sticky='w', pady=(15, 5))

        def add_param_row(
            parent,
            label,
            key=None,
            unit='',
            hint='',
            toggle_key=None,
            widget_list=None,
            group_toggle_widgets: bool = False,
        ):
            r = get_next_row(parent)
            if toggle_key:
                if toggle_key not in self.conf_vars:
                    def_val = self.default_settings.get(toggle_key, True)
                    self.conf_vars[toggle_key] = tk.BooleanVar(value=self.app.settings.get(toggle_key, def_val))
                cb = tk.Checkbutton(parent, variable=self.conf_vars[toggle_key], bg=bg_col, fg=txt_col, activebackground=bg_col, selectcolor='#5E81AC') 
                cb.grid(row=r, column=0, sticky='w', padx=(5, 0))
                if widget_list is not None and group_toggle_widgets:
                    widget_list.append(cb)
            
            lbl = ttk.Label(parent, text=label, font=('Malgun Gothic', 10), foreground=txt_col)
            lbl.grid(row=r, column=1, sticky='w', pady=4)
            
            if key:
                val = self.app.settings.get(key, self.default_settings.get(key, ''))
                ent = ttk.Entry(parent, width=12, justify='center', exportselection=0)
                ent.insert(0, str(val))
                ent.grid(row=r, column=2, sticky='w', padx=5)
                ent.bind("<FocusIn>", lambda e: ent.selection_clear())
                self.params_entries[key] = ent
                if widget_list is not None: widget_list.append(ent)
                self._bind_opt_interaction(ent, key, grid_row=r, grid_parent=parent)

            if unit:
                un = ttk.Label(parent, text=unit, font=('Malgun Gothic', 9), foreground=primary_col)
                un.grid(row=r, column=3, sticky='w')
                if widget_list is not None: widget_list.append(un)
            
            if hint:
                hn = ttk.Label(parent, text=f'  ({hint})', style='Dimmed.TLabel')
                hn.grid(row=r, column=5, sticky='w', padx=10)
                if widget_list is not None: widget_list.append(hn)
            if widget_list is not None: widget_list.append(lbl)

        def add_sizing_row(parent, label, amt_key, pct_key, unit_key):
            r = get_next_row(parent)
            ttk.Label(parent, text=label, font=('Malgun Gothic', 10), foreground=txt_col).grid(row=r, column=1, sticky='w', pady=4)
            frame = ttk.Frame(parent)
            frame.grid(row=r, column=2, columnspan=2, sticky='w')
            
            ent = ttk.Entry(frame, width=10, justify='center', exportselection=0)
            ent.pack(side=tk.LEFT, padx=(5, 2))
            ent.bind("<FocusIn>", lambda e: ent.selection_clear())

            amt_peers = (amt_key,) if amt_key in self.app.opt_params_vars else ()
            self._bind_opt_interaction(ent, pct_key, grid_row=r, grid_parent=parent, peer_keys=amt_peers)
            if amt_key in self.app.opt_params_vars:
                self._bind_opt_interaction(
                    ent,
                    amt_key,
                    grid_row=r,
                    grid_parent=parent,
                    pack_in=frame,
                    peer_keys=(pct_key,),
                    bind_widget_rclick=False,
                )

            if unit_key not in self.conf_vars:
                self.conf_vars[unit_key] = tk.StringVar(value=self.app.settings.get(unit_key, '원'))
            cb = ttk.Combobox(frame, textvariable=self.conf_vars[unit_key], values=['원', '%'], width=3, state='readonly')
            cb.pack(side=tk.LEFT)
            self.sizing_rows.append({'ent': ent, 'unit_var': self.conf_vars[unit_key], 'amt_key': amt_key, 'pct_key': pct_key})
            def on_unit_change(*args):
                u = self.conf_vars[unit_key].get()
                val = self.app.settings.get(amt_key if u == '원' else pct_key, '')
                ent.delete(0, tk.END)
                ent.insert(0, str(val))
            self.conf_vars[unit_key].trace_add('write', on_unit_change)
            on_unit_change()

        # UI 빌드 (그리드 순서대로 배치)
        cap_f = ttk.LabelFrame(_sec_parent('fund'), text=' 💰 [1] 자금 및 계좌 관리 ', padding='15')
        cap_f.pack(fill=tk.X, pady=10)
        add_param_row(cap_f, '백테스팅용 초기자금', 'backtest_initial_capital', '원', '시뮬레이션 시작 및 잔고 부족 시 기준')
        add_header(cap_f, '포지션 사이징 (배분 방식)')
        add_sizing_row(cap_f, '1단계(정찰) 비중', 'stage1_amt', 's1_equity_pct', 's1_sizing_unit')
        add_sizing_row(cap_f, '2단계(본대) 비중', 'stage2_amt', 's2_equity_pct', 's2_sizing_unit')
        add_param_row(
            cap_f,
            '동적백테 A/B 수 균형(신규 S1)',
            None,
            '',
            '보유 종목 수 비율이 목표를 넘는 타입의 신규 S1만 차단(동률이면 허용). 매수금액 불변. A·B 둘 다 켤 때만 적용',
            toggle_key='use_type_balance_gate',
        )
        add_param_row(
            cap_f,
            '  └ Type A 목표 비중',
            'type_balance_a_pct',
            '%',
            'Type B 목표 = (100 − A)%. 예: 60이면 A 60%·B 40%에 가깝게 유지',
        )
        add_param_row(cap_f, '가상 매매 자동화', None, '', '로직 포착 시 자동 진입 시뮬레이션', toggle_key='use_auto_virtual')

        _univ_root = _sec_parent('univ')
        sel_f = ttk.LabelFrame(_univ_root, text=' 🔍 Type A — 눌림목 (Track A 종목 선정) ', padding='15')
        sel_f.pack(fill=tk.X, expand=False, pady=(0, 12))
        add_param_row(
            sel_f,
            'Track A 활성화',
            None,
            '',
            'MA 수렴 + 눌림목 유니버스 선정. ON/OFF 시 Type A의 S1 진입(use_type_a)·S2 추매 전체(use_s2_master_a)와 동시에 맞춤',
            toggle_key='use_sel_track_a',
        )
        add_param_row(
            sel_f,
            '최대 감시 종목 수 (A)',
            'max_slots_a',
            '개',
            'Track A 일별 감시·선정 상한 (보유 종목 수 제한 아님)',
        )
        add_param_row(sel_f, '최소 거래대금', 'min_trde_val', '억', '5일 평균 거래대금 기준')
        add_param_row(sel_f, '이평선 수렴도 기준', 'ma_convergence', '%', '에너지 응축(Short/Mid) 확인 — Track A 정체성')
        add_param_row(sel_f, '종목 선정 RSI 하한', 'sel_rsi_min', '', '과매도권/반등 유니버스 확인 (기본값 30)')
        add_param_row(sel_f, '종목 선정 RSI 상한', 'sel_rsi_max', '', '유니버스 선정 탄성 확인 (기본값 70)')
        add_param_row(sel_f, '거래량 분출 비율', 'vol_ratio', '배', '전일 대비 분출 시점 포착')
        add_param_row(sel_f, '분출 확인 기간', 'vol_lookback', '일', '최근 N일 내 분출 이력 확인')
        add_param_row(
            sel_f,
            '일봉 MACD(OSC) 필터',
            None,
            '',
            '선정 시 일봉 OSC>0 필수 · MACD 고정 Fast 12 / Slow 26 / Signal 9 (변경 불가) · Track B와 동일',
        )

        sel_b_f = ttk.LabelFrame(_univ_root, text=' 🔍 Type B — 추세돌파 (Track B 종목 선정) ', padding='15')
        sel_b_f.pack(fill=tk.X, expand=False, pady=(0, 10))
        add_param_row(
            sel_b_f,
            'Track B 활성화',
            None,
            '',
            'MA 정배열 확산 + 추세돌파 유니버스 선정. ON/OFF 시 Type B의 S1 진입(use_type_b)·S2 추매 전체(use_s2_master_b)와 동시에 맞춤',
            toggle_key='use_sel_track_b',
        )
        add_param_row(
            sel_b_f,
            '최대 감시 종목 수 (B)',
            'max_slots_b',
            '개',
            'Track B 일별 감시·선정 상한 (보유 종목 수 제한 아님)',
        )
        add_param_row(sel_b_f, '최소 거래대금 (B)', 'sel_b_min_trde_val', '억', 'Track B 전용 거래대금 기준 (기본값 60억)')
        add_param_row(sel_b_f, '종목 선정 RSI 하한 (B)', 'sel_b_rsi_min', '', '추세·모멘텀 하한 (기본값 45) — Track B 정체성')
        add_param_row(sel_b_f, '종목 선정 RSI 상한 (B)', 'sel_b_rsi_max', '', '과열 방지 상한 (기본값 75)')
        add_param_row(sel_b_f, '거래량 분출 비율 (B)', 'sel_b_vol_ratio', '배', 'Track B 전용 거래량 분출 배수 (기본값 1.5)')
        add_param_row(sel_b_f, '분출 확인 기간 (B)', 'sel_b_vol_lookback', '일', 'Track B 전용 분출 이력 확인 기간 (기본값 10일)')
        add_param_row(
            sel_b_f,
            '일봉 MACD(OSC) 필터',
            None,
            '',
            'Track A와 동일: 선정 시 일봉 OSC>0 필수 · MACD 고정 Fast 12 / Slow 26 / Signal 9 (변경 불가)',
        )

        strat_f = ttk.LabelFrame(_sec_parent('s1'), text=' 🎯 [3] 1단계 진입 (S1) — 감시 후 매수 신호 ', padding='15')
        strat_f.pack(fill=tk.X, pady=10)

        add_header(strat_f, '── 단계 공통 필수 필터 (A/B 모두 적용) ──')
        add_param_row(
            strat_f,
            'S1 진입 RSI 하한',
            'buy_rsi_min',
            '',
            'Type A·B 15분 신호 모두 동일 하한 적용(use_rsi_min OFF면 0으로 완화) · Wilder RSI(14)',
            toggle_key='use_rsi_min',
        )
        add_param_row(
            strat_f,
            '15분봉 MACD 필터',
            None,
            '',
            '켜면 15분 OSC>0 필수(기본). MACD 고정 12/26/9 · 실시간 수급 모멘텀',
            toggle_key='use_macd_signal',
        )
        add_param_row(
            strat_f,
            'RSI 시그널 필터',
            None,
            '',
            'RSI(14) vs 시그널(9봉 단순이평) 등 — 지표 주기 UI 비노출·고정 14/9',
            toggle_key='use_rsi_sig_filter',
        )

        # Type A → Type B 순(세로): 동일 그리드 폭으로 가독성 유지
        add_header(strat_f, '── Type A: 눌림목 진입 조건 ──')
        add_param_row(strat_f, 'A 전용 사용', None, '', '중립/순풍 장세 사용 가능', toggle_key='use_type_a')
        add_param_row(
            strat_f,
            'RSI 상한선',
            'buy_rsi_max_a',
            '',
            '눌림 깊이 확인용 (기본 65) · 기준선 Wilder RSI(14)',
            widget_list=self.type_a_widgets,
        )
        # Type A Smart Buy(use_smart_buy_a)는 미사용 예정 — 설정 UI 비노출(로직·설정 키는 유지)
        add_param_row(strat_f, '이평 골든크로스', 'gc_window', '분', 'N분 내 GC 발생 여부', toggle_key='use_gc', widget_list=self.type_a_widgets)
        add_param_row(
            strat_f,
            'RSI 상향돌파',
            'rsi_window',
            '분',
            'N분 내 RSI(14) 50선 돌파 여부',
            toggle_key='use_rsi_signal',
            widget_list=self.type_a_widgets,
        )
        add_param_row(
            strat_f,
            'MA60 안전구역',
            None,
            '',
            'Type A 전용. (종가÷MA60)×100%가 아래 하한·상한 사이일 때만 통과 — MA60 지표값의 최소/최대가 아님',
            toggle_key='use_ma60_safe',
            widget_list=self.type_a_widgets,
        )
        add_param_row(
            strat_f,
            '  ㄴ 이격 하한(%)',
            'ma60_min',
            '%',
            '입력=허용 최소 이격%(기본 100≈60일선 가격 이상)',
            widget_list=self.type_a_widgets,
        )
        add_param_row(
            strat_f,
            '  ㄴ 이격 상한(%)',
            'ma60_max',
            '%',
            '입력=허용 최대 이격%(기본 110≈60일선 대비 +10%까지)',
            widget_list=self.type_a_widgets,
        )
        add_param_row(strat_f, '거래량 급증확인', 'vol_surge', '배', '전일 평균 대비 폭발력', toggle_key='use_vol_surge', widget_list=self.type_a_widgets)
        add_param_row(
            strat_f,
            '  ㄴ 전일동시간 평균 기간 (S1·A)',
            'prev_vol_lookback_s1_a',
            '일',
            '15분 S1 Type A: Prev_Day_Vol_S1_A 산출(거래일 N일). 구 prev_vol_lookback 미설정 시 폴백',
            widget_list=self.type_a_widgets,
        )
        add_param_row(strat_f, '시장레짐 Type A 보정(1차)', None, '', 'AGGRESSIVE=기준, UP/NEUTRAL에서 vol·MA60만 보수화', toggle_key='use_type_a_regime_phase1', widget_list=self.type_a_widgets)
        add_param_row(strat_f, '  ㄴ UP 거래량 +Δ', 'type_a_phase1_up_vol_surge_add', '배', '기준 vol_surge에 가산', widget_list=self.type_a_widgets)
        add_param_row(strat_f, '  ㄴ NEUTRAL 거래량 +Δ', 'type_a_phase1_neutral_vol_surge_add', '배', 'UP보다 크게', widget_list=self.type_a_widgets)
        add_param_row(strat_f, '  ㄴ UP MA60 폭 축소', 'type_a_phase1_up_ma60_shrink_pct', '%p', '하한↑·상한↓ 각각 이 만큼', widget_list=self.type_a_widgets)
        add_param_row(strat_f, '  ㄴ NEUTRAL MA60 폭 축소', 'type_a_phase1_neutral_ma60_shrink_pct', '%p', 'UP보다 크게', widget_list=self.type_a_widgets)

        add_header(strat_f, '── Type B: 돌파형 진입 조건 ──')
        add_param_row(strat_f, 'B 전용 사용', None, '', '순풍(UP) 장세 전용', toggle_key='use_type_b')
        r_sb = get_next_row(strat_f)
        ttk.Label(strat_f, text='동일 봉 A+B 동시 신호 시', font=('Malgun Gothic', 10), foreground=txt_col).grid(
            row=r_sb, column=1, sticky='w', pady=4
        )
        try:
            if 'same_bar_sig_priority' not in self.conf_vars:
                _d = str(self.default_settings.get('same_bar_sig_priority', 'confluence'))
                self.conf_vars['same_bar_sig_priority'] = tk.StringVar(
                    value=str(self.app.settings.get('same_bar_sig_priority', _d)).strip().lower()
                )
            _sb_vals = ('confluence', 'prefer_a', 'prefer_b')
            if self.conf_vars['same_bar_sig_priority'].get() not in _sb_vals:
                self.conf_vars['same_bar_sig_priority'].set('confluence')
            cb_samebar = ttk.Combobox(
                strat_f,
                textvariable=self.conf_vars['same_bar_sig_priority'],
                values=_sb_vals,
                width=14,
                state='readonly',
            )
            cb_samebar.grid(row=r_sb, column=2, sticky='w', padx=5)
            ttk.Label(
                strat_f,
                text='Sig_Type (합치도·A고정·B고정=구버전)',
                style='Dimmed.TLabel',
            ).grid(row=r_sb, column=5, sticky='w', padx=10)
        except Exception as e:
            try:
                if hasattr(self, 'app') and hasattr(self.app, 'log'):
                    self.app.log(f'동일봉 Sig_Type UI 생성 실패: {e}')
            except Exception:
                pass
        add_param_row(
            strat_f,
            'RSI 상한선',
            'buy_rsi_max_b',
            '',
            '돌파 강점 확인용 (기본 70) · 기준선 Wilder RSI(14)',
            widget_list=self.type_b_widgets,
        )
        add_param_row(strat_f, 'Smart Buy', None, '', '돌파 시 수급 필터 적용 (VP/BA)', toggle_key='use_smart_buy_b', widget_list=self.type_b_widgets)
        add_param_row(strat_f, '돌파 거래량', 'vol_surge_b', '배', '돌파 시점 폭발적 수급', toggle_key='use_type_b_vol', widget_list=self.type_b_widgets)
        add_param_row(
            strat_f,
            '  ㄴ 전일동시간 평균 기간 (S1·B)',
            'prev_vol_lookback_s1_b',
            '일',
            '15분 S1 Type B: Prev_Day_Vol_S1_B. 구 prev_vol_lookback_b 미설정 시 폴백',
            widget_list=self.type_b_widgets,
        )
        add_param_row(
            strat_f,
            '시장레짐 Type B 보정(1차)',
            None,
            '',
            'AGGRESSIVE=기준, UP/NEUTRAL에서 vol_surge_b만 보수화(A와 독립)',
            toggle_key='use_type_b_regime_phase1',
            widget_list=self.type_b_widgets,
        )
        add_param_row(
            strat_f,
            '  ㄴ UP 거래량 +Δ(B)',
            'type_b_phase1_up_vol_surge_add',
            '배',
            '기준 vol_surge_b에 가산',
            widget_list=self.type_b_widgets,
        )
        add_param_row(
            strat_f,
            '  ㄴ NEUTRAL 거래량 +Δ(B)',
            'type_b_phase1_neutral_vol_surge_add',
            '배',
            'UP보다 크게',
            widget_list=self.type_b_widgets,
        )
        add_param_row(strat_f, '추세 정렬 확인', None, '', '일봉/1분봉 추세 일치 여부', toggle_key='use_trend_aligned_b', widget_list=self.type_b_widgets)
        add_param_row(strat_f, 'MACD GC 확인', None, '', '추가 모멘텀(GC) 확인', toggle_key='use_macd_gc_b', widget_list=self.type_b_widgets)

        s2_f = ttk.LabelFrame(_sec_parent('s2'), text=' 📈 [4] S2 — Type A 전용 (s2_* 만) ', padding='15')
        s2_f.pack(fill=tk.X, pady=10)
        add_param_row(
            s2_f,
            '★ Type A 추매(S2) 전체',
            None,
            '',
            'OFF면 A 종목 2차 매수 전부 차단(백테스트·동적·가상 동일)',
            toggle_key='use_s2_master_a',
        )
        add_param_row(
            s2_f,
            '1. 20일선 지지 확인',
            's2_ma20_gap',
            '%',
            toggle_key='use_s2_ma20',
            widget_list=self.s2_a_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            s2_f,
            '2. 5일선 회복/지지',
            's2_ma5_gap',
            '%',
            toggle_key='use_s2_ma5',
            widget_list=self.s2_a_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            s2_f,
            '3. RSI 시그널 확인',
            's2_rsi_gap',
            '%',
            toggle_key='use_s2_rsi_sig',
            hint='RSI(14)가 시그널(9봉 MA) 대비 상향 돌파',
            widget_list=self.s2_a_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            s2_f,
            '4. MACD GC 확인',
            's2_macd_gap',
            '',
            toggle_key='use_s2_macd_gc',
            hint='MACD가 시그널 상향 돌파',
            widget_list=self.s2_a_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            s2_f,
            '5. 거래량 급증 확인',
            's2_vol_ratio',
            '배',
            toggle_key='use_s2_vol_surge',
            hint='이전 평균 거래량 대비 배수',
            widget_list=self.s2_a_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            s2_f,
            '  ㄴ 전일동시간 평균 기간 (S2·A)',
            'prev_vol_lookback_s2_a',
            '일',
            'S2 Type A 거래량 조건: Prev_Day_Vol_S2_A. 구 prev_vol_lookback 미설정 시 폴백',
            widget_list=self.s2_a_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            s2_f,
            '6. 최소 수익률 확보',
            's2_profit_limit',
            '%',
            toggle_key='use_s2_profit',
            hint='진입가 대비 최소 수익률',
            widget_list=self.s2_a_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            s2_f,
            '7. 추매 Smart Buy (Type A)',
            None,
            '',
            '켜면 [5] VP/BA로 Type A 추매만 필터',
            toggle_key='use_smart_buy_s2',
            widget_list=self.s2_a_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            s2_f,
            '8. 진입 후 대기 시간',
            's2_delay_hours',
            '시간',
            '최소 보유 시간 허들',
            toggle_key='use_s2_delay',
            widget_list=self.s2_a_widgets,
            group_toggle_widgets=True,
        )

        s2b_f = ttk.LabelFrame(_sec_parent('s2'), text=' 📈 [4-B] S2 — Type B 전용 (s2_b_* 만) ', padding='15')
        s2b_f.pack(fill=tk.X, pady=10)
        add_header(s2b_f, 'Type B 종목은 아래 s2_b_* 만 사용합니다 (s2_* 와 독립)')
        add_param_row(
            s2b_f,
            '★ Type B 추매(S2) 전체',
            None,
            '',
            'OFF면 B 종목 2차 매수 전부 차단(백테스트·동적·가상 동일)',
            toggle_key='use_s2_master_b',
        )
        add_param_row(
            s2b_f,
            '1. 20일선 지지 확인 (B)',
            's2_b_ma20_gap',
            '%',
            toggle_key='use_s2_b_ma20',
            widget_list=self.s2_b_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            s2b_f,
            '2. 5일선 회복/지지 (B)',
            's2_b_ma5_gap',
            '%',
            toggle_key='use_s2_b_ma5',
            widget_list=self.s2_b_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            s2b_f,
            '3. RSI 시그널 확인 (B)',
            's2_b_rsi_gap',
            '%',
            toggle_key='use_s2_b_rsi_sig',
            hint='RSI(14)가 시그널(9봉 MA) 대비 상향 돌파',
            widget_list=self.s2_b_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            s2b_f,
            '4. MACD GC 확인 (B)',
            's2_b_macd_gap',
            '',
            toggle_key='use_s2_b_macd_gc',
            hint='MACD가 시그널 상향 돌파',
            widget_list=self.s2_b_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            s2b_f,
            '5. 거래량 급증 확인 (B)',
            's2_b_vol_ratio',
            '배',
            toggle_key='use_s2_b_vol_surge',
            hint='이전 평균 거래량 대비 배수',
            widget_list=self.s2_b_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            s2b_f,
            '  ㄴ 전일동시간 평균 기간 (S2·B)',
            'prev_vol_lookback_s2_b',
            '일',
            'S2 Type B 거래량 조건: Prev_Day_Vol_S2_B. 구 prev_vol_lookback_b 미설정 시 폴백',
            widget_list=self.s2_b_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            s2b_f,
            '6. 최소 수익률 확보 (B)',
            's2_b_profit_limit',
            '%',
            toggle_key='use_s2_b_profit',
            hint='진입가 대비 최소 수익률',
            widget_list=self.s2_b_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            s2b_f,
            '7. 추매 Smart Buy (Type B)',
            None,
            '',
            '켜면 [5] VP/BA로 Type B 추매만 필터',
            toggle_key='use_smart_buy_s2_b',
            widget_list=self.s2_b_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            s2b_f,
            '8. 진입 후 대기 시간 (B)',
            's2_b_delay_hours',
            '시간',
            '최소 보유 시간 허들',
            toggle_key='use_s2_b_delay',
            widget_list=self.s2_b_widgets,
            group_toggle_widgets=True,
        )

        rt_f = ttk.LabelFrame(_sec_parent('smart'), text=' 🔫 [5] 전술적 수급 (Smart Buy / Smart Exit) ', padding='15')
        rt_f.pack(fill=tk.X, pady=10)
        add_param_row(rt_f, 'Smart Buy 최소 체결강도 (VP)', 'smart_buy_vp', '%', 'S1 + S2 Type A/B Smart Buy 공통 임계')
        add_param_row(rt_f, 'Smart Buy 매도벽 비율 (BA)', 'smart_buy_ba_ratio', '배', 'S1 + S2 Type A/B Smart Buy 공통 임계')
        add_param_row(rt_f, 'Smart Exit 탈출용 체결강도 (VP)', 'smart_exit_vp', '%', '에너지 소멸 확인', toggle_key='use_smart_exit')
        add_param_row(rt_f, 'Smart Exit 탈출용 매수함정 비율', 'smart_exit_ba_ratio', '배', '허수매수세/지루함 포착', toggle_key='use_smart_exit')
        add_param_row(rt_f, 'Smart Exit 가격 하락 임계', 'smart_exit_price_threshold', '%', '고점 대비 하락폭 제한', toggle_key='use_smart_exit')

        mkt_f = ttk.LabelFrame(
            _sec_parent('mkt'),
            text=' 🌐 [6] 시장 트렌드 게이트 (MSI / ADX / VIX · S1·S2 공통) ',
            padding='15',
        )
        mkt_f.pack(fill=tk.X, pady=10)
        add_param_row(
            mkt_f,
            '★ 시장 게이트 전체',
            None,
            '',
            'ON: 아래 임계값으로 UP/NEUTRAL/DOWN 판정 후 S1·S2에 반영. OFF: 시장차단 없음(임계값은 저장만)',
            toggle_key='use_market_filter',
        )
        add_param_row(
            mkt_f,
            'MSI 하락 임계값 (DOWN 기준)',
            'msi_down_threshold',
            '',
            '이 값 미만이면 DOWN 후보 (동적 백테스트 SSOT, 기본 -0.10)',
            widget_list=self.mkt_gate_widgets,
        )
        add_param_row(
            mkt_f,
            'MSI 상승 임계값 (UP 기준)',
            'msi_up_threshold',
            '',
            'UP/NEUTRAL 경계 등에 사용 (기본 0.10)',
            widget_list=self.mkt_gate_widgets,
        )
        add_param_row(
            mkt_f,
            'ADX 최소 추세강도 (NEUTRAL 기준)',
            'adx_min_threshold',
            '',
            'ADX가 이 값 미만이면 NEUTRAL 후보 (기본 15)',
            widget_list=self.mkt_gate_widgets,
        )
        add_param_row(
            mkt_f,
            'VIX 공포지수 상한 (DOWN 기준)',
            'vix_down_threshold',
            '',
            '이 값 초과 시 DOWN 후보 (기본 30)',
            widget_list=self.mkt_gate_widgets,
        )

        exit_parent = _sec_parent('exit')
        exit_a_f = ttk.LabelFrame(exit_parent, text=' 🛡️ [7-A] Type A 청산 · 리스크 (눌림·A추매) ', padding='15')
        exit_a_f.pack(fill=tk.X, pady=10)
        add_param_row(
            exit_a_f,
            '갭하락 즉시 손절 (A)',
            'gap_down_limit_a',
            '%',
            'Type A·A추매 보유 시 적용',
            toggle_key='use_gap_down_exit_a',
        )
        add_param_row(
            exit_a_f,
            '개장 초기 매도 유예 (A)',
            'gap_grace_mins_a',
            '분',
            '9시대 휩소 방어',
            toggle_key='use_gap_grace_a',
        )
        add_param_row(
            exit_a_f,
            'ATR 분석 주기 (A)',
            'atr_period_a',
            '봉',
            'ATR_A 지표·A트랙 ATR SL/트레일',
            toggle_key='use_atr_exit_a',
        )
        add_header(exit_a_f, '── S1 눌림 (A) ──')
        add_param_row(exit_a_f, '익절', 's1_a_take_profit', '%', '눌림목 목표 수익률', toggle_key='use_s1_a_tp')
        add_param_row(exit_a_f, '손절', 's1_a_stop_loss', '%', '눌림목 최후 방어선', toggle_key='use_s1_a_stop_loss')
        add_param_row(exit_a_f, 'Flash Exit', 's1_a_flash_exit_pct', '%', 'A 전용 즉시 탈출', toggle_key='use_s1_a_flash_exit')
        add_param_row(
            exit_a_f,
            'ATR 손절',
            'atr_multiplier_sl_a',
            '배',
            'ON이면 ATR 기준 SL, OFF면 위 고정 % 손절(S1)',
            toggle_key='use_atr_stop_loss_a',
        )
        add_param_row(
            exit_a_f,
            'ATR 트레일링',
            'atr_multiplier_trail_a',
            '배',
            'ON이면 ATR 추격 익절, OFF면 S1 구간 ATR 트레일 없음',
            toggle_key='use_atr_trailing_stop_a',
        )
        add_param_row(
            exit_a_f,
            'ATR 트레일: 목표 익절% 이후만 (A-S1)',
            None,
            '',
            'ON이면 S1(A)에서 목표 익절% 도달 전까지 고정 TAKE_PROFIT 청산을 생략·트레일만 무장',
            toggle_key='use_atr_trail_only_after_tp_s1_a',
        )
        add_header(exit_a_f, '── S2 눌림 (A) ──')
        add_param_row(exit_a_f, '익절', 's2_take_profit', '%', 'A 추매 포지션', toggle_key='use_s2_tp')
        add_param_row(exit_a_f, '손절', 's2_stop_loss', '%', 'A 추매 포지션', toggle_key='use_s2_stop_loss')
        add_param_row(exit_a_f, 'Flash Exit', 's2_flash_exit_pct', '%', 'A 추매 즉시 탈출', toggle_key='use_s2_flash_exit')
        add_param_row(
            exit_a_f,
            'ATR 손절',
            'atr_multiplier_sl_s2',
            '배',
            'ATR_SL(평단−배수×ATR). TRAIL은 아래 추격 배수',
            toggle_key='use_atr_stop_loss_s2',
        )
        add_param_row(
            exit_a_f,
            'ATR 트레일링',
            'atr_multiplier_trail_s2',
            '배',
            'ATR_TRAIL 전용 (A 추매 구간)',
            toggle_key='use_atr_trailing_stop_s2',
        )
        add_param_row(
            exit_a_f,
            'ATR 트레일: 목표 익절% 이후만 (A-S2)',
            None,
            '',
            'ON이면 S2(A)에서 목표 익절% 이후에만 ATR 트레일 평가',
            toggle_key='use_atr_trail_only_after_tp_s2_a',
        )

        exit_b_f = ttk.LabelFrame(exit_parent, text=' 🛡️ [7-B] Type B 청산 · 리스크 (돌파·B추매) ', padding='15')
        exit_b_f.pack(fill=tk.X, pady=10)
        add_param_row(
            exit_b_f,
            '갭하락 즉시 손절 (B)',
            'gap_down_limit_b',
            '%',
            'Type B·B추매 보유 시 적용',
            toggle_key='use_gap_down_exit_b',
        )
        add_param_row(
            exit_b_f,
            '개장 초기 매도 유예 (B)',
            'gap_grace_mins_b',
            '분',
            '9시대 휩소 방어',
            toggle_key='use_gap_grace_b',
        )
        add_param_row(
            exit_b_f,
            'ATR 분석 주기 (B)',
            'atr_period_b',
            '봉',
            'ATR_B 지표·B트랙 ATR SL/트레일',
            toggle_key='use_atr_exit_b',
        )
        add_header(exit_b_f, '── S1 돌파 (B) ──')
        add_param_row(exit_b_f, '익절', 's1_b_take_profit', '%', '돌파형 목표 수익률', toggle_key='use_s1_b_tp')
        add_param_row(exit_b_f, '손절', 's1_b_stop_loss', '%', '돌파형 최후 방어선', toggle_key='use_s1_b_stop_loss')
        add_param_row(exit_b_f, 'Flash Exit', 's1_b_flash_exit_pct', '%', 'B 전용 즉시 탈출', toggle_key='use_s1_b_flash_exit')
        add_param_row(
            exit_b_f,
            'ATR 손절',
            'atr_multiplier_sl_b',
            '배',
            'ON이면 ATR 기준 SL, OFF면 고정 % 손절(S1)',
            toggle_key='use_atr_stop_loss_b',
        )
        add_param_row(
            exit_b_f,
            'ATR 트레일링',
            'atr_multiplier_trail_b',
            '배',
            'ON이면 ATR 추격 익절',
            toggle_key='use_atr_trailing_stop_b',
        )
        add_param_row(
            exit_b_f,
            'ATR 트레일: 목표 익절% 이후만 (B-S1)',
            None,
            '',
            'ON이면 S1(B)에서 목표 익절% 도달 전까지 고정 TAKE_PROFIT 청산을 생략·트레일만 무장',
            toggle_key='use_atr_trail_only_after_tp_s1_b',
        )
        add_header(exit_b_f, '── S2 돌파 (B) ──')
        add_param_row(exit_b_f, '익절', 's2_b_take_profit', '%', 'B 추매 (s2_b_*)', toggle_key='use_s2_b_tp')
        add_param_row(exit_b_f, '손절', 's2_b_stop_loss', '%', 'B 추매', toggle_key='use_s2_b_stop_loss')
        add_param_row(exit_b_f, 'Flash Exit', 's2_b_flash_exit_pct', '%', 'B 추매 즉시 탈출', toggle_key='use_s2_b_flash_exit')
        add_param_row(
            exit_b_f,
            'ATR 손절',
            'atr_multiplier_sl_s2_b',
            '배',
            'ATR_SL 전용 (B 추매). TP/Flash 우선이면 체감 변화가 적을 수 있음',
            toggle_key='use_atr_stop_loss_s2_b',
        )
        add_param_row(
            exit_b_f,
            'ATR 트레일링',
            'atr_multiplier_trail_s2_b',
            '배',
            'ATR_TRAIL 전용 (B 추매 구간)',
            toggle_key='use_atr_trailing_stop_s2_b',
        )
        add_param_row(
            exit_b_f,
            'ATR 트레일: 목표 익절% 이후만 (B-S2)',
            None,
            '',
            'ON이면 S2(B)에서 목표 익절% 이후에만 ATR 트레일 평가',
            toggle_key='use_atr_trail_only_after_tp_s2_b',
        )

        # 초기 표시: 첫 번째 사이드바 항목
        _show_settings_section('fund')

        self.app.root.after(100, self._init_toggles)
        
        def force_cleanup():
            self.parent.focus_force()
            for ent in self.params_entries.values():
                ent.selection_clear()
        self.parent.after(1000, force_cleanup)

    def _init_toggles(self):
        def apply_st(widgets, is_on):
            st = tk.NORMAL if is_on else tk.DISABLED
            pref = '' if is_on else 'Dimmed.'
            for w in widgets:
                try:
                    if isinstance(w, tk.Checkbutton):
                        w.config(state=st)
                        continue
                    w.config(state=st)
                    if isinstance(w, ttk.Label):
                        w.configure(style=f'{pref}TLabel')
                    elif hasattr(w, 'configure'):
                        w.configure(style=f'{pref}TEntry')
                except (tk.TclError, AttributeError):
                    pass
        if 'use_type_a' in self.conf_vars:
            self.conf_vars['use_type_a'].trace_add('write', lambda *a: apply_st(self.type_a_widgets, self.conf_vars['use_type_a'].get()))
            apply_st(self.type_a_widgets, self.conf_vars['use_type_a'].get())
        if 'use_type_b' in self.conf_vars:
            self.conf_vars['use_type_b'].trace_add('write', lambda *a: apply_st(self.type_b_widgets, self.conf_vars['use_type_b'].get()))
            apply_st(self.type_b_widgets, self.conf_vars['use_type_b'].get())
        if 'use_s2_master_a' in self.conf_vars:
            self.conf_vars['use_s2_master_a'].trace_add(
                'write', lambda *a: apply_st(self.s2_a_widgets, self.conf_vars['use_s2_master_a'].get())
            )
            apply_st(self.s2_a_widgets, self.conf_vars['use_s2_master_a'].get())
        if 'use_s2_master_b' in self.conf_vars:
            self.conf_vars['use_s2_master_b'].trace_add(
                'write', lambda *a: apply_st(self.s2_b_widgets, self.conf_vars['use_s2_master_b'].get())
            )
            apply_st(self.s2_b_widgets, self.conf_vars['use_s2_master_b'].get())
        if 'use_market_filter' in self.conf_vars:
            self.conf_vars['use_market_filter'].trace_add(
                'write', lambda *a: apply_st(self.mkt_gate_widgets, self.conf_vars['use_market_filter'].get())
            )
            apply_st(self.mkt_gate_widgets, self.conf_vars['use_market_filter'].get())

        # 유니버스 Track A/B → 해당 타입 S1·S2 마스터 동기화 (사용자가 한 곳에서 켜고 끄도록)
        def _sync_sel_track_a_to_type_a(*_args: object) -> None:
            try:
                if 'use_sel_track_a' not in self.conf_vars:
                    return
                v = bool(self.conf_vars['use_sel_track_a'].get())
                if 'use_type_a' in self.conf_vars:
                    self.conf_vars['use_type_a'].set(v)
                if 'use_s2_master_a' in self.conf_vars:
                    self.conf_vars['use_s2_master_a'].set(v)
            except (tk.TclError, AttributeError):
                pass

        def _sync_sel_track_b_to_type_b(*_args: object) -> None:
            try:
                if 'use_sel_track_b' not in self.conf_vars:
                    return
                v = bool(self.conf_vars['use_sel_track_b'].get())
                if 'use_type_b' in self.conf_vars:
                    self.conf_vars['use_type_b'].set(v)
                if 'use_s2_master_b' in self.conf_vars:
                    self.conf_vars['use_s2_master_b'].set(v)
            except (tk.TclError, AttributeError):
                pass

        if 'use_sel_track_a' in self.conf_vars:
            self.conf_vars['use_sel_track_a'].trace_add('write', _sync_sel_track_a_to_type_a)
        if 'use_sel_track_b' in self.conf_vars:
            self.conf_vars['use_sel_track_b'].trace_add('write', _sync_sel_track_b_to_type_b)

    def reset_to_defaults(self):
        if messagebox.askyesno('초기화 확인', '모든 파라미터를 초기값으로 복원하시겠습니까?'):
            self.app.settings = self.default_settings.copy()
            if hasattr(self.app, 'reset_ui_tabs'): self.app.reset_ui_tabs()
                                   
    def save_settings(self, sync_to_backtest=True):
        try:
            for k, ent in self.params_entries.items():
                if not ent: continue
                v = ent.get().strip().replace(',', '')
                if not v: continue
                if k in [
                    'backtest_initial_capital', 'max_slots', 'max_slots_a', 'max_slots_b',
                    'atr_period', 'atr_period_a', 'atr_period_b',
                    'gap_grace_mins', 'gap_grace_mins_a', 'gap_grace_mins_b',
                    'gc_window', 'rsi_window',
                ]:
                    self.app.settings[k] = int(float(v))
                else: 
                    self.app.settings[k] = float(v)
            for k, var in self.conf_vars.items():
                if hasattr(var, 'get'): self.app.settings[k] = var.get()

            for row in self.sizing_rows:
                ent = row['ent']
                unit = row['unit_var'].get()
                v = ent.get().strip().replace(',', '')
                if not v: continue
                active_key = row['amt_key'] if unit == '원' else row['pct_key']
                self.app.settings[active_key] = float(v)
            
            if self.app.config_handler.save_settings(self.app.settings):
                if hasattr(self.app, 'stream_processor'):
                    self.app.stream_processor.settings = self.app.settings
                if hasattr(self.app, 'backtest_tab'):
                    # [v16.48] 자동 동기화 로직 제거 (최적화 선택은 사용자가 직접 수동으로만 수행)
                    pass
                self.status_label.config(text="✅ 설정이 저장되었습니다.", foreground="#A3BE8C")
                self.app.log('✅ 설정이 저장되었습니다. (유효 파라미터 동기화 완료)')
            else:
                raise Exception("파일 저장 실패")
        except Exception as e: 
            self.status_label.config(text=f"❌ 저장 실패: {e}", foreground="#BF616A")
            messagebox.showerror('오류', f'저장 실패: {e}')

    def update_criteria(self):
        pass

    def get_criteria_text(self, format_type="ui"):
        return self.app.selection_service._get_criteria_summary(format_type)
