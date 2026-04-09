# -*- coding: utf-8 -*-
"""
KOSPI(001) 일봉 + VIX로 4단계 시장 레짐(DOWN/NEUTRAL/UP/AGGRESSIVE)을
백테스트와 동일한 분기(strategy.market_regime)로 산출하고,
지정 기간을 색 밴드로 구분한 Plotly HTML 차트를 생성합니다.

사용 예:
  python tools/plot_market_regime_bands.py
  python tools/plot_market_regime_bands.py --start 2024-04-01 --end 2026-03-31 -o regime.html
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np
import pandas as pd

# 프로젝트 루트
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backtest_engine import TradingStrategy  # noqa: E402
from data_manager_v2 import DataManagerV2  # noqa: E402
from strategy.market_regime import (  # noqa: E402
    classify_market_regime_series,
    msi_band_thresholds,
)

# 레짐별 밴드 색 (Nord 계열, 투명도는 vrect에서 조절)
_REGIME_COLORS: Dict[str, str] = {
    "DOWN": "#BF616A",
    "NEUTRAL": "#81A1C1",
    "UP": "#A3BE8C",
    "AGGRESSIVE": "#EBCB8B",
}


def _load_settings(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except OSError:
        return {}
    except json.JSONDecodeError:
        return {}


def _msi_display_y_range(
    msi_series: pd.Series,
    msi_down: float,
    msi_up: float,
) -> Tuple[float, float]:
    """
    Plotly 보조 Y축(MSI) 범위.
    자동 스케일이 종가 축과 섞이거나 tozero로 눌려 수평선처럼 보이는 경우를 막기 위해
    데이터·임계선·0을 모두 포함하는 구간을 명시한다.
    """
    try:
        s = pd.to_numeric(msi_series, errors="coerce").dropna()
        if s.empty:
            lo, hi = -0.2, 0.2
        else:
            lo, hi = float(s.min()), float(s.max())
        # 임계는 market_regime에서 이미 정상 범위로 정화됨; 0축 포함
        lo = min(lo, float(msi_down), 0.0)
        hi = max(hi, float(msi_up), 0.0)
        span = max(hi - lo, 1e-6)
        pad = max(span * 0.15, 0.03)
        return lo - pad, hi + pad
    except Exception:
        return -0.35, 0.35


def _daily_from_db(
    code: str,
    start_dt: str,
    end_dt: str,
) -> Optional[pd.DataFrame]:
    """bars_1d에서 일봉 로드 → DatetimeIndex, OHLCV 표준 컬럼."""
    try:
        db_path = os.path.join(os.getcwd(), "trading_data.db")
        if not os.path.isfile(db_path):
            return None
        dm = DataManagerV2(db_path)
        df = dm.get_daily_bars(code, start_dt=start_dt, end_dt=end_dt)
        if df is None or df.empty:
            return None
        tcol = "time" if "time" in df.columns else "Time"
        df = df.copy()
        df["Date"] = pd.to_datetime(df[tcol])
        df.set_index("Date", inplace=True)
        rename = {}
        for c in df.columns:
            cl = c.lower()
            if cl in ("open", "high", "low", "close", "volume"):
                rename[c] = c.capitalize() if c.islower() else c
        df.rename(columns=rename, inplace=True)
        need = {"Open", "High", "Low", "Close", "Volume"}
        if not need.issubset(set(df.columns)):
            return None
        return df[list(need)].sort_index()
    except Exception:
        return None


def _daily_from_fdr(w_start: str, end_dt: str) -> Tuple[Optional[pd.DataFrame], Optional[pd.Series]]:
    """FinanceDataReader로 KS11·VIX (백테스트 _prepare_market_data와 유사)."""
    try:
        import FinanceDataReader as fdr
    except Exception:
        return None, None
    try:
        market_df = fdr.DataReader("KS11", w_start, end_dt)
        if market_df is None or market_df.empty:
            return None, None
        market_df = market_df.copy()
        market_df.columns = [
            c.capitalize() if str(c).lower() == "close" else c for c in market_df.columns
        ]
        if "Close" not in market_df.columns:
            return None, None
        for c in ("Open", "High", "Low", "Volume"):
            if c not in market_df.columns:
                if c == "Volume":
                    market_df[c] = 0.0
                else:
                    market_df[c] = market_df["Close"]
        try:
            vix_df = fdr.DataReader("VIX", w_start, end_dt)
        except Exception:
            try:
                vix_df = fdr.DataReader("FRED:VIXCLS", w_start, end_dt)
            except Exception:
                vix_df = None
        if vix_df is None or vix_df.empty:
            vix_s = pd.Series(np.nan, index=market_df.index)
        else:
            vix_df = vix_df.copy()
            if "Close" not in vix_df.columns and len(vix_df.columns):
                vix_df.columns = ["Close"] + list(vix_df.columns[1:])
            vix_df.columns = [
                c.capitalize() if str(c).lower() == "close" else c for c in vix_df.columns
            ]
            vix_s = vix_df["Close"].reindex(market_df.index).ffill()
        return market_df[["Open", "High", "Low", "Close", "Volume"]], vix_s
    except Exception:
        return None, None


def _contiguous_regime_segments(
    idx: pd.DatetimeIndex,
    regime: pd.Series,
) -> List[Tuple[pd.Timestamp, pd.Timestamp, str]]:
    """연속 동일 레짐 구간 (x0, x1, 이름) 리스트."""
    if len(idx) == 0:
        return []
    r = regime.reindex(idx).fillna("UP")
    segs: List[Tuple[pd.Timestamp, pd.Timestamp, str]] = []
    start = idx[0]
    cur = str(r.iloc[0])
    for i in range(1, len(idx)):
        ri = str(r.iloc[i])
        if ri != cur:
            segs.append((start, idx[i - 1], cur))
            start = idx[i]
            cur = ri
    segs.append((start, idx[-1], cur))
    return segs


def build_regime_chart(
    plot_start: str,
    plot_end: str,
    settings: Mapping[str, Any],
    *,
    warmup_days: int = 200,
) -> "go.Figure":
    """plot_start~plot_end 구간 밴드 차트 (지표는 앞쪽 warmup 포함)."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except Exception as e:
        raise RuntimeError("Plotly가 필요합니다. pip install plotly") from e

    p0 = pd.Timestamp(plot_start)
    p1 = pd.Timestamp(plot_end)
    w0 = (p0 - timedelta(days=warmup_days)).strftime("%Y-%m-%d")
    end_s = p1.strftime("%Y-%m-%d")

    market_df = _daily_from_db("001", w0, end_s)
    vix_df = _daily_from_db("^VIX", w0, end_s)
    vix_s: Optional[pd.Series] = None
    if vix_df is not None and not vix_df.empty and "Close" in vix_df.columns:
        vix_s = vix_df["Close"].reindex(market_df.index).ffill() if market_df is not None else None

    if market_df is None or len(market_df) < 130:
        m2, v2 = _daily_from_fdr(w0, end_s)
        if m2 is not None and len(m2) >= 130:
            market_df = m2
            vix_s = v2
        elif market_df is None or len(market_df) < 130:
            raise RuntimeError(
                "KOSPI 일봉이 부족합니다. trading_data.db 001 일봉을 채우거나 "
                "FinanceDataReader 네트워크를 확인하세요."
            )

    if vix_s is None:
        vix_s = pd.Series(15.0, index=market_df.index)
    else:
        vix_s = vix_s.reindex(market_df.index).ffill().bfill()
        vix_s = vix_s.fillna(15.0)

    m_ind = TradingStrategy.calculate_indicators(market_df.copy(), settings or {})
    if "MSI" not in m_ind.columns or "ADX" not in m_ind.columns:
        raise RuntimeError("MSI/ADX 계산 실패 — 일봉 컬럼을 확인하세요.")

    regime_full = classify_market_regime_series(
        m_ind["MSI"], m_ind["ADX"], vix_s, settings
    )
    regime_full.index = m_ind.index

    disp_idx = m_ind.index[(m_ind.index >= p0) & (m_ind.index <= p1)]
    close_disp = m_ind.loc[disp_idx, "Close"]
    msi_disp = m_ind.loc[disp_idx, "MSI"]
    regime_disp = regime_full.reindex(disp_idx).fillna("UP")
    msi_lo, msi_hi = msi_band_thresholds(settings)

    # 3행: 이중 Y축(overlay)은 환경에 따라 y2 범위가 어긋나 MSI가 -100~ 등으로 깨짐 → MSI 전용 행 분리
    fig = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        row_heights=[0.48, 0.30, 0.22],
        subplot_titles=("KOSPI (종가) · 레짐 밴드", "MSI", "시장 레짐 (4단계)"),
    )

    msi_y0, msi_y1 = _msi_display_y_range(msi_disp, msi_lo, msi_hi)
    msi_y_num = pd.to_numeric(msi_disp, errors="coerce")

    # 1행: 종가 + 밴드만
    fig.add_trace(
        go.Scatter(
            x=close_disp.index,
            y=close_disp.values,
            name="KOSPI 종가",
            line=dict(color="#ECEFF4", width=1.2),
            hovertemplate="%{x|%Y-%m-%d}<br>종가=%{y:,.2f}<extra></extra>",
        ),
        row=1,
        col=1,
    )

    segs = _contiguous_regime_segments(disp_idx, regime_disp)
    for x0, x1, reg in segs:
        c = _REGIME_COLORS.get(reg, "#4C566A")
        x1_ext = x1 + timedelta(days=1)
        fig.add_vrect(
            x0=x0,
            x1=x1_ext,
            fillcolor=c,
            opacity=0.22,
            layer="below",
            line_width=0,
            row=1,
            col=1,
        )

    # 2행: MSI 전용 Y축 (종가와 완전 분리)
    fig.add_trace(
        go.Scatter(
            x=msi_disp.index,
            y=msi_y_num,
            name="MSI",
            mode="lines",
            line=dict(color="#88C0D0", width=1.8),
            connectgaps=True,
            hovertemplate="%{x|%Y-%m-%d}<br>MSI=%{y:.4f}<extra></extra>",
        ),
        row=2,
        col=1,
    )
    try:
        fig.add_hline(
            y=msi_lo,
            line_dash="dash",
            line_color="#BF616A",
            line_width=1,
            opacity=0.65,
            annotation_text=f"msi_down ({msi_lo:g})",
            annotation_position="bottom left",
            row=2,
            col=1,
        )
        fig.add_hline(
            y=msi_hi,
            line_dash="dash",
            line_color="#A3BE8C",
            line_width=1,
            opacity=0.65,
            annotation_text=f"msi_up ({msi_hi:g})",
            annotation_position="top left",
            row=2,
            col=1,
        )
        fig.add_hline(
            y=0.0,
            line_dash="dot",
            line_color="#4C566A",
            line_width=1,
            opacity=0.5,
            row=2,
            col=1,
        )
    except Exception:
        pass

    # 3행: 레짐 단계 (0~3)
    y_map = {"DOWN": 0, "NEUTRAL": 1, "UP": 2, "AGGRESSIVE": 3}
    y_vals = [y_map.get(str(r), 2) for r in regime_disp]
    mcolors = [_REGIME_COLORS.get(str(r), "#888888") for r in regime_disp]
    fig.add_trace(
        go.Scatter(
            x=list(disp_idx),
            y=y_vals,
            mode="markers",
            name="레짐(일별)",
            marker=dict(size=7, color=mcolors, line=dict(width=0)),
            showlegend=False,
        ),
        row=3,
        col=1,
    )
    for name in ("DOWN", "NEUTRAL", "UP", "AGGRESSIVE"):
        fig.add_trace(
            go.Scatter(
                x=[None],
                y=[None],
                mode="markers",
                name=name,
                marker=dict(size=10, color=_REGIME_COLORS[name]),
                showlegend=True,
            ),
            row=3,
            col=1,
        )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#2E3440",
        plot_bgcolor="#3B4252",
        font=dict(color="#ECEFF4", size=11),
        title=dict(
            text=f"시장 레짐 4단계 밴드 ({plot_start} ~ {plot_end})",
            x=0.5,
        ),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=820,
        margin=dict(t=80, b=40),
    )
    fig.update_xaxes(showgrid=True, gridcolor="#4C566A")
    fig.update_yaxes(
        title_text="KOSPI (종가)",
        showgrid=True,
        gridcolor="#4C566A",
        row=1,
        col=1,
    )
    fig.update_yaxes(
        title_text="MSI",
        range=[msi_y0, msi_y1],
        rangemode="normal",
        constrain="range",
        showgrid=True,
        gridcolor="rgba(136, 192, 208, 0.22)",
        zeroline=True,
        zerolinecolor="#4C566A",
        tickformat=".3f",
        row=2,
        col=1,
    )
    fig.update_yaxes(
        tickmode="array",
        tickvals=[0, 1, 2, 3],
        ticktext=["DOWN", "NEUTRAL", "UP", "AGGRESSIVE"],
        row=3,
        col=1,
    )

    return fig


def main() -> None:
    parser = argparse.ArgumentParser(description="시장 레짐 4단계 밴드 차트 생성")
    parser.add_argument("--start", default="2024-04-01", help="표시 시작일 YYYY-MM-DD")
    parser.add_argument("--end", default="2026-03-31", help="표시 종료일 YYYY-MM-DD")
    parser.add_argument(
        "-o",
        "--output",
        default="",
        help="출력 HTML 경로 (기본: ./market_regime_bands_{start}_{end}.html)",
    )
    parser.add_argument(
        "--settings",
        default="",
        help="settings.json 경로 (기본: 프로젝트 루트 settings.json)",
    )
    args = parser.parse_args()

    settings_path = args.settings or os.path.join(_ROOT, "settings.json")
    settings = _load_settings(settings_path)

    out = args.output.strip()
    if not out:
        out = os.path.join(
            os.getcwd(),
            f"market_regime_bands_{args.start.replace('-', '')}_{args.end.replace('-', '')}.html",
        )

    try:
        fig = build_regime_chart(args.start, args.end, settings)
        fig.write_html(out, include_plotlyjs="cdn", config={"displayModeBar": True})
        print(f"저장 완료: {os.path.abspath(out)}")
    except Exception as e:
        print(f"오류: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
