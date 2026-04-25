import os
import pandas as pd
import plotly.graph_objects as go
from datetime import timedelta
import sys

# Add root directory to sys.path for absolute imports of root modules
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.append(root_dir)

from strategy.data_loader import BacktestDataLoader


def generate_stock_market_chart(
    code_str: str,
    name: str,
    s_df: pd.DataFrame,
    trades: pd.DataFrame,
    out_dir: str,
) -> str | None:
    """
    종목 가격 + 매수/매도 마커.
    시장 지표(MSI/ADX/VIX)·레짐 밴드는 사용하지 않는다.
    """
    if s_df is None or s_df.empty:
        return None
    try:
        idx = s_df.index
        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=idx,
                y=s_df["Close"],
                name="종가",
                line=dict(color="#E2E8F0", width=2),
                hovertemplate="가격: %{y:,.0f}원<extra></extra>",
            )
        )
        buys = trades[trades["Action"].astype(str).str.contains("BUY", case=False, na=False)]
        sells = trades[trades["Action"].astype(str).str.contains("SELL|HOLD", case=False, na=False)]
        if not buys.empty:
            fig.add_trace(
                go.Scatter(
                    x=buys["Date"],
                    y=buys["Price"],
                    mode="markers",
                    name="매수",
                    marker=dict(
                        symbol="triangle-up",
                        size=12,
                        color="#f87171",
                        line=dict(width=1, color="white"),
                    ),
                )
            )
        if not sells.empty:
            fig.add_trace(
                go.Scatter(
                    x=sells["Date"],
                    y=sells["Price"],
                    mode="markers",
                    name="매도/평가",
                    marker=dict(
                        symbol="triangle-down",
                        size=12,
                        color="#60a5fa",
                        line=dict(width=1, color="white"),
                    ),
                )
            )
        fig.update_layout(
            title=dict(text=f"<b>{name} ({code_str})</b>", x=0.02, y=0.98),
            template="plotly_dark",
            height=560,
            hovermode="x unified",
            paper_bgcolor="#0f172a",
            plot_bgcolor="#0f172a",
            margin=dict(l=60, r=40, t=80, b=60),
            showlegend=True,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        fig.update_yaxes(title_text="가격 (원)", tickformat=",d")
        filename = f"chart_{code_str}.html"
        fig.write_html(os.path.join(out_dir, filename), include_plotlyjs="cdn")
        return filename
    except Exception as e:
        try:
            print(f"  (!) 차트 생성 오류 ({code_str}): {e}")
        except Exception:
            pass
        return None


def generate_single_backtest_chart(
    code_str: str,
    best_settings: dict,
    trades_df: pd.DataFrame,
    interval: str,
    market_df: object = None,
    vix_df: object = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> str | None:
    """
    특정 종목에 대한 인터랙티브 차트(온디맨드).
    market_df / vix_df 인자는 하위 호환용으로만 남기며 무시한다.
    """
    _ = (best_settings, market_df, vix_df)  # 미사용
    try:
        loader = BacktestDataLoader()
        group = trades_df[trades_df["Code"] == code_str].sort_values(by="Date")
        if group.empty:
            try:
                code_str_int = int(str(code_str).zfill(6))
                group = trades_df[trades_df["Code"] == code_str_int].sort_values(by="Date")
            except Exception:
                group = pd.DataFrame()
        if group.empty:
            return None

        name = str(group["Name"].iloc[0])
        if not start_date:
            start_date = (group["Date"].min() - timedelta(days=5)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = (group["Date"].max() + timedelta(days=5)).strftime("%Y-%m-%d")

        price_df = loader.get_data(
            str(code_str).zfill(6), start_date, end_date, interval=interval
        )
        if price_df is None or price_df.empty:
            return None

        price_df = price_df[~price_df.index.duplicated(keep="last")].sort_index()
        charts_dir = os.path.join(os.getcwd(), "report_charts")
        try:
            os.makedirs(charts_dir, exist_ok=True)
        except OSError as e:
            try:
                print(f"  (!) report_charts 폴더 생성 실패: {e}")
            except Exception:
                pass
            return None

        return generate_stock_market_chart(code_str, name, price_df, group, charts_dir)
    except Exception as e:
        try:
            print(f"  (!) 백테 차트(온디맨드) 오류 ({code_str}): {e}")
        except Exception:
            pass
        return None
