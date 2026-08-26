# -*- coding: utf-8 -*-
"""
감시 재시작 직후: 직전 완성슬롯 catchup 평가 **이전**에
계좌·체결·미체결을 맞춰 중복 주문을 줄입니다.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Set

from utils.normalization import canonical_stock_code


def _log(app: Any, msg: str) -> None:
    try:
        fn = getattr(app, "log", None)
        if callable(fn):
            fn(msg)
    except Exception:
        pass


def _sync_account_blocking(app: Any) -> bool:
    """ka10085 보유 동기화(워밍업 스레드에서 블로킹·완료까지 대기)."""
    pf = getattr(app, "portfolio", None)
    if pf is None:
        return False
    lock = getattr(app, "_portfolio_sync_thread_lock", None)
    acquired = False
    p_acquired = False
    plock = getattr(pf, "_sync_in_progress_lock", None)
    try:
        if lock is not None:
            try:
                acquired = bool(lock.acquire(timeout=120.0))
            except Exception:
                acquired = False
            if not acquired:
                _log(
                    app,
                    "⏳ [RestartGuard] 계좌 동기화 앱락 대기 실패 — catchup 전 동기화 생략",
                )
                return False
        # PortfolioManager 내부 락은 non-blocking 이므로 여기서 대기형으로 잡음
        if plock is not None:
            try:
                p_acquired = bool(plock.acquire(timeout=120.0))
            except Exception:
                p_acquired = False
            if not p_acquired:
                _log(
                    app,
                    "⏳ [RestartGuard] 계좌 동기화 포트폴리오락 대기 실패",
                )
                return False
            if hasattr(pf, "_sync_with_actual_account_impl"):
                pf._sync_with_actual_account_impl(
                    silent=True,
                    force=True,
                    refresh_cash_deposit=True,
                    schedule_gui_refresh=True,
                    schedule_orders_refresh=True,
                    sync_context="restart_pre_catchup",
                )
            else:
                pf.sync_with_actual_account(
                    silent=True,
                    force=True,
                    refresh_cash_deposit=True,
                    schedule_gui_refresh=True,
                    schedule_orders_refresh=True,
                    sync_context="restart_pre_catchup",
                )
        else:
            pf.sync_with_actual_account(
                silent=True,
                force=True,
                refresh_cash_deposit=True,
                schedule_gui_refresh=True,
                schedule_orders_refresh=True,
                sync_context="restart_pre_catchup",
            )
        return True
    except Exception as e:
        _log(app, f"⚠️ [RestartGuard] 계좌 동기화 실패: {e}")
        return False
    finally:
        if p_acquired and plock is not None:
            try:
                plock.release()
            except Exception:
                pass
        if acquired and lock is not None:
            try:
                lock.release()
            except Exception:
                pass


def _sync_today_exchange_fills(app: Any) -> int:
    """당일 kt00007 체결을 order_history에 병합."""
    try:
        om = getattr(app, "order_manager", None)
        dm = getattr(app, "data_manager", None)
        if om is None or dm is None:
            return 0
        if not hasattr(om, "sync_exchange_fills_into_database"):
            return 0
        return int(
            om.sync_exchange_fills_into_database(
                dm,
                dates=[date.today()],
                silent=True,
                logger=getattr(app, "log", None),
            )
            or 0
        )
    except Exception as e:
        _log(app, f"⚠️ [RestartGuard] kt00007 체결 병합 실패: {e}")
        return 0


def _fetch_ka10075_unfilled(
    app: Any, *, trde_tp: str
) -> List[Dict[str, Any]]:
    """
    ka10075 미체결 목록.

    trde_tp: ``0`` 전체, ``1`` 매도, ``2`` 매수.
    """
    out: List[Dict[str, Any]] = []
    try:
        om = getattr(app, "order_manager", None)
        api = getattr(om, "account_api", None) if om is not None else None
        if api is None or not hasattr(api, "unfilled_orders_request_ka10075"):
            return out
        import config

        use_mock = bool(getattr(config, "USE_MOCK", False))
        stex = "1" if use_mock else "0"
        res = api.unfilled_orders_request_ka10075(
            all_stk_tp="0",
            trde_tp=str(trde_tp),
            stex_tp=stex,
        )
        if not isinstance(res, dict):
            return out
        try:
            rc = res.get("return_code", 0)
            if rc is not None and int(str(rc).strip()) != 0:
                return out
        except (TypeError, ValueError):
            pass
        oso = res.get("oso") or []
        if not isinstance(oso, list):
            return out
        for row in oso:
            if not isinstance(row, dict):
                continue
            try:
                nk = canonical_stock_code(
                    str(row.get("stk_cd") or row.get("pdno") or "")
                )
            except Exception:
                nk = ""
            if not nk:
                continue
            try:
                oq = int(
                    float(
                        str(row.get("oso_qty") or row.get("ord_qty") or 0)
                        .replace(",", "")
                        .strip()
                        or 0
                    )
                )
            except (TypeError, ValueError):
                oq = 0
            if oq <= 0:
                continue
            out.append(
                {
                    "code": nk,
                    "order_no": str(row.get("ord_no") or "").strip(),
                    "oso_qty": oq,
                }
            )
    except Exception as e:
        _log(app, f"⚠️ [RestartGuard] ka10075(trde={trde_tp}) 실패: {e}")
    return out


def _restore_s1_buy_pending(app: Any, rows: List[Dict[str, Any]]) -> Set[str]:
    """미체결 매수 종목을 s1_buy_pending_codes에 복구."""
    restored: Set[str] = set()
    try:
        oes = getattr(app, "order_execution_service", None)
        if oes is None:
            tc = getattr(app, "trading_controller", None)
            oes = getattr(tc, "order_execution_service", None) if tc else None
        lows = getattr(oes, "limit_order_watch_service", None) if oes else None
        if lows is None:
            return restored
        pending = getattr(lows, "s1_buy_pending_codes", None)
        if not isinstance(pending, set):
            return restored
        for row in rows:
            try:
                nk = canonical_stock_code(str(row.get("code") or ""))
            except Exception:
                nk = ""
            if not nk:
                continue
            pending.add(nk)
            restored.add(nk)
    except Exception as e:
        _log(app, f"⚠️ [RestartGuard] S1 pending 복구 실패: {e}")
    return restored


def _mark_exit_pending_for_unfilled_sells(
    app: Any, rows: List[Dict[str, Any]]
) -> Set[str]:
    """미체결 매도 종목의 virtual_positions.exit_pending 복구."""
    marked: Set[str] = set()
    try:
        pf = getattr(app, "portfolio", None)
        vp = getattr(pf, "virtual_positions", None) if pf is not None else None
        if not isinstance(vp, dict):
            return marked
        for row in rows:
            try:
                nk = canonical_stock_code(str(row.get("code") or ""))
            except Exception:
                nk = ""
            if not nk:
                continue
            pos = None
            for k in list(vp.keys()):
                try:
                    if canonical_stock_code(str(k)) == nk:
                        pos = vp.get(k)
                        break
                except Exception:
                    continue
            if not isinstance(pos, dict):
                continue
            pos["exit_pending"] = True
            marked.add(nk)
    except Exception as e:
        _log(app, f"⚠️ [RestartGuard] exit_pending 복구 실패: {e}")
    return marked


def prepare_before_restart_slot_catchup(app: Any) -> Dict[str, Any]:
    """
    재시작 catchup 평가 직전 가드.

    1) 계좌(보유) 동기화
    2) 당일 체결(kt00007) DB 병합
    3) 미체결 매수 → S1 pending 복구
    4) 미체결 매도 → exit_pending 표시
    """
    report: Dict[str, Any] = {
        "account_synced": False,
        "fills_merged": 0,
        "s1_pending": [],
        "exit_pending": [],
    }
    try:
        report["account_synced"] = bool(_sync_account_blocking(app))
    except Exception:
        report["account_synced"] = False
    try:
        report["fills_merged"] = int(_sync_today_exchange_fills(app) or 0)
    except Exception:
        report["fills_merged"] = 0
    # 체결 반영 후 보유가 늦게 잡히는 경우 대비 한 번 더 짧게 동기화
    if int(report.get("fills_merged") or 0) > 0:
        try:
            _sync_account_blocking(app)
            report["account_synced"] = True
        except Exception:
            pass
    try:
        buy_rows = _fetch_ka10075_unfilled(app, trde_tp="2")
        restored = _restore_s1_buy_pending(app, buy_rows)
        report["s1_pending"] = sorted(restored)
    except Exception:
        report["s1_pending"] = []
    try:
        sell_rows = _fetch_ka10075_unfilled(app, trde_tp="1")
        marked = _mark_exit_pending_for_unfilled_sells(app, sell_rows)
        report["exit_pending"] = sorted(marked)
    except Exception:
        report["exit_pending"] = []
    try:
        _log(
            app,
            "ℹ️ [RestartGuard] catchup 전 동기화 "
            f"account={report['account_synced']} "
            f"fills+={report['fills_merged']} "
            f"s1_pending={len(report['s1_pending'])} "
            f"exit_pending={len(report['exit_pending'])}"
        )
    except Exception:
        pass
    return report
