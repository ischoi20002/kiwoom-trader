"""단일 종목 시뮬 유틸.

유니버스·슬롯 없이 해당 종목만 재현하고 generate_trade_report HTML 을 생성한다.
- 기존 전략: SimpleBacktester 로 S1/S2/청산 재현
- 변동성 돌파: 당일 시가 + 전일 레인지×K 터치 시 매수, 당일 종가 청산
최적화/동적백테 산출물(trade_report.html, backtest_trades.csv)은 덮어쓰지 않는다.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import os
import pandas as pd

from backtest_engine import BASE_DIR, SimpleBacktester, TradingStrategy, settings_for_backtest
from reporting.trade_report import generate_trade_report
from strategy.data_loader import BacktestDataLoader
from utils.fee_rates import fee_decimal_rates_backtest
from utils.normalization import canonical_stock_code

WARMUP_CALENDAR_DAYS: int = 200
DEFAULT_REPORT_PATH: str = os.path.join(BASE_DIR, "single_stock_trade_report.html")
# 단일 종목 전용 자금: 포트폴리오 초기자본·슬롯 배분과 분리
SINGLE_STOCK_INITIAL_CAPITAL: int = 10_000_000
SINGLE_STOCK_S1_EQUITY_PCT: float = 44.0
SINGLE_STOCK_S2_EQUITY_PCT: float = 54.0

STRATEGY_S1_S2: str = "s1_s2"
STRATEGY_VOL_BREAKOUT: str = "vol_breakout"
DEFAULT_VOL_BREAKOUT_K: float = 0.5
VOL_BREAKOUT_K_MIN: float = 0.01
VOL_BREAKOUT_K_MAX: float = 2.0
# 당일 청산 전략이므로 수수료 여유를 남기고 현금의 대부분을 투입
VOL_BREAKOUT_EQUITY_PCT: float = 95.0
VOL_BREAKOUT_SIG_TYPE: str = "VOL_BO"

ProgressCallback = Optional[Callable[[str, Optional[float]], None]]


class SingleStockSimError(ValueError):
    """입력·데이터 부족 등 사용자에게 바로 보여줄 시뮬 오류."""


def _notify(callback: ProgressCallback, msg: str, val: Optional[float] = None) -> None:
    """진행 콜백이 있으면 호출한다. 콜백 예외는 시뮬을 막지 않는다."""
    if callback is None:
        return
    try:
        callback(msg, val)
    except Exception:
        try:
            callback(msg)
        except Exception:
            pass


def _parse_iso_date(value: Any, label: str) -> str:
    """YYYY-MM-DD 문자열로 정규화한다."""
    try:
        raw = str(value or "").strip()
        dt = datetime.strptime(raw[:10], "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except Exception as e:
        raise SingleStockSimError(f"{label} 날짜 형식이 올바르지 않습니다. (YYYY-MM-DD)") from e


def _normalize_code(code: Any) -> str:
    """종목코드를 6자리로 맞춘다."""
    try:
        normalized = canonical_stock_code(code)
    except Exception as e:
        raise SingleStockSimError("종목코드를 확인할 수 없습니다.") from e
    if not normalized or len(normalized) != 6:
        raise SingleStockSimError("종목코드는 6자리로 입력해 주세요. (예: 005930)")
    return normalized


def _slice_from_start(df: pd.DataFrame, start_date: str) -> pd.DataFrame:
    """지표 계산 후 요청 시작일 이전 봉을 제거한다."""
    try:
        start_ts = pd.Timestamp(start_date)
        sliced = df.loc[df.index >= start_ts]
        if isinstance(sliced, pd.DataFrame):
            return sliced.copy()
        return pd.DataFrame(sliced).copy()
    except Exception as e:
        raise SingleStockSimError(f"기간 슬라이스에 실패했습니다: {e}") from e


def _build_trades_df(
    tester: SimpleBacktester,
    code: str,
    stock_name: str,
) -> pd.DataFrame:
    """SimpleBacktester.trades 를 리포트용 DataFrame 으로 변환한다."""
    cols = ["Date", "Code", "Name", "Action", "Price", "Qty", "Return", "Reason", "Fee", "Sig_Type"]
    try:
        raw = list(getattr(tester, "trades", None) or [])
    except Exception:
        raw = []
    if not raw:
        return pd.DataFrame(columns=cols)
    try:
        trades_df = pd.DataFrame(raw)
        trades_df["Code"] = code
        trades_df["Name"] = stock_name
        if "Reason" in trades_df.columns:
            trades_df["Reason"] = trades_df["Reason"].astype(str).str.replace(",", ";", regex=False)
        if "Date" in trades_df.columns:
            trades_df["Date"] = pd.to_datetime(trades_df["Date"], errors="coerce")
        for col in cols:
            if col not in trades_df.columns:
                trades_df[col] = "" if col in ("Name", "Action", "Reason", "Sig_Type", "Code") else 0
        return trades_df
    except Exception as e:
        raise SingleStockSimError(f"매매 내역 변환에 실패했습니다: {e}") from e


def _normalize_strategy(value: Any) -> str:
    """전략 키를 s1_s2 / vol_breakout 으로 정규화한다."""
    try:
        raw = str(value or "").strip().lower().replace(" ", "").replace("_", "")
    except Exception:
        return STRATEGY_S1_S2
    if raw in (
        "volbreakout",
        "volatility",
        "volatilitybreakout",
        "vb",
        "변동성",
        "변동성돌파",
    ):
        return STRATEGY_VOL_BREAKOUT
    return STRATEGY_S1_S2


def _parse_vol_k(value: Any) -> float:
    """변동성 돌파 K 값을 검증한다."""
    try:
        k = float(value)
    except (TypeError, ValueError) as e:
        raise SingleStockSimError(
            f"K 값이 올바르지 않습니다. ({VOL_BREAKOUT_K_MIN} ~ {VOL_BREAKOUT_K_MAX})"
        ) from e
    if k < VOL_BREAKOUT_K_MIN or k > VOL_BREAKOUT_K_MAX:
        raise SingleStockSimError(
            f"K 값은 {VOL_BREAKOUT_K_MIN} ~ {VOL_BREAKOUT_K_MAX} 사이여야 합니다. (입력: {k})"
        )
    return k


def _session_day_groups(df: pd.DataFrame) -> list[tuple[pd.Timestamp, pd.DataFrame]]:
    """인트라데이 봉을 거래일(자정 정규화) 단위로 묶는다."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return []
    try:
        work = df.copy()
        if not isinstance(work.index, pd.DatetimeIndex):
            if "Date" in work.columns:
                work = work.set_index(pd.to_datetime(work["Date"], errors="coerce"))
            else:
                return []
        work = work[work.index.notna()].sort_index()
        if work.empty:
            return []
        grouped = work.groupby(work.index.normalize(), sort=True)
        out: list[tuple[pd.Timestamp, pd.DataFrame]] = []
        for day, grp in grouped:
            try:
                if grp is None or grp.empty:
                    continue
                out.append((pd.Timestamp(day), grp.copy()))
            except Exception:
                continue
        return out
    except Exception:
        return []


