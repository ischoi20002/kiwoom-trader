# -*- coding: utf-8 -*-
"""
실시간 체결 틱(호가 제외)을 일자별 JSONL 로 저장합니다.

- 웹소켓 콜백 스레드에서 ``record`` 만 호출(블로킹 최소화).
- 전용 데몬 스레드가 큐에서 배치로 꺼내 파일에 append.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.server_mode_paths import project_root


class RealtimeTickRecorder:
    """
    체결 틱 JSONL 기록기.

    Parameters
    ----------
    app :
        ``settings`` · 로그 콜백용 ``KiwoomApp`` 등.
    """

    def __init__(self, app: Any) -> None:
        self._app = app
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=50_000)
        self._drops: int = 0
        self._last_drop_log_ts: float = 0.0

    def _settings(self) -> Dict[str, Any]:
        try:
            return getattr(self._app, "settings", None) or {}
        except Exception:
            return {}

    def _enabled(self) -> bool:
        try:
            return bool(self._settings().get("realtime_tick_recorder_enabled", False))
        except Exception:
            return False

    def _subdir(self) -> str:
        try:
            s = str(self._settings().get("realtime_tick_recorder_subdir", "realtime_tick_logs"))
            return s.strip() or "realtime_tick_logs"
        except Exception:
            return "realtime_tick_logs"

    def _batch_max(self) -> int:
        try:
            n = int(float(self._settings().get("realtime_tick_recorder_batch_max", 500)))
        except (TypeError, ValueError):
            n = 500
        return max(50, min(n, 5000))

    def _flush_wait_sec(self) -> float:
        try:
            t = float(self._settings().get("realtime_tick_recorder_flush_wait_sec", 0.5))
        except (TypeError, ValueError):
            t = 0.5
        return max(0.1, min(t, 5.0))

    def _mock_suffix(self) -> str:
        try:
            import config as _cfg

            return "mock" if bool(getattr(_cfg, "USE_MOCK", True)) else "live"
        except Exception:
            return "mock"

    def _log(self, msg: str) -> None:
        try:
            fn = getattr(self._app, "log", None)
            if callable(fn):
                fn(msg)
        except Exception:
            pass

    def _ensure_thread(self) -> None:
        if not self._enabled():
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._writer_loop,
                name="RealtimeTickRecorder",
                daemon=True,
            )
            self._thread.start()

    def _out_path(self, day: str) -> Path:
        """``YYYY-MM-DD_trades_{mock|live}.jsonl``"""
        root = Path(project_root())
        sub = self._subdir()
        safe_sub = sub.replace("..", "").strip("/\\") or "realtime_tick_logs"
        d = root / safe_sub
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return d / f"{day}_trades_{self._mock_suffix()}.jsonl"

    def _writer_loop(self) -> None:
        batch_max = self._batch_max()
        wait_sec = self._flush_wait_sec()
        # 종료 후에도 큐가 빌 때까지 drain
        while not self._stop.is_set() or not self._q.empty():
            batch: List[Dict[str, Any]] = []
            deadline = time.monotonic() + wait_sec
            while len(batch) < batch_max:
                if self._stop.is_set() and self._q.empty():
                    break
                timeout = max(0.0, deadline - time.monotonic())
                try:
                    to = min(0.25, timeout) if timeout > 0 else 0.25
                    item = self._q.get(timeout=to)
                    batch.append(item)
                    try:
                        while len(batch) < batch_max:
                            batch.append(self._q.get_nowait())
                    except queue.Empty:
                        pass
                except queue.Empty:
                    break
            if batch:
                self._flush_batch(batch)

    def _flush_batch(self, batch: List[Dict[str, Any]]) -> None:
        """날짜별로 나누어 append (배치 내 날짜 혼재 가능)."""
        by_day: Dict[str, List[str]] = {}
        for row in batch:
            try:
                day = str(row.get("day") or "")[:10]
                if len(day) < 10:
                    day = datetime.now().strftime("%Y-%m-%d")
                line = json.dumps(row, ensure_ascii=False) + "\n"
                by_day.setdefault(day, []).append(line)
            except (TypeError, ValueError):
                continue
        for day, lines in by_day.items():
            path = self._out_path(day)
            try:
                with open(path, "a", encoding="utf-8") as fp:
                    fp.writelines(lines)
            except OSError as e:
                try:
                    self._log(f"⚠️ [TickRec] 파일 쓰기 실패 {path}: {e}")
                except Exception:
                    pass

    def record(
        self,
        code: Any,
        now_price: float,
        volume: float,
        trde_val: float,
        **kwargs: Any,
    ) -> None:
        """
        체결 1건 기록(호가 전용 패킷은 스킵).

        ``volume`` 은 키움 누적거래량(cum) 가정(``SignalEngine`` 과 동일).
        """
        if not self._enabled():
            return
        if bool(kwargs.get("_orderbook_only", False)):
            return
        self._ensure_thread()
        try:
            nk = str(code).split("_")[0].zfill(6)
            now = datetime.now()
            day = now.strftime("%Y-%m-%d")
            row: Dict[str, Any] = {
                "day": day,
                "ts_wall": now.strftime("%Y-%m-%d %H:%M:%S.%f"),
                "recv_mono": float(time.perf_counter()),
                "code": nk,
                "price": float(now_price),
                "cum_volume": float(volume),
                "trde_val": float(trde_val),
            }
        except (TypeError, ValueError):
            return
        try:
            self._q.put_nowait(row)
        except queue.Full:
            self._drops += 1
            ts = time.time()
            if ts - self._last_drop_log_ts >= 30.0:
                self._last_drop_log_ts = ts
                try:
                    self._log(
                        f"⚠️ [TickRec] 큐 포화로 틱 드롭 누적={self._drops} (상한 완화: 설정 realtime_tick_recorder_* )"
                    )
                except Exception:
                    pass

    def shutdown(self) -> None:
        """종료 시 큐 비우고 스레드 종료."""
        try:
            self._stop.set()
        except Exception:
            pass
        th: Optional[threading.Thread] = None
        with self._lock:
            th = self._thread
        if th is not None and th.is_alive():
            try:
                th.join(timeout=5.0)
            except Exception:
                pass
        with self._lock:
            self._thread = None
