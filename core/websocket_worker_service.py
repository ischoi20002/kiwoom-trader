# -*- coding: utf-8 -*-
import asyncio
import threading
from typing import Any, Optional


class WebsocketWorkerService:
    """
    TradingController가 생성하는 웹소켓 워커 스레드용 래퍼.

    - Tk 프로세스에서 asyncio 루프를 별도 스레드로 만들고
      `app.monitor_tab._ws_client_loop()`를 실행합니다.
    - 실제 웹소켓 프로토콜 구현은 `MonitorTab._ws_client_loop()`에 있습니다.
    """

    def __init__(self, app: Any, *, log_func: Optional[Any] = None) -> None:
        self.app = app
        self.log = log_func or print

    def worker_loop(self) -> None:
        """새 asyncio event loop을 만들고 websocket client loop을 실행합니다."""
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        try:
            new_loop.run_until_complete(
                self.app.monitor_tab._ws_client_loop()
            )
        except Exception as e:
            try:
                self.log(f"⚠️ [System] 웹소켓 워커 종료: {e}")
            except Exception:
                pass
        finally:
            try:
                new_loop.close()
            except Exception:
                pass

    def start_worker_thread(
        self,
        *,
        thread_holder: Any,
    ) -> Optional[threading.Thread]:
        """
        convenience:
        - caller가 보유한 `monitor_thread` 같은 객체에 스레드를 주입하고 싶을 때 사용
        """
        try:
            t = threading.Thread(target=self.worker_loop, daemon=True)
            t.start()
            return t
        except Exception:
            return None

