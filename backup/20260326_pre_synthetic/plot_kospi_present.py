
import FinanceDataReader as fdr
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
import os
from datetime import datetime

def plot_kospi_to_present():
    today_str = "2026-03-16"
    print(f"Fetching KOSPI data up to {today_str}...")
    
    # Fetch from 2024 to get enough data for 120MA
    df = fdr.DataReader('KS11', '2024-01-01', today_str)
    
    if df.empty:
        print("Failed to fetch data.")
        return

    # Calculate MAs
    df['MA20'] = df['Close'].rolling(20).mean()
    df['MA60'] = df['Close'].rolling(60).mean()
    df['MA120'] = df['Close'].rolling(120).mean()
    
    # Calculate Market Strength Index (MSI)
    df['MSI'] = ((df['MA20'] / df['MA60'] - 1) + (df['MA60'] / df['MA120'] - 1)) * 100
    
    # Filter to show from 2025-01-01 to present
    df_plot = df.loc['2025-01-01':]
    
    # Plotting
    plt.style.use('dark_background')
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10), gridspec_kw={'height_ratios': [2, 1]}, sharex=True)
    
    # --- Subplot 1: Price and MAs ---
    ax1.plot(df_plot.index, df_plot['Close'], label='KOSPI Index', color='white', linewidth=1.5, alpha=0.9)
    ax1.plot(df_plot.index, df_plot['MA20'], label='MA20 (Short)', color='#38bdf8', linewidth=1, alpha=0.8)
    ax1.plot(df_plot.index, df_plot['MA60'], label='MA60 (Mid)', color='#f59e0b', linewidth=1, alpha=0.8)
    ax1.plot(df_plot.index, df_plot['MA120'], label='MA120 (Long)', color='#22c55e', linewidth=1, alpha=0.8)
    
    # Significant Points
    events = [
        ('2025-01-24', 'GC 20/60', '#38bdf8'),
        ('2025-03-24', 'BULL (GC 60/120)', '#22c55e'),
        ('2026-03-11', 'CORRECTION START?', '#f43f5e') # Recent peak/drop seen in backtests
    ]
    
    for dt_str, label, color in events:
        dt = pd.to_datetime(dt_str)
        if dt in df_plot.index:
            ax1.annotate(label, xy=(dt, df_plot.loc[dt, 'Close']), 
                        xytext=(0, 25), textcoords='offset points', ha='center',
                        arrowprops=dict(arrowstyle='->', color=color),
                        color=color, fontsize=8, fontweight='bold')
            ax1.axvline(x=dt, color=color, linestyle='--', alpha=0.2)
            ax2.axvline(x=dt, color=color, linestyle='--', alpha=0.2)

    ax1.set_title(f'KOSPI Market Analysis: 2025 - {today_str}', fontsize=16, color='#38bdf8', pad=20)
    ax1.legend(loc='upper left', frameon=True, facecolor='#1e293b', edgecolor='#334155')
    ax1.grid(True, linestyle=':', alpha=0.1)
    ax1.set_ylabel('Index Price')

    # --- Subplot 2: Market Strength Index (MSI) ---
    msi_line = df_plot['MSI']
    ax2.plot(df_plot.index, msi_line, color='#f43f5e', linewidth=1.5, label='MSI (MA Spread %)')
    
    # Fill based on strength
    ax2.fill_between(df_plot.index, msi_line, 0, where=(msi_line >= 0), color='#22c55e', alpha=0.6)
    ax2.fill_between(df_plot.index, msi_line, 0, where=(msi_line < 0), color='#ef4444', alpha=0.6)
    
    ax2.axhline(y=5, color='#22c55e', linestyle=':', alpha=0.5, label='High Strength (>5%)')
    ax2.axhline(y=0, color='white', linestyle='-', alpha=0.3)
    
    ax2.set_ylabel('Strength Index (%)')
    ax2.legend(loc='upper left', frameon=True, facecolor='#1e293b', edgecolor='#334155')
    ax2.grid(True, linestyle=':', alpha=0.1)
    
    plt.xticks(rotation=45)
    plt.tight_layout()
    
    artifact_path = r'C:\Users\ischo\OneDrive\바이브\kospi_present_strength.png'
    plt.savefig(artifact_path, dpi=150)
    
    # Copy to brain artifacts
    brain_path = r'C:\Users\ischo\.gemini\antigravity\brain\2b2248cb-83c8-4c02-a0b4-a63ed817d3b2\kospi_present_strength.png'
    plt.savefig(brain_path, dpi=150)
    
    print(f"Extended analysis chart saved to: {brain_path}")

if __name__ == "__main__":
    plot_kospi_to_present()
