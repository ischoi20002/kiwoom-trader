# -*- coding: utf-8 -*-
"""
틱 누적 1분봉(realtime_1m_tick_*.db) vs 공식 ka10080(1분) 비교.

- 틱 DB: 시작 라벨(open) — 09:14 = 09:14:00~09:14:59
- 공식 API: cntr_tm = 봉 종가 시각 — 같은 봉은 09:15
- 비교 키: 틱 open_lab ↔ 공식 (end - 1분)

사용:
  python tools/compare_tick_1m_vs_official_1m.py
  python tools/compare_tick_1m_vs_official_1m.py --day 2026-08-04 --codes 005930
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _tick_db_path() -> Path:
    try:
        from utils.server_mode_paths import realtime_tick_1m_db_path

        return Path(realtime_tick_1m_db_path())
    except Exception:
        return ROOT / "realtime_1m_tick_mock.db"


def _load_tick(
    db: Path, day: Optional[str], codes: Optional[List[str]]
) -> pd.DataFrame:
    if not db.is_file():
        return pd.DataFrame()
    con = sqlite3.connect(str(db))
    try:
        sql = (
            "SELECT code, datetime, open, high, low, close, volume "
            "FROM bars_1m_tick_rt"
        )
        where: List[str] = []
        params: List[Any] = []
        if day:
            where.append("datetime >= ? AND datetime < ?")
            params.extend([f"{day} 00:00:00", f"{day} 23:59:59"])
        if codes:
            ph = ",".join("?" * len(codes))
            where.append(f"code IN ({ph})")
            params.extend([str(c).zfill(6) for c in codes])
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY code, datetime"
        df = pd.read_sql_query(sql, con, params=params)
    except Exception as e:
        print(f"[warn] tick load: {e!r}")
        df = pd.DataFrame()
    finally:
        con.close()
    if df.empty:
        return df
    df["code"] = df["code"].astype(str).str.zfill(6)
    df["datetime"] = pd.to_datetime(df["datetime"])
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # 거래시간 1분만
    try:
        from utils.krx_bars import is_krx_m1_closed_open_label

        mask = df["datetime"].map(lambda t: bool(is_krx_m1_closed_open_label(t)))
        df = df.loc[mask].copy()
    except Exception:
        pass
    return df


def _raw_to_1m_close_index(raw_list: List[Dict[str, Any]]) -> pd.DataFrame:
    if not raw_list:
        return pd.DataFrame()
    df = pd.DataFrame(raw_list)
    f_cl, f_op, f_hg, f_lw, f_tm, f_vl = (
        "cur_prc",
        "open_pric",
        "high_pric",
        "low_pric",
        "cntr_tm",
        "trde_qty",
    )
    for c in (f_cl, f_op, f_hg, f_lw, f_vl):
        if c not in df.columns:
            return pd.DataFrame()
        df[c] = pd.to_numeric(
            df[c].astype(str).str.replace("+", "").str.replace("-", ""),
            errors="coerce",
        ).fillna(0)
    df["datetime"] = pd.to_datetime(
        df[f_tm].astype(str), format="%Y%m%d%H%M%S", errors="coerce"
    )
    df = df.dropna(subset=["datetime"])
    if df.empty:
        return pd.DataFrame()
    df = df.set_index("datetime").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    out = df[[f_op, f_hg, f_lw, f_cl, f_vl]].copy()
    out.columns = ["open", "high", "low", "close", "volume"]
    return out


def _fetch_official_1m(code: str, token: str, dm: Any, max_pages: int = 6) -> pd.DataFrame:
    merged: List[pd.DataFrame] = []
    nk = ""
    for _ in range(max(1, max_pages)):
        raw_list, nk_out = dm.fetch_kiwoom_minute_chart_page(
            code,
            token,
            nk,
            tic_scope="1",
            fetch_context="cmp_tick_1m",
        )
        if not raw_list:
            break
        chunk = _raw_to_1m_close_index(raw_list)
        if chunk is not None and not chunk.empty:
            merged.append(chunk)
        nk = (nk_out or "").strip()
        if not nk:
            break
        time.sleep(0.15)
    if not merged:
        return pd.DataFrame()
    out = pd.concat(merged).sort_index()
    return out[~out.index.duplicated(keep="last")]


def _row_at_open(off: pd.DataFrame, open_lab: pd.Timestamp) -> Optional[pd.Series]:
    """공식 close-index에서 open 라벨에 해당하는 행."""
    if off is None or off.empty:
        return None
    end_ts = pd.Timestamp(open_lab).floor("min") + pd.Timedelta(minutes=1)
    if end_ts in off.index:
        return off.loc[end_ts]
    want = end_ts.floor("s")
    for ix in off.index:
        try:
            if pd.Timestamp(ix).floor("s") == want:
                return off.loc[ix]
        except Exception:
            continue
    return None


def compare(tick: pd.DataFrame, official_by_code: Dict[str, pd.DataFrame]) -> None:
    if tick.empty:
        print("틱 1분 데이터 없음 — 감시(누적)가 돌고 있는지 확인하세요.")
        return

    rows: List[Dict[str, Any]] = []
    for _, r in tick.iterrows():
        code = str(r["code"])
        open_lab = pd.Timestamp(r["datetime"]).floor("min")
        off = official_by_code.get(code)
        orow = _row_at_open(off, open_lab) if off is not None else None
        item: Dict[str, Any] = {
            "code": code,
            "open_lab": open_lab,
            "t_o": float(r["open"]),
            "t_h": float(r["high"]),
            "t_l": float(r["low"]),
            "t_c": float(r["close"]),
            "t_v": float(r["volume"]),
        }
        if orow is None:
            item["status"] = "OFFICIAL_MISS"
        else:
            item["o_o"] = float(orow["open"])
            item["o_h"] = float(orow["high"])
            item["o_l"] = float(orow["low"])
            item["o_c"] = float(orow["close"])
            item["o_v"] = float(orow["volume"])
            ohlc_eq = (
                item["t_o"] == item["o_o"]
                and item["t_h"] == item["o_h"]
                and item["t_l"] == item["o_l"]
                and item["t_c"] == item["o_c"]
            )
            item["status"] = "OHLC_EQ" if ohlc_eq else "OHLC_DIFF"
            item["d_o"] = item["t_o"] - item["o_o"]
            item["d_h"] = item["t_h"] - item["o_h"]
            item["d_l"] = item["t_l"] - item["o_l"]
            item["d_c"] = item["t_c"] - item["o_c"]
            item["d_v"] = item["t_v"] - item["o_v"]
        rows.append(item)

    n = len(rows)
    n_miss = sum(1 for x in rows if x["status"] == "OFFICIAL_MISS")
    n_eq = sum(1 for x in rows if x["status"] == "OHLC_EQ")
    n_diff = sum(1 for x in rows if x["status"] == "OHLC_DIFF")
    print(f"\n=== 요약 (틱 {n}봉, 거래시간 필터 후) ===")
    print(f"  OHLC 일치: {n_eq}")
    print(f"  OHLC 불일치: {n_diff}")
    print(f"  공식 행 없음: {n_miss}")

    show = [x for x in rows if x["status"] != "OHLC_EQ"][:40]
    if not show:
        show = rows[:20]
    print("\n=== 상세 (불일치/미스 우선, 최대 40) ===")
    for x in show:
        lab = pd.Timestamp(x["open_lab"]).strftime("%Y-%m-%d %H:%M")
        if x["status"] == "OFFICIAL_MISS":
            print(
                f"  {x['code']} {lab} MISS  "
                f"tick OHLCV={x['t_o']:.0f}/{x['t_h']:.0f}/{x['t_l']:.0f}/{x['t_c']:.0f}/{x['t_v']:.0f}"
            )
        elif x["status"] == "OHLC_EQ":
            print(
                f"  {x['code']} {lab} EQ    "
                f"OHLCV={x['t_o']:.0f}/{x['t_h']:.0f}/{x['t_l']:.0f}/{x['t_c']:.0f}/{x['t_v']:.0f}"
            )
        else:
            print(
                f"  {x['code']} {lab} DIFF "
                f"tick={x['t_o']:.0f}/{x['t_h']:.0f}/{x['t_l']:.0f}/{x['t_c']:.0f}/{x['t_v']:.0f}  "
                f"off={x['o_o']:.0f}/{x['o_h']:.0f}/{x['o_l']:.0f}/{x['o_c']:.0f}/{x['o_v']:.0f}  "
                f"dO/H/L/C/V={x['d_o']:.0f}/{x['d_h']:.0f}/{x['d_l']:.0f}/{x['d_c']:.0f}/{x['d_v']:.0f}"
            )

    if n_diff:
        diffs = [x for x in rows if x["status"] == "OHLC_DIFF"]
        for key, label in (
            ("d_o", "시가"),
            ("d_h", "고가"),
            ("d_l", "저가"),
            ("d_c", "종가"),
            ("d_v", "거래량"),
        ):
            vals = [abs(float(x[key])) for x in diffs]
            print(
                f"  |Δ{label}| med={pd.Series(vals).median():.1f} "
                f"max={max(vals):.1f} (n={len(vals)})"
            )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default="", help="YYYY-MM-DD (비우면 틱 DB 전체)")
    ap.add_argument("--codes", default="", help="쉼표 구분 종목코드")
    ap.add_argument("--max-pages", type=int, default=8)
    args = ap.parse_args()

    day = (args.day or "").strip() or None
    codes = [c.strip().zfill(6) for c in args.codes.split(",") if c.strip()] or None

    db = _tick_db_path()
    print(f"틱 DB: {db} (exists={db.is_file()})")
    tick = _load_tick(db, day, codes)
    print(f"틱 봉 수(거래시간): {len(tick)}")
    if not tick.empty:
        print(
            f"  기간: {tick['datetime'].min()} ~ {tick['datetime'].max()}  "
            f"종목수={tick['code'].nunique()}"
        )
        print(f"  종목: {sorted(tick['code'].unique().tolist())[:20]}")

    if tick.empty:
        print(
            "\n오늘/지정일 틱 누적이 비어 있습니다. "
            "GUI 감시 중이고 realtime_tick_m1_accumulate_save_only=ON 인지 확인하세요."
        )
        return 1

    # 인증 + DM
    try:
        import config
        from core.auth_manager import AuthManager
        from data_manager_v2 import DataManagerV2
        from core.bt_hp_signal_bridge import ka10080_raw_rows_to_1m_df
    except Exception as e:
        print(f"import 실패: {e!r}")
        return 2

    try:
        from utils.server_mode_paths import is_mock_mode

        mock = bool(is_mock_mode())
    except Exception:
        mock = True

    try:
        config.USE_MOCK = mock
        if mock:
            config.URL = getattr(config, "MOCK_URL", config.URL)
    except Exception:
        pass

    am = AuthManager()
    token = am.get_auth_token()
    if not token:
        print("토큰 발급 실패")
        return 3

    dm = DataManagerV2(str(ROOT / "trading_data.db"))
    official: Dict[str, pd.DataFrame] = {}

    def _fetch(code: str) -> pd.DataFrame:
        merged: List[pd.DataFrame] = []
        nk = ""
        for _ in range(max(1, int(args.max_pages))):
            raw_list, nk_out = dm.fetch_kiwoom_minute_chart_page(
                code,
                str(token),
                nk,
                tic_scope="1",
                fetch_context="cmp_tick_1m",
            )
            if not raw_list:
                break
            chunk = ka10080_raw_rows_to_1m_df(raw_list)
            if chunk is not None and not getattr(chunk, "empty", True):
                # Open/High/... → lower
                c2 = chunk.rename(
                    columns={
                        "Open": "open",
                        "High": "high",
                        "Low": "low",
                        "Close": "close",
                        "Volume": "volume",
                    }
                )
                merged.append(c2[["open", "high", "low", "close", "volume"]])
            nk = (nk_out or "").strip()
            if not nk:
                break
            time.sleep(0.15)
        if not merged:
            return pd.DataFrame()
        out = pd.concat(merged).sort_index()
        return out[~out.index.duplicated(keep="last")]

    for code in sorted(tick["code"].unique().tolist()):
        print(f"공식 1분 조회: {code} ...")
        try:
            official[code] = _fetch(code)
            print(f"  → {len(official[code])} rows")
        except Exception as e:
            print(f"  → 실패: {e!r}")
            official[code] = pd.DataFrame()
        time.sleep(0.35)

    compare(tick, official)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
