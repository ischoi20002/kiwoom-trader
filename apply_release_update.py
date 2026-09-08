# -*- coding: utf-8 -*-
"""
GitHub Release 운영 zip 적용 엔트리포인트.

``remote_update.bat`` 또는 수동 실행:
  py -3 -u apply_release_update.py
  .venv\\Scripts\\python.exe -u apply_release_update.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def main() -> int:
    # 운영 폴더를 cwd / 모듈 경로로
    here = Path(__file__).resolve().parent
    try:
        os.chdir(here)
    except Exception:
        pass
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))

    try:
        from core.app_updater import apply_latest_release_update
    except Exception as e:
        print(f"[오류] app_updater import 실패: {e!r}")
        return 1

    def _log(msg: str) -> None:
        try:
            print(msg, flush=True)
        except Exception:
            pass

    try:
        ok, msg = apply_latest_release_update(log=_log, restart=True)
    except Exception as e:
        print(f"[오류] 업데이트 예외: {e!r}")
        return 1
    if not ok:
        print(f"[오류] {msg}")
        return 1
    print(f"[OK] {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
