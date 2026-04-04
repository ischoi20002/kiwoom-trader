# -*- coding: utf-8 -*-
"""
시장 레짐 1차 보정이 Type A/B 토글과 분리되는지 빠른 정적 검증.

실행: python tools/regime_coupling_audit.py
"""

from __future__ import annotations

import os
import sys

# 프로젝트 루트를 path에 추가
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _main() -> int:
    from strategy.type_a_regime import apply_type_a_phase1_settings
    from strategy.type_b_regime import apply_type_b_phase1_overlay

    base = {
        "use_type_a_regime_phase1": True,
        "use_type_a": False,
        "use_vol_surge": True,
        "vol_surge": 1.5,
        "use_ma60_safe": True,
        "ma60_min": 100.0,
        "ma60_max": 110.0,
        "type_a_phase1_up_vol_surge_add": 0.99,
        "type_a_phase1_neutral_vol_surge_add": 1.99,
        "type_a_phase1_up_ma60_shrink_pct": 5.0,
        "type_a_phase1_neutral_ma60_shrink_pct": 9.0,
        "use_type_b": True,
        "use_type_b_regime_phase1": True,
        "use_type_b_vol": True,
        "vol_surge_b": 2.0,
        "type_b_phase1_up_vol_surge_add": 0.5,
        "type_b_phase1_neutral_vol_surge_add": 0.9,
    }

    err: list[str] = []

    # 1) A 레짐 ON + Type A OFF → vol_surge / ma60 이 원본과 동일해야 함
    for reg in ("UP", "NEUTRAL", "AGGRESSIVE"):
        out = apply_type_a_phase1_settings(base, reg)
        if float(out.get("vol_surge", 0)) != float(base["vol_surge"]):
            err.append(f"type_a: A OFF인데 vol_surge 변경됨 ({reg}) {out.get('vol_surge')}")
        if float(out.get("ma60_min", 0)) != float(base["ma60_min"]):
            err.append(f"type_a: A OFF인데 ma60_min 변경됨 ({reg})")
        if float(out.get("ma60_max", 0)) != float(base["ma60_max"]):
            err.append(f"type_a: A OFF인데 ma60_max 변경됨 ({reg})")

    # 2) B 레짐 ON + Type B OFF → vol_surge_b 불변
    base_b_off = {**base, "use_type_b": False, "use_type_b_regime_phase1": True}
    for reg in ("UP", "NEUTRAL", "AGGRESSIVE"):
        w = dict(base_b_off)
        out = apply_type_b_phase1_overlay(base_b_off, w, reg)
        if float(out.get("vol_surge_b", 0)) != float(base_b_off["vol_surge_b"]):
            err.append(f"type_b: B OFF인데 vol_surge_b 변경됨 ({reg}) {out.get('vol_surge_b')}")

    if err:
        print("[FAIL] 레짐 커플링 검증 실패:")
        for e in err:
            print("  -", e)
        return 1

    print("[OK] Type A/B OFF 시 각 레짐 보정이 수치에 적용되지 않음.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
