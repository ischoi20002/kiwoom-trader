# -*- coding: utf-8 -*-
"""
실시간 틱 수신 → 배치 처리 서비스.

- 기본: Tk 메인에서 `after`로 flush 후 SignalEngine 호출(레거시).
- `use_realtime_tick_worker_thread=True`: 무거운 틱 처리는 전용 데몬 워커에서 수행하고,
  매수·청산·트리/가상탭 갱신만 메인 스레드 큐(`KiwoomApp._drain_tick_worker_main_queue`)로 넘깁니다.
"""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.terminal_trace import terminal_trace


class RealtimeTickDispatchService:
    """실시간 틱 수신 → 배치 디스패치(메인 또는 워커 스레드)."""

    def __init__(self, app: Any, signal_engine: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.signal_engine = signal_engine
        self.log = log_func or print

        self._lock = threading.RLock()
        self._rt_tick_latest: Dict[str, Tuple[Any, ...]] = {}
        self._tick_flush_after_id: Optional[Any] = None
        self._tick_flush_trace_count: int = 0

        self._worker_stop = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        # 무제한 큐: 틱 드롭보다 메모리 압박이 나을 때만 설정으로 워커 끔(use_realtime_tick_worker_thread=False)
        self._worker_batch_queue: "queue.SimpleQueue[List[Tuple[Any, ...]]]" = (
            queue.SimpleQueue()
        )

        # 메인 스레드에서 SignalEngine 호출이 너무 오래 걸리면 Tk 이벤트 루프가 막혀
        # 화면이 멈춘 것처럼 보일 수 있음 → 긴 처리만 진단 로그로 남김
        self._slow_tick_last_ts: Dict[str, float] = {}
        self._slow_tick_log_cooldown_sec: float = 20.0
        self._slow_tick_threshold_sec: float = 0.50

    def _use_worker_thread(self) -> bool:
        try:
            st = getattr(self.app, "settings", None) or {}
            return bool(st.get("use_realtime_tick_worker_thread", True))
        except Exception:
            return True

    def _monitoring_active(self) -> bool:
        """
        실시간 감시가 켜진 뒤에만 틱 워커를 사용합니다.
        워밍업 중(is_monitoring=False)에는 워커가 bars_data·monitor_info를 건드리면
        워밍업 루프와 경쟁해 완료가 멈추거나 데이터가 꼬일 수 있습니다.
        """
        try:
            return bool(getattr(self.app, "is_monitoring", False)) or bool(
                getattr(
                    getattr(self.app, "trading_controller", None),
                    "is_monitoring",
                    False,
                )
            )
        except Exception:
            return False

    def shutdown(self) -> None:
        """감시 중단·앱 종료 시 워커 스레드를 정지하고 대기 배치를 비웁니다."""
        try:
            self._worker_stop.set()
        except Exception:
            pass
        try:
            wt = self._worker_thread
            if wt is not None and wt.is_alive():
                wt.join(timeout=2.5)
        except Exception:
            pass
        try:
            while True:
                try:
                    self._worker_batch_queue.get_nowait()
                except queue.Empty:
                    break
        except Exception:
            pass
        try:
            self._worker_batch_queue = queue.SimpleQueue()
        except Exception:
            pass
        try:
            self._worker_stop.clear()
        except Exception:
            pass
        try:
            self._worker_thread = None
        except Exception:
            pass

    def _ensure_worker_started(self) -> None:
        if not self._use_worker_thread():
            return
        try:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._worker_stop.clear()
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="RealtimeTickWorker",
                daemon=True,
            )
            self._worker_thread.start()
        except Exception as e:
            try:
                self.log(f"⚠️ [Tick] 워커 스레드 시작 실패(메인 처리로 폴백): {e}")
            except Exception:
                pass

    def _worker_loop(self) -> None:
        """배치 단위로 SignalEngine 을 워커에서 호출합니다."""
        max_per_batch = 6
        try:
            st = getattr(self.app, "settings", None) or {}
            max_per_batch = int(
                float(st.get("realtime_tick_worker_max_ticks_per_batch", 6))
            )
        except (TypeError, ValueError):
            max_per_batch = 6
        max_per_batch = max(1, min(max_per_batch, 24))
        while not self._worker_stop.is_set():
            try:
                batch_list = self._worker_batch_queue.get_nowait()
            except queue.Empty:
                if self._worker_stop.wait(0.05):
                    break
                continue
            if not batch_list:
                continue
            head = batch_list[:max_per_batch]
            tail = batch_list[max_per_batch:]
            if tail:
                try:
                    self._worker_batch_queue.put(tail)
                except Exception:
                    pass
            for i, tup in enumerate(head):
                if self._worker_stop.is_set():
                    break
                try:
                    c, np0, vol, tv, kw = tup
                    self.signal_engine.on_tick_received(
                        c, np0, vol, tv, defer_main_callbacks=True, **dict(kw)
                    )
                except Exception as e:
                    try:
                        self.log(f"⚠️ [Tick] 워커 처리 예외: {e}")
                    except Exception:
                        pass
                if i % 2 == 1:
                    try:
                        time.sleep(0.001)
                    except Exception:
                        pass
            try:
                self.app._ensure_tick_main_action_poller()
            except Exception:
                pass

    def _tick_flush_interval_ms(self) -> int:
        """
        틱 배치 flush 주기(ms) 결정.

        - 너무 촘촘하면 Tk 메인 스레드가 틱 처리에 과점유되어 UI가 멈춘 것처럼 보일 수 있음
        - 너무 느리면 실시간성 저하(현재 사용자는 1초 지연 허용)
        """
        try:
            app_val = getattr(self.app, "REALTIME_TICK_DISPATCH_FLUSH_MS", None)
            if isinstance(app_val, (int, float)) and app_val and app_val > 0:
                ms = int(app_val)
            else:
                st = getattr(self.app, "settings", None) or {}
                ms = int(float(st.get("realtime_tick_flush_ms", 100)))
        except Exception:
            ms = 100

        # 안전 범위: 10ms 미만은 의미 없는 오버헤드, 1000ms 이상은 실시간성 크게 저하
        return max(10, min(ms, 1000))

    def _max_codes_per_tick_slice(self) -> int:
        """
        한 번의 flush 에서 처리할 최대 종목 수.
        전 종목을 한 프레임에서 SignalEngine 에 넣으면 Tk UI가 멈춘 것처럼 보입니다.
        """
        try:
            st = getattr(self.app, "settings", None) or {}
            n = int(float(st.get("realtime_tick_flush_max_codes_per_slice", 12)))
        except (TypeError, ValueError):
            n = 12
        return max(1, min(n, 80))

    def _slice_yield_ms(self) -> int:
        """잔여 틱이 있을 때 다음 슬라이스까지 대기(ms). 0~50 권장."""
        try:
            st = getattr(self.app, "settings", None) or {}
            ms = int(float(st.get("realtime_tick_flush_slice_yield_ms", 1)))
        except (TypeError, ValueError):
            ms = 1
        return max(0, min(ms, 50))

    def _get_root(self) -> Optional[Any]:
        try:
            return getattr(self.app, "root", None)
        except Exception:
            return None

    def on_tick_received(
        self,
        code: Any,
        now_price: float,
        volume: float,
        trde_val: float,
        **kwargs: Any,
    ) -> None:
        """
        웹소켓 콜백 진입점(비메인 스레드 가능).
        - Tk API(root.after)는 메인 스레드에서만 호출합니다.
          (비메인 스레드에서 호출 시 UI 교착/응답없음 가능)
        - 비메인 스레드에서는 SignalEngine을 defer_main_callbacks=True로 직접 호출하고,
          UI 반영은 메인 큐 폴러로 위임합니다.
        - root가 없으면 즉시 SignalEngine 호출(호환/테스트용)
        """
        # 검증용 틱→1분: 비차단 put 만 (신호·주문과 격리)
        # 신호용 슬롯 말분 틱 누적: RtTickM1Store.feed_tick (공식14+틱1 합성)
        try:
            skip = bool(kwargs.get("_orderbook_only", False)) or bool(
                kwargs.get("_official_sync_eval", False)
            )
            trade_time = kwargs.get("trade_time")
            px = float(now_price) if now_price is not None else 0.0
            vol = float(volume) if volume is not None else 0.0
            acc = getattr(self.app, "rt_tick_m1_accumulator", None)
            if acc is not None:
                acc.offer_tick(
                    code,
                    px,
                    vol,
                    skip=skip,
                    now=trade_time,
                )
            if not skip:
                m1s = getattr(self.app, "rt_tick_m1_store", None)
                if m1s is not None:
                    try:
                        sealed_lab = m1s.feed_tick(
                            code,
                            px,
                            vol,
                            now=trade_time if isinstance(trade_time, datetime) else None,
                        )
                    except Exception:
                        sealed_lab = None
                    # 다음 분(:00) 체결로 말분이 seal → 즉시 완성15 평가
                    if sealed_lab is not None:
                        try:
                            poller = getattr(
                                self.app, "rt_watchlist_m1_api_poller", None
                            )
                            if poller is not None and hasattr(
                                poller, "on_tick_tail_next_minute"
                            ):
                                poller.on_tick_tail_next_minute(code, sealed_lab)
                        except Exception:
                            pass
        except Exception:
            pass
        try:
            root = self._get_root()
            if root is None:
                self.signal_engine.on_tick_received(
                    code, now_price, volume, trde_val, **dict(kwargs)
                )
                return

            try:
                main_tid = int(getattr(self.app, "_main_thread_id", 0) or 0)
            except Exception:
                main_tid = 0
            is_main_thread = False
            try:
                is_main_thread = threading.get_ident() == main_tid and main_tid != 0
            except Exception:
                is_main_thread = False

            if not is_main_thread:
                # 비메인 스레드에서는 Tk API를 전혀 호출하지 않습니다.
                # heavy 경로는 SignalEngine 내부 스로틀 + main action queue 폴러로 완충합니다.
                try:
                    if self._use_worker_thread() and self._monitoring_active():
                        self._ensure_worker_started()
                        self._worker_batch_queue.put(
                            [
                                (
                                    code,
                                    now_price,
                                    volume,
                                    trde_val,
                                    dict(kwargs),
                                )
                            ]
                        )
                    else:
                        self.signal_engine.on_tick_received(
                            code,
                            now_price,
                            volume,
                            trde_val,
                            defer_main_callbacks=True,
                            **dict(kwargs),
                        )
                except Exception:
                    # 워커 enqueue 실패 시 즉시 계산 경로로 폴백해 틱 유실을 방지합니다.
                    try:
                        self.signal_engine.on_tick_received(
                            code,
                            now_price,
                            volume,
                            trde_val,
                            defer_main_callbacks=True,
                            **dict(kwargs),
                        )
                    except Exception:
                        pass
                try:
                    self.app._ensure_tick_main_action_poller()
                except Exception:
                    pass
                return

            kw = dict(kwargs)
            key = str(code).zfill(6)
            with self._lock:
                self._rt_tick_latest[key] = (code, now_price, volume, trde_val, kw)

                if self._tick_flush_after_id is None:
                    flush_ms = self._tick_flush_interval_ms()
                    self._tick_flush_after_id = root.after(
                        flush_ms, self.flush_on_main
                    )
        except Exception:
            # root가 있는 상황에서도 무조건 실패하면 틱이 영구 소실됩니다.
            # 따라서 fallback으로 즉시 처리합니다.
            try:
                self.signal_engine.on_tick_received(
                    code, now_price, volume, trde_val, **dict(kwargs)
                )
            except Exception:
                pass

    def _dispatch_batch(self, batch: Dict[str, Tuple[Any, ...]]) -> None:
        """배치를 워커 큐에 넣거나 메인에서 직접 처리합니다."""
        if not batch:
            return
        use_w = self._use_worker_thread() and self._monitoring_active()
        if use_w:
            self._ensure_worker_started()
            batch_list: List[Tuple[Any, ...]] = list(batch.values())
            try:
                self._worker_batch_queue.put(batch_list)
            except Exception:
                pass
            try:
                self.app._ensure_tick_main_action_poller()
            except Exception:
                pass
            return

        for _k, tup in batch.items():
            try:
                c, np0, vol, tv, kw = tup
                t0 = time.perf_counter()
                self.signal_engine.on_tick_received(c, np0, vol, tv, **dict(kw))
                dt = time.perf_counter() - t0
                # worker 경로가 아니고, 메인에서 오래 처리되는 경우 Tk 이벤트 루프가 막힘
                if dt >= self._slow_tick_threshold_sec:
                    try:
                        # 코드별 쿨다운으로 로그 폭주 방지
                        cc = str(c).split("_")[0].zfill(6)
                        now_ts = time.time()
                        last = float(self._slow_tick_last_ts.get(cc, 0.0) or 0.0)
                        if now_ts - last >= self._slow_tick_log_cooldown_sec:
                            self._slow_tick_last_ts[cc] = now_ts
                            sl = getattr(self.app, "session_logger", None)
                            if sl is not None:
                                sl.log(
                                    "tick_dispatch_slow",
                                    {"code": cc, "dt_sec": float(dt), "now_price": float(np0)},
                                )
                    except Exception:
                        pass
            except Exception as e:
                try:
                    self.log(f"⚠️ [Tick] 처리 예외: {e}")
                except Exception:
                    pass

    def flush_on_main(self) -> None:
        """Tk 메인 스레드: 누적 tick을 슬라이스로 꺼내 워커에 넘기거나 메인에서 처리."""
        try:
            max_slice = self._max_codes_per_tick_slice()
            with self._lock:
                keys = list(self._rt_tick_latest.keys())
                take = keys[:max_slice]
                batch = {k: self._rt_tick_latest.pop(k) for k in take}
            try:
                nb = len(batch)
                if nb > 0:
                    self._tick_flush_trace_count += 1
                    tc = self._tick_flush_trace_count
                    if tc <= 12 or tc % 200 == 0:
                        terminal_trace(
                            f"TickDispatch flush #{tc} slice={nb} "
                            f"worker={self._use_worker_thread()} "
                            f"keys={list(batch.keys())[:12]!r}",
                            app=self.app,
                            component="Tick",
                        )
            except Exception:
                pass
            self._dispatch_batch(batch)
        except Exception:
            pass
        finally:
            # 잔여 틱이 있으면 짧은 yield 후 이어서 처리 → Tk 이벤트 루프에 숨구멍 제공
            try:
                with self._lock:
                    self._tick_flush_after_id = None
                    has_more = bool(self._rt_tick_latest)
                if has_more:
                    root = self._get_root()
                    if root is not None:
                        yms = self._slice_yield_ms()
                        self._tick_flush_after_id = root.after(
                            yms, self.flush_on_main
                        )
            except Exception:
                self._tick_flush_after_id = None
