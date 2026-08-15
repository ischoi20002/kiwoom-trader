# -*- coding: utf-8 -*-
"""
상습 -EV 종목 제외 — 전반→후반 워크포워드 검증.

전반(H1)에서 종목별 S1 기대값을 보고 명단을 만든 뒤,
후반(H2)에서 그 종목을 빼면 기대값이 오르는지 본다.

  python tools/diagnose_sel_name_denylist_wf.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

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

IN_CSV = ROOT / "tools" / "bt_compare_out_sel_outside_s1" / "s1_rounds.csv"
OUT_DIR = ROOT / "tools" / "bt_compare_out_sel_outside_s1"
CUT = pd.Timestamp("2026-01-01")

RULES: List[Dict[str, Any]] = [
    {"id": "N5_AVG0", "title": "n>=5 이고 평균<0", "min_n": 5, "max_avg": 0.0},
    {"id": "N8_AVG0", "title": "n>=8 이고 평균<0", "min_n": 8, "max_avg": 0.0},
    {"id": "N8_AVG-05", "title": "n>=8 이고 평균<=-0.5", "min_n": 8, "max_avg": -0.5},
    {"id": "N10_AVG0", "title": "n>=10 이고 평균<0", "min_n": 10, "max_avg": 0.0},
]


def _metrics(df: pd.DataFrame) -> Dict[str, Any]:
    """라운드 집합의 건수·승률·평균."""
    if df is None or df.empty:
        return {
            "n": 0,
            "n_win": 0,
            "win_rate": 0.0,
            "avg_ret": 0.0,
            "sum_ret": 0.0,
            "sum_pos": 0.0,
            "n_names": 0,
        }
    rets = pd.to_numeric(df["ret_pct"], errors="coerce")
    return {
        "n": int(len(df)),
        "n_win": int((rets > 0).sum()),
        "win_rate": float((rets > 0).mean() * 100.0),
        "avg_ret": float(rets.mean()),
        "sum_ret": float(rets.sum()),
        "sum_pos": float(rets[rets > 0].sum()) if (rets > 0).any() else 0.0,
        "n_names": int(df["code"].nunique()),
    }


def _delta(after: Dict[str, Any], before: Dict[str, Any]) -> Dict[str, Any]:
    """제외 후 − 제외 전."""
    out: Dict[str, Any] = {}
    for k in ("n", "n_win", "n_names"):
        out[k] = int(after.get(k, 0) or 0) - int(before.get(k, 0) or 0)
    for k in ("win_rate", "avg_ret", "sum_ret", "sum_pos"):
        out[k] = float(after.get(k, 0.0) or 0.0) - float(before.get(k, 0.0) or 0.0)
    return out


def _name_stats(df: pd.DataFrame) -> pd.DataFrame:
    """종목별 H1 통계."""
    if df is None or df.empty:
        return pd.DataFrame()
    rows: List[Dict[str, Any]] = []
    for (code, name), g in df.groupby(["code", "name"], dropna=False):
        rets = pd.to_numeric(g["ret_pct"], errors="coerce")
        rows.append(
            {
                "code": str(code),
                "name": str(name),
                "n": int(len(g)),
                "n_win": int((rets > 0).sum()),
                "win_rate": float((rets > 0).mean() * 100.0),
                "avg_ret": float(rets.mean()),
                "sum_ret": float(rets.sum()),
            }
        )
    return pd.DataFrame(rows)


def _denylist(stats: pd.DataFrame, min_n: int, max_avg: float) -> pd.DataFrame:
    """H1 통계에서 제외 명단."""
    if stats is None or stats.empty:
        return pd.DataFrame()
    m = (stats["n"] >= int(min_n)) & (stats["avg_ret"] <= float(max_avg))
    return stats.loc[m].sort_values("avg_ret")


def _apply(df: pd.DataFrame, banned: Set[str]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """명단 제외 전/후."""
    if df is None or df.empty:
        return df, df
    kept = df[~df["code"].isin(banned)].copy()
    return df, kept


def _pack(
    label: str,
    before: pd.DataFrame,
    after: pd.DataFrame,
    banned: Set[str],
) -> Dict[str, Any]:
    """한 풀(전체/Valid탈락/선정)의 전후 비교."""
    b = _metrics(before)
    a = _metrics(after)
    hit = before[before["code"].isin(banned)] if before is not None and not before.empty else pd.DataFrame()
    return {
        "pool": label,
        "before": b,
        "after": a,
        "delta": _delta(a, b),
        "removed": _metrics(hit),
        "removed_share_pct": (
            100.0 * float(len(hit)) / float(len(before)) if before is not None and len(before) else 0.0
        ),
    }


def run() -> Dict[str, Any]:
    """워크포워드 검증을 돌린다."""
    if not IN_CSV.is_file():
        raise RuntimeError(f"라운드 파일이 없습니다: {IN_CSV}")
    df = pd.read_csv(IN_CSV, encoding="utf-8-sig")
    df["code"] = df["code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    df["ret_pct"] = pd.to_numeric(df["ret_pct"], errors="coerce")
    df["entry_day"] = pd.to_datetime(df["entry_day"], errors="coerce")
    df = df.dropna(subset=["entry_day", "ret_pct", "code"])
    if "name" not in df.columns:
        df["name"] = df["code"]

    h1 = df[df["entry_day"] < CUT].copy()
    h2 = df[df["entry_day"] >= CUT].copy()
    h1_stats = _name_stats(h1)

    h2_all = h2
    h2_nv = h2[h2["bucket"] == "not_valid"]
    h2_sel = h2[h2["bucket"] == "selected"]

    experiments: List[Dict[str, Any]] = []
    for rule in RULES:
        ban_df = _denylist(h1_stats, int(rule["min_n"]), float(rule["max_avg"]))
        banned = set(ban_df["code"].astype(str))
        _, all_kept = _apply(h2_all, banned)
        _, nv_kept = _apply(h2_nv, banned)
        _, sel_kept = _apply(h2_sel, banned)
        exp = {
            "id": rule["id"],
            "title": rule["title"],
            "n_banned": int(len(ban_df)),
            "banned_h1_avg": float(ban_df["avg_ret"].mean()) if len(ban_df) else 0.0,
            "banned_names": ban_df.head(15).to_dict("records"),
            "h2_all": _pack("all", h2_all, all_kept, banned),
            "h2_not_valid": _pack("not_valid", h2_nv, nv_kept, banned),
            "h2_selected": _pack("selected", h2_sel, sel_kept, banned),
        }
        experiments.append(exp)

    # 대조: H1 +EV 명단만 남기기 (가점/편입 방향)
    pos = h1_stats[(h1_stats["n"] >= 8) & (h1_stats["avg_ret"] > 0)]
    pos_set = set(pos["code"].astype(str))
    admit_nv = h2_nv[h2_nv["code"].isin(pos_set)]
    admit_all = h2_all[h2_all["code"].isin(pos_set)]

    summary: Dict[str, Any] = {
        "cut": str(CUT.date()),
        "h1": {**_metrics(h1), "n_names": int(h1["code"].nunique())},
        "h2": {**_metrics(h2), "n_names": int(h2["code"].nunique())},
        "h1_pos_ev_n8": int(len(pos)),
        "experiments": experiments,
        "admit_h1_pos_n8": {
            "n_names": int(len(pos)),
            "h2_all": _metrics(admit_all),
            "h2_not_valid": _metrics(admit_nv),
            "h2_not_valid_base": _metrics(h2_nv),
        },
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "denylist_wf.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if not h1_stats.empty:
        h1_stats.sort_values("avg_ret").to_csv(
            OUT_DIR / "h1_name_stats.csv", index=False, encoding="utf-8-sig"
        )
    return summary


def _line(m: Dict[str, Any]) -> str:
    return (
        f"n={int(m.get('n', 0)):4d}  "
        f"종 {int(m.get('n_names', 0)):3d}  "
        f"승률 {float(m.get('win_rate', 0)):5.1f}%  "
        f"평균 {float(m.get('avg_ret', 0)):+6.3f}%  "
        f"합 {float(m.get('sum_ret', 0)):+7.1f}"
    )


def _print(s: Dict[str, Any]) -> None:
    print("\n" + "=" * 76)
    print(f"상습 -EV 제외 워크포워드  (컷 {s.get('cut')})")
    print("=" * 76)
    print(f"  H1  {_line(s.get('h1') or {})}")
    print(f"  H2  {_line(s.get('h2') or {})}")
    for exp in s.get("experiments") or []:
        print("\n" + "-" * 76)
        print(f"  규칙 {exp['id']}: {exp['title']}  →  명단 {exp['n_banned']}종  (H1평균 {exp['banned_h1_avg']:+.2f}%)")
        for key, title in (
            ("h2_all", "H2 전 유니버스"),
            ("h2_not_valid", "H2 Valid탈락 (필터를 풀 때)"),
            ("h2_selected", "H2 실제 선정"),
        ):
            pack = exp.get(key) or {}
            d = pack.get("delta") or {}
            print(f"    {title}")
            print(f"      전  {_line(pack.get('before') or {})}")
            print(f"      후  {_line(pack.get('after') or {})}")
            print(
                f"      Δ   건수 {int(d.get('n', 0)):+d}  "
                f"승률 {float(d.get('win_rate', 0)):+.2f}%p  "
                f"평균 {float(d.get('avg_ret', 0)):+.3f}%p  "
                f"제거비중 {float(pack.get('removed_share_pct', 0)):.1f}%"
            )
        names = exp.get("banned_names") or []
        if names:
            head = ", ".join(f"{r.get('name')}({float(r.get('avg_ret', 0)):+.1f}%)" for r in names[:8])
            print(f"    명단 예: {head}")

    adm = s.get("admit_h1_pos_n8") or {}
    print("\n" + "-" * 76)
    print(f"  대조: H1 +EV(n>=8) {adm.get('n_names')}종만 H2 Valid탈락에서 남김")
    print(f"    Valid탈락 전체  {_line(adm.get('h2_not_valid_base') or {})}")
    print(f"    +EV만 남김      {_line(adm.get('h2_not_valid') or {})}")
    print(f"\n저장: {OUT_DIR / 'denylist_wf.json'}")


def main() -> None:
    summary = run()
    _print(summary)


if __name__ == "__main__":
    main()
