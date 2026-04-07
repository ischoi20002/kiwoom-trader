# -*- coding: utf-8 -*-
"""
지정가 주문 전 참고가(시세) 일관성 검증.
화면 트리 시세와 monitor_info 캐시가 크게 어긋나거나,
주문 기준가가 신뢰할 수 있는 시세와 멀리 떨어져 있으면 주문을 차단합니다.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from utils.normalization import get_tick_size


def _to_float(v: Any) -> float:
    try:
        if v is None or v == "":
            return 0.0
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def monitor_info_cached_price(monitor_info: Optional[Dict[str, Any]], code: str) -> float:
    """monitor_info 에 저장된 종목 시세 (last_price 우선, 없으면 price)."""
    try:
        mi = (monitor_info or {}).get(str(code).zfill(6)) or {}
        lp = _to_float(mi.get("last_price"))
        if lp > 0:
            return lp
        return _to_float(mi.get("price"))
    except Exception:
        return 0.0


def validate_limit_order_reference_price(
    *,
    code: str,
    ref_price: float,
    order_type: str,
    monitor_info: Optional[Dict[str, Any]],
    tree_price: Optional[float],
    settings: Dict[str, Any],
) -> Tuple[bool, str]:
    """
    지정가(order_type 00)일 때만 검사. 통과 시 (True, "").

    - 화면 시세(tree)와 캐시(monitor_info)가 모두 있으면
      '캐시 vs 화면' 허용 틱(넓게)을 넘어 불일치하면 차단.
    - 주문 기준가 ref 는 틱 단가 기준으로 캐시 시세를 우선한 신뢰 시세와
      '참고가 vs 신뢰 시세' 허용 틱(좁게) 안에 있어야 함.
    - 캐시·화면 모두 없으면 차단(지정가 주문 불가).
    """
    try:
        if str(order_type).strip() != "00":
            return True, ""

        if not bool(settings.get("limit_order_price_guard_enabled", True)):
            return True, ""

        try:
            ref = int(round(float(ref_price)))
        except (TypeError, ValueError):
            return False, "주문 기준가가 숫자가 아닙니다."

        if ref <= 0:
            return False, "주문 기준가가 0 이하입니다."

        max_ticks = int(float(settings.get("limit_order_max_tick_deviation", 3)))
        max_ticks = max(1, min(max_ticks, 50))

        cache_vs_tree_ticks = int(
            float(settings.get("limit_order_cache_vs_tree_mismatch_ticks", 10))
        )
        cache_vs_tree_ticks = max(3, min(cache_vs_tree_ticks, 50))

        p_mi = monitor_info_cached_price(monitor_info, code)

        p_tree: Optional[float] = None
        if tree_price is not None:
            try:
                pt = float(tree_price)
                if pt > 0:
                    p_tree = pt
            except (TypeError, ValueError):
                p_tree = None

        # 1) 화면 vs 내부 캐시: 큰 괴리면 둘 중 하나가 낡았거나 경로가 엇갈림
        if p_tree and p_mi and p_tree > 0 and p_mi > 0:
            t_mid = (p_tree + p_mi) / 2.0
            tick_mid = get_tick_size(t_mid)
            max_ct = float(cache_vs_tree_ticks) * tick_mid
            if abs(p_tree - p_mi) > max_ct:
                return (
                    False,
                    f"화면 현재가({p_tree:,.0f}원)와 내부 캐시({p_mi:,.0f}원)가 "
                    f"허용 범위({cache_vs_tree_ticks}호가단위 초과)를 벗어났습니다. "
                    "시세 동기화 후 다시 시도하세요.",
                )

        # 2) 신뢰 시세: 실시간 틱으로 갱신되는 캐시 우선, 없으면 화면만
        trusted: Optional[float] = None
        if p_mi > 0:
            trusted = p_mi
        elif p_tree and p_tree > 0:
            trusted = p_tree

        if trusted is None or trusted <= 0:
            return (
                False,
                "실시간 시세(또는 화면 현재가)를 확인할 수 없어 지정가 주문을 보낼 수 없습니다.",
            )

        tick_tol = get_tick_size(max(float(ref), float(trusted)))
        max_dist = float(max_ticks) * tick_tol
        if abs(float(ref) - float(trusted)) > max_dist:
            return (
                False,
                f"주문 기준가({ref:,}원)와 확인된 시세({trusted:,.0f}원) 차이가 "
                f"허용 범위({max_ticks}호가단위)를 넘습니다. 갱신 후 다시 시도하세요.",
            )

        return True, ""
    except Exception as e:
        return False, f"시세 검증 오류: {e}"
