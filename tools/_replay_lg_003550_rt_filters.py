# -*- coding: utf-8 -*-
"""
003550 LG — 2026-05-12 백테 S1(09:30 봉) 시점에 가깝게,
StreamProcessor 봉 적재 + TradingStrategy 지표/신호 + 실시간과 동일한 게이트(Ready) 판정을 재현합니다.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CODE = "003550"
DAY = "2026-05-12"
BAR_CLOSE = pd.Timestamp(f"{DAY} 09:30:00")  # DB 라벨(우측) = 09:15~09:30 구간 종가 시각
TICK_PATH = ROOT / "realtime_tick_logs" / f"{DAY}_trades_mock.jsonl"
SETTINGS_PATH = ROOT / "settings.json"


def slot_end_ts(dt: datetime) -> datetime:
    h, m = dt.hour, dt.minute
    tgt_m = ((m // 15) + 1) * 15
    th = h
    if tgt_m == 60:
        tgt_m = 0
        th = h + 1
    return dt.replace(hour=th, minute=tgt_m, second=0, microsecond=0)


def load_settings() -> dict:
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_db_bars_upto(end_ts: str) -> pd.DataFrame:
    con = sqlite3.connect(str(ROOT / "trading_data.db"))
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
            params=(CODE, end_ts),
        )
    finally:
        con.close()
    if df.empty:
        return pd.DataFrame()
    df["datetime"] = pd.to_datetime(df["dt"], errors="coerce")
    df = df.dropna(subset=["datetime"]).set_index("datetime").drop(columns=["dt"])
    return df


def db_slice_before_bar_close() -> pd.DataFrame:
    """09:30 라벨 '완성' 직전까지의 확정봉만(해당 라벨 행 제외)."""
    all_upto = load_db_bars_upto(f"{DAY} 23:59:59")
    return all_upto[all_upto.index < BAR_CLOSE]


def db_bar_at_close() -> pd.Series:
    all_upto = load_db_bars_upto(f"{DAY} 23:59:59")
    return all_upto.loc[BAR_CLOSE]


def ticks_in_slot(slot_end: pd.Timestamp) -> list[dict]:
    """slot_end 라벨 구간에 속하는 틱(ts_wall 초 단위)."""
    slot_start = slot_end - pd.Timedelta(minutes=15)
    out: list[dict] = []
    for line in TICK_PATH.open("r", encoding="utf-8"):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if str(o.get("code") or "").zfill(6) != CODE:
            continue
        ts = datetime.strptime(str(o["ts_wall"])[:19], "%Y-%m-%d %H:%M:%S")
        if ts < slot_start.to_pydatetime() or ts >= slot_end.to_pydatetime():
            continue
        out.append(o)
    out.sort(key=lambda x: x["ts_wall"])
    return out


def ticks_to_partial_bar(rows: list[dict]) -> pd.DataFrame:
    """StreamProcessor 슬롯 라벨 = slot_end_ts(ts)."""
    if not rows:
        return pd.DataFrame()
    prev_cum = None
    o = h = l = c = None
    vol = 0.0
    for r in rows:
        ts = datetime.strptime(str(r["ts_wall"])[:19], "%Y-%m-%d %H:%M:%S")
        price = float(r.get("price") or 0)
        cum = float(r.get("cum_volume") or 0)
        if o is None:
            o = h = l = c = price
            if prev_cum is not None and cum >= prev_cum:
                vol += cum - prev_cum
            elif prev_cum is not None and cum < prev_cum:
                vol += max(0.0, cum)
        else:
            h = max(h, price)
            l = min(l, price)
            c = price
            if prev_cum is not None and cum >= prev_cum:
                vol += cum - prev_cum
            elif prev_cum is not None and cum < prev_cum:
                vol += max(0.0, cum)
        prev_cum = cum
    end = pd.Timestamp(
        slot_end_ts(datetime.strptime(str(rows[0]["ts_wall"])[:19], "%Y-%m-%d %H:%M:%S"))
    )
    return pd.DataFrame(
        [
            {
                "datetime": end,
                "Open": o,
                "High": h,
                "Low": l,
                "Close": c,
                "Volume": max(0.0, round(vol)),
            }
        ]
    ).set_index("datetime")


def main() -> None:
    settings = load_settings()
    from core.defaults import effective_min_15m_bars_for_monitoring
    from backtest_engine import TradingStrategy

    min_bars = int(effective_min_15m_bars_for_monitoring(settings))
    gate_on = bool(settings.get("realtime_use_fixed_s1_presignal_gate", True))
    causal_on = bool(settings.get("realtime_causal_s1_enabled", False))

    hist = load_db_bars_upto(f"{DAY} 23:59:59")
    hist_before = hist[hist.index < BAR_CLOSE]
    n_hist = len(hist_before)
    bar_db = hist.loc[BAR_CLOSE]

    rows = ticks_in_slot(BAR_CLOSE)
    partial = ticks_to_partial_bar(rows)

    print("=== 입력 ===")
    print("BAR_CLOSE(DB 라벨):", BAR_CLOSE)
    print("이 구간 틱 수:", len(rows), "/ 첫틱·마지막틱:", rows[0]["ts_wall"] if rows else "-", rows[-1]["ts_wall"] if rows else "-")
    print("min_15m_bars_for_signal(유효):", min_bars)
    print("realtime_use_fixed_s1_presignal_gate:", gate_on)
    print("realtime_causal_s1_enabled:", causal_on)
    print()
    print("DB 확정봉만(09:30 미포함) 개수:", n_hist, "→ bars_ready:", n_hist >= min_bars)
    print()
    print("DB 09:30 라벨 완성봉 OHLCV:", int(bar_db["Open"]), int(bar_db["High"]), int(bar_db["Low"]), int(bar_db["Close"]), int(bar_db["Volume"]))
    if partial.empty:
        print("틱 부분봉 없음")
        return
    pr = partial.iloc[0]
    print("틱 재구성 동일슬롯 OHLCV:", int(pr["Open"]), int(pr["High"]), int(pr["Low"]), int(pr["Close"]), int(pr["Volume"]))
    print()

    # A) 백테와 동일: DB 완성 09:30 봉까지 붙인 DF
    df_bt = pd.concat([hist_before, pd.DataFrame([bar_db])]).sort_index()
    df_bt = TradingStrategy.calculate_indicators(df_bt, settings)
    sig_bt = TradingStrategy.get_buy_signals_detailed(df_bt, settings, "UP")
    last_bt = sig_bt.iloc[-1]
    print("=== A) DB 완성봉 기준(백테스트와 동일한 OHLCV) ===")
    print("Signal:", bool(last_bt["Signal"]), "Sig_Type:", last_bt["Sig_Type"], "Met_Ratio:", float(last_bt["Met_Ratio"]))
    print("'Ready' 열 존재:", "Ready" in sig_bt.columns)
    print()

    # B) 실시간 유사: 확정봉 + 틱으로 만든 동일 슬롯 미완성봉(틱 전부 반영)
    df_rt = pd.concat([hist_before, partial]).sort_index()
    df_rt = TradingStrategy.calculate_indicators(df_rt, settings)
    sig_rt = TradingStrategy.get_buy_signals_detailed(df_rt, settings, "UP")
    last_rt = sig_rt.iloc[-1]
    print("=== B) 확정봉 + 틱부분봉(실시간이 보던 OHLCV에 가깝게) ===")
    print("Signal:", bool(last_rt["Signal"]), "Sig_Type:", last_rt["Sig_Type"], "Met_Ratio:", float(last_rt["Met_Ratio"]))
    print()

    # C) 실시간 게이트(캐시 경로): SignalEngine._s1_presignal_ready_for_gate 와 동일
    def _ready_for_gate(last_sig: dict) -> bool:
        if "Ready" not in last_sig:
            return bool(last_sig.get("Signal", False))
        return bool(last_sig.get("Ready", False))

    last_sig_dict_bt = last_bt.to_dict() if hasattr(last_bt, "to_dict") else dict(last_bt)
    gate_ready_bt = _ready_for_gate(last_sig_dict_bt)
    gate_signal_bt = bool(last_sig_dict_bt.get("Signal", False))
    gate_pass_bt = gate_signal_bt and gate_ready_bt

    last_sig_dict_rt = last_rt.to_dict() if hasattr(last_rt, "to_dict") else dict(last_rt)
    gate_ready_rt = _ready_for_gate(last_sig_dict_rt)
    gate_signal_rt = bool(last_sig_dict_rt.get("Signal", False))
    gate_pass_rt = gate_signal_rt and gate_ready_rt

    print("=== C) SignalEngine 캐시 경로 presignal 게이트(수정 후 로직) ===")
    print("DB완성 Signal:", gate_signal_bt, "게이트용 Ready:", gate_ready_bt, "(키 없으면 Signal 동기)", "→ gate 통과:", gate_pass_bt)
    print("틱부분 Signal:", gate_signal_rt, "게이트용 Ready:", gate_ready_rt, "→ gate 통과:", gate_pass_rt)
    print()

    # D) 인과 S1: 엔진과 동일하게 tail lookback 후 마지막 행 신호 + 전체 길이로 Ready
    def eval_causal_s1(bars_df: pd.DataFrame, label: str) -> None:
        lb = int(float(settings.get("realtime_causal_s1_lookback_bars", 400)))
        lb = max(80, min(lb, 2500))
        ready_min = max(1, int(float(settings.get("hp_causal_ready_min_bar_index", 19))))
        tail = bars_df.iloc[max(0, len(bars_df) - lb) :].copy()
        df_ind = TradingStrategy.calculate_indicators(tail, settings)
        sigd = TradingStrategy.get_buy_signals_detailed(df_ind, settings, "UP")
        last_s = sigd.iloc[-1]
        last_sig_row = last_s.to_dict() if hasattr(last_s, "to_dict") else dict(last_s)
        global_i = max(0, int(len(bars_df)) - 1)
        ready_ok = int(global_i) >= int(ready_min)
        sig_on = bool(last_sig_row.get("Signal", False)) and bool(ready_ok)
        print(f"=== D) 인과 S1 동일 로직 ({label}) ===")
        print("tail 길이:", len(tail), "/ 전체봉수 global_i:", global_i, "/ ready_min:", ready_min, "→ ready_ok:", ready_ok)
        print(
            "tail 마지막행 Signal:",
            bool(last_sig_row.get("Signal", False)),
            "Sig_Type:",
            last_sig_row.get("Sig_Type"),
            "Met_Ratio:",
            float(last_sig_row.get("Met_Ratio", 0.0)),
        )
        print("인과 S1 is_buy(sig_on):", sig_on)
        print()

    eval_causal_s1(df_bt, "DB완성+히스토리")
    eval_causal_s1(df_rt, "틱부분봉+히스토리")

    print("=== E) 풀계산(need_full_calc=True) 분기 요약 ===")
    print(
        "풀계산 경로(1078~1176)에는 고정 프리시그널 게이트 블록이 없고, "
        "is_buy = last_sig.Signal 만 적용된 뒤 인과 S1이 선택적으로 덮어씁니다."
    )
    print()

    # F) 실제 SignalEngine 메서드로 LG(003550) 동일 last_sig 게이트 검증
    print("=== F) LG — SignalEngine._s1_presignal_ready_for_gate 실제 호출 ===")
    try:
        from core.signal_engine import SignalEngine

        class _MiniApp:
            """SignalEngine 생성에 필요한 최소 app."""

            def __init__(self, st: dict) -> None:
                self.settings = st

        eng = SignalEngine(_MiniApp(settings))
        for label, ser in (
            ("DB완성봉 last_sig", last_bt),
            ("틱부분봉 last_sig", last_rt),
        ):
            ls = ser.to_dict() if hasattr(ser, "to_dict") else dict(ser)
            sig_b = bool(ls.get("Signal", False))
            # 옛 코드: ready = bool(last_sig.get("Ready", False)) → 키 없으면 항상 False
            old_ready = bool(ls.get("Ready", False))
            old_gate_is_buy = sig_b and old_ready
            fixed_ready = bool(eng._s1_presignal_ready_for_gate(ls))
            fixed_gate_is_buy = sig_b and fixed_ready
            print(f"{label}:")
            print(f"  Signal={sig_b}, 상세에 Ready 키 존재: {'Ready' in ls}")
            print(f"  옛 게이트(signal and get(Ready,False)): is_buy={old_gate_is_buy}")
            print(f"  현재 엔진 _s1_presignal_ready_for_gate: ready={fixed_ready}, is_buy={fixed_gate_is_buy}")
        print()
        print("결론: LG 003550 / 해당 슬롯에서 Signal=True 이면 수정 후 게이트 is_buy=True (막히지 않음).")
    except Exception as exc:
        print("F) 검증 실패:", exc)


if __name__ == "__main__":
    main()
