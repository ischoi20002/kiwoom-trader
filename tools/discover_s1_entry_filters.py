# -*- coding: utf-8 -*-
"""
S1 진입 시점 피처로 수익/손실을 가르는 추가 필터 후보를 유추한다.

- BUY1 → 동일 종목 다음 SELL 라운드트립
- trading_data.db bars_15m + calculate_all_indicators 로 진입봉 스냅샷
- Type A / Type B 분리, 시간 기준 IS/OOS 임계 스캔

사용 예:
  python tools/discover_s1_entry_filters.py
  python tools/discover_s1_entry_filters.py --trades tools/bt_compare_out_hard_suppress_to_off/BASE_full/trades.csv
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from data_manager_v2 import DataManagerV2  # noqa: E402
from strategy.indicators import calculate_all_indicators  # noqa: E402

OUT_DIR = ROOT / "tools" / "s1_filter_discovery"
DEFAULT_TRADES = ROOT / "tools" / "bt_compare_out_hard_suppress_to_off" / "BASE_full" / "trades.csv"
MARKET_CODE = "069500"  # KODEX 200
WARMUP_CAL_DAYS = 280
IS_FRAC = 0.70
MIN_IS_KEEP = 25
MIN_OOS_KEEP = 20
MIN_OOS_N = 30
TOP_IS_PER_TYPE = 40
CANDIDATE_MAX = 15

# 해석 가능한 연속 피처 (이미 하드게이트로 쓰는 조건과 중복해도 여유 임계 탐색)
FEATURE_SPECS: List[Tuple[str, str]] = [
    ("rsi", "RSI"),
    ("rsi_sig_gap", "RSI - RSI_Sig"),
    ("osc", "OSC"),
    ("osc_delta", "OSC - Prev_OSC"),
    ("adx", "ADX"),
    ("msi", "MSI"),
    ("close_ma5", "Close/MA5"),
    ("close_ma20", "Close/MA20"),
    ("close_ma60", "Close/MA60"),
    ("ma5_ma20", "MA5/MA20"),
    ("ma20_ma60", "MA20/MA60"),
    ("vol_ratio", "Vol_Ratio"),
    ("vol_vs_prev", "Volume/PrevDayVol"),
    ("atr_pct", "ATR/Close%"),
    ("bar_of_day", "장중봉순번(0=시가)"),
    ("dow", "요일(0=월)"),
    ("mkt_ret_1d", "지수 전일종가대비%"),
    ("mkt_ret_intraday", "지수 당일시가대비%"),
    ("stock_open_gap", "종목 당일시가대비%"),
]


def _load_settings() -> Dict[str, Any]:
    try:
        return json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[WARN] settings.json 로드 실패: {e}")
        return {}


def _norm_code(v: Any) -> str:
    s = str(v).replace(".0", "").strip()
    return s.zfill(6) if s.isdigit() else s


def _exit_tag(reason: str) -> str:
    r = str(reason or "").split("|", 1)[0].strip().upper()
    for key in (
        "TAKE_PROFIT",
        "ATR_SL",
        "ATR_TRAIL",
        "FLASH_EXIT",
        "GAP_DOWN",
        "STOP_LOSS",
        "SMART_EXIT",
    ):
        if key in r:
            return key
    return r[:32] if r else "UNKNOWN"


def pair_s1_rounds(trades: pd.DataFrame) -> pd.DataFrame:
    """BUY1 직후 같은 종목 첫 SELL을 라운드로 묶음."""
    df = trades.copy()
    df["Date"] = pd.to_datetime(df["Date"], format="mixed", errors="coerce")
    df["Code"] = df["Code"].map(_norm_code)
    df = df.dropna(subset=["Date"]).sort_values(["Code", "Date"])
    rows: List[Dict[str, Any]] = []
    for code, g in df.groupby("Code", sort=False):
        open_buy: Optional[pd.Series] = None
        for _, r in g.iterrows():
            act = str(r.get("Action", ""))
            if act == "BUY1":
                open_buy = r
            elif act == "SELL" and open_buy is not None:
                try:
                    ret = float(r.get("Return", np.nan))
                except Exception:
                    ret = float("nan")
                try:
                    hold_h = (r["Date"] - open_buy["Date"]).total_seconds() / 3600.0
                except Exception:
                    hold_h = float("nan")
                sig = str(open_buy.get("Sig_Type", "") or "").upper()
                rows.append(
                    {
                        "code": code,
                        "name": str(open_buy.get("Name", "")),
                        "entry_dt": open_buy["Date"],
                        "exit_dt": r["Date"],
                        "sig_type": sig,
                        "entry_price": float(open_buy.get("Price", np.nan)),
                        "exit_price": float(r.get("Price", np.nan)),
                        "ret_pct": ret,
                        "win": 1 if (np.isfinite(ret) and ret > 0) else 0,
                        "exit_tag": _exit_tag(str(r.get("Reason", ""))),
                        "hold_hours": hold_h,
                    }
                )
                open_buy = None
            elif act == "BUY2":
                continue
    return pd.DataFrame(rows)


def _prep_ohlcv(raw: pd.DataFrame) -> pd.DataFrame:
    """DataManager 봉 → 지표 계산용 OHLCV."""
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = raw.copy()
    # 컬럼 정규화
    rename = {}
    for c in df.columns:
        cl = str(c).lower()
        if cl in ("time", "datetime", "date"):
            rename[c] = "time"
        elif cl == "open":
            rename[c] = "Open"
        elif cl == "high":
            rename[c] = "High"
        elif cl == "low":
            rename[c] = "Low"
        elif cl == "close":
            rename[c] = "Close"
        elif cl == "volume":
            rename[c] = "Volume"
    df = df.rename(columns=rename)
    if "time" not in df.columns or "Close" not in df.columns:
        return pd.DataFrame()
    df["time"] = pd.to_datetime(df["time"], format="mixed", errors="coerce")
    df = df.dropna(subset=["time"]).sort_values("time").drop_duplicates("time", keep="last")
    df = df.set_index("time")
    for col in ("Open", "High", "Low", "Close", "Volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _nearest_row(ind: pd.DataFrame, ts: pd.Timestamp) -> Optional[pd.Series]:
    """진입 시각과 일치(또는 직전) 봉."""
    if ind is None or ind.empty:
        return None
    try:
        ts = pd.Timestamp(ts)
        if ts in ind.index:
            return ind.loc[ts]
        # 동일 분 반올림 매칭
        idx = ind.index
        # 직전 봉 (미래 누수 방지)
        pos = idx.searchsorted(ts, side="right") - 1
        if pos < 0:
            return None
        row = ind.iloc[pos]
        # 너무 멀면(>1봉=20분) 스킵
        delta_m = abs((idx[pos] - ts).total_seconds()) / 60.0
        if delta_m > 20:
            return None
        return row
    except Exception:
        return None


def _safe_div(a: Any, b: Any) -> float:
    try:
        a_f, b_f = float(a), float(b)
        if not np.isfinite(a_f) or not np.isfinite(b_f) or abs(b_f) < 1e-12:
            return float("nan")
        return a_f / b_f
    except Exception:
        return float("nan")


def _feat_from_row(
    row: pd.Series,
    entry_dt: pd.Timestamp,
    sig_type: str,
    day_open: Optional[float],
    mkt_row: Optional[pd.Series],
    mkt_day_open: Optional[float],
    mkt_prev_close: Optional[float],
) -> Dict[str, float]:
    out: Dict[str, float] = {}
    try:
        close = float(row.get("Close", np.nan))
    except Exception:
        close = float("nan")

    def g(key: str) -> float:
        try:
            v = float(row.get(key, np.nan))
            return v if np.isfinite(v) else float("nan")
        except Exception:
            return float("nan")

    out["rsi"] = g("RSI")
    out["rsi_sig_gap"] = out["rsi"] - g("RSI_Sig") if np.isfinite(out["rsi"]) else float("nan")
    out["osc"] = g("OSC")
    prev_osc = g("Prev_OSC")
    out["osc_delta"] = out["osc"] - prev_osc if np.isfinite(out["osc"]) and np.isfinite(prev_osc) else float("nan")
    out["adx"] = g("ADX")
    out["msi"] = g("MSI")
    out["close_ma5"] = _safe_div(close, g("MA5"))
    out["close_ma20"] = _safe_div(close, g("MA20"))
    out["close_ma60"] = _safe_div(close, g("MA60"))
    out["ma5_ma20"] = _safe_div(g("MA5"), g("MA20"))
    out["ma20_ma60"] = _safe_div(g("MA20"), g("MA60"))
    out["vol_ratio"] = g("Vol_Ratio")
    prev_key = "Prev_Day_Vol_S1_A" if "TYPE_A" in sig_type else "Prev_Day_Vol_S1_B"
    prev_vol = g(prev_key)
    if not np.isfinite(prev_vol):
        prev_vol = g("Prev_Day_Vol") if "TYPE_A" in sig_type else g("Prev_Day_Vol_B")
    out["vol_vs_prev"] = _safe_div(g("Volume"), prev_vol)
    atr = g("ATR")
    out["atr_pct"] = (atr / close * 100.0) if np.isfinite(atr) and np.isfinite(close) and close > 0 else float("nan")

    try:
        # 정규장 09:00 기준 15분봉 순번
        mins = entry_dt.hour * 60 + entry_dt.minute
        out["bar_of_day"] = float((mins - 9 * 60) // 15)
    except Exception:
        out["bar_of_day"] = float("nan")
    try:
        out["dow"] = float(entry_dt.dayofweek)
    except Exception:
        out["dow"] = float("nan")

    if day_open is not None and np.isfinite(close) and day_open > 0:
        out["stock_open_gap"] = (close / day_open - 1.0) * 100.0
    else:
        out["stock_open_gap"] = float("nan")

    if mkt_prev_close is not None and mkt_row is not None:
        try:
            mc = float(mkt_row.get("Close", np.nan))
            out["mkt_ret_1d"] = (mc / mkt_prev_close - 1.0) * 100.0 if mkt_prev_close > 0 else float("nan")
        except Exception:
            out["mkt_ret_1d"] = float("nan")
    else:
        out["mkt_ret_1d"] = float("nan")

    if mkt_day_open is not None and mkt_row is not None:
        try:
            mc = float(mkt_row.get("Close", np.nan))
            out["mkt_ret_intraday"] = (mc / mkt_day_open - 1.0) * 100.0 if mkt_day_open > 0 else float("nan")
        except Exception:
            out["mkt_ret_intraday"] = float("nan")
    else:
        out["mkt_ret_intraday"] = float("nan")

    return out


def build_feature_snapshot(
    rounds: pd.DataFrame,
    settings: Dict[str, Any],
) -> pd.DataFrame:
    """라운드별 진입봉 피처 스냅샷."""
    dm = DataManagerV2()
    codes = sorted(rounds["code"].astype(str).unique().tolist())
    print(f"[피처] 종목 {len(codes)}개, 라운드 {len(rounds)}건 지표 조인…")

    # 시장 지수 봉
    t_min = rounds["entry_dt"].min() - pd.Timedelta(days=WARMUP_CAL_DAYS)
    t_max = rounds["entry_dt"].max() + pd.Timedelta(days=2)
    try:
        mkt_raw = dm.get_bars(
            MARKET_CODE,
            start_dt=str(t_min.date()),
            end_dt=str(t_max.date()),
            interval="15m",
        )
        mkt = _prep_ohlcv(mkt_raw)
        if not mkt.empty:
            mkt = calculate_all_indicators(mkt, settings)
            print(f"  시장({MARKET_CODE}) 봉 {len(mkt)}개")
        else:
            print(f"  [WARN] 시장 봉 없음: {MARKET_CODE}")
    except Exception as e:
        print(f"  [WARN] 시장 봉 로드 실패: {e}")
        mkt = pd.DataFrame()

    # 종목별 캐시
    ind_cache: Dict[str, pd.DataFrame] = {}
    for i, code in enumerate(codes, 1):
        try:
            sub = rounds[rounds["code"] == code]
            c_min = sub["entry_dt"].min() - pd.Timedelta(days=WARMUP_CAL_DAYS)
            c_max = sub["entry_dt"].max() + pd.Timedelta(days=2)
            raw = dm.get_bars(
                code,
                start_dt=str(c_min.date()),
                end_dt=str(c_max.date()),
                interval="15m",
            )
            ohlcv = _prep_ohlcv(raw)
            if ohlcv.empty or len(ohlcv) < 80:
                ind_cache[code] = pd.DataFrame()
                continue
            ind_cache[code] = calculate_all_indicators(ohlcv, settings)
        except Exception as e:
            print(f"  [WARN] {code} 지표 실패: {e}")
            ind_cache[code] = pd.DataFrame()
        if i % 20 == 0 or i == len(codes):
            print(f"  … {i}/{len(codes)}")

    feat_rows: List[Dict[str, Any]] = []
    miss = 0
    for _, r in rounds.iterrows():
        code = str(r["code"])
        ind = ind_cache.get(code, pd.DataFrame())
        entry_dt = pd.Timestamp(r["entry_dt"])
        row = _nearest_row(ind, entry_dt)
        if row is None:
            miss += 1
            continue

        # 당일 시가
        day_open: Optional[float] = None
        try:
            day = entry_dt.normalize()
            day_bars = ind[(ind.index >= day) & (ind.index < day + pd.Timedelta(days=1))]
            if not day_bars.empty:
                day_open = float(day_bars.iloc[0]["Open"])
        except Exception:
            day_open = None

        mkt_row = _nearest_row(mkt, entry_dt) if not mkt.empty else None
        mkt_day_open: Optional[float] = None
        mkt_prev_close: Optional[float] = None
        try:
            if not mkt.empty:
                day = entry_dt.normalize()
                mkt_day = mkt[(mkt.index >= day) & (mkt.index < day + pd.Timedelta(days=1))]
                if not mkt_day.empty:
                    mkt_day_open = float(mkt_day.iloc[0]["Open"])
                prev = mkt[mkt.index < day]
                if not prev.empty:
                    mkt_prev_close = float(prev.iloc[-1]["Close"])
        except Exception:
            pass

        feats = _feat_from_row(
            row,
            entry_dt,
            str(r["sig_type"]),
            day_open,
            mkt_row,
            mkt_day_open,
            mkt_prev_close,
        )
        base = r.to_dict()
        base.update(feats)
        feat_rows.append(base)

    print(f"  조인 성공 {len(feat_rows)} / 실패 {miss}")
    return pd.DataFrame(feat_rows)


def _split_is_oos(df: pd.DataFrame, frac: float = IS_FRAC) -> Tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    d = df.sort_values("entry_dt").reset_index(drop=True)
    if len(d) < 40:
        cut = d["entry_dt"].iloc[-1]
        return d, d.iloc[0:0].copy(), cut
    cut_i = int(len(d) * frac)
    cut_i = max(1, min(len(d) - 1, cut_i))
    cut = d.iloc[cut_i]["entry_dt"]
    is_df = d[d["entry_dt"] < cut].copy()
    oos_df = d[d["entry_dt"] >= cut].copy()
    return is_df, oos_df, pd.Timestamp(cut)


def _metrics(df: pd.DataFrame) -> Dict[str, float]:
    if df is None or df.empty:
        return {"n": 0, "win_rate": float("nan"), "avg_ret": float("nan"), "med_ret": float("nan"), "exp": float("nan")}
    ret = pd.to_numeric(df["ret_pct"], errors="coerce")
    n = int(ret.notna().sum())
    if n == 0:
        return {"n": 0, "win_rate": float("nan"), "avg_ret": float("nan"), "med_ret": float("nan"), "exp": float("nan")}
    wins = (ret > 0).sum()
    avg = float(ret.mean())
    return {
        "n": n,
        "win_rate": float(wins / n * 100.0),
        "avg_ret": avg,
        "med_ret": float(ret.median()),
        "exp": avg,  # 동일 가중 기대값
    }


def _scan_feature(
    is_df: pd.DataFrame,
    oos_df: pd.DataFrame,
    feat: str,
    label: str,
    sig_type: str,
) -> List[Dict[str, Any]]:
    """분위수 임계 × (>= / <=) 스캔."""
    results: List[Dict[str, Any]] = []
    if feat not in is_df.columns:
        return results
    base_is = _metrics(is_df)
    base_oos = _metrics(oos_df)
    if base_is["n"] < MIN_IS_KEEP or base_oos["n"] < MIN_OOS_N:
        return results

    vals = pd.to_numeric(is_df[feat], errors="coerce").dropna()
    if len(vals) < MIN_IS_KEEP:
        return results
    # 고유값 적으면 스킵
    if vals.nunique() < 8:
        qs = sorted(vals.unique().tolist())
    else:
        qs = sorted(set(float(x) for x in vals.quantile([0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7, 0.75, 0.8, 0.85]).tolist()))

    for thr in qs:
        if not np.isfinite(thr):
            continue
        for direction in (">=", "<="):
            if direction == ">=":
                keep_is = is_df[pd.to_numeric(is_df[feat], errors="coerce") >= thr]
                keep_oos = oos_df[pd.to_numeric(oos_df[feat], errors="coerce") >= thr]
                rule = f"{feat} >= {thr:.4g}"
            else:
                keep_is = is_df[pd.to_numeric(is_df[feat], errors="coerce") <= thr]
                keep_oos = oos_df[pd.to_numeric(oos_df[feat], errors="coerce") <= thr]
                rule = f"{feat} <= {thr:.4g}"

            m_is = _metrics(keep_is)
            m_oos = _metrics(keep_oos)
            if m_is["n"] < MIN_IS_KEEP or m_oos["n"] < MIN_OOS_KEEP:
                continue
            # 과도한 희생(IS에서 거래 30% 미만만 남김) 제외
            if m_is["n"] < base_is["n"] * 0.30:
                continue

            d_avg_is = m_is["avg_ret"] - base_is["avg_ret"]
            d_wr_is = m_is["win_rate"] - base_is["win_rate"]
            d_avg_oos = m_oos["avg_ret"] - base_oos["avg_ret"]
            d_wr_oos = m_oos["win_rate"] - base_oos["win_rate"]

            # IS에서 평균수익 또는 승률 개선 필요
            if d_avg_is <= 0 and d_wr_is <= 0:
                continue

            # 점수: IS 평균수익 개선 위주 + 승률 보조, 표본 페널티
            score = d_avg_is + 0.02 * d_wr_is
            results.append(
                {
                    "sig_type": sig_type,
                    "feature": feat,
                    "label": label,
                    "rule": rule,
                    "direction": direction,
                    "threshold": float(thr),
                    "score_is": float(score),
                    "is_n": m_is["n"],
                    "is_keep_pct": float(m_is["n"] / base_is["n"] * 100.0),
                    "is_avg_ret": m_is["avg_ret"],
                    "is_win_rate": m_is["win_rate"],
                    "is_d_avg": float(d_avg_is),
                    "is_d_wr": float(d_wr_is),
                    "oos_n": m_oos["n"],
                    "oos_keep_pct": float(m_oos["n"] / base_oos["n"] * 100.0) if base_oos["n"] else float("nan"),
                    "oos_avg_ret": m_oos["avg_ret"],
                    "oos_win_rate": m_oos["win_rate"],
                    "oos_d_avg": float(d_avg_oos),
                    "oos_d_wr": float(d_wr_oos),
                    "base_is_avg": base_is["avg_ret"],
                    "base_oos_avg": base_oos["avg_ret"],
                    "base_is_wr": base_is["win_rate"],
                    "base_oos_wr": base_oos["win_rate"],
                    "oos_ok": bool(d_avg_oos > 0 or d_wr_oos > 0.5),
                }
            )
    return results


def discover_candidates(feat_df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Type별 스캔 → OOS 통과 후보."""
    all_rows: List[Dict[str, Any]] = []
    summary: Dict[str, Any] = {"by_type": {}}

    for sig in ("TYPE_A", "TYPE_B"):
        sub = feat_df[feat_df["sig_type"].astype(str).str.upper() == sig].copy()
        is_df, oos_df, cut = _split_is_oos(sub)
        base_is = _metrics(is_df)
        base_oos = _metrics(oos_df)
        summary["by_type"][sig] = {
            "n_total": int(len(sub)),
            "cut": str(cut),
            "is": base_is,
            "oos": base_oos,
        }
        print(
            f"\n[{sig}] n={len(sub)} | IS<{cut.date()} n={base_is['n']} "
            f"avg={base_is['avg_ret']:.2f}% wr={base_is['win_rate']:.1f}% | "
            f"OOS n={base_oos['n']} avg={base_oos['avg_ret']:.2f}% wr={base_oos['win_rate']:.1f}%"
        )
        type_hits: List[Dict[str, Any]] = []
        for feat, label in FEATURE_SPECS:
            type_hits.extend(_scan_feature(is_df, oos_df, feat, label, sig))

        if not type_hits:
            print(f"  IS 후보 없음")
            continue
        hit_df = pd.DataFrame(type_hits).sort_values("score_is", ascending=False)
        # 피처당 최고 1개만 IS top에 남김
        hit_df = hit_df.drop_duplicates(subset=["feature", "direction"], keep="first")
        top = hit_df.head(TOP_IS_PER_TYPE)
        oos_pass = top[top["oos_ok"]].copy()
        # OOS 평균수익 개선 우선 정렬
        oos_pass = oos_pass.sort_values(["oos_d_avg", "oos_d_wr"], ascending=False).head(CANDIDATE_MAX)
        print(f"  IS 스캔 {len(type_hits)} → 피처중복제거 {len(hit_df)} → OOS통과 {len(oos_pass)}")
        all_rows.extend(oos_pass.to_dict(orient="records"))
        summary["by_type"][sig]["oos_pass_n"] = int(len(oos_pass))

    return pd.DataFrame(all_rows), summary


