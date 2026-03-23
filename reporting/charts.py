import os
import json
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timedelta
import sys

# Add root directory to sys.path for absolute imports of root modules
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.append(root_dir)

from strategy.data_loader import BacktestDataLoader

def generate_stock_market_chart(code_str, name, s_df, msi_sync, adx_sync, vix_sync, trades, out_dir):
    """[v11.5] ULTRA FINAL Strategy Dashboard: Layer-Focused Banding"""
    if s_df is None or s_df.empty: return None
    
    try:
        idx = s_df.index
        # Precise cleaning
        msi_c = msi_sync.ffill().bfill().fillna(0) if msi_sync is not None else pd.Series(0, index=idx)
        adx_c = adx_sync.ffill().bfill().fillna(0) if adx_sync is not None else pd.Series(0, index=idx)
        vix_c = vix_sync.ffill().bfill().fillna(0) if vix_sync is not None else pd.Series(0, index=idx)

        fig = make_subplots(
            rows=4, cols=1, 
            shared_xaxes=True, 
            vertical_spacing=0.04, 
            row_heights=[0.5, 0.16, 0.16, 0.16],
            specs=[[{"secondary_y": True}], [{"secondary_y": False}], [{"secondary_y": False}], [{"secondary_y": False}]],
            subplot_titles=(
                f"<b>Price & Strategy Visualization (v11.5)</b>", 
                f"<b>ADX (Trend Strength)</b>", 
                f"<b>VIX (Risk Scale)</b>", 
                f"<b>MSI (Market Strength)</b>"
            )
        )

        # 1. Row 1: Strategy Status Color Logic
        def get_vivid_color(m, a, v):
            if m > 0 and a > 25 and v < 20: return "rgba(34, 197, 94, 0.45)"  # Pure Green
            if m > 0 and v < 20: return "rgba(16, 185, 129, 0.25)"            # Emerald
            if m < 0 and v > 30: return "rgba(244, 63, 94, 0.4)"             # Bright Rose
            return "rgba(100, 116, 139, 0.1)"                                # Neutral Slate
        
        colors = [get_vivid_color(msi_c.iloc[i], adx_c.iloc[i], vix_c.iloc[i]) for i in range(len(idx))]
        
        start_t = idx[0]
        curr_c = colors[0]
        for i in range(1, len(idx)):
            if colors[i] != curr_c:
                fig.add_vrect(x0=start_t, x1=idx[i], fillcolor=curr_c, line_width=0, opacity=1, row=1, col=1)
                start_t, curr_c = idx[i], colors[i]
        fig.add_vrect(x0=start_t, x1=idx[-1], fillcolor=curr_c, line_width=0, opacity=1, row=1, col=1)

        # 2. Main Price Trace
        fig.add_trace(go.Scatter(
            x=idx, y=s_df['Close'], name='Stock Price', 
            line=dict(color='#FFFFFF', width=2.5),
            hovertemplate="Price: %{y:,.0f} KRW<extra></extra>"
        ), row=1, col=1, secondary_y=False)

        # 3. Synchronized Tooltip Ghost Traces
        status_labels = []
        for i in range(len(idx)):
            m, a, v = msi_c.iloc[i], adx_c.iloc[i], vix_c.iloc[i]
            if m > 0 and a > 25 and v < 20: status_labels.append("Aggressive Buy (Green)")
            elif m > 0 and v < 20: status_labels.append("Safe Trade (Emerald)")
            elif m < 0 and v > 30: status_labels.append("Risk/Panic (Rose)")
            else: status_labels.append("Watch Out (Slate)")

        fig.add_trace(go.Scatter(
            x=idx, y=[0]*len(idx), name='STATUS', marker=dict(opacity=0),
            customdata=status_labels, hovertemplate="<b>Status: %{customdata}</b><extra></extra>",
            showlegend=False
        ), row=1, col=1, secondary_y=True)

        fig.add_trace(go.Scatter(x=idx, y=[0]*len(idx), name='MSI', marker=dict(opacity=0), customdata=msi_c, 
                                hovertemplate="MSI: %{customdata:+.1f}%<extra></extra>", showlegend=False), row=1, col=1, secondary_y=True)
        fig.add_trace(go.Scatter(x=idx, y=[0]*len(idx), name='ADX', marker=dict(opacity=0), customdata=adx_c, 
                                hovertemplate="ADX: %{customdata:.1f}<extra></extra>", showlegend=False), row=1, col=1, secondary_y=True)
        fig.add_trace(go.Scatter(x=idx, y=[0]*len(idx), name='VIX', marker=dict(opacity=0), customdata=vix_c, 
                                hovertemplate="VIX: %{customdata:.1f}<extra></extra>", showlegend=False), row=1, col=1, secondary_y=True)

        # 3. Indicators
        fig.add_trace(go.Scatter(x=idx, y=adx_c, name='ADX', line=dict(color='#fbbf24', width=2)), row=2, col=1)
        fig.add_trace(go.Scatter(x=idx, y=vix_c, name='VIX', line=dict(color='#c084fc', width=2, dash='dot')), row=3, col=1)
        fig.add_trace(go.Scatter(x=idx, y=msi_c, name='MSI', fill='tozeroy', line=dict(color='#34d399', width=2), 
                                fillcolor='rgba(52, 211, 153, 0.2)'), row=4, col=1)

        fig.update_layout(
            title=dict(text=f"<b>{name} ({code_str}) Final Dashboard v11.5</b>", x=0.02, y=0.98),
            template='plotly_dark',
            height=1000,
            hovermode='x',
            paper_bgcolor='#0f172a',
            plot_bgcolor='#0f172a',
            margin=dict(l=60, r=60, t=100, b=100),
            showlegend=True,
            legend=dict(orientation="h", yanchor="bottom", y=-0.1, xanchor="center", x=0.5)
        )

        fig.update_yaxes(title_text="Price (KRW)", tickformat=',d', row=1, col=1, secondary_y=False)
        fig.update_yaxes(showticklabels=False, showgrid=False, range=[0, 100], row=1, col=1, secondary_y=True)
        fig.update_yaxes(title_text="ADX", row=2, col=1)
        fig.update_yaxes(title_text="VIX", row=3, col=1)
        fig.update_yaxes(title_text="MSI (%)", row=4, col=1)

        # 5. Signals
        buys = trades[trades['Action'].str.contains('BUY')]
        sells = trades[trades['Action'].str.contains('SELL')]
        
        for _, t in trades.iterrows():
            is_buy = 'BUY' in t['Action']
            color = '#ef4444' if is_buy else '#3b82f6'
            fig.add_vline(x=t['Date'], line_width=1.5, line_dash="dash", line_color=color, opacity=0.3)

        if not buys.empty:
            fig.add_trace(go.Scatter(x=buys['Date'], y=buys['Price'], mode='markers', name='BUY', marker=dict(symbol='triangle-up', size=16, color='#ef4444', line=dict(width=1, color='white'))), row=1, col=1)
        if not sells.empty:
            fig.add_trace(go.Scatter(x=sells['Date'], y=sells['Price'], mode='markers', name='SELL', marker=dict(symbol='triangle-down', size=16, color='#3b82f6', line=dict(width=1, color='white'))), row=1, col=1)

        filename = f"chart_{code_str}.html"
        fig.write_html(os.path.join(out_dir, filename), include_plotlyjs='cdn')
        return filename

    except Exception as e:
        print(f"  (!) Dashboard error ({code_str}): {e}")
        return None

