# -*- coding: utf-8 -*-
"""2026년 A/B/C 통합 백테스트의 TYPE_C 직접·간접 기여를 분석합니다."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tools" / "bt_all_tracks_2026"


def main() -> None:
    """저장된 통합·기준선 결과로 TYPE_C 손익과 월별 차이를 계산합니다."""
    comparison = json.loads(
        (OUT / "comparison.json").read_text(encoding="utf-8")
    )
    trades = pd.read_csv(OUT / "ABC" / "trades.csv")
    trades["Date"] = pd.to_datetime(trades["Date"], format="mixed", errors="coerce")
    trades["Return"] = pd.to_numeric(trades["Return"], errors="coerce")
    trades["Amount"] = pd.to_numeric(trades["Amount"], errors="coerce").fillna(0.0)
    trades["Fee"] = pd.to_numeric(trades["Fee"], errors="coerce").fillna(0.0)

    type_c = trades[trades["Sig_Type"].astype(str).str.upper() == "TYPE_C"].copy()
    exits = type_c[type_c["Action"].isin(["SELL", "HOLD"])].copy()
    ratio = exits["Return"] / 100.0
    exits["gross_pnl_est"] = exits["Amount"] * ratio / (1.0 + ratio)
    exits["month"] = exits["Date"].dt.strftime("%Y-%m")
    c_gross = float(exits["gross_pnl_est"].sum())
    c_fees = float(type_c["Fee"].sum())
    c_net = c_gross - c_fees

    abc_monthly = comparison["ABC"]["monthly_returns"]
    ab_monthly = comparison["AB_BASELINE"]["monthly_returns"]
    monthly_rows: list[Dict[str, Any]] = []
    for month, abc_return in abc_monthly.items():
        ab_return = float(ab_monthly.get(month, 0.0))
        month_c = exits[exits["month"] == month]
        monthly_rows.append(
            {
                "month": month,
                "abc_return_pct": float(abc_return),
                "ab_return_pct": ab_return,
                "delta_pct_point": round(float(abc_return) - ab_return, 3),
                "type_c_exits": int(len(month_c)),
                "type_c_gross_pnl_est": round(
                    float(month_c["gross_pnl_est"].sum()), 0
                ),
            }
        )

    portfolio_delta = float(
        comparison["delta_abc_minus_ab"]["profit_krw"]
    )
    result = {
        "period": f"{comparison['ABC']['period']}",
        "type_c_direct": {
            "n_entries": int((type_c["Action"] == "BUY1").sum()),
            "n_exits": int(len(exits)),
            "gross_pnl_est": round(c_gross, 0),
            "fees_krw": round(c_fees, 0),
            "net_pnl_est": round(c_net, 0),
        },
        "portfolio_effect": {
            "abc_minus_ab_profit_krw": round(portfolio_delta, 0),
            "residual_path_and_opportunity_cost_krw": round(
                portfolio_delta - c_net, 0
            ),
            "return_delta_pct_point": comparison["delta_abc_minus_ab"][
                "return_pct"
            ],
            "mdd_delta_pct_point": comparison["delta_abc_minus_ab"][
                "max_dd_pct"
            ],
        },
        "monthly": monthly_rows,
    }
    (OUT / "analysis.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
