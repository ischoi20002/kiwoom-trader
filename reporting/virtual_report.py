import os
import json
import importlib
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from reporting.daily_settlement_report import (
    enrich_order_rows_for_virtual_trade,
    _build_plotly_15m_section,
    _esc,
    _fmt_krw_cell,
    _fmt_pct_net_cell,
    _fmt_vp_report_cell,
)
from reporting.krx_trading_calendar import trading_days_inclusive
from reporting.plotly_deferred_chart_js import PLOTLY_DEFERRED_CHART_IIFE
from utils.fee_rates import fee_decimal_rates_live_trading, unrealized_at_price_net_of_sell_fee


def _resolve_virtual_analysis_helpers() -> Tuple[
    Optional[Callable[..., Any]],
    Optional[Callable[..., Any]],
    Optional[str],
]:
    """
    trade_report는 모듈 로드 순서·구버전 파일 등으로 최상단 import가 실패할 수 있어
    리포트 생성 시점에 importlib로 다시 로드합니다.
    """
    try:
        tr = importlib.import_module("reporting.trade_report")
        f_df = getattr(tr, "virtual_executions_to_trades_df", None)
        f_html = getattr(tr, "build_virtual_portfolio_analysis_html", None)
        if not callable(f_df) or not callable(f_html):
            return (
                None,
                None,
                "reporting/trade_report.py에 virtual_executions_to_trades_df 또는 "
                "build_virtual_portfolio_analysis_html 정의가 없습니다. 프로젝트를 최신으로 맞춰 주세요.",
            )
        return f_df, f_html, None
    except Exception as e:
        try:
            msg = f"{type(e).__name__}: {e}"
        except Exception:
            msg = "알 수 없는 오류"
        return None, None, msg


def _pos_date_hint_for_event(pos: Dict[str, Any]) -> str:
    """이벤트 time이 시각만(HH:MM:SS)일 때 앞에 붙일 날짜(YYYY-MM-DD)."""
    try:
        exd = str(pos.get("exit_date") or "").strip()
        if len(exd) >= 10 and exd[4] == "-":
            return exd[:10]
        exdt = str(pos.get("exit_datetime") or "").strip()
        if len(exdt) >= 10 and exdt[4] == "-":
            return exdt[:10]
        et = pos.get("entry_time")
        if isinstance(et, str):
            if "T" in et:
                return et[:10]
            if len(et) >= 10 and et[4] == "-":
                return et[:10]
    except Exception:
        pass
    try:
        return datetime.now().strftime("%Y-%m-%d")
    except Exception:
        return ""


def _event_display_time(ev: Dict[str, Any], pos: Dict[str, Any]) -> str:
    """리포트용: 스냅샷 시각 우선, 없으면 time에 날짜 보강."""
    try:
        hp = ev.get("_report_hint_pos")
        if isinstance(hp, dict):
            pos = hp
        sn = ev.get("snapshot")
        if isinstance(sn, dict):
            st = sn.get("time")
            if isinstance(st, str) and (rs := st.strip()):
                rs = rs.replace("T", " ")
                if len(rs) >= 16 and rs[4] == "-":
                    return rs[:19]
                _segs = rs.split(":")
                if len(_segs) == 3 and len(rs) <= 8:
                    d0 = _pos_date_hint_for_event(pos)
                    return f"{d0} {rs}" if d0 else rs
                return rs[:19] if len(rs) > 8 else rs
        raw = str(ev.get("time") or "").strip()
        if not raw or raw == "-":
            return "-"
        if len(raw) >= 16 and raw[4] == "-" and (" " in raw or "T" in raw):
            return raw.replace("T", " ")[:19]
        segs = raw.split(":")
        if len(segs) == 3 and len(raw) <= 8:
            d = _pos_date_hint_for_event(pos)
            return f"{d} {raw}" if d else raw
        return raw
    except Exception:
        return str(ev.get("time", "-"))


def _position_events_contain_buy(evs: List[Any]) -> bool:
    for ev in evs:
        if isinstance(ev, dict) and "BUY" in str(ev.get("action", "")).upper():
            return True
    return False


def _entry_timestamp_str(pos: Dict[str, Any]) -> str:
    et = pos.get("entry_time")
    if not isinstance(et, str) or not et.strip():
        return ""
    s = et.strip().replace("T", " ")
    if len(s) >= 19 and s[4] == "-":
        return s[:19]
    return s