def generate_single_backtest_chart(code_str, best_settings, trades_df, interval, market_df=None, vix_df=None, start_date=None, end_date=None):
    """[v8.5] 특정 종목에 대한 인터랙티브 차트를 온디맨드(On-demand)로 생성"""
    try:
        loader = BacktestDataLoader()
        group = trades_df[trades_df['Code'] == code_str].sort_values(by='Date')
        if group.empty:
            code_str_int = int(code_str)
            group = trades_df[trades_df['Code'] == code_str_int].sort_values(by='Date')
            if group.empty: return None

        name = group['Name'].iloc[0]
        
        if not start_date:
            start_date = (group['Date'].min() - timedelta(days=5)).strftime('%Y-%m-%d')
        if not end_date:
            end_date = (group['Date'].max() + timedelta(days=5)).strftime('%Y-%m-%d')
        
        price_df = loader.get_data(code_str, start_date, end_date, interval=interval)
        if price_df is None or price_df.empty: return None
        
        price_df = price_df[~price_df.index.duplicated(keep='last')].sort_index()
        idx = price_df.index
        msi_sync, adx_sync, vix_sync = None, None, None
        
        if market_df is not None and not market_df.empty:
            m_df = market_df[~market_df.index.duplicated(keep='last')].sort_index()
            m_close = m_df['Close'] if 'Close' in m_df else m_df.iloc[:, 0]
            
            m_ma20 = m_close.rolling(20).mean()
            m_ma60 = m_close.rolling(60).mean()
            m_ma120 = m_close.rolling(120).mean()
            msi = ((m_ma20 / m_ma60 - 1) + (m_ma60 / m_ma120 - 1)) * 100
            msi_sync = msi.reindex(idx, method='ffill')
            
            def _calc_adx(df, n=14):
                try:
                    df = df.copy()
                    h, l, c = df['High'], df['Low'], df['Close']
                    tr = pd.concat([h-l, abs(h-c.shift(1)), abs(l-c.shift(1))], axis=1).max(axis=1)
                    pdm = np.where((h-h.shift(1))>(l.shift(1)-l), np.maximum(h-h.shift(1), 0), 0)
                    mdm = np.where((l.shift(1)-l)>(h-h.shift(1)), np.maximum(l.shift(1)-l, 0), 0)
                    trs = tr.rolling(n).sum().replace(0, np.nan)
                    pdms, mdms = pd.Series(pdm, index=df.index).rolling(n).sum(), pd.Series(mdm, index=df.index).rolling(n).sum()
                    pdi, mdi = 100*(pdms/trs).fillna(0), 100*(mdms/trs).fillna(0)
                    dx_denom = (pdi+mdi).replace(0, np.nan)
                    dx = 100*(abs(pdi-mdi)/dx_denom).fillna(0)
                    return dx.rolling(n).mean().fillna(0)
                except: return pd.Series(0, index=df.index)
            adx = _calc_adx(m_df)
            adx_sync = adx.reindex(idx, method='ffill')

        if vix_df is not None and not vix_df.empty:
            v_df = vix_df[~vix_df.index.duplicated(keep='last')].sort_index()
            v_close = v_df['Close'] if 'Close' in v_df else v_df.iloc[:, 0]
            vix_sync = v_close.reindex(idx, method='ffill')

        charts_dir = os.path.join(os.getcwd(), "report_charts")
        if not os.path.exists(charts_dir): os.makedirs(charts_dir)
        
        return generate_stock_market_chart(code_str, name, price_df, msi_sync, adx_sync, vix_sync, group, charts_dir)
    except Exception as e:
        print(f"  (!) On-demand chart error ({code_str}): {e}")
        return None
