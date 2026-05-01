# -*- coding: utf-8 -*-
from typing import Any, Dict, Optional, Tuple
import threading


class RealtimeTickDispatchService:
    """
    실시간 틱 수신 → Tk 메인 스레드 배치 디스패치 서비스.

    - 콜백 스레드에서 tick을 누적하고, 한 프레임당 마지막 tick만 유지
    - Tk 메인 스레드에서 일괄 처리하여 SignalEngine 호출 타이밍을 안정화
    """

    def __init__(self, app: Any, signal_engine: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.signal_engine = signal_engine
        self.log = log_func or print

        self._lock = threading.RLock()
        self._rt_tick_latest: Dict[str, Tuple[Any, ...]] = {}
        self._tick_flush_after_id: Optional[Any] = None

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
        - Tk root가 있으면 누적 후 after로 flush 예약
        - root가 없으면 즉시 SignalEngine 호출(호환/테스트용)
        """
        try:
            root = self._get_root()
            if root is None:
                self.signal_engine.on_tick_received(
                    code, now_price, volume, trde_val, **dict(kwargs)
                )
                return

            kw = dict(kwargs)
            key = str(code).zfill(6)
            with self._lock:
                self._rt_tick_latest[key] = (code, now_price, volume, trde_val, kw)

                if self._tick_flush_after_id is None:
                    self._tick_flush_after_id = root.after(
                        16, self.flush_on_main
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

    def flush_on_main(self) -> None:
        """Tk 메인 스레드에서 누적 tick을 일괄 처리."""
        try:
            with self._lock:
                batch = dict(self._rt_tick_latest)
                self._rt_tick_latest.clear()
            for _k, tup in batch.items():
                try:
                    c, np0, vol, tv, kw = tup
                    self.signal_engine.on_tick_received(c, np0, vol, tv, **kw)
                except Exception as e:
                    try:
                        self.log(f"⚠️ [Tick] 처리 예외: {e}")
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            # 새 tick이 추가되었으면 다음 프레임 flush 예약
            try:
                with self._lock:
                    self._tick_flush_after_id = None
                    has_more = bool(self._rt_tick_latest)
                if has_more:
                    root = self._get_root()
                    if root is not None:
                        self._tick_flush_after_id = root.after(
                            16, self.flush_on_main
                        )
            except Exception:
                self._tick_flush_after_id = None

