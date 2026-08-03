# -*- coding: utf-8 -*-
"""
동적 백테스트 고정밀(15m pre-signal + 1m intrabar)과 **동일한** 인트라바 지표·신호 행을 계산합니다.

- `dynamic_backtest._process_intra_bar_dynamic` · 실시간 ``SignalEngine`` 인트라바 미러가 공유합니다.
- ``realtime_intrabar_close_now_signals`` ON 시: 같은 M1에서 forming OHLCV + intrabar RSI/MACD로
  ``get_buy_signals_detailed`` 를 재실행해 Signal/Sig_Type 을 정적 복사 없이 판정합니다.
"""

from __future__ import annotations

import random
import time
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from backtest_engine import TradingStrategy
from utils.krx_bars import m1_window_start_for_15m_end

# dynamic_backtest._process_intra_bar_dynamic 과 동일 키 목록
_STATIC_SYNC_KEYS: Tuple[str, ...] = (
    "Signal",
    "Ready",
    "Sig_Type",
    "S1_Simultaneous_AB",
    "Volume",
    "Prev_Day_Vol",
    "Prev_Day_Vol_B",
    "Prev_Day_Vol_S1_A",
    "Prev_Day_Vol_S1_B",
    "Prev_Day_Vol_S2_A",
    "Prev_Day_Vol_S2_B",
    "Vol_MA20",
)

# 정적 복사 시 신호만 제외(Volume·Prev_Day_Vol 등은 유지)
_SIGNAL_STATIC_ONLY: Tuple[str, ...] = (
    "Signal",
    "Ready",
    "Sig_Type",
    "S1_Simultaneous_AB",
)
_INTRABAR_SIM_PRICE_KEYS: Tuple[str, ...] = (
    "RSI",
    "MACD",
    "MACD_Sig",
    "OSC",
    "MA5",
    "MA20",
    "MA60",
    "_ema12",
    "_ema26",
    "_macd_sig",
    "_avg_gain",
    "_avg_loss",
)


def intrabar_close_now_signals_enabled(settings: Optional[Dict[str, Any]]) -> bool:
    """같은 M1 '지금 닫히면?' 신호 재판정 사용 여부."""
    try:
        return bool((settings or {}).get("realtime_intrabar_close_now_signals", True))
    except Exception:
        return True


def _apply_ready_column_to_df(
    df_ind: pd.DataFrame, settings: Dict[str, Any]
) -> pd.DataFrame:
    """백테·실시간 공통 Ready 열(``hp_causal_ready_min_bar_index``)."""
    try:
        if df_ind is None or getattr(df_ind, "empty", True):
            return df_ind
        ready_min = max(
            0, int(float(settings.get("hp_causal_ready_min_bar_index", 19)))
        )
        out = df_ind.copy()
        out["Ready"] = out.reset_index(drop=True).index >= ready_min
        return out
    except Exception:
        return df_ind


def _row_to_plain_dict(row: Any) -> Dict[str, Any]:
    try:
        if isinstance(row, dict):
            return dict(row)
        if hasattr(row, "to_dict"):
            return dict(row.to_dict())
    except Exception:
        pass
    return {}


def _base_idx_for_curr_15m(full_15m: pd.DataFrame, curr_15m_time: Any) -> int:
    """full_15m.index 에서 curr_15m_time 위치(정수). slice 면 시작 정수."""
    try:
        loc = full_15m.index.get_loc(curr_15m_time)
        if isinstance(loc, slice):
            start = loc.start
            return int(start) if start is not None else 0
        if isinstance(loc, (list, tuple, np.ndarray)):
            return int(loc[0])
        return int(loc)
    except (KeyError, TypeError, ValueError):
        return len(full_15m) - 1


