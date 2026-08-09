# -*- coding: utf-8 -*-
"""
모바일 읽기 전용 대시보드용 상태 스냅샷.

- Tk 위젯 / Kiwoom REST 호출 금지 (HTTP 워커 스레드에서 안전).
- PortfolioManager.lock 하에서 딕셔너리 복사.
"""
from __future__ import annotations

import csv
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from utils.fee_rates import unrealized_at_price_net_of_sell_fee
from utils.server_mode_paths import (
    equity_snapshots_root_dir,
    is_mock_mode,
    state_db_path,
    virtual_db_path,
)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        if f != f or abs(f) == float("inf"):
            return default
        return f
    except (TypeError, ValueError):
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _norm_code(code: Any) -> str:
    try:
        c = str(code or "").strip()
        if "." in c:
            c = c.split(".")[0]
        c = "".join(ch for ch in c if ch.isdigit())
        if not c:
            return ""
        return c.zfill(6)[-6:]
    except Exception:
        return ""


def _monitor_last_price(info: Dict[str, Any]) -> float:
    if not isinstance(info, dict):
        return 0.0
    for k in ("last_price", "price", "Close", "close", "cur_prc"):
        p = _safe_float(info.get(k), 0.0)
        if p > 0:
            return p
    return 0.0


def _copy_positions(portfolio: Any) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        lock = getattr(portfolio, "lock", None)
        if lock is not None:
            with lock:
                raw = dict(getattr(portfolio, "virtual_positions", None) or {})
        else:
            raw = dict(getattr(portfolio, "virtual_positions", None) or {})
        for k, v in raw.items():
            nk = _norm_code(k)
            if not nk or not isinstance(v, dict):
                continue
            out[nk] = dict(v)
    except Exception:
        pass
    return out


def _copy_history(portfolio: Any) -> List[Dict[str, Any]]:
    try:
        lock = getattr(portfolio, "lock", None)
        if lock is not None:
            with lock:
                hist = list(getattr(portfolio, "virtual_history", None) or [])
        else:
            hist = list(getattr(portfolio, "virtual_history", None) or [])
        return [dict(x) for x in hist if isinstance(x, dict)]
    except Exception:
        return []


def _copy_monitor_info(app: Any) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        mi = getattr(app, "monitor_info", None) or {}
        if not isinstance(mi, dict):
            return out
        for k, v in mi.items():
            nk = _norm_code(k)
            if not nk:
                continue
            if isinstance(v, dict):
                out[nk] = dict(v)
    except Exception:
        pass
    return out


def _portfolio_cash_fields(portfolio: Any) -> Dict[str, float]:
    try:
        lock = getattr(portfolio, "lock", None)
        if lock is not None:
            with lock:
                return {
                    "cash": _safe_float(getattr(portfolio, "actual_deposit", 0)),
                    "d1_cash": _safe_float(getattr(portfolio, "d1_deposit", 0)),
                    "d2_cash": _safe_float(getattr(portfolio, "d2_deposit", 0)),
                    "broker_tot_evlu": _safe_float(
                        getattr(portfolio, "last_tot_evlu_amt", 0)
                    ),
                    "total_equity_attr": _safe_float(
                        getattr(portfolio, "total_equity", 0)
                    ),
                }
        return {
            "cash": _safe_float(getattr(portfolio, "actual_deposit", 0)),
            "d1_cash": _safe_float(getattr(portfolio, "d1_deposit", 0)),
            "d2_cash": _safe_float(getattr(portfolio, "d2_deposit", 0)),
            "broker_tot_evlu": _safe_float(getattr(portfolio, "last_tot_evlu_amt", 0)),
            "total_equity_attr": _safe_float(getattr(portfolio, "total_equity", 0)),
        }
    except Exception:
        return {
            "cash": 0.0,
            "d1_cash": 0.0,
            "d2_cash": 0.0,
            "broker_tot_evlu": 0.0,
            "total_equity_attr": 0.0,
        }


