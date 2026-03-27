import tkinter as tk
from tkinter import ttk, messagebox
import json
import os
import pandas as pd

# Assume DEFAULT_SETTINGS is accessible or passed. 
# For now, I'll import it from a common place if possible, 
# or just expect the app to provide it.
# Looking at Get_Price_GUI.py, it's defined globally.

class SettingsTab:
    def __init__(self, parent, app, default_settings):
        self.parent = parent
        self.app = app
        self.default_settings = default_settings
        self.params_entries = {}
        self.conf_vars = {}
        self.type_a_widgets = []
        self.type_b_widgets = []
        self._setup_ui()

    def _setup_ui(self):
        # [v4.1] Canvas 기반 스크롤 시스템 구축
        # [v5.2] 노르딕 테마 대응: 배경색을 애플리케이션 공통 배경색으로 변경
        canvas = tk.Canvas(self.parent, background=self.app.colors.get('bg', "#2E3440"), highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.parent, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas, padding="30")

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        # 마우스 휠 지원
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
            
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        
        # [v22.6] 고사양 GUI: 캔버스 너비에 따라 내부 프레임이 자동 확장되도록 구성
        # window_id를 저장하여 나중에 너비 조정에 사용
        self.window_id = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        
        def _on_canvas_configure(event):
            # 캔버스의 너비만큼 내부 프레임 너비 강제 조정 (Full-Width 정렬 해결)
            canvas.itemconfig(self.window_id, width=event.width)
            
        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.configure(yscrollcommand=scrollbar.set)

        # [v22.8] 고정 하단 버튼 영역 (스크롤 무관)
        btn_frame = ttk.Frame(self.parent, padding="20")
        btn_frame.pack(side=tk.BOTTOM, fill=tk.X)
        
        ttk.Button(btn_frame, text="💾 설정 저장 및 즉시 적용", command=self.save_settings, width=30, style="Action.TButton").pack(side=tk.RIGHT, padx=10)
        ttk.Button(btn_frame, text="🔄 공장 초기화", command=self.reset_to_defaults, width=20).pack(side=tk.RIGHT, padx=10)

        # [v22.9] 고정 상단 헤더 영역 (스크롤 무관)
        header_sticky = ttk.Frame(self.parent, padding=(25, 15, 25, 0))
        header_sticky.pack(side=tk.TOP, fill=tk.X)
        
        ttk.Label(header_sticky, text="⚙️ 전략 파라미터 최적화", font=("Malgun Gothic", 18, "bold"), foreground=self.app.colors['primary']).pack(side=tk.LEFT)
        self.app.settings_status_label = ttk.Label(header_sticky, text="", font=("Malgun Gothic", 10), foreground=self.app.colors['secondary'])
        self.app.settings_status_label.pack(side=tk.RIGHT)

        # 캔버스 및 스크롤바 배치 (위쪽 공간 전체 활용)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        st_main_frame = scrollable_frame

        # --- Helper Methods for Standardized Layout ---
        def add_standard_row(parent, label, key, unit="", hint="", width=30):
            """표준화된 파라미터 행 추가 (칼각 정렬)"""
            row = ttk.Frame(parent, padding="4")
            row.pack(fill=tk.X)
            
            ttk.Label(row, text=label, width=width, font=("Malgun Gothic", 10)).pack(side=tk.LEFT)
            
            ent = ttk.Entry(row, width=12, justify='center')
            val = self.app.settings.get(key, self.default_settings.get(key, ""))
            # 숫자 뒤 천단위 콤마 처리 (원화인 경우)
            if isinstance(val, int) and val > 10000:
                ent.insert(0, f"{val:,}")
            else:
                ent.insert(0, str(val))
            ent.pack(side=tk.LEFT, padx=5)
            
            ttk.Label(row, text=unit if unit else "", width=5).pack(side=tk.LEFT)
            ttk.Label(row, text=hint, font=("Malgun Gothic", 9), foreground=self.app.colors['secondary']).pack(side=tk.LEFT, padx=10)
            
            self.params_entries[key] = ent
            return row

        def add_toggle_param_row(parent, toggle_key, title, param_key=None, unit="", hint="", sub_param_key=None, sub_unit="", sub_label="", widget_list=None):
            """체크박스와 파라미터가 결합된 표준 행"""
            row = ttk.Frame(parent, padding="4")
            row.pack(fill=tk.X)
            
            self.conf_vars[toggle_key] = tk.BooleanVar(value=self.app.settings.get(toggle_key, True))
            cb = ttk.Checkbutton(row, text=title, variable=self.conf_vars[toggle_key], width=28)
            cb.pack(side=tk.LEFT)
            
            sub_widgets = []
            if param_key:
                ent = ttk.Entry(row, width=8, justify='center')
                ent.insert(0, str(self.app.settings.get(param_key, self.default_settings.get(param_key, ""))))
                ent.pack(side=tk.LEFT, padx=5)
                un = ttk.Label(row, text=unit, width=3)
                un.pack(side=tk.LEFT)
                self.params_entries[param_key] = ent
                sub_widgets.extend([ent, un])
                
                if sub_param_key:
                    ttk.Label(row, text=f" {sub_label} ").pack(side=tk.LEFT)
                    sub_ent = ttk.Entry(row, width=8, justify='center')
                    sub_ent.insert(0, str(self.app.settings.get(sub_param_key, self.default_settings.get(sub_param_key, ""))))
                    sub_ent.pack(side=tk.LEFT, padx=5)
                    sub_un = ttk.Label(row, text=sub_unit, width=3)
                    sub_un.pack(side=tk.LEFT)
                    self.params_entries[sub_param_key] = sub_ent
                    sub_widgets.extend([sub_ent, sub_un])

            if hint:
                h_lbl = ttk.Label(row, text=hint, font=("Malgun Gothic", 9), foreground=self.app.colors['secondary'])
                h_lbl.pack(side=tk.LEFT, padx=10)
                sub_widgets.append(h_lbl)
                
            if widget_list is not None:
                widget_list.extend(sub_widgets)
            return row

        def add_multi_standard_row(parent, title, param_key, unit="", hint="", sub_param_key=None, sub_unit="", sub_label=""):
            """체크박스 없는 표준 다중 파라미터 행"""
            row = ttk.Frame(parent, padding="4")
            row.pack(fill=tk.X)
            
            ttk.Label(row, text=title, width=30, font=("Malgun Gothic", 10)).pack(side=tk.LEFT)
            
            ent = ttk.Entry(row, width=8, justify='center')
            ent.insert(0, str(self.app.settings.get(param_key, self.default_settings.get(param_key, ""))))
            ent.pack(side=tk.LEFT, padx=5)
            ttk.Label(row, text=unit, width=3).pack(side=tk.LEFT)
            self.params_entries[param_key] = ent
            
            if sub_param_key:
                ttk.Label(row, text=f" {sub_label} ").pack(side=tk.LEFT)
                sub_ent = ttk.Entry(row, width=8, justify='center')
                sub_ent.insert(0, str(self.app.settings.get(sub_param_key, self.default_settings.get(sub_param_key, ""))))
                sub_ent.pack(side=tk.LEFT, padx=5)
                ttk.Label(row, text=sub_unit, width=3).pack(side=tk.LEFT)
                self.params_entries[sub_param_key] = sub_ent

            if hint:
                ttk.Label(row, text=hint, font=("Malgun Gothic", 9), foreground=self.app.colors['secondary']).pack(side=tk.LEFT, padx=10)
            return row

        def add_section_sizing_row(parent, label, amt_key, pct_key, unit_key):
            """통합 자금 관리 행 (원/%)"""
            row = ttk.Frame(parent, padding="4")
            row.pack(fill=tk.X)
            ttk.Label(row, text=label, width=30, font=("Malgun Gothic", 10)).pack(side=tk.LEFT)
            
            ent_var = tk.StringVar()
            unit_var = tk.StringVar(value=self.app.settings.get(unit_key, "원"))
            
            # 초기값 로드
            if unit_var.get() == "원":
                val = self.app.settings.get(amt_key, 1000000)
                ent_var.set(f"{val:,}" if isinstance(val, int) else str(val))
            else:
                ent_var.set(str(self.app.settings.get(pct_key, 10.0)))
            
            ent = ttk.Entry(row, width=12, textvariable=ent_var, justify='center')
            ent.pack(side=tk.LEFT, padx=5)
            unit_cb = ttk.Combobox(row, values=["원", "%"], textvariable=unit_var, width=5, state="readonly")
            unit_cb.pack(side=tk.LEFT)
            
            def on_unit_change(*args):
                new_unit = unit_var.get()
                if new_unit == "원":
                    v = self.app.settings.get(amt_key, 1000000)
                    ent_var.set(f"{v:,}" if isinstance(v, int) else str(v))
                else:
                    ent_var.set(str(self.app.settings.get(pct_key, 10.0)))
            unit_var.trace_add("write", on_unit_change)
            
            self.params_entries[amt_key] = ent
            self.params_entries[pct_key] = ent
            self.conf_vars[unit_key] = unit_var

        # --- [1] 자금 및 계좌 관리 (Account & Fund Management) ---
        cap_f = ttk.LabelFrame(st_main_frame, text=" 💰 [1] 자금 및 계좌 관리 (Account & Fund) ", padding="15")
        cap_f.pack(fill=tk.X, pady=10)
        
        add_standard_row(cap_f, "1. 총 투자 원금 (시작)", "total_capital", "원", "시뮬레이션 기준 자본")
        add_standard_row(cap_f, "2. 최대 감시 종목 수", "max_slots", "개", "순위 내 상위 N개 종목만 감시")
        add_section_sizing_row(cap_f, "3. 1단계(정찰) 비중", "stage1_amt", "s1_equity_pct", "s1_sizing_unit")
        add_section_sizing_row(cap_f, "4. 2단계(본대) 비중", "stage2_amt", "s2_equity_pct", "s2_sizing_unit")
        row_opts = ttk.Frame(cap_f, padding="4")
        row_opts.pack(fill=tk.X)
        self.conf_vars["use_auto_virtual"] = tk.BooleanVar(value=self.app.settings.get("use_auto_virtual", True))
        ttk.Checkbutton(row_opts, text="가상 매매 자동화 (타점 포착 시 자동 진입)", 
                        variable=self.conf_vars["use_auto_virtual"]).pack(side=tk.LEFT)

        # --- [2] 종목 선정 필터 (Stock Selection) ---
        sel_f = ttk.LabelFrame(st_main_frame, text=" 🔍 [2] 종목 선정 필터 (Stock Selection) ", padding="15")
        sel_f.pack(fill=tk.X, pady=10)
        
        add_standard_row(sel_f, "1. 최소 거래대금", "min_trde_val", "억", "⬇️ 유동성 필터링")
        add_standard_row(sel_f, "2. 거래량 분출 비율", "vol_ratio", "배", "⬆️ 강한 수급 확인 (전일대비)")
        add_standard_row(sel_f, "3. 분출 확인 기간", "vol_lookback", "일", "⬆️ 과거 수급 지속성")
        add_standard_row(sel_f, "4. 이평선 수렴도 기준", "ma_convergence", "%", "⬇️ MA5/20 밀집도")
        add_standard_row(sel_f, "5. 선정 RSI 하한", "sel_rsi_min", "", "⬆️ 과매도 반등 하한")
        add_standard_row(sel_f, "6. 선정 RSI 상한", "sel_rsi_max", "", "⬇️ 과매수 필터링")
        
        # MACD Filter in Selection
        macd_sel_row = ttk.Frame(sel_f, padding="4")
        macd_sel_row.pack(fill=tk.X)
        ttk.Label(macd_sel_row, text="7. MACD 모멘텀 필터", width=30, font=("Malgun Gothic", 10)).pack(side=tk.LEFT)
        for k in ["macd_fast", "macd_slow", "macd_sig"]:
            ent = ttk.Entry(macd_sel_row, width=5, justify='center')
            ent.insert(0, str(self.app.settings.get(k, self.default_settings[k])))
            ent.pack(side=tk.LEFT, padx=3)
            self.params_entries[k] = ent
        ttk.Label(macd_sel_row, text=" (P/S/Sig) ", font=("Malgun Gothic", 9), foreground=self.app.colors['secondary']).pack(side=tk.LEFT, padx=5)

        # --- [3] 1단계 진입 조건 (Stage 1 Entry) ---
        entry_f = ttk.LabelFrame(st_main_frame, text=" 🚀 [3] 1단계 진입 전략 (Stage 1 Entry) ", padding="15")
        entry_f.pack(fill=tk.X, pady=10)
        
        # Type A
        type_a_f = ttk.LabelFrame(entry_f, text=" Type A (눌림목 타점) ", padding="10")
        type_a_f.pack(fill=tk.X, pady=5)
        add_toggle_param_row(type_a_f, "use_type_a", "Type A 활성화", "buy_rsi_max_a", "", "RSI 눌림 상한:", widget_list=self.type_a_widgets)
        add_toggle_param_row(type_a_f, "use_smart_buy_a", "ㄴ Smart Buy (수급 컨펌)", hint="체결강도/잔량비 확인", widget_list=self.type_a_widgets)
        add_toggle_param_row(type_a_f, "use_gc", "ㄴ 이평선 골든크로스", "gc_window", "분", "최소 유지 시간", widget_list=self.type_a_widgets)
        add_toggle_param_row(type_a_f, "use_rsi_signal", "ㄴ RSI 50 상향돌파", "rsi_window", "분", "최소 유지 시간", widget_list=self.type_a_widgets)
        add_toggle_param_row(type_a_f, "use_ma60_safe", "ㄴ MA60 안전구역", "ma60_min", "%", "위치:", sub_param_key="ma60_max", sub_unit="%", sub_label="~", widget_list=self.type_a_widgets)
        add_toggle_param_row(type_a_f, "use_vol_surge", "ㄴ 거래량 급증 확인", "vol_surge", "배", "전일 동시간 대비", widget_list=self.type_a_widgets)

        # Type B
        type_b_f = ttk.LabelFrame(entry_f, text=" Type B (강한 돌파 타점) ", padding="10")
        type_b_f.pack(fill=tk.X, pady=5)
        add_toggle_param_row(type_b_f, "use_type_b", "Type B 활성화", "buy_rsi_max_b", "", "RSI 돌파 상한:", widget_list=self.type_b_widgets)
        add_toggle_param_row(type_b_f, "use_smart_buy_b", "ㄴ Smart Buy (수급 컨펌)", hint="체결강도/잔량비 확인", widget_list=self.type_b_widgets)
        add_toggle_param_row(type_b_f, "use_macd_gc_b", "ㄴ MACD 골든크로스", hint="(MACD > Signal)", widget_list=self.type_b_widgets)
        add_toggle_param_row(type_b_f, "use_trend_aligned_b", "ㄴ 정배열 추세 확인", hint="(20 > 60MA)", widget_list=self.type_b_widgets)
        add_toggle_param_row(type_b_f, "use_type_b_vol", "ㄴ 거래량 급증 확인", "vol_surge_b", "배", "전일 동시간 대비", widget_list=self.type_b_widgets)

        # Common Filters
        comm_f = ttk.LabelFrame(entry_f, text=" 단계 공통 필수 필터 ", padding="10")
        comm_f.pack(fill=tk.X, pady=5)
        add_toggle_param_row(comm_f, "use_market_filter", "1. 시장지수 필터 (MSI/VIX/ADX)", hint="시장 급락 시 진입 금지")
        add_toggle_param_row(comm_f, "use_rsi_min", "2. 매수 RSI 하한선", "buy_rsi_min", "", "공통 최소 RSI")
        add_toggle_param_row(comm_f, "use_macd_signal", "3. MACD 모멘텀 상승", hint="(OSC > 0 & 증가 중)")
        add_toggle_param_row(comm_f, "use_rsi_sig_filter", "4. RSI 모멘텀 상승", hint="(RSI(14) >= RSI_Sig & RSI > RSI.shift(1))")

        # --- [4] 2단계 추매 전략 (Stage 2 Scaling) ---
        s2_f = ttk.LabelFrame(st_main_frame, text=" 📈 [4] 2단계 추매 전략 (Scaling Up) ", padding="15")
        s2_f.pack(fill=tk.X, pady=10)
        
        add_toggle_param_row(s2_f, "use_s2_ma20", "1. 20일선 지지 확인", "s2_ma20_gap", "%", "이격도 기준")
        add_toggle_param_row(s2_f, "use_smart_buy_s2", "2. Smart Buy (수급 컨펌)", hint="체결강도/잔량비 확인")
        add_toggle_param_row(s2_f, "use_s2_ma5", "3. 5일선 지지 확인", "s2_ma5_gap", "%", "이격도 기준")
        add_toggle_param_row(s2_f, "use_s2_vol_surge", "4. 추매 거래량 분출", "s2_vol_ratio", "배", "최소 거래량")
        add_toggle_param_row(s2_f, "use_s2_rsi_sig", "5. RSI 모멘텀 상승", "s2_rsi_gap", "%", "최소 상회폭")
        add_toggle_param_row(s2_f, "use_s2_macd_gc", "6. MACD 모멘텀 상승", "s2_macd_gap", "%", "최소 상회폭")
        add_toggle_param_row(s2_f, "use_s2_profit", "7. 포지션 수익률 필터", "s2_profit_limit", "%", "최소 수익률(>)")
        add_toggle_param_row(s2_f, "use_s2_delay", "8. 시간 기반 대기", "s2_delay_hours", "시간", "최소 경과 시간")


        # --- [5] 전술적 진입 컨펌 (Tactical Entry Confirmation) ---
        rt_f = ttk.LabelFrame(st_main_frame, text=" 🔫 [5] 전술적 진입 컨펌 (Entry Confirmation) ", padding="15")
        rt_f.pack(fill=tk.X, pady=10)
        add_multi_standard_row(rt_f, "Smart Buy 기준 설정 (VP/BA)", "smart_buy_vp", "%", "최소 체결강도", sub_param_key="smart_buy_ba_ratio", sub_unit="배", sub_label="매도잔량비")

        # --- [6] 매도 및 리스크 관리 (Exit & Risk Management) ---
        exit_f = ttk.LabelFrame(st_main_frame, text=" 🛡️ [6] 매도 및 리스크 관리 (Exit & Risk) ", padding="15")
        exit_f.pack(fill=tk.X, pady=10)
        
        # Global Safety
        safe_f = ttk.LabelFrame(exit_f, text=" 공통 안전 장치 및 전술 (Safety & Tactical) ", padding="10")
        safe_f.pack(fill=tk.X, pady=5)
        add_toggle_param_row(safe_f, "use_smart_exit", "1. Smart Exit (실시간 매도)", "smart_exit_vp", "%", "체결강도 하회", sub_param_key="smart_exit_ba_ratio", sub_unit="배", sub_label="매수잔량비")
        add_standard_row(safe_f, "   ㄴ 스마트 엑싯 가격 하한", "smart_exit_price_threshold", "%", "고점 대비 (예: -2.0%)")
        add_standard_row(safe_f, "2. 갭하락 즉시매도(Cut)", "gap_down_limit", "%", "시가 갭하락 제한")
        
        row_grace = ttk.Frame(safe_f, padding="4")
        row_grace.pack(fill=tk.X)
        self.conf_vars["use_gap_grace"] = tk.BooleanVar(value=self.app.settings.get("use_gap_grace", True))
        ttk.Checkbutton(row_grace, text="3. 장 시작 15분 매도 유예 (손절/Flash Exit 털기 방지)", 
                        variable=self.conf_vars["use_gap_grace"]).pack(side=tk.LEFT)
        
        # S1 Exit
        s1e_f = ttk.LabelFrame(exit_f, text=" 1단계 전용 (Stage 1 Exit) [ (A)눌림 | (B)돌파 ] ", padding="10")
        s1e_f.pack(fill=tk.X, pady=5)
        
        def add_toggle_split_row(parent, toggle_key, label, key_a, key_b, unit):
            row = ttk.Frame(parent, padding="4")
            row.pack(fill=tk.X)
            
            # Checkbox
            self.conf_vars[toggle_key] = tk.BooleanVar(value=self.app.settings.get(toggle_key, True))
            cb = ttk.Checkbutton(row, text=label, variable=self.conf_vars[toggle_key], width=20)
            cb.pack(side=tk.LEFT)
            
            ttk.Label(row, text="(A)", foreground=self.app.colors['secondary']).pack(side=tk.LEFT)
            ea = ttk.Entry(row, width=6, justify='center')
            ea.insert(0, str(self.app.settings.get(key_a, "")))
            ea.pack(side=tk.LEFT, padx=2)
            ttk.Label(row, text=unit, width=3).pack(side=tk.LEFT)
            
            ttk.Label(row, text=" | (B)", foreground=self.app.colors['secondary']).pack(side=tk.LEFT)
            eb = ttk.Entry(row, width=6, justify='center')
            eb.insert(0, str(self.app.settings.get(key_b, "")))
            eb.pack(side=tk.LEFT, padx=2)
            ttk.Label(row, text=unit, width=3).pack(side=tk.LEFT)
            
            self.params_entries[key_a] = ea
            self.params_entries[key_b] = eb
            
            # Dimming Logic
            def sync_state(*args):
                st = tk.NORMAL if self.conf_vars[toggle_key].get() else tk.DISABLED
                ea.config(state=st); eb.config(state=st)
            self.conf_vars[toggle_key].trace_add("write", sync_state)
            sync_state()
            # [v22.7] Error fix: prevent recursion error when sync_state runs initially
            # No changes needed here, just noting the logic stability.

        add_toggle_split_row(s1e_f, "use_s1_tp", "1. 목표 익절가", "s1_a_take_profit", "s1_b_take_profit", "%")
        add_toggle_split_row(s1e_f, "use_s1_stop_loss", "2. 기본 손절가", "s1_a_stop_loss", "s1_b_stop_loss", "%")
        add_toggle_split_row(s1e_f, "use_s1_flash_exit", "3. Flash Exit", "s1_a_flash_exit_pct", "s1_b_flash_exit_pct", "%")

        # S2 Exit
        s2e_f = ttk.LabelFrame(exit_f, text=" 2단계 전용 (Stage 2 Exit) ", padding="10")
        s2e_f.pack(fill=tk.X, pady=5)
        add_toggle_param_row(s2e_f, "use_s2_tp", "1. 목표 익절가", "s2_take_profit", "%", "평단가 대비")
        add_toggle_param_row(s2e_f, "use_s2_stop_loss", "2. 기본 손절가", "s2_stop_loss", "%", "평단가 대비")
        add_toggle_param_row(s2e_f, "use_s2_flash_exit", "3. Flash Exit", "s2_flash_exit_pct", "%", "매수후최고가 대비")

        # ATR Exit
        atr_f = ttk.LabelFrame(exit_f, text=" 변동성 기반 (ATR Exit) ", padding="10")
        atr_f.pack(fill=tk.X, pady=5)
        add_toggle_param_row(atr_f, "use_atr_exit", "ATR 기반 전략 활성화", "atr_period", "일", "계산 기간")
        add_toggle_param_row(atr_f, "use_atr_stop_loss", "    ㄴ ATR 손절 배수(SL)", "atr_multiplier_sl", "배", "평단 - (ATR*N)")
        add_toggle_param_row(atr_f, "use_atr_trailing_stop", "    ㄴ ATR 추격 배수(Trail)", "atr_multiplier_trail", "배", "고가 - (ATR*N)")
        
        # Toggle initializing
        def run_toggles():
            def apply_state(widgets, is_on):
                st = tk.NORMAL if is_on else tk.DISABLED
                for w in widgets:
                    try: w.config(state=st)
                    except: pass
            
            def on_type_a():
                apply_state(self.type_a_widgets, self.conf_vars["use_type_a"].get())
            def on_type_b():
                apply_state(self.type_b_widgets, self.conf_vars["use_type_b"].get())
            
            if "use_type_a" in self.conf_vars:
                self.conf_vars["use_type_a"].trace_add("write", lambda *a: on_type_a())
                on_type_a()
            if "use_type_b" in self.conf_vars:
                self.conf_vars["use_type_b"].trace_add("write", lambda *a: on_type_b())
                on_type_b()

        self.app.root.after(100, run_toggles)

    def reset_to_defaults(self):
        if messagebox.askyesno("초기화 확인", "모든 파라미터를 초기값으로 복원하시겠습니까?"):
            self.app.settings = self.default_settings.copy()
            # UI 강제 갱신 트리거 (Get_Price_GUI에서 감시)
            if hasattr(self.app, 'reset_ui_tabs'):
                self.app.reset_ui_tabs()
            else:
                messagebox.showinfo("알림", "설정이 초기화되었습니다. 프로그램을 재시작하거나 탭을 다시 열어주세요.")
                                       
    def save_settings(self, sync_to_backtest=True):
        import traceback
        try:
            s1_unit = self.conf_vars.get("s1_sizing_unit").get() if "s1_sizing_unit" in self.conf_vars else "원"
            s2_unit = self.conf_vars.get("s2_sizing_unit").get() if "s2_sizing_unit" in self.conf_vars else "원"

            for key in self.params_entries:
                val = self.params_entries[key].get().strip()
                if not val: continue
                
                # 콤마 제거 후 변환
                clean_val = val.replace(',', '')
                
                if key == "stage1_amt" and s1_unit == "원":
                    self.app.settings[key] = int(float(clean_val))
                elif key == "s1_equity_pct" and s1_unit == "%":
                    self.app.settings[key] = float(clean_val)
                elif key == "stage2_amt" and s2_unit == "원":
                    self.app.settings[key] = int(float(clean_val))
                elif key == "s2_equity_pct" and s2_unit == "%":
                    self.app.settings[key] = float(clean_val)
                elif key in ['total_capital', 'max_slots', 'gc_window', 'rsi_window', 'atr_period', 'power_sustained_min']:
                    self.app.settings[key] = int(float(clean_val))
                else:
                    try: self.app.settings[key] = float(clean_val)
                    except: pass

            self.app.settings["s1_sizing_unit"] = s1_unit
            self.app.settings["s2_sizing_unit"] = s2_unit

            for key, var in self.conf_vars.items():
                if hasattr(var, 'get'):
                    self.app.settings[key] = var.get()

            # [Persistence] 날짜 저장
            if hasattr(self.app, 'backtest_entries'):
                for k in ['start_date', 'end_date']:
                    if k in self.app.backtest_entries:
                        self.app.settings[f'bt_{k}'] = self.app.backtest_entries[k].get().strip()
            
            if hasattr(self.app, '_final_save_settings'):
                self.app._final_save_settings()
            
            if sync_to_backtest and hasattr(self.app, 'backtest_tab'):
                self.app.backtest_tab._sync_opt_toggles_with_settings()
            
            self.app.log("✅ 설정이 저장되었습니다.")
            if self.app.settings_status_label:
                self.app.settings_status_label.config(text="저장 완료")
                self.app.root.after(3000, lambda: self.app.settings_status_label.config(text="") if self.app.settings_status_label else None)
        except Exception as e:
            messagebox.showerror("오류", f"저장 실패: {e}")
            traceback.print_exc()
