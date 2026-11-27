# -*- coding: utf-8 -*-
"""
K-Trader GUI 진입점.

1) config.py 부트스트랩·첫 실행 설정 마법사
2) 준비되면 k_trader_gui 본 기동

운영/개발 모두 ``run_k_trader.bat`` → 이 파일을 호출한다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _prepare_stdio() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass


def main() -> int:
    _prepare_stdio()
    here = Path(__file__).resolve().parent
    try:
        os.chdir(here)
    except Exception:
        pass
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))

    try:
        print("K-Trader: 시작 중…", flush=True)
    except Exception:
        pass

    try:
        from ui.first_run_config_wizard import maybe_bootstrap_and_restart
    except Exception as ex:
        print(f"[오류] 설정 부트스트랩 import 실패: {ex!r}", flush=True)
        return 1

    try:
        if maybe_bootstrap_and_restart():
            return 0
    except Exception as ex:
        print(f"[오류] 설정 마법사 실패: {ex!r}", flush=True)
        return 1

    # 본 GUI — 모듈 import 후 __main__ 과 동일하게 기동
    try:
        import tkinter as tk
        import k_trader_gui as gui_mod
    except Exception as ex:
        print(f"[오류] GUI import 실패: {ex!r}", flush=True)
        return 1

    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    auto = "--auto-start" in sys.argv
    try:
        app = gui_mod.KiwoomApp(root, auto_start=auto)
        _ = app
    except Exception as ex:
        print(f"[오류] KiwoomApp 기동 실패: {ex!r}", flush=True)
        try:
            root.destroy()
        except Exception:
            pass
        return 1
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
