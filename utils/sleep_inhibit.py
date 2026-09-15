# -*- coding: utf-8 -*-
"""Windows 유휴 최대절전(하이버네이트) 억제.

장중·장전 자동시작이 깨지지 않도록 시스템 유휴 타이머를 막는다.
전원 옵션이 「최대 절전」이든 「절전」이든, 유휴로 꺼지는 것을 막는다
(운영은 최대절전 사용을 전제로 한다).

노트북 덮개를 닫으면 전원 옵션(덮개 동작)이 우선하므로 이 API로는 막지 못한다.
모니터만 꺼지는 것은 기본 허용한다.

Away Mode 는 쓰지 않는다 — 해제 누락 시 최대절전이 영구 지연되는 사례가 있다.

중요: PowerRequest 와 SetThreadExecutionState 를 **동시에** 걸지 않는다.
둘 다 걸면 한쪽만 해제돼도 Windows 유휴 타이머가 계속 리셋되어
(최대절전 30분 설정이어도) 몇 시간이 지나도 진입하지 않을 수 있다.
"""
from __future__ import annotations

import atexit
import ctypes
import sys
from typing import Callable, Optional

# SetThreadExecutionState 플래그
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002
_ES_CONTINUOUS = 0x80000000

# PowerRequestType
_POWER_REQUEST_DISPLAY_REQUIRED = 0
_POWER_REQUEST_SYSTEM_REQUIRED = 1

_POWER_REQUEST_CONTEXT_VERSION = 0
_POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_REASON_TEXT = "K-Trader 자동매매 — 최대절전 억제"


class _REASON_CONTEXT(ctypes.Structure):
    """REASON_CONTEXT — SIMPLE_STRING 경로만 사용 (유니온을 문자열 포인터로 축약)."""

    _fields_ = [
        ("Version", ctypes.c_ulong),
        ("Flags", ctypes.c_ulong),
        ("SimpleReasonString", ctypes.c_wchar_p),
    ]


def _kernel32() -> ctypes.WinDLL:
    """64bit HANDLE 마샬링을 위해 동일 WinDLL 인스턴스·argtypes 를 쓴다."""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.SetThreadExecutionState.argtypes = [ctypes.c_uint]
    k32.SetThreadExecutionState.restype = ctypes.c_uint
    k32.PowerCreateRequest.argtypes = [ctypes.POINTER(_REASON_CONTEXT)]
    k32.PowerCreateRequest.restype = ctypes.c_void_p
    k32.PowerSetRequest.argtypes = [ctypes.c_void_p, ctypes.c_int]
    k32.PowerSetRequest.restype = ctypes.c_bool
    k32.PowerClearRequest.argtypes = [ctypes.c_void_p, ctypes.c_int]
    k32.PowerClearRequest.restype = ctypes.c_bool
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.CloseHandle.restype = ctypes.c_bool
    return k32


