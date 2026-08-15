# -*- coding: utf-8 -*-
"""
모의(mockapi) vs 실전(api.kiwoom.com) ka10080 15분봉 첫 등장 시각 비교.

사용:
  # 직전 완성 슬롯이 지금 조회되는지 (즉시)
  python tools/compare_mock_vs_live_15m_latency.py --mode check-last

  # 다음 슬롯 마감까지 대기 후 1초 폴링 (지연 측정)
  python tools/compare_mock_vs_live_15m_latency.py --mode wait-next

  # 둘 다
  python tools/compare_mock_vs_live_15m_latency.py --mode both

  python tools/compare_mock_vs_live_15m_latency.py --mode wait-next --codes 017800,139480 --timeout 180
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

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class EnvAuth:
    name: str
    base_url: str
    app_key: str
    app_secret: str
    token: Optional[str] = None


def _slot_end_floor(now: datetime) -> datetime:
    """현재 시각이 속한 15분 슬롯의 종료 라벨(우측 정각)."""
    from utils.krx_bars import krx_stream_slot_end_datetime

    return krx_stream_slot_end_datetime(now)


def last_completed_slot(now: Optional[datetime] = None) -> datetime:
    """직전 완성 슬롯 라벨."""
    t = now or datetime.now()
    end = _slot_end_floor(t)
    if t < end:
        # 아직 진행 중 → 직전 슬롯
        return end - timedelta(minutes=15)
    # 정확히 경계면 방금 완성
    return end


def next_slot_end(now: Optional[datetime] = None) -> datetime:
    t = now or datetime.now()
    end = _slot_end_floor(t)
    if t < end:
        return end
    return end + timedelta(minutes=15)


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
            log(f"[{env.name}] 토큰 실패 HTTP {res.status_code}: {res.text[:120]}")
            return None
        data = res.json()
        tok = data.get("token") or data.get("access_token")
        if not tok:
            log(f"[{env.name}] 토큰 필드 없음: {list(data.keys())}")
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


def fetch_row(
    env: EnvAuth,
    dm: Any,
    code: str,
    slot: datetime,
    *,
    max_pages: int = 8,
) -> Optional[Tuple[float, float, float, float, float]]:
    """USE_MOCK를 잠시 env에 맞춰 두지 않고, DataManager는 token+내부 URL 이슈가 있을 수 있음.

    DataManagerV2.fetch_kiwoom_minute_chart_page 는 config.URL 을 쓰므로
    호출 전에 config.URL / USE_MOCK 를 해당 env로 전환한다 (락으로 직렬화).
    """
    from core.rt_closed_15m_official_replace import fetch_official_15m_row_for_timestamp

    if not env.token:
        return None
    return fetch_official_15m_row_for_timestamp(
        dm,
        code,
        env.token,
        slot,
        tic_scope="15",
        max_pages=max_pages,
        max_retries=1,
        inter_retry_sec=0.2,
        log=None,
        app=None,
    )


_config_lock = threading.Lock()


def fetch_row_switched(
    env: EnvAuth,
    dm: Any,
    code: str,
    slot: datetime,
    *,
    max_pages: int = 8,
) -> Optional[Tuple[float, float, float, float, float]]:
    import config

    with _config_lock:
        old_mock = bool(getattr(config, "USE_MOCK", True))
        old_url = getattr(config, "URL", None)
        try:
            is_mock = env.name == "MOCK"
            config.USE_MOCK = is_mock
            config.URL = env.base_url
            return fetch_row(env, dm, code, slot, max_pages=max_pages)
        finally:
            config.USE_MOCK = old_mock
            config.URL = old_url


def fmt_ohlcv(tup: Optional[Tuple[float, float, float, float, float]]) -> str:
    if not tup:
        return "(없음)"
    o, h, l, c, v = tup
    return f"O={int(o)} H={int(h)} L={int(l)} C={int(c)} V={int(v)}"


def check_slot_now(
    envs: List[EnvAuth],
    dm: Any,
    codes: List[str],
    slot: datetime,
) -> None:
    slot_s = slot.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n=== 즉시 조회: 슬롯 {slot_s} ===")
    now = datetime.now()
    print(f"지금 {now.strftime('%H:%M:%S')} (슬롯 마감 후 {(now - slot).total_seconds():.0f}초)")
    for code in codes:
        print(f"\n-- {code} --")
        for env in envs:
            t0 = time.perf_counter()
            tup = fetch_row_switched(env, dm, code, slot)
            dt = (time.perf_counter() - t0) * 1000
            status = "FOUND" if tup else "NOT_FOUND"
            print(f"  [{env.name:4s}] {status:9s} {fmt_ohlcv(tup)}  ({dt:.0f}ms)")


def poll_until_found(
    env: EnvAuth,
    dm: Any,
    code: str,
    slot: datetime,
    *,
    interval_sec: float,
    timeout_sec: float,
    results: Dict[str, Any],
    key: str,
) -> None:
    deadline = time.monotonic() + timeout_sec
    n = 0
    first_mono = None
    first_wall = None
    last_tup = None
    while time.monotonic() < deadline:
        n += 1
        tup = fetch_row_switched(env, dm, code, slot, max_pages=6)
        if tup is not None:
            first_mono = time.monotonic()
            first_wall = datetime.now()
            last_tup = tup
            break
        time.sleep(max(0.2, interval_sec))
    results[key] = {
        "env": env.name,
        "code": code,
        "attempts": n,
        "found": last_tup is not None,
        "first_wall": first_wall,
        "ohlcv": last_tup,
        "elapsed_from_slot": (
            (first_wall - slot).total_seconds() if first_wall is not None else None
        ),
    }


def wait_next_and_poll(
    envs: List[EnvAuth],
    dm: Any,
    codes: List[str],
    *,
    interval_sec: float,
    timeout_sec: float,
) -> None:
    slot = next_slot_end()
    slot_s = slot.strftime("%Y-%m-%d %H:%M:%S")
    now = datetime.now()
    wait_s = (slot - now).total_seconds()
    print(f"\n=== 다음 슬롯 대기 폴링 ===")
    print(f"대상 슬롯: {slot_s}")
    print(f"대기: {wait_s:.1f}초 (마감 직후 {interval_sec}s 간격, 최대 {timeout_sec:.0f}s)")
    if wait_s > 0:
        # 마감 0.2초 전부터 준비
        sleep_for = max(0.0, wait_s - 0.2)
        time.sleep(sleep_for)
        while datetime.now() < slot:
            time.sleep(0.05)

    print(f"폴링 시작 @ {datetime.now().strftime('%H:%M:%S.%f')[:-3]}")
    results: Dict[str, Any] = {}
    threads: List[threading.Thread] = []
    # 환경×종목을 순차로 두면 공정 비교가 안 되므로, 종목당 모의/실전 병렬
    # (config 락으로 API URL 전환은 직렬 → 실제 호출은 교차. 공정성을 위해
    #  종목은 1개씩, 모의·실전만 거의 동시 시작하되 락으로 번갈아 호출됨)
    for code in codes:
        for env in envs:
            key = f"{env.name}:{code}"
            th = threading.Thread(
                target=poll_until_found,
                args=(env, dm, code, slot),
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
            # 실전/모의 시작 시각을 거의 같게
            time.sleep(0.05)
        for th in threads:
            th.join(timeout=timeout_sec + 30)
        threads.clear()

        print(f"\n-- {code} @ {slot_s} --")
        for env in envs:
            r = results.get(f"{env.name}:{code}") or {}
            if not r.get("found"):
                print(
                    f"  [{env.name:4s}] NOT_FOUND "
                    f"(시도 {r.get('attempts', 0)}회, {timeout_sec:.0f}s 내)"
                )
            else:
                fw = r["first_wall"]
                el = r["elapsed_from_slot"]
                print(
                    f"  [{env.name:4s}] FOUND +{el:.1f}s "
                    f"@ {fw.strftime('%H:%M:%S.%f')[:-3]} "
                    f"시도={r.get('attempts')} {fmt_ohlcv(r.get('ohlcv'))}"
                )
        # 요약
        m = results.get(f"MOCK:{code}") or {}
        l = results.get(f"LIVE:{code}") or {}
        if m.get("found") and l.get("found"):
            diff = float(m["elapsed_from_slot"]) - float(l["elapsed_from_slot"])
            faster = "LIVE" if diff > 0 else ("MOCK" if diff < 0 else "동일")
            print(f"  => 모의-실전 지연차: {diff:+.1f}s (더 빠른 쪽: {faster})")
        elif m.get("found") and not l.get("found"):
            print("  => 모의만 발견, 실전 미발견")
        elif l.get("found") and not m.get("found"):
            print("  => 실전만 발견, 모의 미발견")
        else:
            print("  => 양쪽 모두 미발견")


def parse_codes(raw: str) -> List[str]:
    out: List[str] = []
    for p in str(raw or "").split(","):
        c = p.strip().zfill(6)
        if c and c.isdigit():
            out.append(c)
    return out or ["017800", "139480"]


def main() -> int:
    ap = argparse.ArgumentParser(description="모의 vs 실전 15분 공식봉 지연 비교")
    ap.add_argument(
        "--mode",
        choices=("check-last", "wait-next", "both"),
        default="both",
    )
    ap.add_argument("--codes", default="017800,139480")
    ap.add_argument("--interval", type=float, default=1.0, help="폴링 간격(초)")
    ap.add_argument("--timeout", type=float, default=120.0, help="슬롯 후 최대 대기(초)")
    ap.add_argument(
        "--slot",
        default="",
        help="check-last 시 강제 슬롯 'YYYY-MM-DD HH:MM:SS'",
    )
    args = ap.parse_args()
    codes = parse_codes(args.codes)

    # Windows 콘솔
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    mock, live = build_envs()
    if not mock.app_key or not live.app_key:
        print("config에 MOCK/LIVE 키가 없습니다.")
        return 1

    mock.token = get_token(mock)
    live.token = get_token(live)
    if not mock.token or not live.token:
        print("토큰 발급 실패 — 모의·실전 키/권한을 확인하세요.")
        return 1

    from data_manager_v2 import DataManagerV2

    dm = DataManagerV2(db_path=str(ROOT / "trading_data.db"))

    if args.mode in ("check-last", "both"):
        if args.slot.strip():
            slot = datetime.strptime(args.slot.strip()[:19], "%Y-%m-%d %H:%M:%S")
        else:
            slot = last_completed_slot()
        check_slot_now([mock, live], dm, codes, slot)

    if args.mode in ("wait-next", "both"):
        wait_next_and_poll(
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
