# -*- coding: utf-8 -*-
"""
실시간 감시 '일일 결산' HTML 리포트.
- 거래일별 갭 채우기, 메타데이터(완료/부분), Plotly 15분봉(캔들)+주문 오버레이
- 15분 차트는 trading_data.db 없이 키움 ka10080(1분) REST만 사용 (실패 시 부분 리포트)
"""
from __future__ import annotations

import html
import json
import os
from datetime import date, datetime, timedelta
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple


try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    _HAS_PLOTLY = True
except Exception:
    _HAS_PLOTLY = False

from reporting.krx_trading_calendar import (
    next_trading_day,
    previous_trading_day,
    trading_days_inclusive,
)
from reporting.plotly_deferred_chart_js import PLOTLY_DEFERRED_CHART_IIFE
from utils.fee_rates import fee_decimal_rates_live_trading


def _esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


def _fmt_vp_report_cell(v: Any) -> str:
    """리포트 표용 체결강도(소수 1자리 %)."""
    if v is None:
        return "-"
    try:
        return f"{float(v):.1f}%"
    except Exception:
        return "-"


def _is_buy_side(side: Any) -> bool:
    s = str(side or "").strip().lower()
    return s in ("buy", "b") or "매수" in str(side or "")


def _parse_ts_loose(s: Any) -> Optional[datetime]:
    """주문/청산 시각 문자열을 datetime으로 (초 단위 비교용)."""
    if s is None:
        return None
    t = str(s).strip()
    if len(t) >= 19:
        t = t[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(t)
    except ValueError:
        return None


def _fill_qty_price(o: Dict[str, Any]) -> Tuple[int, float]:
    """체결 수량·가격 (미체결 시 주문 수량·주문가 폴백)."""
    try:
        fq = int(o.get("filled_qty") or 0)
    except (TypeError, ValueError):
        fq = 0
    if fq <= 0:
        try:
            fq = int(o.get("qty") or 0)
        except (TypeError, ValueError):
            fq = 0
    try:
        fp = float(o.get("filled_price") or 0)
    except (TypeError, ValueError):
        fp = 0.0
    if fp <= 0:
        try:
            fp = float(o.get("price") or 0)
        except (TypeError, ValueError):
            fp = 0.0
    return fq, fp


def _fmt_krw_cell(v: Optional[float]) -> str:
    if v is None:
        return "—"
    try:
        return f"{int(round(float(v))):,}"
    except Exception:
        return "—"


def _fmt_pct_net_cell(v: Optional[float]) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):+.2f}%"
    except Exception:
        return "—"


def _virtual_history_sells_for_day(
    portfolio: Any, report_day: str, excl: set
) -> Dict[str, List[Dict[str, Any]]]:
    """해당 영업일 청산된 가상 이력(코드별). 수익은 포트폴리오 on_fill과 동일하게 수수료 반영됨."""
    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    day_key = str(report_day)[:10]
    for h in getattr(portfolio, "virtual_history", None) or []:
        try:
            c = str(h.get("code", "")).strip().zfill(6)
        except Exception:
            continue
        if c in excl:
            continue
        exd = str(h.get("exit_date", "") or "")[:10]
        if exd != day_key:
            continue
        exdt = h.get("exit_datetime")
        if not exdt:
            exdt = f"{exd} {h.get('exit_time', '12:00:00')}"
        try:
            rp = float(h.get("realized_pnl_amt", 0) or 0)
            pp = float(h.get("final_pnl_pct", 0) or 0)
        except (TypeError, ValueError):
            rp, pp = 0.0, 0.0
        out[c].append(
            {
                "exit_datetime": exdt,
                "realized_pnl_amt": rp,
                "final_pnl_pct": pp,
                "_used": False,
            }
        )
    for c in out:
        out[c].sort(key=lambda x: str(x.get("exit_datetime", "")))
    return dict(out)


def _pop_matched_exit_history(
    pool: List[Dict[str, Any]], order_ts: str, max_seconds: float = 600.0
) -> Optional[Dict[str, Any]]:
    """주문 시각과 가장 가까운 미사용 청산 이력 1건 (수익률·실현손익 신뢰 소스)."""
    ts = _parse_ts_loose(order_ts)
    if ts is None:
        return None
    best_i = -1
    best_diff = 1e18
    for i, h in enumerate(pool):
        if h.get("_used"):
            continue
        ht = _parse_ts_loose(h.get("exit_datetime"))
        if ht is None:
            continue
        try:
            diff = abs((ht - ts).total_seconds())
        except Exception:
            continue
        if diff < best_diff:
            best_diff = diff
            best_i = i
    if best_i < 0 or best_diff > max_seconds:
        return None
    chosen = pool[best_i]
    chosen["_used"] = True
    return chosen


def _enrich_single_code_orders(
    sorted_orders: List[Dict[str, Any]],
    pool: List[Dict[str, Any]],
    settings: dict,
) -> List[Dict[str, Any]]:
    """
    한 종목의 주문 행(시간순)에 수수료·매수/매도액·손익 열을 붙입니다.
    pool: 청산 이력(가상 매칭용) — _pop_matched_exit_history 가 소비합니다.
    """
    buy_r, sell_r = fee_decimal_rates_live_trading(settings)
    state_q = 0
    state_cost = 0.0
    cum_realized = 0.0
    rows_out: List[Dict[str, Any]] = []

    for o in sorted_orders:
        fq, fp = _fill_qty_price(o)
        gross = float(fq) * float(fp)
        is_buy = _is_buy_side(o.get("side"))

        fee_amt: Optional[float] = None
        buy_amt: Optional[float] = None
        sell_amt: Optional[float] = None
        pnl_pct: Optional[float] = None
        pnl_net: Optional[float] = None
        disp_cum: Optional[float] = None

        if fq <= 0 or fp <= 0:
            rows_out.append(
                {
                    **o,
                    "_fee_amt": None,
                    "_buy_amt": None,
                    "_sell_amt": None,
                    "_pnl_pct": None,
                    "_disp_cum": None,
                }
            )
            continue

        if is_buy:
            fee_amt = gross * buy_r
            buy_amt = gross
            state_q += fq
            state_cost += gross + fee_amt
        else:
            fee_amt = gross * sell_r
            sell_amt = gross
            net_proceeds = gross - fee_amt

            intra_pnl: Optional[float] = None
            intra_pct: Optional[float] = None
            try:
                if state_q >= fq and fq > 0 and state_cost > 0:
                    alloc = state_cost * (float(fq) / float(state_q))
                    intra_pnl = net_proceeds - alloc
                    intra_pct = (intra_pnl / alloc * 100.0) if alloc > 0 else None
            except Exception:
                intra_pnl, intra_pct = None, None

            matched = _pop_matched_exit_history(pool, str(o.get("ts", "")))
            if matched is not None:
                try:
                    pnl_net = float(matched.get("realized_pnl_amt", 0) or 0)
                    pnl_pct = float(matched.get("final_pnl_pct", 0) or 0)
                except (TypeError, ValueError):
                    pnl_net, pnl_pct = None, None
            if pnl_net is None:
                pnl_net = intra_pnl
                pnl_pct = intra_pct

            if pnl_net is not None:
                cum_realized += pnl_net
                disp_cum = cum_realized
            elif cum_realized > 0:
                disp_cum = cum_realized

            try:
                if state_q >= fq and fq > 0 and state_cost > 0:
                    alloc_u = state_cost * (float(fq) / float(state_q))
                    state_cost -= alloc_u
                state_q = max(0, state_q - fq)
                if state_q == 0:
                    state_cost = 0.0
            except Exception:
                state_q = max(0, state_q - fq)

        rows_out.append(
            {
                **o,
                "_fee_amt": fee_amt,
                "_buy_amt": buy_amt,
                "_sell_amt": sell_amt,
                "_pnl_pct": pnl_pct,
                "_disp_cum": disp_cum,
            }
        )

    return rows_out


