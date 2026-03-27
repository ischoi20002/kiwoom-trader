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
        self.mo_main_frame = ttk.Frame(self.parent, padding="15")
        self.mo_main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 컨트롤 영역
        self.ctrl_frame = ttk.Frame(self.mo_main_frame)
        self.ctrl_frame.pack(fill=tk.X, pady=(0, 15))
        
        self.app.btn_monitor_toggle = ttk.Button(self.ctrl_frame, text="▶️ 실시간 감시 시작", command=self.app.toggle_monitoring, style="Accent.TButton")
        self.app.btn_monitor_toggle.pack(side=tk.RIGHT, padx=5)
        
        self.app.lbl_monitor_status = ttk.Label(self.ctrl_frame, text="상태: 대기 중", font=("Malgun Gothic", 10), foreground="#81A1C1")
        self.app.lbl_monitor_status.pack(side=tk.RIGHT, padx=10)

        # [v3.5] 시스템 심박수 (Heartbeat)
        self.app.lbl_heartbeat = ttk.Label(self.ctrl_frame, text="💓 심박수: 대기", font=("Malgun Gothic", 9), foreground="#A9A9A9")
        self.app.lbl_heartbeat.pack(side=tk.LEFT, padx=10)

        # [v27.3] 시장 역풍 알림 라벨 (기본 숨김)
        self.app.lbl_headwind_caution = ttk.Label(self.ctrl_frame, text="⚠️ 시장 역풍 - 매수 신호가 일시 정지되었습니다", 
                                                font=("Malgun Gothic", 10, "bold"), foreground="#BF616A")
        # 초기에는 pack하지 않고 대기

        # 전략 요약 정보 라벨
        self.app.lbl_criteria = ttk.Label(self.mo_main_frame, text="", font=("Malgun Gothic", 9), foreground="#ECEFF4", justify=tk.LEFT)
        self.app.lbl_criteria.pack(fill=tk.X)
        self.app._update_criteria_label() 
        
        # 트리뷰 영역 (Split-View 도입) [v2.2]
        paned = ttk.PanedWindow(self.mo_main_frame, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True)
        
        # [상단] 보유 종목 창
        active_frame = ttk.LabelFrame(paned, text=" 🔥 보유 및 대응 종목 (Active Positions) ", padding="5")
        paned.add(active_frame, weight=1)
        
        # [하단] 일반 감시 창
        watchlist_frame = ttk.LabelFrame(paned, text=" 👁️ 실시간 감시 목록 (Watchlist) ", padding="5")
        paned.add(watchlist_frame, weight=2)
        
        columns = ("Score", "Code", "Name", "Price", "Ratio", "VolPower", "Status", "TotalAsk", "TotalBid", "Volume", "TrdeVal")
        headings = {
            "Score": "가점", "Code": "코드", "Name": "종목명",
            "Price": "현재가", "Ratio": "등락률", 
            "VolPower": "체결강도", "Status": "신호상태", "TotalAsk": "매도잔량", "TotalBid": "매수잔량",
            "Volume": "거래량", "TrdeVal": "대금(억)"
        }
        col_widths = {
            "Score": 50, "Code": 75, "Name": 120,
            "Price": 100, "Ratio": 95, 
            "VolPower": 85, "Status": 90, "TotalAsk": 100, "TotalBid": 100,
            "Volume": 110, "TrdeVal": 75
        }

        # 1. 상단 트리뷰 (Active)
        self.app.tree_active_monitor = ttk.Treeview(active_frame, columns=columns, show="headings", selectmode="browse", height=5)
        for col in columns:
            self.app.tree_active_monitor.heading(col, text=headings[col], anchor=tk.CENTER)
            anchor = tk.W if col == "Name" else tk.CENTER
            self.app.tree_active_monitor.column(col, width=col_widths[col], anchor=anchor)
        self.app.tree_active_monitor.pack(fill=tk.BOTH, expand=True)

        # 2. 하단 트리뷰 (Watchlist)
        self.app.tree_monitor = ttk.Treeview(watchlist_frame, columns=columns, show="headings", selectmode="browse")
        for col in columns:
            self.app.tree_monitor.heading(col, text=headings[col], anchor=tk.CENTER)
            anchor = tk.W if col == "Name" else tk.CENTER
            self.app.tree_monitor.column(col, width=col_widths[col], anchor=anchor)
        
        yscrollbar = ttk.Scrollbar(watchlist_frame, orient=tk.VERTICAL, command=self.app.tree_monitor.yview)
        self.app.tree_monitor.configure(yscroll=yscrollbar.set)
        yscrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.app.tree_monitor.pack(side=tk.LEFT, expand=True, fill=tk.BOTH)
        
        # [v3.0] 우클릭 컨텍스트 메뉴 바인딩
        # self.app.tree_monitor.bind("<Button-3>", self.app._on_tree_monitor_right_click)
        
        # 태그 색상 설정 (노르딕 테마 고정밀 - 저채도)
        for t in [self.app.tree_monitor, self.app.tree_active_monitor]:
            t.tag_configure("oddrow", background="#242933") # Polar Night
            t.tag_configure("evenrow", background="#3B4252") # Lighter Slate (More contrast)
            t.tag_configure("price_up", foreground="#FF4C4C") # 선명한 빨강 (상승)
            t.tag_configure("price_down", foreground="#4C9EFF") # 선명한 파랑 (하락)
            t.tag_configure("price_stable", foreground="#A9A9A9") # 보합 (Silver Grey)
            t.tag_configure("status_high", foreground="#EBCB8B") # 높은 합치도 (Yellow/Orange)
            t.tag_configure("status_low", foreground="#4C566A") # 낮은 합치도 (Grey Blue)
            t.tag_configure("active_pos", background="#434C5E") # 보유 중 강조 배경

    async def _ws_client_loop(self):
        """키움 REST API 방식 웹소켓 루프 (자동 재연결 및 안정성 강화)"""
        while self.app.is_monitoring:
            try:
                self.app.root.after(0, self.app.log, "📡 [시스템] 실시간 서버 접속 시도...")
                
                token_manager = TokenManager()
                access_token = token_manager.get_token()
                if not access_token:
                    self.app.root.after(0, self.app.log, "❌ [시스템] 액세스 토큰이 없습니다. 10초 후 재시도...")
                    await asyncio.sleep(10)
                    continue
                
                if not self.app.monitor_codes:
                    self.app.root.after(0, self.app.log, "⚠️ [시스템] 감시 대상 종목이 없습니다.")
                    self.app.is_monitoring = False
                    return

                # [v3.7.3] 모의투자 환경에 따른 웹소켓 주소 동적 결정
                if getattr(config, 'USE_MOCK', False):
                    ws_url = 'wss://mockapi.kiwoom.com:10000/api/dostk/websocket'
                else:
                    ws_url = 'wss://api.kiwoom.com:10000/api/dostk/websocket'
                
                # [v3.5] ping_timeout 추가 및 안정성 강화
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
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
                                            'data': [{'item': self.app.monitor_codes, 'type': ['0B', '0D']}]
                                        }
                                        await ws.send(json.dumps(reg_packet))
                                    else:
                                        self.app.log(f"❌ [시스템] 로그인 실패: {packet.get('return_msg')}. 15초 후 재시도...")
                                        await asyncio.sleep(15)
                                        break # Try reconnecting from scratch
                                elif trnm == 'PING':
                                    await ws.send(json.dumps(packet))
                                elif trnm == 'REAL' or ('values' in packet and ('item' in packet or 'stk_cd' in packet)):
                                    self._process_realtime_packet(packet)
                        except asyncio.TimeoutError:
                            continue
                        except Exception as e:
                            self.app.log(f"⚠️ [시스템] 수신 에러: {e}")
                            break # Inner loop break -> triggers reconnect
                            
            except Exception as e:
                if self.app.is_monitoring:
                    # [v3.5.1] API Error (HTTP 429) 감지 시 대기 시간 대폭 연장
                    error_msg = str(e)
                    wait_sec = 5
                    if "429" in error_msg:
                        wait_sec = 60 # 429 발생 시 1분 대기
                        self.app.log(f"🛑 [시스템] API 호출 제한(429) 감지. {wait_sec}초 후 다시 시도합니다.")
                    else:
                        self.app.log(f"❌ [시스템] 연결 에러: {e}. {wait_sec}초 후 재연결 시도...")
                    
                    await asyncio.sleep(wait_sec)
            finally:
                if not self.app.is_monitoring:
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
                p_type = item_data.get('type', '0B') # 기본값 0B
                
                def get_val(fid, default=0):
                    v = vals.get(str(fid))
                    if v is None or v == "": return default
                    try: return float(str(v).replace('+', '').replace('-', '').strip())
                    except: return default

                if p_type == '0B':
                    now_price = int(get_val(10)) 
                    if now_price == 0: return
                    
                    volume = int(get_val(13))
                    trde_val = int(get_val(26) // 1000)
                    ratio_val = vals.get('12', '0')
                    diff_val = vals.get('11', '0')
                    vol_power = get_val(228) # 체결강도
                    
                    # Update Info
                    info['price'] = now_price
                    info['ratio'] = ratio_val
                    info['diff'] = diff_val
                    info['volume'] = volume
                    info['trde_val'] = trde_val
                    info['vol_power'] = vol_power
                    
                    # Signal processing
                    self.app._on_realtime_data_received(code, now_price, volume, trde_val, vol_power=vol_power)

                elif p_type == '0D':
                    total_ask = int(get_val(121)) # 매도호가총잔량
                    total_bid = int(get_val(125)) # 매수호가총잔량
                    
                    info['total_ask'] = total_ask
                    info['total_bid'] = total_bid
                    
                    # 호가 잔량 업데이트를 위해 signal_engine 호출 (가격 정보는 이전 값 유지)
                    self.app._on_realtime_data_received(code, info.get('price', 0), info.get('volume', 0), info.get('trde_val', 0), 
                                                        total_ask=total_ask, total_bid=total_bid)

                # NXT Detection
                mkt = item_data.get('market', item_data.get('mkt', ''))
                if mkt == 'NXT' or '_nx' in full_code.lower():
                    info['is_nxt'] = True

    def _update_visual_theme(self, trend):
        """[v27.3] 시장 지질(Background) 테마를 '온몸'으로 변경"""
        # 1. 색상 정의
        themes = {
            "UP": {
                "bg": "#2E3440", "field": "#242933", 
                "odd": "#242933", "even": "#3B4252",
                "fg": "#ECEFF4", "desc": "🟢 시장 순풍: 모든 전략 가동 중"
            },
            "NEUTRAL": {
                "bg": "#3B4252", "field": "#3B4252", 
                "odd": "#343B47", "even": "#434C5E",
                "fg": "#D8DEE9", "desc": "⚪ 시장 중립: Type B 매수 차단 (관망 권장)"
            },
            "DOWN": {
                "bg": "#3B2E2E", "field": "#3B2E2E", 
                "odd": "#2E2424", "even": "#423B3B",
                "fg": "#D8DEE9", "desc": "⚠️ 시장 역풍: 모든 매수 신호 정지"
            }
        }
        t = themes.get(trend, themes["UP"])
        
        # 2. 메인 프레임 및 컨트롤 영역 배경 변경
        style = ttk.Style()
        style.configure("Monitor.TFrame", background=t['bg'])
        style.configure("Monitor.TLabel", background=t['bg'], foreground=t['fg'])
        
        self.mo_main_frame.config(style="Monitor.TFrame")
        self.ctrl_frame.config(style="Monitor.TFrame")
        
        # 2.1 하위 라벨들 스타일 일괄 적용
        for widget in self.mo_main_frame.winfo_children():
            if isinstance(widget, ttk.Label):
                widget.config(style="Monitor.TLabel")
        for widget in self.ctrl_frame.winfo_children():
            if isinstance(widget, ttk.Label):
                widget.config(style="Monitor.TLabel")
        
        # 3. 트리뷰 필드 및 줄무늬 색상 변경
        style.configure("Treeview", fieldbackground=t['field'], background=t['field'])
        if self.app.tree_monitor:
            self.app.tree_monitor.tag_configure("oddrow", background=t['odd'])
            self.app.tree_monitor.tag_configure("evenrow", background=t['even'])
        
        # 4. 상태 안내 문구 및 가시성 제어
        if hasattr(self.app, 'lbl_headwind_caution'):
            self.app.lbl_headwind_caution.config(text=t['desc'], background=t['bg'])
            if trend == "UP":
                self.app.lbl_headwind_caution.pack_forget()
            else:
                self.app.lbl_headwind_caution.pack(side=tk.LEFT, padx=10)
                
        # 5. 하단 상태바 동기화 전파 (선택사항)
        if self.app.status_label:
            self.app.status_label.config(text=f"장세: {trend}", foreground=t['fg'], background=t['bg'])
