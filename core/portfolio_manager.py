import threading
import json
import os
from datetime import datetime

class PortfolioManager:
    """
    [v3.8] 가상 포지션 관리, 계좌 동기화(Reconciliation), DB 연동을 전담하는 모듈.
    """
    def __init__(self, data_manager, order_manager, settings=None, log_func=None, gui_callback=None, telegram_callback=None):
        self.data_manager = data_manager
        self.order_manager = order_manager
        self.settings = settings or {}
        self.log = log_func or print
        self.gui_callback = gui_callback
        self.telegram_callback = telegram_callback
        
        # [v3.9] Fees for virtual/mock trading
        self.buy_f_rate = self.settings.get('buy_fee_mock', 0.0) / 100.0
        self.sell_f_rate = self.settings.get('sell_fee_mock', 0.0) / 100.0
        
        self.virtual_positions = {}
        self.virtual_history = []
        self.virtual_db_file = "virtual_db.json"
        
        self.lock = threading.Lock()
        
    def load_all(self):
        """DB 및 파일에서 포지션 데이터를 로드합니다."""
        with self.lock:
            # 1. 파일에서 히스토리 로드
            if os.path.exists(self.virtual_db_file):
                try:
                    with open(self.virtual_db_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        self.virtual_history = data.get('history', [])
                except Exception as e:
                    self.log(f"⚠️ [Portfolio] 히스토리 로드 실패: {e}")

            # 2. DB에서 활성 포지션 로드 (DataManagerV2 기준)
            try:
                # [v3.0.4] DataManagerV2의 get_active_positions() 활용
                active = self.data_manager.get_active_positions() # 딕셔너리 리턴 가정
                if active:
                    # active가 리스트라면 딕셔너리로 변환
                    if isinstance(active, list):
                        for row in active:
                            # row: (code, name, avg_p, qty, stage, invested, time, reason)
                            code = row[0]
                            self.virtual_positions[code] = {
                                "name": row[1], "avg_price": float(row[2]), "qty": int(row[3]),
                                "stage": int(row[4]), "invested": float(row[5]),
                                "entry_time": row[6], "reason": row[7],
                                "events": [], "max_now": float(row[2])
                            }
                    else:
                        self.virtual_positions = active
                    self.log(f"📥 [Portfolio] DB에서 {len(self.virtual_positions)}개의 활성 포지션 복구")
            except Exception as e:
                self.log(f"⚠️ [Portfolio] DB 포지션 복구 실패: {e}")

    def save_db(self):
        """현재 상태를 파일에 저장합니다 (히스토리 위주)."""
        try:
            with open(self.virtual_db_file, 'w', encoding='utf-8') as f:
                json.dump({'history': self.virtual_history}, f, ensure_ascii=False, indent=4)
        except Exception as e:
            self.log(f"⚠️ [Portfolio] DB 저장 실패: {e}")

    def sync_with_actual_account(self, silent=False):
        """[v3.7.8] 실제 키움 계좌의 보유 종목과 로컬 가상 포지션을 동기화 (Reconciliation)"""
        holdings = self.order_manager.get_holdings()
        if holdings is None: 
            self.log("⚠️ [Sync] 계좌 정보를 실시간으로 가져올 수 없습니다.")
            return
        
        server_codes = set()
        updated_count = 0
        new_count = 0
        
        with self.lock:
            # 1. 서버 데이터 기준으로 로컬 데이터 업데이트
            for item in holdings:
                code = item.get('pdno') or item.get('code')
                if not code: continue
                
                name = item.get('prdt_name') or item.get('name', 'Unknown')
                qty = self._to_i(item.get('hldg_qty') or item.get('qty', 0))
                avg_price = self._to_f(item.get('pchs_avg_pric') or item.get('avg_price', 0.0))
                server_codes.add(code)
                
                if code in self.virtual_positions:
                    self.virtual_positions[code]['qty'] = qty
                    self.virtual_positions[code]['avg_price'] = avg_price
                    self.virtual_positions[code]['invested'] = float(qty) * float(avg_price)
                    updated_count += 1
                else:
                    self.virtual_positions[code] = {
                        "name": name, "avg_price": avg_price, "qty": qty,
                        "stage": 1, "invested": float(qty) * float(avg_price),
                        "entry_time": datetime.now().isoformat(),
                        "reason": "SYNCED", "events": [], "max_now": avg_price
                    }
                    self.data_manager.save_active_position(code, name, avg_price, qty, 1, float(qty) * float(avg_price), datetime.now().isoformat(), "SYNCED")
                    new_count += 1
                    
            # 2. 로컬에만 있는 종목 제거 (매매 누락 보정)
            local_codes = list(self.virtual_positions.keys())
            removed_count = 0
            for code in local_codes:
                if code not in server_codes:
                    pos = self.virtual_positions[code]
                    curr_p = self._to_f(pos.get('current_price', pos.get('avg_price', 0)))
                    avg_p = self._to_f(pos.get('avg_price', 0))
                    
                    pnl_pct = (curr_p / avg_p - 1) * 100 if avg_p != 0 else 0
                    pnl_amt = (curr_p - avg_p) * self._to_i(pos.get('qty', 0))
                    
                    pos['events'].append({
                        "time": datetime.now().strftime("%H:%M:%S"),
                        "action": "SYNC_EXIT", "price": curr_p, "qty": pos.get('qty', 0),
                        "pnl": f"{pnl_pct:+.2f}%", "reason": "RECONCILIATION"
                    })
                    pos['final_pnl_pct'] = pnl_pct
                    pos['realized_pnl_amt'] = pnl_amt
                    pos['exit_price'] = curr_p
                    pos['exit_time'] = datetime.now().strftime("%H:%M:%S")
                    pos['code'] = code
                    self.virtual_history.append(pos)
                    
                    self.virtual_positions.pop(code, None)
                    self.data_manager.delete_active_position(code)
                    removed_count += 1
                    self.log(f"⚠️ [Sync] {pos['name']}({code}) 종목이 서버에서 사라져 이력으로 이동 (Reconciled)")

        self.save_db()
        if self.gui_callback: self.gui_callback()
        
        result_msg = f"🔄 동기화: 갱신 {updated_count}, 신규 {new_count}, 제거 {removed_count}"
        self.log(f"✅ [Portfolio] {result_msg}")

    def _to_f(self, v):
        try: return float(v) if not isinstance(v, list) else 0.0
        except: return 0.0
    
    def _to_i(self, v):
        try: return int(v) if not isinstance(v, list) else 0
        except: return 0

    def on_fill(self, code, side, price, qty, name=None, reason=None):
        """체결(CHE) 데이터를 처리하여 포지션을 업데이트합니다."""
        now_str = datetime.now().strftime('%H:%M:%S')
        with self.lock:
            if side == "buy":
                # 진입 혹은 추매
                if code not in self.virtual_positions:
                    # 신규 진입 (Stage 1)
                    meta = self.data_manager.get_latest_order_meta(code)
                    reg_name = name or (meta[0] if meta else "Unknown")
                    reg_reason = reason or (meta[1] if meta else "SIGNAL")
                    
                    fp = self._to_f(price)
                    iq = self._to_i(qty)
                    
                    fee_amt = fp * iq * self.buy_f_rate
                    total_invested = fp * iq + fee_amt
                    
                    self.virtual_positions[code] = {
                        "name": reg_name, "avg_price": fp, "qty": iq,
                        "stage": 1, "invested": total_invested,
                        "entry_time": datetime.now().isoformat(),
                        "reason": reg_reason, "events": [], "max_now": fp
                    }
                    self.data_manager.save_active_position(code, reg_name, fp, iq, 1, total_invested, datetime.now().isoformat(), reg_reason)
                    self.log(f"✅ [Portfolio] {reg_name}({code}) 신규 진입 등록 ({price:,}원)")
                    if self.telegram_callback:
                        msg = f"<b>💰 [가상진입]</b>\n종목: {reg_name}({code})\n가: {price:,}원 / 수: {qty}주\n시간: {now_str}"
                        threading.Thread(target=self.telegram_callback, args=(msg,), daemon=True).start()
                else:
                    # 추가 매수 (Stage 2)
                    pos = self.virtual_positions[code]
                    old_qty = self._to_i(pos.get('qty', 0))
                    old_avg = self._to_f(pos.get('avg_price', 0.0))
                    new_total_qty = old_qty + self._to_i(qty)
                    new_avg_price = ((old_avg * old_qty) + (self._to_f(price) * self._to_i(qty))) / new_total_qty if new_total_qty > 0 else self._to_f(price)
                    
                    pos['qty'] = new_total_qty
                    pos['avg_price'] = new_avg_price
                    pos['invested'] = float(new_avg_price) * float(new_total_qty)
                    pos['stage'] = 2
                    
                    # DB 동기화
                    self.data_manager.save_active_position(code, pos['name'], new_avg_price, new_total_qty, 2, pos['invested'], pos['entry_time'], pos['reason'])
                    self.log(f"🔥 [Portfolio] {pos['name']}({code}) 2차 수량 업데이트 (평단: {int(new_avg_price):,}원)")
                    if self.telegram_callback:
                        msg = f"<b>🔥 [추매완료]</b>\n종목: {pos['name']}({code})\n추가: {price:,}원 / {qty}주\n평단: {int(new_avg_price):,}원\n시간: {now_str}"
                        threading.Thread(target=self.telegram_callback, args=(msg,), daemon=True).start()
            
            elif side == "sell":
                # 청산
                if code in self.virtual_positions:
                    pos = self.virtual_positions[code]
                    exit_reason = pos.get('exit_reason', 'MANUAL')
                    
                    avg_p = self._to_f(pos.get('avg_price', 0.0))
                    qty_val = self._to_i(pos.get('qty', 0))
                    p_val = self._to_f(price)
                    
                    # [v3.9] Apply Sell Fee
                    sell_fee = p_val * qty_val * self.sell_f_rate
                    net_proceeds = p_val * qty_val - sell_fee
                    cost_basis = pos.get('invested', avg_p * qty_val)
                    
                    pnl_amt = net_proceeds - cost_basis
                    pnl_pct = (pnl_amt / cost_basis) * 100 if cost_basis != 0 else 0
                    
                    pos['final_pnl_pct'] = pnl_pct
                    pos['realized_pnl_amt'] = pnl_amt
                    pos['exit_price'] = p_val
                    pos['exit_time'] = now_str
                    pos['code'] = code
                    self.virtual_history.append(pos)
                    
                    self.virtual_positions.pop(code, None)
                    self.data_manager.delete_active_position(code)
                    self.log(f"🚨 [Portfolio] {pos['name']}({code}) 최종 청산 완료 ({pnl_pct:+.2f}%)")
                    if self.telegram_callback:
                        msg = f"<b>🚨 [매도완료/{exit_reason}]</b>\n종목: {pos['name']}({code})\n최종수익: {pnl_pct:+.1f}%\n수익금: {int(pnl_amt):,}원\n시간: {now_str}"
                        threading.Thread(target=self.telegram_callback, args=(msg,), daemon=True).start()

        self.save_db()
        if self.gui_callback: self.gui_callback()

    def clear_all(self):
        """모든 가상 포지션을 초기화합니다."""
        with self.lock:
            codes = list(self.virtual_positions.keys())
            for code in codes:
                self.data_manager.delete_active_position(code)
            self.virtual_positions = {}
        self.save_db()
        if self.gui_callback: self.gui_callback()

    def sell_all(self):
        """[v3.8] 비상 상황: 모든 가상/실제 포지션 즉시 시장가 매도 청산"""
        with self.lock:
            codes = list(self.virtual_positions.keys())
            for code in codes:
                pos = self.virtual_positions[code]
                qty = pos.get('qty', 0)
                if qty > 0:
                    # 시장가(03) 매도 주문
                    self.order_manager.place_order(code, qty, 0, order_type="03", side="sell")
                self.data_manager.delete_active_position(code)
            self.virtual_positions = {}
        self.save_db()
        if self.gui_callback: self.gui_callback()
