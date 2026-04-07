import tkinter as tk
from tkinter import ttk, messagebox
import threading
from typing import Any, Dict, Optional, Tuple
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


def _monitor_fmt_vs_prev(info: Dict[str, Any]) -> str:
    """전일 종가 대비 등락폭(원). 실시간 fid 11 등 → monitor_info['diff']."""
    try:
        raw = info.get("diff")
        if raw is None:
            return "—"
        s = str(raw).strip().replace(",", "")
        if s in ("", "-", "—"):
            return "—"
        iv = int(round(float(s)))
        if iv == 0:
            return "0"
        return f"{iv:+,}"
    except Exception:
        return "—"


def _monitor_fmt_bid_ask(info: Dict[str, Any]) -> str:
    """매도잔량 / 매수잔량 (호가)."""
    try:
        ta = int(float(info.get("total_ask") or 0))
        tb = int(float(info.get("total_bid") or 0))
        return f"{ta:,} / {tb:,}"
    except Exception:
        return "— / —"


def format_strategy_type_label(sel_track: object) -> str:
    """
    선정 트랙(sel_track: A / B / AB) → 실시간 감시 '전략유형' 컬럼 표시문자열.
    SignalEngine 등에서 동일 문구를 쓰려면 이 함수를 import 해 사용합니다.
    """
    try:
        tr = str(sel_track if sel_track is not None else "").strip().upper()
        if tr == "A":
            return "전략 A"
        if tr == "B":
            return "전략 B"
        if tr in ("AB", "A+B"):
            return "전략 A+B"
        return "전략 A+B"
    except Exception:
        return "전략 A+B"


