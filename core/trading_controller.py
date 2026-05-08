# -*- coding: utf-8 -*-

import threading
import time
import asyncio
import pandas as pd
import os
import config
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from core.limit_order_watch_service import LimitOrderWatchService
from core.terminal_trace import terminal_trace
from core.warmup_stream_prep_service import WarmupStreamPrepService
from core.websocket_worker_service import WebsocketWorkerService
from utils.normalization import canonical_stock_code


def _implied_price_too_close_to_avg(
    px: float, avg: float, *, tol: float = 0.501
) -> bool:
    """브로커 스냅이 평단과 사실상 같으면 '현재가'로 쓰지 않기 위한 판별."""
    try:
        if px <= 0 or avg <= 0:
            return False
        return abs(float(px) - float(avg)) < float(tol)
    except (TypeError, ValueError):
        return False


def virtual_position_reference_price_for_monitor(
    pos: Dict[str, Any],
    *,
    order_manager: Optional[Any] = None,
    code: Optional[str] = None,
) -> float:
    """
    가상보유 포지션 dict에서 감시 트리·monitor_info 표시용 참고 현재가를 꺼냅니다.

    보유 탭(`virtual_tab`)과 동일하게 **계좌 동기화(ka10085) 스냅**을 우선합니다.
    다만 `ka_cur_prc`·`평가금액/수량`이 **평균단가와 동일(또는 토클)** 하면 실시간 시세가
    아닌 '평단 복제'인 경우가 많아 **무시**하고 REST로 넘어갑니다.

    그 다음 **REST 호가 현재가(ka10004)** 를 먼저 쓰고, 실패 시에만 **stkinfo(ka10001)**
    (``cur_prc`` 우선·전일 필드 폴백) 으로 둡니다. (역순이면 전일·지연 필드가 ``last_price``
    시드로 들어가 보유 탭 **현재가** 가 어제·그제 종가처럼 보이는 버그가 난다.)
    평균단가(`avg_price`)는 직접 반환하지 않습니다.

    Returns:
        0.0 초과 시 참고가, 없으면 0.0
    """
    try:
        if not isinstance(pos, dict):
            return 0.0
        try:
            avg = float(pos.get("avg_price", 0) or 0)
        except (TypeError, ValueError):
            avg = 0.0
        ka_cur = float(pos.get("ka_cur_prc") or 0)
        if ka_cur > 0 and not _implied_price_too_close_to_avg(ka_cur, avg):
            return ka_cur
        qty = int(pos.get("qty", 0) or 0)
        ka_ev = float(pos.get("ka_evlu_amt") or 0)
        if ka_ev > 0 and qty > 0:
            try:
                impl = float(ka_ev) / float(qty)
            except (TypeError, ValueError, ZeroDivisionError):
                impl = 0.0
            if impl > 0 and not _implied_price_too_close_to_avg(impl, avg):
                return impl
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    if order_manager is not None and code:
        try:
            cod = str(code).strip().zfill(6)
            if cod and cod != "000000":
                fn_cur = getattr(order_manager, "fetch_stock_cur_prc_rest", None)
                if callable(fn_cur):
                    try:
                        cp = fn_cur(cod)
                        if cp is not None and float(cp) > 0:
                            return float(cp)
                    except Exception:
                        pass
                fn_pc = getattr(order_manager, "fetch_stock_prev_close_ka10001", None)
                if callable(fn_pc):
                    try:
                        pc = fn_pc(cod, silent=True)
                        if pc is not None and float(pc) > 0:
                            return float(pc)
                    except Exception:
                        pass
        except Exception:
            pass
    return 0.0


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
        # 지정가 미체결 타임아웃 감시/취소/플래그 정리를 서비스로 위임
        self.limit_order_watch_service = LimitOrderWatchService(
            self.app, log_func=self.log
        )
        # 기존 코드 호환용 alias (execute_buy_signal 내부 중복매수 방지)
        self._limit_order_meta = self.limit_order_watch_service.limit_order_meta
        self._s1_buy_order_pending_codes = (
            self.limit_order_watch_service.s1_buy_pending_codes
        )
        # D+2로 1주도 못 살 때 Skip 로그 — 틱 반복 방지( epoch 마지막 출력 시각 )
        self._buy_skip_d2_one_lot_log_at: Dict[str, float] = {}
        # EXIT 주문 실패 시 매 틱 동기 REST 재시도로 메인 UI가 멈추는 현상 방지용 쿨다운
        self._exit_retry_not_before: Dict[str, float] = {}
        self._exit_retry_skip_log_at: Dict[str, float] = {}
        # _run_start_sequence 가 백그라운드에서 도는 동안 중복 start() 방지
        self._run_start_sequence_active: bool = False
        # stop() 호출 시 워밍업 직후 단계(WS 기동 등)를 건너뛰기 위함
        self._abort_monitor_start: bool = False

        # 워밍업/스트림 준비를 담당하는 서비스 추출
        self.warmup_stream_prep_service = WarmupStreamPrepService(
            self, log_func=self.log
        )

        # 웹소켓 워커 래퍼 서비스 추출
        self.websocket_worker_service = WebsocketWorkerService(
            self.app, log_func=self.log
        )

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
            try:
                terminal_trace(
                    "감시 start() → 백그라운드 스레드 spawn (_run_start_sequence_guarded)",
                    app=self.app,
                    component="StartSeq",
                )
            except Exception:
                pass
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
        # 워밍업·감시 외 구간에서 틱 워커가 bars_data 를 건드리지 않도록 먼저 정지합니다.
        try:
            tds = getattr(self.app, "tick_dispatch_service", None)
            if tds is not None:
                tds.shutdown()
        except Exception:
            pass
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
            try:
                terminal_trace(
                    "_run_start_sequence_guarded 진입 (warmup_stream_prep_service.run)",
                    app=self.app,
                    component="StartSeq",
                )
            except Exception:
                pass
            self._run_start_sequence()
        except Exception as e:
            try:
                self.log(f"❌ [Error] 시작 시퀀스(보호 래퍼) 실패: {e}")
            except Exception:
                pass
            try:
                terminal_trace(
                    f"_run_start_sequence_guarded 예외: {e!r}",
                    app=self.app,
                    component="StartSeq",
                )
            except Exception:
                pass
        finally:
            try:
                terminal_trace(
                    "_run_start_sequence_guarded finally → _run_start_sequence_active=False",
                    app=self.app,
                    component="StartSeq",
                )
            except Exception:
                pass
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
            raw_c = row.get("Code", row.get("code", ""))
            c = canonical_stock_code(raw_c)
            if not c:
                continue
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
        vp: List[str] = []
        try:
            vpd = getattr(self.app.portfolio, "virtual_positions", None) or {}
            # 보유 키가 443060_2 처럼 접미사가 있으면 zfill(6)만으로는 정규화되지 않아
            # 워밍업 API·트리 iid(6자리)와 불일치 → 검증 UI·틱 경로가 어긋남
            for c_raw in vpd.keys():
                try:
                    nk = str(c_raw).split("_")[0].zfill(6)
                except Exception:
                    continue
                if nk not in vp:
                    vp.append(nk)
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
                # 트리 iid·REG·워밍업은 항상 6자리 종목코드 — VP 키 접미(443060_2)는 베이스만 사용
                nk = str(c_raw).split("_")[0].zfill(6)
                if nk in self.monitor_info:
                    continue
                nm = pos.get("name") or nk
                ap = float(pos.get("avg_price", 0) or 0)
                ref_p = virtual_position_reference_price_for_monitor(
                    pos,
                    order_manager=getattr(self.app, "order_manager", None),
                    code=nk,
                )
                self.monitor_info[nk] = {
                    "name": nm,
                    "ma20": 0.0,
                    "ma60": 0.0,
                    "prev_vol": 0.0,
                    "score": 0.0,
                    "ma_buffer_scale": 1.0,
                    "open": ap,
                    "last_price": float(ref_p),
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
            try:
                terminal_trace(
                    f"_apply_warmup_ready_stock code={code} n_ready={n_ready} "
                    f"→ status_cb STARTED",
                    app=self.app,
                    component="StartSeq",
                )
            except Exception:
                pass
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
                        target=self.websocket_worker_service.worker_loop, daemon=True
                    )
                    self.monitor_thread.start()

            mt = getattr(self.app, "monitor_tab", None)
            if mt is not None:
                mt._pending_reg_refresh = True
        except Exception as e:
            self.log(f"⚠️ [System] 준비 종목 스트림 반영 실패: {e}")

    def _run_start_sequence(self):
        """워밍업부터 웹소켓 연결까지 마스터 시퀀스 (서비스 위임)."""
        try:
            self.warmup_stream_prep_service.run()
        except Exception as e:
            self.log(f"❌ [Error] 시작 시퀀스 실패: {e}")
            self.stop()

    def _ws_worker_loop(self):
        """(호환) 기존 동작을 유지하되 websocket_worker_service 위임."""
        try:
            self.websocket_worker_service.worker_loop()
        except Exception:
            pass

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

    def _narrow_monitor_sel_track_after_s1_buy(self, code: object, reason: object) -> None:
        """
        선정 단계에서 SelTrack 이 AB(또는 A+B)인 종목이 실제 S1 매수(TYPE_A/B)로 나가면
        감시 메타의 sel_track 을 단일 A 또는 B 로 맞춰 이후 UI·스마트매수 분기와 일치시킵니다.
        """
        try:
            nk = str(code or "").split("_")[0].zfill(6)
            rs = str(reason or "").upper()
            if "TYPE_B" in rs:
                new_tr = "B"
            elif "TYPE_A" in rs:
                new_tr = "A"
            else:
                return

            def _patch(mi: Any) -> None:
                if not isinstance(mi, dict):
                    return
                for key in list(mi.keys()):
                    if str(key).split("_")[0].zfill(6) != nk:
                        continue
                    ent = mi.get(key)
                    if not isinstance(ent, dict):
                        continue
                    cur = str(
                        ent.get("sel_track") or ent.get("SelTrack") or ""
                    ).strip().upper()
                    if cur not in ("AB", "A+B", ""):
                        continue
                    ent["sel_track"] = new_tr
                    try:
                        ent["SelTrack"] = new_tr
                    except Exception:
                        pass

            _patch(getattr(self.app, "monitor_info", None))
            _patch(getattr(self, "monitor_info", None))
        except Exception:
            pass

    def execute_buy_signal(self, code, name, price, reason="TYPE_A") -> bool:
        """[v18.0/v4.5.4] 1단계 진입 시그널에 따른 매매 집행 (실전/가상 통합).
        Returns True iff 주문 접수(ord_no) 또는 가상 체결까지 성공 — SignalEngine 쿨타임 갱신용.
        """
        # 지정가 매수: 실제 주문 호가 = 참고가+1틱 — 로그·JSONL의 price는 주문가 기준으로 통일
        try:
            adj_price = int(
                self.app.order_manager.get_adjusted_price(float(price), "buy")
            )
        except Exception:
            try:
                adj_price = int(float(price))
            except Exception:
                adj_price = 0

        try:
            sl = getattr(self.app, "session_logger", None)
            if sl:
                sl.log(
                    "buy_execute_start",
                    {
                        "code": code,
                        "name": name,
                        "price": float(adj_price),
                        "ref_price": float(price),
                        "reason": reason,
                    },
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
        # invest_amt는 D+2/(1+매수수수료율) 이하의 매수 원금 상한이다. (portfolio._max_buy_principal_cash_d2_limit)
        # adj_price(호가+1틱) 기준으로 산출한 주수가 0이면, 1주도 살 돈이 없는 것이므로
        # max(1, ...)로 1주를 강제하면 D+2 예수금이 마이너스(미수)로 간다 — 절대 금지.
        try:
            qty = int(invest_amt // int(adj_price))
        except (TypeError, ValueError, ZeroDivisionError):
            qty = 0
        if qty <= 0:
            try:
                d2v = float(getattr(self.app.portfolio, "d2_deposit", 0) or 0)
            except (TypeError, ValueError):
                d2v = 0.0
            try:
                try:
                    nc_key = str(code).zfill(6)
                except Exception:
                    nc_key = "000000"
                lk = f"{nc_key}:d2_one_lot"
                _now = time.time()
                _last = float(self._buy_skip_d2_one_lot_log_at.get(lk, 0.0) or 0.0)
                if _now - _last >= 180.0:
                    self._buy_skip_d2_one_lot_log_at[lk] = _now
                    self.log(
                        f"⚠️ [Skip] {name}({code}) S1 매수 생략: D+2 현금 한도로 "
                        f"지정가 기준 1주({adj_price:,}원)도 살 수 없음 "
                        f"(투자원금 상한 {invest_amt:,}원, D+2≈{d2v:,.0f}원). "
                        f"고가주·잔고 부족 시 발생합니다."
                    )
            except Exception as _log_e:
                try:
                    self.log(
                        f"⚠️ [Skip] {name}({code}) S1 매수 생략(D+2 한도, 1주 불가) — 로그 처리 오류: {_log_e}"
                    )
                except Exception:
                    pass
            try:
                sl = getattr(self.app, "session_logger", None)
                if sl:
                    sl.log(
                        "buy_skipped",
                        {
                            "code": code,
                            "name": name,
                            "reason": "d2_insufficient_for_one_lot",
                            "invest_amt": int(invest_amt),
                            "adj_price": int(adj_price),
                            "d2_deposit": d2v,
                        },
                    )
            except Exception:
                pass
            return False

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

        self.log(
            f"🚀 [Buy Signal] {name}({code}) {reason} 진입 시도 "
            f"(주문가 {adj_price:,}원, 참고가 {int(price):,}원, {qty}주)"
        )

        try:
            self._narrow_monitor_sel_track_after_s1_buy(code, reason)
        except Exception:
            pass

        # 4. 텔레그램 알림 (비동기)
        import threading
        msg = (
            f"<b>💰 [매수발송/{reason}]</b>\n종목: {name}({code})\n"
            f"주문가: {adj_price:,}원 (참고가 {int(price):,}원) / 수: {qty}주\n"
            f"시간: {datetime.now().strftime('%H:%M:%S')}"
        )
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

        if action == "EXIT":
            # 지정가 매도: 실제 주문 호가 = 참고가−1틱 — 로그·JSONL은 주문가 기준으로 매수와 동일하게 통일
            try:
                exit_ord_price = int(
                    self.app.order_manager.get_adjusted_price(float(now_price), "sell")
                )
            except Exception:
                try:
                    exit_ord_price = int(float(now_price))
                except Exception:
                    exit_ord_price = 0
            try:
                ref_px = int(float(now_price or 0))
            except Exception:
                ref_px = 0

            # 이미 매도 주문 접수 대기 중이면 중복 EXIT 제출 금지
            try:
                if bool(pos.get("exit_pending", False)):
                    return
            except Exception:
                pass

            # [Audit] Exit Execution — 주문 실패 시 exit_pending 미설정(재평가·재시도 가능)
            if allow_order:
                # 주문 거부/네트워크 지연 시 매 틱 동기 place_order를 반복하면 Tk 메인루프가 장시간 점유됩니다.
                # 실패 후 짧은 쿨다운 동안은 재제출을 스킵해 UI 프리징을 완화합니다.
                nk = str(code).zfill(6)
                now_epoch = time.time()
                try:
                    not_before = float(
                        self._exit_retry_not_before.get(nk, 0.0) or 0.0
                    )
                except Exception:
                    not_before = 0.0
                if now_epoch < not_before:
                    try:
                        last_skip = float(
                            self._exit_retry_skip_log_at.get(nk, 0.0) or 0.0
                        )
                    except Exception:
                        last_skip = 0.0
                    if now_epoch - last_skip >= 5.0:
                        self._exit_retry_skip_log_at[nk] = now_epoch
                        try:
                            wait_s = max(0.0, not_before - now_epoch)
                            self.log(
                                f"ℹ️ [Exit Signal/{reason}] {pos.get('name', code)}({nk}) "
                                f"재시도 쿨다운 {wait_s:.1f}s 남음 — 동기 재주문 스킵"
                            )
                        except Exception:
                            pass
                    return

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
                            "price": int(exit_ord_price),
                            "ref_price": float(ref_px),
                            "result": "submitted" if ord_no else "failed",
                            "order_no": ord_no or "",
                            "reason": f"EXIT_{reason}",
                        }
                        _os.update(self._supply_log_fields(code))
                        sl.log("order_sell", _os)
                except Exception:
                    pass
                if ord_no:
                    # 접수 성공 시 실패 쿨다운 상태 초기화
                    try:
                        self._exit_retry_not_before.pop(nk, None)
                        self._exit_retry_skip_log_at.pop(nk, None)
                    except Exception:
                        pass
                    pos["exit_pending"] = True
                    # 주문 로그 `reason` 컬럼과 동일 문자열 — 종료내역·on_fill과 정합
                    pos["exit_reason"] = f"EXIT_{reason}"
                    pos["pending_exit_ord_no"] = str(ord_no).strip()
                    # 동기화로 로컬 보유가 사라져도 체결 시 종료 확정할 수 있게 접수 직후 스냅샷 저장
                    try:
                        self.app.portfolio.register_pending_exit_snapshot(
                            str(ord_no).strip(),
                            code,
                            pos,
                            f"EXIT_{reason}",
                        )
                    except Exception as ex:
                        self.log(f"⚠️ [Exit] 매도 스냅샷 등록 실패: {ex}")
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
                        f"🚨 [Exit Signal/{reason}] {pos['name']}({code}) 매도 접수 "
                        f"({profit_ratio:+.2f}%) 주문가 {exit_ord_price:,}원 · 참고가 {ref_px:,}원"
                    )
                    import threading
                    msg = (
                        f"<b>🚀 [매도발송/{reason}]</b>\n종목: {pos['name']}({code})\n"
                        f"주문가: {exit_ord_price:,}원 (참고가 {ref_px:,}원)\n"
                        f"수익률: {profit_ratio:+.1f}%\n시간: {datetime.now().strftime('%H:%M:%S')}"
                    )
                    threading.Thread(
                        target=self.app.send_telegram_message, args=(msg,), daemon=True
                    ).start()
                else:
                    # 실패 시 짧은 쿨다운(기본 8초) 적용
                    try:
                        st = getattr(self.app, "settings", None) or {}
                        cd = float(st.get("exit_order_retry_cooldown_sec", 8.0))
                    except Exception:
                        cd = 8.0
                    cd = max(1.0, min(cd, 120.0))
                    try:
                        self._exit_retry_not_before[nk] = time.time() + cd
                    except Exception:
                        pass
                    self.log(
                        f"⚠️ [Exit Signal/{reason}] {pos['name']}({code}) 매도 주문 실패 "
                        f"— {cd:.1f}s 후 재시도 ({profit_ratio:+.2f}%)"
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
                    code,
                    "sell",
                    now_price,
                    pos["qty"],
                    name=pos.get("name"),
                    reason=f"EXIT_{reason}",
                    snapshot=snapshot,
                )
                self.log(
                    f"🚨 [Exit Signal/{reason}] {pos['name']}({code}) 가상 청산 "
                    f"({profit_ratio:+.2f}%) 주문가 {exit_ord_price:,}원 · 참고가 {ref_px:,}원"
                )
                import threading
                msg = (
                    f"<b>🚀 [매도발송/{reason}]</b>\n종목: {pos['name']}({code})\n"
                    f"주문가: {exit_ord_price:,}원 (참고가 {ref_px:,}원)\n"
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
                            f"🔥 [Scale-in] {pos['name']}({code}) 2차 추매 접수 "
                            f"(주문가 {adj_price:,}원, 참고가 {int(now_price):,}원, {add_qty}주)"
                        )
                        import threading
                        msg = (
                            f"<b>🔥 [추매발송/2차]</b>\n종목: {pos['name']}({code})\n"
                            f"주문가: {adj_price:,}원 (참고가 {int(now_price):,}원)\n"
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
        """settings.json `limit_order_unfilled_timeout_sec` — 서비스에서 사용."""
        try:
            return self.limit_order_watch_service._get_timeout_ms()
        except Exception:
            return 5000

    def clear_limit_order_watch(self, order_no: Optional[str]) -> None:
        """체결 등으로 주문이 종료되면 타이머·대기 플래그를 정리합니다."""
        try:
            self.limit_order_watch_service.clear(order_no)
        except Exception:
            # 컨트롤러 호출은 UI/체결 이벤트에서 발생 가능 → 전체 중단 방지
            pass

    def _register_limit_order_watch(
        self,
        order_no: str,
        code: str,
        kind: str,
        qty: int = 0,
    ) -> None:
        """지정가 접수 후 미체결 타임아웃 시 취소 API 및 상태 정리(재판정 가능)."""
        try:
            self.limit_order_watch_service.register(
                order_no=order_no, code=code, kind=kind, qty=qty
            )
        except Exception as e:
            try:
                self.log(f"⚠️ [지정가] 미체결 감시 등록 실패: {e}")
            except Exception:
                pass

    def _handle_limit_order_timeout(self, order_no: str) -> None:
        """미체결 타임아웃: 서비스 위임(취소 API 호출 후 플래그 해제)."""
        try:
            self.limit_order_watch_service.timeout(order_no)
        except Exception:
            pass
