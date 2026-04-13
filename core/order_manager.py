import requests
import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import config
from utils.normalization import get_tick_size
from core.price_guard import validate_limit_order_reference_price
from core.supply_display import capture_supply_snapshot

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

    def _get_base_url(self):
        return config.MOCK_URL if getattr(config, 'USE_MOCK', False) else config.URL

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
    ):
        """
        주문 발송 (REST API 호출 - v4.0.0 Refactored)
        - order_type \"00\"(지정가): 매수는 현재가(참고가)+1틱, 매도는 -1틱으로 ord_uv 설정.
        - order_type \"03\"(시장가): 호가 0 — 시장가만 필요할 때만 사용.
        - skip_price_guard: True 시 지정가 시세 일관성 검증 생략(비상 매도 등 한정 사용).
        """
        use_mock = getattr(config, 'USE_MOCK', False)
        mode_str = "모의투자" if use_mock else "실전투자"
        
        # [v16.9] 실제 주문 실행 전 최종 확인 (Safety Switch)
        allow_order = getattr(config, 'ALLOW_MOCK_ORDER', False) if use_mock else getattr(config, 'ALLOW_REAL_ORDER', False)
        
        if not allow_order:
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
            self.app.log(f"❌ [{mode_str}] 주문 엔진 로드 실패")
            return None

        try:
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
            adj_price = self.get_adjusted_price(price, side) if order_type == "00" else price
            
            # trde_tp: "0"(Limit), "3"(Market)
            # order_type "00" -> "0", "03" -> "3" 등 변환
            api_trde_tp = str(int(order_type)) 
            
            self.app.log(f"🚀 [{mode_str}] 주문 발송 시도: {code}({name}) | {qty}주 | {adj_price:,}원")

            if side == "buy":
                # stock_buy_order_request_kt10000
                res = self.order_api.stock_buy_order_request_kt10000(
                    dmst_stex_tp="KRX", # 기본 거래소
                    stk_cd=code,
                    ord_qty=str(int(qty)),
                    trde_tp=api_trde_tp,
                    ord_uv=str(int(adj_price)) if order_type == "00" else "0"
                )
            else:
                # stock_sell_order_request_kt10001
                res = self.order_api.stock_sell_order_request_kt10001(
                    dmst_stex_tp="KRX",
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
                self.app.log(f"✅ [{mode_str}] 주문 성공! 번호: {ord_no}")
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

                # 웹소켓 CHE가 없을 때도 포지션·체결가 연동 (메인 스레드에서 실행)
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
                                c, o, s, q, float(ap), nm, rs
                            ),
                        )
                    else:
                        self.app.reconcile_exchange_fill_after_order_accept(
                            code, ord_no, side, int(qty), float(adj_price), name, reason
                        )
                except Exception as sync_e:
                    self.logger.warning("체결 동기화 스케줄 실패: %s", sync_e)

                # 텔레그램 알림
                side_kr = "매수" if side == "buy" else "매도"
                msg = f"<b>🚀 [주문 성공]</b>\n종목: {code}({name})\n구분: {side_kr}\n가격: {adj_price:,}원 / 수량: {qty}주\n번호: {ord_no}"
                import threading
                threading.Thread(target=self.app.send_telegram_message, args=(msg,), daemon=True).start()
                return ord_no
            else:
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
                    pv = self._safe_float(obj.get(k), 0.0)
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

    def _sum_holdings_market_value(self, holdings: list) -> float:
        """
        ka10085 acnt_prft_rt 행별 주식 평가금액 합산.
        모의/실전 API에 따라 evlu·시세 필드가 비는 응답이 섞이면 총자산이 예수금만으로 떨어지므로
        매입가(평단)×수량까지 폴백합니다.
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
                ev = self._safe_float(
                    row.get("evlu_amt") or row.get("stk_evlu_amt") or 0
                )
                if ev > 0:
                    s += ev
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

    def get_holdings(self):
        """[v4.0.0] 실시간 계좌 잔고 동기화 (ka10085 규격)"""
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

                # [v4.8.5] 보유 없을 때 총자산·예수금 보정
                if total_equity == 0 and len(holdings) == 0:
                    deposit = self.get_deposit()
                    if deposit:
                        total_equity = float(deposit)
                        d1_deposit = total_equity
                        d2_deposit = total_equity

                # ka10085의 d1_samt/d2_samt는 총자산과 혼동·누락되는 경우가 있어 예수금상세 TR로 보강
                cash_available = 0.0
                dep_detail = self.get_deposit_detail()
                if dep_detail:
                    d1d = float(dep_detail.get("d1") or 0)
                    d2d = float(dep_detail.get("d2") or 0)
                    # 예수금상세(ka10001/kt00001)가 D+1/D+2 권위 — d2d>0 만 쓰면 음수(D+2 부족)가 무시되어 동기화 표시가 틀어짐
                    d1_deposit = d1d
                    d2_deposit = d2d
                    cash_available = float(
                        dep_detail.get("ord_alow")
                        or dep_detail.get("pymn_alow")
                        or dep_detail.get("entr")
                        or 0
                    )

                # 예수금상세로 d2 보강 후: 총자산 = D+2 예수 + 보유 평가 합 (tot_evlu_amt는 이전 d2/예수 기준으로 남을 수 있음)
                stocks_mv = self._sum_holdings_market_value(holdings)
                n_hold, hold_detail = self._summarize_holdings_qty_positive(holdings)
                try:
                    total_equity = float(d2_deposit) + float(stocks_mv)
                except Exception:
                    pass
                # 주문가능(ord_alow 등)은 키움에서 보통 D+2 추정예수와 같게 나오는 경우가 많음 — 동일하면 한 번만 표기
                try:
                    ca = float(cash_available or 0)
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
                        f"✅ [{mode_str}] 계좌 동기화 | 총자산 {total_equity:,.0f}원 | "
                        f"{dep_seg} | 실질 보유 {n_hold}종목"
                        f"\n   └ 보유: {hold_detail}"
                    )
                except Exception as le:
                    try:
                        self.app.log(f"⚠️ [계좌] 동기화 요약 로그 실패: {le}")
                    except Exception:
                        pass
                return {
                    "holdings": holdings,
                    "total_equity": total_equity,
                    "d1_deposit": d1_deposit,
                    "d2_deposit": d2_deposit,
                    "cash_available": cash_available,
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
                msg = res.get("return_msg", "Unknown error")
                self.app.log(f"⚠️ 계좌 조회 실패: {msg} (ErrorCode: {ret_code})")
                return None

        except Exception as e:
            self.app.log(f"💥 계좌 조회 중 네트워크/파싱 오류: {str(e)}")
            return None

    def get_deposit_detail(self) -> Optional[Dict[str, Any]]:
        """
        예수금상세(ka10001/kt00001). ka10085의 d1_samt/d2_samt와 달리
        D+1/D+2 추정예수금(d1_entra/d2_entra)·주문가능금액(ord_alow_amt)이 분리되어 옵니다.
        qry_tp: 3=추정조회(추정 예수 우선), 2=일반조회.
        """
        use_mock = getattr(config, "USE_MOCK", False)
        api_id = "kt00001" if use_mock else "ka10001"
        host = config.MOCK_URL if use_mock else config.URL
        token = self.app.auth_manager.get_auth_token()
        if not token:
            return None
        url = f"{host}/api/dostk/acnt"
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {token}",
            "api-id": api_id,
        }
        for qry_tp in ("3", "2"):
            try:
                res = requests.post(
                    url, headers=headers, json={"qry_tp": qry_tp}, timeout=12
                )
                data = res.json()
                out_body = data.get("output", data.get("data", data))
                if not isinstance(out_body, dict):
                    continue
                rc = data.get("return_code", out_body.get("return_code", 0))
                try:
                    if rc is not None and int(str(rc).strip()) != 0:
                        continue
                except (TypeError, ValueError):
                    pass

                def _pick(*keys: str) -> float:
                    for k in keys:
                        v = out_body.get(k)
                        if v is not None and str(v).strip() not in ("", "-"):
                            return self._safe_float(v)
                    return 0.0

                d1 = _pick("d1_entra", "d1_samt", "entr_d1")
                d2 = _pick("d2_entra", "d2_samt", "entr_d2", "d2entra")
                entr = _pick("entr")
                ord_alow = _pick("ord_alow_amt", "nstd_re_ib_amt")
                pymn = _pick("pymn_alow_amt")

                # return_code 정상이면 반환 (D+2 추정예수가 음수인 경우도 있음 — any(x>0)로 두면 조회 실패로 떨어져 ka10085 잔여값이 남음)
                return {
                    "d1": d1,
                    "d2": d2,
                    "entr": entr,
                    "ord_alow": ord_alow,
                    "pymn_alow": pymn,
                    "qry_tp_used": qry_tp,
                }
            except Exception as e:
                self.logger.debug("예수금상세 qry_tp=%s 실패: %s", qry_tp, e)
                continue
        return None

    def get_deposit(self) -> Optional[int]:
        """[v4.5.3] 가용 예수(주문가능 우선) 단일값 — 내부적으로 예수금상세 조회."""
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
        api_id = 'kt00001' if use_mock else 'ka10001'
        host = config.MOCK_URL if use_mock else config.URL
        
        token = self.app.auth_manager.get_auth_token()
        if not token: return None
        
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
            
            # ka10001(실전): nstd_re_ib_amt(주문가능현금), kt00001(모의): entr(예수금)
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
    ) -> bool:
        """
        미체결 지정가 주문 취소 (kt10003).
        cncl_qty: '0' 이면 잔량 전부 취소.
        """
        use_mock = getattr(config, "USE_MOCK", False)
        mode_str = "모의투자" if use_mock else "실전투자"
        oid = str(order_no or "").strip()
        code = str(stk_cd or "").zfill(6)
        if not oid or not code:
            self.app.log(f"⚠️ [{mode_str}] 취소 실패: 주문번호/종목코드 누락")
            return False
        if not self.order_api:
            self.app.log(f"❌ [{mode_str}] 주문 엔진 없음 — 취소 불가")
            return False
        allow_order = getattr(config, "ALLOW_MOCK_ORDER", False) if use_mock else getattr(
            config, "ALLOW_REAL_ORDER", False
        )
        if not allow_order:
            self.app.log(f"⚠️ [{mode_str}] 취소 생략 (Safety Switch: OFF)")
            return False
        try:
            res = self.order_api.stock_cancel_order_request_kt10003(
                dmst_stex_tp="KRX",
                orig_ord_no=oid,
                stk_cd=code,
                cncl_qty=str(cncl_qty).strip() or "0",
            )
            ret_code = res.get("return_code", -1)
            ret_msg = res.get("return_msg", "")
            out_ord = str(res.get("ord_no", "") or "").strip()
            if ret_code == 0 or out_ord:
                self.app.log(
                    f"✅ [{mode_str}] 주문 취소 접수: 원주문 {oid} / 응답 ord_no={out_ord or '-'}"
                )
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
                return True
            self.app.log(
                f"❌ [{mode_str}] 주문 취소 거부: {ret_msg} (code={ret_code})"
            )
            return False
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
            return False
