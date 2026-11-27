@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

title K-Trader - Remote Updating...

REM 운영 폴더 기준 (bat 위치). 개발 PC에서 vibe 의 bat 을 실행해도
REM 기본 적용 대상은 c:\k-trader.
set "OP=c:\k-trader"
set "HERE=%~dp0"
if exist "%HERE%k_trader_gui.py" set "OP=%HERE:~0,-1%"
set "LOG=%OP%\last_remote_update.log"

echo --------------------------------------------------
echo [K-Trader] Release Update  %DATE% %TIME%
echo 대상: %OP%
echo 로그: %LOG%
echo --------------------------------------------------
(
  echo ==== Release Update %DATE% %TIME% ====
  echo OP=%OP%
) > "%LOG%" 2>&1

call :log "[1/3] 파일 잠금 해제 대기 (5초)..."
timeout /t 5 /nobreak >nul

call :log "[2/3] GitHub Release zip 적용..."
cd /d "%OP%"

set "PY="
if exist "%OP%\python\python.exe" set "PY=%OP%\python\python.exe"
if not defined PY if exist "%OP%\.venv\Scripts\python.exe" set "PY=%OP%\.venv\Scripts\python.exe"
if not defined PY (
  where py >nul 2>&1
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
  where python >nul 2>&1
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  call :fail "Python 없음 — %OP%\python 또는 .venv 또는 py/python PATH"
  exit /b 1
)

if exist "%OP%\apply_release_update.py" (
  %PY% -u "%OP%\apply_release_update.py" >> "%LOG%" 2>&1
) else (
  %PY% -u -c "from core.app_updater import apply_latest_release_update; import sys; ok,m=apply_latest_release_update(restart=True); print(m); sys.exit(0 if ok else 1)" >> "%LOG%" 2>&1
)
if errorlevel 1 (
  call :fail "Release 업데이트 실패 — %LOG% 확인"
  exit /b 1
)

call :log "[3/3] 완료 (GUI 재시작은 apply 스크립트가 수행)"
echo.
echo [완료] 문제가 있으면 %LOG% 를 확인하세요.
timeout /t 2 /nobreak >nul
exit /b 0

:log
echo %~1
echo %~1 >> "%LOG%"
goto :eof

:fail
echo.
echo [오류] %~1
echo [오류] %~1 >> "%LOG%"
echo.
echo 이 창을 닫지 말고 메시지를 확인한 뒤 아무 키나 누르세요.
pause >nul
goto :eof
