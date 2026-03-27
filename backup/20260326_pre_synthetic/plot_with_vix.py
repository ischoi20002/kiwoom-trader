
import FinanceDataReader as fdr
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
import os
from datetime import datetime

def plot_with_vix():
    today_str = "2026-03-16"
    print(f"Fetching Market data (KOSPI & VIX) up to {today_str}...")
    
    # 1. Fetch KOSPI
    df_ks = fdr.DataReader('KS11', '2024-01-01', today_str)
    
    # 2. Fetch VIX (CBOE Volatility Index)
    # Using 'VIX' or 'FRED:VIX' usually works. 
    # For safety with Yahoo/FRED providers, let's try a common symbol.
    try:
        df_vix = fdr.DataReader('VIX', '2024-01-01', today_str)
    except:
        # Fallback to FRED if direct VIX fails
        df_vix = fdr.DataReader('FRED:VIXCLS', '2024-01-01', today_str)
        df_vix.columns = ['Close']

    if df_ks.empty or df_vix.empty:
        print("Failed to fetch some data.")
        return

    # Calculate KOSPI MAs
    df_ks['MA20'] = df_ks['Close'].rolling(20).mean()
    df_ks['MA60'] = df_ks['Close'].rolling(60).mean()
    df_ks['MA120'] = df_ks['Close'].rolling(120).mean()
    
    # Calculate Market Strength Index (MSI)
    df_ks['MSI'] = ((df_ks['MA20'] / df_ks['MA60'] - 1) + (df_ks['MA60'] / df_ks['MA120'] - 1)) * 100
    
    # Synchronize dates
    df_plot = df_ks.loc['2025-01-01':]
    vix_plot = df_vix.reindex(df_plot.index).ffill() # Align VIX to KOSPI dates
    
    # Plotting
    plt.style.use('dark_background')
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(12, 12), gridspec_kw={'height_ratios': [2, 1, 1]}, sharex=True)
    
    # --- Subplot 1: KOSPI Price and MAs ---
    ax1.plot(df_plot.index, df_plot['Close'], label='KOSPI Index', color='white', linewidth=1.5, alpha=0.9)
    ax1.plot(df_plot.index, df_plot['MA20'], label='MA20', color='#38bdf8', linewidth=1, alpha=0.8)
    ax1.plot(df_plot.index, df_plot['MA60'], label='MA60', color='#f59e0b', linewidth=1, alpha=0.8)
    ax1.plot(df_plot.index, df_plot['MA120'], label='MA120', color='#22c55e', linewidth=1, alpha=0.8)
    
    # Events
    events = [
        ('2025-03-24', 'BULL GC', '#22c55e'),
        ('2026-03-11', 'WAR/CORRECTION', '#f43f5e')
    ]
    for dt_str, label, color in events:
        dt = pd.to_datetime(dt_str)
        if dt in df_plot.index:
            ax1.axvline(x=dt, color=color, linestyle='--', alpha=0.2)
            ax2.axvline(x=dt, color=color, linestyle='--', alpha=0.2)
            ax3.axvline(x=dt, color=color, linestyle='--', alpha=0.2)
            ax1.annotate(label, xy=(dt, df_plot.loc[dt, 'Close']), xytext=(0, 20), textcoords='offset points', color=color, ha='center', fontsize=8, fontweight='bold')

    ax1.set_title(f'Market Analysis: KOSPI, MSI & VIX (Fear Gauge)', fontsize=16, color='#38bdf8', pad=20)
    ax1.legend(loc='upper left', frameon=True, facecolor='#1e293b', edgecolor='#334155')
    ax1.grid(True, linestyle=':', alpha=0.1)
    ax1.set_ylabel('KOSPI Index')

    # --- Subplot 2: Market Strength Index (MSI) ---
    msi_line = df_plot['MSI']
    ax2.plot(df_plot.index, msi_line, color='#f43f5e', linewidth=1.2, label='MSI (Strength)')
    ax2.fill_between(df_plot.index, msi_line, 0, where=(msi_line >= 0), color='#10b981', alpha=0.6)
    ax2.fill_between(df_plot.index, msi_line, 0, where=(msi_line < 0), color='#475569', alpha=0.6)
    ax2.axhline(y=0, color='white', linestyle='-', alpha=0.3)
    ax2.set_ylabel('Strength (%)')
    ax2.legend(loc='upper left', frameon=True, facecolor='#1e293b', edgecolor='#334155')

    # --- Subplot 3: VIX (Fear Gauge) ---
    ax3.plot(df_plot.index, vix_plot['Close'], color='#a855f7', linewidth=1.5, label='VIX Index')
    ax3.fill_between(df_plot.index, vix_plot['Close'], 20, where=(vix_plot['Close'] > 20), color='#f43f5e', alpha=0.4, label='Fear Zone (>20)')
    ax3.axhline(y=20, color='white', linestyle='--', alpha=0.4)
    ax3.axhline(y=30, color='#f43f5e', linestyle=':', alpha=0.6, label='Panic (>30)')
    
    ax3.set_ylabel('VIX (Volatility)')
    ax3.legend(loc='upper left', frameon=True, facecolor='#1e293b', edgecolor='#334155')
    ax3.grid(True, linestyle=':', alpha=0.1)

    plt.xticks(rotation=45)
    plt.tight_layout()
    
    artifact_path = r'C:\Users\ischo\.gemini\antigravity\brain\2b2248cb-83c8-4c02-a0b4-a63ed817d3b2\kospi_vix_analysis.png'
    plt.savefig(artifact_path, dpi=150)
    print(f"Full analysis chart saved to: {artifact_path}")

if __name__ == "__main__":
    plot_with_vix()
