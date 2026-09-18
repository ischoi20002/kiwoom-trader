import requests
import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

import config
from utils.normalization import get_tick_size, normalize_code
from core.price_guard import validate_limit_order_reference_price
from core.supply_display import capture_supply_snapshot

# ka10004(호가) 전역 최소 간격 — 모의/실전 유량=1(초당 1회) 대응
_KA10004_GLOBAL_LOCK = threading.Lock()
_KA10004_GLOBAL_NEXT_MONOTONIC: float = 0.0


def _ka10004_global_min_interval_sec(app: Any = None) -> float:
    try:
        st = getattr(app, "settings", None) or {}
        v = float(st.get("realtime_ka10004_global_min_interval_sec", 1.05))
        return max(0.35, min(3.0, v))
    except Exception:
        return 1.05


def _ka10004_wait_global_slot(app: Any = None) -> None:
    """ka10004 호출 직전 — 전역 최소 간격만큼 대기(락 밖에서 sleep)."""
    global _KA10004_GLOBAL_NEXT_MONOTONIC
    gap = _ka10004_global_min_interval_sec(app)
    try:
        with _KA10004_GLOBAL_LOCK:
            now = time.monotonic()
            start_at = max(now, float(_KA10004_GLOBAL_NEXT_MONOTONIC))
            _KA10004_GLOBAL_NEXT_MONOTONIC = start_at + gap
        wait_s = float(start_at) - time.monotonic()
        if wait_s > 0.0:
            time.sleep(wait_s)
    except Exception:
        try:
            time.sleep(min(0.2, gap))
        except Exception:
            pass


# [v4.0] Kiwoom REST API 공식 패키지 모듈 도입
try:
    from kiwoom_rest_api.koreanstock.order import Order
    from kiwoom_rest_api.koreanstock.account import Account
except ImportError:
    Order = None
    Account = None

