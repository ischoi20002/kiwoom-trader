# -*- coding: utf-8 -*-
"""
금일 손익용 **기준 총자산** 은 보유별 직전 장마감가로 **순매각 기준** 주식 평가
( ``invested`` + 매도 수수료 차감 미실현, ``utils.fee_rates`` )를 합산한 뒤
현재 ``portfolio``의 D+2 예수와 함께 포지션 탭과 동일한 ``_compute_virtual_tab_total_assets`` 골격으로 합성한다.
금일 손익 비교 시에는 증권사 단일 총평가(±2.5% 치환)를 **쓰지 않고** ``D+2 + 주식 순매각 합`` 만 사용한다.

로컬 JSON(``eod_equity_snapshots_*.json``) 읽기/쓰기 함수는 **과거 호환·수동 점검용**으로만 남긴다.
금일 손익·일일 리포트 요약은 EOD 파일을 **쓰지도 읽지도 않는다**.
"""
from __future__ import annotations

import json
import math
import os
from datetime import date, datetime
from typing import Any, Dict, Optional, Tuple

from reporting.krx_trading_calendar import previous_trading_day
from utils.fee_rates import unrealized_at_price_net_of_sell_fee
from utils.server_mode_paths import eod_equity_snapshots_path


def _portfolio_total_assets_like_virtual_tab_for_synthetic(
    portfolio: Any, stock_eval: float
) -> float:
    """
    포지션 탭 총자산과 동일한 **D+2 예수 + 주식 평가** 골격만 쓴다.
    직전 장마감가 합성 시에는 ``last_tot_evlu_amt`` 와의 ±2.5% 정합 분기를 **쓰지 않는다**
    (당일 증권사 총평가가 기준에 섞이지 않도록).
    """
    try:
        d2 = float(getattr(portfolio, "d2_deposit", 0) or 0)
        try:
            se = float(stock_eval or 0)
        except (TypeError, ValueError):
            se = 0.0
        combined = d2 + se
        if abs(combined) > 1e-6 or se > 0 or abs(d2) > 1e-6:
            return float(combined)
        raw_te = float(getattr(portfolio, "last_tot_evlu_amt", 0) or 0)
        if raw_te > 0:
            return float(raw_te)
        pte = float(getattr(portfolio, "total_equity", 0) or 0)
        if pte > 0:
            return float(pte)
        cash = float(getattr(portfolio, "actual_deposit", 0) or 0)
        if cash > 0:
            return float(cash)
        api_mv = float(getattr(portfolio, "holdings_stocks_mv_api", 0) or 0)
        if api_mv > 0:
            return float(api_mv)
        return 0.0
    except Exception:
        try:
            return float(getattr(portfolio, "total_equity", 0) or 0)
        except Exception:
            return 0.0


def compute_synthetic_prior_total_assets_at_prev_close(app: Any) -> Optional[float]:
    """
    보유 종목별 직전 장마감가( ``fetch_stock_prev_close_ka10001`` )로
    **포지션 탭과 동일한 순매각 주식 합**( ``invested`` + 매도 수수료 차감 미실현 )을 구하고,
    ``VirtualTab._compute_virtual_tab_total_assets(..., skip_broker_te_reconcile=True)`` 로
    **D+2 + 순매각 합** 만 반영한 기준 총자산을 반환한다(금일 손익 분모와 동일 정의).

    안전 규칙:
    - 보유 종목 중 하나라도 직전 장마감가 조회에 실패하면(0/None), 평단으로 대체하지 않고 ``None`` 반환.
      (부분 폴백으로 허위 금일손익이 생성되는 것을 방지)
    """
    try:
        om = getattr(app, "order_manager", None)
        if om is None:
            return None
        fn = getattr(om, "fetch_stock_prev_close_ka10001", None)
        if not callable(fn):
            return None
        portfolio = getattr(app, "portfolio", None)
        if portfolio is None:
            return None
        try:
            settings = dict(getattr(app, "settings", None) or {})
        except Exception:
            settings = {}
        try:
            with portfolio.lock:
                vp = dict(getattr(portfolio, "virtual_positions", None) or {})
        except Exception:
            vp = dict(getattr(portfolio, "virtual_positions", None) or {})
        # 포지션 탭과 동일: 6자리 코드 기준 첫 항목만 집계 (3490 / 003490 이중 키 방지).
        merged_vp: Dict[str, Dict[str, Any]] = {}
        for k_raw, p0 in vp.items():
            try:
                nk = str(k_raw).split("_")[0].zfill(6)
                if nk not in merged_vp:
                    merged_vp[nk] = p0
            except Exception:
                continue

        stock_liquid_prev = 0.0
        for _nk, pos in merged_vp.items():
            try:
                if not isinstance(pos, dict):
                    continue
                qty = int(pos.get("qty", 0) or 0)
                if qty <= 0:
                    continue
                try:
                    ap = float(pos.get("avg_price", 0) or 0)
                except (TypeError, ValueError):
                    ap = 0.0
                code = str(_nk).zfill(6)
                pc: Optional[float] = None
                try:
                    pc = fn(code, silent=True)
                except Exception:
                    pc = None
                # 직전 종가 미조회 시 부분 합성 금지: 허위 금일손익 방지(분모 None 처리)
                if pc is None or float(pc) <= 0:
                    return None
                try:
                    inv = float(pos.get("invested", 0) or 0)
                except (TypeError, ValueError):
                    inv = 0.0
                ur = unrealized_at_price_net_of_sell_fee(
                    pos, float(pc), qty, ap, settings
                )
                stock_liquid_prev += inv + ur
            except Exception:
                continue
        try:
            from ui.tabs.virtual_tab import VirtualTab

            total = VirtualTab._compute_virtual_tab_total_assets(
                portfolio,
                stock_liquid_prev,
                skip_broker_te_reconcile=True,
            )
        except Exception:
            total = _portfolio_total_assets_like_virtual_tab_for_synthetic(
                portfolio, stock_liquid_prev
            )
        if total > 0 and math.isfinite(total):
            return float(total)
        return None
    except Exception:
        return None


