# -*- coding: utf-8 -*-
"""
종가 단일가 완성봉 신호를 익일 시초 동시호가 지정가 주문으로 이월하는 서비스.

- 저장: JSON 파일 영속화
- 복구: 앱 시작 시 자동 로드
- 제출: 다음 거래일 개장(SessionHours.open) 직전~직후 창에서 1회 제출
- 만료: 개장+5분 이후까지 미제출/잔존 시 만료 처리
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from reporting.krx_trading_calendar import next_trading_day


class NextOpenPendingActionService:
    """종가·애프터 말봉 신호를 익일 시초 동시호가 지정가로 이월하는 서비스."""

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print
        self._actions: List[Dict[str, Any]] = []
        self._after_id: Any = None
        self._path = Path(__file__).resolve().parents[1] / "next_open_pending_actions.json"
        self.load()

    @staticmethod
    def _now() -> datetime:
        return datetime.now()

    @staticmethod
    def _today() -> date:
        return datetime.now().date()

    @staticmethod
    def reason_prefix_from_slot(source_slot: Any) -> str:
        """``NEXT_OPEN_0900|src=HH:MM`` (슬롯 라벨 시각)."""
        try:
            s = str(source_slot or "").strip()
            if len(s) >= 16 and s[11:13].isdigit() and s[14:16].isdigit():
                return f"NEXT_OPEN_0900|src={s[11:16]}"
        except Exception:
            pass
        try:
            t = datetime.fromisoformat(str(source_slot)[:19].replace(" ", "T"))
            return f"NEXT_OPEN_0900|src={t.strftime('%H:%M')}"
        except Exception:
            return "NEXT_OPEN_0900|src=15:30"

    @staticmethod
    def _slot_label_hm(source_slot: Any) -> str:
        try:
            pfx = NextOpenPendingActionService.reason_prefix_from_slot(source_slot)
            if "|src=" in pfx:
                return pfx.split("|src=", 1)[1]
        except Exception:
            pass
        return "??:??"

    @staticmethod
    def _next_target_day_from(slot_ts: Any) -> Optional[str]:
        try:
            d0 = datetime.strptime(str(slot_ts)[:10], "%Y-%m-%d").date()
            nxt = next_trading_day(d0)
            if nxt is None:
                return None
            return nxt.strftime("%Y-%m-%d")
        except Exception:
            return None

    def _read_json(self) -> List[Dict[str, Any]]:
        try:
            if not self._path.exists():
                return []
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [dict(x) for x in raw if isinstance(x, dict)]
        except Exception:
            pass
        return []

    def save(self) -> None:
        try:
            self._path.write_text(
                json.dumps(self._actions, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            try:
                self.log(f"⚠️ [NextOpen] 대기 액션 저장 실패: {e}")
            except Exception:
                pass

    def load(self) -> None:
        self._actions = self._read_json()
        try:
            if self._actions:
                self.log(f"ℹ️ [NextOpen] 대기 액션 {len(self._actions)}건 복구")
        except Exception:
            pass

    def start_scheduler(self) -> None:
        root = getattr(self.app, "root", None)
        if root is None:
            return

        def _tick() -> None:
            try:
                self.process_due_actions()
            except Exception as e:
                try:
                    self.log(f"⚠️ [NextOpen] 스케줄러 오류: {e}")
                except Exception:
                    pass
            try:
                self._after_id = root.after(1000, _tick)
            except Exception:
                self._after_id = None

        try:
            if self._after_id is not None:
                root.after_cancel(self._after_id)
        except Exception:
            pass
        try:
            self._after_id = root.after(1000, _tick)
        except Exception:
            self._after_id = None

    def stop_scheduler(self) -> None:
        try:
            root = getattr(self.app, "root", None)
            if root is not None and self._after_id is not None:
                root.after_cancel(self._after_id)
        except Exception:
            pass
        self._after_id = None

    def _upsert_action(self, row: Dict[str, Any]) -> None:
        key = (
            str(row.get("code", "")).zfill(6),
            str(row.get("kind", "")),
            str(row.get("target_date", "")),
        )
        kept: List[Dict[str, Any]] = []
        for old in self._actions:
            okey = (
                str(old.get("code", "")).zfill(6),
                str(old.get("kind", "")),
                str(old.get("target_date", "")),
            )
            if okey != key:
                kept.append(old)
        kept.append(row)
        self._actions = kept
        self.save()

    def queue_buy_signal(
        self,
        *,
        code: str,
        name: str,
        sig_type: str,
        limit_price: float,
        source_slot: Any,
    ) -> bool:
        target_date = self._next_target_day_from(source_slot)
        src_hm = self._slot_label_hm(source_slot)
        reason_pfx = self.reason_prefix_from_slot(source_slot)
        if not target_date:
            try:
                self.log(
                    f"⚠️ [NextOpen] {src_hm} 매수 예약 실패 {name}({str(code).zfill(6)}) "
                    f"— 익일 거래일 산출 불가 (slot={source_slot})"
                )
            except Exception:
                pass
            return False
        row = {
            "kind": "buy_signal",
            "code": str(code).zfill(6),
            "name": str(name or ""),
            "sig_type": str(sig_type or "TYPE_A"),
            "limit_price": float(limit_price),
            "reason": reason_pfx,
            "reason_prefix": reason_pfx,
            "source_slot": str(source_slot),
            "target_date": target_date,
            "submitted_at": "",
            "created_at": self._now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._upsert_action(row)
        try:
            self.log(
                f"📌 [NextOpen] {src_hm}→익일시초 매수 예약 "
                f"{row['name']}({row['code']}) {row['sig_type']} "
                f"지정가 {int(round(float(limit_price))):,}원 "
                f"· 제출일 {target_date} · slot={source_slot}"
            )
        except Exception:
            pass
        try:
            sl = getattr(self.app, "session_logger", None)
            if sl is not None and hasattr(sl, "log"):
                sl.log(
                    "next_open_buy_queued",
                    {
                        "code": row["code"],
                        "name": row["name"],
                        "sig_type": row["sig_type"],
                        "limit_price": float(row["limit_price"]),
                        "target_date": target_date,
                        "source_slot": str(source_slot),
                        "reason_prefix": reason_pfx,
                    },
                )
        except Exception:
            pass
        return True

    def queue_strategy_action(
        self,
        *,
        code: str,
        action: str,
        reason: str,
        limit_price: float,
        profit_ratio: float,
        source_slot: Any,
    ) -> bool:
        target_date = self._next_target_day_from(source_slot)
        src_hm = self._slot_label_hm(source_slot)
        reason_pfx = self.reason_prefix_from_slot(source_slot)
        if not target_date:
            try:
                self.log(
                    f"⚠️ [NextOpen] {src_hm} {action} 예약 실패 ({str(code).zfill(6)}) "
                    f"— 익일 거래일 산출 불가 (slot={source_slot})"
                )
            except Exception:
                pass
            return False
        row = {
            "kind": "strategy_action",
            "code": str(code).zfill(6),
            "action": str(action or ""),
            "reason": str(reason or ""),
            "reason_prefix": reason_pfx,
            "limit_price": float(limit_price),
            "profit_ratio": float(profit_ratio),
            "source_slot": str(source_slot),
            "target_date": target_date,
            "submitted_at": "",
            "created_at": self._now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._upsert_action(row)
        try:
            act = str(action or "").upper()
            act_lbl = "청산" if act == "EXIT" else ("추매(S2)" if act == "STAGE2" else act)
            self.log(
                f"📌 [NextOpen] {src_hm}→익일시초 {act_lbl} 예약 "
                f"({row['code']}) {reason or '-'} "
                f"지정가 {int(round(float(limit_price))):,}원 "
                f"· 수익률 {float(profit_ratio):+.2f}% "
                f"· 제출일 {target_date} · slot={source_slot}"
            )
        except Exception:
            pass
        return True

    def _is_submit_window(self, now: datetime) -> bool:
        """익일(또는 당일) 개장 직전~직후 제출 창(open-10초 ~ open+30초)."""
        try:
            from utils.krx_session_hours import get_session_hours

            h = get_session_hours(now.date())
            open_dt = h.open_datetime()
            return (open_dt - timedelta(seconds=10)) <= now <= (open_dt + timedelta(seconds=30))
        except Exception:
            return now.time() >= time(8, 59, 50) and now.time() <= time(9, 0, 30)

    def _is_expire_window(self, now: datetime) -> bool:
        """개장+5분 이후 미제출 만료."""
        try:
            from utils.krx_session_hours import get_session_hours

            h = get_session_hours(now.date())
            return now > (h.open_datetime() + timedelta(minutes=5))
        except Exception:
            return now.time() > time(9, 5, 0)

    def process_due_actions(self) -> None:
        # next_open_1530_enabled 가 꺼져 있어도 큐에 남은 건 제출
        # (실전 NXT 불가 종목 폴백 예약 등)
        if not self._actions:
            return
        now = self._now()
        today_s = now.strftime("%Y-%m-%d")
        changed = False
        kept: List[Dict[str, Any]] = []

        for row in list(self._actions):
            try:
                target_date = str(row.get("target_date", ""))
                submitted = str(row.get("submitted_at", "")).strip()
                if target_date > today_s:
                    kept.append(row)
                    continue
                if target_date < today_s:
                    try:
                        self.log(
                            f"ℹ️ [NextOpen] 기간 만료 폐기 "
                            f"{row.get('kind')} {str(row.get('code','')).zfill(6)} "
                            f"target={target_date}"
                        )
                    except Exception:
                        pass
                    changed = True
                    continue
                if submitted:
                    kept.append(row)
                    continue
                if self._is_submit_window(now):
                    try:
                        self.log(
                            f"🚀 [NextOpen] 09:00 직전 동시호가 제출 시도 "
                            f"{row.get('kind')} {str(row.get('code','')).zfill(6)} "
                            f"지정가 {int(round(float(row.get('limit_price', 0) or 0))):,}원"
                        )
                    except Exception:
                        pass
                    ok = self._submit_one(row)
                    if ok:
                        row["submitted_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
                        try:
                            self.log(
                                f"✅ [NextOpen] 09:00 동시호가 제출 완료 "
                                f"{row.get('kind')} {str(row.get('code','')).zfill(6)} "
                                f"@ {row['submitted_at']}"
                            )
                        except Exception:
                            pass
                        changed = True
                    else:
                        try:
                            self.log(
                                f"⚠️ [NextOpen] 09:00 동시호가 제출 실패 "
                                f"{row.get('kind')} {str(row.get('code','')).zfill(6)} "
                                f"— 창 내 재시도"
                            )
                        except Exception:
                            pass
                        kept.append(row)
                    continue
                if self._is_expire_window(now):
                    try:
                        self.log(
                            f"ℹ️ [NextOpen] 09:05 이후 미제출 만료 "
                            f"{row.get('kind')} {str(row.get('code','')).zfill(6)} "
                            f"target={target_date}"
                        )
                    except Exception:
                        pass
                    changed = True
                    continue
                kept.append(row)
            except Exception:
                kept.append(row)

        self._actions = kept
        if changed:
            self.save()

    def _submit_one(self, row: Dict[str, Any]) -> bool:
        code = str(row.get("code", "")).zfill(6)
        try:
            limit_price = float(row.get("limit_price", 0) or 0)
        except Exception:
            limit_price = 0.0
        if not code or limit_price <= 0:
            return True

        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is None or not hasattr(oes, "mark_next_open_limit_order"):
                return False
            pfx = str(row.get("reason_prefix") or "").strip()
            if not pfx:
                r0 = str(row.get("reason") or "").strip()
                if r0.startswith("NEXT_OPEN_"):
                    pfx = r0
                else:
                    pfx = self.reason_prefix_from_slot(row.get("source_slot"))
            oes.mark_next_open_limit_order(
                code,
                limit_price,
                reason_prefix=pfx,
            )
        except Exception:
            return False

        kind = str(row.get("kind", ""))
        if kind == "buy_signal":
            try:
                return bool(
                    self.app.trading_controller.execute_buy_signal(
                        code,
                        str(row.get("name", "") or code),
                        float(limit_price),
                        reason=str(row.get("sig_type", "TYPE_A")),
                    )
                )
            except Exception:
                return False

        if kind == "strategy_action":
            try:
                self.app.order_execution_service.handle_strategy_action(
                    code,
                    str(row.get("action", "")),
                    str(row.get("reason", "")),
                    now_price=float(limit_price),
                    profit_ratio=float(row.get("profit_ratio", 0.0) or 0.0),
                )
                return True
            except Exception:
                return False
        return True
