# -*- coding: utf-8 -*-
import os
import threading
from typing import Any, Optional

from core.terminal_trace import terminal_trace

# ``WatchlistStore`` 기본 파일명과 동일(경로만 안내용).
_WATCHLIST_JSON_NAME = "watchlist_favorites.json"


class WarmupStreamPrepService:
    """
    감시 시작 전 워밍업 + 스트림 준비를 담당하는 서비스.

    - 기존 `TradingController._run_start_sequence()`의 구현을 옮겨 책임을 분리
    - TradingController는 중복/경로 제어(가드 래퍼)만 유지
    - **증분 스트림**(종목별로 워밍업 직후 WS·틱을 켜는 모드)은 비활성화하고,
      전 종목 워밍업이 끝난 뒤 웹소켓을 한 번 기동해 틱을 **일괄** 수신합니다(UI·메인스레드 안정화).
    """

    def __init__(self, controller: Any, *, log_func: Optional[Any] = None) -> None:
        self.controller = controller
        self.log = log_func or getattr(controller, "log", print)

    def run(self) -> None:
        c = self.controller
        try:
            try:
                terminal_trace(
                    "WarmupStreamPrepService.run() 진입 "
                    f"(thread={threading.current_thread().name!r})",
                    app=c.app,
                    component="WarmupPrep",
                )
            except Exception:
                pass
            c._abort_monitor_start = False
            c.app._active_stream_codes = []
            c._incremental_stream_bootstrapped = False
            try:
                from core.realtime_official_bars_sync import (
                    remember_monitor_inprogress_slot,
                )

                remember_monitor_inprogress_slot(c.app)
            except Exception:
                pass

            # Transition 게이트 FDR 일봉 조회는 수 초~수십 초 걸릴 수 있음.
            # start() 메인 스레드에서 돌리면 UI가 멈춘 것처럼 보이므로 여기서 수행.
            try:
                c._status_cb_on_main(
                    "STATUS_TEXT",
                    "상태: 레짐 게이트 갱신 중…",
                )
            except Exception:
                pass
            try:
                c._refresh_transition_runtime_flags()
            except Exception as e:
                try:
                    c.log(f"⚠️ [Transition] 백그라운드 레짐 갱신 실패: {e}")
                except Exception:
                    pass

            merged = c._merged_warmup_codes()
            c._ensure_watchlist_monitor_info()
            c._ensure_virtual_positions_monitor_info()

            # 일일 선정만 쓰는 것처럼 보여도, 관심·가상보유가 워밍업·틱 수신에 합쳐짐
            try:
                n_mon = len(c.monitor_codes or [])
                n_mg = len(merged)
                if n_mg != n_mon:
                    try:
                        wl_n = len(c.app.watchlist_store.get_codes())
                    except Exception:
                        wl_n = 0
                    try:
                        vp_n = len(
                            getattr(c.app.portfolio, "virtual_positions", None) or {}
                        )
                    except Exception:
                        vp_n = 0
                    wl_path = os.path.join(os.getcwd(), _WATCHLIST_JSON_NAME)
                    c.log(
                        "ℹ️ [System] 실시간 워밍업·틱 수신 대상은 "
                        f"일일 선정 {n_mon}종목 + 관심목록 {wl_n}건 + 가상보유 {vp_n}건을 "
                        f"합친 뒤 중복 제거한 총 {n_mg}종목입니다. "
                        f"(관심 파일: {wl_path})"
                    )
            except Exception:
                pass

            require_full = bool(
                c.app.settings.get("require_full_warmup_for_monitoring", False)
            )
            # 설정 incremental_warmup_stream 과 무관하게 증분 WS/틱은 끔(전체 완료 후 일괄).
            incremental_stream = False
            on_ready = None

            # status_cb 는 Tk 위젯 갱신을 포함할 수 있어 반드시 메인 스레드로 위임합니다.
            c._status_cb_on_main(
                "STATUS_TEXT",
                f"상태: 분석 중 (감시 {len(c.monitor_codes)} + 스트림 합계 {len(merged)})",
            )
            c._status_cb_on_main("WARMUP_BEGIN", list(merged))
            try:
                c.log(
                    "ℹ️ [System] 워밍업: 전 종목 검증이 끝난 뒤 웹소켓·틱을 일괄 시작합니다 "
                    "(증분 스트림 비활성)."
                )
            except Exception:
                pass

            results = c.app.warmup_service.run(
                merged,
                c.monitor_info,
                c.app.bars_data,
                on_stock_ready=on_ready,
            )
            try:
                n_ok = sum(
                    1
                    for cc in merged
                    if isinstance(results.get(cc), dict) and results[cc].get("ok")
                )
                terminal_trace(
                    f"warmup_service.run() 반환 merged={len(merged)} 성공={n_ok} "
                    f"incremental={incremental_stream}",
                    app=c.app,
                    component="WarmupPrep",
                )
            except Exception:
                pass

            if c._abort_monitor_start:
                c.log(
                    "ℹ️ [System] 감시 중단 요청으로 워밍업 이후(접속·등록) 단계를 생략합니다."
                )
                return

            failed = [cc for cc in merged if not results.get(cc, {}).get("ok")]
            if failed:
                if require_full:
                    # 점진 스트림으로 이미 WS가 켜진 경우: 전부 실패 처리 전 감시 중단
                    if getattr(c, "_incremental_stream_bootstrapped", False):
                        try:
                            c.stop()
                        except Exception as se:
                            c.log(
                                f"⚠️ [System] 워밍업 실패 시 감시 중단 처리 오류: {se}"
                            )
                    for cc in merged:
                        try:
                            c.app.bars_data.pop(cc, None)
                        except Exception:
                            pass
                    c._status_cb_on_main(
                        "WARMUP_FAILED",
                        {"results": results, "failed": failed},
                    )
                    c._status_cb_on_main(
                        "STATUS_TEXT",
                        "상태: 워밍업 실패 — 감시 미시작 (신호상태 열 참고)",
                    )
                    c.log(
                        "❌ [System] 워밍업 미달 종목이 있어 실시간 감시를 시작하지 않습니다."
                    )
                    return

                # 완화 모드: 실패 종목만 제외 (미권장)
                for cc in failed:
                    try:
                        c.app.bars_data.pop(cc, None)
                    except Exception:
                        pass
                    try:
                        c.monitor_info.pop(cc, None)
                    except Exception:
                        pass
                c.monitor_codes = [cc for cc in c.monitor_codes if cc not in failed]
                if not [cc for cc in merged if results.get(cc, {}).get("ok")]:
                    c._status_cb_on_main(
                        "WARMUP_FAILED",
                        {"results": results, "failed": failed},
                    )
                    c._status_cb_on_main(
                        "STATUS_TEXT",
                        "상태: 워밍업 실패 — 감시 대상 없음",
                    )
                    c.log("❌ [System] 워밍업 실패로 감시 가능 종목이 없습니다.")
                    return

                c.log(
                    f"⚠️ [System] 워밍업 실패 {len(failed)}종목 제외 후 감시 진행 "
                    f"(require_full_warmup_for_monitoring=False)"
                )
                c._status_cb_on_main(
                    "MONITOR_CODES_FILTERED",
                    {
                        "codes": list(c.monitor_codes),
                        "dropped": failed,
                        "results": results,
                    },
                )

            successful = [cc for cc in merged if results.get(cc, {}).get("ok")]
            c.app._active_stream_codes = successful

            def _notify_started_and_reg_refresh() -> None:
                try:
                    n_sel = len(c.monitor_codes or [])
                except Exception:
                    n_sel = 0
                c._status_cb_on_main(
                    "STARTED",
                    {
                        "n_selected": int(n_sel),
                        "n_stream": int(len(successful)),
                        "incremental": False,
                    },
                )
                mt = getattr(c.app, "monitor_tab", None)
                if mt is not None:
                    mt._pending_reg_refresh = True

            # 메인 스레드의 _apply 가 WS를 띄우기 전에 워커가 여기까지 도달할 수 있음 → 중복 소켓 방지
            ws_already_running = (
                c.monitor_thread is not None and c.monitor_thread.is_alive()
            )
            incremental_ws_ready = incremental_stream and ws_already_running

            if incremental_ws_ready:
                c.log(
                    "✅ [System] 전 종목 15분봉 워밍업 검증 완료. 실시간 구독 목록을 최종 동기화합니다."
                )
                c.is_monitoring = True
                try:
                    c.app.is_monitoring = True
                except Exception:
                    pass
                _notify_started_and_reg_refresh()
                return

            c.log(
                "✅ [System] 전 종목 15분봉 워밍업 검증 완료. 실시간 모니터링을 시작합니다."
            )

            c.is_monitoring = True
            try:
                c.app.is_monitoring = True
            except Exception:
                pass
            _notify_started_and_reg_refresh()

            def _mark_ready_and_catchup() -> None:
                try:
                    from core.realtime_official_bars_sync import mark_tick_stream_ready

                    mark_tick_stream_ready(c.app)
                except Exception:
                    pass
                # 직전 슬롯 재평가 전: 보유·체결·미체결을 맞춰 중복 주문 방지
                try:
                    from core.restart_pre_catchup import (
                        prepare_before_restart_slot_catchup,
                    )

                    prepare_before_restart_slot_catchup(c.app)
                except Exception as ex:
                    try:
                        c.log(f"⚠️ [RestartGuard] catchup 전 가드 실패: {ex}")
                    except Exception:
                        pass
                try:
                    poller = getattr(c.app, "rt_watchlist_m1_api_poller", None)
                    if poller is not None:
                        poller.catchup_eval_inprogress_slot_after_stream_ready()
                except Exception:
                    pass

            # [Step 2] 가상 실천 엔진(WS Worker) 구동 (이미 살아 있으면 재시작하지 않음)
            if ws_already_running:
                c.log(
                    "ℹ️ [System] 웹소켓 워커가 이미 동작 중이어서 추가 기동을 건너뜁니다."
                )
                _mark_ready_and_catchup()
                return

            c._stop_event.clear()
            c.monitor_thread = threading.Thread(
                target=c.websocket_worker_service.worker_loop, daemon=True
            )
            try:
                terminal_trace(
                    "웹소켓 worker_thread.start() 직전 "
                    f"(ws_already_running={ws_already_running})",
                    app=c.app,
                    component="WarmupPrep",
                )
            except Exception:
                pass
            c.monitor_thread.start()
            _mark_ready_and_catchup()

        except Exception as e:
            self.log(f"❌ [Error] 시작 시퀀스 실패: {e}")
            c.stop()

