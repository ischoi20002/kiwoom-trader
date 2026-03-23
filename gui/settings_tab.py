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

        # [1] 종목 선정 필터
        filter_frame = ttk.LabelFrame(st_main_frame, text=" 🔍 [1] 종목 선정 필터 (Stock Selection) ", padding="20")
        filter_frame.pack(fill=tk.X, pady=10)
        
        param_configs = [
            ("min_trde_val", "1. 최소 거래대금", "억", "⬇️ 유동성 필터링 (대형주/중소형주 범위)"),
            ("vol_ratio", "2. 거래량 분출 비율", "배", "⬆️ 강력한 수급 포착 (전일 동시간 대비)"),
            ("vol_lookback", "3. 분출 확인 기간", "일", "⬆️ 과거 이력 포함 범위"),
            ("ma_convergence", "4. 이평선 수렴도 기준", "%", "⬇️ 밀집 구간 포착 정밀도 (수치가 작을수록 밀집)"),
            ("sel_rsi_min", "5. 선정 RSI 하한", "", "⬆️ 과매도 반등 하한"),
            ("sel_rsi_max", "6. 선정 RSI 상한", "", "⬇️ 과매수 진입 전 필터링"),
            ("macd_fast", "7. MACD 단기 EMA", "", "포착 속도 조절"),
            ("macd_slow", "8. MACD 장기 EMA", "", "추세 안정성 조절"),
            ("macd_sig", "9. MACD Signal EMA", "", "신호 민감도 조절")
        ]
        
        for key, label, unit, impact in param_configs:
            row = ttk.Frame(filter_frame, padding="6")
            row.pack(fill=tk.X)
            
            ttk.Label(row, text=label, width=25, font=("Malgun Gothic", 10)).pack(side=tk.LEFT)
            ent = ttk.Entry(row, width=10, justify='center')
            ent.insert(0, str(self.app.settings.get(key, self.default_settings[key])))
            ent.pack(side=tk.LEFT, padx=5)
            
            if unit:
                ttk.Label(row, text=unit, width=5).pack(side=tk.LEFT)
            else:
                ttk.Label(row, text="", width=5).pack(side=tk.LEFT)
                
            ttk.Label(row, text=impact, font=("Malgun Gothic", 9), foreground=self.app.colors['secondary']).pack(side=tk.LEFT, padx=10)
            self.params_entries[key] = ent
            
            # 아래 행과의 구분선
            ttk.Separator(filter_frame, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=10)

        # [2] 1단계: 신규 매수 진입 조건 (Stage 1)
        s1_container = ttk.LabelFrame(st_main_frame, text=" 🚀 [2] 신규 매수 조건 (Stage 1 Entry) ", padding="15")
        s1_container.pack(fill=tk.X, pady=10)
        
        group_a = ttk.LabelFrame(s1_container, text=" Type A (눌림목) 전용 ", padding="10")
        group_a.pack(fill=tk.X, pady=5)
        group_b = ttk.LabelFrame(s1_container, text=" Type B (추세 돌파) 전용 ", padding="10")
        group_b.pack(fill=tk.X, pady=5)
        group_common = ttk.LabelFrame(s1_container, text=" 단계 공통 필수 조건 ", padding="10")
        group_common.pack(fill=tk.X, pady=5)

        def add_signal_row(parent, var_key, title, param_key=None, unit="", label_text="", 
                           sub_param_key=None, sub_unit="", sub_label="", 
                           widget_list=None, command=None, include_cb_in_list=True):
            """시그널 설정을 위한 한 행 추가 (v17.5: 위젯 리스트 관리 기능 보완)용"""
            row = ttk.Frame(parent, padding="6")
            row.pack(fill=tk.X)
            self.conf_vars[var_key] = tk.BooleanVar(value=self.app.settings.get(var_key, True))
            cb = ttk.Checkbutton(row, text=title, variable=self.conf_vars[var_key], width=20, command=command)
            cb.pack(side=tk.LEFT)
            
            sub_widgets = []
            if label_text:
                lbl = ttk.Label(row, text=f" {label_text} ", font=("Malgun Gothic", 9), foreground=self.app.colors['secondary'])
                lbl.pack(side=tk.LEFT)
                sub_widgets.append(lbl)

            if param_key:
                ent = ttk.Entry(row, width=6, justify='center')
                ent.insert(0, str(self.app.settings.get(param_key, self.default_settings[param_key])))
                ent.pack(side=tk.LEFT, padx=5)
                un = ttk.Label(row, text=unit, font=("Helvetica", 9))
                un.pack(side=tk.LEFT)
                self.params_entries[param_key] = ent
                sub_widgets.extend([ent, un])
                
                if sub_param_key:
                    sep_text = sub_label if sub_label else " ~ "
                    sep = ttk.Label(row, text=f" {sep_text} ", font=("Helvetica", 9))
                    sep.pack(side=tk.LEFT)
                    sub_ent = ttk.Entry(row, width=6, justify='center')
                    sub_ent.insert(0, str(self.app.settings.get(sub_param_key, self.default_settings[sub_param_key])))
                    sub_ent.pack(side=tk.LEFT, padx=5)
                    sub_un = ttk.Label(row, text=sub_unit, font=("Helvetica", 9))
                    sub_un.pack(side=tk.LEFT)
                    self.params_entries[sub_param_key] = sub_ent
                    sub_widgets.extend([sep, sub_ent, sub_un])
            
            if widget_list is not None:
                if include_cb_in_list:
                    widget_list.append(cb)
                widget_list.extend(sub_widgets)
            
            ttk.Separator(parent, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=10)

        def add_split_exit_row(parent, var_key, title, param_a, param_b, unit):
            row = ttk.Frame(parent)
            row.pack(fill=tk.X, pady=2)
            self.conf_vars[var_key] = tk.BooleanVar(value=self.app.settings.get(var_key, True))
            cb = ttk.Checkbutton(row, text=title, variable=self.conf_vars[var_key], width=20)
            cb.pack(side=tk.LEFT)
            
            ttk.Label(row, text=" (A) ", foreground=self.app.colors['secondary']).pack(side=tk.LEFT)
            ent_a = ttk.Entry(row, width=5, justify='center')
            ent_a.insert(0, str(self.app.settings.get(param_a, self.default_settings[param_a])))
            ent_a.pack(side=tk.LEFT, padx=2)
            ttk.Label(row, text=unit).pack(side=tk.LEFT)
            self.params_entries[param_a] = ent_a
            
            ttk.Label(row, text=" | ", foreground=self.app.colors['secondary']).pack(side=tk.LEFT, padx=10)
            
            ttk.Label(row, text=" (B) ", foreground=self.app.colors['secondary']).pack(side=tk.LEFT)
            ent_b = ttk.Entry(row, width=5, justify='center')
            ent_b.insert(0, str(self.app.settings.get(param_b, self.default_settings[param_b])))
            ent_b.pack(side=tk.LEFT, padx=2)
            ttk.Label(row, text=unit).pack(side=tk.LEFT)
            self.params_entries[param_b] = ent_b

            ttk.Separator(parent, orient=tk.HORIZONTAL).pack(fill=tk.X, padx=10)

        def toggle_type_a():
            is_active = self.conf_vars["use_type_a"].get()
            state = tk.NORMAL if is_active else tk.DISABLED
            for w in self.type_a_widgets:
                try: w.config(state=state)
                except: pass
            
            # [v18.8] Sync all sub-features with the main toggle (On/Off)
            for k in ["use_gc", "use_vol_surge", "use_ma60_safe", "use_rsi_signal"]:
                if k in self.conf_vars:
                    self.conf_vars[k].set(is_active)
        
        def toggle_type_b():
            is_active = self.conf_vars["use_type_b"].get()
            state = tk.NORMAL if is_active else tk.DISABLED
            for w in self.type_b_widgets:
                try: w.config(state=state)
                except: pass
                
            # [v18.8] Sync all sub-features with the main toggle (On/Off)
            for k in ["use_macd_gc_b", "use_trend_aligned_b", "use_type_b_vol"]:
                if k in self.conf_vars:
                    self.conf_vars[k].set(is_active)

        # Type A settings
        ttk.Label(group_a, text="※ 일봉 골든크로스(5/20) 조건 하에서만 발동됩니다.", font=("Malgun Gothic", 9), foreground=self.app.colors['primary']).pack(anchor=tk.W, padx=10, pady=2)
        add_signal_row(group_a, "use_type_a", "Type A 활성화", "buy_rsi_max_a", "", "RSI 눌림 상한:", 
                       widget_list=self.type_a_widgets, command=toggle_type_a, include_cb_in_list=False)
        add_signal_row(group_a, "use_gc", "1. 골든크로스 15분", label_text="(이평선 간격 포함)", widget_list=self.type_a_widgets)
        add_signal_row(group_a, "use_vol_surge", "2. 거래량 급증(A)", "vol_surge", "배", "전일 동시간 대비:", widget_list=self.type_a_widgets)
        add_signal_row(group_a, "use_ma60_safe", "3. MA60 안전구역", "ma60_min", "%", "위치:", "ma60_max", "%", widget_list=self.type_a_widgets)
        add_signal_row(group_a, "use_rsi_signal", "4. RSI 50 상향돌파", "rsi_window", "분", "유지 시간:", widget_list=self.type_a_widgets)

        # Type B settings
        add_signal_row(group_b, "use_type_b", "Type B 활성화", "buy_rsi_max_b", "", "RSI 돌파 상한:", 
                       widget_list=self.type_b_widgets, command=toggle_type_b, include_cb_in_list=False)
        add_signal_row(group_b, "use_macd_gc_b", "1. MACD 골든크로스(B)", label_text="(MACD > Sig Cross)", widget_list=self.type_b_widgets)
        add_signal_row(group_b, "use_trend_aligned_b", "2. 정배열 추세 확인(B)", label_text="(20>60MA & P>20MA)", widget_list=self.type_b_widgets)
        add_signal_row(group_b, "use_type_b_vol", "3. 거래량 급증(B)", "vol_surge_b", "배", "전일 동시간 대비:", widget_list=self.type_b_widgets)

        # Common Stage 1 settings
        add_signal_row(group_common, "use_market_filter", "1. 시장지수 필터", label_text="(하락장 매수 정지)")
        add_signal_row(group_common, "use_vol_power", "2. 체결강도 강화", "vol_power_lvl", "", "기준값:")
        add_signal_row(group_common, "use_macd_signal", "3. MACD 최종 확인", label_text="(OSC > 0 강세 확인)")
        add_signal_row(group_common, "use_rsi_min", "4. 매수 RSI 하한", "buy_rsi_min", "", "전략 공통:")

        self.app.root.after(100, toggle_type_a)
        self.app.root.after(100, toggle_type_b)

        # [3] 2단계: 추매 승인 조건 (Stage 2)
        s2_container = ttk.LabelFrame(st_main_frame, text=" 📈 [3] 추매 승인 조건 (Stage 2 Buy) ", padding="15")
        s2_container.pack(fill=tk.X, pady=10)
        
        # [v22.0] 1단계와 유사한 논리 흐름으로 순서 재정렬 (추세 -> 지지 -> 거래량 -> 지표 -> 제약)
        add_signal_row(s2_container, "use_s2_ma20", "1. 20일선 위 위치", "s2_ma20_gap", "%", "이격도(>)")
        add_signal_row(s2_container, "use_s2_ma5", "2. MA5 지지 확인", "s2_ma5_gap", "%", "이격도(>)")
        add_signal_row(s2_container, "use_s2_vol_surge", "3. 거래 수급 확인", "s2_vol_ratio", "배", "평균 대비:")
        add_signal_row(s2_container, "use_s2_rsi_sig", "4. RSI 강세 유지", "s2_rsi_gap", "%", "이격도(>)")
        add_signal_row(s2_container, "use_s2_macd_gc", "5. MACD 정배열", "s2_macd_gap", "%", "이격도(>)")
        add_signal_row(s2_container, "use_s2_osc", "6. OSC 확장 확인", "s2_osc_gap", "%", "이격도(>)")
        add_signal_row(s2_container, "use_s2_profit", "7. 수익권 확인", "s2_profit_limit", "%", "최소 수익률:")
        
        # [v21.1] 2차 매수 최소 대기 기간 (Day Delay)
        row_delay = ttk.Frame(s2_container, padding="6")
        row_delay.pack(fill=tk.X)
        ttk.Label(row_delay, text="최소 대기 기간 (1차 매수 후)", width=25).pack(side=tk.LEFT)
        ent_delay = ttk.Entry(row_delay, width=6, justify='center')
        ent_delay.insert(0, str(self.app.settings.get("s2_min_days", 1)))
        ent_delay.pack(side=tk.LEFT, padx=5)
        ttk.Label(row_delay, text="일", font=("Helvetica", 9)).pack(side=tk.LEFT)
        self.params_entries["s2_min_days"] = ent_delay

        # [4] 매도(탈출) 및 자금 운용 조건
        exit_container = ttk.LabelFrame(st_main_frame, text=" 💰 [4] 매도 및 자금 관리 (Exit & Money) ", padding="15")
        exit_container.pack(fill=tk.X, pady=10)

        # (4.1) 1단계(정찰) 매도 조건
        s1_exit_sub = ttk.LabelFrame(exit_container, text=" 1단계(정찰) 매도 조건 [ (A)눌림 | (B)돌파 ] ", padding="10")
        s1_exit_sub.pack(fill=tk.X, pady=5)
        add_split_exit_row(s1_exit_sub, "use_s1_flash_exit", "Flash Exit 적용", "s1_a_flash_exit_pct", "s1_b_flash_exit_pct", "%")
        add_split_exit_row(s1_exit_sub, "use_s1_ma_break", "이평선(20일) 이탈", "s1_a_ma_break_buffer", "s1_b_ma_break_buffer", "%")
        add_split_exit_row(s1_exit_sub, "use_s1_stop_loss", "기본 손절 적용 (S1)", "s1_a_stop_loss", "s1_b_stop_loss", "%")
        add_split_exit_row(s1_exit_sub, "use_s1_tp", "기본 익절 적용 (S1)", "s1_a_take_profit", "s1_b_take_profit", "%")

        # (4.2) 2단계(본대) 매도 조건
        s2_exit_sub = ttk.LabelFrame(exit_container, text=" 2단계(본대) 매도 조건 ", padding="10")
        s2_exit_sub.pack(fill=tk.X, pady=5)
        add_signal_row(s2_exit_sub, "use_s2_flash_exit", "Flash Exit 적용", "s2_flash_exit_pct", "%", "고점 대비 하락폭:")
        add_signal_row(s2_exit_sub, "use_s2_rsi_tp", "다이나믹 50% 익절", "s2_rsi_tp_limit", "", "RSI 과열:", sub_param_key="s2_rsi_tp_drop", sub_unit="%", sub_label=" 고점대비 하락:")
        add_signal_row(s2_exit_sub, "use_s2_rsi_exit", "RSI 추세 이탈 매도", "rsi_exit_limit", "", "RSI 기준값:")
        add_signal_row(s2_exit_sub, "use_s2_ma_break", "이평선(20일) 이탈", "s2_ma_break_buffer", "%", "완충 지대:")
        add_signal_row(s2_exit_sub, "use_s2_stop_loss", "기본 손절 적용 (S2)", "s2_stop_loss", "%", "손절 수익률:")
        add_signal_row(s2_exit_sub, "use_s2_tp", "기본 익절 적용 (S2)", "s2_take_profit", "%", "목표 수익률:")

        # (4.3) 자금 운용 및 공통 설정
        money_sub = ttk.LabelFrame(exit_container, text=" 자금 운용 및 기타 설정 ", padding="10")
        money_sub.pack(fill=tk.X, pady=5)
        
        def add_risk_row(parent, label_text, param_key, unit=""):
            row = ttk.Frame(parent, padding="5")
            row.pack(fill=tk.X)
            ttk.Label(row, text=label_text, width=25).pack(side=tk.LEFT)
            ent = ttk.Entry(row, width=10, justify='center')
            ent.insert(0, str(self.app.settings.get(param_key, self.default_settings[param_key])))
            ent.pack(side=tk.LEFT, padx=5)
            ttk.Label(row, text=unit).pack(side=tk.LEFT)
            self.params_entries[param_key] = ent

        add_risk_row(money_sub, "1. 총 운용 자산 (초기 자본):", "total_capital", "원")
        add_risk_row(money_sub, "2. 최대 감시 종목 수 (슬롯):", "max_slots", "개")
        add_risk_row(money_sub, "3. 1단계(정찰) 비중 금액:", "stage1_amt", "원")
        add_risk_row(money_sub, "4. 2단계(본대) 추매 금액:", "stage2_amt", "원")
        add_risk_row(money_sub, "5. 갭하락 마지노선:", "gap_down_limit", "%")
        
        # [v6.0] 가상 매매 자동화 토글
        row_auto = ttk.Frame(money_sub, padding="5")
        row_auto.pack(fill=tk.X)
        self.conf_vars["use_auto_virtual"] = tk.BooleanVar(value=self.app.settings.get("use_auto_virtual", True))
        ttk.Checkbutton(row_auto, text="가상 매수 자동화 (타점 포착 시 자동 진입)", 
                        variable=self.conf_vars["use_auto_virtual"]).pack(side=tk.LEFT, padx=10)
        
        # (하단 버튼은 이제 self.parent에 직접 배치됨)

    def reset_to_defaults(self):
        if messagebox.askyesno("초기화 확인", "모든 파라미터를 초기값으로 복원하시겠습니까?"):
            self.app.settings = self.default_settings.copy()
            for key in self.params_entries:
                self.params_entries[key].delete(0, tk.END)
                self.params_entries[key].insert(0, str(self.app.settings[key]))
            for key, var in self.conf_vars.items():
                var.set(self.app.settings.get(key, self.default_settings.get(key, True)))
            self.save_settings()

    def save_settings(self):
        try:
            for key in self.params_entries:
                val = self.params_entries[key].get()
                if key in ["vol_lookback", "rsi_window", "stage1_amt", "stage2_amt", "stage3_amt", "s2_min_days", "max_slots"]:
                    self.app.settings[key] = int(val) if val.isdigit() else int(float(val))
                elif key in ["rsi_tp_limit", "s2_rsi_tp_limit", "rsi_exit_limit", "stop_loss", "s2_stop_loss", "stage2_trigger", "take_profit"]:
                    self.app.settings[key] = float(val)
                else:
                    self.app.settings[key] = float(val)
            
            for key, var in self.conf_vars.items():
                self.app.settings[key] = var.get()

            # [Persistence] 백테스팅 날짜 저장
            if hasattr(self.app, 'backtest_entries') and 'start_date' in self.app.backtest_entries:
                try:
                    raw_start = self.app.backtest_entries['start_date'].get().strip()
                    raw_end = self.app.backtest_entries['end_date'].get().strip()
                    self.app.settings['bt_start_date'] = pd.to_datetime(raw_start).strftime('%Y-%m-%d')
                    self.app.settings['bt_end_date'] = pd.to_datetime(raw_end).strftime('%Y-%m-%d')
                except:
                    self.app.settings['bt_start_date'] = self.app.backtest_entries['start_date'].get().strip()
                    self.app.settings['bt_end_date'] = self.app.backtest_entries['end_date'].get().strip()
            
            # Use KiwoomApp's save logic or handle here
            self.app._final_save_settings()
            
            # [v15.8] 백테스팅 탭의 최적화 체크박스 상태 강제 동기화
            if hasattr(self.app, 'backtest_tab'):
                self.app.backtest_tab._sync_opt_toggles_with_settings()
            
            self.app.log("✅ 설정이 저장되었습니다.")
            self.app._update_criteria_label()
            if self.app.settings_status_label:
                self.app.settings_status_label.config(text="저장 완료")
                self.app.root.after(3000, lambda: self.app.settings_status_label.config(text="") if self.app.settings_status_label else None)
        except Exception as e:
            messagebox.showerror("오류", f"저장 실패: {e}")