class OrderManager:
    """[v4.0.0] K-Trader 주문 집행 매니저 (Kiwoom REST API 공식 규격 준수)"""
    def __init__(self, app):
        self.app = app
        self.logger = logging.getLogger("OrderManager")
        
        # [v4.0] API 객체 초기화
        # AuthManager는 KiwoomBaseAPI가 요구하는 get_token() 인터페이스를 구현함
        base_url = config.MOCK_URL if getattr(config, 'USE_MOCK', False) else config.URL
        
        if Order:
            self.order_api = Order(base_url=base_url, token_manager=self.app.auth_manager)
            self.account_api = Account(base_url=base_url, token_manager=self.app.auth_manager)
        else:
            self.order_api = None
            self.account_api = None
            self.logger.error("❌ kiwoom_rest_api 패키지를 로드할 수 없습니다.")

        # 직전 주문 API 거부 메시지(매도 실패 시 800033 등 후처리용)
        self.last_order_reject_msg: str = ""
        # touch 호가(ka10004) 단기 캐시 — 15분 확정 평가 중 prefetch 용
        self._touch_quote_cache: Dict[str, Tuple[float, Optional[float], Optional[float]]] = {}
        self._touch_quote_cache_lock = threading.Lock()

    def _touch_quote_cache_ttl_sec(self) -> float:
        try:
            st = getattr(self.app, "settings", None) or {}
            v = float(st.get("realtime_touch_quote_cache_ttl_sec", 15.0))
            return max(0.5, min(60.0, v))
        except Exception:
            return 15.0

    @staticmethod
    def quote_venue_stk_cd(stk_cd: str, *, venue: str = "KRX") -> str:
        """
        ka10004 종목코드 — KRX 6자리, NXT ``{code}_NX``.

        ``005930_NX`` 등 접미가 이미 있으면 그대로 사용합니다.
        """
        try:
            raw = str(stk_cd or "").strip().upper()
        except Exception:
            return ""
        if not raw:
            return ""
        if "_NX" in raw or "_AL" in raw:
            return raw
        base = raw.split("_")[0].zfill(6)
        if not base or base == "000000":
            return ""
        v = str(venue or "KRX").strip().upper()
        if v == "NXT":
            return f"{base}_NX"
        return base

    def cache_best_bid_ask(
        self,
        stk_cd: str,
        bid: Optional[float],
        ask: Optional[float],
        *,
        venue: str = "KRX",
    ) -> None:
        """호가 캐시를 갱신합니다."""
        try:
            cache_key = self.quote_venue_stk_cd(stk_cd, venue=venue)
        except Exception:
            return
        if not cache_key:
            return
        try:
            with self._touch_quote_cache_lock:
                self._touch_quote_cache[cache_key] = (
                    time.monotonic(),
                    float(bid) if bid is not None and float(bid) > 0 else None,
                    float(ask) if ask is not None and float(ask) > 0 else None,
                )
        except Exception:
            pass

    def get_cached_best_bid_ask(
        self,
        stk_cd: str,
        *,
        max_age_sec: Optional[float] = None,
        venue: str = "KRX",
    ) -> Tuple[Optional[float], Optional[float]]:
        """유효 TTL 내 캐시된 (bid, ask). 없거나 만료면 (None, None)."""
        try:
            cache_key = self.quote_venue_stk_cd(stk_cd, venue=venue)
        except Exception:
            return None, None
        if not cache_key:
            return None, None
        ttl = (
            float(max_age_sec)
            if max_age_sec is not None
            else self._touch_quote_cache_ttl_sec()
        )
        try:
            with self._touch_quote_cache_lock:
                hit = self._touch_quote_cache.get(cache_key)
            if not hit:
                return None, None
            ts_m, bid, ask = hit
            if (time.monotonic() - float(ts_m)) > float(ttl):
                return None, None
            return bid, ask
        except Exception:
            return None, None

    def prefetch_best_bid_ask_rest(
        self, stk_cd: str, *, venue: str = "KRX"
    ) -> Tuple[Optional[float], Optional[float]]:
        """ka10004를 조회해 캐시에 넣고 반환(평가와 주문 경로 겹침용)."""
        bid, ask = self.fetch_best_bid_ask_rest(
            stk_cd, prefer_cache=False, update_cache=True, venue=venue
        )
        return bid, ask

    def _get_base_url(self):
        return config.MOCK_URL if getattr(config, 'USE_MOCK', False) else config.URL

    @staticmethod
    def dmst_stex_tp_for_orders() -> str:
        """
        키움 REST 주문·취소·체결내역(kt00007) 등에 쓰는 국내거래소 구분(dmst_stex_tp).

        - 모의투자: KRX
        - 실전투자: SOR (스마트오더 라우팅; 실서버 전환 시 요구)
        """
        return "KRX" if getattr(config, "USE_MOCK", False) else "SOR"

    def get_adjusted_price(self, price, side):
        """[v3.8] 공격적인 체결을 위해 ±1틱 보정 가격 산출"""
        tick = get_tick_size(price)
        if side == "buy":
            return int(price + tick)
        else:
            return int(price - tick)

    def place_order(
        self,
        code,
        qty,
        price,
        order_type="00",
        side="buy",
        name="",
        reason="",
        skip_price_guard: bool = False,
        apply_limit_tick_adjust: bool = True,
        skip_immediate_fill_reconcile: bool = False,
    ):
        """
        주문 발송 (REST API 호출 - v4.0.0 Refactored)
        - order_type \"00\"(지정가): 기본은 매수 +1틱·매도 −1틱 보정(`apply_limit_tick_adjust=True`).
          터치호가(매도1/매수1) 주문은 ``apply_limit_tick_adjust=False`` 로 **보정 없이** ``price`` 그대로.
        - skip_immediate_fill_reconcile: True 시 접수 직후 2초×8 체결 폴링 생략(터치 감시가 담당).
        - order_type \"03\"(시장가): 호가 0 — 레거시·테스트용.
        - skip_price_guard: True 시 지정가 시세 일관성 검증 생략(비상 매도·호가 직접조회 주문 등).
        """
        use_mock = getattr(config, 'USE_MOCK', False)
        mode_str = "모의투자" if use_mock else "실전투자"
        
        # [v16.9] 실제 주문 실행 전 최종 확인 (Safety Switch)
        allow_order = getattr(config, 'ALLOW_MOCK_ORDER', False) if use_mock else getattr(config, 'ALLOW_REAL_ORDER', False)
        
        if not allow_order:
            try:
                self.last_order_reject_msg = ""
            except Exception:
                pass
            self.app.log(f"⚠️ [{mode_str}] 주문 차단됨 (Safety Switch: OFF)")
            try:
                sl = getattr(self.app, "session_logger", None)
                if sl:
                    sl.log(
                        "order_blocked",
                        {
                            "code": code,
                            "side": side,
                            "qty": int(qty),
                            "reason": "safety_switch_off",
                            "name": name,
                        },
                    )
            except Exception:
                pass
            return None

        if not self.order_api:
            try:
                self.last_order_reject_msg = ""
            except Exception:
                pass
            self.app.log(f"❌ [{mode_str}] 주문 엔진 로드 실패")
            return None

        try:
            try:
                self.last_order_reject_msg = ""
            except Exception:
                pass
            if (
                not skip_price_guard
                and str(order_type).strip() == "00"
            ):
                tree_p: Optional[float] = None
                try:
                    mt = getattr(self.app, "monitor_tab", None)
                    if mt is not None and hasattr(mt, "get_tree_display_price"):
                        tree_p = mt.get_tree_display_price(code)
                except Exception:
                    tree_p = None
                st = getattr(self.app, "settings", None) or {}
                ok_pg, msg_pg = validate_limit_order_reference_price(
                    code=str(code).zfill(6),
                    ref_price=float(price),
                    order_type=str(order_type),
                    monitor_info=getattr(self.app, "monitor_info", None),
                    tree_price=tree_p,
                    settings=st if isinstance(st, dict) else {},
                )
                if not ok_pg:
                    try:
                        self.last_order_reject_msg = str(msg_pg or "")
                    except Exception:
                        pass
                    self.app.log(f"❌ [{mode_str}] 주문 차단(시세 검증): {msg_pg}")
                    try:
                        sl = getattr(self.app, "session_logger", None)
                        if sl:
                            sl.log(
                                "order_blocked",
                                {
                                    "code": code,
                                    "side": side,
                                    "qty": int(qty),
                                    "reason": "price_guard",
                                    "detail": msg_pg,
                                    "name": name,
                                },
                            )
                    except Exception:
                        pass
                    return None

            # [v4.0] 가격 보정 적용 및 API 규격 매핑
            if order_type == "00":
                if apply_limit_tick_adjust:
                    adj_price = self.get_adjusted_price(price, side)
                else:
                    try:
                        adj_price = int(round(float(price)))
                    except (TypeError, ValueError):
                        adj_price = 0
            else:
                adj_price = price
            
            # trde_tp: "0"(Limit), "3"(Market)
            # order_type "00" -> "0", "03" -> "3" 등 변환
            api_trde_tp = str(int(order_type)) 

            stex = self.dmst_stex_tp_for_orders()
            if side == "buy":
                # stock_buy_order_request_kt10000
                res = self.order_api.stock_buy_order_request_kt10000(
                    dmst_stex_tp=stex,
                    stk_cd=code,
                    ord_qty=str(int(qty)),
                    trde_tp=api_trde_tp,
                    ord_uv=str(int(adj_price)) if order_type == "00" else "0"
                )
            else:
                # stock_sell_order_request_kt10001
                res = self.order_api.stock_sell_order_request_kt10001(
                    dmst_stex_tp=stex,
                    stk_cd=code,
                    ord_qty=str(int(qty)),
                    trde_tp=api_trde_tp,
                    ord_uv=str(int(adj_price)) if order_type == "00" else "0"
                )
            
            # API 응답 처리
            ret_code = res.get("return_code", -1)
            ret_msg = res.get("return_msg", "No Message")
            ord_no = res.get("ord_no", "")

            if ret_code == 0 or ord_no:
                self.app.log(
                    f"✅ [{mode_str}] 주문 접수: {code}({name}) ord={ord_no} | "
                    f"{qty}주 @ {adj_price:,}원"
                )
                # [v3.8] DB 로그 + 감시 화면과 동일한 체결강도·호가 스냅샷
                try:
                    _sn = capture_supply_snapshot(self.app, code)
                except Exception:
                    _sn = {"vol_power": None, "bid_ask_text": ""}
                # [v3.8] DB 로그 기록
                self.app.data_manager.log_order(
                    code,
                    side,
                    qty,
                    adj_price,
                    order_type,
                    ord_no,
                    "SUCCESS",
                    name=name,
                    reason=reason,
                    snap_vol_power=_sn.get("vol_power"),
                    snap_bid_ask=_sn.get("bid_ask_text") or "",
                )
                try:
                    fcs = getattr(self.app, "fill_confirmation_service", None)
                    if fcs is not None and ord_no:
                        fcs.ensure_submitted_state(
                            str(ord_no).strip(),
                            code=str(code).zfill(6),
                            side=str(side).lower(),
                            order_qty=int(qty),
                            name=str(name or ""),
                            reason=str(reason or ""),
                        )
                except Exception:
                    pass

                # 터치호가: LimitOrderWatchService 가 kt00007·on_fill 처리 (중복 폴링 방지)
                if not skip_immediate_fill_reconcile:
                    try:
                        root = getattr(self.app, "root", None)
                        if root is not None and hasattr(root, "after"):
                            root.after(
                                0,
                                lambda c=code,
                                o=ord_no,
                                s=side,
                                q=int(qty),
                                ap=int(adj_price),
                                nm=name,
                                rs=reason: self.app.reconcile_exchange_fill_after_order_accept(
                                    c, o, s, q, float(ap), nm, rs, boost_polling=False
                                ),
                            )
                        else:
                            self.app.reconcile_exchange_fill_after_order_accept(
                                code,
                                ord_no,
                                side,
                                int(qty),
                                float(adj_price),
                                name,
                                reason,
                                boost_polling=False,
                            )
                    except Exception as sync_e:
                        self.logger.warning("체결 동기화 스케줄 실패: %s", sync_e)

                # 텔레그램 알림은 체결 확정 시점(FillConfirmationService)에서만 발송.
                # 계좌요약 텔레그램도 접수+40초가 아니라 체결 확정 후 force 동기화로 발송.
                return ord_no
            else:
                try:
                    self.last_order_reject_msg = str(ret_msg or "")
                except Exception:
                    pass
                self.app.log(f"❌ [{mode_str}] 주문 거부: {ret_msg} (ErrorCode: {ret_code})")
                try:
                    sl = getattr(self.app, "session_logger", None)
                    if sl:
                        sl.log(
                            "order_api_reject",
                            {
                                "code": code,
                                "side": side,
                                "qty": int(qty),
                                "ret_code": ret_code,
                                "ret_msg": str(ret_msg),
                                "name": name,
                                "reason": reason,
                            },
                        )
                except Exception:
                    pass
                return None

        except Exception as e:
            try:
                self.last_order_reject_msg = str(e)
            except Exception:
                pass
            self.app.log(f"💥 주문 프로세스 오류: {str(e)}")
            try:
                sl = getattr(self.app, "session_logger", None)
                if sl:
                    sl.log(
                        "order_api_exception",
                        {"code": code, "side": side, "error": str(e)},
                    )
            except Exception:
                pass
            return None

    @staticmethod
    def _cancel_reject_means_no_open_qty(ret_msg: Any) -> bool:
        """
        kt10003 취소 거부 중 '미체결 잔량 없음(통상 전량 체결 후)' 패턴.
        이 경우 DB를 CANCELLED로 덮어쓰면 안 됩니다.
        """
        try:
            s = str(ret_msg or "")
        except Exception:
            s = ""
        if not s:
            return False
        if "4033" in s:
            return True
        if "취소할 수량이 없" in s:
            return True
        if "정정/취소할 수량" in s and "없" in s:
            return True
        return False

    @staticmethod
    def _cancel_reject_means_stale_order(ret_msg: Any) -> bool:
        """kt10003: 원주문번호 없음(만료·타계좌·DB만 남은 접수) — 체결 신뢰 대상 아님."""
        try:
            s = str(ret_msg or "")
        except Exception:
            s = ""
        if not s:
            return False
        if "4032" in s:
            return True
        if "원주문번호가 존재하지" in s:
            return True
        if "원주문" in s and "존재하지" in s:
            return True
        return False

    def _notify_no_open_qty_to_fill_service(self, code: str, order_no: str) -> None:
        """RC4033 직후 FillConfirmationService 즉시 반영(터치 poll·타임아웃과 병행)."""
        try:
            fcs = getattr(self.app, "fill_confirmation_service", None)
            if fcs is None:
                return
            oid = str(order_no or "").strip()
            nc = str(code or "").zfill(6)
            dm = getattr(self.app, "data_manager", None)
            row = None
            if dm is not None:
                try:
                    row = dm.get_order_history_row_by_order_no(oid)
                except Exception:
                    row = None
            side = "sell"
            qty = 0
            px = 0.0
            nm = ""
            rs = ""
            kind = ""
            if isinstance(row, dict):
                side = str(row.get("side") or "sell").strip().lower()
                qty = int(row.get("qty", 0) or 0)
                px = float(row.get("price") or 0)
                nm = str(row.get("name") or "")
                rs = str(row.get("reason") or "")
                if "EXIT" in rs.upper():
                    kind = "exit_sell"
            st = getattr(self.app, "settings", None) or {}
            if not bool(st.get("exchange_fill_trust_no_open_qty", True)):
                return

            def _run() -> None:
                try:
                    if side == "sell":
                        fcs.apply_trusted_fill_on_no_open_qty(
                            code=nc,
                            order_no=oid,
                            side=side,
                            qty=qty,
                            fill_price_hint=px,
                            name=nm,
                            reason=rs,
                            kind=kind,
                        )
                except Exception:
                    pass

            try:
                threading.Thread(
                    target=_run, daemon=True, name=f"NoOpenQty-{oid}"
                ).start()
            except Exception:
                _run()
        except Exception:
            pass

    def _safe_float(self, val, default: float = 0.0) -> float:
        """API 숫자 필드(콤마·부호 문자열)를 float로 변환합니다."""
        try:
            if val is None or val == "":
                return default
            return float(str(val).replace(",", "").replace("+", "").strip())
        except Exception:
            return default

    def _merged_tot_evlu_amt(self, merged: dict) -> float:
        """
        ka10085 응답에서 총평가(총자산) 금액 필드를 찾습니다.
        키 이름이 버전·래퍼(output)별로 달라질 수 있어 후보를 순차 시도합니다.
        """
        try:
            bags: List[Dict[str, Any]] = []
            if isinstance(merged, dict):
                bags.append(merged)
                out = merged.get("output")
                if isinstance(out, dict):
                    bags.append(out)
            for bag in bags:
                for key in (
                    "tot_evlu_amt",
                    "tot_evlu_amt_smtl",
                    "tot_asst_amt",
                    "tot_asst_evlu_amt",
                    "evlu_tot_amt",
                    "tot_evlu_amt1",
                ):
                    v = bag.get(key)
                    if v is None or str(v).strip() in ("", "-"):
                        continue
                    x = self._safe_float(v, 0.0)
                    if x > 0:
                        return x
        except Exception:
            pass
        return 0.0

    def fetch_stock_cur_prc_rest(self, stk_cd: str) -> Optional[float]:
        """
        키움 REST `ka10004`(주식호가)로 단일 종목 현재가(cur_prc)를 조회합니다.
        로컬 trading_data.db 캔들에 의존하지 않으며, 웹소켓 틱이 없을 때 폴백용입니다.
        """
        try:
            code = str(stk_cd or "").strip().zfill(6)
        except Exception:
            return None
        if not code or code == "000000":
            return None
        try:
            use_mock = getattr(config, "USE_MOCK", False)
            host = config.MOCK_URL if use_mock else config.URL
            token = self.app.auth_manager.get_auth_token()
            if not token:
                return None
            url = f"{host}/api/dostk/mrkcond"
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "authorization": f"Bearer {token}",
                "api-id": "ka10004",
            }
            res = requests.post(
                url, headers=headers, json={"stk_cd": code}, timeout=8
            )
            if res.status_code != 200:
                return None
            try:
                data = res.json()
            except Exception:
                return None
            if not isinstance(data, dict):
                return None

            def _pick_cur(obj: Any) -> Optional[float]:
                if obj is None:
                    return None
                if isinstance(obj, list):
                    for it in obj:
                        p = _pick_cur(it)
                        if p is not None and p > 0:
                            return p
                    return None
                if not isinstance(obj, dict):
                    return None
                for k in (
                    "cur_prc",
                    "stck_prpr",
                    "prpr",
                    "now_prc",
                    "base_pric",
                    "stck_prpr_nxt",
                ):
                    if k not in obj:
                        continue
                    # cur_prc 등은 등락 부호가 붙어 음수 문자열로 올 수 있음 → 절댓값
                    pv = abs(self._safe_float(obj.get(k), 0.0))
                    if pv > 0:
                        return float(pv)
                return None

            for key in ("output", "output1", "data", "stk_hpga_qry"):
                p = _pick_cur(data.get(key))
                if p is not None and p > 0:
                    return float(p)
            p2 = _pick_cur(data)
            if p2 is not None and p2 > 0:
                return float(p2)
        except requests.RequestException as e:
            try:
                self.logger.debug("fetch_stock_cur_prc_rest 요청 실패: %s", e)
            except Exception:
                pass
        except Exception as e:
            try:
                self.logger.debug("fetch_stock_cur_prc_rest 예외: %s", e)
            except Exception:
                pass
        return None

    def fetch_best_bid_ask_rest(
        self,
        stk_cd: str,
        *,
        prefer_cache: bool = True,
        update_cache: bool = True,
        max_age_sec: Optional[float] = None,
        venue: str = "KRX",
        max_retries: int = 3,
        log_fail: bool = False,
    ) -> Tuple[Optional[float], Optional[float]]:
        """
        키움 REST ``ka10004``(주식호가)로 **매수1호가·매도1호가** 를 조회합니다.

        prefer_cache=True 이면 TTL 내 캐시를 우선 반환해 touch 주문 지연을 줄입니다.
        ``venue="NXT"`` 이면 ``{code}_NX`` 로 NXT 호가를 조회합니다(실전 전용).
        모의/실전 유량(초당 1회)에 맞춰 전역 직렬화·429 재시도합니다.

        Returns
        -------
        (매수1호가, 매도1호가) 또는 실패 시 (None, None).
        """
        try:
            api_stk = self.quote_venue_stk_cd(stk_cd, venue=venue)
            code = str(stk_cd or "").strip().split("_")[0].zfill(6)
        except Exception:
            return None, None
        if not code or code == "000000" or not api_stk:
            return None, None

        if prefer_cache:
            try:
                c_bid, c_ask = self.get_cached_best_bid_ask(
                    code, max_age_sec=max_age_sec, venue=venue
                )
                if (c_bid is not None and float(c_bid) > 0) or (
                    c_ask is not None and float(c_ask) > 0
                ):
                    return c_bid, c_ask
            except Exception:
                pass

        def _hp_int_from_obj(obj: Any) -> Tuple[int, int]:
            """단일 dict 에서 (bid1, ask1) 정수. 없으면 0."""
            if not isinstance(obj, dict):
                return 0, 0
            bid_v = ask_v = 0
            for k in (
                "buy_fpr_bid",
                "buy_frst_bid",
                "fpr_bid_buy",
                "bid_fpr_bid",
            ):
                if k in obj:
                    bid_v = self._safe_int_hp_field(obj.get(k))
                    if bid_v > 0:
                        break
            for k in (
                "sel_fpr_bid",
                "sel_frst_bid",
                "fpr_bid_sel",
                "ask_fpr_bid",
            ):
                if k in obj:
                    ask_v = self._safe_int_hp_field(obj.get(k))
                    if ask_v > 0:
                        break
            return bid_v, ask_v

        def _walk_collect(obj: Any, best: List[int]) -> None:
            """best = [bid, ask] 최종 후보(>0 인 것만 갱신)."""
            if isinstance(obj, dict):
                b, a = _hp_int_from_obj(obj)
                if b > 0:
                    best[0] = b
                if a > 0:
                    best[1] = a
                for v in obj.values():
                    _walk_collect(v, best)
            elif isinstance(obj, list):
                for it in obj:
                    _walk_collect(it, best)

        last_fail = ""
        retries = max(1, min(int(max_retries or 1), 6))
        for attempt in range(retries):
            try:
                use_mock = getattr(config, "USE_MOCK", False)
                host = config.MOCK_URL if use_mock else config.URL
                token = self.app.auth_manager.get_auth_token()
                if not token:
                    last_fail = "no_token"
                    break
                url = f"{host}/api/dostk/mrkcond"
                headers = {
                    "Content-Type": "application/json;charset=UTF-8",
                    "authorization": f"Bearer {token}",
                    "api-id": "ka10004",
                }
                try:
                    _ka10004_wait_global_slot(self.app)
                except Exception:
                    pass
                res = requests.post(
                    url, headers=headers, json={"stk_cd": api_stk}, timeout=8
                )
                http_st = int(getattr(res, "status_code", 0) or 0)
                data: Any = None
                try:
                    data = res.json()
                except Exception:
                    data = None
                rc = None
                rm = ""
                if isinstance(data, dict):
                    try:
                        rc = data.get("return_code")
                    except Exception:
                        rc = None
                    try:
                        rm = str(data.get("return_msg") or "")
                    except Exception:
                        rm = ""

                # 유량 초과: 대기 후 재시도
                rate_limited = http_st == 429 or (
                    isinstance(rc, (int, float)) and int(rc) == 5
                ) or ("허용된 요청" in rm)
                if rate_limited:
                    last_fail = f"http_{http_st}_rc={rc}_rate_limit"
                    if attempt + 1 < retries:
                        time.sleep(1.05 + 0.15 * float(attempt))
                        continue
                    break

                if http_st != 200:
                    last_fail = f"http_{http_st}_rc={rc}"
                    if attempt + 1 < retries:
                        time.sleep(0.35)
                        continue
                    break

                if not isinstance(data, dict):
                    last_fail = "bad_json"
                    break

                acc = [0, 0]
                _walk_collect(data, acc)
                bid1, ask1 = acc[0], acc[1]
                if bid1 <= 0 and ask1 <= 0:
                    last_fail = f"empty_bid_ask_rc={rc}"
                    if attempt + 1 < retries:
                        time.sleep(0.35)
                        continue
                    break
                out_bid = float(bid1) if bid1 > 0 else None
                out_ask = float(ask1) if ask1 > 0 else None
                if update_cache:
                    try:
                        self.cache_best_bid_ask(
                            code, out_bid, out_ask, venue=venue
                        )
                    except Exception:
                        pass
                return out_bid, out_ask
            except requests.RequestException as e:
                last_fail = f"req:{e}"
                try:
                    self.logger.debug("fetch_best_bid_ask_rest 요청 실패: %s", e)
                except Exception:
                    pass
                if attempt + 1 < retries:
                    time.sleep(0.5)
                    continue
            except Exception as e:
                last_fail = f"exc:{e}"
                try:
                    self.logger.debug("fetch_best_bid_ask_rest 예외: %s", e)
                except Exception:
                    pass
                break

        if log_fail or last_fail:
            try:
                msg = (
                    f"⚠️ [ka10004] {code}({venue}) 호가 조회 실패 "
                    f"({last_fail or 'unknown'}, tries={retries})"
                )
                if log_fail:
                    self.app.log(msg)
                else:
                    self.logger.debug(msg)
            except Exception:
                pass
        return None, None

    def _safe_int_hp_field(self, v: Any) -> int:
        """호가 문자열(앞자리 부호 등) → 양의 정수 가격."""
        try:
            x = abs(float(str(v).replace(",", "").strip()))
            if x > 0:
                return int(round(x))
        except (TypeError, ValueError):
            pass
        return 0

    def modify_limit_order(
        self,
        orig_ord_no: str,
        stk_cd: str,
        mdfy_qty: int,
        mdfy_uv: int,
    ) -> Tuple[bool, str, str]:
        """
        키움 REST ``kt10002`` 지정가 **정정** 주문.

        Returns
        -------
        (성공 여부, 응답 주문번호 또는 빈 문자열, 메시지)
        """
        use_mock = getattr(config, "USE_MOCK", False)
        mode_str = "모의투자" if use_mock else "실전투자"
        oid = str(orig_ord_no or "").strip()
        code = str(stk_cd or "").zfill(6)
        if not oid or not code or code == "000000":
            return False, "", "주문번호/종목코드 누락"
        if not self.order_api:
            return False, "", "주문 엔진 없음"
        allow_order = (
            getattr(config, "ALLOW_MOCK_ORDER", False)
            if use_mock
            else getattr(config, "ALLOW_REAL_ORDER", False)
        )
        if not allow_order:
            try:
                self.app.log(f"⚠️ [{mode_str}] 정정 생략 (Safety Switch: OFF)")
            except Exception:
                pass
            return False, "", "safety_switch_off"
        try:
            mq = max(1, int(mdfy_qty))
            uv = max(1, int(mdfy_uv))
        except (TypeError, ValueError):
            return False, "", "수량/단가 숫자 변환 실패"
        try:
            res = self.order_api.stock_modify_order_request_kt10002(
                dmst_stex_tp=self.dmst_stex_tp_for_orders(),
                orig_ord_no=oid,
                stk_cd=code,
                mdfy_qty=str(mq),
                mdfy_uv=str(uv),
            )
            ret_code = int(res.get("return_code", -1) or -1)
            ret_msg = str(res.get("return_msg", "") or "")
            out_ord = str(res.get("ord_no", "") or "").strip()
            if ret_code == 0 or out_ord:
                try:
                    self.app.log(
                        f"✅ [{mode_str}] 정정 접수: 원={oid} → 응답ord={out_ord or '-'} "
                        f"{code} 잔량{mq}@{uv:,}원"
                    )
                except Exception:
                    pass
                return True, out_ord, ret_msg
            try:
                self.app.log(
                    f"❌ [{mode_str}] 정정 거부: {ret_msg} (code={ret_code})"
                )
            except Exception:
                pass
            return False, "", ret_msg
        except Exception as e:
            try:
                self.app.log(f"💥 [{mode_str}] 정정 예외: {e}")
            except Exception:
                pass
            return False, "", str(e)

    def _dostk_stk_cd_try_list(self, stk_cd: str) -> List[str]:
        """REST ``stk_cd`` 파라미터 후보(6자리·앞자리 0 제거 등)."""
        try:
            c = str(stk_cd or "").strip().zfill(6)
        except Exception:
            return []
        if not c or c == "000000":
            return []
        out: List[str] = []
        for x in (c, c.lstrip("0") or c):
            if x and x not in out:
                out.append(x)
        return out or [c]

    def _pick_prev_close_from_flat_stkinfo_row(
        self, row: Any
    ) -> Tuple[Optional[float], Optional[str]]:
        """
        ka10001(stkinfo) 한 행에서 **직전 장마감 시세(현재가 필드)** 를 우선하고,
        없거나 0이면 명세상 전일 종가 필드를 봅니다.

        - ``cur_prc``: **최우선**. 직전 장마감 기준 시세(값 앞 ``+``/``-`` 는 등락 표시,
          ``abs``로 가격만 취함). 일부 종목에서 ``prdy_clpr`` 계열이 하루 이상 밀리는
          경우가 있어 기본 표시·기동 시 참고가는 여기를 믿는다.
        - ``pred_close_pric`` / ``prdy_clpr`` / ``stck_prdy_clpr``: ``cur_prc`` 로
          채울 수 없을 때만 사용.

        ``base_pric``(기준가)·``bfdy_clpr``(전전일 등)·``yst_clpr`` 는 실제 장마감일과
        어긋나는 사례가 있어 **쓰지 않습니다.**
        """
        if not isinstance(row, dict):
            return None, None
        try:
            if "cur_prc" in row:
                v = abs(self._safe_float(row.get("cur_prc"), 0.0))
                if v > 0:
                    return float(v), "cur_prc"
        except Exception:
            pass
        for k in (
            "pred_close_pric",
            "prdy_clpr",
            "stck_prdy_clpr",
        ):
            if k not in row:
                continue
            try:
                v = self._safe_float(row.get(k), 0.0)
                if v > 0:
                    return float(v), k
            except Exception:
                pass
        return None, None

    def _pick_prev_close_from_ka10001_response(
        self, data: dict
    ) -> Tuple[Optional[float], Optional[str], Optional[dict]]:
        """
        stkinfo 응답: ``output`` / ``output1`` / ``output2`` 블록을 **루트 본문보다 먼저** 본다.
        일부 실전 응답에서 루트에만 지연·오래된 ``prdy_clpr`` 가 깔리고, 실제 시세 행은
        ``output`` 에 있어 2거래일 전 종가가 선택되는 사례가 있어 순서를 바꾼다.
        루트 평탄 JSON(모의 등)은 ``output`` 이 없으면 마지막에 루트만 조회된다.
        """
        if not isinstance(data, dict):
            return None, None, None
        try:
            chunks: List[Any] = []
            for key in ("output", "output1", "output2"):
                blk = data.get(key)
                if isinstance(blk, dict):
                    chunks.append(blk)
                elif isinstance(blk, list):
                    for it in blk[:20]:
                        if isinstance(it, dict):
                            chunks.append(it)
            chunks.append(data)
            for ch in chunks:
                hit, pk = self._pick_prev_close_from_flat_stkinfo_row(ch)
                if hit is not None:
                    return hit, pk, ch
                try:
                    for v in ch.values():
                        if isinstance(v, dict):
                            hit2, pk2 = self._pick_prev_close_from_flat_stkinfo_row(v)
                            if hit2 is not None:
                                return hit2, pk2, v
                except Exception:
                    pass
        except Exception:
            pass
        return None, None, None

    def _extract_prev_clpr_deep(self, root: Any) -> Optional[float]:
        """
        비정형 래핑 시 dict마다 ``cur_prc``(절댓값) 우선, 이후 전일 전용 키만 순회합니다.
        기준가·전전일·기타 필드는 제외합니다.
        """
        seen: Set[int] = set()
        pkeys = (
            "pred_close_pric",
            "prdy_clpr",
            "stck_prdy_clpr",
        )

        def _vis(x: Any) -> Optional[float]:
            if x is None:
                return None
            if isinstance(x, (dict, list)):
                xid = id(x)
                if xid in seen:
                    return None
                seen.add(xid)
                if isinstance(x, dict):
                    try:
                        if "cur_prc" in x:
                            vcp = abs(self._safe_float(x.get("cur_prc"), 0.0))
                            if vcp > 0:
                                return float(vcp)
                    except Exception:
                        pass
                    for k in pkeys:
                        if k in x:
                            try:
                                v = self._safe_float(x.get(k), 0.0)
                                if v > 0:
                                    return float(v)
                            except Exception:
                                pass
                    for v in x.values():
                        r = _vis(v)
                        if r is not None and r > 0:
                            return r
                    return None
                for it in x:
                    r = _vis(it)
                    if r is not None and r > 0:
                        return r
                return None
            return None

        try:
            return _vis(root)
        except Exception:
            return None

    def fetch_stock_prev_close_ka10001(
        self, stk_cd: str, *, silent: bool = False
    ) -> Optional[float]:
        """
        키움 REST ``ka10001``(주식기본정보)로 **직전 장마감 시세** 를 조회합니다.
        응답 행에서 ``cur_prc`` 절댓값을 **최우선**(기동·가상보유 참고가· 일부 종목의 지연된
        ``prdy_clpr`` 회피), 없을 때만 ``pred_close_pric``·``prdy_clpr``·``stck_prdy_clpr``.
        ``base_pric`` 등 기준가·전전일 전용으로 오인되기 쉬운 필드는 쓰지 않습니다.
        호가(``/api/dostk/mrkcond``, ka10004)와 달리 본 TR은 **``POST /api/dostk/stkinfo``** 를 써야 합니다.
        ``kt10001`` 은 매도 주문 TR이므로 전일가 조회에 사용하지 않습니다.
        """
        try:
            code = str(stk_cd or "").strip().zfill(6)
        except Exception:
            return None
        if not code or code == "000000":
            return None
        try:
            use_mock = getattr(config, "USE_MOCK", False)
            host = config.MOCK_URL if use_mock else config.URL
            token = self.app.auth_manager.get_auth_token()
            if not token:
                return None
            url_stkinfo = f"{host}/api/dostk/stkinfo"
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "authorization": f"Bearer {token}",
                "api-id": "ka10001",
            }
            for param in self._dostk_stk_cd_try_list(code):
                try:
                    res = requests.post(
                        url_stkinfo,
                        headers=headers,
                        json={"stk_cd": param},
                        timeout=8,
                    )
                except requests.RequestException:
                    continue
                if res.status_code != 200:
                    continue
                try:
                    data = res.json()
                except Exception:
                    continue
                if not isinstance(data, dict):
                    continue
                try:
                    rc = data.get("return_code", data.get("rt_cd", 0))
                    if rc is not None and str(rc).strip() not in ("", "0", "None"):
                        try:
                            ri = int(rc)
                            if ri != 0:
                                continue
                        except (TypeError, ValueError):
                            continue
                except Exception:
                    pass
                pv, _, _ = self._pick_prev_close_from_ka10001_response(data)
                if pv is None:
                    pv = self._extract_prev_clpr_deep(data)
                if pv is not None and pv > 0:
                    return float(pv)
            return None
        except Exception as e:
            try:
                self.logger.debug("fetch_stock_prev_close_ka10001 예외: %s", e)
            except Exception:
                pass
            return None

    def _sum_holdings_market_value(self, holdings: list) -> float:
        """
        ka10085 acnt_prft_rt 행별 주식 순수 평가(현재가×수량) 합산.
        ``evlu_amt`` 등은 증권사/필드에 따라 매입·제비용 정의 차이가 있어,
        현재가 필드가 있으면 우선 현재가×수량만 사용합니다(매입수수료 미반영).
        현재가·평가액 모두 없을 때만 평단×수량으로 폴백합니다.
        """
        s = 0.0
        for row in holdings:
            if not isinstance(row, dict):
                continue
            try:
                qty = self._safe_float(
                    row.get("rmnd_qty") or row.get("hldg_qty") or row.get("qty") or 0
                )
                if qty <= 0:
                    continue
                cur = self._safe_float(
                    row.get("cur_prc")
                    or row.get("prpr")
                    or row.get("stck_prpr")
                    or row.get("now_prc")
                    or 0
                )
                if cur > 0:
                    s += cur * qty
                    continue
                ev = self._safe_float(
                    row.get("evlu_amt") or row.get("stk_evlu_amt") or 0
                )
                if ev > 0:
                    s += ev
                    continue
                avg = self._safe_float(
                    row.get("pchs_avg_pric")
                    or row.get("pur_pric")
                    or row.get("avg_price")
                    or row.get("bpsp_avg_pric")
                    or 0
                )
                if avg > 0:
                    s += avg * qty
            except Exception:
                continue
        return s

    def _resolve_total_equity(
        self,
        merged: dict,
        holdings: list,
        stocks_mv: float,
        api_tot: float,
        d1: float,
        d2: float,
    ) -> tuple[float, float, float]:
        """
        tot_evlu_amt 가 '예수+주식' 총자산이 아니라 주식평가만 담는 경우가 있어
        D+2 예수금 + 주식평가 합과 비교해 총자산을 확정합니다.
        """
        try:
            # d2<0 은 미수·결제부족 등으로 유효할 수 있음 — d2<=0 이면 양수 주문가능으로 덮어쓰면 D+2가 왜곡됨
            if d2 == 0 and (len(holdings) > 0 or stocks_mv > 0):
                dep = self.get_deposit()
                if dep and float(dep) > 0:
                    d2 = float(dep)
                    if d1 <= 0:
                        d1 = d2
        except Exception:
            pass

        combined = float(d2) + float(stocks_mv)

        try:
            if len(holdings) == 0 and stocks_mv <= 0:
                tot = api_tot if api_tot > 0 else combined
                if tot <= 0 and d2 > 0:
                    tot = float(d2)
                return (tot, d1, d2)

            if combined <= 0:
                return (api_tot if api_tot > 0 else d2, d1, d2)

            # API 총자산이 (예수+주식)과 거의 같으면 API 값 사용
            if api_tot > 0 and abs(api_tot - combined) / max(combined, 1.0) < 0.02:
                return (api_tot, d1, d2)

            # 예수금 필드가 비어 합산이 실제보다 작은데 API 총자산이 더 크면 API 우선
            if api_tot > 0 and api_tot > combined * 1.08:
                return (api_tot, d1, d2)

            # 합산이 증권사 총평가(tot_evlu_amt)보다 크게 튀면 — D+2·평가 필드 오인식 등 — 총평가 우선
            if api_tot > 0 and combined > api_tot * 1.03:
                return (api_tot, d1, d2)

            # 그 외에는 합산(예수+주식평가)을 총자산으로 사용
            return (combined, d1, d2)
        except Exception:
            return (combined if combined > 0 else api_tot, d1, d2)

    def _kiwoom_single_record_dict(self, data: Any) -> Optional[dict]:
        """
        키움 REST 단건(예수금상세 등) 응답에서 레코드 dict 한 개를 꺼낸다.
        ``output`` 만 dict 인 줄 알고 파싱하면 ``output1`` 리스트 형태일 때 전부 실패해 D+1/D+2가 0으로 남을 수 있다.
        루트·output 병합(모의 kt00001 등)도 처리한다.
        """
        _DEP_KEYS = (
            "d1_entra",
            "d2_entra",
            "d1_entra_amt",
            "d2_entra_amt",
            "d1_samt",
            "d2_samt",
            "entr",
            "pymn_alow_amt",
            "ord_alow_amt",
            "nstd_re_ib_amt",
        )
        try:
            if not isinstance(data, dict):
                return None
            merged: dict = {}
            skip = frozenset(
                ("output", "output1", "output2", "data", "acnt_bal", "return_msg")
            )
            for k, v in data.items():
                if k in skip or k.startswith("return_") or k in ("rt_cd", "return_cd"):
                    continue
                if isinstance(v, (dict, list)):
                    continue
                merged[k] = v
            for key in ("output", "output1", "output2", "data", "acnt_bal"):
                ob = data.get(key)
                if isinstance(ob, dict) and ob:
                    merged = {**merged, **ob}
                elif isinstance(ob, list) and ob:
                    first = ob[0]
                    if isinstance(first, dict) and first:
                        merged = {**merged, **first}
            if merged and any(k in merged for k in _DEP_KEYS):
                return merged
            if any(k in data for k in _DEP_KEYS):
                return dict(data)
        except Exception as e:
            try:
                self.logger.debug("_kiwoom_single_record_dict 오류: %s", e)
            except Exception:
                pass
        return None

    def _deposit_fields_from_body(
        self, out_body: dict, *, qry_tp: str
    ) -> Optional[Dict[str, Any]]:
        """예수금상세 레코드 dict → 표준 deposit_detail dict."""
        try:
            if not isinstance(out_body, dict) or not out_body:
                return None

            def _pick(*keys: str) -> float:
                for k in keys:
                    v = out_body.get(k)
                    if v is not None and str(v).strip() not in ("", "-"):
                        return self._safe_float(v)
                return 0.0

            def _pick_with_key(*keys: str) -> Tuple[float, str]:
                for k in keys:
                    v = out_body.get(k)
                    if v is not None and str(v).strip() not in ("", "-"):
                        return self._safe_float(v), k
                return 0.0, ""

            d1, d1_src = _pick_with_key(
                "d1_samt",
                "d1_entra",
                "d1_entra_amt",
                "entr_d1",
                "d1_entr",
                "d1_pymn_alow_amt",
            )
            d2, d2_src = _pick_with_key(
                "d2_samt",
                "d2_entra",
                "d2_entra_amt",
                "entr_d2",
                "d2_entr",
                "d2entra",
                "d2_pymn_alow_amt",
            )
            entr = _pick("entr")
            ord_alow = _pick(
                "ord_alow_amt",
                "ord_alow_amt_entr",
                "nstd_re_ib_amt",
            )
            pymn = _pick(
                "pymn_alow_amt",
                "pymn_alow_amt_entr",
                "pymn_alow",
                "d2_pymn_alow_amt",
                "d1_pymn_alow_amt",
            )
            return {
                "d1": d1,
                "d2": d2,
                "entr": entr,
                "ord_alow": ord_alow,
                "pymn_alow": pymn,
                "qry_tp_used": qry_tp,
                "d1_src": d1_src,
                "d2_src": d2_src,
            }
        except Exception:
            return None

    def _fetch_deposit_detail_payload(self, qry_tp: str) -> Optional[dict]:
        """예수금상세(kt00001) TR 1회 호출 — SDK 우선, 실패 시 REST(/api/dostk/acnt)."""
        use_mock = getattr(config, "USE_MOCK", False)
        # 모의·실전 모두 예수금상세 API ID는 kt00001 (ka10001은 stkinfo용)
        if self.account_api is not None:
            try:
                fn = getattr(
                    self.account_api,
                    "deposit_detail_status_request_kt00001",
                    None,
                )
                if callable(fn):
                    data = fn(qry_tp=str(qry_tp))
                    if isinstance(data, dict):
                        return data
            except Exception as ex:
                try:
                    self.logger.debug("kt00001 SDK qry_tp=%s: %s", qry_tp, ex)
                except Exception:
                    pass

        api_id = "kt00001"
        host = config.MOCK_URL if use_mock else config.URL
        token = self.app.auth_manager.get_auth_token()
        if not token:
            return None
        url = f"{host}/api/dostk/acnt"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "api-id": api_id,
            "cont-yn": "N",
            "next-key": "",
        }
        try:
            res = requests.post(
                url, headers=headers, json={"qry_tp": str(qry_tp)}, timeout=12
            )
            if res.status_code != 200:
                return None
            data = res.json()
            return data if isinstance(data, dict) else None
        except Exception as ex:
            try:
                self.logger.debug("예수금상세 REST qry_tp=%s: %s", qry_tp, ex)
            except Exception:
                pass
            return None

    def _summarize_holdings_qty_positive(self, holdings: list) -> tuple[int, str]:
        """
        ka10085 acnt_prft_rt 중 잔고수량>0인 행만 모아 (종목 수, 로그용 요약 문자열) 반환.
        청산 완료(0주) 행은 API에 남아 있어도 제외합니다.
        """
        parts: list[str] = []
        try:
            for row in holdings:
                if not isinstance(row, dict):
                    continue
                code = str(
                    row.get("stk_cd") or row.get("pdno") or row.get("code") or "?"
                ).strip()
                name = str(
                    row.get("stk_nm")
                    or row.get("prdt_name")
                    or row.get("name")
                    or "?"
                ).strip()
                qty_raw = row.get("rmnd_qty") or row.get("hldg_qty") or row.get("qty") or 0
                try:
                    qty = int(
                        float(
                            str(qty_raw).replace(",", "").replace("+", "").strip() or 0
                        )
                    )
                except (TypeError, ValueError):
                    qty = 0
                if qty <= 0:
                    continue
                parts.append(f"{code} {name}×{qty}주")
        except Exception:
            pass
        detail = " | ".join(parts) if parts else "(해당 없음)"
        if len(detail) > 1500:
            detail = detail[:1500] + " ...(생략)"
        return (len(parts), detail)

    def get_holdings(
        self,
        *,
        refresh_cash_deposit: bool = True,
        log_summary: bool = True,
        sync_context: str = "",
    ):
        """
        실시간 계좌 잔고 동기화 (ka10085 규격).

        refresh_cash_deposit=False 인 경우: 보유 목록(ka10085)만 사용하고
        예수금상세(kt00001)·별도 예수 TR(get_deposit) 호출을 생략합니다.
        log_summary=False: 요약 로그 생략(체결 후 silent 동기화 등).
        sync_context: 로그 접두 구분 (예: post_fill_holdings, post_fill_full).
        """
        if not self.account_api:
            return None

        use_mock = getattr(config, 'USE_MOCK', False)
        mode_str = "모의투자" if use_mock else "실전투자"
        
        try:
            # ka10085: 계좌 수익률/잔고 요청
            # stex_tp "0": 통합거래소
            res = self.account_api.account_return_rate_request_ka10085(stex_tp="0")
            if not isinstance(res, dict):
                res = {}

            # 응답이 output 래퍼 안에 있을 수 있어 상위와 병합
            merged: dict = dict(res)
            out = res.get("output")
            if isinstance(out, dict):
                merged = {**res, **out}
            
            ret_code = merged.get("return_code", res.get("return_code", -1))
            if ret_code == 0:
                holdings = merged.get("acnt_prft_rt") or res.get("acnt_prft_rt") or []
                if not isinstance(holdings, list):
                    holdings = []
                api_tot = self._merged_tot_evlu_amt(merged)
                d1_deposit = self._safe_float(merged.get("d1_samt", 0))
                d2_deposit = self._safe_float(merged.get("d2_samt", 0))
                stocks_mv = self._sum_holdings_market_value(holdings)
                total_equity, d1_deposit, d2_deposit = self._resolve_total_equity(
                    merged, holdings, stocks_mv, api_tot, d1_deposit, d2_deposit
                )

                cash_orderable = 0.0  # 주문가능(ord_alow) — 로그·D+2 비교용. 포지션 탭 '예수금'과 별개.
                cash_withdrawable = 0.0  # 출금가능(pymn_alow) — 포지션 탭 예수금(보유 현금)으로만 사용.

                if refresh_cash_deposit:
                    # [v4.8.5] 보유 없을 때 총자산·예수금 보정
                    if total_equity == 0 and len(holdings) == 0:
                        try:
                            deposit = self.get_deposit()
                            if deposit:
                                total_equity = float(deposit)
                                d1_deposit = total_equity
                                d2_deposit = total_equity
                        except Exception as dep_ex:
                            try:
                                self.app.log(f"⚠️ [계좌] 예수 보정(get_deposit) 실패: {dep_ex}")
                            except Exception:
                                pass

                    # ka10085의 d1_samt/d2_samt는 총자산과 혼동·누락되는 경우가 있어 예수금상세 TR로 보강
                    try:
                        dep_detail = self.get_deposit_detail()
                    except Exception as det_ex:
                        dep_detail = None
                        try:
                            self.app.log(f"⚠️ [계좌] 예수금상세 조회 실패: {det_ex}")
                        except Exception:
                            pass
                    if dep_detail:
                        d1d = float(dep_detail.get("d1") or 0)
                        d2d = float(dep_detail.get("d2") or 0)
                        # 예수금상세(kt00001)가 D+1/D+2 권위 — d2d>0 만 쓰면 음수(D+2 부족)가 무시되어 동기화 표시가 틀어짐
                        d1_deposit = d1d
                        d2_deposit = d2d
                        try:
                            _ov = float(dep_detail.get("ord_alow") or 0)
                            if _ov > 0:
                                cash_orderable = _ov
                        except (TypeError, ValueError):
                            cash_orderable = 0.0
                        try:
                            _pv = float(dep_detail.get("pymn_alow") or 0)
                            cash_withdrawable = max(0.0, _pv)
                        except (TypeError, ValueError):
                            cash_withdrawable = 0.0

                    # 예수금상세로 d2 보강 후: 총자산 = D+2 예수 + 보유 평가 합 (tot_evlu_amt는 이전 d2/예수 기준으로 남을 수 있음)
                    stocks_mv = self._sum_holdings_market_value(holdings)
                    try:
                        total_equity = float(d2_deposit) + float(stocks_mv)
                    except Exception:
                        pass

                n_hold, _hold_detail = self._summarize_holdings_qty_positive(holdings)

                if log_summary:
                    _ctx = str(sync_context or "").strip()
                    if _ctx == "post_fill_full":
                        _tag = "체결후"
                    elif _ctx == "post_fill_holdings":
                        _tag = "체결후·보유"
                    else:
                        _tag = mode_str

                    if refresh_cash_deposit:
                        # 주문가능(ord)은 키움에서 보통 D+2 추정예수와 같게 나오는 경우가 많음 — 동일하면 한 번만 표기
                        try:
                            ca = float(cash_orderable or 0)
                            d2v = float(d2_deposit or 0)
                            d1v = float(d1_deposit or 0)
                            tol = max(1.0, d2v * 0.0001) if d2v > 0 else 1.0
                            if ca > 0 and abs(ca - d2v) > tol:
                                dep_seg = (
                                    f"주문가능금액 {ca:,.0f}원 | D+2 예수금 {d2v:,.0f}원 | "
                                    f"D+1 예수금 {d1v:,.0f}원"
                                )
                            else:
                                show_amt = d2v if d2v > 0 else ca
                                dep_seg = (
                                    f"주문가능금액(D+2예수) {show_amt:,.0f}원 | "
                                    f"D+1 예수금 {d1v:,.0f}원"
                                )
                        except Exception:
                            dep_seg = (
                                f"주문가능금액(D+2예수) {d2_deposit:,.0f}원 | "
                                f"D+1 예수금 {d1_deposit:,.0f}원"
                            )
                        try:
                            self.app.log(
                                f"✅ [{_tag}] 계좌 동기화 | 총자산 {total_equity:,.0f}원 | "
                                f"{dep_seg} | 실질 보유 {n_hold}종목"
                            )
                        except Exception as le:
                            try:
                                self.app.log(f"⚠️ [계좌] 동기화 요약 로그 실패: {le}")
                            except Exception:
                                pass
                    else:
                        try:
                            self.app.log(
                                f"✅ [{_tag}] 보유 동기화 | 실질 보유 {n_hold}종목"
                            )
                        except Exception as le:
                            try:
                                self.app.log(f"⚠️ [계좌] 정합 로그 실패: {le}")
                            except Exception:
                                pass

                return {
                    "holdings": holdings,
                    "total_equity": total_equity,
                    "d1_deposit": d1_deposit,
                    "d2_deposit": d2_deposit,
                    # 주문가능(로그 등) / 출금가능(포지션 탭 예수금 = actual_deposit)
                    "cash_orderable": cash_orderable,
                    "cash_withdrawable": cash_withdrawable,
                    # 하위 호환: 예전 키 — 출금가능과 동일 의미로 맞춤(주문가능 아님)
                    "cash_available": cash_withdrawable,
                    # ka10085 보유 행 합산 평가액(evlu_amt 등) — 가상탭·총자산 표시용
                    "stocks_market_value": float(stocks_mv),
                    # 응답 원본 총평가금액(증권사 기준 총자산) — UI 최우선 표시
                    "tot_evlu_amt": float(api_tot),
                }
            elif ret_code == 429 or "429" in str(res):
                # [v4.5.1 API Throttle] 전용 서버 조회 건수 초과 대응
                self.app.log("⚠️ [Throttle] 키움 서버 조회 제한(429)에 도달했습니다. 1~2분 후 자동으로 동기화됩니다.")
                return None
            else:
                msg = str(res.get("return_msg", "Unknown error") or "")
                if "8005" in msg or int(ret_code or 0) == 3:
                    try:
                        self.app.auth_manager.invalidate_cached_token()
                    except Exception:
                        pass
                    try:
                        self.app.get_auth_token()
                    except Exception:
                        pass
                    self.app.log(
                        "⚠️ [계좌] 토큰 무효(8005) — 캐시 삭제 후 재인증을 시도합니다."
                    )
                else:
                    self.app.log(f"⚠️ 계좌 조회 실패: {msg} (ErrorCode: {ret_code})")
                return None

        except Exception as e:
            self.app.log(f"💥 계좌 조회 중 네트워크/파싱 오류: {str(e)}")
            return None

    def get_deposit_detail(self) -> Optional[Dict[str, Any]]:
        """
        예수금상세(kt00001, 모의·실전 동일). ka10085의 d1_samt/d2_samt와 달리
        D+1/D+2 추정예수금(d1_entra/d2_entra)·주문가능금액(ord_alow_amt)이 분리되어 옵니다.
        qry_tp: 3=추정조회(추정 예수 우선), 2=일반조회.
        """
        mode = "kt00001"
        last_msg = ""
        for qry_tp in ("3", "2"):
            try:
                data = self._fetch_deposit_detail_payload(qry_tp)
                if not isinstance(data, dict):
                    continue
                last_msg = str(
                    data.get("return_msg", data.get("msg", "")) or last_msg
                )
                rc = data.get("return_code", data.get("rt_cd", data.get("return_cd")))
                try:
                    if rc is not None and int(str(rc).strip()) != 0:
                        continue
                except (TypeError, ValueError):
                    pass

                out_body = self._kiwoom_single_record_dict(data)
                if not isinstance(out_body, dict) or not out_body:
                    continue
                rc2 = out_body.get("return_code", out_body.get("rt_cd"))
                try:
                    if rc2 is not None and int(str(rc2).strip()) != 0:
                        continue
                except (TypeError, ValueError):
                    pass

                parsed = self._deposit_fields_from_body(out_body, qry_tp=qry_tp)
                if parsed is not None:
                    return parsed
            except Exception as e:
                try:
                    self.logger.debug("예수금상세 qry_tp=%s 실패: %s", qry_tp, e)
                except Exception:
                    pass
                continue
        try:
            extra = f" ({last_msg})" if last_msg else ""
            self.app.log(
                f"⚠️ [계좌] 예수금상세({mode}) 파싱 실패{extra} — "
                "잔고(ka10085)는 반영됐을 수 있으나 D+1/D+2·출금가능은 ka10085 값으로 표시됩니다."
            )
        except Exception:
            pass
        return None

    def get_withdrawable_deposit(self) -> Optional[int]:
        """
        출금가능금액(pymn_alow)만 반환.
        포지션 탭 '예수금'(actual_deposit)·sync_deposit 전용. 주문가능(ord_alow)과 혼동 금지.
        """
        try:
            d = self.get_deposit_detail()
            if d:
                v = float(d.get("pymn_alow") or 0)
                return int(max(0, v))
        except Exception:
            pass
        use_mock = getattr(config, 'USE_MOCK', False)
        # 모의·실전 동일: 예수금상세는 kt00001 (/api/dostk/acnt)
        api_id = 'kt00001'
        host = config.MOCK_URL if use_mock else config.URL

        token = self.app.auth_manager.get_auth_token()
        if not token:
            return None

        url = f"{host}/api/dostk/acnt"
        headers = {
            'Content-Type': 'application/json;charset=UTF-8',
            'authorization': f'Bearer {token}',
            'api-id': api_id,
        }

        try:
            payload = {'qry_tp': '2'}
            res = requests.post(url, headers=headers, json=payload, timeout=10)
            data = res.json()
            out_body = data.get('output', data.get('data', data))

            raw = out_body.get("pymn_alow_amt") or out_body.get("pymn_alow") or "0"
            return int(max(0, float(str(raw).replace(",", ""))))
        except Exception as e:
            self.app.log(f"❌ 출금가능 예수 조회 실패: {e}")
            return None

    def get_deposit(self) -> Optional[int]:
        """내부 총자산·D+2 보정용 — 주문가능·출금·D+2·entr 중 양수 우선(기존 동작). UI 예수금은 get_withdrawable_deposit()."""
        try:
            d = self.get_deposit_detail()
            if d:
                for k in ("ord_alow", "pymn_alow", "d2", "entr"):
                    v = float(d.get(k) or 0)
                    if v > 0:
                        return int(v)
        except Exception:
            pass
        use_mock = getattr(config, 'USE_MOCK', False)
        api_id = 'kt00001'
        host = config.MOCK_URL if use_mock else config.URL

        token = self.app.auth_manager.get_auth_token()
        if not token:
            return None

        url = f"{host}/api/dostk/acnt"
        headers = {
            'Content-Type': 'application/json;charset=UTF-8',
            'authorization': f'Bearer {token}',
            'api-id': api_id,
        }

        try:
            payload = {'qry_tp': '2'}
            res = requests.post(url, headers=headers, json=payload, timeout=10)
            data = res.json()
            out_body = data.get('output', data.get('data', data))

            # kt00001: entr(예수금) 우선, 없으면 nstd_re_ib_amt(주문가능현금)
            deposit_str = out_body.get('entr', out_body.get('nstd_re_ib_amt', '0'))
            return int(float(str(deposit_str).replace(',', '')))
        except Exception as e:
            self.app.log(f"❌ 예수금 상세 조회 실패: {e}")
            return None

    def cancel_order(
        self,
        order_no: str,
        stk_cd: str,
        cncl_qty: str = "0",
    ) -> Literal["cancelled", "failed", "no_open_qty", "stale_ord_no"]:
        """
        미체결 지정가 주문 취소 (kt10003).
        cncl_qty: '0' 이면 잔량 전부 취소.

        Returns
        -------
        Literal["cancelled", "failed", "no_open_qty", "stale_ord_no"]
            - cancelled: 취소 접수 성공(미체결 잔량이 있었고 취소됨)
            - no_open_qty: 취소할 미체결 없음(통상 전량 체결 직후). DB를 CANCELLED로 덮지 않음.
            - stale_ord_no: 원주문 없음(RC4032 등) — 로컬 스냅샷·접수 로그만 정리
            - failed: 그 외 거부·예외
        """
        use_mock = getattr(config, "USE_MOCK", False)
        mode_str = "모의투자" if use_mock else "실전투자"
        oid = str(order_no or "").strip()
        code = str(stk_cd or "").zfill(6)
        if not oid or not code:
            self.app.log(f"⚠️ [{mode_str}] 취소 실패: 주문번호/종목코드 누락")
            return "failed"
        if not self.order_api:
            self.app.log(f"❌ [{mode_str}] 주문 엔진 없음 — 취소 불가")
            return "failed"
        allow_order = getattr(config, "ALLOW_MOCK_ORDER", False) if use_mock else getattr(
            config, "ALLOW_REAL_ORDER", False
        )
        if not allow_order:
            self.app.log(f"⚠️ [{mode_str}] 취소 생략 (Safety Switch: OFF)")
            return "failed"
        try:
            res = self.order_api.stock_cancel_order_request_kt10003(
                dmst_stex_tp=self.dmst_stex_tp_for_orders(),
                orig_ord_no=oid,
                stk_cd=code,
                cncl_qty=str(cncl_qty).strip() or "0",
            )
            ret_code = res.get("return_code", -1)
            ret_msg = res.get("return_msg", "")
            out_ord = str(res.get("ord_no", "") or "").strip()
            if not (ret_code == 0 or out_ord) and self._cancel_reject_means_no_open_qty(
                ret_msg
            ):
                try:
                    self.app.log(
                        f"ℹ️ [{mode_str}] 주문 취소 거부(미체결 없음·통상 전량체결): "
                        f"{ret_msg} — 즉시 체결 반영 시도"
                    )
                except Exception:
                    pass
                try:
                    self._notify_no_open_qty_to_fill_service(code, oid)
                except Exception:
                    pass
                return "no_open_qty"
            if not (ret_code == 0 or out_ord) and self._cancel_reject_means_stale_order(
                ret_msg
            ):
                try:
                    self.app.log(
                        f"ℹ️ [{mode_str}] 주문 취소 생략(원주문 없음·만료): {ret_msg}"
                    )
                except Exception:
                    pass
                try:
                    dm = getattr(self.app, "data_manager", None)
                    if dm is not None and hasattr(dm, "update_order_status"):
                        dm.update_order_status(code, oid, "CANCELLED")
                except Exception:
                    pass
                return "stale_ord_no"
            if ret_code == 0 or out_ord:
                self.app.log(
                    f"✅ [{mode_str}] 주문 취소 접수: 원주문 {oid} / 응답 ord_no={out_ord or '-'}"
                )
                # 주문 로그: 부분체결 수량을 절대 0으로 덮지 않음.
                # (예전엔 CANCELLED+filled_qty=0 으로 덮어 UI·동기화가 체결을 놓침)
                try:
                    dm = getattr(self.app, "data_manager", None)
                    if dm is not None and hasattr(dm, "update_order_status"):
                        keep_code = code
                        keep_filled_price = 0
                        keep_filled_qty = 0
                        keep_order_qty = 0
                        try:
                            row = dm.get_order_history_row_by_order_no(oid)
                        except Exception:
                            row = None
                        if isinstance(row, dict):
                            try:
                                keep_code = str(row.get("code") or code).split("_")[0].zfill(6)
                            except Exception:
                                keep_code = code
                            try:
                                keep_filled_price = int(float(row.get("filled_price") or 0))
                            except (TypeError, ValueError):
                                keep_filled_price = 0
                            try:
                                keep_filled_qty = int(row.get("filled_qty") or 0)
                            except (TypeError, ValueError):
                                keep_filled_qty = 0
                            try:
                                keep_order_qty = int(row.get("qty") or 0)
                            except (TypeError, ValueError):
                                keep_order_qty = 0
                        # kt00007 다건·fill_state 누적과 MAX
                        try:
                            sum_fq = int(dm.sum_order_history_filled_qty(oid) or 0)
                            keep_filled_qty = max(keep_filled_qty, sum_fq)
                        except Exception:
                            pass
                        try:
                            fcs = getattr(self.app, "fill_confirmation_service", None)
                            if fcs is not None:
                                keep_filled_qty = max(
                                    keep_filled_qty,
                                    int(fcs.get_exchange_filled_qty(oid) or 0),
                                    int(fcs.get_applied_qty(oid) or 0),
                                )
                                if keep_order_qty <= 0:
                                    keep_order_qty = int(fcs.get_order_qty(oid) or 0)
                        except Exception:
                            pass
                        if keep_filled_qty > 0 and keep_order_qty > 0:
                            if keep_filled_qty >= keep_order_qty:
                                new_st = "FILLED"
                            else:
                                new_st = "PARTIAL"
                        elif keep_filled_qty > 0:
                            new_st = "PARTIAL"
                        else:
                            new_st = "CANCELLED"
                        dm.update_order_status(
                            keep_code,
                            oid,
                            new_st,
                            filled_price=keep_filled_price,
                            filled_qty=keep_filled_qty,
                        )
                        # 부분체결분만 증분 반영(잔량 취소를 전량체결로 오인하지 않음)
                        if keep_filled_qty > 0:
                            try:
                                fcs2 = getattr(
                                    self.app, "fill_confirmation_service", None
                                )
                                if fcs2 is not None:
                                    side_l = "buy"
                                    nm = ""
                                    rs = ""
                                    if isinstance(row, dict):
                                        side_l = str(
                                            row.get("side") or "buy"
                                        ).strip().lower()
                                        nm = str(row.get("name") or "")
                                        rs = str(row.get("reason") or "")
                                    if side_l not in ("buy", "sell"):
                                        side_l = "buy"
                                    fcs2.ingest_rest_confirmed(
                                        code=keep_code,
                                        order_no=oid,
                                        side=side_l,
                                        qty=int(keep_order_qty or keep_filled_qty),
                                        fill_price=float(keep_filled_price or 0),
                                        name=nm,
                                        reason=rs,
                                        row={
                                            "filled_qty": int(keep_filled_qty),
                                            "filled_price": int(keep_filled_price),
                                            "qty": int(
                                                keep_order_qty or keep_filled_qty
                                            ),
                                            "result": str(new_st),
                                        },
                                    )
                            except Exception:
                                pass
                except Exception:
                    pass
                try:
                    sl = getattr(self.app, "session_logger", None)
                    if sl:
                        sl.log(
                            "order_cancel",
                            {
                                "orig_ord_no": oid,
                                "stk_cd": code,
                                "return_code": ret_code,
                                "return_msg": str(ret_msg),
                                "cncl_ord_no": out_ord,
                            },
                        )
                except Exception:
                    pass
                return "cancelled"
            self.app.log(
                f"❌ [{mode_str}] 주문 취소 거부: {ret_msg} (code={ret_code})"
            )
            return "failed"
        except Exception as e:
            self.app.log(f"💥 [{mode_str}] 주문 취소 예외: {e}")
            try:
                sl = getattr(self.app, "session_logger", None)
                if sl:
                    sl.log(
                        "order_cancel_exception",
                        {"orig_ord_no": oid, "stk_cd": code, "error": str(e)},
                    )
            except Exception:
                pass
            return "failed"

    def _clear_local_exit_lock_for_order(
        self,
        code: str,
        order_no: str,
        *,
        log_fn: Optional[Any] = None,
    ) -> None:
        """스냅샷·exit_pending·VP 플래그에서 해당 주문번호 잠금을 제거합니다."""
        nk = normalize_code(str(code or "").split("_")[0])
        oid = str(order_no or "").strip()
        if not nk or not oid:
            return
        try:
            pf = getattr(self.app, "portfolio", None)
            if pf is not None:
                try:
                    pf._discard_pending_exit_snapshot(oid)
                except Exception:
                    pass
                try:
                    with pf.lock:
                        p = pf.virtual_positions.get(nk)
                        if p and str(
                            p.get("pending_exit_ord_no") or ""
                        ).strip() == oid:
                            p["exit_pending"] = False
                            p.pop("pending_exit_ord_no", None)
                except Exception:
                    pass
        except Exception:
            pass

    def purge_local_sell_lock_refs_for_code(
        self,
        code: str,
        *,
        log_fn: Optional[Any] = None,
    ) -> None:
        """증권사에 없는 원주문(RC4032) 등 — 로컬 매도대기 참조만 일괄 제거."""
        nk = normalize_code(str(code or "").split("_")[0])
        if not nk:
            return
        _log = log_fn or getattr(self.app, "log", None) or (lambda _m: None)
        try:
            pf = getattr(self.app, "portfolio", None)
            if pf is not None:
                with pf.lock:
                    snap_items = list(
                        (pf._pending_exit_snapshots or {}).items()
                    )
                for oid, rec in snap_items:
                    try:
                        if normalize_code((rec or {}).get("code")) == nk:
                            pf._discard_pending_exit_snapshot(str(oid))
                    except Exception:
                        pass
                try:
                    with pf.lock:
                        p = pf.virtual_positions.get(nk)
                        if p:
                            p["exit_pending"] = False
                            p.pop("pending_exit_ord_no", None)
                except Exception:
                    pass
        except Exception as ex:
            try:
                _log(f"⚠️ [매도잠금해제] 로컬 참조 정리 실패({nk}): {ex}")
            except Exception:
                pass

    def fetch_unfilled_sell_orders_ka10075(
        self, code: str
    ) -> List[Dict[str, Any]]:
        """ka10075 미체결 — 해당 종목 매도 주문 목록(증권사 기준)."""
        nk = normalize_code(str(code or "").split("_")[0])
        if not nk or not self.account_api:
            return []
        out: List[Dict[str, Any]] = []
        try:
            use_mock = getattr(config, "USE_MOCK", False)
            stex = "1" if use_mock else "0"
            res = self.account_api.unfilled_orders_request_ka10075(
                all_stk_tp="1",
                trde_tp="1",
                stex_tp=stex,
                stock_code=nk,
            )
            if not isinstance(res, dict):
                return out
            try:
                rc = res.get("return_code", 0)
                if rc is not None and int(str(rc).strip()) != 0:
                    return out
            except (TypeError, ValueError):
                pass
            oso = res.get("oso") or []
            if not isinstance(oso, list):
                oso = []
            for row in oso:
                if not isinstance(row, dict):
                    continue
                c = normalize_code(row.get("stk_cd") or row.get("pdno") or "")
                if c != nk:
                    continue
                try:
                    oq = int(
                        float(
                            str(row.get("oso_qty") or row.get("ord_qty") or 0)
                            .replace(",", "")
                            .strip()
                            or 0
                        )
                    )
                except (TypeError, ValueError):
                    oq = 0
                if oq <= 0:
                    continue
                out.append(
                    {
                        "order_no": str(row.get("ord_no") or "").strip(),
                        "code": c,
                        "oso_qty": oq,
                        "ord_qty": int(
                            float(
                                str(row.get("ord_qty") or 0)
                                .replace(",", "")
                                .strip()
                                or 0
                            )
                        ),
                    }
                )
        except Exception as e:
            try:
                self.logger.debug("fetch_unfilled_sell_orders_ka10075: %s", e)
            except Exception:
                pass
        return out

    def sum_broker_unfilled_sell_qty_for_code(self, code: str) -> int:
        """증권사 ka10075 미체결 매도 수량 합."""
        total = 0
        try:
            for row in self.fetch_unfilled_sell_orders_ka10075(code):
                try:
                    total += int((row or {}).get("oso_qty") or 0)
                except (TypeError, ValueError):
                    continue
        except Exception:
            pass
        return max(0, total)

    def cancel_broker_unfilled_sells_for_code(
        self,
        code: str,
        *,
        log_fn: Optional[Any] = None,
    ) -> int:
        """증권사 미체결(ka10075) 매도 주문을 kt10003으로 취소합니다."""
        nk = normalize_code(str(code or "").split("_")[0])
        _log = log_fn or getattr(self.app, "log", None) or (lambda _m: None)
        cancelled = 0
        try:
            rows = self.fetch_unfilled_sell_orders_ka10075(nk)
        except Exception:
            rows = []
        if rows:
            try:
                _log(
                    f"ℹ️ [매도잠금해제] {nk} 증권사 미체결 매도 {len(rows)}건 조회"
                )
            except Exception:
                pass
        for row in rows:
            try:
                oid = str((row or {}).get("order_no") or "").strip()
                if not oid:
                    continue
                st = self.cancel_order(oid, nk)
                if st == "cancelled":
                    cancelled += 1
                if st in ("cancelled", "no_open_qty", "stale_ord_no"):
                    self._clear_local_exit_lock_for_order(
                        nk, oid, log_fn=_log
                    )
            except Exception as ex:
                try:
                    _log(f"⚠️ [매도잠금해제] 미체결 취소 예외({nk}): {ex}")
                except Exception:
                    pass
        return cancelled

    def cancel_stale_open_sell_orders_for_code(
        self,
        code: str,
        *,
        log_fn: Optional[Any] = None,
    ) -> int:
        """
        종목별 매도 잠금 해제: 증권사 미체결(ka10075) → 스냅샷/DB 접수 → 로컬 참조 정리.

        800033(매도가능수량 부족) 또는 exit_pending 고착 시 호출합니다.
        """
        nk = normalize_code(str(code or "").split("_")[0])
        if not nk or nk == "000000":
            return 0
        _log = log_fn or getattr(self.app, "log", None) or (lambda _m: None)
        cancelled = 0
        seen: Set[str] = set()

        try:
            cancelled += int(
                self.cancel_broker_unfilled_sells_for_code(nk, log_fn=_log)
                or 0
            )
            for row in self.fetch_unfilled_sell_orders_ka10075(nk):
                o = str((row or {}).get("order_no") or "").strip()
                if o:
                    seen.add(o)
        except Exception:
            pass

        def _after_cancel(st: str, oid: str) -> None:
            nonlocal cancelled
            if st == "cancelled":
                cancelled += 1
            if st in ("cancelled", "no_open_qty", "stale_ord_no"):
                self._clear_local_exit_lock_for_order(nk, oid, log_fn=_log)

        try:
            pf = getattr(self.app, "portfolio", None)
            if pf is not None:
                with pf.lock:
                    snap_items = list(
                        (pf._pending_exit_snapshots or {}).items()
                    )
                for oid, rec in snap_items:
                    try:
                        if not isinstance(rec, dict):
                            continue
                        if normalize_code(rec.get("code")) != nk:
                            continue
                        o = str(oid or "").strip()
                        if not o or o in seen:
                            if o:
                                _after_cancel("stale_ord_no", o)
                            continue
                        seen.add(o)
                        _after_cancel(self.cancel_order(o, nk), o)
                    except Exception as ex_row:
                        try:
                            _log(
                                f"⚠️ [매도잠금해제] 스냅샷 취소 예외({o}): {ex_row}"
                            )
                        except Exception:
                            pass
        except Exception as ex_snap:
            try:
                _log(f"⚠️ [매도잠금해제] 스냅샷 순회 실패: {ex_snap}")
            except Exception:
                pass

        try:
            dm = getattr(self.app, "data_manager", None)
            if dm is not None and hasattr(dm, "get_open_sell_orders_for_code"):
                for row in dm.get_open_sell_orders_for_code(nk):
                    try:
                        o = str((row or {}).get("order_no") or "").strip()
                        if not o or o in seen:
                            continue
                        seen.add(o)
                        _after_cancel(self.cancel_order(o, nk), o)
                    except Exception as ex_db:
                        try:
                            _log(f"⚠️ [매도잠금해제] DB 주문 취소 예외: {ex_db}")
                        except Exception:
                            pass
        except Exception as ex_list:
            try:
                _log(f"⚠️ [매도잠금해제] 접수 매도 조회 실패: {ex_list}")
            except Exception:
                pass

        try:
            self.purge_local_sell_lock_refs_for_code(nk, log_fn=_log)
        except Exception:
            pass

        if cancelled > 0:
            try:
                _log(
                    f"ℹ️ [매도잠금해제] {nk} 매도 취소 {cancelled}건 — "
                    f"매도가능수량 복구 시도"
                )
            except Exception:
                pass
        return cancelled

    def sum_open_sell_lock_qty_for_code(self, code: str) -> int:
        """매도가능수량에서 잠긴 양 추정(증권사 미체결 + 로컬 접수 SUCCESS)."""
        nk = normalize_code(str(code or "").split("_")[0])
        if not nk:
            return 0
        broker = 0
        local = 0
        try:
            broker = int(self.sum_broker_unfilled_sell_qty_for_code(nk))
        except Exception:
            broker = 0
        try:
            dm = getattr(self.app, "data_manager", None)
            if dm is not None and hasattr(dm, "get_open_sell_orders_for_code"):
                for row in dm.get_open_sell_orders_for_code(nk):
                    try:
                        oq = int((row or {}).get("qty") or 0)
                        fq = int((row or {}).get("filled_qty") or 0)
                        rem = max(0, oq - fq)
                        if rem > 0:
                            local += rem
                    except (TypeError, ValueError):
                        continue
        except Exception:
            pass
        return max(broker, local)

    # ------------------------------------------------------------------ #
    # 증권사 REST 체결내역 (kt00007) → 포지션관리 주문 로그 DB 보강
    # ------------------------------------------------------------------ #

    def _normalize_kt00007_stk_cd(self, raw: Any) -> str:
        """kt00007 stk_cd → 6자리 종목코드."""
        try:
            c = normalize_code(raw)
            s = str(c or "").strip().split("_", 1)[0].zfill(6)
            return s if len(s) == 6 and s.isdigit() else ""
        except Exception:
            return ""

    @staticmethod
    def _digits_only(s: Any) -> str:
        """문자열에서 숫자만 추출 (키움 API 시각·일자 필드용)."""
        try:
            return "".join(ch for ch in str(s or "") if ch.isdigit())
        except Exception:
            return ""

    @staticmethod
    def _kiwoom_time_to_hms(clock: Any) -> str:
        """
        순수 시각 6자리(HHMMSS) → HH:MM:SS.
        14자리 이상(YYYYMMDDHHMMSS)은 앞 6자를 시각으로 쓰면 안 되므로,
        이 경우에는 시각 부(뒤 6자)만 사용합니다.
        """
        try:
            d = OrderManager._digits_only(clock)
            if len(d) >= 14:
                d = d[8:14]
            if len(d) >= 6:
                return f"{d[0:2]}:{d[2:4]}:{d[4:6]}"
        except Exception:
            pass
        return "00:00:00"

    @staticmethod
    def _kt00007_row_fill_timestamp(
        query_ord_dt_yyyymmdd: str, raw: Dict[str, Any]
    ) -> Tuple[str, str, str]:
        """
        kt00007 한 행에서 order_history.timestamp 및 fill_row_key용 일·시각을 만듭니다.

        - API가 체결(또는 확인) 시각을 YYYYMMDDHHMMSS(14자리)로 주면 그대로 사용합니다.
        - 6자리(HHMMSS)만 오면 일자는 행의 cntr_dt·ord_dt 등 8자리 필드를 우선하고,
          없으면 조회에 사용한 ord_dt(query)를 붙입니다.

        Returns:
            (timestamp 'YYYY-MM-DD HH:MM:SS', yyyymmdd_key, hhmmss_key 'HH:MM:SS')
        """
        qd = OrderManager._digits_only(query_ord_dt_yyyymmdd)
        if len(qd) >= 8:
            qd = qd[:8]
        else:
            qd = ""

        # 체결/확인/주문 시각 후보 (REST 필드명이 버전별로 다를 수 있어 순차 시도)
        clock_raw = (
            raw.get("cntr_tm")
            or raw.get("cnfm_tm")
            or raw.get("ord_tm")
            or raw.get("tm")
            or raw.get("cntg_tm")
            or raw.get("stck_cntg_hour")
            or ""
        )
        clock = OrderManager._digits_only(clock_raw)

        # 14자리 이상: YYYYMMDDHHMMSS (초까지 포함된 표준 형)
        if len(clock) >= 14:
            ymd = clock[:8]
            hms6 = clock[8:14]
            ts = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]} {hms6[0:2]}:{hms6[2:4]}:{hms6[4:6]}"
            hms_key = f"{hms6[0:2]}:{hms6[2:4]}:{hms6[4:6]}"
            return (ts, ymd, hms_key)

        # 12자리: YYYYMMDDHHMM (초는 00)
        if len(clock) == 12:
            ymd = clock[:8]
            hm = clock[8:12]
            ts = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]} {hm[0:2]}:{hm[2:4]}:00"
            hms_key = f"{hm[0:2]}:{hm[2:4]}:00"
            return (ts, ymd, hms_key)

        # 6자리(또는 그 이상에서 뒤 6자리): 당일 시각만 — 일자는 행 필드 우선
        hms6 = clock[:6] if len(clock) >= 6 else (clock.zfill(6) if len(clock) > 0 else "000000")
        if len(hms6) < 6:
            hms6 = hms6.zfill(6)

        row_date = ""
        for key in (
            "cntr_dt",
            "ord_dt",
            "stk_ord_dt",
            "trde_dt",
            "dt",
            "ordr_dt",
            "rgst_dt",
            "exec_dt",
        ):
            try:
                dd = OrderManager._digits_only(raw.get(key))
                if len(dd) >= 8:
                    row_date = dd[:8]
                    break
            except Exception:
                continue

        use_d = row_date if len(row_date) == 8 else qd
        if len(use_d) != 8:
            try:
                use_d = datetime.now().strftime("%Y%m%d")
            except Exception:
                use_d = "19700101"

        hms_key = f"{hms6[0:2]}:{hms6[2:4]}:{hms6[4:6]}"
        ts = f"{use_d[:4]}-{use_d[4:6]}-{use_d[6:8]} {hms_key}"
        return (ts, use_d, hms_key)

    @staticmethod
    def _side_from_kt00007_row(row: Dict[str, Any]) -> str:
        """매수/매도 → order_history side."""
        try:
            blob = f"{row.get('io_tp_nm', '')} {row.get('trde_tp', '')}"
        except Exception:
            blob = ""
        if "매도" in blob:
            return "sell"
        return "buy"

    def _extract_kt00007_rows(self, data: Any) -> List[Dict[str, Any]]:
        """응답 JSON에서 체결 행 리스트 추출."""
        if not isinstance(data, dict):
            return []
        try:
            lst = data.get("acnt_ord_cntr_prps_dtl")
            if isinstance(lst, list):
                return [x for x in lst if isinstance(x, dict)]
        except Exception:
            pass
        try:
            out = data.get("output")
            if isinstance(out, dict):
                lst2 = out.get("acnt_ord_cntr_prps_dtl")
                if isinstance(lst2, list):
                    return [x for x in lst2 if isinstance(x, dict)]
            if isinstance(out, list):
                return [x for x in out if isinstance(x, dict)]
        except Exception:
            pass
        return []

    def _post_kt00007_page(
        self,
        ord_dt_yyyymmdd: str,
        *,
        cont_yn: str = "N",
        next_key: str = "",
        fr_ord_no: str = "",
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Optional[requests.Response]]:
        """
        계좌별주문체결내역상세요청(kt00007) 1페이지.
        qry_tp=4: 체결내역만.
        """
        use_mock = getattr(config, "USE_MOCK", False)
        host = config.MOCK_URL if use_mock else config.URL
        token = self.app.auth_manager.get_auth_token()
        if not token:
            return [], {}, None

        url = f"{host}/api/dostk/acnt"
        headers: Dict[str, str] = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "api-id": "kt00007",
            "cont-yn": str(cont_yn or "N").strip()[:1] or "N",
            "next-key": str(next_key or "")[:50],
        }
        body: Dict[str, str] = {
            "ord_dt": str(ord_dt_yyyymmdd or "").strip()[:8],
            "qry_tp": "4",
            "stk_bond_tp": "1",
            "sell_tp": "0",
            "stk_cd": "",
            "fr_ord_no": str(fr_ord_no or "").strip()[:20],
            "dmst_stex_tp": OrderManager.dmst_stex_tp_for_orders(),
        }
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=20)
        except requests.RequestException as e:
            try:
                self.logger.debug("kt00007 요청 실패: %s", e)
            except Exception:
                pass
            return [], {}, None

        try:
            data = resp.json()
        except Exception:
            data = {}
        rows = self._extract_kt00007_rows(data)
        return rows, data if isinstance(data, dict) else {}, resp

    def fetch_exchange_fills_for_date(
        self, day: date, *, fr_ord_no: str = ""
    ) -> List[Dict[str, Any]]:
        """
        특정 캘린더일의 체결내역만 수집. 연속조회(cont-yn) 전부 펼침.
        반환: merge_exchange_fill_order_rows 에 넘길 dict 목록.
        """
        out: List[Dict[str, Any]] = []
        ord_dt = day.strftime("%Y%m%d")
        cont = "N"
        nk = ""
        for _ in range(80):
            rows, data, resp = self._post_kt00007_page(
                ord_dt, cont_yn=cont, next_key=nk, fr_ord_no=fr_ord_no
            )
            try:
                rc = data.get("return_code", 0)
                if rc is not None and int(str(rc).strip()) != 0:
                    break
            except Exception:
                if not rows:
                    break
            for raw in rows:
                try:
                    code = self._normalize_kt00007_stk_cd(
                        raw.get("stk_cd") or raw.get("pdno")
                    )
                    if not code:
                        continue
                    fq = int(
                        float(
                            str(raw.get("cntr_qty") or "0")
                            .replace(",", "")
                            .replace("+", "")
                            or 0
                        )
                    )
                    fp = int(
                        float(
                            str(raw.get("cntr_uv") or "0")
                            .replace(",", "")
                            .replace("+", "")
                            or 0
                        )
                    )
                    if fq <= 0 or fp <= 0:
                        continue
                    ord_no = str(raw.get("ord_no") or "").strip()
                    try:
                        ts, day_key, hms_key = self._kt00007_row_fill_timestamp(
                            ord_dt, raw
                        )
                    except Exception:
                        # 파싱 실패 시 조회일 + 시각 0 (로그만 남기고 행은 가능한 한 삽입)
                        try:
                            self.logger.debug(
                                "kt00007 일시 파싱 폴백: ord_no=%s raw_tm=%s",
                                ord_no,
                                raw.get("cntr_tm")
                                or raw.get("cnfm_tm")
                                or raw.get("ord_tm"),
                            )
                        except Exception:
                            pass
                        day_key = ord_dt[:8] if len(ord_dt) >= 8 else ord_dt
                        hms_key = "00:00:00"
                        ts = f"{day_key[:4]}-{day_key[4:6]}-{day_key[6:8]} {hms_key}"
                    side = self._side_from_kt00007_row(raw)
                    nm = str(raw.get("stk_nm") or "").strip() or code
                    hint = str(raw.get("io_tp_nm") or raw.get("trde_tp") or "").strip()
                    fill_key = f"{day_key}|{ord_no}|{hms_key}|{code}|{fq}|{fp}|{side}"
                    out.append(
                        {
                            "fill_row_key": fill_key,
                            "code": code,
                            "name": nm,
                            "side": side,
                            "qty": fq,
                            "price": fp,
                            "filled_price": fp,
                            "filled_qty": fq,
                            "order_no": ord_no or "EXCH",
                            "order_type": "00",
                            "timestamp": ts,
                            "result": "FILLED",
                            "reason": f"API:{hint}" if hint else "API:체결",
                            "snap_vol_power": None,
                            "snap_bid_ask": "",
                        }
                    )
                except Exception:
                    continue

            if resp is None:
                break
            try:
                c_yn = (
                    resp.headers.get("cont-yn")
                    or resp.headers.get("Cont-Yn")
                    or "N"
                )
                n_key = resp.headers.get("next-key") or resp.headers.get("Next-Key") or ""
            except Exception:
                c_yn, n_key = "N", ""
            if str(c_yn).upper() == "Y" and str(n_key).strip():
                cont, nk = "Y", str(n_key).strip()
                continue
            break
        return out

    def sync_exchange_fills_into_database(
        self,
        data_manager: Any,
        *,
        lookback_days: int = 7,
        dates: Optional[List[date]] = None,
        silent: bool = False,
        logger: Optional[Any] = None,
    ) -> int:
        """
        kt00007 체결을 `order_history`에 병합(중복 fill_row_key 제외).
        실전/모의는 config.USE_MOCK 에 따라 각각의 state DB에 기록됩니다.
        """
        log_fn = logger or getattr(self.app, "log", print)
        total_added = 0
        try:
            if dates is not None:
                day_list = list(dates)
            else:
                try:
                    n = max(1, min(int(lookback_days), 60))
                except (TypeError, ValueError):
                    n = 7
                today = date.today()
                day_list = [today - timedelta(days=i) for i in range(n)]
            for d in day_list:
                try:
                    rows = self.fetch_exchange_fills_for_date(d)
                    if not rows:
                        continue
                    added = int(
                        data_manager.merge_exchange_fill_order_rows(rows) or 0
                    )
                    total_added += added
                except Exception as e:
                    if not silent:
                        try:
                            log_fn(f"⚠️ [kt00007] {d} 체결 병합 실패: {e}")
                        except Exception:
                            pass
        except Exception as e:
            if not silent:
                try:
                    log_fn(f"⚠️ [kt00007] 체결내역 동기화 실패: {e}")
                except Exception:
                    pass
        return total_added

    def sync_exchange_fills_for_order_no(
        self,
        data_manager: Any,
        order_no: str,
        *,
        dates: Optional[List[date]] = None,
        silent: bool = False,
        logger: Optional[Any] = None,
    ) -> int:
        """특정 주문번호만 kt00007 조회·병합(per-order 긴급 sync)."""
        log_fn = logger or getattr(self.app, "log", print)
        oid = str(order_no or "").strip()
        if not oid:
            return 0
        total_added = 0
        try:
            if dates is not None:
                day_list = list(dates)
            else:
                day_list = [date.today()]
            for d in day_list:
                try:
                    rows = self.fetch_exchange_fills_for_date(d, fr_ord_no=oid)
                    if not rows:
                        continue
                    added = int(
                        data_manager.merge_exchange_fill_order_rows(rows) or 0
                    )
                    total_added += added
                except Exception as e:
                    if not silent:
                        try:
                            log_fn(f"⚠️ [kt00007/{oid}] 체결 병합 실패: {e}")
                        except Exception:
                            pass
        except Exception as e:
            if not silent:
                try:
                    log_fn(f"⚠️ [kt00007/{oid}] 동기화 실패: {e}")
                except Exception:
                    pass
        return total_added
