import os
import pandas as pd

# Default path for legacy compatibility
SIGNAL_CSV = "backtest_signals.csv"

def generate_signal_report(csv_path=None, output_path="signal_report.html", target_codes=None):
    """기록된 신호 데이터를 바탕으로 HTML 보고서 생성 (v6.3)"""
    if csv_path is None: csv_path = SIGNAL_CSV
    if not os.path.exists(csv_path):
        # [v15.4.3] CSV doesn't exist (no signals recorded)
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(f"<html><body style='background:#0f172a; color:white; padding:50px; text-align:center;'><h1>신호 데이터가 없습니다.</h1><p>로그 파일({os.path.basename(csv_path)})이 생성되지 않았습니다.</p></body></html>")
        except: pass
        return

    try:
        df = pd.read_csv(csv_path, encoding='utf-8-sig', dtype={'Code': str})
        if df.empty:
            # [v15.4.3] No data found in CSV
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(f"<html><body style='background:#0f172a; color:white; padding:50px; text-align:center;'><h1>신호 내역이 전혀 없습니다.</h1><p>CSV 파일이 비어 있습니다.</p><p>Path: {output_path}</p></body></html>")
            return
        
        # [v15.4.1] Aggressive filtering to ensure ONLY target stocks are shown
        debug_msg = "Filter Disabled"
        if target_codes is not None:
            codes_set = set([str(c).strip().zfill(6) for c in target_codes])
            before_count = len(df)
            df = df[df['Code'].astype(str).str.strip().str.zfill(6).isin(codes_set)].copy()
            after_count = len(df)
            debug_msg = f"Filter Active: {len(codes_set)} stocks | Before: {before_count} rows | After: {after_count} rows"
            
            if df.empty:
                try:
                    with open(output_path, "w", encoding="utf-8") as f:
                        f.write(f"<html><body style='background:#0f172a; color:white; padding:50px; text-align:center;'><h1>신호 내역 없음</h1><p>{debug_msg}</p><p>CWD: {os.getcwd()}</p></body></html>")
                except: pass
                return

        # [v7.1] Clean up duplicate headers
        df = df[df['Date'] != 'Date'].copy()
        df['Date'] = pd.to_datetime(df['Date'], format='mixed', errors='coerce')
        df = df.dropna(subset=['Date'])
        df = df.sort_values(by='Date', ascending=False)
    except Exception as e:
        print(f"  (!) Signal Report Error: {e}"); return

    html_template = """
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8"><title>백테스팅 신호 분석 리포트</title>
        <style>
            :root {{ --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --dim: #94a3b8; --primary: #38bdf8; --success: #22c55e; --danger: #ef4444; }}
            body {{ font-family: 'Inter', sans-serif; background: var(--bg); color: var(--text); padding: 20px; line-height: 1.5; }}
            .container {{ max-width: 1200px; margin: 0 auto; }}
            header {{ border-bottom: 1px solid #334155; padding-bottom: 15px; margin-bottom: 25px; }}
            h1 {{ color: var(--primary); margin: 0; }}
            .summary {{ display: flex; gap: 15px; margin-bottom: 25px; }}
            .card {{ background: var(--card); padding: 15px; border-radius: 10px; flex: 1; }}
            .card-val {{ font-size: 1.4rem; font-weight: bold; }}
            table {{ width: 100%; border-collapse: collapse; background: var(--card); border-radius: 10px; overflow: hidden; }}
            th {{ background: #334155; color: var(--dim); padding: 10px; text-align: left; font-size: 0.8rem; }}
            td {{ padding: 10px; border-bottom: 1px solid #334155; font-size: 0.9rem; }}
            .badge {{ padding: 2px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; }}
            .badge-true {{ background: rgba(34,197,94,0.2); color: #4ade80; }}
            .badge-false {{ background: rgba(239,68,68,0.2); color: #f87171; }}
        </style>
    </head>
    <body style="padding-top: 20px;">
        <div class="container">
            <header><h1>Backtest Signal Analysis</h1><p style="color:var(--dim)">Candidate signals vs Market Filters</p></header>
            <div class="summary">
                <div class="card"><div style="color:var(--dim)">Total Candidates</div><div class="card-val">{total}</div></div>
                <div class="card"><div style="color:var(--dim)">Final Buy Signals</div><div class="card-val" style="color:var(--success)">{f_true}</div></div>
                <div class="card"><div style="color:var(--dim)">Filtered Out</div><div class="card-val" style="color:var(--danger)">{f_false}</div></div>
            </div>
            <table>
                <thead><tr><th>날짜</th><th>종목명</th><th>종목코드</th><th>현재가</th><th>RSI</th><th>MACD</th><th>GC</th><th>MA60</th><th>Vol</th><th>시장</th><th>최종</th></tr></thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </body></html>
    """
    rows = []
    for _, r in df.iterrows():
        def badge(v): return f'<span class="badge badge-{str(v).lower()}">{"OK" if v else "NO"}</span>'
        rows.append(f"""<tr>
            <td>{r['Date'].strftime('%m-%d %H:%M')}</td>
            <td style="font-weight:bold; color:var(--primary)">{r.get('Name', '')}</td>
            <td>{r['Code']}</td>
            <td>{int(r['Price']) if not pd.isna(r['Price']) else 0:,}</td>
            <td>{badge(r['RSI_Range'])} ({r['RSI']:.1f})</td>
            <td>{badge(r['MACD_OK'])}</td><td>{badge(r['MA_GC'])}</td>
            <td>{badge(r['MA60_Range'])}</td><td>{badge(r['Vol_Surge'])}</td>
            <td style="color:{'#4ade80' if r['Market_Trend']=='UP' else '#f87171'}">{r['Market_Trend']}</td>
            <td>{badge(r['Final_Signal'])}</td></tr>""")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_template.format(total=len(df), f_true=len(df[df['Final_Signal']]), f_false=len(df[~df['Final_Signal']]), rows="".join(rows)))
