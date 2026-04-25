import pandas as pd
import numpy as np
import threading
from datetime import datetime
import traceback
from backtest_engine import TradingStrategy
from core.defaults import effective_min_15m_bars_for_monitoring
from ui.tabs.monitor_tab import format_entry_signal_type_label
from utils.krx_session import is_krx_regular_session
from typing import Any, Optional, Tuple

class SignalEngine:
    """[v25.7] 실시간 체결 데이터 분석 및 시그널 포착 엔진 클래스"""
    def __init__(self, app):
        self.app = app
        self._last_realtime_err = None
        self.last_buy_sig_time = {} # [v1.0] 중복 시그널 방지용 쿨타임 관리
        self._wl_buy_last_tm = {}
        self._wl_sell_last_tm = {}

    def _to_scalar(self, val, default=0):
        """[v4.5.9] 헬퍼: Series/DF/Numpy 등 어떤 형태에서든 안전하게 단일 스칼라 값 추출"""
        if val is None: return default
        # 1. Pandas 객체 처리 (Series, DataFrame)
        if hasattr(val, 'iloc'):
            if hasattr(val, 'size') and val.size == 0: return default
            return self._to_scalar(val.iloc[0], default)
        # 2. Numpy 스칼라 처리
        if hasattr(val, 'item') and not hasattr(val, 'shape'): # 진짜 스칼라인 경우만
            return val.item()
        # 3. 리스트/어레이 처리
        if isinstance(val, (list, np.ndarray)) and len(val) > 0:
            return self._to_scalar(val[0], default)
        return val

    def _format_s1_confluence_status_line(
        self, last_sig: Any, sel_track: str = "AB"
    ) -> str:
        """
        S1 합치도 표시: 눌림목(A)과 추세돌파(B) 조건 충족 수를 보여준다.
        선정 트랙이 A/B 단일이면 해당 트랙 지표만 표시(교차 트랙 문구 생략).
        """
        try:
            tr_raw = str(sel_track or "AB").strip().upper()
            tr = tr_raw if tr_raw in ("A", "B", "AB") else "AB"
            ma_c = int(self._to_scalar(last_sig.get("Met_A", 0), 0))
            ta_t = max(1, int(self._to_scalar(last_sig.get("Tot_A", 1), 1)))
            mb_c = int(self._to_scalar(last_sig.get("Met_B", 0), 0))
            tb_t = max(1, int(self._to_scalar(last_sig.get("Tot_B", 1), 1)))
            if tr == "A":
                return f"눌림A {ma_c}/{ta_t}"
            if tr == "B":
                return f"추세B {mb_c}/{tb_t}"
            st = str(self._to_scalar(last_sig.get("Sig_Type", "NONE"), "NONE"))
            if st == "TYPE_B":
                return f"추세B {mb_c}/{tb_t} (눌림A {ma_c}/{ta_t})"
            if st == "TYPE_A":
                return f"눌림A {ma_c}/{ta_t} (추세B {mb_c}/{tb_t})"
            return f"눌림A {ma_c}/{ta_t} · 추세B {mb_c}/{tb_t}"
        except Exception:
            try:
                mc = int(self._to_scalar(last_sig["Met_Count"], 0))
                tc = max(1, int(self._to_scalar(last_sig["Total_Count"], 1)))
                return f"({mc}/{tc})"
            except Exception:
                return "—"

    def _format_monitor_vs_prev_cell(self, info: dict) -> str:
        """감시 트리 '대비' 열: 전일 종가 대비 등락폭(원). 웹소켓에서 info['diff'] 유지."""
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

    def _has_open_position(self, code: str) -> bool:
        """virtual_positions 에 보유 중인지(6자리·접미사 키 호환)."""
        try:
            vp = getattr(self.app, "virtual_positions", None) or {}
            if not vp:
                return False
            if code in vp:
                return True
            nk = str(code).zfill(6)
            if nk in vp:
                return True
            for k in vp:
                try:
                    if str(k).split("_")[0].zfill(6) == nk:
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _indicator_last_row_for_exit(self, code: str) -> Optional[dict]:
        """
        S1 신호용 min_15m_bars 미만이어도 청산·S2 판단에 쓸 마지막 지표 행.
        (기존에는 bars_ready 가 False 면 evaluate_position 자체를 호출하지 않아 ATR 등 청산이 멈춤)
        """
        try:
            _bd = (self.app.bars_data or {}).get(code)
            if _bd is None or getattr(_bd, "empty", True):
                return None
            if len(_bd) < 2:
                return None
            df_with_ind = TradingStrategy.calculate_indicators(_bd, self.app.settings)
            return df_with_ind.iloc[-1].to_dict()
        except Exception:
            return None

    def _parse_ws_vs_prev_close(self, info: dict) -> Tuple[Optional[float], Optional[float]]:
        """
        실시간 0B 패킷 fid 11·12: 전일 종가 대비 (원), 등락률 (% 수치, 부호 포함).
        파싱 실패·미수신 시 (None, None).
        """
        try:
            d_raw = info.get("diff")
            r_raw = info.get("ratio")
            d_f: Optional[float] = None
            r_f: Optional[float] = None
            if d_raw is not None:
                ds = str(d_raw).strip().replace(",", "")
                if ds not in ("", "-", "—"):
                    d_f = float(ds)
            if r_raw is not None:
                rs = (
                    str(r_raw)
                    .strip()
                    .replace("%", "")
                    .replace(",", "")
                )
                if rs not in ("", "-", "—"):
                    r_f = float(rs)
            return (d_f, r_f)
        except (TypeError, ValueError):
            return (None, None)

    def _monitor_tree_ratio_and_direction(
        self,
        info: dict,
        now_price: float,
        intraday_diff: float,
        open_price: float,
    ) -> Tuple[str, float]:
        """
        등락율 열 문자열과 행 방향(양수/음수/0).
        전일 종가 기준 fid 12·11을 우선하고, 없을 때만 시가 대비로 폴백한다.
        (시가 대비 %는 전일대비 '대비' 열과 불일치하는 원인이 됨)
        """
        try:
            d_ws, r_ws = self._parse_ws_vs_prev_close(info)
            # 전일대비(원)을 먼저 쓰면 '대비' 열( fid 11 )과 등락율이 항상 같은 기준이다.
            if d_ws is not None:
                if abs(d_ws) < 1e-12:
                    return ("0.00%", 0.0)
                prev_c = float(now_price) - float(d_ws)
                if prev_c > 1e-9:
                    pct = (float(d_ws) / prev_c) * 100.0
                    sg = "▲" if d_ws > 0 else "▼"
                    return (f"{sg} {abs(pct):.2f}%", float(d_ws))
                return ("0.00%", 0.0)
            if r_ws is not None:
                if abs(r_ws) < 1e-12:
                    return ("0.00%", 0.0)
                sg = "▲" if r_ws > 0 else "▼"
                return (f"{sg} {abs(r_ws):.2f}%", float(r_ws))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        try:
            op = float(open_price)
            if op > 1e-12:
                pct = (float(intraday_diff) / op) * 100.0
                if abs(pct) < 1e-12:
                    return ("0.00%", 0.0)
                sg = "▲" if intraday_diff > 0 else "▼"
                return (f"{sg} {abs(pct):.2f}%", float(pct))
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        return ("0.00%", 0.0)

    def _format_monitor_bid_ask_cell(self, info: dict) -> str:
        """감시 트리 Bid/Ask 열: 매도잔량 / 매수잔량 및 매도÷매수 비율(소수 1자리)."""
        try:
            from core.supply_display import format_bid_ask_monitor_display

            return format_bid_ask_monitor_display(
                self._to_scalar(info.get("total_ask", 0), 0),
                self._to_scalar(info.get("total_bid", 0), 0),
            )
        except Exception:
            return "— / — (—)"

    def _code_in_virtual_positions(self, code: str) -> bool:
        """가상·실제 보유(virtual_positions)에 해당 종목이 있는지 6자리 기준으로 확인합니다."""
        try:
            nk = str(code).split("_")[0].zfill(6)
            vp = getattr(self.app.portfolio, "virtual_positions", None) or {}
            if nk in vp or code in vp:
                return True
            for k in vp:
                if str(k).zfill(6) == nk:
                    return True
        except Exception:
            pass
        return False

    def on_tick_received(self, code, now_price, volume, _trde_val, **kwargs):
        """실시간 체결 데이터(Tick) 처리 본체. `_trde_val` 은 호출 규격 유지용(감시 트리 대금 열 제거)."""
        try:
            if now_price <= 0: return # [v2.2] 유효하지 않은 가격 데이터 로직 진입 차단
            code = str(code).split("_")[0].zfill(6)
            if self._code_in_virtual_positions(code) and code not in self.app.monitor_info:
                try:
                    self.app._ensure_vp_monitor_stub(code)
                except Exception:
                    pass
            if code not in self.app.monitor_info: return
            info = self.app.monitor_info[code]
            # 지정가 주문 시세 검증용: 틱 단가로 캐시를 매 틱 동기화 (화면·주문 기준 불일치 방지)
            try:
                info["last_price"] = float(now_price)
                info["price"] = float(now_price)
            except (TypeError, ValueError):
                pass
            try:
                if info.get("vp_price_stub"):
                    info.pop("vp_price_stub", None)
            except Exception:
                pass
            try:
                wl_all_set = self.app.watchlist_store.codes_set()
            except Exception:
                wl_all_set = set()
            is_in_watchlist = code in wl_all_set
            wl_only_set = self._watchlist_only_codes()
            is_wl_only = code in wl_only_set
            try:
                _wl_ap = self.app.watchlist_store.get_avg_price(code)
            except Exception:
                _wl_ap = None
            has_wl_avg = _wl_ap is not None and float(_wl_ap) > 0

            # Update additional fields from kwargs (vol_power, total_ask, total_bid 등)
            for k, v in kwargs.items():
                info[k] = v
            
            # 1. 지표 재계산 및 봉 데이터 업데이트
            self._update_realtime_bar(code, now_price, volume)
            
            # 2. 실시간 지표 추출 (RSI, MACD 등)
            rsi_val = self._calculate_realtime_rsi(code)
            
            # 3. 데이터 추출
            open_price = info.get('open', 0)
            if open_price == 0:
                open_price = now_price
                info['open'] = open_price
            # 시가 대비(내부용). 감시 트리 '등락율'은 전일종가 fid 12 우선(_monitor_tree_ratio_and_direction).
            intraday_diff = float(now_price) - float(open_price)
            
            # 4. 시그널 상태 제어
            tag = ""
            ma60_ratio = 0.0
            info['confluence'] = info.get('confluence', '-')
            
            # RSI 50 돌파 신호 로직
            s_state = self.app.rsi_signals.get(code, {'last_rsi': 0.0, 'signal_time': None})
            last_rsi_f = float(s_state.get('last_rsi', 0.0))
            now_dt = datetime.now()
            try:
                _session_ok = is_krx_regular_session(now_dt)
            except Exception:
                _session_ok = True

            if last_rsi_f > 0 and last_rsi_f < 50.0 and rsi_val >= 50.0:
                s_state['signal_time'] = now_dt
            
            if s_state['signal_time']:
                if rsi_val < 50.0:
                    s_state['signal_time'] = None
                elif (now_dt - s_state['signal_time']).total_seconds() > self.app.settings.get('rsi_window', 45) * 60:
                    s_state['signal_time'] = None
                else:
                    tag = "rsi_signal"
            
            s_state['last_rsi'] = float(rsi_val)
            self.app.rsi_signals[code] = s_state

            # [Phase 5] 수급 분석 (Orderbook Analysis)
            total_ask = info.get('total_ask', 0)
            total_bid = info.get('total_bid', 1) # Prevent div by zero
            ask_bid_ratio = total_ask / total_bid if total_bid > 0 else 0
            bid_ask_ratio = total_bid / total_ask if total_ask > 0 else 0
            # 0D TR은 total_* 만 넣고 ask_bid_ratio 키는 안 넣음. Smart Buy가 info.get(..., 1.0)을 쓰면
            # BA 임계(예: 1.5)보다 낮은 가짜 1.0으로 매수가 전부 막히는 오류가 난다 → 계산값을 info에 반영.
            try:
                info["ask_bid_ratio"] = float(ask_bid_ratio)
                info["bid_ask_ratio"] = float(bid_ask_ratio)
            except (TypeError, ValueError):
                try:
                    info["ask_bid_ratio"] = 0.0
                    info["bid_ask_ratio"] = 0.0
                except Exception:
                    pass

            # 체결강도 하회 지속 관리
            vol_power = info.get('vol_power', 100.0)
            is_power_exhausted = False
            if vol_power < self.app.settings.get('smart_exit_vp', 100.0):
                if info.get('power_below_start') is None:
                    info['power_below_start'] = now_dt
                else:
                    duration = (now_dt - info['power_below_start']).total_seconds() / 60.0
                    if duration >= 2: # Keep 2 mins as standard for now or use a new key
                        is_power_exhausted = True
            else:
                info['power_below_start'] = None

            # [v35.7] 자가 학습형 합성 모델 보정 (Adaptive Calibration)
            
            # 5. 지표 및 시그널 계산 주기 관리 (100틱마다 또는 분봉 변경 시)
            info['tick_count'] = info.get('tick_count', 0) + 1
            last_bar_count = info.get('last_bar_count', 0)
            try:
                _min_sig_bars = effective_min_15m_bars_for_monitoring(
                    self.app.settings
                )
            except Exception:
                _min_sig_bars = 60
            _bd = (self.app.bars_data or {}).get(code)
            try:
                current_bar_count = len(_bd) if _bd is not None and not getattr(_bd, "empty", True) else 0
            except Exception:
                current_bar_count = 0
            # 15분봉(마지막 미완성 1봉 포함) 행 수가 기준 미만이면 S1 신호·청산·S2 전부 보류
            bars_ready = (
                _bd is not None
                and not getattr(_bd, "empty", True)
                and current_bar_count >= _min_sig_bars
            )

            should_recalc = (current_bar_count > last_bar_count) or (info['tick_count'] % 100 == 1)

            is_buy = False
            last_row = None
            if bars_ready:
                if should_recalc or 'last_detailed_sig' not in info:
                    df_with_ind = TradingStrategy.calculate_indicators(self.app.bars_data[code], self.app.settings)
                    try:
                        info['last_detailed_sig'] = TradingStrategy.get_buy_signals_detailed(
                            df_with_ind, self.app.settings, "UP"
                        )
                        info['last_bar_count'] = current_bar_count
                        info['last_row_cache'] = df_with_ind.iloc[-1].to_dict()
                    except Exception as exc_sig:
                        # 시그널/지표 계산 실패 시 캐시가 없으면 아래 분기에서 안내 문구 표시
                        if self._last_realtime_err != str(exc_sig):
                            self._last_realtime_err = str(exc_sig)
                            print(
                                f"[SignalEngine] get_buy_signals_detailed 실패 ({code}): {exc_sig}"
                            )
                            traceback.print_exc()
                
                if "last_detailed_sig" not in info:
                    info["confluence"] = "시그널 계산 실패(봉·지표 확인)"
                    info["met_ratio"] = 0.0
                    is_buy = False
                    last_row = None
                else:
                    detailed_signals = info['last_detailed_sig']
                    last_sig = detailed_signals.iloc[-1]
                
                    # [v4.5.9] 전방위적 스칼라 쉴드 적용
                    is_buy = bool(self._to_scalar(last_sig['Signal'], False))
                    _sel_tr = str(
                        info.get("sel_track", info.get("SelTrack", "AB")) or "AB"
                    ).strip().upper()
                    info['confluence'] = self._format_s1_confluence_status_line(
                        last_sig, _sel_tr
                    )
                    info['met_ratio'] = float(self._to_scalar(last_sig['Met_Ratio'], 0.0))
                
                    last_row = info['last_row_cache']
                    rsi_sig = float(self._to_scalar(last_row.get('RSI_Sig', 0), 0.0))
                    macd_val = float(self._to_scalar(last_row.get('MACD', 0), 0.0))
                    osc_val = float(self._to_scalar(last_row.get('OSC', 0), 0.0))
                
                    ma60_scalar = float(self._to_scalar(last_row['MA60'], 0.0))
                    ma60_ratio = (now_price / ma60_scalar) * 100 if ma60_scalar > 0 else 0.0
                
                    if is_buy:
                        sig_type = str(
                            self._to_scalar(last_sig.get("Sig_Type", "TYPE_B"), "TYPE_B")
                        )
                        # Smart Buy 스위치는 감시 '전략유형'(sel_track)과 맞춤. sel_track=A 인데 Sig_Type만 B면
                        # Type B SB가 적용되어 '수급 대기'가 뜨는 불일치를 방지함.
                        if _sel_tr == "A":
                            use_sb_for_type = bool(
                                self.app.settings.get("use_smart_buy_a", False)
                            )
                        elif _sel_tr == "B":
                            use_sb_for_type = bool(
                                self.app.settings.get("use_smart_buy_b", True)
                            )
                        else:
                            st_up = str(sig_type).upper()
                            if "TYPE_B" in st_up:
                                use_sb_for_type = bool(
                                    self.app.settings.get("use_smart_buy_b", True)
                                )
                            elif "TYPE_A" in st_up:
                                use_sb_for_type = bool(
                                    self.app.settings.get("use_smart_buy_a", False)
                                )
                            else:
                                use_sb_for_type = False
                    
                        if use_sb_for_type:
                            vol_power = float(self._to_scalar(info.get('vol_power', 100.0), 100.0))
                            ask_bid_ratio = float(self._to_scalar(info.get('ask_bid_ratio', 1.0), 1.0))
                            min_p = float(self.app.settings.get('smart_buy_vp', 100.0))
                            # S2·백테스트와 동일 키. 구버전에 smart_buy_ratio만 있으면 폴백
                            _st = self.app.settings
                            min_r = float(
                                _st.get("smart_buy_ba_ratio", _st.get("smart_buy_ratio", 1.5))
                            )
                        
                            if vol_power < min_p or ask_bid_ratio < min_r:
                                is_buy = False
                                info['confluence'] = "수급 대기"
                                # 종합 리포트에서 스마트 진입 거절 건수 집계용 — 동일 15분봉당 1회만 기록
                                try:
                                    _sb_log_key = "_smart_buy_block_logged_bar"
                                    if info.get(_sb_log_key) != current_bar_count:
                                        info[_sb_log_key] = current_bar_count
                                        _sl = getattr(self.app, "session_logger", None)
                                        if _sl:
                                            _sl.log(
                                                "smart_buy_blocked",
                                                {
                                                    "code": str(code).zfill(6),
                                                    "name": str(info.get("name", "")),
                                                    "vol_power": vol_power,
                                                    "ask_bid_ratio": ask_bid_ratio,
                                                    "min_vp": min_p,
                                                    "min_ba": min_r,
                                                    "sig_type": str(
                                                        self._to_scalar(
                                                            last_sig.get("Sig_Type", ""),
                                                            "",
                                                        )
                                                    ),
                                                },
                                            )
                                except Exception:
                                    pass

                    # 관심목록 전용: Type B 매수 신호만 (감시와 동일 필터 후 B만)
                    if is_wl_only and is_buy:
                        _st_wl = str(self._to_scalar(last_sig.get("Sig_Type", "TYPE_B"), "TYPE_B"))
                        if "TYPE_B" not in _st_wl.upper():
                            is_buy = False

                    # 정규장 외(장전·장후): 매수·알림 전에 차단 (합치도 문구만 통일)
                    if not _session_ok:
                        info["confluence"] = "⏳ 정규장 외(실시간 체결 미진행) · 봉 준비"
                        info["met_ratio"] = 0.0
                        is_buy = False

                    # 쿨타임 및 중복 진입 방지
                    is_cool = True
                    if code in self.last_buy_sig_time:
                        if (datetime.now() - self.last_buy_sig_time[code]).total_seconds() < 180:
                            is_cool = False
                
                    if is_buy and is_cool and (not is_wl_only) and code not in self.app.virtual_positions:
                        try:
                            sl = getattr(self.app, "session_logger", None)
                            if sl:
                                sl.log(
                                    "signal_s1_raw",
                                    {
                                        "code": code,
                                        "name": str(info.get("name", "")),
                                        "price": float(now_price),
                                        "met_ratio": float(
                                            self._to_scalar(info.get("met_ratio", 0.0), 0.0)
                                        ),
                                        "confluence": str(info.get("confluence", "")),
                                    },
                                )
                        except Exception:
                            pass
                        try:
                            buy_ok = bool(
                                self.app._on_buy_signal_received(
                                    code,
                                    now_price,
                                    rsi_sig,
                                    macd_val,
                                    osc_val,
                                    ma60_ratio,
                                )
                            )
                        except Exception:
                            buy_ok = False
                        if buy_ok:
                            self.last_buy_sig_time[code] = datetime.now()

                    # 관심목록(감시 제외): 평단 미등록 시에만 매수 신호 텔레그램 (등록 시 매도만 감시)
                    if is_wl_only and is_buy and is_cool and not has_wl_avg:
                        _t_wb = self._wl_buy_last_tm.get(code)
                        if _t_wb is None or (datetime.now() - _t_wb).total_seconds() >= 180:
                            self._wl_buy_last_tm[code] = datetime.now()
                            try:
                                _nm = self.app.watchlist_store.get_display_name(code)
                            except Exception:
                                _nm = str(info.get("name", code))
                            _mr = float(self._to_scalar(info.get("met_ratio", 0.0), 0.0))
                            _msg = (
                                "<b>⭐ [텔레그램·관심목록 전용]</b>\n"
                                "<i>감시 자동매매/주문과 별도 채널 (알림만)</i>\n\n"
                                "<b>TYPE B 매수 신호</b>\n"
                                f"종목: {_nm} ({code})\n"
                                f"가: {int(now_price):,}원\n"
                                f"합치도: {_mr * 100:.0f}%\n"
                                f"시간: {datetime.now().strftime('%H:%M:%S')}"
                            )
                            threading.Thread(
                                target=self._send_watchlist_telegram,
                                args=(_msg,),
                                daemon=True,
                            ).start()
            else:
                info['confluence'] = f"봉 충전 {current_bar_count}/{_min_sig_bars} (신호·매매 대기)"
                info['met_ratio'] = 0.0

            # 청산·S2 표시 공통: 마지막 지표 행 (봉 부족 시 보유면 보조 계산)
            eval_ind: Optional[dict] = None
            if bars_ready:
                eval_ind = last_row
            elif self._has_open_position(code):
                eval_ind = self._indicator_last_row_for_exit(code)

            # 보유 중이면 신호상태는 S1 합치도 대신 S2 추매 합치도
            if self._code_in_virtual_positions(code) and eval_ind is not None:
                try:
                    vp = getattr(self.app.portfolio, "virtual_positions", None) or {}
                    pos_h = vp.get(code) or vp.get(str(code).zfill(6))
                    if pos_h is None:
                        nk = str(code).zfill(6)
                        for k, v in vp.items():
                            if str(k).split("_")[0].zfill(6) == nk:
                                pos_h = v
                                break
                    if isinstance(pos_h, dict):
                        stg = int(pos_h.get("entry_n", pos_h.get("stage", 1)) or 1)
                        if stg >= 2:
                            info["confluence"] = "추매(S2) 완료"
                            info["met_ratio"] = 1.0
                        else:
                            s2_txt, s2_mr = (
                                TradingStrategy.compute_s2_confluence_display_text(
                                    pos_h,
                                    self.app.settings,
                                    eval_ind,
                                    market_trend="UP",
                                    curr_dt=datetime.now(),
                                    sel_track_hint=info.get("sel_track")
                                    or info.get("SelTrack"),
                                    now_price=float(now_price),
                                )
                            )
                            info["confluence"] = s2_txt
                            info["met_ratio"] = float(s2_mr)
                except Exception:
                    pass

            # 7. 실시간 보유 포지션: 청산·S2 (지표 행 확보 시; 봉 수 부족 시에도 보유면 보조 행 사용)
            bid_ask_ratio = float(self._to_scalar(info.get('bid_ask_ratio', 1.0), 1.0))
            ask_bid_ratio = float(self._to_scalar(info.get('ask_bid_ratio', 1.0), 1.0))
            vol_power = float(self._to_scalar(info.get('vol_power', 100.0), 100.0))

            if eval_ind is not None and not is_wl_only and _session_ok:
                res = self.app.strategy_engine.evaluate_position(
                    code,
                    now_price,
                    ind=eval_ind,
                    is_power_exhausted=is_power_exhausted,
                    bid_ask_ratio=bid_ask_ratio,
                    ask_bid_ratio=ask_bid_ratio,
                    vol_power=vol_power,
                    sel_track=info.get("sel_track") or info.get("SelTrack"),
                )
                if res["action"] != "HOLD":
                    self.app._on_strategy_action_needed(
                        code,
                        res["action"],
                        res["reason"],
                        now_price=now_price,
                        profit_ratio=res.get("profit_ratio", 0),
                    )

            # 관심목록 전용: 평단(기준가) 등록 시에만 S2-B 매도 신호 텔레그램 (미등록 시 매수만)
            if (
                bars_ready
                and is_wl_only
                and last_row is not None
                and _session_ok
                and has_wl_avg
            ):
                self._tick_watchlist_synthetic_s2_exit(
                    code,
                    now_price,
                    last_row,
                    info,
                    bid_ask_ratio=bid_ask_ratio,
                    ask_bid_ratio=ask_bid_ratio,
                    vol_power=vol_power,
                    is_power_exhausted=is_power_exhausted,
                    ref_avg=float(_wl_ap),
                )

            # 8. 트리뷰 업데이트 큐잉 (관심목록에 등록된 종목은 감시 화면에 표시하지 않음, 감시목록과 별개)
            if not is_in_watchlist:
                try:
                    np_f = float(now_price)
                except (TypeError, ValueError):
                    np_f = 0.0
                try:
                    op_f = float(open_price)
                except (TypeError, ValueError):
                    op_f = 0.0
                ratio_cell, dir_rn = self._monitor_tree_ratio_and_direction(
                    info, np_f, intraday_diff, op_f
                )
                p_tag = (
                    "price_up"
                    if dir_rn > 0
                    else ("price_down" if dir_rn < 0 else "price_stable")
                )

                strat_label = "—"
                atr_cell = "—"
                pos = None
                try:
                    # ATR 기준가 열: 가상·실제 보유 종목만 (감시만 하는 종목은 '—')
                    nk = str(code).zfill(6)
                    vp = self.app.virtual_positions
                    pos = vp.get(nk)
                    if pos is None:
                        for k, v in vp.items():
                            if str(k).split("_")[0].zfill(6) == nk:
                                pos = v
                                break
                    strat_label = format_entry_signal_type_label(
                        pos if isinstance(pos, dict) else None,
                        info.get("sel_track", info.get("SelTrack", "AB")),
                    )
                except Exception:
                    strat_label = format_entry_signal_type_label(
                        None,
                        info.get("sel_track", info.get("SelTrack", "AB")),
                    )
                _vp_str = f"{float(self._to_scalar(info.get('vol_power', 0.0), 0.0)):.1f}%"
                if not _session_ok:
                    _vp_str = "—"
                try:
                    info["status_text"] = str(info.get("confluence", "대기"))
                except Exception:
                    pass
                try:
                    if pos is not None and np_f > 0:
                        # 신호·매매용 last_row 는 bars_ready 일 때만 채워짐.
                        # 표시는 봉 수와 무관하게 마지막 봉+지표로 ATR 열을 채운다.
                        _bd_atr = (self.app.bars_data or {}).get(code)
                        row_for_atr: Optional[dict] = None
                        if last_row is not None:
                            if isinstance(last_row, dict):
                                row_for_atr = last_row
                            elif hasattr(last_row, "to_dict"):
                                row_for_atr = last_row.to_dict()
                            else:
                                row_for_atr = dict(last_row)
                        else:
                            if (
                                _bd_atr is not None
                                and not getattr(_bd_atr, "empty", True)
                            ):
                                df_i = TradingStrategy.calculate_indicators(
                                    _bd_atr, self.app.settings
                                )
                                row_for_atr = df_i.iloc[-1].to_dict()
                        if row_for_atr is not None:
                            atr_cell = TradingStrategy.compute_atr_exit_display_text(
                                pos,
                                self.app.settings,
                                row_for_atr,
                                float(np_f),
                                bars_df=_bd_atr,
                                sel_track_hint=info.get("sel_track")
                                or info.get("SelTrack"),
                            )
                except Exception:
                    atr_cell = "—"
                # 미보유일 때만 S1 매수 신호 → 타점 문구 (보유 시 위에서 S2 합치도 유지)
                try:
                    if is_buy and not self._code_in_virtual_positions(code):
                        info["confluence"] = "🔥 타점 포착"
                        info["status_text"] = "🔥 타점 포착"
                except Exception:
                    pass
                full_values = (
                    f"{float(self._to_scalar(info.get('score', 0.0), 0.0)):.1f}",
                    code,
                    info["name"],
                    strat_label,
                    f"{int(now_price):,}",
                    atr_cell,
                    self._format_monitor_vs_prev_cell(info),
                    ratio_cell,
                    str(info.get("confluence", "-")),
                    _vp_str,
                    self._format_monitor_bid_ask_cell(info),
                    f"{int(volume):,}",
                )
                
                final_tags = []
                
                # 합치도 기반 색상 태그
                m_ratio = float(self._to_scalar(info.get('met_ratio', 0.0), 0.0))
                if is_buy and not self._code_in_virtual_positions(code):
                    final_tags.append("status_high")
                elif m_ratio >= 0.7:
                    final_tags.append("status_high")
                else:
                    final_tags.append("status_low")

                # 가격 태그를 가장 뒤에 추가하여 최우선 적용
                final_tags.append(p_tag)
                    
                # [v27.9] is_blink 플래그를 항상 False로 전달
                self.app.pending_tree_updates[code] = (full_values, tuple(final_tags), False)
                if not self.app._update_timer_active:
                    self.app._update_timer_active = True
                    self.app.root.after(100, self.app._render_tree_updates)

            # 보유 종목이 있으면 포지션 관리 탭 실시간 평가·요약 갱신 (스로틀은 앱 쪽)
            if self._code_in_virtual_positions(code):
                try:
                    self.app.request_virtual_tab_realtime_render()
                except Exception:
                    pass

        except Exception as e:
            if not self._last_realtime_err == str(e):
                err_msg = f"⚠️ [SignalEngine] {code} 처리 오류: {e}"
                self.app.log(err_msg)
                self._last_realtime_err = str(e)

    def _watchlist_only_codes(self) -> set:
        """감시 목록에 없고 관심목록에만 있는 종목 (정규화 6자리)."""
        try:
            mc = getattr(self.app.trading_controller, "monitor_codes", None) or []
            mon = {str(c).zfill(6) for c in mc}
        except Exception:
            mon = set()
        try:
            wl = {str(c).zfill(6) for c in self.app.watchlist_store.get_codes()}
        except Exception:
            wl = set()
        return wl - mon

    def _send_watchlist_telegram(self, msg: str) -> None:
        try:
            self.app.send_telegram_message(msg)
        except Exception:
            pass

    def _tick_watchlist_synthetic_s2_exit(
        self,
        code: str,
        now_price: float,
        last_row: dict,
        info: dict,
        *,
        bid_ask_ratio: float,
        ask_bid_ratio: float,
        vol_power: float,
        is_power_exhausted: bool,
        ref_avg: Optional[float] = None,
    ) -> None:
        """
        보유 여부와 무관하게 Type B · Stage2 청산 규칙만 적용한 합성 포지션 평가.
        ref_avg 가 있으면 사용자 등록 평단을 기준으로, 없으면 당일 시가(info['open'])를 평단으로 둠.
        EXIT 시 텔레그램만 발송.
        """
        try:
            st_root = getattr(self.app, "watchlist_synth_state", None)
            if not isinstance(st_root, dict):
                return
            use_ref = ref_avg is not None and float(ref_avg) > 0
            day = datetime.now().date().isoformat()
            s = st_root.get(code)
            if not isinstance(s, dict):
                s = {}
                st_root[code] = s
            if use_ref:
                base_avg = float(ref_avg)
            else:
                open_p = float(self._to_scalar(info.get("open", 0), 0.0))
                if open_p <= 0:
                    open_p = float(now_price)
                base_avg = open_p
            if s.get("session") != day:
                s["session"] = day
                s["avg_p"] = base_avg
                s["max_p"] = max(base_avg, float(now_price))
            else:
                if use_ref:
                    s["avg_p"] = base_avg
                s["max_p"] = max(float(s.get("max_p", now_price)), float(now_price))
            avg_p = float(s.get("avg_p", base_avg))
            pos = {
                "stage": 2,
                "entry_n": 2,
                "avg_p": avg_p,
                "avg_price": avg_p,
                "max_p": float(s["max_p"]),
                "max_price": float(s["max_p"]),
                "sig_type": "TYPE_B",
                "reason": "WATCHLIST_SYNTH_S2B",
            }
            res = TradingStrategy.evaluate_position(
                code,
                float(now_price),
                pos,
                self.app.settings,
                indicator_row=last_row,
                current_time=datetime.now(),
                is_realtime=True,
                kwargs={
                    "is_power_exhausted": is_power_exhausted,
                    "bid_ask_ratio": bid_ask_ratio,
                    "ask_bid_ratio": ask_bid_ratio,
                    "vol_power": vol_power,
                },
            )
            try:
                s["max_p"] = float(pos.get("max_p", s["max_p"]))
            except Exception:
                pass
            if res.get("action") != "EXIT":
                return
            t0 = self._wl_sell_last_tm.get(code)
            if t0 is not None and (datetime.now() - t0).total_seconds() < 180:
                return
            self._wl_sell_last_tm[code] = datetime.now()
            try:
                nm = self.app.watchlist_store.get_display_name(code)
            except Exception:
                nm = str(info.get("name", code))
            pr = float(res.get("profit_ratio", 0) or 0)
            er = str(res.get("reason", ""))
            anchor = (
                f"등록 평단 {avg_p:,.0f}원 기준"
                if use_ref
                else "당일 시가 앵커"
            )
            msg = (
                "<b>⭐ [텔레그램·관심목록 전용]</b>\n"
                "<i>감시 자동매매/주문과 별도 · 합성 S2-B 평가(보유 무관)</i>\n\n"
                f"<b>TYPE B S2 매도 신호 — {er}</b>\n"
                f"종목: {nm} ({code})\n"
                f"합성 수익률: {pr:+.1f}% ({anchor})\n"
                f"시간: {datetime.now().strftime('%H:%M:%S')}"
            )
            threading.Thread(
                target=self._send_watchlist_telegram,
                args=(msg,),
                daemon=True,
            ).start()
            if use_ref:
                s["avg_p"] = float(ref_avg)
                s["max_p"] = max(float(s.get("max_p", now_price)), float(now_price))
            else:
                s["avg_p"] = float(now_price)
                s["max_p"] = float(now_price)
        except Exception:
            pass

    def _update_realtime_bar(self, code, price, volume):
        self.app.bars_data = self.app.stream_processor.update_realtime_bar(code, price, volume, self.app.bars_data or {})

    def _calculate_realtime_rsi(self, code):
        """[v15.0] 실시간 봉 데이터를 기반으로 즉시 RSI 산출"""
        if code not in self.app.bars_data: return 0.0
        df = self.app.bars_data[code]
        if len(df) < 15: return 0.0
        
        delta = df['Close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        
        rs = gain / (loss + 1e-10)
        rsi = 100 - (100 / (1 + rs))
        return rsi.iloc[-1] if not pd.isna(rsi.iloc[-1]) else 0.0