def _write_report(
    candidates: pd.DataFrame,
    summary: Dict[str, Any],
    trades_path: Path,
    feat_n: int,
    out_dir: Path,
) -> Path:
    lines: List[str] = []
    lines.append("# S1 진입 필터 후보 리포트")
    lines.append("")
    lines.append(f"- trades: `{trades_path.as_posix()}`")
    lines.append(f"- feature rows: {feat_n}")
    lines.append(f"- IS fraction: {IS_FRAC:.0%}, OOS min n: {MIN_OOS_N}")
    lines.append("")
    for sig, info in summary.get("by_type", {}).items():
        lines.append(f"## {sig}")
        lines.append(
            f"- total={info.get('n_total')} cut={info.get('cut')} | "
            f"IS avg={info['is']['avg_ret']:.2f}% wr={info['is']['win_rate']:.1f}% "
            f"(n={info['is']['n']}) | "
            f"OOS avg={info['oos']['avg_ret']:.2f}% wr={info['oos']['win_rate']:.1f}% "
            f"(n={info['oos']['n']})"
        )
        lines.append("")

    if candidates is None or candidates.empty:
        lines.append("## 후보")
        lines.append("OOS까지 통과한 해석 가능 필터가 없습니다.")
    else:
        lines.append("## OOS 통과 후보 (추가 조건 후보)")
        lines.append("")
        lines.append(
            "| Type | Rule | 의미 | IS keep% | IS Δavg | IS Δwr | OOS keep% | OOS Δavg | OOS Δwr | OOS n |"
        )
        lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for _, r in candidates.iterrows():
            lines.append(
                f"| {r['sig_type']} | `{r['rule']}` | {r['label']} | "
                f"{r['is_keep_pct']:.0f}% | {r['is_d_avg']:+.2f}%p | {r['is_d_wr']:+.1f}%p | "
                f"{r['oos_keep_pct']:.0f}% | {r['oos_d_avg']:+.2f}%p | {r['oos_d_wr']:+.1f}%p | "
                f"{int(r['oos_n'])} |"
            )
        lines.append("")
        lines.append("### 권장 해석 (운영 반영 전)")
        lines.append("")
        # Type별 상위 3
        for sig in ("TYPE_A", "TYPE_B"):
            sub = candidates[candidates["sig_type"] == sig].head(3)
            if sub.empty:
                lines.append(f"- **{sig}**: OOS 통과 후보 없음")
                continue
            lines.append(f"- **{sig}**")
            for _, r in sub.iterrows():
                lines.append(
                    f"  - `{r['rule']}` ({r['label']}): "
                    f"OOS 평균수익 {r['oos_d_avg']:+.2f}%p / 승률 {r['oos_d_wr']:+.1f}%p "
                    f"(잔여 거래 {r['oos_keep_pct']:.0f}%, n={int(r['oos_n'])})"
                )
        lines.append("")
        lines.append(
            "주의: 단일 임계 스캔이므로 과적합 여지가 있습니다. "
            "실제 반영 시 settings 토글로 넣고 BASE vs NEW 전체 BT를 한 번 더 비교하세요."
        )

    path = out_dir / "report.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="S1 수익/손실 구별 필터 후보 유추")
    ap.add_argument(
        "--trades",
        type=str,
        default=str(DEFAULT_TRADES),
        help="백테스트 trades.csv 경로",
    )
    ap.add_argument(
        "--out",
        type=str,
        default=str(OUT_DIR),
        help="출력 디렉터리",
    )
    args = ap.parse_args()
    trades_path = Path(args.trades)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not trades_path.is_file():
        # 폴백: 루트 backtest_trades.csv
        alt = ROOT / "backtest_trades.csv"
        if alt.is_file():
            print(f"[WARN] {trades_path} 없음 → {alt} 사용")
            trades_path = alt
        else:
            print(f"[ERR] trades 파일 없음: {trades_path}")
            return 1

    print(f"[로드] {trades_path}")
    trades = pd.read_csv(trades_path)
    rounds = pair_s1_rounds(trades)
    print(
        f"  S1 라운드 {len(rounds)} | "
        f"A={(rounds.sig_type.str.upper()=='TYPE_A').sum()} "
        f"B={(rounds.sig_type.str.upper()=='TYPE_B').sum()} | "
        f"승률={(rounds.win.mean()*100):.1f}% 평균Ret={rounds.ret_pct.mean():.2f}%"
    )
    rounds.to_csv(out_dir / "s1_rounds.csv", index=False, encoding="utf-8-sig")

    settings = _load_settings()
    try:
        feat_df = build_feature_snapshot(rounds, settings)
    except Exception:
        traceback.print_exc()
        return 1

    if feat_df.empty:
        print("[ERR] 피처 스냅샷 비어 있음")
        return 1
    feat_df.to_csv(out_dir / "features_snapshot.csv", index=False, encoding="utf-8-sig")

    # 피처별 단순 상관(참고)
    corr_rows = []
    for feat, label in FEATURE_SPECS:
        if feat not in feat_df.columns:
            continue
        for sig in ("TYPE_A", "TYPE_B"):
            sub = feat_df[feat_df["sig_type"].str.upper() == sig]
            d = sub[[feat, "ret_pct"]].replace([np.inf, -np.inf], np.nan).dropna()
            if len(d) < 30:
                continue
            try:
                spearman = float(d[feat].rank().corr(d["ret_pct"].rank()))
            except Exception:
                spearman = float("nan")
            corr_rows.append({"sig_type": sig, "feature": feat, "label": label, "spearman": spearman, "n": len(d)})
    corr_df = pd.DataFrame(corr_rows)
    if not corr_df.empty:
        corr_df["abs_sp"] = corr_df["spearman"].abs()
        corr_df = corr_df.sort_values(["sig_type", "abs_sp"], ascending=[True, False]).drop(columns=["abs_sp"])
        corr_df.to_csv(out_dir / "feature_corr.csv", index=False, encoding="utf-8-sig")
        print("\n[참고] |Spearman| 상위 (Type별)")
        for sig in ("TYPE_A", "TYPE_B"):
            top = corr_df[corr_df["sig_type"] == sig].head(5)
            for _, r in top.iterrows():
                print(f"  {sig} {r['feature']}: ρ={r['spearman']:+.3f} (n={int(r['n'])})")

    candidates, summary = discover_candidates(feat_df)
    if not candidates.empty:
        candidates.to_csv(out_dir / "candidates_oos.csv", index=False, encoding="utf-8-sig")
    (out_dir / "candidates_oos.json").write_text(
        json.dumps(
            {
                "summary": summary,
                "candidates": candidates.to_dict(orient="records") if not candidates.empty else [],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    report = _write_report(candidates, summary, trades_path, len(feat_df), out_dir)
    print(f"\n[완료] 리포트: {report}")
    if not candidates.empty:
        print("\n=== OOS 통과 Top ===")
        for _, r in candidates.head(12).iterrows():
            print(
                f"  {r['sig_type']} | {r['rule']} | "
                f"OOS Δavg={r['oos_d_avg']:+.2f}%p Δwr={r['oos_d_wr']:+.1f}%p "
                f"keep={r['oos_keep_pct']:.0f}% n={int(r['oos_n'])}"
            )
    else:
        print("\nOOS 통과 후보 없음 — 임계/표본 조건을 완화하거나 다른 trades로 재실행하세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
