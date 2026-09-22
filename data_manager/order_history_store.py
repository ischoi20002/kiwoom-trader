# -*- coding: utf-8 -*-
"""주문 이력·체결 상태 state DB 믹스인."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from utils.normalization import normalize_code, canonical_stock_code


class OrderHistoryStoreMixin:
    def log_order(
        self,
        code,
        side,
        qty,
        price,
        order_type,
        order_no,
        result,
        name="",
        reason="",
        snap_vol_power=None,
        snap_bid_ask=None,
    ):
        """주문 접수 DB 기록. snap_* 는 감시 화면과 동일한 체결강도·호가 문자열 스냅샷."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            code_db = canonical_stock_code(code)
        except Exception:
            code_db = str(code or "").strip().split("_", 1)[0].zfill(6)
        if not code_db or code_db == "000000":
            code_db = str(code or "").strip()
        with self._lock, self._get_state_connection() as conn:
            conn.execute(
                """
                INSERT INTO order_history (
                    code, side, qty, price, order_type, order_no, timestamp, result, name, reason,
                    snap_vol_power, snap_bid_ask
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code_db,
                    side,
                    qty,
                    price,
                    order_type,
                    order_no,
                    now,
                    result,
                    name,
                    reason,
                    snap_vol_power,
                    (snap_bid_ask if snap_bid_ask is not None else ""),
                ),
            )
            conn.commit()


    def _ensure_order_history_exchange_columns_current(self) -> None:
        """기존 state DB 에 order_source / fill_row_key 컬럼이 없을 때 보강."""
        try:
            with self._get_state_connection() as conn:
                for stmt in (
                    "ALTER TABLE order_history ADD COLUMN order_source TEXT DEFAULT ''",
                    "ALTER TABLE order_history ADD COLUMN fill_row_key TEXT DEFAULT ''",
                ):
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError:
                        pass
                try:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_order_history_fillkey "
                        "ON order_history(order_source, fill_row_key)"
                    )
                except sqlite3.Error:
                    pass
                conn.commit()
        except Exception:
            pass


    def merge_exchange_fill_order_rows(self, rows: List[Dict[str, Any]]) -> int:
        """
        키움 kt00007 등에서 가져온 체결 행을 order_history 에 삽입합니다.
        (order_source, fill_row_key) 동일 건은 스킵 — 실전/모의 state DB 각각에 반영됩니다.
        """
        if not rows:
            return 0
        self._ensure_order_history_exchange_columns_current()
        added = 0
        try:
            with self._lock, self._get_state_connection() as conn:
                for r in rows:
                    try:
                        fk = str((r or {}).get("fill_row_key") or "").strip()
                        if not fk:
                            continue
                        dup = conn.execute(
                            "SELECT 1 FROM order_history WHERE order_source = ? AND fill_row_key = ? LIMIT 1",
                            ("KT00007", fk),
                        ).fetchone()
                        if dup:
                            continue
                        conn.execute(
                            """
                            INSERT INTO order_history (
                                code, side, qty, price, order_type, order_no, timestamp, result,
                                name, reason, filled_price, filled_qty,
                                snap_vol_power, snap_bid_ask,
                                order_source, fill_row_key
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(r.get("code") or "").zfill(6),
                                str(r.get("side") or "buy"),
                                int(r.get("qty") or 0),
                                int(r.get("price") or 0),
                                str(r.get("order_type") or "00"),
                                str(r.get("order_no") or ""),
                                str(r.get("timestamp") or ""),
                                str(r.get("result") or "FILLED"),
                                str(r.get("name") or ""),
                                str(r.get("reason") or ""),
                                int(r.get("filled_price") or 0),
                                int(r.get("filled_qty") or 0),
                                r.get("snap_vol_power"),
                                str(r.get("snap_bid_ask") or ""),
                                "KT00007",
                                fk,
                            ),
                        )
                        added += 1
                    except Exception as e:
                        try:
                            print(f"[DataManagerV2] merge_exchange_fill_order_rows 행 스킵: {e}")
                        except Exception:
                            pass
                conn.commit()
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] merge_exchange_fill_order_rows 실패: {e}")
            except Exception:
                pass
            return added
        except Exception as e:
            try:
                print(f"[DataManagerV2] merge_exchange_fill_order_rows 오류: {e}")
            except Exception:
                pass
            return added
        return added


    def get_today_orders(self):
        today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
        with self._get_state_connection() as conn:
            cursor = conn.execute("""
                SELECT timestamp, code, side, qty, price, filled_price, filled_qty, result, name, reason,
                       snap_vol_power, snap_bid_ask, fill_snap_vol_power, fill_snap_bid_ask
                FROM order_history 
                WHERE timestamp >= ? 
                ORDER BY timestamp DESC
            """, (today_start,))
            return cursor.fetchall()


    def get_orders_for_calendar_date(self, day: date) -> List[Tuple[Any, ...]]:
        """
        캘린더 하루치 주문 이력. 오늘은 get_today_orders()와 동일(당일 0시~현재 시각).
        과거 날짜는 해당일 00:00:00 ~ 23:59:59 (표시는 최신순).
        """
        try:
            if day == date.today():
                return self.get_today_orders()
            start_dt = day.strftime("%Y-%m-%d 00:00:00")
            end_dt = day.strftime("%Y-%m-%d 23:59:59")
            with self._get_state_connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT timestamp, code, side, qty, price, filled_price, filled_qty, result, name, reason,
                           snap_vol_power, snap_bid_ask, fill_snap_vol_power, fill_snap_bid_ask
                    FROM order_history
                    WHERE timestamp >= ? AND timestamp <= ?
                    ORDER BY timestamp DESC
                    """,
                    (start_dt, end_dt),
                )
                return list(cursor.fetchall())
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] get_orders_for_calendar_date 실패: {e}")
            except Exception:
                pass
            return []
        except Exception as e:
            try:
                print(f"[DataManagerV2] get_orders_for_calendar_date 오류: {e}")
            except Exception:
                pass
            return []


    def get_distinct_order_calendar_dates(self) -> List[date]:
        """
        order_history 에 레코드가 있는 캘린더 날짜 목록(오름차순, 중복 없음).
        주문 로그 날짜 네비에서 빈 날짜 건너뛰기용.
        """
        try:
            with self._get_state_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT DISTINCT substr(timestamp, 1, 10) AS d
                    FROM order_history
                    WHERE timestamp IS NOT NULL AND length(trim(timestamp)) >= 10
                    ORDER BY d ASC
                    """
                )
                rows = cur.fetchall()
            out: List[date] = []
            for row in rows:
                try:
                    s = str(row[0]).strip()[:10]
                    out.append(datetime.strptime(s, "%Y-%m-%d").date())
                except (TypeError, ValueError, IndexError):
                    continue
            return out
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] get_distinct_order_calendar_dates 실패: {e}")
            except Exception:
                pass
            return []
        except Exception as e:
            try:
                print(f"[DataManagerV2] get_distinct_order_calendar_dates 오류: {e}")
            except Exception:
                pass
            return []


    def get_orders_between(self, start_dt: str, end_dt: str):
        """
        날짜·시간 범위의 주문 이력을 시간순으로 반환합니다.
        (일일 결산 리포트 등 분석용)
        """
        try:
            with self._get_state_connection() as conn:
                cursor = conn.execute(
                    """
                    SELECT timestamp, code, side, qty, price, filled_price, filled_qty, result, name, reason, order_no, order_type,
                           snap_vol_power, snap_bid_ask, fill_snap_vol_power, fill_snap_bid_ask
                    FROM order_history
                    WHERE timestamp >= ? AND timestamp <= ?
                    ORDER BY timestamp ASC
                    """,
                    (start_dt, end_dt),
                )
                return cursor.fetchall()
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] get_orders_between 실패: {e}")
            except Exception:
                pass
            return []


    def get_latest_order_meta(self, code):
        with self._get_state_connection() as conn:
            cursor = conn.execute("""
                SELECT name, reason 
                FROM order_history 
                WHERE code = ? AND (result = 'SUCCESS' OR result = 'MOCK')
                ORDER BY timestamp DESC LIMIT 1
            """, (code,))
            res = cursor.fetchone()
            return res if res else (None, None)


    def _normalize_order_stock_code(self, raw: Any) -> str:
        """order_history.code 와 포트폴리오 6자리 키를 맞추기 위한 정규화."""
        try:
            s = str(raw or "").strip()
            if not s:
                return ""
            if "_" in s:
                s = s.split("_", 1)[0]
            return s.zfill(6)
        except Exception:
            return ""


    def get_last_successful_buy_for_code(self, code: str) -> Optional[Dict[str, Any]]:
        """
        동일 종목의 가장 최근 매수 성공(또는 체결) 건 — 로컬 보유 없이 매도만 반영될 때 원가 추정용.
        """
        try:
            target = self._normalize_order_stock_code(code)
            if not target:
                return None
            with self._get_state_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT timestamp, code, qty, price, filled_price, filled_qty, result, name, reason
                    FROM order_history
                    WHERE lower(side) = 'buy'
                      AND (
                          upper(result) IN ('SUCCESS', 'FILLED', 'MOCK')
                          OR result = 'MOCK'
                      )
                    ORDER BY timestamp DESC
                    LIMIT 120
                    """
                )
                rows = cur.fetchall()
            for row in rows:
                (
                    ts,
                    c,
                    q,
                    p,
                    fp,
                    fq,
                    _res,
                    nm,
                    rs,
                ) = row
                if self._normalize_order_stock_code(c) != target:
                    continue
                try:
                    fq_i = int(fq or 0)
                except (TypeError, ValueError):
                    fq_i = 0
                try:
                    q_i = int(q or 0)
                except (TypeError, ValueError):
                    q_i = 0
                try:
                    fp_i = int(fp or 0)
                except (TypeError, ValueError):
                    fp_i = 0
                try:
                    p_i = int(p or 0)
                except (TypeError, ValueError):
                    p_i = 0
                buy_px = float(fp_i if fp_i > 0 else p_i)
                if buy_px <= 0:
                    continue
                qty_eff = fq_i if fq_i > 0 else q_i
                if qty_eff <= 0:
                    continue
                return {
                    "timestamp": str(ts or ""),
                    "code": str(c or ""),
                    "qty": int(qty_eff),
                    "buy_price": buy_px,
                    "name": str(nm or ""),
                    "reason": str(rs or ""),
                }
            return None
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] get_last_successful_buy_for_code 실패: {e}")
            except Exception:
                pass
            return None
        except Exception as e:
            try:
                print(f"[DataManagerV2] get_last_successful_buy_for_code 오류: {e}")
            except Exception:
                pass
            return None


    def get_latest_buy_reason_with_sig_type(self, code: str) -> Optional[str]:
        """
        해당 종목 매수 이력 중 reason 에 TYPE_A / TYPE_B 가 명시된 가장 최근 건.
        (kt00007 FILLED 등이 최상단이어도 실제 전략 구분이 있는 행을 찾기 위함)
        """
        try:
            target = self._normalize_order_stock_code(code)
            if not target:
                return None
            with self._get_state_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT reason, code
                    FROM order_history
                    WHERE lower(side) = 'buy'
                      AND (
                          upper(result) IN ('SUCCESS', 'FILLED', 'MOCK')
                          OR result = 'MOCK'
                      )
                    ORDER BY timestamp DESC
                    LIMIT 200
                    """,
                )
                rows = cur.fetchall()
            for row in rows:
                try:
                    rs = row[0] if row is not None else None
                    raw_c = row[1] if row is not None and len(row) > 1 else None
                except Exception:
                    continue
                try:
                    if self._normalize_order_stock_code(raw_c) != target:
                        continue
                except Exception:
                    continue
                try:
                    r = str(rs or "").upper()
                except Exception:
                    continue
                if "TYPE_B" in r or "TYPE_A" in r:
                    return str(rs or "")
            return None
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] get_latest_buy_reason_with_sig_type 실패: {e}")
            except Exception:
                pass
            return None
        except Exception as e:
            try:
                print(f"[DataManagerV2] get_latest_buy_reason_with_sig_type 오류: {e}")
            except Exception:
                pass
            return None


    def get_sell_order_meta_by_order_no(self, order_no: str) -> Optional[Dict[str, Any]]:
        """주문번호로 매도 접수 행 조회 — CHE 경로에서 종목명·사유 보강."""
        try:
            oid = str(order_no or "").strip()
            if not oid:
                return None
            with self._get_state_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT name, reason, qty, price, code
                    FROM order_history
                    WHERE order_no = ?
                      AND lower(side) = 'sell'
                    ORDER BY timestamp DESC
                    LIMIT 1
                    """,
                    (oid,),
                )
                row = cur.fetchone()
            if not row:
                return None
            return {
                "name": str(row[0] or ""),
                "reason": str(row[1] or ""),
                "qty": int(row[2] or 0),
                "price": int(row[3] or 0),
                "code": str(row[4] or ""),
            }
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] get_sell_order_meta_by_order_no 실패: {e}")
            except Exception:
                pass
            return None
        except Exception as e:
            try:
                print(f"[DataManagerV2] get_sell_order_meta_by_order_no 오류: {e}")
            except Exception:
                pass
            return None


    def get_order_history_row_by_order_no(
        self, order_no: str
    ) -> Optional[Dict[str, Any]]:
        """order_history에서 order_no 1건을 조회합니다.

        반환 필드(대략):
        - code, side, name, reason
        - price, filled_price, filled_qty
        - result
        - timestamp
        """
        try:
            oid = str(order_no or "").strip()
            if not oid:
                return None
            with self._get_state_connection() as conn:
                row = conn.execute(
                    """
                    SELECT
                        code,
                        side,
                        name,
                        reason,
                        price,
                        filled_price,
                        filled_qty,
                        result,
                        timestamp,
                        qty
                    FROM order_history
                    WHERE order_no = ?
                    ORDER BY
                        CASE
                            WHEN UPPER(TRIM(COALESCE(result, ''))) = 'FILLED'
                                 OR COALESCE(filled_qty, 0) > 0
                            THEN 1
                            ELSE 0
                        END DESC,
                        timestamp DESC,
                        id DESC
                    LIMIT 1
                    """,
                    (oid,),
                ).fetchone()
            if not row:
                return None
            return {
                "code": str(row[0] or ""),
                "side": str(row[1] or ""),
                "name": str(row[2] or ""),
                "reason": str(row[3] or ""),
                "price": float(row[4] or 0),
                "filled_price": float(row[5] or 0),
                "filled_qty": int(row[6] or 0),
                "result": str(row[7] or ""),
                "timestamp": row[8],
                "qty": int(row[9] or 0),
            }
        except Exception:
            return None


    def get_open_sell_orders_for_code(
        self,
        code: str,
        *,
        lookback_days: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        접수(SUCCESS)만 남아 증권사 매도가능수량을 잠그는 매도 주문 목록.

        kt00007·체결 지연·미체결 타임아웃 직후 등으로 로컬 DB에 SUCCESS가 남은 경우
        800033(매도가능수량 부족) 복구 시 취소 대상으로 사용합니다.
        """
        out: List[Dict[str, Any]] = []
        try:
            nc = self._normalize_order_stock_code(code)
            if not nc:
                return out
            lookback_days = max(1, min(int(lookback_days or 5), 30))
            since = (
                datetime.now() - timedelta(days=lookback_days)
            ).strftime("%Y-%m-%d 00:00:00")
            with self._get_state_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT order_no, code, qty, filled_qty, price, timestamp, reason
                    FROM order_history
                    WHERE timestamp >= ?
                      AND lower(trim(COALESCE(side, ''))) = 'sell'
                      AND upper(trim(COALESCE(result, ''))) = 'SUCCESS'
                      AND COALESCE(filled_qty, 0) = 0
                      AND COALESCE(qty, 0) > 0
                      AND trim(COALESCE(order_no, '')) != ''
                    ORDER BY timestamp ASC
                    """,
                    (since,),
                ).fetchall()
            for row in rows or []:
                try:
                    raw_c = self._normalize_order_stock_code(row[1])
                    if raw_c != nc:
                        continue
                    oid = str(row[0] or "").strip()
                    if not oid:
                        continue
                    oq = int(row[2] or 0)
                    if oq <= 0:
                        continue
                    out.append(
                        {
                            "order_no": oid,
                            "code": raw_c,
                            "qty": oq,
                            "filled_qty": int(row[3] or 0),
                            "price": int(float(row[4] or 0)),
                            "timestamp": str(row[5] or ""),
                            "reason": str(row[6] or ""),
                        }
                    )
                except (TypeError, ValueError, IndexError):
                    continue
        except Exception as e:
            try:
                print(f"[DataManagerV2] get_open_sell_orders_for_code 오류: {e}")
            except Exception:
                pass
        return out


    def upsert_order_fill_state_submitted(
        self,
        order_no: str,
        *,
        code: str,
        side: str,
        order_qty: int,
        name: str = "",
        reason: str = "",
    ) -> None:
        """접수 직후 order_fill_state 행을 생성·갱신합니다."""
        try:
            oid = str(order_no or "").strip()
            if not oid:
                return
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self._lock, self._get_state_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO order_fill_state (
                        order_no, code, side, order_qty, exchange_filled_qty,
                        applied_qty, name, reason, updated_at
                    ) VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?)
                    ON CONFLICT(order_no) DO UPDATE SET
                        code = excluded.code,
                        side = excluded.side,
                        order_qty = CASE
                            WHEN excluded.order_qty > 0 THEN excluded.order_qty
                            ELSE order_fill_state.order_qty
                        END,
                        name = CASE
                            WHEN excluded.name != '' THEN excluded.name
                            ELSE order_fill_state.name
                        END,
                        reason = CASE
                            WHEN excluded.reason != '' THEN excluded.reason
                            ELSE order_fill_state.reason
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (
                        oid,
                        str(code or "").zfill(6),
                        str(side or "").lower(),
                        int(order_qty or 0),
                        str(name or ""),
                        str(reason or ""),
                        now,
                    ),
                )
                conn.commit()
        except Exception:
            pass


    def get_order_fill_state(self, order_no: str) -> Optional[Dict[str, Any]]:
        try:
            oid = str(order_no or "").strip()
            if not oid:
                return None
            with self._get_state_connection() as conn:
                row = conn.execute(
                    """
                    SELECT order_no, code, side, order_qty, exchange_filled_qty,
                           applied_qty, name, reason, updated_at
                    FROM order_fill_state WHERE order_no = ?
                    """,
                    (oid,),
                ).fetchone()
            if not row:
                return None
            return {
                "order_no": str(row[0] or ""),
                "code": str(row[1] or ""),
                "side": str(row[2] or ""),
                "order_qty": int(row[3] or 0),
                "exchange_filled_qty": int(row[4] or 0),
                "applied_qty": int(row[5] or 0),
                "name": str(row[6] or ""),
                "reason": str(row[7] or ""),
                "updated_at": row[8],
            }
        except Exception:
            return None


    def increment_order_fill_applied_qty(
        self,
        order_no: str,
        delta: int,
        *,
        exchange_filled_qty: Optional[int] = None,
    ) -> None:
        try:
            oid = str(order_no or "").strip()
            dq = int(delta or 0)
            if not oid or dq <= 0:
                return
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self._lock, self._get_state_connection() as conn:
                if exchange_filled_qty is not None:
                    conn.execute(
                        """
                        UPDATE order_fill_state
                        SET applied_qty = applied_qty + ?,
                            exchange_filled_qty = MAX(exchange_filled_qty, ?),
                            updated_at = ?
                        WHERE order_no = ?
                        """,
                        (dq, int(exchange_filled_qty), now, oid),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE order_fill_state
                        SET applied_qty = applied_qty + ?,
                            updated_at = ?
                        WHERE order_no = ?
                        """,
                        (dq, now, oid),
                    )
                conn.commit()
        except Exception:
            pass


    def increment_order_fill_exchange_qty(self, order_no: str, delta: int) -> None:
        try:
            oid = str(order_no or "").strip()
            dq = int(delta or 0)
            if not oid or dq <= 0:
                return
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self._lock, self._get_state_connection() as conn:
                conn.execute(
                    """
                    UPDATE order_fill_state
                    SET exchange_filled_qty = exchange_filled_qty + ?,
                        updated_at = ?
                    WHERE order_no = ?
                    """,
                    (dq, now, oid),
                )
                conn.commit()
        except Exception:
            pass


    def set_order_fill_exchange_qty(self, order_no: str, cumulative: int) -> None:
        try:
            oid = str(order_no or "").strip()
            cum = int(cumulative or 0)
            if not oid or cum <= 0:
                return
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self._lock, self._get_state_connection() as conn:
                conn.execute(
                    """
                    UPDATE order_fill_state
                    SET exchange_filled_qty = MAX(exchange_filled_qty, ?),
                        updated_at = ?
                    WHERE order_no = ?
                    """,
                    (cum, now, oid),
                )
                conn.commit()
        except Exception:
            pass


    def sum_order_history_filled_qty(self, order_no: str) -> int:
        """당일 동일 주문번호 체결 수량 합(kt00007 다건·부분체결)."""
        try:
            oid = str(order_no or "").strip()
            if not oid:
                return 0
            today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
            with self._get_state_connection() as conn:
                row = conn.execute(
                    """
                    SELECT COALESCE(SUM(filled_qty), 0)
                    FROM order_history
                    WHERE order_no = ?
                      AND timestamp >= ?
                      AND COALESCE(filled_qty, 0) > 0
                    """,
                    (oid, today_start),
                ).fetchone()
            return int(row[0] or 0) if row else 0
        except Exception:
            return 0


    def list_order_fill_states_pending_today(self) -> List[str]:
        """exchange_filled_qty > applied_qty 이거나 applied < order_qty 인 주문번호."""
        try:
            today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
            with self._get_state_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT order_no FROM order_fill_state
                    WHERE updated_at >= ?
                      AND order_qty > 0
                      AND applied_qty < order_qty
                    """,
                    (today_start,),
                ).fetchall()
            return [str(r[0]) for r in rows if r and r[0]]
        except Exception:
            return []


    def remap_order_history_order_no(self, old_no: str, new_no: str) -> bool:
        """
        정정 접수 등으로 주문번호가 바뀐 경우, 동일 주문에 대한 ``order_history`` 행의
        ``order_no`` 를 일괄 치환합니다(미체결 감시·체결동기화 키 정합).
        """
        try:
            o_old = str(old_no or "").strip()
            o_new = str(new_no or "").strip()
            if not o_old or not o_new or o_old == o_new:
                return False
            with self._lock, self._get_state_connection() as conn:
                cur = conn.execute(
                    "UPDATE order_history SET order_no = ? WHERE order_no = ?",
                    (o_new, o_old),
                )
                try:
                    conn.execute(
                        "UPDATE order_fill_state SET order_no = ? WHERE order_no = ?",
                        (o_new, o_old),
                    )
                except sqlite3.Error:
                    pass
                conn.commit()
                return bool(cur.rowcount and cur.rowcount > 0)
        except Exception as e:
            try:
                print(f"[DataManagerV2] remap_order_history_order_no 오류: {e}")
            except Exception:
                pass
            return False


    def get_order_history_row_for_telegram(
        self, code: str, order_no: str
    ) -> Optional[Dict[str, Any]]:
        """
        텔레그램 주문/체결 메시지를 '포지션관리탭 주문·체결 내역' 컬럼과 동일한 값으로 만들기 위한 조회.

        반환 Dict 필드(대략):
        - timestamp, side, name, reason
        - price(주문가), filled_price(체결가), filled_qty(체결수량)
        - snap_vol_power(VP), snap_bid_ask(호가)
        """
        try:
            ncode = normalize_code(str(code or "").strip())
            oid = str(order_no or "").strip()
            if not ncode or not oid:
                return None

            with self._get_state_connection() as conn:
                row = conn.execute(
                    """
                    SELECT
                        timestamp,
                        side,
                        name,
                        reason,
                        price,
                        filled_price,
                        filled_qty,
                        snap_vol_power,
                        snap_bid_ask
                    FROM order_history
                    WHERE code = ?
                      AND order_no = ?
                    ORDER BY timestamp DESC, id DESC
                    LIMIT 1
                    """,
                    (ncode, oid),
                ).fetchone()

            if not row:
                return None

            return {
                "timestamp": row[0],
                "side": row[1],
                "name": row[2],
                "reason": row[3],
                "price": row[4],
                "filled_price": row[5],
                "filled_qty": row[6],
                "snap_vol_power": row[7],
                "snap_bid_ask": row[8],
            }
        except Exception:
            return None


    def update_order_status(
        self,
        code,
        order_no,
        new_status,
        filled_price=0,
        filled_qty=0,
        fill_snap_vol_power=None,
        fill_snap_bid_ask=None,
    ):
        """
        체결 반영 시 fill_snap_* 로 체결 시점 수급 스냅샷을 함께 저장할 수 있습니다.

        - ``code`` 는 로그·표시용이며, **주문번호**가 일치하면 ``canonical_stock_code`` 가
          REST 체결 경로와 다르게 들어와도 갱신되도록 ``code`` 는 WHERE 에 강제하지 않습니다.
        - ``MOCK_ID`` 행은 동일 주문번호가 여러 테스트에 쓰일 수 있어 ``code`` 로 한정합니다.
        """
        sets = ["result = ?", "filled_price = ?", "filled_qty = ?"]
        params: List[Any] = [new_status, filled_price, filled_qty]
        if fill_snap_vol_power is not None:
            sets.append("fill_snap_vol_power = ?")
            params.append(fill_snap_vol_power)
        if fill_snap_bid_ask is not None:
            sets.append("fill_snap_bid_ask = ?")
            params.append(fill_snap_bid_ask)
        today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
        oid = str(order_no or "").strip()
        try:
            nc = canonical_stock_code(str(code or "").strip())
        except Exception:
            try:
                nc = str(code or "").split("_")[0].zfill(6)
            except Exception:
                nc = str(code or "").strip()

        with self._lock, self._get_state_connection() as conn:
            try:
                # 체결수량은 절대 감소시키지 않음(취소·상태 덮어쓰기 시 부분체결 보존)
                try:
                    fq_in = int(filled_qty or 0)
                except (TypeError, ValueError):
                    fq_in = 0
                try:
                    prev = conn.execute(
                        """
                        SELECT COALESCE(MAX(filled_qty), 0), COALESCE(MAX(filled_price), 0)
                        FROM order_history
                        WHERE order_no = ?
                          AND timestamp >= ?
                        """,
                        (oid, today_start),
                    ).fetchone()
                    if prev:
                        fq_in = max(fq_in, int(prev[0] or 0))
                        if int(filled_price or 0) <= 0 and int(prev[1] or 0) > 0:
                            filled_price = int(prev[1] or 0)
                except Exception:
                    pass
                params[1] = filled_price
                params[2] = fq_in
                if oid.upper() == "MOCK_ID":
                    sql = f"""
                        UPDATE order_history
                        SET {", ".join(sets)}
                        WHERE order_no = 'MOCK_ID'
                          AND (code = ? OR code = ?)
                          AND timestamp >= ?
                    """
                    conn.execute(
                        sql,
                        tuple(params + [str(code or "").strip(), nc, today_start]),
                    )
                else:
                    sql = f"""
                        UPDATE order_history
                        SET {", ".join(sets)}
                        WHERE order_no = ?
                          AND timestamp >= ?
                    """
                    conn.execute(sql, tuple(params + [oid, today_start]))
            except sqlite3.Error:
                # 폴백: 구버전과 동일(코드까지 일치)
                try:
                    sql_fb = f"""
                        UPDATE order_history
                        SET {", ".join(sets)}
                        WHERE code = ? AND (order_no = ? OR order_no = 'MOCK_ID')
                        AND timestamp >= ?
                    """
                    conn.execute(
                        sql_fb,
                        tuple(params + [nc, oid, today_start]),
                    )
                except sqlite3.Error:
                    pass
            conn.commit()


    def patch_buy_filled_price_after_reconcile(
        self,
        code: str,
        broker_avg_price: float,
        broker_qty: int,
    ) -> None:
        """
        계좌 동기화(ka10085) 직후 증권사 매입평균과 order_history 를 맞춥니다.

        - 동일 종목 **매수** 행이 DB에 1건일 때만 처리(추매 등 복수 건은 혼합 평단이라 생략).
        - REST 체결 확정 스레드가 늦거나 kt00007 미반영으로 ``result=SUCCESS``(접수)만 남은 경우,
          잔고 수량·주문 수량이 일치하면 **FILLED**·``filled_qty`` 로 승격해 주문·체결 UI와 포지션을 맞춥니다.
        """
        try:
            if broker_qty <= 0 or broker_avg_price <= 0:
                return
            raw = str(code or "").strip()
            if not raw:
                return
            nc = normalize_code(raw)
            if nc.isdigit() and len(nc) < 6:
                nc = nc.zfill(6)
            fp_int = int(round(float(broker_avg_price)))
            candidates: List[str] = []
            for c in (nc, raw.zfill(6) if raw.isdigit() else "", raw):
                if c and c not in candidates:
                    candidates.append(c)
            with self._lock, self._get_state_connection() as conn:
                rows: List[Tuple[Any, ...]] = []
                for cand in candidates:
                    try:
                        cur = conn.execute(
                            """
                            SELECT id,
                                   COALESCE(filled_qty, 0),
                                   COALESCE(qty, 0),
                                   COALESCE(filled_price, 0),
                                   UPPER(TRIM(COALESCE(result, '')))
                            FROM order_history
                            WHERE code = ?
                              AND LOWER(TRIM(COALESCE(side, ''))) = 'buy'
                              AND (
                                UPPER(COALESCE(result, '')) IN ('FILLED', 'SUCCESS', 'MOCK')
                                OR COALESCE(filled_qty, 0) > 0
                              )
                              AND UPPER(COALESCE(result, '')) NOT IN ('CANCELLED', 'REJECTED', 'FAILED')
                            ORDER BY timestamp DESC, id DESC
                            """,
                            (cand,),
                        )
                        rows = cur.fetchall()
                        if rows:
                            break
                    except sqlite3.Error:
                        continue
                # 기존 로직은 buy 행이 1건일 때만 패치했는데,
                # 같은 종목에 대해 접수(SUCCESS)만 남은 buy가 여러 건 누적되면
                # len(rows) != 1 이 되어 패치가 영구히 스킵될 수 있습니다.
                # (예: filled_qty=0이 계속 남아 VP(on_fill/확정)가 늦게/누락되는 케이스)
                selected = None
                broker_qty_i = int(broker_qty)
                for (rid0, fq0, oq0, old_fp0, res_u0) in rows:
                    try:
                        fq_i0 = int(fq0 or 0)
                    except (TypeError, ValueError):
                        fq_i0 = 0
                    try:
                        oq_i0 = int(oq0 or 0)
                    except (TypeError, ValueError):
                        oq_i0 = 0
                    if broker_qty_i <= 0 or oq_i0 <= 0:
                        continue
                    # 접수만 남은 케이스 위주로 선택
                    if fq_i0 > 0:
                        continue
                    if oq_i0 != broker_qty_i:
                        continue
                    res_u_norm0 = str(res_u0 or "").strip().upper()
                    if res_u_norm0 not in ("SUCCESS", "MOCK", ""):
                        continue
                    try:
                        old_fp_i0 = int(old_fp0 or 0)
                    except (TypeError, ValueError):
                        old_fp_i0 = 0
                    if old_fp_i0 > 0:
                        # 가격까지 이미 맞춰진 상태면 굳이 패치할 필요가 없습니다.
                        continue
                    selected = (rid0, fq0, oq0, old_fp0, res_u_norm0)
                    break

                # 매칭되는 후보가 없으면 기존 호환을 위해 rows[0]만 처리(=1건 전제)
                if selected is None:
                    if len(rows) != 1:
                        return
                    rid, fq, oq, old_fp, res_u = rows[0]
                else:
                    rid, fq, oq, old_fp, res_u = selected
                try:
                    fq_i = int(fq or 0)
                except (TypeError, ValueError):
                    fq_i = 0
                try:
                    oq_i = int(oq or 0)
                except (TypeError, ValueError):
                    oq_i = 0
                eff_q = fq_i if fq_i > 0 else oq_i
                if eff_q > 0 and eff_q != int(broker_qty):
                    return
                try:
                    old_fp_i = int(old_fp or 0)
                except (TypeError, ValueError):
                    old_fp_i = 0
                # 접수(SUCCESS/MOCK)만 남고 잔고와 수량이 맞으면 체결 확정으로 승격
                try:
                    need_fill_status = (
                        str(res_u or "").strip().upper() in ("SUCCESS", "MOCK", "")
                        and fq_i <= 0
                        and oq_i > 0
                        and int(broker_qty) == oq_i
                    )
                except Exception:
                    need_fill_status = False

                if need_fill_status:
                    conn.execute(
                        """
                        UPDATE order_history
                        SET filled_price = ?, filled_qty = ?, result = 'FILLED'
                        WHERE id = ?
                        """,
                        (fp_int, int(broker_qty), int(rid)),
                    )
                    conn.commit()
                    return

                if old_fp_i == fp_int:
                    return
                conn.execute(
                    "UPDATE order_history SET filled_price = ? WHERE id = ?",
                    (fp_int, int(rid)),
                )
                conn.commit()
        except Exception as e:
            try:
                print(f"[DataManagerV2] patch_buy_filled_price_after_reconcile: {e}")
            except Exception:
                pass


    def patch_market_sell_success_when_not_in_holdings(
        self,
        held_codes: Any,
        *,
        ambiguous_zero_holdings: bool = False,
        patched_rows_out: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        """
        ka10085 동기화 직후: 잔고에 없는 종목인데 매도가 ``SUCCESS``(접수)만 남은 행을
        ``FILLED`` 로 승격합니다.

        - **시장가(03)** 뿐 아니라 **지정가(00)**·터치호가 매도도 동일하게 보정합니다.
          (``kt00007``/CHE 지연으로 체결 행만 늦고 잔고는 이미 빠진 경우.)
        - ``ambiguous_zero_holdings=True`` (보유 API 0건인데 로컬 포지션만 있는 등)일 때는
          잔고 응답이 불확실하므로 **아무 것도 하지 않습니다**.
        - ``patched_rows_out`` 이 리스트이면 승격한 행 ``dict`` 를 append(포지션 on_fill 연동용).
        """
        if ambiguous_zero_holdings:
            return 0
        try:
            hc: Set[str] = set()
            for x in held_codes or set():
                nc = self._normalize_order_stock_code(x)
                if nc:
                    hc.add(nc)
        except Exception:
            hc = set()
        today_start = datetime.now().strftime("%Y-%m-%d 00:00:00")
        patched = 0
        try:
            with self._lock, self._get_state_connection() as conn:
                cur = conn.execute(
                    """
                    SELECT id, code, qty, price, order_no, name, reason
                    FROM order_history
                    WHERE timestamp >= ?
                      AND lower(trim(COALESCE(side, ''))) = 'sell'
                      AND upper(trim(COALESCE(result, ''))) = 'SUCCESS'
                      AND COALESCE(filled_qty, 0) = 0
                      AND COALESCE(qty, 0) > 0
                      AND trim(COALESCE(order_type, '')) IN ('03', '3', '00', '0', '')
                    ORDER BY id ASC
                    """,
                    (today_start,),
                )
                rows = cur.fetchall()
                for row in rows or []:
                    try:
                        rid = int(row[0])
                        raw_c = row[1]
                        oq = int(row[2] or 0)
                        op = int(float(row[3] or 0))
                        ord_no = str(row[4] or "").strip()
                        nm = str(row[5] or "")
                        rs = str(row[6] or "")
                    except (TypeError, ValueError, IndexError):
                        continue
                    nc = self._normalize_order_stock_code(raw_c)
                    if not nc or nc in hc:
                        continue
                    fp = op if op > 0 else 0
                    try:
                        conn.execute(
                            """
                            UPDATE order_history
                            SET result = 'FILLED',
                                filled_qty = ?,
                                filled_price = CASE WHEN ? > 0 THEN ? ELSE COALESCE(price, 0) END
                            WHERE id = ?
                            """,
                            (oq, fp, fp, rid),
                        )
                        patched += 1
                        if isinstance(patched_rows_out, list):
                            patched_rows_out.append(
                                {
                                    "code": nc,
                                    "qty": int(oq),
                                    "price": float(fp if fp > 0 else op),
                                    "order_no": ord_no,
                                    "name": nm,
                                    "reason": rs,
                                }
                            )
                    except sqlite3.Error:
                        continue
                conn.commit()
        except sqlite3.Error as e:
            try:
                print(f"[DataManagerV2] patch_market_sell_success_when_not_in_holdings: {e}")
            except Exception:
                pass
        except Exception as e:
            try:
                print(f"[DataManagerV2] patch_market_sell_success_when_not_in_holdings 오류: {e}")
            except Exception:
                pass
        return patched

