# -*- coding: utf-8 -*-
"""
실시간 감시 '일일 결산' HTML 리포트.
- 거래일별 갭 채우기, 메타데이터(완료/부분), Plotly 15분봉+주문 오버레이
- 리포트 생성 시 종목별 DB 분봉 갱신 시도(실패 시 부분 리포트)
"""
from __future__ import annotations

import html
import json
import os
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from reporting.realtime_session_logger import RealtimeSessionLogger

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    _HAS_PLOTLY = True
except Exception:
    _HAS_PLOTLY = False

from reporting.krx_trading_calendar import (
    next_trading_day,
    previous_trading_day,
    trading_days_inclusive,
)


def _json_dumps_pretty(d: Any) -> str:
    try:
        return json.dumps(d, ensure_ascii=False, indent=2)
    except Exception:
        return str(d)


def _esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def report_dir_for_date(day: str) -> str:
    """realtime_session_logs/{YYYY-MM-DD}/ 절대 경로."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "realtime_session_logs", day)


def report_html_path(day: str) -> str:
    return os.path.join(report_dir_for_date(day), "daily_settlement.html")


def _session_end_local(report_day: str, buffer_minutes: int) -> datetime:
    """KST 로컬 기준 장 마감(15:30) + 버퍼."""
    d = datetime.strptime(report_day[:10], "%Y-%m-%d")
    return d.replace(hour=15, minute=30, second=0, microsecond=0) + timedelta(minutes=max(0, int(buffer_minutes)))


def generation_is_complete(generated_at: datetime, report_day: str, buffer_minutes: int) -> bool:
    """
    완료 리포트: (1) 생성일이 리포트일 다음날 이후(새벽 생성 허용)
    또는 (2) 같은 날이면 장마감+버퍼 이후.
    """
    try:
        rd = datetime.strptime(report_day[:10], "%Y-%m-%d").date()
    except Exception:
        return False
    gdate = generated_at.date()
    if gdate > rd:
        return True
    if gdate == rd:
        return generated_at >= _session_end_local(report_day, buffer_minutes)
    return False


def read_embedded_meta(html_text: str) -> Optional[Dict[str, Any]]:
    """HTML 본문에서 daily-report-meta JSON 추출."""
    try:
        key = 'id="daily-report-meta"'
        i0 = html_text.find(key)
        if i0 < 0:
            return None
        i = html_text.find(">", i0)
        if i < 0:
            return None
        i += 1
        j = html_text.find("</script>", i)
        if j < 0:
            return None
        raw = html_text[i:j].strip()
        return json.loads(raw)
    except Exception:
        return None


def report_needs_regeneration(
    path: str,
    report_day: str,
    buffer_minutes: int,
) -> bool:
    """파일이 없거나, 완료 조건을 만족하지 않으면 True."""
    if not path or not os.path.isfile(path):
        return True
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return True
    meta = read_embedded_meta(txt)
    gen_s = (meta or {}).get("generated_at") or ""
    try:
        if gen_s:
            generated_at = datetime.strptime(gen_s[:19], "%Y-%m-%d %H:%M:%S")
        else:
            generated_at = datetime.fromtimestamp(os.path.getmtime(path))
    except Exception:
        generated_at = datetime.fromtimestamp(os.path.getmtime(path))
    if not generation_is_complete(generated_at, report_day, buffer_minutes):
        return True
    status = (meta or {}).get("status", "complete")
    if status == "partial":
        # 부분 리포트는 재실행 시 갱신 가능(사용자 재시도)
        return True
    return False


def _report_excluded_watchlist_codes_from_app(app: Any) -> set:
    try:
        mc = getattr(app.trading_controller, "monitor_codes", None) or []
    except Exception:
        mc = []
    try:
        return app.watchlist_store.codes_report_exclusion_set(mc)
    except Exception:
        return set()


def _compute_summary_from_app(app: Any, report_day: str) -> Dict[str, Any]:
    """VirtualTab 요약과 유사. report_day 기준 금일 실현 필터."""
    portfolio = app.portfolio
    monitor_info = getattr(app, "monitor_info", {}) or {}
    excl = _report_excluded_watchlist_codes_from_app(app)
    today_str = report_day

    total_eval = 0.0
    total_unrealized = 0.0
    for code, pos in portfolio.virtual_positions.items():
        if str(code).zfill(6) in excl:
            continue
        info = monitor_info.get(code, {})
        curr_p = float(info.get("price", pos.get("avg_price", 0)))
        qty = int(pos.get("qty", 0))
        avg_p = float(pos.get("avg_price", 0))
        eval_amt = curr_p * qty
        unrealized = (curr_p - avg_p) * qty
        total_eval += eval_amt
        total_unrealized += unrealized

    total_day_realized = 0.0
    for pos in portfolio.virtual_history:
        if str(pos.get("code", "")).zfill(6) in excl:
            continue
        pnl_amt = float(pos.get("realized_pnl_amt", 0) or 0)
        if str(pos.get("entry_time", "")).startswith(today_str):
            total_day_realized += pnl_amt

    total_assets = float(getattr(portfolio, "total_equity", 0) or 0)
    current_cash = float(getattr(portfolio, "actual_deposit", 0) or 0)
    stock_eval = total_eval
    if total_assets == 0:
        total_assets = current_cash + stock_eval

    day_pnl_total = total_day_realized + total_unrealized
    try:
        start_cap = float(str(app.settings.get("backtest_initial_capital", 10000000)).replace(",", ""))
    except Exception:
        start_cap = 10000000.0
    safe_cap = max(1.0, start_cap if total_assets == 0 else (total_assets - day_pnl_total))
    day_pnl_pct = (day_pnl_total / safe_cap) * 100 if safe_cap else 0.0
    total_profit_pct = (total_assets / safe_cap - 1) * 100 if safe_cap else 0.0

    d1 = float(getattr(portfolio, "d1_deposit", 0) or 0)
    d2 = float(getattr(portfolio, "d2_deposit", 0) or 0)

    return {
        "total_assets": total_assets,
        "cash": current_cash,
        "stock_eval": stock_eval,
        "d1_cash": d1,
        "d2_cash": d2,
        "day_pnl_amt": day_pnl_total,
        "day_pnl_pct": day_pnl_pct,
        "cum_return_pct": total_profit_pct,
        "unrealized": total_unrealized,
        "realized_today": total_day_realized,
    }


def _refresh_symbol_bars(app: Any, code: str, log: Optional[Callable[[str], None]]) -> bool:
    """키움 분봉 동기화 후 15분 리샘플. 실패 시 False."""
    try:
        dm = app.data_manager

        def _logger(msg: str) -> None:
            if log:
                log(msg)

        ok = dm.update_historical_data(code, logger=_logger)
        return bool(ok)
    except Exception as e:
        if log:
            try:
                log(f"[일일결산] {code} 분봉 갱신 예외: {e}")
            except Exception:
                pass
        return False


def _build_plotly_15m_section(
    code: str,
    name: str,
    report_day: str,
    df: Any,
    orders_for_code: List[Dict[str, Any]],
) -> str:
    """15분 OHLC + 거래량 + MA + 주문 시각 마커. df는 get_bars 결과(DataFrame)."""
    if not _HAS_PLOTLY:
        return '<p class="warn">Plotly 미설치로 차트를 생략했습니다.</p>'

    try:
        import pandas as pd

        if df is None or df.empty:
            return '<p class="muted">해당 일자 15분봉 데이터가 없습니다.</p>'

        work = df.copy()
        if "time" in work.columns:
            work["time"] = pd.to_datetime(work["time"])
            x = work["time"]
        elif work.index.name == "time" or isinstance(work.index, pd.DatetimeIndex):
            x = work.index
        else:
            return '<p class="muted">차트용 시간 열을 찾을 수 없습니다.</p>'

        o = work["Open"].astype(float)
        h = work["High"].astype(float)
        l = work["Low"].astype(float)
        c = work["Close"].astype(float)
        v = work["Volume"].astype(float)

        ma5 = c.rolling(5, min_periods=1).mean()
        ma20 = c.rolling(20, min_periods=1).mean()

        fig = make_subplots(
            rows=2,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.06,
            row_heights=[0.68, 0.32],
            subplot_titles=(f"{name} ({code}) 15분", "거래량"),
        )

        fig.add_trace(
            go.Candlestick(
                x=x,
                open=o,
                high=h,
                low=l,
                close=c,
                name="15m",
                increasing_line_color="#A3BE8C",
                decreasing_line_color="#BF616A",
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(x=x, y=ma5, name="MA5", line=dict(color="#EBCB8B", width=1)),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(x=x, y=ma20, name="MA20", line=dict(color="#88C0D0", width=1)),
            row=1,
            col=1,
        )

        colors_vol = ["#A3BE8C" if c.iloc[i] >= o.iloc[i] else "#BF616A" for i in range(len(work))]
        fig.add_trace(
            go.Bar(x=x, y=v, name="Volume", marker_color=colors_vol, opacity=0.7),
            row=2,
            col=1,
        )

        # 주문 시각 세로선
        for orec in orders_for_code:
            ts = orec.get("ts", "")
            try:
                tx = pd.to_datetime(ts)
                side = str(orec.get("side", "")).upper()
                color = "#BF616A" if "매도" in side or "SELL" in side else "#88C0D0"
                fig.add_vline(x=tx, line_width=1, line_dash="dot", line_color=color, opacity=0.55, row=1, col=1)
                fig.add_vline(x=tx, line_width=1, line_dash="dot", line_color=color, opacity=0.35, row=2, col=1)
            except Exception:
                continue

        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="#2E3440",
            plot_bgcolor="#3B4252",
            height=480,
            margin=dict(l=48, r=24, t=48, b=40),
            hovermode="x unified",
            showlegend=True,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
            xaxis_rangeslider_visible=False,
        )
        fig.update_xaxes(showspikes=True, spikemode="across", spikesnap="cursor", spikedash="solid", row=1, col=1)
        fig.update_xaxes(showspikes=True, spikemode="across", spikesnap="cursor", spikedash="solid", row=2, col=1)

        import plotly.io as pio

        div = pio.to_html(fig, include_plotlyjs=False, full_html=False)
        return f'<div class="chartwrap">{div}</div>'
    except Exception as e:
        return f'<p class="warn">차트 생성 오류: {_esc(e)}</p>'


def generate_daily_settlement_report(
    app: Any,
    report_date: Optional[str] = None,
    output_path: Optional[str] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> str:
    """
    일일 결산 HTML 생성. 저장 경로 절대 경로 반환.

    Parameters
    ----------
    report_date : YYYY-MM-DD, None이면 오늘(로컬).
    log_callback : 진행 로그(선택).
    """
    res = generate_daily_settlement_report_ex(app, report_date, output_path, log_callback)
    return res["path"]


def generate_daily_settlement_report_ex(
    app: Any,
    report_date: Optional[str] = None,
    output_path: Optional[str] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """상세 결과 dict: path, status, warnings, chart_failures 등."""
    warnings: List[str] = []
    chart_failures: List[str] = []
    buffer_minutes = int(app.settings.get("daily_report_session_buffer_minutes", 10))

    def _log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    day = report_date or datetime.now().strftime("%Y-%m-%d")
    base_dir = report_dir_for_date(day)
    try:
        os.makedirs(base_dir, exist_ok=True)
    except OSError as e:
        warnings.append(f"폴더 생성 경고: {e}")

    if output_path is None:
        output_path = report_html_path(day)

    dm = app.data_manager
    start_dt = f"{day} 00:00:00"
    end_dt = f"{day} 23:59:59"
    try:
        orders: List[Tuple] = dm.get_orders_between(start_dt, end_dt)
    except Exception:
        orders = []

    events = RealtimeSessionLogger().read_events(day)
    summary = _compute_summary_from_app(app, day)
    excl = _report_excluded_watchlist_codes_from_app(app)

    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for row in orders:
        try:
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
                order_no,
                order_type,
            ) = row
        except Exception:
            continue
        c = str(code).zfill(6)
        if c in excl:
            continue
        if c not in by_code:
            by_code[c] = []
        by_code[c].append(
            {
                "ts": ts,
                "name": name or "",
                "side": side,
                "qty": qty,
                "price": price,
                "filled_price": f_price,
                "filled_qty": f_qty,
                "result": result,
                "reason": reason or "",
                "order_no": order_no or "",
                "order_type": order_type or "",
            }
        )

    # 차트 대상: 주문·체결 기록이 있는 모든 종목
    codes_for_charts = sorted(by_code.keys())

    generated_at = datetime.now()
    overall_status: str = "complete"

    chart_sections = ""
    for code in codes_for_charts:
        nm = by_code[code][0].get("name", "") if by_code[code] else ""
        ok_sync = _refresh_symbol_bars(app, code, _log)
        if not ok_sync:
            chart_failures.append(code)
            overall_status = "partial"

        df_15 = None
        try:
            df_15 = dm.get_bars(code, start_dt=start_dt, end_dt=end_dt, interval="15m")
        except Exception as e:
            warnings.append(f"{code} 15분 조회 실패: {e}")
            overall_status = "partial"

        block = _build_plotly_15m_section(code, nm or code, day, df_15, by_code[code])
        chart_sections += f"""
        <div class="card" id="chart-{_esc(code)}">
            <h3>{_esc(nm)} <span class="muted">({_esc(code)})</span> — 15분봉</h3>
            {block}
        </div>"""

    if chart_failures:
        warnings.append(f"분봉 갱신 실패(스킵): {', '.join(chart_failures)}")

    # 종목별 주문 표
    code_sections = ""
    for code in sorted(by_code.keys()):
        rows = ""
        for o in by_code[code]:
            rows += f"""<tr>
                <td>{_esc(o['ts'])}</td>
                <td>{_esc(o['side'])}</td>
                <td class="num">{o['qty']:,}</td>
                <td class="num">{o['price']:,}</td>
                <td class="num">{o['filled_price'] if o['filled_price'] else '-'}</td>
                <td class="num">{o['filled_qty'] if o['filled_qty'] else '-'}</td>
                <td>{_esc(o['result'])}</td>
                <td>{_esc(o['order_no'])}</td>
                <td>{_esc(o['reason'])}</td>
            </tr>"""
        nm = by_code[code][0].get("name", "") if by_code[code] else ""
        code_sections += f"""
        <div class="card">
            <h3>{_esc(nm)} <span class="muted">({_esc(code)})</span></h3>
            <table>
                <thead>
                    <tr>
                        <th>시각</th><th>구분</th><th>수량</th><th>주문가</th><th>체결가</th><th>체결수량</th>
                        <th>상태</th><th>주문번호</th><th>사유</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>"""

    ev_rows = ""
    for ev in sorted(events, key=lambda x: x.get("ts", "")):
        data = ev.get("data", {}) or {}
        try:
            raw_c = data.get("code")
            if raw_c is not None and str(raw_c).strip() != "":
                ec = str(raw_c).strip().zfill(6)
                if ec in excl:
                    continue
        except Exception:
            pass
        k = ev.get("kind", "")
        ts = ev.get("ts", "")
        ev_rows += f"""<tr>
            <td>{_esc(ts)}</td>
            <td>{_esc(k)}</td>
            <td class="jsoncell"><pre>{_esc(_json_dumps_pretty(data))}</pre></td>
        </tr>"""

    summary_rows = f"""
        <tr><td>총 평가금액 (API)</td><td class="num">{summary['total_assets']:,.0f} 원</td></tr>
        <tr><td>예수금 (가용)</td><td class="num">{summary['cash']:,.0f} 원</td></tr>
        <tr><td>예수금 D+1</td><td class="num">{summary['d1_cash']:,.0f} 원</td></tr>
        <tr><td>예수금 D+2</td><td class="num">{summary['d2_cash']:,.0f} 원</td></tr>
        <tr><td>주식 평가금 (보유 합)</td><td class="num">{summary['stock_eval']:,.0f} 원</td></tr>
        <tr><td>금일 실현(엔트리일 기준 합산)</td><td class="num">{summary['realized_today']:+,.0f} 원</td></tr>
        <tr><td>미실현 손익 (보유분)</td><td class="num">{summary['unrealized']:+,.0f} 원</td></tr>
        <tr><td>금일 손익 (실현+미실현)</td><td class="num">{summary['day_pnl_amt']:+,.0f} 원 ({summary['day_pnl_pct']:+.2f}%)</td></tr>
        <tr><td>누적 수익률 (참고)</td><td class="num">{summary['cum_return_pct']:+.2f}%</td></tr>
    """

    gen_str = generated_at.strftime("%Y-%m-%d %H:%M:%S")
    complete_flag = generation_is_complete(generated_at, day, buffer_minutes)

    meta_obj: Dict[str, Any] = {
        "report_date": day,
        "generated_at": gen_str,
        "status": overall_status,
        "session_buffer_minutes": buffer_minutes,
        "generation_marked_complete": bool(complete_flag),
        "chart_failed_codes": chart_failures,
        "warnings": warnings,
        "schema_version": 1,
    }
    meta_json = json.dumps(meta_obj, ensure_ascii=False)

    prev_d = previous_trading_day(day)
    next_d = next_trading_day(day)
    nav_html = ""
    if prev_d:
        nav_html += f'<a class="navbtn" href="../{prev_d.strftime("%Y-%m-%d")}/daily_settlement.html">◀ 이전 거래일</a> '
    nav_html += f'<span class="muted">{_esc(day)}</span> '
    if next_d:
        nav_html += f'<a class="navbtn" href="../{next_d.strftime("%Y-%m-%d")}/daily_settlement.html">다음 거래일 ▶</a>'

    warn_banner = ""
    if overall_status == "partial":
        warn_banner = """
        <div class="banner warn">
            일부 종목의 분봉 갱신 또는 차트 생성에 실패했습니다. 주문·체결 표는 유지됩니다. 네트워크·API 확인 후 다시 생성해 주세요.
        </div>"""

    plotly_cdn = '<script src="https://cdn.plot.ly/plotly-2.24.1.min.js"></script>'

    html_doc = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <title>일일 결산 — {_esc(day)}</title>
    {plotly_cdn}
    <script type="application/json" id="daily-report-meta">{meta_json}</script>
    <style>
        :root {{
            --bg: #2E3440;
            --fg: #ECEFF4;
            --muted: #88C0D0;
            --bd: #4C566A;
            --plus: #A3BE8C;
            --minus: #BF616A;
        }}
        body {{
            font-family: 'Malgun Gothic', 'Segoe UI', sans-serif;
            background: var(--bg);
            color: var(--fg);
            margin: 0;
            padding: 24px;
            line-height: 1.5;
        }}
        .container {{ max-width: 1100px; margin: 0 auto; }}
        h1 {{ font-weight: 600; color: var(--muted); margin-top: 0; }}
        h2 {{ font-size: 1.1rem; color: var(--muted); border-bottom: 1px solid var(--bd); padding-bottom: 8px; }}
        .muted {{ color: #81A1C1; font-weight: normal; }}
        .navbtn {{ color: #88C0D0; text-decoration: none; margin: 0 8px; }}
        .navbtn:hover {{ text-decoration: underline; }}
        table.summary {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; }}
        table.summary td {{ padding: 8px 12px; border-bottom: 1px solid var(--bd); }}
        table.summary td:first-child {{ color: #D8DEE9; width: 40%; }}
        .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
        .card {{
            background: #3B4252;
            border: 1px solid var(--bd);
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 20px;
        }}
        .banner.warn {{
            background: #4C566A;
            border: 1px solid #BF616A;
            color: #ECEFF4;
            padding: 12px 16px;
            border-radius: 8px;
            margin-bottom: 16px;
        }}
        p.warn {{ color: #BF616A; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        th, td {{ padding: 6px 8px; border-bottom: 1px solid #434C5E; text-align: left; }}
        th {{ color: var(--muted); }}
        .jsoncell pre {{
            margin: 0; white-space: pre-wrap; word-break: break-all;
            font-size: 11px; color: #D8DEE9;
        }}
        .chartwrap {{ overflow-x: auto; }}
        footer {{ margin-top: 32px; font-size: 12px; color: #81A1C1; text-align: center; }}
    </style>
</head>
<body>
<div class="container">
    <nav style="margin-bottom:16px;">{nav_html}</nav>
    <h1>일일 결산 리포트 <span class="muted">{_esc(day)}</span></h1>
    <p class="muted">생성 시각: {_esc(gen_str)} — 상태: {_esc(overall_status)} — 완료판정(장마감+버퍼): {'예' if complete_flag else '아니오(장중·재생성 권장)'}</p>
    {warn_banner}

    <h2>요약</h2>
    <table class="summary">{summary_rows}</table>

    <h2>종목별 15분봉 (주문 시각 표시)</h2>
    {chart_sections if chart_sections else '<p class="muted">해당 일자 주문 기록이 없어 차트를 생략했습니다.</p>'}

    <h2>종목별 주문 · 체결 (DB order_history)</h2>
    {code_sections if code_sections else '<p class="muted">해당 일자 주문 기록이 없습니다.</p>'}

    <h2>세션 이벤트 로그 (JSONL 복사본)</h2>
    <p class="muted">파일: realtime_session_logs/{_esc(day)}/session_events.jsonl</p>
    <div class="card" style="padding:0; overflow:auto;">
        <table>
            <thead><tr><th>시각</th><th>종류</th><th>데이터</th></tr></thead>
            <tbody>{ev_rows if ev_rows else '<tr><td colspan="3" class="muted">이벤트 없음</td></tr>'}</tbody>
        </table>
    </div>

    <footer>K-Trader 실시간 감시 일일 결산</footer>
</div>
</body>
</html>"""

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_doc)
    except OSError as e:
        raise RuntimeError(f"일일 결산 리포트 저장 실패: {e}") from e

    return {
        "path": os.path.abspath(output_path),
        "status": overall_status,
        "warnings": warnings,
        "chart_failures": chart_failures,
        "report_date": day,
    }


