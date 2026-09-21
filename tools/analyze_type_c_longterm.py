# -*- coding: utf-8 -*-
"""TYPE_C 장기 백테스트의 연도·월·청산 성과를 분석합니다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RESULT_DIR = ROOT / "tools" / "bt_type_c_long_20240603_20260918"
INITIAL_CASH = 10_000_000.0


def _exit_reason(reason: Any) -> str:
    """체결 방식 접두사와 무관하게 실제 TYPE_C 청산 원인을 반환합니다."""
    text = str(reason or "").upper()
    for value in (
        "BOX_C_TAKE_PROFIT",
        "BOX_C_GAP_DOWN",
        "BOX_C_BREAKDOWN",
        "BOX_C_TIME_EXIT",
        "BOX_C_STOP",
    ):
        if value in text:
            return value
    return text.split("|", 1)[0] or "UNKNOWN"


def main() -> None:
    """저장된 장기 백테스트 결과를 분석 JSON으로 변환합니다."""
    equity = pd.read_csv(RESULT_DIR / "equity.csv")
    trades = pd.read_csv(RESULT_DIR / "trades.csv")

    equity["date"] = pd.to_datetime(equity["date"], format="mixed", errors="coerce")
    equity["equity"] = pd.to_numeric(equity["equity"], errors="coerce")
    equity = equity.dropna(subset=["date", "equity"]).sort_values("date")
    equity["day"] = equity["date"].dt.normalize()
    daily = equity.groupby("day", as_index=False).last()

    trades["Date"] = pd.to_datetime(trades["Date"], format="mixed", errors="coerce")
    trades["Return"] = pd.to_numeric(trades["Return"], errors="coerce")
    trades["Fee"] = pd.to_numeric(trades["Fee"], errors="coerce").fillna(0.0)
    trades["Price"] = pd.to_numeric(trades["Price"], errors="coerce").fillna(0.0)
    trades["Qty"] = pd.to_numeric(trades["Qty"], errors="coerce").fillna(0.0)
    exits = trades[trades["Action"] == "SELL"].copy()
    exits["exit_reason"] = exits["Reason"].map(_exit_reason)

    yearly_rows: list[Dict[str, Any]] = []
    previous_equity = INITIAL_CASH
    for year, group in daily.groupby(daily["day"].dt.year, sort=True):
        end_equity = float(group.iloc[-1]["equity"])
        values = pd.concat(
            [pd.Series([previous_equity]), group["equity"].reset_index(drop=True)],
            ignore_index=True,
        )
        drawdown = values / values.cummax() - 1.0
        year_exits = exits[exits["Date"].dt.year == int(year)]
        yearly_rows.append(
            {
                "year": int(year),
                "return_pct": round((end_equity / previous_equity - 1.0) * 100.0, 3),
                "max_dd_pct": round(float(drawdown.min()) * 100.0, 3),
                "buy_count": int(
                    (
                        (trades["Action"] == "BUY1")
                        & (trades["Date"].dt.year == int(year))
                    ).sum()
                ),
                "exit_count": int(len(year_exits)),
                "win_rate_pct": round(
                    float((year_exits["Return"] > 0).mean() * 100.0)
                    if len(year_exits)
                    else 0.0,
                    2,
                ),
                "end_equity": round(end_equity, 0),
            }
        )
        previous_equity = end_equity

    month_end = daily.set_index("day")["equity"].resample("ME").last().dropna()
    month_base = pd.concat(
        [
            pd.Series(
                [INITIAL_CASH],
                index=[
                    pd.Timestamp(daily.iloc[0]["day"]) - pd.Timedelta(days=1)
                ],
            ),
            month_end,
        ]
    )
    monthly_returns = month_base.pct_change().dropna() * 100.0
    monthly_rows = [
        {"month": index.strftime("%Y-%m"), "return_pct": round(float(value), 3)}
        for index, value in monthly_returns.items()
    ]

    reason_rows: list[Dict[str, Any]] = []
    for reason, group in exits.groupby("exit_reason", sort=False):
        values = group["Return"].dropna()
        reason_rows.append(
            {
                "reason": str(reason),
                "count": int(len(group)),
                "win_rate_pct": round(float((values > 0).mean() * 100.0), 2),
                "avg_return_pct": round(float(values.mean()), 3),
                "sum_return_pct": round(float(values.sum()), 3),
            }
        )
    reason_rows.sort(key=lambda row: row["count"], reverse=True)

    equity_values = daily["equity"].reset_index(drop=True)
    running_peak = equity_values.cummax()
    drawdown = equity_values / running_peak - 1.0
    trough_position = int(drawdown.idxmin())
    peak_position = int(equity_values.iloc[: trough_position + 1].idxmax())
    trough_equity = float(equity_values.iloc[trough_position])
    recovery = daily.iloc[trough_position + 1 :]
    recovery = recovery[recovery["equity"] >= float(equity_values.iloc[peak_position])]

    returns = exits["Return"].dropna()
    positive_sum = float(returns[returns > 0].sum())
    negative_sum = abs(float(returns[returns < 0].sum()))
    active_months = {
        value.strftime("%Y-%m")
        for value in trades.loc[trades["Action"] == "BUY1", "Date"].dropna()
    }
    total_fees = float(trades["Fee"].sum())
    result = {
        "summary": {
            "calendar_months": int(len(monthly_rows)),
            "active_months": int(len(active_months)),
            "profitable_months": int(sum(row["return_pct"] > 0 for row in monthly_rows)),
            "losing_months": int(sum(row["return_pct"] < 0 for row in monthly_rows)),
            "flat_months": int(sum(row["return_pct"] == 0 for row in monthly_rows)),
            "monthly_positive_rate_pct": round(
                sum(row["return_pct"] > 0 for row in monthly_rows)
                / max(1, len(monthly_rows))
                * 100.0,
                2,
            ),
            "best_month": max(monthly_rows, key=lambda row: row["return_pct"]),
            "worst_month": min(monthly_rows, key=lambda row: row["return_pct"]),
            "sum_exit_return_pct": round(float(returns.sum()), 3),
            "return_profit_factor": round(
                positive_sum / negative_sum if negative_sum > 0 else np.inf, 3
            ),
            "total_fees_krw": round(total_fees, 0),
            "average_round_trip_fees_krw": round(total_fees / max(1, len(exits)), 0),
            "invested_day_rate_pct": round(
                float((daily["holdings_n"] > 0).mean() * 100.0), 2
            ),
        },
        "drawdown": {
            "peak_date": daily.iloc[peak_position]["day"].strftime("%Y-%m-%d"),
            "trough_date": daily.iloc[trough_position]["day"].strftime("%Y-%m-%d"),
            "max_dd_pct": round(float(drawdown.iloc[trough_position]) * 100.0, 3),
            "recovery_date": (
                recovery.iloc[0]["day"].strftime("%Y-%m-%d")
                if not recovery.empty
                else None
            ),
            "trough_equity": round(trough_equity, 0),
        },
        "yearly": yearly_rows,
        "monthly": monthly_rows,
        "exit_reasons": reason_rows,
    }
    (RESULT_DIR / "analysis.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
