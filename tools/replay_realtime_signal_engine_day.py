# -*- coding: utf-8 -*-
"""
``SignalEngine.on_tick_received`` 경로(풀계산·인트라바 미러·슬롯 게이트 포함)를 시뮬레이션합니다.

**드라이브 방식** (``--drive``)

- ``session_ticks`` (기본): ``realtime_session_logs/<DAY>/session_events.jsonl`` 의
  ``ws_real_0b_seen_periodic`` 만 순서대로 재생. (당일 세션에 찍힌 종목·틱 수에 의존)
- ``db_1m_closes``: **실시간 틱 로그 없음.** ``trading_data.db`` 의 공식 **1분봉 종가**만
  시간순으로 넣어 동일 엔진을 돌립니다. 봉 갱신은 엔진 내부의 공식 바 동기화 경로를 따릅니다.

공통:

- ``bars_15m`` / ``bars_1m`` 으로 15m·M1 저장소를 복원합니다.
- 실제 Tk/주문은 호출하지 않고, ``_on_buy_signal_received`` / ``enqueue_tick_main_action`` 만 집계합니다.

사용 예 (프로젝트 루트):

    # 세션 틱 로그 기반
    python tools/replay_realtime_signal_engine_day.py --day 2026-05-14

    # 틱 없이 DB 1분봉만 (동적 감시 종목 = 동적 백테 부트스트랩과 동일)
    python tools/replay_realtime_signal_engine_day.py --day 2026-05-14 --drive db_1m_closes

    # 틱 없이, 감시 종목을 직접 지정
    python tools/replay_realtime_signal_engine_day.py --day 2026-05-14 --drive db_1m_closes --codes 028260,007070

선택: 동일 기간 동적 백테 1영업일 요약:

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


def parse_codes_csv_arg(s: str) -> List[str]:
    """콤마 구분 종목코드 → 6자리 정규화 리스트."""
    out: List[str] = []
    for part in str(s or "").split(","):
        p = str(part).strip()
        if not p:
            continue
        out.append(p.zfill(6))
    return out


def collect_dynamic_monitor_codes_for_replay(
    day: str, settings: Dict[str, Any]
) -> Tuple[List[str], Dict[str, str]]:
    """
    동적 백테스트와 동일한 부트스트랩·당일 감시 리스트(당일 15m 데이터가 있는 종목만).
    """
    from datetime import timedelta

    import strategy_selector
    from dynamic_backtest import (
        DynamicBacktester,
        _dual_track_new_candidates,
        _interleave_track_codes,
    )

    try:
        bt = DynamicBacktester(settings)
        if not bt._bootstrap_portfolio_data(day, day, None):
            print("[replay] 동적 유니버스 부트스트랩 실패")
            return [], {}
        trading_days: List[str] = list(getattr(bt, "_bt_trading_days", None) or [])
        if day not in trading_days:
            print(f"[replay] 거래일 목록에 없음: {day} in {trading_days[:5]}...")
            return [], {}
        idx = trading_days.index(day)
        prev_day_idx = idx - 1
        ref_day = (
            trading_days[prev_day_idx]
            if prev_day_idx >= 0
            else (getattr(bt, "_bt_ref_day_for_first", None) or day)
        )
        top_codes_a = bt.schedule_a.get(ref_day, [])
        top_codes_b = bt.schedule_b.get(ref_day, [])
        top_codes = _interleave_track_codes(top_codes_a, top_codes_b)
        track_map: Dict[str, str] = {}
        for c in top_codes_a:
            track_map[str(c)] = "A"
        for c in top_codes_b:
            ck = str(c)
            if ck in track_map:
                track_map[ck] = "AB"
            else:
                track_map[ck] = "B"
        if not top_codes:
            prev_day_dt = datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)
            candidates_df = strategy_selector.select_monitoring_targets(
                bt.settings,
                logger_callback=lambda x: None,
                target_date=prev_day_dt,
                data_manager=bt.db,
            )
            if candidates_df is not None and not candidates_df.empty:
                max_fb = int(bt.settings.get("max_slots_a", 20)) + int(
                    bt.settings.get("max_slots_b", 10)
                )
                use_b_fb = bool(bt.settings.get("use_sel_track_b", True))
                for _, row in candidates_df.head(max_fb).iterrows():
                    c = str(row["Code"])
                    top_codes.append(c)
                    if use_b_fb and row.get("Valid_B", False):
                        track_map[c] = (
                            "B" if row.get("Valid_A", True) is False else "AB"
                        )
                    else:
                        track_map.setdefault(c, "A")
        max_sa = int(bt.settings.get("max_slots_a", 20))
        max_sb = int(bt.settings.get("max_slots_b", 10))
        holdings_codes = list(bt.holdings.keys())
        if top_codes_a or top_codes_b:
            new_candidates = _dual_track_new_candidates(
                top_codes_a, top_codes_b, holdings_codes, max_sa, max_sb
            )
        else:
            hc_set = set(holdings_codes)
            new_candidates = [
                c for c in top_codes if c not in hc_set
            ][: max_sa + max_sb]
        monitoring_list = holdings_codes + new_candidates
        out_codes: List[str] = []
        d1 = f"{day} 23:59:59"
        for c in monitoring_list:
            if c not in bt.cached_15m_data:
                continue
            try:
                day_full = bt.cached_15m_data[c].loc[day:d1]
            except Exception:
                continue
            if day_full is None or getattr(day_full, "empty", True):
                continue
            out_codes.append(str(c).zfill(6))
        return out_codes, track_map
    except Exception as e:
        print(f"[replay] collect_dynamic_monitor_codes_for_replay 실패: {e!r}")
        return [], {}


def build_db_1m_close_events(
    day: str, codes_ordered: List[str]
) -> List[Tuple[datetime, str, float, int]]:
    """당일 공식 1분봉 종가·거래량을 (시각, 종목) 기준 시간순 이벤트로 병합."""
    events: List[Tuple[datetime, str, float, int]] = []
    for c in codes_ordered:
        df1 = load_1m_bars_day(c, day)
        if df1 is None or getattr(df1, "empty", True):
            continue
        for _, row in df1.iterrows():
            try:
                ts = pd.Timestamp(row["dt"]).to_pydatetime()
            except Exception:
                continue
            try:
                px = float(row.get("c", row.get("close", 0)) or 0)
            except (TypeError, ValueError):
                px = 0.0
            if px <= 0:
                continue
            try:
                vol = int(float(row.get("v", row.get("volume", 0)) or 0))
            except (TypeError, ValueError):
                vol = 0
            events.append((ts, str(c).zfill(6), px, max(vol, 1)))
    events.sort(key=lambda x: (x[0], x[1]))
    return events


def seed_monitor_signal_caches(
    settings: Dict[str, Any],
    code: str,
    df15: pd.DataFrame,
    stream_bar_count: int,
    sel_track: Optional[str] = None,
) -> Dict[str, Any]:
    """워밍업과 동일하게 last_row_cache / last_sig_cache 시드."""
    from backtest_engine import TradingStrategy

    _dft = str(settings.get("default_sel_track", "AB") or "AB")
    _tr = str(sel_track).strip().upper() if sel_track else _dft
    if _tr not in ("A", "B", "AB"):
        _tr = "AB"
    row: Dict[str, Any] = {
        "name": code,
        "sel_track": _tr,
        "SelTrack": _tr,
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
    ap = argparse.ArgumentParser(
        description="SignalEngine 리플레이: 세션 틱 또는 DB 1분봉 종가만"
    )
    ap.add_argument(
        "--day",
        type=str,
        default=datetime.now().strftime("%Y-%m-%d"),
        help="YYYY-MM-DD (기본: 오늘)",
    )
    ap.add_argument(
        "--drive",
        type=str,
        choices=("session_ticks", "db_1m_closes"),
        default="session_ticks",
        help="session_ticks=세션 JSONL WS 틱, db_1m_closes=DB 공식 1분 종가만(실시간 틱 로그 불필요)",
    )
    ap.add_argument(
        "--codes",
        type=str,
        default="",
        help="db_1m_closes 일 때만: 콤마 구분 종목. 비우면 동적 백테와 동일 감시 리스트",
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
        help="이벤트 상한(균등 샘플링, session_ticks·db_1m 공통). 0이면 전체",
    )
    ap.add_argument(
        "--dynamic-bt",
        action="store_true",
        help="동일 일자 동적 백테(고정밀) 1회 실행 후 매수 건수 출력",
    )
    args = ap.parse_args()
    day = str(args.day).strip()[:10]
    day_end = f"{day} 23:59:59"

    settings = dict(load_settings())
    settings["realtime_s1_full_calc_min_interval_sec"] = 1.0

    drive = str(getattr(args, "drive", "session_ticks") or "session_ticks").strip()
    track_map: Dict[str, str] = {}
    ticks: List[Tuple[datetime, str, float, int]] = []
    codes_ordered: List[str] = []

    if drive == "db_1m_closes":
        codes_arg = parse_codes_csv_arg(str(getattr(args, "codes", "") or ""))
        if codes_arg:
            codes_ordered = codes_arg
            track_map = {c: "AB" for c in codes_ordered}
        else:
            codes_ordered, track_map = collect_dynamic_monitor_codes_for_replay(
                day, settings
            )
        if not codes_ordered:
            print(
                "[replay] db_1m_closes: 감시 종목이 없습니다. "
                "--codes 로 지정하거나 동적 스케줄·DB를 확인하세요."
            )
            return
        ticks = build_db_1m_close_events(day, codes_ordered)
        if not ticks:
            print("[replay] db_1m_closes: 해당 일자·종목에 1분봉이 없습니다.")
            return
        print("[replay] 드라이브=db_1m_closes (session_events·실시간 틱 미사용)")
        print(
            f"[replay] 일자={day} 감시종목={len(codes_ordered)} "
            f"{codes_ordered[:20]}{'...' if len(codes_ordered) > 20 else ''}"
        )
    else:
        ticks, _tick_codes_all = parse_session_ticks(day)
        if not ticks:
            print(
                "[replay] 유효 틱이 없습니다. "
                "해당 일자 session_events.jsonl 의 ws_real_0b_seen_periodic 을 확인하거나, "
                "--drive db_1m_closes 로 DB 1분봉만 재생하세요."
            )
            return
        print("[replay] 드라이브=session_ticks (세션 JSONL)")
        tick_codes = {c for _, c, _, _ in ticks}
        codes_ordered = sorted(tick_codes)
        print(
            f"[replay] 일자={day} 감시종목(틱 기준)={len(codes_ordered)} "
            f"{codes_ordered[:15]}{'...' if len(codes_ordered) > 15 else ''}"
        )

    if int(args.max_ticks) > 0:
        n0 = len(ticks)
        ticks = subsample_ticks(ticks, int(args.max_ticks))
        print(
            f"[replay] 이벤트 샘플링: {n0} -> {len(ticks)} (--max-ticks={int(args.max_ticks)})"
        )
    print(f"[replay] 이벤트 수={len(ticks)}")

    bars_data: Dict[str, pd.DataFrame] = {}
    m1_counts: Dict[str, int] = {}
    from core.rt_tick_m1_store import RtTickM1Store

    m1_store = RtTickM1Store(max_completed_per_code=48, logger_callback=None)
    for c in codes_ordered:
        df15 = load_15m_bars(c, day_end)
        if df15.empty:
            print(f"[replay] 경고: 15m 없음 -> 스킵 {c}")
            continue
        if int(args.tail_15m) > 0 and len(df15) > int(args.tail_15m):
            df15 = df15.tail(int(args.tail_15m)).copy()
        bars_data[c] = df15
        df1 = load_1m_bars_day(c, day)
        m1_counts[c] = seed_m1_store(m1_store, c, df1)

    if not bars_data:
        print("[replay] 15m 로드된 종목이 없습니다.")
        return

    # SignalEngine 은 bars_data 의 **마지막 행**으로 슬롯 키·지표 iloc[-1] 을 본다.
    # 장종료까지 통째로 넣으면 항상 '당일 마지막 15분봉'만 보게 되어
    # 오전·오후 신호가 백테와 어긋난다. 리플레이 시각 ts 까지의 봉만 남긴다.
    bars_data_full_backup: Dict[str, pd.DataFrame] = {
        str(k): (v.copy() if v is not None else v) for k, v in bars_data.items()
    }

    monitor_info: Dict[str, Any] = {}
    for c in bars_data.keys():
        st = track_map.get(c) if track_map else None
        monitor_info[c] = seed_monitor_signal_caches(
            settings, c, bars_data[c], len(bars_data[c]), sel_track=st
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

    # SignalEngine 은 datetime.now() 로 정규장 여부를 검사한다.
    # 리플레이를 장외에 돌리면 매수가 전부 막히므로, 엔진 모듈에 바인딩된
    # is_krx_regular_session 만 리플레이 구간 동안 항상 True 로 둔다.
    _orig_krx_session = se_mod.is_krx_regular_session

    def _replay_session_always_open(_dt: Any = None) -> bool:
        return True

    se_mod.is_krx_regular_session = _replay_session_always_open  # type: ignore[method-assign]

    try:
        for ts, c, px, vol in ticks:
            if c not in bars_data:
                continue
            _fake._v += 1.05
            try:
                full_df = bars_data_full_backup.get(c)
                if full_df is not None and not getattr(full_df, "empty", True):
                    tts = pd.Timestamp(ts)
                    sliced = full_df.loc[full_df.index <= tts].copy()
                    if not getattr(sliced, "empty", True):
                        app.bars_data[c] = sliced
            except (TypeError, ValueError, KeyError) as e:
                try:
                    print(f"[replay] bars_data 슬라이스 실패 ({c} @ {ts}): {e!r}")
                except Exception:
                    pass
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
        se_mod.is_krx_regular_session = _orig_krx_session  # type: ignore[method-assign]

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

    if getattr(args, "dynamic_bt", False):
        print()
        run_dynamic_bt_one_day(day, load_settings())


if __name__ == "__main__":
    main()
