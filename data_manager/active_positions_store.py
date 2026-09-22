# -*- coding: utf-8 -*-
"""활성 포지션 state DB CRUD 믹스인."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

import sqlite3

from utils.normalization import normalize_code, canonical_stock_code

class ActivePositionsStoreMixin:
    def save_active_position(
        self,
        code,
        name,
        avg_price,
        qty,
        stage,
        invested,
        entry_time,
        reason,
        max_p=None,
        max_p_high=None,
        box_lower=None,
        box_upper=None,
        box_atr_entry=None,
        box_breakdown_closes=None,
        box_last_breakdown_bar=None,
    ):
        """
        활성 포지션 UPSERT.

        ``max_p`` / ``max_p_high`` 미지정 시 기존 DB 값을 유지하고,
        지정 시에는 기존값과 비교해 **더 큰 고점만** 남긴다(재시작·다일 Flash용).
        """
        try:
            avg_f = float(avg_price or 0)
        except (TypeError, ValueError):
            avg_f = 0.0
        with self._lock, self._get_state_connection() as conn:
            prev_mp = 0.0
            prev_mph = 0.0
            try:
                row = conn.execute(
                    "SELECT max_p, max_p_high FROM active_positions WHERE code = ?",
                    (code,),
                ).fetchone()
                if row is not None:
                    try:
                        prev_mp = float(row[0] or 0)
                    except (TypeError, ValueError):
                        prev_mp = 0.0
                    try:
                        prev_mph = float(row[1] or 0)
                    except (TypeError, ValueError):
                        prev_mph = 0.0
            except sqlite3.Error:
                prev_mp, prev_mph = 0.0, 0.0

            def _as_peak(raw: Any, fallback: float) -> float:
                if raw is None:
                    return float(fallback or 0.0)
                try:
                    v = float(raw)
                except (TypeError, ValueError):
                    return float(fallback or 0.0)
                return v if v > 0 else float(fallback or 0.0)

            mp = _as_peak(max_p, prev_mp)
            mph = _as_peak(max_p_high, prev_mph)
            # 평단 이상으로만 고점 유지(초기 진입 시 avg 시드)
            try:
                if avg_f > 0:
                    mp = max(mp, avg_f)
            except Exception:
                pass
            mp = max(float(mp or 0), float(prev_mp or 0))
            mph = max(float(mph or 0), float(prev_mph or 0))

            def _as_box_float(raw: Any) -> float:
                try:
                    value = float(raw or 0)
                    return value if value > 0 else 0.0
                except (TypeError, ValueError):
                    return 0.0

            try:
                breakdown_count = max(0, int(box_breakdown_closes or 0))
            except (TypeError, ValueError):
                breakdown_count = 0
            conn.execute(
                """
                INSERT OR REPLACE INTO active_positions
                (code, name, avg_price, qty, stage, invested, entry_time, reason,
                 status, max_p, max_p_high, box_lower, box_upper, box_atr_entry,
                 box_breakdown_closes, box_last_breakdown_bar)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    name,
                    avg_price,
                    qty,
                    stage,
                    invested,
                    entry_time,
                    reason,
                    mp,
                    mph,
                    _as_box_float(box_lower),
                    _as_box_float(box_upper),
                    _as_box_float(box_atr_entry),
                    breakdown_count,
                    str(box_last_breakdown_bar or ""),
                ),
            )
            conn.commit()


    def update_active_position_box_state(
        self,
        code: str,
        breakdown_closes: int,
        last_breakdown_bar: Optional[str] = None,
    ) -> None:
        """Track C의 연속 하단 이탈 상태를 재시작 이후에도 유지합니다."""
        try:
            count = max(0, int(breakdown_closes or 0))
            bar_key = str(last_breakdown_bar or "")
            with self._lock, self._get_state_connection() as conn:
                conn.execute(
                    """
                    UPDATE active_positions
                    SET box_breakdown_closes = ?, box_last_breakdown_bar = ?
                    WHERE code = ? AND status = 'OPEN'
                    """,
                    (count, bar_key, str(code).zfill(6)),
                )
                conn.commit()
        except (sqlite3.Error, TypeError, ValueError):
            pass


    def update_active_position_peaks(
        self,
        code: str,
        max_p: float,
        max_p_high: Optional[float] = None,
    ) -> None:
        """
        Flash/ATR 고점만 단방향(상승) 갱신.

        평가 루프에서 자주 호출되므로 행이 없으면 no-op.
        """
        try:
            mp = float(max_p or 0)
        except (TypeError, ValueError):
            return
        if mp <= 0:
            return
        try:
            mph = float(max_p_high) if max_p_high is not None else 0.0
        except (TypeError, ValueError):
            mph = 0.0
        try:
            with self._lock, self._get_state_connection() as conn:
                if mph > 0:
                    conn.execute(
                        """
                        UPDATE active_positions
                        SET max_p = MAX(COALESCE(max_p, 0), ?),
                            max_p_high = MAX(COALESCE(max_p_high, 0), ?)
                        WHERE code = ? AND status = 'OPEN'
                        """,
                        (mp, mph, code),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE active_positions
                        SET max_p = MAX(COALESCE(max_p, 0), ?)
                        WHERE code = ? AND status = 'OPEN'
                        """,
                        (mp, code),
                    )
                conn.commit()
        except Exception:
            pass


    def delete_active_position(self, code):
        with self._lock, self._get_state_connection() as conn:
            conn.execute("DELETE FROM active_positions WHERE code = ?", (code,))
            conn.commit()


    def get_active_positions(self):
        """
        OPEN 포지션 목록.

        Returns
        -------
        list[tuple]
            ``(code, name, avg_price, qty, stage, invested, entry_time, reason,
            max_p, max_p_high, box_lower, box_upper, box_atr_entry,
            box_breakdown_closes, box_last_breakdown_bar)``
        """
        with self._get_state_connection() as conn:
            try:
                cursor = conn.execute(
                    "SELECT code, name, avg_price, qty, stage, invested, entry_time, reason, "
                    "COALESCE(max_p, 0), COALESCE(max_p_high, 0), "
                    "COALESCE(box_lower, 0), COALESCE(box_upper, 0), "
                    "COALESCE(box_atr_entry, 0), COALESCE(box_breakdown_closes, 0), "
                    "COALESCE(box_last_breakdown_bar, '') "
                    "FROM active_positions WHERE status='OPEN'"
                )
                return cursor.fetchall()
            except sqlite3.OperationalError:
                # 구 스키마 폴백
                cursor = conn.execute(
                    "SELECT code, name, avg_price, qty, stage, invested, entry_time, reason "
                    "FROM active_positions WHERE status='OPEN'"
                )
                rows = cursor.fetchall()
                return [
                    tuple(r) + (0.0, 0.0, 0.0, 0.0, 0.0, 0, "")
                    for r in rows
                ]