def _first_breakout_fill(
    day_bars: pd.DataFrame,
    target: float,
) -> Optional[tuple[pd.Timestamp, float]]:
    """당일 봉에서 목표가 최초 터치 시각·체결가를 찾는다.

    시가가 이미 목표 이상이면 시가(갭 관통), 아니면 목표가.
    """
    try:
        tgt = float(target)
    except (TypeError, ValueError):
        return None
    if tgt <= 0 or day_bars is None or not isinstance(day_bars, pd.DataFrame) or day_bars.empty:
        return None
    for ts, row in day_bars.iterrows():
        try:
            o = float(row.get("Open", 0) or 0)
            h = float(row.get("High", 0) or 0)
        except (TypeError, ValueError):
            continue
        if h < tgt:
            continue
        try:
            fill = o if o >= tgt else tgt
        except Exception:
            fill = tgt
        if fill <= 0:
            continue
        try:
            fill_ts = pd.Timestamp(ts)
        except Exception:
            continue
        return fill_ts, float(fill)
    return None


def _day_close_fill(day_bars: pd.DataFrame) -> Optional[tuple[pd.Timestamp, float]]:
    """당일 마지막 봉의 종가·시각을 반환한다."""
    if day_bars is None or not isinstance(day_bars, pd.DataFrame) or day_bars.empty:
        return None
    try:
        last = day_bars.iloc[-1]
        px = float(last.get("Close", 0) or 0)
        if px <= 0:
            return None
        return pd.Timestamp(day_bars.index[-1]), px
    except Exception:
        return None


