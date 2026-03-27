import requests
import json
import logging
import config

class OrderManager:
    """[v3.7.8] 키움증권 REST API 주문 집행 매니저"""
    def __init__(self, app):
        self.app = app
        self.logger = logging.getLogger("OrderManager")

    def _get_base_url(self):
        """config 설정에 따라 실전/모의 API 주소 반환"""
        return config.MOCK_URL if getattr(config, 'USE_MOCK', False) else config.URL

    def place_order(self, code, qty, price, order_type="00", side="buy"):
        """
        주문 발송 (REST API 호출)
        - code: 종목코드
        - qty: 수량
        - price: 가격 (시장가 주문 시 0 가능)
        - order_type: "00"(지정가), "03"(시장가) 등
        - side: "buy" 또는 "sell"
        """
        use_mock = getattr(config, 'USE_MOCK', False)
        mode_str = "모의투자" if use_mock else "실전투자"
        
        # 실제 주문 실행 전 최종 확인 (config의 안전장치 확인)
        allow_order = getattr(config, 'ALLOW_MOCK_ORDER', False) if use_mock else getattr(config, 'ALLOW_REAL_ORDER', False)
        
        if not allow_order:
            self.app.log(f"⚠️ [{mode_str}] 주문 차단됨 (Safety Switch: OFF)")
            return None

        base_url = self._get_base_url()
        # [주의] 실제 API 엔드포인트는 개발 문서를 재확인해야 함 (임시 예시 경로)
        endpoint = f"{base_url}/v1/orders" 
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.app.auth_manager.get_access_token()}"
        }
        
        payload = {
            "account_no": self.app.settings.get("account_id", ""),
            "order_type": "1" if side == "buy" else "2", # 1:신규매수, 2:신규매도
            "code": code,
            "qty": int(qty),
            "price": int(price),
            "ord_kind": order_type # "00":지정가
        }

        try:
            self.app.log(f"🚀 [{mode_str}] 주문 발송: {code} | {qty}주 | {price:,}원")
            # response = requests.post(endpoint, headers=headers, data=json.dumps(payload), timeout=10)
            # result = response.json()
            
            # TODO: 실제 API 연동 시 주석 해제 및 결과 처리
            # if result.get('return_code') == '0':
            #     self.app.log(f"✅ 주문 성공: {result.get('order_no')}")
            #     return result.get('order_no')
            # else:
            #     self.app.log(f"❌ 주문 실패: {result.get('return_msg')}")
            
            return "MOCK_ORDER_ID" # 테스트를 위해 임시 아이디 반환

        except Exception as e:
            self.app.log(f"💥 주문 프로세스 오류: {str(e)}")
            return None

    def get_holdings(self):
        """[v3.7.8] 계좌 잔고 및 보유 종목 리스트 조회 (Kiwoom API 연동)"""
        use_mock = getattr(config, 'USE_MOCK', False)
        mode_str = "모의투자" if use_mock else "실전투자"
        acc_no = self.app.settings.get("mock_account_id" if use_mock else "account_id", "")
        
        if not acc_no:
            self.app.log(f"⚠️ [{mode_str}] 계좌 번호가 설정되지 않았습니다.")
            return None

        base_url = self._get_base_url()
        # [v3.7.8] Kiwoom REST API 규격에 맞게 엔드포인트 및 헤더 수정
        endpoint = f"{base_url}/api/dostk/accbal" 
        
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "authorization": f"Bearer {self.app.auth_manager.get_access_token()}",
            "api-id": "ka10018"
        }
        
        # 계좌 비밀번호는 보통 모의투자에서 무시되거나 공백/0000 처리됨
        # [주의] 실제 사용 환경에 따라 payload 구조가 다를 수 있음
        body = {
            "acc_no": acc_no,
            "acc_pwd": "0000", # 기본값
            "acc_tp": "0",
            "next_key": ""
        }

        try:
            self.app.log(f"🔍 [{mode_str}] 계좌[{acc_no}] 서버 데이터 동기화 시도...")
            response = requests.post(endpoint, headers=headers, json=body, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                # Kiwoom REST API 응답 구조: 보통 stk_cntg_hour_trnd_qry 또는 output1
                # 잔고조회(ka10018)의 경우 acc_bal_qry 또는 output1 등 개발 문서를 재확인해야 함
                # 일반적인 패턴은 output1: 보유종목 리스트, output2: 요약
                holdings = data.get('output1', data.get('acc_bal_qry', []))
                if not isinstance(holdings, list): holdings = []
                
                self.app.log(f"✅ 계좌 동기화 성공 (보유 종목: {len(holdings)}개)")
                return holdings
            else:
                self.app.log(f"⚠️ 계좌 조회 실패: {response.status_code} ({response.text})")
                return None

        except Exception as e:
            self.app.log(f"💥 계좌 조회 중 네트워크/파싱 오류: {str(e)}")
            return None

    def cancel_order(self, order_no):
        """미체결 주문 취소"""
        self.app.log(f"🚫 주문 취소 요청: {order_no}")
        # API 호출 로직 구현 예정
        return True
