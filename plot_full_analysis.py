
import FinanceDataReader as fdr
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
import os
from datetime import datetime

def calculate_adx(df, n=14):
    df = df.copy()
    df['H-L'] = df['High'] - df['Low']
    df['H-PC'] = abs(df['High'] - df['Close'].shift(1))
    df['L-PC'] = abs(df['Low'] - df['Close'].shift(1))
    df['TR'] = df[['H-L', 'H-PC', 'L-PC']].max(axis=1)
    
    df['+DM'] = np.where((df['High'] - df['High'].shift(1)) > (df['Low'].shift(1) - df['Low']), 
                         np.maximum(df['High'] - df['High'].shift(1), 0), 0)
    df['-DM'] = np.where((df['Low'].shift(1) - df['Low']) > (df['High'] - df['High'].shift(1)), 
                         np.maximum(df['Low'].shift(1) - df['Low'], 0), 0)
    
    # Smoothed TR, +DM, -DM using Wilder's EMA or simple rolling sum
    tr_s = df['TR'].rolling(n).sum()
    pdm_s = df['+DM'].rolling(n).sum()
    mdm_s = df['-DM'].rolling(n).sum()
    
    df['+DI'] = 100 * (pdm_s / tr_s)
    df['-DI'] = 100 * (mdm_s / tr_s)
    
    df['DX'] = 100 * (abs(df['+DI'] - df['-DI']) / (df['+DI'] + df['-DI']))
    df['ADX'] = df['DX'].rolling(n).mean()
    
    return df[['ADX', '+DI', '-DI']]

def plot_comprehensive_analysis():
    today_str = "2026-03-16"
    print(f"Fetching Comprehensive Market data up to {today_str}...")
    
    # 1. Fetch KOSPI
    df_ks = fdr.DataReader('KS11', '2024-01-01', today_str)
    
    # 2. Fetch VIX
    try:
        df_vix = fdr.DataReader('VIX', '2024-01-01', today_str)
    except:
        df_vix = fdr.DataReader('FRED:VIXCLS', '2024-01-01', today_str)
        df_vix.columns = ['Close']

    if df_ks.empty or df_vix.empty:
        print("Failed to fetch some data.")
        return

    # Calculate Indicators
    df_ks['MA20'] = df_ks['Close'].rolling(20).mean()
    df_ks['MA60'] = df_ks['Close'].rolling(60).mean()
    df_ks['MA120'] = df_ks['Close'].rolling(120).mean()
    df_ks['MSI'] = ((df_ks['MA20'] / df_ks['MA60'] - 1) + (df_ks['MA60'] / df_ks['MA120'] - 1)) * 100
    
    adx_df = calculate_adx(df_ks)
    df_ks = pd.concat([df_ks, adx_df], axis=1)
    
    # Synchronize and filter
    df_plot = df_ks.loc['2025-01-01':]
    vix_plot = df_vix.reindex(df_plot.index).ffill()
    
    plt.style.use('dark_background')
    fig, (ax1, ax2, ax3, ax4) = plt.subplots(4, 1, figsize=(12, 14), gridspec_kw={'height_ratios': [2, 1, 1, 1]}, sharex=True)
    
    # Color palette
    colors = {'price': 'white', 'ma20': '#38bdf8', 'ma60': '#f59e0b', 'ma120': '#22c55e', 
              'msi': '#f43f5e', 'vix': '#a855f7', 'adx': '#facc15'}

    # --- Subplot 1: Price ---
    ax1.plot(df_plot.index, df_plot['Close'], label='KOSPI', color=colors['price'], linewidth=1.2)
    ax1.plot(df_plot.index, df_plot['MA20'], label='MA20', color=colors['ma20'], linewidth=1, alpha=0.7)
    ax1.plot(df_plot.index, df_plot['MA120'], label='MA120', color=colors['ma120'], linewidth=1, alpha=0.7)
    ax1.set_title('KOSPI Comprehensive Trend Analysis (2025 - Present)', fontsize=16, color=colors['ma20'], pad=20)
    ax1.legend(loc='upper left', frameon=False)
    ax1.grid(alpha=0.1)

    # --- Subplot 2: MSI (Market Strength) ---
    ax2.plot(df_plot.index, df_plot['MSI'], color=colors['msi'], label='MSI (MA Spread %)')
    ax2.fill_between(df_plot.index, df_plot['MSI'], 0, where=(df_plot['MSI'] >= 0), color='#22c55e', alpha=0.6)
    ax2.fill_between(df_plot.index, df_plot['MSI'], 0, where=(df_plot['MSI'] < 0), color='#ef4444', alpha=0.6)
    ax2.set_ylabel('MSI (%)')
    ax2.legend(loc='upper left', frameon=False)

    # --- Subplot 3: ADX (Trend Strength) ---
    ax3.plot(df_plot.index, df_plot['ADX'], color=colors['adx'], linewidth=2, label='ADX (Trend Strength)')
    ax3.plot(df_plot.index, df_plot['+DI'], color='#22c55e', linestyle='--', linewidth=0.8, alpha=0.6, label='+DI')
    ax3.plot(df_plot.index, df_plot['-DI'], color='#f43f5e', linestyle='--', linewidth=0.8, alpha=0.6, label='-DI')
    ax3.axhline(y=25, color='gray', linestyle=':', alpha=0.5, label='Active Trend (25)')
    ax3.set_ylabel('ADX / DI')
    ax3.legend(loc='upper left', frameon=False, ncol=3)

    # --- Subplot 4: VIX (Fear Gauge) ---
    ax4.plot(df_plot.index, vix_plot['Close'], color=colors['vix'], label='VIX Index')
    ax4.fill_between(df_plot.index, vix_plot['Close'], 20, where=(vix_plot['Close'] > 20), color='#f43f5e', alpha=0.4)
    ax4.axhline(y=20, color='white', linestyle='--', alpha=0.3)
    ax4.set_ylabel('VIX')
    ax4.legend(loc='upper left', frameon=False)

    # Marker for Mar 11 War
    for ax in [ax1, ax2, ax3, ax4]:
        dt_war = pd.to_datetime('2026-03-11')
        if dt_war in df_plot.index:
            ax.axvline(x=dt_war, color='#f43f5e', linestyle='-', linewidth=1.5, alpha=0.5)
    
    ax1.annotate('WAR START', xy=(pd.to_datetime('2026-03-11'), df_plot.loc['2026-03-11', 'Close']), 
                 xytext=(-60, 20), textcoords='offset points', color='#f43f5e', 
                 arrowprops=dict(arrowstyle='->', color='#f43f5e'), fontweight='bold')

    plt.xticks(rotation=45)
    plt.tight_layout()
    
    output_path = r'C:\Users\ischo\.gemini\antigravity\brain\2b2248cb-83c8-4c02-a0b4-a63ed817d3b2\market_full_analysis.png'
    plt.savefig(output_path, dpi=150)
    print(f"Full comprehensive chart saved to: {output_path}")

if __name__ == "__main__":
    plot_comprehensive_analysis()
