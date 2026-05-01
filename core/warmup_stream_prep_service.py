# -*- coding: utf-8 -*-
import threading
from typing import Any, Optional


class WarmupStreamPrepService:
    """
    감시 시작 전 워밍업 + 스트림 준비를 담당하는 서비스.

    - 기존 `TradingController._run_start_sequence()`의 구현을 옮겨 책임을 분리
    - TradingController는 중복/경로 제어(가드 래퍼)만 유지
    """

    def __init__(self, controller: Any, *, log_func: Optional[Any] = None) -> None:
        self.controller = controller
        self.log = log_func or getattr(controller, "log", print)

    def run(self) -> None:
        c = self.controller
        try:
            c._abort_monitor_start = False
            c.app._active_stream_codes = []
            c._incremental_stream_bootstrapped = False

            merged = c._merged_warmup_codes()
            c._ensure_watchlist_monitor_info()
            c._ensure_virtual_positions_monitor_info()

            require_full = bool(
                c.app.settings.get("require_full_warmup_for_monitoring", False)
            )
            incremental_stream = bool(
                c.app.settings.get("incremental_warmup_stream", True)
            )

            if c.status_cb:
                c.status_cb(
                    "STATUS_TEXT",
                    f"상태: 분석 중 (감시 {len(c.monitor_codes)} + 스트림 합계 {len(merged)})",
                )
            if c.status_cb:
                c.status_cb("WARMUP_BEGIN", list(merged))

            on_ready = (
                c._schedule_warmup_stock_ready if incremental_stream else None
            )
            results = c.app.warmup_service.run(
                merged,
                c.monitor_info,
                c.app.bars_data,
                on_stock_ready=on_ready,
            )

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
                    if c.status_cb:
                        c.status_cb(
                            "WARMUP_FAILED",
                            {"results": results, "failed": failed},
                        )
                    if c.status_cb:
                        c.status_cb(
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
                    if c.status_cb:
                        c.status_cb(
                            "WARMUP_FAILED",
                            {"results": results, "failed": failed},
                        )
                    if c.status_cb:
                        c.status_cb(
                            "STATUS_TEXT",
                            "상태: 워밍업 실패 — 감시 대상 없음",
                        )
                    c.log("❌ [System] 워밍업 실패로 감시 가능 종목이 없습니다.")
                    return

                c.log(
                    f"⚠️ [System] 워밍업 실패 {len(failed)}종목 제외 후 감시 진행 "
                    f"(require_full_warmup_for_monitoring=False)"
                )
                if c.status_cb:
                    c.status_cb(
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
                c._status_cb_on_main("STARTED", len(successful))
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

            # [Step 2] 가상 실천 엔진(WS Worker) 구동 (이미 살아 있으면 재시작하지 않음)
            if ws_already_running:
                c.log(
                    "ℹ️ [System] 웹소켓 워커가 이미 동작 중이어서 추가 기동을 건너뜁니다."
                )
                return

            c._stop_event.clear()
            c.monitor_thread = threading.Thread(
                target=c.websocket_worker_service.worker_loop, daemon=True
            )
            c.monitor_thread.start()

        except Exception as e:
            self.log(f"❌ [Error] 시작 시퀀스 실패: {e}")
            c.stop()

