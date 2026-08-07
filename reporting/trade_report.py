from __future__ import annotations

import os
import json
import re
import logging
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import sys

# Add root directory to sys.path for absolute imports of root modules
root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.append(root_dir)

from strategy.data_loader import BacktestDataLoader
from utils.plotly_cdn import plotly_cdn_script_tag


def _depth_exit_bucket(reason: str) -> str:
    """청산 사유 문자열을 심층 분석용 버킷으로 분류."""
    if not reason or pd.isna(reason):
        return "OTHER"
    r = str(reason).upper()
    if "TAKE_PROFIT" in r:
        return "TP"
    if "STOP_LOSS" in r:
        return "SL"
    if "ATR_TRAIL" in r:
        return "ATR_TRAIL"
    if "ATR_SL" in r:
        return "ATR_SL"
    if "FLASH" in r:
        return "FLASH"
    if "SMART" in r:
        return "SMART"
    if "GAP" in r:
        return "GAP"
    return "OTHER"


def _fmt_pct(v: float, digits: int = 4) -> str:
    try:
        return f"{float(v):+.{digits}f}%"
    except (TypeError, ValueError):
        return "-"


def _build_depth_analytics_html(df: pd.DataFrame) -> str:
    """완료 라운드트립 기준 수익률 분포·집중도·S2 여부 HTML 블록 생성."""
    try:
        if df is None or df.empty:
            return ""

        work = df.sort_values(["Code", "Date"]).copy()
        rows = work.to_dict("records")
        n = len(rows)

        trips: list[tuple[bool, float, float, str, str, object]] = []
        open_rows: list[tuple[str, object]] = []
        idx = 0
        while idx < n:
            r = rows[idx]
            act = str(r.get("Action", ""))
            if not act.upper().startswith("BUY"):
                idx += 1
                continue
            code = str(r.get("Code", ""))
            had_s2 = False
            cost = 0.0
            fees = 0.0
            j = idx
            while j < n and str(rows[j].get("Code", "")) == code:
                a = str(rows[j].get("Action", ""))
                if not a.upper().startswith("BUY"):
                    break
                if "BUY2" in a.upper() or a.upper() == "ADDON":
                    had_s2 = True
                px = float(rows[j].get("Price", 0) or 0)
                qy = int(float(rows[j].get("Qty", 0) or 0))
                fee = float(rows[j].get("Fee", 0) or 0) if not pd.isna(rows[j].get("Fee", 0)) else 0.0
                cost += px * qy + fee
                fees += fee
                j += 1

            if j >= n or str(rows[j].get("Code", "")) != code:
                open_rows.append((code, rows[idx].get("Date")))
                idx = j
                continue

            ex = rows[j]
            ex_act = str(ex.get("Action", ""))
            if ex_act not in ("SELL", "HOLD"):
                open_rows.append((code, rows[idx].get("Date")))
                idx = j + 1
                continue

            sell_p = float(ex.get("Price", 0) or 0)
            sell_q = int(float(ex.get("Qty", 0) or 0))
            ret_pct = float(ex.get("Return", 0) or 0) if not pd.isna(ex.get("Return", 0)) else 0.0
            fee_s = float(ex.get("Fee", 0) or 0) if not pd.isna(ex.get("Fee", 0)) else 0.0
            fees += fee_s
            revenue = sell_p * sell_q
            profit_krw = revenue - cost - fee_s
            reason = str(ex.get("Reason", ""))
            trips.append((had_s2, ret_pct, profit_krw, reason, code, ex.get("Date")))
            idx = j + 1

        if not trips and not open_rows:
            return ""

        profits = np.array([t[2] for t in trips], dtype=float)
        rets = np.array([t[1] for t in trips], dtype=float)
        total_p = float(profits.sum()) if len(profits) else 0.0

        def stat_block(title: str, arr: np.ndarray) -> str:
            if arr.size == 0:
                return f"<tr><td colspan='7' style='color:var(--dim);'>{title}: 데이터 없음</td></tr>"
            return (
                f"<tr><td><b>{title}</b></td>"
                f"<td class='num'>{arr.size}</td>"
                f"<td class='num'>{_fmt_pct(float(arr.mean()))}</td>"
                f"<td class='num'>{_fmt_pct(float(np.median(arr)))}</td>"
                f"<td class='num'>{_fmt_pct(float(np.percentile(arr, 25)))}</td>"
                f"<td class='num'>{_fmt_pct(float(np.percentile(arr, 75)))}</td>"
                f"<td class='num'>{_fmt_pct(float(arr.min()))} ~ {_fmt_pct(float(arr.max()))}</td></tr>"
            )

        s1_mask = np.array([not t[0] for t in trips], dtype=bool)
        s2_mask = ~s1_mask
        rets_s1 = rets[s1_mask]
        rets_s2 = rets[s2_mask]

        conc_rows = ""
        if len(profits) > 0 and total_p != 0:
            order = np.argsort(-profits)
            sorted_p = profits[order]
            nt = len(sorted_p)

            def share_top(k: int) -> float:
                return 100.0 * float(sorted_p[:k].sum()) / total_p

            k10 = max(1, int(np.ceil(nt * 0.1)))
            half = nt // 2
            order_asc = np.argsort(profits)
            low_half = float(profits[order_asc[:half]].sum()) if half > 0 else 0.0
            rest_1 = float(total_p - sorted_p[0])

            pos = profits[profits > 0]
            pos_share_lines = ""
            if len(pos) > 0 and pos.sum() > 0:
                ps = np.sort(pos)[::-1]
                for k in (1, 3, 5):
                    if k <= len(ps):
                        pct = 100.0 * float(ps[:k].sum()) / float(pos.sum())
                        pos_share_lines += f"<div>이익 거래 상위 {k}건 → 전체 <b>이익 합</b>의 <b>{pct:.1f}%</b></div>"

            conc_rows = f"""
            <table class="depth-table">
                <thead><tr><th>지표</th><th>값</th></tr></thead>
                <tbody>
                    <tr><td>상위 1건 / 전체 손익</td><td class="num"><b>{share_top(1):.1f}%</b></td></tr>
                    <tr><td>상위 3건 누적</td><td class="num">{share_top(3):.1f}%</td></tr>
                    <tr><td>상위 5건 누적</td><td class="num">{share_top(5):.1f}%</td></tr>
                    <tr><td>상위 10건 누적</td><td class="num">{share_top(10):.1f}%</td></tr>
                    <tr><td>상위 10% ({k10}건) 누적</td><td class="num">{share_top(k10):.1f}%</td></tr>
                    <tr><td>손익 낮은 50% ({half}건) 합계</td><td class="num">{low_half:,.0f} 원</td></tr>
                    <tr><td>최고 1건 제외 후 합계</td><td class="num">{rest_1:,.0f} 원</td></tr>
                </tbody>
            </table>
            <div class="depth-subnote">{pos_share_lines}</div>
            """

            top1_ratio = share_top(1) / 100.0
            verdict = (
                "상위 소수 거래에 손익이 크게 쏠려 있습니다. 진입·청산 필터를 점검할 만합니다."
                if top1_ratio >= 0.35 or share_top(3) >= 55
                else "한두 건에 전체가 매달린 극단 구조는 아닙니다. 다만 상위 꼬리가 손익을 당기는 전형적 분포일 수 있습니다."
            )
            if rest_1 < 0 and total_p > 0:
                verdict = "최고 1건을 제외하면 합계가 마이너스에 가깝습니다. 소수 대박 의존도가 매우 높습니다."
            elif rest_1 > 0 and total_p > 0 and share_top(5) < 55:
                verdict = "최고권 거래에 덜 의존하는 편입니다. 꼬리 이익과 손실 분포를 함께 보시면 됩니다."
        else:
            verdict = "손익 합계가 0에 가깝거나 완료 라운드가 없어 집중도 해석이 제한됩니다."
            conc_rows = "<p class='depth-subnote'>집중도 계산 생략 (손익 합 0 또는 건수 없음)</p>"

        bucket_rows_s1 = ""
        bucket_rows_s2 = ""

        b1: dict[str, list[float]] = defaultdict(list)
        b2: dict[str, list[float]] = defaultdict(list)
        for t in trips:
            b = _depth_exit_bucket(t[3])
            (b2 if t[0] else b1)[b].append(t[1])

        def bucket_table(bmap: dict[str, list[float]], title: str) -> str:
            if not any(bmap.values()):
                return f"<p class='depth-subnote'>{title}: 해당 없음</p>"
            lines = [
                f"<table class='depth-table'><thead><tr><th>{title}</th><th>n</th><th>수익률 중앙값</th><th>평균</th></tr></thead><tbody>"
            ]
            for b in sorted(bmap.keys()):
                v = bmap[b]
                if not v:
                    continue
                arr = np.array(v, dtype=float)
                lines.append(
                    f"<tr><td>{b}</td><td class='num'>{len(v)}</td>"
                    f"<td class='num'>{_fmt_pct(float(np.median(arr)))}</td>"
                    f"<td class='num'>{_fmt_pct(float(arr.mean()))}</td></tr>"
                )
            lines.append("</tbody></table>")
            return "".join(lines)

        bucket_rows_s1 = bucket_table(b1, "S2 없음")
        bucket_rows_s2 = bucket_table(b2, "S2 포함")

        open_html = ""
        if open_rows:
            parts = "".join(
                f"<li><code>{c}</code> — {d}</li>" for c, d in open_rows
            )
            open_html = f"""
            <div class="depth-open-box">
                <b>미청산 추정</b> (테스트 종료 시점에 SELL/HOLD 없이 BUY만 있는 종목 · 집계에서 제외)
                <ul style="margin:8px 0 0 18px; color:var(--dim); font-size:0.88rem;">{parts}</ul>
            </div>
            """

        return f"""
            {open_html}
            <div class="analysis-stack-spaced">
                <div class="analysis-subblock">
                    <div class="depth-subtitle">수익률 분포 (%)</div>
                    <table class="depth-table">
                        <thead>
                            <tr>
                                <th>구분</th><th>n</th><th>평균</th><th>중앙값</th><th>p25</th><th>p75</th><th>min ~ max</th>
                            </tr>
                        </thead>
                        <tbody>
                            {stat_block("전체 완료", rets)}
                            {stat_block("S2 없음 (1차만)", rets_s1)}
                            {stat_block("S2 포함", rets_s2)}
                        </tbody>
                    </table>
                </div>
                <div class="analysis-subblock">
                    <div class="depth-subtitle">손익 집중도 (원화 추정)</div>
                    {conc_rows}
                    <p class="depth-verdict"><b>해석:</b> {verdict}</p>
                </div>
                <div class="analysis-subblock">
                    <div class="depth-subtitle">청산 유형별 수익률 (%) — S2 없음</div>
                    {bucket_rows_s1}
                </div>
                <div class="analysis-subblock">
                    <div class="depth-subtitle">청산 유형별 수익률 (%) — S2 포함</div>
                    {bucket_rows_s2}
                </div>
            </div>
        """
    except Exception as e:
        return f'<p class="depth-err">심층 분석 생성 오류: {e}</p>'


def _reason_matches_smart_exit(reason_series: pd.Series) -> pd.Series:
    """고정밀(1M_SMART_EXIT+…), 엔진(SMART_EXIT), 구형 표기(Smart Exit) 모두 인식."""
    r = reason_series.fillna('').astype(str)
    return r.str.contains(r'SMART_EXIT|Smart\s*Exit', case=False, na=False, regex=True)


def _reason_matches_smart_buy(reason_series: pd.Series) -> pd.Series:
    """SmartBuy|, 1M_SmartBuy|, SmartBuy Rejected, smart_buy 등 변형 인식."""
    r = reason_series.fillna('').astype(str)
    return r.str.contains(r'SmartBuy|SMART_BUY|smart_buy', case=False, na=False, regex=True)


def _reason_str_has_smart_buy(reason: Any) -> bool:
    """단일 Reason 문자열에 스마트 진입(통과) 태깅이 있는지."""
    try:
        s = str(reason or "")
        return bool(re.search(r"SmartBuy|SMART_BUY|smart_buy", s, re.I))
    except Exception:
        return False


def _bucket_virtual_buy_row(
    action: Any,
    sig_type: Any,
    reason: Any,
) -> str:
    """
    가상 체결 매수 행을 Type A/B × Stage S1/S2 네 칸 중 하나로 분류합니다.
    반환: 'AS1'|'AS2'|'BS1'|'BS2'|'' (미분류)
    """
    try:
        act_u = str(action or "").upper().strip()
        if not act_u.startswith("BUY") and act_u != "ADDON":
            return ""
        is_s2 = any(
            x in act_u for x in ("BUY2", "BUY_2ND", "ADDON", "S2")
        ) or act_u == "ADDON"
        st = str(sig_type or "").upper()
        ru = str(reason or "").upper()
        has_a = "TYPE_A" in st or "TYPE_A" in ru
        has_b = "TYPE_B" in st or "TYPE_B" in ru
        if has_a and not has_b:
            return "AS2" if is_s2 else "AS1"
        if has_b:
            return "BS2" if is_s2 else "BS1"
        return ""
    except Exception:
        return ""


