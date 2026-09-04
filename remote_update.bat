@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

title K-Trader - Remote Updating...

set "REPO=c:\vibe"
set "OP=c:\k-trader"
set "BRANCH=main"

echo --------------------------------------------------
echo [1/4] 파일 잠금 해제 대기 (5초)...
timeout /t 5 /nobreak >nul

echo [2/4] Git pull (%REPO% %BRANCH%)...
if not exist "%REPO%\.git" (
  echo [오류] Git 저장소 없음: %REPO%
  exit /b 1
)
cd /d "%REPO%"
git fetch origin %BRANCH% --prune
if errorlevel 1 (
  echo [오류] git fetch 실패
  exit /b 1
)
git pull --ff-only origin %BRANCH%
if errorlevel 1 (
  echo [오류] git pull --ff-only 실패 (로컬 커밋/충돌 확인)
  exit /b 1
)

echo [3/4] 운영 배포 (deploy_production.py --preserve-runtime)...
py -3 -u "%REPO%\tools\deploy_production.py" --preserve-runtime
if errorlevel 1 (
  echo [오류] 배포 실패
  exit /b 1
)

echo [4/4] 운영 폴더에서 재시작 (%OP%)...
if not exist "%OP%\run_k_trader.bat" (
  echo [오류] run_k_trader.bat 없음: %OP%
  exit /b 1
)
cd /d "%OP%"
start "" run_k_trader.bat --auto-start

echo.
echo Remote Update Sequence Completed!
echo --------------------------------------------------
exit /b 0
