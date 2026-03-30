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
        
        self.btn_monitor_toggle = ttk.Button(self.ctrl_frame, text="▶️ 실시간 감시 시작", command=self.app.toggle_monitoring, style="Accent.TButton")
        self.btn_monitor_toggle.pack(side=tk.RIGHT, padx=5)
        
        self.lbl_monitor_status = ttk.Label(self.ctrl_frame, text="상태: 대기 중", font=("Malgun Gothic", 10), foreground=self.app.colors['primary'])
        self.lbl_monitor_status.pack(side=tk.RIGHT, padx=10)

        self.pb_warmup = ttk.Progressbar(self.ctrl_frame, orient=tk.HORIZONTAL, length=180, mode='determinate')
        self.lbl_warmup_detail = ttk.Label(self.ctrl_frame, text="", font=("Malgun Gothic", 9), foreground=self.app.colors['secondary']) 
        
        self.lbl_headwind_caution = ttk.Label(self.ctrl_frame, text="⚠️ 시장 역풍 - 매수 신호가 일시 정지되었습니다", 
                                                font=("Malgun Gothic", 10, "bold"), foreground="#BF616A")
        
        # 트리뷰 영역 (Split-View 도입) [v2.2]
        paned = ttk.PanedWindow(self.mo_main_frame, orient=tk.VERTICAL)
        paned.pack(fill=tk.BOTH, expand=True)
        
        # [상단] 보유 종목 창 (세로폭 축소)
        active_frame = ttk.LabelFrame(paned, text=" 🔥 보유 및 대응 종목 (Active Positions) ", padding="5")
        paned.add(active_frame, weight=0) # 고정 비중(최소화)
        
        # [하단] 일반 감시 창 (확대)
        watchlist_frame = ttk.LabelFrame(paned, text=" 👁️ 실시간 감시 목록 (Watchlist) ", padding="5")
        paned.add(watchlist_frame, weight=1)
        
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

        # 1. 상단 트리뷰 (Active) - 높이 축소
        self.tree_active_monitor = ttk.Treeview(active_frame, columns=columns, show="headings", selectmode="browse", height=3)
        for col in columns:
            self.tree_active_monitor.heading(col, text=headings[col], anchor=tk.CENTER)
            anchor = tk.W if col == "Name" else tk.CENTER
            self.tree_active_monitor.column(col, width=col_widths[col], anchor=anchor)
        self.tree_active_monitor.pack(fill=tk.BOTH, expand=True)

        # 2. 하단 트리뷰 (Watchlist)
        self.tree_monitor = ttk.Treeview(watchlist_frame, columns=columns, show="headings", selectmode="browse")
        for col in columns:
            self.tree_monitor.heading(col, text=headings[col], anchor=tk.CENTER)
            anchor = tk.W if col == "Name" else tk.CENTER
            self.tree_monitor.column(col, width=col_widths[col], anchor=anchor)
        
        yscrollbar = ttk.Scrollbar(watchlist_frame, orient=tk.VERTICAL, command=self.tree_monitor.yview)
        self.tree_monitor.configure(yscroll=yscrollbar.set)
        yscrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree_monitor.pack(side=tk.LEFT, expand=True, fill=tk.BOTH)
        
        # [v4.7.5] 'HD Crisp UI' 도입: 굵지 않고 날렵한 Malgun Gothic 및 차분한 프로 컬러 사용
        row_font = ("Malgun Gothic", 9, "normal")
        up_col = "#E35F5F" # 채도가 너무 높아 번져 보이는 #FF0000 대신 차분한 레드
        dn_col = "#81A1C3" # 너무 쨍한 파란색 대신 가시성 좋은 노르딕 블루
        
        for t in [self.tree_monitor, self.tree_active_monitor]:
            # 1. 상승 (레드)
            t.tag_configure("up_even", foreground=up_col, background="#3B4252", font=row_font)
            t.tag_configure("up_odd", foreground=up_col, background="#242933", font=row_font)
            
            # 2. 하락 (블루)
            t.tag_configure("down_even", foreground=dn_col, background="#3B4252", font=row_font)
            t.tag_configure("down_odd", foreground=dn_col, background="#242933", font=row_font)
            
            # 3. 보합 (기본)
            t.tag_configure("stable_even", foreground="#D8DEE9", background="#3B4252", font=row_font)
            t.tag_configure("stable_odd", foreground="#D8DEE9", background="#242933", font=row_font)
            
            # 4. 보유 종목 특수 (Active)
            t.tag_configure("active_up", foreground=up_col, background="#434C5E", font=row_font)
            t.tag_configure("active_down", foreground=dn_col, background="#434C5E", font=row_font)
            t.tag_configure("active_stable", foreground="#D8DEE9", background="#434C5E", font=row_font)
            
            # 5. 보조 상태 (합치도)
            t.tag_configure("status_high", foreground="#EBCB8B", font=row_font)
            t.tag_configure("status_low", foreground="#616E88", font=row_font)

    async def _ws_client_loop(self):
        """키움 REST API 방식 웹소켓 루프 (자동 재연결 및 안정성 강화)"""
        while self.app.is_monitoring:
            try:
                self.app.root.after(0, self.app.log, "📡 [시스템] 실시간 서버 접속 시도...")
                
                access_token = self.app.auth_manager.get_auth_token()
                if not access_token:
                    self.app.root.after(0, self.app.log, "⏳ [시스템] 거래 서버 점검 또는 인증 대기 중... (10초 후 재시도)")
                    await asyncio.sleep(10)
                    continue
                
                if not self.app.monitor_codes:
                    self.app.root.after(0, self.app.log, "⚠️ [시스템] 감시 대상 종목이 없습니다.")
                    self.app.is_monitoring = False
                    return

                if getattr(config, 'USE_MOCK', False):
                    ws_url = 'wss://mockapi.kiwoom.com:10000/api/dostk/websocket'
                else:
                    ws_url = 'wss://api.kiwoom.com:10000/api/dostk/websocket'
                
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
                                        break
                                elif trnm == 'PING':
                                    await ws.send(json.dumps(packet))
                                elif trnm in ['REAL', 'CHE'] or ('values' in packet and ('item' in packet or 'stk_cd' in packet)):
                                    self._process_realtime_packet(packet)
                        except asyncio.TimeoutError:
                            continue
                        except Exception as e:
                            self.app.log(f"⚠️ [시스템] 수신 에러: {e}")
                            break
                            
            except Exception as e:
                if self.app.is_monitoring:
                    error_msg = str(e)
                    wait_sec = 5
                    if "429" in error_msg:
                        wait_sec = 60
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
            
            if trnm == 'CHE':
                order_no = item_data.get('ord_no')
                cntg_qty = int(item_data.get('cntg_qty', 0))
                cntg_unpr = int(item_data.get('cntg_unpr', 0))
                buy_sell_tp = item_data.get('buy_sell_tp', '1') # 1:매도, 2:매수
                side = "buy" if buy_sell_tp == '2' else "sell"
                self.app._on_execution_received(code, order_no, cntg_qty, cntg_unpr, side)
                continue

            if code in self.app.monitor_info:
                info = self.app.monitor_info[code]
                vals = item_data.get('values', {})
                p_type = item_data.get('type', '0B')
                
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
                    vol_power = get_val(228)
                    info['price'] = now_price
                    info['ratio'] = ratio_val
                    info['diff'] = diff_val
                    info['volume'] = volume
                    info['trde_val'] = trde_val
                    info['vol_power'] = vol_power
                    self.app._on_realtime_data_received(code, now_price, volume, trde_val, vol_power=vol_power)
                elif p_type == '0D':
                    total_ask = int(get_val(121))
                    total_bid = int(get_val(125))
                    info['total_ask'] = total_ask
                    info['total_bid'] = total_bid
                    self.app._on_realtime_data_received(code, info.get('price', 0), info.get('volume', 0), info.get('trde_val', 0), 
                                                        total_ask=total_ask, total_bid=total_bid)
                mkt = item_data.get('market', item_data.get('mkt', ''))
                if mkt == 'NXT' or '_nx' in full_code.lower():
                    info['is_nxt'] = True

    def _update_visual_theme(self, trend):
        """[v27.3] 시장 지질(Background) 테마를 '온몸'으로 변경"""
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
        style = ttk.Style()
        style.configure("Monitor.TFrame", background=t['bg'])
        style.configure("Monitor.TLabel", background=t['bg'], foreground=t['fg'])
        self.mo_main_frame.config(style="Monitor.TFrame")
        self.ctrl_frame.config(style="Monitor.TFrame")
        for widget in self.mo_main_frame.winfo_children():
            if isinstance(widget, ttk.Label): widget.config(style="Monitor.TLabel")
        for widget in self.ctrl_frame.winfo_children():
            if isinstance(widget, ttk.Label): widget.config(style="Monitor.TLabel")
        style.configure("Treeview", fieldbackground=t['field'], background=t['field'])
        if self.tree_monitor:
            self.tree_monitor.tag_configure("oddrow", background=t['odd'])
            self.tree_monitor.tag_configure("evenrow", background=t['even'])
        if hasattr(self, 'lbl_headwind_caution'):
            self.lbl_headwind_caution.config(text=t['desc'], background=t['bg'])
            if trend == "UP": self.lbl_headwind_caution.pack_forget()
            else: self.lbl_headwind_caution.pack(side=tk.LEFT, padx=10)
        if self.app.status_label:
            self.app.status_label.config(text=f"장세: {trend}", foreground=t['fg'], background=t['bg'])

    def update_view(self, updates, virtual_positions):
        """[v4.5.3] App으로부터 전달받은 실시간 틱 데이터를 화면에 렌더링 (캡슐화)"""
        if not self.tree_monitor: return
        
        try:
            if updates:
                items = list(updates.items())
                updates.clear()
                
                for iid, (values, tag, _) in items:
                    # 보유 여부에 따라 타겟 트리 결정
                    is_active = iid in virtual_positions
                    target_tree = self.tree_active_monitor if is_active else self.tree_monitor
                    other_tree = self.tree_monitor if is_active else self.tree_active_monitor
                    
                    # 반대편 트리에 있으면 삭제 (이동 효과)
                    if other_tree.exists(iid):
                        other_tree.delete(iid)
                        
                    # [v4.5.42] 행의 인덱스와 기본 태그 정의 (안전한 index 추출)
                    if not target_tree.exists(iid):
                        target_tree.insert("", tk.END, iid=iid, values=values)
                    
                    # [Composite Tag Selection] v4.6.5
                    idx = target_tree.index(iid)
                    is_even = (idx % 2 == 0)
                    row_type = "even" if is_even else "odd"
                    
                    # 방향 결정
                    if "price_up" in str(tag): direction = "up"
                    elif "price_down" in str(tag): direction = "down"
                    else: direction = "stable"
                    
                    # 최종 태그 조합
                    if is_active:
                        final_tag = f"active_{direction}"
                    else:
                        final_tag = f"{direction}_{row_type}"
                    
                    # 데이터와 태그가 변경된 경우에만 업데이트
                    curr = target_tree.item(iid)
                    if curr['values'] != values or final_tag not in curr['tags']:
                        target_tree.item(iid, values=values, tags=(final_tag,))
        except Exception as e:
            print(f"[UI Error] MonitorTab Render Error: {e}")

    def update_watchlist(self, monitor_info):
        """[v4.5.3] 감시 종목 리스트 초기화 렌더링"""
        self.tree_monitor.delete(*self.tree_monitor.get_children())
        self.tree_active_monitor.delete(*self.tree_active_monitor.get_children())
        
        # [v4.5.7] 가점(Score) 기준 내림차순 정렬하여 초기 삽입
        sorted_items = sorted(monitor_info.items(), key=lambda x: x[1].get('score', 0), reverse=True)
        
        for code, info in sorted_items:
            now_p = info.get('price', 0)
            values = (
                f"{info.get('score', 0):.1f}", code, info['name'],
                f"{now_p:,.0f}", info.get('ratio', '0'), 
                f"{info.get('vol_power', 0):.1f}%", info.get('status_text', '대기'),
                f"{info.get('total_ask', 0):,}", f"{info.get('total_bid', 0):,}",
                f"{info.get('volume', 0):,}", f"{info.get('trde_val', 0):,.1f}"
            )
            self.tree_monitor.insert("", tk.END, iid=code, values=values)

    def load_targets(self, codes_or_info):
        """KiwoomApp과의 호환성을 위한 래퍼"""
        if isinstance(codes_or_info, dict):
            self.update_watchlist(codes_or_info)
        else:
            # codes 리스트인 경우 (TradingController로부터 받은 경우 등)
            pass

    def update_warmup(self, cur, total, name, code):
        """[v4.6.0] 비동기 배경 동기화(Sync) 진행률 UI 업데이트"""
        if not hasattr(self, 'pb_warmup'): return
        
        # [v4.6.0] 동기화 중임을 적극적으로 알림
        if not self.pb_warmup.winfo_ismapped():
            self.pb_warmup.pack(side=tk.LEFT, padx=(20, 10))
            self.lbl_warmup_detail.pack(side=tk.LEFT, padx=5)
            
        pct = (cur / total) * 100
        self.pb_warmup['value'] = pct
        self.lbl_warmup_detail.config(text=f"🔄 동기화 중: {name} ({cur}/{total})")
        
        if cur >= total:
            self.lbl_warmup_detail.config(text="✅ 동기화 완료", foreground="#A3BE8C") # Success Green
            self.app.root.after(5000, self._hide_warmup)

    def _hide_warmup(self):
        if hasattr(self, 'pb_warmup'):
            self.pb_warmup.pack_forget()
            self.lbl_warmup_detail.pack_forget()
