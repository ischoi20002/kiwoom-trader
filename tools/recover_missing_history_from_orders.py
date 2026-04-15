# -*- coding: utf-8 -*-
"""
order_history(state DB)에서 누락된 매도 건을 읽어 virtual_db JSON 의 history 에 병합합니다.

사용 예:
  python tools/recover_missing_history_from_orders.py --date 2026-04-15
  python tools/recover_missing_history_from_orders.py --date 2026-04-15 --dry-run

--mock / --live 로 state DB 를 고를 수 있으며, 기본은 config.USE_MOCK 과 동일합니다.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from utils.server_mode_paths import virtual_db_path
except Exception:

    def virtual_db_path() -> str:
        return os.path.join(ROOT, "virtual_db_mock.json")


def _f(x: Any) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _i(x: Any) -> int:
    try:
        return int(float(x or 0))
    except (TypeError, ValueError):
        return 0


def _is_sell_side(side: Any) -> bool:
    s = str(side or "").strip().lower()
    return s in ("sell", "s") or "매도" in str(side or "")


def _is_filled_result(result: Any) -> bool:
    r = str(result or "").strip().upper()
    return r in ("FILLED", "SUCCESS", "MOCK")


def _norm_code(c: Any) -> str:
    s = str(c or "").strip().split("_", 1)[0]
    return s.zfill(6) if s else ""


def _fee_rates() -> Tuple[float, float]:
    try:
        from utils.fee_rates import fee_decimal_rates_live_trading

        path = os.path.join(ROOT, "settings.json")
        st: Dict[str, Any] = {}
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                st = json.load(f)
        return fee_decimal_rates_live_trading(st)
    except Exception:
        return 0.0035, 0.005


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


def _history_fingerprint(h: Dict[str, Any]) -> Tuple[str, str, int, int]:
    """중복 검사용 (코드, 청산일, 체결가, 수량)."""
    c = _norm_code(h.get("code"))
    ed = str(h.get("exit_date") or "")[:10]
    try:
        ep = int(round(float(h.get("exit_price") or 0)))
    except Exception:
        ep = 0
    try:
        q = int(h.get("qty") or 0)
    except Exception:
        q = 0
    return (c, ed, ep, q)


def _fetch_sells_for_day(
    state_db: str, day: str
) -> List[Tuple[Any, ...]]:
    if not os.path.isfile(state_db):
        raise FileNotFoundError(f"state DB 없음: {state_db}")
    start = f"{day} 00:00:00"
    end = f"{day} 23:59:59"
    with sqlite3.connect(state_db) as conn:
        cur = conn.execute(
            """
            SELECT timestamp, code, side, qty, price, filled_price, filled_qty, result, name, reason, order_no
            FROM order_history
            WHERE timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC
            """,
            (start, end),
        )
        return list(cur.fetchall())


def _fetch_last_buy_before(
    state_db: str, code: str, before_ts: str
) -> Optional[Tuple[Any, ...]]:
    nc = _norm_code(code)
    alts = {nc}
    try:
        alts.add(str(int(nc)))
    except Exception:
        pass
    placeholders = ",".join("?" * len(alts))
    with sqlite3.connect(state_db) as conn:
        cur = conn.execute(
            f"""
            SELECT timestamp, code, side, qty, price, filled_price, filled_qty, result, name, reason, order_no
            FROM order_history
            WHERE code IN ({placeholders}) AND timestamp < ?
            ORDER BY timestamp DESC
            LIMIT 60
            """,
            (*tuple(alts), before_ts),
        )
        for row in cur.fetchall():
            ts, c, side, qty, price, fp, fq, res, name, reason, ono = row
            if not _is_filled_result(res):
                continue
            if _is_sell_side(side):
                continue
            return row
    return None


def _build_recovered_position(
    sell_row: Tuple[Any, ...],
    buy_row: Optional[Tuple[Any, ...]],
    buy_r: float,
    sell_r: float,
) -> Dict[str, Any]:
    ts, code, side, qty, price, fp, fq, res, name, reason, order_no = sell_row
    code_n = _norm_code(code)
    sell_px = _f(fp if _f(fp) > 0 else price)
    sell_q = _i(fq if _i(fq) > 0 else qty)
    if sell_q <= 0 or sell_px <= 0:
        raise ValueError(f"매도 행 수량/가격 없음: {sell_row}")

    if buy_row:
        _bts, _bc, _bs, bqty, bprice, bfp, bfq, bres, bname, breason, bono = buy_row
        avg_p = _f(bfp if _f(bfp) > 0 else bprice)
        entry_iso = str(buy_row[0])[:19].replace(" ", "T")
        nm = str(bname or name or "").strip() or str(name or "").strip() or code_n
        entry_reason = str(breason or reason or "SIGNAL").strip() or "SIGNAL"
        buy_q = _i(bfq if _i(bfq) > 0 else bqty)
        if buy_q <= 0:
            buy_q = sell_q
    else:
        avg_p = sell_px
        entry_iso = (str(ts)[:10] or "1970-01-01") + "T09:00:00"
        nm = str(name or "").strip() or code_n
        entry_reason = str(reason or "RECOVERED_NO_BUY_ROW").strip()
        buy_q = sell_q

    gross_b = avg_p * float(sell_q)
    invested = gross_b + gross_b * float(buy_r)
    sell_fee = sell_px * float(sell_q) * float(sell_r)
    net = sell_px * float(sell_q) - sell_fee
    pnl_amt = net - invested
    pnl_pct = (pnl_amt / invested * 100.0) if abs(invested) > 1e-9 else 0.0

    exit_dt = str(ts)[:19]
    if len(exit_dt) < 19:
        exit_dt = (str(ts) + " 00:00:00")[:19]

    snap = {
        "time": exit_dt,
        "price": float(sell_px),
        "reason": f"RECOVERED_FROM_DB:{str(order_no or '')[:32]}",
    }

    pos: Dict[str, Any] = {
        "name": nm,
        "avg_price": float(avg_p),
        "qty": int(sell_q),
        "stage": 1,
        "invested": float(invested),
        "entry_time": entry_iso,
        "reason": entry_reason,
        "events": [
            {
                "time": exit_dt,
                "action": "SELL",
                "price": float(sell_px),
                "qty": int(sell_q),
                "snapshot": snap,
            }
        ],
        "max_now": float(avg_p),
        "final_pnl_pct": float(pnl_pct),
        "realized_pnl_amt": float(pnl_amt),
        "exit_price": float(sell_px),
        "exit_date": exit_dt[:10],
        "exit_time": exit_dt[11:19] if len(exit_dt) >= 19 else str(ts)[11:19],
        "exit_datetime": exit_dt,
        "code": code_n,
        "exit_reason": "RECOVERED_ORDER_DB",
    }
    return pos


def _already_in_history(
    hist: List[Dict[str, Any]], fp: Tuple[str, str, int, int]
) -> bool:
    for h in hist:
        if not isinstance(h, dict):
            continue
        if _history_fingerprint(h) == fp:
            return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="order_history → virtual_history 복구")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mock", action="store_true", help="trading_state_mock.db 사용")
    ap.add_argument("--live", action="store_true", help="trading_state_live.db 사용")
    ap.add_argument(
        "--names",
        default="고려아연,LG씨엔에스,미스토홀딩스",
        help="쉼표로 구분한 종목명 부분일치(비우면 해당일 전체 매도 FILLED 후보)",
    )
    args = ap.parse_args()
    day = str(args.date).strip()[:10]
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        print("날짜 형식: YYYY-MM-DD")
        return 2

    if args.mock and args.live:
        print("--mock 과 --live 는 동시에 쓸 수 없습니다.")
        return 2

    if args.mock:
        state_db = os.path.join(ROOT, "trading_state_mock.db")
    elif args.live:
        state_db = os.path.join(ROOT, "trading_state_live.db")
    else:
        try:
            import config as _cfg

            use_m = bool(getattr(_cfg, "USE_MOCK", True))
        except Exception:
            use_m = True
        state_db = os.path.join(
            ROOT, "trading_state_mock.db" if use_m else "trading_state_live.db"
        )

    vpath = virtual_db_path()
    name_subs = [s.strip() for s in str(args.names or "").split(",") if s.strip()]

    buy_r, sell_r = _fee_rates()
    sells = _fetch_sells_for_day(state_db, day)
    candidates: List[Tuple[Any, ...]] = []
    for row in sells:
        if not _is_sell_side(row[2]):
            continue
        if not _is_filled_result(row[7]):
            continue
        nm = str(row[8] or "")
        rs = str(row[9] or "")
        if name_subs:
            blob = nm + " " + rs
            if not any(sub in blob for sub in name_subs):
                continue
        candidates.append(row)

    if not candidates:
        print(f"[복구] 후보 매도 없음: {state_db} / {day} / names={name_subs or '(전체)'}")
        return 1

    data = _load_json(vpath)
    hist: List[Dict[str, Any]] = list(data.get("history") or [])

    added: List[str] = []
    skipped: List[str] = []
    for sell_row in candidates:
        try:
            br = _fetch_last_buy_before(state_db, str(sell_row[1]), str(sell_row[0]))
            pos = _build_recovered_position(sell_row, br, buy_r, sell_r)
        except Exception as e:
            skipped.append(f"{sell_row[1]} 오류: {e}")
            continue
        fp = _history_fingerprint(pos)
        if _already_in_history(hist, fp):
            skipped.append(f"{fp} 이미 history 존재")
            continue
        hist.insert(0, pos)
        added.append(f"{pos['code']} {pos['name']} {fp[1]} @{fp[2]} x{fp[3]}")

    data["history"] = hist

    print(f"[복구] state_db={state_db}")
    print(f"[복구] virtual_db={vpath}")
    print(f"[복구] 추가 {len(added)}건: {added}")
    print(f"[복구] 스킵 {len(skipped)}건:")
    for s in skipped:
        print("  ", s)

    if args.dry_run:
        print("[복구] dry-run - 파일 미저장")
        return 0

    bak = vpath + ".bak_recover"
    try:
        shutil.copy2(vpath, bak)
        print(f"[복구] 백업: {bak}")
    except OSError as e:
        print(f"[복구] 백업 실패(중단): {e}")
        return 3

    try:
        _save_json(vpath, data)
    except OSError as e:
        print(f"[복구] 저장 실패: {e}")
        return 4

    print("[복구] 완료 - 앱 재시작 후 포지션 관리 탭을 확인하세요.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
