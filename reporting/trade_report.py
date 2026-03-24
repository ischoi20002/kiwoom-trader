import os
import json
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import sys

# Add root directory to sys.path for absolute imports of root modules
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.append(root_dir)

from strategy.data_loader import BacktestDataLoader

def generate_trade_report(df, interval, output_path="trade_report.html", total_trades=0, win_rate=0, avg_ret=0, best_settings=None, market_df=None, vix_df=None, callback=None, target_codes=None, start_date=None, end_date=None, daily_equity=None, high_precision=False):
    """매매 실행 내역 HTML 리포트 생성 (v8.1) - 고정밀 모드 표시 지원"""
    # [v8.2] 표시용 인터벌 조정
    display_interval = "1m" if high_precision else interval
    # [v8.1] 모드 텍스트 결정
    mode_label = "고정밀(1분봉)" if high_precision else f"일반({display_interval})"
    mode_color = "#38bdf8" if high_precision else "#94a3b8"
    if df.empty:
        # [v15.4.3] No data found in DF
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(f"<html><body style='background:#0f172a; color:white; padding:50px; text-align:center;'><h1>매매 내역이 전혀 없습니다.</h1><p>데이터가 비어 있습니다.</p><p>Path: {output_path}</p></body></html>")
            print(f"DEBUG [generate_trade_report]: Empty DF, placeholder written to {output_path}")
        except Exception as e:
            print(f"DEBUG [generate_trade_report]: Error writing placeholder: {e}")
        return

    # [v15.4.0] Aggressive filtering to ensure ONLY target stocks are shown
    debug_msg = "Filter Disabled (target_codes is None)"
    if target_codes is not None:
        codes_set = set([str(c).strip().zfill(6) for c in target_codes])
        before_count = len(df)
        df = df[df['Code'].astype(str).str.strip().str.zfill(6).isin(codes_set)].copy()
        after_count = len(df)
        debug_msg = f"Filter Active: {len(codes_set)} stocks | Before: {before_count} rows | After: {after_count} rows"
        
        if df.empty:
            try:
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(f"<html><body style='background:#0f172a; color:white; padding:50px; text-align:center;'><h1>매매 내역 없음</h1><p>{debug_msg}</p><p>CWD: {os.getcwd()}</p></body></html>")
            except: pass
            return
    
    # [v15.5] 파라미터 그룹화 정의
    PARAM_GROUPS = {
        "🔍 [1] 종목 선정 (Stock Selection)": [
            "min_trde_val", "vol_ratio", "vol_lookback", "ma_convergence", "sel_rsi_min", "sel_rsi_max", 
            "macd_fast", "macd_slow", "macd_sig", "rsi_window", "rsi_sig_period", "vol_power_lvl"
        ],
        "🚀 [2] 신규 매수 (Stage 1 Entry)": [
            "stage1_amt", "use_type_a", "buy_rsi_max_a", "use_gc", "use_vol_surge", "vol_surge", 
            "use_ma60_safe", "ma60_min", "ma60_max", "use_type_b", "buy_rsi_max_b", "vol_surge_b", 
            "use_type_b_vol", "use_macd_gc_b", "use_trend_aligned_b", "use_market_filter", "use_gap_down_filter", "gap_down_limit"
        ],
        "💰 [3] 추가 매수 (Stage 2 Add-on)": [
            "stage2_amt", "stage2_trigger", "s2_delay_hours", "use_s2_ma5", "s2_ma5_gap", "use_s2_profit", 
            "s2_profit_limit", "use_s2_rsi_sig", "s2_rsi_gap", "use_s2_macd_gc", 
            "s2_macd_gap", "use_s2_vol_surge", "s2_vol_ratio", "use_s2_ma20", "s2_ma20_gap"
        ],
        "🛡️ [4] 매도 및 손절 (Exit Strategy)": [
            "use_s1_flash_exit", "s1_a_flash_exit_pct", "s1_b_flash_exit_pct", "use_s1_stop_loss", 
            "s1_a_stop_loss", "s1_b_stop_loss", "use_s1_tp", "s1_a_take_profit", "s1_b_take_profit", 
        ]
    }

    # [v15.6] 파라미터 값 포맷팅 함수
    def format_val(v):
        if isinstance(v, (int, float, np.integer, np.floating)):
            if float(v).is_integer(): return str(int(v))
            return f"{float(v):.1f}"
        return str(v)

    # 파라미터 섹션 생성
    param_html = ""
    if best_settings:
        group_htmls = []
        assigned_keys = set()
        
        for group_name, keys in PARAM_GROUPS.items():
            items = []
            for k in keys:
                if k in best_settings:
                    v = best_settings[k]
                    assigned_keys.add(k)
                    if isinstance(v, bool):
                        val_class = "val-on" if v else "val-off"
                        val_text = "ON" if v else "OFF"
                        items.append(f'<div class="p-item"><span class="p-key">{k}</span><span class="p-val {val_class}">{val_text}</span></div>')
                    else:
                        items.append(f'<div class="p-item"><span class="p-key">{k}</span><span class="p-val">{format_val(v)}</span></div>')
            
            if items:
                # [v15.6] Details/Summary를 이용한 접기/펼치기 구현 (기본: 닫힘)
                group_htmls.append(f"""
                <details class="p-group">
                    <summary class="p-group-title">{group_name}</summary>
                    <div class="p-grid">{"".join(items)}</div>
                </details>
                """)
        
        # 정의되지 않은 나머지 파라미터들
        others = []
        for k, v in best_settings.items():
            if k not in assigned_keys:
                if isinstance(v, bool):
                    val_class = "val-on" if v else "val-off"
                    val_text = "ON" if v else "OFF"
                    others.append(f'<div class="p-item"><span class="p-key">{k}</span><span class="p-val {val_class}">{val_text}</span></div>')
                else:
                    others.append(f'<div class="p-item"><span class="p-key">{k}</span><span class="p-val">{format_val(v)}</span></div>')
        
        if others:
            group_htmls.append(f"""
            <details class="p-group">
                <summary class="p-group-title">⚙️ 기타 설정 (General)</summary>
                <div class="p-grid">{"".join(others)}</div>
            </details>
            """)
            
        param_html = f'<div class="p-container">{"".join(group_htmls)}</div>'

    html_template = """
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8"><title>Backtest Analysis Report</title>
        <link href="https://fonts.googleapis.com/css2?family=Pretendard:wght@400;600;700&display=swap" rel="stylesheet">
        <script src="https://cdn.plot.ly/plotly-2.24.1.min.js"></script>
        <style>
            :root {{ 
                --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --dim: #94a3b8; 
                --buy: #38bdf8; --sell: #fbbf24; --gain: #4ade80; --loss: #f87171; 
                --primary: #3b82f6; --sidebar-w: 280px;
            }}
            body {{ font-family: 'Pretendard', sans-serif; background: var(--bg); color: var(--text); margin: 0; display: flex; height: 100vh; overflow: hidden; }}
            .sidebar {{ width: var(--sidebar-w); background: #0a0f1d; border-right: 1px solid #1e293b; display: flex; flex-direction: column; flex-shrink: 0; }}
            .sidebar-header {{ padding: 25px 20px; border-bottom: 2px solid #1e293b; background: rgba(59,130,246,0.05); }}
            .sidebar-title {{ font-size: 1.2rem; font-weight: 800; color: var(--primary); display: flex; align-items: center; gap: 10px; text-transform: uppercase; letter-spacing: 1px; }}
            .nav-list {{ list-style: none; padding: 15px; margin: 0; overflow-y: auto; flex: 1; }}
            .nav-item {{ margin-bottom: 6px; }}
            .nav-item a {{ display: flex; justify-content: space-between; padding: 12px 15px; color: var(--dim); text-decoration: none; border-radius: 10px; font-size: 0.9rem; transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1); border: 1px solid transparent; }}
            .nav-item a:hover {{ background: #1e293b; color: white; border-color: #334155; transform: translateX(5px); }}
            .nav-item .code-badge {{ background: #334155; color: #cbd5e1; font-size: 0.7rem; padding: 2px 6px; border-radius: 4px; font-weight: 600; }}
            
            .main-content {{ flex: 1; overflow-y: auto; padding: 40px; scroll-behavior: smooth; background: radial-gradient(circle at top right, #1e293b11, transparent); }}
            .container {{ max-width: 1300px; margin: 0 auto; }}
            
            /* 파라미터 그룹 스타일 [v15.6] */
            .p-container {{ margin-bottom: 40px; display: flex; flex-direction: column; gap: 15px; }}
            .p-group {{ background: #1e293bCC; border: 1px solid #334155; border-radius: 16px; padding: 15px 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); cursor: default; }}
            .p-group[open] {{ background: #1e293b; border-color: var(--primary); }}
            .p-group-title {{ font-size: 0.95rem; font-weight: 700; color: var(--buy); list-style: none; outline: none; cursor: pointer; display: flex; justify-content: space-between; align-items: center; }}
            .p-group-title::-webkit-details-marker {{ display: none; }}
            .p-group-title::after {{ content: '📂'; font-size: 0.8rem; opacity: 0.5; transition: 0.2s; }}
            .p-group[open] .p-group-title::after {{ content: '📖'; opacity: 1; transform: rotate(180deg); }}
            .p-group-title:hover {{ opacity: 0.8; }}
            
            .p-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px; padding-top: 15px; border-top: 1px solid #334155; margin-top: 10px; }}
            .p-item {{ display: flex; justify-content: space-between; align-items: center; background: rgba(15, 23, 42, 0.4); padding: 8px 12px; border-radius: 8px; border: 1px solid #33415566; }}
            .p-key {{ font-size: 0.8rem; color: var(--dim); font-family: 'JetBrains Mono', monospace; }}
            .p-val {{ font-size: 0.9rem; font-weight: 600; color: var(--text); }}
            .val-on {{ color: #4ade80; background: rgba(74, 222, 128, 0.1); padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; }}
            .val-off {{ color: #94a3b8; background: rgba(148, 163, 184, 0.1); padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; }}
            
            .header-flex {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 35px; border-bottom: 1px solid #1e293b; padding-bottom: 20px; }}
            h1 {{ margin: 0; font-size: 2.2rem; font-weight: 900; letter-spacing: -1px; }}
            .mode-badge-top {{ font-size: 0.8rem; background: {mode_color}; color: #0f172a; padding: 4px 12px; border-radius: 8px; font-weight: 800; }}
            
            .stats {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 15px; margin-bottom: 40px; }}
            .stat-card {{ background: linear-gradient(135deg, #1e293b, #0f172a); padding: 20px 15px; border-radius: 16px; border: 1px solid #334155; position: relative; overflow: hidden; }}
            .stat-card::after {{ content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 4px; background: var(--primary); opacity: 0.5; }}
            .stat-card div:first-child {{ color: var(--dim); font-size: 0.8rem; font-weight: 600; margin-bottom: 8px; }}
            .stat-val {{ font-size: 1.5rem; font-weight: 800; font-variant-numeric: tabular-nums; }}
            
            /* [v27.0] 전략별 진입 통계 스타일 */
            .strategy-stats {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; margin-bottom: 25px; }}
            .strategy-card {{ 
                background: #1e293b; padding: 15px; border-radius: 12px; border: 1px solid #334155;
                display: flex; justify-content: space-between; align-items: center;
            }}
            .str-label {{ font-size: 0.9rem; font-weight: 700; color: var(--dim); }}
            .str-count {{ font-size: 1.2rem; font-weight: 800; color: var(--primary); }}
            .str-count.type-a {{ color: #4ade80; }}
            .str-count.type-b {{ color: #38bdf8; }}
            .str-count.type-s2 {{ color: #fbbf24; }}
            .str-count.s1-color {{ color: #94a3b8; }}
            .str-count.s2-color {{ color: #fb7185; }}
            
            .stock-group {{ background: #1e293b88; padding: 30px; border-radius: 24px; margin-bottom: 60px; border: 1px solid #334155; backdrop-filter: blur(10px); }}
            .stock-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 25px; border-bottom: 2px solid #334155; padding-bottom: 15px; }}
            .stock-header-title {{ font-size: 1.5rem; font-weight: 800; color: #fff; display: flex; align-items: center; gap: 12px; }}
            .stock-header-code {{ color: var(--buy); font-family: monospace; font-size: 1.1rem; }}
            
            table {{ width: 100%; border-collapse: separate; border-spacing: 0; margin-top: 15px; }}
            th {{ color: var(--dim); padding: 15px 12px; text-align: left; font-size: 0.75rem; border-bottom: 2px solid #334155; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; }}
            td {{ padding: 16px 12px; border-bottom: 1px solid #1e293b; font-size: 0.9rem; font-variant-numeric: tabular-nums; }}
            tr:last-child td {{ border-bottom: none; }}
            tr:hover td {{ background: rgba(59, 130, 246, 0.03); }}
            .action-buy {{ color: var(--buy); font-weight: 700; background: rgba(56,189,248,0.1); padding: 2px 6px; border-radius: 4px; }}
            .action-sell {{ color: var(--sell); font-weight: 700; background: rgba(245,158,11,0.1); padding: 2px 6px; border-radius: 4px; }}
            .action-hold {{ color: #a855f7; font-weight: 700; background: rgba(168,85,247,0.1); padding: 2px 6px; border-radius: 4px; }}
            .profit {{ color: var(--gain); font-weight: 700; }}
            .loss {{ color: var(--loss); font-weight: 700; }}
            .reason-text {{ color: var(--dim); font-size: 0.75rem; cursor: help; border-bottom: 1px dotted #444; display: block; padding: 2px 0; }}
            .reason-cell {{ width: 100%; position: relative; }}
            .indicator-panel {{ 
                font-size: 0.75rem; margin-top: 5px; background: #1e293b; 
                padding: 12px; border-radius: 10px; display: none; 
                position: absolute; left: 0; top: 100%; z-index: 100; 
                border: 1px solid #3b82f6aa; box-shadow: 0 10px 25px -10px rgba(0,0,0,0.8);
                min-width: 180px; pointer-events: none;
                backdrop-filter: blur(8px);
            }}
            .reason-cell:hover .indicator-panel {{ display: block; }}
            .chart-cell {{ position: relative; cursor: crosshair; display: inline-block; vertical-align: middle; width: 100px; height: 40px; }}
            .bar-trigger {{ position: absolute; top: 0; width: 18px; height: 100%; opacity: 0; z-index: 10; }}
            .chart-panel {{ 
                font-size: 0.72rem; background: #1e293b; padding: 12px; border-radius: 8px; 
                display: none; position: absolute; left: 100%; top: 50%; transform: translateY(-50%); 
                z-index: 60; border: 1px solid #334155; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.5); 
                min-width: 150px; pointer-events: none; margin-left: 10px;
            }}
            /* Selective display based on which trigger is hovered */
            .bt-0:hover ~ .cp-0 {{ display: block; }}
            .bt-1:hover ~ .cp-1 {{ display: block; }}
            .bt-2:hover ~ .cp-2 {{ display: block; }}
            .bt-3:hover ~ .cp-3 {{ display: block; }}
            .bt-4:hover ~ .cp-4 {{ display: block; }}
            
            .chart-val-row {{ display: flex; justify-content: space-between; gap: 15px; border-bottom: 1px solid #334155; padding: 3px 0; }}
            .chart-val-label {{ color: var(--dim); }}
            .chart-val-value {{ color: white; font-weight: bold; }}
            tr:hover {{ background: rgba(255,255,255,0.03); }}
            ::-webkit-scrollbar {{ width: 8px; }}
            ::-webkit-scrollbar-track {{ background: var(--bg); }}
            ::-webkit-scrollbar-thumb {{ background: #334155; border-radius: 10px; }}
            ::-webkit-scrollbar-thumb:hover {{ background: #475569; }}
        </style>
    </head>
    <body>
        <div class="sidebar">
            <div class="sidebar-header"><div class="sidebar-title">📊 Stock List</div></div>
            <ul class="nav-list">{sidebar_items}</ul>
        </div>
        <div class="main-content">
            <div class="container">
                <div class="header-flex">
                    <h1>Trade Execution Detailed Log <span class="mode-badge-top">{mode_label}</span></h1>
                    <div style="color:var(--dim)">{now} | {start_date} ~ {end_date} | {interval}</div>
                </div>
                <div class="stats">
                    <div class="stat-card"><div>Total Trades</div><div class="stat-val">{total}</div></div>
                    <div class="stat-card"><div>Win Rate</div><div class="stat-val" style="color:var(--gain)">{win_rate:.1f}%</div></div>
                    <div class="stat-card"><div>Avg Return</div><div class="stat-val" style="color:{ret_color}">{avg_ret:+.2f}%</div></div>
                    <div class="stat-card"><div>Total Profit (Gross)</div><div class="stat-val" style="color:{gross_profit_color}">{gross_profit:+,}</div></div>
                    <div class="stat-card"><div>Total Fees</div><div class="stat-val" style="color:var(--loss)">-{total_fees:,}</div></div>
                    <div class="stat-card" style="border: 2px solid {total_profit_color}; background: rgba(0,255,127,0.05);">
                        <div style="font-weight:bold;">Net Profit</div>
                        <div class="stat-val" style="color:{total_profit_color}; font-size:1.8rem; font-weight:800;">{total_profit:+,} <small style="font-size:0.8rem;">KRW</small></div>
                    </div>
                </div>
                {strategy_stats_html}
                {equity_chart_html}
                <div style="margin-top:20px; color:var(--dim); font-size:0.85rem; margin-bottom:5px;">Strategy Parameters</div>
                {param_html}
                {content}
            </div>
        </div>
        <script>
            // [v16.7] Tooltip position correction for small charts and reason panels
            document.addEventListener('mouseover', (e) => {{
                if (e.target.classList.contains('bar-trigger')) {{
                    const cell = e.target.closest('.chart-cell');
                    if (!cell) return;
                    const panelIdx = e.target.className.split('bt-')[1];
                    const panel = cell.querySelector('.cp-' + panelIdx);
                    if (panel) {{
                        const rect = panel.getBoundingClientRect();
                        if (rect.right > window.innerWidth) {{
                            panel.style.left = 'auto';
                            panel.style.right = '100%';
                            panel.style.marginLeft = '0';
                            panel.style.marginRight = '10px';
                        }}
                    }}
                }}
                
                // [v16.9] 상세 사유 툴팁 위치 자동 보정
                const reasonCell = e.target.closest('.reason-cell');
                if (reasonCell) {{
                    const panel = reasonCell.querySelector('.indicator-panel');
                    if (panel) {{
                        // 임시로 보이게 해서 크기 측정 (CSS hover와 충돌 방지)
                        panel.style.display = 'block';
                        panel.style.visibility = 'hidden';
                        
                        const rect = panel.getBoundingClientRect();
                        const winH = window.innerHeight;
                        const winW = window.innerWidth;
                        
                        // 하단 잘림 방지
                        if (rect.bottom > winH) {{
                            panel.style.top = 'auto';
                            panel.style.bottom = '100%';
                            panel.style.marginBottom = '15px';
                        }} else {{
                            panel.style.top = '100%';
                            panel.style.bottom = 'auto';
                            panel.style.marginBottom = '0';
                        }}
                        
                        // 우측 잘림 방지
                        if (rect.right > winW) {{
                            panel.style.left = 'auto';
                            panel.style.right = '0';
                        }} else {{
                            panel.style.left = '0';
                            panel.style.right = 'auto';
                        }}
                        
                        panel.style.visibility = 'visible';
                        panel.style.display = ''; // CSS 클래스 제어로 복구
                    }}
                }}
            }});
        </script>
    </body></html>
    """

    base_dir = os.path.dirname(os.path.abspath(output_path))

    # Profit calculations
    total_profit_val = 0
    if not df.empty:
        for c in df['Code'].unique():
            stock_trades = df[df['Code'] == c]
            s_profit = 0
            for _, tr in stock_trades.iterrows():
                amt = float(tr['Price']) * float(tr['Qty'])
                fee = float(tr.get('Fee', 0)) if not pd.isna(tr.get('Fee', 0)) else 0
                if "BUY" in str(tr['Action']): s_profit -= (amt + fee)
                else: s_profit += (amt - fee)
            total_profit_val += s_profit
    
    total_fees = df['Fee'].fillna(0).sum() if 'Fee' in df.columns else 0
    total_fees = int(total_fees) if not pd.isna(total_fees) else 0
    total_profit_val = int(total_profit_val) if not pd.isna(total_profit_val) else 0
    total_profit_color = "var(--gain)" if total_profit_val >= 0 else "var(--loss)"
    total_fees_val = total_fees
    gross_profit_val = total_profit_val + total_fees_val
    gross_profit_color = "var(--gain)" if gross_profit_val >= 0 else "var(--loss)"
    
    # [v27.0] Strategy Execution Breakdown - More robust counting (Prefix prioritized)
    count_a = len(df[(df['Action'].str.startswith('BUY', na=False)) & (df['Reason'].str.contains('TYPE_A', case=False, na=False))])
    count_b = len(df[(df['Action'].str.startswith('BUY', na=False)) & (df['Reason'].str.contains('TYPE_B', case=False, na=False))])
    count_s2 = len(df[(df['Action'].str.contains('BUY2', na=False)) | (df['Action'] == 'ADDON') | (df['Reason'].str.contains('Stage 2|ADDON', case=False, na=False))])
    
    # [v27.3] Performance by Strategy Depth (S1 Only vs S2 Reached)
    s1_rets, s2_rets = [], []
    for c in df['Code'].unique():
        s_df = df[df['Code'] == c]
        has_s2 = False
        for _, r in s_df.iterrows():
            action = str(r['Action'])
            if "BUY" in action:
                if any(x in action for x in ["BUY2", "BUY_2ND", "ADDON", "S2"]):
                    has_s2 = True
            elif "SELL" in action:
                ret = float(r.get('Return', 0))
                if has_s2: s2_rets.append(ret)
                else: s1_rets.append(ret)
                has_s2 = False 

    avg_s1 = sum(s1_rets)/len(s1_rets) if s1_rets else 0
    avg_s2 = sum(s2_rets)/len(s2_rets) if s2_rets else 0

    strategy_stats_html = f"""
    <div style="color:var(--dim); font-size:0.85rem; margin-bottom:10px;">Strategy Entry Breakdown (진입 횟수)</div>
    <div class="strategy-stats">
        <div class="strategy-card">
            <span class="str-label">Type A (눌림목)</span>
            <span class="str-count type-a">{count_a} <small style="font-size:0.7rem; color:var(--dim)">건</small></span>
        </div>
        <div class="strategy-card">
            <span class="str-label">Type B (추세돌파)</span>
            <span class="str-count type-b">{count_b} <small style="font-size:0.7rem; color:var(--dim)">건</small></span>
        </div>
        <div class="strategy-card">
            <span class="str-label">Stage 2 (추가매수)</span>
            <span class="str-count type-s2">{count_s2} <small style="font-size:0.7rem; color:var(--dim)">건</small></span>
        </div>
    </div>
    
    <div style="color:var(--dim); font-size:0.85rem; margin-bottom:10px; margin-top:20px;">Strategy Depth Performance (수익률 집계)</div>
    <div class="strategy-stats">
        <div class="strategy-card">
            <span class="str-label">Stage 1 Only (정찰)</span>
            <span class="str-count s1-color">{avg_s1:+.2f}% <small style="font-size:0.7rem; color:var(--dim)">({len(s1_rets)}건)</small></span>
        </div>
        <div class="strategy-card">
            <span class="str-label">Stage 2 Reached (추매)</span>
            <span class="str-count s2-color">{avg_s2:+.2f}% <small style="font-size:0.7rem; color:var(--dim)">({len(s2_rets)}건)</small></span>
        </div>
        <div class="strategy-card" style="border: 1px dashed #fb718555;">
            <span class="str-label">Alpha (추매 가산점)</span>
            <span class="str-count" style="color:#fff;">{avg_s2 - avg_s1:+.2f}%</span>
        </div>
    </div>
    """

    # Portfolio Equity Chart
    equity_chart_html = ""
    if daily_equity:
        try:
            # [v15.5.0] Sort by date to avoid plot artifacts
            daily_equity = sorted(daily_equity, key=lambda x: x['date'])
            dates = [d['date'] for d in daily_equity]
            equities = [d['equity'] for d in daily_equity]
            cashes = [d['cash'] for d in daily_equity]
            holdings = [max(0, d['equity'] - d['cash']) for d in daily_equity]
            
            # [v15.5.1] Synchronize Y-axes to ensure the red line tracks the top of the stacked area
            max_val = max(equities) * 1.05
            
            equity_chart_html = f"""
            <div style="background:var(--card-bg); border-radius:12px; padding:20px; margin-top:20px; border:1px solid #333;">
                <h3 style="margin-top:0; margin-bottom:15px; font-size:1.1rem; color:var(--dim);">📈 포트폴리오 자산 추이 및 비중 분석</h3>
                <div id="equity-chart" style="height:400px; width:100%;"></div>
                <script>
                    const traceEquity = {{
                        x: {json.dumps(dates)}, y: {json.dumps(equities)},
                        name: '총 자본 (평가액)', type: 'scatter', mode: 'lines',
                        line: {{ color: '#f43f5e', width: 3 }}, yaxis: 'y2'
                    }};
                    const traceCash = {{
                        x: {json.dumps(dates)}, y: {json.dumps(cashes)},
                        name: '현금 잔고', type: 'scatter', mode: 'none', fill: 'tozeroy',
                        fillcolor: 'rgba(56, 189, 248, 0.2)', stackgroup: 'one'
                    }};
                    const traceHoldings = {{
                        x: {json.dumps(dates)}, y: {json.dumps(holdings)},
                        name: '주식 보유금액', type: 'scatter', mode: 'none', fill: 'tonexty',
                        fillcolor: 'rgba(245, 158, 11, 0.4)', stackgroup: 'one'
                    }};
                    const layout = {{
                        paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
                        margin: {{ t: 10, b: 40, l: 60, r: 60 }},
                        legend: {{ orientation: 'h', x: 0.5, xanchor: 'center', y: 1.1, font: {{ color: '#ccc' }} }},
                        xaxis: {{ gridcolor: '#333', tickfont: {{ color: '#888' }} }},
                        yaxis: {{ 
                            gridcolor: '#333', tickfont: {{ color: '#888' }}, 
                            title: '자산 구성 (원)', side: 'left',
                            range: [0, {max_val}]
                        }},
                        yaxis2: {{ 
                            overlaying: 'y', side: 'right', showgrid: false, 
                            tickfont: {{ color: '#f43f5e' }}, title: '총 자산 (원)',
                            range: [0, {max_val}]
                        }},
                        hovermode: 'x unified'
                    }};
                    Plotly.newPlot('equity-chart', [traceCash, traceHoldings, traceEquity], layout, {{ responsive: true, displayModeBar: false }});
                </script>
            </div>
            """
        except Exception as e:
            equity_chart_html = f"<p style='color:red'>Chart generation error: {e}</p>"

    # Market Indicators for context
    msi, adx, vix_series = None, None, None
    if market_df is not None and not market_df.empty:
        try:
            m_ma20 = market_df['Close'].rolling(20).mean()
            m_ma60 = market_df['Close'].rolling(60).mean()
            m_ma120 = market_df['Close'].rolling(120).mean()
            msi = ((m_ma20 / m_ma60 - 1) + (m_ma60 / m_ma120 - 1)) * 100
            
            def _calc_adx(df, n=14):
                h, l, c = df['High'], df['Low'], df['Close']
                tr = pd.concat([h-l, abs(h-c.shift(1)), abs(l-c.shift(1))], axis=1).max(axis=1)
                pdm = np.where((h-h.shift(1))>(l.shift(1)-l), np.maximum(h-h.shift(1), 0), 0)
                mdm = np.where((l.shift(1)-l)>(h-h.shift(1)), np.maximum(l.shift(1)-l, 0), 0)
                trs, pdms, mdms = tr.rolling(n).sum(), pd.Series(pdm, index=df.index).rolling(n).sum(), pd.Series(mdm, index=df.index).rolling(n).sum()
                pdi, mdi = 100*(pdms/trs), 100*(mdms/trs)
                return 100*(abs(pdi-mdi)/(pdi+mdi)).rolling(n).mean()
            adx = _calc_adx(market_df)
            if vix_df is not None and not vix_df.empty:
                vix_series = vix_df['Close'] if 'Close' in vix_df.columns else vix_df.iloc[:, 0]
        except: pass

    loader = BacktestDataLoader()
    content = []
    sidebar_items = []
    sorted_codes = sorted(df['Code'].unique())
    
    if callback: callback("매매 리포트 생성 중...", 95)

    for code in sorted_codes:
        group = df[df['Code'] == code].sort_values(by='Date')
        name = group['Name'].iloc[0]
        code_str = str(code).zfill(6)
        sidebar_items.append(f'<li class="nav-item"><a href="#stock-{code_str}">{name} ({code_str})</a></li>')
        
        chart_link_html = f'<a href="https://finance.naver.com/item/main.naver?code={code_str}" target="_blank" style="text-decoration:none;"><button style="background:var(--buy); color:white; border:none; padding:5px 12px; border-radius:5px; cursor:pointer; font-size:0.85rem; margin-left:15px;">📊 네이버 증권 차트</button></a>'

        group_rows = []
        stock_realized_profit = 0
        price_df = loader.get_data(code_str, (group['Date'].min() - timedelta(days=5)).strftime('%Y-%m-%d'), 
                                 (group['Date'].max() + timedelta(days=5)).strftime('%Y-%m-%d'), interval=interval)
        
        for _, r in group.iterrows():
            status_badge = "-"
            try:
                if msi is not None and adx is not None and vix_series is not None:
                    m_idx = msi.index.get_indexer([r['Date']], method='pad')[0]
                    if m_idx != -1:
                        m_val, a_val, v_val = msi.iloc[m_idx], adx.iloc[m_idx], vix_series.iloc[m_idx]
                        if m_val > 0 and a_val > 25 and v_val < 20:
                            status_badge = '<span style="background:#22c55e; color:white; padding:2px 6px; border-radius:4px; font-weight:bold; font-size:0.75rem;">AGGRESSIVE</span>'
                        elif m_val > 0 and v_val < 20:
                            status_badge = '<span style="background:#fbbf24; color:black; padding:2px 6px; border-radius:4px; font-weight:bold; font-size:0.75rem;">SAFE</span>'
                        elif m_val < 0 and v_val > 30:
                            status_badge = '<span style="background:#ef4444; color:white; padding:2px 6px; border-radius:4px; font-weight:bold; font-size:0.75rem;">RISK</span>'
                        else:
                            status_badge = '<span style="background:#f97316; color:white; padding:2px 6px; border-radius:4px; font-weight:bold; font-size:0.75rem;">WATCH</span>'
            except: pass

            amt = r['Price'] * r['Qty']
            fee = r.get('Fee', 0) if not pd.isna(r.get('Fee', 0)) else 0
            ret_str, action_cls, cum_profit_str = "", "action-buy", "-"
            
            if "BUY" not in str(r['Action']):
                ret_val = float(r.get('Return', 0))
                avg_p_rev = r['Price'] / (1 + ret_val/100) if (1 + ret_val/100) != 0 else r['Price']
                stock_realized_profit += (r['Price'] - avg_p_rev) * r['Qty'] - fee
                action_cls = "action-sell"
                if r['Action'] in ["SELL", "HOLD", "PARTIAL_SELL"]:
                    r_cls = "profit" if ret_val > 0 else "loss"
                    ret_str = f'<span class="{r_cls}">{ret_val:+.2f}%</span>'
                    if r['Action'] == "HOLD":
                        action_cls = "action-hold"
                        ret_str = f'<span class="{r_cls}">{ret_val:+.2f}% (Held)</span>'
                p_row_cls = "profit" if stock_realized_profit >= 0 else "loss"
                cum_profit_str = f'<span class="{p_row_cls}">{int(stock_realized_profit):+,}</span>'
            
            candle_svg = "-"
            if price_df is not None and not price_df.empty:
                try:
                    target_idx = price_df.index.get_indexer([r['Date']], method='nearest')[0]
                    window = price_df.iloc[max(0, target_idx-2) : min(len(price_df), target_idx+3)]
                    if not window.empty:
                        svg_h, w_high, w_low = 40, window['High'].max(), window['Low'].min()
                        diff = max(w_high - w_low, 1)
                        scale = lambda v: svg_h - ((v - w_low) / diff * svg_h)
                        
                        svg_parts, triggers, panels = [], [], []
                        for i, (idx, row) in enumerate(window.iterrows()):
                            x, color = 5 + i * 18, ("#f43f5e" if row['Close'] >= row['Open'] else "#3b82f6")
                            yh, yl, yo, yc = scale(row['High']), scale(row['Low']), scale(row['Open']), scale(row['Close'])
                            title_str = f'[{idx.strftime("%m-%d %H:%M")}]\n시가: {int(row["Open"]):,}\n고가: {int(row["High"]):,}\n저가: {int(row["Low"]):,}\n종가: {int(row["Close"]):,}'
                            svg_parts.append(f'<g><title>{title_str}</title><line x1="{x+7}" y1="{yh}" x2="{x+7}" y2="{yl}" stroke="{color}" stroke-width="1.5"/><rect x="{x}" y="{min(yo, yc)}" width="14" height="{max(1,abs(yo-yc))}" fill="{color}"/></g>')
                            
                            triggers.append(f'<div class="bar-trigger bt-{i}" style="left:{x}px;"></div>')
                            panels.append(f"""<div class="chart-panel cp-{i}">
                                <div style="color:var(--buy); font-weight:bold; margin-bottom:8px; border-bottom:1px solid #475569; padding-bottom:5px;">📊 Candle ({idx.strftime('%H:%M')})</div>
                                <div class="chart-val-row"><span class="chart-val-label">시가</span><span class="chart-val-value">{int(row['Open']):,}</span></div>
                                <div class="chart-val-row"><span class="chart-val-label">고가</span><span class="chart-val-value">{int(row['High']):,}</span></div>
                                <div class="chart-val-row"><span class="chart-val-label">저가</span><span class="chart-val-value">{int(row['Low']):,}</span></div>
                                <div class="chart-val-row"><span class="chart-val-label">종가</span><span class="chart-val-value">{int(row['Close']):,}</span></div>
                            </div>""")
                        
                        candle_svg = f'<div class="chart-cell"><svg width="100" height="40">{"".join(svg_parts)}</svg>{"".join(triggers)}{"".join(panels)}</div>'
                except: pass

            raw_reason = str(r['Reason'])
            parts = raw_reason.split("|")
            reason_main = parts[0]
            
            ind_list = []
            # [v16.9] 모든 파이프(|) 구분 항목을 툴팁에 포함
            for pair in parts[1:]:
                if ":" in pair:
                    k, v = pair.split(":", 1)
                    color = ("var(--buy)" if k == "RSI" and float(v) < 35 else "var(--loss)" if k == "RSI" and float(v) > 65 else "var(--dim)")
                    ind_list.append(f'<div style="display:flex; justify-content:space-between; gap:10px; border-bottom:1px solid #444; padding:2px 0;"><span style="color:var(--dim)">{k}</span><span style="color:{color}; font-weight:bold;">{v}</span></div>')
                else:
                    # 콜론이 없는 일반 텍스트도 추가
                    ind_list.append(f'<div style="border-bottom:1px solid #444; padding:4px 0; color:var(--text); font-size:0.7rem;">• {pair}</div>')
            
            indicators_html = ""
            if ind_list:
                indicators_html = f'<div class="indicator-panel"><div style="min-width:160px; padding:5px;">{"".join(ind_list)}</div></div>'

            group_rows.append(f"""<tr><td>{r['Date'].strftime('%m-%d %H:%M')}</td><td><span class="{action_cls}">{r['Action']}</span></td><td>{candle_svg}</td><td class="num">{int(r['Price']):,}</td><td class="num">{int(r['Qty']):,}</td><td class="num">{int(amt):,}</td><td class="num">{int(fee):,}</td><td class="num">{int(r.get('Total_Qty', 0)):,}</td><td class="num">{ret_str}</td><td class="num">{cum_profit_str}</td><td class="num">{status_badge}</td><td class="num"><div class="reason-cell"><span class="reason-text">{reason_main} 🔍</span>{indicators_html}</div></td></tr>""")
        
        profit_color = "var(--gain)" if stock_realized_profit >= 0 else "var(--loss)"
        content.append(f"""<div class="stock-group" id="stock-{code_str}"><div class="stock-header"><div style="display:flex; align-items:center;">{name} <span style="color:var(--dim); font-size:0.9rem; margin-left:8px;">({code_str})</span>{chart_link_html}</div><div style="font-size:1rem; color:white">누적 수익금: <span style="color:{profit_color}">{int(stock_realized_profit):+,}원</span></div></div><table><thead><tr><th>시간</th><th>구분</th><th>차트</th><th>가격</th><th>수량</th><th>거래액</th><th>비용</th><th>보유</th><th>수익률</th><th>누적 수익금액</th><th>시장</th><th>상세 사유</th></tr></thead><tbody>{"".join(group_rows)}</tbody></table></div>""")

    ret_color = "var(--gain)" if avg_ret > 0 else "var(--loss)"
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_template.format(now=now_str, interval=display_interval, mode_label=mode_label, mode_color=mode_color, total=total_trades, win_rate=win_rate, avg_ret=avg_ret, ret_color=ret_color, total_profit=int(total_profit_val), total_profit_color=total_profit_color, gross_profit=int(gross_profit_val), gross_profit_color=gross_profit_color, total_fees=int(total_fees_val), param_html=param_html, sidebar_items="".join(sidebar_items), content="".join(content), start_date=start_date if start_date else "Simulation", end_date=end_date if end_date else "End", equity_chart_html=equity_chart_html, strategy_stats_html=strategy_stats_html))
    print(f"  [Report] Execution log saved: {output_path}")
