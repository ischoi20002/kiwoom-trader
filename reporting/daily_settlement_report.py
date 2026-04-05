# -*- coding: utf-8 -*-
"""
실시간 감시 '일일 결산' HTML 리포트.
가상포지션 탭에서 사람이 직접 검토하기 위한 요약 + 종목별 주문/체결 타임라인.
"""
from __future__ import annotations

import html
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from reporting.realtime_session_logger import RealtimeSessionLogger


def _json_dumps_pretty(d: Any) -> str:
    try:
        return json.dumps(d, ensure_ascii=False, indent=2)
    except Exception:
        return str(d)


def _esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _report_excluded_watchlist_codes_from_app(app: Any) -> set:
    """일일결산에서 뺄 종목 = 관심목록에만 있고 감시목록에는 없는 코드(겹치면 포함)."""
    try:
        mc = getattr(app.trading_controller, "monitor_codes", None) or []
    except Exception:
        mc = []
    try:
        return app.watchlist_store.codes_report_exclusion_set(mc)
    except Exception:
        return set()


def _compute_summary_from_app(app: Any) -> Dict[str, Any]:
    """VirtualTab._render_virtual_positions 와 동일한 요약 로직."""
    portfolio = app.portfolio
    monitor_info = getattr(app, "monitor_info", {}) or {}
    today_str = datetime.now().strftime("%Y-%m-%d")
    excl = _report_excluded_watchlist_codes_from_app(app)

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
        start_cap = float(
            str(app.settings.get("backtest_initial_capital", 10000000)).replace(",", "")
        )
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


def generate_daily_settlement_report(
    app: Any,
    report_date: Optional[str] = None,
    output_path: Optional[str] = None,
) -> str:
    """
    일일 결산 HTML을 생성하고 저장한 파일의 절대 경로를 반환합니다.

    Parameters
    ----------
    app : KiwoomApp
    report_date : str, optional
        YYYY-MM-DD. None이면 오늘.
    output_path : str, optional
        저장 경로. None이면 realtime_session_logs/{date}/daily_settlement.html
    """
    try:
        day = report_date or datetime.now().strftime("%Y-%m-%d")
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base_dir = os.path.join(_root, "realtime_session_logs", day)
        try:
            os.makedirs(base_dir, exist_ok=True)
        except OSError:
            pass

        if output_path is None:
            output_path = os.path.join(base_dir, "daily_settlement.html")

        dm = app.data_manager
        start_dt = f"{day} 00:00:00"
        end_dt = f"{day} 23:59:59"
        try:
            orders: List[Tuple] = dm.get_orders_between(start_dt, end_dt)
        except Exception:
            orders = []

        events = RealtimeSessionLogger().read_events(day)

        summary = _compute_summary_from_app(app)
        excl = _report_excluded_watchlist_codes_from_app(app)

        # 종목별 그룹 (주문)
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

        # JSONL 이벤트: kind별로 표에 넣기 쉽게 정렬
        events_sorted = sorted(events, key=lambda x: x.get("ts", ""))

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
        for ev in events_sorted:
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

        html_doc = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <title>일일 결산 — {_esc(day)}</title>
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
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        th, td {{ padding: 6px 8px; border-bottom: 1px solid #434C5E; text-align: left; }}
        th {{ color: var(--muted); }}
        .jsoncell pre {{
            margin: 0; white-space: pre-wrap; word-break: break-all;
            font-size: 11px; color: #D8DEE9;
        }}
        footer {{ margin-top: 32px; font-size: 12px; color: #81A1C1; text-align: center; }}
    </style>
</head>
<body>
<div class="container">
    <h1>일일 결산 리포트 <span class="muted">{_esc(day)}</span></h1>
    <p class="muted">생성 시각: {_esc(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))} — 아래 요약은 GUI 가상포지션 탭과 동일한 산식을 사용합니다.</p>

    <h2>요약</h2>
    <table class="summary">{summary_rows}</table>

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

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_doc)
        return os.path.abspath(output_path)
    except Exception as e:
        raise RuntimeError(f"일일 결산 리포트 생성 실패: {e}") from e
