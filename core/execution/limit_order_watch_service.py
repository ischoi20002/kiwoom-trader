# -*- coding: utf-8 -*-
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class LimitOrderMeta:
    """미체결 지정가 타임아웃 예약 메타(단일 주문번호 기준)."""

    code: str
    kind: str
    after_id: Any
    qty: int = 0
    # False: 시장가 등 — 타임아웃 시 취소 API 없음(체결 시 clear()로만 정리)
    cancel_on_timeout: bool = True
    # --- 터치호가(매수=매도1호가, 매도=매수1호가) 정정 루프 ---
    touch_mode: bool = False
    touch_chain_key: str = ""
    touch_side: str = ""
    touch_name: str = ""
    touch_reason: str = ""
    touch_amend_attempts_done: int = 0
    # kt00007/DB 체결확인 결과가 pending으로 계속 나오는 동안은 너무 이른 정정(amend)을 피하기 위한 카운터
    touch_pending_confirm_cnt: int = 0
    # True: (정정 시도 1회) 횟수 소진으로 카운트
    # False: 체결 동기화/DB 반영 대기 목적(예: RC4042)이라면 소진하지 않음
    touch_amend_should_increment: bool = True
    fill_price_hint: float = 0.0
    # "confirm": kt00007 체결 확인만 / "amend": 호가 정정만 (확인 후 재시도 간격 대기)
    touch_poll_phase: str = "confirm"
    touch_pending_logged: bool = False
    # 접수 시각(time.time) — 취소 프로브 최소 대기 산정용
    registered_at: float = 0.0
    # 15:15 동시호가 시장가 — 취소/정정 없이 kt00007 폴링만
    auction_watch: bool = False