def _tag_reason_for_virtual_smart_buy_stats(
    reason: str,
    action: str,
    sig_type: str,
    settings: Optional[Dict[str, Any]],
) -> str:
    """
    실시간 가상 체결은 포지션 `reason`에 `SmartBuy|` 접두를 남기지 않는 경우가 많아
    종합 리포트 스마트 진입 집계가 0으로 나오는 문제를 완화한다.

    설정에서 해당 경로 Smart Buy가 켜져 있으면, **이미 체결된** 매수 진입은 SB 조건을 통과한 것으로 보고
    백테스트와 동일한 `SmartBuy|` 접두를 붙인다. (감시 세션 JSONL과 무관; 거절 건수는 여전히 DF에 없음)
    """
    if not settings:
        return str(reason or "")
    r = str(reason or "")
    if re.search(r"SmartBuy|SMART_BUY|smart_buy", r, re.I):
        return r
    ru = r.upper()
    if "SYNCED" in ru or "RECOVERED" in ru:
        return r
    st = str(sig_type or "").upper()
    is_b = "TYPE_B" in st or "TYPE_B" in ru
    act_u = str(action or "").upper()
    is_s2 = "BUY2" in act_u
    try:
        if is_s2:
            if is_b:
                if bool(settings.get("use_smart_buy_s2_b", True)):
                    return f"SmartBuy|{r}" if r else "SmartBuy|S2"
            else:
                if bool(settings.get("use_smart_buy_s2", True)):
                    return f"SmartBuy|{r}" if r else "SmartBuy|S2"
        else:
            if is_b:
                if bool(settings.get("use_smart_buy_b", True)):
                    return f"SmartBuy|{r}" if r else "SmartBuy|SIG"
            else:
                if bool(settings.get("use_smart_buy_a", False)):
                    return f"SmartBuy|{r}" if r else "SmartBuy|SIG"
    except Exception:
        pass
    return r