def _load_store() -> Dict[str, Any]:
    """스냅샷 JSON 전체(dict). 파손 시 빈 ``by_date``."""
    path = eod_equity_snapshots_path()
    if not os.path.isfile(path):
        return {"by_date": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"by_date": {}}
        bd = data.get("by_date")
        if not isinstance(bd, dict):
            data["by_date"] = {}
        return data
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {"by_date": {}}
    except Exception:
        return {"by_date": {}}


def _save_store(data: Dict[str, Any]) -> None:
    """원자적 저장(임시 파일 후 replace)."""
    path = eod_equity_snapshots_path()
    try:
        base = os.path.dirname(path)
        if base:
            os.makedirs(base, exist_ok=True)
    except OSError:
        pass
    try:
        tmp = f"{path}.tmp"
        out = dict(data)
        out["by_date"] = dict(out.get("by_date") or {})
        out["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass
    except Exception:
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass


def read_eod_total_assets_for_day(trading_day_yyyy_mm_dd: str) -> Optional[float]:
    """[레거시·점검용] 로컬 JSON ``by_date`` 에서만 조회. 금일 손익 계산에는 쓰지 않는다."""
    try:
        key = str(trading_day_yyyy_mm_dd)[:10]
    except (TypeError, ValueError, AttributeError):
        return None
    try:
        raw = (_load_store().get("by_date") or {}).get(key)
        if raw is None:
            return None
        v = float(raw)
        if not math.isfinite(v) or v <= 0:
            return None
        return float(v)
    except (TypeError, ValueError, AttributeError):
        return None
    except Exception:
        return None


def write_eod_total_assets_for_day(trading_day_yyyy_mm_dd: str, total_assets: float) -> None:
    """[레거시] 로컬 JSON에 기록. 앱 자동 경로에서는 더 이상 호출하지 않는다."""
    try:
        key = str(trading_day_yyyy_mm_dd)[:10]
        v = float(total_assets)
    except (TypeError, ValueError, AttributeError):
        return
    if not math.isfinite(v) or v <= 0:
        return
    try:
        st = _load_store()
        bd = dict(st.get("by_date") or {})
        bd[key] = float(v)
        st["by_date"] = bd
        _save_store(st)
    except Exception:
        pass


def read_prev_eod_total_assets_for_calendar_day(cal: date) -> Optional[float]:
    """
    ``cal`` 기준 직전 영업일 EOD JSON 만 조회한다.

    .. note::
        금일 손익 계산은 본 함수를 **사용하지 않는다** (직전 장마감 시세 합성만 사용).
        스크립트·점검용으로만 유지한다.
    """
    try:
        prev = previous_trading_day(cal)
    except Exception:
        prev = None
    if prev is None:
        return None
    return read_eod_total_assets_for_day(prev.strftime("%Y-%m-%d"))


def compute_day_pnl_vs_eod_store(
    app: Any,
    *,
    calendar_day: date,
    current_total_assets: float,
) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[str]]:
    """
    금일 손익(원), 대비(%), 분모 **기준 총자산**(직전 장마감 시세 합성), 직전 영업일 키.

    기준 총자산은 **EOD JSON을 읽지 않고**, 매 호출 ``compute_synthetic_prior_total_assets_at_prev_close`` 로만 취한다.
    (함수 이름은 호환을 위해 유지.)

    Parameters
    ----------
    current_total_assets :
        당일 기준 총자산. **금일 손익 정합**을 위해 ``VirtualTab._compute_virtual_tab_total_assets(..., skip_broker_te_reconcile=True)`` 로 구한 값을 넘긴다.
    """
    try:
        prev = previous_trading_day(calendar_day)
    except Exception:
        prev = None
    if prev is None:
        return None, None, None, None
    prev_key = prev.strftime("%Y-%m-%d")
    try:
        prev_assets = compute_synthetic_prior_total_assets_at_prev_close(app)
    except Exception:
        prev_assets = None
    if prev_assets is None or prev_assets <= 0:
        return None, None, None, prev_key
    try:
        cur = float(current_total_assets)
    except (TypeError, ValueError):
        return None, None, float(prev_assets), prev_key
    if not math.isfinite(cur):
        return None, None, float(prev_assets), prev_key
    try:
        d_amt = cur - float(prev_assets)
        d_pct = (d_amt / float(prev_assets)) * 100.0
        if not math.isfinite(d_pct):
            d_pct = 0.0
        return float(d_amt), float(d_pct), float(prev_assets), prev_key
    except (TypeError, ValueError, ZeroDivisionError):
        return None, None, float(prev_assets), prev_key
    except Exception:
        return None, None, float(prev_assets), prev_key


def maybe_record_eod_after_session(
    app: Any,
    total_assets: float,
    *,
    now: Optional[datetime] = None,
) -> None:
    """
    [폐기] 과거 EOD JSON 자동 기록용이었으나, 금일 손익이 EOD를 쓰지 않으므로 **아무 것도 하지 않는다**.
    호출부·시그니처 호환만 유지.
    """
    return
