import os
import json
from datetime import datetime
import pandas as pd
import numpy as np

def generate_virtual_narrative_report(history, output_path="virtual_trade_report.html", settings=None):
    """
    [v18.0] 가상 매매 전용 나러티브 리포트 생성.
    매수/매도 시점의 스냅샷 데이터를 활용하여 의사결정 근거를 시각화합니다.
    """
    if not history:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("<html><body style='background:#1a1b26; color:#a9b1d6; padding:50px; text-align:center; font-family:sans-serif;'>")
            f.write("<h1>기록된 매매 내역이 없습니다.</h1><p>첫 가상 매매가 종료된 후 리포트가 활성화됩니다.</p></body></html>")
        return

    # [1] 파라미터 보드 생성 (v18.1)
    param_html = ""
    if settings:
        PARAM_STRUCTURE = [
            {"name": "💰 [1] 자금관리", "items": [{"label": "초기자금", "key": "backtest_initial_capital", "unit": "원"}, {"label": "비중", "keys": ["stage1_amt", "s1_equity_pct"], "unit_key": "s1_sizing_unit"}]},
            {"name": "🔍 [2] 종목선정", "items": [{"label": "RSI 범위", "keys": ["sel_rsi_min", "sel_rsi_max"], "sep": "~"}, {"label": "거래대금", "key": "min_trde_val", "unit": "억"}]},
            {"name": "🚀 [4] S1 전략", "items": [{"label": "Type A", "toggle": "use_type_a", "key": "buy_rsi_max_a"}, {"label": "Type B", "toggle": "use_type_b", "key": "buy_rsi_max_b"}, {"label": "Smart Buy", "keys": ["smart_buy_vp", "smart_buy_ba_ratio"], "sep": "/"}]},
            {"name": "📈 [5] S2 전략", "items": [{"label": "S2 활성화", "toggle": "use_s2_profit", "key": "s2_profit_limit", "unit": "%"}, {"label": "추매 Smart Buy", "toggle": "use_smart_buy_s2"}]},
            {"name": "🛡️ [7] 리스크 제어", "items": [
                {"label": "Smart Exit", "toggle": "use_smart_exit", "key": "smart_exit_ba_ratio"},
                {"label": "갭하락손절", "toggle": "use_gap_down_exit", "key": "gap_down_limit", "unit": "%"},
                {"label": "A 익절/손절", "toggle": "use_s1_a_tp", "keys": ["s1_a_take_profit", "s1_a_stop_loss"], "sep": "/"},
                {"label": "B 익절/손절", "toggle": "use_s1_b_tp", "keys": ["s1_b_take_profit", "s1_b_stop_loss"], "sep": "/"}
            ]}
        ]
        
        def format_val(v):
            if isinstance(v, (int, float, np.integer, np.floating)): 
                if float(v) > 1000000: return f"{int(v):,}"
                if float(v).is_integer(): return str(int(v))
                return f"{float(v):.1f}"
            return str(v)

        group_htmls = []
        for group in PARAM_STRUCTURE:
            items_html = []
            for item in group["items"]:
                label = item["label"]; toggle_key = item.get("toggle"); val_key = item.get("key"); val_keys = item.get("keys")
                unit = item.get("unit", ""); sep = item.get("sep", ", "); is_on = True; status_html = ""; v_list = []
                
                if toggle_key:
                    val = settings.get(toggle_key, False)
                    is_on = val
                    st_cls = "val-on" if val else "val-off"; st_txt = "ON" if val else "OFF"
                    status_html = f'<span class="p-val {st_cls}">{st_txt}</span>'
                
                ukey = item.get("unit_key")
                if ukey and ukey in settings:
                    curr_u = settings[ukey]; target_key = val_keys[0] if curr_u == "원" else val_keys[1]
                    v_list.append(format_val(settings.get(target_key, 0))); unit = curr_u
                elif val_key: v_list.append(format_val(settings.get(val_key, 0)))
                elif val_keys:
                    for k in val_keys: v_list.append(format_val(settings.get(k, 0)))
                
                value_html = f'<span class="p-val-sub">({sep.join(v_list)}{unit})</span>' if (v_list and is_on) else ""
                if toggle_key or v_list:
                    items_html.append(f'<div class="p-item"><span class="p-key">{label}</span><div class="p-val-group">{status_html}{value_html}</div></div>')
            
            if items_html:
                group_htmls.append(f'<details class="p-group"><summary class="p-group-title">{group["name"]}</summary><div class="p-grid">{"".join(items_html)}</div></details>')
        param_html = f'<div class="p-container">{"".join(group_htmls)}</div>'

    # [2] 데이터 정리
    trades = []
    for pos in history:
        # 단일 종목의 진입-청산 라이프사이클 추출
        ticker = pos.get('code', '000000')
        name = pos.get('name', 'Unknown')
        entry_time = pos.get('entry_time', '-')
        exit_time = pos.get('exit_time', '-')
        profit_pct = pos.get('final_pnl_pct', 0.0)
        profit_amt = pos.get('realized_pnl_amt', 0)
        
        events_html = ""
        for ev in pos.get('events', []):
            ev_time = ev.get('time', '-')
            action = ev.get('action', 'N/A')
            price = ev.get('price', 0)
            qty = ev.get('qty', 0)
            sn = ev.get('snapshot', {})
            
            # 스냅샷 기반 근거 카드 생성
            sn_html = ""
            if sn:
                m_trend = sn.get('market_trend', 'NEUTRAL')
                m_color = "#f7768e" if m_trend == "DOWN" else "#9ece6a" if m_trend == "UP" else "#7aa2f7"
                sn_html = f"""
                <div class="snapshot-card">
                    <div class="sn-item"><span>시장:</span> <b style="color:{m_color}">{m_trend}</b></div>
                    <div class="sn-item"><span>RSI:</span> <b>{sn.get('rsi', '-')}</b></div>
                    <div class="sn-item"><span>체결강도:</span> <b>{sn.get('vol_power', 100):.1f}%</b></div>
                    <div class="sn-item"><span>매도벽(B/A):</span> <b>{sn.get('bid_ask_ratio', '-')}</b></div>
                    <div class="sn-item"><span>이평:</span> <b>{sn.get('ma_alignment', '-')}</b></div>
                    <div class="sn-item"><span>ADX:</span> <b>{sn.get('adx', '-')}</b></div>
                    <div class="sn-reason">사유: {sn.get('reason', 'N/A')}</div>
                </div>
                """
            
            action_cls = "act-buy" if "BUY" in action else "act-sell"
            events_html += f"""
            <div class="event-row">
                <div class="event-meta">
                    <span class="ev-time">{ev_time}</span>
                    <span class="ev-action {action_cls}">{action}</span>
                    <span class="ev-price">{price:,.0f}원 ({qty}주)</span>
                </div>
                {sn_html}
            </div>
            """

        status_cls = "p-plus" if profit_pct > 0 else "p-minus"
        trades.append(f"""
        <div class="trade-card">
            <div class="trade-header">
                <div class="t-name"><b>{name}</b> <small>{ticker}</small></div>
                <div class="t-result {status_cls}">{profit_pct:+.2f}% ({int(profit_amt):+,}원)</div>
            </div>
            <div class="trade-body">
                <div class="t-timeline-label">Transaction Narrative:</div>
                {events_html}
            </div>
        </div>
        """)

    # [2] 통계 계산
    win_cnt = sum(1 for p in history if p.get('final_pnl_pct', 0) > 0)
    win_rate = (win_cnt / len(history)) * 100 if history else 0
    total_pnl = sum(p.get('realized_pnl_amt', 0) for p in history)
    avg_pnl_pct = sum(p.get('final_pnl_pct', 0) for p in history) / len(history) if history else 0

    # [3] HTML 템플릿
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>가상 매매 나러티브 리포트</title>
        <style>
            :root {{
                --bg: #1a1b26;
                --card: #24283b;
                --text: #787c99;
                --text-bright: #a9b1d6;
                --primary: #7aa2f7;
                --plus: #9ece6a;
                --minus: #f7768e;
                --accent: #bb9af7;
                --border: #414868;
            }}
            body {{
                background: var(--bg);
                color: var(--text-bright);
                font-family: 'Pretendard', 'Malgun Gothic', sans-serif;
                margin: 0; padding: 30px;
                line-height: 1.6;
            }}
            .container {{ max-width: 1000px; margin: 0 auto; }}
            header {{ margin-bottom: 40px; border-bottom: 1px solid var(--border); padding-bottom: 20px; }}
            h1 {{ margin: 0; font-weight: 300; color: var(--primary); letter-spacing: -1px; }}
            .stats-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 15px; margin-bottom: 40px; }}
            .stat-box {{ background: var(--card); padding: 20px; border-radius: 12px; text-align: center; border: 1px solid var(--border); }}
            .stat-val {{ display: block; font-size: 24px; font-weight: 700; color: #fff; }}
            .stat-label {{ font-size: 13px; color: var(--text); }}
            
            .trade-section {{ display: flex; flex-direction: column; gap: 20px; }}
            .trade-card {{ background: var(--card); border-radius: 16px; overflow: hidden; border: 1px solid var(--border); transition: transform 0.2s; }}
            .trade-card:hover {{ transform: translateY(-3px); border-color: var(--primary); }}
            .trade-header {{ background: rgba(0,0,0,0.2); padding: 15px 25px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); }}
            .t-name {{ font-size: 18px; }}
            .t-name small {{ color: var(--text); margin-left: 8px; font-family: monospace; }}
            .t-result {{ font-size: 20px; font-weight: 800; }}
            
            .trade-body {{ padding: 20px 25px; }}
            .t-timeline-label {{ font-size: 12px; text-transform: uppercase; color: var(--text); margin-bottom: 15px; letter-spacing: 1px; }}
            
            .event-row {{ margin-bottom: 25px; position: relative; padding-left: 20px; border-left: 2px solid var(--border); }}
            .event-row::before {{ content: ''; position: absolute; left: -7px; top: 5px; width: 12px; height: 12px; background: var(--card); border: 2px solid var(--primary); border-radius: 50%; }}
            .event-meta {{ margin-bottom: 10px; display: flex; align-items: center; gap: 12px; }}
            .ev-time {{ color: var(--accent); font-family: monospace; font-size: 14px; }}
            .ev-action {{ padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 800; }}
            .act-buy {{ background: rgba(158, 206, 106, 0.15); color: var(--plus); border: 1px solid var(--plus); }}
            .act-sell {{ background: rgba(247, 118, 142, 0.15); color: var(--minus); border: 1px solid var(--minus); }}
            .ev-price {{ font-weight: 500; font-size: 15px; }}
            
            .snapshot-card {{ background: rgba(0,0,0,0.15); padding: 12px 18px; border-radius: 8px; display: flex; flex-wrap: wrap; gap: 15px; font-size: 13px; }}
            .sn-item span {{ color: var(--text); margin-right: 5px; }}
            .sn-reason {{ width: 100%; border-top: 1px solid var(--border); margin-top: 5px; padding-top: 5px; color: var(--accent); font-style: italic; }}
            
            .p-plus {{ color: var(--plus); }}
            .p-minus {{ color: var(--minus); }}
            
            /* [v18.1] 파라미터 컨테이너 스타일 (trade_report.py와 통일) */
            .p-container {{ margin-bottom: 40px; display: flex; flex-direction: column; gap: 10px; }}
            .p-group {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 12px 18px; }}
            .p-group-title {{ font-size: 0.9rem; font-weight: 700; color: var(--primary); cursor: pointer; outline: none; list-style: none; }}
            .p-group-title::-webkit-details-marker {{ display: none; }}
            .p-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 10px; padding-top: 12px; margin-top: 8px; border-top: 1px solid var(--border); }}
            .p-item {{ display: flex; justify-content: space-between; align-items: center; font-size: 13px; }}
            .p-key {{ color: var(--text); }}
            .p-val-group {{ display: flex; gap: 8px; align-items: center; }}
            .val-on {{ color: var(--plus); background: rgba(158, 206, 106, 0.1); padding: 1px 6px; border-radius: 4px; font-size: 11px; }}
            .val-off {{ color: var(--text); background: rgba(120, 124, 153, 0.1); padding: 1px 6px; border-radius: 4px; font-size: 11px; }}
            .p-val-sub {{ color: var(--primary); font-family: monospace; font-weight: 600; }}

            footer {{ margin-top: 50px; text-align: center; color: var(--text); font-size: 12px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <header>
                <h1>Virtual Trade Narrative Report</h1>
                <p style="color:var(--text); margin:5px 0 0 0;">가상 매매 의사결정 시점의 시장 데이터 분석 및 복기 리포트</p>
            </header>
            
            <div class="stats-grid">
                <div class="stat-box">
                    <span class="stat-val">{len(history)}</span>
                    <span class="stat-label">Total Trades</span>
                </div>
                <div class="stat-box">
                    <span class="stat-val { 'p-plus' if win_rate >= 50 else 'p-minus' }">{win_rate:.1f}%</span>
                    <span class="stat-label">Win Rate</span>
                </div>
                <div class="stat-box">
                    <span class="stat-val { 'p-plus' if total_pnl > 0 else 'p-minus' }">{int(total_pnl):+,}</span>
                    <span class="stat-label">Total P&L (KRW)</span>
                </div>
                <div class="stat-box">
                    <span class="stat-val { 'p-plus' if avg_pnl_pct > 0 else 'p-minus' }">{avg_pnl_pct:+.2f}%</span>
                    <span class="stat-label">Avg Return</span>
                </div>
            </div>

            {f'<div style="color:var(--text); font-size:12px; margin-bottom:10px;">Execution Strategy Parameters:</div>' if settings else ""}
            {param_html}
            
            <div style="color:var(--text); font-size:12px; margin-bottom:10px; margin-top:30px;">Trade Timeline & Narratives:</div>
            <div class="trade-section">
                {"".join(trades[::-1])}
            </div>
            
            <footer>
                © 2026 Antigravity Trading System | Report Generated at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            </footer>
        </div>
    </body>
    </html>
    """
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"✅ Narrative Report generated: {output_path}")
