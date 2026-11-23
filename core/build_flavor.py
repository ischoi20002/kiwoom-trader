# -*- coding: utf-8 -*-
"""
빌드 구분.

- ``dev``: 개발(c:\\vibe). config.LICENSE_ENFORCE_REAL=False 로 검사 일시 해제 가능.
- ``production``: 운영 배포본. deploy_production 이 이 값을 production 으로 패치하며,
  라이선스 검사 우회 스위치를 무시한다.
"""

from __future__ import annotations

# deploy_production.stage_production 이 운영 트리에서 이 값을 덮어쓴다.
BUILD_FLAVOR: str = "dev"


def is_production_build() -> bool:
    try:
        return str(BUILD_FLAVOR or "").strip().lower() == "production"
    except Exception:
        return False
