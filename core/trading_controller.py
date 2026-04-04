# -*- coding: utf-8 -*-

import threading
import time
import asyncio
import pandas as pd
import os
import config
from datetime import datetime
from strategy.simulation_models import SimulatedRealtimeMetrics

class TradingController:
    """
    [v4.5.3] 트레이딩 생명주기 및 각 엔진의 흐트러진 제어권을 통합 관리하는 마스터 컨트롤러.
    GUI와 비즈니스 로직을 완전히 격리합니다.
    """
    def __init__(self, app, status_callback=None, log_func=None):
        self.app = app
        self.log = log_func or print
        self.status_cb = status_callback # (status_type, data) -> void
        
        # State
        self.is_monitoring = False
        self.monitor_codes = []
        self.monitor_info = {}
        self.monitor_thread = None
        
        # Threads
        self._stop_event = threading.Event()

    def toggle(self):
        """감시 상태를 토글합니다."""
        if not self.is_monitoring:
            return self.start()
        else:
            return self.stop()

    def start(self):
        """실시간 감시 엔진을 시작합니다."""
        if self.monitor_thread and self.monitor_thread.is_alive(): 
            return False

        # 1. 선정 종목 유효성 확인
        target_cache = "selected_targets.csv"
        if not os.path.exists(target_cache):
            self.log("⚠️ [Check] 먼저 종목 선정을 수행해주세요.")
            if self.status_cb: self.status_cb("SHOW_WARNING", "먼저 종목 선정을 수행해주세요.")
            return False

        try:
            df_targets = pd.read_csv(target_cache, dtype={'Code': str})
            self.monitor_codes = df_targets['Code'].tolist()
            self._initialize_monitor_info(df_targets)
            
            if self.status_cb: self.status_cb("TARGETS_LOADED", df_targets)
            
            # 2. 시작 시퀀스 (별도 스레드에서 수행 - UI 프리징 방지)
            threading.Thread(target=self._run_start_sequence, daemon=True).start()
            return True
        except Exception as e:
            self.log(f"❌ [Error] 감시 엔진 시작 실패: {e}")
            return False

    def stop(self):
        """실시간 감시 엔진을 중단합니다."""
        self.is_monitoring = False
        self._stop_event.set()
        
        # 지표 데이터 캐시 저장 (ConfigHandler 위임)
        self.app.config_handler.save_bars_cache(self.app.bars_data or {})
        
        if self.status_cb: self.status_cb("STOPPED", None)
        self.log("⏹️ [System] 실시간 감시가 중단되었습니다. 모든 자원 정리 완료.")
        return True

    def set_monitor_targets(self, df):
        """[v4.5.6] 선정된 종목 리스트를 실시간 감시 대상으로 등록"""
        # 기존 _initialize_monitor_info는 대문자 'Code'를 기대함
        temp_df = df.copy()
        temp_df.columns = [c.capitalize() if c.lower() in ['code', 'name', 'score', 'price', 'open', 'ma20', 'ma60', 'prevvol'] else c for c in temp_df.columns]
        
        if 'Code' not in temp_df.columns and 'code' in df.columns:
            temp_df.rename(columns={'code': 'Code'}, inplace=True)
            
        # 감시 상한: Track A/B 합 (구 max_slots 단일 키는 폴백)
        max_sa = int(self.app.settings.get('max_slots_a', 20))
        max_sb = int(self.app.settings.get('max_slots_b', 10))
        max_slots = max_sa + max_sb
        if max_slots <= 0:
            max_slots = int(self.app.settings.get('max_slots', 20))
        self.monitor_codes = temp_df['Code'].tolist()[:max_slots]
        self._initialize_monitor_info(temp_df.head(max_slots))
        self.log(f"✅ [Controller] {len(self.monitor_codes)}개 종목이 감시 대상으로 등록되었습니다.")

    def _initialize_monitor_info(self, df):
        """감시 종목 기초 정보 및 트리뷰 매칭 정보 초기화"""
        self.monitor_info = {}
        for _, row in df.iterrows():
            c = str(row['Code']).zfill(6)
            self.monitor_info[c] = {
                'name': row.get('Name', 'Unknown'),
                'ma20': float(row.get('MA20', 0)),
                'ma60': float(row.get('MA60', 0)),
                'prev_vol': float(row.get('PrevVol', 0)),
                'score': row.get('Score', 0.0),
                'ma_buffer_scale': float(row.get('MABufScale', 1.0)),
                'open': float(row.get('Open', 0)),
                'last_price': float(row.get('Price', 0)) if row.get('Price') else 0,
                'sel_track': str(
                    row.get('SelTrack', row.get('sel_track', row.get('seltrack', 'AB')))
                ).strip().upper()
                or 'AB',
            }

    def _run_start_sequence(self):
        """워밍업부터 웹소켓 연결까지 마스터 시퀀스"""
        try:
            # [Step 1] Warmup: 전 종목 15분봉·장중 구간 검증 통과 시에만 감시 진행
            if self.status_cb:
                self.status_cb("STATUS_TEXT", f"상태: 분석 중 ({len(self.monitor_codes)}종목)")
            if self.status_cb:
                self.status_cb("WARMUP_BEGIN", list(self.monitor_codes))

            results = self.app.warmup_service.run(
                self.monitor_codes,
                self.monitor_info,
                self.app.bars_data,
            )

            require_full = bool(
                self.app.settings.get("require_full_warmup_for_monitoring", True)
            )
            failed = [
                c for c in self.monitor_codes if not results.get(c, {}).get("ok")
            ]
            if failed:
                if require_full:
                    for c in self.monitor_codes:
                        try:
                            self.app.bars_data.pop(c, None)
                        except Exception:
                            pass
                    if self.status_cb:
                        self.status_cb(
                            "WARMUP_FAILED",
                            {"results": results, "failed": failed},
                        )
                    if self.status_cb:
                        self.status_cb(
                            "STATUS_TEXT",
                            "상태: 워밍업 실패 — 감시 미시작 (신호상태 열 참고)",
                        )
                    self.log(
                        "❌ [System] 워밍업 미달 종목이 있어 실시간 감시를 시작하지 않습니다."
                    )
                    return
                # 완화 모드: 실패 종목만 제외 (미권장)
                for c in failed:
                    try:
                        self.app.bars_data.pop(c, None)
                    except Exception:
                        pass
                    try:
                        self.monitor_info.pop(c, None)
                    except Exception:
                        pass
                self.monitor_codes = [
                    c for c in self.monitor_codes if c not in failed
                ]
                if not self.monitor_codes:
                    if self.status_cb:
                        self.status_cb(
                            "WARMUP_FAILED",
                            {"results": results, "failed": failed},
                        )
                    if self.status_cb:
                        self.status_cb(
                            "STATUS_TEXT",
                            "상태: 워밍업 실패 — 감시 대상 없음",
                        )
                    self.log("❌ [System] 워밍업 실패로 감시 가능 종목이 없습니다.")
                    return
                self.log(
                    f"⚠️ [System] 워밍업 실패 {len(failed)}종목 제외 후 감시 진행 "
                    f"(require_full_warmup_for_monitoring=False)"
                )
                if self.status_cb:
                    self.status_cb(
                        "MONITOR_CODES_FILTERED",
                        {
                            "codes": list(self.monitor_codes),
                            "dropped": failed,
                            "results": results,
                        },
                    )

            self.log(
                "✅ [System] 전 종목 15분봉 워밍업 검증 완료. 실시간 모니터링을 시작합니다."
            )

            self.is_monitoring = True
            if self.status_cb:
                self.status_cb("STARTED", len(self.monitor_codes))

            # [Step 2] 가상 실천 엔진(WS Worker) 구동
            self._stop_event.clear()
            self.monitor_thread = threading.Thread(
                target=self._ws_worker_loop, daemon=True
            )
            self.monitor_thread.start()

            # [Step 3] 주기적 관리 루프 시작
            self._start_periodic_routines()

        except Exception as e:
            self.log(f"❌ [Error] 시작 시퀀스 실패: {e}")
            self.stop()

    def _ws_worker_loop(self):
        """웹소켓 비동기 루프 래퍼"""
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            # MonitorTab에 구현된 웹소켓 루프 호출 (Bridge 역할)
            new_loop.run_until_complete(self.app.monitor_tab._ws_client_loop())
        except Exception as e:
            self.log(f"⚠️ [System] 웹소켓 워커 종료: {e}")

    def _start_periodic_routines(self):
        """자가 학습 모델 저장 및 보고 루프 (재귀적 스케줄링)"""
        def run_save():
            if not self.is_monitoring: return
            SimulatedRealtimeMetrics.save_params()
            self.app.root.after(300000, run_save) # 5분

        def run_report():
            if not self.is_monitoring: return
            p = SimulatedRealtimeMetrics.params
            if p.get('total_calibrations', 0) > 100:
                msg = f"📈 [Model Report] 합성 모델 자가 학습 진행 중... (학습 횟수: {p['total_calibrations']})\n"
                msg += f"   - 체결강도 평균 오차: {p['avg_error_vp']:.2f} | 호가잔량 평균 오차: {p['avg_error_ba']:.2f}"
                self.log(msg)
            self.app.root.after(1800000, run_report) # 30분

        # 초기 트리거
        self.app.root.after(300000, run_save)
        self.app.root.after(1800000, run_report)

    def execute_buy_signal(self, code, name, price, reason="TYPE_A"):
        """[v18.0/v4.5.4] 1단계 진입 시그널에 따른 매매 집행 (실전/가상 통합)"""
        if code in self.app.portfolio.virtual_positions: return
        
        # 0. 자동 매매 마스터 스위치 확인 (UI 연동)
        if not self.app.settings.get('use_auto_virtual', False):
            self.log(f"🔔 [Signal] {name}({code}) {reason} 포착! (자동 매매 OFF 상태)")
            return

        # 1. 투자금액 산출 (Risk Management)
        use_mock = getattr(config, 'USE_MOCK', False)
        allow_order = getattr(config, 'ALLOW_MOCK_ORDER', False) if use_mock else getattr(config, 'ALLOW_REAL_ORDER', False)
        
        invest_amt = self.app.portfolio.calculate_order_quantity(stage=1, is_simulation=not allow_order)
        if invest_amt <= 0:
            self.log(f"⚠️ [Skip] {name}({code}) 진입 건너뜀 (투자금액 0 또는 자산 정보 부재)")
            return
            
        # 2. 주문 수량 및 가격 보정
        adj_price = self.app.order_manager.get_adjusted_price(price, "buy")
        qty = max(1, invest_amt // adj_price)
        
        # 3. 실전/모의 주문 발송 또는 가상 체결 처리
        if allow_order:
            self.app.order_manager.place_order(code, qty, price, order_type="03", side="buy", name=name, reason=reason)
        else:
            snapshot = self.app.portfolio.capture_snapshot(code, price, reason=reason)
            self.app.portfolio.on_fill(code, "buy", price, qty, name=name, reason=reason, snapshot=snapshot)
            
        self.log(f"🚀 [Buy Signal] {name}({code}) {reason} 진입 시도 ({price:,}원, {qty}주)")
        
        # 4. 텔레그램 알림 (비동기)
        import threading
        msg = f"<b>💰 [매수발송/{reason}]</b>\n종목: {name}({code})\n가: {price:,}원 / 수: {qty}주\n시간: {datetime.now().strftime('%H:%M:%S')}"
        threading.Thread(target=self.app.send_telegram_message, args=(msg,), daemon=True).start()

    def handle_strategy_action(self, code, action, reason, **kwargs):
        """[v4.5.3/v4.5.4] StrategyEngine에서 결정된 액션(EXIT/STAGE2)을 처리합니다."""
        if action == "HOLD": return
        
        pos = self.app.portfolio.virtual_positions.get(code)
        if not pos: return
        
        now_price = kwargs.get('now_price', 0)
        profit_ratio = kwargs.get('profit_ratio', 0.0)
        
        use_mock = getattr(config, 'USE_MOCK', False)
        allow_order = getattr(config, 'ALLOW_MOCK_ORDER', False) if use_mock else getattr(config, 'ALLOW_REAL_ORDER', False)

        # 0. 자동 매매 마스터 스위치 확인 (UI 연동)
        if not self.app.settings.get('use_auto_virtual', False): return

        if action == "EXIT":
            # [Audit] Exit Execution
            if allow_order:
                self.app.order_manager.place_order(code, pos['qty'], now_price, order_type="03", side="sell", name=pos['name'], reason=f"EXIT_{reason}")
            else:
                snapshot = self.app.portfolio.capture_snapshot(code, now_price, reason=f"EXIT_{reason}")
                self.app.portfolio.on_fill(code, "sell", now_price, pos['qty'], snapshot=snapshot)

            self.log(f"🚨 [Exit Signal/{reason}] {pos['name']}({code}) 청산 ({profit_ratio:+.2f}%)")
            pos['exit_pending'] = True
            pos['exit_reason'] = reason
            
            # [v4.4] 텔레그램 알림
            import threading
            msg = f"<b>🚀 [매도발송/{reason}]</b>\n종목: {pos['name']}({code})\n수익률: {profit_ratio:+.1f}%\n시간: {datetime.now().strftime('%H:%M:%S')}"
            threading.Thread(target=self.app.send_telegram_message, args=(msg,), daemon=True).start()

        elif action == "STAGE2":
            # [Audit] Stage 2 Scale-in Execution
            add_amt = self.app.portfolio.calculate_order_quantity(stage=2, is_simulation=not allow_order)
            if add_amt <= 0: return
                
            adj_price = self.app.order_manager.get_adjusted_price(now_price, "buy")
            add_qty = add_amt // adj_price
            
            if add_qty > 0:
                if allow_order:
                    self.app.order_manager.place_order(code, add_qty, now_price, order_type="03", side="buy", name=pos['name'], reason="Stage 2 Scaling")
                else:
                    snapshot = self.app.portfolio.capture_snapshot(code, now_price, reason="Stage 2 Scaling")
                    self.app.portfolio.on_fill(code, "buy", now_price, add_qty, snapshot=snapshot)
                
                self.log(f"🔥 [Scale-in] {pos['name']}({code}) 2차 추매 ({now_price:,}원, {add_qty}주)")
                
                # [v4.4] 텔레그램 알림
                import threading
                msg = f"<b>🔥 [추매발송/2차]</b>\n종목: {pos['name']}({code})\n평단대비: {profit_ratio:+.1f}%\n시간: {datetime.now().strftime('%H:%M:%S')}"
                threading.Thread(target=self.app.send_telegram_message, args=(msg,), daemon=True).start()
