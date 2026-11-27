@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
echo.
echo [K-Trader] 폴더: %CD%
echo.

set "ENTRY=run_entry.py"
if not exist "%~dp0run_entry.py" set "ENTRY=k_trader_gui.py"

set "PY="
if exist "%~dp0python\python.exe" (
  set "PY=%~dp0python\python.exe"
  echo [Python] 포터블 python\ 사용
) else if exist "%~dp0.venv\Scripts\python.exe" (
  set "PY=%~dp0.venv\Scripts\python.exe"
  echo [Python] .venv 사용
)

if defined PY (
  "!PY!" -u "!ENTRY!" %*
  goto :done
)

where py >nul 2>&1
if not errorlevel 1 (
  echo [Python] py -3 사용 ^(포터블/.venv 없음 — Setup.exe 또는 install_k_trader.bat 권장^)
  py -3 -u "!ENTRY!" %*
  goto :done
)

where python >nul 2>&1
if not errorlevel 1 (
  echo [Python] python 사용 ^(포터블/.venv 없음 — Setup.exe 또는 install_k_trader.bat 권장^)
  python -u "!ENTRY!" %*
  goto :done
)

echo [오류] Python 을 찾을 수 없습니다.
echo        Setup.exe 로 설치하거나 install_k_trader.bat 을 실행하세요.
pause
exit /b 1

:done
if errorlevel 1 (
  echo.
  echo 종료 코드: 오류. 위 메시지를 확인하세요.
  echo        패키지 미설치 시: install_k_trader.bat 또는 Setup.exe
  pause
)
exit /b 0
