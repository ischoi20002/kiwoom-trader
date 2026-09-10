# -*- coding: utf-8 -*-
"""
모의 vs 실전 ka10080(tic_scope=1) 1분봉 첫 등장 시각 비교.

사용:
  python tools/compare_mock_vs_live_1m_latency.py --mode check-last
  python tools/compare_mock_vs_live_1m_latency.py --mode wait-next --codes 017800
  python tools/compare_mock_vs_live_1m_latency.py --mode both --timeout 60
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_config_lock = threading.Lock()


@dataclass
class EnvAuth:
    name: str
    base_url: str
    app_key: str
    app_secret: str
    token: Optional[str] = None


def get_token(env: EnvAuth, log=print) -> Optional[str]:
    url = f"{env.base_url.rstrip('/')}/oauth2/token"
    body = {
        "grant_type": "client_credentials",
        "appkey": env.app_key,
        "secretkey": env.app_secret,
    }
    try:
        log(f"[{env.name}] 토큰 발급 중...")
        res = requests.post(url, json=body, timeout=15)
        if res.status_code != 200:
            log(f"[{env.name}] 토큰 실패 HTTP {res.status_code}")
            return None
        data = res.json()
        tok = data.get("token") or data.get("access_token")
        if not tok:
            return None
        log(f"[{env.name}] 토큰 OK")
        return str(tok)
    except Exception as e:
        log(f"[{env.name}] 토큰 예외: {e!r}")
        return None


def build_envs() -> Tuple[EnvAuth, EnvAuth]:
    import config

    mock = EnvAuth(
        name="MOCK",
        base_url=str(getattr(config, "MOCK_URL", "https://mockapi.kiwoom.com")),
        app_key=str(getattr(config, "MOCK_APP_KEY", "") or ""),
        app_secret=str(getattr(config, "MOCK_APP_SECRET", "") or ""),
    )
    live = EnvAuth(
        name="LIVE",
        base_url=str(getattr(config, "REAL_URL", "https://api.kiwoom.com")),
        app_key=str(getattr(config, "APP_KEY", "") or ""),
        app_secret=str(getattr(config, "APP_SECRET", "") or ""),
    )
    return mock, live


def last_completed_m1_open(now: Optional[datetime] = None) -> pd.Timestamp:
    from utils.krx_bars import m1_bar_open_label_for_just_completed_minute

    return pd.Timestamp(m1_bar_open_label_for_just_completed_minute(now or datetime.now()))


def next_minute_boundary(now: Optional[datetime] = None) -> datetime:
    t = now or datetime.now()
    floored = t.replace(second=0, microsecond=0)
    if t > floored:
        return floored + timedelta(minutes=1)
    return floored + timedelta(minutes=1)


def pick_1m_row(df: pd.DataFrame, closed_open: pd.Timestamp) -> Optional[pd.Series]:
    if df is None or getattr(df, "empty", True):
        return None
    want = pd.Timestamp(closed_open).floor("min").floor("s")
    if want in df.index:
        return df.loc[want]
    for ix in df.index:
        try:
            if pd.Timestamp(ix).floor("s") == want:
                return df.loc[ix]
        except Exception:
            continue
    return None


def fetch_1m_row(
    env: EnvAuth,
    dm: Any,
    code: str,
    closed_open: pd.Timestamp,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """Returns (ohlcv dict or None, status)."""
    import config
    from core.bt_hp_signal_bridge import ka10080_raw_rows_to_1m_df
    from utils.krx_bars import chart_covers_m1_closed_minute

    if not env.token:
        return None, "no_token"

    with _config_lock:
        old_mock = bool(getattr(config, "USE_MOCK", True))
        old_url = getattr(config, "URL", None)
        try:
            config.USE_MOCK = env.name == "MOCK"
            config.URL = env.base_url
            raw_list, _nk = dm.fetch_kiwoom_minute_chart_page(
                code,
                str(env.token),
                "",
                tic_scope="1",
                fetch_context="m1_latency_probe",
            )
        finally:
            config.USE_MOCK = old_mock
            config.URL = old_url

    if not raw_list:
        return None, "empty_raw"
    df = ka10080_raw_rows_to_1m_df(raw_list)
    row = pick_1m_row(df, closed_open)
    if row is not None:
        return (
            {
                "open": int(round(float(row["Open"]))),
                "high": int(round(float(row["High"]))),
                "low": int(round(float(row["Low"]))),
                "close": int(round(float(row["Close"]))),
                "volume": int(round(float(row["Volume"]))),
            },
            "ok",
        )
    if chart_covers_m1_closed_minute(df, closed_open):
        return None, "row_not_found"
    try:
        mx = pd.Timestamp(df.index.max()).strftime("%H:%M:%S") if not df.empty else "-"
    except Exception:
        mx = "-"
    return None, f"minute_not_ready(max={mx})"


def fmt(row: Optional[Dict[str, Any]]) -> str:
    if not row:
        return "(없음)"
    return (
        f"O={row['open']} H={row['high']} L={row['low']} "
        f"C={row['close']} V={row['volume']}"
    )


def check_last(envs: List[EnvAuth], dm: Any, codes: List[str], closed_open: pd.Timestamp) -> None:
    from utils.krx_bars import m1_bar_end_ts_for_closed_open

    end_ts = m1_bar_end_ts_for_closed_open(closed_open)
    print(
        f"\n=== 즉시 조회 1분봉 open={closed_open.strftime('%H:%M:%S')} "
        f"end={end_ts.strftime('%H:%M:%S')} ==="
    )
    now = datetime.now()
    print(f"지금 {now.strftime('%H:%M:%S')} (분 마감 후 {(now - end_ts.to_pydatetime()).total_seconds():.0f}초)")
    for code in codes:
        print(f"\n-- {code} --")
        for env in envs:
            t0 = time.perf_counter()
            row, st = fetch_1m_row(env, dm, code, closed_open)
            ms = (time.perf_counter() - t0) * 1000
            tag = "FOUND" if row else st
            print(f"  [{env.name:4s}] {tag:20s} {fmt(row)}  ({ms:.0f}ms)")


def poll_one(
    env: EnvAuth,
    dm: Any,
    code: str,
    closed_open: pd.Timestamp,
    minute_end: datetime,
    *,
    interval_sec: float,
    timeout_sec: float,
    results: Dict[str, Any],
    key: str,
) -> None:
    deadline = time.monotonic() + timeout_sec
    n = 0
    first_wall = None
    last_row = None
    last_st = ""
    while time.monotonic() < deadline:
        n += 1
        row, st = fetch_1m_row(env, dm, code, closed_open)
        last_st = st
        if row is not None:
            first_wall = datetime.now()
            last_row = row
            break
        time.sleep(max(0.15, interval_sec))
    results[key] = {
        "env": env.name,
        "code": code,
        "attempts": n,
        "found": last_row is not None,
        "first_wall": first_wall,
        "ohlcv": last_row,
        "last_status": last_st,
        "elapsed_from_minute_end": (
            (first_wall - minute_end).total_seconds() if first_wall is not None else None
        ),
    }


def wait_next(
    envs: List[EnvAuth],
    dm: Any,
    codes: List[str],
    *,
    interval_sec: float,
    timeout_sec: float,
) -> None:
    from utils.krx_bars import m1_bar_open_label_for_just_completed_minute

    boundary = next_minute_boundary()
    print("\n=== 다음 1분 마감 대기 폴링 ===")
    print(f"마감 시각: {boundary.strftime('%H:%M:%S')}")
    wait_s = (boundary - datetime.now()).total_seconds()
    print(f"대기: {wait_s:.1f}초 (이후 {interval_sec}s 간격, 최대 {timeout_sec:.0f}s)")
    if wait_s > 0:
        time.sleep(max(0.0, wait_s - 0.15))
        while datetime.now() < boundary:
            time.sleep(0.02)

    # 방금 끝난 분
    closed_open = pd.Timestamp(m1_bar_open_label_for_just_completed_minute(boundary))
    print(
        f"폴링 시작 @ {datetime.now().strftime('%H:%M:%S.%f')[:-3]} "
        f"대상 open={closed_open.strftime('%H:%M:%S')}"
    )

    results: Dict[str, Any] = {}
    for code in codes:
        threads: List[threading.Thread] = []
        for env in envs:
            key = f"{env.name}:{code}"
            th = threading.Thread(
                target=poll_one,
                args=(env, dm, code, closed_open, boundary),
                kwargs={
                    "interval_sec": interval_sec,
                    "timeout_sec": timeout_sec,
                    "results": results,
                    "key": key,
                },
                daemon=True,
            )
            threads.append(th)
            th.start()
            time.sleep(0.03)
        for th in threads:
            th.join(timeout=timeout_sec + 30)

        print(f"\n-- {code} @ minute_end={boundary.strftime('%H:%M:%S')} --")
        for env in envs:
            r = results.get(f"{env.name}:{code}") or {}
            if not r.get("found"):
                print(
                    f"  [{env.name:4s}] NOT_FOUND "
                    f"(시도 {r.get('attempts', 0)}, last={r.get('last_status')})"
                )
            else:
                el = r["elapsed_from_minute_end"]
                fw = r["first_wall"]
                print(
                    f"  [{env.name:4s}] FOUND +{el:.1f}s "
                    f"@ {fw.strftime('%H:%M:%S.%f')[:-3]} "
                    f"시도={r.get('attempts')} {fmt(r.get('ohlcv'))}"
                )
        m = results.get(f"MOCK:{code}") or {}
        l = results.get(f"LIVE:{code}") or {}
        if m.get("found") and l.get("found"):
            diff = float(m["elapsed_from_minute_end"]) - float(l["elapsed_from_minute_end"])
            faster = "LIVE" if diff > 0 else ("MOCK" if diff < 0 else "동일")
            print(f"  => 모의-실전 지연차: {diff:+.1f}s (더 빠른 쪽: {faster})")
        elif l.get("found") and not m.get("found"):
            print("  => 실전만 발견")
        elif m.get("found") and not l.get("found"):
            print("  => 모의만 발견")
        else:
            print("  => 양쪽 미발견")


def parse_codes(raw: str) -> List[str]:
    out: List[str] = []
    for p in str(raw or "").split(","):
        c = p.strip().zfill(6)
        if c and c.isdigit():
            out.append(c)
    return out or ["017800"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("check-last", "wait-next", "both"), default="both")
    ap.add_argument("--codes", default="017800")
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()
    codes = parse_codes(args.codes)

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    mock, live = build_envs()
    mock.token = get_token(mock)
    live.token = get_token(live)
    if not mock.token or not live.token:
        print("토큰 실패")
        return 1

    from data_manager_v2 import DataManagerV2

    dm = DataManagerV2(db_path=str(ROOT / "trading_data.db"))

    if args.mode in ("check-last", "both"):
        check_last([mock, live], dm, codes, last_completed_m1_open())

    if args.mode in ("wait-next", "both"):
        wait_next(
            [mock, live],
            dm,
            codes,
            interval_sec=float(args.interval),
            timeout_sec=float(args.timeout),
        )

    print("\n완료.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