def enrich_order_rows_for_virtual_trade(
    orders: List[Dict[str, Any]],
    settings: dict,
    exit_snap: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    종합 리포트용: 한 종목·한 라운드(진입~청산 또는 보유 중) 주문 행에
    일일 결산과 동일한 수수료·손익 열을 보강합니다.

    exit_snap: 청산된 라운드 1건의 가상 이력( exit_datetime, realized_pnl_amt, final_pnl_pct ).
    보유 중이면 None — 매도 행은 주문 시퀀스 기준 추정 손익만 표시됩니다.
    """
    pool: List[Dict[str, Any]] = []
    if exit_snap is not None:
        exdt = str(exit_snap.get("exit_datetime") or "").strip()
        if not exdt:
            ed = str(exit_snap.get("exit_date") or "")[:10]
            etx = str(exit_snap.get("exit_time") or "")
            if ed and etx:
                etx_s = str(etx).strip()
                if len(etx_s) >= 8 and ":" in etx_s:
                    exdt = f"{ed} {etx_s[:8]}"
                else:
                    exdt = f"{ed} {etx_s}"
        if exdt:
            try:
                rp = float(exit_snap.get("realized_pnl_amt", 0) or 0)
                pp = float(exit_snap.get("final_pnl_pct", 0) or 0)
            except (TypeError, ValueError):
                rp, pp = 0.0, 0.0
            pool = [
                {
                    "exit_datetime": exdt.replace("T", " ")[:19]
                    if len(exdt) >= 19
                    else exdt,
                    "realized_pnl_amt": rp,
                    "final_pnl_pct": pp,
                    "_used": False,
                }
            ]
    sorted_o = sorted(list(orders), key=lambda x: str(x.get("ts", "")))
    return _enrich_single_code_orders(sorted_o, pool, settings)


def _build_enriched_order_rows(
    by_code: Dict[str, List[Dict[str, Any]]],
    settings: dict,
    portfolio: Any,
    excl: set,
    report_day: str,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    일일 결산 표용: 수수료, 매수/매도 금액, 순수익률·순누적실현.
    순손익은 가상 이력 매칭 우선, 없으면 당일 주문만으로 평단 복원 시 계산.
    """
    hist_by_code = _virtual_history_sells_for_day(portfolio, report_day, excl)
    enriched: Dict[str, List[Dict[str, Any]]] = {}

    for code, orders in by_code.items():
        ckey = str(code).strip().zfill(6)
        pool = [dict(x) for x in hist_by_code.get(ckey, [])]
        sorted_o = sorted(orders, key=lambda x: str(x.get("ts", "")))
        enriched[code] = _enrich_single_code_orders(sorted_o, pool, settings)

    return enriched


def report_dir_for_date(day: str) -> str:
    """realtime_session_logs/{YYYY-MM-DD}/ 절대 경로."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "realtime_session_logs", day)


def report_html_path(day: str) -> str:
    return os.path.join(report_dir_for_date(day), "daily_settlement.html")


def _session_end_local(report_day: str, buffer_minutes: int) -> datetime:
    """KST 로컬 기준 장 마감(15:30) + 버퍼."""
    d = datetime.strptime(report_day[:10], "%Y-%m-%d")
    return d.replace(hour=15, minute=30, second=0, microsecond=0) + timedelta(minutes=max(0, int(buffer_minutes)))


def generation_is_complete(generated_at: datetime, report_day: str, buffer_minutes: int) -> bool:
    """
    완료 리포트: (1) 생성일이 리포트일 다음날 이후(새벽 생성 허용)
    또는 (2) 같은 날이면 장마감+버퍼 이후.
    """
    try:
        rd = datetime.strptime(report_day[:10], "%Y-%m-%d").date()
    except Exception:
        return False
    gdate = generated_at.date()
    if gdate > rd:
        return True
    if gdate == rd:
        return generated_at >= _session_end_local(report_day, buffer_minutes)
    return False


def read_embedded_meta(html_text: str) -> Optional[Dict[str, Any]]:
    """HTML 본문에서 daily-report-meta JSON 추출."""
    try:
        key = 'id="daily-report-meta"'
        i0 = html_text.find(key)
        if i0 < 0:
            return None
        i = html_text.find(">", i0)
        if i < 0:
            return None
        i += 1
        j = html_text.find("</script>", i)
        if j < 0:
            return None
        raw = html_text[i:j].strip()
        return json.loads(raw)
    except Exception:
        return None


def report_needs_regeneration(
    path: str,
    report_day: str,
    buffer_minutes: int,
) -> bool:
    """파일이 없거나, 완료 조건을 만족하지 않으면 True."""
    if not path or not os.path.isfile(path):
        return True
    try:
        with open(path, "r", encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return True
    meta = read_embedded_meta(txt)
    gen_s = (meta or {}).get("generated_at") or ""
    try:
        if gen_s:
            generated_at = datetime.strptime(gen_s[:19], "%Y-%m-%d %H:%M:%S")
        else:
            generated_at = datetime.fromtimestamp(os.path.getmtime(path))
    except Exception:
        generated_at = datetime.fromtimestamp(os.path.getmtime(path))
    if not generation_is_complete(generated_at, report_day, buffer_minutes):
        return True
    status = (meta or {}).get("status", "complete")
    if status == "partial":
        # 부분 리포트는 재실행 시 갱신 가능(사용자 재시도)
        return True
    return False


def _normalize_total_assets_display(
    total_assets: float, current_cash: float, stock_eval: float
) -> float:
    """
    API tot_evlu_amt 가 주식 평가만 담는 등으로 총자산이 예수+평가보다 작게 나올 때 보정.
    (포지션 관리 탭 _normalize_total_assets_display 와 동일)
    """
    try:
        computed = float(current_cash) + float(stock_eval)
        if computed <= 0:
            return float(total_assets)
        ta = float(total_assets)
        if ta <= 0:
            return computed
        if ta < computed * 0.88:
            return computed
        return ta
    except Exception:
        return float(total_assets)


def _compute_total_assets_for_daily_report(portfolio: Any, stock_eval: float) -> float:
    """
    일일 결산·포지션 관리 탭과 동일: D+2 예수 + 주식평가(stock_eval).
    tot_evlu_amt는 합계와 ±2.5% 이내면 증권사 단일값으로 정합.
    """
    try:
        d2 = float(getattr(portfolio, "d2_deposit", 0) or 0)
        api_mv = float(getattr(portfolio, "holdings_stocks_mv_api", 0) or 0)
        se = float(stock_eval) if float(stock_eval or 0) > 0 else api_mv
        combined = d2 + se
        raw_te = float(getattr(portfolio, "last_tot_evlu_amt", 0) or 0)
        pte = float(getattr(portfolio, "total_equity", 0) or 0)
        if abs(combined) > 1e-6 or se > 0 or d2 != 0:
            if raw_te > 0:
                den = max(abs(raw_te), abs(combined), 1.0)
                if abs(raw_te - combined) / den <= 0.025:
                    return float(raw_te)
            return float(combined)
        if raw_te > 0:
            return float(raw_te)
        if pte > 0:
            return float(pte)
        cash = float(getattr(portfolio, "actual_deposit", 0) or 0)
        if cash > 0:
            return float(cash)
        if api_mv > 0:
            return float(api_mv)
        return 0.0
    except Exception:
        try:
            return float(getattr(portfolio, "total_equity", 0) or 0)
        except Exception:
            return 0.0


def _compute_summary_from_app(app: Any, report_day: str) -> Dict[str, Any]:
    """VirtualTab 요약과 유사. report_day 기준 금일 실현 필터."""
    portfolio = app.portfolio
    monitor_info = getattr(app, "monitor_info", {}) or {}
    today_str = report_day

    total_eval = 0.0
    total_unrealized = 0.0
    for code, pos in portfolio.virtual_positions.items():
        info = monitor_info.get(code, {})
        curr_p = float(info.get("price", pos.get("avg_price", 0)))
        qty = int(pos.get("qty", 0))
        avg_p = float(pos.get("avg_price", 0))
        eval_amt = curr_p * qty
        unrealized = (curr_p - avg_p) * qty
        total_eval += eval_amt
        total_unrealized += unrealized

    total_day_realized = 0.0
    for pos in portfolio.virtual_history:
        pnl_amt = float(pos.get("realized_pnl_amt", 0) or 0)
        if str(pos.get("entry_time", "")).startswith(today_str):
            total_day_realized += pnl_amt

    current_cash = float(getattr(portfolio, "actual_deposit", 0) or 0)
    api_mv = float(getattr(portfolio, "holdings_stocks_mv_api", 0) or 0)
    stock_eval = api_mv if api_mv > 0 else total_eval
    total_assets = _compute_total_assets_for_daily_report(portfolio, stock_eval)

    day_pnl_total = total_day_realized + total_unrealized
    try:
        start_cap = float(str(app.settings.get("backtest_initial_capital", 10000000)).replace(",", ""))
    except Exception:
        start_cap = 10000000.0
    safe_cap = max(1.0, start_cap if total_assets == 0 else (total_assets - day_pnl_total))
    day_pnl_pct = (day_pnl_total / safe_cap) * 100 if safe_cap else 0.0
    total_profit_pct = (total_assets / safe_cap - 1) * 100 if safe_cap else 0.0

    d1 = float(getattr(portfolio, "d1_deposit", 0) or 0)
    d2 = float(getattr(portfolio, "d2_deposit", 0) or 0)

    return {
        "total_assets": total_assets,
        "cash": current_cash,
        "stock_eval": stock_eval,
        "d1_cash": d1,
        "d2_cash": d2,
        "day_pnl_amt": day_pnl_total,
        "day_pnl_pct": day_pnl_pct,
        "cum_return_pct": total_profit_pct,
        "unrealized": total_unrealized,
        "realized_today": total_day_realized,
    }


def _prepare_daily_report_15m_df(df: Any) -> Any:
    """
    15분 OHLCV DataFrame을 차트용으로 통일: time 열, OHLCV 숫자, 시간순·중복 제거.
    (HTML에 Plotly div id 중복 시 차트가 깨지는 문제는 별도로 div_id로 방지)
    """
    import pandas as pd

    if df is None or getattr(df, "empty", True):
        return None
    w = df.copy()
    cmap: Dict[str, str] = {}
    for c in list(w.columns):
        k = str(c).strip().lower()
        if k in ("time", "datetime", "date"):
            cmap[c] = "time"
        elif k == "open":
            cmap[c] = "Open"
        elif k == "high":
            cmap[c] = "High"
        elif k == "low":
            cmap[c] = "Low"
        elif k == "close":
            cmap[c] = "Close"
        elif k == "volume":
            cmap[c] = "Volume"
    if cmap:
        w.rename(columns=cmap, inplace=True)
    if "time" not in w.columns:
        if isinstance(w.index, pd.DatetimeIndex):
            w = w.reset_index()
            first = str(w.columns[0])
            w.rename(columns={first: "time"}, inplace=True)
        else:
            return None
    need = ("Open", "High", "Low", "Close", "Volume")
    for col in need:
        if col not in w.columns:
            return None
    w["time"] = pd.to_datetime(w["time"], errors="coerce")
    w = w.dropna(subset=["time"])
    if w.empty:
        return None
    for col in need:
        w[col] = pd.to_numeric(w[col], errors="coerce")
    w = w.dropna(subset=["Close"])
    w = w.sort_values("time", kind="mergesort").drop_duplicates(subset=["time"], keep="last")
    w = w.reset_index(drop=True)
    if w.empty:
        return None
    return w


def _order_timestamp_aligned_to_bars(ts_raw: Any, bar_times: Any) -> Optional[Any]:
    """
    주문/체결 시각을 15분봉 time 열과 동일한 스케일(datetime64, naive/tz)로 맞춘다.
    (SVG 세로선·x 보간에 사용)
    """
    import pandas as pd

    t: Optional[pd.Timestamp]
    try:
        loose = _parse_ts_loose(ts_raw)
        if loose is not None:
            t = pd.Timestamp(loose)
        else:
            t = pd.to_datetime(ts_raw, errors="coerce")
    except Exception:
        t = pd.to_datetime(ts_raw, errors="coerce")
    try:
        if t is None or bool(pd.isna(t)):
            return None
    except Exception:
        return None
    try:
        btz = getattr(getattr(bar_times, "dtype", None), "tz", None)
        if btz is not None:
            if t.tzinfo is None:
                try:
                    t = t.tz_localize(btz)
                except Exception:
                    try:
                        t = t.tz_localize("Asia/Seoul", ambiguous="infer")
                    except Exception:
                        pass
            try:
                if t.tzinfo is not None:
                    t = t.tz_convert(btz)
            except Exception:
                pass
        elif t.tzinfo is not None:
            try:
                t = t.tz_convert("Asia/Seoul")
            except Exception:
                pass
            try:
                t = t.replace(tzinfo=None)
            except Exception:
                pass
    except Exception:
        pass
    return t


def _order_time_to_nearest_bar_index(ts_raw: Any, bar_times: Any) -> Optional[int]:
    """
    장세션 간격 제거(연속 봉 인덱스 x축)용: 주문 시각에 가장 가까운 15분봉 행 인덱스(0..n-1).
    """
    import pandas as pd

    t_aligned = _order_timestamp_aligned_to_bars(ts_raw, bar_times)
    if t_aligned is None:
        return None
    series = pd.to_datetime(bar_times, errors="coerce")
    if series.empty or bool(series.isna().all()):
        return None
    try:
        t = pd.Timestamp(t_aligned)
        if bool(pd.isna(t)):
            return None
    except Exception:
        return None
    try:
        diffs = (series - t).abs()
        j = int(diffs.argmin())
    except Exception:
        return None
    if j < 0 or j >= len(series):
        return None
    return j


def _plotly_fig_to_deferred_embed(fig: Any) -> str:
    """
    figure JSON을 HTML에 인라인(`<script type="application/json">`).
    `file://` 로 열 때도 브라우저가 **fetch로 로컬 JSON을 읽지 않아도** 동작한다.
    (별도 `chart_payloads/*.json` + fetch 는 `file:` origin 에서 CORS 로 막힘)
    """
    import uuid

    try:
        import plotly.io as pio

        raw = pio.to_json(fig)
    except Exception as e:
        return f'<p class="warn">차트 JSON 인코딩 오류: {_esc(e)}</p>'
    # HTML 파서가 script 블록을 닫는 것으로 오인하지 않게
    safe = raw.replace("</script>", "<\\/script>")
    div_id = "plotly-" + uuid.uuid4().hex
    return (
        '<div class="chartwrap"><div class="plotly-deferred-wrap" data-plotly-rendered="0">'
        f'<div id="{_esc(div_id)}" class="plotly-graph-div" '
        'style="height:480px;width:100%;"></div>'
        f'<script type="application/json" class="plotly-fig-json" '
        f'data-plotly-target="{_esc(div_id)}">{safe}</script>'
        "</div></div>"
    )


def _build_plotly_15m_section(
    code: str,
    name: str,
    report_day: str,
    df: Any,
    orders_for_code: List[Dict[str, Any]],
    *,
    max_display_rows: Optional[int] = 500,
    compress_session_gaps: bool = False,
) -> str:
    """
    15분 OHLC + 거래량 + MA + 주문 시각 (Plotly).
    주문선은 trace가 아니라 shape(세로선).
    렌더는 **펼치기 시점**에 `Plotly.newPlot` (데이터는 HTML 내 JSON 인라인) — 숨김 패널 즉시 newPlot
    및 plotly.js/py 버전 불일치로 인한 `bdata` 깨짐을 피한다.

    Args:
        report_day: 일일결산은 YYYY-MM-DD 한 날짜, 가상 종합리포트는 "YYYY-MM-DD ~ YYYY-MM-DD" 구간 라벨 가능.
        max_display_rows: 차트에 쓸 최대 봉 수(초과 시 최신 구간만 tail). None이면 잘라내지 않음.
        compress_session_gaps: True면 x축을 실시간이 아니라 **연속 봉 번호**로 두어
            야간·휴장일 사이 빈 구간 없이 장중 봉만 이어서 표시(호버에 실제 일시).
    """
    if not _HAS_PLOTLY:
        return '<p class="warn">Plotly 미설치로 차트를 생략했습니다.</p>'

    try:
        import pandas as pd

        if df is None or df.empty:
            return '<p class="muted">해당 기간 15분봉 데이터가 없습니다.</p>'

        work = _prepare_daily_report_15m_df(df)
        if work is None or work.empty:
            return '<p class="muted">차트용 15분봉 정규화 실패(시간·OHLCV 열 확인).</p>'

        try:
            max_rows = max_display_rows
            if max_rows is not None and int(max_rows) > 0 and len(work) > int(max_rows):
                work = work.tail(int(max_rows)).reset_index(drop=True)
        except Exception:
            pass

        compress = bool(compress_session_gaps)
        n_bars = len(work)
        if compress:
            x_plot = list(range(n_bars))
            try:
                ts_hover = work["time"].dt.strftime("%Y-%m-%d %H:%M").tolist()
            except Exception:
                ts_hover = [str(v) for v in work["time"].tolist()]
        else:
            x_plot = work["time"]
            ts_hover = None

        x = x_plot
        o = work["Open"].astype(float)
        h = work["High"].astype(float)
        low_s = work["Low"].astype(float)
        close_s = work["Close"].astype(float)
        v = work["Volume"].astype(float)

        ma5 = close_s.rolling(5, min_periods=1).mean()
        ma20 = close_s.rolling(20, min_periods=1).mean()

        rt = str(report_day or "").strip()
        title_top = f"{name} ({code}) 15분"
        if rt:
            title_top = f"{title_top} · {rt}"
        if compress:
            title_top = f"{title_top} · 연속 장중"
        fig = make_subplots(
            rows=2,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.06,
            row_heights=[0.68, 0.32],
            subplot_titles=(
                title_top,
                "거래량",
            ),
        )

        # 캔들·거래량: 한국 시장 관례(상승=빨강, 하락=파랑). MA 색(#88C0D0 등)과 구분되게 하락은 #5E81AC.
        _up = "#BF616A"
        _dn = "#5E81AC"
        # Plotly 5/6: 캔들 색은 increasing/decreasing 하위 dict(line, fillcolor) 사용
        _cs_kw: Dict[str, Any] = dict(
            x=x,
            open=o,
            high=h,
            low=low_s,
            close=close_s,
            name="15m",
            increasing=dict(line=dict(color=_up), fillcolor=_up),
            decreasing=dict(line=dict(color=_dn), fillcolor=_dn),
        )
        if compress and ts_hover:
            _cs_kw["text"] = ts_hover
            _cs_kw["hovertemplate"] = (
                "%{text}<br>"
                "시가 %{open:,.0f} 고 %{high:,.0f} 저 %{low:,.0f} 종 %{close:,.0f}"
                "<extra></extra>"
            )
        fig.add_trace(go.Candlestick(**_cs_kw), row=1, col=1)
        fig.add_trace(
            go.Scatter(
                x=x,
                y=ma5,
                name="MA5",
                line=dict(color="#EBCB8B", width=1),
                hoverinfo="skip",
            ),
            row=1,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=x,
                y=ma20,
                name="MA20",
                line=dict(color="#88C0D0", width=1),
                hoverinfo="skip",
            ),
            row=1,
            col=1,
        )

        colors_vol = [
            _up if close_s.iloc[i] >= o.iloc[i] else _dn for i in range(len(work))
        ]
        fig.add_trace(
            go.Bar(
                x=x,
                y=v,
                name="Volume",
                marker_color=colors_vol,
                opacity=0.7,
                orientation="v",
            ),
            row=2,
            col=1,
        )

        # 주문 시각: Scatter 트레이스 대신 layout shape(예전 HTML과 동일) — 트레이스 수·베이스64 지연 없음
        for orec in orders_for_code:
            ts = orec.get("ts", "")
            try:
                if compress:
                    jx = _order_time_to_nearest_bar_index(ts, work["time"])
                    if jx is None:
                        continue
                    tx = float(jx)
                else:
                    tx = _order_timestamp_aligned_to_bars(ts, work["time"])
                    if tx is None:
                        continue
                side = str(orec.get("side", "")).upper()
                is_sell = "매도" in side or "SELL" in side
                col = "#BF616A" if is_sell else "#88C0D0"
                fig.add_shape(
                    type="line",
                    x0=tx,
                    x1=tx,
                    y0=0,
                    y1=1,
                    xref="x",
                    yref="y domain",
                    line=dict(color=col, width=1, dash="dot"),
                    opacity=0.55,
                    row=1,
                    col=1,
                )
                fig.add_shape(
                    type="line",
                    x0=tx,
                    x1=tx,
                    y0=0,
                    y1=1,
                    xref="x2",
                    yref="y2 domain",
                    line=dict(color=col, width=1, dash="dot"),
                    opacity=0.35,
                    row=2,
                    col=1,
                )
            except Exception:
                continue

        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="#2E3440",
            plot_bgcolor="#3B4252",
            height=480,
            margin=dict(l=48, r=24, t=48, b=40),
            hovermode="x unified",
            dragmode=False,
            showlegend=True,
            legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
            xaxis_rangeslider_visible=False,
        )
        if compress and n_bars > 0:
            try:
                max_ticks = min(14, max(2, n_bars))
                if n_bars <= 1:
                    tickvals = [0]
                    ticktext = [
                        work["time"].iloc[0].strftime("%m/%d %H:%M")
                        if hasattr(work["time"].iloc[0], "strftime")
                        else str(work["time"].iloc[0])
                    ]
                else:
                    step = max(1, (n_bars - 1) // (max_ticks - 1))
                    tickvals = list(range(0, n_bars, step))
                    if tickvals[-1] != n_bars - 1:
                        tickvals.append(n_bars - 1)
                    ticktext = []
                    for i in tickvals:
                        tcell = work["time"].iloc[int(i)]
                        try:
                            ticktext.append(
                                tcell.strftime("%m/%d %H:%M")
                                if hasattr(tcell, "strftime")
                                else str(tcell)
                            )
                        except Exception:
                            ticktext.append(str(i))
            except Exception:
                tickvals = [0, max(0, n_bars - 1)]
                ticktext = ["0", str(n_bars - 1)]
            fig.update_xaxes(
                type="linear",
                tickmode="array",
                tickvals=tickvals,
                ticktext=ticktext,
                rangeslider_visible=False,
                showspikes=True,
                spikemode="across",
                spikesnap="cursor",
                spikedash="solid",
                row=1,
                col=1,
            )
            fig.update_xaxes(
                type="linear",
                tickmode="array",
                tickvals=tickvals,
                ticktext=ticktext,
                showspikes=True,
                spikemode="across",
                spikesnap="cursor",
                spikedash="solid",
                row=2,
                col=1,
            )
        else:
            fig.update_xaxes(
                type="date",
                rangeslider_visible=False,
                showspikes=True,
                spikemode="across",
                spikesnap="cursor",
                spikedash="solid",
                row=1,
                col=1,
            )
            fig.update_xaxes(
                type="date",
                showspikes=True,
                spikemode="across",
                spikesnap="cursor",
                spikedash="solid",
                row=2,
                col=1,
            )

        return _plotly_fig_to_deferred_embed(fig)
    except Exception as e:
        return f'<p class="warn">차트 생성 오류: {_esc(e)}</p>'


def _daily_report_atr_exit_line(app: Any, code_str: str, df_15: Any) -> str:
    """
    일일 결산 카드용: 리포트 생성 시점 가상 보유 + 해당일 15분봉(지표 포함)으로
    ATR 손절·트레일 기준가 문자열.
    """
    try:
        from backtest_engine import TradingStrategy

        vp = getattr(app.portfolio, "virtual_positions", None) or {}
        pos = None
        for k, v in vp.items():
            if str(k).split("_")[0].zfill(6) == code_str:
                pos = v
                break
        if pos is None:
            return "리포트 시점 비보유"
        if df_15 is None or getattr(df_15, "empty", True):
            return "당일 15분봉 없음"
        df_i = TradingStrategy.calculate_indicators(df_15.copy(), app.settings)
        last = df_i.iloc[-1]
        try:
            ir = last.to_dict()
        except Exception:
            ir = dict(last) if hasattr(last, "keys") else {}
        np_ = float(ir.get("Close", 0) or 0)
        if np_ <= 0:
            return "종가 없음"
        return TradingStrategy.compute_atr_exit_display_text(
            pos, app.settings, ir, np_, bars_df=df_15
        )
    except Exception:
        return "—"


def generate_daily_settlement_report(
    app: Any,
    report_date: Optional[str] = None,
    output_path: Optional[str] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> str:
    """
    일일 결산 HTML 생성. 저장 경로 절대 경로 반환.

    Parameters
    ----------
    report_date : YYYY-MM-DD, None이면 오늘(로컬).
    log_callback : 진행 로그(선택).
    """
    res = generate_daily_settlement_report_ex(app, report_date, output_path, log_callback)
    return res["path"]


def generate_daily_settlement_report_ex(
    app: Any,
    report_date: Optional[str] = None,
    output_path: Optional[str] = None,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """상세 결과 dict: path, status, warnings, chart_failures 등."""
    warnings: List[str] = []
    chart_failures: List[str] = []
    buffer_minutes = int(app.settings.get("daily_report_session_buffer_minutes", 10))

    def _log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    day = report_date or datetime.now().strftime("%Y-%m-%d")
    base_dir = report_dir_for_date(day)
    try:
        os.makedirs(base_dir, exist_ok=True)
    except OSError as e:
        warnings.append(f"폴더 생성 경고: {e}")

    if output_path is None:
        output_path = report_html_path(day)

    dm = app.data_manager
    start_dt = f"{day} 00:00:00"
    end_dt = f"{day} 23:59:59"
    try:
        orders: List[Tuple] = dm.get_orders_between(start_dt, end_dt)
    except Exception:
        orders = []

    summary = _compute_summary_from_app(app, day)

    # 가상 청산 이력 매칭 시 제외할 종목(예: 지수·더미). 미설정 시 빈 집합.
    try:
        raw_excl = getattr(app, "daily_report_exclude_codes", None)
        if raw_excl is None:
            excl = set()
        elif isinstance(raw_excl, (set, frozenset)):
            excl = set(str(x).strip().zfill(6) for x in raw_excl if str(x).strip())
        elif isinstance(raw_excl, (list, tuple)):
            excl = set(str(x).strip().zfill(6) for x in raw_excl if str(x).strip())
        else:
            excl = set()
    except Exception:
        excl = set()

    by_code: Dict[str, List[Dict[str, Any]]] = {}
    for row in orders:
        try:
            (
                ts,
                code,
                side,
                qty,
                price,
                f_price,
                f_qty,
                result,
                name,
                reason,
                order_no,
                order_type,
                snap_vp,
                snap_ba,
                _fill_snap_vp,  # DB 컬럼 유지, 리포트 표에는 미표시
                _fill_snap_ba,
            ) = row
        except Exception:
            continue
        c = str(code).zfill(6)
        if c not in by_code:
            by_code[c] = []
        by_code[c].append(
            {
                "ts": ts,
                "name": name or "",
                "side": side,
                "qty": qty,
                "price": price,
                "filled_price": f_price,
                "filled_qty": f_qty,
                "result": result,
                "reason": reason or "",
                "order_no": order_no or "",
                "order_type": order_type or "",
                "snap_vol_power": snap_vp,
                "snap_bid_ask": snap_ba or "",
                # 체결 시각 스냅샷(체결강도·호가)은 일일 리포트 표에 포함하지 않음
            }
        )

    # 차트 대상: 주문·체결 기록이 있는 모든 종목
    codes_for_charts = sorted(by_code.keys())

    generated_at = datetime.now()
    overall_status: str = "complete"

    # 종목별 15분봉 HTML 조각 (아래 종목 카드 안에서만 버튼으로 펼침)
    chart_body_by_code: Dict[str, str] = {}
    atr_display_by_code: Dict[str, str] = {}
    for code in codes_for_charts:
        code_str = str(code).strip().zfill(6)
        nm = by_code[code][0].get("name", "") if by_code[code] else ""
        df_15 = None
        try:
            fetch_fn = getattr(dm, "fetch_15m_bars_for_trading_day_rest_only", None)
            if callable(fetch_fn):
                df_15 = fetch_fn(code, day, logger=_log)
            else:
                warnings.append(
                    f"{code_str} DataManager.fetch_15m_bars_for_trading_day_rest_only 없음"
                )
        except Exception as e:
            warnings.append(f"{code_str} REST 15분봉 조회 실패: {e}")
            overall_status = "partial"
            df_15 = None
        if df_15 is None or getattr(df_15, "empty", True):
            chart_failures.append(code_str)
            overall_status = "partial"

        atr_display_by_code[code_str] = _daily_report_atr_exit_line(app, code_str, df_15)
        block = _build_plotly_15m_section(code, nm or code, day, df_15, by_code[code])
        chart_body_by_code[code_str] = block

    if chart_failures:
        warnings.append(
            f"15분 차트(REST 분봉) 없음·실패(스킵): {', '.join(chart_failures)}"
        )

    enriched_by_code = _build_enriched_order_rows(by_code, app.settings, app.portfolio, excl, day)

    # 종목별 카드: 주문 표 + 해당 종목만 펼 수 있는 15분봉 차트
    code_sections = ""
    for code in sorted(by_code.keys()):
        code_str = str(code).strip().zfill(6)
        rows = ""
        for o in enriched_by_code.get(code, []):
            _sba = (o.get("snap_bid_ask") or "").strip() or "-"
            rows += f"""<tr>
                <td>{_esc(o['ts'])}</td>
                <td>{_esc(o['side'])}</td>
                <td class="num">{o['qty']:,}</td>
                <td class="num">{o['price']:,}</td>
                <td class="num">{o['filled_price'] if o['filled_price'] else '-'}</td>
                <td class="num">{o['filled_qty'] if o['filled_qty'] else '-'}</td>
                <td class="num">{_esc(_fmt_krw_cell(o.get('_fee_amt')))}</td>
                <td class="num">{_esc(_fmt_krw_cell(o.get('_buy_amt')))}</td>
                <td class="num">{_esc(_fmt_krw_cell(o.get('_sell_amt')))}</td>
                <td class="num">{_esc(_fmt_pct_net_cell(o.get('_pnl_pct')))}</td>
                <td class="num">{_esc(_fmt_krw_cell(o.get('_disp_cum')))}</td>
                <td class="num">{_esc(_fmt_vp_report_cell(o.get('snap_vol_power')))}</td>
                <td>{_esc(_sba)}</td>
                <td>{_esc(o['result'])}</td>
                <td>{_esc(o['order_no'])}</td>
                <td>{_esc(o['reason'])}</td>
            </tr>"""
        nm = by_code[code][0].get("name", "") if by_code[code] else ""
        chart_inner = chart_body_by_code.get(
            code_str, '<p class="muted">15분봉 차트를 생성하지 못했습니다.</p>'
        )
        disp = nm or code_str
        btn_open_lbl = f"{disp} 15분봉 차트 접기"
        btn_closed_lbl = f"{disp} 15분봉 차트 펼치기"
        atr_note = atr_display_by_code.get(
            code_str, _daily_report_atr_exit_line(app, code_str, None)
        )
        code_sections += f"""
        <div class="card stock-report-card" id="stock-report-{_esc(code_str)}">
            <h3>{_esc(nm)} <span class="muted">({_esc(code_str)})</span></h3>
            <p class="muted" style="margin:4px 0 12px 0;">ATR 청산 기준가 (리포트일 15분 종가·보유 시): {_esc(atr_note)}</p>
            <table>
                <thead>
                    <tr>
                        <th>시각</th><th>구분</th><th>수량</th><th>주문가</th><th>체결가</th><th>체결수량</th>
                        <th>수수료</th><th>매수금액</th><th>매도금액</th>
                        <th>수익률(순)</th><th>누적실현(순)</th>
                        <th>체결강(주문)</th><th>호가(주문)</th>
                        <th>상태</th><th>주문번호</th><th>사유</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
            <div class="stock-chart-toolbar">
                <span class="muted stock-chart-toolbar-label">15분봉 · 주문 시각</span>
                <button type="button" class="chart-toggle-btn"
                    aria-expanded="false"
                    aria-controls="chart-panel-{_esc(code_str)}"
                    data-text-open="{_esc(btn_open_lbl)}"
                    data-text-closed="{_esc(btn_closed_lbl)}">{_esc(btn_closed_lbl)}</button>
            </div>
            <div class="chart-panel-collapsible" id="chart-panel-{_esc(code_str)}" hidden>
                <div class="chart-details-body">{chart_inner}</div>
            </div>
        </div>"""

    summary_rows = f"""
        <tr><td>총 자산 (D+2+주식평가, 포지션 관리 탭과 동일)</td><td class="num">{summary['total_assets']:,.0f} 원</td></tr>
        <tr><td>예수금 (가용)</td><td class="num">{summary['cash']:,.0f} 원</td></tr>
        <tr><td>예수금 D+1</td><td class="num">{summary['d1_cash']:,.0f} 원</td></tr>
        <tr><td>예수금 D+2</td><td class="num">{summary['d2_cash']:,.0f} 원</td></tr>
        <tr><td>주식 평가금 (보유 합)</td><td class="num">{summary['stock_eval']:,.0f} 원</td></tr>
        <tr><td>금일 실현(엔트리일 기준 합산)</td><td class="num">{summary['realized_today']:+,.0f} 원</td></tr>
        <tr><td>미실현 손익 (보유분)</td><td class="num">{summary['unrealized']:+,.0f} 원</td></tr>
        <tr><td>금일 손익 (실현+미실현)</td><td class="num">{summary['day_pnl_amt']:+,.0f} 원 ({summary['day_pnl_pct']:+.2f}%)</td></tr>
        <tr><td>누적 수익률 (참고)</td><td class="num">{summary['cum_return_pct']:+.2f}%</td></tr>
    """

    gen_str = generated_at.strftime("%Y-%m-%d %H:%M:%S")
    complete_flag = generation_is_complete(generated_at, day, buffer_minutes)

    meta_obj: Dict[str, Any] = {
        "report_date": day,
        "generated_at": gen_str,
        "status": overall_status,
        "session_buffer_minutes": buffer_minutes,
        "generation_marked_complete": bool(complete_flag),
        "chart_failed_codes": chart_failures,
        "warnings": warnings,
        "schema_version": 1,
    }
    try:
        import plotly

        meta_obj["plotly_py_version"] = str(getattr(plotly, "__version__", ""))
    except Exception:
        meta_obj["plotly_py_version"] = ""
    try:
        from plotly.io._html import plotly_cdn_url

        meta_obj["plotly_js_cdn"] = plotly_cdn_url()
    except Exception:
        meta_obj["plotly_js_cdn"] = ""
    meta_json = json.dumps(meta_obj, ensure_ascii=False)

    prev_d = previous_trading_day(day)
    next_d = next_trading_day(day)
    nav_html = ""
    if prev_d:
        nav_html += f'<a class="navbtn" href="../{prev_d.strftime("%Y-%m-%d")}/daily_settlement.html">◀ 이전 거래일</a> '
    nav_html += f'<span class="muted">{_esc(day)}</span> '
    if next_d:
        nav_html += f'<a class="navbtn" href="../{next_d.strftime("%Y-%m-%d")}/daily_settlement.html">다음 거래일 ▶</a>'

    warn_banner = ""
    if overall_status == "partial":
        warn_banner = """
        <div class="banner warn">
            일부 종목의 분봉 갱신 또는 차트 생성에 실패했습니다. 주문·체결 표는 유지됩니다. 네트워크·API 확인 후 다시 생성해 주세요.
        </div>"""

    _plotly_cdn_js = json.dumps(meta_obj.get("plotly_js_cdn") or "")
    plotly_head_script = (
        f"<script>window.__DAILY_REPORT_PLOTLY_CDN__ = {_plotly_cdn_js};</script>"
    )

    html_doc = (
        f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <title>일일 결산 — {_esc(day)}</title>
    {plotly_head_script}
    <script type="application/json" id="daily-report-meta">{meta_json}</script>
    <style>
        :root {{
            --bg: #2E3440;
            --fg: #ECEFF4;
            --muted: #88C0D0;
            --bd: #4C566A;
            --plus: #A3BE8C;
            --minus: #BF616A;
        }}
        body {{
            font-family: 'Malgun Gothic', 'Segoe UI', sans-serif;
            background: var(--bg);
            color: var(--fg);
            margin: 0;
            padding: 24px;
            line-height: 1.5;
        }}
        .container {{ max-width: 1100px; margin: 0 auto; }}
        h1 {{ font-weight: 600; color: var(--muted); margin-top: 0; }}
        h2 {{ font-size: 1.1rem; color: var(--muted); border-bottom: 1px solid var(--bd); padding-bottom: 8px; }}
        .muted {{ color: #81A1C1; font-weight: normal; }}
        .navbtn {{ color: #88C0D0; text-decoration: none; margin: 0 8px; }}
        .navbtn:hover {{ text-decoration: underline; }}
        table.summary {{ width: 100%; border-collapse: collapse; margin-bottom: 24px; }}
        table.summary td {{ padding: 8px 12px; border-bottom: 1px solid var(--bd); }}
        table.summary td:first-child {{ color: #D8DEE9; width: 40%; }}
        .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
        .card {{
            background: #3B4252;
            border: 1px solid var(--bd);
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 20px;
        }}
        .banner.warn {{
            background: #4C566A;
            border: 1px solid #BF616A;
            color: #ECEFF4;
            padding: 12px 16px;
            border-radius: 8px;
            margin-bottom: 16px;
        }}
        p.warn {{ color: #BF616A; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
        th, td {{ padding: 6px 8px; border-bottom: 1px solid #434C5E; text-align: left; }}
        th {{ color: var(--muted); }}
        .chartwrap {{ overflow-x: auto; }}
        /* 종목 카드 안: 이 종목 15분봉만 버튼으로 펼침 */
        .stock-chart-toolbar {{
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-top: 16px;
            padding-top: 12px;
            border-top: 1px solid var(--bd);
        }}
        .stock-chart-toolbar-label {{ font-size: 0.9rem; }}
        .chart-toggle-btn {{
            background: #88C0D0;
            color: #2E3440;
            border: none;
            padding: 8px 14px;
            border-radius: 6px;
            font-size: 0.88rem;
            font-weight: 600;
            cursor: pointer;
            flex-shrink: 0;
        }}
        .chart-toggle-btn:hover {{ filter: brightness(1.08); }}
        .chart-toggle-btn:focus-visible {{ outline: 2px solid #ECEFF4; outline-offset: 2px; }}
        .chart-panel-collapsible {{ margin-top: 12px; }}
        .chart-details-body {{
            padding-top: 4px;
            border-top: 1px solid var(--bd);
        }}
        footer {{ margin-top: 32px; font-size: 12px; color: #81A1C1; text-align: center; }}
    </style>
</head>
<body>
<div class="container">
    <nav style="margin-bottom:16px;">{nav_html}</nav>
    <h1>일일 결산 리포트 <span class="muted">{_esc(day)}</span></h1>
    <p class="muted">생성 시각: {_esc(gen_str)} — 상태: {_esc(overall_status)} — 완료판정(장마감+버퍼): {'예' if complete_flag else '아니오(장중·재생성 권장)'}</p>
    <p class="muted" style="font-size:12px;margin-top:-6px;">차트 데이터는 HTML에 포함되어 <code>file://</code> 로 열어도 동작합니다. 펼칠 때만 Plotly로 그립니다(초기 로드 시 plotly.js CDN은 첫 펼침 때만 로드).</p>
    {warn_banner}

    <h2>요약</h2>
    <table class="summary">{summary_rows}</table>

    <h2>종목별 리포트 (주문 · 체결 · 15분봉 차트)</h2>
    <p class="muted" style="margin-top:-8px;margin-bottom:16px;">각 종목 카드 하단의 버튼으로 <b>해당 종목</b> 15분봉 차트만 펼칠 수 있습니다.</p>
    {code_sections if code_sections else '<p class="muted">해당 일자 주문 기록이 없습니다.</p>'}

    <footer>K-Trader 실시간 감시 일일 결산</footer>
</div>
<script>
"""
        + PLOTLY_DEFERRED_CHART_IIFE
        + """
</script>
</body>
</html>"""
    )

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_doc)
    except OSError as e:
        raise RuntimeError(f"일일 결산 리포트 저장 실패: {e}") from e

    return {
        "path": os.path.abspath(output_path),
        "status": overall_status,
        "warnings": warnings,
        "chart_failures": chart_failures,
        "report_date": day,
    }


