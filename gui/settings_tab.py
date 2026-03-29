import tkinter as tk
from tkinter import ttk, messagebox
import json
import os
import pandas as pd
import typing as tp

class SettingsTab:
    def __init__(self, parent, app, default_settings):
        self.parent = parent
        self.app = app
        self.default_settings = default_settings
        self.params_entries = {}
        self.conf_vars = {}
        self.type_a_widgets = []
        self.type_b_widgets = []
        self.grid_rows = {} 
        self.window_id: tp.Any = None 
        self._setup_ui()

    def _setup_ui(self):
        # --- Theme Styling ---
        bg_col = self.app.colors.get('bg', '#2E3440')
        txt_col = self.app.colors.get('text', '#D8DEE9')
        primary_col = self.app.colors.get('primary', '#88C0D0')
        active_col = '#5E81AC' 

        style = ttk.Style()
        style.configure('Dimmed.TLabel', foreground='#666666')
        style.configure('Dimmed.TEntry', foreground='#666666')
        style.map('Dimmed.TEntry', fieldbackground=[('disabled', '#3B4252')])

        canvas = tk.Canvas(self.parent, background=bg_col, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.parent, orient='vertical', command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas, padding='30')

        scrollable_frame.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind_all('<MouseWheel>', lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), 'units'))
        self.window_id = canvas.create_window((0, 0), window=scrollable_frame, anchor='nw')
        
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(self.window_id, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)

        # 하단 버튼
        btn_frame = ttk.Frame(self.parent, padding='20')
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X)
        ttk.Button(btn_frame, text='💾 설정 저장 및 즉시 적용', command=self.save_settings, width=30, style='Action.TButton').pack(side=tk.RIGHT, padx=10)
        ttk.Button(btn_frame, text='🔄 공장 초기화', command=self.reset_to_defaults, width=20).pack(side=tk.RIGHT, padx=10)

        # 헤더
        header_sticky = ttk.Frame(self.parent, padding=(25, 15, 25, 0))
        header_sticky.pack(side=tk.TOP, fill=tk.X)
        ttk.Label(header_sticky, text='⚙️ 전략 파라미터 최적화', font=('Malgun Gothic', 18, 'bold'), foreground=primary_col).pack(side=tk.LEFT)
        self.app.settings_status_label = ttk.Label(header_sticky, text='', font=('Malgun Gothic', 10), foreground=self.app.colors.get('secondary', '#81A1C1'))
        self.app.settings_status_label.pack(side=tk.RIGHT)

        scrollbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)

        st_main_frame = scrollable_frame

        # --- Helper Methods for TRUE 5-Column Alignment ---
        def setup_section_columns(frame):
            frame.columnconfigure(0, weight=0, minsize=40)
            frame.columnconfigure(1, weight=0, minsize=220)
            frame.columnconfigure(2, weight=0, minsize=100)
            frame.columnconfigure(3, weight=0, minsize=60)
            frame.columnconfigure(4, weight=1)

        def get_next_row(parent):
            if parent not in self.grid_rows:
                self.grid_rows[parent] = 0
                setup_section_columns(parent)
            curr = self.grid_rows[parent]
            self.grid_rows[parent] += 1
            return curr

        def add_header(parent, text):
            r = get_next_row(parent)
            ttk.Label(parent, text=text, font=('Malgun Gothic', 10, 'bold'), foreground=primary_col).grid(row=r, column=0, columnspan=5, sticky='w', pady=(15, 5))

        def add_param_row(parent, label, key=None, unit='', hint='', toggle_key=None, widget_list=None):
            r = get_next_row(parent)
            if toggle_key:
                if toggle_key not in self.conf_vars:
                    def_val = self.default_settings.get(toggle_key, True)
                    self.conf_vars[toggle_key] = tk.BooleanVar(value=self.app.settings.get(toggle_key, def_val))
                cb = tk.Checkbutton(parent, variable=self.conf_vars[toggle_key], bg=bg_col, fg=txt_col, activebackground=bg_col, selectcolor=active_col) 
                cb.grid(row=r, column=0, sticky='w', padx=(5, 0))
            lbl = ttk.Label(parent, text=label, font=('Malgun Gothic', 10))
            lbl.grid(row=r, column=1, sticky='w', pady=4)
            if key:
                val = self.app.settings.get(key, self.default_settings.get(key, ''))
                ent = ttk.Entry(parent, width=12, justify='center')
                ent.insert(0, str(val))
                ent.grid(row=r, column=2, sticky='w', padx=5)
                self.params_entries[key] = ent
                if widget_list is not None: widget_list.append(ent)
            if unit:
                un = ttk.Label(parent, text=unit, font=('Malgun Gothic', 9))
                un.grid(row=r, column=3, sticky='w')
                if widget_list is not None: widget_list.append(un)
            if hint:
                hn = ttk.Label(parent, text=f'  ({hint})', style='Dimmed.TLabel')
                hn.grid(row=r, column=4, sticky='w', padx=10)
                if widget_list is not None: widget_list.append(hn)
            if widget_list is not None: widget_list.append(lbl)

        def add_sizing_row(parent, label, amt_key, pct_key, unit_key):
            r = get_next_row(parent)
            ttk.Label(parent, text=label, font=('Malgun Gothic', 10)).grid(row=r, column=1, sticky='w', pady=4)
            frame = ttk.Frame(parent)
            frame.grid(row=r, column=2, columnspan=2, sticky='w')
            ent = ttk.Entry(frame, width=10, justify='center')
            self.params_entries[amt_key] = ent
            self.params_entries[pct_key] = ent 
            ent.pack(side=tk.LEFT, padx=(5, 2))
            if unit_key not in self.conf_vars:
                self.conf_vars[unit_key] = tk.StringVar(value=self.app.settings.get(unit_key, '원'))
            cb = ttk.Combobox(frame, textvariable=self.conf_vars[unit_key], values=['원', '%'], width=3, state='readonly')
            cb.pack(side=tk.LEFT)
            def on_unit_change(*args):
                u = self.conf_vars[unit_key].get()
                val = self.app.settings.get(amt_key if u == '원' else pct_key, '')
                ent.delete(0, tk.END)
                ent.insert(0, str(val))
            self.conf_vars[unit_key].trace_add('write', on_unit_change)
            on_unit_change()

        cap_f = ttk.LabelFrame(st_main_frame, text=' 💰 [1] 자금 및 계좌 관리 (Account & Fund) ', padding='15')
        cap_f.pack(fill=tk.X, pady=10)
        add_param_row(cap_f, '백테스팅용 초기자금', 'backtest_initial_capital', '원', '시뮬레이션 시작 및 잔고 부족 시 기준')
        add_param_row(cap_f, '최대 감시 종목 수', 'max_slots', '개', '유니버스 선정 개수 제한')
        add_header(cap_f, '포지션 사이징 (배분 방식)')
        add_sizing_row(cap_f, '1단계(정찰) 비중', 'stage1_amt', 's1_equity_pct', 's1_sizing_unit')
        add_sizing_row(cap_f, '2단계(본대) 비중', 'stage2_amt', 's2_equity_pct', 's2_sizing_unit')
        add_param_row(cap_f, '가상 매매 자동화', None, '', '로직 포착 시 자동 진입 시뮬레이션', toggle_key='use_auto_virtual')

        sel_f = ttk.LabelFrame(st_main_frame, text=' 🔍 [2] 종목 선정 필터 (Stock Selection - Daily) ', padding='15')
        sel_f.pack(fill=tk.X, pady=10)
        add_param_row(sel_f, '최소 거래대금', 'min_trde_val', '억', '5일 평균 거래대금 기준')
        add_param_row(sel_f, '거래량 분출 비율', 'vol_ratio', '배', '전일 대비 분출 시점 포착')
        add_param_row(sel_f, '분출 확인 기간', 'vol_lookback', '일', '최근 N일 내 분출 이력 확인')
        add_param_row(sel_f, '이평선 수렴도 기준', 'ma_convergence', '%', '에너지 응축(Short/Mid) 확인')
        add_param_row(sel_f, '종목 선정 RSI 상한', 'sel_rsi_max', '', '유니버스 선정 탄성 확인 (기본값 70)')
        add_param_row(sel_f, '종목 선정 RSI 하한', 'sel_rsi_min', '', '과매도권/반등 유니버스 확인 (기본값 30)')
        add_param_row(sel_f, '일봉 MACD(OSC) 필터', None, '', '일봉 OSC 양수 상시 확인')
        add_param_row(sel_f, '  ㄴ Fast Period', 'macd_fast_daily', '', '기본값 12')
        add_param_row(sel_f, '  ㄴ Slow Period', 'macd_slow_daily', '', '기본값 26')
        add_param_row(sel_f, '  ㄴ Signal Period', 'macd_sig_daily', '', '기본값 9')

        entry_f = ttk.LabelFrame(st_main_frame, text=' 🎯 [3] 진입 시그널 필터 (Signal Logic - 15m) ', padding='15')
        entry_f.pack(fill=tk.X, pady=10)
        add_param_row(entry_f, '시장 지수 필터 (3단계-매수공통)', None, '', 'UP/NEUTRAL/DOWN 차등 가중치', toggle_key='use_market_filter')
        add_param_row(entry_f, '매수 진입 RSI 하한', 'buy_rsi_min', '', '과매도권/반등 확인용 최저선', toggle_key='use_rsi_min')
        add_param_row(entry_f, '15분봉 MACD 필터', None, '', '실시간 수급 모멘텀 확인', toggle_key='use_macd_signal')
        add_param_row(entry_f, '  ㄴ Fast Period', 'macd_fast_entry', '', '기본값 12')
        add_param_row(entry_f, '  ㄴ Slow Period', 'macd_slow_entry', '', '기본값 26')
        add_param_row(entry_f, '  ㄴ Signal Period', 'macd_sig_entry', '', '기본값 9')
        add_param_row(entry_f, 'RSI 시그널 필터', 'rsi_sig_period', '일', 'S1/S2 공통 지표 주기', toggle_key='use_rsi_sig_filter')

        strat_f = ttk.LabelFrame(st_main_frame, text=' 🚀 [4] 단계별 매매 상세 전략 (Mastery Strategies) ', padding='15')
        strat_f.pack(fill=tk.X, pady=10)
        add_header(strat_f, '1단계 (S1) [Type A: 눌림목 가이드]')
        add_param_row(strat_f, 'A 전용 사용', None, '', '중립/순풍 장세 사용 가능', toggle_key='use_type_a')
        add_param_row(strat_f, 'A RSI 상한선', 'buy_rsi_max_a', '', '눌림 깊이 확인용 (기본 65)', widget_list=self.type_a_widgets)
        add_param_row(strat_f, 'A Smart Buy', None, '', '진입 시 수급 필터 적용 (VP/BA)', toggle_key='use_smart_buy_a', widget_list=self.type_a_widgets)
        add_param_row(strat_f, 'A 이평 골든크로스', 'gc_window', '일', 'N일 내 GC 발생 여부', toggle_key='use_gc', widget_list=self.type_a_widgets)
        add_param_row(strat_f, 'A RSI 상향돌파', None, '', 'RSI 50 돌파 모멘텀', toggle_key='use_rsi_signal', widget_list=self.type_a_widgets)
        add_param_row(strat_f, 'A MA60 안전구역', None, '', '60일선 대비 이격 제한', toggle_key='use_ma60_safe', widget_list=self.type_a_widgets)
        add_param_row(strat_f, '  ㄴ MA60 하한', 'ma60_min', '%', '기본 100% 이상', widget_list=self.type_a_widgets)
        add_param_row(strat_f, '  ㄴ MA60 상한', 'ma60_max', '%', '기본 110% 이하', widget_list=self.type_a_widgets)
        add_param_row(strat_f, 'A 거래량 급증확인', 'vol_surge', '배', '전일 평균 대비 폭발력', toggle_key='use_vol_surge', widget_list=self.type_a_widgets)
        add_param_row(strat_f, 'A ATR 손절', 'atr_multiplier_sl_a', '배', '철벽 수비형 ATR 손절', toggle_key='use_atr_stop_loss_a', widget_list=self.type_a_widgets)
        add_param_row(strat_f, 'A 추격 익절', 'atr_multiplier_trail_a', '배', '수익 극대화용 트레일링', toggle_key='use_atr_trailing_stop_a', widget_list=self.type_a_widgets)
        
        add_header(strat_f, '1단계 (S1) [Type B: 돌파형 가이드]')
        add_param_row(strat_f, 'B 전용 사용', None, '', '순풍(UP) 장세 전용', toggle_key='use_type_b')
        add_param_row(strat_f, 'B RSI 상한선', 'buy_rsi_max_b', '', '돌파 강점 확인용 (기본 70)', widget_list=self.type_b_widgets)
        add_param_row(strat_f, 'B Smart Buy', None, '', '돌파 시 수급 필터 적용 (VP/BA)', toggle_key='use_smart_buy_b', widget_list=self.type_b_widgets)
        add_param_row(strat_f, 'B 돌파 거래량', 'vol_surge_b', '배', '돌파 시점 폭발적 수급', toggle_key='use_type_b_vol', widget_list=self.type_b_widgets)
        add_param_row(strat_f, 'B 추세 정렬 확인', None, '', '일봉/1분봉 추세 일치 여부', toggle_key='use_trend_aligned_b', widget_list=self.type_b_widgets)
        add_param_row(strat_f, 'B MACD GC 실전', None, '', '추가 모멘텀(GC) 확인', toggle_key='use_macd_gc_b', widget_list=self.type_b_widgets)
        add_param_row(strat_f, 'B ATR 손절', 'atr_multiplier_sl_b', '배', '공격적 수비형 ATR 손절', toggle_key='use_atr_stop_loss_b', widget_list=self.type_b_widgets)
        add_param_row(strat_f, 'B 추격 익절', 'atr_multiplier_trail_b', '배', '고가추격형 트레일링', toggle_key='use_atr_trailing_stop_b', widget_list=self.type_b_widgets)

        s2_f = ttk.LabelFrame(st_main_frame, text=' 📈 [5] 2단계 추매 전략 (Scaling Up) ', padding='15')
        s2_f.pack(fill=tk.X, pady=10)
        add_header(s2_f, '추매 진입 조건 (순풍 전용)')
        add_param_row(s2_f, '1. 20일선 지지 확인', 's2_ma20_gap', '%', toggle_key='use_s2_ma20')
        add_param_row(s2_f, '2. 5일선 회복/지지', 's2_ma5_gap', '%', toggle_key='use_s2_ma5')
        add_param_row(s2_f, '3. RSI 시그널 확인', 's2_rsi_gap', '%', toggle_key='use_s2_rsi_sig', hint='RSI가 시그널선을 상향 돌파')
        add_param_row(s2_f, '4. MACD GC 확인', 's2_macd_gap', '', toggle_key='use_s2_macd_gc', hint='MACD가 시그널 상향 돌파')
        add_param_row(s2_f, '5. 거래량 급증 확인', 's2_vol_ratio', '배', toggle_key='use_s2_vol_surge', hint='이전 평균 거래량 대비 배수')
        add_param_row(s2_f, '6. 최소 수익률 확보', 's2_profit_limit', '%', toggle_key='use_s2_profit', hint='진입가 대비 최소 수익률')
        add_param_row(s2_f, '7. 추매용 Smart Buy', None, '', '추매용 수급 임계치 적용', toggle_key='use_smart_buy_s2')
        add_param_row(s2_f, '8. 진입 후 대기 시간', 's2_delay_hours', '시간', '최소 보유 시간 허들', toggle_key='use_s2_delay')
        add_header(s2_f, 'S2 전용 리스크 제어')
        add_param_row(s2_f, 'S2 ATR 손절', 'atr_multiplier_sl_s2', '배', '평단 보호 최선책', toggle_key='use_atr_stop_loss_s2')
        add_param_row(s2_f, 'S2 추격 익절', 'atr_multiplier_trail_s2', '배', '피라미딩 익절 보호', toggle_key='use_atr_trailing_stop_s2')

        rt_f = ttk.LabelFrame(st_main_frame, text=' 🔫 [6] 전술적 수급 임계값 (Smart Wall System) ', padding='15')
        rt_f.pack(fill=tk.X, pady=10)
        add_header(rt_f, '🚀 Smart Buy (진입 수급) 임계치')
        add_param_row(rt_f, 'Smart Buy 최소 체결강도 (VP)', 'smart_buy_vp', '%', '상승 에너지 확인')
        add_param_row(rt_f, 'Smart Buy 진입용 매도벽 비율', 'smart_buy_ba_ratio', '배', '뚫고 올라갈 물량 유무')
        add_header(rt_f, '🛡️ Smart Exit (조기 탈출) 임계치')
        add_param_row(rt_f, 'Smart Exit 탈출용 체결강도 (VP)', 'smart_exit_vp', '%', '에너지 소멸 확인', toggle_key='use_smart_exit')
        add_param_row(rt_f, 'Smart Exit 탈출용 매수함정 비율', 'smart_exit_ba_ratio', '배', '허수매수세/지루함 포착', toggle_key='use_smart_exit')
        add_param_row(rt_f, 'Smart Exit 가격 하락 임계', 'smart_exit_price_threshold', '%', '고점 대비 하락폭 제한', toggle_key='use_smart_exit')

        exit_f = ttk.LabelFrame(st_main_frame, text=' 🛡️ [7] 공통 리스크 관리 (Global Exit & Safety) ', padding='15')
        exit_f.pack(fill=tk.X, pady=10)
        add_param_row(exit_f, '1. 갭하락 즉시 손절', 'gap_down_limit', '%', '시가 붕괴 대응', toggle_key='use_gap_down_exit')
        add_param_row(exit_f, '2. 개장 초기 매도 유예', None, '', '휩소 방지(9:15까지)', toggle_key='use_gap_grace')
        add_param_row(exit_f, '3. ATR 분석 주기', 'atr_period', '봉', '공통 ATR 지표 주기', toggle_key='use_atr_exit')
        add_header(exit_f, '고정 익절/손절 (Hard TP/SL)')
        add_param_row(exit_f, 'S1 눌림(A) 전용 익절', 's1_a_take_profit', '%', '눌림목 목표 수익률', toggle_key='use_s1_a_tp')
        add_param_row(exit_f, 'S1 눌림(A) 전용 손절', 's1_a_stop_loss', '%', '눌림목 최후 방어선', toggle_key='use_s1_a_stop_loss')
        add_param_row(exit_f, 'S1 돌파(B) 전용 익절', 's1_b_take_profit', '%', '돌파형 목표 수익률', toggle_key='use_s1_b_tp')
        add_param_row(exit_f, 'S1 돌파(B) 전용 손절', 's1_b_stop_loss', '%', '돌파형 최후 방어선', toggle_key='use_s1_b_stop_loss')
        add_param_row(exit_f, 'Stage 2 전용 익절', 's2_take_profit', '%', '추매 포지션 목표', toggle_key='use_s2_tp')
        add_param_row(exit_f, 'Stage 2 전용 손절', 's2_stop_loss', '%', '추매 포지션 가이드', toggle_key='use_s2_stop_loss')
        add_header(exit_f, '⚡ Flash Exit (급격한 추세 반전 대응)')
        add_param_row(exit_f, 'S1 눌림(A) Flash Exit', 's1_a_flash_exit_pct', '%', 'A 전용 즉시 탈출 기준', toggle_key='use_s1_a_flash_exit')
        add_param_row(exit_f, 'S1 돌파(B) Flash Exit', 's1_b_flash_exit_pct', '%', 'B 전용 즉시 탈출 기준', toggle_key='use_s1_b_flash_exit')
        add_param_row(exit_f, 'Stage 2 Flash Exit', 's2_flash_exit_pct', '%', '추매 포지션 즉시 탈출', toggle_key='use_s2_flash_exit')

        self.app.root.after(100, self._init_toggles)

    def _init_toggles(self):
        def apply_st(widgets, is_on):
            st = tk.NORMAL if is_on else tk.DISABLED
            pref = '' if is_on else 'Dimmed.'
            for w in widgets:
                try: 
                    if isinstance(w, tk.Checkbutton): continue
                    w.config(state=st)
                    if isinstance(w, ttk.Label): w.configure(style=f'{pref}TLabel')
                    elif hasattr(w, 'configure'): w.configure(style=f'{pref}TEntry')
                except: pass
        if 'use_type_a' in self.conf_vars:
            self.conf_vars['use_type_a'].trace_add('write', lambda *a: apply_st(self.type_a_widgets, self.conf_vars['use_type_a'].get()))
            apply_st(self.type_a_widgets, self.conf_vars['use_type_a'].get())
        if 'use_type_b' in self.conf_vars:
            self.conf_vars['use_type_b'].trace_add('write', lambda *a: apply_st(self.type_b_widgets, self.conf_vars['use_type_b'].get()))
            apply_st(self.type_b_widgets, self.conf_vars['use_type_b'].get())


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
                if k in ['backtest_initial_capital', 'max_slots', 'atr_period', 'macd_fast_daily', 'macd_slow_daily', 'macd_sig_daily', 'macd_fast_entry', 'macd_slow_entry', 'macd_sig_entry']:
                    self.app.settings[k] = int(float(v))
                else: 
                    self.app.settings[k] = float(v)
            for k, var in self.conf_vars.items():
                if hasattr(var, 'get'): self.app.settings[k] = var.get()
            if hasattr(self.app, '_final_save_settings'): self.app._final_save_settings()
            if hasattr(self.app, 'backtest_tab'): self.app.backtest_tab._sync_opt_toggles_with_settings()
            self.app.log('✅ 설정이 저장되었습니다.')
        except Exception as e: 
            messagebox.showerror('오류', f'저장 실패: {e}')
