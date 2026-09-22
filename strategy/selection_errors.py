# -*- coding: utf-8 -*-
"""종목 선정 관련 예외."""
from __future__ import annotations


class SelectionDbStaleError(Exception):
    """로컬 일봉 DB가 최신 장마감까지 갱신되지 않아 실시간 종목 선정을 중단할 때."""