def _synthetic_buy_s1_event(pos: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """과거 데이터 등으로 events에 매수가 없을 때 진입 요약 행을 한 줄 보강합니다."""
    raw_evs = pos.get("events") or []
    if _position_events_contain_buy(raw_evs):
        return None
    ts = _entry_timestamp_str(pos)
    if not ts:
        return None
    try:
        ap = float(pos.get("avg_price") or 0.0)
    except (TypeError, ValueError):
        ap = 0.0
    try:
        q = int(pos.get("qty") or 0)
    except (TypeError, ValueError):
        q = 0
    if q <= 0:
        for ev in reversed(raw_evs):
            if not isinstance(ev, dict):
                continue
            if "SELL" in str(ev.get("action", "")).upper():
                try:
                    q = int(ev.get("qty") or 0)
                except (TypeError, ValueError):
                    q = 0
                break
    hint = str(pos.get("reason") or pos.get("sig_type") or "").strip()
    reason_txt = (
        "리포트 보강: 저장 데이터에 매수 체결 이벤트가 없어 "
        "진입 시각(entry_time)·평단·수량으로 요약합니다."
    )
    if hint:
        reason_txt += f" (신호/유형: {hint})"
    return {
        "time": ts,
        "action": "BUY_S1",
        "price": ap,
        "qty": q,
        "snapshot": {
            "time": ts,
            "market_trend": "N/A",
            "vol_power": 100.0,
            "bid_ask_ratio": "-",
            "ma_alignment": "N/A",
            "adx": 0.0,
            "rsi": 0.0,
            "reason": reason_txt,
        },
    }


def _open_mtm_pct_and_krw(
    pos: Dict[str, Any], settings: Optional[Dict[str, Any]] = None
) -> Tuple[Optional[float], float]:
    """
    보유 건의 현재가 기준 수익률(%) 및 미실현 손익(원).
    포지션 탭과 동일: 매도 수수료 차감 순매각 − invested(매수비용).
    current_price 가 유효하지 않으면 (None, 0.0).
    """
    try:
        ap = float(pos.get("avg_price") or 0)
        q = int(pos.get("qty") or 0)
    except (TypeError, ValueError):
        return None, 0.0
    if ap <= 0 or q <= 0:
        return None, 0.0
    try:
        cpf = float(pos.get("current_price"))
    except (TypeError, ValueError):
        return None, 0.0
    if cpf <= 0:
        return None, 0.0
    s = settings if isinstance(settings, dict) else {}
    try:
        krw = float(unrealized_at_price_net_of_sell_fee(pos, float(cpf), int(q), float(ap), s))
    except Exception:
        krw = 0.0
    try:
        inv_pf = float(pos.get("invested", 0) or 0)
    except (TypeError, ValueError):
        inv_pf = 0.0
    if inv_pf <= 1e-9 and ap > 0 and q > 0:
        inv_pf = float(ap) * float(q)
    if inv_pf > 1e-9:
        pct = (krw / inv_pf) * 100.0
    elif ap > 0 and cpf > 0:
        pct = (cpf / ap - 1.0) * 100.0
    else:
        return None, 0.0
    return float(pct), float(krw)


def _open_net_gross_sell_fee(
    pos: Dict[str, Any], sell_fee_decimal: float
) -> Tuple[float, float, float]:
    """
    현재가 기준 '매도 시' 추정: 액면 평가액(cp×qty), 매도 수수료, 순입금(액면−수수료).
    settings 기반 sell_fee_decimal 은 fee_decimal_rates_live_trading 과 동일.
    """
    try:
        q = int(pos.get("qty") or 0)
        cpf = float(pos.get("current_price") or 0)
    except (TypeError, ValueError):
        return 0.0, 0.0, 0.0
    if q <= 0 or cpf <= 0:
        return 0.0, 0.0, 0.0
    try:
        gross = float(cpf) * float(q)
        fee = gross * float(max(0.0, sell_fee_decimal))
        net = gross - fee
        return net, gross, fee
    except Exception:
        return 0.0, 0.0, 0.0


def _history_bucket_key(h: Dict[str, Any]) -> str:
    """동일 종목(6자리)을 한 카드로 묶기 위한 키. 코드 없으면 행 단위로 분리."""
    try:
        c = str(h.get("code") or "").strip().zfill(6)
        if not c or c == "000000":
            return f"__nocode_{id(h)}"
        return c
    except Exception:
        return f"__nocode_{id(h)}"


def _parse_entry_dt_sort(h: Dict[str, Any]) -> datetime:
    et = h.get("entry_time")
    if isinstance(et, str) and len(et) >= 10:
        s = et.replace("T", " ")[:19]
        try:
            return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return datetime.min


def _parse_exit_dt_sort(h: Dict[str, Any]) -> datetime:
    ex = h.get("exit_datetime")
    if isinstance(ex, str) and len(ex) >= 10:
        try:
            return datetime.strptime(ex.replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    ed, etx = h.get("exit_date"), h.get("exit_time")
    if ed and etx:
        try:
            return datetime.strptime(f"{str(ed)[:10]} {str(etx)[:8]}", "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return datetime.min


def _exit_display_str(h: Dict[str, Any]) -> str:
    ex = str(h.get("exit_datetime") or "").strip()
    if ex:
        return ex.replace("T", " ")[:19]
    if h.get("exit_date") and h.get("exit_time"):
        return f'{h.get("exit_date")} {h.get("exit_time")}'
    return ""


def _round_invested(h: Dict[str, Any]) -> float:
    try:
        v = float(h.get("invested") or 0)
        if v > 0:
            return v
    except (TypeError, ValueError):
        pass
    try:
        return float(h.get("avg_price") or 0) * int(h.get("qty") or 0)
    except (TypeError, ValueError):
        return 0.0


def _merge_history_group_one_card(code: str, grp: List[Dict[str, Any]]) -> Dict[str, Any]:
    """동일 종목코드의 청산 이력 여러 건을 한 카드·한 타임라인으로 합칩니다."""
    rounds = sorted([h for h in grp if isinstance(h, dict)], key=_parse_entry_dt_sort)
    flat: List[Dict[str, Any]] = []
    for h in rounds:
        for ev in _normalize_position_events_for_report(h):
            e2 = dict(ev)
            e2["_report_hint_pos"] = h
            flat.append(e2)
    try:
        flat.sort(key=lambda e: _parse_event_sort_dt(e, {}))
    except Exception:
        pass
    total_realized = 0
    for h in rounds:
        try:
            total_realized += int(h.get("realized_pnl_amt") or 0)
        except (TypeError, ValueError):
            pass
    total_invested = sum(_round_invested(h) for h in rounds)
    blended_pct = (total_realized / total_invested * 100.0) if total_invested > 1e-9 else 0.0
    last_h = max(rounds, key=_parse_exit_dt_sort)
    first_h = rounds[0]
    name = str(last_h.get("name") or first_h.get("name") or "Unknown")
    exit_disp = _exit_display_str(last_h)
    return {
        "code": code,
        "name": name,
        "entry_time": first_h.get("entry_time"),
        "exit_datetime": exit_disp or last_h.get("exit_datetime"),
        "exit_date": last_h.get("exit_date"),
        "exit_time": last_h.get("exit_time"),
        "final_pnl_pct": blended_pct,
        "realized_pnl_amt": total_realized,
        "_merged_rounds": len(rounds),
        "_report_flat_events": flat,
        "avg_price": float(last_h.get("avg_price") or 0),
        "qty": int(last_h.get("qty") or 0),
    }


def _merge_and_order_closed_history(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    종목코드별로 청산 이력을 병합한 뒤, 원본 리스트 역순으로 처음 등장하는 종목 순서를 유지합니다.
    (기존 reversed(closed_cards)와 동일하게 '리스트 뒤쪽에 가까운 종목'이 먼저 나오게 함)
    """
    if not history:
        return []
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    for h in history:
        if not isinstance(h, dict):
            continue
        key = _history_bucket_key(h)
        buckets.setdefault(key, []).append(h)
    merged_map: Dict[str, Dict[str, Any]] = {}
    for key, grp in buckets.items():
        if len(grp) == 1:
            merged_map[key] = grp[0]
        else:
            c6 = str(grp[0].get("code") or "").strip().zfill(6)
            if not c6 or c6 == "000000":
                c6 = key
            merged_map[key] = _merge_history_group_one_card(c6, grp)
    seen: set = set()
    ordered: List[Dict[str, Any]] = []
    for h in reversed(history):
        if not isinstance(h, dict):
            continue
        key = _history_bucket_key(h)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(merged_map[key])
    return ordered


def _parse_event_sort_dt(ev: Dict[str, Any], pos: Dict[str, Any]) -> datetime:
    disp = _event_display_time(ev, pos)
    try:
        return datetime.strptime(disp[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime(1970, 1, 1, 0, 0, 0)


def _normalize_position_events_for_report(pos: Dict[str, Any]) -> List[Dict[str, Any]]:
    """매수 누락 보강 후 시간순 정렬 — 종합 리포트에서 진입·청산 흐름을 한눈에 보이게 합니다."""
    pre = pos.get("_report_flat_events")
    if isinstance(pre, list) and len(pre) > 0:
        return list(pre)
    raw = [e for e in (pos.get("events") or []) if isinstance(e, dict)]
    syn = _synthetic_buy_s1_event(pos)
    merged: List[Dict[str, Any]] = ([syn] if syn else []) + list(raw)
    try:
        merged.sort(key=lambda e: _parse_event_sort_dt(e, pos))
    except Exception:
        pass
    return merged


def _narrative_reason_for_event(
    ev: Dict[str, Any], sn: Dict[str, Any], pos: Dict[str, Any]
) -> str:
    """
    리포트 스냅샷 '사유' 표시: 매도는 exit_reason(익절/손절 등) 우선.
    과거 데이터에 REST_SELL만 있으면 포지션의 exit_reason으로 보강합니다.
    """
    try:
        base = str((sn or {}).get("reason", "") or "").strip()
    except Exception:
        base = ""
    try:
        actu = str(ev.get("action", "") or "").upper()
    except Exception:
        actu = ""
    if "SELL" not in actu and "SYNC_EXIT" not in actu:
        return base or "N/A"
    hint = ev.get("_report_hint_pos")
    src: Dict[str, Any]
    src = hint if isinstance(hint, dict) else pos
    try:
        er = str((src or {}).get("exit_reason", "") or "").strip()
    except Exception:
        er = ""
    if not er:
        return base or "N/A"
    bup = base.upper()
    if not base or base == "N/A" or bup.startswith("REST_") or bup.startswith("EXEC_"):
        return er
    if er.upper() in bup or bup in er.upper():
        return base
    return f"{er} ({base})"


def _format_vol_power_snip(sn: Dict[str, Any]) -> str:
    try:
        vp_raw = sn.get("vol_power", 100)
        vp_f = float(vp_raw)
        return f"{vp_f:.1f}%"
    except (TypeError, ValueError):
        return str(sn.get("vol_power", "-"))


def _build_events_html(pos: Dict[str, Any]) -> str:
    events_html = ""
    for ev in _normalize_position_events_for_report(pos):
        ev_time = _event_display_time(ev, pos)
        action = ev.get("action", "N/A")
        price = ev.get("price", 0)
        qty = ev.get("qty", 0)
        sn = ev.get("snapshot", {}) if isinstance(ev.get("snapshot"), dict) else {}

        sn_html = ""
        if sn:
            vp_show = _format_vol_power_snip(sn)
            sn_html = f"""
                <div class="snapshot-card">
                    <div class="sn-item"><span>RSI:</span> <b>{sn.get('rsi', '-')}</b></div>
                    <div class="sn-item"><span>체결강도:</span> <b>{vp_show}</b></div>
                    <div class="sn-item"><span>매도벽(B/A):</span> <b>{sn.get('bid_ask_ratio', '-')}</b></div>
                    <div class="sn-item"><span>이평:</span> <b>{sn.get('ma_alignment', '-')}</b></div>
                    <div class="sn-item"><span>ADX:</span> <b>{sn.get('adx', '-')}</b></div>
                    <div class="sn-reason">사유: {_narrative_reason_for_event(ev, sn, pos)}</div>
                </div>
                """
        else:
            sn_html = """
                <div class="snapshot-card snapshot-missing">
                    이 체결 시점에는 시장 스냅샷이 비어 있습니다. (체결 가격·수량·시각 위주로 참고하세요.)
                </div>
                """

        action_cls = "act-buy" if "BUY" in str(action) else "act-sell"
        try:
            p_show = float(price)
        except (TypeError, ValueError):
            p_show = 0.0
        try:
            q_show = int(qty)
        except (TypeError, ValueError):
            q_show = 0
        events_html += f"""
            <div class="event-row">
                <div class="event-meta">
                    <span class="ev-time">{ev_time}</span>
                    <span class="ev-action {action_cls}">{action}</span>
                    <span class="ev-price">{p_show:,.0f}원 ({q_show}주)</span>
                </div>
                {sn_html}
            </div>
            """
    return events_html


def _order_tuple_to_dict(row: Tuple[Any, ...]) -> Optional[Dict[str, Any]]:
    """get_orders_between 한 행을 일일 결산과 동일한 dict로 변환합니다."""
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
            _fill_snap_vp,
            _fill_snap_ba,
        ) = row
    except (TypeError, ValueError):
        return None
    return {
        "ts": ts,
        "code": str(code).zfill(6),
        "side": side,
        "qty": qty,
        "price": price,
        "filled_price": f_price,
        "filled_qty": f_qty,
        "result": result,
        "name": name or "",
        "reason": reason or "",
        "order_no": order_no or "",
        "order_type": order_type or "",
        "snap_vol_power": snap_vp,
        "snap_bid_ask": snap_ba or "",
    }


def _order_query_window_bounds(pos: Dict[str, Any], is_open: bool) -> Tuple[str, str]:
    """order_history 조회 구간 [시작, 끝] (문자열, get_orders_between 용)."""
    try:
        et = pos.get("entry_time")
        if isinstance(et, str) and len(et) >= 10:
            d0 = et.replace("T", " ")[:10]
            start = f"{d0} 00:00:00"
        else:
            start = datetime.now().strftime("%Y-%m-%d 00:00:00")
    except Exception:
        start = datetime.now().strftime("%Y-%m-%d 00:00:00")
    try:
        if is_open:
            end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        else:
            ex = str(pos.get("exit_datetime") or "").strip().replace("T", " ")
            if len(ex) >= 19:
                edt = datetime.strptime(ex[:19], "%Y-%m-%d %H:%M:%S")
                end = (edt + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            elif pos.get("exit_date"):
                ed = str(pos.get("exit_date"))[:10]
                etx = str(pos.get("exit_time") or "23:59:59")
                if len(etx) >= 8 and ":" in etx:
                    end = f"{ed} {etx[:8]}"
                else:
                    end = f"{ed} 23:59:59"
            else:
                end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return start, end


def _exit_snap_from_closed_position(pos: Dict[str, Any]) -> Dict[str, Any]:
    """청산 카드 1건 → enrich 매칭용 exit 스냅."""
    ex = str(pos.get("exit_datetime") or "").strip()
    if not ex and pos.get("exit_date") and pos.get("exit_time"):
        ex = f'{pos.get("exit_date")} {pos.get("exit_time")}'
    ex = ex.replace("T", " ")
    if len(ex) >= 19:
        ex = ex[:19]
    try:
        rp = float(pos.get("realized_pnl_amt") or 0)
    except (TypeError, ValueError):
        rp = 0.0
    try:
        pp = float(pos.get("final_pnl_pct") or 0)
    except (TypeError, ValueError):
        pp = 0.0
    return {
        "exit_datetime": ex,
        "realized_pnl_amt": rp,
        "final_pnl_pct": pp,
        "exit_date": pos.get("exit_date"),
        "exit_time": pos.get("exit_time"),
    }


def _build_orders_table_html_from_rows(
    rows: List[Dict[str, Any]], *, is_open: bool
) -> str:
    """일일 결산과 동일 열 구성의 주문·체결 표 HTML."""
    if not rows:
        return ""
    tr_parts: List[str] = []
    for o in rows:
        try:
            qv = int(o.get("qty") or 0)
        except (TypeError, ValueError):
            qv = 0
        try:
            pv = float(o.get("price") or 0)
        except (TypeError, ValueError):
            pv = 0.0
        fp = o.get("filled_price")
        fq = o.get("filled_qty")
        _sba = (o.get("snap_bid_ask") or "").strip() or "-"
        tr_parts.append(
            f"""<tr>
                <td>{_esc(o.get("ts"))}</td>
                <td>{_esc(o.get("side"))}</td>
                <td class="num">{qv:,}</td>
                <td class="num">{pv:,.0f}</td>
                <td class="num">{_esc(fp if fp else "-")}</td>
                <td class="num">{_esc(fq if fq else "-")}</td>
                <td class="num">{_esc(_fmt_krw_cell(o.get("_fee_amt")))}</td>
                <td class="num">{_esc(_fmt_krw_cell(o.get("_buy_amt")))}</td>
                <td class="num">{_esc(_fmt_krw_cell(o.get("_sell_amt")))}</td>
                <td class="num">{_esc(_fmt_pct_net_cell(o.get("_pnl_pct")))}</td>
                <td class="num">{_esc(_fmt_krw_cell(o.get("_disp_cum")))}</td>
                <td class="num">{_esc(_fmt_vp_report_cell(o.get("snap_vol_power")))}</td>
                <td>{_esc(_sba)}</td>
                <td>{_esc(o.get("result"))}</td>
                <td>{_esc(o.get("order_no"))}</td>
                <td>{_esc(o.get("reason"))}</td>
            </tr>"""
        )
    tbody = "".join(tr_parts)
    hint = (
        "진입일 00:00 ~ 현재 시각"
        if is_open
        else "진입일 00:00 ~ 청산 시각+1h (해당 라운드 주문 수집)"
    )
    return f"""
            <div class="vr-order-block">
                <div class="vr-order-hint">주문·체결 원장 (<code>trading_state.db</code> order_history) · {hint}</div>
                <div class="vr-order-wrap">
                    <table class="vr-order-table">
                        <thead>
                            <tr>
                                <th>시각</th><th>구분</th><th>수량</th><th>주문가</th><th>체결가</th><th>체결수량</th>
                                <th>수수료</th><th>매수금액</th><th>매도금액</th>
                                <th>수익률(순)</th><th>누적실현(순)</th>
                                <th>체결강(주문)</th><th>호가(주문)</th>
                                <th>상태</th><th>주문번호</th><th>사유</th>
                            </tr>
                        </thead>
                        <tbody>{tbody}</tbody>
                    </table>
                </div>
            </div>
            """


def _fetch_orders_table_html_for_position(
    data_manager: Any,
    settings: Optional[dict],
    pos: Dict[str, Any],
    is_open: bool,
) -> str:
    """DataManager로 주문을 조회해 일일 결산 수준 표 HTML을 반환합니다."""
    if data_manager is None or not isinstance(settings, dict):
        return ""
    try:
        start_s, end_s = _order_query_window_bounds(pos, is_open)
        if start_s > end_s:
            start_s, end_s = end_s, start_s
        raw_rows = data_manager.get_orders_between(start_s, end_s)
    except Exception:
        return (
            '<div class="vr-order-block"><p class="vr-order-hint" style="color:#bf616a;">'
            "주문 이력 조회에 실패했습니다.</p></div>"
        )
    try:
        code = str(pos.get("code", "")).zfill(6)
    except Exception:
        code = "000000"
    ods: List[Dict[str, Any]] = []
    for row in raw_rows or []:
        d = _order_tuple_to_dict(row)
        if d and str(d.get("code", "")).zfill(6) == code:
            ods.append(d)
    if not ods:
        return (
            '<div class="vr-order-block"><p class="vr-order-hint">해당 기간 '
            "<code>order_history</code> 에 기록이 없습니다. "
            "(미주문·체결 전·모의 미기록·다른 PC DB 등)</p></div>"
        )
    try:
        exit_snap = None if is_open else _exit_snap_from_closed_position(pos)
        enriched = enrich_order_rows_for_virtual_trade(ods, settings, exit_snap)
    except Exception:
        enriched = ods
    return _build_orders_table_html_from_rows(enriched, is_open=is_open)


def _entry_day_yyyy_mm_dd(pos: Dict[str, Any]) -> str:
    """포지션 진입일(YYYY-MM-DD). 파싱 실패 시 오늘."""
    try:
        et = pos.get("entry_time")
        if isinstance(et, str) and len(et) >= 10:
            head = et.replace("T", " ")[:10]
            if len(head) == 10 and head[4] == "-" and head[7] == "-":
                return head
    except Exception:
        pass
    return datetime.now().strftime("%Y-%m-%d")


def _chart_period_end_day_str(pos: Dict[str, Any], is_open: bool) -> str:
    """15분 차트 구간 끝 캘린더일: 미청산=오늘, 청산=청산일(또는 exit_datetime 날짜)."""
    try:
        if is_open:
            return datetime.now().strftime("%Y-%m-%d")
        exd = str(pos.get("exit_date") or "").strip()
        if len(exd) >= 10 and exd[4] == "-":
            return exd[:10]
        exdt = str(pos.get("exit_datetime") or "").strip().replace("T", " ")
        if len(exdt) >= 10 and exdt[4] == "-":
            return exdt[:10]
    except Exception:
        pass
    return datetime.now().strftime("%Y-%m-%d")


def _concat_15m_bars_for_trading_days(
    data_manager: Any,
    code_str: str,
    trading_days: List[date],
    *,
    rest_day_cache: Optional[Dict[Tuple[str, str], Any]] = None,
) -> Any:
    """
    거래일별 15분봉을 순회해 이어붙인 DataFrame (없으면 None).
    데이터는 ``fetch_15m_bars_for_trading_day`` 시 DB 우선이며,
    **REST 폴백이 실제로 쓰인 뒤에만** 일자 간 짧은 대기(API 부하).

    Parameters
    ----------
    rest_day_cache
        같은 `generate_virtual_narrative_report` 호출 안에서 공유하는 딕셔너리.
        키 ``(종목코드 6자리, 'YYYY-MM-DD')`` 로 일자 단위 결과를 재사용해 중복 REST를 줄입니다.
    """
    import time

    import pandas as pd

    fn = getattr(data_manager, "fetch_15m_bars_for_trading_day", None)
    if not callable(fn):
        fn = getattr(data_manager, "fetch_15m_bars_for_trading_day_rest_only", None)
    if not callable(fn):
        return None
    if not trading_days:
        return None
    try:
        n_code = str(code_str).strip().zfill(6)
    except Exception:
        n_code = "000000"
    parts: List[Any] = []
    n = len(trading_days)
    for i, d in enumerate(trading_days):
        day_s = d.strftime("%Y-%m-%d")
        key = (n_code, day_s)
        chunk: Any = None
        used_rest = False
        if rest_day_cache is not None and key in rest_day_cache:
            chunk = rest_day_cache[key]
        else:
            try:
                chunk = fn(code_str, day_s, logger=None)
                used_rest = (
                    getattr(data_manager, "_last_report_15m_source", "") == "rest"
                )
                if rest_day_cache is not None:
                    try:
                        rest_day_cache[key] = chunk
                    except Exception:
                        pass
            except Exception:
                chunk = None
        if chunk is not None and not getattr(chunk, "empty", True):
            parts.append(chunk)
        if i + 1 < n and used_rest:
            try:
                time.sleep(0.12)
            except Exception:
                pass
    if not parts:
        return None
    try:
        return pd.concat(parts, ignore_index=True, sort=False)
    except Exception:
        return None


def _build_virtual_chart_section_html(
    pos: Dict[str, Any],
    is_open: bool,
    data_manager: Any,
    settings: Optional[dict],
    section_id: str,
    display_name: str,
    code_str: str,
    *,
    rest_day_cache: Optional[Dict[Tuple[str, str], Any]] = None,
) -> str:
    """
    일일 결산과 동일한 15분 Plotly(캔들+거래량+MA+주문 세로선).
    데이터 구간은 **진입일~청산일(또는 오늘)** 의 KRX 거래일마다 15분봉을 이어붙인 것.
    `file://` 호환 인라인 JSON + 펼침 시 렌더(공통 `plotly_deferred_chart_js`).
    """
    entry_day = _entry_day_yyyy_mm_dd(pos)
    end_day = _chart_period_end_day_str(pos, is_open)
    try:
        d0 = datetime.strptime(entry_day[:10], "%Y-%m-%d").date()
        d1 = datetime.strptime(end_day[:10], "%Y-%m-%d").date()
        if d0 > d1:
            d0, d1 = d1, d0
            entry_day, end_day = end_day, entry_day
    except Exception:
        d0 = datetime.now().date()
        d1 = d0
        entry_day = end_day = d0.strftime("%Y-%m-%d")

    try:
        tdays = trading_days_inclusive(entry_day, end_day)
    except Exception:
        tdays = []
    if not tdays:
        try:
            tdays = [datetime.strptime(end_day[:10], "%Y-%m-%d").date()]
        except Exception:
            tdays = []

    df_15 = None
    if data_manager is not None:
        try:
            df_15 = _concat_15m_bars_for_trading_days(
                data_manager,
                code_str,
                tdays,
                rest_day_cache=rest_day_cache,
            )
        except Exception:
            df_15 = None

    orders_list: List[Dict[str, Any]] = []
    if data_manager is not None:
        try:
            start_full, end_full = _order_query_window_bounds(pos, is_open)
            for row in data_manager.get_orders_between(start_full, end_full) or []:
                d = _order_tuple_to_dict(row)
                if d and str(d.get("code", "")).zfill(6) == code_str:
                    orders_list.append(d)
        except Exception:
            pass

    chart_label = entry_day if entry_day == end_day else f"{entry_day} ~ {end_day}"
    try:
        inner = _build_plotly_15m_section(
            code_str,
            display_name or code_str,
            chart_label,
            df_15,
            orders_list,
            max_display_rows=None,
            compress_session_gaps=True,
        )
    except Exception as e:
        inner = f'<p class="warn" style="color:#f7768e;">차트 생성 오류: {_esc(str(e))}</p>'
    panel_id = f"vr-chart-panel-{section_id}"
    btn_open = f"{display_name} 15분봉 차트 접기"
    btn_closed = f"{display_name} 15분봉 차트 펼치기"
    toolbar_hint = (
        f"15분봉 · 주문 시각 (진입~{'현재' if is_open else '청산'} · "
        f"{_esc(entry_day)} ~ {_esc(end_day)}, 거래일 {len(tdays)}일분 · "
        f"동일 리포트 내 종목·일자 데이터 캐시(DB·REST))"
    )
    return f"""
        <div class="vr-stock-chart-toolbar">
            <span class="muted vr-stock-chart-toolbar-label">{toolbar_hint}</span>
            <button type="button" class="chart-toggle-btn"
                aria-expanded="false"
                aria-controls="{_esc(panel_id)}"
                data-text-open="{_esc(btn_open)}"
                data-text-closed="{_esc(btn_closed)}">{_esc(btn_closed)}</button>
        </div>
        <div class="chart-panel-collapsible" id="{_esc(panel_id)}" hidden>
            <div class="chart-details-body">{inner}</div>
        </div>
        """


def _build_trade_card(
    pos: Dict[str, Any],
    *,
    is_open: bool = False,
    section_id: str = "",
    orders_table_html: str = "",
    chart_section_html: str = "",
    settings: Optional[dict] = None,
) -> str:
    ticker = str(pos.get("code", "000000")).zfill(6)
    name = pos.get("name", "Unknown")
    events_html = _build_events_html(pos)
    eval_lines = ""

    if is_open:
        try:
            qty = int(pos.get("qty") or 0)
        except (TypeError, ValueError):
            qty = 0
        try:
            ap = float(pos.get("avg_price") or 0)
        except (TypeError, ValueError):
            ap = 0.0
        try:
            stg = int(pos.get("stage") or 1)
        except (TypeError, ValueError):
            stg = 1
        unreal = ""
        if ap > 0:
            try:
                cpf = float(pos.get("current_price") or 0)
            except (TypeError, ValueError):
                cpf = 0.0
            if cpf > 0:
                u_pct = (cpf / ap - 1.0) * 100.0
                unreal = f" · 현재가 기준 {u_pct:+.2f}%"
        et_raw = pos.get("entry_time")
        if isinstance(et_raw, str) and et_raw.strip():
            ers = et_raw.strip().replace("T", " ")
            if len(ers) >= 16 and ers[4] == "-":
                et_lbl = ers[:19]
            else:
                et_lbl = _event_display_time({"time": ers}, pos)
        else:
            et_lbl = "-"
        sub = f'<div class="t-sub">진입 시각: {et_lbl}</div>'
        if isinstance(settings, dict):
            try:
                _, sell_d = fee_decimal_rates_live_trading(settings)
                net_v, gross_v, fee_v = _open_net_gross_sell_fee(pos, sell_d)
                if gross_v > 0:
                    eval_lines = (
                        f'<div class="t-sub t-eval">평가 액면: {gross_v:,.0f}원 · '
                        f"매도수수료(추정): {fee_v:,.0f}원 · "
                        f'<b style="color:var(--text-bright);">순입금 추정: {net_v:,.0f}원</b>'
                        f' <span style="color:var(--text);font-size:11px;">(설정 매도율 반영)</span></div>'
                    )
            except Exception:
                eval_lines = ""
        header_right = f'<div class="t-result t-open">보유 중 (S{stg}) · {qty}주 @ {int(ap):,}원{unreal}</div>'
    else:
        try:
            profit_pct = float(pos.get("final_pnl_pct", 0.0))
        except (TypeError, ValueError):
            profit_pct = 0.0
        try:
            profit_amt = int(pos.get("realized_pnl_amt", 0))
        except (TypeError, ValueError):
            profit_amt = 0
        status_cls = "p-plus" if profit_pct > 0 else "p-minus"
        header_right = f'<div class="t-result {status_cls}">{profit_pct:+.2f}% ({profit_amt:+,}원)</div>'
        ex_dt = str(pos.get("exit_datetime") or "").strip()
        en_dt = str(pos.get("entry_time") or "").strip().replace("T", " ")[:19]
        if not ex_dt and pos.get("exit_date") and pos.get("exit_time"):
            ex_dt = f'{pos.get("exit_date")} {pos.get("exit_time")}'
        try:
            n_merge = int(pos.get("_merged_rounds") or 1)
        except (TypeError, ValueError):
            n_merge = 1
        if n_merge > 1:
            sub = (
                f'<div class="t-sub">동일 종목 청산 <b>{n_merge}</b>회 합산 · '
                f"최초 진입: {en_dt or '-'} · 최종 청산: {ex_dt or '-'}</div>"
            )
        else:
            sub = f'<div class="t-sub">진입: {en_dt or "-"} · 청산: {ex_dt or "-"}</div>'

    sid = section_id.strip().replace('"', "").replace("'", "") if section_id else ""
    id_attr = f' id="{sid}"' if sid else ""
    return f"""
        <div class="trade-card"{id_attr}>
            <div class="trade-header">
                <div class="t-name-wrap">
                    <div class="t-name"><b>{name}</b> <small>{ticker}</small></div>
                    {sub}
                    {eval_lines}
                </div>
                {header_right}
            </div>
            <div class="trade-body">
                {orders_table_html}
                <div class="t-timeline-label">매수·매도 타임라인 (가상 체결·스냅샷, 시간순)</div>
                {events_html}
                {chart_section_html}
            </div>
        </div>
        """


def generate_virtual_narrative_report(
    history: Optional[List[Dict[str, Any]]] = None,
    output_path: str = "virtual_trade_report.html",
    settings: Optional[dict] = None,
    open_positions: Optional[List[Dict[str, Any]]] = None,
    data_manager: Any = None,
    progress_callback: Optional[Callable[[int, int, str], None]] = None,
) -> None:
    """
    가상 매매 종합 리포트: 나러티브 타임라인 + 포트폴리오 백테스트 리포트와 동일한 통계 블록.
    동일 종목코드 청산 이력은 한 카드로 병합.
    open_positions: 미청산 virtual_positions.
    data_manager: 있으면 trading_state.db 주문·체결 원장을 종목별로 표시(일일 결산과 동일 열).
    progress_callback: GUI 진행 표시용 (done, total, message). total은 고정, done은 0..total.
    """
    history_raw = list(history or [])
    open_positions = list(open_positions or [])
    # 보유 건에 current_price가 없으면 평단으로 채워 0% MTM으로라도 집계·카드 표시를 일관화
    for _op in open_positions:
        if not isinstance(_op, dict):
            continue
        try:
            ap0 = float(_op.get("avg_price") or 0)
        except (TypeError, ValueError):
            ap0 = 0.0
        try:
            cp0 = float(_op.get("current_price"))
        except (TypeError, ValueError):
            cp0 = 0.0
        if cp0 <= 0 and ap0 > 0:
            _op["current_price"] = ap0

    def _emit_pg(done: int, total: int, msg: str) -> None:
        """진행률 콜백(스레드·GUI에서 예외 없이 호출 가능)."""
        if progress_callback is None:
            return
        try:
            tt = max(1, int(total))
            dd = max(0, min(int(done), tt))
            progress_callback(dd, tt, str(msg))
        except Exception:
            pass

    if not history_raw and not open_positions:
        _emit_pg(0, 1, "표시할 내역 없음 — 빈 리포트 작성 중…")
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(
                    "<html><body style='background:#1a1b26; color:#a9b1d6; padding:50px; text-align:center; font-family:sans-serif;'>"
                )
                f.write(
                    "<h1>표시할 가상 매매 내역이 없습니다.</h1>"
                    "<p>보유 포지션이 없고, 청산 완료된 이력(virtual_history)도 없습니다.</p></body></html>"
                )
        except Exception:
            pass
        _emit_pg(1, 1, "완료")
        return

    # 청산 이력 병합(통계·카드 단계 수 산정에 필요) — 본문 카드와 동일 순서
    history_display = _merge_and_order_closed_history(
        [h for h in history_raw if isinstance(h, dict)]
    )
    n_open = sum(1 for p in open_positions if isinstance(p, dict))
    n_closed = sum(1 for p in history_display if isinstance(p, dict))
    total_pg = max(2, n_open + n_closed + 2)

    # 통계·카드·차트 전역에서 사용 (통계 블록보다 먼저 정의해야 함)
    _s = settings if isinstance(settings, dict) else {}

    # [1] 포트폴리오 백테스트 리포트와 같은 「백테스트 분석 통계」 (실시간 설정 파라미터는 제외)
    _emit_pg(0, total_pg, "백테스트형 통계·분석 블록 생성 중…")
    portfolio_analysis_html = ""
    try:
        _fn_df, _fn_html, _imp_err = _resolve_virtual_analysis_helpers()
        if _fn_df is not None and _fn_html is not None:
            _adf = _fn_df(history_raw, open_positions, _s)
            _sb_blocked = 0
            try:
                from reporting.trade_report import (
                    collect_session_jsonl_days_for_virtual_history,
                    count_session_kind_events,
                )

                _log_days = collect_session_jsonl_days_for_virtual_history(
                    history_raw, open_positions
                )
                _sb_blocked = count_session_kind_events(
                    "smart_buy_blocked", _log_days
                )
            except Exception:
                pass
            portfolio_analysis_html = _fn_html(
                _adf,
                open_holdings_count=len(open_positions),
                session_smart_buy_blocked=_sb_blocked,
            )
        else:
            _stat_err_html = (
                str(_imp_err or "알 수 없음")
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            portfolio_analysis_html = (
                '<div class="vr-analysis-wrap"><p class="depth-subnote" style="color:#bf616a;">'
                "<b>통계 모듈 로드 실패</b> — 백테스트형 통계를 건너뜁니다.</p>"
                f'<p class="depth-subnote" style="font-size:0.8rem;opacity:0.9;">{_stat_err_html}</p>'
                '<p class="depth-subnote">힌트: GUI를 저장소 루트 폴더에서 실행하고, '
                "<code>reporting/trade_report.py</code>에 가상 통계 함수가 포함된 최신 버전인지 확인하세요.</p></div>"
            )
            try:
                print(f"[virtual_report] 통계 헬퍼 로드 실패: {_imp_err}")
            except Exception:
                pass
    except Exception as _e_stats:
        portfolio_analysis_html = (
            f'<div class="vr-analysis-wrap"><p class="depth-subnote" style="color:#bf616a;">'
            f"통계 생성 오류: {_e_stats}</p></div>"
        )

    # [2] 좌측 바로가기용 앵커 (history_display 는 상단에서 이미 병합됨)
    nav_items: List[Tuple[str, str, str]] = [("v-top", "↑ 맨 위", "")]
    # 동일 리포트 생성 중 (종목 6자리, YYYY-MM-DD) → 15분봉 일자 REST 결과 재사용
    rest_day_cache: Dict[Tuple[str, str], Any] = {}
    open_parts: List[str] = []
    pg_step = 1
    for i, p in enumerate(open_positions):
        if not isinstance(p, dict):
            continue
        code = str(p.get("code", "")).zfill(6)
        nm_disp = str(p.get("name", "Unknown"))
        _emit_pg(
            pg_step,
            total_pg,
            f"보유 {nm_disp} ({code}) — 주문·15분 차트 조회 중…",
        )
        nid = f"v-open-{code}-{i}"
        nav_items.append((nid, nm_disp, code))
        try:
            _ot = _fetch_orders_table_html_for_position(
                data_manager, _s, p, is_open=True
            )
        except Exception:
            _ot = ""
        try:
            _ch = _build_virtual_chart_section_html(
                p,
                True,
                data_manager,
                _s,
                nid,
                str(p.get("name", "Unknown")),
                code,
                rest_day_cache=rest_day_cache,
            )
        except Exception as _e_ch:
            _ch = f'<p class="warn" style="color:#f7768e;">15분 차트: {_esc(str(_e_ch))}</p>'
        open_parts.append(
            _build_trade_card(
                p,
                is_open=True,
                section_id=nid,
                orders_table_html=_ot,
                chart_section_html=_ch,
                settings=_s,
            )
        )
        pg_step += 1
    open_cards_html = "".join(open_parts)

    closed_parts: List[str] = []
    for i, p in enumerate(history_display):
        if not isinstance(p, dict):
            continue
        code = str(p.get("code", "")).zfill(6)
        nm_c = str(p.get("name", "Unknown"))
        _emit_pg(
            pg_step,
            total_pg,
            f"청산 {nm_c} ({code}) — 주문·15분 차트 조회 중…",
        )
        nid = f"v-closed-{code}-{i}"
        nav_items.append((nid, nm_c, code))
        try:
            _ot = _fetch_orders_table_html_for_position(
                data_manager, _s, p, is_open=False
            )
        except Exception:
            _ot = ""
        try:
            _ch = _build_virtual_chart_section_html(
                p,
                False,
                data_manager,
                _s,
                nid,
                str(p.get("name", "Unknown")),
                code,
                rest_day_cache=rest_day_cache,
            )
        except Exception as _e_ch2:
            _ch = f'<p class="warn" style="color:#f7768e;">15분 차트: {_esc(str(_e_ch2))}</p>'
        closed_parts.append(
            _build_trade_card(
                p,
                is_open=False,
                section_id=nid,
                orders_table_html=_ot,
                chart_section_html=_ch,
                settings=_s,
            )
        )
        pg_step += 1
    closed_trades_html = "".join(closed_parts)

    sidebar_links: List[str] = []
    for nid, nm, cd in nav_items:
        if nid == "v-top":
            sidebar_links.append(
                f'<li class="vr-nav-item vr-nav-top"><a href="#{nid}">{nm}</a></li>'
            )
        else:
            badge = f'<span class="vr-code-badge">{cd}</span>' if cd else ""
            esc_nm = (
                str(nm).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            sidebar_links.append(
                f'<li class="vr-nav-item"><a href="#{nid}">'
                f'<span class="vr-nav-name">{esc_nm}</span>{badge}</a></li>'
            )
    sidebar_html = "\n            ".join(sidebar_links)

    try:
        _, _sell_fee_dec = fee_decimal_rates_live_trading(_s)
    except Exception:
        _sell_fee_dec = 0.0
    total_open_net = 0.0
    total_open_gross = 0.0
    total_open_sell_fee = 0.0
    for _op in open_positions:
        if not isinstance(_op, dict):
            continue
        try:
            n, g, f = _open_net_gross_sell_fee(_op, _sell_fee_dec)
            total_open_net += n
            total_open_gross += g
            total_open_sell_fee += f
        except Exception:
            pass

    # [2] 통계 계산 — 청산 완료 + 미청산(현재가 MTM)을 함께 반영
    win_cnt = sum(1 for p in history_raw if isinstance(p, dict) and p.get("final_pnl_pct", 0) > 0)
    for _op in open_positions:
        if not isinstance(_op, dict):
            continue
        m_pct, _ = _open_mtm_pct_and_krw(_op, _s)
        if m_pct is not None and m_pct > 0:
            win_cnt += 1
    n_stats = len(history_raw) + len(open_positions)
    win_rate = (win_cnt / n_stats) * 100 if n_stats else 0.0
    total_pnl = sum(
        int(p.get("realized_pnl_amt") or 0) for p in history_raw if isinstance(p, dict)
    )
    for _op in open_positions:
        if not isinstance(_op, dict):
            continue
        _pct_u, krw_u = _open_mtm_pct_and_krw(_op, _s)
        if _pct_u is not None:
            try:
                total_pnl += int(round(float(krw_u)))
            except (TypeError, ValueError):
                pass
    pct_parts: List[float] = [
        float(p.get("final_pnl_pct", 0) or 0) for p in history_raw if isinstance(p, dict)
    ]
    for _op in open_positions:
        if not isinstance(_op, dict):
            continue
        m_pct, _ = _open_mtm_pct_and_krw(_op, _s)
        if m_pct is not None:
            pct_parts.append(float(m_pct))
    avg_pnl_pct = (sum(pct_parts) / len(pct_parts)) if pct_parts else 0.0

    _db_line = ""
    if data_manager is not None:
        _db_line = (
            '<p style="color:var(--text); font-size:12px; margin-top:10px; line-height:1.5;">'
            "각 종목 카드 상단에 <b>주문·체결 원장</b>(<code>trading_state.db</code> "
            "<code>order_history</code>)을 표시합니다. 일일 결산 리포트와 동일한 열 구성입니다.</p>"
        )

    # [3] HTML 템플릿 (좌측 종목 바로가기 + 본문)
    try:
        from plotly.io._html import plotly_cdn_url

        _vr_cdn_js = json.dumps(plotly_cdn_url())
    except Exception:
        _vr_cdn_js = json.dumps("")

    html = (
        f"""
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8">
        <title>가상 매매 종합 리포트</title>
        <script>window.__DAILY_REPORT_PLOTLY_CDN__ = {_vr_cdn_js};</script>
        <style>
            :root {{
                --bg: #1a1b26;
                --card: #24283b;
                --text: #787c99;
                --text-bright: #a9b1d6;
                --primary: #7aa2f7;
                --plus: #9ece6a;
                --minus: #f7768e;
                --accent: #bb9af7;
                --border: #414868;
                --sidebar-w: 268px;
            }}
            html {{ scroll-behavior: smooth; }}
            body {{
                background: var(--bg);
                color: var(--text-bright);
                font-family: 'Pretendard', 'Malgun Gothic', sans-serif;
                margin: 0;
                line-height: 1.6;
            }}
            .vr-sidebar {{
                position: fixed;
                left: 0;
                top: 0;
                bottom: 0;
                width: var(--sidebar-w);
                background: #16171f;
                border-right: 1px solid var(--border);
                z-index: 1000;
                display: flex;
                flex-direction: column;
                overflow-y: auto;
            }}
            .vr-sidebar-header {{
                padding: 18px 16px;
                border-bottom: 1px solid var(--border);
                font-size: 0.95rem;
                font-weight: 800;
                color: var(--primary);
            }}
            .vr-nav-list {{
                list-style: none;
                margin: 0;
                padding: 10px 8px 24px;
            }}
            .vr-nav-item a {{
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 8px;
                padding: 10px 12px;
                margin-bottom: 4px;
                border-radius: 10px;
                color: var(--text-bright);
                text-decoration: none;
                font-size: 0.88rem;
                border: 1px solid transparent;
            }}
            .vr-nav-item a:hover {{
                background: var(--card);
                border-color: var(--border);
                color: #eceff4;
            }}
            .vr-nav-top a {{ color: var(--accent); font-weight: 700; }}
            .vr-nav-name {{ flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
            .vr-code-badge {{
                font-family: monospace;
                font-size: 0.68rem;
                background: #3b4252;
                color: #d8dee9;
                padding: 2px 6px;
                border-radius: 4px;
                flex-shrink: 0;
            }}
            .vr-main {{
                margin-left: var(--sidebar-w);
                padding: 28px 32px 48px;
                min-height: 100vh;
            }}
            .container {{ max-width: 1040px; margin: 0 auto; }}
            header#v-top {{ margin-bottom: 36px; border-bottom: 1px solid var(--border); padding-bottom: 20px; }}
            h1 {{ margin: 0; font-weight: 600; color: var(--primary); letter-spacing: -0.5px; font-size: 1.75rem; }}
            .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 15px; margin-bottom: 28px; }}
            .stat-box {{ background: var(--card); padding: 20px; border-radius: 12px; text-align: center; border: 1px solid var(--border); }}
            .stat-val {{ display: block; font-size: 24px; font-weight: 700; color: #fff; }}
            .stat-label {{ font-size: 13px; color: var(--text); }}
            .trade-section {{ display: flex; flex-direction: column; gap: 20px; }}
            .trade-card {{
                background: var(--card);
                border-radius: 16px;
                overflow: hidden;
                border: 1px solid var(--border);
                transition: transform 0.2s, border-color 0.2s;
                scroll-margin-top: 88px;
            }}
            .trade-card:hover {{ transform: translateY(-2px); border-color: var(--primary); }}
            .trade-header {{ background: rgba(0,0,0,0.2); padding: 15px 25px; display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; border-bottom: 1px solid var(--border); }}
            .t-name-wrap {{ flex: 1; min-width: 0; }}
            .t-name {{ font-size: 18px; }}
            .t-name small {{ color: var(--text); margin-left: 8px; font-family: monospace; }}
            .t-sub {{ font-size: 12px; color: var(--text); margin-top: 6px; line-height: 1.4; }}
            .t-result {{ font-size: 20px; font-weight: 800; white-space: nowrap; }}
            .t-open {{ color: var(--primary); }}
            .trade-body {{ padding: 20px 25px; }}
            .t-timeline-label {{ font-size: 12px; text-transform: uppercase; color: var(--text); margin-bottom: 15px; letter-spacing: 1px; }}
            .event-row {{ margin-bottom: 25px; position: relative; padding-left: 20px; border-left: 2px solid var(--border); }}
            .event-row::before {{ content: ''; position: absolute; left: -7px; top: 5px; width: 12px; height: 12px; background: var(--card); border: 2px solid var(--primary); border-radius: 50%; }}
            .event-meta {{ margin-bottom: 10px; display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }}
            .ev-time {{ color: var(--accent); font-family: monospace; font-size: 14px; }}
            .ev-action {{ padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 800; }}
            .act-buy {{ background: rgba(158, 206, 106, 0.15); color: var(--plus); border: 1px solid var(--plus); }}
            .act-sell {{ background: rgba(247, 118, 142, 0.15); color: var(--minus); border: 1px solid var(--minus); }}
            .ev-price {{ font-weight: 500; font-size: 15px; }}
            .snapshot-card {{ background: rgba(0,0,0,0.15); padding: 12px 18px; border-radius: 8px; display: flex; flex-wrap: wrap; gap: 15px; font-size: 13px; }}
            .snapshot-missing {{ border: 1px dashed var(--border); color: var(--text); font-size: 12px; font-style: italic; }}
            .sn-item span {{ color: var(--text); margin-right: 5px; }}
            .sn-reason {{ width: 100%; border-top: 1px solid var(--border); margin-top: 5px; padding-top: 5px; color: var(--accent); font-style: italic; }}
            .vr-order-block {{ margin-bottom: 18px; }}
            .vr-order-hint {{
                font-size: 0.75rem;
                color: var(--text);
                margin-bottom: 8px;
                line-height: 1.45;
            }}
            .vr-order-wrap {{
                overflow-x: auto;
                border: 1px solid var(--border);
                border-radius: 12px;
                background: rgba(0,0,0,0.12);
            }}
            .vr-order-table {{
                width: 100%;
                border-collapse: collapse;
                font-size: 0.78rem;
                min-width: 1100px;
            }}
            .vr-order-table th, .vr-order-table td {{
                padding: 8px 10px;
                border-bottom: 1px solid var(--border);
                text-align: left;
                white-space: nowrap;
            }}
            .vr-order-table th {{
                color: var(--text);
                font-weight: 600;
                font-size: 0.72rem;
            }}
            .vr-order-table td.num {{
                text-align: right;
                font-variant-numeric: tabular-nums;
                font-family: ui-monospace, monospace;
            }}
            .p-plus {{ color: var(--plus); }}
            .p-minus {{ color: var(--minus); }}
            .vr-analysis-wrap {{
                --dim: #787c99;
                --buy: #7aa2f7;
                --gain: #9ece6a;
                --loss: #bf616a;
                margin-bottom: 8px;
            }}
            .depth-section, .analysis-report-section {{
                background: var(--card);
                border: 1px solid var(--border);
                border-radius: 16px;
                padding: 22px 24px;
                margin-bottom: 24px;
            }}
            .depth-table {{ width: 100%; border-collapse: collapse; font-size: 0.84rem; }}
            .depth-table th, .depth-table td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: middle; }}
            .depth-table th {{ color: var(--text); font-weight: 600; font-size: 0.78rem; }}
            .depth-table td.num {{ text-align: right; font-variant-numeric: tabular-nums; font-family: ui-monospace, monospace; }}
            .analysis-main-title {{ margin: 0 0 8px 0; font-size: 1.35rem; font-weight: 800; color: var(--primary); }}
            .analysis-main-lead {{ color: var(--text); font-size: 0.88rem; line-height: 1.6; margin: 0 0 12px 0; }}
            .analysis-h3 {{ font-size: 1.02rem; font-weight: 700; color: #eceff4; margin: 24px 0 12px 0; padding-bottom: 8px; border-bottom: 1px solid var(--border); }}
            .analysis-h3:first-of-type {{ margin-top: 4px; }}
            .analysis-type-card {{ background: rgba(0,0,0,0.12); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; margin-bottom: 14px; }}
            .analysis-type-card.type-a {{ border-left: 4px solid #9ece6a; }}
            .analysis-type-card.type-b {{ border-left: 4px solid #88c0d0; }}
            .analysis-type-card.type-c {{ border-left: 4px solid #c084fc; }}
            .depth-subtitle {{ font-size: 0.92rem; font-weight: 700; margin-bottom: 8px; color: #eceff4; }}
            .depth-lead {{ color: var(--text); font-size: 0.86rem; line-height: 1.55; }}
            .depth-subnote {{ font-size: 0.82rem; color: var(--text); line-height: 1.5; }}
            .depth-open-box {{ background: rgba(180,142,173,0.1); border: 1px solid var(--border); border-radius: 12px; padding: 14px 18px; margin-bottom: 18px; font-size: 0.88rem; }}
            .depth-verdict {{ margin-top: 14px; padding: 12px 14px; background: rgba(94,129,172,0.1); border: 1px solid var(--border); border-radius: 10px; font-size: 0.85rem; color: var(--text-bright); }}
            .depth-err {{ color: #bf616a; }}
            .analysis-stack-spaced > .analysis-subblock + .analysis-subblock {{ margin-top: 18px; }}
            td.gain, span.gain {{ color: var(--plus); font-weight: 600; }}
            td.loss, span.loss {{ color: var(--minus); font-weight: 600; }}
            .depth-bucket-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
            @media (max-width: 900px) {{
                .vr-sidebar {{ position: relative; width: 100%; border-right: none; border-bottom: 1px solid var(--border); }}
                .vr-main {{ margin-left: 0; padding: 16px; }}
                .stats-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
                .depth-bucket-grid {{ grid-template-columns: 1fr; }}
            }}
            footer {{ margin-top: 48px; text-align: center; color: var(--text); font-size: 12px; }}
            /* 일일 결산과 동일 15분 차트: 펼침 + Plotly (plotly_deferred_chart_js) */
            .vr-stock-chart-toolbar {{
                display: flex;
                flex-wrap: wrap;
                align-items: center;
                justify-content: space-between;
                gap: 12px;
                margin-top: 20px;
                padding-top: 12px;
                border-top: 1px solid var(--border);
            }}
            .vr-stock-chart-toolbar-label {{ font-size: 0.85rem; }}
            .chart-toggle-btn {{
                background: #7aa2f7;
                color: #1a1b26;
                border: none;
                padding: 8px 14px;
                border-radius: 6px;
                font-size: 0.88rem;
                font-weight: 600;
                cursor: pointer;
                flex-shrink: 0;
            }}
            .chart-toggle-btn:hover {{ filter: brightness(1.08); }}
            .chart-panel-collapsible {{ margin-top: 12px; }}
            .chart-details-body {{ padding-top: 4px; border-top: 1px solid var(--border); }}
            .chartwrap {{ overflow-x: auto; }}
        </style>
    </head>
    <body>
        <aside class="vr-sidebar" aria-label="종목 바로가기">
            <div class="vr-sidebar-header">종목 바로가기</div>
            <ul class="vr-nav-list">
            {sidebar_html}
            </ul>
        </aside>
        <div class="vr-main">
        <div class="container">
            <header id="v-top">
                <h1>가상 매매 종합 리포트</h1>
                <p style="color:var(--text); margin:8px 0 0 0;">의사결정 시점 스냅샷·나러티브와 포트폴리오 백테스트형 통계를 함께 봅니다.</p>
                <p style="color:var(--text); font-size:12px; margin-top:8px; line-height:1.5;">각 종목 카드 하단에 <b>15분봉 차트</b>(캔들·거래량·MA·주문 시각은 일일 결산과 동일 형식, 데이터는 <b>진입일~청산일(미청산 시 현재일)</b> 구간의 거래일 15분봉을 이어 붙임)을 펼쳐 볼 수 있습니다. <code>file://</code> 로 열어도 동작합니다.</p>
                {_db_line}
            </header>

            <div class="stats-grid">
                <div class="stat-box">
                    <span class="stat-val">{len(history_raw)}</span>
                    <span class="stat-label">청산 완료</span>
                </div>
                <div class="stat-box">
                    <span class="stat-val { 'p-plus' if win_rate >= 50 else 'p-minus' }">{win_rate:.1f}%</span>
                    <span class="stat-label">Win Rate</span>
                </div>
                <div class="stat-box">
                    <span class="stat-val { 'p-plus' if total_pnl > 0 else 'p-minus' }">{int(total_pnl):+,}</span>
                    <span class="stat-label">Total P&amp;L (KRW)</span>
                </div>
                <div class="stat-box">
                    <span class="stat-val { 'p-plus' if avg_pnl_pct > 0 else 'p-minus' }">{avg_pnl_pct:+.2f}%</span>
                    <span class="stat-label">Avg Return</span>
                </div>
                <div class="stat-box">
                    <span class="stat-val" style="color:var(--primary);">{int(round(total_open_net)):,}</span>
                    <span class="stat-label">미청산 평가(순·매도비)</span>
                </div>
            </div>
            {(
                f'<p style="color:var(--text);font-size:13px;margin:-12px 0 8px 0;">'
                f'미청산 보유: <b style="color:var(--primary);">{len(open_positions)}</b> 종목 · '
                f'평가 액면 합 <b>{total_open_gross:,.0f}</b>원 · 매도수수료(추정) <b>{total_open_sell_fee:,.0f}</b>원 · '
                f'<b style="color:var(--text-bright);">순입금 추정 합 {int(round(total_open_net)):,}원</b>'
                f' <span style="font-size:11px;opacity:0.85;">(현재가×수량, settings 매도 수수료율)</span></p>'
            ) if open_positions else ""}

            {portfolio_analysis_html}

            {f'<div style="color:var(--text); font-size:12px; margin-bottom:10px; margin-top:8px;">보유 중 (미청산)</div><div class="trade-section">{open_cards_html}</div>' if open_cards_html else ""}
            <div style="color:var(--text); font-size:12px; margin-bottom:10px; margin-top:28px;">청산 완료</div>
            <div class="trade-section">
                {closed_trades_html if closed_trades_html else '<p style="color:var(--text);padding:12px 0;">청산 완료된 가상 매매 이력이 없습니다.</p>'}
            </div>

            <footer>
                © 2026 Antigravity Trading System | 생성 시각 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            </footer>
        </div>
        </div>
    <script>
"""
        + PLOTLY_DEFERRED_CHART_IIFE
        + """
    </script>
    </body>
    </html>
    """
    )

    _emit_pg(pg_step, total_pg, "HTML 파일 저장 중…")
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"✅ 종합 리포트 생성: {output_path}")
    except Exception as _e_write:
        try:
            print(f"[virtual_report] 리포트 저장 실패: {_e_write}")
        except Exception:
            pass
        raise
    _emit_pg(total_pg, total_pg, "완료")
