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
                return None

        except Exception as e:
            self.app.log(f"💥 주문 프로세스 오류: {str(e)}")
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
                self.app.log(f"✅ 계좌 동기화 성공 (보유 종목: {len(holdings)}개)")
                return holdings
            else:
                msg = res.get("return_msg", "Unknown error")
                self.app.log(f"⚠️ 계좌 조회 실패: {msg} (ErrorCode: {ret_code})")
                return None

        except Exception as e:
            self.app.log(f"💥 계좌 조회 중 네트워크/파싱 오류: {str(e)}")
            return None

    def cancel_order(self, order_no):
        """미체결 주문 취소"""
        self.app.log(f"🚫 주문 취소 요청: {order_no}")
        # API 호출 로직 구현 예정
        return True
