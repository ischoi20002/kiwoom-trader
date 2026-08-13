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
        self.sel_a_v2_widgets: list[tp.Any] = []
        self.sel_a_legacy_widgets: list[tp.Any] = []
        self.sel_b_v2_widgets: list[tp.Any] = []
        self.sel_b_legacy_widgets: list[tp.Any] = []
        self.grid_rows = {} 
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

    def _ensure_opt_var(self, key: str, label: str = "") -> bool:
        """설정 행 키를 최적화 ○/● 대상에 자동 등록. 성공 시 True."""
        try:
            if not key:
                return False
            if not hasattr(self.app, "opt_params_vars") or self.app.opt_params_vars is None:
                self.app.opt_params_vars = {}
            if key not in self.app.opt_params_vars:
                saved_val = bool(self.app.settings.get(f"opt_{key}", False))
                self.app.opt_params_vars[key] = tk.BooleanVar(value=saved_val)
            if not hasattr(self.app, "opt_param_labels") or self.app.opt_param_labels is None:
                self.app.opt_param_labels = {}
            lab = str(label or "").strip()
            if lab:
                # 들여쓰기 장식 제거한 표시명 저장
                self.app.opt_param_labels[key] = lab.lstrip(" ㄴ└─-").strip() or lab
            return True
        except Exception:
            return False

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
        label: str = "",
    ) -> None:
        """[v16.50] 최적화 ○/● 아이콘. 설정 UI에 노출된 모든 키를 대상으로 자동 등록."""
        if not self._ensure_opt_var(key, label=label):
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
                try:
                    if _any_peer_active():
                        widget.configure(style="Opt.TEntry")
                    else:
                        widget.configure(style="TEntry")
                except (tk.TclError, AttributeError, TypeError):
                    # Checkbutton 등 Entry 스타일이 없는 위젯은 아이콘만 갱신
                    pass
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

        # [UI] 상단 버튼: 오른쪽 정렬 + 나란히 배치
        top_btn_frame = ttk.Frame(header_sticky)
        top_btn_frame.pack(side=tk.RIGHT)

        # pack(side=RIGHT) 특성상 시각적 좌→우 순서를 맞추려면 역순으로 pack
        ttk.Button(
            top_btn_frame,
            text='💾 설정 저장 및 즉시 적용',
            command=self.save_settings,
            width=30,
            style='Action.TButton',
        ).pack(side=tk.RIGHT, padx=10)
        ttk.Button(
            top_btn_frame,
            text='🔄 공장 초기화',
            command=self.reset_to_defaults,
            width=20,
        ).pack(side=tk.RIGHT, padx=10)
        ttk.Button(
            top_btn_frame,
            text='🧹 최적화 전체 해제',
            command=self.clear_all_optimization,
            width=20,
        ).pack(side=tk.RIGHT, padx=10)

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
                '  ↓ ⑤ Smart Buy / Smart Exit\n'
                '  ↓ ⑥ 청산'
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
            ('exit', '⑥ 청산 · 리스크 (A/B)'),
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
                # 체크박스 + (최적화 대상이면) ○/● 를 같은 칸에 배치
                cell0 = ttk.Frame(parent)
                cell0.grid(row=r, column=0, sticky='w', padx=(5, 0))
                cb = tk.Checkbutton(
                    cell0,
                    variable=self.conf_vars[toggle_key],
                    bg=bg_col,
                    fg=txt_col,
                    activebackground=bg_col,
                    selectcolor='#5E81AC',
                )
                cb.pack(side=tk.LEFT)
                if widget_list is not None and group_toggle_widgets:
                    widget_list.append(cb)
                try:
                    self._bind_opt_interaction(
                        cb,
                        toggle_key,
                        pack_in=cell0,
                        bind_widget_rclick=True,
                        label=label,
                    )
                except Exception:
                    pass
            
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
                self._bind_opt_interaction(
                    ent, key, grid_row=r, grid_parent=parent, label=label
                )

            if unit:
                un = ttk.Label(parent, text=unit, font=('Malgun Gothic', 9), foreground=primary_col)
                un.grid(row=r, column=3, sticky='w')
                if widget_list is not None: widget_list.append(un)
            
            if hint:
                hn = ttk.Label(parent, text=f'  ({hint})', style='Dimmed.TLabel')
                hn.grid(row=r, column=5, sticky='w', padx=10)
                if widget_list is not None: widget_list.append(hn)
            if widget_list is not None: widget_list.append(lbl)

        # UI 빌드 (그리드 순서대로 배치)
        cap_f = ttk.LabelFrame(_sec_parent('fund'), text=' 💰 [1] 자금 및 계좌 관리 ', padding='15')
        cap_f.pack(fill=tk.X, pady=10)
        add_param_row(cap_f, '백테스팅용 초기자금', 'backtest_initial_capital', '원', '시뮬레이션 시작 및 잔고 부족 시 기준')
        add_header(cap_f, '포지션 사이징 (총자산 대비 %)')
        add_param_row(
            cap_f,
            '1단계(정찰) 비중',
            's1_equity_pct',
            '%',
            '총자산 대비 S1 진입 비중',
        )
        add_param_row(
            cap_f,
            '2단계(본대) 비중',
            's2_equity_pct',
            '%',
            '총자산 대비 S2 추매 비중',
        )
        # 레거시 키 호환: 단위는 항상 % 고정 (금액 지정 UI 제거)
        self.conf_vars['s1_sizing_unit'] = tk.StringVar(value='%')
        self.conf_vars['s2_sizing_unit'] = tk.StringVar(value='%')
        add_header(cap_f, '거래 수수료 요율 (체결금액 대비 %)')
        add_param_row(
            cap_f,
            '실전 — 매수',
            'buy_fee_live',
            '%',
            '키움 실전(USE_MOCK=False) 가상 체결·손익·일일 리포트',
        )
        add_param_row(cap_f, '실전 — 매도', 'sell_fee_live', '%', '')
        add_param_row(
            cap_f,
            '모의 — 매수',
            'buy_fee_mock',
            '%',
            '키움 모의(USE_MOCK=True) 가상 체결·손익·일일 리포트',
        )
        add_param_row(cap_f, '모의 — 매도', 'sell_fee_mock', '%', '')
        add_param_row(
            cap_f,
            '백테 — 매수',
            'buy_fee_bt',
            '%',
            'SimpleBacktester·DynamicBacktester·파라미터 최적화',
        )
        add_param_row(cap_f, '백테 — 매도', 'sell_fee_bt', '%', '')
        add_param_row(
            cap_f,
            'A/B 수 균형(신규 S1)',
            None,
            '',
            '보유 종목 수 비율이 목표를 넘는 타입의 신규 S1만 차단(동률이면 허용). '
            '매수금액 불변. A·B 둘 다 켤 때만 적용(BT·RT 공통)',
            toggle_key='use_type_balance_gate',
        )
        add_param_row(
            cap_f,
            '  └ Type A 목표 비중',
            'type_balance_a_pct',
            '%',
            'Type B 목표 = (100 − A)%. 예: 65이면 A 65%·B 35%에 가깝게 유지',
        )
        add_param_row(
            cap_f,
            '선정 시 거래정지 종목 제외',
            None,
            '',
            '네이버 실시간 tradeStopType=HALTED — 대시보드 종목 선정(실시간)에서만 적용',
            toggle_key='exclude_trading_halt_on_selection',
        )

        # ── 모바일 읽기 전용 현황 API ──
        mobile_f = ttk.LabelFrame(
            _sec_parent('fund'),
            text=' 📱 모바일 현황 API (읽기 전용 · 포트포워딩) ',
            padding='15',
        )
        mobile_f.pack(fill=tk.X, pady=10)
        add_param_row(
            mobile_f,
            '모바일 현황 API 사용',
            None,
            '',
            '공유기 포트포워딩으로 외부 조회 (주문 불가)',
            toggle_key='mobile_status_api_enabled',
        )
        add_param_row(
            mobile_f,
            'API 포트',
            'mobile_status_api_port',
            '',
            '기본 8765 — Windows 방화벽·공유기 내부 포트와 맞춤',
        )
        # 문자열 설정은 conf_vars로 수집 (params_entries float 변환 회피)
        for _mk, _mlabel, _mhint in (
            ('mobile_status_api_token', '접속 PIN', '숫자 8자리 — 아래 생성 버튼 (공백 입력도 가능)'),
            ('mobile_status_public_url', '공개 URL', '예: http://mydomain.ddns.net:28765'),
            ('mobile_status_tls_cert', 'TLS 인증서 경로', '선택 — PEM cert (없으면 HTTP)'),
            ('mobile_status_tls_key', 'TLS 키 경로', '선택 — PEM private key'),
            ('mobile_status_api_host', '바인드 주소', '기본 0.0.0.0 (포트포워딩용)'),
        ):
            r = get_next_row(mobile_f)
            ttk.Label(mobile_f, text=_mlabel, font=('Malgun Gothic', 10), foreground=txt_col).grid(
                row=r, column=1, sticky='w', pady=4
            )
            _init = str(self.app.settings.get(_mk, self.default_settings.get(_mk, '')) or '')
            self.conf_vars[_mk] = tk.StringVar(value=_init)
            _ent = ttk.Entry(mobile_f, textvariable=self.conf_vars[_mk], width=42, exportselection=0)
            _ent.grid(row=r, column=2, columnspan=3, sticky='we', padx=5)
            if _mhint:
                ttk.Label(mobile_f, text=f'  ({_mhint})', style='Dimmed.TLabel').grid(
                    row=r, column=5, sticky='w', padx=10
                )
        r_tok = get_next_row(mobile_f)
        ttk.Button(
            mobile_f,
            text='PIN 생성·채우기',
            width=18,
            command=self._generate_mobile_status_token,
        ).grid(row=r_tok, column=2, sticky='w', padx=5, pady=(4, 0))
        ttk.Label(
            mobile_f,
            text='  (저장 후 재기동 · 대시보드「설정 링크」로 폰 전달)',
            style='Dimmed.TLabel',
        ).grid(row=r_tok, column=5, sticky='w', padx=10)

        _univ_root = _sec_parent('univ')

        # ── 유니버스 인덱스 소스 선택 (KODEX200 vs KOSPI 시총 상위 N) ──
        # settings['universe_index_choice'] 내부값은 KODEX200 / KOSPI200 유지.
        # UI 표시만 'KOSPI' (저장 시 KOSPI → KOSPI200 매핑).
        univ_source_f = ttk.LabelFrame(
            _univ_root,
            text=' 🔎 유니버스 인덱스 소스 ',
            padding='15',
        )
        univ_source_f.pack(fill=tk.X, expand=False, pady=(0, 12))

        try:
            if 'universe_index_choice' not in self.conf_vars:
                init_val = self.app.settings.get(
                    'universe_index_choice',
                    self.default_settings.get('universe_index_choice', 'KODEX200'),
                )
                init_disp = str(init_val).strip().upper()
                if init_disp == 'KOSPI200':
                    init_disp = 'KOSPI'
                elif init_disp not in ('KODEX200', 'KOSPI'):
                    init_disp = 'KODEX200'
                self.conf_vars['universe_index_choice'] = tk.StringVar(value=init_disp)
        except Exception:
            # UI 로딩 실패 시에도 나머지 섹션은 유지되도록 방어
            self.conf_vars['universe_index_choice'] = tk.StringVar(value='KODEX200')

        ttk.Label(
            univ_source_f,
            text='선택',
            font=('Malgun Gothic', 10),
            foreground=txt_col,
        ).grid(row=0, column=0, sticky='w', padx=(5, 10))

        univ_cmb = ttk.Combobox(
            univ_source_f,
            textvariable=self.conf_vars['universe_index_choice'],
            values=['KODEX200', 'KOSPI'],
            state='readonly',
            width=10,
        )
        univ_cmb.grid(row=0, column=1, sticky='w', padx=(5, 10))

        # 저장 단계에서는 conf_vars의 .get() 값을 settings에 반영합니다.
        # (save_settings()에서 self.conf_vars 순회)
        ttk.Label(
            univ_source_f,
            text='(KODEX=고정 200 / KOSPI=시총 상위 N)',
            font=('Malgun Gothic', 9),
            foreground=primary_col,
        ).grid(row=0, column=2, sticky='w', padx=(10, 5))

        # KOSPI 선택 시 marcap 시총 상위 N 슬라이스에 사용합니다.
        try:
            if 'kospi_top_n' not in self.conf_vars:
                init_topn = self.app.settings.get(
                    'kospi_top_n',
                    self.default_settings.get('kospi_top_n', 200),
                )
                self.conf_vars['kospi_top_n'] = tk.StringVar(value=str(init_topn))
        except Exception:
            self.conf_vars['kospi_top_n'] = tk.StringVar(value='200')

        kospi_n_lbl = ttk.Label(
            univ_source_f,
            text='상위 N(150/200/250/300)',
            font=('Malgun Gothic', 9),
            foreground=primary_col,
        )
        kospi_n_lbl.grid(row=1, column=0, sticky='w', padx=(5, 10), pady=(4, 0))

        kospi_n_cmb = ttk.Combobox(
            univ_source_f,
            textvariable=self.conf_vars['kospi_top_n'],
            values=['150', '200', '250', '300'],
            state='readonly',
            width=10,
        )
        kospi_n_cmb.grid(row=1, column=1, sticky='w', padx=(5, 10), pady=(4, 0))

        # 유니버스 갱신 주기 (백테·실시간 공통, 기본 분기)
        _REFRESH_LABELS = [
            ('quarterly', '분기'),
            ('monthly', '월별'),
            ('bimonthly', '2개월'),
            ('fourmonth', '4개월'),
            ('semiannual', '반기'),
            ('biweekly', '격주'),
            ('weekly', '주별'),
            ('daily', '일별'),
        ]
        _REFRESH_DISP = [f'{lab} ({key})' for key, lab in _REFRESH_LABELS]
        _REFRESH_BY_DISP = {f'{lab} ({key})': key for key, lab in _REFRESH_LABELS}
        _REFRESH_BY_KEY = {key: f'{lab} ({key})' for key, lab in _REFRESH_LABELS}

        try:
            if 'kospi_universe_refresh' not in self.conf_vars:
                init_ref = str(
                    self.app.settings.get(
                        'kospi_universe_refresh',
                        self.default_settings.get('kospi_universe_refresh', 'quarterly'),
                    )
                ).strip().lower()
                self.conf_vars['kospi_universe_refresh'] = tk.StringVar(
                    value=_REFRESH_BY_KEY.get(init_ref, _REFRESH_BY_KEY['quarterly'])
                )
        except Exception:
            self.conf_vars['kospi_universe_refresh'] = tk.StringVar(
                value=_REFRESH_BY_KEY['quarterly']
            )

        kospi_ref_lbl = ttk.Label(
            univ_source_f,
            text='갱신 주기',
            font=('Malgun Gothic', 9),
            foreground=primary_col,
        )
        kospi_ref_lbl.grid(row=2, column=0, sticky='w', padx=(5, 10), pady=(4, 0))

        kospi_ref_cmb = ttk.Combobox(
            univ_source_f,
            textvariable=self.conf_vars['kospi_universe_refresh'],
            values=_REFRESH_DISP,
            state='readonly',
            width=18,
        )
        kospi_ref_cmb.grid(row=2, column=1, sticky='w', padx=(5, 10), pady=(4, 0))
        ttk.Label(
            univ_source_f,
            text='(백테·실시간 공통, 권장: 분기)',
            font=('Malgun Gothic', 9),
            foreground=primary_col,
        ).grid(row=2, column=2, sticky='w', padx=(10, 5), pady=(4, 0))

        def _sync_kospi_top_n_enabled(*_args: object) -> None:
            """KODEX200이면 상위 N·갱신주기 비활성·흐리게, KOSPI면 활성."""
            try:
                choice = str(self.conf_vars['universe_index_choice'].get()).strip().upper()
                is_kospi = choice in ('KOSPI', 'KOSPI200')
                kospi_n_cmb.configure(state='readonly' if is_kospi else 'disabled')
                kospi_n_lbl.configure(style='TLabel' if is_kospi else 'Dimmed.TLabel')
                kospi_ref_cmb.configure(state='readonly' if is_kospi else 'disabled')
                kospi_ref_lbl.configure(style='TLabel' if is_kospi else 'Dimmed.TLabel')
                if is_kospi:
                    kospi_n_lbl.configure(foreground=primary_col)
                    kospi_ref_lbl.configure(foreground=primary_col)
            except (tk.TclError, AttributeError, KeyError):
                pass

        try:
            self.conf_vars['universe_index_choice'].trace_add(
                'write', _sync_kospi_top_n_enabled
            )
            _sync_kospi_top_n_enabled()
        except Exception:
            pass

        # save 시 표시문구 → 내부 키로 변환하기 위해 매핑 보관
        try:
            self._kospi_refresh_disp_to_key = _REFRESH_BY_DISP
        except Exception:
            pass

        # ── 레짐 전환 게이트 (TRANS_S8_NS2: B-share 급락 + 변동성) ──
        transition_f = ttk.LabelFrame(
            _univ_root,
            text=' 🔄 레짐 전환 게이트 (Valid A/B · B-share) ',
            padding='15',
        )
        transition_f.pack(fill=tk.X, expand=False, pady=(0, 12))
        add_param_row(
            transition_f,
            '전환 게이트 사용',
            None,
            '',
            'ON: Valid_B 비중 급락 + 변동성 확인 시 TRANSITION/HARD. 실시간·동적 백테 공통',
            toggle_key='use_transition_gate',
        )
        add_header(transition_f, '── 전환(TRANSITION) 감지 ──')
        add_param_row(
            transition_f,
            '선행 B-share 하한(%)',
            'transition_prior_share_min',
            '%',
            '5일 전 B-share가 이 값 이상이었을 때만 전환 후보',
        )
        add_param_row(
            transition_f,
            'B-share 5일 변화 상한',
            'transition_share_delta5_max',
            '%p',
            '음수. 5일간 B-share 하락폭이 이 값 이하(더 큰 하락)이면 후보',
        )
        add_param_row(
            transition_f,
            'B-share 3일 변화 상한',
            'transition_share_delta3_max',
            '%p',
            '음수. 3일 경로 보조 조건',
        )
        add_param_row(
            transition_f,
            '지수 rvol20 하한',
            'transition_rvol_min',
            '',
            'KODEX200 연환산 20일 변동성(%). 이 값 이상이면 변동성 확인',
        )
        add_param_row(
            transition_f,
            '교차섹션 ret std 하한',
            'transition_xs_ret_std_min',
            '',
            '유니버스 일간 수익률 표준편차(%). 지수 rvol 결측 시 보조',
        )
        add_header(transition_f, '── TRANSITION 시 슬롯·차단 ──')
        add_param_row(
            transition_f,
            'SOFT 슬롯 A',
            'transition_soft_slots_a',
            '개',
            'TRANSITION일 Track A 감시 상한 (기본 8, TRANS_S8_NS2)',
        )
        add_param_row(
            transition_f,
            'SOFT 슬롯 B',
            'transition_soft_slots_b',
            '개',
            'TRANSITION일 Track B 감시 상한',
        )
        add_param_row(
            transition_f,
            'TRANSITION 시 S1 차단',
            None,
            '',
            'ON: TRANSITION에서 신규 진입(S1) 중단. OFF면 위 슬롯 축소만 적용',
            toggle_key='transition_block_s1_on_soft',
        )
        add_param_row(
            transition_f,
            'TRANSITION 시 S2 차단',
            None,
            '',
            'TRANS_S8_NS2: OFF(HARD만 S2 차단). ON이면 TRANSITION에서도 추매 중단',
            toggle_key='transition_block_s2_on_soft',
        )
        add_param_row(
            transition_f,
            'TRANSITION 무시',
            None,
            '',
            'ON: TRANSITION 판정을 OFF로 간주(슬롯·차단 미적용)',
            toggle_key='transition_ignore_soft',
        )
        add_header(transition_f, '── HARD(chop) 임계 ──')
        add_param_row(
            transition_f,
            'Valid_A 하한 (chop)',
            'chop_valid_a_min',
            '개',
            'A 과밀: Valid_A≥이 값 & Valid_B≤b_max → HARD',
        )
        add_param_row(
            transition_f,
            'Valid_B 상한 (chop)',
            'chop_valid_b_max',
            '개',
            'B 고갈 조건',
        )
        add_param_row(
            transition_f,
            'B-share 상한 (%)',
            'chop_valid_b_share_max_pct',
            '%',
            'B-share≤이 값 & Valid_A≥floor → HARD',
        )
        add_param_row(
            transition_f,
            'Valid_A floor (chop)',
            'chop_valid_a_floor',
            '개',
            'B-share 상한 조건용 Valid_A 최소',
        )
        add_param_row(
            transition_f,
            '강한 상승일 HARD 억제',
            None,
            '',
            'ON: KODEX200 일수익률≥임계면 chop→HARD 건너뜀 (폭등일 A과밀 오발동 완화)',
            toggle_key='hard_suppress_on_index_up',
        )
        add_param_row(
            transition_f,
            'HARD 억제 상승 임계 (%)',
            'hard_suppress_index_up_pct',
            '%',
            '지수(KODEX200) 일수익률이 이 값 이상이면 HARD 억제',
        )
        add_param_row(
            transition_f,
            '억제 시 OFF만',
            None,
            '',
            'ON: 상승일 HARD 억제 후 TRANSITION 폴백 없이 OFF (슬롯 축소 없음)',
            toggle_key='hard_suppress_to_off_only',
        )

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
        add_param_row(sel_f, '최소 거래대금', 'min_trde_val', '억', '5일 평균 거래대금 기준 (v2·레거시 공통)')
        add_header(sel_f, '── Track A v2: 단기 상대강도·고가대비 되돌림·이평 근접·ATR%·갭 ──')
        add_param_row(
            sel_f,
            'RS 룩백 (거래일)',
            'sel_a_rs_lookback',
            '일',
            '일자별 교차섹션에서 사용할 단기 수익률 기간 N',
            widget_list=self.sel_a_v2_widgets,
        )
        add_param_row(
            sel_f,
            'RS 분위 하한',
            'sel_a_rs_rank_min',
            '',
            '0~1. 하한 미만(너무 약함) 제외',
            widget_list=self.sel_a_v2_widgets,
        )
        add_param_row(
            sel_f,
            'RS 분위 상한',
            'sel_a_rs_rank_max',
            '',
            '0~1. 상한 초과(과열) 제외',
            widget_list=self.sel_a_v2_widgets,
        )
        add_param_row(
            sel_f,
            '고가대비 되돌림 룩백',
            'sel_a_pb_lookback',
            '일',
            '최근 N일 고가 대비 종가 되돌림 % 계산 구간',
            widget_list=self.sel_a_v2_widgets,
        )
        add_param_row(
            sel_f,
            '되돌림 하한 %',
            'sel_a_pb_min_pct',
            '%',
            '너무 얕은 조정(추격) 제외',
            widget_list=self.sel_a_v2_widgets,
        )
        add_param_row(
            sel_f,
            '되돌림 상한 %',
            'sel_a_pb_max_pct',
            '%',
            '과도한 낙폭 제외',
            widget_list=self.sel_a_v2_widgets,
        )
        add_param_row(
            sel_f,
            'MA20 근접 허용(비율)',
            'sel_a_ma20_near_pct',
            '',
            '종가와 MA20 괴리 허용(소수, 예: 0.045≈4.5%)',
            widget_list=self.sel_a_v2_widgets,
        )
        add_param_row(
            sel_f,
            '종가 ≥ MA5 필수',
            None,
            '',
            'OFF면 MA20 근접만 요구',
            toggle_key='sel_a_require_close_ge_ma5',
            widget_list=self.sel_a_v2_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            sel_f,
            'MA5 근접 허용(비율)',
            'sel_a_ma5_eps_pct',
            '',
            '위 토글 ON일 때 MA5 통과용 허용 오차(소수)',
            widget_list=self.sel_a_v2_widgets,
        )
        add_param_row(
            sel_f,
            'ATR 기간',
            'sel_a_atr_period',
            '봉',
            'Wilder ATR 기간(일봉)',
            widget_list=self.sel_a_v2_widgets,
        )
        add_param_row(
            sel_f,
            'ATR% 하한',
            'sel_a_atr_pct_min',
            '',
            'ATR/종가 비율 하한(소수, 예: 0.006=0.6%)',
            widget_list=self.sel_a_v2_widgets,
        )
        add_param_row(
            sel_f,
            'ATR% 상한',
            'sel_a_atr_pct_max',
            '',
            'ATR/종가 비율 상한(소수)',
            widget_list=self.sel_a_v2_widgets,
        )
        add_param_row(
            sel_f,
            '갭 절대값 상한',
            'sel_a_gap_abs_max_pct',
            '%',
            '전일 종가 대비 당일 시가 갭 % 절대값',
            widget_list=self.sel_a_v2_widgets,
        )
        add_param_row(
            sel_f,
            'OSC>0 필수 (v2)',
            None,
            '',
            '일봉 MACD 히스토그램 양수',
            toggle_key='sel_a_require_osc_positive',
            widget_list=self.sel_a_v2_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            sel_f,
            'MA60 존재 필수',
            None,
            '',
            '60일 이평 계산 가능한 길이',
            toggle_key='sel_a_require_ma60',
            widget_list=self.sel_a_v2_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            sel_f,
            '거래량 분출 필수 (A)',
            None,
            '',
            '레거시 vol_ratio·vol_lookback로 VolBurstActive_A 요구',
            toggle_key='sel_a_require_vol_burst',
            widget_list=self.sel_a_v2_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            sel_f,
            '선정 RSI 밴드 필수',
            None,
            '',
            'ON이면 sel_rsi_min~max도 v2 단계에서 적용',
            toggle_key='sel_a_require_sel_rsi_band',
            widget_list=self.sel_a_v2_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            sel_f,
            '최소 일봉 이력',
            'sel_a_min_history_bars',
            '봉',
            '보조지표 계산에 필요한 최소 바 수',
            widget_list=self.sel_a_v2_widgets,
        )
        add_param_row(
            sel_f,
            '일봉 MACD 파라미터',
            None,
            '',
            'Fast/Slow/Signal은 macd_fast_daily 등 전역 키와 동일 (선정·신호 공용)',
            widget_list=self.sel_a_v2_widgets,
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
        add_param_row(
            sel_b_f,
            '최소 거래대금 (B)',
            'sel_b_min_trde_val',
            '억',
            'v2·레거시 공통: 5일 평균 거래대금 하한',
        )
        add_param_row(
            sel_b_f,
            '종목 선정 RSI 하한 (B)',
            'sel_b_rsi_min',
            '',
            'v2·레거시 공통 (v2에서 sel_b_require_sel_rsi_band ON일 때만 강제)',
        )
        add_param_row(sel_b_f, '종목 선정 RSI 상한 (B)', 'sel_b_rsi_max', '', '과열 방지 상한')
        add_param_row(sel_b_f, '거래량 분출 비율 (B)', 'sel_b_vol_ratio', '배', 'VolBurstActive_B 산출용 (v2·레거시 공통)')
        add_param_row(sel_b_f, '분출 확인 기간 (B)', 'sel_b_vol_lookback', '일', '분출 이력 룩백')
        add_header(sel_b_f, '── Track B v2: 단기 RS·고가대비 종가·MA20 기울기·ATR%·갭 ──')
        add_param_row(
            sel_b_f,
            'v2 일봉 선정 스택 사용',
            None,
            '',
            '레거시 모드 미사용(항상 v2 기준)',
        )
        add_param_row(
            sel_b_f,
            'RS 룩백 (거래일)',
            'sel_b_rs_lookback',
            '일',
            '일자별 교차섹션 단기 수익률 N',
            widget_list=self.sel_b_v2_widgets,
        )
        add_param_row(
            sel_b_f,
            'RS 분위 하한',
            'sel_b_rs_rank_min',
            '',
            '0~1',
            widget_list=self.sel_b_v2_widgets,
        )
        add_param_row(
            sel_b_f,
            'RS 분위 상한',
            'sel_b_rs_rank_max',
            '',
            '0~1',
            widget_list=self.sel_b_v2_widgets,
        )
        add_param_row(
            sel_b_f,
            '고가 대비 종가 룩백',
            'sel_b_hi_lookback',
            '일',
            '최근 N일 고가 대비 종가 위치(%)',
            widget_list=self.sel_b_v2_widgets,
        )
        add_param_row(
            sel_b_f,
            '고가대비 종가 하한 %',
            'sel_b_hi_min_pct',
            '%',
            '너무 약한 종가 위치 제외',
            widget_list=self.sel_b_v2_widgets,
        )
        add_param_row(
            sel_b_f,
            '고가대비 종가 상한 %',
            'sel_b_hi_max_pct',
            '%',
            '과도하게 고가에 붙은 경우 제외',
            widget_list=self.sel_b_v2_widgets,
        )
        add_param_row(
            sel_b_f,
            'MA20 기울기 룩백',
            'sel_b_ma20_slope_lookback',
            '일',
            'MA20 변화율 계산 간격',
            widget_list=self.sel_b_v2_widgets,
        )
        add_param_row(
            sel_b_f,
            'MA20 기울기 최소(비율)',
            'sel_b_ma20_slope_min',
            '',
            '예: 0.001 = 0.1% 상승 기울기',
            widget_list=self.sel_b_v2_widgets,
        )
        add_param_row(
            sel_b_f,
            '종가 ≥ MA5 필수',
            None,
            '',
            'ON이면 정배열에 MA5 하한(아래 ε·추가 이격) 적용. v2 다른 조건과 겹치면 토글만으로 스케줄이 안 바뀔 수 있음',
            toggle_key='sel_b_require_close_ge_ma5',
            widget_list=self.sel_b_v2_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            sel_b_f,
            'MA5 근접 허용(비율)',
            'sel_b_ma5_eps_pct',
            '',
            '토글 ON일 때 종가/MA5 하한 (1−ε). 소수(0.005=0.5% 아래까지 허용)',
            widget_list=self.sel_b_v2_widgets,
        )
        add_param_row(
            sel_b_f,
            'MA5 추가 이격(비율)',
            'sel_b_ma5_min_excess_ratio',
            '',
            '0이면 기존과 동일. 0.01이면 (1−ε)+0.01 배율까지 5일선 위 추가 요구(체감 필터)',
            widget_list=self.sel_b_v2_widgets,
        )
        add_param_row(
            sel_b_f,
            'ATR 기간',
            'sel_b_atr_period',
            '봉',
            '일봉 ATR 기간',
            widget_list=self.sel_b_v2_widgets,
        )
        add_param_row(
            sel_b_f,
            'ATR% 하한',
            'sel_b_atr_pct_min',
            '',
            'ATR/종가 비율 하한(소수)',
            widget_list=self.sel_b_v2_widgets,
        )
        add_param_row(
            sel_b_f,
            'ATR% 상한',
            'sel_b_atr_pct_max',
            '',
            'ATR/종가 비율 상한(소수)',
            widget_list=self.sel_b_v2_widgets,
        )
        add_param_row(
            sel_b_f,
            '갭 절대값 상한',
            'sel_b_gap_abs_max_pct',
            '%',
            '전일 종가 대비 당일 시가 갭',
            widget_list=self.sel_b_v2_widgets,
        )
        add_param_row(
            sel_b_f,
            'OSC>0 필수 (B v2)',
            None,
            '',
            '일봉 MACD 히스토그램 양수',
            toggle_key='sel_b_require_osc_positive',
            widget_list=self.sel_b_v2_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            sel_b_f,
            'OSC 전봉 대비 상승 필수',
            None,
            '',
            '우상향 추가 조건',
            toggle_key='sel_b_require_osc_rising',
            widget_list=self.sel_b_v2_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            sel_b_f,
            'MA60 존재 필수 (B)',
            None,
            '',
            '60일 이평 유효',
            toggle_key='sel_b_require_ma60',
            widget_list=self.sel_b_v2_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            sel_b_f,
            '거래량 분출 필수 (B v2)',
            None,
            '',
            'VolBurstActive_B 요구',
            toggle_key='sel_b_require_vol_burst',
            widget_list=self.sel_b_v2_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            sel_b_f,
            '선정 RSI 밴드 필수 (B)',
            None,
            '',
            'ON이면 위 RSI 하한·상한을 v2 단계에서 적용',
            toggle_key='sel_b_require_sel_rsi_band',
            widget_list=self.sel_b_v2_widgets,
            group_toggle_widgets=True,
        )
        add_param_row(
            sel_b_f,
            '최소 일봉 이력 (B)',
            'sel_b_min_history_bars',
            '봉',
            '실시간 보조지표 계산 최소 길이',
            widget_list=self.sel_b_v2_widgets,
        )
        add_param_row(
            sel_b_f,
            '일봉 MACD 파라미터 (B)',
            None,
            '',
            'Fast/Slow/Signal은 macd_fast_daily 등 전역 키와 동일',
            widget_list=self.sel_b_v2_widgets,
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
        add_param_row(
            strat_f,
            '동일 종목 S1 재매수 쿨다운',
            's1_buy_signal_cooldown_sec',
            '초',
            '직전 S1 매수 성공 시각 기준 최소 간격(틱 중복·연속 주문 방지). 기본 1800=30분',
        )
        add_param_row(
            strat_f,
            'S1: 로컬 15분 확정 시 평가',
            None,
            '',
            'ON=완성 15분에서 S1·S2·청산',
            toggle_key='realtime_s1_eval_on_local_15m_close',
        )
        add_param_row(
            strat_f,
            '15:30 신호 익일 시초 이월',
            None,
            '',
            'ON=15:30 완성봉 S1·S2·EXIT 신호를 즉시 주문하지 않고, 익일 09:00 동시호가 지정가로 이월(BT·RT 공통)',
            toggle_key='next_open_1530_enabled',
        )
        add_param_row(
            strat_f,
            '틱 말분 합성 평가',
            None,
            '',
            'ON(권장)=매분 틱 1분 seal, 말분에 15분 합성 후 S1·S2·청산. '
            '다음분 :00 틱 즉시, 무거래는 타임아웃(초)',
            toggle_key='realtime_15m_last_minute_from_ticks',
        )
        add_param_row(
            strat_f,
            'API실패 시 틱 결측채움',
            None,
            '',
            'ON(권장)=완성15 합성 시 공식 1분이 없는 분을 틱 seal로 채움. '
            'S1·S2·청산 모두 동일(ka10080 empty 등에서도 평가 진행)',
            toggle_key='realtime_15m_compose_tick_fill_on_api_miss',
        )
        add_param_row(
            strat_f,
            'head 공식 soft교체',
            None,
            '',
            'ON(권장)=슬롯 앞구간 닫힌 뒤 ka10080 1회로 공식 1분을 덮어씀(정합). '
            '실패해도 틱봉·말분 평가 유지',
            toggle_key='realtime_m1_head_official_catchup_enabled',
        )
        add_param_row(
            strat_f,
            '틱말분 타임아웃',
            'realtime_15m_tick_tail_wait_sec',
            '초',
            '다음분 :00 틱이 없을 때(무거래) 말분·매분 seal 대기. 기본 1.5',
        )
        add_param_row(
            strat_f,
            '공식 말분 1분 평가',
            None,
            '',
            'OFF(권장)=말분은 틱. ON=틱 OFF일 때 말분 마감 후 ka10080 1회 더 조회해 평가'
            '(슬롯당 head+말분 최대 2회)',
            toggle_key='realtime_15m_eval_on_official_last_m1',
        )
        add_param_row(
            strat_f,
            'M1 ON시에도 공식15 교체',
            None,
            '',
            'OFF(기본)=M1 경로에서 공식 15분 REST 스킵. '
            'ON=슬롯 완성 후 ~2분 지연·단발 조회로 정합 교체(M1 평가/프로브와 경쟁하지 않음)',
            toggle_key='realtime_closed_15m_official_replace_with_m1',
        )
        add_param_row(
            strat_f,
            'ka10080 전역 간격',
            'realtime_ka10080_global_min_interval_sec',
            '초',
            '키움 ka10080 유량=1(초당 1회). 권장 1.05. 0.5 이하면 429(허용요청초과) 위험',
        )
        add_param_row(
            strat_f,
            '라이브가 주문 게이트(S1·S2·EXIT)',
            None,
            '',
            'ON=완성봉으로 S1·S2·청산 판정 후, 주문 직전 라이브가로 추격/이탈·MA20 정합 확인. '
            '실패 시 주문 스킵(S1은 쿨다운 미소모)',
            toggle_key='realtime_s1_live_confirm_gate_enabled',
        )
        add_param_row(
            strat_f,
            '라이브 추격 상한',
            'realtime_s1_live_max_chase_pct',
            '%',
            '봉종가 대비 라이브가 상승 허용 한도(기본 0.8). 초과 시 주문 스킵',
        )
        add_param_row(
            strat_f,
            '라이브 이탈 하한',
            'realtime_s1_live_max_fade_pct',
            '%',
            '봉종가 대비 라이브가 하락 허용 한도(기본 1.0). 초과 시 주문 스킵',
        )
        add_param_row(
            strat_f,
            '라이브 MA20 정합',
            None,
            '',
            'ON=봉종가와 라이브가가 MA20 같은 쪽에 있을 때만 주문(위↔아래 교차 시 스킵)',
            toggle_key='realtime_s1_live_ma20_side_confirm',
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
            '  ㄴ 이평 수렴 상한',
            'ma_convergence',
            '%',
            'GC 판정: (MA5/MA20-1)×100 ≤ 이 값 (기본 1.2)',
            widget_list=self.type_a_widgets,
        )
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
        add_header(strat_f, '── Type B: 돌파형 진입 조건 ──')
        add_param_row(strat_f, 'B 전용 사용', None, '', '순풍(UP) 장세 전용', toggle_key='use_type_b')
        # 동시신호(A+B) 확정: ①초안 → ②동수 시 B 덮기 → ③트랙 바로확정(ON이면 ①② 무시)
        add_header(strat_f, '── 동시신호(A+B) 타입 확정 ──')
        r_sb = get_next_row(strat_f)
        ttk.Label(
            strat_f,
            text='① 초안 선택',
            font=('Malgun Gothic', 10),
            foreground=txt_col,
        ).grid(row=r_sb, column=1, sticky='w', pady=4)
        try:
            if 'same_bar_sig_priority' not in self.conf_vars:
                _d = str(self.default_settings.get('same_bar_sig_priority', 'prefer_a'))
                _cur = str(self.app.settings.get('same_bar_sig_priority', _d)).strip().lower()
                if _cur == 'confluence':
                    _cur = 'prefer_a'
                self.conf_vars['same_bar_sig_priority'] = tk.StringVar(value=_cur)
            _sb_vals = ('prefer_a', 'prefer_b', 'prefer_track')
            if self.conf_vars['same_bar_sig_priority'].get() not in _sb_vals:
                self.conf_vars['same_bar_sig_priority'].set('prefer_a')
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
                text=(
                    'A·B 동시 봉의 임시 우승자. '
                    'prefer_a=A · prefer_b=B · prefer_track=선정트랙(A→A, B→B). '
                    '이후 ② 동수면 B 덮기 적용'
                ),
                style='Dimmed.TLabel',
            ).grid(row=r_sb, column=5, sticky='w', padx=10)

            r_tie = get_next_row(strat_f)
            ttk.Label(
                strat_f,
                text='② 동수 처리',
                font=('Malgun Gothic', 10),
                foreground=txt_col,
            ).grid(row=r_tie, column=1, sticky='w', pady=4)
            if 's1_simul_balance_tie_mode' not in self.conf_vars:
                _dt = str(
                    self.default_settings.get('s1_simul_balance_tie_mode', 'always')
                )
                self.conf_vars['s1_simul_balance_tie_mode'] = tk.StringVar(
                    value=str(
                        self.app.settings.get('s1_simul_balance_tie_mode', _dt)
                    ).strip().lower()
                )
            _tie_vals = ('always', 'force_a', 'block', 'empty_only', 'when_held', 'off')
            if self.conf_vars['s1_simul_balance_tie_mode'].get() not in _tie_vals:
                self.conf_vars['s1_simul_balance_tie_mode'].set('always')
            ttk.Combobox(
                strat_f,
                textvariable=self.conf_vars['s1_simul_balance_tie_mode'],
                values=_tie_vals,
                width=14,
                state='readonly',
            ).grid(row=r_tie, column=2, sticky='w', padx=5)
            ttk.Label(
                strat_f,
                text=(
                    '보유 A수=B수일 때. always=B로 덮기 · force_a=A로 덮기(B차단) · '
                    'block=매수 전면차단 · empty_only=0:0만 B · '
                    'when_held=보유있을때만 B · off=안 바꿈'
                ),
                style='Dimmed.TLabel',
            ).grid(row=r_tie, column=5, sticky='w', padx=10)

            add_param_row(
                strat_f,
                '③ 트랙으로 바로 확정',
                None,
                '',
                'ON이면 ①②를 건너뛰고 선정 트랙(A→TYPE_A, B→TYPE_B)으로 확정. '
                'OFF(권장)=①→② 순서 유지',
                toggle_key='same_bar_sig_prefer_sel_track',
            )
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
        add_param_row(rt_f, 'Smart Buy 매도벽 비율 (BA)', 'smart_buy_ba_ratio', '배', '매도잔량/매수잔량 — S1·S2 Smart Buy 공통 임계')
        add_param_row(rt_f, 'Smart Exit 탈출용 체결강도 (VP)', 'smart_exit_vp', '%', '에너지 소멸 확인', toggle_key='use_smart_exit')
        add_param_row(rt_f, 'Smart Exit 탈출용 매도벽 비율', 'smart_exit_ba_ratio', '배', '매도잔량/매수잔량(Smart Buy BA와 동일). 임계 이상이면 탈출 후보', toggle_key='use_smart_exit')
        add_param_row(rt_f, 'Smart Exit 가격 하락 임계', 'smart_exit_price_threshold', '%', '고점 대비 하락 폭(%) — 양수 권장. 음수 입력 시에도 절댓값으로 동일 적용', toggle_key='use_smart_exit')

        exit_parent = _sec_parent('exit')
        exit_a_f = ttk.LabelFrame(exit_parent, text=' 🛡️ [6-A] Type A 청산 · 리스크 (눌림·A추매) ', padding='15')
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

        exit_b_f = ttk.LabelFrame(exit_parent, text=' 🛡️ [6-B] Type B 청산 · 리스크 (돌파·B추매) ', padding='15')
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

        # A/B 공통: ATR_SL 이후 동일종목 재진입 쿨다운
        exit_cd_f = ttk.LabelFrame(
            exit_parent,
            text=' 🛡️ ATR_SL 재진입 쿨다운 (A/B 공통) ',
            padding='15',
        )
        exit_cd_f.pack(fill=tk.X, pady=10)
        add_header(exit_cd_f, '── 진입 직후 ATR_SL 유예 (Simple/동적 공통) ──')
        add_param_row(
            exit_cd_f,
            '유예 시간',
            'atr_sl_entry_grace_hours',
            '시간',
            '0=비활성. 진입 후 N시간 동안 SL 배수 확대(당일유예 OFF일 때)',
        )
        add_param_row(
            exit_cd_f,
            '유예 SL 배수',
            'atr_sl_entry_grace_mult',
            '배',
            '0=비활성. 유예 중 ATR_SL 배수(기본 SL 배수보다 커야 의미 있음)',
        )
        add_param_row(
            exit_cd_f,
            '당일 유예',
            None,
            '',
            'ON이면 진입 당일 종일 grace_mult 적용(시간 유예보다 우선)',
            toggle_key='atr_sl_entry_grace_same_day',
        )
        add_header(exit_cd_f, '── ATR_SL 후 재진입 쿨다운 (동적·실시간) ──')
        add_param_row(
            exit_cd_f,
            'ATR_SL 후 재진입 금지',
            'atr_sl_reentry_cooldown_days',
            '일',
            '0=비활성. ATR_SL 청산 후 동일 종목 S1 신규매수를 N일(달력일) 동안 차단. 권장 3',
        )
        add_param_row(
            exit_cd_f,
            '연속 SL 조회 기간',
            'atr_sl_streak_lookback_days',
            '일',
            '이 기간 안 ATR_SL 횟수를 세어 연속 손절 판정(기본 10)',
        )
        add_param_row(
            exit_cd_f,
            '연속 SL 시 재진입 금지',
            'atr_sl_streak_cooldown_days',
            '일',
            '0=비활성. 조회기간 내 ATR_SL 2회 이상이면 기본 N일 대신 이 일수로 연장(권장 5~7)',
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

        # [v] 레거시 미사용 정책: sel_a_use_new_stack / sel_b_use_new_stack 토글은 제거되며,
        # v2 위젯만 활성화합니다.

    def reset_to_defaults(self):
        """공장 초기화: DEFAULT_SETTINGS 를 메모리·settings.json 에 모두 반영한 뒤 UI 를 재구성한다."""
        if not messagebox.askyesno(
            '초기화 확인', '모든 파라미터를 초기값으로 복원하시겠습니까?'
        ):
            return
        try:
            from copy import deepcopy
            from core.defaults import DEFAULT_SETTINGS

            # 모듈 디폴트를 깊은 복사로 복원 (중첩 dict 공유·오염 방지)
            restored = deepcopy(DEFAULT_SETTINGS)
            # 포지션 사이징은 %만 지원
            restored['s1_sizing_unit'] = '%'
            restored['s2_sizing_unit'] = '%'
            self.app.settings = restored
            if not self.app.config_handler.save_settings(self.app.settings):
                messagebox.showerror('오류', '초기값 파일 저장에 실패했습니다.')
                return
            if hasattr(self.app, 'reset_ui_tabs'):
                self.app.reset_ui_tabs()
            try:
                self.app.log('🔄 공장 초기화 완료 — DEFAULT_SETTINGS 를 settings.json 에 저장했습니다.')
            except Exception:
                pass
            try:
                self.status_label.config(
                    text='✅ 공장 초기화 완료 (디폴트 저장됨)',
                    foreground='#A3BE8C',
                )
            except Exception:
                pass
        except Exception as e:
            try:
                messagebox.showerror('오류', f'공장 초기화 실패: {e}')
            except Exception:
                pass
                                   
    def _generate_mobile_status_token(self) -> None:
        """모바일 현황 API용 랜덤 토큰을 생성해 입력란에 채웁니다."""
        try:
            from core.status_api import generate_status_api_token

            tok = generate_status_api_token()
            var = self.conf_vars.get("mobile_status_api_token")
            if var is not None and hasattr(var, "set"):
                var.set(tok)
            else:
                self.app.settings["mobile_status_api_token"] = tok
            try:
                pretty = f"{tok[:4]} {tok[4:]}" if len(tok) == 8 else tok
                messagebox.showinfo(
                    "PIN 생성",
                    f"새 PIN: {pretty}\n\n「설정 저장 및 즉시 적용」을 눌러 주세요.\n"
                    "대시보드「설정 링크」로 폰에내면 입력 없이 접속됩니다.",
                    parent=self.parent,
                )
            except Exception:
                pass
        except Exception as e:
            try:
                messagebox.showerror("오류", f"토큰 생성 실패: {e}", parent=self.parent)
            except Exception:
                pass

    def collect_from_ui(self) -> None:
        """설정 탭 위젯 값을 ``app.settings`` 메모리에만 반영한다 (파일 저장 없음)."""
        for k, ent in self.params_entries.items():
            if not ent:
                continue
            v = ent.get().strip().replace(',', '')
            if not v:
                continue
            if k in [
                'backtest_initial_capital', 'max_slots', 'max_slots_a', 'max_slots_b',
                'max_slots_band_extra_cap',
                'transition_soft_slots_a', 'transition_soft_slots_b',
                'chop_valid_a_min', 'chop_valid_b_max', 'chop_valid_a_floor',
                'atr_sl_reentry_cooldown_days',
                'atr_sl_streak_lookback_days',
                'atr_sl_streak_cooldown_days',
                'atr_period', 'atr_period_a', 'atr_period_b',
                'gap_grace_mins', 'gap_grace_mins_a', 'gap_grace_mins_b',
                'gc_window', 'rsi_window',
                'mobile_status_api_port',
            ]:
                self.app.settings[k] = int(float(v))
            else:
                self.app.settings[k] = float(v)
        for k, var in self.conf_vars.items():
            if hasattr(var, 'get'):
                val = var.get()
                # UI 표시 'KOSPI' → 내부 설정키 'KOSPI200' 유지
                if k == 'universe_index_choice':
                    try:
                        raw = str(val).strip().upper()
                        if raw in ('KOSPI', 'KOSPI200'):
                            val = 'KOSPI200'
                        else:
                            val = 'KODEX200'
                    except Exception:
                        val = 'KODEX200'
                if k == 'kospi_universe_refresh':
                    try:
                        raw = str(val).strip()
                        mapped = getattr(self, '_kospi_refresh_disp_to_key', {}).get(raw)
                        if mapped:
                            val = mapped
                        else:
                            # 이미 내부 키이거나 괄호 안 키 추출
                            import re

                            m = re.search(r'\(([a-z]+)\)\s*$', raw.lower())
                            val = m.group(1) if m else raw.lower()
                        from core.marcap_universe import normalize_refresh_mode

                        val = normalize_refresh_mode(val)
                    except Exception:
                        val = 'quarterly'
                self.app.settings[k] = val

        # 포지션 사이징은 %만 지원 (금액 단위 제거)
        self.app.settings['s1_sizing_unit'] = '%'
        self.app.settings['s2_sizing_unit'] = '%'

    def save_settings(self, sync_to_backtest=True):
        try:
            self.collect_from_ui()

            if self.app.config_handler.save_settings(self.app.settings):
                if hasattr(self.app, 'stream_processor'):
                    self.app.stream_processor.settings = self.app.settings
                if hasattr(self.app, 'backtest_tab'):
                    # [v16.48] 자동 동기화 로직 제거 (최적화 선택은 사용자가 직접 수동으로만 수행)
                    pass
                try:
                    if hasattr(self.app, 'restart_status_api_from_settings'):
                        self.app.restart_status_api_from_settings()
                except Exception:
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