def run_daily_settlement_batch(
    app: Any,
    start_date: str,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    start_date ~ 오늘 사이 KRX 거래일 중, 재생성이 필요한 일자만 일일결산 HTML 생성.
    최대 31일 범위. 완료 후 '오늘' 리포트 경로를 opened_path 로 반환.
    """
    from reporting.krx_trading_calendar import clamp_batch_range

    def _log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    today = datetime.now().strftime("%Y-%m-%d")
    s, e, err = clamp_batch_range(start_date, today, max_days=31)
    if err:
        return {"ok": False, "error": err, "opened_path": None, "generated": [], "skipped": []}

    days = trading_days_inclusive(s, e)
    buffer_minutes = int(app.settings.get("daily_report_session_buffer_minutes", 10))

    generated: List[str] = []
    skipped: List[str] = []
    for d in days:
        ds = d.strftime("%Y-%m-%d")
        p = report_html_path(ds)
        if not report_needs_regeneration(p, ds, buffer_minutes):
            skipped.append(ds)
            _log(f"[일일결산] 스킵(완료): {ds}")
            continue
        try:
            generate_daily_settlement_report_ex(app, ds, None, _log)
            generated.append(ds)
            _log(f"[일일결산] 생성: {ds}")
        except Exception as e:
            _log(f"[일일결산] 오류 {ds}: {e}")

    opened_path: Optional[str] = None
    tpath = report_html_path(today)
    if os.path.isfile(tpath):
        opened_path = os.path.abspath(tpath)
    else:
        for d0 in reversed(days):
            p = report_html_path(d0.strftime("%Y-%m-%d"))
            if os.path.isfile(p):
                opened_path = os.path.abspath(p)
                break

    return {
        "ok": True,
        "error": None,
        "opened_path": opened_path,
        "generated": generated,
        "skipped": skipped,
    }
