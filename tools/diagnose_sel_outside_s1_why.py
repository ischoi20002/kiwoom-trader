# -*- coding: utf-8 -*-
"""
선정 밖 S1 수익 자리 — 왜 빠졌는지 3축 분해.

1) 종목 반복(이름 이력)
2) 테마/업종 몰림
3) 시초 갭·익일 시초 청산
(+ Valid 필터 탈락 사유)

입력: tools/bt_compare_out_sel_outside_s1/s1_rounds.csv
  python tools/diagnose_sel_outside_s1_why.py
"""
from __future__ import annotations

import json
import os
import sys
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

from backtest_engine import settings_for_backtest  # noqa: E402
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
load_scored_panel = _fwd.load_scored_panel

IN_DIR = ROOT / "tools" / "bt_compare_out_sel_outside_s1"
OUT_DIR = IN_DIR
ROUNDS_PATH = IN_DIR / "s1_rounds.csv"
THEMES_PATH = ROOT / "naver_themes.json"

# 테마 키워드 → 굵은 업종
_SECTOR_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("반도체", ("반도체", "팹리스", "시스템반도체", "메모리", "파운드리", "웨이퍼")),
    ("2차전지/전기차", ("2차전지", "전기차", "배터리", "양극재", "음극재", "전해액")),
    ("에너지", ("태양광", "풍력", "수소", "연료전지", "SOFC", "ESS", "원전", "원자력", "전력")),
    ("방산/항공", ("방위", "방산", "우주항공", "누리호", "항공")),
    ("조선/해운", ("조선", "해운", "조선기자재")),
    ("자동차", ("자동차", "타이어", "현대차")),
    ("건설/중공업", ("건설", "중공업", "건설기계", "시멘트")),
    ("유통/소비", ("백화점", "유통", "쇼핑", "음식료", "화장품", "의류")),
    ("바이오/제약", ("바이오", "제약", "의료")),
    ("금융", ("은행", "보험", "증권", "금융")),
    ("소재/화학", ("화학", "철강", "석유", "정유", "비철")),
    ("IT부품", ("MLCC", "콘덴서", "회로", "PCB", "카메라", "디스플레이", "OLED")),
]


