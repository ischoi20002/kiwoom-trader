# -*- coding: utf-8 -*-
"""SignalEngine 15:30 / 19:45 슬롯·평가 시각 판별 믹스인 (라우팅 로직 제외)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

import pandas as pd


class SignalSlotTimeHelpersMixin:
    @staticmethod
    def _is_1530_bar_slot(bar_ts: Any) -> bool:
        try:
            from utils.krx_bars import is_krx_last_15m_slot

            return bool(is_krx_last_15m_slot(bar_ts))
        except Exception:
            try:
                t = pd.Timestamp(bar_ts)
                return int(t.hour) == 15 and int(t.minute) == 30
            except Exception:
                return False


    @staticmethod
    def _is_1945_bar_slot(bar_ts: Any) -> bool:
        """애프터 마지막 15분 라벨(기본 19:45)."""
        try:
            from utils.krx_bars import is_krx_aftermarket_last_15m_slot

            return bool(is_krx_aftermarket_last_15m_slot(bar_ts))
        except Exception:
            try:
                t = pd.Timestamp(bar_ts)
                return int(t.hour) == 19 and int(t.minute) == 45
            except Exception:
                return False


    def _current_slot_key_from_bars(self, bars_df: Any) -> str:
        """
        ``bars_data`` 마지막 행 기준 15분 슬롯 키(YYYY-mm-dd HH:MM:SS).

        .. note::
            실시간에서는 ``bars_data`` 가 슬롯마다 갱신되어 마지막 행이 **현재(또는 직전) 슬롯**을 가리킨다.
            장종료까지 통째로 고정된 DataFrame을 넣으면 키가 **항상 당일 마지막 봉**으로 고정되어
            슬롯 게이트·지표 ``iloc[-1]`` 이 백테와 어긋날 수 있으므로, 리플레이·검증 시에는
            ``ts_wall`` 시각까지 잘라 넣는 것이 안전하다.
        """
        try:
            if bars_df is None or getattr(bars_df, "empty", True):
                return ""
            last_idx = bars_df.index[-1]
            return pd.Timestamp(last_idx).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return ""


    def _allow_closed_15m_eval_wall_now(self, bar_ts: Any) -> bool:
        """
        완성15 평가를 허용하는 벽시계 구간인지.

        정규장·애프터마켓(tradable)은 허용.
        ``15:30`` 슬롯은 단일가 확인용으로 당일 15:40까지.
        ``19:45`` 슬롯은 20:00 완성 직후 평가용으로 당일 20:10까지.
        """
        try:
            now = datetime.now()
            if is_krx_tradable_session(now):
                return True
            if int(now.weekday()) >= 5:
                return False
            hm = int(now.hour) * 60 + int(now.minute)
            # 15:30 종가 단일가 그레이스
            if self._eval_1530_slot_enabled() and self._is_1530_bar_slot(bar_ts):
                return (15 * 60 + 30) <= hm <= (15 * 60 + 40)
            # 19:45 애프터 말봉 — 20:00 직후 평가
            if self._next_open_1945_enabled() and self._is_1945_bar_slot(bar_ts):
                return (20 * 60) <= hm <= (20 * 60 + 10)
            return False
        except Exception:
            return True

