# -*- coding: utf-8 -*-
"""
동적 백테 `DynamicBacktester._process_intra_bar_dynamic` 과 동일한 1분 인트라바 루프.

- 청산(EXIT)은 분마다 보수적 체결가로 `TradingStrategy.evaluate_position` 평가(선착순).
- 신규(S1)·추매(S2) 매수도 동적 백테와 동일 분기(트랙·S2 승인·스마트바이).
- 실시간 `SignalEngine` 은 본 모듈 결과로 미보유 S1·보유 청산·S2 매매 경로를 통일한다.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from backtest_engine import TradingStrategy

from core.bt_hp_signal_bridge import (
    compute_intrabar_indicator_step,
    evaluate_bt_hp_s1_new_entry_only,
    evaluate_close_now_s1_at_m1_idx,
    intrabar_close_now_signals_enabled,
    resolve_df_with_ind_for_close_now,
)


def m1_float_ohlc(m1_r: Any, cname: str, cname2: str, default: float) -> float:
    """Series/dict 겸용 OHLC 스칼라 읽기."""
    try:
        for name in (cname, cname2):
            v = None
            if hasattr(m1_r, "get"):
                v = m1_r.get(name)
            elif hasattr(m1_r, "__contains__") and name in m1_r:
                v = m1_r[name]
            if v is not None and not pd.isna(v):
                f = float(v)
                if np.isfinite(f):
                    return f
    except (TypeError, KeyError, ValueError):
        pass
    return float(default)


def m1_sell_price_conservative(m1_r: Any) -> float:
    """1분: 매도 판가 보수적 — 저가 우선, 없으면 종가."""
    try:
        if m1_r is None:
            return 0.0
        c = m1_float_ohlc(m1_r, "Close", "close", 0.0)
        v = m1_float_ohlc(m1_r, "Low", "low", float("nan"))
        if v > 0 and np.isfinite(v):
            return float(v)
        if c > 0:
            return float(c)
    except (TypeError, ValueError, KeyError):
        pass
    return 0.0


def m1_buy_price_conservative(m1_r: Any) -> float:
    """1분: 매수 체결 보수적 — 고가 우선, 없으면 종가."""
    try:
        if m1_r is None:
            return 0.0
        c = m1_float_ohlc(m1_r, "Close", "close", 0.0)
        v = m1_float_ohlc(m1_r, "High", "high", float("nan"))
        if v > 0 and np.isfinite(v):
            return float(v)
        if c > 0:
            return float(c)
    except (TypeError, ValueError, KeyError):
        pass
    return 0.0


def floor_sell_px_to_same_15m_bar(px_m1: float, curr_15m: Any) -> float:
    """1분 저가가 동일 15분 봉 저가보다 낮으면 15분 저가로 상향(동적 백테와 동일)."""
    try:
        if curr_15m is None or not (px_m1 > 0) or not np.isfinite(float(px_m1)):
            return float(px_m1)
        lo_15 = m1_float_ohlc(curr_15m, "Low", "low", float("nan"))
        if lo_15 == lo_15 and lo_15 > 0 and float(px_m1) < float(lo_15):
            return float(lo_15)
    except (TypeError, ValueError, KeyError):
        pass
    return float(px_m1)


def _row_to_dict(ind: Any) -> Optional[Dict[str, Any]]:
    if ind is None:
        return None
    try:
        if isinstance(ind, dict):
            return dict(ind)
        if hasattr(ind, "to_dict"):
            return dict(ind.to_dict())
    except (TypeError, ValueError, AttributeError):
        pass
    return None


def _m1_hi_for_maxp(m1_r: Any) -> float:
    try:
        return float(m1_float_ohlc(m1_r, "High", "high", 0.0))
    except (TypeError, ValueError):
        return 0.0


def resolve_position_entry_ts(pos: Dict[str, Any]) -> Optional[pd.Timestamp]:
    """
    포지션 진입 시각 — ``entry_time`` / ``entry_date`` / 첫 BUY 이벤트 순으로 해석.

    SYNCED 선반영 뒤 체결 확정 전 ``entry_time`` 이 장전 동기화 시각으로 남는 경우,
    확정 BUY 이벤트 시각이 더 늦으면 **이벤트 시각을 우선**합니다.
    """
    field_ts: Optional[pd.Timestamp] = None
    try:
        et = pos.get("entry_time") or pos.get("entry_date")
        if et is not None and str(et).strip():
            field_ts = pd.Timestamp(et)
    except (TypeError, ValueError):
        field_ts = None

    buy_ts: Optional[pd.Timestamp] = None
    try:
        for ev in reversed(pos.get("events") or []):
            if not isinstance(ev, dict):
                continue
            act = str(ev.get("action", "") or "")
            if act.startswith("BUY_"):
                t = ev.get("time")
                if t is not None and str(t).strip():
                    buy_ts = pd.Timestamp(t)
                    break
    except (TypeError, ValueError):
        buy_ts = None

    if field_ts is not None and buy_ts is not None:
        try:
            return max(field_ts, buy_ts)
        except Exception:
            return buy_ts
    if buy_ts is not None:
        return buy_ts
    return field_ts


def _m1_is_before_position_entry(m1_time: Any, pos_sim: Dict[str, Any]) -> bool:
    """
    진입 시각 이전에 **완료된** 1분봉이면 True(청산 미러에서 제외).

    분 단위 floor(예: 10:15)만 쓰면 같은 15분 슬롯의 진입 전 1분(10:01~10:14) 저가가
    ATR SL에 걸리는 오판이 납니다. 1분봉 open 라벨 기준 **봉 종료 시각(open+1분)** 이
    진입 시각 이하이면 해당 분은 제외합니다.
    """
    try:
        entry_ts = resolve_position_entry_ts(pos_sim)
        if entry_ts is None:
            # 진입 시각 미상 — 보수적으로 청산 미러에서 제외(오판 방지)
            return True
        m1_open = pd.Timestamp(m1_time).floor("min")
        m1_close = m1_open + pd.Timedelta(minutes=1)
        return m1_close <= entry_ts
    except (TypeError, ValueError):
        return True


def _safe_float_ind(ind: Any, key: str, default: float = float("nan")) -> float:
    """지표 dict/Series에서 float 안전 추출."""
    try:
        if ind is None:
            return float(default)
        v = ind.get(key) if hasattr(ind, "get") else None
        if v is None and hasattr(ind, "__contains__") and key in ind:
            try:
                v = ind[key]
            except Exception:
                v = None
        if v is None or (isinstance(v, float) and not np.isfinite(v)):
            return float(default)
        f = float(v)
        return f if np.isfinite(f) else float(default)
    except (TypeError, ValueError, KeyError, AttributeError):
        return float(default)


def close_now_fast_prefilter_ok(settings: Dict[str, Any], ind_d: Any) -> bool:
    """
    close-now(무거움) 호출 전 싼 게이트.

    매수 RSI 밴드보다 넓게 열어 두어 경계 신호는 거의 놓치지 않게 한다.
    """
    try:
        if hasattr(ind_d, "get"):
            ready = ind_d.get("Ready", None)
            if ready is not None and not bool(ready):
                return False
    except (TypeError, ValueError, AttributeError):
        pass

    rsi = _safe_float_ind(ind_d, "RSI")
    osc = _safe_float_ind(ind_d, "OSC", 0.0)
    try:
        rmin = float(settings.get("buy_rsi_min", 40.0))
    except (TypeError, ValueError):
        rmin = 40.0
    try:
        rmax = max(
            float(settings.get("buy_rsi_max_a", 72.0)),
            float(settings.get("buy_rsi_max_b", 80.0)),
        )
    except (TypeError, ValueError):
        rmax = 80.0
    lo, hi = rmin - 12.0, rmax + 12.0
    if np.isfinite(rsi) and (rsi < lo or rsi > hi):
        return False
    # OSC 절대값 게이트는 가격 스케일 OSC(수백~수천)에서 거의 항상 실패하므로
    # bt_rt_parity_fast_osc_abs_max <= 0 이면 비활성(기본 0).
    try:
        osc_abs_max = float(settings.get("bt_rt_parity_fast_osc_abs_max", 0.0))
    except (TypeError, ValueError):
        osc_abs_max = 0.0
    if osc_abs_max > 0 and np.isfinite(osc) and abs(osc) > osc_abs_max:
        return False
    return True


def close_now_fast_throttle_skip(
    holder: Dict[str, Any], ind_d: Any, settings: Dict[str, Any]
) -> bool:
    """
    직전 close-now가 미체결이고 RSI/OSC 변화가 미미하면 True → 이번 분 skip.
    """
    try:
        if bool(holder.get("cn_last_can_execute", False)):
            return False
    except Exception:
        pass
    try:
        dr = float(settings.get("bt_rt_parity_fast_rsi_delta", 0.35))
    except (TypeError, ValueError):
        dr = 0.35
    try:
        do = float(settings.get("bt_rt_parity_fast_osc_delta", 0.08))
    except (TypeError, ValueError):
        do = 0.08
    rsi = _safe_float_ind(ind_d, "RSI")
    osc = _safe_float_ind(ind_d, "OSC", 0.0)
    prev_r = holder.get("cn_last_rsi")
    prev_o = holder.get("cn_last_osc")
    try:
        if prev_r is None and prev_o is None:
            return False
        if prev_r is not None and np.isfinite(rsi) and np.isfinite(float(prev_r)):
            if abs(rsi - float(prev_r)) > dr:
                return False
        if prev_o is not None and np.isfinite(osc) and np.isfinite(float(prev_o)):
            if abs(osc - float(prev_o)) > do:
                return False
        return True
    except (TypeError, ValueError):
        return False


def evaluate_bt_hp_intrabar_unified(
    settings: Dict[str, Any],
    code: str,
    full_15m: pd.DataFrame,
    m1_chunk: pd.DataFrame,
    pos_h: Optional[Dict[str, Any]],
    code_track: str,
    *,
    skip_holder_exit: bool = False,
    df_with_ind: Optional[pd.DataFrame] = None,
    close_now_fast_skip: bool = False,
) -> Dict[str, Any]:
    """
    동적 백테 고정밀 인트라바 1회분과 동일한 시뮬레이션.

    Parameters
    ----------
    skip_holder_exit
        True면 보유 구간에서 ``TradingStrategy.evaluate_position`` 청산 판정만 생략하고
        max_p 갱신·S2 등은 그대로 진행한다(실시간: S2만 M1 주기에 맞출 때).
    close_now_fast_skip
        True면(백테 RT정합 고속) S1 close-now 전에 RSI/OSC 사전필터·스로틀 적용.
        실시간 미러는 False 유지.

    Returns
    -------
    dict
        ok, reason, indicator_last, exit_first, stage2_addon, s1_eval
        - ``exit_first``: {"reason", "profit_ratio", "eval_price", "m1_idx"} 또는 None
        - ``stage2_addon``: {"reason", "fill_price", "m1_idx"} 또는 None
        - ``s1_eval``: 미보유 시 ``evaluate_bt_hp_s1_new_entry_only`` 와 동형 dict(루프 중 신규 체결 시 즉시 반환)
    """
    out: Dict[str, Any] = {
        "ok": False,
        "reason": "",
        "indicator_last": None,
        "exit_first": None,
        "stage2_addon": None,
        "s1_eval": None,
    }
    try:
        if (
            full_15m is None
            or getattr(full_15m, "empty", True)
            or m1_chunk is None
            or getattr(m1_chunk, "empty", True)
        ):
            out["reason"] = "empty_frame"
            return out

        curr_15m = full_15m.iloc[-1]
        # 15:15/15:30 완성 슬롯 — S1·S2 생략(청산 경로만 유지)
        try:
            from utils.krx_bars import is_krx_no_signal_eval_15m_slot

            _skip_eod_sig = bool(
                is_krx_no_signal_eval_15m_slot(getattr(curr_15m, "name", None) or full_15m.index[-1])
            )
        except Exception:
            _skip_eod_sig = False
        pos_sim: Optional[Dict[str, Any]] = (
            copy.deepcopy(pos_h) if isinstance(pos_h, dict) else None
        )
        holder: Dict[str, Any] = {
            "initialized": False,
            "last_15m_dict": None,
            "prices_tail": None,
            "df_with_ind_close_now": None,
        }

        use_close_now = intrabar_close_now_signals_enabled(settings)
        cn_df = resolve_df_with_ind_for_close_now(
            settings, full_15m, df_with_ind, holder
        )
        sync_static_sig = not use_close_now or cn_df is None

        use_sb_a = bool(settings.get("use_smart_buy_a", False))
        use_sb_b = bool(settings.get("use_smart_buy_b", True))
        sb_ba = float(
            settings.get("smart_buy_ba_ratio", settings.get("smart_buy_ratio", 1.2))
        )
        sb_vp = float(settings.get("smart_buy_vp", 110.0))
        use_sb_s2_a = bool(settings.get("use_smart_buy_s2", True))
        use_sb_s2_b = bool(settings.get("use_smart_buy_s2_b", True))

        nk = str(code).strip().zfill(6)
        ct = str(code_track or "AB").strip().upper()

        for m1_idx in range(len(m1_chunk)):
            m1_r = m1_chunk.iloc[m1_idx]
            m1_time = m1_chunk.index[m1_idx]

            full_cached = full_15m
            indicator_to_use: Any = curr_15m
            if full_cached is not None and curr_15m is not None:
                indicator_to_use = compute_intrabar_indicator_step(
                    settings,
                    full_cached,
                    curr_15m,
                    m1_chunk,
                    m1_idx,
                    holder,
                    sync_static_signal_keys=sync_static_sig,
                )

            ind_d = _row_to_dict(indicator_to_use)
            out["indicator_last"] = ind_d

            # --- 1) 청산(보유 시 최우선) ---
            if pos_sim is not None:
                before_entry = _m1_is_before_position_entry(m1_time, pos_sim)
                if not before_entry:
                    try:
                        hi = _m1_hi_for_maxp(m1_r)
                        mp = float(pos_sim.get("max_p", 0) or 0)
                        if hi > mp:
                            pos_sim["max_p"] = hi
                    except (TypeError, ValueError, KeyError):
                        pass

                if not skip_holder_exit and not before_entry:
                    px_sell = m1_sell_price_conservative(m1_r)
                    px_sell = floor_sell_px_to_same_15m_bar(px_sell, curr_15m)
                    try:
                        ba_m = float(
                            m1_r.get("bid_ask_ratio", 1.0)
                            if hasattr(m1_r, "get")
                            else 1.0
                        )
                        ab_m = float(
                            m1_r.get(
                                "ask_bid_ratio",
                                1.0 / (ba_m + 1e-10),
                            )
                            if hasattr(m1_r, "get")
                            else 1.0
                        )
                        vp_m = float(
                            m1_r.get("vol_power", m1_r.get("Vol_power", 100.0))
                            if hasattr(m1_r, "get")
                            else 100.0
                        )
                    except (TypeError, ValueError):
                        ba_m, ab_m, vp_m = 1.0, 1.0, 100.0

                    res = TradingStrategy.evaluate_position(
                        nk,
                        float(px_sell),
                        pos_sim,
                        settings,
                        indicator_row=ind_d,
                        current_time=m1_time,
                        is_realtime=True,
                        kwargs={
                            "bid_ask_ratio": ba_m,
                            "ask_bid_ratio": ab_m,
                            "vol_power": vp_m,
                            # [버그픽스] 동기화 포지션 등 sig_type 이 비어 있을 때 트랙 힌트로
                            # Type A/B 청산 키(TP/SL/Flash/Trail)를 올바르게 적용(상향 폴백).
                            "sel_track": ct,
                        },
                    )
                    if isinstance(res, dict) and str(res.get("action") or "") == "EXIT":
                        out["ok"] = True
                        out["exit_first"] = {
                            "reason": str(res.get("reason") or "EXIT"),
                            "profit_ratio": float(res.get("profit_ratio", 0) or 0),
                            "eval_price": float(px_sell),
                            "m1_idx": int(m1_idx),
                            "m1_bar": str(m1_time),
                        }
                        out["reason"] = "exit"
                        return out

            # --- 2) 매수(S1 신규 / S2 추매) ---
            if ind_d is not None and not _skip_eod_sig:
                is_addon_target = pos_sim is not None and int(
                    pos_sim.get("entry_n", pos_sim.get("stage", 1)) or 1
                ) < 2

                is_buy_signal = False
                sig_type = "TYPE_B"

                if pos_sim is None and use_close_now and cn_df is not None:
                    run_cn = True
                    if close_now_fast_skip:
                        try:
                            if not close_now_fast_prefilter_ok(settings, ind_d):
                                run_cn = False
                            elif close_now_fast_throttle_skip(holder, ind_d, settings):
                                run_cn = False
                        except Exception:
                            run_cn = True
                    cn_can = False
                    if run_cn:
                        cn = evaluate_close_now_s1_at_m1_idx(
                            settings,
                            cn_df,
                            full_15m,
                            m1_chunk,
                            m1_idx,
                            ct,
                            holdings={},
                            precomputed_sim=ind_d if isinstance(ind_d, dict) else None,
                        )
                        cn_can = bool(cn.get("can_execute"))
                        if cn_can:
                            is_buy_signal = True
                            sig_type = str(cn.get("sig_type") or "TYPE_B")
                            row_cn = cn.get("indicator_row")
                            if isinstance(row_cn, dict):
                                ind_d = {**ind_d, **row_cn}
                    if close_now_fast_skip:
                        try:
                            holder["cn_last_rsi"] = _safe_float_ind(ind_d, "RSI")
                            holder["cn_last_osc"] = _safe_float_ind(ind_d, "OSC", 0.0)
                            holder["cn_last_can_execute"] = bool(cn_can)
                        except Exception:
                            pass
                elif not is_addon_target:
                    is_buy_signal = bool(ind_d.get("Signal", False)) and bool(
                        ind_d.get("Ready", False)
                    )
                    if is_buy_signal and pos_sim is None:
                        sig_type = str(
                            TradingStrategy.effective_s1_sig_type_for_buy(
                                settings, ind_d, {}, ct
                            )
                        )
                elif is_addon_target:
                    is_buy_signal = True
                    sig_type = str(ind_d.get("Sig_Type", "TYPE_B"))

                if is_buy_signal or is_addon_target:
                    if pos_sim is None and not (
                        use_close_now and cn_df is not None
                    ):
                        sig_type = str(
                            TradingStrategy.effective_s1_sig_type_for_buy(
                                settings, ind_d, {}, ct
                            )
                        )
                    elif is_addon_target:
                        sig_type = str(ind_d.get("Sig_Type", "TYPE_B"))

                    if (sig_type == "TYPE_A" and ct == "B") or (
                        sig_type == "TYPE_B" and ct == "A"
                    ):
                        continue
                    if (not is_addon_target) and TradingStrategy.simultaneous_type_a_blocked(
                        settings, ind_d, sig_type
                    ):
                        continue
                    if (not is_addon_target) and TradingStrategy.simultaneous_equal_holdings_blocked(
                        settings, ind_d, {}
                    ):
                        continue

                    use_sb_for_type = bool(use_sb_a) if (
                        sig_type == "TYPE_A" and not is_addon_target
                    ) else bool(use_sb_b) if (
                        sig_type == "TYPE_B" and not is_addon_target
                    ) else (
                        use_sb_s2_b
                        if "TYPE_B" in str(pos_sim.get("sig_type", "")).upper()
                        else use_sb_s2_a
                    )

                    can_execute = True
                    reason_base = (
                        "1M_SIG" if not is_addon_target else "1M_ADDON(S2)"
                    )

                    if is_addon_target and pos_sim is not None:
                        pos_s2 = dict(pos_sim)
                        try:
                            et = pos_sim.get("entry_time") or pos_sim.get("entry_date")
                            pos_s2["entry_date"] = (
                                pd.Timestamp(et) if et is not None else m1_time
                            )
                        except (TypeError, ValueError):
                            pos_s2["entry_date"] = m1_time
                        pos_s2["avg_p"] = pos_sim.get("avg_p", pos_sim.get("avg_price"))
                        pos_s2["sig_type"] = pos_sim.get("sig_type", "TYPE_A")
                        try:
                            if not TradingStrategy._check_s2_approval(
                                pos_s2,
                                None,
                                settings,
                                indicator_row=ind_d,
                                market_trend="UP",
                                curr_dt=m1_time,
                            ):
                                can_execute = False
                        except (TypeError, ValueError, AttributeError):
                            can_execute = False

                    if can_execute and use_sb_for_type:
                        if is_addon_target and curr_15m is not None:
                            try:
                                _cr = curr_15m
                                if hasattr(_cr, "get"):
                                    _ba = float(_cr.get("bid_ask_ratio", 1.0))
                                    ab_ratio = float(
                                        _cr.get(
                                            "ask_bid_ratio",
                                            _cr.get("Ask_Bid_Ratio", 1.0 / (_ba + 1e-10)),
                                        )
                                    )
                                    v_power = float(
                                        _cr.get(
                                            "vol_power", _cr.get("Vol_power", 100.0)
                                        )
                                    )
                                else:
                                    raise TypeError("no get")
                            except (TypeError, ValueError, KeyError, AttributeError):
                                try:
                                    ba2 = float(
                                        m1_r.get("bid_ask_ratio", 1.0)
                                        if hasattr(m1_r, "get")
                                        else 1.0
                                    )
                                    ab_ratio = float(
                                        m1_r.get(
                                            "ask_bid_ratio", 1.0 / (ba2 + 1e-10)
                                        )
                                        if hasattr(m1_r, "get")
                                        else 1.0
                                    )
                                    v_power = float(
                                        m1_r.get(
                                            "vol_power", m1_r.get("Vol_power", 100.0)
                                        )
                                        if hasattr(m1_r, "get")
                                        else 100.0
                                    )
                                except (TypeError, ValueError):
                                    ab_ratio, v_power = 1.0, 100.0
                        else:
                            try:
                                ba2 = float(
                                    m1_r.get("bid_ask_ratio", 1.0)
                                    if hasattr(m1_r, "get")
                                    else 1.0
                                )
                                ab_ratio = float(
                                    m1_r.get("ask_bid_ratio", 1.0 / (ba2 + 1e-10))
                                    if hasattr(m1_r, "get")
                                    else 1.0
                                )
                                v_power = float(
                                    m1_r.get(
                                        "vol_power", m1_r.get("Vol_power", 100.0)
                                    )
                                    if hasattr(m1_r, "get")
                                    else 100.0
                                )
                            except (TypeError, ValueError):
                                ab_ratio, v_power = 1.0, 100.0
                        if ab_ratio < sb_ba or v_power < sb_vp:
                            can_execute = False

                    if can_execute:
                        reason = reason_base
                        if use_sb_for_type:
                            reason = "SmartBuy|" + reason
                        reason += f"+{m1_idx}m"
                        fill_px = m1_buy_price_conservative(m1_r)
                        if is_addon_target:
                            out["ok"] = True
                            out["stage2_addon"] = {
                                "reason": reason,
                                "fill_price": float(fill_px),
                                "m1_idx": int(m1_idx),
                            }
                            out["reason"] = "stage2_addon"
                            return out
                        # S1 신규 체결(동적 백테와 동일: 첫 체결 시 루프 종료)
                        out["s1_eval"] = {
                            "ok": True,
                            "is_buy_signal": True,
                            "can_execute_entry": True,
                            "sig_type": sig_type,
                            "indicator_to_use": ind_d,
                            "reason": reason,
                        }
                        out["ok"] = True
                        out["reason"] = "s1_executed_intrabar"
                        return out

        out["ok"] = True
        out["reason"] = "no_intrabar_trade"
        if pos_sim is None:
            out["s1_eval"] = evaluate_bt_hp_s1_new_entry_only(
                settings,
                full_15m,
                m1_chunk,
                ct,
                holdings={},
            )
        return out
    except (TypeError, ValueError, KeyError, AttributeError) as e:
        out["reason"] = f"error:{e}"
        return out