def simulate_vol_breakout_trades(
    df15: pd.DataFrame,
    start_date: str,
    end_date: str,
    k: float,
    *,
    m1_df: Optional[pd.DataFrame] = None,
    initial_capital: int = SINGLE_STOCK_INITIAL_CAPITAL,
    equity_pct: float = VOL_BREAKOUT_EQUITY_PCT,
    buy_fee_rate: float = 0.00015,
    sell_fee_rate: float = 0.00215,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """변동성 돌파 매매 목록과 요약을 만든다. (데이터 로드와 분리 — 테스트용)

    목표가 = 당일 시가 + (전일 고가 − 전일 저가) × K
    장중 최초 터치 시 매수, 당일 15분봉 종가에 전량 청산. 하룻밤에 포지션을 넘기지 않는다.
    """
    trades: list[dict[str, Any]] = []
    try:
        start_ts = pd.Timestamp(start_date).normalize()
        end_ts = pd.Timestamp(end_date).normalize()
    except Exception as e:
        raise SingleStockSimError(f"변동성 돌파 기간을 해석하지 못했습니다: {e}") from e

    try:
        k_val = float(k)
        cap0 = float(initial_capital)
        deploy = max(1.0, min(100.0, float(equity_pct))) / 100.0
        br = max(0.0, float(buy_fee_rate))
        sr = max(0.0, float(sell_fee_rate))
    except (TypeError, ValueError) as e:
        raise SingleStockSimError(f"변동성 돌파 자금/수수료 설정이 올바르지 않습니다: {e}") from e
    if cap0 <= 0:
        raise SingleStockSimError("초기 자금이 0 이하입니다.")

    days_15 = _session_day_groups(df15)
    if not days_15:
        raise SingleStockSimError("변동성 돌파에 사용할 15분봉이 없습니다.")

    m1_map: dict[pd.Timestamp, pd.DataFrame] = {}
    try:
        if m1_df is not None and isinstance(m1_df, pd.DataFrame) and not m1_df.empty:
            m1_map = {day: grp for day, grp in _session_day_groups(m1_df)}
    except Exception:
        m1_map = {}

    balance = cap0
    prev_high: Optional[float] = None
    prev_low: Optional[float] = None
    sell_rets: list[float] = []
    peak = cap0
    mdd = 0.0

    for day, bars15 in days_15:
        try:
            d_high = float(bars15["High"].max())
            d_low = float(bars15["Low"].min())
            d_open = float(bars15["Open"].iloc[0])
        except Exception:
            d_high, d_low, d_open = 0.0, 0.0, 0.0

        in_range = start_ts <= day <= end_ts
        if (
            in_range
            and prev_high is not None
            and prev_low is not None
            and d_open > 0
        ):
            try:
                rng = float(prev_high) - float(prev_low)
            except (TypeError, ValueError):
                rng = 0.0
            if rng > 0:
                target = d_open + rng * k_val
                touch_bars = m1_map.get(day, bars15)
                fill = _first_breakout_fill(touch_bars, target)
                close_fill = _day_close_fill(bars15)
                if fill is not None and close_fill is not None:
                    entry_ts, entry_px = fill
                    exit_ts, exit_px = close_fill
                    try:
                        qty = int(balance * deploy // (entry_px * (1.0 + br)))
                    except Exception:
                        qty = 0
                    if qty > 0:
                        buy_fee = int(entry_px * qty * br)
                        sell_fee = int(exit_px * qty * sr)
                        try:
                            ret_v = (exit_px / entry_px - 1.0) * 100.0
                        except Exception:
                            ret_v = 0.0
                        reason_buy = (
                            f"[VOL_BO] 변동성돌파 K={k_val:g} "
                            f"목표가={target:,.0f} 전일레인지={rng:,.0f}"
                        )
                        trades.append(
                            {
                                "Date": entry_ts,
                                "Action": "BUY_1ST",
                                "Price": float(entry_px),
                                "Qty": int(qty),
                                "Return": 0.0,
                                "Reason": reason_buy,
                                "Fee": buy_fee,
                                "Sig_Type": VOL_BREAKOUT_SIG_TYPE,
                            }
                        )
                        trades.append(
                            {
                                "Date": exit_ts,
                                "Action": "SELL",
                                "Price": float(exit_px),
                                "Qty": int(qty),
                                "Return": float(ret_v),
                                "Reason": "당일종가청산",
                                "Fee": sell_fee,
                                "Sig_Type": VOL_BREAKOUT_SIG_TYPE,
                            }
                        )
                        balance = balance - (entry_px * qty + buy_fee) + (exit_px * qty - sell_fee)
                        sell_rets.append(float(ret_v))
                        try:
                            if balance > peak:
                                peak = balance
                            if peak > 0:
                                dd = (balance / peak - 1.0) * 100.0
                                if dd < mdd:
                                    mdd = dd
                        except Exception:
                            pass

        if d_high > 0 and d_low > 0:
            prev_high, prev_low = d_high, d_low

    n_exits = len(sell_rets)
    try:
        win_rate = (sum(1 for r in sell_rets if r > 0) / n_exits * 100.0) if n_exits else 0.0
    except Exception:
        win_rate = 0.0
    try:
        final_ret = (balance / cap0 - 1.0) * 100.0 if cap0 > 0 else 0.0
    except Exception:
        final_ret = 0.0

    summary: dict[str, Any] = {
        "final_ret": float(final_ret),
        "win_rate": float(win_rate),
        "trades": int(n_exits),
        "mdd": float(mdd),
        "final_balance": float(balance),
        "k": float(k_val),
        "strategy": STRATEGY_VOL_BREAKOUT,
    }
    return trades, summary


def _exit_stats(trades_df: pd.DataFrame, summary: dict[str, Any]) -> tuple[int, float, float]:
    """청산 건수·승률·평균수익률을 리포트 헤더용으로 계산한다."""
    try:
        if trades_df is not None and not trades_df.empty and "Action" in trades_df.columns:
            exits = trades_df[trades_df["Action"].astype(str).isin(["SELL", "HOLD"])]
            n_exits = int(len(exits))
        else:
            n_exits = int(summary.get("trades") or 0)
    except Exception:
        n_exits = int(summary.get("trades") or 0)
    try:
        win_rate = float(summary.get("win_rate") or 0.0)
    except Exception:
        win_rate = 0.0
    try:
        avg_ret = float(summary.get("final_ret") or 0.0)
    except Exception:
        avg_ret = 0.0
    return n_exits, win_rate, avg_ret


def run_single_stock_simulation(
    settings: dict[str, Any],
    code: str,
    start_date: str,
    end_date: str,
    high_precision: bool = False,
    output_path: str = DEFAULT_REPORT_PATH,
    callback: ProgressCallback = None,
    strategy: str = STRATEGY_S1_S2,
    vol_k: float = DEFAULT_VOL_BREAKOUT_K,
) -> dict[str, Any]:
    """특정 종목의 매매를 기간 동안 재현하고 HTML 리포트를 만든다.

    Parameters
    ----------
    settings:
        GUI/settings.json 설정. 엔진 전달 전에 settings_for_backtest 로 복사한다.
    code:
        종목코드 (6자리 또는 앞 0 생략).
    start_date, end_date:
        YYYY-MM-DD.
    high_precision:
        True 이면 1분봉 인트라바 경로(기존 전략·변동성 돌파 최초 터치).
    output_path:
        HTML 저장 경로. 기본값은 single_stock_trade_report.html.
    callback:
        (메시지, 진행률) 진행 알림.
    strategy:
        ``s1_s2``(기존 Type A/B) 또는 ``vol_breakout``(변동성 돌파).
    vol_k:
        변동성 돌파 K. 기존 전략에서는 무시한다.

    Returns
    -------
    dict
        summary, trades_df, report_path, code, name, n_trades, high_precision
    """
    code_n = _normalize_code(code)
    start_s = _parse_iso_date(start_date, "시작")
    end_s = _parse_iso_date(end_date, "종료")
    if start_s > end_s:
        raise SingleStockSimError("시작일이 종료일보다 늦습니다.")

    strat = _normalize_strategy(strategy)
    k_val = DEFAULT_VOL_BREAKOUT_K
    if strat == STRATEGY_VOL_BREAKOUT:
        k_val = _parse_vol_k(vol_k)

    try:
        active = settings_for_backtest(dict(settings or {}))
        # 단일 종목 전용 자금(포트폴리오 초기자본·슬롯 비중과 무관)
        active["backtest_initial_capital"] = SINGLE_STOCK_INITIAL_CAPITAL
        active["s1_equity_pct"] = SINGLE_STOCK_S1_EQUITY_PCT
        active["s2_equity_pct"] = SINGLE_STOCK_S2_EQUITY_PCT
        active["s2_b_equity_pct"] = SINGLE_STOCK_S2_EQUITY_PCT
    except SingleStockSimError:
        raise
    except Exception as e:
        raise SingleStockSimError(f"전략 설정 준비에 실패했습니다: {e}") from e

    try:
        start_dt = datetime.strptime(start_s, "%Y-%m-%d")
        warmup_start = (start_dt - timedelta(days=WARMUP_CALENDAR_DAYS)).strftime("%Y-%m-%d")
    except Exception as e:
        raise SingleStockSimError(f"워밍업 시작일을 계산하지 못했습니다: {e}") from e

    try:
        hp = bool(high_precision)
    except Exception:
        hp = False

    out_path = output_path or DEFAULT_REPORT_PATH
    try:
        out_path = os.path.abspath(str(out_path))
    except Exception:
        out_path = DEFAULT_REPORT_PATH

    loader = BacktestDataLoader()
    _notify(callback, f"{code_n} 15분봉 로딩 중...", 10)
    try:
        df15 = loader.get_data(code_n, warmup_start, end_s, interval="15m")
    except Exception as e:
        raise SingleStockSimError(f"15분봉 로드 실패 ({code_n}): {e}") from e
    if df15 is None or df15.empty:
        raise SingleStockSimError(
            f"{code_n} 의 15분봉이 없습니다. ({warmup_start} ~ {end_s})\n"
            "시세 수집 후 다시 실행해 주세요."
        )

    m1_df: Optional[pd.DataFrame] = None
    if hp:
        _notify(callback, f"{code_n} 1분봉 로딩 중...", 25)
        try:
            m1_df = loader.get_data(code_n, warmup_start, end_s, interval="1m")
        except Exception as e:
            raise SingleStockSimError(f"1분봉 로드 실패 ({code_n}): {e}") from e
        if m1_df is None or m1_df.empty:
            raise SingleStockSimError(
                f"{code_n} 의 1분봉이 없습니다. 고정밀을 끄거나 1분봉을 수집한 뒤 다시 실행해 주세요."
            )

    try:
        idx_choice = str(active.get("universe_index_choice", "KODEX200") or "KODEX200")
        name_map = TradingStrategy.get_stock_name_map(idx_choice)
        stock_name = str(name_map.get(code_n, code_n) or code_n)
    except Exception:
        stock_name = code_n

    chart_title: Optional[str] = None
    if strat == STRATEGY_VOL_BREAKOUT:
        df_sim = _slice_from_start(df15, start_s)
        if df_sim.empty:
            raise SingleStockSimError(
                f"{code_n} 의 {start_s} ~ {end_s} 구간에 15분봉이 없습니다."
            )
        _notify(callback, f"{stock_name}({code_n}) 변동성 돌파 실행 중...", 60)
        try:
            buy_r, sell_r = fee_decimal_rates_backtest(active)
        except Exception:
            buy_r, sell_r = 0.00015, 0.00215
        try:
            raw_trades, summary = simulate_vol_breakout_trades(
                df15,
                start_s,
                end_s,
                k_val,
                m1_df=m1_df,
                initial_capital=SINGLE_STOCK_INITIAL_CAPITAL,
                equity_pct=VOL_BREAKOUT_EQUITY_PCT,
                buy_fee_rate=buy_r,
                sell_fee_rate=sell_r,
            )
        except SingleStockSimError:
            raise
        except Exception as e:
            raise SingleStockSimError(f"변동성 돌파 시뮬레이션 실패: {e}") from e
        tester_like = type("_TradesHolder", (), {"trades": raw_trades})()
        trades_df = _build_trades_df(tester_like, code_n, stock_name)
        price_src = None
        try:
            keep_cols = [c for c in ("Close", "Open", "High", "Low") if c in df_sim.columns]
            if keep_cols:
                price_src = df_sim[keep_cols].copy()
        except Exception:
            price_src = None
        chart_title = f"{stock_name} ({code_n}) 변동성 돌파 (K={k_val:g})"
    else:
        _notify(callback, "지표 계산 중...", 40)
        try:
            df15 = TradingStrategy.calculate_indicators(df15, active)
        except Exception as e:
            raise SingleStockSimError(f"지표 계산에 실패했습니다: {e}") from e
        if df15 is None or df15.empty:
            raise SingleStockSimError("지표 계산 결과가 비어 있습니다.")

        df_sim = _slice_from_start(df15, start_s)
        if df_sim.empty:
            raise SingleStockSimError(
                f"{code_n} 의 {start_s} ~ {end_s} 구간에 15분봉이 없습니다."
            )

        _notify(callback, f"{stock_name}({code_n}) 시뮬레이션 실행 중...", 60)
        tester = SimpleBacktester(active)
        try:
            summary = tester.run(
                df_sim,
                code_n,
                market_df=None,
                vix_df=None,
                log_to_csv=False,
                skip_indicators=True,
                high_precision=hp,
                m1_df_preloaded=m1_df,
            )
        except Exception as e:
            raise SingleStockSimError(f"시뮬레이션 실행 실패: {e}") from e
        if not summary:
            raise SingleStockSimError(
                f"{code_n} 시뮬레이션이 실패했습니다. 로그의 [Error] Simulation failed 를 확인해 주세요."
            )

        trades_df = _build_trades_df(tester, code_n, stock_name)
        price_src = None
        try:
            eq_src = getattr(tester, "df", None)
            if eq_src is not None and isinstance(eq_src, pd.DataFrame) and not eq_src.empty and "Close" in eq_src.columns:
                keep_cols = [c for c in ("Close", "Open", "High", "Low", "MA5", "MA20") if c in eq_src.columns]
                price_src = eq_src[keep_cols].copy()
            elif df_sim is not None and isinstance(df_sim, pd.DataFrame) and "Close" in df_sim.columns:
                keep_cols = [c for c in ("Close", "MA5", "MA20") if c in df_sim.columns]
                price_src = df_sim[keep_cols].copy()
        except Exception:
            price_src = None

    n_exits, win_rate, avg_ret = _exit_stats(trades_df, summary)

    _notify(callback, "리포트 생성 중...", 85)
    try:
        report_df = trades_df.copy() if trades_df is not None else pd.DataFrame()
        generate_trade_report(
            report_df,
            "15m",
            out_path,
            n_exits,
            win_rate,
            avg_ret,
            active,
            callback=callback,
            target_codes=[code_n],
            start_date=start_s,
            end_date=end_s,
            daily_equity=None,
            high_precision=hp,
            price_df=price_src,
            price_chart_title=chart_title,
        )
    except Exception as e:
        raise SingleStockSimError(f"리포트 생성에 실패했습니다: {e}") from e

    if not os.path.exists(out_path):
        raise SingleStockSimError("리포트 파일이 생성되지 않았습니다.")

    try:
        n_trades = int(len(trades_df)) if trades_df is not None else 0
    except Exception:
        n_trades = 0

    _notify(callback, "완료", 100)
    return {
        "summary": summary,
        "trades_df": trades_df,
        "report_path": out_path,
        "code": code_n,
        "name": stock_name,
        "n_trades": n_trades,
        "n_exits": n_exits,
        "high_precision": hp,
        "start_date": start_s,
        "end_date": end_s,
        "strategy": strat,
        "vol_k": k_val if strat == STRATEGY_VOL_BREAKOUT else None,
    }