def _holding_metrics(
    pos: Dict[str, Any],
    last_price: float,
    settings: Dict[str, Any],
) -> Dict[str, float]:
    qty = _safe_int(pos.get("qty"), 0)
    avg = _safe_float(pos.get("avg_price"), 0.0)
    invested = _safe_float(pos.get("invested"), 0.0)
    if invested <= 0 and qty > 0 and avg > 0:
        invested = float(qty) * avg
    unreal = 0.0
    try:
        if qty > 0 and last_price > 0:
            unreal = float(
                unrealized_at_price_net_of_sell_fee(
                    pos, last_price, qty, avg, settings or {}
                )
            )
    except Exception:
        try:
            unreal = (last_price - avg) * qty if qty > 0 and avg > 0 else 0.0
        except Exception:
            unreal = 0.0
    eval_amt = invested + unreal
    pct = (unreal / invested * 100.0) if invested > 0 else 0.0
    return {
        "invested": invested,
        "unrealized_amt": unreal,
        "unrealized_pct": pct,
        "eval_amt": eval_amt,
    }


def build_status(app: Any) -> Dict[str, Any]:
    """GET /api/v1/status"""
    now = datetime.now()
    use_mock = bool(is_mock_mode())
    is_mon = False
    try:
        is_mon = bool(getattr(app, "is_monitoring", False))
    except Exception:
        is_mon = False
    try:
        tc = getattr(app, "trading_controller", None)
        if tc is not None:
            is_mon = is_mon or bool(getattr(tc, "is_monitoring", False))
    except Exception:
        pass

    monitor_count = 0
    try:
        mi = getattr(app, "monitor_info", None) or {}
        if isinstance(mi, dict):
            monitor_count = len(mi)
    except Exception:
        monitor_count = 0

    remote_running = False
    try:
        rc = getattr(app, "remote_commander", None)
        remote_running = bool(rc is not None and getattr(rc, "is_running", False))
    except Exception:
        remote_running = False

    settings = getattr(app, "settings", None) or {}
    selection_fresh = False
    selection_mtime: Optional[str] = None
    selection_count = 0
    try:
        from core.selection_freshness import (
            count_selected_targets,
            is_selected_targets_fresh,
            selected_targets_mtime,
        )

        selection_fresh = bool(
            is_selected_targets_fresh(settings=settings if isinstance(settings, dict) else None)
        )
        mt = selected_targets_mtime()
        if mt is not None:
            selection_mtime = mt.isoformat(timespec="seconds")
        selection_count = int(count_selected_targets())
    except Exception:
        pass

    return {
        "server_time": now.isoformat(timespec="seconds"),
        "use_mock": use_mock,
        "mode": "mock" if use_mock else "live",
        "is_monitoring": is_mon,
        "monitor_code_count": monitor_count,
        "remote_commander_running": remote_running,
        "selection_fresh": selection_fresh,
        "selection_mtime": selection_mtime,
        "selection_count": selection_count,
        "paths": {
            "virtual_db": virtual_db_path(),
            "state_db": state_db_path(),
            "equity_root": equity_snapshots_root_dir(),
        },
    }


def build_portfolio(app: Any) -> Dict[str, Any]:
    """GET /api/v1/portfolio"""
    now = datetime.now()
    portfolio = getattr(app, "portfolio", None)
    settings = getattr(app, "settings", None) or {}
    if not isinstance(settings, dict):
        settings = {}

    cash_f = (
        _portfolio_cash_fields(portfolio)
        if portfolio is not None
        else {
            "cash": 0.0,
            "d1_cash": 0.0,
            "d2_cash": 0.0,
            "broker_tot_evlu": 0.0,
            "total_equity_attr": 0.0,
        }
    )
    positions = _copy_positions(portfolio) if portfolio is not None else {}
    mi = _copy_monitor_info(app)

    total_invested = 0.0
    total_unreal = 0.0
    for code, pos in positions.items():
        lp = _monitor_last_price(mi.get(code, {}))
        if lp <= 0:
            lp = _safe_float(pos.get("avg_price"), 0.0)
        m = _holding_metrics(pos, lp, settings)
        total_invested += m["invested"]
        total_unreal += m["unrealized_amt"]

    stock_eval = total_invested + total_unreal
    d2 = cash_f["d2_cash"]
    total_assets = d2 + stock_eval
    if total_assets <= 0 and cash_f["broker_tot_evlu"] > 0:
        total_assets = cash_f["broker_tot_evlu"]
    if total_assets <= 0 and cash_f["total_equity_attr"] > 0:
        total_assets = cash_f["total_equity_attr"]
    if total_assets <= 0:
        total_assets = cash_f["cash"] + stock_eval

    day_pnl_amt: Optional[float] = None
    day_pnl_pct: Optional[float] = None
    day_pnl_na = False
    baseline_source: Optional[str] = None
    try:
        from utils.session_open_day_pnl import ensure_day_state

        st = ensure_day_state(app, now)
        day_pnl_na = bool(st.get("na"))
        baseline_source = st.get("baseline_source")
        if not day_pnl_na and st.get("baseline") is not None:
            base = _safe_float(st.get("baseline"), 0.0)
            if base > 0:
                day_pnl_amt = float(total_assets) - base
                day_pnl_pct = (day_pnl_amt / base) * 100.0
    except Exception:
        pass

    return {
        "server_time": now.isoformat(timespec="seconds"),
        "cash": cash_f["cash"],
        "d1_cash": cash_f["d1_cash"],
        "d2_cash": cash_f["d2_cash"],
        "stock_eval": stock_eval,
        "total_assets": total_assets,
        "broker_tot_evlu": cash_f["broker_tot_evlu"],
        "day_pnl_amt": day_pnl_amt,
        "day_pnl_pct": day_pnl_pct,
        "day_pnl_na": day_pnl_na,
        "baseline_source": baseline_source,
        "holdings_count": len(positions),
        "updated_at": now.isoformat(timespec="seconds"),
    }


