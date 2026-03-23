import tkinter as tk
from tkinter import ttk
import threading
import asyncio
import websockets
import json
import time
import pandas as pd
from datetime import datetime, timedelta
import FinanceDataReader as fdr

# Custom modules
import config
from kiwoom_rest_api.auth.token import TokenManager

class MonitorTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        self._setup_ui()

    def _setup_ui(self):
        """실시간 감시 탭 레이아웃 설정"""
        mo_main_frame = ttk.Frame(self.parent, padding="15")
        mo_main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 컨트롤 영역
        ctrl_frame = ttk.Frame(mo_main_frame)
        ctrl_frame.pack(fill=tk.X, pady=(0, 15))
        
        self.app.btn_monitor_toggle = ttk.Button(ctrl_frame, text="▶️ 실시간 감시 시작", command=self.app.toggle_monitoring, style="Accent.TButton")
        self.app.btn_monitor_toggle.pack(side=tk.RIGHT, padx=5)
        
        self.app.lbl_monitor_status = ttk.Label(ctrl_frame, text="상태: 대기 중", font=("Malgun Gothic", 10), foreground="#81A1C1")
        self.app.lbl_monitor_status.pack(side=tk.RIGHT, padx=10)

        # 전략 요약 정보 라벨
        self.app.lbl_criteria = ttk.Label(mo_main_frame, text="", font=("Malgun Gothic", 9), foreground="#ECEFF4", justify=tk.LEFT)
        self.app.lbl_criteria.pack(fill=tk.X)
        self.app._update_criteria_label() 
        
        # 트리뷰 영역
        tree_frame = ttk.Frame(mo_main_frame)
        tree_frame.pack(fill=tk.BOTH, expand=True)
        
        columns = ("Score", "Code", "Name", "Open", "Price", "Diff", "Ratio", "Volume", "TrdeVal")
        self.app.tree_monitor = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        
        headings = {
            "Score": "가점", "Code": "코드", "Name": "종목명",
            "Open": "시가", "Price": "현재가", 
            "Diff": "대비", "Ratio": "등락률", "Volume": "거래량", "TrdeVal": "거래대금"
        }
        for col, text in headings.items():
            self.app.tree_monitor.heading(col, text=text)
            
        col_widths = {
            "Score": 45, "Code": 65, "Name": 120,
            "Open": 85, "Price": 85, "Diff": 75, "Ratio": 75,
            "Volume": 105, "TrdeVal": 80
        }
        for col, width in col_widths.items():
            self.app.tree_monitor.column(col, width=width, anchor=tk.E if col not in ["Code", "Name", "Score"] else tk.W)
        
        yscrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.app.tree_monitor.yview)
        self.app.tree_monitor.configure(yscroll=yscrollbar.set)
        yscrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        if self.app.tree_monitor: self.app.tree_monitor.pack(side=tk.LEFT, expand=True, fill=tk.BOTH)
        
        # [v3.0] 우클릭 컨텍스트 메뉴 바인딩
        # self.app.tree_monitor.bind("<Button-3>", self.app._on_tree_monitor_right_click)
        
        # 태그 색상 설정 (노르딕 테마 고정밀 - 저채도)
        self.app.tree_monitor.tag_configure("highlight_green", background="#434C5E", foreground="#A3BE8C") 
        self.app.tree_monitor.tag_configure("rsi_signal", background="#4C566A", foreground="#88C0D0", font=("Malgun Gothic", 10, "bold")) 
        self.app.tree_monitor.tag_configure("buy_signal", background="#EBCB8B", foreground="#2E3440", font=("Malgun Gothic", 10, "bold"))
        self.app.tree_monitor.tag_configure("price_up", foreground="#BF616A") 
        self.app.tree_monitor.tag_configure("price_down", foreground="#81A1C1")
        self.app.tree_monitor.tag_configure("blink", background="#434C5E") # [v3.1] 주가 변동 시 하이라이트 효과

    async def _ws_client_loop(self):
        """키움 REST API 방식 웹소켓 루프"""
        try:
            self.app.root.after(0, self.app.log, "📡 [시스템] 실시간 서버 접속 시도...")
            
            token_manager = TokenManager()
            access_token = token_manager.get_token()
            if not access_token:
                self.app.root.after(0, self.app.log, "❌ [시스템] 액세스 토큰이 없습니다.")
                return
            
            if not self.app.monitor_codes:
                self.app.root.after(0, self.app.log, "⚠️ [시스템] 감시 대상 종목이 없습니다.")
                self.app.is_monitoring = False
                return

            ws_url = 'wss://api.kiwoom.com:10000/api/dostk/websocket'
            async with websockets.connect(ws_url, ping_interval=20) as ws:
                self.app.root.after(0, self.app.log, "✅ [시스템] 서버 연결 성공. 로그인 시도...")
                
                login_packet = {'trnm': 'LOGIN', 'token': access_token}
                await ws.send(json.dumps(login_packet))

                self.app.debug_packet_logged = False 
                
                while self.app.is_monitoring:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        raw_data = json.loads(msg)
                        data_list = raw_data if isinstance(raw_data, list) else [raw_data]
                        
                        for packet in data_list:
                            trnm = packet.get('trnm')
                            if trnm == 'LOGIN':
                                if packet.get('return_code') == 0:
                                    self.app.log(f"✅ [시스템] 로그인 성공. 종목 등록 ({len(self.app.monitor_codes)}개)...")
                                    reg_packet = {
                                        'trnm': 'REG', 'grp_no': '1', 'refresh': '1', 'market': 'AL',
                                        'data': [{'item': self.app.monitor_codes, 'type': ['0B']}]
                                    }
                                    await ws.send(json.dumps(reg_packet))
                                else:
                                    self.app.log(f"❌ [시스템] 로그인 실패: {packet.get('return_msg')}")
                                    self.app.is_monitoring = False
                                    break
                            elif trnm == 'PING':
                                await ws.send(json.dumps(packet))
                            elif trnm == 'REAL' or ('values' in packet and ('item' in packet or 'stk_cd' in packet)):
                                self._process_realtime_packet(packet)
                    except asyncio.TimeoutError:
                        continue
                    except Exception as e:
                        self.app.log(f"⚠️ [시스템] 수신 에러: {e}")
                        break
        except Exception as e:
            self.app.log(f"❌ [시스템] 연결 에러: {e}")
        finally:
            self.app.is_monitoring = False
            self.app.root.after(0, self.app.stop_monitoring)

    def _process_realtime_packet(self, packet):
        """실시간 패킷 처리 로직 분리"""
        trnm = packet.get('trnm')
        if trnm == 'REAL':
            payload = packet.get('data', [])
            if not isinstance(payload, list): payload = [payload]
        else:
            payload = [packet]
        
        for item_data in payload:
            raw_code = item_data.get('item', item_data.get('stk_cd', item_data.get('item_code', '')))
            full_code = str(raw_code).strip()
            code = full_code.split('_')[0].zfill(6)
            
            if code in self.app.monitor_info:
                info = self.app.monitor_info[code]
                vals = item_data.get('values', {})
                
                def get_val(fid, default=0):
                    v = vals.get(str(fid))
                    if v is None or v == "": return default
                    try: return float(str(v).replace('+', '').replace('-', '').strip())
                    except: return default

                now_price = int(get_val(10)) 
                if now_price == 0: continue
                
                volume = int(get_val(13))
                trde_val = int(get_val(26) // 1000)
                ratio_val = vals.get('12', '0')
                diff_val = vals.get('11', '0')
                open_price = int(get_val(16))
                
                # Update Info
                info['price'] = now_price
                info['ratio'] = ratio_val
                info['diff'] = diff_val
                info['volume'] = volume
                info['trde_val'] = trde_val
                
                # NXT Detection
                mkt = item_data.get('market', item_data.get('mkt', ''))
                if mkt == 'NXT' or '_nx' in full_code.lower():
                    info['is_nxt'] = True

                # Signal processing (simplified for modularity, calls app)
                self.app._on_realtime_data_received(code, now_price, volume, trde_val)
