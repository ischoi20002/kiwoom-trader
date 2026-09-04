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
if not exist "%OP%\run_k_trader.bat" (
  call :fail "run_k_trader.bat 없음: %OP%"
  exit /b 1
)

REM start 인자/작업폴더를 명시해 재기동 누락을 막음
start "K-Trader" /D "%OP%" cmd /c "call \"%OP%\run_k_trader.bat\" --auto-start"

call :log "Remote Update Sequence Completed."
echo.
echo [완료] 운영 GUI를 다시 띄웠습니다. 창이 안 보이면 %LOG% 를 확인하세요.
timeout /t 3 /nobreak >nul
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