def _load_settings() -> Dict[str, Any]:
    """DEFAULT + settings.json."""
    s: Dict[str, Any] = dict(DEFAULT_SETTINGS)
    try:
        raw = json.loads((ROOT / "settings.json").read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            s.update(raw)
    except Exception:
        pass
    try:
        st = settings_for_backtest(s)
    except Exception:
        st = s
    st["universe_index_choice"] = "KOSPI200"
    st["kospi_top_n"] = 200
    return st


def _norm_code(value: Any) -> str:
    try:
        return str(value or "").replace(".0", "").strip().zfill(6)
    except Exception:
        return ""


def _f(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        if np.isnan(x) or np.isinf(x):
            return default
        return x
    except Exception:
        return default


def _load_themes() -> Dict[str, List[str]]:
    """종목코드 → 네이버 테마 목록."""
    try:
        raw = json.loads(THEMES_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[Warn] naver_themes.json 로드 실패: {e}")
        return {}
    out: Dict[str, List[str]] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        code = _norm_code(k)
        if not code:
            continue
        themes: List[str] = []
        if isinstance(v, list):
            themes = [str(t) for t in v if t]
        elif isinstance(v, str):
            themes = [v]
        out[code] = themes
    return out


def _coarse_sector(themes: List[str], name: str) -> str:
    """테마·종목명으로 굵은 업종 하나."""
    blob = " ".join(themes) + " " + str(name or "")
    for sector, keys in _SECTOR_RULES:
        if any(k in blob for k in keys):
            return sector
    if "홀딩스" in str(name) or "지주" in blob:
        return "지주"
    return "기타"


def _gap_bin(gap: float) -> str:
    """시초 갭 구간."""
    if gap <= -2.0:
        return "갭다운≤-2%"
    if gap < -0.3:
        return "갭다운 -2~-0.3"
    if gap <= 0.3:
        return "플랫"
    if gap < 2.0:
        return "갭업 0.3~2"
    return "갭업≥2%"


def _name_stats(df: pd.DataFrame) -> pd.DataFrame:
    """종목별 라운드 통계."""
    if df is None or df.empty:
        return pd.DataFrame()
    rows: List[Dict[str, Any]] = []
    try:
        for code, g in df.groupby("code"):
            rets = pd.to_numeric(g["ret_pct"], errors="coerce")
            pos = rets[rets > 0]
            rows.append(
                {
                    "code": str(code),
                    "name": str(g["name"].iloc[0]) if "name" in g.columns else str(code),
                    "n": int(len(g)),
                    "n_win": int(len(pos)),
                    "win_rate": float((rets > 0).mean() * 100.0) if len(g) else 0.0,
                    "avg_ret": float(rets.mean()) if len(g) else 0.0,
                    "sum_pos": float(pos.sum()) if len(pos) else 0.0,
                    "sum_all": float(rets.sum()) if len(g) else 0.0,
                }
            )
    except Exception:
        return pd.DataFrame()
    out = pd.DataFrame(rows)
    if out.empty:
        return out
    return out.sort_values("sum_pos", ascending=False)


def _share_top(stats: pd.DataFrame, col: str, ns: List[int]) -> Dict[str, float]:
    """상위 N 종목이 col 합에서 차지하는 비중."""
    out: Dict[str, float] = {}
    if stats is None or stats.empty or col not in stats.columns:
        return out
    total = float(stats[col].sum())
    if total <= 0:
        return {f"top{n}_pct": 0.0 for n in ns}
    s = stats.sort_values(col, ascending=False)
    for n in ns:
        out[f"top{n}_pct"] = float(s[col].head(n).sum() / total * 100.0)
    return out


def _load_daily(dm: DataManagerV2, codes: List[str], start: str, end: str) -> pd.DataFrame:
    """일봉 OHLCV."""
    if not codes:
        return pd.DataFrame()
    try:
        pad = (pd.Timestamp(start) - timedelta(days=20)).strftime("%Y-%m-%d")
    except Exception:
        pad = start
    ph = ",".join(["?"] * len(codes))
    q = f"""
        SELECT code, datetime as time, open, high, low, close
        FROM bars_1d
        WHERE code IN ({ph}) AND datetime BETWEEN ? AND ?
        ORDER BY code, time
    """
    try:
        with dm._get_connection() as conn:
            df = pd.read_sql_query(q, conn, params=list(codes) + [pad, end])
    except Exception as e:
        print(f"[Warn] bars_1d 조회 실패: {e}")
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    try:
        df["code"] = df["code"].map(_norm_code)
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df["d"] = df["time"].dt.strftime("%Y-%m-%d")
        for c in ("open", "high", "low", "close"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["time", "close"])
        df = df.sort_values(["code", "time"])
        df["prev_close"] = df.groupby("code")["close"].shift(1)
        df["gap_pct"] = (df["open"] / df["prev_close"] - 1.0) * 100.0
        return df
    except Exception as e:
        print(f"[Warn] 일봉 정규화 실패: {e}")
        return pd.DataFrame()


def _attach_gap(rounds: pd.DataFrame, daily: pd.DataFrame) -> pd.DataFrame:
    """진입일 시초 갭·익일 시초 청산 여부를 붙인다."""
    out = rounds.copy()
    if daily is None or daily.empty:
        out["entry_gap_pct"] = np.nan
        out["gap_bin"] = "unknown"
        out["next_open_exit"] = False
        return out
    try:
        key = daily[["code", "d", "gap_pct", "open", "close"]].rename(
            columns={"d": "entry_day", "gap_pct": "entry_gap_pct", "open": "day_open", "close": "day_close"}
        )
        out = out.merge(key, on=["code", "entry_day"], how="left")
    except Exception:
        out["entry_gap_pct"] = np.nan
    try:
        out["gap_bin"] = out["entry_gap_pct"].map(
            lambda x: _gap_bin(float(x)) if pd.notna(x) else "unknown"
        )
    except Exception:
        out["gap_bin"] = "unknown"
    try:
        et = pd.to_datetime(out["entry_dt"], errors="coerce")
        xt = pd.to_datetime(out["exit_dt"], errors="coerce")
        out["hold_nights"] = (xt.dt.normalize() - et.dt.normalize()).dt.days
        out["exit_hhmm"] = xt.dt.strftime("%H:%M")
        out["entry_hhmm"] = et.dt.strftime("%H:%M")
        out["next_open_exit"] = (
            (out["hold_nights"] >= 1)
            & (out["exit_hhmm"].isin(["09:00", "09:15", "09:30"]))
        )
        out["open_entry"] = out["entry_hhmm"].isin(["09:00", "09:15"])
    except Exception:
        out["next_open_exit"] = False
        out["open_entry"] = False
    return out


def _valid_fail_flags(row: pd.Series, st: Dict[str, Any]) -> Dict[str, bool]:
    """ref_day 패널 한 행에서 A/B 공통·트랙별 탈락 플래그."""
    flags: Dict[str, bool] = {}
    try:
        rsi = _f(row.get("RSI"), 50.0)
        osc = _f(row.get("OSC"), 0.0)
        avg5 = _f(row.get("AvgVal5"), 0.0)
        flags["liq_a"] = avg5 < float(st.get("min_trde_val", 30.0)) * 1e8
        flags["liq_b"] = avg5 < float(st.get("sel_b_min_trde_val", 55.0)) * 1e8
        flags["osc_pos"] = osc <= 0
        flags["rsi_a"] = not (
            float(st.get("sel_rsi_min", 30.0)) <= rsi <= float(st.get("sel_rsi_max", 70.0))
        )
        flags["rsi_b"] = not (
            float(st.get("sel_b_rsi_min", 45.0)) <= rsi <= float(st.get("sel_b_rsi_max", 80.0))
        )
        flags["burst_b"] = not bool(row.get("VolBurstActive_B", False))
        flags["ma_ok_a"] = not bool(row.get("_sel_ma_ok", False))
        flags["trend_b"] = not bool(row.get("_sel_b_trend_ok", False))
        flags["slope_b"] = not bool(row.get("_sel_b_slope_ok", False))

        rs_a = _f(row.get("_sel_rs_pct"), np.nan)
        flags["rs_a"] = not (
            float(st.get("sel_a_rs_rank_min", 0.35))
            <= rs_a
            <= float(st.get("sel_a_rs_rank_max", 0.8))
        ) if pd.notna(row.get("_sel_rs_pct")) else True
        pb = _f(row.get("_sel_pb_pct"), np.nan)
        flags["pb_a"] = not (
            float(st.get("sel_a_pb_min_pct", 5.0))
            <= pb
            <= float(st.get("sel_a_pb_max_pct", 16.0))
        ) if pd.notna(row.get("_sel_pb_pct")) else True
        rs_b = _f(row.get("_sel_b_rs_pct"), np.nan)
        flags["rs_b"] = not (
            float(st.get("sel_b_rs_rank_min", 0.45)) <= rs_b <= float(st.get("sel_b_rs_rank_max", 1.0))
        ) if pd.notna(row.get("_sel_b_rs_pct")) else True
        hi = _f(row.get("_sel_b_hi_pct"), np.nan)
        flags["hi_b"] = not (
            float(st.get("sel_b_hi_min_pct", 92.0))
            <= hi
            <= float(st.get("sel_b_hi_max_pct", 99.7))
        ) if pd.notna(row.get("_sel_b_hi_pct")) else True
        gap_a = abs(_f(row.get("_sel_gap_pct"), 0.0))
        flags["gap_a"] = gap_a > float(st.get("sel_a_gap_abs_max_pct", 6.0))
        gap_b = abs(_f(row.get("_sel_b_gap_pct"), 0.0))
        flags["gap_b"] = gap_b > float(st.get("sel_b_gap_abs_max_pct", 6.0))
    except Exception:
        pass
    return flags


def _fail_table(
    joined: pd.DataFrame,
    st: Dict[str, Any],
    mask: pd.Series,
) -> List[Dict[str, Any]]:
    """마스크된 행의 Valid 탈락 사유 빈도."""
    labels = {
        "rsi_a": "A RSI밴드",
        "pb_a": "A 눌림폭",
        "rs_a": "A RS분위",
        "ma_ok_a": "A 이평위치",
        "liq_a": "A 대금",
        "rsi_b": "B RSI밴드",
        "hi_b": "B 고점근접",
        "rs_b": "B RS분위",
        "trend_b": "B 정배열",
        "slope_b": "B MA20기울기",
        "burst_b": "B 거래량분출",
        "liq_b": "B 대금",
        "osc_pos": "OSC≤0",
        "gap_a": "A 갭과다",
        "gap_b": "B 갭과다",
    }
    sub = joined.loc[mask] if mask is not None else joined
    if sub is None or sub.empty:
        return []
    counts: Dict[str, int] = {k: 0 for k in labels}
    n = 0
    for _, row in sub.iterrows():
        flags = _valid_fail_flags(row, st)
        if not flags:
            continue
        n += 1
        for k in labels:
            if flags.get(k):
                counts[k] += 1
    if n <= 0:
        return []
    rows = []
    for k, title in labels.items():
        rows.append(
            {
                "key": k,
                "title": title,
                "n": counts[k],
                "pct": 100.0 * counts[k] / n,
            }
        )
    rows.sort(key=lambda x: x["pct"], reverse=True)
    return rows


def _bin_table(df: pd.DataFrame, col: str) -> List[Dict[str, Any]]:
    """범주 열의 건수·승률·평균."""
    if df is None or df.empty or col not in df.columns:
        return []
    rows: List[Dict[str, Any]] = []
    try:
        for key, g in df.groupby(col, dropna=False):
            rets = pd.to_numeric(g["ret_pct"], errors="coerce")
            rows.append(
                {
                    "bin": str(key),
                    "n": int(len(g)),
                    "n_win": int((rets > 0).sum()),
                    "win_rate": float((rets > 0).mean() * 100.0) if len(g) else 0.0,
                    "avg_ret": float(rets.mean()) if len(g) else 0.0,
                    "sum_pos": float(rets[rets > 0].sum()) if (rets > 0).any() else 0.0,
                }
            )
    except Exception:
        return []
    rows.sort(key=lambda x: x["n"], reverse=True)
    return rows


def run() -> Dict[str, Any]:
    """3축 분해를 돌리고 JSON으로 남긴다."""
    if not ROUNDS_PATH.is_file():
        raise RuntimeError(f"라운드 파일이 없습니다: {ROUNDS_PATH}")
    rounds = pd.read_csv(ROUNDS_PATH, encoding="utf-8-sig")
    rounds["code"] = rounds["code"].map(_norm_code)
    rounds["ret_pct"] = pd.to_numeric(rounds["ret_pct"], errors="coerce")
    rounds["entry_day"] = rounds["entry_day"].astype(str)
    rounds["ref_day"] = rounds["ref_day"].astype(str)
    if "name" not in rounds.columns:
        rounds["name"] = rounds["code"]

    missed = rounds[rounds["bucket"] != "selected"].copy()
    not_valid = rounds[rounds["bucket"] == "not_valid"].copy()
    selected = rounds[rounds["bucket"] == "selected"].copy()

    # --- 1) 종목 반복 ---
    nv_stats = _name_stats(not_valid)
    all_stats = _name_stats(rounds)
    pos_names = nv_stats[nv_stats["avg_ret"] > 0] if not nv_stats.empty else pd.DataFrame()
    neg_names = nv_stats[nv_stats["avg_ret"] <= 0] if not nv_stats.empty else pd.DataFrame()
    name_block = {
        "n_names_not_valid": int(len(nv_stats)),
        "n_names_pos_ev": int(len(pos_names)),
        "n_names_neg_ev": int(len(neg_names)),
        "n_names_5plus_wins": int((nv_stats["n_win"] >= 5).sum()) if not nv_stats.empty else 0,
        "share_pos_mass": _share_top(nv_stats, "sum_pos", [5, 10, 20, 30]),
        "top_pos_ev": pos_names.head(15).to_dict("records") if not pos_names.empty else [],
        "worst_neg_ev": (
            neg_names.sort_values("sum_all").head(10).to_dict("records")
            if not neg_names.empty
            else []
        ),
    }

    # --- 2) 테마/업종 ---
    themes = _load_themes()
    for df in (rounds, missed, not_valid, selected):
        if df.empty:
            continue
        df["sector"] = [
            _coarse_sector(themes.get(c, []), n)
            for c, n in zip(df["code"], df["name"])
        ]
    sector_nv = _bin_table(not_valid, "sector")
    sector_sel = _bin_table(selected, "sector")
    # 테마 explode (수익 자리만, not_valid)
    theme_rows: List[Dict[str, Any]] = []
    try:
        nv_pos = not_valid[not_valid["ret_pct"] > 0]
        bag: Dict[str, Dict[str, float]] = {}
        for _, r in nv_pos.iterrows():
            for th in themes.get(str(r["code"]), []) or ["(테마없음)"]:
                d = bag.setdefault(th, {"n": 0.0, "sum_pos": 0.0})
                d["n"] += 1
                d["sum_pos"] += float(r["ret_pct"])
        theme_rows = [
            {"theme": k, "n": int(v["n"]), "sum_pos": float(v["sum_pos"])}
            for k, v in bag.items()
        ]
        theme_rows.sort(key=lambda x: x["sum_pos"], reverse=True)
    except Exception:
        theme_rows = []

    # --- 3) 갭 ---
    st = _load_settings()
    dm = DataManagerV2()
    start = str(rounds["entry_day"].min())
    end = str(rounds["entry_day"].max())
    daily = _load_daily(dm, sorted(set(rounds["code"])), start, end)
    rounds_g = _attach_gap(rounds, daily)
    nv_g = rounds_g[rounds_g["bucket"] == "not_valid"]
    sel_g = rounds_g[rounds_g["bucket"] == "selected"]
    gap_block = {
        "not_valid": _bin_table(nv_g, "gap_bin"),
        "selected": _bin_table(sel_g, "gap_bin"),
    }
    try:
        nv_next = nv_g[nv_g["next_open_exit"] == True]  # noqa: E712
        sel_next = sel_g[sel_g["next_open_exit"] == True]  # noqa: E712
        gap_block["next_open_exit"] = {
            "not_valid_n": int(len(nv_next)),
            "not_valid_share_pct": float(len(nv_next) / len(nv_g) * 100.0) if len(nv_g) else 0.0,
            "not_valid_win_rate": float((nv_next["ret_pct"] > 0).mean() * 100.0) if len(nv_next) else 0.0,
            "not_valid_avg": float(nv_next["ret_pct"].mean()) if len(nv_next) else 0.0,
            "selected_n": int(len(sel_next)),
            "selected_share_pct": float(len(sel_next) / len(sel_g) * 100.0) if len(sel_g) else 0.0,
            "selected_win_rate": float((sel_next["ret_pct"] > 0).mean() * 100.0) if len(sel_next) else 0.0,
            "selected_avg": float(sel_next["ret_pct"].mean()) if len(sel_next) else 0.0,
        }
        nv_open = nv_g[nv_g["open_entry"] == True]  # noqa: E712
        gap_block["open_entry"] = {
            "not_valid_n": int(len(nv_open)),
            "not_valid_share_pct": float(len(nv_open) / len(nv_g) * 100.0) if len(nv_g) else 0.0,
            "not_valid_avg": float(nv_open["ret_pct"].mean()) if len(nv_open) else 0.0,
        }
    except Exception as e:
        gap_block["next_open_exit"] = {"error": str(e)}

    # --- Valid 탈락 사유 ---
    fail_block: Dict[str, Any] = {}
    try:
        panel_start = (pd.Timestamp(start) - timedelta(days=40)).strftime("%Y-%m-%d")
        df_panel, _, _ = load_scored_panel(st, panel_start, end, dm)
        df_panel["code"] = df_panel["code"].map(_norm_code)
        df_panel["ref_day"] = pd.to_datetime(df_panel["time"], errors="coerce").dt.strftime(
            "%Y-%m-%d"
        )
        keep = [
            c
            for c in (
                "code",
                "ref_day",
                "Valid_A",
                "Valid_B",
                "RSI",
                "OSC",
                "AvgVal5",
                "VolBurstActive_B",
                "_sel_ma_ok",
                "_sel_rs_pct",
                "_sel_pb_pct",
                "_sel_gap_pct",
                "_sel_b_trend_ok",
                "_sel_b_slope_ok",
                "_sel_b_rs_pct",
                "_sel_b_hi_pct",
                "_sel_b_gap_pct",
            )
            if c in df_panel.columns
        ]
        panel_s = df_panel[keep].copy()
        nv_join = not_valid.merge(panel_s, on=["code", "ref_day"], how="left")
        win_m = nv_join["ret_pct"] > 0
        lose_m = nv_join["ret_pct"] <= 0
        fail_win = _fail_table(nv_join, st, win_m)
        fail_lose = _fail_table(nv_join, st, lose_m)
        lose_map = {r["key"]: r["pct"] for r in fail_lose}
        compared = []
        for r in fail_win:
            lp = float(lose_map.get(r["key"], 0.0))
            compared.append(
                {
                    **r,
                    "lose_pct": lp,
                    "win_minus_lose_pctp": float(r["pct"] - lp),
                }
            )
        compared.sort(key=lambda x: abs(x["win_minus_lose_pctp"]), reverse=True)
        fail_block = {
            "n_joined": int(nv_join["RSI"].notna().sum()) if "RSI" in nv_join.columns else 0,
            "fail_on_wins": fail_win[:12],
            "fail_diff": compared[:12],
        }
    except Exception as e:
        fail_block = {"error": str(e), "trace": traceback.format_exc()[-400:]}

    summary: Dict[str, Any] = {
        "n_rounds": int(len(rounds)),
        "n_not_valid": int(len(not_valid)),
        "name": name_block,
        "sector_not_valid": sector_nv,
        "sector_selected": sector_sel,
        "themes_not_valid_wins": theme_rows[:15],
        "gap": gap_block,
        "valid_fail": fail_block,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "why_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not nv_stats.empty:
        nv_stats.to_csv(OUT_DIR / "name_stats_not_valid.csv", index=False, encoding="utf-8-sig")
    return summary


def _print(s: Dict[str, Any]) -> None:
    """콘솔 요약."""
    print("\n" + "=" * 72)
    print("선정 밖 수익 자리 — 3축 분해")
    print("=" * 72)

    nm = s.get("name") or {}
    print("\n[1] 종목 반복")
    print(
        f"  Valid탈락 종목 {nm.get('n_names_not_valid')}개 중 "
        f"+EV {nm.get('n_names_pos_ev')} / −EV {nm.get('n_names_neg_ev')}"
    )
    print(f"  수익 5회 이상 종목  {nm.get('n_names_5plus_wins')}개")
    sh = nm.get("share_pos_mass") or {}
    if sh:
        print(
            f"  수익 합 집중  top5 {sh.get('top5_pct', 0):.1f}%  "
            f"top10 {sh.get('top10_pct', 0):.1f}%  "
            f"top20 {sh.get('top20_pct', 0):.1f}%"
        )
    print("  +EV 상위")
    for r in (nm.get("top_pos_ev") or [])[:8]:
        print(
            f"    {r.get('name')}({r.get('code')})  "
            f"n={r.get('n')} 승 {r.get('n_win')}  "
            f"승률 {float(r.get('win_rate') or 0):.0f}%  "
            f"평균 {float(r.get('avg_ret') or 0):+.2f}%  "
            f"합+ {float(r.get('sum_pos') or 0):+.1f}%"
        )

    print("\n[2] 업종 (Valid 탈락)")
    print(f"  {'업종':<14s} {'건수':>6s} {'승률%':>7s} {'평균%':>8s} {'수익합':>8s}")
    for r in (s.get("sector_not_valid") or [])[:10]:
        print(
            f"  {str(r.get('bin')):<14s} "
            f"{int(r.get('n', 0)):6d} "
            f"{float(r.get('win_rate', 0)):7.1f} "
            f"{float(r.get('avg_ret', 0)):+8.2f} "
            f"{float(r.get('sum_pos', 0)):8.1f}"
        )
    print("  선정된 쪽 업종 평균")
    for r in (s.get("sector_selected") or [])[:6]:
        print(
            f"    {r.get('bin')}: n={r.get('n')} 승률 {float(r.get('win_rate') or 0):.1f}% "
            f"평균 {float(r.get('avg_ret') or 0):+.2f}%"
        )
    print("  수익 테마 상위")
    for r in (s.get("themes_not_valid_wins") or [])[:8]:
        print(
            f"    {r.get('theme')}  n={r.get('n')}  합+ {float(r.get('sum_pos') or 0):+.1f}%"
        )

    print("\n[3] 시초 갭 (Valid 탈락)")
    gap = (s.get("gap") or {}).get("not_valid") or []
    print(f"  {'갭':<16s} {'건수':>6s} {'승률%':>7s} {'평균%':>8s}")
    for r in gap:
        print(
            f"  {str(r.get('bin')):<16s} "
            f"{int(r.get('n', 0)):6d} "
            f"{float(r.get('win_rate', 0)):7.1f} "
            f"{float(r.get('avg_ret', 0)):+8.2f}"
        )
    nx = (s.get("gap") or {}).get("next_open_exit") or {}
    if nx and "error" not in nx:
        print(
            f"  익일 시초 청산  Valid탈락 {nx.get('not_valid_share_pct', 0):.1f}% "
            f"(승률 {nx.get('not_valid_win_rate', 0):.1f}% 평균 {nx.get('not_valid_avg', 0):+.2f}%)"
        )
        print(
            f"                 선정됨   {nx.get('selected_share_pct', 0):.1f}% "
            f"(승률 {nx.get('selected_win_rate', 0):.1f}% 평균 {nx.get('selected_avg', 0):+.2f}%)"
        )

    vf = s.get("valid_fail") or {}
    print("\n[+] Valid 탈락 사유 (수익 자리 vs 손실 자리)")
    if vf.get("error"):
        print(f"  실패: {vf.get('error')}")
    else:
        print(f"  패널 조인 {vf.get('n_joined')}건")
        print(f"  {'사유':<14s} {'수익자리%':>9s} {'손실자리%':>9s} {'차이':>7s}")
        for r in (vf.get("fail_diff") or [])[:10]:
            print(
                f"  {str(r.get('title')):<14s} "
                f"{float(r.get('pct', 0)):9.1f} "
                f"{float(r.get('lose_pct', 0)):9.1f} "
                f"{float(r.get('win_minus_lose_pctp', 0)):+7.1f}"
            )
        print("  (차이 + : 수익 자리에서 더 자주 탈락 → 필터가 좋은 자리를 자름)")
    print(f"\n저장: {OUT_DIR / 'why_summary.json'}")


def main() -> None:
    try:
        summary = run()
        _print(summary)
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
