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
    
    avg_cash_ratio = 0 # [v32.1] Initialize for template
    
    # [v4.5] UI-Consistent Hierarchical Parameter Mapping (6-Section Sync)
    PARAM_STRUCTURE = [
        {
            "name": "💰 [1] 자금 및 계좌 관리 (Account & Fund)",
            "items": [
                {"label": "백테스팅용 초기자금", "key": "backtest_initial_capital", "unit": "원", "is_amt": True},
                {"label": "최대 감시 종목 수", "key": "max_slots", "unit": "개"},
                {"label": "1단계(정찰) 비중", "keys": ["stage1_amt", "s1_equity_pct"], "unit_key": "s1_sizing_unit"},
                {"label": "2단계(본대) 비중", "keys": ["stage2_amt", "s2_equity_pct"], "unit_key": "s2_sizing_unit"},
                {"label": "가상 매매 자동화", "toggle": "use_auto_virtual"}
            ]
        },
        {
            "name": "🔍 [2] 종목 선정 필수 필터 (Stock Selection)",
            "items": [
                {"label": "최소 거래대금", "key": "min_trde_val", "unit": "억"},
                {"label": "거래량 분출 비율", "key": "vol_ratio", "unit": "배"},
                {"label": "분출 확인 기간", "key": "vol_lookback", "unit": "일"},
                {"label": "종목 선정 RSI 범위", "keys": ["sel_rsi_min", "sel_rsi_max"], "sep": "~"},
                {"label": "일봉 MACD 필터", "keys": ["macd_fast_daily", "macd_slow_daily", "macd_sig_daily"], "sep": "/"},
                {"label": "이평선 수렴도 기준", "key": "ma_convergence", "unit": "%"},
                {"label": "시장 지수 필터", "toggle": "use_market_filter"},
            ]
        },
        {
            "name": "🎯 [3] 진입 시그널 필터 (Signal Logic - 15m)",
            "items": [
                {"label": "매수 진입 RSI 하한", "toggle": "use_rsi_min", "key": "buy_rsi_min"},
                {"label": "15분봉 MACD 필터", "toggle": "use_macd_signal"},
                {"label": "  ㄴ MACD E-Periods", "keys": ["macd_fast_entry", "macd_slow_entry", "macd_sig_entry"], "sep": "/"},
                {"label": "RSI 시그널 필터", "toggle": "use_rsi_sig_filter", "key": "rsi_sig_period", "unit": "일"}
            ]
        },
        {
            "name": "🚀 [4] 1단계 진입 전략 (Stage 1 Master)",
            "items": [
                {"label": "Type A (눌림목) 활성화", "toggle": "use_type_a", "key": "buy_rsi_max_a", "unit": " RSI"},
                {"label": "ㄴ Smart Buy (Type A)", "toggle": "use_smart_buy_a"},
                {"label": "ㄴ 거래량 급증 확인(A)", "toggle": "use_vol_surge", "key": "vol_surge", "unit": "배"},
                {"label": "ㄴ MA60 지지(상/하)", "keys": ["ma60_min", "ma60_max"], "unit": "%", "sep": "~"},
                {"label": "ㄴ 이평 골든크로스(분)", "toggle": "use_gc", "key": "gc_window", "unit": "분"},
                {"label": "ㄴ RSI 50 돌파 컨펌(분)", "toggle": "use_rsi_signal", "key": "rsi_window", "unit": "분"},
                {"label": "ㄴ A 전용 ATR 손절(SL)", "toggle": "use_atr_stop_loss_a", "key": "atr_multiplier_sl_a", "unit": "배"},
                {"label": "ㄴ A 전용 추격 익절(Trl)", "toggle": "use_atr_trailing_stop_a", "key": "atr_multiplier_trail_a", "unit": "배"},
                {"label": "Type B (돌파 타점) 활성화", "toggle": "use_type_b", "key": "buy_rsi_max_b", "unit": " RSI"},
                {"label": "ㄴ Smart Buy (Type B)", "toggle": "use_smart_buy_b"},
                {"label": "ㄴ MACD 골든크로스(B)", "toggle": "use_macd_gc_b"},
                {"label": "ㄴ 정배열 추세 확인(B)", "toggle": "use_trend_aligned_b"},
                {"label": "ㄴ 돌파 시 거래량 분출", "toggle": "use_type_b_vol", "key": "vol_surge_b", "unit": "배"},
                {"label": "ㄴ B 전용 ATR 손절(SL)", "toggle": "use_atr_stop_loss_b", "key": "atr_multiplier_sl_b", "unit": "배"},
                {"label": "ㄴ B 전용 추격 익절(Trl)", "toggle": "use_atr_trailing_stop_b", "key": "atr_multiplier_trail_b", "unit": "배"}
            ]
        },
        {
            "name": "📈 [5] 2단계 추매 전략 (Scaling Up)",
            "items": [
                {"label": "1. 20일선 지지 확인", "toggle": "use_s2_ma20", "key": "s2_ma20_gap", "unit": "%"},
                {"label": "2. 5일선 회복/지지", "toggle": "use_s2_ma5", "key": "s2_ma5_gap", "unit": "%"},
                {"label": "3. 추매용 Smart Buy", "toggle": "use_smart_buy_s2"},
                {"label": "4. 추매 시 거래량 급증", "toggle": "use_s2_vol_surge", "key": "s2_vol_ratio", "unit": "배"},
                {"label": "5. RSI 모멘텀 상승", "toggle": "use_s2_rsi_sig", "key": "s2_rsi_gap", "unit": "%"},
                {"label": "6. MACD 모멘텀 상승", "toggle": "use_s2_macd_gc", "key": "s2_macd_gap", "unit": "%"},
                {"label": "7. 수익권 진입 필터", "toggle": "use_s2_profit", "key": "s2_profit_limit", "unit": "%"},
                {"label": "8. 진입 후 대기 시간", "toggle": "use_s2_delay", "key": "s2_delay_hours", "unit": "시간"},
                {"label": "ㄴ S2 전용 ATR 손절(SL)", "toggle": "use_atr_stop_loss_s2", "key": "atr_multiplier_sl_s2", "unit": "배"},
                {"label": "ㄴ S2 전용 추격 익절(Trl)", "toggle": "use_atr_trailing_stop_s2", "key": "atr_multiplier_trail_s2", "unit": "배"}
            ]
        },
        {
            "name": "💰 [6] 진입 수능 임계값 (Smart Buy Wall)",
            "items": [
                {"label": "최소 체결강도 (VP)", "key": "smart_buy_vp", "unit": "%"},
                {"label": "진입용 최소 매도벽", "key": "smart_buy_ba_ratio", "unit": "배"}
            ]
        },
        {
            "name": "🛡️ [7] 공통 매도 및 리스크 관리 (Exit & Trap)",
            "items": [
                {"label": "Smart Exit (조기탈출)", "toggle": "use_smart_exit", "keys": ["smart_exit_vp", "smart_exit_ba_ratio"], "sep": "VP / Ratio: "},
                {"label": "ㄴ 가격 하락 임계", "key": "smart_exit_price_threshold", "unit": "%"},
                {"label": "갭하락 즉시 손절(Cut)", "key": "gap_down_limit", "unit": "%"},
                {"label": "개장 초기 매도 유예", "toggle": "use_gap_grace"},
                {"label": "ATR 공통 제어", "toggle": "use_atr_exit", "key": "atr_period", "unit": "일"},
                {"label": "S1 눌림 익절/손절", "toggle": "use_s1_a_tp", "keys": ["s1_a_take_profit", "s1_a_stop_loss"], "unit": "%", "sep": "TP / SL: "},
                {"label": "S1 돌파 익절/손절", "toggle": "use_s1_b_tp", "keys": ["s1_b_take_profit", "s1_b_stop_loss"], "unit": "%", "sep": "TP / SL: "},
                {"label": "S1 눌림 Flash Exit", "toggle": "use_s1_a_flash_exit", "key": "s1_a_flash_exit_pct", "unit": "%"},
                {"label": "S1 돌파 Flash Exit", "toggle": "use_s1_b_flash_exit", "key": "s1_b_flash_exit_pct", "unit": "%"},
                {"label": "S2 익절/손절/Flash", "toggle": "use_s2_tp", "keys": ["s2_take_profit", "s2_stop_loss", "s2_flash_exit_pct"], "unit": "%", "sep": " / "}
            ]
        }
    ]

    # [v16.1] Efficient Formatter
    def format_val(v):
        if isinstance(v, (int, float, np.integer, np.floating)):
            if float(v) > 1000000: # 금액 표시 (KRW)
                return f"{int(v):,}"
            if float(v).is_integer(): return str(int(v))
            return f"{float(v):.1f}"
        return str(v)

    # 파라미터 섹션 생성
    param_html = ""
    if best_settings:
        group_htmls = []
        assigned_keys = set()
        
        for group in PARAM_STRUCTURE:
            items_html = []
            for item in group["items"]:
                label = item["label"]
                toggle_key = item.get("toggle")
                val_key = item.get("key")
                val_keys = item.get("keys")
                unit = item.get("unit", "")
                sep = item.get("sep", ", ")
                mult = item.get("multiply", 1)
                
                # 1. 상태 정보 (ON/OFF)
                status_html = ""
                is_on = True
                if toggle_key:
                    assigned_keys.add(toggle_key)
                    val = best_settings.get(toggle_key, False)
                    is_on = val
                    status_class = "val-on" if val else "val-off"
                    status_text = "ON" if val else "OFF"
                    # [v17.0] ON/OFF 배지만 있으면 충분, 값은 괄호로 묶어 통합 표시
                    status_html = f'<span class="p-val {status_class}">{status_text}</span>'
                
                # 2. 값 정보 (ON일 때만 또는 키가 지정된 경우 표시)
                value_html = ""
                v_list = []
                # [v33.0] unit_key가 있으면 해당 단위에 맞는 키만 선택
                unit_key = item.get("unit_key")
                if unit_key and unit_key in best_settings:
                    current_unit = best_settings[unit_key]
                    mapped_unit = current_unit # "원", "%" 등
                    if val_keys and len(val_keys) >= 2:
                        target_key = val_keys[0] if current_unit == "원" else val_keys[1]
                        assigned_keys.add(target_key)
                        v_list.append(format_val(best_settings.get(target_key, 0)))
                        unit = mapped_unit
                elif val_key:
                    assigned_keys.add(val_key)
                    if val_key in best_settings:
                        v_list.append(format_val(best_settings[val_key] * mult))
                elif val_keys:
                    for k in val_keys:
                        assigned_keys.add(k)
                        if k in best_settings:
                            v_list.append(format_val(best_settings[k] * mult))
                
                if v_list:
                    val_str = sep.join(v_list)
                    # OFF인 경우에는 괄호 내용을 숨기거나 희미하게 처리 (선택사항: 여기서는 ON일 때만 표시)
                    if is_on:
                        value_html = f'<span class="p-val-sub">({val_str}{unit})</span>'
                    else:
                        value_html = "" # OFF이면 값 숨김 (요청사항 반영)
                
                if toggle_key or v_list: # 키가 정의된 항목만 표시
                    items_html.append(f"""
                    <div class="p-item">
                        <span class="p-key">{label}</span>
                        <div class="p-val-group">
                            {status_html}
                            {value_html}
                        </div>
                    </div>
                    """)
            
            if items_html:
                group_htmls.append(f"""
                <details class="p-group">
                    <summary class="p-group-title">{group['name']}</summary>
                    <div class="p-grid">{"".join(items_html)}</div>
                </details>
                """)
        
        # [v16.1] 정의되지 않은 나머지 파라미터들 (기타 설정)
        others = []
        for k, v in best_settings.items():
            if k not in assigned_keys and k not in ['bt_start_date', 'bt_end_date']:
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
            body {{ font-family: 'Pretendard', sans-serif; background: var(--bg); color: var(--text); margin: 0; min-height: 100vh; overflow-x: hidden; }}
            .sidebar {{ width: var(--sidebar-w); background: #0a0f1d; border-right: 1px solid #1e293b; display: flex; flex-direction: column; position: fixed; left: 0; top: 0; bottom: 0; z-index: 1000; overflow-y: auto; }}
            .sidebar-header {{ padding: 25px 20px; border-bottom: 2px solid #1e293b; background: rgba(59,130,246,0.05); }}
            .sidebar-title {{ font-size: 1.2rem; font-weight: 800; color: var(--primary); display: flex; align-items: center; gap: 10px; text-transform: uppercase; letter-spacing: 1px; }}
            .nav-list {{ list-style: none; padding: 15px; margin: 0; overflow-y: auto; flex: 1; }}
            .nav-item {{ margin-bottom: 6px; }}
            .nav-item a {{ display: flex; justify-content: space-between; padding: 12px 15px; color: var(--dim); text-decoration: none; border-radius: 10px; font-size: 0.9rem; transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1); border: 1px solid transparent; }}
            .nav-item a:hover {{ background: #1e293b; color: white; border-color: #334155; transform: translateX(5px); }}
            .nav-item .code-badge {{ background: #334155; color: #cbd5e1; font-size: 0.7rem; padding: 2px 6px; border-radius: 4px; font-weight: 600; }}
            
            .main-content {{ margin-left: calc(var(--sidebar-w) + 30px); padding: 40px; background: radial-gradient(circle at top right, #1e293b11, transparent); min-height: 100vh; display: block; overflow-x: visible; }}
            .container {{ width: 100%; max-width: 1400px; margin: 0; display: block; }}
            .header-flex {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 35px; border-bottom: 1px solid #1e293b; padding-bottom: 20px; width: 100%; }}
            
            /* 파라미터 그룹 스타일 [v15.6] */
            .p-container {{ margin-top: 40px; margin-bottom: 50px; display: flex; flex-direction: column; gap: 20px; clear: both; width: 100%; }}
            .p-group {{ background: #1e293b; border: 1px solid #334155; border-radius: 16px; padding: 15px 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); cursor: default; display: block; }}
            .p-group[open] {{ background: #1e293b; border-color: var(--primary); box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.4); }}
            .p-group-title {{ font-size: 0.95rem; font-weight: 700; color: var(--buy); list-style: none; outline: none; cursor: pointer; display: flex; justify-content: space-between; align-items: center; }}
            .p-group-title::-webkit-details-marker {{ display: none; }}
            .p-group-title::after {{ content: '📂'; font-size: 0.8rem; opacity: 0.5; transition: 0.2s; }}
            .p-group[open] .p-group-title::after {{ content: '📖'; opacity: 1; transform: rotate(180deg); }}
            .p-group-title:hover {{ opacity: 0.8; }}
            
            .p-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 10px; padding-top: 15px; border-top: 1px solid #334155; margin-top: 10px; }}
            .p-item {{ display: flex; justify-content: space-between; align-items: center; background: rgba(15, 23, 42, 0.4); padding: 8px 12px; border-radius: 8px; border: 1px solid #33415566; }}
            .p-key {{ font-size: 0.85rem; color: var(--dim); font-family: 'Pretendard', sans-serif; }}
            .p-val-group {{ display: flex; align-items: center; gap: 8px; }}
            .p-val {{ font-size: 0.9rem; font-weight: 700; color: var(--text); min-width: 35px; text-align: center; }}
            .p-val-sub {{ font-size: 0.8rem; color: var(--primary); font-weight: 600; font-family: 'JetBrains Mono', monospace; }}
            .val-on {{ color: #4ade80; background: rgba(74, 222, 128, 0.1); padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; }}
            .val-off {{ color: #94a3b8; background: rgba(148, 163, 184, 0.1); padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; }}
            
            .header-flex {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 35px; border-bottom: 1px solid #1e293b; padding-bottom: 20px; }}
            h1 {{ margin: 0; font-size: 2.2rem; font-weight: 900; letter-spacing: -1px; }}
            .mode-badge-top {{ font-size: 0.8rem; background: {mode_color}; color: #0f172a; padding: 4px 12px; border-radius: 8px; font-weight: 800; }}
            
            .stats {{ display: grid !important; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 15px; margin-bottom: 40px; clear: both; width: 100%; }}
            .stat-card {{ background: linear-gradient(135deg, #1e293b, #0f172a); padding: 15px 12px; border-radius: 16px; border: 1px solid #334155; position: relative; box-sizing: border-box; min-width: 180px; }}
            .stat-val {{ font-size: 1.25rem; font-weight: 800; font-variant-numeric: tabular-nums; }}
            
            /* [v27.0] 전략별 진입 통계 스타일 - 수직 스택 구조 고정 */
            .strategy-stats {{ display: block; width: 100%; margin-bottom: 30px; }}
            .strategy-card {{ 
                background: #1e293b; padding: 20px; border-radius: 16px; border: 1px solid #334155;
                margin-bottom: 15px; width: 100%; box-sizing: border-box;
                box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
            }}
            .str-label {{ font-size: 1rem; font-weight: 700; color: #fff; }}
            .str-count {{ font-size: 1.2rem; font-weight: 800; color: var(--primary); }}
            .str-count.type-a {{ color: #4ade80; }}
            .str-count.type-b {{ color: #38bdf8; }}
            .str-count.type-s2 {{ color: #fbbf24; }}
            .str-count.s1-color {{ color: #94a3b8; }}
            .str-count.s2-color {{ color: #fb7185; }}
            
            /* 카드 내부 테이블 최적화 */
            .strategy-card table {{ width: 100%; border-collapse: collapse; margin-top: 10px; table-layout: auto; }}
            .strategy-card th {{ padding: 8px 0; border-bottom: 1px solid #334155; color: var(--dim); font-size: 0.75rem; text-align: left; }}
            .strategy-card td {{ padding: 10px 0; border-bottom: 1px solid #1e293b88; font-size: 0.9rem; }}
            .strategy-card tr:last-child td {{ border-bottom: none; }}
            
            .stock-group {{ background: #1e293b88; padding: 30px; border-radius: 24px; margin-bottom: 60px; border: 1px solid #334155; backdrop-filter: blur(10px); }}
            .stock-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 25px; border-bottom: 2px solid #334155; padding-bottom: 15px; }}
            .stock-header-title {{ font-size: 1.5rem; font-weight: 800; color: #fff; display: flex; align-items: center; gap: 12px; }}
            .stock-header-code {{ color: var(--buy); font-family: monospace; font-size: 1.1rem; }}
            
            table {{ width: 100%; border-collapse: separate; border-spacing: 0; margin-top: 15px; table-layout: fixed; }}
            th {{ color: var(--dim); padding: 15px 12px; text-align: left; font-size: 0.75rem; border-bottom: 2px solid #334155; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; word-break: break-all; }}
            td {{ padding: 16px 12px; border-bottom: 1px solid #1e293b; font-size: 0.96rem; font-variant-numeric: tabular-nums; word-break: break-all; }}
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
                    <div class="stat-card"><div>Avg Cash Ratio</div><div class="stat-val" style="color:#38bdf8">{avg_cash_ratio:.1f}%</div></div>
                    
                    <div class="stat-card"><div>Total Profit (Gross)</div><div class="stat-val" style="color:{gross_profit_color}">{gross_profit:+,}</div></div>
                    <div class="stat-card"><div>Total Fees</div><div class="stat-val" style="color:var(--loss)">-{total_fees:,}</div></div>
                    <div class="stat-card" style="border: 2px solid {total_profit_color}; background: rgba(0,255,127,0.05);">
                        <div style="font-weight:bold;">Net Profit</div>
                        <div class="stat-val" style="color:{total_profit_color}; font-size:1.6rem; font-weight:800;">{total_profit:+,} <small style="font-size:0.8rem;">KRW</small></div>
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
                action = str(tr['Action'])
                amt = float(tr['Price']) * float(tr['Qty'])
                fee = float(tr.get('Fee', 0)) if not pd.isna(tr.get('Fee', 0)) else 0
                if "BUY" in action: 
                    total_profit_val = float(total_profit_val) - (amt + fee)
                elif action in ['SELL', 'HOLD']:
                    total_profit_val = float(total_profit_val) + (amt - fee)
            # total_profit_val += s_profit (기존 로직 대체)
    
    def _s_int(v, default=0):
        try:
            if v is None or pd.isna(v): return default
            return int(float(v))
        except: return default

    # [v27.0] Summary Section NaN Safety
    total_fees_val = _s_int(df['Fee'].sum() if 'Fee' in df.columns else 0)
    total_profit_val = _s_int(total_profit_val)
    total_profit_color = "var(--gain)" if total_profit_val >= 0 else "var(--loss)"
    
    gross_profit_val = total_profit_val + total_fees_val
    gross_profit_color = "var(--gain)" if gross_profit_val >= 0 else "var(--loss)"
    
    # [v27.0] Strategy Execution Breakdown - More robust counting (Prefix prioritized)
    count_a = len(df[(df['Action'].str.startswith('BUY', na=False)) & (df['Reason'].str.contains('TYPE_A', case=False, na=False))])
    count_b = len(df[(df['Action'].str.startswith('BUY', na=False)) & (df['Reason'].str.contains('TYPE_B', case=False, na=False))])
    count_s2 = len(df[(df['Action'].str.contains('BUY2', na=False)) | (df['Action'] == 'ADDON') | (df['Reason'].str.contains('Stage 2|ADDON', case=False, na=False))])
    
    # [v4.0] Precise Stage 1 Type A/B Filtering (Excluding S2/ADDON)
    s1_a_mask = (df['Action'].isin(['SELL', 'HOLD'])) & \
                (df['Sig_Type'] == 'TYPE_A') & \
                (~df['Reason'].str.contains('S2|Stage 2|추매|ADDON', case=False, na=False))
    s1_a_rets = df[s1_a_mask]['Return'].tolist()
    
    s1_b_mask = (df['Action'].isin(['SELL', 'HOLD'])) & \
                (df['Sig_Type'] == 'TYPE_B') & \
                (~df['Reason'].str.contains('S2|Stage 2|추매|ADDON', case=False, na=False))
    s1_b_rets = df[s1_b_mask]['Return'].tolist()
    
    # [v27.5] Smart Buy/Exit Tracking
    count_smart_buy = len(df[(df['Action'].str.startswith('BUY', na=False)) & (df['Reason'].str.contains('SmartBuy', case=False, na=False))])
    count_smart_exit = len(df[(df['Action'].str.startswith('SELL', na=False)) & (df['Reason'].str.contains('Smart Exit', case=False, na=False))])
    count_rejected = len(df[df['Action'] == 'REJECTED_SIG'])
    total_signals = count_smart_buy + count_rejected
    pass_rate = (count_smart_buy / max(1, total_signals)) * 100
    
    # [v5.0] Granular Profit & Yield Breakdown (S1-Only vs S1+S2)
    a_s1_p, a_s1_i = [], []     # Type A, Stage 1 Only
    a_s2_p, a_s2_i = [], []     # Type A, with Stage 2
    b_s1_p, b_s1_i = [], []     # Type B, Stage 1 Only
    b_s2_p, b_s2_i = [], []     # Type B, with Stage 2
    
    total_s1_p, total_s1_i = [], []  # All S1-only exits
    total_s2_p, total_s2_i = [], []  # All S1+S2 exits

    for c in df['Code'].unique():
        s_df = df[df['Code'] == c]
        entry_sig = "NONE"
        investment_sum = 0
        has_s2 = False
        
        # Action-based sequence parsing
        temp_inv = 0
        temp_has_s2 = False
        temp_sig = "NONE"
        
        for _, r in s_df.iterrows():
            action = str(r['Action'])
            price = float(r['Price'])
            qty = float(r['Qty'])
            fee = float(r.get('Fee', 0)) if not pd.isna(r.get('Fee', 0)) else 0
            
            if "BUY" in action:
                if any(x in action for x in ["BUY2", "BUY_2ND", "ADDON", "S2"]):
                    temp_has_s2 = True
                else:
                    temp_sig = str(r.get('Sig_Type', 'NONE'))
                temp_inv += (price * qty) + fee
            
            elif action in ["SELL", "HOLD"]:
                profit = (price * qty) - fee - temp_inv
                
                if temp_has_s2:
                    if "TYPE_A" in temp_sig:
                        a_s2_p.append(profit); a_s2_i.append(temp_inv)
                    elif "TYPE_B" in temp_sig:
                        b_s2_p.append(profit); b_s2_i.append(temp_inv)
                    total_s2_p.append(profit); total_s2_i.append(temp_inv)
                else:
                    if "TYPE_A" in temp_sig:
                        a_s1_p.append(profit); a_s1_i.append(temp_inv)
                    elif "TYPE_B" in temp_sig:
                        b_s1_p.append(profit); b_s1_i.append(temp_inv)
                    total_s1_p.append(profit); total_s1_i.append(temp_inv)
                
                # Reset for next trade in SAME ticker
                temp_inv = 0
                temp_has_s2 = False
                temp_sig = "NONE"

    def calc_yield(profits, invested):
        if not invested or sum(invested) == 0: return 0
        return (sum(profits) / sum(invested)) * 100

    y_a1, s_a1 = calc_yield(a_s1_p, a_s1_i), sum(a_s1_p)
    y_a2, s_a2 = calc_yield(a_s2_p, a_s2_i), sum(a_s2_p)
    y_b1, s_b1 = calc_yield(b_s1_p, b_s1_i), sum(b_s1_p)
    y_b2, s_b2 = calc_yield(b_s2_p, b_s2_i), sum(b_s2_p)
    y_s1, s_s1 = calc_yield(total_s1_p, total_s1_i), sum(total_s1_p)
    y_s2, s_s2 = calc_yield(total_s2_p, total_s2_i), sum(total_s2_p)

    # [v4.7] Refined Statistics (Signals vs Fills)
    count_fills = len(df[df['Action'].isin(['SELL', 'HOLD'])])
    total_signals = count_smart_buy + count_rejected
    pass_rate = (count_smart_buy / max(1, total_signals)) * 100

    strategy_stats_html = f"""
    <div style="color:var(--dim); font-size:0.85rem; margin-bottom:12px;">Strategy Execution Map (전략별 실행 성과 지도)</div>
    <div class="strategy-stats" style="display: flex; flex-direction: column; gap: 15px;">
        
        <!-- Type A Section (눌림목) -->
        <div class="strategy-card" style="display: block; border-left: 5px solid var(--type-a); padding: 15px; background: var(--card-bg); border-radius: 8px;">
            <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #334155; padding-bottom:10px; margin-bottom:12px;">
                <span style="color:var(--type-a); font-weight:800; font-size:1.1rem;">Type A (눌림목)</span>
                <span style="font-size:0.9rem; color:var(--dim);">총 진입 횟수: <b style="color:#fff; font-size:1.1rem;">{len(a_s1_p)+len(a_s2_p)}</b> 건</span>
            </div>
            <table style="width: 100%; border-collapse: collapse;">
                <thead>
                    <tr style="color:var(--dim); text-align:left;">
                        <th style="width: 30%;">진입 구분</th>
                        <th style="text-align:center; width: 20%;">건수</th>
                        <th style="text-align:center; width: 20%;">가중 수익률</th>
                        <th style="text-align:right; width: 30%;">총 수익금 (Net KRW)</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td style="padding: 8px 0;">Stage 1 Only (1차만 진행)</td>
                        <td style="text-align:center; font-weight:600;">{len(a_s1_p)}건</td>
                        <td style="text-align:center;" class="{'gain' if s_a1 >= 0 else 'loss'}">{y_a1:+.2f}%</td>
                        <td style="text-align:right; font-weight:700;" class="{'gain' if s_a1 >= 0 else 'loss'}">{int(s_a1):,}원</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px 0;">with Stage 2 (추매 포함)</td>
                        <td style="text-align:center; font-weight:600;">{len(a_s2_p)}건</td>
                        <td style="text-align:center;" class="{'gain' if s_a2 >= 0 else 'loss'}">{y_a2:+.2f}%</td>
                        <td style="text-align:right; font-weight:700;" class="{'gain' if s_a2 >= 0 else 'loss'}">{int(s_a2):,}원</td>
                    </tr>
                </tbody>
            </table>
            <div style="margin-top:15px; padding-top:12px; border-top:2px solid #334155; text-align:right;">
                <span style="color:var(--dim); font-size:0.85rem; margin-right:10px;">Type A 합계 수익:</span> 
                <span class="{'gain' if (s_a1+s_a2) >= 0 else 'loss'}" style="font-size:1.3rem; font-weight:900;">{int(s_a1+s_a2):,}원</span>
            </div>
        </div>

        <!-- Type B Section (추세돌파) -->
        <div class="strategy-card" style="display: block; border-left: 5px solid var(--type-b); padding: 15px; background: var(--card-bg); border-radius: 8px;">
            <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #334155; padding-bottom:10px; margin-bottom:12px;">
                <span style="color:var(--type-b); font-weight:800; font-size:1.1rem;">Type B (추세돌파)</span>
                <span style="font-size:0.9rem; color:var(--dim);">총 진입 횟수: <b style="color:#fff; font-size:1.1rem;">{len(b_s1_p)+len(b_s2_p)}</b> 건</span>
            </div>
            <table style="width: 100%; border-collapse: collapse;">
                <thead>
                    <tr style="color:var(--dim); text-align:left;">
                        <th style="width: 30%;">진입 구분</th>
                        <th style="text-align:center; width: 20%;">건수</th>
                        <th style="text-align:center; width: 20%;">가중 수익률</th>
                        <th style="text-align:right; width: 30%;">총 수익금 (Net KRW)</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td style="padding: 8px 0;">Stage 1 Only (1차만 진행)</td>
                        <td style="text-align:center; font-weight:600;">{len(b_s1_p)}건</td>
                        <td style="text-align:center;" class="{'gain' if s_b1 >= 0 else 'loss'}">{y_b1:+.2f}%</td>
                        <td style="text-align:right; font-weight:700;" class="{'gain' if s_b1 >= 0 else 'loss'}">{int(s_b1):,}원</td>
                    </tr>
                    <tr>
                        <td style="padding: 8px 0;">with Stage 2 (추매 포함)</td>
                        <td style="text-align:center; font-weight:600;">{len(b_s2_p)}건</td>
                        <td style="text-align:center;" class="{'gain' if s_b2 >= 0 else 'loss'}">{y_b2:+.2f}%</td>
                        <td style="text-align:right; font-weight:700;" class="{'gain' if s_b2 >= 0 else 'loss'}">{int(s_b2):,}원</td>
                    </tr>
                </tbody>
            </table>
        </div>

        <div style="display:flex; flex-wrap: wrap; gap:15px; width:100%;">
            <!-- Overall Summary Section -->
            <div class="strategy-card" style="flex:2; min-width:300px;">
                <span class="str-label" style="border-bottom:1px solid #444; padding-bottom:5px; display:block; margin-bottom:10px;">Overall Execution (전체 실행 정산)</span>
                <div style="display:flex; justify-content:space-between; font-size:0.9rem; margin-bottom:8px;">
                    <span style="color:var(--dim)">S1 Only (1단계 전용):</span>
                    <span class="{'gain' if s_s1 >= 0 else 'loss'}" style="font-weight:700;">{y_s1:.2f}% ({int(s_s1):,}원 / {len(total_s1_p)}건)</span>
                </div>
                <div style="display:flex; justify-content:space-between; font-size:0.9rem;">
                    <span style="color:var(--dim)">with S2 (추매 포함):</span>
                    <span class="{'gain' if s_s2 >= 0 else 'loss'}" style="font-weight:700;">{y_s2:.2f}% ({int(s_s2):,}원 / {len(total_s2_p)}건)</span>
                </div>
            </div>

            <!-- Signal Stats Section -->
            <div class="strategy-card" style="flex:1; min-width:200px;">
                <span class="str-label" style="border-bottom:1px solid #444; padding-bottom:5px; display:block; margin-bottom:10px;">Signal Stats (신호 통계)</span>
                <div style="display:flex; justify-content:space-between; font-size:0.85rem; margin-bottom:6px;">
                    <span style="color:var(--dim)">실제 진입:</span>
                    <span>{len(total_s1_p)+len(total_s2_p)} 건 / {total_signals} 건</span>
                </div>
                <div style="display:flex; justify-content:space-between; font-size:0.85rem;">
                    <span style="color:var(--dim)">SmartBuy 통과:</span>
                    <span style="color:#22c55e; font-weight:bold;">{pass_rate:.1f}%</span>
                </div>
            </div>
        </div>
    </div>
    
    <div style="color:var(--dim); font-size:0.85rem; margin-bottom:10px; margin-top:20px;">Smart Metrics (수급 및 실시간 필터링)</div>
    <div class="strategy-stats">
        <div class="strategy-card">
            <span class="str-label">Smart Buy (통과율: {pass_rate:.1f}%)</span>
            <span class="str-count" style="color:#22c55e">{count_smart_buy} <small style="font-size:0.7rem; color:var(--dim)">/ {total_signals} 건</small></span>
        </div>
        <div class="strategy-card">
            <span class="str-label">Smart Exit (실시간 매도)</span>
            <span class="str-count" style="color:#ef4444">{count_smart_exit} <small style="font-size:0.7rem; color:var(--dim)">건</small></span>
        </div>
        <div class="strategy-card">
            <span class="str-label">Signals Rejected (신호 거절됨)</span>
            <span class="str-count" style="color:var(--dim)">{count_rejected} <small style="font-size:0.7rem;">건</small></span>
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
            
            # [v32.1] Calculate Cash Metrics
            ratios = [(d['cash'] / d['equity'] * 100) for d in daily_equity if d['equity'] > 0]
            avg_cash_ratio = sum(ratios) / len(ratios) if ratios else 0
            
            # [v15.5.1] Synchronize Y-axes to ensure the red line tracks the top of the stacked area
            max_val = max(equities) * 1.05
            
            equity_chart_html = f"""
            <div style="background:var(--card-bg); border-radius:12px; padding:20px; margin-top:30px; border:1px solid #334155; display: block; clear: both; width: 100%; box-sizing: border-box; overflow: hidden;">
                <h3 style="margin-top:0; margin-bottom:15px; font-size:1.15rem; color:var(--primary); border-bottom: 1px solid #334155; padding-bottom: 10px;">📈 포트폴리오 자산 추이 및 비중 분석</h3>
                <div id="equity-chart" style="height:450px; width:100%; min-height:450px;"></div>
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
                    # [v18.0] Intraday Timestamp Handling: Match by nearest prior daily index
                    target_date = r['Date'].replace(hour=0, minute=0, second=0, microsecond=0)
                    m_idx = msi.index.get_indexer([target_date], method='pad')[0]
                    if m_idx == -1: # Try without time reset if pad failed
                        m_idx = msi.index.get_indexer([r['Date']], method='pad')[0]
                        
                    if m_idx != -1:
                        m_val, a_val, v_val = msi.iloc[m_idx], adx.iloc[m_idx], vix_series.iloc[m_idx]
                        if m_val > 0 and a_val > 25 and v_val < 20:
                            status_badge = '<span style="background:#22c55e; color:white; padding:2px 8px; border-radius:4px; font-weight:800; font-size:0.75rem;">UP (Aggressive)</span>'
                        elif m_val > 0 and v_val < 25:
                            status_badge = '<span style="background:#84cc16; color:white; padding:2px 8px; border-radius:4px; font-weight:800; font-size:0.75rem;">UP (Safe)</span>'
                        elif m_val < -1 or v_val > 30:
                            status_badge = '<span style="background:#ef4444; color:white; padding:2px 8px; border-radius:4px; font-weight:800; font-size:0.75rem;">DOWN (Risk)</span>'
                        else:
                            status_badge = '<span style="background:#f97316; color:white; padding:2px 8px; border-radius:4px; font-weight:800; font-size:0.75rem;">NEUTRAL</span>'
                    else:
                        # Fallback: Just show Neutral if data exists but index is slightly off
                        status_badge = '<span style="background:#64748b; color:white; padding:2px 8px; border-radius:4px; font-weight:800; font-size:0.75rem;">NEUTRAL</span>'
            except: pass

            amt = r['Price'] * r['Qty']
            fee = r.get('Fee', 0) if not pd.isna(r.get('Fee', 0)) else 0
            ret_str, action_cls, cum_profit_str = "", "action-buy", "-"
            
            action_raw = str(r['Action'])
            if "BUY" in action_raw:
                pass # Default values OK
            elif action_raw == 'REJECTED_SIG':
                action_cls = "action-rejected"
                ret_str = '<span style="color:var(--dim)">Filtered</span>'
            elif "PROTECT" in action_raw:
                action_cls = "action-hold" # [v8.0] 파란색 계열로 표시
                ret_str = "-"
                cum_profit_str = "-"
            else:
                ret_val = float(r.get('Return', 0)) if not pd.isna(r.get('Return', 0)) else 0
                # [v8.0] 실제 매매인 경우에만 누적 수익금 계산
                if action_raw in ['SELL', 'HOLD', 'PARTIAL_SELL']:
                    avg_p_rev = r['Price'] / (1 + ret_val/100) if (1 + ret_val/100) != 0 else r['Price']
                    p_inc = (r['Price'] - avg_p_rev) * r['Qty'] - fee
                    if not pd.isna(p_inc):
                        stock_realized_profit += p_inc
                
                action_cls = "action-sell"
                if r['Action'] in ["SELL", "HOLD", "PARTIAL_SELL"]:
                    r_cls = "profit" if ret_val > 0 else "loss"
                    ret_str = f'<span class="{r_cls}">{ret_val:+.2f}%</span>'
                    if r['Action'] == "HOLD":
                        action_cls = "action-hold"
                        ret_str = f'<span class="{r_cls}">{ret_val:+.2f}% (Held)</span>'
                
                p_row_cls = "profit" if stock_realized_profit >= 0 else "loss"
                p_val_display = int(stock_realized_profit) if not pd.isna(stock_realized_profit) else 0
                cum_profit_str = f'<span class="{p_row_cls}">{p_val_display:+,}</span>'
            
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

            group_rows.append(f"""<tr><td>{r['Date'].strftime('%m-%d %H:%M')}</td><td><span class="{action_cls}">{r['Action']}</span></td><td>{candle_svg}</td><td class="num">{_s_int(r['Price']):,}</td><td class="num">{_s_int(r['Qty']):,}</td><td class="num">{_s_int(amt):,}</td><td class="num">{_s_int(fee):,}</td><td class="num">{_s_int(r.get('Total_Qty', 0)):,}</td><td class="num">{ret_str}</td><td class="num">{cum_profit_str}</td><td class="num">{status_badge}</td><td class="num"><div class="reason-cell"><span class="reason-text">{reason_main} 🔍</span>{indicators_html}</div></td></tr>""")
        
        profit_color = "var(--gain)" if stock_realized_profit >= 0 else "var(--loss)"
        content.append(f"""<div class="stock-group" id="stock-{code_str}"><div class="stock-header"><div style="display:flex; align-items:center;">{name} <span style="color:var(--dim); font-size:0.9rem; margin-left:8px;">({code_str})</span>{chart_link_html}</div><div style="font-size:1rem; color:white">누적 수익금: <span style="color:{profit_color}">{_s_int(stock_realized_profit):+,}원</span></div></div><table><thead><tr><th>시간</th><th>구분</th><th>차트</th><th>가격</th><th>수량</th><th>거래액</th><th>비용</th><th>보유</th><th>수익률</th><th>누적 수익금액</th><th>시장</th><th>상세 사유</th></tr></thead><tbody>{"".join(group_rows)}</tbody></table></div>""")

    ret_color = "var(--gain)" if avg_ret > 0 else "var(--loss)"
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_template.format(now=now_str, interval=display_interval, mode_label=mode_label, mode_color=mode_color, total=total_trades, win_rate=win_rate, avg_ret=avg_ret, ret_color=ret_color, total_profit=total_profit_val, total_profit_color=total_profit_color, gross_profit=gross_profit_val, gross_profit_color=gross_profit_color, total_fees=total_fees_val, param_html=param_html, sidebar_items="".join(sidebar_items), content="".join(content), start_date=start_date if start_date else "Simulation", end_date=end_date if end_date else "End", equity_chart_html=equity_chart_html, strategy_stats_html=strategy_stats_html, avg_cash_ratio=avg_cash_ratio))
    print(f"  [Report] Execution log saved: {output_path}")
