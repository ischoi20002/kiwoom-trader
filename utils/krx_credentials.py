# -*- coding: utf-8 -*-
"""
KRX 정보데이터시스템(data.krx.co.kr) 계정 → 환경변수 반영.

pykrx 는 ``KRX_ID`` / ``KRX_PW`` 환경변수로 로그인합니다. (import 시점 1회 + 세션이 없으면
요청 때마다 재조회) 프로세스 환경에 값이 없으면 ``config.py`` 의 ``KRX_ID`` / ``KRX_PW`` 를
옮겨 넣습니다. 이미 환경변수가 있으면 그 값을 우선합니다.
"""

from __future__ import annotations

import os

_ENV_KEYS = ("KRX_ID", "KRX_PW")


def ensure_krx_env() -> bool:
    """KRX_ID/KRX_PW 를 프로세스 환경에 채웁니다. 둘 다 준비되면 True, 실패해도 예외 없음."""
    try:
        if all(os.environ.get(k, "").strip() for k in _ENV_KEYS):
            return True
        try:
            import config  # type: ignore
        except Exception:
            return False
        for k in _ENV_KEYS:
            if os.environ.get(k, "").strip():
                continue
            v = str(getattr(config, k, "") or "").strip()
            if v:
                os.environ[k] = v
        return all(os.environ.get(k, "").strip() for k in _ENV_KEYS)
    except Exception:
        return False