def build_holdings(app: Any) -> Dict[str, Any]:
    """GET /api/v1/holdings"""
    now = datetime.now()
    portfolio = getattr(app, "portfolio", None)
    settings = getattr(app, "settings", None) or {}
    if not isinstance(settings, dict):
        settings = {}
    positions = _copy_positions(portfolio) if portfolio is not None else {}
    mi = _copy_monitor_info(app)
    rows: List[Dict[str, Any]] = []
    for code, pos in positions.items():
        info = mi.get(code, {})
        lp = _monitor_last_price(info)
        if lp <= 0:
            lp = _safe_float(pos.get("avg_price"), 0.0)
        m = _holding_metrics(pos, lp, settings)
        rows.append(
            {
                "code": code,
                "name": str(pos.get("name") or info.get("name") or code),
                "qty": _safe_int(pos.get("qty"), 0),
                "avg_price": _safe_float(pos.get("avg_price"), 0.0),
                "stage": _safe_int(pos.get("stage") or pos.get("entry_n"), 1),
                "invested": m["invested"],
                "sig_type": str(pos.get("sig_type") or ""),
                "reason": str(pos.get("reason") or ""),
                "entry_time": str(pos.get("entry_time") or ""),
                "last_price": lp,
                "unrealized_amt": m["unrealized_amt"],
                "unrealized_pct": m["unrealized_pct"],
                "eval_amt": m["eval_amt"],
                "exit_pending": bool(pos.get("exit_pending")),
            }
        )
    rows.sort(key=lambda r: abs(_safe_float(r.get("unrealized_amt"))), reverse=True)
    return {"server_time": now.isoformat(timespec="seconds"), "holdings": rows}


def build_watchlist(app: Any) -> Dict[str, Any]:
    """GET /api/v1/watchlist"""
    now = datetime.now()
    mi = _copy_monitor_info(app)
    targets: List[Dict[str, Any]] = []
    try:
        from core.selection_freshness import selected_targets_csv_path

        path = selected_targets_csv_path()
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if not isinstance(row, dict):
                        continue
                    code = _norm_code(row.get("Code") or row.get("code"))
                    if not code:
                        continue
                    info = mi.get(code, {})
                    targets.append(
                        {
                            "code": code,
                            "name": str(
                                row.get("Name")
                                or row.get("name")
                                or info.get("name")
                                or code
                            ),
                            "score": _safe_float(row.get("Score") or info.get("score")),
                            "sel_track": str(
                                row.get("SelTrack") or info.get("sel_track") or ""
                            ),
                            "last_price": _monitor_last_price(info)
                            or _safe_float(row.get("Price")),
                            "ratio": _safe_float(
                                info.get("ratio") if info.get("ratio") is not None else row.get("Ratio")
                            ),
                            "diff": _safe_float(
                                info.get("diff") if info.get("diff") is not None else row.get("Diff")
                            ),
                            "confluence": str(info.get("confluence") or ""),
                            "status_text": str(info.get("status_text") or ""),
                            "vol_power": _safe_float(info.get("vol_power")),
                        }
                    )
    except Exception:
        targets = []

    favorites: List[Dict[str, Any]] = []
    try:
        store = getattr(app, "watchlist_store", None)
        if store is not None and hasattr(store, "get_items"):
            for it in store.get_items() or []:
                if not isinstance(it, dict):
                    continue
                code = _norm_code(it.get("code"))
                if not code:
                    continue
                favorites.append(
                    {
                        "code": code,
                        "name": str(it.get("name") or code),
                    }
                )
    except Exception:
        favorites = []

    return {
        "server_time": now.isoformat(timespec="seconds"),
        "targets": targets,
        "favorites": favorites,
    }


