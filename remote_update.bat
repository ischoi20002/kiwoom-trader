@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

title K-Trader - Remote Updating...

set "REPO=c:\vibe"
set "OP=c:\k-trader"
set "BRANCH=main"
set "LOG=%OP%\last_remote_update.log"

echo --------------------------------------------------
echo [K-Trader] Remote Update  %DATE% %TIME%
echo 로그: %LOG%
echo --------------------------------------------------
(
  echo ==== Remote Update %DATE% %TIME% ====
) > "%LOG%" 2>&1

call :log "[1/4] 파일 잠금 해제 대기 (5초)..."
timeout /t 5 /nobreak >nul

call :log "[2/4] Git pull (%REPO% %BRANCH%)..."
if not exist "%REPO%\.git" (
  call :fail "Git 저장소 없음: %REPO%"
  exit /b 1
)
cd /d "%REPO%"
git fetch origin %BRANCH% --prune >> "%LOG%" 2>&1
if errorlevel 1 (
  call :fail "git fetch 실패 — %LOG% 확인"
  exit /b 1
)
git pull --ff-only origin %BRANCH% >> "%LOG%" 2>&1
if errorlevel 1 (
  call :fail "git pull --ff-only 실패 (로컬 커밋/충돌) — %LOG% 확인"
  exit /b 1
)

call :log "[3/4] 운영 배포 (deploy_production.py --preserve-runtime)..."
py -3 -u "%REPO%\tools\deploy_production.py" --preserve-runtime >> "%LOG%" 2>&1
if errorlevel 1 (
  call :fail "배포 실패 — %LOG% 확인"
  exit /b 1
)

call :log "[4/4] 운영 GUI 재시작 (%OP%)..."
cd /d "%OP%"

REM 콘솔 창 없이 pythonw 로 기동 (재시작 시 cmd 잔류 방지)
set "PYW=%OP%\.venv\Scripts\pythonw.exe"
set "PY=%OP%\.venv\Scripts\python.exe"
if exist "%PYW%" (
  start "" /D "%OP%" "%PYW%" -u k_trader_gui.py --auto-start
) else if exist "%PY%" (
  start "" /D "%OP%" "%PY%" -u k_trader_gui.py --auto-start
) else if exist "%OP%\run_k_trader.bat" (
  start "K-Trader" /D "%OP%" "%OP%\run_k_trader.bat" --auto-start
) else (
  call :fail "pythonw/python/run_k_trader.bat 없음: %OP%"
  exit /b 1
)

REM 기동 확인(최대 ~10초)
set "STARTED=0"
for /L %%i in (1,1,10) do (
  timeout /t 1 /nobreak >nul
  powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*k_trader_gui.py*' } | Select-Object -First 1) { exit 0 } else { exit 1 }" >nul 2>&1
  if not errorlevel 1 (
    set "STARTED=1"
    goto :started_ok
  )
)
:started_ok
if "!STARTED!"=="0" (
  call :fail "재시작 후 k_trader_gui 프로세스가 보이지 않음 — %OP%\run_k_trader.bat 직접 실행 확인"
  exit /b 1
)

call :log "Remote Update Sequence Completed. (GUI process detected, no console)"
echo.
echo [완료] 운영 GUI를 다시 띄웠습니다. 문제가 있으면 %LOG% 를 확인하세요.
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
