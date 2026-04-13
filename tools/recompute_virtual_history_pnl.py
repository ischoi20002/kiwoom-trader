# -*- coding: utf-8 -*-
"""
virtual_db_mock.json / virtual_db_live.json (현재 USE_MOCK) 의 매매 종료 내역(history)에 대해,
체결 이벤트 + 현재 settings 수수료율로 invested / realized_pnl_amt / final_pnl_pct 를 재계산합니다.

PortfolioManager.on_fill 과 동일한 규칙:
- BUY_S1: invested = 체결금액 + 매수수수료, avg = 체결가
- BUY_S2: 가중 평단, invested = 평단 * 총수량 (기존 엔진과 동일)
- SELL: net = 매도체결금액 - 매도수수료, 손익 = net - invested

실행: 프로젝트 루트에서  python tools/recompute_virtual_history_pnl.py
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

try:
    from utils.server_mode_paths import migrate_legacy_virtual_db_files, virtual_db_path
except Exception:
    migrate_legacy_virtual_db_files = None  # type: ignore
    virtual_db_path = None  # type: ignore


def _load_json_loose(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            s = f.read()
    except OSError as e:
        raise RuntimeError(f"파일 읽기 실패: {path} ({e})") from e
    s = re.sub(r"\bNaN\b", "null", s, flags=re.IGNORECASE)
    s = re.sub(r"\bInfinity\b", "null", s, flags=re.IGNORECASE)
    return json.loads(s)


def _sanitize_for_json(obj: Any) -> Any:
    """json.dump 가 거부하는 float nan/inf 를 None 으로."""
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_for_json(x) for x in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
    return obj


def _load_settings() -> Dict[str, Any]:
    path = os.path.join(ROOT, "settings.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    try:
        from core.defaults import DEFAULT_SETTINGS

        return dict(DEFAULT_SETTINGS)
    except Exception:
        return {}


def _fee_rates(settings: Dict[str, Any]) -> Tuple[float, float]:
    try:
        from utils.fee_rates import fee_decimal_rates_live_trading

        return fee_decimal_rates_live_trading(settings)
    except Exception:
        return 0.0035, 0.005


def _f(x: Any) -> float:
    try:
        if x is None or x == "":
            return 0.0
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _i(x: Any) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def replay_from_events(
    events: List[Dict[str, Any]], buy_r: float, sell_r: float
) -> Optional[Tuple[float, int, float, float, float, float]]:
    """
    BUY_S1/S2 누적 후 마지막 SELL 1회로 청산한다고 가정.
    반환: (avg_price, qty, invested, exit_price, pnl_amt, pnl_pct)
    """
    if not events:
        return None
    avg: float = 0.0
    qty: int = 0
    invested: float = 0.0
    last_sell_p: float = 0.0
    last_sell_q: int = 0
    for e in events:
        if not isinstance(e, dict):
            continue
        act = str(e.get("action", "") or "").upper()
        if act == "BUY_S1":
            fp = _f(e.get("price"))
            iq = _i(e.get("qty"))
            if fp <= 0 or iq <= 0:
                continue
            fee = fp * float(iq) * buy_r
            invested = fp * float(iq) + fee
            avg, qty = fp, iq
        elif act == "BUY_S2" and qty > 0:
            price = _f(e.get("price"))
            q = _i(e.get("qty"))
            if price <= 0 or q <= 0:
                continue
            oq, oa = qty, avg
            nq = oq + q
            na = ((oa * float(oq)) + (price * float(q))) / float(nq) if nq > 0 else price
            qty = nq
            avg = na
            invested = float(avg) * float(qty)
        elif act == "SELL" and qty > 0:
            last_sell_p = _f(e.get("price"))
            last_sell_q = _i(e.get("qty")) or qty
    if qty <= 0 or last_sell_p <= 0:
        return None
    sf = last_sell_p * float(last_sell_q) * sell_r
    net = last_sell_p * float(last_sell_q) - sf
    pnl = net - invested
    pct = (pnl / invested * 100.0) if abs(invested) > 1e-9 else 0.0
    return (avg, qty, invested, last_sell_p, pnl, pct)


def recompute_position(
    pos: Dict[str, Any], buy_r: float, sell_r: float
) -> Dict[str, Any]:
    """단일 종료 레코드 갱신. SYNC_EXIT 전용·이벤트 없음은 건너뜀."""
    out = dict(pos)
    evs = out.get("events") or []
    if not isinstance(evs, list):
        evs = []

    acts = [str(e.get("action", "") or "").upper() for e in evs if isinstance(e, dict)]
    has_sell = "SELL" in acts
    has_buy = "BUY_S1" in acts or "BUY_S2" in acts
    has_sync_exit_only = (not has_sell) and ("SYNC_EXIT" in acts)

    if has_sync_exit_only:
        return out
    if not has_sell:
        return out

    if has_buy:
        tup = replay_from_events(evs, buy_r, sell_r)
        if tup is None:
            return out
        avg, qty, invested, ex_p, pnl, pct = tup
        out["avg_price"] = float(avg)
        out["qty"] = int(qty)
        out["invested"] = float(invested)
        out["exit_price"] = float(ex_p)
        out["realized_pnl_amt"] = float(pnl)
        out["final_pnl_pct"] = float(pct)
        return out

    # 매도만 있음(SYNCED 등) — 평균매수가·수량으로 매입원(매수수수료 포함) 재구성
    last_sell: Optional[Dict[str, Any]] = None
    for e in reversed(evs):
        if isinstance(e, dict) and str(e.get("action", "") or "").upper() == "SELL":
            last_sell = e
            break
    if last_sell is None:
        return out
    ap = _f(out.get("avg_price"))
    q = _i(out.get("qty"))
    pv = _f(last_sell.get("price", out.get("exit_price")))
    if ap <= 0 or q <= 0 or pv <= 0:
        return out
    gross_b = ap * float(q)
    inv = gross_b + gross_b * buy_r
    sell_fee = pv * float(q) * sell_r
    net = pv * float(q) - sell_fee
    pnl = net - inv
    pct = (pnl / inv * 100.0) if abs(inv) > 1e-9 else 0.0
    out["invested"] = float(inv)
    out["exit_price"] = float(pv)
    out["realized_pnl_amt"] = float(pnl)
    out["final_pnl_pct"] = float(pct)
    return out


def main() -> None:
    try:
        if migrate_legacy_virtual_db_files:
            migrate_legacy_virtual_db_files()
        db_path = virtual_db_path() if virtual_db_path else os.path.join(
            ROOT, "virtual_db_mock.json"
        )
    except Exception:
        db_path = os.path.join(ROOT, "virtual_db_mock.json")
    if not os.path.isfile(db_path):
        print(f"[SKIP] 없음: {db_path}")
        return
    bak = db_path + ".bak_recompute"
    try:
        shutil.copy2(db_path, bak)
        print(f"[백업] {bak}")
    except OSError as e:
        print(f"[오류] 백업 실패: {e}")
        return

    settings = _load_settings()
    buy_r, sell_r = _fee_rates(settings)
    print(
        f"[수수료] buy_r={buy_r:.6f}, sell_r={sell_r:.6f} "
        f"(settings + config.USE_MOCK 반영)"
    )

    try:
        data = _load_json_loose(db_path)
    except Exception as e:
        print(f"[오류] JSON 로드: {e}")
        return
    hist = data.get("history") or []
    if not isinstance(hist, list):
        print("[오류] history 가 배열이 아님")
        return

    new_hist: List[Dict[str, Any]] = []
    for pos in hist:
        if isinstance(pos, dict):
            try:
                new_hist.append(recompute_position(pos, buy_r, sell_r))
            except Exception as e:
                print(f"[경고] 건너뜀 {pos.get('code')}: {e}")
                new_hist.append(pos)
        else:
            new_hist.append(pos)

    data["history"] = new_hist
    data = _sanitize_for_json(data)
    try:
        with open(db_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except OSError as e:
        print(f"[오류] 저장 실패: {e}")
        return
    print(f"[완료] {len(new_hist)}건 재계산 후 저장: {db_path}")


if __name__ == "__main__":
    main()
