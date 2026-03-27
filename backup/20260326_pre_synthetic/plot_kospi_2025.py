
import FinanceDataReader as fdr
import matplotlib.pyplot as plt
import pandas as pd
import os

def plot_kospi():
    print("Fetching KOSPI data...")
    df = fdr.DataReader('KS11', '2024-01-01', '2025-12-31')
    
    # Calculate MAs
    df['MA20'] = df['Close'].rolling(20).mean()
    df['MA60'] = df['Close'].rolling(60).mean()
    df['MA120'] = df['Close'].rolling(120).mean()
    
    # Filter for 2025
    df_2025 = df.loc['2025-01-01':'2025-12-31']
    
    # Plotting
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(12, 7))
    
    ax.plot(df_2025.index, df_2025['Close'], label='KOSPI Index', color='white', linewidth=1.5, alpha=0.9)
    ax.plot(df_2025.index, df_2025['MA20'], label='MA20', color='#38bdf8', linewidth=1, alpha=0.8)
    ax.plot(df_2025.index, df_2025['MA60'], label='MA60', color='#f59e0b', linewidth=1, alpha=0.8)
    ax.plot(df_2025.index, df_2025['MA120'], label='MA120', color='#22c55e', linewidth=1, alpha=0.8)
    
    # Highlight Golden Cross points found earlier
    # 20/60 GC: 2025-01-24, 2025-05-19
    # 60/120 GC: 2025-03-24
    gc_dates = [
        ('2025-01-24', 'GC 20/60', '#38bdf8'),
        ('2025-05-19', 'GC 20/60', '#38bdf8'),
        ('2025-03-24', 'DEEP BULL (GC 60/120)', '#22c55e')
    ]
    
    for dt_str, label, color in gc_dates:
        dt = pd.to_datetime(dt_str)
        if dt in df_2025.index:
            ax.annotate(label, xy=(dt, df_2025.loc[dt, 'Close']), 
                        xytext=(10, 20), textcoords='offset points',
                        arrowprops=dict(arrowstyle='->', color=color),
                        color=color, fontsize=9, fontweight='bold')
            ax.axvline(x=dt, color=color, linestyle='--', alpha=0.3)

    ax.set_title('KOSPI Index & Moving Averages (2025)', fontsize=16, color='#38bdf8', pad=20)
    ax.set_xlabel('Date', color='#94a3b8')
    ax.set_ylabel('Price', color='#94a3b8')
    ax.legend(loc='upper left', frameon=True, facecolor='#1e293b', edgecolor='#334155')
    ax.grid(True, linestyle=':', alpha=0.2)
    
    # Prettify ticks
    plt.xticks(rotation=45)
    plt.tight_layout()
    
    output_path = r'C:\Users\ischo\.gemini\antigravity\brain\2b2248cb-83c8-4c02-a0b4-a63ed817d3b2\kospi_2025_chart.png'
    plt.savefig(output_path, dpi=150)
    print(f"Chart saved to: {output_path}")

if __name__ == "__main__":
    plot_kospi()
