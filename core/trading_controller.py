# -*- coding: utf-8 -*-

import threading
import time
import asyncio
import pandas as pd
import os
import config
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from core.terminal_trace import terminal_trace
from core.warmup_stream_prep_service import WarmupStreamPrepService
from core.websocket_worker_service import WebsocketWorkerService
from utils.normalization import canonical_stock_code
from utils.monitor_vp_stub import stamp_vp_price_stub


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
    allow_rest: bool = True,
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
    if allow_rest and order_manager is not None and code:
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
        # D+2로 1주도 못 살 때 Skip 로그 — 틱 반복 방지( epoch 마지막 출력 시각 )
        # (주문·미체결 감시는 OrderExecutionService 가 담당)
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
        try:
            bw = getattr(self.app, "rt_15m_slot_boundary_watcher", None)
            if bw is not None:
                bw.stop()
        except Exception:
            pass
        try:
            poller = getattr(self.app, "rt_watchlist_m1_api_poller", None)
            if poller is not None:
                poller.reset_round_state()
        except Exception:
            pass
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
        """감시 종목 ∪ 관심목록 ∪ 가상 보유 (순서 유지·중복 제거). 실계좌 보유는 포함하지 않음."""
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
                # 메인 스레드(체결·포지션 탭 렌더)에서 동기 REST 금지.
                # 틱이 오기 전(장 시작 전)에는 bars_data의 마지막 Close(대개 전일종가)로 시드하고,
                # 이후 k_trader_gui._refresh_vp_price_stubs_after_auth()가 백그라운드 REST로 last_price를 교체합니다.
                ref_p = 0.0
                try:
                    bd = getattr(self.app, "bars_data", None)
                    if isinstance(bd, dict):
                        df = bd.get(nk)
                        if df is not None and not getattr(df, "empty", True):
                            lr = df.iloc[-1]
                            # calculate_indicators 계열에서 'Close'를 사용하므로 여기서도 우선합니다.
                            close_val = lr.get("Close") if hasattr(lr, "get") else None
                            if close_val is None:
                                # 혹시 컬럼명이 다른 경우를 위한 보조 폴백
                                close_val = lr.get("close") if hasattr(lr, "get") else None
                            if close_val is not None:
                                ref_p = float(close_val) if float(close_val) > 0 else 0.0
                except Exception:
                    ref_p = 0.0
                if ref_p <= 0 and ap > 0:
                    # bars_data가 아직 없거나 비어 있으면, 평단으로 마지막 수단 시드(기존 동작 유지).
                    ref_p = float(ap)
                row_mi: Dict[str, Any] = {
                    "name": nm,
                    "ma20": 0.0,
                    "ma60": 0.0,
                    "prev_vol": 0.0,
                    "score": 0.0,
                    "ma_buffer_scale": 1.0,
                    "open": ref_p if ref_p > 0 else ap,
                    "last_price": ref_p,
                    "sel_track": "AB",
                }
                stamp_vp_price_stub(
                    row_mi,
                    settings=getattr(self.app, "settings", None),
                )
                self.monitor_info[nk] = row_mi
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

            try:
                bw = getattr(self.app, "rt_15m_slot_boundary_watcher", None)
                if bw is not None and n_ready == 1:
                    bw.start()
            except Exception:
                pass

            self.is_monitoring = True
            if n_ready == 1:
                try:
                    from core.krx_market_halt_slots import purge_stale_market_halt_slots

                    n_purged = purge_stale_market_halt_slots(self.app)
                    if n_purged > 0:
                        self.log(
                            f"ℹ️ [시장중단] 전일 이전 슬롯 {n_purged}건 정리"
                        )
                except Exception:
                    pass
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
                try:
                    n_sel = len(self.monitor_codes or [])
                except Exception:
                    n_sel = 0
                self.status_cb(
                    "STARTED",
                    {
                        "n_selected": int(n_sel),
                        "n_stream": int(n_ready),
                        "incremental": True,
                    },
                )

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
        """OrderExecutionService 위임 — S1 매수 집행."""
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is not None:
                return bool(oes.execute_buy_signal(code, name, price, reason=reason))
        except Exception:
            pass
        return False

    def handle_strategy_action(self, code, action, reason, **kwargs):
        """OrderExecutionService 위임 — EXIT/STAGE2 집행."""
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is not None:
                oes.handle_strategy_action(code, action, reason, **kwargs)
        except Exception:
            pass

    @property
    def limit_order_watch_service(self):
        """호환: 미체결 감시는 OrderExecutionService 가 소유."""
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is not None:
                return oes.limit_order_watch_service
        except Exception:
            pass
        return None

    @property
    def _limit_order_meta(self):
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is not None:
                return oes._limit_order_meta
        except Exception:
            pass
        return {}

    @property
    def _s1_buy_order_pending_codes(self):
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is not None:
                return oes.s1_buy_order_pending_codes
        except Exception:
            pass
        return set()

    def clear_limit_order_watch(self, order_no: Optional[str]) -> None:
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is not None:
                oes.clear_limit_order_watch(order_no)
        except Exception:
            pass

    def _register_limit_order_watch(self, *args, **kwargs) -> None:
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is not None:
                oes._register_limit_order_watch(*args, **kwargs)
        except Exception:
            pass

    def _handle_limit_order_timeout(self, order_no: str) -> None:
        try:
            oes = getattr(self.app, "order_execution_service", None)
            if oes is not None:
                oes._handle_limit_order_timeout(order_no)
        except Exception:
            pass