class SleepInhibitor:
    """메인 스레드에서 acquire/refresh/release 를 호출해야 한다 (스레드별 요청)."""

    def __init__(self, logger: Optional[Callable[[str], None]] = None) -> None:
        self._log = logger
        self._active: bool = False
        self._keep_display: bool = False
        self._power_handle: Optional[int] = None
        self._reason_keep: Optional[str] = None
        self._atexit_registered: bool = False
        # "power" | "exec" | None — 한 가지만 사용
        self._mode: Optional[str] = None
        self._k32: Optional[ctypes.WinDLL] = None

    def _emit(self, msg: str) -> None:
        try:
            if self._log is not None:
                self._log(msg)
        except Exception:
            pass

    def _k32_api(self) -> Optional[ctypes.WinDLL]:
        if sys.platform != "win32":
            return None
        if self._k32 is None:
            try:
                self._k32 = _kernel32()
            except Exception:
                return None
        return self._k32

    @property
    def active(self) -> bool:
        return self._active

    def acquire(self, keep_display: bool = False) -> bool:
        """최대절전(유휴 전원) 억제를 켠다. 이미 켜져 있고 옵션이 같으면 유지."""
        try:
            keep_display = bool(keep_display)
            if self._active and self._keep_display == keep_display:
                # PowerRequest 는 유지형 — 주기 ExecutionState 재적용 불필요
                # (재적용하면 유휴 타이머만 리셋되어 해제 후에도 관측이 헷갈림)
                if self._mode == "exec":
                    self.refresh()
                return True
            # 잔존 핸들·이전 모드 정리 후 재획득
            if self._active or self._power_handle is not None or self._mode is not None:
                self.release()
            if sys.platform != "win32":
                self._emit("⚠️ [최대절전방지] Windows 가 아니라 적용하지 않습니다.")
                return False
            self._keep_display = keep_display

            # 단일 경로: PowerRequest 우선, 실패 시에만 ExecutionState
            ok_power = self._acquire_power_request(keep_display)
            if ok_power:
                self._mode = "power"
            else:
                ok_exec = self._set_execution_state(keep_display)
                if not ok_exec:
                    self._emit(
                        "⚠️ [최대절전방지] Windows API 호출 실패 — "
                        "유휴 최대절전이 그대로일 수 있습니다."
                    )
                    return False
                self._mode = "exec"

            self._active = True
            if not self._atexit_registered:
                try:
                    atexit.register(self.release)
                    self._atexit_registered = True
                except Exception:
                    pass
            disp = "화면 유지" if keep_display else "화면 꺼짐 허용"
            via = "PowerRequest" if self._mode == "power" else "ExecutionState"
            self._emit(
                f"🔆 [최대절전방지] 유휴 최대절전 억제 시작 ({disp}, {via})"
            )
            return True
        except Exception as e:
            self._emit(f"⚠️ [최대절전방지] 시작 실패: {e}")
            return False

    def refresh(self) -> None:
        """ExecutionState 폴백 모드에서만 유휴 타이머를 다시 리셋한다."""
        if not self._active or self._mode != "exec":
            return
        try:
            if sys.platform != "win32":
                return
            self._set_execution_state(self._keep_display)
        except Exception:
            pass

    def release(self) -> None:
        """최대절전 억제를 해제한다. 프로그램 종료 시 반드시 호출."""
        if (
            not self._active
            and self._power_handle is None
            and self._mode is None
        ):
            return
        clear_ok = True
        try:
            clear_ok = self._release_power_request()
        except Exception:
            clear_ok = False
        try:
            if sys.platform == "win32":
                k32 = self._k32_api()
                if k32 is not None:
                    # CONTINUOUS 단독 = 이 스레드의 이전 SYSTEM/DISPLAY 요구 해제
                    # (과거 이중 적용·폴백 모드 잔여분까지 항상 정리)
                    k32.SetThreadExecutionState(_ES_CONTINUOUS)
                    k32.SetThreadExecutionState(_ES_CONTINUOUS)
        except Exception:
            pass
        was_active = self._active
        if clear_ok:
            self._active = False
            self._keep_display = False
            self._mode = None
        else:
            # PowerRequest Clear 실패 — 핸들 유지, 다음 tick 재시도
            self._active = False
            self._keep_display = False
            self._mode = "power" if self._power_handle is not None else None
        if was_active or not clear_ok:
            if clear_ok:
                self._emit("🌙 [최대절전방지] 유휴 최대절전 억제를 해제했습니다.")
            else:
                self._emit(
                    "⚠️ [최대절전방지] 억제 해제 시도 — PowerRequest 해제 실패 가능. "
                    "관리자 전원 요청(powercfg /requests)을 확인하세요."
                )

    def _set_execution_state(self, keep_display: bool) -> bool:
        try:
            k32 = self._k32_api()
            if k32 is None:
                return False
            flags = _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
            if keep_display:
                flags |= _ES_DISPLAY_REQUIRED
            prev = int(k32.SetThreadExecutionState(flags) or 0)
            return prev != 0
        except Exception:
            return False

    def _acquire_power_request(self, keep_display: bool) -> bool:
        """Modern Standby 대응용 PowerRequest."""
        handle: Optional[int] = None
        try:
            k32 = self._k32_api()
            if k32 is None:
                return False

            self._reason_keep = _REASON_TEXT
            ctx = _REASON_CONTEXT()
            ctx.Version = _POWER_REQUEST_CONTEXT_VERSION
            ctx.Flags = _POWER_REQUEST_CONTEXT_SIMPLE_STRING
            ctx.SimpleReasonString = self._reason_keep

            raw = k32.PowerCreateRequest(ctypes.byref(ctx))
            if not raw or raw == _INVALID_HANDLE_VALUE:
                return False
            handle = int(raw)

            if not k32.PowerSetRequest(
                ctypes.c_void_p(handle), _POWER_REQUEST_SYSTEM_REQUIRED
            ):
                k32.CloseHandle(ctypes.c_void_p(handle))
                return False
            if keep_display:
                try:
                    k32.PowerSetRequest(
                        ctypes.c_void_p(handle), _POWER_REQUEST_DISPLAY_REQUIRED
                    )
                except Exception:
                    pass
            self._power_handle = handle
            return True
        except Exception:
            if handle:
                try:
                    k32 = self._k32_api()
                    if k32 is not None:
                        k32.CloseHandle(ctypes.c_void_p(handle))
                except Exception:
                    pass
            self._power_handle = None
            return False

    def _release_power_request(self) -> bool:
        """PowerRequest 를 Clear 후 Close.

        SYSTEM Clear 가 실패하면 핸들을 남겨 다음 release/재시도가 가능하게 한다.
        """
        handle = self._power_handle
        if not handle:
            return True
        try:
            k32 = self._k32_api()
            if k32 is None:
                return False
            h = ctypes.c_void_p(int(handle))
            system_cleared = False
            try:
                system_cleared = bool(
                    k32.PowerClearRequest(h, _POWER_REQUEST_SYSTEM_REQUIRED)
                )
            except Exception:
                system_cleared = False
            try:
                k32.PowerClearRequest(h, _POWER_REQUEST_DISPLAY_REQUIRED)
            except Exception:
                pass
            if not system_cleared:
                try:
                    system_cleared = bool(
                        k32.PowerClearRequest(h, _POWER_REQUEST_SYSTEM_REQUIRED)
                    )
                except Exception:
                    system_cleared = False
            if not system_cleared:
                return False
            try:
                k32.CloseHandle(h)
            except Exception:
                pass
            self._power_handle = None
            return True
        except Exception:
            return False
