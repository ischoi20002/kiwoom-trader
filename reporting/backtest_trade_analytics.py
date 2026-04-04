# -*- coding: utf-8 -*-
"""
backtest_trades.csv 후처리: 종목별·월별 손익, 매수~매도 보유기간(FIFO 매칭).
실행: python -m reporting.backtest_trade_analytics [CSV경로]
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional

import pandas as pd


@dataclass
class Lot:
    """잔여 수량·미배분 매수 수수료(FIFO)"""

    dt: pd.Timestamp
    qty_left: int
    price: float
    fee_left: float
    name: str
    sig_type: str


def _pnl_gross_krw(sell_price: float, qty: int, return_pct: float) -> float:
    """SELL 행의 Return(%)과 체결가 기준 근사 실현손익(원). Return = (매도/평단 - 1)*100 가정."""
    if qty <= 0 or return_pct == -100.0:
        return 0.0
    return float(sell_price) * float(qty) * float(return_pct) / (100.0 + float(return_pct))


def analyze_trades_csv(csv_path: str) -> Dict[str, Any]:
    try:
        df = pd.read_csv(csv_path, encoding="utf-8-sig")
    except Exception as e:
        raise RuntimeError(f"CSV 로드 실패: {e}") from e

    if df.empty:
        return {"error": "빈 파일"}

    df = df.sort_values("Date").reset_index(drop=True)
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])

    lots: Dict[str, Deque[Lot]] = defaultdict(deque)
    # 각 SELL(청산 건) 단위 기록
    closes: List[Dict[str, Any]] = []

    for _, row in df.iterrows():
        try:
            code = str(int(float(row["Code"]))).zfill(6)
        except (TypeError, ValueError):
            code = str(row["Code"]).strip().zfill(6)
        act = str(row.get("Action", "")).strip()
        dt = row["Date"]

        if act in ("BUY1", "BUY2"):
            try:
                q = int(float(row["Qty"]))
            except (TypeError, ValueError):
                continue
            if q <= 0:
                continue
            price = float(row["Price"]) if pd.notna(row["Price"]) else 0.0
            fee = float(row["Fee"]) if pd.notna(row["Fee"]) else 0.0
            name = str(row.get("Name", ""))
            sig = str(row.get("Sig_Type", ""))
            lots[code].append(Lot(dt=dt, qty_left=q, price=price, fee_left=fee, name=name, sig_type=sig))

        elif act == "SELL":
            try:
                q_rem = int(float(row["Qty"]))
            except (TypeError, ValueError):
                continue
            if q_rem <= 0:
                continue
            sell_price = float(row["Price"]) if pd.notna(row["Price"]) else 0.0
            ret_pct = float(row["Return"]) if pd.notna(row["Return"]) else 0.0
            sell_fee = float(row["Fee"]) if pd.notna(row["Fee"]) else 0.0
            name = str(row.get("Name", ""))
            sig = str(row.get("Sig_Type", ""))

            gross = _pnl_gross_krw(sell_price, q_rem, ret_pct)
            buy_fee_alloc = 0.0
            first_buy_dt: Optional[pd.Timestamp] = None

            while q_rem > 0 and lots[code]:
                lot = lots[code][0]
                take = min(q_rem, lot.qty_left)
                if lot.qty_left > 0 and take > 0:
                    frac = take / float(lot.qty_left)
                    buy_fee_alloc += lot.fee_left * frac
                    lot.fee_left -= lot.fee_left * frac
                if first_buy_dt is None:
                    first_buy_dt = lot.dt
                lot.qty_left -= take
                q_rem -= take
                if lot.qty_left <= 0:
                    lots[code].popleft()

            net = gross - sell_fee - buy_fee_alloc
            hold_days: Optional[int] = None
            if first_buy_dt is not None:
                try:
                    hold_days = int((dt.normalize() - first_buy_dt.normalize()).days)
                except Exception:
                    hold_days = None

            closes.append(
                {
                    "code": code,
                    "name": name,
                    "sig_type": sig,
                    "sell_dt": dt,
                    "hold_days": hold_days,
                    "return_pct": ret_pct,
                    "pnl_gross_krw": gross,
                    "pnl_net_krw": net,
                    "sell_qty": int(row["Qty"]),
                }
            )

    if not closes:
        return {"error": "SELL 체결이 없습니다.", "n_buys": len(df[df["Action"].astype(str).str.startswith("BUY")])}

    cdf = pd.DataFrame(closes)

    # 종목별
    by_code = (
        cdf.groupby(["code", "name"], as_index=False)
        .agg(
            청산건수=("sell_dt", "count"),
            합계손익원=("pnl_net_krw", "sum"),
            평균수익률pct=("return_pct", "mean"),
            중앙보유일=("hold_days", "median"),
            평균보유일=("hold_days", "mean"),
        )
        .sort_values("합계손익원", ascending=False)
    )

    # 월별
    cdf["ym"] = cdf["sell_dt"].dt.to_period("M").astype(str)
    by_month = (
        cdf.groupby("ym", as_index=False)
        .agg(청산건수=("sell_dt", "count"), 합계손익원=("pnl_net_krw", "sum"), 평균수익률pct=("return_pct", "mean"))
        .sort_values("ym")
    )

    # 보유기간 전체 분포
    hd = cdf["hold_days"].dropna()
    hold_summary = {
        "건수": int(len(hd)),
        "중앙값_일": float(hd.median()) if len(hd) else None,
        "평균_일": float(hd.mean()) if len(hd) else None,
        "최소_일": int(hd.min()) if len(hd) else None,
        "최대_일": int(hd.max()) if len(hd) else None,
    }

    return {
        "csv_path": csv_path,
        "n_closes": len(cdf),
        "by_code": by_code,
        "by_month": by_month,
        "hold_summary": hold_summary,
        "total_net_pnl_krw": float(cdf["pnl_net_krw"].sum()),
    }


def _print_report(result: Dict[str, Any]) -> None:
    if "error" in result:
        print(result.get("error"))
        return
    print("=" * 60)
    print(f"파일: {result['csv_path']}")
    print(f"청산 건수(SELL 매칭): {result['n_closes']}")
    print(f"추정 합계 순손익(원, 수수료 반영 근사): {result['total_net_pnl_krw']:,.0f}")
    print()
    h = result["hold_summary"]
    print("[보유기간 매수→매도, FIFO 기준]")
    print(f"  표본: {h['건수']}건 | 중앙값 {h['중앙값_일']:.1f}일 | 평균 {h['평균_일']:.1f}일 | 범위 {h['최소_일']}~{h['최대_일']}일")
    print()
    print("[월별]")
    print(result["by_month"].to_string(index=False))
    print()
    print("[종목별 상위 25 (합계 손익 원)]")
    print(result["by_code"].head(25).to_string(index=False))
    print()
    print("[종목별 하위 15 (합계 손익 원)]")
    print(result["by_code"].tail(15).to_string(index=False))


def _configure_stdout_utf8() -> None:
    """Windows 콘솔에서 한글 출력 깨짐 완화."""
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def main() -> None:
    _configure_stdout_utf8()
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_csv = os.path.join(base, "backtest_trades.csv")
    path = sys.argv[1] if len(sys.argv) > 1 else default_csv
    if not os.path.isfile(path):
        print(f"파일 없음: {path}")
        sys.exit(1)
    try:
        r = analyze_trades_csv(path)
        _print_report(r)
        out_csv = os.path.join(base, "backtest_analysis_by_code.csv")
        out_m = os.path.join(base, "backtest_analysis_by_month.csv")
        if "by_code" in r:
            r["by_code"].to_csv(out_csv, index=False, encoding="utf-8-sig")
            print(f"\n저장: {out_csv}")
        if "by_month" in r:
            r["by_month"].to_csv(out_m, index=False, encoding="utf-8-sig")
            print(f"저장: {out_m}")
    except Exception as e:
        print(f"분석 실패: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
