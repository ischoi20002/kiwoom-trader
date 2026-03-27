
import FinanceDataReader as fdr
import matplotlib.pyplot as plt
import pandas as pd
import numpy as np
import os

def plot_kospi_with_strength():
    print("Fetching KOSPI data and calculating strength...")
    df = fdr.DataReader('KS11', '2024-01-01', '2025-12-31')
    
    # Calculate MAs
    df['MA20'] = df['Close'].rolling(20).mean()
    df['MA60'] = df['Close'].rolling(60).mean()
    df['MA120'] = df['Close'].rolling(120).mean()
    
    # Calculate Market Strength Index (MSI) based on MA Spread
    # MSI = (MA20/MA60 - 1) + (MA60/MA120 - 1) normalized as %
    df['MSI'] = ((df['MA20'] / df['MA60'] - 1) + (df['MA60'] / df['MA120'] - 1)) * 100
    
    # Filter for 2025
    df_2025 = df.loc['2025-01-01':'2025-12-31']
    
    # Plotting
    plt.style.use('dark_background')
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10), gridspec_kw={'height_ratios': [2, 1]}, sharex=True)
    
    # --- Subplot 1: Price and MAs ---
    ax1.plot(df_2025.index, df_2025['Close'], label='KOSPI Index', color='white', linewidth=1.5, alpha=0.9)
    ax1.plot(df_2025.index, df_2025['MA20'], label='MA20 (Short)', color='#38bdf8', linewidth=1, alpha=0.8)
    ax1.plot(df_2025.index, df_2025['MA60'], label='MA60 (Mid)', color='#f59e0b', linewidth=1, alpha=0.8)
    ax1.plot(df_2025.index, df_2025['MA120'], label='MA120 (Long)', color='#22c55e', linewidth=1, alpha=0.8)
    
    # Annotate Golden Crosses
    gc_dates = [
        ('2025-01-24', 'GC 20/60', '#38bdf8'),
        ('2025-03-24', 'DEEP BULL (GC 60/120)', '#22c55e'),
        ('2025-05-19', 'GC 20/60', '#38bdf8')
    ]
    
    for dt_str, label, color in gc_dates:
        dt = pd.to_datetime(dt_str)
        if dt in df_2025.index:
            ax1.annotate(label, xy=(dt, df_2025.loc[dt, 'Close']), 
                        xytext=(10, 20), textcoords='offset points',
                        arrowprops=dict(arrowstyle='->', color=color),
                        color=color, fontsize=9, fontweight='bold')
            ax1.axvline(x=dt, color=color, linestyle='--', alpha=0.2)
            ax2.axvline(x=dt, color=color, linestyle='--', alpha=0.2)

    ax1.set_title('KOSPI 2025: Index & Market Strength Analysis', fontsize=16, color='#38bdf8', pad=20)
    ax1.legend(loc='upper left', frameon=True, facecolor='#1e293b', edgecolor='#334155')
    ax1.grid(True, linestyle=':', alpha=0.1)
    ax1.set_ylabel('Index Price')

    # --- Subplot 2: Market Strength Index (MSI) ---
    msi_line = df_2025['MSI']
    ax2.plot(df_2025.index, msi_line, color='#f43f5e', linewidth=1.5, label='MSI (MA Spread %)')
    
    # Fill based on strength
    ax2.fill_between(df_2025.index, msi_line, 0, where=(msi_line >= 0), color='#10b981', alpha=0.6)
    ax2.fill_between(df_2025.index, msi_line, 0, where=(msi_line < 0), color='#475569', alpha=0.6)
    
    # Threshold lines
    ax2.axhline(y=5, color='#22c55e', linestyle=':', alpha=0.5, label='High Strength (>5%)')
    ax2.axhline(y=0, color='white', linestyle='-', alpha=0.3)
    
    ax2.set_ylabel('Strength Index (%)')
    ax2.legend(loc='upper left', frameon=True, facecolor='#1e293b', edgecolor='#334155')
    ax2.grid(True, linestyle=':', alpha=0.1)
    
    plt.xticks(rotation=45)
    plt.tight_layout()
    
    output_path = r'C:\Users\ischo\OneDrive\바이브\kospi_2025_strength_analysis.png'
    # Copy to artifacts directory later or use absolute path
    plt.savefig(output_path, dpi=150)
    
    # Also save to artifacts for embedding
    artifact_path = r'C:\Users\ischo\.gemini\antigravity\brain\2b2248cb-83c8-4c02-a0b4-a63ed817d3b2\kospi_2025_strength.png'
    plt.savefig(artifact_path, dpi=150)
    
    print(f"Analysis chart saved to: {artifact_path}")

if __name__ == "__main__":
    plot_kospi_with_strength()
