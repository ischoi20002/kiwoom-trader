# -*- coding: utf-8 -*-
"""
지정 일자의 ``realtime_session_logs/<DAY>/session_events.jsonl`` 틱 이벤트를 순서대로 재생하여
``SignalEngine.on_tick_received`` 경로(풀계산·인트라바 미러·슬롯 게이트 포함)를 시뮬레이션합니다.

- ``trading_data.db`` 의 ``bars_15m`` / ``bars_1m`` 으로 감시 종목 봉·M1 저장소를 복원합니다.
- 실제 Tk/주문은 호출하지 않고, ``_on_buy_signal_received`` / ``enqueue_tick_main_action`` 만 집계합니다.

사용 예 (프로젝트 루트):

    python tools/replay_realtime_signal_engine_day.py --day 2026-05-14

틱이 많으면 균등 샘플링(기본 4000건):

    python tools/replay_realtime_signal_engine_day.py --day 2026-05-14 --max-ticks 4000

선택: 동일 기간 동적 백테 1영업일 요약(시간 소요 가능):

    python tools/replay_realtime_signal_engine_day.py --day 2026-05-14 --dynamic-bt
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import types
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SETTINGS_PATH = ROOT / "settings.json"
TRADING_DB = ROOT / "trading_data.db"
SESSION_DIR = ROOT / "realtime_session_logs"


@dataclass
class MockWatchlistStore:
    """감시 종목만 관심목록에 있는 것으로 간주(wl_only 비활성)."""

    codes: Set[str]

    def codes_set(self) -> Set[str]:
        return set(self.codes)

    def get_avg_price(self, code: str) -> Optional[float]:
        return None

    def get_display_name(self, code: str) -> str:
        return str(code)


@dataclass
class MockTradingController:
    monitor_codes: List[str]
    monitor_info: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MockPortfolio:
    virtual_positions: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MockApp:
    settings: Dict[str, Any]
    bars_data: Dict[str, Any]
    monitor_info: Dict[str, Any]
    virtual_positions: Dict[str, Any]
    portfolio: MockPortfolio
    watchlist_store: MockWatchlistStore
    rt_tick_m1_store: Any
    rsi_signals: Dict[str, Any]
    trading_controller: MockTradingController
    strategy_engine: Any = None
    session_logger: Any = None
    log: Callable[[str], None] = print
    pending_tree_updates: Dict[str, Any] = field(default_factory=dict)
    _bars_data_lock: Any = None
    _update_timer_active: bool = False

    def __post_init__(self) -> None:
        self._bars_data_lock = None
        self.trading_controller.monitor_info = self.monitor_info

    def enqueue_tick_main_action(self, kind: str, payload: Any) -> None:
        return

    def _on_buy_signal_received(self, *args: Any, **kwargs: Any) -> bool:
        return True

    def _on_strategy_action_needed(self, *args: Any, **kwargs: Any) -> None:
        return

    def _ensure_vp_monitor_stub(self, code: str) -> None:
        return

    def mark_tick_worker_ui_dirty(self) -> None:
        return

    def ensure_monitor_tree_refresh_scheduled(self, *args: Any, **kwargs: Any) -> None:
        return

    def send_telegram_message(self, *args: Any, **kwargs: Any) -> None:
        return


def load_settings() -> Dict[str, Any]:
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_15m_bars(code: str, day_end: str) -> pd.DataFrame:
    """day_end 까지(포함) 확정 15분봉."""
    try:
        con = sqlite3.connect(str(TRADING_DB))
        try:
            df = pd.read_sql_query(
                """
                SELECT datetime AS dt, open AS Open, high AS High, low AS Low,
                       close AS Close, volume AS Volume
                FROM bars_15m
                WHERE code = ? AND datetime <= ?
                ORDER BY datetime
                """,
                con,
                params=(str(code).zfill(6), day_end),
            )
        finally:
            con.close()
    except Exception as e:
        print(f"[replay] bars_15m 로드 실패 ({code}): {e}")
        return pd.DataFrame()
    if df.empty:
        return pd.DataFrame()
    df["datetime"] = pd.to_datetime(df["dt"], errors="coerce")
    df = df.dropna(subset=["datetime"]).set_index("datetime").drop(columns=["dt"])
    return df


def load_1m_bars_day(code: str, day: str) -> pd.DataFrame:
    d0 = f"{day} 00:00:00"
    d1 = f"{day} 23:59:59"
    try:
        con = sqlite3.connect(str(TRADING_DB))
        try:
            df = pd.read_sql_query(
                """
                SELECT datetime AS dt, open AS o, high AS h, low AS l,
                       close AS c, volume AS v
                FROM bars_1m
                WHERE code = ? AND datetime >= ? AND datetime <= ?
                ORDER BY datetime
                """,
                con,
                params=(str(code).zfill(6), d0, d1),
            )
        finally:
            con.close()
    except Exception:
        return pd.DataFrame()
    if df.empty:
        return pd.DataFrame()
    df["dt"] = pd.to_datetime(df["dt"], errors="coerce")
    return df.dropna(subset=["dt"])


def seed_m1_store(store: Any, code: str, df1: pd.DataFrame) -> int:
    """완료 1분을 순서대로 ``RtTickM1Store`` 에 반영."""
    from core.rt_tick_m1_store import RtTickM1Store

    assert isinstance(store, RtTickM1Store)
    nk = str(code).zfill(6)
    n = 0
    for _, row in df1.iterrows():
        try:
            ol = pd.Timestamp(row["dt"]).floor("min")
            store.apply_official_closed_minute(
                nk,
                ol,
                float(row["o"]),
                float(row["h"]),
                float(row["l"]),
                float(row["c"]),
                float(row["v"] or 0),
            )
            n += 1
        except Exception:
            continue
    return n


def subsample_ticks(
    ticks: List[Tuple[datetime, str, float, int]], max_n: int
) -> List[Tuple[datetime, str, float, int]]:
    """틱이 과다할 때 균등 샘플링(시간 순서 유지)."""
    if max_n <= 0 or len(ticks) <= max_n:
        return ticks
    try:
        step = max(1.0, float(len(ticks)) / float(max_n))
        out: List[Tuple[datetime, str, float, int]] = []
        i = 0.0
        taken = 0
        while int(i) < len(ticks) and taken < max_n:
            out.append(ticks[int(i)])
            i += step
            taken += 1
        return out
    except Exception:
        return ticks[:max_n]


def parse_session_ticks(day: str) -> Tuple[List[Tuple[datetime, str, float, int]], Set[str]]:
    """
    session_events.jsonl 에서 ``ws_real_0b_seen_periodic`` 틱만 추출.
    (다른 kind 는 가격이 없거나 형식이 달라 제외)
    """
    path = SESSION_DIR / day / "session_events.jsonl"
    ticks: List[Tuple[datetime, str, float, int]] = []
    codes: Set[str] = set()
    if not path.is_file():
        print(f"[replay] 세션 JSONL 없음: {path}")
        return ticks, codes
    with open(path, "r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if str(o.get("kind", "")) != "ws_real_0b_seen_periodic":
                continue
            data = o.get("data") or {}
            c = str(data.get("code", "")).zfill(6)
            if len(c) != 6:
                continue
            try:
                ts = datetime.strptime(str(o.get("ts", ""))[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            try:
                px = float(data.get("now_price", 0) or 0)
            except (TypeError, ValueError):
                px = 0.0
            if px <= 0:
                continue
            try:
                vol = int(float(data.get("volume", 0) or 0))
            except (TypeError, ValueError):
                vol = 0
            ticks.append((ts, c, px, max(vol, 1)))
            codes.add(c)
    ticks.sort(key=lambda x: x[0])
    return ticks, codes


def seed_monitor_signal_caches(
    settings: Dict[str, Any],
    code: str,
    df15: pd.DataFrame,
    stream_bar_count: int,
) -> Dict[str, Any]:
    """워밍업과 동일하게 last_row_cache / last_sig_cache 시드."""
    from backtest_engine import TradingStrategy

    row: Dict[str, Any] = {
        "name": code,
        "sel_track": str(settings.get("default_sel_track", "AB") or "AB"),
        "SelTrack": str(settings.get("default_sel_track", "AB") or "AB"),
        "open": float(df15.iloc[-1]["Open"]) if len(df15) else 0.0,
        "vol_power": 100.0,
        "total_ask": 1.0,
        "total_bid": 1.0,
    }
    if df15 is None or getattr(df15, "empty", True):
        return row
    try:
        df_ind = TradingStrategy.calculate_indicators(df15, settings)
        detailed = TradingStrategy.get_buy_signals_detailed(
            df_ind, settings, "UP"
        )
        if detailed is None or getattr(detailed, "empty", True):
            return row
        last_ser = detailed.iloc[-1]
        last_sig = (
            last_ser.to_dict()
            if hasattr(last_ser, "to_dict")
            else dict(last_ser)
        )
        row["last_detailed_sig"] = detailed
        row["last_bar_count"] = int(stream_bar_count)
        row["last_row_cache"] = df_ind.iloc[-1].to_dict()
        row["last_sig_cache"] = last_sig
    except Exception as e:
        print(f"[replay] 시드 실패 ({code}): {e}")
    return row


def run_dynamic_bt_one_day(day: str, settings: Dict[str, Any]) -> None:
    """동적 백테 1일(고정밀) — 매수 건수 요약만 출력."""
    try:
        from dynamic_backtest import DynamicBacktester
    except Exception as e:
        print(f"[replay] DynamicBacktester 임포트 실패: {e}")
        return
    try:
        bt = DynamicBacktester(settings)
        bt.high_precision = True
        ok = bt.run(day, day, interval="15m", callback=None, high_precision=True)
        if not ok:
            print("[replay] 동적 백테 run 실패(부트스트랩 등)")
            return
        buys = [
            t
            for t in (bt.trades_history or [])
            if str(t.get("Action", "")).upper().startswith("BUY")
        ]
        print(f"[동적 백테 {day}] 매수 체결 기록: {len(buys)}건")
        for t in buys[:20]:
            print(
                f"  - {t.get('Code')} {t.get('Action')} "
                f"@ {t.get('Price')} {t.get('Reason', '')[:48]}"
            )
        if len(buys) > 20:
            print(f"  ... 외 {len(buys) - 20}건")
    except Exception as e:
        print(f"[replay] 동적 백테 실행 예외: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="실시간 세션 JSONL → SignalEngine 리플레이")
    ap.add_argument(
        "--day",
        type=str,
        default=datetime.now().strftime("%Y-%m-%d"),
        help="YYYY-MM-DD (기본: 오늘)",
    )
    ap.add_argument(
        "--tail-15m",
        type=int,
        default=900,
        help="리플레이용 15분봉 최대 행 수(끝에서 슬라이스). 0이면 전체(느릴 수 있음)",
    )
    ap.add_argument(
        "--max-ticks",
        type=int,
        default=4000,
        help="틱 상한(균등 샘플링). 0이면 전체(매우 느릴 수 있음)",
    )
    ap.add_argument(
        "--dynamic-bt",
        action="store_true",
        help="동일 일자 동적 백테(고정밀) 1회 실행 후 매수 건수 출력",
    )
    args = ap.parse_args()
    day = str(args.day).strip()[:10]
    day_end = f"{day} 23:59:59"

    settings = load_settings()
    ticks, _tick_codes_all = parse_session_ticks(day)
    if not ticks:
        print(
            "[replay] 유효 틱이 없습니다. "
            "해당 일자 session_events.jsonl 의 ws_real_0b_seen_periodic 를 확인하세요."
        )
        return

    if int(args.max_ticks) > 0:
        n0 = len(ticks)
        ticks = subsample_ticks(ticks, int(args.max_ticks))
        print(f"[replay] 틱 샘플링: {n0} → {len(ticks)} (--max-ticks={int(args.max_ticks)})")

    tick_codes = {c for _, c, _, _ in ticks}
    codes_ordered = sorted(tick_codes)
    print(f"[replay] 일자={day} 감시종목(틱 기준)={len(codes_ordered)} {codes_ordered[:15]}{'...' if len(codes_ordered) > 15 else ''}")
    print(f"[replay] 틱 수={len(ticks)}")

    # 리플레이: 가짜 time.time 스텝과 맞춰 풀계산 스로틀(실시간과 동일 분기, 부하 완화)
    settings = dict(settings)
    settings["realtime_s1_full_calc_min_interval_sec"] = 1.0

    bars_data: Dict[str, pd.DataFrame] = {}
    m1_counts: Dict[str, int] = {}
    from core.rt_tick_m1_store import RtTickM1Store

    m1_store = RtTickM1Store(max_completed_per_code=48, logger_callback=None)
    for c in codes_ordered:
        df15 = load_15m_bars(c, day_end)
        if df15.empty:
            print(f"[replay] 경고: 15m 없음 → 스킵 {c}")
            continue
        if int(args.tail_15m) > 0 and len(df15) > int(args.tail_15m):
            df15 = df15.tail(int(args.tail_15m)).copy()
        bars_data[c] = df15
        df1 = load_1m_bars_day(c, day)
        m1_counts[c] = seed_m1_store(m1_store, c, df1)

    monitor_info: Dict[str, Any] = {}
    for c in bars_data.keys():
        monitor_info[c] = seed_monitor_signal_caches(
            settings, c, bars_data[c], len(bars_data[c])
        )

    tc = MockTradingController(monitor_codes=list(bars_data.keys()))
    tc.monitor_info = monitor_info
    app = MockApp(
        settings=settings,
        bars_data=bars_data,
        monitor_info=monitor_info,
        virtual_positions={},
        portfolio=MockPortfolio(virtual_positions={}),
        watchlist_store=MockWatchlistStore(set(bars_data.keys())),
        rt_tick_m1_store=m1_store,
        rsi_signals={},
        trading_controller=tc,
        strategy_engine=None,
        session_logger=None,
    )
    from core.strategy_engine import StrategyEngine as SE

    app.strategy_engine = SE(app)

    from core.signal_engine import SignalEngine

    engine = SignalEngine(app)
    buy_calls: List[Dict[str, Any]] = []
    strat_actions: List[Dict[str, Any]] = []

    def _rec_buy(*a: Any, **kw: Any) -> bool:
        buy_calls.append({"args": a, "kwargs": kw})
        return True

    def _rec_strat(code: str, action: str, reason: str, **kw: Any) -> None:
        strat_actions.append({"code": code, "action": action, "reason": reason, **kw})

    app._on_buy_signal_received = _rec_buy  # type: ignore[method-assign]
    app.enqueue_tick_main_action = (  # type: ignore[method-assign]
        lambda *a, **k: strat_actions.append({"kind": a[0] if a else "", "payload": a[1] if len(a) > 1 else None})
    )
    app._on_strategy_action_needed = _rec_strat  # type: ignore[method-assign]

    import core.signal_engine as se_mod

    _real_time_mod = se_mod.time
    _orig_time_fn = _real_time_mod.time
    _fake = types.SimpleNamespace()
    _fake._v = 1_700_000_000.0

    def _fake_time() -> float:
        return float(_fake._v)

    se_mod.time.time = _fake_time  # type: ignore[method-assign]

    try:
        for ts, c, px, vol in ticks:
            if c not in bars_data:
                continue
            _fake._v += 1.05
            try:
                engine.on_tick_received(
                    c,
                    float(px),
                    int(vol),
                    0.0,
                    defer_main_callbacks=False,
                    ts_wall=ts.strftime("%Y-%m-%d %H:%M:%S.%f"),
                )
            except Exception as e:
                print(f"[replay] on_tick 예외 {c} {ts}: {e}")
    finally:
        se_mod.time.time = _orig_time_fn  # type: ignore[method-assign]

    print()
    print("=== SignalEngine 리플레이 결과 ===")
    print(f"S1 매수 콜백(_on_buy_signal_received) 호출: {len(buy_calls)}회")
    for b in buy_calls[:15]:
        a = b.get("args") or ()
        if len(a) >= 1:
            print(f"  - code={a[0]} price={a[1] if len(a) > 1 else '-'}")
    if len(buy_calls) > 15:
        print(f"  ... 외 {len(buy_calls) - 15}회")
    print(f"enqueue_tick_main_action 누적: {len(strat_actions)}건 (buy_signal/strategy_action 등)")
    print(f"bars_15m 로드 종목: {list(bars_data.keys())}")
    print(f"bars_1m 시드 분 수(종목별): {m1_counts}")

    if args.dynamic_bt:
        print()
        run_dynamic_bt_one_day(day, load_settings())


if __name__ == "__main__":
    main()
