@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"
echo.
echo [K-Trader] 폴더: %CD%
echo.

set "PY=%~dp0.venv\Scripts\python.exe"
if exist "!PY!" (
  echo [Python] .venv 사용
  "!PY!" -u k_trader_gui.py %*
  goto :done
)

where py >nul 2>&1
if not errorlevel 1 (
  echo [Python] py -3 사용 ^(.venv 없음 — install_k_trader.bat 실행 권장^)
  py -3 -u k_trader_gui.py %*
  goto :done
)

where python >nul 2>&1
if not errorlevel 1 (
  echo [Python] python 사용 ^(.venv 없음 — install_k_trader.bat 실행 권장^)
  python -u k_trader_gui.py %*
  goto :done
)

echo [오류] Python 3 을 찾을 수 없습니다.
echo        install_k_trader.bat 으로 .venv 를 만든 뒤 다시 실행하세요.
pause
exit /b 1

:done
if errorlevel 1 (
  echo.
  echo 종료 코드: 오류. 위 메시지를 확인하세요.
  echo        패키지 미설치 시: install_k_trader.bat
  pause
)
exit /b 0
