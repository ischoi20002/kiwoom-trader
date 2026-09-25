import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import os
import pandas as pd
import numpy as np
import importlib.util
import webbrowser
from datetime import datetime, timedelta
import traceback
from typing import Optional

class BacktestTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self.gui_queue = queue.Queue() # [v35.2] Thread-safe GUI update queue
        self.btn_narrative_report: Optional[ttk.Button] = None
        self.btn_daily_settlement: Optional[ttk.Button] = None
        self.btn_single_stock_sim: Optional[ttk.Button] = None
        self.last_opt_df = None  # [v25.2] 최적합 파라미터 저장용
        self._ss_dlg: Optional[tk.Toplevel] = None
        self._ss_status_lbl: Optional[tk.Label] = None
        self._ss_btn_run: Optional[tk.Button] = None
        self._ss_code_entry: Optional[tk.Entry] = None
        self._ss_start_entry: Optional[tk.Entry] = None
        self._ss_end_entry: Optional[tk.Entry] = None
        self._ss_hp_var: Optional[tk.BooleanVar] = None
        self._ss_strategy_var: Optional[tk.StringVar] = None
        self._ss_vol_k_entry: Optional[tk.Entry] = None
        self._ss_vol_k_hint: Optional[tk.Label] = None
        self._ss_header_lbl: Optional[tk.Label] = None
        self._ss_sub_lbl: Optional[tk.Label] = None
        self._ss_running: bool = False
        self._init_vars()
        self._setup_ui()
        self._poll_queue()

    def _init_vars(self):
        """[v26.0] 동적 테이블 렌더링을 위한 설정 초기화 (38개 파라미터 매핑)"""
        self.backtest_entries = {}
        self.backtest_force_refresh_var = tk.BooleanVar(value=False)
        from core.defaults import DEFAULT_SETTINGS
        self.base_backtest_settings = DEFAULT_SETTINGS.copy()
        
        # [v16.50] 최적화 상태 변수를 메인 앱의 중앙 저장소와 연동
        self.opt_params_vars = self.app.opt_params_vars
        
        # [v26.0] 최적화 파라미터 상세 정보 사전 (표시명, 카테고리, 설명)
        # [주의] 종목선정·전환게이트·RT·Smart Buy/Exit 수치는 SimpleBacktester 최적화와 무관 → 제외
        # 엑셀(optimization_params_audit) 확정 순서 우선 → 이후 레거시 키(미선택 시에도 툴팁용)
        self.PARAM_INFO = {
            's1_equity_pct': ('S1_Equity%', '자금관리', '1단계 비중 (%)'),
            's2_equity_pct': ('S2_Equity%', '자금관리', '2단계 비중 (%)'),
            'buy_rsi_min': ('BuyRSI_Min', 'S1 공통', 'S1 진입 RSI 하한'),
            'buy_rsi_max_a': ('A_RSI_High', 'S1 Type A', '눌림(A) RSI 상한선'),
            'gc_window': ('A_GC_Window', 'S1 Type A', '골든크로스 기간(분)'),
            'rsi_window': ('RSI_Window', 'S1 Type A', 'RSI 상향돌파 윈도(분)'),
            'ma_convergence': ('MA_Conv%', 'S1 Type A', 'GC용 MA5/20 수렴 상한(%)'),
            'ma60_min': ('MA60_Min%', 'S1 Type A', 'MA60 이격 하한(%)'),
            'ma60_max': ('MA60_Max%', 'S1 Type A', 'MA60 이격 상한(%)'),
            'vol_surge': ('A_Vol_Surge', 'S1 Type A', '거래량 급증 배수'),
            'prev_vol_lookback_s1_a': ('PrevVol_S1A', 'S1 Type A', '전일동시간 평균 일수 S1·A'),
            'buy_rsi_min_b': ('B_RSI_Low', 'S1 Type B', '돌파(B) RSI 하한선'),
            'buy_rsi_max_b': ('B_RSI_High', 'S1 Type B', '돌파(B) RSI 상한선'),
            'vol_surge_b': ('B_Vol_Surge', 'S1 Type B', '돌파 거래량 배수'),
            'prev_vol_lookback_s1_b': ('PrevVol_S1B', 'S1 Type B', '전일동시간 평균 일수 S1·B'),
            's2_ma20_gap': ('S2_MA20', 'S2 Type A', '20일선 지지 갭(%)'),
            's2_ma5_gap': ('S2_MA5', 'S2 Type A', '5일선 갭(%)'),
            's2_rsi_gap': ('S2_RSI_Gap', 'S2 Type A', 'RSI 시그널 갭(%)'),
            's2_macd_gap': ('S2_MACD', 'S2 Type A', 'MACD GC 갭'),
            's2_vol_ratio': ('S2_Vol_Ratio', 'S2 Type A', '거래량 급증 배수'),
            'prev_vol_lookback_s2_a': ('PrevVol_S2A', 'S2 Type A', '전일동시간 평균 일수 S2·A'),
            's2_profit_limit': ('S2_Min_Profit', 'S2 Type A', '최소 수익률(%)'),
            's2_delay_hours': ('S2_Delay_Hrs', 'S2 Type A', '진입 후 대기(시간)'),
            's2_b_ma20_gap': ('S2B_MA20', 'S2 Type B', '20일선 지지 갭(B)%'),
            's2_b_ma5_gap': ('S2B_MA5', 'S2 Type B', '5일선 갭(B)%'),
            's2_b_rsi_gap': ('S2B_RSI', 'S2 Type B', 'RSI 시그널 갭(B)%'),
            's2_b_macd_gap': ('S2B_MACD', 'S2 Type B', 'MACD GC(B)'),
            's2_b_vol_ratio': ('S2B_Vol', 'S2 Type B', '거래량 급증(B) 배수'),
            'prev_vol_lookback_s2_b': ('PrevVol_S2B', 'S2 Type B', '전일동시간 평균 일수 S2·B'),
            's2_b_profit_limit': ('S2B_Profit', 'S2 Type B', '최소 수익률(B)%'),
            's2_b_delay_hours': ('S2B_Delay', 'S2 Type B', '대기(B) 시간'),
            'gap_down_limit_a': ('Gap_A', 'Type A 청산', '갭하락 즉시 손절 (A) (%)'),
            'gap_grace_mins_a': ('Grace_A', 'Type A 청산', '개장 매도 유예 (A) (분)'),
            'atr_period_a': ('ATR_P_A', 'Type A 청산', 'ATR 분석 주기 (A) (봉)'),
            's1_a_take_profit': ('A_Take_Profit', 'Type A 청산', '눌림(A) 목표 익절률 (%)'),
            's1_a_stop_loss': ('A_Stop_Loss', 'Type A 청산', '눌림(A) 최대 손절률 (%)'),
            's1_a_flash_exit_pct': ('A_Flash_Exit', 'Type A 청산', '눌림(A) Flash Exit (%)'),
            'atr_multiplier_sl_a': ('A_ATR_Stop', 'Type A 청산', 'S1(A) ATR 손절 배수'),
            'atr_multiplier_trail_a': ('A_ATR_Trail', 'Type A 청산', 'S1(A) ATR 추격 익절 배수'),
            's2_take_profit': ('S2_Take_Profit', 'Type A 청산', 'S2(A) 목표 익절률 (%)'),
            's2_stop_loss': ('S2_Stop_Loss', 'Type A 청산', 'S2(A) 최대 손절률 (%)'),
            's2_flash_exit_pct': ('S2_Flash_Exit', 'Type A 청산', 'S2(A) Flash Exit (%)'),
            'atr_multiplier_sl_s2': ('S2_ATR_Stop', 'Type A 청산', 'S2(A) ATR 손절 배수'),
            'atr_multiplier_trail_s2': ('S2_ATR_Trail', 'Type A 청산', 'S2(A) ATR 추격 익절 배수'),
            'gap_down_limit_b': ('Gap_B', 'Type B 청산', '갭하락 즉시 손절 (B) (%)'),
            'gap_grace_mins_b': ('Grace_B', 'Type B 청산', '개장 매도 유예 (B) (분)'),
            'atr_period_b': ('ATR_P_B', 'Type B 청산', 'ATR 분석 주기 (B) (봉)'),
            's1_b_take_profit': ('B_Take_Profit', 'Type B 청산', '돌파(B) 목표 익절률 (%)'),
            's1_b_stop_loss': ('B_Stop_Loss', 'Type B 청산', '돌파(B) 최대 손절률 (%)'),
            's1_b_flash_exit_pct': ('B_Flash_Exit', 'Type B 청산', '돌파(B) Flash Exit (%)'),
            'atr_multiplier_sl_b': ('B_ATR_Stop', 'Type B 청산', 'S1(B) ATR 손절 배수'),
            'atr_multiplier_trail_b': ('B_ATR_Trail', 'Type B 청산', 'S1(B) ATR 추격 익절 배수'),
            's2_b_take_profit': ('S2B_Take_Profit', 'Type B 청산', 'S2(B) 목표 익절률 (%)'),
            's2_b_stop_loss': ('S2B_Stop_Loss', 'Type B 청산', 'S2(B) 최대 손절률 (%)'),
            's2_b_flash_exit_pct': ('S2B_Flash_Exit', 'Type B 청산', 'S2(B) Flash Exit (%)'),
            'atr_multiplier_sl_s2_b': ('S2B_ATR_Stop', 'Type B 청산', 'S2(B) ATR 손절 배수'),
            'atr_multiplier_trail_s2_b': ('S2B_ATR_Trail', 'Type B 청산', 'S2(B) ATR 추격 익절 배수'),
            'atr_sl_entry_grace_hours': ('ATR_SL_Grace_H', 'ATR_SL 유예', '진입 후 ATR_SL 배수 확대 시간'),
            'atr_sl_entry_grace_mult': ('ATR_SL_Grace_M', 'ATR_SL 유예', '유예 구간 ATR_SL 배수'),
            'atr_sl_entry_grace_same_day': ('ATR_SL_Grace_Day', 'ATR_SL 유예', '당일 유예 ON/OFF'),
            # ON/OFF (SimpleBacktester에서 의미 있는 체크박스)
            'use_rsi_min': ('Use_RSI_Min', 'S1 공통', 'S1 RSI 하한 ON/OFF'),
            'use_macd_signal': ('Use_MACD', 'S1 공통', '15분 MACD(OSC>0) 필터 ON/OFF'),
            'use_rsi_sig_filter': ('Use_RSI_Sig', 'S1 공통', 'RSI 시그널 필터 ON/OFF'),
            'use_type_a': ('Use_Type_A', 'S1 Type A', 'Type A 진입 ON/OFF'),
            'use_gc': ('Use_GC', 'S1 Type A', '골든크로스 조건 ON/OFF'),
            'use_rsi_signal': ('Use_RSI_Pulse', 'S1 Type A', 'RSI 상향돌파 ON/OFF'),
            'use_ma60_safe': ('Use_MA60', 'S1 Type A', 'MA60 이격 안전대 ON/OFF'),
            'use_vol_surge': ('Use_VolA', 'S1 Type A', '거래량 급증 ON/OFF'),
            'use_type_b': ('Use_Type_B', 'S1 Type B', 'Type B 진입 ON/OFF'),
            'use_type_b_vol': ('Use_VolB', 'S1 Type B', '돌파 거래량 ON/OFF'),
            'use_trend_aligned_b': ('Use_TrendB', 'S1 Type B', '추세 정렬 ON/OFF'),
            'use_macd_gc_b': ('Use_MacdGcB', 'S1 Type B', 'MACD GC ON/OFF'),
            'use_s2_master_a': ('S2_Master_A', 'S2 Type A', 'Type A 추매(S2) 전체 ON/OFF'),
            'use_s2_ma20': ('Use_S2_MA20', 'S2 Type A', 'S2 MA20 조건 ON/OFF'),
            'use_s2_ma5': ('Use_S2_MA5', 'S2 Type A', 'S2 MA5 조건 ON/OFF'),
            'use_s2_rsi_sig': ('Use_S2_RSI', 'S2 Type A', 'S2 RSI 시그널 ON/OFF'),
            'use_s2_macd_gc': ('Use_S2_MACD', 'S2 Type A', 'S2 MACD GC ON/OFF'),
            'use_s2_vol_surge': ('Use_S2_Vol', 'S2 Type A', 'S2 거래량 ON/OFF'),
            'use_s2_profit': ('Use_S2_Profit', 'S2 Type A', 'S2 최소수익 ON/OFF'),
            'use_s2_delay': ('Use_S2_Delay', 'S2 Type A', 'S2 대기시간 ON/OFF'),
            'use_s2_master_b': ('S2_Master_B', 'S2 Type B', 'Type B 추매(S2) 전체 ON/OFF'),
            'use_s2_b_ma20': ('Use_S2B_MA20', 'S2 Type B', 'S2B MA20 ON/OFF'),
            'use_s2_b_ma5': ('Use_S2B_MA5', 'S2 Type B', 'S2B MA5 ON/OFF'),
            'use_s2_b_rsi_sig': ('Use_S2B_RSI', 'S2 Type B', 'S2B RSI ON/OFF'),
            'use_s2_b_macd_gc': ('Use_S2B_MACD', 'S2 Type B', 'S2B MACD ON/OFF'),
            'use_s2_b_vol_surge': ('Use_S2B_Vol', 'S2 Type B', 'S2B 거래량 ON/OFF'),
            'use_s2_b_profit': ('Use_S2B_Profit', 'S2 Type B', 'S2B 최소수익 ON/OFF'),
            'use_s2_b_delay': ('Use_S2B_Delay', 'S2 Type B', 'S2B 대기시간 ON/OFF'),
            'use_gap_down_exit_a': ('Use_GapA', 'Type A 청산', '갭다운 청산(A) ON/OFF'),
            'use_gap_grace_a': ('Use_GraceA', 'Type A 청산', '갭 유예(A) ON/OFF'),
            'use_atr_exit_a': ('Use_AtrExitA', 'Type A 청산', 'ATR 청산 마스터(A) ON/OFF'),
            'use_s1_a_tp': ('Use_A_TP', 'Type A 청산', 'S1A 익절 ON/OFF'),
            'use_s1_a_stop_loss': ('Use_A_SL', 'Type A 청산', 'S1A 손절 ON/OFF'),
            'use_s1_a_flash_exit': ('Use_A_Flash', 'Type A 청산', 'S1A Flash ON/OFF'),
            'use_atr_stop_loss_a': ('Use_A_AtrSL', 'Type A 청산', 'ATR 손절(A) ON/OFF'),
            'use_atr_trailing_stop_a': ('Use_A_Trail', 'Type A 청산', 'ATR 트레일(A) ON/OFF'),
            'use_atr_trail_only_after_tp_s1_a': ('Use_A_TrailAfterTP', 'Type A 청산', '익절후 트레일(S1A) ON/OFF'),
            'use_s2_tp': ('Use_S2_TP', 'Type A 청산', 'S2A 익절 ON/OFF'),
            'use_s2_stop_loss': ('Use_S2_SL', 'Type A 청산', 'S2A 손절 ON/OFF'),
            'use_s2_flash_exit': ('Use_S2_Flash', 'Type A 청산', 'S2A Flash ON/OFF'),
            'use_atr_stop_loss_s2': ('Use_S2_AtrSL', 'Type A 청산', 'ATR 손절(S2A) ON/OFF'),
            'use_atr_trailing_stop_s2': ('Use_S2_Trail', 'Type A 청산', 'ATR 트레일(S2A) ON/OFF'),
            'use_atr_trail_only_after_tp_s2_a': ('Use_S2_TrailAfterTP', 'Type A 청산', '익절후 트레일(S2A) ON/OFF'),
            'use_gap_down_exit_b': ('Use_GapB', 'Type B 청산', '갭다운 청산(B) ON/OFF'),
            'use_gap_grace_b': ('Use_GraceB', 'Type B 청산', '갭 유예(B) ON/OFF'),
            'use_atr_exit_b': ('Use_AtrExitB', 'Type B 청산', 'ATR 청산 마스터(B) ON/OFF'),
            'use_s1_b_tp': ('Use_B_TP', 'Type B 청산', 'S1B 익절 ON/OFF'),
            'use_s1_b_stop_loss': ('Use_B_SL', 'Type B 청산', 'S1B 손절 ON/OFF'),
            'use_s1_b_flash_exit': ('Use_B_Flash', 'Type B 청산', 'S1B Flash ON/OFF'),
            'use_atr_stop_loss_b': ('Use_B_AtrSL', 'Type B 청산', 'ATR 손절(B) ON/OFF'),
            'use_atr_trailing_stop_b': ('Use_B_Trail', 'Type B 청산', 'ATR 트레일(B) ON/OFF'),
            'use_atr_trail_only_after_tp_s1_b': ('Use_B_TrailAfterTP', 'Type B 청산', '익절후 트레일(S1B) ON/OFF'),
            'use_s2_b_tp': ('Use_S2B_TP', 'Type B 청산', 'S2B 익절 ON/OFF'),
            'use_s2_b_stop_loss': ('Use_S2B_SL', 'Type B 청산', 'S2B 손절 ON/OFF'),
            'use_s2_b_flash_exit': ('Use_S2B_Flash', 'Type B 청산', 'S2B Flash ON/OFF'),
            'use_atr_stop_loss_s2_b': ('Use_S2B_AtrSL', 'Type B 청산', 'ATR 손절(S2B) ON/OFF'),
            'use_atr_trailing_stop_s2_b': ('Use_S2B_Trail', 'Type B 청산', 'ATR 트레일(S2B) ON/OFF'),
            'use_atr_trail_only_after_tp_s2_b': ('Use_S2B_TrailAfterTP', 'Type B 청산', '익절후 트레일(S2B) ON/OFF'),
            # 레거시(엑셀 미선택 시에도 툴팁·재활성 대비) — ma_convergence는 S1 Type A 신호용으로 상단 등록
            'min_trde_val': ('MinTrde', '유니버스(A)', '최소 거래대금(억)'),
            'vol_ratio': ('VolRatio', '유니버스(A)', '거래량 분출 배수'),
            'vol_lookback': ('VolLookback', '유니버스(A)', '분출 확인 기간(일)'),
            'sel_rsi_min': ('SelRSI_Min', '유니버스(A)', '선정 RSI 하한'),
            'sel_rsi_max': ('SelRSI_Max', '유니버스(A)', '선정 RSI 상한'),
            'sel_b_rsi_min': ('SelB_RSI_Min', '유니버스(B)', '선정 RSI 하한(B)'),
            'sel_b_rsi_max': ('SelB_RSI_Max', '유니버스(B)', '선정 RSI 상한(B)'),
            'sel_b_min_trde_val': ('SelB_Trde', '유니버스(B)', '최소 거래대금(B)억'),
        }

    def _show_copyable_error(self, title, message):
        """[v4.10.10] 마우스 선택 및 복사가 가능한 커스텀 에러 다이얼로그"""
        top = tk.Toplevel(self.app.root)
        top.title(title)
        top.geometry("600x400")
        top.configure(bg="#2E3440")
        top.transient(self.app.root)
        top.grab_set()
        
        # Heading
        header = tk.Frame(top, bg="#3B4252", height=40)
        header.pack(fill=tk.X)
        tk.Label(header, text=f"⚠️ {title}", bg="#3B4252", fg="#BF616A", font=("Segoe UI", 11, "bold")).pack(pady=8)
        
        # Guide
        tk.Label(top, text="아래 에러 내용을 마우스로 드래그하여 복사(Ctrl+C)할 수 있습니다.", 
                 bg="#2E3440", fg="#D8DEE9", font=("Segoe UI", 9)).pack(pady=(10, 5))
        
        # Text area
        container = tk.Frame(top, bg="#2E3440")
        container.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        
        txt = tk.Text(container, wrap=tk.WORD, bg="#1E222A", fg="#ECEFF4", 
                      font=("Consolas", 10), padx=15, pady=15, relief=tk.FLAT)
        txt.insert(tk.END, message)
        txt.config(state=tk.DISABLED) # Read-only but selectable
        txt.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        # Close button
        btn_close = tk.Button(top, text="닫기", command=top.destroy, bg="#4C566A", fg="white", 
                             font=("Segoe UI", 10, "bold"), width=15, relief=tk.FLAT, cursor="hand2")
        btn_close.pack(pady=15)

    def _poll_queue(self):
        """[v35.2] GUI 큐 모니터링 및 업데이트 실행 (Thread-safe)"""
        try:
            while not self.gui_queue.empty():
                task = self.gui_queue.get_nowait()
                if callable(task):
                    task()
        except queue.Empty:
            pass
        finally:
            self.parent.after(100, self._poll_queue)

    def _call_virtual_tab(self, method_name: str) -> None:
        """포지션 관리 탭에 구현된 동작을 그대로 호출합니다."""
        try:
            vt = getattr(self.app, "virtual_tab", None)
            if vt is None:
                messagebox.showerror(
                    "오류",
                    "포지션 관리 탭이 아직 초기화되지 않았습니다.",
                    parent=self.parent,
                )
                return
            fn = getattr(vt, method_name, None)
            if not callable(fn):
                messagebox.showerror("오류", f"기능을 찾을 수 없습니다: {method_name}")
                return
            fn()
        except Exception as e:
            try:
                messagebox.showerror("오류", str(e), parent=self.parent)
            except Exception:
                pass

    def _setup_ui(self):
        """전략검증 탭 레이아웃: 시세·리포트 → 기간·최적화·검증 → 진행·결과."""
        # --- 시세 하베스팅 + 가상 매매 리포트 (한 줄 도구 모음) ---
        tools_lf = ttk.LabelFrame(
            self.parent,
            text=" 시세 수집 · 리포트 생성 ",
            padding=(12, 8),
        )
        tools_lf.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(10, 4))
        tools_row = ttk.Frame(tools_lf)
        tools_row.pack(fill=tk.X)
        ttk.Label(
            tools_row,
            text="DB 하베스팅·포지션·주문 기반 HTML (개발 검증)",
            font=("Malgun Gothic", 8),
            foreground=self.app.colors.get("secondary", "#787c99"),
        ).pack(side=tk.LEFT, padx=(0, 10))
        btn_cluster = ttk.Frame(tools_row)
        btn_cluster.pack(side=tk.RIGHT)
        _rep_style = "Action.TButton"
        _rep_w = 14
        self.btn_narrative_report = ttk.Button(
            btn_cluster,
            text="종합리포트",
            style=_rep_style,
            width=_rep_w,
            command=lambda: self._call_virtual_tab("generate_report"),
        )
        self.btn_narrative_report.pack(side=tk.LEFT, padx=(0, 4))
        self.btn_daily_settlement = ttk.Button(
            btn_cluster,
            text="일일결산",
            style=_rep_style,
            width=_rep_w,
            command=lambda: self._call_virtual_tab("generate_daily_settlement"),
        )
        self.btn_daily_settlement.pack(side=tk.LEFT, padx=(0, 4))
        self.btn_single_stock_sim = ttk.Button(
            btn_cluster,
            text="종목시뮬",
            style=_rep_style,
            width=_rep_w,
            command=self._open_single_stock_sim_dialog,
        )
        self.btn_single_stock_sim.pack(side=tk.LEFT, padx=(0, 4))
        self.btn_harvest_prices = ttk.Button(
            btn_cluster,
            text="최신주가데이타수집",
            style=_rep_style,
            width=22,
            command=self._on_harvest_latest_prices,
        )
        self.btn_harvest_prices.pack(side=tk.LEFT, padx=(4, 0))

        # --- 워크플로: 기간 → 최적화 → 성과 검증 (3열 동일 비중) ---
        workflow = ttk.LabelFrame(
            self.parent,
            text=" 📋 기간 · 최적화 · 성과 검증 ",
            padding=(10, 8),
        )
        workflow.pack(side=tk.TOP, fill=tk.BOTH, expand=False, padx=10, pady=(2, 6))
        top_grid_frame = ttk.Frame(workflow)
        top_grid_frame.pack(fill=tk.BOTH, expand=True)
        for c in (0, 1, 2):
            top_grid_frame.columnconfigure(c, weight=1, uniform="bt_top_col")
        
        # 1. 기간 설정 (Date Settings)
        date_lf = ttk.LabelFrame(top_grid_frame, text=" 📅 1. 기본 기간 설정 ", padding="15")
        date_lf.grid(row=0, column=0, sticky="nsew", padx=(0, 4), pady=2)
        
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        
        ttk.Label(date_lf, text="시작:", foreground="#EBCB8B").grid(row=0, column=0, sticky=tk.W)
        self.backtest_entries['start_date'] = ttk.Entry(date_lf, width=12)
        self.backtest_entries['start_date'].insert(0, self.app.settings.get('bt_start_date', "2025-03-04"))
        self.backtest_entries['start_date'].grid(row=0, column=1, padx=(5, 2), pady=2)
        ttk.Button(date_lf, text="📅", width=3, 
                   command=lambda: self._show_picker('start_date')).grid(row=0, column=2, padx=(0, 5))

        ttk.Label(date_lf, text="종료:", foreground="#EBCB8B").grid(row=1, column=0, sticky=tk.W)
        self.backtest_entries['end_date'] = ttk.Entry(date_lf, width=12)
        self.backtest_entries['end_date'].insert(0, self.app.settings.get('bt_end_date', yesterday))
        self.backtest_entries['end_date'].grid(row=1, column=1, padx=(5, 2), pady=2)
        ttk.Button(date_lf, text="📅", width=3, 
                   command=lambda: self._show_picker('end_date')).grid(row=1, column=2, padx=(0, 5))

        # 2. 전략 최적화 (Optimization)
        opt_lf = ttk.LabelFrame(top_grid_frame, text=" 🚀 2. 전략 최적화 ", padding="15")
        opt_lf.grid(row=0, column=1, sticky="nsew", padx=4, pady=2)
        
        btn_row_opt = ttk.Frame(opt_lf)
        btn_row_opt.pack(expand=True)
        self.btn_start_optimize = ttk.Button(btn_row_opt, text="최적화 시작", width=15, command=self._on_btn_start_optimize)
        self.btn_start_optimize.pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_row_opt, text="📊 최적화결과 리포트", width=18, command=self._open_opt_trade_report).pack(side=tk.LEFT, padx=2)

        # [Perf] 최적화 옵션 행 (trials 수 + 고정밀 토글)
        opt_cfg_row = ttk.Frame(opt_lf)
        opt_cfg_row.pack(pady=(6, 0))
        ttk.Label(opt_cfg_row, text="Trials:", font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 2))
        self.opt_n_trials_var = tk.StringVar(value="50")
        ttk.Spinbox(opt_cfg_row, from_=10, to=300, increment=10, textvariable=self.opt_n_trials_var,
                    width=5, font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 10))
        self.opt_high_precision_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_cfg_row, text="고정밀(1m)", variable=self.opt_high_precision_var,
                        style="TCheckbutton").pack(side=tk.LEFT)

        # 최적화 대상: 동적백테 선정 스케줄(A/B) 합집합 (대표 10종 고정 폐기)
        self.lbl_opt_targets = ttk.Label(
            opt_lf,
            text="🔍 대상: 동적백테 선정 스케줄 종목 풀 (실행 시 수집)",
            font=("Segoe UI", 8),
            foreground="#EBCB8B",
            cursor="hand2",
        )
        self.lbl_opt_targets.pack(pady=(5, 0))
        from ui.gui_utils import ToolTip

        ToolTip(
            self.lbl_opt_targets,
            lambda e: (
                "성과 검증(DynamicBacktester)과 같은 prepare_dynamic_universe 스케줄에서\n"
                "기간 내 Track A/B 선정 종목의 합집합으로 Simple 최적화를 돌립니다.\n"
                "일별 슬롯·레짐 게이트까지 매 trial 재현하지는 않습니다(속도 타협)."
            ),
        )


        # 3. 성과 검증 (Portfolio Backtest)
        bt_lf = ttk.LabelFrame(top_grid_frame, text=" 📈 3. 성과 검증 ", padding="15")
        bt_lf.grid(row=0, column=2, sticky="nsew", padx=(4, 0), pady=2)
        
        btn_row_bt = ttk.Frame(bt_lf)
        btn_row_bt.pack(pady=5)
        self.btn_start_dynamic = ttk.Button(btn_row_bt, text="백테스트 시작", width=15, command=self._on_btn_start_dynamic_backtest)
        self.btn_start_dynamic.pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_row_bt, text="📊 백테스트결과 리포트", width=18, command=self._open_dynamic_trade_report).pack(side=tk.LEFT, padx=2)

        # 고정밀 / RT정합 / 고속: 다크 테마에서 ON·OFF 토글 버튼 사용
        parity_row = ttk.Frame(bt_lf)
        parity_row.pack(pady=(8, 0), fill=tk.X)
        try:
            _hp_default = bool(self.app.settings.get("bt_high_precision", True))
        except Exception:
            _hp_default = True
        try:
            _parity_default = bool(self.app.settings.get("bt_rt_parity_intrabar", False))
        except Exception:
            _parity_default = False
        try:
            _fast_default = bool(self.app.settings.get("bt_rt_parity_fast", True))
        except Exception:
            _fast_default = True
        self.bt_high_precision_var = tk.BooleanVar(value=_hp_default)
        self.bt_rt_parity_var = tk.BooleanVar(value=_parity_default)
        self.bt_rt_parity_fast_var = tk.BooleanVar(value=_fast_default)
        self.btn_high_precision = self._make_on_off_toggle(
            parity_row,
            "고정밀(1분)",
            self.bt_high_precision_var,
            on_change=self._sync_bt_precision_widget_state,
        )
        self.btn_high_precision.pack(side=tk.LEFT)
        self.btn_rt_parity = self._make_on_off_toggle(
            parity_row,
            "RT정합(전종목1분)",
            self.bt_rt_parity_var,
            on_change=self._sync_rt_parity_fast_widget_state,
        )
        self.btn_rt_parity.pack(side=tk.LEFT, padx=(10, 0))
        self.btn_rt_parity_fast = self._make_on_off_toggle(
            parity_row,
            "고속 하이브리드",
            self.bt_rt_parity_fast_var,
        )
        self.btn_rt_parity_fast.pack(side=tk.LEFT, padx=(10, 0))
        ttk.Label(
            parity_row,
            text="고정밀OFF=15분 · RT정합OFF=Signal후보만 close-now",
            font=("Segoe UI", 8),
            foreground="#88C0D0",
        ).pack(side=tk.LEFT, padx=(8, 0))
        self._sync_bt_precision_widget_state()

        # ---중단: 진행 상태 (Full-Width Notification Bar)---
        status_lf = ttk.LabelFrame(self.parent, text=" 🔔 실시간 진행 상태 ", padding="10")
        status_lf.pack(side=tk.TOP, fill=tk.X, padx=15, pady=5)
        self.lbl_bt_status = ttk.Label(status_lf, text="시스템 대기 중...", font=("Segoe UI", 10, "bold"), 
                                         foreground=self.app.colors['primary'], anchor=tk.CENTER)
        self.lbl_bt_status.pack(fill=tk.X, expand=True)



        # 하단: 결과 테이블 영역
        res_frame = ttk.LabelFrame(self.parent, text=" 💎 최적화 결과 랭킹 (수익률 순) ")
        res_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=10, pady=5)

        # [v18.0] 테이블 어셈블리 (중앙 정렬을 위한 컨테이너)
        self.table_assembly = ttk.Frame(res_frame)
        self.table_assembly.grid_columnconfigure(0, weight=1)
        self.table_assembly.grid_rowconfigure(0, weight=1)
        self.table_assembly.pack(fill=tk.BOTH, expand=True, pady=10) # [v33.9] Maximize Available Space
        
        # [v25.2] 선택 조합 백테스트 버튼 (누락된 버튼 복구)
        btn_apply_opt = ttk.Button(res_frame, text="🛠️ 선택한 조합으로 포트폴리오 백테스트 시작", 
                                  command=self._on_btn_backtest_with_selected)
        btn_apply_opt.pack(pady=5)

        # --- [v33.9] Unified Horizontal Scrolling Container ---
        self.canvas_bt = tk.Canvas(self.table_assembly, bd=0, highlightthickness=0, bg="#2E3440")
        h_scroll = ttk.Scrollbar(self.table_assembly, orient=tk.HORIZONTAL, command=self.canvas_bt.xview)
        
        self.canvas_bt.configure(xscrollcommand=h_scroll.set)
        
        # Inner Frame to hold everything
        self.scroll_frame_bt = tk.Frame(self.canvas_bt, bg="#2E3440")
        self.canvas_window = self.canvas_bt.create_window((0, 0), window=self.scroll_frame_bt, anchor="nw")
        
        # Structure the container with grid
        self.canvas_bt.grid(row=0, column=0, sticky="nsew")
        h_scroll.grid(row=1, column=0, sticky="ew")

        # Now create children with scroll_frame_bt as parent
        # [v16.15] 상단 카테고리 프레임 제거 (UI 정예화를 위해 삭제)
        pass

        # 대청소: 하드코딩된 트리뷰 셋업과 각종 헤더/툴팁 맵핑 완전히 삭제
        # 최소한의 기본 결과용 뼈대만 남기고, 모든 옵션 컬럼은 결과값 기반 렌더링에서 동적 생성할 예정입니다.
        cols = ["rank", "avg_ret", "avg_mdd", "win_rate", "trades"]
        self.tree_backtest = ttk.Treeview(self.scroll_frame_bt, columns=cols, show="headings", height=15)
        
        headers = {
            "rank": "순위", "avg_ret": "수익률(%)", "avg_mdd": "MDD", "win_rate": "승률(%)", "trades": "매매수"
        }
        for col in cols:
            self.tree_backtest.heading(col, text=headers.get(col, col))
            self.tree_backtest.column(col, width=65, anchor=tk.CENTER, stretch=False)
        
        self.tree_backtest.bind("<Double-1>", lambda e: self._on_btn_backtest_with_selected())
        
        v_scroll = ttk.Scrollbar(self.table_assembly, orient=tk.VERTICAL, command=self.tree_backtest.yview)
        v_scroll.grid(row=0, column=1, sticky="ns")
        self.tree_backtest.configure(yscrollcommand=v_scroll.set)
        
        self.tree_backtest.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        def _update_scroll_region(e):
            self.canvas_bt.configure(scrollregion=self.canvas_bt.bbox("all"))

        self.scroll_frame_bt.bind("<Configure>", _update_scroll_region)

        style = ttk.Style()
        style.configure("Treeview", rowheight=25)
        # 다크 테마 기반 행 색상: 홀수/짝수 배경 + 텍스트 대비 확보
        self.tree_backtest.tag_configure('odd',  background='#2E3440', foreground='#D8DEE9')
        self.tree_backtest.tag_configure('even', background='#3B4252', foreground='#D8DEE9')
        # 수익/손실 행은 foreground만 덮어씀 (배경은 odd/even 태그에서 결정)
        self.tree_backtest.tag_configure('plus',  foreground='#A3BE8C')
        self.tree_backtest.tag_configure('minus', foreground='#BF616A')
        
        # [v26.0] 지능형 툴팁 장착 (카테고리 정보 포함)
        from ui.gui_utils import ToolTip
        def get_header_tip(event):
            region = self.tree_backtest.identify_region(event.x, event.y)
            if region == "heading":
                col_id = self.tree_backtest.identify_column(event.x)
                try:
                    col_index = int(col_id.replace("#", "")) - 1
                    cols = self.tree_backtest["columns"]
                    col_name = cols[col_index]
                    
                    if col_name in self.PARAM_INFO:
                        alias, category, desc = self.PARAM_INFO[col_name]
                        space = self._resolve_search_space_for_active(self.app.settings)
                        # 비선택 키도 범위 미리보기용으로 폴백 포함
                        if col_name not in space:
                            space = {col_name: self._fallback_search_values(col_name, self.app.settings)}
                        range_info = f"\n🔍 최적화 범위: {space.get(col_name, '')}"
                        return f"📂 {category}\n✨ {desc}{range_info}\n(Key: {col_name})"
                    labels = getattr(self.app, "opt_param_labels", None) or {}
                    lab = labels.get(col_name) or col_name
                    vals = self._fallback_search_values(col_name, self.app.settings)
                    return f"✨ {lab}\n🔍 최적화 범위: {vals}\n(Key: {col_name})"
                except: return None
            return None
        ToolTip(self.tree_backtest, get_header_tip)

    def _show_harvest_started_toast(self) -> None:
        """
        하베스팅 시작 안내 — 2초 후 자동으로 닫힘.
        messagebox.showinfo 는 모달이라 탭 전환까지 막으므로 Toplevel 비모달 사용.
        """
        msg = (
            "데이터 하베스팅이 별도 창에서 시작되었습니다.\n"
            "이 프로그램(GUI)을 종료해도 수집은 계속됩니다."
        )
        try:
            root = getattr(self.app, "root", None)
            if root is None:
                messagebox.showinfo("알림", msg, parent=self.parent)
                return
            top = tk.Toplevel(root)
            top.title("알림")
            try:
                top.transient(root)
            except Exception:
                pass
            try:
                top.resizable(False, False)
            except Exception:
                pass
            bg, fg = "#2E3440", "#ECEFF4"
            try:
                bg = str(self.app.colors.get("bg", bg))
                fg = str(self.app.colors.get("fg", fg))
            except Exception:
                pass
            top.configure(bg=bg)
            frm = tk.Frame(top, bg=bg, padx=18, pady=14)
            frm.pack(fill=tk.BOTH, expand=True)
            tk.Label(
                frm,
                text=msg,
                bg=bg,
                fg=fg,
                font=("Malgun Gothic", 10),
                justify=tk.LEFT,
            ).pack()
            top.update_idletasks()
            w = int(top.winfo_reqwidth())
            h = int(top.winfo_reqheight())
            rx = int(root.winfo_rootx())
            ry = int(root.winfo_rooty())
            rw = int(root.winfo_width())
            rh = int(root.winfo_height())
            x = rx + max(0, (rw - w) // 2)
            y = ry + max(0, (rh - h) // 2)
            top.geometry(f"+{x}+{y}")
            try:
                top.after(2000, top.destroy)
            except Exception:
                pass
        except Exception:
            try:
                messagebox.showinfo("알림", msg, parent=self.parent)
            except Exception:
                pass

    def _on_harvest_latest_prices(self) -> None:
        """harvest_service.py 를 별도 콘솔에서 실행해 로컬 시세 DB를 갱신합니다."""
        try:
            import subprocess
            import sys

            from utils.server_mode_paths import project_root

            root = project_root()
            harvest_script = os.path.join(root, "harvest_service.py")
            if not os.path.isfile(harvest_script):
                raise FileNotFoundError(f"harvest_service.py 를 찾을 수 없습니다: {harvest_script}")

            creationflags = (
                int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
                if os.name == "nt"
                else 0
            )
            subprocess.Popen(
                [sys.executable, "-u", harvest_script],
                cwd=root,
                creationflags=creationflags,
            )
            try:
                self.app.log("🚀 [하베스팅] 전 종목 수집 프로세스가 별도 창에서 시작되었습니다.")
            except Exception:
                pass
            self._show_harvest_started_toast()
        except Exception as e:
            try:
                self.app.log(f"❌ [하베스팅] 실행 실패: {e}")
            except Exception:
                pass
            try:
                messagebox.showerror(
                    "오류",
                    f"수집 프로세스 실행 중 오류 발생: {e}",
                    parent=self.parent,
                )
            except Exception:
                pass

    def _get_dynamic_search_space(self, settings):
        """[v15.7] 현재 설정값을 기준으로 주변 탐색 범위를 동적으로 생성"""
        s = settings
        def range_for(val, step, count=3):
            if isinstance(val, int):
                return [int(val + (i - count//2) * step) for i in range(count)]
            else:
                return [round(float(val) + (i - count//2) * step, 2) for i in range(count)]

        return {
            's1_equity_pct': range_for(s.get('s1_equity_pct', 10.0), 5.0),
            's2_equity_pct': range_for(s.get('s2_equity_pct', 20.0), 10.0),
            'buy_rsi_min': range_for(s.get('buy_rsi_min', 40.0), 5.0),
            'buy_rsi_min_b': range_for(s.get('buy_rsi_min_b', 65.0), 5.0),
            'buy_rsi_max_b': range_for(s.get('buy_rsi_max_b', 65.0), 5.0),
            'buy_rsi_max_a': range_for(s.get('buy_rsi_max_a', 58.0), 5.0),
            'ma60_min': range_for(s.get('ma60_min', 100.0), 2.0),
            'ma60_max': range_for(s.get('ma60_max', 110.0), 2.0),
            'vol_surge': range_for(s.get('vol_surge', 1.5), 0.3),
            'vol_surge_b': range_for(s.get('vol_surge_b', 1.5), 0.3),
            'prev_vol_lookback_s1_a': range_for(int(float(s.get('prev_vol_lookback_s1_a', s.get('prev_vol_lookback', 10)))), 2),
            'prev_vol_lookback_s1_b': range_for(int(float(s.get('prev_vol_lookback_s1_b', s.get('prev_vol_lookback_b', 10)))), 2),
            'prev_vol_lookback_s2_a': range_for(int(float(s.get('prev_vol_lookback_s2_a', s.get('prev_vol_lookback', 10)))), 2),
            'prev_vol_lookback_s2_b': range_for(int(float(s.get('prev_vol_lookback_s2_b', s.get('prev_vol_lookback_b', 10)))), 2),
            's2_ma20_gap': range_for(s.get('s2_ma20_gap', 0.0), 0.5),
            's2_ma5_gap': range_for(s.get('s2_ma5_gap', -0.5), 0.25),
            's2_rsi_gap': range_for(s.get('s2_rsi_gap', 0.0), 0.5),
            's2_macd_gap': range_for(s.get('s2_macd_gap', 0.0), 0.5),
            's2_b_ma20_gap': range_for(s.get('s2_b_ma20_gap', 0.0), 0.5),
            's2_b_ma5_gap': range_for(s.get('s2_b_ma5_gap', -0.5), 0.25),
            's2_b_rsi_gap': range_for(s.get('s2_b_rsi_gap', 0.0), 0.5),
            's2_b_macd_gap': range_for(s.get('s2_b_macd_gap', 0.0), 0.5),
            's2_b_vol_ratio': range_for(s.get('s2_b_vol_ratio', 1.5), 0.2),
            's2_b_profit_limit': range_for(s.get('s2_b_profit_limit', 1.0), 0.5),
            's2_b_delay_hours': range_for(s.get('s2_b_delay_hours', 24.0), 6.0),
            's1_a_take_profit': range_for(s.get('s1_a_take_profit', 5.0), 2.0),
            's1_a_stop_loss': range_for(s.get('s1_a_stop_loss', 2.0), 1.0),
            's1_b_take_profit': range_for(s.get('s1_b_take_profit', 4.0), 2.0),
            's1_b_stop_loss': range_for(s.get('s1_b_stop_loss', 2.0), 1.0),
            's2_take_profit': range_for(s.get('s2_take_profit', 5.0), 2.0),
            's2_stop_loss': range_for(s.get('s2_stop_loss', 2.0), 0.5),
            's2_b_take_profit': range_for(s.get('s2_b_take_profit', 5.0), 2.0),
            's2_b_stop_loss': range_for(s.get('s2_b_stop_loss', 2.0), 0.5),
            's2_vol_ratio': range_for(s.get('s2_vol_ratio', 1.0), 0.2),
            's2_profit_limit': range_for(s.get('s2_profit_limit', 1.5), 0.5),
            's2_delay_hours': range_for(s.get('s2_delay_hours', 2.0), 1.0),
            'ma_convergence': range_for(float(s.get('ma_convergence', 1.2)), 0.5),
            'atr_sl_entry_grace_hours': range_for(float(s.get('atr_sl_entry_grace_hours', 0.0)), 6.0),
            'atr_sl_entry_grace_mult': range_for(float(s.get('atr_sl_entry_grace_mult', 0.0)), 0.5),
            'atr_sl_entry_grace_same_day': [True, False],
            # ON/OFF categorical
            'use_rsi_min': [True, False],
            'use_macd_signal': [True, False],
            'use_rsi_sig_filter': [True, False],
            'use_type_a': [True, False],
            'use_gc': [True, False],
            'use_rsi_signal': [True, False],
            'use_ma60_safe': [True, False],
            'use_vol_surge': [True, False],
            'use_type_b': [True, False],
            'use_type_b_vol': [True, False],
            'use_trend_aligned_b': [True, False],
            'use_macd_gc_b': [True, False],
            'use_s2_master_a': [True, False],
            'use_s2_ma20': [True, False],
            'use_s2_ma5': [True, False],
            'use_s2_rsi_sig': [True, False],
            'use_s2_macd_gc': [True, False],
            'use_s2_vol_surge': [True, False],
            'use_s2_profit': [True, False],
            'use_s2_delay': [True, False],
            'use_s2_master_b': [True, False],
            'use_s2_b_ma20': [True, False],
            'use_s2_b_ma5': [True, False],
            'use_s2_b_rsi_sig': [True, False],
            'use_s2_b_macd_gc': [True, False],
            'use_s2_b_vol_surge': [True, False],
            'use_s2_b_profit': [True, False],
            'use_s2_b_delay': [True, False],
            'use_gap_down_exit_a': [True, False],
            'use_gap_grace_a': [True, False],
            'use_atr_exit_a': [True, False],
            'use_s1_a_tp': [True, False],
            'use_s1_a_stop_loss': [True, False],
            'use_s1_a_flash_exit': [True, False],
            'use_atr_stop_loss_a': [True, False],
            'use_atr_trailing_stop_a': [True, False],
            'use_atr_trail_only_after_tp_s1_a': [True, False],
            'use_s2_tp': [True, False],
            'use_s2_stop_loss': [True, False],
            'use_s2_flash_exit': [True, False],
            'use_atr_stop_loss_s2': [True, False],
            'use_atr_trailing_stop_s2': [True, False],
            'use_atr_trail_only_after_tp_s2_a': [True, False],
            'use_gap_down_exit_b': [True, False],
            'use_gap_grace_b': [True, False],
            'use_atr_exit_b': [True, False],
            'use_s1_b_tp': [True, False],
            'use_s1_b_stop_loss': [True, False],
            'use_s1_b_flash_exit': [True, False],
            'use_atr_stop_loss_b': [True, False],
            'use_atr_trailing_stop_b': [True, False],
            'use_atr_trail_only_after_tp_s1_b': [True, False],
            'use_s2_b_tp': [True, False],
            'use_s2_b_stop_loss': [True, False],
            'use_s2_b_flash_exit': [True, False],
            'use_atr_stop_loss_s2_b': [True, False],
            'use_atr_trailing_stop_s2_b': [True, False],
            'use_atr_trail_only_after_tp_s2_b': [True, False],
            'gc_window': range_for(s.get('gc_window', 90), 10),
            'rsi_window': range_for(s.get('rsi_window', 45), 15),
            'atr_period_a': range_for(s.get('atr_period_a', s.get('atr_period', 14)), 4),
            'atr_period_b': range_for(s.get('atr_period_b', s.get('atr_period', 14)), 4),
            'atr_multiplier_sl_a': range_for(s.get('atr_multiplier_sl_a', 2.5), 0.5),
            'atr_multiplier_trail_a': range_for(s.get('atr_multiplier_trail_a', 3.0), 0.5),
            'atr_multiplier_sl_b': range_for(s.get('atr_multiplier_sl_b', 2.5), 0.5),
            'atr_multiplier_trail_b': range_for(s.get('atr_multiplier_trail_b', 3.0), 0.5),
            'atr_multiplier_sl_s2': range_for(s.get('atr_multiplier_sl_s2', 2.5), 0.5),
            'atr_multiplier_trail_s2': range_for(s.get('atr_multiplier_trail_s2', 3.0), 0.5),
            'atr_multiplier_sl_s2_b': range_for(s.get('atr_multiplier_sl_s2_b', 2.5), 0.5),
            'atr_multiplier_trail_s2_b': range_for(s.get('atr_multiplier_trail_s2_b', 3.0), 0.5),
            'gap_down_limit_a': range_for(s.get('gap_down_limit_a', s.get('gap_down_limit', -3.0)), 1.0),
            'gap_down_limit_b': range_for(s.get('gap_down_limit_b', s.get('gap_down_limit', -3.0)), 1.0),
            'gap_grace_mins_a': range_for(s.get('gap_grace_mins_a', s.get('gap_grace_mins', 15)), 5),
            'gap_grace_mins_b': range_for(s.get('gap_grace_mins_b', s.get('gap_grace_mins', 15)), 5),
            's1_a_flash_exit_pct': range_for(s.get('s1_a_flash_exit_pct', -5.0), 1.0),
            's1_b_flash_exit_pct': range_for(s.get('s1_b_flash_exit_pct', -5.0), 1.0),
            's2_flash_exit_pct': range_for(s.get('s2_flash_exit_pct', -5.0), 1.0),
            's2_b_flash_exit_pct': range_for(s.get('s2_b_flash_exit_pct', -5.0), 1.0),
            # 레거시 유니버스 키(opt_keys 미포함 — 툴팁·구버전 호환용 범위만 유지)
            'min_trde_val': range_for(s.get('min_trde_val', 10), 5),
            'vol_ratio': range_for(s.get('vol_ratio', 1.0), 0.5),
            'vol_lookback': range_for(s.get('vol_lookback', 20), 5),
            'sel_rsi_min': range_for(s.get('sel_rsi_min', 35.0), 5.0),
            'sel_rsi_max': range_for(s.get('sel_rsi_max', 70.0), 5.0),
            'sel_b_rsi_min': range_for(s.get('sel_b_rsi_min', 45.0), 5.0),
            'sel_b_rsi_max': range_for(s.get('sel_b_rsi_max', 75.0), 5.0),
            'sel_b_min_trde_val': range_for(s.get('sel_b_min_trde_val', 60.0), 10),
        }

    def _fallback_search_values(self, key: str, settings: dict) -> list:
        """명시 탐색공간이 없는 설정 키용 — 현재값 주변 또는 True/False."""
        def range_for(val, step, count=3):
            if isinstance(val, int):
                return [int(val + (i - count // 2) * step) for i in range(count)]
            return [round(float(val) + (i - count // 2) * step, 4) for i in range(count)]

        try:
            raw = settings.get(key, self.base_backtest_settings.get(key))
            name = str(key).lower()
            # bool / ON-OFF 계열
            if isinstance(raw, bool) or name.startswith("use_") or name.endswith("_enabled") or "_require_" in name:
                return [True, False]
            if raw is None:
                return [True, False]

            # 정수/실수
            try:
                if isinstance(raw, int) and not isinstance(raw, bool):
                    iv = int(raw)
                    step = max(1, abs(iv) // 10) if abs(iv) >= 10 else 1
                    return range_for(iv, step)
                fv = float(raw)
                if abs(fv) >= 100000:  # 초기자금 등
                    step = max(500000.0, abs(fv) * 0.1)
                    return range_for(fv, step)
                if abs(fv) >= 100:
                    return range_for(fv, max(1.0, abs(fv) * 0.1))
                if abs(fv) >= 10:
                    return range_for(fv, max(0.5, abs(fv) * 0.1))
                if abs(fv) >= 1:
                    return range_for(fv, 0.25)
                # 수수료·소수 비율
                step = 0.05 if abs(fv) < 1 else 0.1
                return range_for(fv, step)
            except (TypeError, ValueError):
                return [True, False]
        except Exception:
            return [True, False]

    def _resolve_search_space_for_active(self, settings: dict) -> dict:
        """● 지정된 모든 키에 대해 탐색공간 구성(명시 범위 + 폴백)."""
        full = self._get_dynamic_search_space(settings)
        out: dict = {}
        try:
            items = list(self.opt_params_vars.items())
        except Exception:
            items = []
        for k, var in items:
            try:
                if not var.get():
                    continue
            except Exception:
                continue
            if k in full:
                out[k] = full[k]
            else:
                out[k] = self._fallback_search_values(k, settings)
        return out

    def _param_header_alias(self, key: str) -> str:
        """결과 트리 헤더용 짧은 이름."""
        try:
            if key in self.PARAM_INFO:
                return str(self.PARAM_INFO[key][0])
        except Exception:
            pass
        try:
            labels = getattr(self.app, "opt_param_labels", None) or {}
            if key in labels and labels[key]:
                return str(labels[key])[:18]
        except Exception:
            pass
        return str(key)[:18]

    def _refresh_columns_from_settings(self, re_render=True):
        """[v26.1] 설정 탭의 파라미터 선택 상태를 즉시 트리뷰 컬럼에 반영 (선택된 항목만!)"""
        try:
            if not hasattr(self, 'tree_backtest'): return
            
            # 1. 기존 데이터 초기화 (기둥 구조 변경 시 데이터 정합성을 위해 일괄 삭제)
            for item in self.tree_backtest.get_children():
                self.tree_backtest.delete(item)
            
            # 2. 기본 결과 컬럼
            base_cols = ["rank", "avg_ret", "avg_mdd", "win_rate", "trades"]
            
            # 3. 선택된 최적화 대상 (PARAM_INFO 순서 우선, 이후 자동등록 키)
            active_params = []
            seen = set()
            for k in self.PARAM_INFO.keys():
                if k in self.opt_params_vars and self.opt_params_vars[k].get():
                    active_params.append(k)
                    seen.add(k)
            for k, var in self.opt_params_vars.items():
                try:
                    if k in seen:
                        continue
                    if var.get():
                        active_params.append(k)
                        seen.add(k)
                except Exception:
                    continue
            
            # 4. 트리뷰 컬럼 명단 갱신 (선택되지 않은 파라미터는 리스트에서 제외됨)
            new_cols = base_cols + active_params
            
            # [Core Fix] displaycolumns를 먼저 비워서 이전 기둥을 완전히 걷어냄
            self.tree_backtest["displaycolumns"] = [] 
            self.tree_backtest["columns"] = new_cols
            self.tree_backtest["displaycolumns"] = new_cols
            
            # 5. 헤더 및 너비 설정
            for col in new_cols:
                if col in base_cols:
                    header_text = {"rank": "순위", "avg_ret": "수익률(%)", "avg_mdd": "MDD", "win_rate": "승률(%)", "trades": "매매수"}[col]
                    width = 75
                else:
                    header_text = self._param_header_alias(col)
                    width = 110
                
                self.tree_backtest.heading(col, text=header_text)
                self.tree_backtest.column(col, width=width, anchor=tk.CENTER, stretch=False)

            # 6. 스크롤 영역 갱신 (테이블 너비 확장)
            if hasattr(self, 'canvas_bt'):
                self.canvas_bt.itemconfig(self.canvas_window, width=5000)
                self.parent.update_idletasks()
                req_w = self.scroll_frame_bt.winfo_reqwidth()
                can_w = self.canvas_bt.winfo_width()
                self.canvas_bt.itemconfig(self.canvas_window, width=max(can_w, req_w))
                self.canvas_bt.configure(scrollregion=self.canvas_bt.bbox("all"))

            # 7. 데이터 재랜더링 (탭 전환 시 기존 결과 유지)
            if re_render and self.last_opt_df is not None:
                self._render_optimization_results(self.last_opt_df, refresh_cols=False)

        except Exception as _err:
            print(f"[ERROR] Column Refresh Fail: {_err}")
            pass

    def _on_btn_start_optimize(self):
        """최적화 버튼 클릭 핸들러"""
        if self.app.is_monitoring:
            messagebox.showwarning("경고", "실시간 감시가 중단된 상태에서만 백테스팅이 가능합니다.")
            return
            
        self.btn_start_optimize.config(state=tk.DISABLED)
        self.lbl_bt_status.config(text="최적화 중... (수 분 소요될 수 있음)", foreground="#BF616A")
        
        try:
            raw_start = self.backtest_entries['start_date'].get().strip()
            raw_end = self.backtest_entries['end_date'].get().strip()
            start_date = pd.to_datetime(raw_start).strftime('%Y-%m-%d')
            end_date = pd.to_datetime(raw_end).strftime('%Y-%m-%d')
            
            self.app.settings['bt_start_date'] = start_date
            self.app.settings['bt_end_date'] = end_date
            self.app._final_save_settings()
            
            s = self.app.settings
            search_space = self._resolve_search_space_for_active(s)
            
            if not search_space:
                messagebox.showwarning("주의", "최소 하나 이상의 파라미터를 선택해야 합니다.")
                self.btn_start_optimize.config(state=tk.NORMAL)
                return
            
            # [Perf] 최적화는 기본적으로 15분봉만 사용 (고정밀 모드는 최종 백테스트에서만 활성화)
            high_precision = getattr(self, 'opt_high_precision_var', None)
            high_precision = high_precision.get() if high_precision else False
            n_trials = getattr(self, 'opt_n_trials_var', None)
            try:
                n_trials = int(n_trials.get()) if n_trials else 50
            except Exception:
                n_trials = 50
            threading.Thread(target=self._run_optimize_thread, args=(start_date, end_date, search_space, high_precision, n_trials), daemon=True).start()
        except Exception as _err:
            self._show_copyable_error("입력 오류", f"입력 형식을 확인해 주세요:\n{str(_err)}\n\n{traceback.format_exc()}")
            self.btn_start_optimize.config(state=tk.NORMAL)

    def _render_optimization_results(self, df, refresh_cols=True):
        """[v26.1] 최적화 결과 랭킹 렌더링 (결과 데이터 삽입)"""
        if df is None or df.empty: return
        
        # 0. 데이터 캐싱
        self.last_opt_df = df.head(20)

        # 1. 화면의 트리뷰 데이터 초기화 (구조는 유지)
        if refresh_cols:
            # 컬럼 구조 강제 갱신과 초기화 동시 수행
            self._refresh_columns_from_settings(re_render=False)
        else:
            for item in self.tree_backtest.get_children():
                self.tree_backtest.delete(item)

        # (삭제 항목: 기존의 중복된 _refresh_columns_from_settings 호출 제거)
        
        # 3. 현재 트리뷰에 설정된 컬럼 순서대로 데이터 추출 및 삽입
        current_cols = self.tree_backtest["columns"]
        
        for i, row in enumerate(df.head(20).itertuples(), 1):
            res_dict = row._asdict()
            values = []
            for col in current_cols:
                val = res_dict.get(col, 0)
                if col == "rank": val = i
                elif col == "trades": val = int(val)
                elif isinstance(val, (float, np.float64, np.float32)): val = f"{val:+.2f}"
                values.append(val)
            
            row_tag = 'even' if i % 2 == 0 else 'odd'
            ret_val = float(res_dict.get('avg_ret', 0))
            perf_tag = 'plus' if ret_val > 0 else 'minus'
            
            self.tree_backtest.insert("", tk.END, values=values, tags=(perf_tag, row_tag))

        # 6. 지오메트리 잠금 해제 (가로 스크롤 영역 갱신)
        # 캔버스 너비를 임시로 크게 벌려 내부 프레임이 제약 없이 펴지게 유도
        self.canvas_bt.itemconfig(self.canvas_window, width=5000)
        self.parent.update_idletasks()
        
        req_w = self.scroll_frame_bt.winfo_reqwidth()
        can_w = self.canvas_bt.winfo_width()
        # 최종적으로 실제 필요한 너비와 캔버스 창 너비 중 큰 값으로 고정
        self.canvas_bt.itemconfig(self.canvas_window, width=max(can_w, req_w))
        self.canvas_bt.configure(scrollregion=self.canvas_bt.bbox("all"))

    def _run_optimize_thread(self, start, end, space, high_precision=False, n_trials=50):
        try:
            import parameter_optimizer as opt_mod
            import strategy_selector

            fallback = [
                "005930",
                "000660",
                "005380",
                "055550",
                "051910",
                "012450",
                "035420",
                "015760",
                "028260",
                "009150",
            ]

            def _opt_log(msg: object, *_a: object, **_k: object) -> None:
                try:
                    text = str(msg)
                except Exception:
                    return
                try:
                    self.gui_queue.put(
                        lambda t=text: self.lbl_bt_status.config(text=t)
                    )
                except Exception:
                    pass
                try:
                    print(text, flush=True)
                except Exception:
                    pass

            dm = getattr(self.app, "data_manager", None)
            opt_codes: list[str] = []
            try:
                if dm is not None:
                    opt_codes = strategy_selector.collect_dynamic_schedule_codes(
                        self.app.settings,
                        start,
                        end,
                        dm,
                        logger_callback=_opt_log,
                    )
            except Exception as e:
                _opt_log(f"[Opt] 선정 스케줄 수집 예외 → 대표주 폴백: {e}")
                opt_codes = []

            if not opt_codes:
                from backtest_engine import TradingStrategy

                idx_choice = self.app.settings.get("universe_index_choice", "KOSPI200")
                name_map = TradingStrategy.get_stock_name_map(idx_choice)
                opt_codes = (
                    list(name_map.keys())[:10] if name_map else list(fallback)
                )
                _opt_log(
                    f"[Opt] 선정 스케줄 비어 있음 → 폴백 {len(opt_codes)}종목"
                )
            else:
                _opt_log(
                    f"[Opt] 선정 스케줄 종목 풀 {len(opt_codes)}종목으로 최적화"
                )

            try:
                preview = ", ".join(opt_codes[:12])
                if len(opt_codes) > 12:
                    preview += f" …(+{len(opt_codes) - 12})"
                self.gui_queue.put(
                    lambda n=len(opt_codes), p=preview: self.lbl_opt_targets.config(
                        text=f"🔍 대상: 선정 스케줄 {n}종목 ({p})"
                    )
                )
            except Exception:
                pass

            optimizer = opt_mod.ParameterOptimizer(opt_codes, space, self.app.settings)

            def update_progress(msg, value):
                self.gui_queue.put(lambda: self.lbl_bt_status.config(text=msg))
                if hasattr(self.app, "progress_var"):
                    self.gui_queue.put(lambda: self.app.progress_var.set(value))

            self.gui_queue.put(
                lambda: self.lbl_bt_status.config(
                    text="최적화 시작...", foreground="orange"
                )
            )
            df_results = optimizer.find_best_params(
                start,
                end,
                interval="15m",
                callback=update_progress,
                high_precision=high_precision,
                method="bayesian",
                n_trials=n_trials,
            )

            if df_results is not None and not df_results.empty:
                self.gui_queue.put(lambda: self._render_optimization_results(df_results))
            else:
                self.gui_queue.put(
                    lambda: messagebox.showwarning(
                        "결과 없음", "매매 신호가 발생하지 않았습니다."
                    )
                )
        except Exception as _err:
            # [v4.10.5] Absolute isolation of error message
            _msg = str(_err)
            self.gui_queue.put(
                lambda error_msg=_msg: messagebox.showerror("최적화 오류", error_msg)
            )
        finally:
            self.gui_queue.put(lambda: self.btn_start_optimize.config(state=tk.NORMAL))
            self.gui_queue.put(
                lambda: self.lbl_bt_status.config(
                    text="최적화 완료!", foreground="#A3BE8C"
                )
            )

    def _open_signal_report(self):
        path = os.path.abspath("signal_report.html")
        if os.path.exists(path): webbrowser.open(f"file://{path}")

    def _open_opt_trade_report(self):
        path = os.path.abspath("trade_report.html")
        if os.path.exists(path): webbrowser.open(f"file://{path}")

    def _open_dynamic_trade_report(self):
        path = os.path.abspath("dynamic_trade_report.html")
        if os.path.exists(path): webbrowser.open(f"file://{path}")

    def _on_btn_backtest_with_selected(self):
        selected = self.tree_backtest.selection()
        if not selected:
            messagebox.showwarning("알림", "백테스트에 적용할 조합을 선택해 주세요.")
            return
        item = self.tree_backtest.item(selected[0])
        rank = item['values'][0]
        if self.last_opt_df is not None:
            try:
                params = self.last_opt_df.iloc[int(rank)-1].to_dict()
                self._show_param_edit_popup(params)
            except Exception as _err:
                messagebox.showerror("오류", str(_err))

    def _show_param_edit_popup(self, params):
        popup = tk.Toplevel(self.app.root)
        popup.title("⚙️ 백테스트 반영 파라미터 확인")
        w, h = 580, 850
        self.app.root.update_idletasks()
        pos_x = self.app.root.winfo_x() + (self.app.root.winfo_width() // 2) - (w // 2)
        pos_y = self.app.root.winfo_y() + (self.app.root.winfo_height() // 2) - (h // 2)
        popup.geometry(f"{w}x{h}+{pos_x}+{pos_y}")
        popup.configure(bg="#2E3440")
        popup.transient(self.app.root)
        popup.grab_set()

        header_frame = tk.Frame(popup, bg="#3B4252", height=50)
        header_frame.pack(fill=tk.X)
        info_text = f"🏆 Rank #{params.get('rank', '-')} | Ret: {params.get('avg_ret', 0):+.2f}% | MDD: {params.get('avg_mdd', 0):.2f}%"
        ttk.Label(header_frame, text=info_text, font=("Malgun Gothic", 10, "bold"), foreground="#81A1C1", background="#3B4252").pack(pady=10)

        entries = {}
        footer = tk.Frame(popup, bg="#2E3440", pady=15)
        footer.pack(side=tk.BOTTOM, fill=tk.X)
        
        def run_confirmed():
            new_params = {}
            for k, _ent in entries.items():
                if isinstance(_ent, tk.BooleanVar): new_params[k] = _ent.get()
                else: 
                    try: 
                        _val_str = _ent.get()
                        new_params[k] = float(_val_str) if '.' in _val_str else int(_val_str)
                    except: pass
            popup.destroy()
            self._on_btn_start_dynamic_backtest(override_params=new_params)

        tk.Button(footer, text="🚀 이 설정으로 백테스트 시작", width=40, height=2, bg="#A3BE8C", fg="#2E3440", font=("Malgun Gothic", 10, "bold"), relief=tk.FLAT, command=run_confirmed).pack()

        container = tk.Frame(popup, bg="#2E3440")
        container.pack(fill=tk.BOTH, expand=True)
        canvas = tk.Canvas(container, bg="#2E3440", highlightthickness=0)
        scroll = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        main = tk.Frame(canvas, bg="#2E3440", padx=15, pady=10)
        main.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0,0), window=main, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        for k, v in params.items():
            if k in ['rank', 'avg_ret', 'avg_mdd', 'avg_win', 'total_trades']: continue
            row = tk.Frame(main, bg="#2E3440", pady=2)
            row.pack(fill=tk.X)
            tk.Label(row, text=k, bg="#2E3440", fg="#E5E9F0", width=20, anchor="w").pack(side=tk.LEFT)
            ent = tk.Entry(row, width=10, bg="#3B4252", fg="#ECEFF4", relief=tk.FLAT)
            ent.insert(0, str(v))
            ent.pack(side=tk.LEFT, padx=5)
            entries[k] = ent

    def _make_on_off_toggle(
        self,
        parent: tk.Misc,
        label: str,
        var: tk.BooleanVar,
        on_change=None,
    ) -> tk.Button:
        """
        다크 테마에서 ON/OFF가 한눈에 보이도록 색·문구가 바뀌는 토글 버튼.
        ON=초록(#A3BE8C), OFF=회색(#4C566A).
        """
        btn = tk.Button(
            parent,
            relief=tk.FLAT,
            padx=10,
            pady=3,
            cursor="hand2",
            font=("Segoe UI", 9, "bold"),
            bd=0,
            highlightthickness=0,
        )

        def _paint(*_args: object) -> None:
            try:
                on = bool(var.get())
            except Exception:
                on = False
            try:
                if str(btn.cget("state")) == "disabled" and not on:
                    # disabled+OFF 는 sync에서 별도 페인팅
                    pass
                if on:
                    btn.config(
                        text=f"{label}  · ON",
                        bg="#A3BE8C",
                        fg="#2E3440",
                        activebackground="#B4CF9E",
                        activeforeground="#2E3440",
                        disabledforeground="#2E3440",
                    )
                else:
                    btn.config(
                        text=f"{label}  · OFF",
                        bg="#4C566A",
                        fg="#ECEFF4",
                        activebackground="#5E81AC",
                        activeforeground="#ECEFF4",
                        disabledforeground="#D8DEE9",
                    )
            except Exception:
                pass

        def _toggle() -> None:
            try:
                if str(btn.cget("state")) == "disabled":
                    return
            except Exception:
                pass
            try:
                var.set(not bool(var.get()))
            except Exception:
                return
            _paint()
            if on_change is not None:
                try:
                    on_change()
                except Exception:
                    pass

        btn.configure(command=_toggle)
        try:
            var.trace_add("write", lambda *_a: _paint())
        except Exception:
            pass
        _paint()
        # sync 등 외부에서 다시 칠할 수 있게 보관
        setattr(btn, "_paint_on_off", _paint)
        setattr(btn, "_on_off_label", label)
        return btn

    def _dim_on_off_toggle(self, btn: Optional[tk.Widget], fallback_label: str) -> None:
        """ON/OFF 토글을 비활성·흐리게 표시."""
        if btn is None:
            return
        try:
            label = str(getattr(btn, "_on_off_label", fallback_label))
            btn.config(
                state=tk.DISABLED,
                text=f"{label}  · —",
                bg="#3B4252",
                fg="#6B7280",
                disabledforeground="#6B7280",
            )
        except Exception:
            pass

    def _sync_bt_precision_widget_state(self) -> None:
        """고정밀 OFF면 RT정합·고속 토글 비활성(15분 전용 저정밀)."""
        try:
            hp_on = bool(self.bt_high_precision_var.get())
        except Exception:
            hp_on = True
        btn_parity = getattr(self, "btn_rt_parity", None)
        if not hp_on:
            try:
                self.bt_rt_parity_var.set(False)
            except Exception:
                pass
            self._dim_on_off_toggle(btn_parity, "RT정합(전종목1분)")
            self._sync_rt_parity_fast_widget_state()
            return
        if btn_parity is not None:
            try:
                btn_parity.config(state=tk.NORMAL)
                paint = getattr(btn_parity, "_paint_on_off", None)
                if callable(paint):
                    paint()
            except Exception:
                pass
        self._sync_rt_parity_fast_widget_state()

    def _sync_rt_parity_fast_widget_state(self) -> None:
        """RT정합 OFF(또는 고정밀 OFF)면 고속 토글을 비활성화하고 흐리게 표시."""
        try:
            hp_on = bool(self.bt_high_precision_var.get())
        except Exception:
            hp_on = True
        try:
            on = bool(self.bt_rt_parity_var.get()) and hp_on
        except Exception:
            on = False
        btn = getattr(self, "btn_rt_parity_fast", None)
        if btn is None:
            return
        try:
            if on:
                btn.config(state=tk.NORMAL)
                paint = getattr(btn, "_paint_on_off", None)
                if callable(paint):
                    paint()
            else:
                self._dim_on_off_toggle(btn, "고속 하이브리드")
        except Exception:
            pass

    def _on_btn_start_dynamic_backtest(self, override_params=None):
        if self.app.is_monitoring:
            messagebox.showwarning("경고", "모니터링 중에는 백테스트가 불가합니다.")
            return
        try:
            raw_start = self.backtest_entries['start_date'].get().strip()
            raw_end = self.backtest_entries['end_date'].get().strip()
            start = pd.to_datetime(raw_start).strftime('%Y-%m-%d')
            end = pd.to_datetime(raw_end).strftime('%Y-%m-%d')
            
            # [v4.10.8] Persist dates for future sessions
            self.app.settings['bt_start_date'] = start
            self.app.settings['bt_end_date'] = end
            try:
                self.app.settings['bt_high_precision'] = bool(
                    self.bt_high_precision_var.get()
                )
            except Exception:
                self.app.settings['bt_high_precision'] = True
            try:
                self.app.settings['bt_rt_parity_intrabar'] = bool(
                    self.bt_rt_parity_var.get()
                )
            except Exception:
                self.app.settings['bt_rt_parity_intrabar'] = False
            # 저정밀(15분)에서는 RT정합 무의미 → 강제 OFF
            if not self.app.settings.get('bt_high_precision', True):
                self.app.settings['bt_rt_parity_intrabar'] = False
            try:
                self.app.settings['bt_rt_parity_fast'] = bool(
                    self.bt_rt_parity_fast_var.get()
                )
            except Exception:
                self.app.settings['bt_rt_parity_fast'] = True
            self.app._final_save_settings()
            
            self.btn_start_dynamic.config(state=tk.DISABLED)
            if not self.app.settings.get("bt_high_precision", True):
                mode_lbl = "저정밀(15분)"
            elif self.app.settings.get("bt_rt_parity_intrabar"):
                mode_lbl = (
                    "RT정합고속"
                    if self.app.settings.get("bt_rt_parity_fast", True)
                    else "RT정합"
                )
            else:
                mode_lbl = "고정밀"
            self.lbl_bt_status.config(
                text=f"백테스트 실행 중... ({mode_lbl})", foreground="orange"
            )
            threading.Thread(target=self._run_dynamic_backtest_thread, args=(start, end, override_params), daemon=True).start()
        except Exception as _err: 
            self._show_copyable_error("오류", f"날짜 형식을 확인하세요:\n{str(_err)}\n\n{traceback.format_exc()}")

    def _run_dynamic_backtest_thread(self, start, end, override_params=None):
        try:
            import dynamic_backtest as db
            def update(msg, val=None):
                self.gui_queue.put(lambda text=msg: self.lbl_bt_status.config(text=text))
            active = self.app.settings.copy()
            if override_params: active.update(override_params)
            try:
                use_hp = bool(active.get("bt_high_precision", True))
            except Exception:
                use_hp = True
            tester = db.DynamicBacktester(active)
            trades = tester.run(start, end, interval='15m', callback=update, high_precision=use_hp)
            try:
                elapsed = float(getattr(tester, "last_run_elapsed_sec", 0.0) or 0.0)
            except Exception:
                elapsed = 0.0
            try:
                hp_on = bool(getattr(tester, "high_precision", use_hp))
            except Exception:
                hp_on = use_hp
            try:
                rt_parity = bool(getattr(tester, "rt_parity_intrabar", False))
            except Exception:
                rt_parity = bool(active.get("bt_rt_parity_intrabar", False))
            try:
                rt_fast = bool(getattr(tester, "rt_parity_fast", False))
            except Exception:
                rt_fast = bool(active.get("bt_rt_parity_fast", True)) and rt_parity
            if not hp_on:
                mode_lbl = "저정밀"
            elif rt_parity:
                mode_lbl = "RT정합고속" if rt_fast else "RT정합"
            else:
                mode_lbl = "고정밀"
            if trades is not None and not trades.empty:
                n_trades = len(trades)
                self.gui_queue.put(
                    lambda n=n_trades, m=mode_lbl, e=elapsed: self.lbl_bt_status.config(
                        text=f"백테스트 완료! ({m} 매매 {n}건 / {e:.1f}초)",
                        foreground="#A3BE8C",
                    )
                )
                report = os.path.abspath("dynamic_trade_report.html")
                if os.path.exists(report):
                    self.gui_queue.put(lambda r=report: webbrowser.open(f"file://{r}"))
            elif trades is not None:
                # 매매 0건이어도 시뮬레이션은 정상 종료 — 미처리 시 상태가 '시계열 모의투자 중'에 멈춘 것처럼 보임
                period = f"{start} ~ {end}"
                self.gui_queue.put(
                    lambda m=mode_lbl, e=elapsed: self.lbl_bt_status.config(
                        text=f"백테스트 완료 ({m} 매매 0건 / {e:.1f}초)",
                        foreground="#EBCB8B",
                    )
                )
                self.gui_queue.put(
                    lambda p=period: messagebox.showinfo(
                        "매매 없음",
                        f"{p} 기간에 체결된 매매가 없습니다.\n"
                        "감시 대상 종목·매수 신호·데이터 유무를 확인하세요.",
                    )
                )
            else:
                self.gui_queue.put(
                    lambda: self.lbl_bt_status.config(
                        text="백테스트 실패", foreground="#BF616A"
                    )
                )
                self.gui_queue.put(
                    lambda: messagebox.showerror(
                        "오류", "백테스트가 중단되었습니다. 로그를 확인하세요."
                    )
                )
        except Exception as _err:
            # [v4.10.5] Absolute closure isolation
            _msg_str = str(_err)
            self.gui_queue.put(lambda error_msg=_msg_str: messagebox.showerror("오류", error_msg))
        finally:
            self.gui_queue.put(lambda: self.btn_start_dynamic.config(state=tk.NORMAL))

    def _show_picker(self, key):
        from ui.gui_utils import CustomDatePicker
        entry = self.backtest_entries.get(key)
        if entry: CustomDatePicker(self.parent, entry, initial_date=entry.get(), colors=self.app.colors)

    def _ss_widget_alive(self, widget: Optional[tk.Misc]) -> bool:
        """다이얼로그 위젯이 아직 살아 있는지 확인한다."""
        try:
            return widget is not None and bool(widget.winfo_exists())
        except Exception:
            return False

    def _ss_on_strategy_change(self) -> None:
        """전략 선택에 따라 K 입력·안내 문구를 전환한다."""
        try:
            strat = str(self._ss_strategy_var.get() or "s1_s2") if self._ss_strategy_var is not None else "s1_s2"
        except Exception:
            strat = "s1_s2"
        is_vb = strat == "vol_breakout"
        try:
            if self._ss_widget_alive(self._ss_vol_k_entry):
                state = tk.NORMAL if is_vb else tk.DISABLED
                self._ss_vol_k_entry.config(state=state)
        except Exception:
            pass
        try:
            if self._ss_widget_alive(self._ss_header_lbl):
                self._ss_header_lbl.config(
                    text="단일 종목 변동성 돌파 재현" if is_vb else "단일 종목 S1 / S2 / 청산 재현"
                )
        except Exception:
            pass
        try:
            if self._ss_widget_alive(self._ss_sub_lbl):
                self._ss_sub_lbl.config(
                    text=(
                        "당일 시가 + 전일 고저폭×K 를 넘으면 매수하고 당일 종가에 청산합니다. 자금 1,000만원."
                        if is_vb
                        else "유니버스·슬롯 없이 해당 종목만 재현합니다. 자금은 1,000만원 · S1 44% · S2 54% 고정."
                    )
                )
        except Exception:
            pass
        try:
            if self._ss_widget_alive(self._ss_vol_k_hint):
                self._ss_vol_k_hint.config(
                    fg="#88C0D0" if is_vb else "#787c99"
                )
        except Exception:
            pass

    def _ss_set_status(self, text: str, color: str = "#88C0D0") -> None:
        """종목시뮬 다이얼로그·전략검증 상태바에 진행 문구를 반영한다."""
        try:
            if self._ss_widget_alive(self._ss_status_lbl):
                self._ss_status_lbl.config(text=text, fg=color)
        except Exception:
            pass
        try:
            self.lbl_bt_status.config(text=text, foreground=color)
        except Exception:
            pass

    def _ss_default_dates(self) -> tuple[str, str]:
        """탭 기간 입력을 기본값으로 쓴다."""
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        start = "2025-03-04"
        end = yesterday
        try:
            ent_s = self.backtest_entries.get("start_date")
            if ent_s is not None:
                start = str(ent_s.get() or "").strip() or start
        except Exception:
            try:
                start = str(self.app.settings.get("bt_start_date", start) or start)
            except Exception:
                pass
        try:
            ent_e = self.backtest_entries.get("end_date")
            if ent_e is not None:
                end = str(ent_e.get() or "").strip() or end
        except Exception:
            try:
                end = str(self.app.settings.get("bt_end_date", end) or end)
            except Exception:
                pass
        return start, end

    def _ss_show_date_picker(self, entry: tk.Entry) -> None:
        """다이얼로그 날짜 엔트리용 달력을 연다."""
        try:
            from ui.gui_utils import CustomDatePicker
            parent = self._ss_dlg if self._ss_widget_alive(self._ss_dlg) else self.parent
            CustomDatePicker(parent, entry, initial_date=entry.get(), colors=self.app.colors)
        except Exception as e:
            try:
                messagebox.showerror("오류", f"날짜 선택기를 열 수 없습니다:\n{e}", parent=self.parent)
            except Exception:
                pass

    def _open_single_stock_report(self) -> None:
        """마지막 단일 종목 시뮬 리포트를 브라우저에서 연다."""
        try:
            from single_stock_sim import DEFAULT_REPORT_PATH
            path = os.path.abspath(DEFAULT_REPORT_PATH)
        except Exception:
            path = os.path.abspath("single_stock_trade_report.html")
        try:
            if os.path.exists(path):
                webbrowser.open(f"file://{path}")
            else:
                parent = self._ss_dlg if self._ss_widget_alive(self._ss_dlg) else self.parent
                messagebox.showinfo("알림", "아직 생성된 종목시뮬 리포트가 없습니다.", parent=parent)
        except Exception as e:
            try:
                messagebox.showerror("오류", str(e), parent=self.parent)
            except Exception:
                pass

    def _open_single_stock_sim_dialog(self) -> None:
        """단일 종목 S1/S2/청산 시뮬 다이얼로그를 연다."""
        try:
            if self._ss_widget_alive(self._ss_dlg):
                try:
                    self._ss_dlg.lift()
                    self._ss_dlg.focus_force()
                except Exception:
                    pass
                return

            colors = getattr(self.app, "colors", None) or {}
            bg = colors.get("bg", "#2E3440")
            card = colors.get("card", "#3B4252")
            fg = colors.get("fg", "#ECEFF4")
            primary = colors.get("primary", "#88C0D0")
            muted = colors.get("secondary", "#787c99")

            dlg = tk.Toplevel(self.app.root)
            self._ss_dlg = dlg
            dlg.title("종목 시뮬")
            dlg.configure(bg=bg)
            dlg.resizable(False, False)
            try:
                dlg.transient(self.app.root)
            except Exception:
                pass

            w, h = 500, 430
            try:
                self.app.root.update_idletasks()
                pos_x = self.app.root.winfo_x() + (self.app.root.winfo_width() // 2) - (w // 2)
                pos_y = self.app.root.winfo_y() + (self.app.root.winfo_height() // 2) - (h // 2)
                dlg.geometry(f"{w}x{h}+{pos_x}+{pos_y}")
            except Exception:
                dlg.geometry(f"{w}x{h}")

            header = tk.Frame(dlg, bg=card)
            header.pack(fill=tk.X)
            self._ss_header_lbl = tk.Label(
                header,
                text="단일 종목 S1 / S2 / 청산 재현",
                font=("Malgun Gothic", 11, "bold"),
                bg=card,
                fg=primary,
            )
            self._ss_header_lbl.pack(anchor=tk.W, padx=16, pady=(12, 2))
            self._ss_sub_lbl = tk.Label(
                header,
                text="유니버스·슬롯 없이 해당 종목만 재현합니다. 자금은 1,000만원 · S1 44% · S2 54% 고정.",
                font=("Malgun Gothic", 8),
                bg=card,
                fg=muted,
            )
            self._ss_sub_lbl.pack(anchor=tk.W, padx=16, pady=(0, 12))

            body = tk.Frame(dlg, bg=bg, padx=16, pady=12)
            body.pack(fill=tk.BOTH, expand=True)
            body.columnconfigure(1, weight=1)

            tk.Label(body, text="종목코드", bg=bg, fg=fg, font=("Malgun Gothic", 9)).grid(
                row=0, column=0, sticky=tk.W, pady=4
            )
            self._ss_code_entry = tk.Entry(
                body, width=14, bg=card, fg=fg, insertbackground=fg, relief=tk.FLAT,
                font=("Consolas", 11),
            )
            self._ss_code_entry.grid(row=0, column=1, sticky=tk.W, padx=(10, 0), pady=4)
            try:
                self._ss_code_entry.focus_set()
            except Exception:
                pass

            start_default, end_default = self._ss_default_dates()

            tk.Label(body, text="시작일", bg=bg, fg=fg, font=("Malgun Gothic", 9)).grid(
                row=1, column=0, sticky=tk.W, pady=4
            )
            start_row = tk.Frame(body, bg=bg)
            start_row.grid(row=1, column=1, sticky=tk.W, padx=(10, 0), pady=4)
            self._ss_start_entry = tk.Entry(
                start_row, width=12, bg=card, fg=fg, insertbackground=fg, relief=tk.FLAT,
                font=("Consolas", 10),
            )
            self._ss_start_entry.insert(0, start_default)
            self._ss_start_entry.pack(side=tk.LEFT)
            tk.Button(
                start_row, text="📅", width=3, relief=tk.FLAT, bg=card, fg=fg,
                command=lambda: self._ss_show_date_picker(self._ss_start_entry),
            ).pack(side=tk.LEFT, padx=(4, 0))

            tk.Label(body, text="종료일", bg=bg, fg=fg, font=("Malgun Gothic", 9)).grid(
                row=2, column=0, sticky=tk.W, pady=4
            )
            end_row = tk.Frame(body, bg=bg)
            end_row.grid(row=2, column=1, sticky=tk.W, padx=(10, 0), pady=4)
            self._ss_end_entry = tk.Entry(
                end_row, width=12, bg=card, fg=fg, insertbackground=fg, relief=tk.FLAT,
                font=("Consolas", 10),
            )
            self._ss_end_entry.insert(0, end_default)
            self._ss_end_entry.pack(side=tk.LEFT)
            tk.Button(
                end_row, text="📅", width=3, relief=tk.FLAT, bg=card, fg=fg,
                command=lambda: self._ss_show_date_picker(self._ss_end_entry),
            ).pack(side=tk.LEFT, padx=(4, 0))

            try:
                hp_default = bool(self.bt_high_precision_var.get())
            except Exception:
                try:
                    hp_default = bool(self.app.settings.get("bt_high_precision", True))
                except Exception:
                    hp_default = True
            tk.Label(body, text="전략", bg=bg, fg=fg, font=("Malgun Gothic", 9)).grid(
                row=3, column=0, sticky=tk.W, pady=4
            )
            strat_row = tk.Frame(body, bg=bg)
            strat_row.grid(row=3, column=1, sticky=tk.W, padx=(10, 0), pady=4)
            self._ss_strategy_var = tk.StringVar(value="s1_s2")
            tk.Radiobutton(
                strat_row,
                text="기존 (Type A/B)",
                variable=self._ss_strategy_var,
                value="s1_s2",
                command=self._ss_on_strategy_change,
                bg=bg,
                fg=fg,
                selectcolor=card,
                activebackground=bg,
                activeforeground=fg,
                font=("Malgun Gothic", 9),
            ).pack(side=tk.LEFT)
            tk.Radiobutton(
                strat_row,
                text="변동성 돌파",
                variable=self._ss_strategy_var,
                value="vol_breakout",
                command=self._ss_on_strategy_change,
                bg=bg,
                fg=fg,
                selectcolor=card,
                activebackground=bg,
                activeforeground=fg,
                font=("Malgun Gothic", 9),
            ).pack(side=tk.LEFT, padx=(12, 0))

            tk.Label(body, text="K 값", bg=bg, fg=fg, font=("Malgun Gothic", 9)).grid(
                row=4, column=0, sticky=tk.W, pady=4
            )
            k_row = tk.Frame(body, bg=bg)
            k_row.grid(row=4, column=1, sticky=tk.W, padx=(10, 0), pady=4)
            self._ss_vol_k_entry = tk.Entry(
                k_row, width=8, bg=card, fg=fg, insertbackground=fg, relief=tk.FLAT,
                font=("Consolas", 10),
            )
            self._ss_vol_k_entry.insert(0, "0.5")
            self._ss_vol_k_entry.pack(side=tk.LEFT)
            self._ss_vol_k_hint = tk.Label(
                k_row,
                text="목표가 = 시가 + 전일레인지×K",
                bg=bg,
                fg=muted,
                font=("Malgun Gothic", 8),
            )
            self._ss_vol_k_hint.pack(side=tk.LEFT, padx=(8, 0))

            self._ss_hp_var = tk.BooleanVar(value=hp_default)
            tk.Checkbutton(
                body,
                text="고정밀(1분)",
                variable=self._ss_hp_var,
                bg=bg,
                fg=fg,
                selectcolor=card,
                activebackground=bg,
                activeforeground=fg,
                font=("Malgun Gothic", 9),
            ).grid(row=5, column=1, sticky=tk.W, padx=(10, 0), pady=(8, 4))

            self._ss_status_lbl = tk.Label(
                body,
                text="대기 중",
                bg=bg,
                fg=primary,
                font=("Malgun Gothic", 8),
                anchor=tk.W,
            )
            self._ss_status_lbl.grid(row=6, column=0, columnspan=2, sticky=tk.EW, pady=(10, 0))

            try:
                self._ss_on_strategy_change()
            except Exception:
                pass

            footer = tk.Frame(dlg, bg=bg, pady=12)
            footer.pack(side=tk.BOTTOM, fill=tk.X, padx=16)
            self._ss_btn_run = tk.Button(
                footer,
                text="시뮬 실행",
                width=14,
                height=1,
                bg="#A3BE8C",
                fg="#2E3440",
                font=("Malgun Gothic", 10, "bold"),
                relief=tk.FLAT,
                cursor="hand2",
                command=self._on_single_stock_sim_run,
            )
            self._ss_btn_run.pack(side=tk.LEFT)
            tk.Button(
                footer,
                text="마지막 리포트",
                width=14,
                height=1,
                bg="#5E81AC",
                fg="#ECEFF4",
                font=("Malgun Gothic", 10, "bold"),
                relief=tk.FLAT,
                cursor="hand2",
                command=self._open_single_stock_report,
            ).pack(side=tk.LEFT, padx=(8, 0))

            def _on_close() -> None:
                try:
                    if self._ss_running:
                        messagebox.showinfo(
                            "실행 중",
                            "시뮬이 끝날 때까지 기다려 주세요.",
                            parent=dlg,
                        )
                        return
                except Exception:
                    pass
                try:
                    dlg.destroy()
                except Exception:
                    pass
                self._ss_dlg = None

            try:
                dlg.protocol("WM_DELETE_WINDOW", _on_close)
            except Exception:
                pass
            try:
                dlg.bind("<Return>", lambda _e: self._on_single_stock_sim_run())
            except Exception:
                pass
        except Exception as e:
            try:
                self._show_copyable_error(
                    "오류",
                    f"종목시뮬 창을 열지 못했습니다:\n{e}\n\n{traceback.format_exc()}",
                )
            except Exception:
                pass

    def _on_single_stock_sim_run(self) -> None:
        """다이얼로그 입력을 검증하고 백그라운드 시뮬을 시작한다."""
        if self._ss_running:
            return
        try:
            code_raw = ""
            if self._ss_widget_alive(self._ss_code_entry):
                code_raw = str(self._ss_code_entry.get() or "").strip()
            start_raw = ""
            if self._ss_widget_alive(self._ss_start_entry):
                start_raw = str(self._ss_start_entry.get() or "").strip()
            end_raw = ""
            if self._ss_widget_alive(self._ss_end_entry):
                end_raw = str(self._ss_end_entry.get() or "").strip()
            try:
                hp = bool(self._ss_hp_var.get()) if self._ss_hp_var is not None else False
            except Exception:
                hp = False
            try:
                strategy = (
                    str(self._ss_strategy_var.get() or "s1_s2")
                    if self._ss_strategy_var is not None
                    else "s1_s2"
                )
            except Exception:
                strategy = "s1_s2"
            vol_k_raw = "0.5"
            if self._ss_widget_alive(self._ss_vol_k_entry):
                try:
                    # DISABLED 상태에서도 get 가능
                    vol_k_raw = str(self._ss_vol_k_entry.get() or "0.5").strip()
                except Exception:
                    vol_k_raw = "0.5"
        except Exception as e:
            messagebox.showerror("오류", f"입력을 읽지 못했습니다:\n{e}", parent=self.parent)
            return

        parent = self._ss_dlg if self._ss_widget_alive(self._ss_dlg) else self.parent
        if not code_raw:
            messagebox.showwarning("알림", "종목코드를 입력해 주세요.", parent=parent)
            return
        try:
            datetime.strptime(start_raw[:10], "%Y-%m-%d")
            datetime.strptime(end_raw[:10], "%Y-%m-%d")
        except Exception:
            messagebox.showwarning("알림", "날짜는 YYYY-MM-DD 형식으로 입력해 주세요.", parent=parent)
            return
        if strategy == "vol_breakout":
            try:
                k_chk = float(vol_k_raw)
                if k_chk < 0.01 or k_chk > 2.0:
                    raise ValueError("range")
            except Exception:
                messagebox.showwarning(
                    "알림",
                    "K 값은 0.01 ~ 2.0 사이 숫자로 입력해 주세요. (예: 0.5)",
                    parent=parent,
                )
                return

        self._ss_running = True
        try:
            if self._ss_widget_alive(self._ss_btn_run):
                self._ss_btn_run.config(state=tk.DISABLED)
        except Exception:
            pass
        try:
            if self.btn_single_stock_sim is not None:
                self.btn_single_stock_sim.config(state=tk.DISABLED)
        except Exception:
            pass
        self._ss_set_status("종목 시뮬 시작...", "#EBCB8B")
        threading.Thread(
            target=self._run_single_stock_sim_thread,
            args=(code_raw, start_raw, end_raw, hp, strategy, vol_k_raw),
            daemon=True,
        ).start()

    def _run_single_stock_sim_thread(
        self,
        code: str,
        start: str,
        end: str,
        high_precision: bool,
        strategy: str = "s1_s2",
        vol_k: str = "0.5",
    ) -> None:
        """단일 종목 시뮬을 백그라운드에서 실행하고 리포트를 연다."""
        try:
            from single_stock_sim import SingleStockSimError, run_single_stock_simulation

            def update(msg: str, val: Optional[float] = None) -> None:
                self.gui_queue.put(lambda m=str(msg): self._ss_set_status(m, "#EBCB8B"))

            try:
                active = self.app.settings.copy()
            except Exception:
                active = dict(self.app.settings or {})

            result = run_single_stock_simulation(
                active,
                code,
                start,
                end,
                high_precision=high_precision,
                callback=update,
                strategy=strategy,
                vol_k=vol_k,
            )
            n_trades = int(result.get("n_trades") or 0)
            name = str(result.get("name") or result.get("code") or code)
            code_n = str(result.get("code") or code)
            report = str(result.get("report_path") or "")
            strat = str(result.get("strategy") or strategy or "s1_s2")
            if strat == "vol_breakout":
                try:
                    k_show = result.get("vol_k")
                    k_txt = f"K={float(k_show):g}" if k_show is not None else "K=?"
                except Exception:
                    k_txt = "K=?"
                mode = f"변동성돌파 {k_txt}"
                if result.get("high_precision"):
                    mode += " / 고정밀"
            else:
                mode = "고정밀" if result.get("high_precision") else "15분"
            done_msg = f"종목시뮬 완료 ({name} {code_n} / {mode} / 매매 {n_trades}건)"
            color = "#A3BE8C" if n_trades > 0 else "#EBCB8B"

            def _finish() -> None:
                self._ss_set_status(done_msg, color)
                if report and os.path.exists(report):
                    try:
                        webbrowser.open(f"file://{os.path.abspath(report)}")
                    except Exception:
                        pass
                if n_trades <= 0:
                    parent = self._ss_dlg if self._ss_widget_alive(self._ss_dlg) else self.parent
                    try:
                        empty_kind = (
                            "변동성 돌파 체결"
                            if strat == "vol_breakout"
                            else "S1/S2/청산"
                        )
                        messagebox.showinfo(
                            "매매 없음",
                            f"{name}({code_n}) {start} ~ {end}\n해당 기간에 체결된 {empty_kind}이 없습니다.\n리포트는 생성되었습니다.",
                            parent=parent,
                        )
                    except Exception:
                        pass

            self.gui_queue.put(_finish)
        except Exception as _err:
            err_text = str(_err)
            tb = traceback.format_exc()

            def _fail() -> None:
                self._ss_set_status("종목시뮬 실패", "#BF616A")
                parent = self._ss_dlg if self._ss_widget_alive(self._ss_dlg) else self.parent
                try:
                    from single_stock_sim import SingleStockSimError
                    is_user = isinstance(_err, SingleStockSimError)
                except Exception:
                    is_user = False
                try:
                    if is_user:
                        messagebox.showwarning("종목시뮬", err_text, parent=parent)
                    else:
                        self._show_copyable_error("종목시뮬 오류", f"{err_text}\n\n{tb}")
                except Exception:
                    try:
                        messagebox.showerror("종목시뮬 오류", err_text, parent=parent)
                    except Exception:
                        pass

            self.gui_queue.put(_fail)
        finally:
            def _unlock() -> None:
                self._ss_running = False
                try:
                    if self._ss_widget_alive(self._ss_btn_run):
                        self._ss_btn_run.config(state=tk.NORMAL)
                except Exception:
                    pass
                try:
                    if self.btn_single_stock_sim is not None:
                        self.btn_single_stock_sim.config(state=tk.NORMAL)
                except Exception:
                    pass

            self.gui_queue.put(_unlock)