class LimitOrderWatchService:
    """
    지정가 미체결 타임아웃 감시/취소/플래그 정리 서비스.

    - 등록: `root.after(ms, cb)` 로 타임아웃 콜백 예약(지정가)
    - 시장가: 메타만 등록하고 타이머·취소 API는 생략 — 체결 시 `clear()` 로 플래그 정리
    - 취소(체결): `clear(order_no)` 로 예약 타이머 취소 + 상태 정리
    - 타임아웃: `timeout(order_no)` 로 취소 API 호출 + 상태 정리
    """

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print

        # 상태: (TradingController 내부에 있던 것) -> 서비스로 이동
        self.limit_order_meta: Dict[str, LimitOrderMeta] = {}
        # S1 지정가 접수 후 체결 전, 동일 종목 중복 매수 방지
        self.s1_buy_pending_codes: Set[str] = set()

        # timer 콜백과 clear 동시 경합 완화
        self._lock = threading.RLock()

    def _get_root(self) -> Optional[Any]:
        try:
            return getattr(self.app, "root", None)
        except Exception:
            return None

    def _get_timeout_ms(self) -> int:
        try:
            sec = float(
                (getattr(self.app, "settings", None) or {}).get(
                    "limit_order_unfilled_timeout_sec", 45.0
                )
            )
        except (TypeError, ValueError):
            sec = 45.0
        sec = max(2.0, min(sec, 120.0))
        return int(sec * 1000)

    def _get_touch_first_poll_ms(self) -> int:
        """
        접수·정정 직후 첫 kt00007 체결 확인까지 대기(ms).

        접수 후 30·40초 조회는 kt00007 미반영인 경우가 많아 기본 60초로 둔다.
        이후 재확인은 ``touch_quote_order_poll_sec``(기본 30초) 간격이다.
        """
        try:
            st = getattr(self.app, "settings", None) or {}
            sec = float(st.get("touch_quote_first_confirm_sec", 60.0))
        except (TypeError, ValueError):
            sec = 60.0
        sec = max(10.0, min(sec, 90.0))
        return int(sec * 1000)

    def _get_touch_poll_ms(self) -> int:
        """터치호가 미반영 재확인·정정 후 확인 간격(ms)."""
        try:
            sec = float(
                (getattr(self.app, "settings", None) or {}).get(
                    "touch_quote_order_poll_sec", 30.0
                )
            )
        except (TypeError, ValueError):
            sec = 30.0
        sec = max(2.0, min(sec, 60.0))
        return int(sec * 1000)

    def _get_touch_max_amends(self) -> int:
        try:
            n = int(
                (getattr(self.app, "settings", None) or {}).get(
                    "touch_quote_order_max_amends", 3
                )
            )
        except (TypeError, ValueError):
            n = 3
        return max(1, min(n, 10))

    def _get_auction_poll_ms(self) -> int:
        """동시호가 시장가 kt00007 확인 간격(기본 30초). 15:30 이후부터 적용."""
        try:
            sec = float(
                (getattr(self.app, "settings", None) or {}).get(
                    "auction_market_order_poll_sec", 30.0
                )
            )
        except (TypeError, ValueError):
            sec = 30.0
        sec = max(5.0, min(sec, 120.0))
        return int(sec * 1000)

    def _auction_fill_confirm_start_dt(self, now: Optional[datetime] = None) -> datetime:
        """
        종가 동시호가 체결 확인 시작 시각(당일 SessionHours.close).

        동시호가 직전 슬롯 완성 후 시장가 접수는 가능하지만,
        continuous_end~close 사이에는 체결이 나지 않으므로 조회는 close부터.
        """
        t = now if isinstance(now, datetime) else datetime.now()
        try:
            from utils.krx_session_hours import get_session_hours

            return get_session_hours(t.date()).close_datetime()
        except Exception:
            return t.replace(hour=15, minute=30, second=0, microsecond=0)

    def _ms_until_auction_fill_confirm_start(
        self, now: Optional[datetime] = None
    ) -> int:
        """종가(close)까지 남은 ms. 이미 지났으면 0."""
        try:
            t = now if isinstance(now, datetime) else datetime.now()
            start = self._auction_fill_confirm_start_dt(t)
            if t >= start:
                return 0
            return max(0, int((start - t).total_seconds() * 1000))
        except Exception:
            return 0

    def _auction_fill_confirm_window_open(self, now: Optional[datetime] = None) -> bool:
        """체결 확인 가능 구간: close 이상."""
        try:
            t = now if isinstance(now, datetime) else datetime.now()
            return t >= self._auction_fill_confirm_start_dt(t)
        except Exception:
            return True

    def _auction_watch_deadline_passed(self) -> bool:
        """로컬 시각 기준 close+3분(기본 15:33) 이후면 폴링 종료."""
        try:
            from utils.krx_session_hours import get_session_hours

            now = datetime.now()
            close_dt = get_session_hours(now.date()).close_datetime()
            return now >= (close_dt + timedelta(minutes=3))
        except Exception:
            try:
                now = datetime.now()
                return (now.hour > 15) or (now.hour == 15 and now.minute >= 33)
            except Exception:
                return False

    def _schedule_auction_poll(
        self, order_no: str, *, delay_ms: Optional[int] = None
    ) -> None:
        """동시호가 폴링 재예약(15:30 전이면 15:30까지 대기)."""
        oid = str(order_no or "").strip()
        if not oid:
            return
        root = self._get_root()
        if root is None:
            return
        try:
            if delay_ms is None:
                wait_start = self._ms_until_auction_fill_confirm_start()
                if wait_start > 0:
                    ms = int(wait_start)
                else:
                    ms = self._get_auction_poll_ms()
            else:
                ms = max(0, int(delay_ms))
        except Exception:
            ms = self._get_auction_poll_ms()

        def _acb() -> None:
            def _worker() -> None:
                try:
                    self._on_auction_poll(oid)
                except Exception as e:
                    try:
                        self.log(f"⚠️ [동시호가] 폴링 오류: {e}")
                    except Exception:
                        pass

            try:
                threading.Thread(target=_worker, daemon=True).start()
            except Exception:
                try:
                    self._on_auction_poll(oid)
                except Exception:
                    pass

        try:
            with self._lock:
                meta = self.limit_order_meta.get(oid)
                if meta is None or not bool(getattr(meta, "auction_watch", False)):
                    return
                aid = root.after(ms, _acb)
                meta.after_id = aid
        except Exception:
            pass

    def _on_auction_poll(self, order_no: str) -> None:
        """동시호가 시장가: 15:30~15:33 kt00007 체결 확인만(취소·정정 없음)."""
        oid = str(order_no or "").strip()
        if not oid:
            return
        meta: Optional[LimitOrderMeta] = None
        try:
            with self._lock:
                meta = self.limit_order_meta.get(oid)
        except Exception:
            meta = None
        if meta is None or not bool(getattr(meta, "auction_watch", False)):
            return

        # 15:20~15:30 사이 조기 타이머가 있어도 조회하지 않음
        if not self._auction_fill_confirm_window_open():
            wait_ms = self._ms_until_auction_fill_confirm_start()
            self._schedule_auction_poll(oid, delay_ms=wait_ms)
            return

        # 체결 확인 (side 미설정 시 kind로 추론)
        side = str(meta.touch_side or "").strip().lower()
        if side not in ("buy", "sell"):
            side = "sell" if str(meta.kind or "") == "exit_sell" else "buy"
            try:
                meta.touch_side = side
            except Exception:
                pass

        try:
            if self._touch_try_confirm_fill(meta, oid):
                return
        except Exception as e:
            try:
                self.log(f"⚠️ [동시호가] 체결 확인 실패: {e}")
            except Exception:
                pass

        if self._auction_watch_deadline_passed():
            try:
                self.log(
                    f"ℹ️ [동시호가] 폴링 종료(15:33+) ord={oid} {meta.code} — "
                    f"미확정 시 WS/잔고 동기화로 정리"
                )
            except Exception:
                pass
            return

        self._schedule_auction_poll(oid)

    def _find_touch_entry(self, chain_key: str) -> Tuple[str, Optional[LimitOrderMeta]]:
        """``touch_chain_key`` 로 (현재 주문번호, 메타) 조회."""
        ck = str(chain_key or "").strip()
        if not ck:
            return "", None
        try:
            with self._lock:
                snap: List[Tuple[str, LimitOrderMeta]] = list(
                    self.limit_order_meta.items()
                )
        except Exception:
            return "", None
        for no, m in snap:
            try:
                if m.touch_mode and str(m.touch_chain_key or "").strip() == ck:
                    return str(no).strip(), m
            except Exception:
                continue
        return "", None

    def _touch_order_fully_filled(self, order_no: str, expect_qty: int) -> bool:
        """주문 로그 기준 전량 체결 여부(부분 체결 후 잔량 정정은 별도 처리)."""
        oid = str(order_no or "").strip()
        if not oid:
            return False
        try:
            dm = getattr(self.app, "data_manager", None)
            if dm is None:
                return False
            row = dm.get_order_history_row_by_order_no(oid)
        except Exception:
            row = None
        if not isinstance(row, dict):
            return False
        try:
            fq = int(row.get("filled_qty") or 0)
        except (TypeError, ValueError):
            fq = 0
        try:
            res_u = str(row.get("result") or "").strip().upper()
        except Exception:
            res_u = ""
        if fq >= max(1, int(expect_qty or 0)):
            return True
        if res_u == "FILLED" and fq > 0:
            return True
        return False

    def _schedule_touch_timer(
        self,
        chain_key: str,
        *,
        phase: str = "confirm",
        first: bool = False,
    ) -> None:
        """
        다음 터치 폴링 타이머 예약(``touch_chain_key`` 기준).

        - confirm + first: 접수·정정 직후 ``touch_quote_first_confirm_sec``(기본 60초)
        - confirm 재시도: ``touch_quote_order_poll_sec``(기본 30초)
        - amend: ``touch_quote_order_poll_sec`` 후 호가 정정
        """
        ck = str(chain_key or "").strip()
        if not ck:
            return
        root = self._get_root()
        if root is None:
            return
        ph = str(phase or "confirm").strip().lower()
        if ph not in ("confirm", "amend"):
            ph = "confirm"
        if ph == "amend":
            ms = self._get_touch_poll_ms()
        elif first:
            ms = self._get_touch_first_poll_ms()
        else:
            ms = self._get_touch_poll_ms()

        def _cb() -> None:
            # 호가 조회·정정 REST는 메인 스레드에서 하면 Tk가 4~6초 멈춘 것처럼 보임
            def _worker() -> None:
                try:
                    self._on_touch_poll(ck)
                except Exception as e:
                    try:
                        self.log(f"⚠️ [터치호가] 폴링 오류: {e}")
                    except Exception:
                        pass

            try:
                threading.Thread(target=_worker, daemon=True).start()
            except Exception:
                try:
                    self._on_touch_poll(ck)
                except Exception as e:
                    try:
                        self.log(f"⚠️ [터치호가] 폴링 오류: {e}")
                    except Exception:
                        pass

        # [버그픽스] 동일 chain_key 에 이미 예약된 타이머가 남아 있으면 먼저 취소한다.
        # (취소 없이 새 after 만 등록하면 좀비 타이머가 누적되어 같은 주문에 대해
        #  _on_touch_poll 워커가 중복 실행 → cancel/정정/체결확인 REST 경쟁 발생)
        try:
            with self._lock:
                for _no, _m in self.limit_order_meta.items():
                    if (
                        _m.touch_mode
                        and str(_m.touch_chain_key or "").strip() == ck
                        and _m.after_id is not None
                    ):
                        try:
                            root.after_cancel(_m.after_id)
                        except Exception:
                            pass
                        _m.after_id = None
        except Exception:
            pass
        try:
            aid = root.after(ms, _cb)
        except Exception:
            return
        try:
            with self._lock:
                for no, m in self.limit_order_meta.items():
                    if (
                        m.touch_mode
                        and str(m.touch_chain_key or "").strip() == ck
                    ):
                        m.after_id = aid
                        m.touch_poll_phase = ph
                        break
        except Exception:
            pass

    def _touch_run_amend_phase(
        self, chain_key: str, current_ord: str, meta: LimitOrderMeta
    ) -> None:
        """(confirm에서 넘어옴) 호가 정정만 수행하고, 다음 confirm은 첫 확인 간격(기본 60초)으로 예약합니다."""
        max_a = self._get_touch_max_amends()
        if meta.touch_amend_attempts_done >= max_a:
            self.timeout(current_ord)
            return
        try:
            meta.touch_amend_should_increment = True
        except Exception:
            pass
        should_schedule = self._touch_execute_amend(meta, current_ord)
        try:
            inc = bool(getattr(meta, "touch_amend_should_increment", True))
            if inc:
                meta.touch_amend_attempts_done = int(
                    meta.touch_amend_attempts_done or 0
                ) + 1
        except Exception:
            pass
        if bool(should_schedule):
            # 정정도 새 접수로 보아 kt00007 반영까지 첫 확인 간격(기본 60초)을 둔다.
            self._schedule_touch_timer(chain_key, phase="confirm", first=True)

    def _touch_apply_ord_no_remap(
        self,
        *,
        old_no: str,
        new_no: str,
        meta: LimitOrderMeta,
        nc: str,
    ) -> None:
        """정정 응답 주문번호가 바뀐 경우 메타 dict 키·DB·포지션 필드 갱신."""
        o_old = str(old_no or "").strip()
        o_new = str(new_no or "").strip()
        if not o_old or not o_new or o_old == o_new:
            return
        try:
            dm = getattr(self.app, "data_manager", None)
            if dm is not None and hasattr(dm, "remap_order_history_order_no"):
                dm.remap_order_history_order_no(o_old, o_new)
        except Exception:
            pass
        try:
            with self._lock:
                if o_old in self.limit_order_meta:
                    self.limit_order_meta.pop(o_old, None)
                    self.limit_order_meta[o_new] = meta
        except Exception:
            pass
        try:
            if meta.kind == "exit_sell":
                with self.app.portfolio.lock:
                    p = self.app.portfolio.virtual_positions.get(nc)
                    if p and str(p.get("pending_exit_ord_no") or "").strip() == o_old:
                        p["pending_exit_ord_no"] = o_new
        except Exception:
            pass
        try:
            if meta.kind == "s2_buy":
                with self.app.portfolio.lock:
                    p2 = self.app.portfolio.virtual_positions.get(nc)
                    if p2 and str(p2.get("s2_pending_ord_no") or "").strip() == o_old:
                        p2["s2_pending_ord_no"] = o_new
        except Exception:
            pass

    def _touch_execute_amend(
        self, meta: LimitOrderMeta, current_ord: str
    ) -> bool:
        """
        최신 호가 재조회 후 ``kt10002`` 정정.

        Returns
        -------
        bool
            True: 다음 터치호가 폴링(다음 정정 시도)을 예약해도 됩니다.
            False: 주문이 terminal(예: RC4033: 남은 수량 없음) 이므로 추가 예약하지 않습니다.
        """
        nc = self._normalize_code_6(meta.code)
        oid = str(current_ord or "").strip()
        side = str(meta.touch_side or "").strip().lower()
        # 기본적으로 정정 시도 횟수는 소진으로 카운트합니다.
        # (특정 에러: RC4042에서는 소진하지 않도록 분기합니다)
        try:
            meta.touch_amend_should_increment = True
        except Exception:
            pass
        if not oid or side not in ("buy", "sell"):
            return False
        om = getattr(self.app, "order_manager", None)
        if om is None or not hasattr(om, "fetch_best_bid_ask_rest"):
            return False
        try:
            bid, ask = om.fetch_best_bid_ask_rest(nc)
        except Exception:
            bid, ask = None, None
        try:
            if side == "buy":
                px = int(round(float(ask))) if ask is not None and float(ask) > 0 else 0
            else:
                px = int(round(float(bid))) if bid is not None and float(bid) > 0 else 0
        except (TypeError, ValueError):
            px = 0
        if px <= 0:
            try:
                self.log(
                    f"⚠️ [터치호가] 정정 생략 — 호가 조회 실패 {nc} (ord={oid})"
                )
            except Exception:
                pass
            return False

        try:
            dm = getattr(self.app, "data_manager", None)
            row = (
                dm.get_order_history_row_by_order_no(oid)
                if dm is not None
                else None
            )
        except Exception:
            row = None
        try:
            filled = int((row or {}).get("filled_qty") or 0)
        except (TypeError, ValueError):
            filled = 0
        try:
            order_qty = int((row or {}).get("qty") or meta.qty or 0)
        except (TypeError, ValueError):
            order_qty = int(meta.qty or 0)

        # 이미 전량 체결된 주문이라면 남은 정정 루프를 더 돌리지 않습니다.
        # (kt00007/DB 반영 지연 등으로 _touch_try_confirm_fill() 타이밍과 어긋나는 경우 방지)
        try:
            expect_qty = int(meta.qty or 0)
        except (TypeError, ValueError):
            expect_qty = int(order_qty or 0)
        if self._touch_order_fully_filled(oid, expect_qty):
            try:
                # 터치 정정 예약 제거
                self.clear(oid)
            except Exception:
                pass
            return False
        rem = max(1, order_qty - max(0, filled))
        try:
            ok, new_no, msg = om.modify_limit_order(oid, nc, rem, px)
        except Exception as ex:
            try:
                self.log(f"⚠️ [터치호가] 정정 예외: {ex}")
            except Exception:
                pass
            return True
        if not ok:
            try:
                self.log(f"⚠️ [터치호가] 정정 거부: {msg} (ord={oid})")
            except Exception:
                pass
            # RC4033 등: "정정/취소할 수량이 없음"(=남은 수량 0, 전량 체결/종료)
            # 이 경우 터치 정정 루프를 계속 돌리면 동일 실패가 반복됩니다.
            try:
                s_msg = str(msg or "")
            except Exception:
                s_msg = ""
            # RC4042: "정정수량이 정정가능수량을 초과"
            # - 대개 직전 체결(부분체결) 반영이 DB/kt00007에 늦게 반영되는 레이스입니다.
            # - 이때 다음 정정을 바로 때리면 동일 실패가 이어질 수 있으므로,
            #   보강 체결동기화를 먼저 돌리고(boos_polling) 정정 횟수는 소진하지 않습니다.
            if (
                "4042" in s_msg
                or "정정가능수량" in s_msg
                or "정정수량이 정정가능수량을 초과" in s_msg
            ):
                try:
                    # RC4042에서 rem 계산 기준( DB filled_qty/qty )을 남겨 원인(동기화 지연 vs 계산 기준 불일치)을 추적합니다.
                    self.log(
                        f"🧪 [터치호가] RC4042 추적 filled_qty={filled} qty={order_qty} rem={rem} (ord={oid}, code={nc})"
                    )
                except Exception:
                    pass
                try:
                    self._schedule_boost_fill_reconcile(
                        order_no=oid,
                        code=nc,
                        kind=str(meta.kind or ""),
                        qty=int(meta.qty or 0),
                    )
                except Exception:
                    pass
                try:
                    meta.touch_amend_should_increment = False
                except Exception:
                    pass
                return True
            if ("4033" in s_msg) or ("no_open_qty" in s_msg) or ("정정/취소할 수량" in s_msg):
                try:
                    self._run_terminal_fill_confirm(meta, oid)
                except Exception:
                    pass
                try:
                    self.clear(oid)
                except Exception:
                    pass
                return False
            return True
        if new_no and new_no != oid:
            self._touch_apply_ord_no_remap(
                old_no=oid, new_no=new_no, meta=meta, nc=nc
            )
        try:
            sl = getattr(self.app, "session_logger", None)
            if sl:
                sl.log(
                    "touch_quote_amend",
                    {
                        "chain": meta.touch_chain_key,
                        "old_ord": oid,
                        "new_ord": new_no or oid,
                        "code": nc,
                        "side": side,
                        "px": px,
                        "rem_qty": rem,
                    },
                )
        except Exception:
            pass
        return True

    def _touch_try_confirm_fill(
        self, meta: LimitOrderMeta, current_ord: str
    ) -> bool:
        """
        kt00007 1회 동기화 후 체결 확정 시 True.

        체결·종료 확정이면 감시 메타를 정리하고 호출부는 폴링을 중단합니다.
        """
        frs = getattr(self.app, "fill_reconciliation_service", None)
        if frs is None or not hasattr(frs, "try_confirm_fill_once"):
            return False
        side = str(meta.touch_side or "").strip().lower()
        if side not in ("buy", "sell"):
            return False
        try:
            status = frs.try_confirm_fill_once(
                meta.code,
                current_ord,
                side,
                int(meta.qty or 0),
                float(meta.fill_price_hint or 0),
                str(meta.touch_name or ""),
                str(meta.touch_reason or ""),
                force_kt00007_sync=True,
            )
        except Exception as ex:
            try:
                self.log(f"⚠️ [터치호가] 체결 확인 오류: {ex}")
            except Exception:
                pass
            return False
        try:
            if status == "filled":
                self.log(
                    f"✅ [터치호가] 체결 확인 완료 ord={current_ord} {meta.code}"
                )
            elif status == "pending":
                if not bool(getattr(meta, "touch_pending_logged", False)):
                    meta.touch_pending_logged = True
                    _sec = self._get_touch_poll_ms() // 1000
                    self.log(
                        f"ℹ️ [터치호가] kt00007 미반영 — {_sec}초 후 재확인 "
                        f"(ord={current_ord})"
                    )
            elif status == "terminal":
                self.log(
                    f"ℹ️ [터치호가] 주문 종료({status}) ord={current_ord} {meta.code}"
                )
        except Exception:
            pass
        # pending이면 "정정(amend)로 바로 가지 않도록" 카운터를 올립니다.
        if status == "pending":
            try:
                meta.touch_pending_confirm_cnt = int(meta.touch_pending_confirm_cnt or 0) + 1
            except Exception:
                pass
            return False

        # pending이 아닌 경우(=확정/종료 등) 카운터 초기화
        try:
            meta.touch_pending_confirm_cnt = 0
        except Exception:
            pass

        if status in ("filled", "terminal", "skipped"):
            if status == "terminal" and str(meta.kind or "") == "exit_sell":
                try:
                    self._on_exit_sell_touch_terminal_without_fill(
                        meta, current_ord
                    )
                except Exception:
                    pass
            try:
                self.clear(current_ord)
            except Exception:
                pass
            return True
        return False

    def _on_exit_sell_touch_terminal_without_fill(
        self, meta: LimitOrderMeta, order_no: str
    ) -> None:
        """터치 매도가 취소·거부 등으로 끝났을 때 exit_pending 해제 및 잔고 동기화."""
        nc = self._normalize_code_6(meta.code)
        oid = str(order_no or "").strip()
        if not nc or not oid:
            return
        try:
            with self.app.portfolio.lock:
                p = self.app.portfolio.virtual_positions.get(nc)
                if p:
                    pend = str(p.get("pending_exit_ord_no") or "").strip()
                    if not pend or pend == oid:
                        p["exit_pending"] = False
                        p.pop("pending_exit_ord_no", None)
        except Exception:
            pass
        try:
            self.log(
                f"ℹ️ [터치호가] 매도 종료(체결 없음) — {nc} 재매도 판단 가능(ord={oid})"
            )
        except Exception:
            pass
        try:
            self._schedule_exit_sell_cancel_recovery(
                code=nc,
                cancelled_ord_no=oid,
                order_qty_hint=int(meta.qty or 0),
            )
        except Exception:
            pass

    def _on_touch_poll(self, chain_key: str) -> None:
        """터치호가: kt00007 확인 → 미체결이면 곧바로 kt10002 정정."""
        current_ord, meta = self._find_touch_entry(chain_key)
        if not current_ord or meta is None or not meta.touch_mode:
            return

        try:
            phase = str(getattr(meta, "touch_poll_phase", "confirm") or "confirm")
        except Exception:
            phase = "confirm"
        if phase == "amend":
            self._touch_run_amend_phase(chain_key, current_ord, meta)
            return

        if self._touch_try_confirm_fill(meta, current_ord):
            return

        # try_confirm_fill_once가 pending인 경우:
        # - 너무 이른 kt10002 정정으로 RC4033/RC4042 연쇄가 발생하는 걸 줄이기 위해
        #   "pending인 동안은 amend를 보내지 않고" 다음 확인만 예약합니다.
        # - 단, pending이 너무 오래 지속되면(=감시 루프가 진행되지 않으면) 기존 정책대로
        #   지정된 최대 회차(touch_quote_order_max_amends)에 도달했을 때 취소(timeout)로 흡수합니다.
        try:
            pending_cnt = int(getattr(meta, "touch_pending_confirm_cnt", 0) or 0)
        except Exception:
            pending_cnt = 0

        max_a = self._get_touch_max_amends()
        probe_after = self._touch_pending_probe_after()
        probe_min_sec = self._touch_pending_cancel_probe_min_sec()
        elapsed_sec = self._touch_elapsed_sec(meta)
        if pending_cnt > 0:
            # pending 횟수 + 최소 대기(기본 3분) 둘 다 충족해야 취소 프로브
            do_probe = (pending_cnt >= probe_after) and (
                elapsed_sec >= float(probe_min_sec)
            )
            if str(meta.kind or "") == "exit_sell":
                try:
                    st = getattr(self.app, "settings", None) or {}
                    exit_probe = int(
                        float(st.get("touch_exit_pending_probe_after", 1))
                    )
                    do_probe = pending_cnt >= max(1, min(exit_probe, 3))
                except (TypeError, ValueError):
                    do_probe = pending_cnt >= 1
            if do_probe:
                # 매도(EXIT): 미체결에 취소 API를 걸면 주문만 취소되고 보유는 그대로 남음.
                # 취소조회(전량체결 여부)는 미체결 타임아웃(timeout)에서만 수행합니다.
                if str(meta.kind or "") == "exit_sell":
                    try:
                        self.log(
                            f"ℹ️ [터치호가] 매도 pending {pending_cnt}회 — "
                            f"kt00007 재확인(미체결 유지, ord={current_ord})"
                        )
                    except Exception:
                        pass
                    try:
                        self._schedule_boost_fill_reconcile(
                            order_no=current_ord,
                            code=meta.code,
                            kind="exit_sell",
                            qty=int(meta.qty or 0),
                        )
                    except Exception:
                        pass
                else:
                    try:
                        cancel_st = str(
                            self.app.order_manager.cancel_order(
                                current_ord, meta.code, "0"
                            )
                            or "failed"
                        )
                    except Exception:
                        cancel_st = "failed"
                    if cancel_st == "no_open_qty":
                        try:
                            self.log(
                                f"ℹ️ [터치호가] pending {pending_cnt}회 — "
                                f"취소조회로 전량체결 확인(ord={current_ord})"
                            )
                        except Exception:
                            pass
                        self._run_terminal_fill_confirm(meta, current_ord)
                        return
            # [버그픽스] pending 구간에서는 touch_amend_attempts_done 가 증가하지 않아
            # `touch_amend_attempts_done < max_a` 가 영원히 참 → self.timeout() 에 도달하지 못하고
            # 30초마다 confirm 만 무한 재예약된다. kt00007 이 계속 미반영(모의/WS만 체결)이면
            # exit_pending(매도)·s1_buy_pending_codes(매수) 가 영구 잠금되어 재매도/재매수 불가.
            # 따라서 누적 pending 확인 횟수에 상한을 두고 초과 시 미체결 타임아웃으로 흡수한다.
            try:
                st = getattr(self.app, "settings", None) or {}
                pending_confirm_max = int(
                    float(st.get("touch_pending_confirm_max", 20))
                )
            except (TypeError, ValueError):
                pending_confirm_max = 20
            pending_confirm_max = max(4, min(pending_confirm_max, 200))
            if pending_cnt >= pending_confirm_max:
                try:
                    self.log(
                        f"⚠️ [터치호가] pending {pending_cnt}회 누적(상한 "
                        f"{pending_confirm_max}) — 미체결 타임아웃 처리(ord={current_ord})"
                    )
                except Exception:
                    pass
                self.timeout(current_ord)
                return
            try:
                if meta.touch_amend_attempts_done < max_a:
                    self._schedule_touch_timer(chain_key, phase="confirm")
                    return
            except Exception:
                try:
                    self._schedule_touch_timer(chain_key, phase="confirm")
                    return
                except Exception:
                    pass
            self.timeout(current_ord)
            return

        # 미체결·미확정: touch_quote_order_poll_sec(기본 30초) 후 정정 단계
        if meta.touch_amend_attempts_done < max_a:
            # "확인+정정"을 한 세트로 묶기 위해 amend 타이머를 별도로 예약하지 않고,
            # confirm 콜백 내에서 즉시 정정을 실행합니다.
            self._touch_run_amend_phase(chain_key, current_ord, meta)
            return

        self.timeout(current_ord)

    def _normalize_code_6(self, code: Any) -> str:
        try:
            return str(code).zfill(6)
        except Exception:
            return "000000"

    def clear(self, order_no: Optional[str]) -> None:
        """주문번호로 예약 타이머 취소 + 관련 상태 플래그 정리."""
        if not order_no:
            return
        oid = str(order_no).strip()
        if not oid:
            return

        with self._lock:
            meta = self.limit_order_meta.pop(oid, None)

        if meta is None:
            return

        try:
            if meta.kind in ("s1_buy", "s2_buy"):
                self.app.portfolio.release_d2_buy_reserve(oid)
        except Exception:
            pass

        root = self._get_root()
        if root is not None:
            aid = getattr(meta, "after_id", None)
            if aid is not None:
                try:
                    root.after_cancel(aid)
                except Exception:
                    pass

        kind = meta.kind
        code = meta.code
        nc = code

        # kind별 플래그 정리 (TradingController 기존 동작 그대로)
        if kind == "s1_buy" and nc:
            self.s1_buy_pending_codes.discard(nc)
        if kind == "s2_buy" and nc:
            try:
                with self.app.portfolio.lock:
                    p2 = self.app.portfolio.virtual_positions.get(nc)
                    if p2:
                        p2.pop("s2_limit_order_pending", None)
                        p2.pop("s2_pending_ord_no", None)
            except Exception:
                pass

    def register(
        self,
        *,
        order_no: str,
        code: str,
        kind: str,
        qty: int = 0,
        cancel_on_timeout: bool = True,
        touch_mode: bool = False,
        touch_side: str = "",
        touch_name: str = "",
        touch_reason: str = "",
        fill_price_hint: float = 0.0,
        auction_watch: bool = False,
    ) -> None:
        """
        주문 접수 후 감시 등록.

        - 일반 지정가: 미체결 ``limit_order_unfilled_timeout_sec`` 후 취소.
        - 시장가: 플래그만(취소 타이머 없음).
        - 터치호가: ``touch_quote_first_confirm_sec`` 후 kt00007 체결 확인,
          미반영이면 ``touch_quote_order_poll_sec``마다 재확인,
          미체결이면 즉시 정정(최대 ``touch_quote_order_max_amends``),
          정정 직후 다시 첫 확인 간격으로 루프.
        - auction_watch: 동시호가 시장가 — 15:30~15:33 kt00007 폴링(그 전·취소·정정 없음).
        """
        oid = str(order_no or "").strip()
        nc = str(code).zfill(6)
        if not oid or not nc:
            return

        # 같은 주문번호는 중복 예약 방지
        self.clear(oid)

        root = self._get_root()
        after_id: Any = None
        use_auction = bool(auction_watch)
        if use_auction:
            if root is None:
                # 메타만 등록(체결 시 clear)
                after_id = None
            else:
                wait_start = self._ms_until_auction_fill_confirm_start()
                poll_ms = self._get_auction_poll_ms()
                # 15:30 전이면 첫 조회를 15:30:00에, 이후면 곧 간격 폴링
                ms = wait_start if wait_start > 0 else poll_ms

                def _acb() -> None:
                    def _worker() -> None:
                        try:
                            self._on_auction_poll(oid)
                        except Exception as e:
                            try:
                                self.log(f"⚠️ [동시호가] 폴링 오류: {e}")
                            except Exception:
                                pass

                    try:
                        threading.Thread(target=_worker, daemon=True).start()
                    except Exception:
                        try:
                            self._on_auction_poll(oid)
                        except Exception as e:
                            try:
                                self.log(f"⚠️ [동시호가] 폴링 오류: {e}")
                            except Exception:
                                pass

                try:
                    if wait_start > 0:
                        self.log(
                            f"⏱️ [동시호가] 시장가 감시 등록 ({kind}) {nc} ord={oid} "
                            f"(취소·정정 없음 · 체결확인 15:30~15:33 · "
                            f"{poll_ms // 1000}초 간격 · "
                            f"첫조회까지 약 {wait_start // 1000}초)"
                        )
                    else:
                        self.log(
                            f"⏱️ [동시호가] 시장가 감시 등록 ({kind}) {nc} ord={oid} "
                            f"(취소·정정 없음 · 체결확인 15:30~15:33 · "
                            f"{poll_ms // 1000}초 간격)"
                        )
                except Exception:
                    pass
                after_id = root.after(ms, _acb)
        elif cancel_on_timeout:
            if root is None:
                return
            if touch_mode:
                ck = oid
                ms = self._get_touch_first_poll_ms()
                poll_sec = self._get_touch_poll_ms() // 1000

                def _tcb() -> None:
                    def _worker() -> None:
                        try:
                            self._on_touch_poll(ck)
                        except Exception as e:
                            try:
                                self.log(f"⚠️ [터치호가] 폴링 오류: {e}")
                            except Exception:
                                pass

                    try:
                        threading.Thread(target=_worker, daemon=True).start()
                    except Exception:
                        try:
                            self._on_touch_poll(ck)
                        except Exception as e:
                            try:
                                self.log(f"⚠️ [터치호가] 폴링 오류: {e}")
                            except Exception:
                                pass

                try:
                    _sec = ms // 1000
                    _retry = poll_sec
                    _cancel_min = int(self._touch_pending_cancel_probe_min_sec())
                    self.log(
                        f"⏱️ [터치호가] ord={oid} · 첫 체결확인 {_sec}초 후 "
                        f"(미반영 시 {_retry}초마다 재확인, "
                        f"취소프로브 최소 {_cancel_min}초, "
                        f"미체결 시 정정 최대 {self._get_touch_max_amends()}회)"
                    )
                except Exception:
                    pass

                after_id = root.after(ms, _tcb)
            else:
                ms = self._get_timeout_ms()

                def _cb() -> None:
                    try:
                        self.timeout(oid)
                    except Exception as e:
                        try:
                            self.log(f"⚠️ [미체결타임아웃] 처리 오류: {e}")
                        except Exception:
                            pass

                try:
                    self.log(
                        f"⏱️ [지정가] 미체결 감시 등록 ({kind}) {nc} ord={oid} / {ms // 1000}초"
                    )
                except Exception:
                    pass

                after_id = root.after(ms, _cb)
        else:
            try:
                self.log(
                    f"⏱️ [시장가] 접수 감시 등록 ({kind}) {nc} ord={oid} "
                    f"(미체결 취소 타이머 없음 — 체결 시 정리)"
                )
            except Exception:
                pass

        meta = LimitOrderMeta(
            code=nc,
            kind=kind,
            after_id=after_id,
            qty=int(qty),
            cancel_on_timeout=bool(cancel_on_timeout) and (not use_auction),
            touch_mode=bool(touch_mode) and (not use_auction),
            touch_chain_key=oid if touch_mode and not use_auction else "",
            touch_side=str(touch_side or "").strip().lower()
            if (touch_mode or use_auction)
            else "",
            touch_name=str(touch_name or "") if (touch_mode or use_auction) else "",
            touch_reason=str(touch_reason or "") if (touch_mode or use_auction) else "",
            touch_amend_attempts_done=0,
            fill_price_hint=float(fill_price_hint or 0)
            if (touch_mode or use_auction)
            else 0.0,
            touch_poll_phase="confirm",
            registered_at=float(time.time()),
            auction_watch=use_auction,
        )

        with self._lock:
            self.limit_order_meta[oid] = meta

        if kind == "s1_buy":
            self.s1_buy_pending_codes.add(nc)

    def _touch_pending_probe_after(self) -> int:
        try:
            n = int(
                (getattr(self.app, "settings", None) or {}).get(
                    "touch_pending_cancel_probe_after", 2
                )
            )
        except (TypeError, ValueError):
            n = 2
        return max(1, min(n, 5))

    def _touch_pending_cancel_probe_min_sec(self) -> float:
        """
        kt00007 미반영(pending) 후 취소 프로브까지 최소 대기(초).

        예전 기본(~90초: 첫확인 60초+재확인 1회)은 너무 빨라
        호가 대기 중 주문이 취소되는 경우가 잦았다. 기본 3분.
        """
        try:
            st = getattr(self.app, "settings", None) or {}
            sec = float(st.get("touch_pending_cancel_probe_min_sec", 180.0))
        except (TypeError, ValueError):
            sec = 180.0
        return max(60.0, min(sec, 900.0))

    def _touch_elapsed_sec(self, meta: LimitOrderMeta) -> float:
        """접수 후 경과 초. registered_at 없으면 충분히 지난 것으로 간주."""
        try:
            t0 = float(getattr(meta, "registered_at", 0.0) or 0.0)
            if t0 <= 0.0:
                return 1.0e9
            return max(0.0, float(time.time()) - t0)
        except Exception:
            return 1.0e9

    def _run_terminal_fill_confirm(self, meta: LimitOrderMeta, order_no: str) -> None:
        """RC4033·no_open_qty 직후 즉시 체결 확정(백그라운드)."""
        oid = str(order_no or "").strip()
        nc = self._normalize_code_6(meta.code)
        if not oid or not nc:
            return

        def _worker() -> None:
            try:
                fcs = getattr(self.app, "fill_confirmation_service", None)
                if fcs is None:
                    self._schedule_boost_fill_reconcile_legacy(
                        order_no=oid,
                        code=nc,
                        kind=str(meta.kind or ""),
                        qty=int(meta.qty or 0),
                    )
                    return
                side = str(meta.touch_side or "").strip().lower()
                if side not in ("buy", "sell"):
                    side = "sell" if str(meta.kind or "") == "exit_sell" else "buy"
                ok = fcs.confirm_after_no_open_qty(
                    code=nc,
                    order_no=oid,
                    side=side,
                    qty=int(meta.qty or 0),
                    fill_price_hint=float(meta.fill_price_hint or 0),
                    name=str(meta.touch_name or ""),
                    reason=str(meta.touch_reason or ""),
                    kind=str(meta.kind or ""),
                )
                if ok:
                    try:
                        self.clear(oid)
                    except Exception:
                        pass
            except Exception as ex:
                try:
                    self.log(f"⚠️ [터치호가] 즉시 체결확정 실패: {ex}")
                except Exception:
                    pass

        try:
            threading.Thread(
                target=_worker, daemon=True, name=f"FillConfirm-{oid}"
            ).start()
        except Exception:
            try:
                _worker()
            except Exception:
                pass

    def _schedule_boost_fill_reconcile(
        self, *, order_no: str, code: str, kind: str, qty: int
    ) -> None:
        """RC4033·4042 등 — 즉시 체결확정 경로(legacy boost 폴링 대체)."""
        oid = str(order_no or "").strip()
        nc = self._normalize_code_6(code)
        meta: Optional[LimitOrderMeta] = None
        try:
            with self._lock:
                meta = self.limit_order_meta.get(oid)
        except Exception:
            meta = None
        if meta is None:
            side = "sell" if str(kind or "") == "exit_sell" else "buy"
            try:
                px = 0.0
                dm = getattr(self.app, "data_manager", None)
                if dm is not None:
                    row = dm.get_order_history_row_by_order_no(oid)
                    if isinstance(row, dict):
                        px = float(row.get("price") or 0)
            except Exception:
                px = 0.0
            meta = LimitOrderMeta(
                code=nc,
                kind=str(kind or ""),
                after_id=None,
                qty=int(qty or 0),
                touch_side=side,
                fill_price_hint=float(px),
            )
        self._run_terminal_fill_confirm(meta, oid)

    def _schedule_boost_fill_reconcile_legacy(
        self, *, order_no: str, code: str, kind: str, qty: int
    ) -> None:
        """
        취소 거부(미체결 없음) 직후: 주문 로그 행 기준으로 kt00007 재폴링을 강하게 돌립니다.
        체결가·수량은 DB/kt00007 확정 후에만 on_fill 됩니다.
        """
        oid = str(order_no or "").strip()
        nc = self._normalize_code_6(code)
        if not oid or not nc:
            return
        try:
            dm = getattr(self.app, "data_manager", None)
            if dm is None:
                return
            row = dm.get_order_history_row_by_order_no(oid)
        except Exception:
            row = None
        if not isinstance(row, dict):
            try:
                self.log(
                    f"ℹ️ [지정가] 강화 체결동기화 생략 — 주문로그 없음(ord={oid}, {nc})"
                )
            except Exception:
                pass
            return
        try:
            c = str(row.get("code") or nc).split("_")[0].zfill(6)
            side = str(row.get("side") or "").strip().lower()
            if side not in ("buy", "sell"):
                side = "sell" if kind == "exit_sell" else "buy"
            try:
                q = int(row.get("qty") or qty or 0)
            except Exception:
                q = int(qty or 0)
            try:
                px = float(row.get("price") or 0)
            except Exception:
                px = 0.0
            nm = str(row.get("name") or "")
            rs = str(row.get("reason") or "")
        except Exception:
            return
        if q <= 0 or px <= 0:
            try:
                self.log(
                    f"ℹ️ [지정가] 강화 체결동기화 생략 — 수량/가격 부족(ord={oid})"
                )
            except Exception:
                pass
            return

        def _run() -> None:
            try:
                frs = getattr(self.app, "fill_reconciliation_service", None)
                if frs is not None and hasattr(frs, "run_light_boost_confirm"):
                    frs.run_light_boost_confirm(
                        c, oid, side, int(q), float(px), nm, rs
                    )
                else:
                    self.app.reconcile_exchange_fill_after_order_accept(
                        c,
                        oid,
                        side,
                        int(q),
                        float(px),
                        nm,
                        rs,
                        boost_polling=True,
                    )
            except Exception as ex:
                try:
                    self.log(f"⚠️ [지정가] 강화 체결동기화 예약 실패: {ex}")
                except Exception:
                    pass

        try:
            root = self._get_root()
            if root is not None and hasattr(root, "after"):
                root.after(0, _run)
            else:
                _run()
        except Exception:
            try:
                _run()
            except Exception:
                pass

    def _schedule_exit_sell_cancel_recovery(
        self,
        *,
        code: str,
        cancelled_ord_no: str,
        order_qty_hint: int,
    ) -> None:
        """
        매도 지정가가 미체결 타임아웃으로 취소된 직후 처리.

        부분 체결 뒤 잔량만 취소되는 경우, 증권사 매도가능수량은 이미 줄었는데
        로컬 VP 수량·exit_pending 해제 타이밍이 어긋나면 곧바로 전량 매도를 재시도해
        800033(매도가능수량 부족)이 난다. kt00007 병합 후 잔고 동기화로 수량을 맞춘 뒤
        exit_pending 을 해제한다.
        """
        nc = self._normalize_code_6(code)
        oid = str(cancelled_ord_no or "").strip()
        if not nc or not oid:
            return
        hint_q = int(order_qty_hint or 0)

        def _on_main_finish() -> None:
            try:
                self._schedule_boost_fill_reconcile(
                    order_no=oid,
                    code=nc,
                    kind="exit_sell",
                    qty=hint_q,
                )
            except Exception:
                pass
            try:
                with self.app.portfolio.lock:
                    p = self.app.portfolio.virtual_positions.get(nc)
                    if p:
                        p["exit_pending"] = False
                        p.pop("pending_exit_ord_no", None)
            except Exception:
                pass
            try:
                self.log(
                    f"ℹ️ [미체결타임아웃] 매도 취소 후 잔고·주문로그 동기화 완료 — "
                    f"종목 {nc} 재매도 판단 가능"
                )
            except Exception:
                pass

        def _bg_sync() -> None:
            try:
                om = getattr(self.app, "order_manager", None)
                dm = getattr(self.app, "data_manager", None)
                if om is not None and dm is not None:
                    om.sync_exchange_fills_into_database(
                        dm,
                        dates=[date.today()],
                        silent=True,
                        logger=self.log,
                    )
            except Exception:
                pass
            try:
                self.app.portfolio.sync_with_actual_account(
                    silent=True,
                    force=True,
                    schedule_gui_refresh=False,
                    schedule_orders_refresh=False,
                )
            except Exception:
                pass
            try:
                root = self._get_root()
                if root is not None and hasattr(root, "after"):
                    def _after_sync_ui() -> None:
                        try:
                            fn = getattr(
                                self.app, "request_virtual_tab_sync_light_refresh", None
                            )
                            if callable(fn):
                                fn()
                        except Exception:
                            pass
                        try:
                            _on_main_finish()
                        except Exception:
                            pass

                    root.after(0, _after_sync_ui)
                else:
                    _on_main_finish()
            except Exception:
                try:
                    _on_main_finish()
                except Exception:
                    pass

        try:
            threading.Thread(target=_bg_sync, daemon=True).start()
        except Exception as ex:
            try:
                self.log(
                    f"⚠️ [미체결타임아웃] 매도 취소 후 동기화 스레드 실패({ex}) — "
                    f"플래그만 즉시 해제합니다(잔고 괴리 가능)."
                )
            except Exception:
                pass
            try:
                with self.app.portfolio.lock:
                    p = self.app.portfolio.virtual_positions.get(nc)
                    if p:
                        p["exit_pending"] = False
                        p.pop("pending_exit_ord_no", None)
            except Exception:
                pass

    def timeout(self, order_no: str) -> None:
        """미체결 타임아웃: 취소 API 호출 후 대기 플래그 해제."""
        if not order_no:
            return
        oid = str(order_no).strip()
        if not oid:
            return

        with self._lock:
            meta = self.limit_order_meta.pop(oid, None)

        if meta is None:
            return

        nc = meta.code
        kind = meta.kind

        cancel_st = "failed"
        try:
            cancel_st = str(
                self.app.order_manager.cancel_order(oid, nc, "0") or "failed"
            )
        except Exception as e:
            try:
                self.log(f"⚠️ [미체결타임아웃] 취소 API 예외: {e}")
            except Exception:
                pass

        ok = cancel_st == "cancelled"
        no_open = cancel_st == "no_open_qty"

        # 주문 로그(세션) 기록
        try:
            sl = getattr(self.app, "session_logger", None)
            if sl:
                sl.log(
                    "limit_order_unfilled_timeout",
                    {
                        "ord_no": oid,
                        "code": nc,
                        "kind": kind,
                        "cancel_ok": ok,
                        "cancel_st": cancel_st,
                    },
                )
        except Exception:
            pass

        # kind별 플래그 정리 (TradingController 기존 동작 그대로)
        if kind == "exit_sell":
            if no_open:
                try:
                    self._run_terminal_fill_confirm(meta, oid)
                except Exception:
                    pass
                try:
                    self.log(
                        f"⏱️ [미체결타임아웃] 매도 취소 생략(미체결 없음) — "
                        f"종목 {nc} 즉시 체결확정(ord={oid})"
                    )
                except Exception:
                    pass
            else:
                try:
                    self.log(
                        f"⏱️ [미체결타임아웃] 매도 취소 시도({ok}) — "
                        f"종목 {nc} 잔고 동기화 후 재매도 판단"
                    )
                except Exception:
                    pass
                try:
                    self._schedule_exit_sell_cancel_recovery(
                        code=nc,
                        cancelled_ord_no=oid,
                        order_qty_hint=int(meta.qty or 0),
                    )
                except Exception as ex:
                    try:
                        self.log(
                            f"⚠️ [미체결타임아웃] 매도 취소 후 동기화 예약 실패({ex}) — "
                            f"플래그만 즉시 해제"
                        )
                    except Exception:
                        pass
                    try:
                        with self.app.portfolio.lock:
                            p = self.app.portfolio.virtual_positions.get(nc)
                            if p:
                                p["exit_pending"] = False
                                p.pop("pending_exit_ord_no", None)
                    except Exception:
                        pass

        elif kind == "s1_buy":
            self.s1_buy_pending_codes.discard(nc)
            if no_open:
                try:
                    self._schedule_boost_fill_reconcile(
                        order_no=oid, code=nc, kind=kind, qty=int(meta.qty or 0)
                    )
                except Exception:
                    pass
            try:
                self.log(
                    f"⏱️ [미체결타임아웃] S1 매수 취소 시도({ok}, no_open={no_open}) "
                    f"— 종목 {nc} 재신호 가능"
                )
            except Exception:
                pass

        elif kind == "s2_buy":
            try:
                with self.app.portfolio.lock:
                    p = self.app.portfolio.virtual_positions.get(nc)
                    if p:
                        p.pop("s2_limit_order_pending", None)
                        p.pop("s2_pending_ord_no", None)
            except Exception:
                pass
            if no_open:
                try:
                    self._schedule_boost_fill_reconcile(
                        order_no=oid, code=nc, kind=kind, qty=int(meta.qty or 0)
                    )
                except Exception:
                    pass
            try:
                self.log(
                    f"⏱️ [미체결타임아웃] S2 매수 취소 시도({ok}, no_open={no_open}) "
                    f"— 종목 {nc} 재조건 시 재추매 가능"
                )
            except Exception:
                pass

