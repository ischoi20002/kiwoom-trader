# -*- coding: utf-8 -*-
"""
선정 밖 S1 수익 자리 진단.

유니버스(K200) 전 종목을 슬롯 없이 종목별 SimpleBacktester 로 돌린 뒤,
각 S1(BUY_1ST→SELL)이 진입일 직전 영업일 선정에 들어 있었는지 분류한다.

버킷
----
- selected            : 그날 감시 슬롯(A∪B)에 포함
- valid_not_selected  : Valid_A/B 는 통과, 점수 상위 N에서 탈락
- not_valid           : 유니버스 안이지만 Valid 필터 탈락
- outside_universe    : 당일 유니버스 밖

사용 예
-------
  python tools/diagnose_sel_outside_s1.py
  python tools/diagnose_sel_outside_s1.py --start 2024-06-03 --end 2026-07-31
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from backtest_engine import SimpleBacktester, TradingStrategy, settings_for_backtest  # noqa: E402
from core.defaults import DEFAULT_SETTINGS  # noqa: E402
from data_manager_v2 import DataManagerV2  # noqa: E402

import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "diagnose_selection_forward_returns",
    str(ROOT / "tools" / "diagnose_selection_forward_returns.py"),
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("diagnose_selection_forward_returns.py 로드 실패")
_fwd = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_fwd)
_build_schedule_a_b = _fwd._build_schedule_a_b
_get_monthly_universe = _fwd._get_monthly_universe
load_scored_panel = _fwd.load_scored_panel

OUT_DIR = ROOT / "tools" / "bt_compare_out_sel_outside_s1"
WARMUP_CALENDAR_DAYS = 200
DEFAULT_START = "2025-07-01"
DEFAULT_END = "2026-07-31"


def _load_settings() -> Dict[str, Any]:
    """DEFAULT + settings.json 을 백테용으로 맞춘다."""
    s: Dict[str, Any] = dict(DEFAULT_SETTINGS)
    try:
        raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            s.update(raw)
    except Exception as e:
        print(f"[Warn] settings.json 로드 실패, DEFAULT 사용: {e}")
    try:
        st = settings_for_backtest(s)
    except Exception as e:
        print(f"[Warn] settings_for_backtest 실패: {e}")
        st = s
    st["universe_index_choice"] = "KOSPI200"
    st["kospi_top_n"] = 200
    st["bt_high_precision"] = False
    st["bt_rt_parity_intrabar"] = False
    # 종목별 기회 스캔 — 슬롯·자금은 수익률%에만 영향, 진입 여부에는 거의 무관
    st["backtest_initial_capital"] = 10_000_000
    st["s1_equity_pct"] = 44.0
    st["s2_equity_pct"] = 54.0
    st["s2_b_equity_pct"] = 54.0
    return st


def _norm_code(value: Any) -> str:
    """종목코드를 6자리로 맞춘다."""
    try:
        return str(value or "").replace(".0", "").strip().zfill(6)
    except Exception:
        return ""


def _day_str(ts: Any) -> str:
    """타임스탬프를 YYYY-MM-DD 로."""
    try:
        return pd.Timestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _ref_day(entry_day: str, trading_days: List[str]) -> str:
    """동적백테와 같이 진입일 직전 영업일을 선정 기준일로 쓴다."""
    try:
        if entry_day in trading_days:
            idx = trading_days.index(entry_day)
            if idx > 0:
                return trading_days[idx - 1]
        earlier = [d for d in trading_days if d < entry_day]
        if earlier:
            return earlier[-1]
    except Exception:
        pass
    return entry_day


def _pair_s1_rounds(trades: pd.DataFrame) -> pd.DataFrame:
    """BUY_1ST/BUY1 직후 같은 종목 첫 SELL 을 라운드로 묶는다."""
    if trades is None or trades.empty:
        return pd.DataFrame()
    df = trades.copy()
    try:
        df["Date"] = pd.to_datetime(df["Date"], format="mixed", errors="coerce")
        df["Code"] = df["Code"].map(_norm_code)
        df = df.dropna(subset=["Date"]).sort_values(["Code", "Date"])
    except Exception:
        return pd.DataFrame()
    rows: List[Dict[str, Any]] = []
    try:
        groups = df.groupby("Code", sort=False)
    except Exception:
        return pd.DataFrame()
    for code, g in groups:
        open_buy: Optional[pd.Series] = None
        for _, r in g.iterrows():
            try:
                act = str(r.get("Action", "") or "").upper()
            except Exception:
                act = ""
            if act in ("BUY_1ST", "BUY1"):
                open_buy = r
            elif act == "SELL" and open_buy is not None:
                try:
                    ret_pct = float(r.get("Return", np.nan))
                except Exception:
                    ret_pct = float("nan")
                try:
                    sig = str(open_buy.get("Sig_Type", "") or "")
                except Exception:
                    sig = ""
                rows.append(
                    {
                        "code": str(code),
                        "entry_dt": open_buy["Date"],
                        "exit_dt": r["Date"],
                        "sig_type": sig,
                        "ret_pct": ret_pct,
                    }
                )
                open_buy = None
    return pd.DataFrame(rows)


def _load_15m_batch(
    dm: DataManagerV2,
    codes: List[str],
    start: str,
    end: str,
) -> Dict[str, pd.DataFrame]:
    """유니버스 15분봉을 한 번에 읽는다."""
    if not codes:
        return {}
    try:
        pad_start = (pd.Timestamp(start) - timedelta(days=WARMUP_CALENDAR_DAYS)).strftime(
            "%Y-%m-%d"
        )
    except Exception:
        pad_start = start
    placeholders = ",".join(["?"] * len(codes))
    query = f"""
        SELECT code, datetime as time, open, high, low, close, volume
        FROM bars_15m
        WHERE code IN ({placeholders}) AND datetime BETWEEN ? AND ?
        ORDER BY code, datetime
    """
    try:
        with dm._get_connection() as conn:
            df_all = pd.read_sql_query(
                query, conn, params=list(codes) + [pad_start, f"{end} 23:59:59"]
            )
    except Exception as e:
        raise RuntimeError(f"bars_15m 일괄 조회 실패: {e}") from e
    if df_all is None or df_all.empty:
        return {}
    try:
        df_all.columns = [c.capitalize() if str(c).lower() != "time" else "time" for c in df_all.columns]
        df_all["time"] = pd.to_datetime(df_all["time"], errors="coerce")
        df_all = df_all.dropna(subset=["time"])
        df_all["Code"] = df_all["Code"].map(_norm_code)
        df_all.set_index("time", inplace=True)
        df_all.sort_index(inplace=True)
    except Exception as e:
        raise RuntimeError(f"15분봉 정규화 실패: {e}") from e
    out: Dict[str, pd.DataFrame] = {}
    try:
        for code, grp in df_all.groupby("Code", sort=False):
            try:
                g = grp.copy()
                if "Code" in g.columns:
                    g = g.drop(columns=["Code"])
                out[str(code)] = g
            except Exception:
                continue
    except Exception as e:
        raise RuntimeError(f"15분봉 종목 분할 실패: {e}") from e
    return out


def _simulate_code(
    settings: Dict[str, Any],
    code: str,
    df15: pd.DataFrame,
    start: str,
    end: str,
) -> pd.DataFrame:
    """한 종목 S1/S2/청산을 슬롯 없이 재현한다."""
    if df15 is None or df15.empty:
        return pd.DataFrame()
    try:
        df_ind = TradingStrategy.calculate_indicators(df15.copy(), settings)
    except Exception:
        return pd.DataFrame()
    if df_ind is None or df_ind.empty:
        return pd.DataFrame()
    try:
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        df_sim = df_ind.loc[(df_ind.index >= start_ts) & (df_ind.index <= end_ts)].copy()
    except Exception:
        return pd.DataFrame()
    if df_sim.empty:
        return pd.DataFrame()
    try:
        tester = SimpleBacktester(settings)
        tester.run(
            df_sim,
            code,
            market_df=None,
            vix_df=None,
            log_to_csv=False,
            skip_indicators=True,
            high_precision=False,
        )
    except Exception:
        return pd.DataFrame()
    raw = list(getattr(tester, "trades", None) or [])
    if not raw:
        return pd.DataFrame()
    try:
        tdf = pd.DataFrame(raw)
        tdf["Code"] = code
        return tdf
    except Exception:
        return pd.DataFrame()


def _bucket_of(
    code: str,
    ref: str,
    selected: Dict[str, set[str]],
    valid: Dict[str, set[str]],
    univ: Dict[str, set[str]],
) -> str:
    """선정/Valid/유니버스 소속으로 버킷을 정한다."""
    try:
        if code in selected.get(ref, set()):
            return "selected"
        if code in valid.get(ref, set()):
            return "valid_not_selected"
        if code in univ.get(ref, set()):
            return "not_valid"
    except Exception:
        pass
    return "outside_universe"


def _summarize(rounds: pd.DataFrame) -> Dict[str, Any]:
    """버킷별 건수·승률·수익 자리를 요약한다."""
    buckets = ["selected", "valid_not_selected", "not_valid", "outside_universe"]
    out: Dict[str, Any] = {"n_rounds": int(len(rounds)), "by_bucket": {}}
    if rounds.empty:
        return out
    try:
        pos = rounds[pd.to_numeric(rounds["ret_pct"], errors="coerce") > 0]
    except Exception:
        pos = pd.DataFrame()
    n_pos = int(len(pos))
    out["n_profitable"] = n_pos
    try:
        out["win_rate_pct"] = float((rounds["ret_pct"] > 0).mean() * 100.0) if len(rounds) else 0.0
        out["avg_ret_pct"] = float(rounds["ret_pct"].mean()) if len(rounds) else 0.0
        out["sum_pos_ret_pct"] = float(pos["ret_pct"].sum()) if n_pos else 0.0
    except Exception:
        out["win_rate_pct"] = 0.0
        out["avg_ret_pct"] = 0.0
        out["sum_pos_ret_pct"] = 0.0

    for b in buckets:
        sub = rounds[rounds["bucket"] == b] if "bucket" in rounds.columns else pd.DataFrame()
        sub_pos = pos[pos["bucket"] == b] if (n_pos and "bucket" in pos.columns) else pd.DataFrame()
        try:
            n = int(len(sub))
            np_ = int(len(sub_pos))
            out["by_bucket"][b] = {
                "n": n,
                "n_profitable": np_,
                "share_all_pct": (100.0 * n / len(rounds)) if len(rounds) else 0.0,
                "share_profitable_pct": (100.0 * np_ / n_pos) if n_pos else 0.0,
                "win_rate_pct": float((sub["ret_pct"] > 0).mean() * 100.0) if n else 0.0,
                "avg_ret_pct": float(sub["ret_pct"].mean()) if n else 0.0,
                "sum_pos_ret_pct": float(sub_pos["ret_pct"].sum()) if np_ else 0.0,
            }
        except Exception:
            out["by_bucket"][b] = {"n": 0, "n_profitable": 0}

    try:
        if n_pos:
            in_sel = int((pos["bucket"] == "selected").sum())
            out["coverage_profitable_pct"] = 100.0 * in_sel / n_pos
        else:
            out["coverage_profitable_pct"] = None
        if len(rounds):
            out["coverage_all_pct"] = 100.0 * float((rounds["bucket"] == "selected").mean())
        else:
            out["coverage_all_pct"] = None
        if out.get("sum_pos_ret_pct"):
            sel_pos = float(out["by_bucket"].get("selected", {}).get("sum_pos_ret_pct", 0.0) or 0.0)
            out["coverage_pos_ret_mass_pct"] = 100.0 * sel_pos / float(out["sum_pos_ret_pct"])
        else:
            out["coverage_pos_ret_mass_pct"] = None
    except Exception:
        out["coverage_profitable_pct"] = None
    return out


def _top_missed(rounds: pd.DataFrame, name_map: Dict[str, str], n: int = 15) -> List[Dict[str, Any]]:
    """선정 밖 수익 라운드를 종목별로 모아 상위 N을 만든다."""
    if rounds is None or rounds.empty or "bucket" not in rounds.columns:
        return []
    try:
        miss = rounds[
            (rounds["bucket"] != "selected")
            & (pd.to_numeric(rounds["ret_pct"], errors="coerce") > 0)
        ].copy()
    except Exception:
        return []
    if miss.empty:
        return []
    rows: List[Dict[str, Any]] = []
    try:
        for code, g in miss.groupby("code"):
            rows.append(
                {
                    "code": str(code),
                    "name": str(name_map.get(str(code), code) or code),
                    "n_profitable": int(len(g)),
                    "sum_ret_pct": float(g["ret_pct"].sum()),
                    "avg_ret_pct": float(g["ret_pct"].mean()),
                    "n_valid_not_selected": int((g["bucket"] == "valid_not_selected").sum()),
                    "n_not_valid": int((g["bucket"] == "not_valid").sum()),
                }
            )
        rows.sort(key=lambda x: float(x.get("sum_ret_pct", 0.0)), reverse=True)
        return rows[: max(1, int(n))]
    except Exception:
        return []


def run(start: str, end: str) -> Dict[str, Any]:
    """선정 패널 + 전 유니버스 종목시뮬을 돌려 요약을 만든다."""
    t0 = time.time()
    st = _load_settings()
    dm = DataManagerV2()

    try:
        panel_start = (pd.Timestamp(start) - timedelta(days=40)).strftime("%Y-%m-%d")
    except Exception:
        panel_start = start

    print(f"[1/4] 선정 패널 {panel_start} ~ {end}")
    df_panel, all_codes, duration_days = load_scored_panel(st, panel_start, end, dm)
    df_panel["code"] = df_panel["code"].map(_norm_code)
    df_panel["d"] = pd.to_datetime(df_panel["time"], errors="coerce").dt.strftime("%Y-%m-%d")
    all_codes = [_norm_code(c) for c in all_codes if _norm_code(c)]
    print(f"  유니버스 {len(all_codes)}종 / 패널 {len(df_panel)}행")

    univ_cache: Dict[str, frozenset[str]] = {}
    schedule_a, schedule_b = _build_schedule_a_b(
        df_panel, st, all_codes, duration_days, univ_cache
    )

    selected: Dict[str, set[str]] = {}
    valid: Dict[str, set[str]] = {}
    univ: Dict[str, set[str]] = {}
    trading_days: List[str] = []
    try:
        days = sorted({str(d) for d in df_panel["d"].dropna().unique() if str(d)})
    except Exception:
        days = []
    trading_days = days
    idx_choice = str(st.get("universe_index_choice", "KOSPI200")).strip().upper()
    top_n = int(st.get("kospi_top_n", 200))
    u_all = set(all_codes)

    for d_str in days:
        try:
            if duration_days < 180:
                u_set = set(u_all)
            else:
                u_set = set(
                    _get_monthly_universe(
                        pd.to_datetime(d_str),
                        univ_cache,
                        idx_choice,
                        kospi_top_n=top_n,
                    )
                )
        except Exception:
            u_set = set(u_all)
        univ[d_str] = {_norm_code(c) for c in u_set if _norm_code(c)}
        sa = {_norm_code(c) for c in (schedule_a.get(d_str, []) or []) if _norm_code(c)}
        sb = {_norm_code(c) for c in (schedule_b.get(d_str, []) or []) if _norm_code(c)}
        selected[d_str] = sa | sb
        try:
            day_mask = df_panel["d"] == d_str
            va = set()
            vb = set()
            if "Valid_A" in df_panel.columns:
                va = set(
                    df_panel.loc[day_mask & df_panel["Valid_A"].astype(bool), "code"].map(_norm_code)
                )
            if "Valid_B" in df_panel.columns:
                vb = set(
                    df_panel.loc[day_mask & df_panel["Valid_B"].astype(bool), "code"].map(_norm_code)
                )
            valid[d_str] = {c for c in (va | vb) if c}
        except Exception:
            valid[d_str] = set()

    n_sel = [len(selected.get(d, set())) for d in days if d >= start]
    n_valid = [len(valid.get(d, set())) for d in days if d >= start]
    print(
        f"  일평균 선정 {float(np.mean(n_sel)) if n_sel else 0:.1f}종 / "
        f"Valid {float(np.mean(n_valid)) if n_valid else 0:.1f}종"
    )

    print(f"[2/4] 15분봉 일괄 로드 ({len(all_codes)}종)")
    cached = _load_15m_batch(dm, all_codes, start, end)
    print(f"  로드 {len(cached)}종")

    print(f"[3/4] 종목별 S1 시뮬 {start} ~ {end}")
    trade_parts: List[pd.DataFrame] = []
    n_ok = 0
    n_fail = 0
    for i, code in enumerate(all_codes, start=1):
        df15 = cached.get(code)
        if df15 is None or df15.empty:
            n_fail += 1
            continue
        try:
            tdf = _simulate_code(st, code, df15, start, end)
        except Exception:
            n_fail += 1
            continue
        if tdf is not None and not tdf.empty:
            trade_parts.append(tdf)
            n_ok += 1
        if i % 20 == 0 or i == len(all_codes):
            print(f"  {i}/{len(all_codes)}  체결있는종 {n_ok}  스킵 {n_fail}", flush=True)

    if trade_parts:
        trades = pd.concat(trade_parts, ignore_index=True)
    else:
        trades = pd.DataFrame()
    rounds = _pair_s1_rounds(trades)
    print(f"  S1 라운드 {len(rounds)}건")

    if not rounds.empty:
        buckets: List[str] = []
        refs: List[str] = []
        for _, r in rounds.iterrows():
            try:
                entry_day = _day_str(r["entry_dt"])
                ref = _ref_day(entry_day, trading_days)
                code = _norm_code(r["code"])
                buckets.append(_bucket_of(code, ref, selected, valid, univ))
                refs.append(ref)
            except Exception:
                buckets.append("outside_universe")
                refs.append("")
        rounds["bucket"] = buckets
        rounds["ref_day"] = refs
        rounds["entry_day"] = rounds["entry_dt"].map(_day_str)

    try:
        name_map = TradingStrategy.get_stock_name_map("KOSPI200") or {}
    except Exception:
        name_map = {}
    if rounds is not None and not rounds.empty:
        try:
            rounds["name"] = rounds["code"].map(lambda c: name_map.get(str(c), str(c)))
        except Exception:
            pass

    summary = _summarize(rounds)
    summary["start"] = start
    summary["end"] = end
    summary["n_universe"] = int(len(all_codes))
    summary["n_codes_with_15m"] = int(len(cached))
    summary["n_codes_with_trades"] = int(n_ok)
    summary["elapsed_sec"] = round(time.time() - t0, 1)
    summary["avg_selected"] = float(np.mean(n_sel)) if n_sel else 0.0
    summary["avg_valid"] = float(np.mean(n_valid)) if n_valid else 0.0
    summary["top_missed"] = _top_missed(rounds, name_map)

    # 탈락 이유 비중 (수익 자리만)
    try:
        pos = rounds[rounds["ret_pct"] > 0] if not rounds.empty else pd.DataFrame()
        missed = pos[pos["bucket"] != "selected"] if not pos.empty else pd.DataFrame()
        if len(missed):
            summary["missed_reason_share_pct"] = {
                "valid_not_selected": float(
                    (missed["bucket"] == "valid_not_selected").mean() * 100.0
                ),
                "not_valid": float((missed["bucket"] == "not_valid").mean() * 100.0),
                "outside_universe": float(
                    (missed["bucket"] == "outside_universe").mean() * 100.0
                ),
            }
        else:
            summary["missed_reason_share_pct"] = {}
    except Exception:
        summary["missed_reason_share_pct"] = {}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if not rounds.empty:
            rounds.to_csv(OUT_DIR / "s1_rounds.csv", index=False, encoding="utf-8-sig")
        (OUT_DIR / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[Warn] 저장 실패: {e}")
    return summary


def _print_summary(s: Dict[str, Any]) -> None:
    """콘솔에 결론이 보이게 요약을 찍는다."""
    print("\n" + "=" * 72)
    print(f"선정 밖 S1 수익 자리  {s.get('start')} ~ {s.get('end')}")
    print("=" * 72)
    print(
        f"  유니버스 {s.get('n_universe')}종  /  "
        f"S1 라운드 {s.get('n_rounds')}건  /  "
        f"수익 자리 {s.get('n_profitable')}건  "
        f"(승률 {float(s.get('win_rate_pct') or 0):.1f}%)"
    )
    cov = s.get("coverage_profitable_pct")
    mass = s.get("coverage_pos_ret_mass_pct")
    if cov is not None:
        print(f"  수익 자리 중 선정에 들어 있던 비율  {float(cov):.1f}%")
    if mass is not None:
        print(f"  수익 합(양수 수익률 합) 중 선정 비중  {float(mass):.1f}%")
    print(
        f"  일평균 선정 {float(s.get('avg_selected') or 0):.1f}종  /  "
        f"Valid {float(s.get('avg_valid') or 0):.1f}종"
    )
    print("-" * 72)
    print(
        f"  {'버킷':<22s} {'전체':>6s} {'수익':>6s} {'수익점유%':>9s} {'승률%':>7s} {'평균%':>8s}"
    )
    labels = {
        "selected": "선정됨",
        "valid_not_selected": "Valid·슬롯탈락",
        "not_valid": "Valid 탈락",
        "outside_universe": "유니버스 밖",
    }
    for key, title in labels.items():
        b = (s.get("by_bucket") or {}).get(key) or {}
        print(
            f"  {title:<22s} "
            f"{int(b.get('n', 0)):6d} "
            f"{int(b.get('n_profitable', 0)):6d} "
            f"{float(b.get('share_profitable_pct', 0)):9.1f} "
            f"{float(b.get('win_rate_pct', 0)):7.1f} "
            f"{float(b.get('avg_ret_pct', 0)):+8.2f}"
        )
    missed = s.get("missed_reason_share_pct") or {}
    if missed:
        print("-" * 72)
        print("  선정 밖 수익 자리의 이유")
        print(
            f"    Valid인데 슬롯 탈락  {float(missed.get('valid_not_selected', 0)):.1f}%"
        )
        print(f"    Valid 필터 탈락      {float(missed.get('not_valid', 0)):.1f}%")
        print(f"    유니버스 밖          {float(missed.get('outside_universe', 0)):.1f}%")
    tops = s.get("top_missed") or []
    if tops:
        print("-" * 72)
        print("  선정 밖 수익 상위 종목")
        for row in tops[:10]:
            print(
                f"    {row.get('name')}({row.get('code')})  "
                f"n={row.get('n_profitable')}  "
                f"합 {float(row.get('sum_ret_pct') or 0):+.1f}%  "
                f"Valid탈락슬롯 {row.get('n_valid_not_selected')} / "
                f"필터탈락 {row.get('n_not_valid')}"
            )
    print(f"\n저장: {OUT_DIR}")
    print(f"소요 {s.get('elapsed_sec')}s")


def main() -> None:
    parser = argparse.ArgumentParser(description="선정 밖 S1 수익 자리 진단")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    args = parser.parse_args()
    try:
        summary = run(str(args.start), str(args.end))
        _print_summary(summary)
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