def virtual_executions_to_trades_df(
    history: List[Dict[str, Any]],
    open_positions: Optional[List[Dict[str, Any]]] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """
    가상 포지션 이력·보유를 포트폴리오 백테스트 리포트와 동일한 집계(`build_virtual_portfolio_analysis_html`)에
    넘길 수 있도록 체결 로그 형태 DataFrame으로 변환합니다.

    Parameters
    ----------
    settings
        있으면 Smart Buy 통과 집계용으로 BUY 행 Reason에 `SmartBuy|` 접두를 보강한다.
    """
    rows: List[Dict[str, Any]] = []
    open_positions = open_positions or []

    def _parse_dt(v: Any) -> Optional[pd.Timestamp]:
        if v is None or (isinstance(v, str) and not str(v).strip()):
            return None
        try:
            s = str(v).strip().replace("T", " ")
            if len(s) >= 19:
                return pd.to_datetime(s[:19])
            return pd.to_datetime(s)
        except Exception:
            try:
                return pd.to_datetime(v)
            except Exception:
                return None

    try:
        for h in history or []:
            if not isinstance(h, dict):
                continue
            code = str(h.get("code", "")).zfill(6)
            if code == "000000":
                continue
            sig_type = str(h.get("sig_type") or "TYPE_A").upper()
            if "TYPE_B" in str(h.get("reason", "")).upper():
                sig_type = "TYPE_B"
            entry_reason = str(h.get("reason", "") or "")
            exit_reason = str(h.get("exit_reason", "") or "")
            evs = h.get("events") or []
            buy_specs: List[tuple] = []
            for ev in evs:
                if not isinstance(ev, dict):
                    continue
                a = str(ev.get("action", "")).upper()
                if a == "BUY_S1":
                    buy_specs.append(
                        ("BUY", float(ev.get("price") or 0), int(ev.get("qty") or 0))
                    )
                elif a == "BUY_S2":
                    buy_specs.append(
                        ("BUY2", float(ev.get("price") or 0), int(ev.get("qty") or 0))
                    )
            ap = float(h.get("avg_price") or 0)
            iq = int(h.get("qty") or 0)
            if not buy_specs:
                buy_specs.append(("BUY", ap, iq))
                if int(h.get("stage") or 1) >= 2:
                    buy_specs.append(("BUY2", ap, 0))
            elif int(h.get("stage") or 1) >= 2 and not any(
                x[0] == "BUY2" for x in buy_specs
            ):
                buy_specs.append(("BUY2", ap, 0))

            d_entry = _parse_dt(h.get("entry_time")) or pd.Timestamp.now()
            for act, px, qy in buy_specs:
                _reason_row = _tag_reason_for_virtual_smart_buy_stats(
                    entry_reason, act, sig_type, settings
                )
                rows.append(
                    {
                        "Code": code,
                        "Action": act,
                        "Price": px,
                        "Qty": qy,
                        "Fee": 0.0,
                        "Sig_Type": sig_type,
                        "Reason": _reason_row,
                        "Return": 0.0,
                        "Date": d_entry,
                    }
                )
            d_exit = _parse_dt(h.get("exit_datetime")) or _parse_dt(
                f'{h.get("exit_date") or ""} {h.get("exit_time") or ""}'.strip()
            )
            if d_exit is None:
                d_exit = d_entry
            rows.append(
                {
                    "Code": code,
                    "Action": "SELL",
                    "Price": float(h.get("exit_price") or ap),
                    "Qty": iq,
                    "Fee": 0.0,
                    "Sig_Type": sig_type,
                    "Reason": exit_reason,
                    "Return": float(h.get("final_pnl_pct") or 0),
                    "Date": d_exit,
                }
            )

        for p in open_positions:
            if not isinstance(p, dict):
                continue
            code = str(p.get("code", "")).zfill(6)
            if code == "000000":
                continue
            sig_type = str(p.get("sig_type") or "TYPE_A").upper()
            if "TYPE_B" in str(p.get("reason", "")).upper():
                sig_type = "TYPE_B"
            ap = float(p.get("avg_price") or 0)
            iq = int(p.get("qty") or 0)
            d_entry = _parse_dt(p.get("entry_time")) or pd.Timestamp.now()
            _er_open = str(p.get("reason", "") or "")
            rows.append(
                {
                    "Code": code,
                    "Action": "BUY",
                    "Price": ap,
                    "Qty": iq,
                    "Fee": 0.0,
                    "Sig_Type": sig_type,
                    "Reason": _tag_reason_for_virtual_smart_buy_stats(
                        _er_open, "BUY", sig_type, settings
                    ),
                    "Return": 0.0,
                    "Date": d_entry,
                }
            )
            if int(p.get("stage") or 1) >= 2:
                rows.append(
                    {
                        "Code": code,
                        "Action": "BUY2",
                        "Price": ap,
                        "Qty": 0,
                        "Fee": 0.0,
                        "Sig_Type": sig_type,
                        "Reason": _tag_reason_for_virtual_smart_buy_stats(
                            _er_open, "BUY2", sig_type, settings
                        ),
                        "Return": 0.0,
                        "Date": d_entry,
                    }
                )
            # 미청산: 리포트 생성 시점 현재가(또는 평단)로 가상 청산 행을 넣어 통계·라운드트립 집계에 반영
            try:
                mark_px = float(p.get("current_price") or 0)
            except (TypeError, ValueError):
                mark_px = 0.0
            if mark_px <= 0:
                mark_px = ap
            try:
                ret_pct = ((mark_px / ap) - 1.0) * 100.0 if ap > 0 else 0.0
            except Exception:
                ret_pct = 0.0
            d_mtm = pd.Timestamp.now()
            rows.append(
                {
                    "Code": code,
                    "Action": "SELL",
                    "Price": mark_px,
                    "Qty": iq,
                    "Fee": 0.0,
                    "Sig_Type": sig_type,
                    "Reason": "MTM_REPORT",
                    "Return": float(ret_pct),
                    "Date": d_mtm,
                }
            )
    except Exception:
        return pd.DataFrame()

    if not rows:
        return pd.DataFrame()
    try:
        out = pd.DataFrame(rows)
        out["Date"] = pd.to_datetime(out["Date"])
        out.sort_values(["Code", "Date"], inplace=True)
        out.reset_index(drop=True, inplace=True)
        return out
    except Exception:
        return pd.DataFrame()


def collect_session_jsonl_days_for_virtual_history(
    history: Optional[List[Dict[str, Any]]],
    open_positions: Optional[List[Dict[str, Any]]],
) -> List[str]:
    """
    가상 청산 이력·보유의 진입·청산 일자 최소~최대 구간에서,
    `realtime_session_logs/YYYY-MM-DD/session_events.jsonl` 파일이 실제로 있는 날만 나열합니다.
    이력과 겹치는 날만 스캔해 불필요한 파일 접근을 줄입니다.
    """
    from datetime import date as date_cls

    def _parse_dt(v: Any) -> Optional[pd.Timestamp]:
        if v is None or (isinstance(v, str) and not str(v).strip()):
            return None
        try:
            s = str(v).strip().replace("T", " ")
            if len(s) >= 19:
                return pd.to_datetime(s[:19])
            return pd.to_datetime(s)
        except Exception:
            try:
                return pd.to_datetime(v)
            except Exception:
                return None

    ts_list: List[pd.Timestamp] = []
    try:
        for coll in (history or [], open_positions or []):
            for item in coll:
                if not isinstance(item, dict):
                    continue
                for key in ("entry_time", "exit_datetime", "exit_date"):
                    t = _parse_dt(item.get(key))
                    if t is not None:
                        ts_list.append(t)
                ed = str(item.get("exit_date") or "").strip()
                ex = str(item.get("exit_time") or "").strip()
                if ed and len(ed) >= 10 and ed[4] == "-":
                    t = _parse_dt(f"{ed[:10]} {ex}".strip())
                    if t is not None:
                        ts_list.append(t)
    except Exception:
        pass

    if not ts_list:
        try:
            t0 = pd.Timestamp.now().normalize()
            d_min = d_max = t0.date()
        except Exception:
            d_min = d_max = date_cls.today()
    else:
        try:
            d_min = min(ts_list).date()
            d_max = max(ts_list).date()
        except Exception:
            try:
                t0 = pd.Timestamp.now().normalize()
                d_min = d_max = t0.date()
            except Exception:
                d_min = d_max = date_cls.today()

    out: List[str] = []
    try:
        from reporting.realtime_session_logger import RealtimeSessionLogger

        lg = RealtimeSessionLogger()
        cur = d_min
        while cur <= d_max:
            ds = cur.isoformat()
            try:
                if os.path.isfile(lg.jsonl_path(ds)):
                    out.append(ds)
            except Exception:
                pass
            cur = cur + timedelta(days=1)
    except Exception:
        pass
    return out


def count_session_kind_events(kind: str, days: Iterable[str]) -> int:
    """
    지정 일자들의 세션 JSONL에서 최상위 `kind` 필드가 일치하는 이벤트 건수를 합산합니다.
    """
    try:
        from reporting.realtime_session_logger import RealtimeSessionLogger
    except Exception:
        return 0

    k = str(kind or "").strip()
    if not k:
        return 0
    total = 0
    try:
        lg = RealtimeSessionLogger()
    except Exception:
        return 0
    for day in days:
        try:
            for ev in lg.read_events(day):
                if str(ev.get("kind", "")).strip() == k:
                    total += 1
        except Exception:
            continue
    return total


def build_virtual_portfolio_analysis_html(
    df: pd.DataFrame,
    *,
    open_holdings_count: int = 0,
    session_smart_buy_blocked: int = 0,
) -> str:
    """
    가상 체결 DataFrame 기준 「백테스트 분석 통계」 HTML을 생성합니다.

    - Type A/B × Stage S1/S2 네 칸 집표(완료 라운드·실현·매수 행·스마트 통과)와
      전역·세션 요약(청산 행·미청산·스마트 시도/거절 등)을 분리합니다.
    - REJECTED_SIG·시장 레짐 등은 가상 DF에 없으면 0 또는 안내로 둡니다.
    - 스마트 진입 거절(수급)은 `session_smart_buy_blocked`로 세션 로그 집계를 넘길 수 있습니다.
    """
    if df is None or df.empty:
        return (
            '<div class="vr-analysis-wrap"><p class="depth-subnote" style="color:#94a3b8;">'
            "체결 로그가 없어 포트폴리오 백테스트형 통계를 계산할 수 없습니다."
            "</p></div>"
        )

    try:
        df = df.copy()
        df["Date"] = pd.to_datetime(df["Date"])
    except Exception:
        return (
            '<div class="vr-analysis-wrap"><p class="depth-subnote" style="color:#94a3b8;">'
            "날짜 파싱 실패로 통계를 생략합니다."
            "</p></div>"
        )

    try:
        _sb_on_buy = _reason_matches_smart_buy(df["Reason"])
        count_smart_buy = int(
            ((df["Action"].astype(str).str.startswith("BUY", na=False)) & _sb_on_buy).sum()
        )
        _sx_on_sell = _reason_matches_smart_exit(df["Reason"])
        count_smart_exit = int(
            (
                (df["Action"].astype(str).str.startswith("SELL", na=False)) & _sx_on_sell
            ).sum()
        )
        count_rejected = 0
        count_smart_buy_rejected = max(0, int(session_smart_buy_blocked or 0))
        total_smart_buy_events = count_smart_buy + count_smart_buy_rejected
        pass_rate = (
            (count_smart_buy / max(1, total_smart_buy_events)) * 100
            if total_smart_buy_events > 0
            else 0.0
        )
        hold_count = max(
            int(open_holdings_count or 0),
            len(df[df["Action"].astype(str) == "HOLD"]["Code"].unique()),
        )
    except Exception:
        count_smart_buy = count_smart_exit = 0
        count_rejected = 0
        count_smart_buy_rejected = max(0, int(session_smart_buy_blocked or 0))
        total_smart_buy_events = count_smart_buy + count_smart_buy_rejected
        pass_rate = (
            (count_smart_buy / max(1, total_smart_buy_events)) * 100
            if total_smart_buy_events > 0
            else 0.0
        )
        hold_count = int(open_holdings_count or 0)

    a_s1_p, a_s1_i = [], []
    a_s2_p, a_s2_i = [], []
    b_s1_p, b_s1_i = [], []
    b_s2_p, b_s2_i = [], []
    total_s1_p, total_s1_i = [], []
    total_s2_p, total_s2_i = [], []

    try:
        for c in df["Code"].unique():
            s_df = df[df["Code"] == c]
            temp_inv = 0.0
            temp_has_s2 = False
            temp_sig = "NONE"

            for _, r in s_df.iterrows():
                action = str(r["Action"])
                price = float(r["Price"])
                qty = float(r["Qty"])
                fee = (
                    float(r.get("Fee", 0))
                    if not pd.isna(r.get("Fee", 0))
                    else 0.0
                )

                if "BUY" in action:
                    if any(x in action for x in ["BUY2", "BUY_2ND", "ADDON", "S2"]):
                        temp_has_s2 = True
                    else:
                        temp_sig = str(r.get("Sig_Type", "NONE"))
                    temp_inv += (price * qty) + fee

                elif action in ["SELL", "HOLD"]:
                    profit = (price * qty) - fee - temp_inv

                    if temp_has_s2:
                        if "TYPE_A" in temp_sig:
                            a_s2_p.append(profit)
                            a_s2_i.append(temp_inv)
                        elif "TYPE_B" in temp_sig:
                            b_s2_p.append(profit)
                            b_s2_i.append(temp_inv)
                        total_s2_p.append(profit)
                        total_s2_i.append(temp_inv)
                    else:
                        if "TYPE_A" in temp_sig:
                            a_s1_p.append(profit)
                            a_s1_i.append(temp_inv)
                        elif "TYPE_B" in temp_sig:
                            b_s1_p.append(profit)
                            b_s1_i.append(temp_inv)
                        total_s1_p.append(profit)
                        total_s1_i.append(temp_inv)

                    temp_inv = 0.0
                    temp_has_s2 = False
                    temp_sig = "NONE"
    except Exception:
        pass

    def calc_yield(profits: list, invested: list) -> float:
        if not invested or sum(invested) == 0:
            return 0.0
        return (sum(profits) / sum(invested)) * 100

    y_a1, s_a1 = calc_yield(a_s1_p, a_s1_i), sum(a_s1_p)
    y_a2, s_a2 = calc_yield(a_s2_p, a_s2_i), sum(a_s2_p)
    y_b1, s_b1 = calc_yield(b_s1_p, b_s1_i), sum(b_s1_p)
    y_b2, s_b2 = calc_yield(b_s2_p, b_s2_i), sum(b_s2_p)
    y_s1, s_s1 = calc_yield(total_s1_p, total_s1_i), sum(total_s1_p)
    y_s2, s_s2 = calc_yield(total_s2_p, total_s2_i), sum(total_s2_p)

    count_fills = len(df[df["Action"].isin(["SELL", "HOLD"])])
    completed_rounds = len(total_s1_p) + len(total_s2_p)

    try:
        depth_fragment = _build_depth_analytics_html(df)
    except Exception:
        depth_fragment = ""

    regime_fragment = (
        '<p class="depth-subnote" style="margin-top:8px;">실시간 가상 매매 리포트입니다. '
        "시장 레짐별 실현 통계는 포트폴리오 백테스트 리포트 "
        "(<code>dynamic_trade_report.html</code>)를 참고하세요.</p>"
    )

    ca1, ca2 = len(a_s1_p), len(a_s2_p)
    cb1, cb2 = len(b_s1_p), len(b_s2_p)
    c_all_s1, c_all_s2 = len(total_s1_p), len(total_s2_p)

    y_all_four = calc_yield(
        a_s1_p + a_s2_p + b_s1_p + b_s2_p,
        a_s1_i + a_s2_i + b_s1_i + b_s2_i,
    )
    profit_all_four = float(s_a1 + s_a2 + s_b1 + s_b2)

    _BK = ("AS1", "AS2", "BS1", "BS2")
    n_buy_by: Dict[str, int] = {k: 0 for k in _BK}
    n_sb_by: Dict[str, int] = {k: 0 for k in _BK}
    n_buy_unclassified = 0
    try:
        for _, row in df.iterrows():
            act = str(row.get("Action", ""))
            au = act.upper()
            if not (au.startswith("BUY") or au == "ADDON"):
                continue
            bk = _bucket_virtual_buy_row(
                act, row.get("Sig_Type"), row.get("Reason")
            )
            if not bk:
                n_buy_unclassified += 1
                continue
            n_buy_by[bk] = int(n_buy_by.get(bk, 0)) + 1
            if _reason_str_has_smart_buy(row.get("Reason")):
                n_sb_by[bk] = int(n_sb_by.get(bk, 0)) + 1
    except Exception:
        pass

    def _vr_td_num(v: int) -> str:
        return f'<td class="num">{int(v)}</td>'

    def _vr_td_money(v: float) -> str:
        cl = "gain" if v >= 0 else "loss"
        return f'<td class="num {cl}">{int(v):,}원</td>'

    def _vr_td_pct(v: float) -> str:
        cl = "gain" if v >= 0 else "loss"
        return f'<td class="num {cl}">{v:+.2f}%</td>'

    _pc_all = "gain" if profit_all_four >= 0 else "loss"
    row_rounds = (
        "<tr><td><b>완료 라운드 수</b></td>"
        f"{_vr_td_num(ca1)}{_vr_td_num(ca2)}{_vr_td_num(cb1)}{_vr_td_num(cb2)}"
        f'<td class="num"><b>{completed_rounds}</b></td></tr>'
    )
    row_profit = (
        "<tr><td><b>총 실현손익(원)</b></td>"
        f"{_vr_td_money(s_a1)}{_vr_td_money(s_a2)}{_vr_td_money(s_b1)}{_vr_td_money(s_b2)}"
        f'<td class="num {_pc_all}"><b>{int(profit_all_four):,}원</b></td></tr>'
    )
    row_yield = (
        "<tr><td><b>가중 수익률(%)</b></td>"
        f"{_vr_td_pct(y_a1)}{_vr_td_pct(y_a2)}{_vr_td_pct(y_b1)}{_vr_td_pct(y_b2)}"
        f"{_vr_td_pct(y_all_four)}</tr>"
    )
    row_sb = (
        "<tr><td>스마트 진입 통과(매수 행)</td>"
        f"{_vr_td_num(n_sb_by.get('AS1', 0))}{_vr_td_num(n_sb_by.get('AS2', 0))}"
        f"{_vr_td_num(n_sb_by.get('BS1', 0))}{_vr_td_num(n_sb_by.get('BS2', 0))}"
        f'<td class="num"><b>{count_smart_buy}</b></td></tr>'
    )
    row_nbuy = (
        "<tr><td>매수 행 수(BUY·ADDON)</td>"
        f"{_vr_td_num(n_buy_by.get('AS1', 0))}{_vr_td_num(n_buy_by.get('AS2', 0))}"
        f"{_vr_td_num(n_buy_by.get('BS1', 0))}{_vr_td_num(n_buy_by.get('BS2', 0))}"
        f'<td class="num"><b>{sum(n_buy_by.values())}</b></td></tr>'
    )
    unclassified_note = ""
    if n_buy_unclassified > 0:
        unclassified_note = (
            f'<p class="depth-subnote" style="margin-top:8px;">'
            f"TYPE_A/B로 분류되지 않은 매수 행 <b>{n_buy_unclassified}</b>건은 "
            f"네 칸 합계(매수 행·스마트 진입 통과)에서 제외했습니다."
            f"</p>"
        )

    analysis_stats_html = f"""
    <div id="analysis-stats" class="analysis-report-section depth-section">
        <h2 class="analysis-main-title">📋 백테스트 분석 통계 (가상 체결 기준)</h2>
        <p class="analysis-main-lead">
            <b>Type A/B × Stage S1/S2</b> 네 칸 기준으로 완료 라운드·실현손익·매수 행·스마트 진입 통과(매수 행)를 한 표에 모았습니다.
            스마트 진입 &quot;통과&quot;는 Reason 패턴·설정 기반 추정입니다.
            스마트 진입 거절(수급)은 세션 <code>smart_buy_blocked</code>를 가상 이력 일자 구간에서 합산합니다.
            REJECTED_SIG·시장 레짐 등은 가상 DF에 없으면 0 또는 안내로 표시됩니다.
        </p>

        <h3 class="analysis-h3">1. Type×Stage·실행·실현</h3>
        <p class="depth-subnote" style="margin-bottom:10px;">
            열: 첫 진입 Type A/B × Stage 1(첫 매수만) / Stage 2(추매 포함).
            합계 열: 건수·원화·매수 행·스마트 통과는 네 칸 합과 일치합니다.
            가중 수익률 합계 열은 네 칸을 합산한 금액가중 평균입니다(단순 네 비율의 산술합 아님).
        </p>
        <table class="depth-table" style="min-width:720px;">
            <thead>
                <tr>
                    <th>항목</th>
                    <th class="num">Type A·S1</th>
                    <th class="num">Type A·S2</th>
                    <th class="num">Type B·S1</th>
                    <th class="num">Type B·S2</th>
                    <th class="num">합계</th>
                </tr>
            </thead>
            <tbody>
                {row_rounds}
                {row_profit}
                {row_yield}
                {row_sb}
                {row_nbuy}
            </tbody>
        </table>
        {unclassified_note}

        <h3 class="analysis-h3">2. 전역·세션 요약</h3>
        <table class="depth-table">
            <thead><tr><th>항목</th><th class="num">값</th><th>비고</th></tr></thead>
            <tbody>
                <tr><td>청산 행 (SELL/HOLD)</td><td class="num">{count_fills}건</td><td style="color:var(--dim);font-size:0.82rem;">로그 상 청산·평가 행 수</td></tr>
                <tr><td>완료 라운드트립 (총)</td><td class="num">{completed_rounds}건</td><td style="color:var(--dim);font-size:0.82rem;">위 표 합계 열과 동일</td></tr>
                <tr><td>미청산 보유 종목 수</td><td class="num">{hold_count}종목</td><td style="color:var(--dim);font-size:0.82rem;">리포트 생성 시점 기준</td></tr>
                <tr><td>스마트 진입 통과 / 시도</td><td class="num">{count_smart_buy} / {total_smart_buy_events}건</td><td style="color:var(--dim);font-size:0.82rem;">통과율 {pass_rate:.1f}% · 시도=통과+세션 거절</td></tr>
                <tr><td>스마트 진입 거절 (수급)</td><td class="num">{count_smart_buy_rejected}건</td><td style="color:var(--dim);font-size:0.82rem;">세션 <code>smart_buy_blocked</code>·네 칸 미분해</td></tr>
                <tr><td>신호 거절 전체 (REJECTED_SIG)</td><td class="num">{count_rejected}건</td><td style="color:var(--dim);font-size:0.82rem;">가상 DF에 없으면 0</td></tr>
                <tr><td>스마트 청산 (SELL·스마트 패턴)</td><td class="num">{count_smart_exit}건</td><td style="color:var(--dim);font-size:0.82rem;">Smart Exit 변형 포함·네 칸 미분해</td></tr>
            </tbody>
        </table>

        <h3 class="analysis-h3">3. 시장 레짐별 실현</h3>
        <div class="analysis-subblock">{regime_fragment}</div>

        <h3 class="analysis-h3">4. 완료 라운드트립 심층</h3>
        <p class="depth-lead" style="margin-bottom:16px;">
            매수 체인(BUY*) 이후 <b>SELL 또는 HOLD</b>로 닫힌 거래만 집계합니다.
        </p>
        <div class="analysis-subblock">{depth_fragment if depth_fragment else '<p class="depth-subnote">완료 라운드가 없거나 심층 분석을 생성할 수 없습니다.</p>'}</div>
    </div>
    """
    return f'<div class="vr-analysis-wrap">{analysis_stats_html}</div>'


def generate_trade_report(df, interval, output_path="trade_report.html", total_trades=0, win_rate=0, avg_ret=0, best_settings=None, callback=None, target_codes=None, start_date=None, end_date=None, daily_equity=None, high_precision=False):
    """매매 실행 내역 HTML 리포트 생성 (v8.1) - 고정밀 모드 표시 지원"""
    # [v8.2] 표시용 인터벌 조정
    display_interval = "1m" if high_precision else interval
    # [v8.1] 모드 텍스트 결정
    mode_label = "고정밀(1분봉)" if high_precision else f"일반({display_interval})"
    mode_color = "#38bdf8" if high_precision else "#94a3b8"

    # [v37.2] Date 데이터 시계열 변환 강제 (인트라데이 매칭 오류 방지)
    if not df.empty and 'Date' in df.columns:
        df['Date'] = pd.to_datetime(df['Date'])

    # [v15.4.0] 종목 필터를 먼저 적용 (통계·표시 동일 유니버스)
    if target_codes is not None and not df.empty:
        codes_set = set([str(c).strip().zfill(6) for c in target_codes])
        df = df[df['Code'].astype(str).str.strip().str.zfill(6).isin(codes_set)].copy()
        if df.empty:
            try:
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(f"<html><body style='background:#0f172a; color:white; padding:50px; text-align:center;'><h1>매매 내역 없음</h1><p>지정된 종목군에 대한 체결 내역이 없습니다.</p></body></html>")
            except Exception:
                pass
            return

    # [v38.0] REJECTED_SIG 등 포함 원본 — Smart Buy 거절·통과율 집계용 (표시용 df와 분리)
    df_stats = df.copy() if not df.empty else df

    # [v37.1] 가독성: 표시는 체결·보유 행만
    if not df.empty:
        mask = df['Action'].astype(str).str.contains('BUY|SELL|HOLD', case=False, na=False)
        df = df[mask].copy()

    if df.empty:
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(f"<html><body style='background:#0f172a; color:white; padding:50px; text-align:center;'><h1>매매 성공 내역 없음</h1><p>필터링 결과 실제 체결된 내역이 없습니다.</p></body></html>")
        except Exception:
            pass
        return
    
    avg_cash_ratio = 0 # [v32.1] Initialize for template
    
    # [v4.5] UI-Consistent Hierarchical Parameter Mapping (6-Section Sync)
    PARAM_STRUCTURE = [
        {
            "name": "💰 [1] 자금 및 계좌 관리 (Account & Fund)",
            "items": [
                {"label": "백테스팅용 초기자금", "key": "backtest_initial_capital", "unit": "원", "is_amt": True},
                {"label": "최대 감시 종목 수 (Track A)", "key": "max_slots_a", "unit": "개"},
                {"label": "최대 감시 종목 수 (Track B)", "key": "max_slots_b", "unit": "개"},
                {"label": "1단계(정찰) 비중", "key": "s1_equity_pct", "unit": "%"},
                {"label": "2단계(본대) 비중", "key": "s2_equity_pct", "unit": "%"},
            ]
        },
        {
            "name": "🔍 [2] 종목 선정 필수 필터 (Stock Selection)",
            "items": [
                {"label": "최소 거래대금", "key": "min_trde_val", "unit": "억"},
                {"label": "거래량 분출 비율", "key": "vol_ratio", "unit": "배"},
                {"label": "분출 확인 기간", "key": "vol_lookback", "unit": "일"},
                {"label": "종목 선정 RSI 범위", "keys": ["sel_rsi_min", "sel_rsi_max"], "sep": "~"},
                {"label": "일봉 MACD 필터", "keys": ["macd_fast_daily", "macd_slow_daily", "macd_sig_daily"], "sep": "/"},
                {"label": "이평선 수렴도 기준", "key": "ma_convergence", "unit": "%"},
            ]
        },
        {
            "name": "🎯 [3] S1 공통 필수 + 15m 보조 필터",
            "items": [
                {"label": "S1 진입 RSI 하한 (A/B 공통)", "toggle": "use_rsi_min", "key": "buy_rsi_min"},
                {"label": "15분봉 MACD 필터 (12/26/9 고정)", "toggle": "use_macd_signal"},
                {"label": "RSI 시그널 필터 (RSI14·시그널9 고정)", "toggle": "use_rsi_sig_filter"},
            ]
        },
        {
            "name": "🚀 [4] 1단계 진입 전략 (Stage 1 Master)",
            "items": [
                {"label": "Type A (눌림목) 활성화", "toggle": "use_type_a", "key": "buy_rsi_max_a", "unit": " RSI"},
                {"label": "ㄴ 거래량 급증 확인(A)", "toggle": "use_vol_surge", "key": "vol_surge", "unit": "배"},
                {"label": "ㄴ 전일동시간 평균(S1·A)", "key": "prev_vol_lookback_s1_a", "unit": "일"},
                {"label": "ㄴ MA60 지지(상/하)", "keys": ["ma60_min", "ma60_max"], "unit": "%", "sep": "~"},
                {"label": "ㄴ 이평 골든크로스(분)", "toggle": "use_gc", "key": "gc_window", "unit": "분"},
                {"label": "ㄴ RSI 50 돌파 컨펌(분)", "toggle": "use_rsi_signal", "key": "rsi_window", "unit": "분"},
                {"label": "Type B (돌파 타점) 활성화", "toggle": "use_type_b", "key": "buy_rsi_max_b", "unit": " RSI"},
                {"label": "ㄴ 전일동시간 평균(S1·B)", "key": "prev_vol_lookback_s1_b", "unit": "일"},
                {"label": "ㄴ Smart Buy (Type B)", "toggle": "use_smart_buy_b"},
                {"label": "ㄴ MACD 골든크로스(B)", "toggle": "use_macd_gc_b"},
                {"label": "ㄴ 정배열 추세 확인(B)", "toggle": "use_trend_aligned_b"},
                {"label": "ㄴ 돌파 시 거래량 분출", "toggle": "use_type_b_vol", "key": "vol_surge_b", "unit": "배"},
            ]
        },
        {
            "name": "📈 [5] 2단계 추매 (Type A: s2_* / Type B: s2_b_*)",
            "items": [
                {"label": "A) 추매(S2) 전체", "toggle": "use_s2_master_a"},
                {"label": "A) 20일선 지지", "toggle": "use_s2_ma20", "key": "s2_ma20_gap", "unit": "%"},
                {"label": "A) 5일선 지지", "toggle": "use_s2_ma5", "key": "s2_ma5_gap", "unit": "%"},
                {"label": "A) Smart Buy S2", "toggle": "use_smart_buy_s2"},
                {"label": "A) 거래량 급증", "toggle": "use_s2_vol_surge", "key": "s2_vol_ratio", "unit": "배"},
                {"label": "A) 전일동시간 기간(S2·A)", "key": "prev_vol_lookback_s2_a", "unit": "일"},
                {"label": "A) RSI 모멘텀", "toggle": "use_s2_rsi_sig", "key": "s2_rsi_gap", "unit": "%"},
                {"label": "A) MACD 모멘텀", "toggle": "use_s2_macd_gc", "key": "s2_macd_gap", "unit": "%"},
                {"label": "A) 수익권 필터", "toggle": "use_s2_profit", "key": "s2_profit_limit", "unit": "%"},
                {"label": "A) 대기 시간", "toggle": "use_s2_delay", "key": "s2_delay_hours", "unit": "시간"},
                {"label": "B) 추매(S2) 전체", "toggle": "use_s2_master_b"},
                {"label": "B) 20일선 지지", "toggle": "use_s2_b_ma20", "key": "s2_b_ma20_gap", "unit": "%"},
                {"label": "B) 5일선 지지", "toggle": "use_s2_b_ma5", "key": "s2_b_ma5_gap", "unit": "%"},
                {"label": "B) Smart Buy S2", "toggle": "use_smart_buy_s2_b"},
                {"label": "B) 거래량 급증", "toggle": "use_s2_b_vol_surge", "key": "s2_b_vol_ratio", "unit": "배"},
                {"label": "B) 전일동시간 기간(S2·B)", "key": "prev_vol_lookback_s2_b", "unit": "일"},
                {"label": "B) RSI 모멘텀", "toggle": "use_s2_b_rsi_sig", "key": "s2_b_rsi_gap", "unit": "%"},
                {"label": "B) MACD 모멘텀", "toggle": "use_s2_b_macd_gc", "key": "s2_b_macd_gap", "unit": "%"},
                {"label": "B) 수익권 필터", "toggle": "use_s2_b_profit", "key": "s2_b_profit_limit", "unit": "%"},
                {"label": "B) 대기 시간", "toggle": "use_s2_b_delay", "key": "s2_b_delay_hours", "unit": "시간"},
            ]
        },
        {
            "name": "💰 [6] Smart Buy 수급 (VP/BA — S1·S2 공통)",
            "items": [
                {"label": "최소 체결강도 (VP)", "key": "smart_buy_vp", "unit": "%"},
                {"label": "진입용 매도벽 비율 (BA)", "key": "smart_buy_ba_ratio", "unit": "배"}
            ]
        },
        {
            "name": "🛡️ [7] Smart Exit (A/B 공통)",
            "items": [
                {"label": "Smart Exit (VP / 매도벽=매도÷매수)", "toggle": "use_smart_exit", "keys": ["smart_exit_vp", "smart_exit_ba_ratio"], "sep": " / "},
                {"label": "ㄴ 가격 하락 임계", "key": "smart_exit_price_threshold", "unit": "%"},
            ]
        },
        {
            "name": "🛡️ [7-A] Type A 청산 · 리스크",
            "items": [
                {"label": "갭하락 즉시 손절", "toggle": "use_gap_down_exit_a", "key": "gap_down_limit_a", "unit": "%"},
                {"label": "개장 초기 매도 유예", "toggle": "use_gap_grace_a", "key": "gap_grace_mins_a", "unit": "분"},
                {"label": "ATR 주기·활성", "toggle": "use_atr_exit_a", "key": "atr_period_a", "unit": "봉"},
                {"label": "S1(A) 익절/손절", "toggle": "use_s1_a_tp", "keys": ["s1_a_take_profit", "s1_a_stop_loss"], "unit": "%", "sep": "TP / SL: "},
                {"label": "S1(A) Flash", "toggle": "use_s1_a_flash_exit", "key": "s1_a_flash_exit_pct", "unit": "%"},
                {"label": "S1(A) ATR 손절(SL)", "toggle": "use_atr_stop_loss_a", "key": "atr_multiplier_sl_a", "unit": "배"},
                {"label": "S1(A) ATR 추격(Trl)", "toggle": "use_atr_trailing_stop_a", "key": "atr_multiplier_trail_a", "unit": "배"},
                {"label": "ATR 트레일: 익절% 이후만 (A-S1)", "toggle": "use_atr_trail_only_after_tp_s1_a"},
                {"label": "S2(A) 익절/손절/Flash", "toggle": "use_s2_tp", "keys": ["s2_take_profit", "s2_stop_loss", "s2_flash_exit_pct"], "unit": "%", "sep": " / "},
                {"label": "S2(A) ATR SL / Trl", "toggle": "use_atr_stop_loss_s2", "keys": ["atr_multiplier_sl_s2", "atr_multiplier_trail_s2"], "unit": "배", "sep": " / "},
                {"label": "ATR 트레일: 익절% 이후만 (A-S2)", "toggle": "use_atr_trail_only_after_tp_s2_a"},
            ]
        },
        {
            "name": "🛡️ [7-B] Type B 청산 · 리스크",
            "items": [
                {"label": "갭하락 즉시 손절", "toggle": "use_gap_down_exit_b", "key": "gap_down_limit_b", "unit": "%"},
                {"label": "개장 초기 매도 유예", "toggle": "use_gap_grace_b", "key": "gap_grace_mins_b", "unit": "분"},
                {"label": "ATR 주기·활성", "toggle": "use_atr_exit_b", "key": "atr_period_b", "unit": "봉"},
                {"label": "S1(B) 익절/손절", "toggle": "use_s1_b_tp", "keys": ["s1_b_take_profit", "s1_b_stop_loss"], "unit": "%", "sep": "TP / SL: "},
                {"label": "S1(B) Flash", "toggle": "use_s1_b_flash_exit", "key": "s1_b_flash_exit_pct", "unit": "%"},
                {"label": "S1(B) ATR 손절(SL)", "toggle": "use_atr_stop_loss_b", "key": "atr_multiplier_sl_b", "unit": "배"},
                {"label": "S1(B) ATR 추격(Trl)", "toggle": "use_atr_trailing_stop_b", "key": "atr_multiplier_trail_b", "unit": "배"},
                {"label": "ATR 트레일: 익절% 이후만 (B-S1)", "toggle": "use_atr_trail_only_after_tp_s1_b"},
                {"label": "S2(B) 익절/손절/Flash", "toggle": "use_s2_b_tp", "keys": ["s2_b_take_profit", "s2_b_stop_loss", "s2_b_flash_exit_pct"], "unit": "%", "sep": " / "},
                {"label": "S2(B) ATR SL / Trl", "toggle": "use_atr_stop_loss_s2_b", "keys": ["atr_multiplier_sl_s2_b", "atr_multiplier_trail_s2_b"], "unit": "배", "sep": " / "},
                {"label": "ATR 트레일: 익절% 이후만 (B-S2)", "toggle": "use_atr_trail_only_after_tp_s2_b"},
            ]
        }
    ]

    # [v16.1] Efficient Formatter
    def format_val(v):
        if isinstance(v, (int, float, np.integer, np.floating)):
            if float(v) > 1000000: # 금액 표시 (KRW)
                return f"{int(v):,}"
            if float(v).is_integer(): return str(int(v))
            return f"{float(v):.1f}"
        return str(v)

    # 파라미터 섹션 생성
    param_html = ""
    if best_settings:
        group_htmls = []
        assigned_keys = set()
        
        for group in PARAM_STRUCTURE:
            items_html = []
            for item in group["items"]:
                label = item["label"]
                toggle_key = item.get("toggle")
                val_key = item.get("key")
                val_keys = item.get("keys")
                unit = item.get("unit", "")
                sep = item.get("sep", ", ")
                mult = item.get("multiply", 1)
                
                # 1. 상태 정보 (ON/OFF)
                status_html = ""
                is_on = True
                if toggle_key:
                    assigned_keys.add(toggle_key)
                    val = best_settings.get(toggle_key, False)
                    is_on = val
                    status_class = "val-on" if val else "val-off"
                    status_text = "ON" if val else "OFF"
                    # [v17.0] ON/OFF 배지만 있으면 충분, 값은 괄호로 묶어 통합 표시
                    status_html = f'<span class="p-val {status_class}">{status_text}</span>'
                
                # 2. 값 정보 (ON일 때만 또는 키가 지정된 경우 표시)
                value_html = ""
                v_list = []
                # [v33.0] unit_key가 있으면 해당 단위에 맞는 키만 선택
                unit_key = item.get("unit_key")
                if unit_key and unit_key in best_settings:
                    current_unit = best_settings[unit_key]
                    mapped_unit = current_unit # "원", "%" 등
                    if val_keys and len(val_keys) >= 2:
                        target_key = val_keys[0] if current_unit == "원" else val_keys[1]
                        assigned_keys.add(target_key)
                        v_list.append(format_val(best_settings.get(target_key, 0)))
                        unit = mapped_unit
                elif val_key:
                    assigned_keys.add(val_key)
                    if val_key in best_settings:
                        v_list.append(format_val(best_settings[val_key] * mult))
                elif val_keys:
                    for k in val_keys:
                        assigned_keys.add(k)
                        if k in best_settings:
                            v_list.append(format_val(best_settings[k] * mult))
                
                if v_list:
                    val_str = sep.join(v_list)
                    # OFF인 경우에는 괄호 내용을 숨기거나 희미하게 처리 (선택사항: 여기서는 ON일 때만 표시)
                    if is_on:
                        value_html = f'<span class="p-val-sub">({val_str}{unit})</span>'
                    else:
                        value_html = "" # OFF이면 값 숨김 (요청사항 반영)
                
                if toggle_key or v_list: # 키가 정의된 항목만 표시
                    items_html.append(f"""
                    <div class="p-item">
                        <span class="p-key">{label}</span>
                        <div class="p-val-group">
                            {status_html}
                            {value_html}
                        </div>
                    </div>
                    """)
            
            if items_html:
                group_htmls.append(f"""
                <details class="p-group">
                    <summary class="p-group-title">{group['name']}</summary>
                    <div class="p-grid">{"".join(items_html)}</div>
                </details>
                """)
        
        # [v16.1] 정의되지 않은 나머지 파라미터들 (기타 설정)
        others = []
        for k, v in best_settings.items():
            if k not in assigned_keys and k not in ['bt_start_date', 'bt_end_date']:
                if isinstance(v, bool):
                    val_class = "val-on" if v else "val-off"
                    val_text = "ON" if v else "OFF"
                    others.append(f'<div class="p-item"><span class="p-key">{k}</span><span class="p-val {val_class}">{val_text}</span></div>')
                else:
                    others.append(f'<div class="p-item"><span class="p-key">{k}</span><span class="p-val">{format_val(v)}</span></div>')
        
        if others:
            group_htmls.append(f"""
            <details class="p-group">
                <summary class="p-group-title">⚙️ 기타 설정 (General)</summary>
                <div class="p-grid">{"".join(others)}</div>
            </details>
            """)
            
        param_html = f'<div class="p-container">{"".join(group_htmls)}</div>'

    html_template = """
    <!DOCTYPE html>
    <html lang="ko">
    <head>
        <meta charset="UTF-8"><title>Backtest Analysis Report</title>
        <link href="https://fonts.googleapis.com/css2?family=Pretendard:wght@400;600;700&display=swap" rel="stylesheet">
        {plotly_script}
        <style>
            :root {{ 
                --bg: #0f172a; --card: #1e293b; --text: #f8fafc; --dim: #94a3b8; 
                --buy: #38bdf8; --sell: #fbbf24; --gain: #4ade80; --loss: #f87171; 
                --primary: #3b82f6; --sidebar-w: 280px;
            }}
            body {{ font-family: 'Pretendard', sans-serif; background: var(--bg); color: var(--text); margin: 0; min-height: 100vh; overflow-x: hidden; }}
            .sidebar {{ width: var(--sidebar-w); background: #0a0f1d; border-right: 1px solid #1e293b; display: flex; flex-direction: column; position: fixed; left: 0; top: 0; bottom: 0; z-index: 1000; overflow-y: auto; }}
            .sidebar-header {{ padding: 25px 20px; border-bottom: 2px solid #1e293b; background: rgba(59,130,246,0.05); }}
            .sidebar-title {{ font-size: 1.2rem; font-weight: 800; color: var(--primary); display: flex; align-items: center; gap: 10px; text-transform: uppercase; letter-spacing: 1px; }}
            .nav-list {{ list-style: none; padding: 15px; margin: 0; overflow-y: auto; flex: 1; }}
            .nav-item {{ margin-bottom: 6px; }}
            .nav-item a {{ display: flex; justify-content: space-between; padding: 12px 15px; color: var(--dim); text-decoration: none; border-radius: 10px; font-size: 0.9rem; transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1); border: 1px solid transparent; }}
            .nav-item a:hover {{ background: #1e293b; color: white; border-color: #334155; transform: translateX(5px); }}
            .nav-item .code-badge {{ background: #334155; color: #cbd5e1; font-size: 0.7rem; padding: 2px 6px; border-radius: 4px; font-weight: 600; }}
            
            .main-content {{ margin-left: calc(var(--sidebar-w) + 30px); padding: 40px; background: radial-gradient(circle at top right, #1e293b11, transparent); min-height: 100vh; display: block; overflow-x: visible; }}
            .container {{ width: 100%; max-width: 1400px; margin: 0; display: block; }}
            .header-flex {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 35px; border-bottom: 1px solid #1e293b; padding-bottom: 20px; width: 100%; }}
            
            /* 파라미터 그룹 스타일 [v15.6] */
            .p-container {{ margin-top: 40px; margin-bottom: 50px; display: flex; flex-direction: column; gap: 20px; clear: both; width: 100%; }}
            .p-group {{ background: #1e293b; border: 1px solid #334155; border-radius: 16px; padding: 15px 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); cursor: default; display: block; }}
            .p-group[open] {{ background: #1e293b; border-color: var(--primary); box-shadow: 0 10px 15px -3px rgba(0, 0, 0, 0.4); }}
            .p-group-title {{ font-size: 0.95rem; font-weight: 700; color: var(--buy); list-style: none; outline: none; cursor: pointer; display: flex; justify-content: space-between; align-items: center; }}
            .p-group-title::-webkit-details-marker {{ display: none; }}
            .p-group-title::after {{ content: '📂'; font-size: 0.8rem; opacity: 0.5; transition: 0.2s; }}
            .p-group[open] .p-group-title::after {{ content: '📖'; opacity: 1; transform: rotate(180deg); }}
            .p-group-title:hover {{ opacity: 0.8; }}
            
            .p-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 10px; padding-top: 15px; border-top: 1px solid #334155; margin-top: 10px; }}
            .p-item {{ display: flex; justify-content: space-between; align-items: center; background: rgba(15, 23, 42, 0.4); padding: 8px 12px; border-radius: 8px; border: 1px solid #33415566; }}
            .p-key {{ font-size: 0.85rem; color: var(--dim); font-family: 'Pretendard', sans-serif; }}
            .p-val-group {{ display: flex; align-items: center; gap: 8px; }}
            .p-val {{ font-size: 0.9rem; font-weight: 700; color: var(--text); min-width: 35px; text-align: center; }}
            .p-val-sub {{ font-size: 0.8rem; color: var(--primary); font-weight: 600; font-family: 'JetBrains Mono', monospace; }}
            .val-on {{ color: #4ade80; background: rgba(74, 222, 128, 0.1); padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; }}
            .val-off {{ color: #94a3b8; background: rgba(148, 163, 184, 0.1); padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; }}
            
            .header-flex {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 35px; border-bottom: 1px solid #1e293b; padding-bottom: 20px; }}
            h1 {{ margin: 0; font-size: 2.2rem; font-weight: 900; letter-spacing: -1px; }}
            .mode-badge-top {{ font-size: 0.8rem; background: {mode_color}; color: #0f172a; padding: 4px 12px; border-radius: 8px; font-weight: 800; }}
            
            .stats {{ display: grid !important; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 15px; margin-bottom: 40px; clear: both; width: 100%; }}
            .stat-card {{ background: linear-gradient(135deg, #1e293b, #0f172a); padding: 15px 12px; border-radius: 16px; border: 1px solid #334155; position: relative; box-sizing: border-box; min-width: 180px; }}
            .stat-val {{ font-size: 1.25rem; font-weight: 800; font-variant-numeric: tabular-nums; }}
            
            /* [v27.0] 전략별 진입 통계 스타일 - 수직 스택 구조 고정 */
            .strategy-stats {{ display: block; width: 100%; margin-bottom: 30px; }}
            .strategy-card {{ 
                background: #1e293b; padding: 20px; border-radius: 16px; border: 1px solid #334155;
                margin-bottom: 15px; width: 100%; box-sizing: border-box;
                box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
            }}
            .str-label {{ font-size: 1rem; font-weight: 700; color: #fff; }}
            .str-count {{ font-size: 1.2rem; font-weight: 800; color: var(--primary); }}
            .str-count.type-a {{ color: #4ade80; }}
            .str-count.type-b {{ color: #38bdf8; }}
            .str-count.type-s2 {{ color: #fbbf24; }}
            .str-count.s1-color {{ color: #94a3b8; }}
            .str-count.s2-color {{ color: #fb7185; }}
            
            /* 카드 내부 테이블 최적화 */
            .strategy-card table {{ width: 100%; border-collapse: collapse; margin-top: 10px; table-layout: auto; }}
            .strategy-card th {{ padding: 8px 0; border-bottom: 1px solid #334155; color: var(--dim); font-size: 0.75rem; text-align: left; }}
            .strategy-card td {{ padding: 10px 0; border-bottom: 1px solid #1e293b88; font-size: 0.9rem; }}
            .strategy-card tr:last-child td {{ border-bottom: none; }}
            
            .stock-group {{ background: #1e293b88; padding: 30px; border-radius: 24px; margin-bottom: 60px; border: 1px solid #334155; backdrop-filter: blur(10px); }}
            .stock-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 25px; border-bottom: 2px solid #334155; padding-bottom: 15px; }}
            .stock-header-title {{ font-size: 1.5rem; font-weight: 800; color: #fff; display: flex; align-items: center; gap: 12px; }}
            .stock-header-code {{ color: var(--buy); font-family: monospace; font-size: 1.1rem; }}
            
            table {{ width: 100%; border-collapse: separate; border-spacing: 0; margin-top: 15px; table-layout: fixed; }}
            th {{ color: var(--dim); padding: 15px 12px; text-align: left; font-size: 0.75rem; border-bottom: 2px solid #334155; font-weight: 700; text-transform: uppercase; letter-spacing: 1px; word-break: break-all; }}
            td {{ padding: 16px 12px; border-bottom: 1px solid #1e293b; font-size: 0.96rem; font-variant-numeric: tabular-nums; word-break: break-all; }}
            tr:last-child td {{ border-bottom: none; }}
            tr:hover td {{ background: rgba(59, 130, 246, 0.03); }}
            .action-buy {{ color: var(--buy); font-weight: 700; background: rgba(56,189,248,0.1); padding: 2px 6px; border-radius: 4px; }}
            .action-sell {{ color: var(--sell); font-weight: 700; background: rgba(245,158,11,0.1); padding: 2px 6px; border-radius: 4px; }}
            .action-hold {{ color: #a855f7; font-weight: 700; background: rgba(168,85,247,0.1); padding: 2px 6px; border-radius: 4px; }}
            .profit {{ color: var(--gain); font-weight: 700; }}
            .loss {{ color: var(--loss); font-weight: 700; }}
            td.gain, span.gain {{ color: var(--gain); font-weight: 600; }}
            td.loss, span.loss {{ color: var(--loss); font-weight: 600; }}
            .reason-text {{ color: var(--dim); font-size: 0.75rem; cursor: help; border-bottom: 1px dotted #444; display: block; padding: 2px 0; }}
            .reason-cell {{ width: 100%; position: relative; }}
            .indicator-panel {{ 
                font-size: 0.75rem; margin-top: 5px; background: #1e293b; 
                padding: 12px; border-radius: 10px; display: none; 
                position: absolute; left: 0; top: 100%; z-index: 100; 
                border: 1px solid #3b82f6aa; box-shadow: 0 10px 25px -10px rgba(0,0,0,0.8);
                min-width: 180px; pointer-events: none;
                backdrop-filter: blur(8px);
            }}
            .reason-cell:hover .indicator-panel {{ display: block; }}
            .chart-cell {{ position: relative; cursor: crosshair; display: inline-block; vertical-align: middle; width: 100px; height: 40px; }}
            .bar-trigger {{ position: absolute; top: 0; width: 18px; height: 100%; opacity: 0; z-index: 10; }}
            .chart-panel {{ 
                font-size: 0.72rem; background: #1e293b; padding: 12px; border-radius: 8px; 
                display: none; position: absolute; left: 100%; top: 50%; transform: translateY(-50%); 
                z-index: 60; border: 1px solid #334155; box-shadow: 0 10px 15px -3px rgba(0,0,0,0.5); 
                min-width: 150px; pointer-events: none; margin-left: 10px;
            }}
            /* Selective display based on which trigger is hovered */
            .bt-0:hover ~ .cp-0 {{ display: block; }}
            .bt-1:hover ~ .cp-1 {{ display: block; }}
            .bt-2:hover ~ .cp-2 {{ display: block; }}
            .bt-3:hover ~ .cp-3 {{ display: block; }}
            .bt-4:hover ~ .cp-4 {{ display: block; }}
            
            .chart-val-row {{ display: flex; justify-content: space-between; gap: 15px; border-bottom: 1px solid #334155; padding: 3px 0; }}
            .chart-val-label {{ color: var(--dim); }}
            .chart-val-value {{ color: white; font-weight: bold; }}
            tr:hover {{ background: rgba(255,255,255,0.03); }}
            ::-webkit-scrollbar {{ width: 8px; }}
            ::-webkit-scrollbar-track {{ background: var(--bg); }}
            ::-webkit-scrollbar-thumb {{ background: #334155; border-radius: 10px; }}
            ::-webkit-scrollbar-thumb:hover {{ background: #475569; }}
            
            /* [v39.0] 심층 분석(완료 라운드트립) */
            .depth-section {{ background: #1e293b; border: 1px solid #334155; border-radius: 16px; padding: 24px 28px; margin-bottom: 36px; clear: both; width: 100%; box-sizing: border-box; }}
            .depth-title {{ margin: 0 0 10px 0; font-size: 1.35rem; font-weight: 800; color: var(--buy); letter-spacing: -0.02em; }}
            .depth-lead {{ color: var(--dim); font-size: 0.88rem; line-height: 1.6; margin: 0 0 18px 0; max-width: 920px; }}
            .depth-grid {{ display: grid; grid-template-columns: 1.15fr 0.85fr; gap: 28px; align-items: start; }}
            @media (max-width: 1100px) {{ .depth-grid {{ grid-template-columns: 1fr; }} }}
            .depth-subtitle {{ font-size: 0.95rem; font-weight: 700; color: #e2e8f0; margin-bottom: 10px; }}
            .depth-table {{ width: 100%; border-collapse: collapse; font-size: 0.84rem; margin-bottom: 6px; }}
            .depth-table th, .depth-table td {{ padding: 9px 10px; border-bottom: 1px solid #334155; text-align: left; vertical-align: middle; }}
            .depth-table th {{ color: var(--dim); font-weight: 600; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.04em; }}
            .depth-table td.num {{ text-align: right; font-variant-numeric: tabular-nums; font-family: ui-monospace, monospace; }}
            .depth-open-box {{ background: rgba(168,85,247,0.09); border: 1px solid rgba(168,85,247,0.35); border-radius: 12px; padding: 14px 18px; margin-bottom: 18px; font-size: 0.88rem; color: #e2e8f0; }}
            .depth-verdict {{ margin-top: 14px; padding: 14px 16px; background: rgba(59,130,246,0.1); border: 1px solid rgba(59,130,246,0.25); border-radius: 10px; font-size: 0.86rem; color: #cbd5e1; line-height: 1.55; }}
            .depth-bucket-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-top: 6px; }}
            @media (max-width: 900px) {{ .depth-bucket-grid {{ grid-template-columns: 1fr; }} }}
            .depth-subnote {{ font-size: 0.8rem; color: var(--dim); margin-top: 10px; line-height: 1.5; }}
            .depth-subnote div {{ margin: 4px 0; }}
            .depth-err {{ color: #f87171; padding: 12px; }}
            /* [v40.0] 통합 분석 통계 (수직 스택) */
            .analysis-report-section {{ background: #1e293b; border: 1px solid #334155; border-radius: 16px; padding: 28px 32px 36px; margin-bottom: 40px; clear: both; width: 100%; box-sizing: border-box; }}
            .analysis-main-title {{ margin: 0 0 8px 0; font-size: 1.45rem; font-weight: 800; color: var(--buy); letter-spacing: -0.02em; }}
            .analysis-main-lead {{ color: var(--dim); font-size: 0.88rem; line-height: 1.65; margin: 0 0 8px 0; max-width: 960px; }}
            .analysis-h3 {{ font-size: 1.02rem; font-weight: 700; color: #e2e8f0; margin: 28px 0 12px 0; padding-bottom: 8px; border-bottom: 1px solid #334155; }}
            .analysis-h3:first-of-type {{ margin-top: 4px; }}
            .analysis-subblock {{ margin-bottom: 4px; }}
            .analysis-stack-spaced > .analysis-subblock + .analysis-subblock {{ margin-top: 22px; }}
            .analysis-type-card {{ background: rgba(15, 23, 42, 0.45); border: 1px solid #334155; border-radius: 12px; padding: 16px 18px; margin-bottom: 16px; }}
            .analysis-type-card.type-a {{ border-left: 4px solid #4ade80; }}
            .analysis-type-card.type-b {{ border-left: 4px solid #38bdf8; }}
        </style>
    </head>
    <body>
        <div class="sidebar">
            <div class="sidebar-header"><div class="sidebar-title">📊 Stock List</div></div>
            <ul class="nav-list">{sidebar_items}</ul>
        </div>
        <div class="main-content">
            <div class="container">
                <div class="header-flex">
                    <h1>Trade Execution Detailed Log <span class="mode-badge-top">{mode_label}</span></h1>
                    <div style="color:var(--dim)">{now} | {start_date} ~ {end_date} | {interval}</div>
                </div>
                <div class="stats">
                    <div class="stat-card"><div>Total Trades</div><div class="stat-val">{total}</div></div>
                    <div class="stat-card"><div>Win Rate</div><div class="stat-val" style="color:var(--gain)">{win_rate:.1f}%</div></div>
                    <div class="stat-card"><div>Avg Return</div><div class="stat-val" style="color:{ret_color}">{avg_ret:+.2f}%</div></div>
                    <div class="stat-card"><div>Avg Cash Ratio</div><div class="stat-val" style="color:#38bdf8">{avg_cash_ratio:.1f}%</div></div>
                    
                    <div class="stat-card"><div>Total Profit (Gross)</div><div class="stat-val" style="color:{gross_profit_color}">{gross_profit:+,} <small style="font-size:0.7rem;">원</small></div></div>
                    <div class="stat-card"><div>Total Fees</div><div class="stat-val" style="color:var(--loss)">-{total_fees:,} <small style="font-size:0.7rem;">원</small></div></div>
                    <div class="stat-card" style="border: 2px solid {total_profit_color}; background: rgba(0,255,127,0.05);">
                        <div style="font-weight:bold;">Net Profit / Loss</div>
                        <div class="stat-val" style="color:{total_profit_color}; font-size:1.6rem; font-weight:800;">{total_profit:+,} <small style="font-size:0.8rem;">KRW</small></div>
                    </div>
                </div>
                {analysis_stats_html}
                {equity_chart_html}
                <div style="margin-top:20px; color:var(--dim); font-size:0.85rem; margin-bottom:5px;">Strategy Parameters</div>
                {param_html}
                {content}
            </div>
        </div>
        <script>
            // [v16.7] Tooltip position correction for small charts and reason panels
            document.addEventListener('mouseover', (e) => {{
                if (e.target.classList.contains('bar-trigger')) {{
                    const cell = e.target.closest('.chart-cell');
                    if (!cell) return;
                    const panelIdx = e.target.className.split('bt-')[1];
                    const panel = cell.querySelector('.cp-' + panelIdx);
                    if (panel) {{
                        const rect = panel.getBoundingClientRect();
                        if (rect.right > window.innerWidth) {{
                            panel.style.left = 'auto';
                            panel.style.right = '100%';
                            panel.style.marginLeft = '0';
                            panel.style.marginRight = '10px';
                        }}
                    }}
                }}
                
                // [v16.9] 상세 사유 툴팁 위치 자동 보정
                const reasonCell = e.target.closest('.reason-cell');
                if (reasonCell) {{
                    const panel = reasonCell.querySelector('.indicator-panel');
                    if (panel) {{
                        // 임시로 보이게 해서 크기 측정 (CSS hover와 충돌 방지)
                        panel.style.display = 'block';
                        panel.style.visibility = 'hidden';
                        
                        const rect = panel.getBoundingClientRect();
                        const winH = window.innerHeight;
                        const winW = window.innerWidth;
                        
                        // 하단 잘림 방지
                        if (rect.bottom > winH) {{
                            panel.style.top = 'auto';
                            panel.style.bottom = '100%';
                            panel.style.marginBottom = '15px';
                        }} else {{
                            panel.style.top = '100%';
                            panel.style.bottom = 'auto';
                            panel.style.marginBottom = '0';
                        }}
                        
                        // 우측 잘림 방지
                        if (rect.right > winW) {{
                            panel.style.left = 'auto';
                            panel.style.right = '0';
                        }} else {{
                            panel.style.left = '0';
                            panel.style.right = 'auto';
                        }}
                        
                        panel.style.visibility = 'visible';
                        panel.style.display = ''; // CSS 클래스 제어로 복구
                    }}
                }}
            }});
        </script>
    </body></html>
    """

    base_dir = os.path.dirname(os.path.abspath(output_path))

    # Profit calculations
    total_profit_val = 0
    if not df.empty:
        for c in df['Code'].unique():
            stock_trades = df[df['Code'] == c]
            s_profit = 0
            for _, tr in stock_trades.iterrows():
                action = str(tr['Action'])
                amt = float(tr['Price']) * float(tr['Qty'])
                fee = float(tr.get('Fee', 0)) if not pd.isna(tr.get('Fee', 0)) else 0
                if "BUY" in action: 
                    total_profit_val = float(total_profit_val) - (amt + fee)
                elif action in ['SELL', 'HOLD']:
                    total_profit_val = float(total_profit_val) + (amt - fee)
            # total_profit_val += s_profit (기존 로직 대체)
    
    def _s_int(v, default=0):
        try:
            if v is None or pd.isna(v): return default
            return int(float(v))
        except: return default

    # [v27.0] Summary Section NaN Safety
    total_fees_val = _s_int(df['Fee'].sum() if 'Fee' in df.columns else 0)
    total_profit_val = _s_int(total_profit_val)
    total_profit_color = "var(--gain)" if total_profit_val >= 0 else "var(--loss)"
    
    gross_profit_val = total_profit_val + total_fees_val
    gross_profit_color = "var(--gain)" if gross_profit_val >= 0 else "var(--loss)"
    
    # [v27.0] Strategy Execution Breakdown - More robust counting (Prefix prioritized)
    count_a = len(df[(df['Action'].str.startswith('BUY', na=False)) & (df['Reason'].str.contains('TYPE_A', case=False, na=False))])
    count_b = len(df[(df['Action'].str.startswith('BUY', na=False)) & (df['Reason'].str.contains('TYPE_B', case=False, na=False))])
    count_s2 = len(df[(df['Action'].str.contains('BUY2', na=False)) | (df['Action'] == 'ADDON') | (df['Reason'].str.contains('Stage 2|ADDON', case=False, na=False))])
    
    # [v27.5] Smart Buy/Exit Tracking & End-of-Test Holdings (HOLD)
    # [v38.0] Reason 접두어(1M_SMART_EXIT, SMART_EXIT 등) 및 df_stats(REJECTED_SIG) 반영
    _sb_on_buy = _reason_matches_smart_buy(df['Reason'])
    count_smart_buy = int(((df['Action'].str.startswith('BUY', na=False)) & _sb_on_buy).sum())
    _sx_on_sell = _reason_matches_smart_exit(df['Reason'])
    count_smart_exit = int(((df['Action'].str.startswith('SELL', na=False)) & _sx_on_sell).sum())
    count_rejected = len(df_stats[df_stats['Action'] == 'REJECTED_SIG']) if not df_stats.empty else 0
    _sb_rej = _reason_matches_smart_buy(df_stats['Reason']) if not df_stats.empty else pd.Series(dtype=bool)
    count_smart_buy_rejected = int(((df_stats['Action'] == 'REJECTED_SIG') & _sb_rej).sum()) if not df_stats.empty else 0
    total_smart_buy_events = count_smart_buy + count_smart_buy_rejected
    pass_rate = (count_smart_buy / max(1, total_smart_buy_events)) * 100 if total_smart_buy_events > 0 else 0.0
    
    # [v4.10.6] Explicit Hold Count for Summary Dashboard
    hold_count = len(df[df['Action'] == 'HOLD']['Code'].unique())
    
    # [v5.0] Granular Profit & Yield Breakdown (S1-Only vs S1+S2)
    a_s1_p, a_s1_i = [], []     # Type A, Stage 1 Only
    a_s2_p, a_s2_i = [], []     # Type A, with Stage 2
    b_s1_p, b_s1_i = [], []     # Type B, Stage 1 Only
    b_s2_p, b_s2_i = [], []     # Type B, with Stage 2
    
    total_s1_p, total_s1_i = [], []  # All S1-only exits
    total_s2_p, total_s2_i = [], []  # All S1+S2 exits

    for c in df['Code'].unique():
        s_df = df[df['Code'] == c]
        entry_sig = "NONE"
        investment_sum = 0
        has_s2 = False
        
        # Action-based sequence parsing
        temp_inv = 0
        temp_has_s2 = False
        temp_sig = "NONE"
        
        for _, r in s_df.iterrows():
            action = str(r['Action'])
            price = float(r['Price'])
            qty = float(r['Qty'])
            fee = float(r.get('Fee', 0)) if not pd.isna(r.get('Fee', 0)) else 0
            
            if "BUY" in action:
                if any(x in action for x in ["BUY2", "BUY_2ND", "ADDON", "S2"]):
                    temp_has_s2 = True
                else:
                    temp_sig = str(r.get('Sig_Type', 'NONE'))
                temp_inv += (price * qty) + fee
            
            elif action in ["SELL", "HOLD"]:
                profit = (price * qty) - fee - temp_inv
                
                if temp_has_s2:
                    if "TYPE_A" in temp_sig:
                        a_s2_p.append(profit); a_s2_i.append(temp_inv)
                    elif "TYPE_B" in temp_sig:
                        b_s2_p.append(profit); b_s2_i.append(temp_inv)
                    total_s2_p.append(profit); total_s2_i.append(temp_inv)
                else:
                    if "TYPE_A" in temp_sig:
                        a_s1_p.append(profit); a_s1_i.append(temp_inv)
                    elif "TYPE_B" in temp_sig:
                        b_s1_p.append(profit); b_s1_i.append(temp_inv)
                    total_s1_p.append(profit); total_s1_i.append(temp_inv)
                
                # Reset for next trade in SAME ticker
                temp_inv = 0
                temp_has_s2 = False
                temp_sig = "NONE"

    def calc_yield(profits, invested):
        if not invested or sum(invested) == 0: return 0
        return (sum(profits) / sum(invested)) * 100

    y_a1, s_a1 = calc_yield(a_s1_p, a_s1_i), sum(a_s1_p)
    y_a2, s_a2 = calc_yield(a_s2_p, a_s2_i), sum(a_s2_p)
    y_b1, s_b1 = calc_yield(b_s1_p, b_s1_i), sum(b_s1_p)
    y_b2, s_b2 = calc_yield(b_s2_p, b_s2_i), sum(b_s2_p)
    y_s1, s_s1 = calc_yield(total_s1_p, total_s1_i), sum(total_s1_p)
    y_s2, s_s2 = calc_yield(total_s2_p, total_s2_i), sum(total_s2_p)

    # [v4.7] Refined Statistics (Signals vs Fills)
    count_fills = len(df[df['Action'].isin(['SELL', 'HOLD'])])
    completed_rounds = len(total_s1_p) + len(total_s2_p)

    depth_fragment = _build_depth_analytics_html(df)
    regime_fragment = '<p class="depth-subnote">시장 지표·레짐 분석은 사용하지 않습니다.</p>'

    ca1, ca2 = len(a_s1_p), len(a_s2_p)
    cb1, cb2 = len(b_s1_p), len(b_s2_p)
    c_all_s1, c_all_s2 = len(total_s1_p), len(total_s2_p)

    analysis_stats_html = f"""
    <div id="analysis-stats" class="analysis-report-section depth-section">
        <h2 class="analysis-main-title">📋 백테스트 분석 통계</h2>
        <p class="analysis-main-lead">
            체결 로그 기준으로 실행 요약, Type A/B·스테이지별 실현, 시장 레짐별 실현, 완료 라운드트립 심층(분포·집중도·청산유형)을 한 흐름으로 정리했습니다.
            상단 카드(승률·순손익 등)와 중복되는 수치는 여기서 한 번만 표 형태로 봅니다.
        </p>

        <h3 class="analysis-h3">1. 실행·신호 요약</h3>
        <table class="depth-table">
            <thead><tr><th>항목</th><th class="num">값</th><th>비고</th></tr></thead>
            <tbody>
                <tr><td>청산 행 (SELL/HOLD)</td><td class="num">{count_fills}건</td><td style="color:var(--dim);font-size:0.82rem;">로그 상 청산·평가 행 수</td></tr>
                <tr><td>완료 라운드트립</td><td class="num">{completed_rounds}건</td><td style="color:var(--dim);font-size:0.82rem;">종목별 매수체인→매도/평가 단위</td></tr>
                <tr><td>종료 시 보유 (HOLD 종목 수)</td><td class="num">{hold_count}종목</td><td style="color:var(--dim);font-size:0.82rem;">고유 종목</td></tr>
                <tr><td>스마트 진입 통과 / 시도</td><td class="num">{count_smart_buy} / {total_smart_buy_events}건</td><td style="color:var(--dim);font-size:0.82rem;">통과율 {pass_rate:.1f}%</td></tr>
                <tr><td>스마트 진입 거절 (수급)</td><td class="num">{count_smart_buy_rejected}건</td><td style="color:var(--dim);font-size:0.82rem;">REJECTED_SIG 중 SmartBuy 패턴</td></tr>
                <tr><td>신호 거절 전체 (REJECTED_SIG)</td><td class="num">{count_rejected}건</td><td style="color:var(--dim);font-size:0.82rem;">df_stats 기준</td></tr>
                <tr><td>스마트 청산 (SELL·스마트 패턴)</td><td class="num">{count_smart_exit}건</td><td style="color:var(--dim);font-size:0.82rem;">Smart Exit 변형 포함</td></tr>
                <tr><td>매수 행·Reason 기준 TYPE_A / TYPE_B</td><td class="num">{count_a} / {count_b}건</td><td style="color:var(--dim);font-size:0.82rem;">참고용(Reason 문자열)</td></tr>
                <tr><td>추매 관련 매수 행</td><td class="num">{count_s2}건</td><td style="color:var(--dim);font-size:0.82rem;">BUY2·ADDON·Reason 키워드</td></tr>
            </tbody>
        </table>

        <h3 class="analysis-h3">2. 전략·스테이지별 실현 (Type A/B)</h3>
        <div class="analysis-type-card type-a">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                <span style="color:#4ade80;font-weight:800;font-size:1.05rem;">Type A (눌림목)</span>
                <span style="color:var(--dim);font-size:0.88rem;">완료 라운드 <b style="color:#fff;">{ca1 + ca2}</b>건</span>
            </div>
            <table class="depth-table">
                <thead><tr><th>구분</th><th class="num">건수</th><th class="num">가중 수익률</th><th class="num">총 수익금</th></tr></thead>
                <tbody>
                    <tr><td>Stage 1만</td><td class="num">{ca1}</td><td class="num {'gain' if s_a1 >= 0 else 'loss'}">{y_a1:+.2f}%</td><td class="num {'gain' if s_a1 >= 0 else 'loss'}">{int(s_a1):,}원</td></tr>
                    <tr><td>S2 포함</td><td class="num">{ca2}</td><td class="num {'gain' if s_a2 >= 0 else 'loss'}">{y_a2:+.2f}%</td><td class="num {'gain' if s_a2 >= 0 else 'loss'}">{int(s_a2):,}원</td></tr>
                </tbody>
            </table>
            <div style="text-align:right;margin-top:8px;color:var(--dim);font-size:0.85rem;">Type A 합계 <span class="{'gain' if (s_a1+s_a2) >= 0 else 'loss'}" style="font-weight:800;font-size:1.05rem;">{int(s_a1+s_a2):,}원</span></div>
        </div>
        <div class="analysis-type-card type-b">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
                <span style="color:#38bdf8;font-weight:800;font-size:1.05rem;">Type B (추세돌파)</span>
                <span style="color:var(--dim);font-size:0.88rem;">완료 라운드 <b style="color:#fff;">{cb1 + cb2}</b>건</span>
            </div>
            <table class="depth-table">
                <thead><tr><th>구분</th><th class="num">건수</th><th class="num">가중 수익률</th><th class="num">총 수익금</th></tr></thead>
                <tbody>
                    <tr><td>Stage 1만</td><td class="num">{cb1}</td><td class="num {'gain' if s_b1 >= 0 else 'loss'}">{y_b1:+.2f}%</td><td class="num {'gain' if s_b1 >= 0 else 'loss'}">{int(s_b1):,}원</td></tr>
                    <tr><td>S2 포함</td><td class="num">{cb2}</td><td class="num {'gain' if s_b2 >= 0 else 'loss'}">{y_b2:+.2f}%</td><td class="num {'gain' if s_b2 >= 0 else 'loss'}">{int(s_b2):,}원</td></tr>
                </tbody>
            </table>
        </div>
        <div class="analysis-subblock">
            <div class="depth-subtitle">전체 합산 (타입 무관)</div>
            <table class="depth-table">
                <thead><tr><th>구분</th><th class="num">건수</th><th class="num">가중 수익률</th><th class="num">총 수익금</th></tr></thead>
                <tbody>
                    <tr><td>S1만 (전 타입)</td><td class="num">{c_all_s1}</td><td class="num {'gain' if s_s1 >= 0 else 'loss'}">{y_s1:.2f}%</td><td class="num {'gain' if s_s1 >= 0 else 'loss'}">{int(s_s1):,}원</td></tr>
                    <tr><td>S2 포함 (전 타입)</td><td class="num">{c_all_s2}</td><td class="num {'gain' if s_s2 >= 0 else 'loss'}">{y_s2:.2f}%</td><td class="num {'gain' if s_s2 >= 0 else 'loss'}">{int(s_s2):,}원</td></tr>
                </tbody>
            </table>
        </div>

        <h3 class="analysis-h3">3. 시장 레짐별 실현</h3>
        <div class="analysis-subblock">{regime_fragment}</div>

        <h3 class="analysis-h3">4. 완료 라운드트립 심층</h3>
        <p class="depth-lead" style="margin-bottom:16px;">
            매수 체인(BUY*) 이후 <b>SELL 또는 HOLD</b>로 닫힌 거래만 집계합니다.
            수익률(%)은 매도 행 <code>Return</code>, 원화 손익은 매수·매도 금액과 수수료로 추정합니다.
        </p>
        <div class="analysis-subblock">{depth_fragment if depth_fragment else '<p class="depth-subnote">완료 라운드가 없거나 심층 분석을 생성할 수 없습니다.</p>'}</div>
    </div>
    """

    # Portfolio Equity Chart
    equity_chart_html = ""
    if daily_equity:
        try:
            # Timestamp 기준 정렬. 구 데이터(날짜만)는 자정으로 파싱되어
            # 시작점(08:59)보다 앞에 오므로 당일 장마감으로 보정한다.
            def _equity_ts(row: dict):
                try:
                    raw = str(row.get("date", "") or "").strip()
                    ts = pd.Timestamp(raw)
                    if len(raw) <= 10:
                        try:
                            from utils.krx_session_hours import get_session_hours

                            hm = int(get_session_hours(ts.date()).close_minutes)
                            h, m = divmod(max(0, hm), 60)
                            ts = ts.normalize() + pd.Timedelta(hours=h, minutes=m)
                        except Exception:
                            ts = ts.normalize() + pd.Timedelta(hours=15, minutes=30)
                    return ts
                except Exception:
                    return pd.Timestamp.min

            daily_equity = sorted(daily_equity, key=_equity_ts)
            dates = [str(d["date"]) for d in daily_equity]
            equities = [d["equity"] for d in daily_equity]
            cashes = [d["cash"] for d in daily_equity]
            holdings = [max(0, d["equity"] - d["cash"]) for d in daily_equity]
            
            # [v32.1] Calculate Cash Metrics
            ratios = [(d['cash'] / d['equity'] * 100) for d in daily_equity if d['equity'] > 0]
            avg_cash_ratio = sum(ratios) / len(ratios) if ratios else 0
            
            # [v15.5.1] Synchronize Y-axes to ensure the red line tracks the top of the stacked area
            max_val = max(equities) * 1.05
            
            equity_chart_html = f"""
            <div style="background:var(--card-bg); border-radius:12px; padding:20px; margin-top:30px; border:1px solid #334155; display: block; clear: both; width: 100%; box-sizing: border-box; overflow: hidden;">
                <h3 style="margin-top:0; margin-bottom:15px; font-size:1.15rem; color:var(--primary); border-bottom: 1px solid #334155; padding-bottom: 10px;">📈 포트폴리오 자산 추이 및 비중 분석</h3>
                <div id="equity-chart" style="height:450px; width:100%; min-height:450px;"></div>
                <script>
                    const traceEquity = {{
                        x: {json.dumps(dates)}, y: {json.dumps(equities)},
                        name: '총 자본 (평가액)', type: 'scatter', mode: 'lines',
                        line: {{ color: '#f43f5e', width: 3 }}, yaxis: 'y2'
                    }};
                    const traceCash = {{
                        x: {json.dumps(dates)}, y: {json.dumps(cashes)},
                        name: '현금 잔고', type: 'scatter', mode: 'none', fill: 'tozeroy',
                        fillcolor: 'rgba(56, 189, 248, 0.2)', stackgroup: 'one'
                    }};
                    const traceHoldings = {{
                        x: {json.dumps(dates)}, y: {json.dumps(holdings)},
                        name: '주식 보유금액', type: 'scatter', mode: 'none', fill: 'tonexty',
                        fillcolor: 'rgba(245, 158, 11, 0.4)', stackgroup: 'one'
                    }};
                    const layout = {{
                        paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)',
                        margin: {{ t: 10, b: 40, l: 60, r: 60 }},
                        legend: {{ orientation: 'h', x: 0.5, xanchor: 'center', y: 1.1, font: {{ color: '#ccc' }} }},
                        xaxis: {{ gridcolor: '#333', tickfont: {{ color: '#888' }} }},
                        yaxis: {{ 
                            gridcolor: '#333', tickfont: {{ color: '#888' }}, 
                            title: '자산 구성 (원)', side: 'left',
                            range: [0, {max_val}]
                        }},
                        yaxis2: {{ 
                            overlaying: 'y', side: 'right', showgrid: false, 
                            tickfont: {{ color: '#f43f5e' }}, title: '총 자산 (원)',
                            range: [0, {max_val}]
                        }},
                        hovermode: 'x unified'
                    }};
                    Plotly.newPlot('equity-chart', [traceCash, traceHoldings, traceEquity], layout, {{ responsive: true, displayModeBar: false }});
                </script>
            </div>
            """
        except Exception as e:
            equity_chart_html = f"<p style='color:red'>Chart generation error: {e}</p>"

    loader = BacktestDataLoader()
    content = []
    sidebar_items = []
    sorted_codes = sorted(df['Code'].unique())
    
    if callback: callback("매매 리포트 생성 중...", 95)

    for code in sorted_codes:
        group = df[df['Code'] == code].sort_values(by='Date')
        name = group['Name'].iloc[0]
        code_str = str(code).zfill(6)
        sidebar_items.append(f'<li class="nav-item"><a href="#stock-{code_str}">{name} ({code_str})</a></li>')
        
        chart_link_html = f'<a href="https://finance.naver.com/item/main.naver?code={code_str}" target="_blank" style="text-decoration:none;"><button style="background:var(--buy); color:white; border:none; padding:5px 12px; border-radius:5px; cursor:pointer; font-size:0.85rem; margin-left:15px;">📊 네이버 증권 차트</button></a>'

        group_rows = []
        stock_realized_profit = 0
        # summary 카드(total_profit) · 분석 통계(temp_inv, 1820행)와 동일: 매수 체인의 (체결액+매수수수료) 누적 후,
        # 청산(SELL/HOLD) 시 (매도액-매도수수료)에서 빼서 순실현(매수·매도 수수료 전부 반영)
        round_trip_cost: float = 0.0
        price_df = loader.get_data(code_str, (group['Date'].min() - timedelta(days=5)).strftime('%Y-%m-%d'), 
                                 (group['Date'].max() + timedelta(days=5)).strftime('%Y-%m-%d'), interval=interval)
        
        for _, r in group.iterrows():
            # 시장 지표·레짐 배지는 사용하지 않음
            status_badge = (
                '<span style="background:#64748b; color:white; padding:2px 8px; '
                'border-radius:4px; font-weight:800; font-size:0.75rem;">-</span>'
            )

            amt = r['Price'] * r['Qty']
            fee = r.get('Fee', 0) if not pd.isna(r.get('Fee', 0)) else 0
            ret_str, action_cls, cum_profit_str = "", "action-buy", "-"
            
            action_raw = str(r['Action'])
            if "BUY" in action_raw:
                try:
                    f_b = float(fee) if not pd.isna(fee) else 0.0
                    am = float(amt) if not pd.isna(amt) else 0.0
                    round_trip_cost += am + f_b
                except (TypeError, ValueError) as e:
                    try:
                        logging.warning("종목표 매수 누적액 산정 실패(%s %s): %s", code_str, action_raw, e)
                    except Exception:
                        pass
            elif action_raw == 'REJECTED_SIG':
                action_cls = "action-rejected"
                ret_str = '<span style="color:var(--dim)">Filtered</span>'
            elif "PROTECT" in action_raw:
                action_cls = "action-hold" # [v8.0] 파란색 계열로 표시
                ret_str = "-"
                cum_profit_str = "-"
            else:
                ret_val = float(r.get('Return', 0)) if not pd.isna(r.get('Return', 0)) else 0
                # [v8.0] + [v39.1] 청산 행: Return·평단 역산 대신, 요약·스테이지 손익(1823행)과 동일한 순실현
                if action_raw in ['SELL', 'HOLD', 'PARTIAL_SELL']:
                    try:
                        f_s = float(fee) if not pd.isna(fee) else 0.0
                        am = float(amt) if not pd.isna(amt) else 0.0
                        p_inc = (am - f_s) - round_trip_cost
                    except (TypeError, ValueError) as e:
                        try:
                            logging.warning("종목표 청산 순익 산정 실패(%s %s): %s", code_str, action_raw, e)
                        except Exception:
                            pass
                        p_inc = 0.0
                    if not pd.isna(p_inc):
                        stock_realized_profit = float(stock_realized_profit) + p_inc
                    try:
                        round_trip_cost = 0.0
                    except Exception:
                        pass
                
                action_cls = "action-sell"
                if r['Action'] in ["SELL", "HOLD", "PARTIAL_SELL"]:
                    r_cls = "profit" if ret_val > 0 else "loss"
                    ret_str = f'<span class="{r_cls}">{ret_val:+.2f}%</span>'
                    if r['Action'] == "HOLD":
                        action_cls = "action-hold"
                        ret_str = f'<span class="{r_cls}">{ret_val:+.2f}% (Held)</span>'
                
                p_row_cls = "profit" if stock_realized_profit >= 0 else "loss"
                p_val_display = _s_int(stock_realized_profit)
                cum_profit_str = f'<span class="{p_row_cls}">{p_val_display:+,}</span>'
            
            candle_svg = "-"
            if price_df is not None and not price_df.empty:
                try:
                    target_idx = price_df.index.get_indexer([r['Date']], method='nearest')[0]
                    window = price_df.iloc[max(0, target_idx-2) : min(len(price_df), target_idx+3)]
                    if not window.empty:
                        svg_h, w_high, w_low = 40, window['High'].max(), window['Low'].min()
                        diff = max(w_high - w_low, 1)
                        scale = lambda v: svg_h - ((v - w_low) / diff * svg_h)
                        
                        svg_parts, triggers, panels = [], [], []
                        for i, (idx, row) in enumerate(window.iterrows()):
                            x, color = 5 + i * 18, ("#f43f5e" if row['Close'] >= row['Open'] else "#3b82f6")
                            yh, yl, yo, yc = scale(row['High']), scale(row['Low']), scale(row['Open']), scale(row['Close'])
                            title_str = f'[{idx.strftime("%m-%d %H:%M")}]\n시가: {int(row["Open"]):,}\n고가: {int(row["High"]):,}\n저가: {int(row["Low"]):,}\n종가: {int(row["Close"]):,}'
                            svg_parts.append(f'<g><title>{title_str}</title><line x1="{x+7}" y1="{yh}" x2="{x+7}" y2="{yl}" stroke="{color}" stroke-width="1.5"/><rect x="{x}" y="{min(yo, yc)}" width="14" height="{max(1,abs(yo-yc))}" fill="{color}"/></g>')
                            
                            triggers.append(f'<div class="bar-trigger bt-{i}" style="left:{x}px;"></div>')
                            panels.append(f"""<div class="chart-panel cp-{i}">
                                <div style="color:var(--buy); font-weight:bold; margin-bottom:8px; border-bottom:1px solid #475569; padding-bottom:5px;">📊 Candle ({idx.strftime('%H:%M')})</div>
                                <div class="chart-val-row"><span class="chart-val-label">시가</span><span class="chart-val-value">{int(row['Open']):,}</span></div>
                                <div class="chart-val-row"><span class="chart-val-label">고가</span><span class="chart-val-value">{int(row['High']):,}</span></div>
                                <div class="chart-val-row"><span class="chart-val-label">저가</span><span class="chart-val-value">{int(row['Low']):,}</span></div>
                                <div class="chart-val-row"><span class="chart-val-label">종가</span><span class="chart-val-value">{int(row['Close']):,}</span></div>
                            </div>""")
                        
                        candle_svg = f'<div class="chart-cell"><svg width="100" height="40">{"".join(svg_parts)}</svg>{"".join(triggers)}{"".join(panels)}</div>'
                except: pass

            raw_reason = str(r['Reason'])
            parts = raw_reason.split("|")
            reason_main = parts[0]
            
            ind_list = []
            # [v16.9] 모든 파이프(|) 구분 항목을 툴팁에 포함
            for pair in parts[1:]:
                if ":" in pair:
                    k, v = pair.split(":", 1)
                    color = ("var(--buy)" if k == "RSI" and float(v) < 35 else "var(--loss)" if k == "RSI" and float(v) > 65 else "var(--dim)")
                    ind_list.append(f'<div style="display:flex; justify-content:space-between; gap:10px; border-bottom:1px solid #444; padding:2px 0;"><span style="color:var(--dim)">{k}</span><span style="color:{color}; font-weight:bold;">{v}</span></div>')
                else:
                    # 콜론이 없는 일반 텍스트도 추가
                    ind_list.append(f'<div style="border-bottom:1px solid #444; padding:4px 0; color:var(--text); font-size:0.7rem;">• {pair}</div>')
            
            indicators_html = ""
            if ind_list:
                indicators_html = f'<div class="indicator-panel"><div style="min-width:160px; padding:5px;">{"".join(ind_list)}</div></div>'

            group_rows.append(f"""<tr><td>{r['Date'].strftime('%m-%d %H:%M')}</td><td><span class="{action_cls}">{r['Action']}</span></td><td>{candle_svg}</td><td class="num">{_s_int(r['Price']):,}</td><td class="num">{_s_int(r['Qty']):,}</td><td class="num">{_s_int(amt):,}</td><td class="num">{_s_int(fee):,}</td><td class="num">{_s_int(r.get('Total_Qty', 0)):,}</td><td class="num">{ret_str}</td><td class="num">{cum_profit_str}</td><td class="num">{status_badge}</td><td class="num"><div class="reason-cell"><span class="reason-text">{reason_main} 🔍</span>{indicators_html}</div></td></tr>""")
        
        profit_color = "var(--gain)" if stock_realized_profit >= 0 else "var(--loss)"
        content.append(f"""<div class="stock-group" id="stock-{code_str}"><div class="stock-header"><div style="display:flex; align-items:center;">{name} <span style="color:var(--dim); font-size:0.9rem; margin-left:8px;">({code_str})</span>{chart_link_html}</div><div style="font-size:1rem; color:white">누적 수익금: <span style="color:{profit_color}">{_s_int(stock_realized_profit):+,}원</span></div></div><table><thead><tr><th>시간</th><th>구분</th><th>차트</th><th>가격</th><th>수량</th><th>거래액</th><th>비용</th><th>보유</th><th>수익률</th><th>누적 수익금액</th><th>시장</th><th>상세 사유</th></tr></thead><tbody>{"".join(group_rows)}</tbody></table></div>""")

    sidebar_items.insert(
        0,
        '<li class="nav-item"><a href="#analysis-stats" style="color:#38bdf8;font-weight:700;">📋 분석 통계</a></li>',
    )

    ret_color = "var(--gain)" if avg_ret > 0 else "var(--loss)"
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_template.format(
            now=now_str, 
            interval=display_interval, 
            mode_label=mode_label, 
            mode_color=mode_color, 
            total=total_trades, 
            win_rate=win_rate, 
            avg_ret=avg_ret, 
            ret_color=ret_color, 
            total_profit=total_profit_val, 
            total_profit_color=total_profit_color, 
            gross_profit=gross_profit_val, 
            gross_profit_color=gross_profit_color, 
            total_fees=total_fees_val, 
            param_html=param_html, 
            sidebar_items="".join(sidebar_items), 
            content="".join(content), 
            start_date=start_date if start_date else "Simulation", 
            end_date=end_date if end_date else "End", 
            equity_chart_html=equity_chart_html,
            analysis_stats_html=analysis_stats_html,
            avg_cash_ratio=avg_cash_ratio,
            plotly_script=plotly_cdn_script_tag(),
        ))
    print(f"  [Report] Execution log saved: {output_path}")