def run_daily_settlement_batch(
    app: Any,
    start_date: str,
    log_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """
    start_date ~ 오늘 사이 KRX 거래일 중, 재생성이 필요한 일자만 일일결산 HTML 생성.
    최대 31일 범위. 완료 후 '오늘' 리포트 경로를 opened_path 로 반환.
    """
    from reporting.krx_trading_calendar import clamp_batch_range

    def _log(msg: str) -> None:
        if log_callback:
            try:
                log_callback(msg)
            except Exception:
                pass

    today = datetime.now().strftime("%Y-%m-%d")
    s, e, err = clamp_batch_range(start_date, today, max_days=31)
    if err:
        return {"ok": False, "error": err, "opened_path": None, "generated": [], "skipped": []}

    days = trading_days_inclusive(s, e)
    buffer_minutes = int(app.settings.get("daily_report_session_buffer_minutes", 10))

    generated: List[str] = []
    skipped: List[str] = []
    for d in days:
        ds = d.strftime("%Y-%m-%d")
        p = report_html_path(ds)
        if not report_needs_regeneration(p, ds, buffer_minutes):
            skipped.append(ds)
            _log(f"[일일결산] 스킵(완료): {ds}")
            continue
        try:
            generate_daily_settlement_report_ex(app, ds, None, _log)
            generated.append(ds)
            _log(f"[일일결산] 생성: {ds}")
        except Exception as e:
            _log(f"[일일결산] 오류 {ds}: {e}")

    opened_path: Optional[str] = None
    tpath = report_html_path(today)
    if os.path.isfile(tpath):
        opened_path = os.path.abspath(tpath)
    else:
        for d0 in reversed(days):
            p = report_html_path(d0.strftime("%Y-%m-%d"))
            if os.path.isfile(p):
                opened_path = os.path.abspath(p)
                break

    return {
        "ok": True,
        "error": None,
        "opened_path": opened_path,
        "generated": generated,
        "skipped": skipped,
    }
