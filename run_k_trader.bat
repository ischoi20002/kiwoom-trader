@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo [K-Trader] 폴더: %CD%
echo.

where python >nul 2>&1
if errorlevel 1 (
  echo python 이 PATH에 없습니다. py 런처로 시도합니다...
  py -3 -u k_trader_gui.py
) else (
  python -u k_trader_gui.py
)

if errorlevel 1 (
  echo.
  echo 종료 코드: 오류. 위 메시지를 확인하세요.
  pause
)