def build_orders_today(app: Any) -> Dict[str, Any]:
    """GET /api/v1/orders/today"""
    now = datetime.now()
    orders: List[Dict[str, Any]] = []
    try:
        dm = getattr(app, "data_manager", None)
        if dm is None:
            return {"server_time": now.isoformat(timespec="seconds"), "orders": []}
        rows = dm.get_today_orders() or []
        for r in rows:
            try:
                orders.append(
                    {
                        "timestamp": str(r[0] if len(r) > 0 else ""),
                        "code": _norm_code(r[1] if len(r) > 1 else ""),
                        "side": str(r[2] if len(r) > 2 else ""),
                        "qty": _safe_int(r[3] if len(r) > 3 else 0),
                        "price": _safe_float(r[4] if len(r) > 4 else 0),
                        "filled_price": _safe_float(r[5] if len(r) > 5 else 0),
                        "filled_qty": _safe_int(r[6] if len(r) > 6 else 0),
                        "result": str(r[7] if len(r) > 7 else ""),
                        "name": str(r[8] if len(r) > 8 else ""),
                        "reason": str(r[9] if len(r) > 9 else ""),
                    }
                )
            except Exception:
                continue
    except Exception:
        orders = []
    return {"server_time": now.isoformat(timespec="seconds"), "orders": orders}


def build_trades_recent(app: Any, limit: int = 30) -> Dict[str, Any]:
    """GET /api/v1/trades/recent"""
    now = datetime.now()
    try:
        lim = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        lim = 30
    portfolio = getattr(app, "portfolio", None)
    hist = _copy_history(portfolio) if portfolio is not None else []

    def _exit_key(t: Dict[str, Any]) -> str:
        for k in ("exit_datetime", "exit_date", "exit_time"):
            v = t.get(k)
            if v:
                return str(v)
        return ""

    hist_sorted = sorted(hist, key=_exit_key, reverse=True)
    trades: List[Dict[str, Any]] = []
    for t in hist_sorted[:lim]:
        code = _norm_code(t.get("code"))
        trades.append(
            {
                "code": code,
                "name": str(t.get("name") or code),
                "sig_type": str(t.get("sig_type") or ""),
                "entry_time": str(t.get("entry_time") or ""),
                "exit_datetime": _exit_key(t),
                "exit_reason": str(t.get("exit_reason") or ""),
                "exit_price": _safe_float(t.get("exit_price")),
                "qty": _safe_int(t.get("qty")),
                "realized_pnl_amt": _safe_float(t.get("realized_pnl_amt")),
                "final_pnl_pct": _safe_float(t.get("final_pnl_pct")),
            }
        )
    return {"server_time": now.isoformat(timespec="seconds"), "trades": trades}


def build_equity_trend(app: Any, days: int = 30) -> Dict[str, Any]:
    """GET /api/v1/equity/trend"""
    now = datetime.now()
    try:
        n = max(1, min(int(days), 365))
    except (TypeError, ValueError):
        n = 30
    end = date.today()
    start = end - timedelta(days=n)
    points: List[Dict[str, Any]] = []
    try:
        from utils.equity_snapshots_store import load_equity_snapshots_range

        _days, recs = load_equity_snapshots_range(start, end)
        for rec in recs:
            if not isinstance(rec, dict):
                continue
            points.append(
                {
                    "date": str(rec.get("date") or "")[:10],
                    "total_assets": _safe_float(
                        rec.get("total_assets", rec.get("total_equity"))
                    ),
                    "d2_deposit": _safe_float(rec.get("d2_deposit")),
                    "stock_eval": _safe_float(rec.get("stock_eval")),
                }
            )
    except Exception:
        points = []
    return {
        "server_time": now.isoformat(timespec="seconds"),
        "days": n,
        "points": points,
    }