def compute_intrabar_indicator_step(
    settings: Dict[str, Any],
    full_15m: pd.DataFrame,
    curr_15m: Any,
    m1_chunk: pd.DataFrame,
    m1_idx: int,
    holder: Dict[str, Any],
    *,
    sync_static_signal_keys: bool = True,
) -> Any:
    """
    `m1_chunk` 의 `m1_idx` 시점까지 동적 백테와 동일하게 `indicator_to_use` 를 갱신합니다.

    Parameters
    ----------
    holder :
        `{"initialized": bool, "last_15m_dict": dict|None, "prices_tail": dict|None}` 형태.
        동일 슬롯·동일 청크 루프에서 재사용합니다.
    sync_static_signal_keys :
        False면 Signal/Ready/Sig_Type 정적 복사 생략(``close_now`` 재판정 경로).
    """
    indicator_to_use = curr_15m
    try:
        if full_15m is None or getattr(full_15m, "empty", True):
            return indicator_to_use
        if curr_15m is None:
            return indicator_to_use

        curr_15m_time = full_15m.index[-1]
        base_idx = _base_idx_for_curr_15m(full_15m, curr_15m_time)

        if m1_idx == 0 or not holder.get("initialized"):
            prev_idx = max(0, base_idx - 1)
            holder["last_15m_dict"] = full_15m.iloc[prev_idx].to_dict()
            holder["prices_tail"] = {
                5: full_15m["Close"].iloc[max(0, base_idx - 4) : base_idx].sum(),
                20: full_15m["Close"].iloc[max(0, base_idx - 19) : base_idx].sum(),
                60: full_15m["Close"].iloc[max(0, base_idx - 59) : base_idx].sum(),
                "rsi_vals": full_15m["Close"]
                .iloc[max(0, base_idx - 15) : base_idx]
                .values,
            }
            holder["initialized"] = True

        m1_r = m1_chunk.iloc[m1_idx]
        last_15m_dict = holder.get("last_15m_dict") or {}
        prices_tail = holder.get("prices_tail") or {}

        sim_15m = TradingStrategy.simulate_intrabar_indicators(
            last_15m_dict,
            m1_r,
            settings,
            hist_df_prices_tail=prices_tail,
        )
        if sim_15m is not None:
            try:
                if sync_static_signal_keys:
                    sync_keys = _STATIC_SYNC_KEYS
                else:
                    sync_keys = tuple(
                        k for k in _STATIC_SYNC_KEYS if k not in _SIGNAL_STATIC_ONLY
                    )
                if hasattr(curr_15m, "index"):
                    for k in sync_keys:
                        if k in curr_15m.index:
                            sim_15m[k] = curr_15m[k]
                elif isinstance(curr_15m, dict):
                    for k in sync_keys:
                        if k in curr_15m:
                            sim_15m[k] = curr_15m[k]
            except (TypeError, KeyError, ValueError):
                pass
            indicator_to_use = sim_15m
    except (TypeError, KeyError, ValueError, IndexError, AttributeError):
        pass
    return indicator_to_use


def _patch_forming_ohlcv_from_m1_subchunk(
    df: pd.DataFrame, m1_sub: pd.DataFrame
) -> None:
    """마지막 15m 행 OHLCV = 슬롯 진행 중 완료 M1 누적(지금 닫히면)."""
    try:
        if (
            df is None
            or getattr(df, "empty", True)
            or m1_sub is None
            or getattr(m1_sub, "empty", True)
        ):
            return
        last_ix = df.index[-1]
        df.at[last_ix, "Open"] = float(m1_sub["Open"].iloc[0])
        df.at[last_ix, "High"] = float(m1_sub["High"].max())
        df.at[last_ix, "Low"] = float(m1_sub["Low"].min())
        df.at[last_ix, "Close"] = float(m1_sub["Close"].iloc[-1])
        df.at[last_ix, "Volume"] = float(m1_sub["Volume"].sum())
    except Exception:
        pass


