# -*- coding: utf-8 -*-

import threading
import time
import asyncio
import pandas as pd
import os
import config
from datetime import datetime
from typing import Any, Dict, Optional, Set


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
        # 지정가 미체결 타임아웃: ord_no -> {after_id, code, kind, qty}
        self._limit_order_meta: Dict[str, Dict[str, Any]] = {}
        # S1 매수 접수 후 체결 전 — 동일 종목 중복 매수 방지
        self._s1_buy_order_pending_codes: Set[str] = set()
        # _run_start_sequence 가 백그라운드에서 도는 동안 중복 start() 방지
        self._run_start_sequence_active: bool = False
        # stop() 호출 시 워밍업 직후 단계(WS 기동 등)를 건너뛰기 위함
        self._abort_monitor_start: bool = False

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
        if getattr(self, "_run_start_sequence_active", False):
            self.log(
                "⚠️ [Check] 감시 시작 절차가 이미 진행 중입니다. 워밍업·접속이 끝날 때까지 기다려 주세요."
            )
            if self.status_cb:
                self.status_cb(
                    "SHOW_WARNING",
                    "감시 시작이 이미 진행 중입니다. 잠시 후 다시 시도하세요.",
                )
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
            self._run_start_sequence_active = True
            threading.Thread(
                target=self._run_start_sequence_guarded, daemon=True
            ).start()
            return True
        except Exception as e:
            self.log(f"❌ [Error] 감시 엔진 시작 실패: {e}")
            return False

    def stop(self):
        """실시간 감시 엔진을 중단합니다."""
        self.is_monitoring = False
        self._abort_monitor_start = True
        try:
            ws = getattr(self.app, "warmup_service", None)
            if ws is not None and getattr(ws, "is_running", False):
                ws.stop()
        except Exception:
            pass
        self._stop_event.set()
        
        # 지표 데이터 캐시 저장 (ConfigHandler 위임)
        self.app.config_handler.save_bars_cache(self.app.bars_data or {})
        
        if self.status_cb: self.status_cb("STOPPED", None)
        self._run_start_sequence_active = False
        self.log("⏹️ [System] 실시간 감시가 중단되었습니다. 모든 자원 정리 완료.")
        return True

    def _run_start_sequence_guarded(self) -> None:
        """_run_start_sequence 예외·정상 종료 후 중복 시작 플래그를 반드시 해제합니다."""
        try:
            self._run_start_sequence()
        except Exception as e:
            try:
                self.log(f"❌ [Error] 시작 시퀀스(보호 래퍼) 실패: {e}")
            except Exception:
                pass
        finally:
            self._run_start_sequence_active = False

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

    def _merged_warmup_codes(self):
        """감시 종목 ∪ 관심목록 ∪ 가상·실제 보유 (순서 유지·중복 제거)."""
        mc = [str(c).zfill(6) for c in (self.monitor_codes or [])]
        try:
            wl = [str(c).zfill(6) for c in self.app.watchlist_store.get_codes()]
        except Exception:
            wl = []
        vp: list = []
        try:
            vpd = getattr(self.app.portfolio, "virtual_positions", None) or {}
            vp = [str(c).zfill(6) for c in vpd.keys()]
        except Exception:
            vp = []
        return list(dict.fromkeys(mc + wl + vp))

    def _ensure_virtual_positions_monitor_info(self) -> None:
        """보유 중인 종목은 감시·웹소켓 스트림에 포함되도록 monitor_info 최소 항목을 채웁니다."""
        try:
            vpd = getattr(self.app.portfolio, "virtual_positions", None) or {}
        except Exception:
            vpd = {}
        for c_raw, pos in list(vpd.items()):
            try:
                c = str(c_raw).zfill(6)
                if c in self.monitor_info:
                    continue
                nm = pos.get("name") or c
                ap = float(pos.get("avg_price", 0) or 0)
                self.monitor_info[c] = {
                    "name": nm,
                    "ma20": 0.0,
                    "ma60": 0.0,
                    "prev_vol": 0.0,
                    "score": 0.0,
                    "ma_buffer_scale": 1.0,
                    "open": ap,
                    "last_price": 0.0,
                    "sel_track": "AB",
                    "vp_price_stub": True,
                }
            except Exception:
                continue

    def _ensure_watchlist_monitor_info(self) -> None:
        """관심목록 전용 종목은 monitor_info 최소 항목을 채웁니다 (감시 트리와 별도)."""
        try:
            wl = set(str(c).zfill(6) for c in self.app.watchlist_store.get_codes())
        except Exception:
            wl = set()
        mc = set(str(c).zfill(6) for c in (self.monitor_codes or []))
        for c in wl - mc:
            if c in self.monitor_info:
                continue
            try:
                name = self.app.watchlist_store.get_display_name(c)
            except Exception:
                name = c
            self.monitor_info[c] = {
                "name": name,
                "ma20": 0.0,
                "ma60": 0.0,
                "prev_vol": 0.0,
                "score": 0.0,
                "ma_buffer_scale": 1.0,
                "open": 0.0,
                "last_price": 0.0,
                "sel_track": "B",
            }

    def _status_cb_on_main(self, status: str, data: Any) -> None:
        """
        워밍업 워커 등 비-Tk 스레드에서 status_cb 호출 시 메인 루프로 위임합니다.
        (Tk 위젯 갱신·플래그 동기화를 안전하게 하기 위함)
        """
        def _call() -> None:
            try:
                if self.status_cb:
                    self.status_cb(status, data)
            except Exception as e:
                self.log(f"⚠️ [System] status_cb({status!r}) 실행 실패: {e}")

        try:
            root = getattr(self.app, "root", None)
            if root is not None:
                root.after(0, _call)
            else:
                _call()
        except Exception as e:
            self.log(f"⚠️ [System] status_cb 메인 스레드 위임 실패: {e}")
            _call()

    def _schedule_warmup_stock_ready(self, code: str) -> None:
        """
        워밍업 워커 스레드 → Tk 메인 스레드: 준비 완료 종목을 스트림 구독에 반영.
        """
        try:
            root = getattr(self.app, "root", None)
            if root is not None:
                root.after(0, lambda c=code: self._apply_warmup_ready_stock(c))
            else:
                self._apply_warmup_ready_stock(code)
        except Exception as e:
            self.log(f"⚠️ [System] 준비 종목 메인 스레드 위임 실패: {e}")

    def _apply_warmup_ready_stock(self, code: str) -> None:
        """
        메인 스레드에서만 호출: incremental_warmup_stream 경로에서
        준비된 종목부터 웹소켓·주기 루프를 기동하고 REG 갱신을 요청합니다.
        """
        try:
            code = str(code).zfill(6)
            bd = getattr(self.app, "bars_data", None)
            if not isinstance(bd, dict):
                bd = {}
            if code not in bd or bd.get(code) is None:
                self.log(
                    f"⚠️ [System] 준비 종목 스트림 반영 건너뜀: bars_data 에 {code} 없음 "
                    f"(Tk 지연·캐시 키 불일치 가능 — 워밍업 완료 후 재시도)"
                )
                return

            if not isinstance(getattr(self.app, "_active_stream_codes", None), list):
                self.app._active_stream_codes = []
            ac = self.app._active_stream_codes
            if code not in ac:
                ac.append(code)
            n_ready = len(ac)

            self.is_monitoring = True
            if self.status_cb:
                self.status_cb("STARTED", n_ready)

            # 워밍업 워커가 _run_start_sequence 에서 먼저 웹소켓을 띄운 경우 중복 기동 방지
            if not getattr(self, "_incremental_stream_bootstrapped", False):
                self._incremental_stream_bootstrapped = True
                if not (
                    self.monitor_thread is not None and self.monitor_thread.is_alive()
                ):
                    self._stop_event.clear()
                    self.monitor_thread = threading.Thread(
                        target=self._ws_worker_loop, daemon=True
                    )
                    self.monitor_thread.start()

            mt = getattr(self.app, "monitor_tab", None)
            if mt is not None:
                mt._pending_reg_refresh = True
        except Exception as e:
            self.log(f"⚠️ [System] 준비 종목 스트림 반영 실패: {e}")

    def _run_start_sequence(self):
        """워밍업부터 웹소켓 연결까지 마스터 시퀀스"""
        try:
            self._abort_monitor_start = False
            self.app._active_stream_codes = []
            self._incremental_stream_bootstrapped = False
            merged = self._merged_warmup_codes()
            self._ensure_watchlist_monitor_info()
            self._ensure_virtual_positions_monitor_info()

            require_full = bool(
                self.app.settings.get("require_full_warmup_for_monitoring", False)
            )
            # require_full 여부와 무관: 준비 완료 종목부터 스트림(실패 시 require_full 분기에서 감시 중단)
            incremental_stream = bool(
                self.app.settings.get("incremental_warmup_stream", True)
            )

            # [Step 1] Warmup: 전 종목 15분봉·장중 구간 검증 통과 시에만 감시 진행
            if self.status_cb:
                self.status_cb(
                    "STATUS_TEXT",
                    f"상태: 분석 중 (감시 {len(self.monitor_codes)} + 스트림 합계 {len(merged)})",
                )
            if self.status_cb:
                self.status_cb("WARMUP_BEGIN", list(merged))

            on_ready = (
                self._schedule_warmup_stock_ready if incremental_stream else None
            )
            results = self.app.warmup_service.run(
                merged,
                self.monitor_info,
                self.app.bars_data,
                on_stock_ready=on_ready,
            )
            if self._abort_monitor_start:
                self.log(
                    "ℹ️ [System] 감시 중단 요청으로 워밍업 이후(접속·등록) 단계를 생략합니다."
                )
                return
            failed = [c for c in merged if not results.get(c, {}).get("ok")]
            if failed:
                if require_full:
                    # 점진 스트림으로 이미 WS가 켜진 경우: 전부 실패 처리 전 감시 중단
                    if getattr(self, "_incremental_stream_bootstrapped", False):
                        try:
                            self.stop()
                        except Exception as se:
                            self.log(f"⚠️ [System] 워밍업 실패 시 감시 중단 처리 오류: {se}")
                    for c in merged:
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
                if not [
                    c for c in merged if results.get(c, {}).get("ok")
                ]:
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

            successful = [c for c in merged if results.get(c, {}).get("ok")]
            self.app._active_stream_codes = successful

            def _notify_started_and_reg_refresh() -> None:
                self._status_cb_on_main("STARTED", len(successful))
                mt = getattr(self.app, "monitor_tab", None)
                if mt is not None:
                    mt._pending_reg_refresh = True

            # 메인 스레드의 _apply 가 WS를 띄우기 전에 워커가 여기까지 도달할 수 있음 → 중복 소켓 방지
            ws_already_running = (
                self.monitor_thread is not None and self.monitor_thread.is_alive()
            )
            # bootstrapped 만 True이고 WS 스레드는 이미 죽은 경우(빈 구독·연결 실패 등) 재기동하지 않고
            # return 하면 UI 만 '감시 중'이 되어 틱이 영원히 안 옴 → 살아 있는 스레드만 '준비 완료'로 본다.
            incremental_ws_ready = incremental_stream and ws_already_running

            if incremental_ws_ready:
                self.log(
                    "✅ [System] 전 종목 15분봉 워밍업 검증 완료. 실시간 구독 목록을 최종 동기화합니다."
                )
                # _ws_client_loop 가 while self.app.is_monitoring 이므로, after(0) STARTED 보다 먼저 동기 반영
                self.is_monitoring = True
                try:
                    self.app.is_monitoring = True
                except Exception:
                    pass
                _notify_started_and_reg_refresh()
                return

            self.log(
                "✅ [System] 전 종목 15분봉 워밍업 검증 완료. 실시간 모니터링을 시작합니다."
            )

            self.is_monitoring = True
            try:
                self.app.is_monitoring = True
            except Exception:
                pass
            _notify_started_and_reg_refresh()

            # [Step 2] 가상 실천 엔진(WS Worker) 구동 (이미 살아 있으면 재시작하지 않음)
            if ws_already_running:
                self.log(
                    "ℹ️ [System] 웹소켓 워커가 이미 동작 중이어서 추가 기동을 건너뜁니다."
                )
                return

            self._stop_event.clear()
            self.monitor_thread = threading.Thread(
                target=self._ws_worker_loop, daemon=True
            )
            self.monitor_thread.start()

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

    def _supply_log_fields(self, code: object) -> Dict[str, Any]:
        """일일 JSONL(order/fill)용: 감시 화면과 동일한 체결강도·호가 스냅샷."""
        try:
            from core.supply_display import (
                capture_supply_snapshot,
                format_vol_power_column,
            )

            nc = str(code).split("_")[0].strip().zfill(6)
            s = capture_supply_snapshot(self.app, nc)
            return {
                "snap_vol_power": s.get("vol_power"),
                "snap_vol_power_str": format_vol_power_column(s.get("vol_power")),
                "snap_bid_ask": s.get("bid_ask_text"),
            }
        except Exception:
            return {
                "snap_vol_power": None,
                "snap_vol_power_str": "—",
                "snap_bid_ask": "— / — (—)",
            }

    def execute_buy_signal(self, code, name, price, reason="TYPE_A") -> bool:
        """[v18.0/v4.5.4] 1단계 진입 시그널에 따른 매매 집행 (실전/가상 통합).
        Returns True iff 주문 접수(ord_no) 또는 가상 체결까지 성공 — SignalEngine 쿨타임 갱신용.
        """
        try:
            sl = getattr(self.app, "session_logger", None)
            if sl:
                sl.log(
                    "buy_execute_start",
                    {"code": code, "name": name, "price": float(price), "reason": reason},
                )
        except Exception:
            pass

        if code in self.app.portfolio.virtual_positions:
            try:
                sl = getattr(self.app, "session_logger", None)
                if sl:
                    sl.log(
                        "buy_skipped",
                        {"code": code, "reason": "already_in_position"},
                    )
            except Exception:
                pass
            return False

        try:
            nc = str(code).zfill(6)
            if nc in self._s1_buy_order_pending_codes:
                self.log(
                    f"⚠️ [Skip] {name}({code}) S1 매수 미체결 대기 중 — 중복 주문 방지"
                )
                return False
        except Exception:
            pass

        # 0. 자동 매매 마스터 스위치 확인 (UI 연동)
        if not self.app.settings.get('use_auto_virtual', False):
            self.log(f"🔔 [Signal] {name}({code}) {reason} 포착! (자동 매매 OFF 상태)")
            try:
                sl = getattr(self.app, "session_logger", None)
                if sl:
                    sl.log(
                        "buy_skipped",
                        {"code": code, "name": name, "reason": "use_auto_virtual_off"},
                    )
            except Exception:
                pass
            return False

        # 1. 투자금액 산출 (Risk Management)
        use_mock = getattr(config, 'USE_MOCK', False)
        allow_order = getattr(config, 'ALLOW_MOCK_ORDER', False) if use_mock else getattr(config, 'ALLOW_REAL_ORDER', False)
        
        invest_amt = self.app.portfolio.calculate_order_quantity(stage=1, is_simulation=not allow_order)
        if invest_amt <= 0:
            self.log(f"⚠️ [Skip] {name}({code}) 진입 건너뜀 (투자금액 0 또는 자산 정보 부재)")
            try:
                sl = getattr(self.app, "session_logger", None)
                if sl:
                    sl.log(
                        "buy_skipped",
                        {"code": code, "name": name, "reason": "zero_invest_amt"},
                    )
            except Exception:
                pass
            return False
            
        # 2. 주문 수량 및 가격 보정
        adj_price = self.app.order_manager.get_adjusted_price(price, "buy")
        qty = max(1, invest_amt // adj_price)
        
        # 3. 실전/모의 주문 발송 또는 가상 체결 처리
        ord_no = None
        if allow_order:
            ord_no = self.app.order_manager.place_order(
                code, qty, price, order_type="00", side="buy", name=name, reason=reason
            )
            try:
                sl = getattr(self.app, "session_logger", None)
                if sl:
                    _ob = {
                        "code": code,
                        "name": name,
                        "qty": int(qty),
                        "price": int(adj_price),
                        "order_type": "00",
                        "result": "submitted" if ord_no else "failed",
                        "order_no": ord_no or "",
                        "reason": reason,
                    }
                    _ob.update(self._supply_log_fields(code))
                    sl.log("order_buy", _ob)
            except Exception:
                pass
        else:
            snapshot = self.app.portfolio.capture_snapshot(code, price, reason=reason)
            self.app.portfolio.on_fill(code, "buy", price, qty, name=name, reason=reason, snapshot=snapshot)

        if allow_order and not ord_no:
            self.log(
                f"⚠️ [Buy Signal] {name}({code}) 매수 주문 실패 — 다음 신호에서 재판정·재시도 가능"
            )
            return False

        if allow_order and ord_no:
            try:
                self._register_limit_order_watch(
                    str(ord_no).strip(),
                    str(code).zfill(6),
                    "s1_buy",
                    qty=int(qty),
                )
            except Exception as e:
                self.log(f"⚠️ [Buy Signal] 미체결 감시 등록 실패: {e}")

        self.log(f"🚀 [Buy Signal] {name}({code}) {reason} 진입 시도 ({price:,}원, {qty}주)")

        # 4. 텔레그램 알림 (비동기)
        import threading
        msg = f"<b>💰 [매수발송/{reason}]</b>\n종목: {name}({code})\n가: {price:,}원 / 수: {qty}주\n시간: {datetime.now().strftime('%H:%M:%S')}"
        threading.Thread(target=self.app.send_telegram_message, args=(msg,), daemon=True).start()
        return True

    def handle_strategy_action(self, code, action, reason, **kwargs):
        """[v4.5.3/v4.5.4] StrategyEngine에서 결정된 액션(EXIT/STAGE2)을 처리합니다."""
        if action == "HOLD":
            return

        try:
            sl = getattr(self.app, "session_logger", None)
            if sl:
                sl.log(
                    "strategy_action",
                    {
                        "code": code,
                        "action": action,
                        "reason": reason,
                        "now_price": float(kwargs.get("now_price", 0) or 0),
                        "profit_ratio": float(kwargs.get("profit_ratio", 0) or 0),
                    },
                )
        except Exception:
            pass

        pos = self.app.portfolio.virtual_positions.get(code)
        if not pos:
            return

        now_price = kwargs.get('now_price', 0)
        profit_ratio = kwargs.get('profit_ratio', 0.0)
        
        use_mock = getattr(config, 'USE_MOCK', False)
        allow_order = getattr(config, 'ALLOW_MOCK_ORDER', False) if use_mock else getattr(config, 'ALLOW_REAL_ORDER', False)

        # 0. 자동 매매 마스터 스위치 확인 (UI 연동)
        if not self.app.settings.get('use_auto_virtual', False):
            try:
                sl = getattr(self.app, "session_logger", None)
                if sl:
                    sl.log(
                        "strategy_action_skipped",
                        {"code": code, "action": action, "reason": "use_auto_virtual_off"},
                    )
            except Exception:
                pass
            return

        if action == "EXIT":
            # [Audit] Exit Execution — 주문 실패 시 exit_pending 미설정(재평가·재시도 가능)
            if allow_order:
                ord_no = self.app.order_manager.place_order(
                    code,
                    pos['qty'],
                    now_price,
                    order_type="00",
                    side="sell",
                    name=pos['name'],
                    reason=f"EXIT_{reason}",
                )
                try:
                    sl = getattr(self.app, "session_logger", None)
                    if sl:
                        _os = {
                            "code": code,
                            "name": pos.get("name", ""),
                            "qty": int(pos.get("qty", 0)),
                            "price": int(now_price),
                            "result": "submitted" if ord_no else "failed",
                            "order_no": ord_no or "",
                            "reason": f"EXIT_{reason}",
                        }
                        _os.update(self._supply_log_fields(code))
                        sl.log("order_sell", _os)
                except Exception:
                    pass
                if ord_no:
                    pos["exit_pending"] = True
                    pos["exit_reason"] = reason
                    pos["pending_exit_ord_no"] = str(ord_no).strip()
                    try:
                        self._register_limit_order_watch(
                            str(ord_no).strip(),
                            str(code).zfill(6),
                            "exit_sell",
                            qty=int(pos.get("qty", 0)),
                        )
                    except Exception as e:
                        self.log(f"⚠️ [Exit] 미체결 감시 등록 실패: {e}")
                    self.log(
                        f"🚨 [Exit Signal/{reason}] {pos['name']}({code}) 매도 접수 ({profit_ratio:+.2f}%)"
                    )
                    import threading
                    msg = (
                        f"<b>🚀 [매도발송/{reason}]</b>\n종목: {pos['name']}({code})\n"
                        f"수익률: {profit_ratio:+.1f}%\n시간: {datetime.now().strftime('%H:%M:%S')}"
                    )
                    threading.Thread(
                        target=self.app.send_telegram_message, args=(msg,), daemon=True
                    ).start()
                else:
                    self.log(
                        f"⚠️ [Exit Signal/{reason}] {pos['name']}({code}) 매도 주문 실패 "
                        f"— exit_pending 미설정, 재평가 가능 ({profit_ratio:+.2f}%)"
                    )
                    try:
                        sl = getattr(self.app, "session_logger", None)
                        if sl:
                            sl.log(
                                "order_exit_failed",
                                {
                                    "code": code,
                                    "name": pos.get("name", ""),
                                    "reason": f"EXIT_{reason}",
                                    "profit_ratio": float(profit_ratio),
                                },
                            )
                    except Exception:
                        pass
            else:
                snapshot = self.app.portfolio.capture_snapshot(
                    code, now_price, reason=f"EXIT_{reason}"
                )
                self.app.portfolio.on_fill(
                    code, "sell", now_price, pos["qty"], snapshot=snapshot
                )
                self.log(
                    f"🚨 [Exit Signal/{reason}] {pos['name']}({code}) 가상 청산 ({profit_ratio:+.2f}%)"
                )
                import threading
                msg = (
                    f"<b>🚀 [매도발송/{reason}]</b>\n종목: {pos['name']}({code})\n"
                    f"수익률: {profit_ratio:+.1f}%\n시간: {datetime.now().strftime('%H:%M:%S')}"
                )
                threading.Thread(
                    target=self.app.send_telegram_message, args=(msg,), daemon=True
                ).start()

        elif action == "STAGE2":
            # [Audit] Stage 2 Scale-in Execution
            add_amt = self.app.portfolio.calculate_order_quantity(stage=2, is_simulation=not allow_order)
            if add_amt <= 0: return
                
            adj_price = self.app.order_manager.get_adjusted_price(now_price, "buy")
            add_qty = add_amt // adj_price
            
            if add_qty > 0:
                if allow_order:
                    ord_no = self.app.order_manager.place_order(
                        code,
                        add_qty,
                        now_price,
                        order_type="00",
                        side="buy",
                        name=pos['name'],
                        reason="Stage 2 Scaling",
                    )
                    try:
                        sl = getattr(self.app, "session_logger", None)
                        if sl:
                            _os2 = {
                                "code": code,
                                "name": pos.get("name", ""),
                                "qty": int(add_qty),
                                "price": int(adj_price),
                                "result": "submitted" if ord_no else "failed",
                                "order_no": ord_no or "",
                                "reason": "Stage 2 Scaling",
                            }
                            _os2.update(self._supply_log_fields(code))
                            sl.log("order_buy_s2", _os2)
                    except Exception:
                        pass
                    if ord_no:
                        pos["s2_limit_order_pending"] = True
                        pos["s2_pending_ord_no"] = str(ord_no).strip()
                        try:
                            self._register_limit_order_watch(
                                str(ord_no).strip(),
                                str(code).zfill(6),
                                "s2_buy",
                                qty=int(add_qty),
                            )
                        except Exception as e:
                            self.log(f"⚠️ [S2] 미체결 감시 등록 실패: {e}")
                        self.log(
                            f"🔥 [Scale-in] {pos['name']}({code}) 2차 추매 접수 ({now_price:,}원, {add_qty}주)"
                        )
                        import threading
                        msg = (
                            f"<b>🔥 [추매발송/2차]</b>\n종목: {pos['name']}({code})\n"
                            f"평단대비: {profit_ratio:+.1f}%\n시간: {datetime.now().strftime('%H:%M:%S')}"
                        )
                        threading.Thread(
                            target=self.app.send_telegram_message, args=(msg,), daemon=True
                        ).start()
                    else:
                        self.log(
                            f"⚠️ [Scale-in] {pos['name']}({code}) 2차 매수 주문 실패 — 재조건 충족 시 재시도"
                        )
                else:
                    snapshot = self.app.portfolio.capture_snapshot(
                        code, now_price, reason="Stage 2 Scaling"
                    )
                    self.app.portfolio.on_fill(
                        code, "buy", now_price, add_qty, snapshot=snapshot
                    )
                    self.log(
                        f"🔥 [Scale-in] {pos['name']}({code}) 2차 추매 가상체결 ({now_price:,}원, {add_qty}주)"
                    )
                    import threading
                    msg = (
                        f"<b>🔥 [추매발송/2차]</b>\n종목: {pos['name']}({code})\n"
                        f"평단대비: {profit_ratio:+.1f}%\n시간: {datetime.now().strftime('%H:%M:%S')}"
                    )
                    threading.Thread(
                        target=self.app.send_telegram_message, args=(msg,), daemon=True
                    ).start()

    def _limit_order_timeout_ms(self) -> int:
        """settings.json `limit_order_unfilled_timeout_sec` — 짧은 지정가 미체결 대기(초)."""
        try:
            sec = float(
                (self.app.settings or {}).get("limit_order_unfilled_timeout_sec", 5.0)
            )
        except (TypeError, ValueError):
            sec = 5.0
        sec = max(2.0, min(sec, 120.0))
        return int(sec * 1000)

    def clear_limit_order_watch(self, order_no: Optional[str]) -> None:
        """체결 등으로 주문이 종료되면 타이머·대기 플래그를 정리합니다."""
        if not order_no:
            return
        oid = str(order_no).strip()
        if not oid:
            return
        meta = self._limit_order_meta.pop(oid, None)
        if meta is None:
            return
        root = getattr(self.app, "root", None)
        aid = meta.get("after_id")
        if root is not None and aid is not None:
            try:
                root.after_cancel(aid)
            except Exception:
                pass
        kind = meta.get("kind")
        code = meta.get("code")
        try:
            nc = str(code).zfill(6) if code else ""
        except Exception:
            nc = ""
        if kind == "s1_buy" and nc:
            self._s1_buy_order_pending_codes.discard(nc)
        if kind == "s2_buy" and nc:
            try:
                with self.app.portfolio.lock:
                    p2 = self.app.portfolio.virtual_positions.get(nc)
                    if p2:
                        p2.pop("s2_limit_order_pending", None)
                        p2.pop("s2_pending_ord_no", None)
            except Exception:
                pass

    def _register_limit_order_watch(
        self,
        order_no: str,
        code: str,
        kind: str,
        qty: int = 0,
    ) -> None:
        """지정가 접수 후 미체결 타임아웃 시 취소 API 및 상태 정리(재판정 가능)."""
        oid = str(order_no or "").strip()
        nc = str(code).zfill(6)
        if not oid or not nc:
            return
        self.clear_limit_order_watch(oid)
        root = getattr(self.app, "root", None)
        if root is None:
            return
        ms = self._limit_order_timeout_ms()

        def _cb() -> None:
            try:
                self._handle_limit_order_timeout(oid)
            except Exception as e:
                self.log(f"⚠️ [미체결타임아웃] 처리 오류: {e}")

        self.log(
            f"⏱️ [지정가] 미체결 감시 등록 ({kind}) {nc} ord={oid} / {ms // 1000}초"
        )
        after_id = root.after(ms, _cb)
        self._limit_order_meta[oid] = {
            "code": nc,
            "kind": kind,
            "after_id": after_id,
            "qty": int(qty),
        }
        if kind == "s1_buy":
            self._s1_buy_order_pending_codes.add(nc)

    def _handle_limit_order_timeout(self, order_no: str) -> None:
        """미체결 타임아웃: 취소 API 호출 후 대기 플래그 해제(횟수 제한 없이 재신호 가능)."""
        oid = str(order_no or "").strip()
        meta = self._limit_order_meta.pop(oid, None)
        if meta is None:
            return
        code = meta.get("code")
        kind = meta.get("kind")
        nc = str(code or "").zfill(6)
        if not nc:
            return
        ok = False
        try:
            ok = bool(self.app.order_manager.cancel_order(oid, nc, "0"))
        except Exception as e:
            self.log(f"⚠️ [미체결타임아웃] 취소 API 예외: {e}")
        try:
            sl = getattr(self.app, "session_logger", None)
            if sl:
                sl.log(
                    "limit_order_unfilled_timeout",
                    {
                        "ord_no": oid,
                        "code": nc,
                        "kind": kind,
                        "cancel_ok": ok,
                    },
                )
        except Exception:
            pass
        if kind == "exit_sell":
            try:
                with self.app.portfolio.lock:
                    p = self.app.portfolio.virtual_positions.get(nc)
                    if p:
                        p["exit_pending"] = False
                        p.pop("pending_exit_ord_no", None)
            except Exception:
                pass
            self.log(
                f"⏱️ [미체결타임아웃] 매도 취소 시도({ok}) — 종목 {nc} 전략 재평가 가능"
            )
        elif kind == "s1_buy":
            self._s1_buy_order_pending_codes.discard(nc)
            self.log(
                f"⏱️ [미체결타임아웃] S1 매수 취소 시도({ok}) — 종목 {nc} 재신호 가능"
            )
        elif kind == "s2_buy":
            try:
                with self.app.portfolio.lock:
                    p = self.app.portfolio.virtual_positions.get(nc)
                    if p:
                        p.pop("s2_limit_order_pending", None)
                        p.pop("s2_pending_ord_no", None)
            except Exception:
                pass
            self.log(
                f"⏱️ [미체결타임아웃] S2 매수 취소 시도({ok}) — 종목 {nc} 재조건 시 재추매 가능"
            )
