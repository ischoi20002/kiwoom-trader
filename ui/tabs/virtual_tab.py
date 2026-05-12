import tkinter as tk
import math
import threading
import os
import json
import importlib
import time
from tkinter import ttk, messagebox
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple
import config
from utils.eod_equity_snapshots import compute_day_pnl_vs_eod_store
from utils.equity_snapshots_store import (
    equity_snapshots_root_candidates,
    read_equity_snapshot_from_root,
)
from utils.fee_rates import (
    fee_decimal_rates_live_trading,
    unrealized_at_price_cost_ex_buy_net_of_sell_fee,
    unrealized_at_price_net_of_sell_fee,
)
from utils.plotly_cdn import plotly_cdn_script_tag
from reporting.krx_trading_calendar import (
    is_trading_day,
    next_trading_day,
    previous_trading_day,
    trading_days_inclusive,
)

# 포지션 탭 트리 컬럼 기본 폭(px). 사용자 조절·세로 분할은 virtual_portfolio_ui.json 에 저장합니다.
_DEFAULT_VIRTUAL_ACTIVE_COL_WIDTHS: Dict[str, int] = {
    "Entry": 140,
    "Code": 72,
    "Name": 120,
    "Qty": 56,
    "PnL": 100,
    "PnLPct": 72,
    "Eval": 100,
    "BuyIn": 96,
    "CurP": 88,
    "AvgP": 88,
    "DayChg": 88,
    "WtPct": 72,
}
_DEFAULT_VIRTUAL_ORDER_COL_WIDTHS: Dict[str, int] = {
    "Time": 138,
    "Code": 88,
    "Name": 100,
    "Side": 88,
    "Qty": 88,
    "FQty": 72,
    "OrderP": 88,
    "FilledP": 88,
    "VPOrd": 72,
    "BAOrd": 200,
    "Status": 88,
    "Reason": 120,
}
_VIRTUAL_ACTIVE_COLS: Tuple[str, ...] = tuple(_DEFAULT_VIRTUAL_ACTIVE_COL_WIDTHS.keys())
_VIRTUAL_ORDER_COLS: Tuple[str, ...] = tuple(_DEFAULT_VIRTUAL_ORDER_COL_WIDTHS.keys())
_VIRTUAL_COL_MIN: Dict[str, int] = {c: 40 for c in _VIRTUAL_ACTIVE_COLS}
_VIRTUAL_COL_MIN.update(
    {c: 40 for c in _VIRTUAL_ORDER_COLS if c not in _VIRTUAL_COL_MIN}
)


class VirtualTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self.vars = {}
        # [v4.5.3] 실시간 보유: ttk.Treeview (행 iid=6자리 코드, 종료/주문과 동일 패턴)
        self.tree_active: Optional[ttk.Treeview] = None
        self.tree_orders = None
        self.lbl_mode_badge = None
        self.btn_env = None  # 운영 버튼은 대시보드로 이전됨(레거시 참조용)
        self.lbl_mock_result = None
        # 종목별 REST 시세 폴백 캐시 (API 과다·429 방지)
        self._fallback_quote_cache: Dict[str, Tuple[float, float]] = {}
        # 주문 로그: 조회 중인 날짜(네비게이션). 기본은 오늘.
        self._order_log_view_date: date = date.today()
        self._order_log_date_var: Optional[tk.StringVar] = None
        self._btn_order_log_prev: Optional[tk.Button] = None
        self._btn_order_log_next: Optional[tk.Button] = None
        self._btn_order_log_today: Optional[tk.Button] = None
        # 주문·체결 통합 트리 필터(구분 / 체결)
        self._order_filter_side_var: Optional[tk.StringVar] = None
        self._order_filter_fill_var: Optional[tk.StringVar] = None
        # 구분 필터: 세그먼트 컨트롤(전체·매수·매도) 버튼 참조
        self._order_side_segment_btns: Dict[str, tk.Button] = {}
        self.btn_narrative_report: Optional[ttk.Button] = None
        self._narrative_report_busy: bool = False
        # 보유/주문 트리 컬럼 폭·세로 Paned 비율 저장
        self._virtual_ui_save_job: Optional[Any] = None
        self._virtual_sash_sync_job: Optional[Any] = None  # 세시 이동 직후 sashpos 동기화용(디바운스)
        self._virtual_main_paned: Optional[ttk.PanedWindow] = None
        self._virtual_saved_sash_rel: Optional[float] = None
        # 보유 합계(수수료 제외) 값 라벨 — 손익 부호에 따라 전경색 갱신
        self._hold_agg_lbl_pnl: Optional[tk.Label] = None
        self._hold_agg_lbl_pct: Optional[tk.Label] = None
        self._hold_fee_toggle_cv: Optional[tk.Canvas] = None
        # 수수료 토글: UI(슬라이더) 즉시 반응 후 합계·트리는 after 로 지연
        self._hold_fee_toggle_after_id: Optional[Any] = None

        # [EOD Equity Snapshot] 자산추이/비중 분석(운영 스냅샷 기반)
        self._equity_trend_html_path: str = os.path.abspath("virtual_equity_trend.html")
        self._equity_trend_last_end_day: Optional[date] = None  # 프리셋 기준일(=마지막 스냅샷)
        self._equity_trend_status_var: tk.StringVar = tk.StringVar(value="스냅샷 데이터 준비 필요")

        self._setup_ui()
        self._render_virtual_positions()
        try:
            self.app.trading_controller._ensure_virtual_positions_monitor_info()
            self.app.monitor_info = dict(self.app.trading_controller.monitor_info or {})
        except Exception:
            pass

    def _setup_ui(self):
        """[v3.9] 수직 레이아웃: 보유 + 주문·체결(필터로 매도·체결 시 종료 매도와 동일 뷰)."""
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
            "open_ur": tk.StringVar(value="0 (0.00%)"),
            "day": tk.StringVar(value="— (전일 EOD 없음)"),
            "cum": tk.StringVar(value="0 (+0.00%)"),
            # 실시간 보유 트리 하단: 보유 전체 합계(매수·매도 수수료 미반영)
            "hold_sum_buy_ex": tk.StringVar(value="0"),
            "hold_sum_eval_ex": tk.StringVar(value="0"),
            "hold_sum_pnl_ex": tk.StringVar(value="0"),
            "hold_sum_pct_ex": tk.StringVar(value="0.00%"),
            # 보유 합계 카드: 수수료 반영 표시 ON/OFF (기본 OFF = 수수료 제외 합계)
            "hold_agg_fee_reflect": tk.BooleanVar(value=False),
        })

        for label, var, color in [
            ("총 자산", "total", self.app.colors['primary']),
            ("예수금", "cash", None),
            ("D+1 예상", "d1_cash", self.app.colors['primary']),
            ("D+2 예상", "d2_cash", self.app.colors['primary']),
            ("주식 평가금", "stock", None),
            ("미실현 손익", "open_ur", "#88C0D0"),
            ("금일 손익 (전일 기준)", "day", "#EBCB8B"),
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

        # 동기화·서버 전환 등은 대시보드 「운영 · 계좌」, 종합리포트·일일결산은 전략검증 탭으로 두었습니다.
        ttk.Label(
            header_f,
            text="동기화·관심목록·서버 전환 → 대시보드 · 종합리포트·일일결산 → 전략검증 탭",
            font=("Malgun Gothic", 8),
            foreground="#787c99",
        ).pack(side=tk.RIGHT, padx=(8, 0), pady=(12, 0))

        # --- 포트폴리오 자산 추이(운영용 EOD 스냅샷 기반) ---
        trend_frame = ttk.LabelFrame(
            main_container,
            text=" 📈 포트폴리오 자산 추이 · 비중(현금=D+2 포함) ",
            padding=(10, 8),
        )
        trend_frame.pack(side=tk.TOP, fill=tk.X, padx=10, pady=(0, 8))

        # 기간 선택 (프리셋 버튼 + 커스텀 입력을 같은 줄에 배치)
        ctrl_row = ttk.Frame(trend_frame)
        ctrl_row.pack(side=tk.TOP, fill=tk.X, pady=(0, 6))

        preset_lf = ttk.Frame(ctrl_row)
        preset_lf.pack(side=tk.LEFT, fill=tk.X)

        ttk.Label(
            preset_lf,
            text="기간:",
            font=("Malgun Gothic", 9, "bold"),
            foreground=self.app.colors["fg"],
        ).pack(side=tk.LEFT)

        btn_row = ttk.Frame(preset_lf)
        btn_row.pack(side=tk.LEFT, padx=6)

        def _mk_preset_handler(code: str) -> Callable[[], None]:
            return lambda: self._on_equity_trend_select_preset(code)

        ttk.Button(btn_row, text="1주일", width=8, command=_mk_preset_handler("1w")).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_row, text="1달", width=8, command=_mk_preset_handler("1m")).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_row, text="6개월", width=8, command=_mk_preset_handler("6m")).pack(
            side=tk.LEFT, padx=(0, 6)
        )
        ttk.Button(btn_row, text="1년", width=8, command=_mk_preset_handler("1y")).pack(
            side=tk.LEFT
        )

        # 커스텀 기간 (캘린더 팝업: CustomDatePicker) — 프리셋과 같은 줄
        custom_lf = ttk.Frame(ctrl_row)
        custom_lf.pack(side=tk.LEFT, fill=tk.X, padx=(10, 0))

        ttk.Label(custom_lf, text="커스텀:", font=("Malgun Gothic", 9, "bold")).pack(
            side=tk.LEFT
        )

        self._equity_trend_start_entry = ttk.Entry(custom_lf, width=12)
        self._equity_trend_start_entry.pack(side=tk.LEFT, padx=(6, 4))
        ttk.Button(
            custom_lf,
            text="📅",
            width=3,
            command=lambda: self._show_equity_trend_picker("start"),
        ).pack(side=tk.LEFT, padx=(0, 10))

        self._equity_trend_end_entry = ttk.Entry(custom_lf, width=12)
        self._equity_trend_end_entry.pack(side=tk.LEFT, padx=(6, 4))
        ttk.Button(
            custom_lf,
            text="📅",
            width=3,
            command=lambda: self._show_equity_trend_picker("end"),
        ).pack(side=tk.LEFT, padx=(0, 10))

        self._equity_trend_apply_btn = ttk.Button(
            custom_lf, text="그래프 새로고침", width=16, command=self._on_equity_trend_refresh
        )
        self._equity_trend_apply_btn.pack(side=tk.LEFT)

        ttk.Label(
            trend_frame,
            textvariable=self._equity_trend_status_var,
            font=("Malgun Gothic", 9),
            foreground="#EBCB8B",
        ).pack(side=tk.TOP, fill=tk.X, pady=(8, 0))

        # --- [2] 메인 레이아웃 (보유 + 주문·체결 2단) ---
        v_paned = ttk.PanedWindow(main_container, orient=tk.VERTICAL)
        v_paned.pack(fill=tk.BOTH, expand=True)
        self._virtual_main_paned = v_paned
        try:
            v_paned.bind("<<PanedWindowMoved>>", self._on_virtual_paned_moved, add="+")
        except Exception:
            pass
        # Windows 등에서 <<PanedWindowMoved>> 가 누락되거나, 이벤트 직후 sashpos 가 아직 갱신 안 되는 경우 대비
        try:
            v_paned.bind("<ButtonRelease-1>", self._on_virtual_paned_mouse_release, add="+")
        except Exception:
            pass

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

        # 2. 주문·체결 내역(한 줄: 구분 세그먼트·체결 콤보 + 날짜 네비)
        v_paned.add(self._build_order_log_section(v_paned, _pm), weight=5)

        self._setup_styles()
        self._schedule_restore_virtual_sash()

        # 초기(가능하면) 커스텀 입력값 세팅
        try:
            self._equity_trend_init_default_dates()
        except Exception:
            pass

    def _equity_trend_init_default_dates(self) -> None:
        """
        커스텀 start/end 엔트리에 기본값을 채웁니다.
        - end: 마지막 스냅샷 날짜
        - start: end 직전 30일(달력 기준) → 거래일 스냅
        """
        end_day = self._equity_trend_get_last_snapshot_day()
        if end_day is None:
            return

        start_guess = end_day - timedelta(days=30)
        start_day = self._equity_trend_snap_to_nearest_trading_day(start_guess)
        if start_day is None:
            start_day = end_day

        self._equity_trend_last_end_day = end_day
        self._equity_trend_start_entry.delete(0, tk.END)
        self._equity_trend_start_entry.insert(0, start_day.isoformat())
        self._equity_trend_end_entry.delete(0, tk.END)
        self._equity_trend_end_entry.insert(0, end_day.isoformat())

    def _equity_trend_get_last_snapshot_day(self) -> Optional[date]:
        try:
            roots = equity_snapshots_root_candidates()
            days: List[date] = []
            for root in roots:
                if not os.path.isdir(root):
                    continue
                for fn in os.listdir(root):
                    if not fn.endswith(".json"):
                        continue
                    d = fn[:10]
                    try:
                        days.append(datetime.strptime(d, "%Y-%m-%d").date())
                    except Exception:
                        continue
            return max(days) if days else None
        except Exception:
            return None

    def _equity_trend_snap_to_nearest_trading_day(self, d: date) -> Optional[date]:
        try:
            if is_trading_day(d):
                return d
            prev = previous_trading_day(d)
            nxt = next_trading_day(d)
            if prev is None and nxt is None:
                return None
            if prev is None:
                return nxt
            if nxt is None:
                return prev
            return prev if abs((d - prev).days) <= abs((nxt - d).days) else nxt
        except Exception:
            return d

    def _show_equity_trend_picker(self, which: str) -> None:
        try:
            from ui.gui_utils import CustomDatePicker

            if which == "start":
                entry = self._equity_trend_start_entry
                init = entry.get().strip() if entry.get().strip() else None
            else:
                entry = self._equity_trend_end_entry
                init = entry.get().strip() if entry.get().strip() else None

            CustomDatePicker(self.parent, entry, initial_date=init, colors=self.app.colors)
        except Exception as e:
            try:
                messagebox.showerror("오류", str(e))
            except Exception:
                pass

    def _on_equity_trend_select_preset(self, preset: str) -> None:
        end_day = self._equity_trend_get_last_snapshot_day()
        if end_day is None:
            self._equity_trend_status_var.set("스냅샷 데이터 없음: equity_snapshots_* 폴더 확인 필요")
            return

        self._equity_trend_last_end_day = end_day
        delta_days = 7 if preset == "1w" else 30 if preset == "1m" else 183 if preset == "6m" else 365
        start_guess = end_day - timedelta(days=delta_days)
        start_day = self._equity_trend_snap_to_nearest_trading_day(start_guess) or start_guess

        self._equity_trend_start_entry.delete(0, tk.END)
        self._equity_trend_start_entry.insert(0, start_day.isoformat())
        self._equity_trend_end_entry.delete(0, tk.END)
        self._equity_trend_end_entry.insert(0, end_day.isoformat())

        self._on_equity_trend_refresh()

    def _on_equity_trend_refresh(self) -> None:
        """
        선택된 기간(start/end)을:
        - 비거래일이면 KRX 거래일로 스냅
        - end는 마지막 스냅샷 날짜를 넘지 않도록 클램프
        로 처리 후 Plotly HTML을 생성합니다.
        """
        try:
            end_last = self._equity_trend_get_last_snapshot_day()
            if end_last is None:
                self._equity_trend_status_var.set("스냅샷 데이터 없음: 자동 EOD 스냅샷을 먼저 생성하세요.")
                return

            raw_start = self._equity_trend_start_entry.get().strip()
            raw_end = self._equity_trend_end_entry.get().strip()
            if not raw_start or not raw_end:
                self._equity_trend_status_var.set("커스텀 start/end 값을 입력하세요.")
                return

            start_day = datetime.strptime(raw_start[:10], "%Y-%m-%d").date()
            end_day = datetime.strptime(raw_end[:10], "%Y-%m-%d").date()
            if start_day > end_day:
                start_day, end_day = end_day, start_day

            start_day = self._equity_trend_snap_to_nearest_trading_day(start_day) or start_day
            end_day = self._equity_trend_snap_to_nearest_trading_day(end_day) or end_day

            # 프리셋 요구사항: end는 last snapshot 기준
            if end_day > end_last:
                end_day = end_last

            daily_equity = self._equity_trend_build_daily_equity(start_day, end_day)
            if not daily_equity:
                self._equity_trend_status_var.set("선택 기간에 스냅샷 데이터가 없습니다.")
                return

            self._render_equity_trend_html(daily_equity, start_day, end_day)
            self._equity_trend_status_var.set(
                f"표시: {start_day.isoformat()} ~ {end_day.isoformat()} (points={len(daily_equity)})"
            )

            # HTML 생성 후 기본 브라우저로 열기 (탭 내 embed 라이브러리 부재)
            try:
                import webbrowser

                webbrowser.open(f"file://{self._equity_trend_html_path}")
            except Exception:
                pass
        except Exception as e:
            try:
                messagebox.showerror("오류", f"자산 추이 그래프 생성 실패: {e}")
            except Exception:
                pass

    def _equity_trend_build_daily_equity(self, start_day: date, end_day: date) -> List[Dict[str, Any]]:
        """
        Plotly에 넣을 daily_equity 리스트를 구성합니다.
        - daily_equity item 키: date, equity(=total_assets), cash(=d2_deposit)
        """
        trading_days = trading_days_inclusive(start_day, end_day)
        if not trading_days:
            return []

        out: List[Optional[Dict[str, Any]]] = []
        last_filled: Optional[Dict[str, Any]] = None
        first_valid_idx: Optional[int] = None

        roots = equity_snapshots_root_candidates()

        for d in trading_days:
            ds = d.isoformat()
            rec = None
            for root in roots:
                rec = read_equity_snapshot_from_root(root, ds)
                if rec is not None:
                    break
            if rec is None:
                if last_filled is None:
                    # 구간 시작부가 스냅샷 공백이면 일단 placeholder
                    out.append(None)
                    continue
                # 스냅샷이 비어있는 날은 직전 스냅샷 값으로 forward-fill
                rec = dict(last_filled)
                rec["date"] = ds

            last_filled = rec
            if first_valid_idx is None:
                first_valid_idx = len(out)

            cash = float(rec.get("d2_deposit", 0) or 0)
            stock_eval = float(rec.get("stock_eval", 0) or 0)
            equity = float(cash + stock_eval)

            out.append({"date": ds, "equity": equity, "cash": cash})

        if not out:
            return []
        if all(v is None for v in out):
            return []

        # 구간 시작부가 스냅샷 공백이면, 첫 유효 스냅샷 값을 back-fill하여 라인이 보이게 합니다.
        if first_valid_idx is not None and first_valid_idx > 0:
            first_rec = out[first_valid_idx]
            if isinstance(first_rec, dict):
                for i in range(0, first_valid_idx):
                    if out[i] is None:
                        cloned = dict(first_rec)
                        cloned["date"] = trading_days[i].isoformat()
                        out[i] = cloned

        return [v for v in out if isinstance(v, dict)]

    def _render_equity_trend_html(
        self, daily_equity: List[Dict[str, Any]], start_day: date, end_day: date
    ) -> None:
        """
        Plotly HTML을 단일 파일로 생성합니다(파일 덮어쓰기).
        """
        try:
            daily_equity = sorted(daily_equity, key=lambda x: x["date"])
            dates = [d["date"] for d in daily_equity]
            equities = [float(d["equity"]) for d in daily_equity]
            cashes = [float(d["cash"]) for d in daily_equity]
            holdings = [max(0.0, float(e) - float(c)) for e, c in zip(equities, cashes)]

            # 최대값은 y 스케일을 안정화하기 위해 완충
            max_val = max(equities) * 1.05 if equities else 1.0

            html_cdn = plotly_cdn_script_tag()

            html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  {html_cdn}
  <style>
    body {{
      margin: 0;
      background: #2E3440;
      color: #ECEFF4;
      font-family: "Malgun Gothic", Segoe UI, Arial, sans-serif;
    }}
    .wrap {{
      padding: 16px 18px;
    }}
    h3 {{
      margin: 0 0 12px 0;
      font-size: 1.15rem;
      border-bottom: 1px solid #334155;
      padding-bottom: 10px;
      color: #88C0D0;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <h3>📈 포트폴리오 자산 추이 및 비중 분석</h3>
    <div style="margin-bottom:8px;color:#A3BE8C;font-size:0.92rem;">
      {start_day.isoformat()} ~ {end_day.isoformat()}
    </div>
    <div id="equity-chart" style="height:520px; width:100%; min-height:520px;"></div>
  </div>
  <script>
    const traceEquity = {{
      x: {json.dumps(dates)},
      y: {json.dumps(equities)},
      name: '총 자본 (평가액)',
      type: 'scatter',
      mode: 'lines+markers',
      line: {{ color: '#f43f5e', width: 3 }},
      marker: {{ size: 8, color: '#f43f5e' }},
      yaxis: 'y2'
    }};
    const traceCash = {{
      x: {json.dumps(dates)},
      y: {json.dumps(cashes)},
      name: '현금 잔고 (D+2)',
      type: 'scatter',
      mode: 'none',
      fill: 'tozeroy',
      fillcolor: 'rgba(56, 189, 248, 0.2)',
      stackgroup: 'one'
    }};
    const traceHoldings = {{
      x: {json.dumps(dates)},
      y: {json.dumps(holdings)},
      name: '주식 보유금액',
      type: 'scatter',
      mode: 'none',
      fill: 'tonexty',
      fillcolor: 'rgba(245, 158, 11, 0.4)',
      stackgroup: 'one'
    }};
    const layout = {{
      paper_bgcolor: 'rgba(0,0,0,0)',
      plot_bgcolor: 'rgba(0,0,0,0)',
      margin: {{ t: 10, b: 40, l: 60, r: 60 }},
      legend: {{ orientation: 'h', x: 0.5, xanchor: 'center', y: 1.1, font: {{ color: '#ccc' }} }},
      xaxis: {{ gridcolor: '#333', tickfont: {{ color: '#888' }} }},
      yaxis: {{
        gridcolor: '#333',
        tickfont: {{ color: '#888' }},
        title: '자산 구성 (원)',
        side: 'left',
        range: [0, {max_val}]
      }},
      yaxis2: {{
        overlaying: 'y',
        side: 'right',
        showgrid: false,
        tickfont: {{ color: '#f43f5e' }},
        title: '총 자산 (원)',
        range: [0, {max_val}]
      }},
      hovermode: 'x unified'
    }};
    Plotly.newPlot('equity-chart', [traceCash, traceHoldings, traceEquity], layout, {{ responsive: true, displayModeBar: false }});
  </script>
</body>
</html>"""

            with open(self._equity_trend_html_path, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception as e:
            try:
                self._equity_trend_status_var.set(f"HTML 생성 실패: {e}")
            except Exception:
                pass

    def _virtual_ui_json_path(self) -> str:
        """settings.json 과 같은 폴더에 포지션 탭 UI 상태를 둡니다."""
        try:
            ch = getattr(self.app, "config_handler", None)
            sf = getattr(ch, "settings_file", None) if ch else None
            if sf:
                base = os.path.dirname(os.path.abspath(sf))
                if base:
                    return os.path.join(base, "virtual_portfolio_ui.json")
        except Exception:
            pass
        return os.path.join(os.getcwd(), "virtual_portfolio_ui.json")

    def _read_virtual_ui_file(self) -> Dict[str, Any]:
        try:
            path = self._virtual_ui_json_path()
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                return raw if isinstance(raw, dict) else {}
        except Exception as e:
            try:
                print(f"[VirtualTab] UI 상태 파일 읽기 실패: {e}")
            except Exception:
                pass
        return {}

    @staticmethod
    def _parse_virtual_sash_rel(raw: Dict[str, Any]) -> Optional[float]:
        try:
            v = raw.get("sash0_rel")
            if v is None:
                return None
            x = float(v)
            if x != x:
                return None
            return max(0.08, min(0.88, x))
        except (TypeError, ValueError):
            return None

    def _merge_virtual_col_widths(
        self,
        keys: Tuple[str, ...],
        defaults: Dict[str, int],
        saved: Any,
    ) -> Dict[str, int]:
        out = {k: int(defaults.get(k, 64)) for k in keys}
        if not isinstance(saved, dict):
            return out
        for k, v in saved.items():
            if k not in out:
                continue
            try:
                iw = int(v)
                lo = int(_VIRTUAL_COL_MIN.get(k, 32))
                if lo <= iw <= 4000:
                    out[k] = iw
            except (TypeError, ValueError):
                pass
        return out

    def _flush_virtual_ui_to_disk(self) -> None:
        """보유·주문 트리 컬럼 폭과 세로 분할 비율을 저장합니다."""
        self._virtual_ui_save_job = None
        try:
            ca: Dict[str, int] = {
                c: int(_DEFAULT_VIRTUAL_ACTIVE_COL_WIDTHS.get(c, 64))
                for c in _VIRTUAL_ACTIVE_COLS
            }
            co: Dict[str, int] = {
                c: int(_DEFAULT_VIRTUAL_ORDER_COL_WIDTHS.get(c, 88))
                for c in _VIRTUAL_ORDER_COLS
            }
            ta = getattr(self, "tree_active", None)
            if ta is not None:
                for c in _VIRTUAL_ACTIVE_COLS:
                    try:
                        lo = int(_VIRTUAL_COL_MIN.get(c, 32))
                        ca[c] = max(lo, int(ta.column(c, "width")))
                    except Exception:
                        pass
            to = getattr(self, "tree_orders", None)
            if to is not None:
                for c in _VIRTUAL_ORDER_COLS:
                    try:
                        lo = int(_VIRTUAL_COL_MIN.get(c, 32))
                        co[c] = max(lo, int(to.column(c, "width")))
                    except Exception:
                        pass
            rel = self._virtual_current_sash_rel()
            if rel is None:
                rel = getattr(self, "_virtual_saved_sash_rel", None)
            if rel is None:
                rel = self._parse_virtual_sash_rel(self._read_virtual_ui_file())
            path = self._virtual_ui_json_path()
            dname = os.path.dirname(path)
            if dname:
                os.makedirs(dname, exist_ok=True)
            payload: Dict[str, Any] = {
                "columns_active": ca,
                "columns_orders": co,
            }
            if rel is not None:
                payload["sash0_rel"] = float(rel)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception as e:
            try:
                print(f"[VirtualTab] UI 상태 저장 실패: {e}")
            except Exception:
                pass

    def _schedule_virtual_ui_save(self) -> None:
        try:
            r = getattr(self.app, "root", None)
            if r is None:
                return
            jid = self._virtual_ui_save_job
            if jid is not None:
                try:
                    r.after_cancel(jid)
                except Exception:
                    pass
            self._virtual_ui_save_job = r.after(380, self._flush_virtual_ui_to_disk)
        except Exception:
            pass

    def _virtual_current_sash_rel(self) -> Optional[float]:
        try:
            p = self._virtual_main_paned
            if p is None:
                return None
            h = int(p.winfo_height())
            if h < 40:
                return None
            sp = int(p.sashpos(0))
            return max(0.08, min(0.88, float(sp) / float(h)))
        except (tk.TclError, TypeError, ValueError, ZeroDivisionError):
            return None

    def _request_virtual_sash_sync(self) -> None:
        """
        세로 분할(sash) 변경 직후에는 Tk가 내부 좌표를 한 틱 늦게 반영하는 경우가 있어
        짧게 지연한 뒤 비율을 읽어 저장합니다. (<<PanedWindowMoved>> 누락·타이밍 이슈 완화)
        """
        try:
            r = getattr(self.app, "root", None)
            if r is None:
                return
            jid = self._virtual_sash_sync_job
            if jid is not None:
                try:
                    r.after_cancel(jid)
                except Exception:
                    pass
            self._virtual_sash_sync_job = r.after(20, self._apply_virtual_sash_sync)
        except Exception:
            pass

    def _apply_virtual_sash_sync(self) -> None:
        """지연 후 메모리에 sash 비율을 반영하고 디스크 저장을 예약합니다."""
        self._virtual_sash_sync_job = None
        try:
            rel = self._virtual_current_sash_rel()
            if rel is not None:
                self._virtual_saved_sash_rel = rel
            self._schedule_virtual_ui_save()
        except Exception:
            pass

    def _release_is_on_paned_sash(self, event: Any) -> bool:
        """패인 자식에서 올린 마우스 이벤트도 오므로, 세시 위에서만 True."""
        try:
            p = self._virtual_main_paned
            if p is None:
                return False
            ex = int(event.x_root - p.winfo_rootx())
            ey = int(event.y_root - p.winfo_rooty())
            rid = p.identify(ex, ey)
            # Windows ttk: 패인 영역은 '' , 세시는 정수 인덱스(0, 1, …)로 오는 경우가 많음
            if rid is None:
                return False
            if isinstance(rid, int):
                return True
            s = str(rid).lower()
            return "sash" in s
        except (tk.TclError, TypeError, ValueError):
            return False
        except Exception:
            return False

    def _on_virtual_paned_moved(self, _event: Any = None) -> None:
        self._request_virtual_sash_sync()

    def _on_virtual_paned_mouse_release(self, event: Any) -> None:
        try:
            if not self._release_is_on_paned_sash(event):
                return
            self._request_virtual_sash_sync()
        except Exception:
            pass

    def _schedule_restore_virtual_sash(self) -> None:
        """
        세로 Paned: 저장된 비율로 sash 복원.
        높이가 아직 1~2px일 때 pos 를 크게 잡으면 상단(보유) 영역이 사실상 사라진 것처럼 보일 수 있어
        최소 높이·클램프를 강화한다.
        """
        try:
            root = getattr(self.app, "root", None)
            if root is None:
                return

            def _try(attempt: int = 0) -> None:
                try:
                    rel = getattr(self, "_virtual_saved_sash_rel", None)
                    if rel is None:
                        rel = 0.42
                    p = self._virtual_main_paned
                    if p is None:
                        return
                    try:
                        p.update_idletasks()
                    except Exception:
                        pass
                    h = int(p.winfo_height())
                    # 레이아웃 미확정 시 대기 (너무 작은 h 에서 sashpos 하면 상단 패인이 0에 가깝게 붕괴)
                    if h < 160 and attempt < 24:
                        root.after(100, lambda: _try(attempt + 1))
                        return
                    min_top = 100
                    min_bot = 100
                    if h < 300:
                        min_top = 72
                        min_bot = 72
                    raw = float(rel) * float(h)
                    pos = int(round(raw))
                    pos = max(min_top, min(pos, h - min_bot))
                    if pos >= h - 8:
                        pos = max(min_top, h // 2)
                    if pos < min_top:
                        pos = min_top
                    p.sashpos(0, pos)
                except Exception:
                    if attempt < 24:
                        root.after(100, lambda: _try(attempt + 1))

            root.after(120, lambda: _try(0))
        except Exception:
            pass

    def _on_virtual_tree_column_drag_end(self, event: tk.Event) -> None:
        """열 구분선 드래그 후 저장 예약."""
        w: Any = None
        try:
            w = event.widget
            if not isinstance(w, ttk.Treeview):
                return
            reg = str(w.identify_region(event.x, event.y)).lower()
            if reg != "separator":
                return
        except tk.TclError:
            return
        except Exception:
            return
        if w is None:
            return
        self._schedule_virtual_ui_save()

    def _build_active_holdings_tree(self, parent: ttk.Widget) -> None:
        """실시간 보유: 매매종료·주문과 동일한 Treeview 패턴 (스크롤·줄무늬·태그)."""
        try:
            cols = (
                "Entry",
                "Code",
                "Name",
                "Qty",
                "PnL",
                "PnLPct",
                "Eval",
                "BuyIn",
                "CurP",
                "AvgP",
                "DayChg",
                "WtPct",
            )
            raw_ui = self._read_virtual_ui_file()
            try:
                self._virtual_saved_sash_rel = self._parse_virtual_sash_rel(raw_ui)
            except Exception:
                pass
            wid_map = self._merge_virtual_col_widths(
                _VIRTUAL_ACTIVE_COLS,
                _DEFAULT_VIRTUAL_ACTIVE_COL_WIDTHS,
                raw_ui.get("columns_active"),
            )
            # Paned 상단 칸에서 LabelFrame 자식만 pack 할 때 세로 확장이 0에 가까워지는 경우 방지
            hold_wrap = ttk.Frame(parent)
            hold_wrap.pack(fill=tk.BOTH, expand=True)
            # --- 보유 전체 통계(수수료 제외): 종목 목록 위 · 컴팩트 한 줄 카드
            c = getattr(self.app, "colors", None) or {}
            card = str(c.get("card", "#3B4252"))
            border = str(c.get("border", "#4C566A"))
            fg = str(c.get("fg", "#ECEFF4"))
            sec = str(c.get("secondary", "#81A1C1"))

            agg_outer = ttk.Frame(hold_wrap)
            agg_outer.pack(side=tk.TOP, fill=tk.X, pady=(0, 6), padx=0)
            agg_card = tk.Frame(
                agg_outer,
                bg=card,
                highlightbackground=border,
                highlightthickness=1,
            )
            agg_card.pack(fill=tk.X)
            ipad = tk.Frame(agg_card, bg=card)
            ipad.pack(fill=tk.X, padx=8, pady=(4, 5))
            toggle_col = tk.Frame(ipad, bg=card)
            toggle_col.pack(side=tk.LEFT, padx=(2, 14), pady=(2, 0))
            self._build_holdings_fee_toggle(toggle_col, card, border, sec)
            metrics = tk.Frame(ipad, bg=card)
            metrics.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            stat_cols: Tuple[Tuple[str, str], ...] = (
                ("매입금액", "hold_sum_buy_ex"),
                ("평가금액", "hold_sum_eval_ex"),
                ("평가손익", "hold_sum_pnl_ex"),
                ("수익율", "hold_sum_pct_ex"),
            )
            for i, (lab_txt, vkey) in enumerate(stat_cols):
                gcol = i * 2
                cell = tk.Frame(metrics, bg=card)
                cell.grid(
                    row=0,
                    column=gcol,
                    sticky=tk.NSEW,
                    padx=(8 if i == 0 else 6, 8),
                )
                metrics.grid_columnconfigure(gcol, weight=1, uniform="hold_stat")
                tk.Label(
                    cell,
                    text=lab_txt,
                    font=("Malgun Gothic", 8),
                    bg=card,
                    fg=sec,
                ).pack(anchor=tk.CENTER)
                val_lbl = tk.Label(
                    cell,
                    textvariable=self.vars[vkey],
                    font=("Malgun Gothic", 10, "bold"),
                    bg=card,
                    fg=fg,
                )
                val_lbl.pack(anchor=tk.CENTER, pady=(1, 0))
                if vkey == "hold_sum_pnl_ex":
                    self._hold_agg_lbl_pnl = val_lbl
                elif vkey == "hold_sum_pct_ex":
                    self._hold_agg_lbl_pct = val_lbl
                if i < len(stat_cols) - 1:
                    sp = tk.Frame(metrics, width=1, bg=border)
                    sp.grid(row=0, column=gcol + 1, sticky=tk.NS, pady=3, padx=0)

            body = ttk.Frame(hold_wrap)
            body.pack(fill=tk.BOTH, expand=True)
            tree = ttk.Treeview(
                body, columns=cols, show="headings", selectmode="browse"
            )
            heads = [
                "진입일시",
                "코드",
                "종목명",
                "수량",
                "손익금",
                "수익율",
                "평가금액",
                "매입금액",
                "현재가",
                "평균단가",
                "전일대비",
                "보유비중",
            ]
            # 진입일시(Entry)는 중앙 정렬
            anchors = (
                tk.CENTER,
                tk.W,
                tk.W,
                tk.E,
                tk.E,
                tk.E,
                tk.E,
                tk.E,
                tk.E,
                tk.E,
                tk.E,
                tk.E,
            )
            for col, text, an in zip(cols, heads, anchors):
                tree.heading(col, text=text)
                try:
                    mw = int(_VIRTUAL_COL_MIN.get(col, 40))
                    cw = max(mw, int(wid_map.get(col, mw)))
                    tree.column(
                        col,
                        width=cw,
                        anchor=an,
                        minwidth=mw,
                        stretch=False,
                    )
                except Exception:
                    tree.column(col, width=72, anchor=an, minwidth=40, stretch=False)

            sb = ttk.Scrollbar(body, orient=tk.VERTICAL, command=tree.yview)
            tree.configure(yscrollcommand=sb.set)
            tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            sb.pack(side=tk.RIGHT, fill=tk.Y)
            tree.bind(
                "<ButtonRelease-1>", self._on_virtual_tree_column_drag_end, add="+"
            )
            self.tree_active = tree
        except Exception as e:
            try:
                print(f"[UI Error] _build_active_holdings_tree: {e}")
            except Exception:
                pass
            self.tree_active = None

    def _build_order_tree(self, parent):
        cols = (
            "Time",
            "Code",
            "Name",
            "Side",
            "Qty",
            "FQty",
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
            "주문수량",
            "체결수량",
            "주문가",
            "체결가",
            "VP",
            "호가",
            "상태",
            "사유",
        ]
        try:
            raw_ui = self._read_virtual_ui_file()
            if getattr(self, "_virtual_saved_sash_rel", None) is None:
                self._virtual_saved_sash_rel = self._parse_virtual_sash_rel(raw_ui)
            wid_map = self._merge_virtual_col_widths(
                _VIRTUAL_ORDER_COLS,
                _DEFAULT_VIRTUAL_ORDER_COL_WIDTHS,
                raw_ui.get("columns_orders"),
            )
        except Exception:
            wid_map = {
                c: int(_DEFAULT_VIRTUAL_ORDER_COL_WIDTHS.get(c, 88))
                for c in _VIRTUAL_ORDER_COLS
            }
        for col, text in zip(cols, display_names):
            tree.heading(col, text=text)
            try:
                mw = int(_VIRTUAL_COL_MIN.get(col, 40))
                w = max(mw, int(wid_map.get(col, mw)))
                tree.column(
                    col, width=w, anchor=tk.CENTER, minwidth=mw, stretch=False
                )
            except Exception:
                tree.column(
                    col, width=88, anchor=tk.CENTER, minwidth=40, stretch=False
                )
        try:
            mw_nm = int(_VIRTUAL_COL_MIN.get("Name", 40))
            tree.column(
                "Name",
                width=max(mw_nm, int(wid_map.get("Name", 100))),
                anchor=tk.W,
                minwidth=mw_nm,
                stretch=False,
            )
            for c in ("Qty", "FQty", "OrderP", "FilledP", "VPOrd", "BAOrd"):
                mw_c = int(_VIRTUAL_COL_MIN.get(c, 40))
                tree.column(
                    c,
                    width=max(mw_c, int(wid_map.get(c, mw_c))),
                    anchor=tk.E,
                    minwidth=mw_c,
                    stretch=False,
                )
            mw_rs = int(_VIRTUAL_COL_MIN.get("Reason", 40))
            tree.column(
                "Reason",
                width=max(mw_rs, int(wid_map.get("Reason", 120))),
                anchor=tk.CENTER,
                minwidth=mw_rs,
                stretch=False,
            )
        except Exception:
            pass

        tree.bind("<ButtonRelease-1>", self._on_virtual_tree_column_drag_end, add="+")

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
        주문·체결 통합: 상단 제목, 그 아래 한 줄에 구분·체결 필터(왼쪽) + 날짜 네비(오른쪽), 트리.
        LabelFrame 라벨 슬롯(labelwidget)은 가로로 늘어나지 않아 본문 상단 프레임에 배치한다.
        """
        # text 비움: labelwidget은 라벨 슬롯만 차지해 가로로 늘어나지 않음.
        order_f = ttk.LabelFrame(paned_parent, padding="5", text="")
        try:
            hdr = ttk.Frame(order_f)
            hdr.pack(fill=tk.X)
            # 매매 종료 내역 등 상단 LabelFrame 제목과 동일: TLabelframe.Label (styles.py)
            ttk.Label(
                hdr,
                text=f" 📋 주문·체결 내역 · {mode_label} ",
                style="TLabelframe.Label",
            ).pack(side=tk.LEFT)

            filt = ttk.Frame(order_f)
            filt.pack(fill=tk.X, pady=(4, 2))
            # 오른쪽 고정을 위해 날짜 네비를 먼저 pack(RIGHT)
            self._build_order_log_date_nav_compact(filt)

            filt_left = ttk.Frame(filt)
            filt_left.pack(side=tk.LEFT, fill=tk.X, expand=True)

            ttk.Label(filt_left, text="구분", font=("Malgun Gothic", 9)).pack(
                side=tk.LEFT, padx=(0, 4)
            )
            self._order_filter_side_var = tk.StringVar(value="전체")
            self._order_side_segment_btns = {}
            self._build_order_side_segment(filt_left)
            ttk.Label(filt_left, text="체결", font=("Malgun Gothic", 9)).pack(
                side=tk.LEFT, padx=(0, 4)
            )
            self._order_filter_fill_var = tk.StringVar(value="전체")
            cb_fill = ttk.Combobox(
                filt_left,
                textvariable=self._order_filter_fill_var,
                values=("전체", "체결", "미체결"),
                state="readonly",
                width=7,
                font=("Malgun Gothic", 9),
            )
            cb_fill.pack(side=tk.LEFT, padx=(0, 8))
            cb_fill.bind("<<ComboboxSelected>>", self._on_order_filter_changed)
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

    def _apply_order_side_segment_styles(self, selected: str) -> None:
        """구분 세그먼트: 선택된 항목만 강조, 나머지는 카드 톤."""
        try:
            c = getattr(self.app, "colors", None) or {}
            bg_dark = str(c.get("bg", "#2E3440"))
            card = str(c.get("card", "#3B4252"))
            fg = str(c.get("fg", "#ECEFF4"))
            primary = str(c.get("primary", "#88C0D0"))
            hover = "#434C5E"
            for label, btn in self._order_side_segment_btns.items():
                if not btn:
                    continue
                is_on = label == selected
                if is_on:
                    btn.configure(
                        bg=primary,
                        fg=bg_dark,
                        activebackground=primary,
                        activeforeground=bg_dark,
                    )
                else:
                    btn.configure(
                        bg=card,
                        fg=fg,
                        activebackground=hover,
                        activeforeground=fg,
                    )
        except Exception as e:
            try:
                print(f"[UI Error] _apply_order_side_segment_styles: {e}")
            except Exception:
                pass

    def _on_order_side_segment(self, label: str) -> None:
        """구분 세그먼트 클릭: StringVar 갱신 후 주문 트리 재렌더."""
        try:
            if self._order_filter_side_var is not None:
                self._order_filter_side_var.set(label)
            self._apply_order_side_segment_styles(label)
            self._on_order_filter_changed()
        except Exception as e:
            try:
                print(f"[UI Error] _on_order_side_segment: {e}")
            except Exception:
                pass

    def _build_order_side_segment(self, filt: ttk.Frame) -> None:
        """
        전체/매수/매도 필터를 iOS 스타일 세그먼트 컨트롤처럼 한 줄로 배치합니다.
        (ttk에 네이티브 세그먼트가 없어 tk.Button + 테마색으로 구현)
        """
        try:
            c = getattr(self.app, "colors", None) or {}
            border_col = str(c.get("border", "#4C566A"))

            outer = tk.Frame(filt, bg=border_col, padx=1, pady=1)
            outer.pack(side=tk.LEFT, padx=(0, 14))
            row = tk.Frame(outer, bg=border_col)
            row.pack()

            labels: Tuple[str, ...] = ("전체", "매수", "매도")
            self._order_side_segment_btns = {}

            for idx, lab in enumerate(labels):
                if idx > 0:
                    sep = tk.Frame(row, width=1, bg=border_col)
                    sep.pack(side=tk.LEFT, fill=tk.Y)

                btn = tk.Button(
                    row,
                    text=lab,
                    font=("Malgun Gothic", 9),
                    relief=tk.FLAT,
                    borderwidth=0,
                    padx=10,
                    pady=3,
                    cursor="hand2",
                    takefocus=0,
                    command=lambda l=lab: self._on_order_side_segment(l),
                )
                btn.pack(side=tk.LEFT)
                self._order_side_segment_btns[lab] = btn

            cur = (
                self._order_filter_side_var.get()
                if self._order_filter_side_var
                else "전체"
            )
            if cur not in labels:
                cur = "전체"
                if self._order_filter_side_var is not None:
                    self._order_filter_side_var.set(cur)
            self._apply_order_side_segment_styles(cur)
        except Exception as e:
            try:
                print(f"[UI Error] _build_order_side_segment: {e}")
            except Exception:
                pass
            # 폴백: 최소한 변수는 유지
            try:
                if self._order_filter_side_var is None:
                    self._order_filter_side_var = tk.StringVar(value="전체")
            except Exception:
                pass

    def _build_order_log_date_nav_compact(self, parent: ttk.Frame) -> None:
        """
        날짜 이전·표시·다음·오늘을 구분 세그먼트와 같은 테두리 스타일로 한 덩어리에 둡니다.
        필터 행 오른쪽에 배치합니다(pack side=RIGHT는 호출부에서 처리).
        """
        try:
            c = getattr(self.app, "colors", None) or {}
            border_col = str(c.get("border", "#4C566A"))
            card = str(c.get("card", "#3B4252"))
            fg = str(c.get("fg", "#ECEFF4"))
            hover = "#434C5E"

            if self._order_log_date_var is None:
                self._order_log_date_var = tk.StringVar(value="")

            outer = tk.Frame(parent, bg=border_col, padx=1, pady=1)
            outer.pack(side=tk.RIGHT, padx=(12, 0))
            row = tk.Frame(outer, bg=border_col)
            row.pack()

            def _nav_btn(text: str, cmd: Callable[[], None]) -> tk.Button:
                return tk.Button(
                    row,
                    text=text,
                    font=("Malgun Gothic", 9),
                    relief=tk.FLAT,
                    borderwidth=0,
                    padx=8,
                    pady=3,
                    cursor="hand2",
                    takefocus=0,
                    command=cmd,
                    bg=card,
                    fg=fg,
                    activebackground=hover,
                    activeforeground=fg,
                )

            self._btn_order_log_prev = _nav_btn("◀", self._order_log_prev_day)
            self._btn_order_log_prev.pack(side=tk.LEFT)

            sep1 = tk.Frame(row, width=1, bg=border_col)
            sep1.pack(side=tk.LEFT, fill=tk.Y)

            tk.Label(
                row,
                textvariable=self._order_log_date_var,
                font=("Malgun Gothic", 9),
                bg=card,
                fg=fg,
                padx=8,
                pady=3,
                width=20,
                anchor="center",
            ).pack(side=tk.LEFT)

            sep2 = tk.Frame(row, width=1, bg=border_col)
            sep2.pack(side=tk.LEFT, fill=tk.Y)

            self._btn_order_log_next = _nav_btn("▶", self._order_log_next_day)
            self._btn_order_log_next.pack(side=tk.LEFT)

            sep3 = tk.Frame(row, width=1, bg=border_col)
            sep3.pack(side=tk.LEFT, fill=tk.Y)

            self._btn_order_log_today = _nav_btn("오늘", self._order_log_today)
            self._btn_order_log_today.pack(side=tk.LEFT)
        except Exception as e:
            try:
                print(f"[UI Error] _build_order_log_date_nav_compact: {e}")
            except Exception:
                pass

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
        """
        view_d 다음으로 이동할 날짜(오늘 이하).

        우선 DB에 주문이 있는 날 중 가장 이른 날을 고릅니다. 그런 날이 없고
        아직 ``view_d < 오늘``이면 **당일 주문이 없어도** 오늘로 이동할 수 있게
        오늘을 반환합니다(빈 목록 조회·「오늘」 버튼과 동일 목적).
        """
        try:
            today = date.today()
            if view_d >= today:
                return None
            ds = self._order_log_calendar_dates_with_orders()
            newer = [x for x in ds if view_d < x <= today]
            if newer:
                return min(newer)
            # 그 사이에 주문 일자가 없으면(예: 어제까지 거래 후 오늘은 무거래) 화살표로 오늘 복귀
            return today
        except Exception:
            return None

    def _sync_order_log_date_label(self) -> None:
        """선택 날짜 표시 및 이전/다음 활성화(이전은 주문일만, 다음은 필요 시 무거래 오늘 포함)."""
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
                    state=(tk.NORMAL if can_prev else tk.DISABLED)
                )
            if self._btn_order_log_next is not None:
                self._btn_order_log_next.config(
                    state=(tk.NORMAL if can_next else tk.DISABLED)
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
        """주문 로그: 다음 일자(주문일 우선, 없으면 무거래 오늘로 한 단계 이동)."""
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
    def _order_log_status_display(result: object) -> str:
        """주문 로그 '상태': 체결 완료는 사용자 표기 '성공'."""
        try:
            s = str(result or "").strip()
        except Exception:
            return "-"
        su = s.upper()
        # result 원천은 '주문 접수(SUCCESS)' 또는 '체결 완료(FILLED)'가 섞여 들어올 수 있음.
        # UI에서는 두 의미를 구분해 표기합니다.
        if su == "FILLED":
            return "성공"
        if su in ("CANCELLED", "CANCELED", "CANCEL", "CNCL"):
            return "취소(미체결)"
        if su == "SUCCESS":
            return "접수"
        return s

    @staticmethod
    def _normalize_order_side_raw(side_raw: object) -> Literal["buy", "sell", "unknown"]:
        """DB·코어에서 오는 side 값을 buy/sell로 정규화(필터·표시용)."""
        try:
            s = str(side_raw or "").strip().lower()
        except Exception:
            return "unknown"
        if s in ("buy", "b", "매수", "bid"):
            return "buy"
        if s in ("sell", "s", "매도", "ask"):
            return "sell"
        return "unknown"

    @staticmethod
    def _order_log_row_is_filled(result: object, f_price: object, f_qty: object) -> bool:
        """
        체결 여부: result가 FILLED이거나 체결 수량이 1주 이상이면 체결로 간주합니다.
        (부분체결·레거시 행도 필터에서 빠지지 않도록 수량 우선)
        """
        try:
            if str(result or "").strip().upper() == "FILLED":
                return True
        except Exception:
            pass
        try:
            if int(f_qty or 0) > 0:
                return True
        except (TypeError, ValueError):
            pass
        return False

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
            # 호가 실패 시 전일 종가(ka10001·stkinfo) — 장 전·호가 비어 있을 때 표시용
            fn_pc = getattr(om, "fetch_stock_prev_close_ka10001", None) if om is not None else None
            if callable(fn_pc):
                try:
                    pc = fn_pc(nk, silent=True)
                    if pc is not None:
                        v2 = float(pc)
                        if v2 > 0:
                            self._fallback_quote_cache[nk] = (now_m, v2)
                            return v2
                except Exception:
                    pass
        except Exception:
            pass
        return 0.0

    def _monitor_live_price(self, nk: str, pos: Dict[str, Any]) -> float:
        """
        6자리 종목코드 기준 현재가(청산 가정 시 단가).
        틱 ``price`` → ``last_price``(평단 미러 제외) → REST 호가·전일 순. **평단 폴백 없음.**
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
        # 보유 전용 스텁(vp_price_stub): 틱이 없을 때 last_price 시드 우선.
        # 다만 시드값이 평단과 동일하면(또는 브로커가 평단만 준 경우) 현재가로 취급하지 않음.
        if stub_flag:
            try:
                raw_lp = info.get("last_price")
                if raw_lp not in (None, "", 0):
                    v = float(raw_lp)
                    if v > 0:
                        if (
                            ap > 0
                            and abs(v - ap) < 0.501
                            and "price" not in info
                        ):
                            pass
                        else:
                            return v
            except (TypeError, ValueError):
                pass
        else:
            try:
                raw_lp = info.get("last_price")
                if raw_lp not in (None, "", 0):
                    v = float(raw_lp)
                    if v > 0:
                        # 구버전 스텁: last만 평단이고 틱 캐시 price 없음 → 실시간으로 보지 않음
                        if (
                            ap > 0
                            and abs(v - ap) < 0.501
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
        # 평단은 '현재가'로 쓰지 않음(미시세·스텁과 구분 불가 → 동일 숫자 오해)
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

    def _unrealized_vs_cost_ex_buy_at_price(
        self, curr_p: float, qty: int, avg_price: float
    ) -> float:
        """
        계좌 「미실현 손익」용: 순매각 − (평단×수량). 매입수수료는 매입 원가(cost)에서 제외.
        """
        try:
            return float(
                unrealized_at_price_cost_ex_buy_net_of_sell_fee(
                    float(curr_p),
                    int(qty),
                    float(avg_price),
                    self.app.settings,
                )
            )
        except Exception:
            return 0.0

    def _unrealized_at_price_net_of_sell_fee(
        self,
        pos: Dict[str, Any],
        curr_p: float,
        qty: int,
        avg_price: float,
    ) -> float:
        """
        현재가(전량 매도 가정) 기준 미실현 손익(원).
        **순매각(매도 수수료 차감) − invested(매수 수수료 포함)**. 평가열은 invested+미실현=순매각.
        """
        try:
            return float(
                unrealized_at_price_net_of_sell_fee(
                    pos,
                    float(curr_p),
                    int(qty),
                    float(avg_price),
                    self.app.settings,
                )
            )
        except Exception:
            return 0.0

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

    @staticmethod
    def _fmt_active_vs_prev(info: Dict[str, Any]) -> str:
        """전일 종가 대비 등락폭(원). monitor_info['diff']와 감시 탭과 동일 규칙."""
        try:
            raw = info.get("diff")
            if raw is None:
                return "—"
            s = str(raw).strip().replace(",", "")
            if s in ("", "-", "—"):
                return "—"
            iv = int(round(float(s)))
            if iv == 0:
                return "0"
            return f"{iv:+,}"
        except Exception:
            return "—"

    def _resolve_active_holdings_prices(
        self, nk: str, pos: Dict[str, Any], info: Dict[str, Any]
    ) -> Tuple[float, int, float, float, int]:
        """
        실시간 보유 1건의 가격·금액(트리 표시와 동일 규칙).

        반환
        ----
        (ap, qty, curr_p, eval_amt, buy_in_display)
            ``buy_in_display``: 트리 「매입금액」열(잔고 ka_pur 사용 시 수수료 포함일 수 있음).
            수수료 **제외** 매입원은 항상 ``ap * qty``(아래 ``_holding_cost_ex_fee``).
        """
        try:
            live_p = float(self._monitor_live_price(nk, pos))
        except (TypeError, ValueError):
            live_p = 0.0
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
            # 잔고 스냅만 있고 감시 틱이 없을 때: ka_cur 는 전일 종가만 주는 경우가 많아
            # _monitor_live_price(호가·stkinfo·스텭 last_price)가 있으면 표시용 현재가는 그쪽을 우선한다.
            curr_p = 0.0
            if live_p > 0:
                curr_p = live_p
            if curr_p <= 0 and ka_cur > 0 and (ap <= 0 or abs(ka_cur - ap) >= 0.501):
                curr_p = ka_cur
            if curr_p <= 0 and ka_ev > 0 and qty > 0:
                try:
                    impl = ka_ev / float(qty)
                except (TypeError, ValueError, ZeroDivisionError):
                    impl = 0.0
                if impl > 0 and (ap <= 0 or abs(impl - ap) >= 0.501):
                    curr_p = impl
            if curr_p <= 0:
                curr_p = live_p if live_p > 0 else 0.0
            # 평가금액은 순수 시가(현재가×수량). ka_ev(evlu_amt)는 증권사 정의·스냅샷에
            # 매입·제비용이 섞인 경우가 있어 현재가가 살아 있으면 곱셈 결과를 우선합니다.
            if qty > 0 and curr_p > 1e-9:
                eval_amt = float(curr_p) * float(qty)
            elif ka_ev > 0:
                eval_amt = ka_ev
            else:
                eval_amt = float(curr_p) * float(qty)
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

        return (float(ap), int(qty), float(curr_p), float(eval_amt), int(buy_in_amt))

    @staticmethod
    def _holding_cost_ex_fee(ap: float, qty: int) -> float:
        """매수 수수료를 제외한 매입금액(평균단가×수량)."""
        try:
            if qty <= 0:
                return 0.0
            a = float(ap)
            if abs(a) <= 1e-12:
                return 0.0
            return float(a) * float(qty)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _finite_tree_float(x: object, fallback: float = 0.0) -> float:
        """Treeview 숫자 표시용: nan·inf·변환 실패 시 fallback."""
        try:
            v = float(x)
        except (TypeError, ValueError):
            return float(fallback)
        if not math.isfinite(v):
            return float(fallback)
        return float(v)

    def _build_holdings_fee_toggle(
        self,
        parent: tk.Widget,
        card: str,
        border: str,
        sec: str,
    ) -> None:
        """보유 합계 카드 왼쪽: 수수료반영 슬라이드 스위치(기본 OFF)."""
        try:
            bg = card
            wrap = tk.Frame(parent, bg=bg)
            wrap.pack(anchor=tk.N)
            tk.Label(
                wrap,
                text="수수료반영",
                font=("Malgun Gothic", 8),
                bg=bg,
                fg=sec,
            ).pack(anchor=tk.CENTER)
            cv = tk.Canvas(
                wrap,
                width=48,
                height=26,
                bg=bg,
                highlightthickness=0,
                bd=0,
                cursor="hand2",
            )
            cv.pack(anchor=tk.CENTER, pady=(4, 0))
            self._hold_fee_toggle_cv = cv

            def _on_click(_event: tk.Event) -> None:
                try:
                    v = self.vars.get("hold_agg_fee_reflect")
                    if v is None:
                        return
                    v.set(not bool(v.get()))
                    # 슬라이더는 즉시 그림 → 메인 스레드가 바로 돌아가 체감 반응 개선
                    self._redraw_holdings_fee_toggle_canvas()
                    try:
                        cv.update_idletasks()
                    except (tk.TclError, AttributeError):
                        pass
                    self._schedule_holdings_fee_toggle_apply()
                except (tk.TclError, AttributeError) as e:
                    try:
                        print(f"[UI] 수수료반영 토글 오류: {e}")
                    except Exception:
                        pass

            cv.bind("<Button-1>", _on_click)
            self._redraw_holdings_fee_toggle_canvas()
        except Exception as e:
            try:
                print(f"[UI Error] _build_holdings_fee_toggle: {e}")
            except Exception:
                pass
            self._hold_fee_toggle_cv = None

    def _redraw_holdings_fee_toggle_canvas(self) -> None:
        """외부에서 값이 바뀐 경우 등 토글 캔버스만 동기화."""
        cv = getattr(self, "_hold_fee_toggle_cv", None)
        if cv is None:
            return
        try:
            cv.delete("all")
            v = self.vars.get("hold_agg_fee_reflect")
            on = bool(v is not None and v.get())
            cset = getattr(self.app, "colors", None) or {}
            border = str(cset.get("border", "#4C566A"))
            track_off = str(cset.get("border", "#4C566A"))
            track_on = str(cset.get("secondary", "#81A1C1"))
            knob = str(cset.get("fg", "#ECEFF4"))
            track_fill = track_on if on else track_off
            cv.create_rectangle(4, 9, 44, 17, fill=track_fill, outline=track_fill)
            cx = 34 if on else 14
            cv.create_oval(cx - 9, 5, cx + 9, 21, fill=knob, outline=border, width=1)
        except tk.TclError:
            pass

    def _holdings_agg_fee_reflect_on(self) -> bool:
        """보유 합계·종목행 표시: 수수료반영 토글 ON 여부."""
        try:
            bv = self.vars.get("hold_agg_fee_reflect")
            return bool(bv is not None and bv.get())
        except (tk.TclError, TypeError, AttributeError):
            return False

    def _apply_holdings_aggregate_display(self, s_den: float, s_eval: float, s_pnl: float) -> None:
        """합계 카드 숫자·손익 색·토글 캔버스까지 반영."""
        if s_den > 1e-6:
            s_pct = (s_pnl / s_den) * 100.0
        else:
            s_pct = 0.0

        buy_disp = int(round(s_den))
        self.vars["hold_sum_buy_ex"].set(f"{buy_disp:,}")
        self.vars["hold_sum_eval_ex"].set(f"{int(round(s_eval)):,}")
        self.vars["hold_sum_pnl_ex"].set(f"{int(round(s_pnl)):+,}")
        self.vars["hold_sum_pct_ex"].set(f"{s_pct:+.2f}%")

        self._redraw_holdings_fee_toggle_canvas()

        c2 = getattr(self.app, "colors", None) or {}
        base_fg = str(c2.get("fg", "#ECEFF4"))
        ok_c = str(c2.get("success", "#A3BE8C"))
        ng_c = str(c2.get("danger", "#BF616A"))
        lp = getattr(self, "_hold_agg_lbl_pnl", None)
        lr = getattr(self, "_hold_agg_lbl_pct", None)
        if lp is not None and lr is not None:
            if s_pnl > 0.5:
                col = ok_c
            elif s_pnl < -0.5:
                col = ng_c
            else:
                col = base_fg
            try:
                lp.config(foreground=col)
                lr.config(foreground=col)
            except tk.TclError:
                pass

    def _holdings_full_row_cache(
        self,
    ) -> List[Tuple[str, Dict[str, Any], Dict[str, Any], Tuple[float, int, float, float, int], float]]:
        """
        보유 1회 스캔: 시세 resolve + 미실현(매도수수료 반영)만 계산.
        토글 시 합계·트리에서 동일 값을 재사용해 중복 연산을 줄인다.
        """
        mi = getattr(self.app, "monitor_info", None) or {}
        out: List[
            Tuple[str, Dict[str, Any], Dict[str, Any], Tuple[float, int, float, float, int], float]
        ] = []
        for nk, pos in self._sorted_active_holdings():
            info = mi.get(nk, {}) or {}
            try:
                ap, qty, curr_p, eval_amt, bi = self._resolve_active_holdings_prices(nk, pos, info)
                ur = self._finite_tree_float(
                    self._unrealized_at_price_net_of_sell_fee(pos, float(curr_p), qty, ap)
                )
            except Exception:
                try:
                    ap, qty, curr_p, eval_amt, bi = self._resolve_active_holdings_prices(
                        nk, pos, info
                    )
                except Exception:
                    continue
                ur = 0.0
            out.append((nk, pos, info, (ap, qty, curr_p, eval_amt, bi), ur))
        return out

    def _update_holdings_aggregate_fee_excluded(self) -> None:
        """보유 합계 카드: OFF=수수료 미반영, ON=매입 invested·평가 순매각(매도수수료만 차감)."""
        try:
            fee_on = self._holdings_agg_fee_reflect_on()

            mi = getattr(self.app, "monitor_info", None) or {}
            s_eval = 0.0
            s_pnl = 0.0
            s_den = 0.0  # 수익률 분모(토글에 따라 매입 정의)

            if not fee_on:
                s_den = 0.0
                for nk, pos in self._sorted_active_holdings():
                    info = mi.get(nk, {}) or {}
                    ap, qty, _cp, eval_amt, _bi = self._resolve_active_holdings_prices(
                        nk, pos, info
                    )
                    s_den += self._holding_cost_ex_fee(ap, qty)
                    s_eval += float(eval_amt)
                s_pnl = s_eval - s_den
            else:
                for nk, pos in self._sorted_active_holdings():
                    info = mi.get(nk, {}) or {}
                    ap, qty, curr_p, eval_amt, _bi = self._resolve_active_holdings_prices(
                        nk, pos, info
                    )
                    try:
                        inv = float(pos.get("invested", 0) or 0)
                    except (TypeError, ValueError):
                        inv = 0.0
                    s_den += inv
                    try:
                        ur = float(
                            self._unrealized_at_price_net_of_sell_fee(
                                pos, float(curr_p), int(qty), float(ap)
                            )
                        )
                    except (TypeError, ValueError):
                        ur = 0.0
                    s_pnl += ur
                    s_eval += inv + ur

            self._apply_holdings_aggregate_display(s_den, s_eval, s_pnl)
        except Exception:
            try:
                self.vars["hold_sum_buy_ex"].set("0")
                self.vars["hold_sum_eval_ex"].set("0")
                self.vars["hold_sum_pnl_ex"].set("0")
                self.vars["hold_sum_pct_ex"].set("0.00%")
                c2 = getattr(self.app, "colors", None) or {}
                base_fg = str(c2.get("fg", "#ECEFF4"))
                for w in (getattr(self, "_hold_agg_lbl_pnl", None), getattr(self, "_hold_agg_lbl_pct", None)):
                    if w is not None:
                        try:
                            w.config(foreground=base_fg)
                        except tk.TclError:
                            pass
            except Exception:
                pass

    def _schedule_holdings_fee_toggle_apply(self) -> None:
        """합계·트리 갱신을 다음 이벤트 루프로 미룸(연속 클릭 시 직전 예약 취소)."""
        try:
            aid = getattr(self, "_hold_fee_toggle_after_id", None)
            if aid is not None:
                try:
                    self.parent.after_cancel(aid)
                except (tk.TclError, ValueError, AttributeError):
                    pass
                self._hold_fee_toggle_after_id = None
            self._hold_fee_toggle_after_id = self.parent.after(
                0, self._deferred_holdings_fee_toggle_apply
            )
        except Exception:
            try:
                self._apply_holdings_fee_toggle_fast()
            except Exception:
                pass

    def _deferred_holdings_fee_toggle_apply(self) -> None:
        """after 콜백: 예약 id 정리 후 실제 계산·트리 반영."""
        try:
            self._hold_fee_toggle_after_id = None
        except Exception:
            pass
        try:
            self._apply_holdings_fee_toggle_fast()
        except Exception:
            pass

    def _apply_holdings_fee_toggle_fast(self) -> None:
        """수수료 토글: 1회 스캔으로 합계+트리 갱신(중복 resolve/미실현 제거)."""
        try:
            rows = self._holdings_full_row_cache()
            fee_on = self._holdings_agg_fee_reflect_on()
            s_eval = 0.0
            s_pnl = 0.0
            s_den = 0.0
            if not fee_on:
                for _nk, pos, _info, res, _ur in rows:
                    ap, qty, _cp, eval_amt, _bi = res
                    s_den += self._holding_cost_ex_fee(ap, qty)
                    s_eval += float(eval_amt)
                s_pnl = s_eval - s_den
            else:
                for _nk, pos, _info, res, ur in rows:
                    ap, qty, _cp, _eval_amt, _bi = res
                    try:
                        inv = float(pos.get("invested", 0) or 0)
                    except (TypeError, ValueError):
                        inv = 0.0
                    s_den += inv
                    s_pnl += ur
                    s_eval += inv + ur

            self._apply_holdings_aggregate_display(s_den, s_eval, s_pnl)

            if not self._active_tree_ready():
                return

            total_eval_weight = 0.0
            for _nk, _pos, _info, res, _ur in rows:
                total_eval_weight += self._finite_tree_float(float(res[3]))

            valid_nks = {r[0] for r in rows}
            for idx, (nk, pos, info, res, ur) in enumerate(rows):
                vals, row_tags, _, _, _, _ = self._active_row_vals_and_tags(
                    idx,
                    nk,
                    pos,
                    info,
                    total_eval_weight,
                    _resolved=res,
                    _ur_fee_incl=ur,
                )
                self._upsert_active_tree_row(nk, vals, row_tags)

            tr = self.tree_active
            if tr is not None and valid_nks:
                try:
                    for cid in list(tr.get_children()):
                        if cid not in valid_nks:
                            self._remove_active_tree_row(cid)
                except Exception as e:
                    try:
                        print(f"[UI] 보유 트리 고아 행 제거 실패: {e}")
                    except Exception:
                        pass
            self._reorder_active_tree_rows()
        except Exception as e:
            try:
                print(f"[UI] 빠른 수수료 토글 실패, 일반 경로로 폴백: {e}")
            except Exception:
                pass
            try:
                self._update_holdings_aggregate_fee_excluded()
                self._refresh_active_holdings_tree_rows()
            except Exception:
                pass

    def _active_row_vals_and_tags(
        self,
        row_idx: int,
        nk: str,
        pos: Dict[str, Any],
        info: Dict[str, Any],
        total_eval_for_weight: Optional[float] = None,
        *,
        _resolved: Optional[Tuple[float, int, float, float, int]] = None,
        _ur_fee_incl: Optional[float] = None,
    ) -> Tuple[tuple, Tuple[str, ...], float, float, float, float]:
        """
        실시간 보유 한 행.

        반환
        ----
        (vals, tags, profit_pct_for_tag, unrealized_for_tag, eval_amt_gross_for_weight, unrealized_fee_included)
            - ``hold_agg_fee_reflect`` OFF: 매입=평단×수량, 평가=시장평가액, 손익·% = 수수료 미반영.
            - ON: 매입=invested(매수수수료 포함), 평가=순매각(매도수수료만 차감), 손익·% = invested 대비.
            ``eval_amt_gross_for_weight``: 보유비중 분모용(항상 시장 평가액 합 기준).
            ``unrealized_fee_included``: 상단 계좌 요약 합산용(변경 없음).
        `total_eval_for_weight`: 보유비중(%) 분모. None이면 해당 열은 '—'.
        `_resolved` / `_ur_fee_incl`: 토글 등에서 이미 계산한 시세·미실현을 넘기면 중복 연산 생략.
        """
        if _resolved is not None:
            ap, qty, curr_p, eval_amt, _buy_in_amt = _resolved
        else:
            ap, qty, curr_p, eval_amt, _buy_in_amt = self._resolve_active_holdings_prices(
                nk, pos, info
            )

        if _ur_fee_incl is not None:
            unrealized_fee_included = self._finite_tree_float(_ur_fee_incl)
        else:
            unrealized_fee_included = self._finite_tree_float(
                self._unrealized_at_price_net_of_sell_fee(pos, float(curr_p), qty, ap)
            )
        cost_ex = self._holding_cost_ex_fee(ap, qty)
        try:
            inv = float(pos.get("invested", 0) or 0)
        except (TypeError, ValueError):
            inv = 0.0

        gross_eval = self._finite_tree_float(float(eval_amt))
        fee_on = self._holdings_agg_fee_reflect_on()
        if not fee_on:
            unrealized_row = gross_eval - float(cost_ex)
            buy_disp = int(round(float(cost_ex)))
            eval_disp = int(round(gross_eval))
            if cost_ex > 1e-6:
                profit_pct_row = (unrealized_row / cost_ex) * 100.0
            elif ap > 0 and float(curr_p) > 0:
                profit_pct_row = (float(curr_p) / float(ap) - 1.0) * 100.0
            else:
                profit_pct_row = 0.0
        else:
            unrealized_row = float(unrealized_fee_included)
            buy_disp = int(round(inv)) if inv > 1e-6 else int(round(float(cost_ex)))
            eval_disp = int(round(inv + unrealized_row))
            if inv > 1e-6:
                profit_pct_row = (unrealized_row / inv) * 100.0
            elif cost_ex > 1e-6:
                profit_pct_row = (unrealized_row / float(cost_ex)) * 100.0
            elif ap > 0 and float(curr_p) > 0:
                profit_pct_row = (float(curr_p) / float(ap) - 1.0) * 100.0
            else:
                profit_pct_row = 0.0

        unrealized_row = self._finite_tree_float(unrealized_row)
        profit_pct_row = self._finite_tree_float(profit_pct_row)
        p_str = f"{profit_pct_row:+.2f}%"
        entry_disp = self._format_entry_datetime(pos.get("entry_time", ""))
        tag_base = "even" if row_idx % 2 == 0 else "odd"
        p_tag = (
            "price_up"
            if unrealized_row > 0
            else ("price_down" if unrealized_row < 0 else "")
        )
        # name 키가 "" 로 있으면 get(name, nk) 가 빈칸이 되므로 종목코드로 폴백
        try:
            raw_nm = pos.get("name")
            nm = (str(raw_nm).strip() if raw_nm is not None else "") or str(nk)
        except Exception:
            nm = str(nk)
        day_chg_disp = self._fmt_active_vs_prev(info)
        try:
            te = float(total_eval_for_weight) if total_eval_for_weight is not None else 0.0
        except (TypeError, ValueError):
            te = 0.0
        if not math.isfinite(te):
            te = 0.0
        if total_eval_for_weight is None or te <= 1e-9:
            wt_disp = "—"
        else:
            try:
                ratio = (gross_eval / te) * 100.0
                if not math.isfinite(ratio):
                    wt_disp = "—"
                else:
                    wt_disp = f"{ratio:.2f}%"
            except Exception:
                wt_disp = "—"
        vals = (
            entry_disp,
            nk,
            nm,
            f"{qty:,}",
            f"{int(round(unrealized_row)):+,}",
            p_str,
            f"{eval_disp:,}",
            f"{buy_disp:,}",
            f"{curr_p:,.0f}" if float(curr_p) > 1e-9 else "—",
            f"{ap:,.0f}",
            day_chg_disp,
            wt_disp,
        )
        return (
            vals,
            (tag_base, p_tag),
            float(profit_pct_row),
            float(unrealized_row),
            float(gross_eval),
            float(unrealized_fee_included),
        )

    def _refresh_active_holdings_tree_rows(self) -> None:
        """수수료반영 토글 등 표시 규칙만 바뀐 뒤 보유 트리 열 값을 즉시 다시 채웁니다."""
        if not self._active_tree_ready():
            return
        try:
            mi = getattr(self.app, "monitor_info", None) or {}
            holdings = self._sorted_active_holdings()
            valid_nks = {nk for nk, _ in holdings}
            total_eval_weight = 0.0
            for idx, (nk, pos) in enumerate(holdings):
                try:
                    info = mi.get(nk, {}) or {}
                    _va, _tg, _pc, _ur, ev_part, _un = self._active_row_vals_and_tags(
                        idx, nk, pos, info, None
                    )
                    total_eval_weight += float(ev_part)
                except Exception as e:
                    try:
                        print(f"[UI] 보유 가중치 계산 스킵({nk}): {e}")
                    except Exception:
                        pass
            for idx, (nk, pos) in enumerate(holdings):
                try:
                    info = mi.get(nk, {}) or {}
                    vals, row_tags, _, _, _, _ = self._active_row_vals_and_tags(
                        idx, nk, pos, info, total_eval_weight
                    )
                    self._upsert_active_tree_row(nk, vals, row_tags)
                except Exception as e:
                    try:
                        print(f"[UI] 보유 행 갱신 스킵({nk}): {e}")
                    except Exception:
                        pass
            tr = self.tree_active
            # 정규화 전 iid·중복 삽입 등으로 남은 고아 행이 있으면 move 순서가 밀려
            # 특정 슬롯이 비어 보이거나 다른 종목이 끼어 보일 수 있어 제거한다.
            if tr is not None and valid_nks:
                try:
                    for cid in list(tr.get_children()):
                        if cid not in valid_nks:
                            self._remove_active_tree_row(cid)
                except Exception as e:
                    try:
                        print(f"[UI] 보유 트리 고아 행 제거 실패: {e}")
                    except Exception:
                        pass
            self._reorder_active_tree_rows()
        except Exception as e:
            try:
                print(f"[UI Error] _refresh_active_holdings_tree_rows: {e}")
            except Exception:
                pass

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
            try:
                if list(tr.get_children()) == ordered:
                    return
            except (tk.TclError, AttributeError):
                pass
            # 0→N-1 순으로 연속 move() 하면 Windows ttk에서 중간에 빈 칸·행 순서 꼬임이
            # 나는 사례가 있어, 목표 인덱스가 큰 행부터 옮긴다.
            for i in range(len(ordered) - 1, -1, -1):
                nk = ordered[i]
                if not tr.exists(nk):
                    continue
                try:
                    tr.move(nk, "", i)
                except tk.TclError:
                    pass
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
            if tr is None or len(vals) < 12:
                return
            tag_list = tuple(t for t in tags if t)
            v12 = tuple(str(vals[i]) for i in range(12))
            if tr.exists(nk):
                tr.item(nk, values=v12, tags=tag_list)
            else:
                tr.insert("", tk.END, iid=nk, values=v12, tags=tag_list)
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
        total_invested = 0
        total_unrealized_pnl = 0
        total_unrealized_account = 0.0
        total_cost_ex_open_den = 0.0

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
        total_eval_weight = 0.0
        try:
            for idx, (nk, pos) in enumerate(active_list):
                info = mi.get(nk, {}) or {}
                _, _, _, _, ev_part, _ = self._active_row_vals_and_tags(
                    idx, nk, pos, info, None
                )
                total_eval_weight += float(ev_part)
        except Exception:
            total_eval_weight = 0.0
        for idx, (nk, pos) in enumerate(active_list):
            info = mi.get(nk, {}) or {}
            vals, row_tags, _pct, _ur_ex, _ev_row, ur_net = self._active_row_vals_and_tags(
                idx, nk, pos, info, total_eval_weight
            )
            total_invested += float(pos.get("invested", 0) or 0)
            total_unrealized_pnl += ur_net
            try:
                ap_r, qr, crp, _, _ = self._resolve_active_holdings_prices(nk, pos, info)
                u_acc = self._unrealized_vs_cost_ex_buy_at_price(
                    float(crp), int(qr), float(ap_r)
                )
                total_unrealized_account += self._finite_tree_float(float(u_acc))
                total_cost_ex_open_den += self._holding_cost_ex_fee(float(ap_r), int(qr))
            except Exception:
                pass

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

        # 2. 종료 포지션 실현손익 합계(상단 누적 수익용). 표시는 주문·체결 트리 필터로 대체.
        try:
            total_closed_pnl = float(self._sum_visible_history_realized_pnl())
        except Exception:
            total_closed_pnl = 0.0

        # 3. 요약: 예수금·D+n은 포트폴리오(체결/수동 동기화 시 REST).
        # 「주식 평가금」= 보유 순매각 합 Σ(현재가×수량 후 매도수수료 차감) = invested 합 + 미실현(
        # 매도 차감) 합 — 금액 자체에는 매입수수료가 포함되지 않으며, 매도 수수료만 반영된다.
        portfolio = self.app.portfolio
        current_cash = getattr(portfolio, "actual_deposit", 0)
        try:
            stock_liquid = float(total_invested) + float(total_unrealized_pnl)
        except (TypeError, ValueError):
            stock_liquid = 0.0
        stock_eval = self._resolve_stock_eval_for_account(portfolio, stock_liquid)
        total_assets = self._compute_virtual_tab_total_assets(portfolio, stock_eval)
        total_assets_day_pnl = self._compute_virtual_tab_total_assets(
            portfolio, stock_eval, skip_broker_te_reconcile=True
        )

        # 미실현 손익: 순매각(매도 수수료 차감) − (평단×수량) 합계. 매입수수료는 매입 원가(cost)에서 제외.
        # 손익률 분모는 Σ(평단×수량).
        try:
            open_ur_amt = float(total_unrealized_account)
        except (TypeError, ValueError):
            open_ur_amt = 0.0
        try:
            cxd_den = float(total_cost_ex_open_den)
        except (TypeError, ValueError):
            cxd_den = 0.0
        if cxd_den > 1e-6:
            open_ur_pct = (open_ur_amt / cxd_den) * 100.0
        else:
            open_ur_pct = 0.0
        try:
            ta = float(total_assets)
        except (TypeError, ValueError):
            ta = 0.0

        # 금일 손익: 직전 장마감 시세 합성 분모와 동일하게 **D+2+순매각 합**(증권사 총평가 치환 없음)을 분자로 사용.
        try:
            d_amt, d_pct, _p0, _pk = compute_day_pnl_vs_eod_store(
                self.app,
                calendar_day=datetime.now().date(),
                current_total_assets=float(total_assets_day_pnl),
            )
        except Exception:
            d_amt, d_pct = None, None
        if d_amt is not None and d_pct is not None:
            day_pnl_txt = f"{int(d_amt):+,} ({d_pct:+.2f}%)"
        else:
            day_pnl_txt = "— (전일 기준 없음)"
        open_ur_txt = f"{int(open_ur_amt):+,} ({open_ur_pct:+.2f}%)"

        # 누적 수익(종료+보유): 매매종료 손익 합 + 보유 「미실현 손익」(계좌 요약 정의와 동일).
        # % 분모는 총자산(백테스트 초기자본은 실시간 요약에 사용하지 않음).
        cum_pnl_amt = float(total_closed_pnl) + float(total_unrealized_account)
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

        self.vars["open_ur"].set(open_ur_txt)
        self.vars["day"].set(day_pnl_txt)
        self.vars["cum"].set(f"{int(cum_pnl_amt):+,} ({cum_pnl_pct:+.2f}%)")
        self._update_holdings_aggregate_fee_excluded()

    def _render_real_data(self):
        """실시간 틱·동기화 후 보유 트리 값만 갱신."""
        if not self._active_tree_ready():
            return
        try:
            mi = getattr(self.app, "monitor_info", None) or {}
            holdings = self._sorted_active_holdings()
            total_eval_weight = 0.0
            try:
                for idx, (nk, pos) in enumerate(holdings):
                    info = mi.get(nk, {}) or {}
                    _, _, _, _, ev_part, _ = self._active_row_vals_and_tags(
                        idx, nk, pos, info, None
                    )
                    total_eval_weight += float(ev_part)
            except Exception:
                total_eval_weight = 0.0
            for idx, (nk, pos) in enumerate(holdings):
                info = mi.get(nk, {}) or {}
                vals, row_tags, _, _, _, _ = self._active_row_vals_and_tags(
                    idx, nk, pos, info, total_eval_weight
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
        """
        틱·경량(refresh_portfolio_light) 직후 상단 요약을 갱신합니다.
        주식평가·총자산·손익은 틱 기준이고, 예수금·D+1/D+2는 **메모리의 portfolio 필드**를 반영합니다
        (REST를 다시 호출하지 않음). 동기화 후 구조 시그니처가 같으면 경량 경로만 타는데,
        그때 이 필드를 갱신하지 않으면 예수·D+n이 0으로 남는 버그가 있었음.
        """
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
            holdings = self._sorted_active_holdings()
            total_eval_weight = 0.0
            try:
                for idx, (nk, pos) in enumerate(holdings):
                    info = mi.get(nk, {}) or {}
                    _, _, _, _, ev_part, _ = self._active_row_vals_and_tags(
                        idx, nk, pos, info, None
                    )
                    total_eval_weight += float(ev_part)
            except Exception:
                total_eval_weight = 0.0
            total_unrealized_pnl = 0.0
            total_invested_open = 0.0
            total_unrealized_account = 0.0
            total_cost_ex_open_den = 0.0
            for idx, (nk, pos) in enumerate(holdings):
                info = mi.get(nk, {}) or {}
                _v, _t, _p, _ur_ex, _ev_row, ur_net = self._active_row_vals_and_tags(
                    idx, nk, pos, info, total_eval_weight
                )
                total_unrealized_pnl += ur_net
                try:
                    total_invested_open += float(pos.get("invested", 0) or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    ap_r, qr, crp, _, _ = self._resolve_active_holdings_prices(nk, pos, info)
                    u_acc = self._unrealized_vs_cost_ex_buy_at_price(
                        float(crp), int(qr), float(ap_r)
                    )
                    total_unrealized_account += self._finite_tree_float(float(u_acc))
                    total_cost_ex_open_den += self._holding_cost_ex_fee(float(ap_r), int(qr))
                except Exception:
                    pass

            portfolio = self.app.portfolio
            try:
                stock_liquid = float(total_invested_open) + float(total_unrealized_pnl)
            except (TypeError, ValueError):
                stock_liquid = 0.0
            stock_eval = self._resolve_stock_eval_for_account(portfolio, stock_liquid)
            total_assets = self._compute_virtual_tab_total_assets(portfolio, stock_eval)
            total_assets_day_pnl = self._compute_virtual_tab_total_assets(
                portfolio, stock_eval, skip_broker_te_reconcile=True
            )

            try:
                open_ur_amt = float(total_unrealized_account)
            except (TypeError, ValueError):
                open_ur_amt = 0.0
            try:
                cxd_den = float(total_cost_ex_open_den)
            except (TypeError, ValueError):
                cxd_den = 0.0
            if cxd_den > 1e-6:
                open_ur_pct = (open_ur_amt / cxd_den) * 100.0
            else:
                open_ur_pct = 0.0
            try:
                ta = float(total_assets)
            except (TypeError, ValueError):
                ta = 0.0

            try:
                d_amt, d_pct, _p0, _pk = compute_day_pnl_vs_eod_store(
                    self.app,
                    calendar_day=datetime.now().date(),
                    current_total_assets=float(total_assets_day_pnl),
                )
            except Exception:
                d_amt, d_pct = None, None
            if d_amt is not None and d_pct is not None:
                day_pnl_txt = f"{int(d_amt):+,} ({d_pct:+.2f}%)"
            else:
                day_pnl_txt = "— (전일 기준 없음)"
            open_ur_txt = f"{int(open_ur_amt):+,} ({open_ur_pct:+.2f}%)"

            closed_h = self._sum_visible_history_realized_pnl()
            cum_pnl_amt = float(closed_h) + float(total_unrealized_account)
            if ta > 1e-6:
                cum_pnl_pct = (cum_pnl_amt / ta) * 100.0
            else:
                cum_pnl_pct = 0.0

            _set_if_diff("total", f"{total_assets:,.0f}")
            _set_if_diff("stock", f"{stock_eval:,.0f}")
            try:
                cash_v = float(getattr(portfolio, "actual_deposit", 0) or 0)
                d1_v = float(getattr(portfolio, "d1_deposit", 0) or 0)
                d2_v = float(getattr(portfolio, "d2_deposit", 0) or 0)
                _set_if_diff("cash", f"{cash_v:,.0f}")
                _set_if_diff("d1_cash", f"{d1_v:,.0f}")
                _set_if_diff("d2_cash", f"{d2_v:,.0f}")
            except (TypeError, ValueError) as _cash_err:
                try:
                    print(f"[UI Error] 요약 예수/D+n 반영: {_cash_err}")
                except Exception:
                    pass
            _set_if_diff("open_ur", open_ur_txt)
            _set_if_diff("day", day_pnl_txt)
            _set_if_diff("cum", f"{int(cum_pnl_amt):+,} ({cum_pnl_pct:+.2f}%)")
            self._update_holdings_aggregate_fee_excluded()
        except Exception:
            pass

    @staticmethod
    def _stock_eval_for_summary(_portfolio: Any, liquid_nav_sum: float) -> float:
        """(레거시) ``_resolve_stock_eval_for_account``와 동일."""
        try:
            return float(liquid_nav_sum or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _resolve_stock_eval_for_account(_portfolio: Any, liquid_nav_sum: float) -> float:
        """
        상단 「주식 평가금」·총자산 주식 분.

        계좌 「주식 평가금」은 **매도 후 실수령 근사**(현재가×수량 − 매도 수수료)의 보유 전체 합에 맞춥니다.
        이를 ``invested``(항목별, 매입에 이미 포함된 매수수수료) 합과
        `_unrealized_at_price_net_of_sell_fee` 미실현 합의 항등변환값 ``liquid_nav_sum``으로 받습니다.

        반면 순수 시가 평가(매도비 미반영)나 잔고 API ``evlu_amt`` 합은 사용자 기대와 달라 **사용하지 않습니다**.

        ``_portfolio`` 인자는 시그니처 호환 유지용입니다.
        """
        try:
            return float(liquid_nav_sum or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _compute_virtual_tab_total_assets(
        portfolio: Any,
        _stock_eval: float,
        *,
        skip_broker_te_reconcile: bool = False,
    ) -> float:
        """
        포지션 관리 탭 「총 자산」: **D+2 예수 + 주식 순매각 합**(``_resolve_stock_eval_for_account``: 매도 수수료만 반영).

        ``last_tot_evlu_amt``(ka10085 단일 총평가)는 합산과 ±2.5% 이내면 증권사 표시와 동일시합니다.
        금일 손익(직전 장마감 대비)은 분모·분자 정합을 위해 ``skip_broker_te_reconcile=True`` 로
        **항상 D+2+순매각 합** 만 씁니다.
        """
        try:
            d2 = float(getattr(portfolio, "d2_deposit", 0) or 0)
            api_mv = float(getattr(portfolio, "holdings_stocks_mv_api", 0) or 0)
            # 주식 부분도 화면과 동일하게 로컬 순수 평가(현재가×수량 합); API 평가 합으로 대체하지 않음.
            try:
                se = float(_stock_eval or 0)
            except (TypeError, ValueError):
                se = 0.0
            combined = d2 + se
            raw_te = float(getattr(portfolio, "last_tot_evlu_amt", 0) or 0)
            pte = float(getattr(portfolio, "total_equity", 0) or 0)
            if abs(combined) > 1e-6 or se > 0 or d2 != 0:
                if (
                    not skip_broker_te_reconcile
                    and raw_te > 0
                ):
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
        """
        DB 주문 내역을 선택 날짜(네비) 기준으로 조회합니다.
        구분(전체·매수·매도)·체결(전체·체결·미체결) 필터 적용 후 트리에 표시합니다.
        (매도+체결 조합은 기존 '매매 종료 내역'과 같은 매도 체결 주문 목록에 해당)
        """
        if not getattr(self, "tree_orders", None):
            return
        self.tree_orders.delete(*self.tree_orders.get_children())
        want_side: Literal["all", "buy", "sell"] = "all"
        want_fill: Literal["all", "filled", "unfilled"] = "all"
        try:
            sv = (
                (self._order_filter_side_var.get() if self._order_filter_side_var else "전체")
                or "전체"
            ).strip()
            if sv == "매수":
                want_side = "buy"
            elif sv == "매도":
                want_side = "sell"
        except Exception:
            want_side = "all"
        try:
            fv = (
                (self._order_filter_fill_var.get() if self._order_filter_fill_var else "전체")
                or "전체"
            ).strip()
            if fv == "체결":
                want_fill = "filled"
            elif fv == "미체결":
                want_fill = "unfilled"
        except Exception:
            want_fill = "all"

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
                    norm = self._normalize_order_side_raw(side)
                except Exception:
                    norm = "unknown"
                if want_side == "buy" and norm != "buy":
                    continue
                if want_side == "sell" and norm != "sell":
                    continue
                try:
                    is_filled = self._order_log_row_is_filled(result, f_price, f_qty)
                except Exception:
                    is_filled = False
                if want_fill == "filled" and not is_filled:
                    continue
                if want_fill == "unfilled" and is_filled:
                    continue

                try:
                    tm_raw = str(ts or "").strip()
                    tm = tm_raw if tm_raw else "-"
                except Exception:
                    tm = "-"
                if norm == "buy":
                    side_kr = "매수"
                    side_tag = "price_up"
                elif norm == "sell":
                    side_kr = "매도"
                    side_tag = "price_down"
                else:
                    try:
                        side_kr = str(side or "?").strip() or "?"
                    except Exception:
                        side_kr = "?"
                    side_tag = "price_down"

                try:
                    qn = int(qty or 0)
                except (TypeError, ValueError):
                    qn = 0
                try:
                    pn = float(price or 0)
                except (TypeError, ValueError):
                    pn = 0.0
                try:
                    fpn = float(f_price or 0)
                except (TypeError, ValueError):
                    fpn = 0.0
                f_p_str = f"{fpn:,.0f}" if fpn > 0 else "-"
                try:
                    fqi = int(f_qty or 0)
                except (TypeError, ValueError):
                    fqi = 0
                fq_str = f"{fqi:,}" if fqi > 0 else "-"
                try:
                    ba_o = (str(snap_ba or "").strip() or "-")
                except Exception:
                    ba_o = "-"
                tag_base = "even" if row_i % 2 == 0 else "odd"
                row_i += 1

                try:
                    rs_disp = str(reason or "").strip()
                    if rs_disp.upper() in ("NONE", "NAN", ""):
                        rs_disp = "—"
                except Exception:
                    rs_disp = "—"

                self.tree_orders.insert(
                    "",
                    tk.END,
                    values=(
                        tm,
                        code,
                        name,
                        side_kr,
                        f"{qn:,}",
                        fq_str,
                        f"{pn:,.0f}",
                        f_p_str,
                        self._order_log_vp_cell(snap_vp),
                        ba_o,
                        self._order_log_status_display(result),
                        rs_disp,
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

    def _on_order_filter_changed(self, _event: Any = None) -> None:
        """구분·체결 필터 변경 시 주문 트리만 다시 그립니다."""
        try:
            self._render_orders()
        except Exception as e:
            try:
                print(f"[UI Error] _on_order_filter_changed: {e}")
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

    def log_order(self, code, side, qty, price, order_type, result, name="", reason=""):
        """[v4.5.3] DB 기록 및 포지션 탭(보유·주문) UI 갱신 (캡슐화)"""
        try:
            from core.supply_display import capture_supply_snapshot

            _sn = capture_supply_snapshot(self.app, code)
        except Exception:
            _sn = {"vol_power": None, "bid_ask_text": ""}
        self.app.data_manager.log_order(
            code,
            side,
            qty,
            price,
            order_type,
            "MOCK_ID",
            result,
            name=name or "",
            reason=reason or "",
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
        """종합 리포트 생성 중 비활성화할 버튼(포지션 탭·전략검증 탭)."""
        out: List[Any] = []
        try:
            if self.btn_narrative_report is not None:
                out.append(self.btn_narrative_report)
        except Exception:
            pass
        try:
            _bt = getattr(self.app, "backtest_tab", None)
            _b = getattr(_bt, "btn_narrative_report", None) if _bt is not None else None
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
        try:
            cset = getattr(self.app, "colors", None) or {}
            row_fg = str(cset.get("fg", "#ECEFF4"))
        except Exception:
            row_fg = "#ECEFF4"
        
        for t in [self.tree_active, self.tree_orders]:
            if t is None:
                continue
            # [v4.8.4] TCheckbutton 스타일은 메인에서 관리하므로 태그에 집중
            # even/odd 에 기본 전경을 두어, 손익 태그 제거 후 글자색이 배경에 묻는(ttk) 현상을 막음
            t.tag_configure("even", background=bg_even, foreground=row_fg)
            t.tag_configure("odd", background=bg_odd, foreground=row_fg)
            
            # [v4.7.5] 글로벌 일관성을 위해 고밀도 시인성 컬러 적용
            t.tag_configure("price_up", foreground=up_col)
            t.tag_configure("price_down", foreground=dn_col)