def _merge_intrabar_sim_into_df_last_row(
    df: pd.DataFrame, sim_row: Dict[str, Any]
) -> None:
    """``simulate_intrabar_indicators`` 결과를 마지막 15m 행에 병합."""
    try:
        if df is None or getattr(df, "empty", True) or not sim_row:
            return
        last_ix = df.index[-1]
        for k in _INTRABAR_SIM_PRICE_KEYS:
            if k in sim_row:
                try:
                    df.at[last_ix, k] = sim_row[k]
                except Exception:
                    pass
        # Close 는 M1 subchunk 와 일치
        if "Close" in sim_row:
            try:
                df.at[last_ix, "Close"] = float(sim_row["Close"])
            except Exception:
                pass
    except Exception:
        pass


def evaluate_close_now_s1_at_m1_idx(
    settings: Dict[str, Any],
    df_with_ind: pd.DataFrame,
    full_15m: pd.DataFrame,
    m1_chunk: pd.DataFrame,
    m1_idx: int,
    code_track: str,
    *,
    holdings: Optional[Dict[str, Any]] = None,
    precomputed_sim: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    같은 M1 시점: forming OHLCV + intrabar RSI/MACD 로 마지막 15m 행 ``get_buy_signals_detailed`` 재판정.

    Parameters
    ----------
    precomputed_sim
        호출측이 이미 ``compute_intrabar_indicator_step`` 한 dict. 있으면 중복 지표 계산을 생략.

    Returns
    -------
    dict
        ok, signal, ready, sig_type, can_execute, reason, indicator_row
    """
    out: Dict[str, Any] = {
        "ok": False,
        "signal": False,
        "ready": False,
        "sig_type": "TYPE_B",
        "can_execute": False,
        "reason": "",
        "indicator_row": None,
    }
    try:
        if not intrabar_close_now_signals_enabled(settings):
            out["reason"] = "disabled"
            return out
        if (
            df_with_ind is None
            or getattr(df_with_ind, "empty", True)
            or m1_chunk is None
            or getattr(m1_chunk, "empty", True)
            or full_15m is None
            or getattr(full_15m, "empty", True)
        ):
            out["reason"] = "empty_frame"
            return out
        mi = int(m1_idx)
        if mi < 0 or mi >= len(m1_chunk):
            out["reason"] = "bad_m1_idx"
            return out

        work = df_with_ind.copy()
        m1_sub = m1_chunk.iloc[: mi + 1]
        _patch_forming_ohlcv_from_m1_subchunk(work, m1_sub)

        if isinstance(precomputed_sim, dict) and precomputed_sim:
            sim_d = dict(precomputed_sim)
        else:
            curr_15m = full_15m.iloc[-1]
            sim_holder: Dict[str, Any] = {
                "initialized": False,
                "last_15m_dict": None,
                "prices_tail": None,
            }
            sim_row = compute_intrabar_indicator_step(
                settings,
                full_15m,
                curr_15m,
                m1_chunk,
                mi,
                sim_holder,
                sync_static_signal_keys=False,
            )
            sim_d = _row_to_plain_dict(sim_row)
        _merge_intrabar_sim_into_df_last_row(work, sim_d)

        work = _apply_ready_column_to_df(work, settings)
        detailed = TradingStrategy.get_buy_signals_detailed(work, settings, "UP")
        if detailed is None or getattr(detailed, "empty", True):
            out["reason"] = "no_signals"
            return out

        last_ser = detailed.iloc[-1]
        last_sig = (
            last_ser.to_dict() if hasattr(last_ser, "to_dict") else dict(last_ser)
        )
        try:
            signal_on = bool(last_sig.get("Signal", False))
        except (TypeError, AttributeError):
            signal_on = False
        # get_buy_signals_detailed 는 Ready 열을 반환하지 않음 → work 의 Ready 를 권위로 사용
        # (미적용 시 Ready 가 항상 False → RT정합/close-now 매수 0건)
        try:
            if work is not None and "Ready" in work.columns and len(work) > 0:
                ready_on = bool(work["Ready"].iloc[-1])
            else:
                ready_on = bool(last_sig.get("Ready", False))
        except (TypeError, AttributeError, IndexError, KeyError):
            ready_on = False
        try:
            last_sig["Ready"] = bool(ready_on)
        except Exception:
            pass

        ct = str(code_track or "AB").strip().upper()
        sig_type = str(
            TradingStrategy.effective_s1_sig_type_for_buy(
                settings, last_sig, holdings or {}, ct
            )
        )

        track_ok = TradingStrategy.track_signal_match_allows(settings, sig_type, ct)
        if track_ok and TradingStrategy.simultaneous_type_a_blocked(
            settings, last_sig, sig_type
        ):
            track_ok = False
            out["ok"] = True
            out["signal"] = bool(signal_on)
            out["ready"] = bool(ready_on)
            out["sig_type"] = sig_type
            out["indicator_row"] = last_sig
            out["can_execute"] = False
            out["reason"] = "simul_type_a_blocked"
            return out
        if track_ok and TradingStrategy.simultaneous_equal_holdings_blocked(
            settings, last_sig, holdings or {}
        ):
            out["ok"] = True
            out["signal"] = bool(signal_on)
            out["ready"] = bool(ready_on)
            out["sig_type"] = sig_type
            out["indicator_row"] = last_sig
            out["can_execute"] = False
            out["reason"] = "simul_equal_holdings_blocked"
            return out

        out["ok"] = True
        out["signal"] = bool(signal_on)
        out["ready"] = bool(ready_on)
        out["sig_type"] = sig_type
        out["indicator_row"] = last_sig
        out["can_execute"] = bool(signal_on and ready_on and track_ok)
        if not signal_on:
            out["reason"] = "signal_off"
        elif not ready_on:
            out["reason"] = "ready_off"
        elif not track_ok:
            out["reason"] = "track_mismatch"
        else:
            out["reason"] = "ok"
        return out
    except (TypeError, ValueError, KeyError, AttributeError) as e:
        out["reason"] = f"error:{e!r}"
        return out


def resolve_df_with_ind_for_close_now(
    settings: Dict[str, Any],
    full_15m: pd.DataFrame,
    df_with_ind: Optional[pd.DataFrame],
    cache_holder: Optional[Dict[str, Any]],
) -> Optional[pd.DataFrame]:
    """close_now 용 지표 프레임 — 전달본 없으면 ``full_15m`` 에서 1회 생성·캐시."""
    try:
        if df_with_ind is not None and not getattr(df_with_ind, "empty", True):
            return df_with_ind
        if full_15m is None or getattr(full_15m, "empty", True):
            return None
        ch = cache_holder if isinstance(cache_holder, dict) else {}
        cached = ch.get("df_with_ind_close_now")
        if cached is not None and not getattr(cached, "empty", True):
            return cached
        built = TradingStrategy.calculate_indicators(full_15m.copy(), settings)
        built = _apply_ready_column_to_df(built, settings)
        ch["df_with_ind_close_now"] = built
        return built
    except Exception:
        return df_with_ind


def _read_sb_flags(settings: Dict[str, Any]) -> Tuple[bool, bool, float, float]:
    """DynamicBacktester._cache_run_hot_settings 와 동일 키."""
    try:
        use_a = bool(settings.get("use_smart_buy_a", False))
        use_b = bool(settings.get("use_smart_buy_b", True))
        sb_ba = float(settings.get("smart_buy_ba_ratio", settings.get("smart_buy_ratio", 1.2)))
        sb_vp = float(settings.get("smart_buy_vp", 110.0))
    except (TypeError, ValueError):
        use_a, use_b = False, True
        sb_ba, sb_vp = 1.2, 110.0
    return use_a, use_b, sb_ba, sb_vp


def evaluate_bt_hp_s1_new_entry_only(
    settings: Dict[str, Any],
    full_15m: pd.DataFrame,
    m1_chunk: pd.DataFrame,
    code_track: str,
    holdings: Optional[Dict[str, Any]] = None,
    m1_row_for_smart_buy: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    신규 진입(S1)만: 동적 고정밀과 동일한 `Signal∧Ready`·트랙·스마트바이 판단.

    Returns
    -------
    dict
        ok, is_buy_signal, can_execute_entry, sig_type, indicator_to_use, reason
    """
    out: Dict[str, Any] = {
        "ok": False,
        "is_buy_signal": False,
        "can_execute_entry": False,
        "sig_type": "TYPE_B",
        "indicator_to_use": None,
        "reason": "",
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
        use_close_now = intrabar_close_now_signals_enabled(settings)
        cn_df = resolve_df_with_ind_for_close_now(
            settings, full_15m, None, {"df_with_ind_close_now": None}
        )
        sync_static_sig = not use_close_now or cn_df is None
        holder: Dict[str, Any] = {
            "initialized": False,
            "last_15m_dict": None,
            "prices_tail": None,
        }
        indicator_to_use: Any = None
        last_m1_idx = len(m1_chunk) - 1
        for m1_idx in range(len(m1_chunk)):
            indicator_to_use = compute_intrabar_indicator_step(
                settings,
                full_15m,
                curr_15m,
                m1_chunk,
                m1_idx,
                holder,
                sync_static_signal_keys=sync_static_sig,
            )

        if indicator_to_use is None:
            out["reason"] = "no_indicator"
            return out

        ind = indicator_to_use
        sig_on = False
        sig_type = "TYPE_B"

        if use_close_now and cn_df is not None and last_m1_idx >= 0:
            cn = evaluate_close_now_s1_at_m1_idx(
                settings,
                cn_df,
                full_15m,
                m1_chunk,
                last_m1_idx,
                code_track,
                holdings=holdings,
            )
            sig_on = bool(cn.get("can_execute"))
            sig_type = str(cn.get("sig_type") or "TYPE_B")
            row_cn = cn.get("indicator_row")
            if isinstance(row_cn, dict):
                ind = {**_row_to_plain_dict(ind), **row_cn}
        else:
            try:
                sig_on = bool(ind.get("Signal", False)) and bool(ind.get("Ready", False))
            except (TypeError, AttributeError):
                sig_on = False
            if sig_on:
                sig_type = TradingStrategy.effective_s1_sig_type_for_buy(
                    settings,
                    ind,
                    holdings or {},
                    str(code_track or "AB").strip().upper(),
                )

        out["ok"] = True
        out["is_buy_signal"] = bool(sig_on)
        out["indicator_to_use"] = ind

        if not sig_on:
            out["reason"] = "signal_off"
            return out

        out["sig_type"] = sig_type

        ct = str(code_track or "AB").strip().upper()
        if not TradingStrategy.track_signal_match_allows(settings, sig_type, ct):
            out["reason"] = "track_mismatch"
            return out
        if TradingStrategy.simultaneous_type_a_blocked(settings, ind, sig_type):
            out["reason"] = "simul_type_a_blocked"
            return out
        if TradingStrategy.simultaneous_equal_holdings_blocked(
            settings, ind, holdings or {}
        ):
            out["reason"] = "simul_equal_holdings_blocked"
            return out

        use_sb_a, use_sb_b, sb_ba, sb_vp = _read_sb_flags(settings)
        if sig_type == "TYPE_A":
            use_sb_for_type = bool(use_sb_a)
        elif sig_type == "TYPE_B":
            use_sb_for_type = bool(use_sb_b)
        else:
            use_sb_for_type = False

        m1_r = m1_row_for_smart_buy
        if m1_r is None:
            try:
                m1_r = m1_chunk.iloc[-1]
            except (TypeError, IndexError, KeyError):
                m1_r = None

        can_execute = True
        if use_sb_for_type and m1_r is not None:
            try:
                if hasattr(m1_r, "get"):
                    ab_ratio = float(
                        m1_r.get(
                            "ask_bid_ratio",
                            1.0 / (float(m1_r.get("bid_ask_ratio", 1.0)) + 1e-10),
                        )
                    )
                    v_power = float(m1_r.get("vol_power", m1_r.get("Vol_power", 100.0)))
                else:
                    ab_ratio = 1.0
                    v_power = 100.0
            except (TypeError, ValueError, KeyError):
                ab_ratio, v_power = 1.0, 100.0
            if ab_ratio < sb_ba or v_power < sb_vp:
                can_execute = False
                out["reason"] = "smart_buy_blocked"

        out["can_execute_entry"] = bool(can_execute)
        if can_execute and not out.get("reason"):
            out["reason"] = "ok"
        return out
    except (TypeError, ValueError, KeyError, AttributeError) as e:
        out["reason"] = f"error:{e}"
        return out


def _apply_synthetic_metrics_m1(df: pd.DataFrame, settings: Dict[str, Any]) -> None:
    """1분 OHLCV에 스마트바이용 합성 수급 컬럼을 채웁니다(동적 백테와 동일 패턴)."""
    if df is None or getattr(df, "empty", True):
        return
    if not bool(settings.get("use_synthetic_realtime", True)):
        return
    try:
        from strategy.synthetic_realtime_fill import (
            synthesize_bid_ask_ratio_vectorized,
            synthesize_vol_power_vectorized,
        )

        for col in ("Vol_power", "Total_ask", "Total_bid"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        p_changes = df["Close"].pct_change().fillna(0).values
        vol_mas = df["Volume"].rolling(20).mean().bfill().values
        vol_ratios = (df["Volume"] / (vol_mas + 1e-10)).values
        vps = df["Vol_power"].values if "Vol_power" in df.columns else np.full(len(df), np.nan)
        nan_vp = np.isnan(vps)
        if np.any(nan_vp):
            vps[nan_vp] = synthesize_vol_power_vectorized(
                p_changes[nan_vp], vol_ratios[nan_vp]
            )
        ratios = synthesize_bid_ask_ratio_vectorized(p_changes, vol_ratios, vps)
        df2 = df.copy()
        df2["Vol_power"] = vps
        df2["vol_power"] = vps
        df2["bid_ask_ratio"] = ratios
        df2["ask_bid_ratio"] = 1.0 / (ratios + 1e-10)
        for c in ("Vol_power", "vol_power", "bid_ask_ratio", "ask_bid_ratio"):
            if c in df2.columns:
                df[c] = df2[c]
    except (TypeError, ValueError, KeyError, ImportError, AttributeError):
        pass


def api_minute_row_key(item: Any) -> str:
    """ka10080 분봉 레코드에서 시각 키(중복 제거용). `warmup_service` 와 동일 규칙."""
    try:
        tm = str(
            item.get("cntr_tm")
            or item.get("cntg_tm")
            or item.get("stck_cntg_hour", "")
        ).strip()
        return tm if len(tm) >= 12 else ""
    except (TypeError, AttributeError, ValueError):
        return ""


def ka10080_raw_rows_to_1m_df(raw_api: Any) -> pd.DataFrame:
    """
    ka10080(tic_scope='1') 응답 행들 → DatetimeIndex OHLCV.
    `WarmupService._minute_api_raw_to_1m_ohlcv` 와 동일 규약(cntr_tm = 봉 종가 시각).
    """
    if not raw_api:
        return pd.DataFrame()
    try:
        df = pd.DataFrame(raw_api)
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
        out.rename(
            columns={
                f_op: "Open",
                f_hg: "High",
                f_lw: "Low",
                f_cl: "Close",
                f_vl: "Volume",
            },
            inplace=True,
        )
        return out[["Open", "High", "Low", "Close", "Volume"]].copy()
    except (TypeError, ValueError, KeyError, AttributeError):
        return pd.DataFrame()


def fetch_m1_from_trading_db(
    conn: Any,
    code: str,
    t0: pd.Timestamp,
    t_hi: pd.Timestamp,
    *,
    settings: Dict[str, Any],
) -> Optional[pd.DataFrame]:
    """`trading_data.db` `bars_1m` 구간 조회(합성 수급 컬럼 포함)."""
    try:
        nk = str(code).strip().zfill(6)
        if t_hi < t0:
            return None
        query = """
            SELECT b.datetime as time, b.open, b.high, b.low, b.close, b.volume,
                   NULL AS vol_power, NULL AS total_ask, NULL AS total_bid
            FROM bars_1m b
            WHERE b.code = ? AND b.datetime BETWEEN ? AND ?
            ORDER BY b.datetime
        """
        df = pd.read_sql_query(
            query,
            conn,
            params=[nk, t0.strftime("%Y-%m-%d %H:%M:%S"), t_hi.strftime("%Y-%m-%d %H:%M:%S")],
        )
        if df is None or df.empty:
            return None
        df.columns = [c.capitalize() if c.lower() != "time" else "time" for c in df.columns]
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.dropna(subset=["time"])
        if df.empty:
            return None
        df = df.set_index("time")
        df["Vol_power"] = np.nan
        df["Total_ask"] = np.nan
        df["Total_bid"] = np.nan
        _apply_synthetic_metrics_m1(df, settings)
        return df
    except (TypeError, ValueError, KeyError, OSError, AttributeError):
        return None


def fetch_m1_via_ka10080_1m(
    dm: Any,
    code: str,
    token: str,
    t0: pd.Timestamp,
    t_hi: pd.Timestamp,
    settings: Dict[str, Any],
) -> pd.DataFrame:
    """
    키움 ka10080(tic_scope=1) 연속조회로 [t0, t_hi] 1분 OHLCV(선택 보강용).
    저정밀 운영에서는 사용하지 않으며, 연구/브리지 설정으로 켠 경우에만 호출됩니다.
    """
    out_empty = pd.DataFrame()
    try:
        if not (token or "").strip():
            return out_empty
        max_pages = int(
            float(settings.get("realtime_signal_bt_hp_bridge_ka10080_max_pages", 15))
        )
        max_pages = max(1, min(max_pages, 40))
        dmin = float(
            settings.get("realtime_warmup_ka10080_page_delay_min", 0.08)
        )
        dmax = float(
            settings.get("realtime_warmup_ka10080_page_delay_max", 0.22)
        )
        if dmax < dmin:
            dmin, dmax = dmax, dmin
        nk = ""
        by_tm: Dict[str, Any] = {}
        for p in range(max_pages):
            try:
                raw_list, nk = dm.fetch_kiwoom_minute_chart_page(
                    str(code).strip().zfill(6),
                    str(token).strip(),
                    (nk or "").strip(),
                    tic_scope="1",
                    fetch_context="bt_hp_bridge",
                )
            except (TypeError, OSError, AttributeError, ValueError):
                break
            if raw_list:
                for item in raw_list:
                    k = api_minute_row_key(item)
                    if k:
                        by_tm[k] = item
            df_all = ka10080_raw_rows_to_1m_df(list(by_tm.values()))
            if df_all is not None and not df_all.empty:
                try:
                    if df_all.index.min() <= t0:
                        break
                except (TypeError, ValueError, KeyError):
                    pass
            if not (nk or "").strip():
                break
            time.sleep(random.uniform(dmin, dmax))
        df_all = ka10080_raw_rows_to_1m_df(list(by_tm.values()))
        if df_all is None or df_all.empty:
            return out_empty
        try:
            sl = df_all.loc[t0:t_hi].copy()
        except (TypeError, KeyError, ValueError):
            return out_empty
        if sl.empty:
            return out_empty
        sl["Vol_power"] = np.nan
        sl["Total_ask"] = np.nan
        sl["Total_bid"] = np.nan
        _apply_synthetic_metrics_m1(sl, settings)
        return sl
    except (TypeError, ValueError, KeyError, AttributeError, OSError):
        return out_empty


def resolve_m1_chunk_for_bt_hp_bridge(
    dm: Any,
    code: str,
    token: Optional[str],
    curr_15m_end_ts: pd.Timestamp,
    now_cap: pd.Timestamp,
    settings: Dict[str, Any],
    *,
    conn: Optional[Any] = None,
    rt_m1_store: Optional[Any] = None,
) -> Optional[pd.DataFrame]:
    """
    BT-HP 브리지용 1분 청크.

    - **기본(운영)**: 실시간 틱으로 쌓인 ``rt_m1_store`` 1분 OHLCV(지연 없음). ``StreamProcessor`` 와 동일 틱.
    - **옵션 REST**: ``realtime_signal_bt_hp_bridge_use_rest_1m`` 가 True 일 때만 틱 구간이 ``t_hi`` 까지
      못 미칠 경우 ka10080 으로 보강(지연·스로틀 감수).
    - **옵션 DB**: ``realtime_signal_bt_hp_bridge_use_trading_db_1m`` + ``conn`` — 로컬 분석용.
    """
    try:
        t_end = pd.Timestamp(curr_15m_end_ts)
        t0 = m1_window_start_for_15m_end(t_end)
        t_hi = min(t_end, pd.Timestamp(now_cap).floor("min"))
        if t_hi < t0:
            return None

        parts: List[pd.DataFrame] = []
        use_tick = bool(settings.get("realtime_signal_bt_hp_bridge_use_tick_1m", True))
        if use_tick and rt_m1_store is not None:
            try:
                df_tick = rt_m1_store.dataframe_for_window(
                    str(code).strip().zfill(6), t0, t_hi, include_open_bucket=True
                )
                if df_tick is not None and not df_tick.empty:
                    _apply_synthetic_metrics_m1(df_tick, settings)
                    parts.append(df_tick)
            except (TypeError, ValueError, OSError, AttributeError, KeyError):
                pass

        use_bt_db = bool(
            settings.get("realtime_signal_bt_hp_bridge_use_trading_db_1m", False)
        )
        if use_bt_db and conn is not None:
            try:
                df_db = fetch_m1_from_trading_db(
                    conn, code, t0, t_hi, settings=settings
                )
                if df_db is not None and not df_db.empty:
                    parts.append(df_db)
            except (TypeError, ValueError, OSError, AttributeError):
                pass

        use_rest = bool(settings.get("realtime_signal_bt_hp_bridge_use_rest_1m", False))
        need_api = True
        if parts:
            try:
                mx = max(
                    pd.Timestamp(p.index.max())
                    for p in parts
                    if p is not None and not getattr(p, "empty", True)
                )
                need_api = bool(mx < t_hi)
            except (TypeError, ValueError, KeyError):
                need_api = True

        if use_rest and need_api and token and dm is not None:
            df_api = fetch_m1_via_ka10080_1m(
                dm, code, str(token).strip(), t0, t_hi, settings
            )
            if df_api is not None and not df_api.empty:
                parts.append(df_api)

        if not parts:
            return None
        # 틱 → DB → REST 순으로 parts 를 쌓았으므로 동일 시각은 앞선 소스(틱)를 유지
        merged = pd.concat(parts, axis=0).sort_index()
        merged = merged[~merged.index.duplicated(keep="first")]
        try:
            merged = merged.loc[t0:t_hi]
        except (TypeError, KeyError, ValueError):
            return None
        if merged.empty:
            return None
        return merged
    except (TypeError, ValueError, KeyError, AttributeError, OSError):
        return None


def fetch_m1_chunk_for_bt_hp_bridge(
    conn: Any,
    code: str,
    curr_15m_end_ts: pd.Timestamp,
    now_cap: pd.Timestamp,
    *,
    settings: Dict[str, Any],
) -> Optional[pd.DataFrame]:
    """하위 호환: DB만 조회(토큰 없이 호출할 때)."""
    try:
        t_end = pd.Timestamp(curr_15m_end_ts)
        t0 = m1_window_start_for_15m_end(t_end)
        t_hi = min(t_end, pd.Timestamp(now_cap).floor("min"))
        return fetch_m1_from_trading_db(conn, code, t0, t_hi, settings=settings)
    except (TypeError, ValueError, AttributeError):
        return None


def sel_track_to_bt_code_track(sel: Any) -> str:
    """감시 `sel_track` → 동적 백테 `code_track` (A/B/AB)."""
    try:
        u = str(sel or "AB").strip().upper()
        if u in ("A", "B", "AB"):
            return u
    except (TypeError, ValueError):
        pass
    return "AB"