class MonitorTab:
    def __init__(self, parent, app):
        self.parent = parent
        self.app = app
        # 보유 종목 추가 시 웹소켓 REG 재전송 요청(과도 호출 방지는 루프에서 스로틀)
        self._pending_reg_refresh: bool = False
        self._last_stream_reg_ts: float = 0.0
        # 실시간 틱 경로: 계좌 동기화 1프레임 등으로 VP에만 없으면 보유→감시 트리로 잘못 옮기는 깜빡임 방지
        self._vp_ui_inactive_streak: Dict[str, int] = {}
        # 틱이 끊긴 종목도 monitor_info 기준으로 트리에 유지 (봉 마감·무거래 구간)
        self._last_monitor_tree_reconcile_ts: float = 0.0
        self._setup_ui()

    def _setup_ui(self):
        """실시간 감시 탭 레이아웃 설정"""
        self.mo_main_frame = ttk.Frame(self.parent, padding="15")
        self.mo_main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 컨트롤 영역
        self.ctrl_frame = ttk.Frame(self.mo_main_frame)
        self.ctrl_frame.pack(fill=tk.X, pady=(0, 15))

        # [임시] 매수신호 없이 주문·체결 흐름 테스트 — 감시/보유 트리에서 행 선택 후 1주 시장가
        test_wrap = ttk.Frame(self.ctrl_frame)
        test_wrap.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(
            test_wrap,
            text="테스트 주문 (임시 · 1주 시장가)",
            font=("Malgun Gothic", 8),
            foreground="#81A1C1",
        ).pack(anchor=tk.W)
        test_btns = ttk.Frame(test_wrap)
        test_btns.pack(fill=tk.X)
        ttk.Button(
            test_btns,
            text="1주 매수",
            width=10,
            command=lambda: self._test_market_order_1share("buy"),
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            test_btns,
            text="1주 매도",
            width=10,
            command=lambda: self._test_market_order_1share("sell"),
        ).pack(side=tk.LEFT, padx=0)
        
        self.btn_monitor_toggle = ttk.Button(self.ctrl_frame, text="▶️ 실시간 감시 시작", command=self.app.toggle_monitoring, style="Accent.TButton")
        self.btn_monitor_toggle.pack(side=tk.RIGHT, padx=5)
        
        self.lbl_monitor_status = ttk.Label(self.ctrl_frame, text="상태: 대기 중", font=("Malgun Gothic", 10), foreground=self.app.colors['primary'])
        self.lbl_monitor_status.pack(side=tk.RIGHT, padx=10)

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
        
        columns = (
            "Score",
            "Code",
            "Name",
            "StratType",
            "Price",
            "VsPrev",
            "Ratio",
            "Status",
            "VolPower",
            "BidAsk",
            "Volume",
            "TrdeVal",
        )
        headings = {
            "Score": "가점",
            "Code": "코드",
            "Name": "종목명",
            "StratType": "전략유형",
            "Price": "현재가",
            "VsPrev": "대비",
            "Ratio": "등락률",
            "Status": "신호상태",
            "VolPower": "체결강도",
            "BidAsk": "Bid/Ask",
            "Volume": "거래량",
            "TrdeVal": "대금(억)",
        }
        col_widths = {
            "Score": 50,
            "Code": 75,
            "Name": 120,
            "StratType": 88,
            "Price": 100,
            "VsPrev": 88,
            "Ratio": 95,
            "Status": 320,
            "VolPower": 85,
            "BidAsk": 130,
            "Volume": 110,
            "TrdeVal": 75,
        }

        # 1. 상단 트리뷰 (Active) - 높이 축소
        self.tree_active_monitor = ttk.Treeview(active_frame, columns=columns, show="headings", selectmode="browse", height=3)
        for col in columns:
            self.tree_active_monitor.heading(col, text=headings[col], anchor=tk.CENTER)
            if col in ("Name", "StratType"):
                anchor = tk.W
            elif col in ("VsPrev", "BidAsk"):
                anchor = tk.E
            else:
                anchor = tk.CENTER
            self.tree_active_monitor.column(col, width=col_widths[col], anchor=anchor)
        self.tree_active_monitor.pack(fill=tk.BOTH, expand=True)

        # 2. 하단 트리뷰 (Watchlist)
        self.tree_monitor = ttk.Treeview(watchlist_frame, columns=columns, show="headings", selectmode="browse")
        for col in columns:
            self.tree_monitor.heading(col, text=headings[col], anchor=tk.CENTER)
            if col in ("Name", "StratType"):
                anchor = tk.W
            elif col in ("VsPrev", "BidAsk"):
                anchor = tk.E
            else:
                anchor = tk.CENTER
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
            
            # 4. 보유 종목 — 감시목록과 동일하게 등락색 + 짝/홀 줄무늬 배경
            for direction, fg in (("up", up_col), ("down", dn_col), ("stable", "#D8DEE9")):
                t.tag_configure(
                    f"active_{direction}_even",
                    foreground=fg,
                    background="#3B4252",
                    font=row_font,
                )
                t.tag_configure(
                    f"active_{direction}_odd",
                    foreground=fg,
                    background="#242933",
                    font=row_font,
                )
            
            # 5. 보조 상태 (합치도)
            t.tag_configure("status_high", foreground="#EBCB8B", font=row_font)
            t.tag_configure("status_low", foreground="#616E88", font=row_font)

            # 6. 워밍업(15분봉 API·DB 검증) 단계 구분
            t.tag_configure(
                "warmup_pending",
                foreground="#ECEFF4",
                background="#434C5E",
                font=row_font,
            )
            t.tag_configure(
                "warmup_ok_row",
                foreground="#A3BE8C",
                background="#3B4252",
                font=row_font,
            )
            t.tag_configure(
                "warmup_fail_row",
                foreground="#ECEFF4",
                background="#5c3d3d",
                font=row_font,
            )

    def get_tree_display_price(self, code: str) -> Optional[float]:
        """
        감시/보유 트리에 표시된 현재가(Price 컬럼). 없으면 None.
        OrderManager 시세 검증용.
        """
        try:
            c = str(code).zfill(6)
            for tree in (self.tree_monitor, self.tree_active_monitor):
                if not tree or not tree.exists(c):
                    continue
                vals = tree.item(c, "values") or ()
                if len(vals) > 4:
                    return float(str(vals[4]).replace(",", "").strip())
        except (TypeError, ValueError, tk.TclError):
            pass
        except Exception:
            pass
        return None

    def _get_selected_monitor_code_name_price(self) -> Optional[Tuple[str, str, int]]:
        """감시·보유 트리에서 선택된 종목 (code, name, 참고가).

        참고가: 지정가(±1틱) 주문의 기준가로 쓰는 값(트리 표시 현재가 우선, 없으면 monitor_info).
        """
        try:
            code: Optional[str] = None
            vals_row: tuple = ()
            for tree in (self.tree_monitor, self.tree_active_monitor):
                if not tree:
                    continue
                sel = tree.selection()
                if not sel:
                    continue
                code = str(sel[0]).zfill(6)
                vals_row = tree.item(sel[0], "values") or ()
                break
            if not code:
                return None
            mi: Dict[str, Any] = (self.app.monitor_info or {}).get(code, {}) or {}
            name = str(mi.get("name", code))
            # 트리 "현재가" 컬럼은 실시간 갱신 우선 — monitor_info.last_price 는 지연될 수 있음
            lp = 0.0
            if len(vals_row) > 4:
                try:
                    lp = float(str(vals_row[4]).replace(",", "").strip())
                except (TypeError, ValueError):
                    lp = 0.0
            if lp <= 0:
                try:
                    lp = float(mi.get("last_price") or mi.get("price") or 0)
                except (TypeError, ValueError):
                    lp = 0.0
            if lp <= 0:
                lp = 1.0
            return code, name, int(lp)
        except Exception:
            return None

    def _test_market_order_1share(self, side: str) -> None:
        """선택 종목에 대해 1주 시장가 테스트 주문(워커 스레드에서 실행)."""
        side = (side or "buy").lower().strip()
        if side not in ("buy", "sell"):
            return

        picked = self._get_selected_monitor_code_name_price()
        if not picked:
            messagebox.showwarning(
                "테스트 주문",
                "감시 목록 또는 보유 종목 트리에서 종목 한 줄을 먼저 선택하세요.",
            )
            return
        code, name, ref_p = picked

        use_mock = getattr(config, "USE_MOCK", False)
        allow = (
            getattr(config, "ALLOW_MOCK_ORDER", False)
            if use_mock
            else getattr(config, "ALLOW_REAL_ORDER", False)
        )
        mode_kr = "모의투자" if use_mock else "실전"
        if not allow:
            messagebox.showwarning(
                "테스트 주문",
                f"{mode_kr}에서 주문이 차단되어 있습니다.\n"
                "config.py 의 ALLOW_MOCK_ORDER 또는 ALLOW_REAL_ORDER 를 True 로 설정하세요.",
            )
            return

        act = "매수" if side == "buy" else "매도"
        if not messagebox.askyesno(
            "테스트 주문 확인",
            f"{act} 1주 지정가(참고가 기준 ±1틱)를 보냅니다.\n\n"
            f"{name} ({code})\n"
            f"참고가: {ref_p:,}원\n"
            f"모드: {mode_kr}\n\n"
            f"사유: TEST_1SHARE_MARKET",
        ):
            return

        def _run() -> None:
            try:
                om = getattr(self.app, "order_manager", None)
                if om is None:
                    self.app.root.after(
                        0,
                        lambda: self.app.log("❌ [테스트주문] OrderManager 없음"),
                    )
                    return
                ord_no = om.place_order(
                    code,
                    1,
                    ref_p,
                    order_type="00",
                    side=side,
                    name=name,
                    reason="TEST_1SHARE_MARKET",
                )
                msg = (
                    f"🧪 [테스트주문] {act} 1주 시장가 응답: "
                    f"{ord_no if ord_no else '실패/거부 — 로그 확인'}"
                )
                self.app.root.after(0, lambda m=msg: self.app.log(m))
            except Exception as e:
                err = str(e)
                self.app.root.after(
                    0,
                    lambda: self.app.log(f"❌ [테스트주문] 예외: {err}"),
                )

        threading.Thread(target=_run, daemon=True).start()

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
                
                stream_codes = []
                try:
                    stream_codes = self.app.stream_subscription_codes()
                except Exception:
                    stream_codes = list(getattr(self.app.trading_controller, "monitor_codes", []) or [])
                if not stream_codes:
                    self.app.root.after(0, self.app.log, "⚠️ [시스템] 웹소켓 등록할 종목이 없습니다.")
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
                            # 보유·감시 종목 변경 시 REG 재전송 (4초 스로틀)
                            if getattr(self, "_pending_reg_refresh", False):
                                now_ts = time.time()
                                if now_ts - getattr(self, "_last_stream_reg_ts", 0) >= 4.0:
                                    self._pending_reg_refresh = False
                                    self._last_stream_reg_ts = now_ts
                                    try:
                                        _sc = self.app.stream_subscription_codes()
                                        if _sc:
                                            reg_packet = {
                                                "trnm": "REG",
                                                "grp_no": "1",
                                                "refresh": "1",
                                                "market": "AL",
                                                "data": [{"item": _sc, "type": ["0B", "0D"]}],
                                            }
                                            await ws.send(json.dumps(reg_packet))
                                            self.app.root.after(
                                                0,
                                                self.app.log,
                                                "📡 [시스템] 실시간 스트림 재등록 (보유·감시 반영)",
                                            )
                                    except Exception as re:
                                        self.app.root.after(
                                            0,
                                            self.app.log,
                                            f"⚠️ [시스템] 스트림 재등록 실패: {re}",
                                        )
                            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                            raw_data = json.loads(msg)
                            data_list = raw_data if isinstance(raw_data, list) else [raw_data]
                            
                            for packet in data_list:
                                trnm = packet.get('trnm')
                                if trnm == 'LOGIN':
                                    if packet.get('return_code') == 0:
                                        try:
                                            _sc = self.app.stream_subscription_codes()
                                        except Exception:
                                            _sc = list(
                                                getattr(
                                                    self.app.trading_controller,
                                                    "monitor_codes",
                                                    [],
                                                )
                                                or []
                                            )
                                        self.app.log(
                                            f"✅ [시스템] 로그인 성공. 종목 등록 ({len(_sc)}개)..."
                                        )
                                        reg_packet = {
                                            'trnm': 'REG', 'grp_no': '1', 'refresh': '1', 'market': 'AL',
                                            'data': [{'item': _sc, 'type': ['0B', '0D']}]
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
                "fg": "#ECEFF4", "desc": "🟢 레짐 UP: 백테 4단계 — Type A/B 가동",
            },
            "AGGRESSIVE": {
                "bg": "#2E3440", "field": "#263238",
                "odd": "#263238", "even": "#37474F",
                "fg": "#A3BE8C", "desc": "🔥 공격적 강세(AGGRESSIVE): MSI·ADX 강세 구간 — 백테와 동일 레짐",
            },
            "NEUTRAL": {
                "bg": "#3B4252", "field": "#3B4252",
                "odd": "#343B47", "even": "#434C5E",
                "fg": "#D8DEE9", "desc": "⚪ 시장 중립: Type B 매수 차단 (관망 권장)",
            },
            "DOWN": {
                "bg": "#3B2E2E", "field": "#3B2E2E",
                "odd": "#2E2424", "even": "#423B3B",
                "fg": "#D8DEE9", "desc": "⚠️ 시장 역풍: 모든 매수 신호 정지",
            },
        }
        tr_key = str(trend).strip().upper()
        t = themes.get(tr_key, themes["UP"])
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
        # 감시·보유 트리: 등락 태그의 짝/홀 배경을 장세 테마 odd/even에 맞춤
        row_font_th = ("Malgun Gothic", 9, "normal")
        up_c = "#E35F5F"
        dn_c = "#81A1C3"
        for tree in (self.tree_monitor, self.tree_active_monitor):
            if not tree:
                continue
            for direction, fg in (("up", up_c), ("down", dn_c), ("stable", "#D8DEE9")):
                tree.tag_configure(
                    f"{direction}_even",
                    background=t["even"],
                    foreground=fg,
                    font=row_font_th,
                )
                tree.tag_configure(
                    f"{direction}_odd",
                    background=t["odd"],
                    foreground=fg,
                    font=row_font_th,
                )
                tree.tag_configure(
                    f"active_{direction}_even",
                    background=t["even"],
                    foreground=fg,
                    font=row_font_th,
                )
                tree.tag_configure(
                    f"active_{direction}_odd",
                    background=t["odd"],
                    foreground=fg,
                    font=row_font_th,
                )
        if self.tree_monitor:
            self.tree_monitor.tag_configure("oddrow", background=t['odd'])
            self.tree_monitor.tag_configure("evenrow", background=t['even'])
        if hasattr(self, 'lbl_headwind_caution'):
            self.lbl_headwind_caution.config(text=t['desc'], background=t['bg'])
            if tr_key in ("UP", "AGGRESSIVE"):
                self.lbl_headwind_caution.pack_forget()
            else:
                self.lbl_headwind_caution.pack(side=tk.LEFT, padx=10)
        if self.app.status_label:
            self.app.status_label.config(text=f"장세: {trend}", foreground=t['fg'], background=t['bg'])

    def _watchlist_codes_set(self) -> set:
        """관심목록 코드(6자리). 감시목록과 독립이며, 이 집합은 감시 화면에서 숨김."""
        try:
            return self.app.watchlist_store.codes_set()
        except Exception:
            return set()

    def _monitor_ratio_direction(self, ratio_val: object) -> str:
        """등락률 표시값 → up / down / stable (줄무늬 행 태그용)."""
        try:
            s = str(ratio_val).replace("%", "").strip()
            if not s:
                return "stable"
            v = float(s.replace("+", ""))
            if v > 0:
                return "up"
            if v < 0:
                return "down"
        except (TypeError, ValueError):
            pass
        return "stable"

    def _monitor_info_entry(self, mi: Dict[str, Any], nk: str) -> Dict[str, Any]:
        """monitor_info 키가 003490 / 3490 혼재해도 6자리 nk로 조회."""
        try:
            if nk in mi:
                return dict(mi[nk]) if isinstance(mi[nk], dict) else {}
            for k, v in mi.items():
                if str(k).split("_")[0].zfill(6) == nk:
                    return dict(v) if isinstance(v, dict) else {}
        except Exception:
            pass
        return {}

    def _compose_monitor_row_values(self, nk: str, info: Dict[str, Any]) -> Tuple[Any, ...]:
        """감시·보유 트리 한 행 values (컬럼 순서는 _setup_ui 와 동일)."""
        try:
            now_p = float(
                info.get("price")
                if info.get("price") not in (None, "", 0)
                else (info.get("last_price") or 0)
            )
        except (TypeError, ValueError):
            now_p = 0.0
        strat = format_strategy_type_label(
            info.get("sel_track", info.get("SelTrack", "AB"))
        )
        ratio_disp = info.get("ratio", "0")
        nm = str(info.get("name", nk) or nk)
        try:
            sc = float(info.get("score", 0) or 0)
        except (TypeError, ValueError):
            sc = 0.0
        try:
            vol_pow = float(info.get("vol_power", 0) or 0)
        except (TypeError, ValueError):
            vol_pow = 0.0
        try:
            vol_i = int(float(info.get("volume", 0) or 0))
        except (TypeError, ValueError):
            vol_i = 0
        try:
            tv = float(info.get("trde_val", 0) or 0)
        except (TypeError, ValueError):
            tv = 0.0
        return (
            f"{sc:.1f}",
            nk,
            nm,
            strat,
            f"{now_p:,.0f}",
            _monitor_fmt_vs_prev(info),
            str(ratio_disp),
            info.get("status_text", "대기"),
            f"{vol_pow:.1f}%",
            _monitor_fmt_bid_ask(info),
            f"{vol_i:,}",
            f"{tv:,.1f}",
        )

    def maybe_reconcile_monitor_trees(
        self,
        monitor_info: Optional[Dict[str, Any]],
        virtual_positions: Any,
        min_interval_s: float = 3.0,
    ) -> None:
        """
        monitor_info 에 있어야 할 종목이 틱 부재로 트리에서 빠진 경우 복구.
        (15:30 봉 마감 등 체결이 없는 종목은 pending_tree_updates 에 안 올라와 행이 사라질 수 있음)
        """
        try:
            if not self.tree_monitor or not self.tree_active_monitor:
                return
            now = time.time()
            if now - self._last_monitor_tree_reconcile_ts < min_interval_s:
                return
            self._last_monitor_tree_reconcile_ts = now

            mi = monitor_info or getattr(self.app, "monitor_info", None) or {}
            wl = self._watchlist_codes_set()
            try:
                vp = (
                    virtual_positions
                    if virtual_positions is not None
                    else (getattr(self.app.portfolio, "virtual_positions", None) or {})
                )
            except Exception:
                vp = {}

            expected: set = set()
            for code in mi.keys():
                nk = str(code).split("_")[0].zfill(6)
                if nk in wl:
                    continue
                expected.add(nk)

            # 관심목록 전용 숨김: 양쪽 트리에서 제거
            for tree in (self.tree_monitor, self.tree_active_monitor):
                if not tree:
                    continue
                for iid in list(tree.get_children()):
                    z = str(iid).split("_")[0].zfill(6)
                    if z in wl and tree.exists(iid):
                        tree.delete(iid)

            # 누락 행 복구·보유/감시 트리 오배치 보정 (monitor_info 기준, 고아 강제 삭제는 하지 않음)
            for nk in sorted(expected):
                info = self._monitor_info_entry(mi, nk)
                if not info:
                    info = {"name": nk, "sel_track": "AB"}
                values = self._compose_monitor_row_values(nk, info)
                is_active = self._virtual_position_is_active(nk, vp)
                target = self.tree_active_monitor if is_active else self.tree_monitor
                other = self.tree_monitor if is_active else self.tree_active_monitor
                if other.exists(nk):
                    try:
                        other.delete(nk)
                    except Exception:
                        pass
                if target.exists(nk):
                    continue
                direction = self._monitor_ratio_direction(info.get("ratio", "0"))
                try:
                    idx = len(target.get_children())
                except Exception:
                    idx = 0
                row_type = "even" if (idx % 2 == 0) else "odd"
                tag = (
                    f"active_{direction}_{row_type}"
                    if is_active
                    else f"{direction}_{row_type}"
                )
                try:
                    target.insert("", tk.END, iid=nk, values=values, tags=(tag,))
                except Exception:
                    pass
        except Exception as e:
            try:
                print(f"[UI Error] maybe_reconcile_monitor_trees: {e}")
            except Exception:
                pass

    def _virtual_position_is_active(self, iid: str, virtual_positions: Any) -> bool:
        """
        트리 iid 와 virtual_positions 키가 3490 vs 003490 처럼 달라도 동일 종목으로 인식.
        (앞자리 0 종목만 보유/감시 트리 간 이동이 틱마다 바뀌는 현상 방지)
        """
        try:
            vp = virtual_positions or {}
            if not vp:
                return False
            nk = str(iid).split("_")[0].zfill(6)
            if nk in vp:
                return True
            for k in vp:
                if str(k).split("_")[0].zfill(6) == nk:
                    return True
            return False
        except Exception:
            return False

    def update_view(self, updates, virtual_positions):
        """[v4.5.3] App으로부터 전달받은 실시간 틱 데이터를 화면에 렌더링 (캡슐화)"""
        if not self.tree_monitor: return
        
        try:
            if updates:
                items = list(updates.items())
                updates.clear()
                wl = self._watchlist_codes_set()
                
                for iid_raw, (values, tag, _) in items:
                    # pending 키·monitor_info 키가 3490/003490로 섞여도 트리 iid는 항상 6자리로 통일
                    iid = str(iid_raw).split("_")[0].zfill(6)
                    cid = iid
                    if cid in wl:
                        for tree in (self.tree_monitor, self.tree_active_monitor):
                            if tree and tree.exists(iid):
                                tree.delete(iid)
                        try:
                            self._vp_ui_inactive_streak.pop(iid, None)
                        except Exception:
                            pass
                        continue
                    # 보유 여부에 따라 타겟 트리 결정
                    is_active_raw = self._virtual_position_is_active(
                        iid, virtual_positions
                    )
                    # 동기화·API 지연으로 VP에서 한 번만 빠지면 보유 트리↔감시 트리가 매 틱 번갈아 깜빡임.
                    # "보유 아님"이 연속 2회 이상일 때만 비보유(감시) 쪽 배치로 확정한다.
                    try:
                        if is_active_raw:
                            self._vp_ui_inactive_streak[iid] = 0
                        else:
                            self._vp_ui_inactive_streak[iid] = int(
                                self._vp_ui_inactive_streak.get(iid, 0)
                            ) + 1
                        is_active = is_active_raw or (
                            self._vp_ui_inactive_streak.get(iid, 0) < 2
                        )
                    except Exception:
                        is_active = is_active_raw
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
                        final_tag = f"active_{direction}_{row_type}"
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
        try:
            self._vp_ui_inactive_streak.clear()
        except Exception:
            self._vp_ui_inactive_streak = {}
        self.tree_monitor.delete(*self.tree_monitor.get_children())
        self.tree_active_monitor.delete(*self.tree_active_monitor.get_children())
        
        # [v4.5.7] 가점(Score) 기준 내림차순 정렬하여 초기 삽입
        sorted_items = sorted(monitor_info.items(), key=lambda x: x[1].get('score', 0), reverse=True)
        wl = self._watchlist_codes_set()
        try:
            vp = getattr(self.app.portfolio, "virtual_positions", None) or {}
        except Exception:
            vp = {}
        
        idx_active = 0
        idx_watch = 0
        for code, info in sorted_items:
            nk = str(code).split("_")[0].zfill(6)
            if nk in wl:
                continue
            ratio_disp = info.get("ratio", "0")
            direction = self._monitor_ratio_direction(ratio_disp)
            values = self._compose_monitor_row_values(nk, info)
            # 보유 중이면 아래(보유 트리)에만 넣음. 예전에는 위 트리만 채워 다음 틱까지 보유 트리가 비어 깜빡임 발생.
            if self._virtual_position_is_active(nk, vp):
                row_type = "even" if (idx_active % 2 == 0) else "odd"
                idx_active += 1
                tag = f"active_{direction}_{row_type}"
                self.tree_active_monitor.insert("", tk.END, iid=nk, values=values, tags=(tag,))
            else:
                row_type = "even" if (idx_watch % 2 == 0) else "odd"
                idx_watch += 1
                tag = f"{direction}_{row_type}"
                self.tree_monitor.insert("", tk.END, iid=nk, values=values, tags=(tag,))

    def load_targets(self, codes_or_info):
        """KiwoomApp과의 호환성을 위한 래퍼 (DataFrame 전달 시 app.monitor_info 사용)."""
        try:
            if isinstance(codes_or_info, dict):
                self.update_watchlist(codes_or_info)
            elif hasattr(self.app, "monitor_info") and self.app.monitor_info:
                self.update_watchlist(self.app.monitor_info)
        except Exception as e:
            print(f"[UI Error] load_targets: {e}")

    def reset_warmup_row_states(self, codes: list) -> None:
        """감시 시작 워밍업 직전: 지정 종목 행을 '대기' 표시로 통일."""
        try:
            wl = self._watchlist_codes_set()
            for code in codes:
                if str(code).zfill(6) in wl:
                    continue
                for tree in (self.tree_monitor, self.tree_active_monitor):
                    if tree and tree.exists(code):
                        vals = list(tree.item(code, "values"))
                        if len(vals) >= 7:
                            vals[6] = "⏳ 워밍업 대기…"
                        tree.item(code, values=tuple(vals), tags=("warmup_pending",))
        except Exception as e:
            print(f"[UI Error] reset_warmup_row_states: {e}")

    def update_warmup_stock_prep(
        self,
        event: str,
        cur: int,
        total: int,
        name: str,
        code: str,
        reason: str = "",
    ) -> None:
        """종목별 워밍업 진행·성공·실패를 신호상태 열·행 배경으로 구분."""
        try:
            code = str(code).zfill(6)
            if code in self._watchlist_codes_set():
                return
            if event == "prep_start":
                st = f"⏳ 데이터 준비 중… ({cur}/{total})"
                tag = "warmup_pending"
            elif event == "prep_ok":
                st = "✅ 15분봉·검증 완료"
                tag = "warmup_ok_row"
            else:
                short = (reason or "실패")[:120]
                st = f"❌ 워밍업 실패: {short}"
                tag = "warmup_fail_row"
            for tree in (self.tree_monitor, self.tree_active_monitor):
                if not tree or not tree.exists(code):
                    continue
                vals = list(tree.item(code, "values"))
                if len(vals) >= 7:
                    vals[6] = st
                tree.item(code, values=tuple(vals), tags=(tag,))
        except Exception as e:
            print(f"[UI Error] update_warmup_stock_prep: {e}")
