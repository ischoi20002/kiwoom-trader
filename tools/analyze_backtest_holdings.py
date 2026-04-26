# -*- coding: utf-8 -*-
"""
백테스트 체결 CSV(`backtest_trades.csv`)에서 라운드트립별 보유기간·당일 청산 비율·청산 사유를 집계한다.

사용 예:
  python tools/analyze_backtest_holdings.py
  python tools/analyze_backtest_holdings.py --csv D:\\data\\my_trades.csv --out hold_summary.txt

주의: `Date`는 시각 포함일 수 있다. 당일매매는 **진입일(날짜) = 청산일(날짜)** 로 판단한다.
      거래일 수는 `pandas.bdate_range`(한국 공휴일 미반영) 근사치다.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

_ABS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_CSV = os.path.join(_ABS_ROOT, "backtest_trades.csv")


def _default_trade_csv() -> str:
    return _DEFAULT_CSV


def load_trades_csv(path: str) -> pd.DataFrame:
    """백테 엔진과 동일한 UTF-8 BOM CSV 를 읽는다."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"파일 없음: {path}")
    try:
        df = pd.read_csv(path, dtype={"Code": str}, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df = pd.read_csv(path, dtype={"Code": str}, encoding="cp949")
    if df.empty:
        raise ValueError("CSV 가 비었습니다.")
    if "Date" not in df.columns or "Action" not in df.columns:
        raise ValueError("필수 컬럼 Date, Action 이 없습니다.")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    df["Code"] = df["Code"].astype(str).str.zfill(6)
    df["Action"] = df["Action"].astype(str).str.strip().str.upper()
    return df.sort_values(["Code", "Date"]).reset_index(drop=True)


def _is_buy_action(act: str) -> bool:
    """CSV·엔진에 따라 `BUY`, `BUY1`, `BUY_1ST`, `BUY_2ND` 등 변형 허용."""
    try:
        u = str(act).strip().upper().replace("_", "")
    except Exception:
        return False
    return u.startswith("BUY")


def extract_round_trips(df: pd.DataFrame) -> pd.DataFrame:
    """
    종목별 시간순으로 매수 액션으로 포지션을 연 뒤, 첫 매수 시각부터
    SELL 또는 HOLD(말일 정리) 시점까지를 한 라운드로 본다.
    """
    records: List[Dict[str, Any]] = []

    for code, g in df.groupby("Code", sort=False):
        entry_dt: Optional[pd.Timestamp] = None
        entry_action: Optional[str] = None
        entry_sig: Any = None
        for _, r in g.iterrows():
            act = str(r["Action"]).upper()
            try:
                ts = pd.Timestamp(r["Date"])
            except Exception:
                continue

            if _is_buy_action(act):
                if entry_dt is None:
                    entry_dt = ts
                    entry_action = act
                    try:
                        entry_sig = r.get("Sig_Type")
                    except Exception:
                        entry_sig = None
                continue

            if act in ("SELL", "HOLD"):
                if entry_dt is None:
                    continue
                exit_dt = ts
                try:
                    cal_days = int(
                        (exit_dt.normalize() - entry_dt.normalize()) / pd.Timedelta(days=1)
                    )
                except Exception:
                    cal_days = 0
                same_day = exit_dt.date() == entry_dt.date()
                try:
                    bdays = int(
                        len(
                            pd.bdate_range(
                                entry_dt.date(),
                                exit_dt.date(),
                                inclusive="both",
                            )
                        )
                    )
                except Exception:
                    bdays = max(1, cal_days + 1)

                try:
                    ret_v = float(r.get("Return", 0) or 0)
                except (TypeError, ValueError):
                    ret_v = 0.0
                reason_full = str(r.get("Reason", "") or "")
                reason_key = reason_full.split("|")[0].strip()[:120]

                records.append(
                    {
                        "Code": code,
                        "Name": r.get("Name", ""),
                        "entry_dt": entry_dt,
                        "exit_dt": exit_dt,
                        "entry_action": entry_action,
                        "exit_action": act,
                        "cal_days": cal_days,
                        "bdays_approx": bdays,
                        "same_day": bool(same_day),
                        "Return_pct": ret_v,
                        "Reason": reason_full[:500],
                        "Reason_key": reason_key,
                        "Sig_Type": r.get("Sig_Type", entry_sig),
                    }
                )
                entry_dt = None
                entry_action = None
                entry_sig = None

    return pd.DataFrame.from_records(records)


def summarize(rt: pd.DataFrame) -> Tuple[str, pd.DataFrame, pd.DataFrame]:
    """콘솔용 한국어 요약 문자열, 사유 집계 DF, 보유일 버킷 DF."""
    n = len(rt)
    if n == 0:
        return "(라운드트립 없음)", pd.DataFrame(), pd.DataFrame()

    sd = int(rt["same_day"].sum())
    sd_pct = 100.0 * sd / n

    lines: List[str] = []
    lines.append(f"라운드트립 수: {n}")
    lines.append(f"당일 청산(진입일=청산일, 캘린더): {sd} ({sd_pct:.1f}%)")
    lines.append("")
    lines.append("보유 캘린더 일수(cal_days): 요약")
    lines.append(str(rt["cal_days"].describe(percentiles=[0.25, 0.5, 0.75, 0.9])))
    lines.append("")
    lines.append("보유 영업일 근사(bdays_approx, 공휴일 미제외): 요약")
    lines.append(str(rt["bdays_approx"].describe(percentiles=[0.25, 0.5, 0.75, 0.9])))
    lines.append("")

    # 버킷
    def _bucket(c: int) -> str:
        if c <= 0:
            return "0일(당일~익일 미만)"
        if c == 1:
            return "1일"
        if c <= 4:
            return "2~4일"
        if c < 10:
            return "5~9일"
        return "10일 이상"

    rt = rt.copy()
    rt["cal_bucket"] = rt["cal_days"].map(_bucket)
    buck = (
        rt.groupby("cal_bucket", dropna=False)
        .agg(n=("Code", "count"), mean_ret=("Return_pct", "mean"))
        .reset_index()
    )

    lines.append("보유(캘린더 일수) 버킷별 건수·평균 수익률(%):")
    for _, row in buck.iterrows():
        lines.append(
            f"  {row['cal_bucket']}: n={int(row['n'])}, 평균수익 {row['mean_ret']:+.3f}%"
        )
    lines.append("")

    reason_df = (
        rt.groupby("Reason_key", dropna=False)
        .agg(
            n=("Code", "count"),
            mean_ret=("Return_pct", "mean"),
            win_rate=("Return_pct", lambda s: float((s > 0).mean() * 100) if len(s) else 0.0),
        )
        .reset_index()
        .sort_values("n", ascending=False)
    )

    lines.append("청산 사유(Reason 첫 세그먼트) 상위:")
    for i, row in reason_df.head(20).iterrows():
        lines.append(
            f"  [{int(row['n']):5d}] {row['Reason_key'][:80]!r} "
            f"| 평균 {row['mean_ret']:+.3f}% | 승률 {row['win_rate']:.1f}%"
        )

    return "\n".join(lines), reason_df, buck


def main() -> None:
    p = argparse.ArgumentParser(description="백테 체결 CSV 보유기간·당일청산·사유 분석")
    p.add_argument("--csv", default=None, help="backtest_trades.csv 경로 (기본: 프로젝트 루트)")
    p.add_argument("--out", default=None, help="요약 텍스트 저장 경로(UTF-8)")
    p.add_argument("--top-reasons", type=int, default=25, help="사유 상위 N행 CSV (--reasons-csv 와 함께)")
    p.add_argument(
        "--reasons-csv",
        default=None,
        help="사유 집계 테이블 CSV 저장 경로",
    )
    p.add_argument(
        "--trips-csv",
        default=None,
        help="라운드트립 행 단위 상세 CSV (미지정 시 저장 안 함)",
    )
    args = p.parse_args()
    path = args.csv or _default_trade_csv()

    try:
        df = load_trades_csv(path)
    except Exception as e:
        print(f"[오류] {e}", file=sys.stderr)
        sys.exit(1)

    rt = extract_round_trips(df)
    text, reason_df, buck_df = summarize(rt)

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print(text)

    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(text)
                f.write("\n")
            print(f"\n[저장] {args.out}")
        except Exception as e:
            print(f"[경고] 요약 파일 저장 실패: {e}", file=sys.stderr)

    if args.reasons_csv and not reason_df.empty:
        try:
            topn = max(1, int(args.top_reasons))
            reason_df.head(topn).to_csv(
                args.reasons_csv, index=False, encoding="utf-8-sig"
            )
            print(f"[저장] {args.reasons_csv} (상위 {topn}행)")
        except Exception as e:
            print(f"[경고] 사유 CSV 저장 실패: {e}", file=sys.stderr)

    if args.trips_csv and not rt.empty:
        try:
            out_cols = [
                "Code",
                "Name",
                "entry_dt",
                "exit_dt",
                "cal_days",
                "bdays_approx",
                "same_day",
                "Return_pct",
                "Reason_key",
                "exit_action",
            ]
            use = [c for c in out_cols if c in rt.columns]
            rt[use].to_csv(args.trips_csv, index=False, encoding="utf-8-sig")
            print(f"[저장] {args.trips_csv} (라운드트립 상세)")
        except Exception as e:
            print(f"[경고] 라운드트립 상세 CSV 저장 실패: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
