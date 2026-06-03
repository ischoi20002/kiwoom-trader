# -*- coding: utf-8 -*-
"""
매수/매도 주문 액션 큐·워커·종목별 직렬화 가드.

KiwoomApp 메인 클래스에서 분리 — SignalEngine 틱 후속 → 워커 → 콜백 실행 경로.
"""
from __future__ import annotations

import queue
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Set, Tuple

BuySignalHandler = Callable[..., bool]
StrategyActionHandler = Callable[..., None]


class OrderActionDispatcher:
    """주문 액션(buy_signal / strategy_action) 비동기 디스패처."""

    def __init__(
        self,
        app: Any,
        *,
        on_buy_signal: BuySignalHandler,
        on_strategy_action: StrategyActionHandler,
        log_func: Optional[Any] = None,
    ) -> None:
        self.app = app
        self._on_buy_signal = on_buy_signal
        self._on_strategy_action = on_strategy_action
        self.log = log_func or print

        self._queue: "queue.SimpleQueue[tuple]" = queue.SimpleQueue()
        self._worker_threads: list[threading.Thread] = []
        self._worker_stop = threading.Event()
        self._guard_lock = threading.Lock()
        self._inflight_codes: Set[str] = set()
        self._last_submit_ts: Dict[Tuple[str, str], float] = {}

    @staticmethod
    def normalize_code(raw_code: Any) -> str:
        """종목코드를 6자리로 정규화합니다."""
        try:
            return str(raw_code).split("_")[0].zfill(6)
        except Exception:
            return str(raw_code)

    def _submit_cooldown_sec(self, kind: str) -> float:
        try:
            st = getattr(self.app, "settings", None) or {}
            if str(kind) == "buy_signal":
                v = float(st.get("buy_signal_submit_cooldown_sec", 0.35))
                return max(0.05, min(v, 5.0))
            v = float(st.get("strategy_action_submit_cooldown_sec", 0.7))
            return max(0.1, min(v, 10.0))
        except Exception:
            return 0.35 if str(kind) == "buy_signal" else 0.7

    def _try_reserve(self, kind: str, code: str) -> bool:
        """종목별 inflight 1건 + kind·code 쿨다운."""
        nk = self.normalize_code(code)
        now_ts = time.time()
        cd = self._submit_cooldown_sec(kind)
        try:
            with self._guard_lock:
                if nk in self._inflight_codes:
                    return False
                last = float(self._last_submit_ts.get((str(kind), nk), 0.0) or 0.0)
                if (now_ts - last) < cd:
                    return False
                self._inflight_codes.add(nk)
                self._last_submit_ts[(str(kind), nk)] = now_ts
                return True
        except Exception:
            return True

    def _release(self, code: str) -> None:
        nk = self.normalize_code(code)
        try:
            with self._guard_lock:
                self._inflight_codes.discard(nk)
        except Exception:
            pass

    def _ensure_workers_started(self) -> None:
        try:
            alive = [t for t in self._worker_threads if t.is_alive()]
            self._worker_threads = alive
            if alive:
                return
            self._worker_stop.clear()
            worker_count = 2
            try:
                st = getattr(self.app, "settings", None) or {}
                worker_count = int(float(st.get("order_action_worker_threads", 2)))
            except Exception:
                worker_count = 2
            worker_count = max(1, min(worker_count, 4))
            for idx in range(worker_count):
                th = threading.Thread(
                    target=self._worker_loop,
                    name=f"OrderActionWorker-{idx + 1}",
                    daemon=True,
                )
                th.start()
                self._worker_threads.append(th)
        except Exception:
            pass

    def enqueue(self, kind: str, payload: Any) -> bool:
        """메인 큐에서 꺼낸 주문 액션을 워커 큐로 위임합니다."""
        try:
            if str(kind) == "buy_signal" and isinstance(payload, tuple) and len(payload) >= 1:
                code = payload[0]
            elif (
                str(kind) == "strategy_action"
                and isinstance(payload, tuple)
                and len(payload) >= 1
            ):
                code = payload[0]
            else:
                return False
            if not self._try_reserve(str(kind), str(code)):
                return False
            self._ensure_workers_started()
            try:
                self._queue.put((str(kind), payload))
            except Exception:
                self._release(str(code))
                return False
            return True
        except Exception:
            return False

    def _worker_loop(self) -> None:
        while not self._worker_stop.is_set():
            try:
                task = self._queue.get_nowait()
            except queue.Empty:
                if self._worker_stop.wait(0.05):
                    break
                continue
            if not isinstance(task, tuple) or len(task) < 2:
                continue
            kind, payload = task[0], task[1]
            code_for_release = ""
            try:
                if str(kind) == "buy_signal" and isinstance(payload, tuple):
                    code_for_release = str(payload[0]) if len(payload) > 0 else ""
                    (
                        bcode,
                        npv,
                        rsi_sig,
                        macd_val,
                        osc_val,
                        ma60_ratio,
                    ) = payload
                    code_for_release = str(bcode)
                    buy_ok = bool(
                        self._on_buy_signal(
                            bcode,
                            float(npv),
                            float(rsi_sig),
                            float(macd_val),
                            float(osc_val),
                            float(ma60_ratio),
                        )
                    )
                    if buy_ok:
                        try:
                            bnk = self.normalize_code(bcode)
                            se = getattr(self.app, "signal_engine", None)
                            if se is not None:
                                se.last_buy_sig_time[bnk] = datetime.now()
                        except Exception:
                            pass
                elif str(kind) == "strategy_action" and isinstance(payload, tuple):
                    code_for_release = str(payload[0]) if len(payload) > 0 else ""
                    scode, sact, sreason, skw = (
                        payload[0],
                        payload[1],
                        payload[2],
                        payload[3] if len(payload) > 3 else {},
                    )
                    if not isinstance(skw, dict):
                        skw = {}
                    self._on_strategy_action(
                        scode,
                        sact,
                        sreason,
                        **skw,
                    )
            except Exception:
                pass
            finally:
                if code_for_release:
                    self._release(code_for_release)

    def shutdown(self) -> None:
        """앱 종료 시 워커를 정지합니다."""
        try:
            self._worker_stop.set()
        except Exception:
            pass
        try:
            for th in list(self._worker_threads):
                try:
                    if th.is_alive():
                        th.join(timeout=1.5)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self._worker_threads = []
        except Exception:
            pass
        try:
            self._worker_stop.clear()
        except Exception:
            pass
