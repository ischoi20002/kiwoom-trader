# -*- coding: utf-8 -*-
"""SignalEngine 감시 트리 UI 헬퍼 믹스인 (매매 로직 비포함)."""
from __future__ import annotations

import math
import time
from typing import Any, Dict, Optional, Tuple

import numpy as np

from core.supply_display import format_bid_ask_monitor_display


class SignalMonitorTreeUiMixin:
    def _to_scalar(self, val, default=0):
        """[v4.5.9] 헬퍼: Series/DF/Numpy 등 어떤 형태에서든 안전하게 단일 스칼라 값 추출"""
        if val is None: return default
        # 1. Pandas 객체 처리 (Series, DataFrame)
        if hasattr(val, 'iloc'):
            if hasattr(val, 'size') and val.size == 0: return default
            return self._to_scalar(val.iloc[0], default)
        # 2. Numpy 스칼라 처리
        if hasattr(val, 'item') and not hasattr(val, 'shape'): # 진짜 스칼라인 경우만
            return val.item()
        # 3. 리스트/어레이 처리
        if isinstance(val, (list, np.ndarray)) and len(val) > 0:
            return self._to_scalar(val[0], default)
        return val


    def _parse_ws_vs_prev_close(self, info: dict) -> Tuple[Optional[float], Optional[float]]:
        """
        실시간 0B 패킷 fid 11·12: 전일 종가 대비 (원), 등락률 (% 수치, 부호 포함).
        파싱 실패·미수신 시 (None, None).
        """
        try:
            d_raw = info.get("diff")
            r_raw = info.get("ratio")
            d_f: Optional[float] = None
            r_f: Optional[float] = None
            if d_raw is not None:
                ds = str(d_raw).strip().replace(",", "")
                if ds not in ("", "-", "—"):
                    d_f = float(ds)
            if r_raw is not None:
                rs = (
                    str(r_raw)
                    .strip()
                    .replace("%", "")
                    .replace(",", "")
                )
                if rs not in ("", "-", "—"):
                    r_f = float(rs)
            return (d_f, r_f)
        except (TypeError, ValueError):
            return (None, None)


    def _monitor_tree_ratio_and_direction(
        self,
        info: dict,
        now_price: float,
        intraday_diff: float,
        open_price: float,
    ) -> Tuple[str, float]:
        """
        등락율 열 문자열과 행 방향(양수/음수/0).
        전일 종가 기준 fid 12·11을 우선하고, 없을 때만 시가 대비로 폴백한다.
        (시가 대비 %는 전일대비 '대비' 열과 불일치하는 원인이 됨)
        """
        try:
            d_ws, r_ws = self._parse_ws_vs_prev_close(info)
            # 전일대비(원)을 먼저 쓰면 '대비' 열( fid 11 )과 등락율이 항상 같은 기준이다.
            if d_ws is not None:
                if abs(d_ws) < 1e-12:
                    return ("0.00%", 0.0)
                prev_c = float(now_price) - float(d_ws)
                sg = "▲" if d_ws > 0 else "▼"
                if prev_c > 1e-9:
                    pct = (float(d_ws) / prev_c) * 100.0
                    return (f"{sg} {abs(pct):.2f}%", float(d_ws))
                # 현재가 미확정(0 등)으로 전일종가 역산 불가 — 방향은 diff 부호 유지(보합 오표시 방지)
                if r_ws is not None and abs(r_ws) > 1e-12:
                    return (f"{sg} {abs(r_ws):.2f}%", float(d_ws))
                return ("—", float(d_ws))
            if r_ws is not None:
                if abs(r_ws) < 1e-12:
                    return ("0.00%", 0.0)
                sg = "▲" if r_ws > 0 else "▼"
                return (f"{sg} {abs(r_ws):.2f}%", float(r_ws))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        try:
            op = float(open_price)
            if op > 1e-12:
                pct = (float(intraday_diff) / op) * 100.0
                if abs(pct) < 1e-12:
                    return ("0.00%", 0.0)
                sg = "▲" if intraday_diff > 0 else "▼"
                return (f"{sg} {abs(pct):.2f}%", float(pct))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        return ("0.00%", 0.0)


    @staticmethod
    def monitor_display_price(info: dict, fallback: float) -> float:
        """
        감시 트리 「현재가」 표시용. WS가 먼저 갱신하는 ``price`` 를 ``last_price`` 보다 우선한다.
        """
        for fld in ("price", "last_price"):
            try:
                v = info.get(fld)
                if v in (None, "", 0):
                    continue
                fv = float(v)
                if fv > 0:
                    return fv
            except (TypeError, ValueError):
                continue
        try:
            return float(fallback)
        except (TypeError, ValueError):
            return 0.0


    def ratio_cell_display_for_monitor_tree(
        self, info: dict, now_price: float
    ) -> str:
        """
        감시 트리 「등락률」 열 표시 문자열 (항상 ▲ / ▼ + 수치%).
        ``info['ratio']`` 가 플러스/마이너스 문자열로 왔다 갔다 해도 동일 규칙으로 렌더한다.
        """
        try:
            op = float(self._to_scalar(info.get("open", 0), 0))
            if op <= 1e-12:
                op = float(now_price)
            np_f = float(now_price)
            intraday_diff = np_f - op
            return self._monitor_tree_ratio_and_direction(
                info, np_f, intraday_diff, op
            )[0]
        except Exception:
            return "0.00%"


    def _format_monitor_bid_ask_cell(self, info: dict) -> str:
        """감시 트리 Bid/Ask 열: 매도잔량 / 매수잔량 및 매도÷매수 비율(소수 1자리)."""
        try:
            from core.supply_display import format_bid_ask_monitor_display

            return format_bid_ask_monitor_display(
                self._to_scalar(info.get("total_ask", 0), 0),
                self._to_scalar(info.get("total_bid", 0), 0),
            )
        except Exception:
            return "— / — (—)"


    def _monitor_ui_enqueue_min_interval_sec(self) -> float:
        try:
            v = float(
                (getattr(self.app, "settings", None) or {}).get(
                    "realtime_monitor_ui_enqueue_min_sec", 0.5
                )
            )
        except (TypeError, ValueError):
            v = 0.5
        return max(0.05, min(v, 2.0))


    def _should_enqueue_monitor_tree_ui(
        self,
        nk: str,
        *,
        force: bool = False,
        is_buy: bool = False,
    ) -> bool:
        """가격 틱마다 트리 pending 을 쌓지 않도록 종목별 스로틀."""
        if force or is_buy:
            return True
        try:
            code = str(nk).split("_")[0].zfill(6)
        except Exception:
            return True
        now_t = time.time()
        iv = self._monitor_ui_enqueue_min_interval_sec()
        last_t = float(self._ui_tree_enqueue_last_ts.get(code, 0.0) or 0.0)
        if (now_t - last_t) < iv:
            return False
        self._ui_tree_enqueue_last_ts[code] = now_t
        return True


    def _resolve_monitor_info_dict_key(self, nk: str) -> Optional[str]:
        """
        ``monitor_info`` 의 실제 dict 키를 반환합니다.
        선정 CSV·레거시에서 ``2380`` / ``2380.0`` 등으로 들어간 경우에도
        틱의 canonical(``002380``)과 매칭합니다.
        """
        try:
            mi = getattr(self.app, "monitor_info", None) or {}
            if not nk:
                return None
            if nk in mi:
                return nk
            for k in mi:
                try:
                    if canonical_stock_code(k) == nk:
                        return str(k)
                except Exception:
                    continue
        except Exception:
            pass
        return None

