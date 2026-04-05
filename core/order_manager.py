import requests
import json
import logging
from datetime import datetime
import config
from utils.normalization import get_tick_size

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

    def place_order(self, code, qty, price, order_type="00", side="buy", name="", reason=""):
        """
        주문 발송 (REST API 호출 - v4.0.0 Refactored)
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
                # [v3.8] DB 로그 기록
                self.app.data_manager.log_order(code, side, qty, adj_price, order_type, ord_no, "SUCCESS", name=name, reason=reason)

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

    def get_holdings(self):
        """[v4.0.0] 실시간 계좌 잔고 동기화 (ka10085 규격)"""
        if not self.account_api:
            return None

        use_mock = getattr(config, 'USE_MOCK', False)
        mode_str = "모의투자" if use_mock else "실전투자"
        
        try:
            self.app.log(f"🔍 [{mode_str}] 실시간 계좌 데이터 동기화 시도...")
            # ka10085: 계좌 수익률/잔고 요청
            # stex_tp "0": 통합거래소
            res = self.account_api.account_return_rate_request_ka10085(stex_tp="0")
            
            ret_code = res.get("return_code", -1)
            if ret_code == 0:
                holdings = res.get("acnt_prft_rt", [])
                # [v4.5 Asset Harden] 총 평가금액(Total Equity) 추출 (예수금 + 주식 평가액)
                total_equity = float(res.get("tot_evlu_amt", 0))
                d1_deposit = float(res.get("d1_samt", 0))
                d2_deposit = float(res.get("d2_samt", 0))
                
                # [v4.8.5 Asset Fallback] 보유 종목이 없으면 총자산이 0으로 잡히는 API 특성 보정
                if total_equity == 0:
                    deposit = self.get_deposit()
                    if deposit:
                        total_equity = float(deposit)
                        d1_deposit = total_equity
                        d2_deposit = total_equity
                
                self.app.log(f"✅ 계좌 동기화 성공 (보유: {len(holdings)}개 | 총자산: {total_equity:,.0f}원)")
                return {
                    "holdings": holdings,
                    "total_equity": total_equity,
                    "d1_deposit": d1_deposit,
                    "d2_deposit": d2_deposit
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

    def get_deposit(self):
        """[v4.5.3] 전용 예수금 상세 조회 (ka10001/kt00001)"""
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

    def cancel_order(self, order_no):
        """미체결 주문 취소"""
        self.app.log(f"🚫 주문 취소 요청: {order_no}")
        # API 호출 로직 구현 예정
        return True
