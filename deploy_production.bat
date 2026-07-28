@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

cd /d "%~dp0"
echo.
echo ============================================================
echo   K-Trader 운영 배포
echo   c:\vibe  --^>  c:\k-trader
echo   소스 폴더: %CD%
echo ============================================================
echo.

REM --- Python 3 찾기 ---
set "PY_EXE="
where py >nul 2>&1
if not errorlevel 1 (
  for /f "delims=" %%i in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "PY_EXE=%%i"
)

if not defined PY_EXE (
  where python >nul 2>&1
  if not errorlevel 1 (
    for /f "delims=" %%i in ('python -c "import sys; print(sys.executable)" 2^>nul') do set "PY_EXE=%%i"
  )
)

if not defined PY_EXE (
  echo [오류] Python 3 을 찾을 수 없습니다.
  echo        Python 3.11+ 설치 후 PATH 등록, 또는 py 런처를 사용하세요.
  goto :fail
)

echo [1/2] Python: !PY_EXE!

set "DEPLOY_SCRIPT=%~dp0tools\deploy_production.py"
if not exist "%DEPLOY_SCRIPT%" (
  echo [오류] tools\deploy_production.py 없음
  echo        K-Trader 개발 폴더(c:\vibe)에서 실행했는지 확인하세요.
  goto :fail
)

echo [2/2] 배포 스크립트 실행...
echo.
"!PY_EXE!" -u "%DEPLOY_SCRIPT%"
if errorlevel 1 goto :fail

echo.
echo ============================================================
echo   배포 완료
echo.
echo   실행:  cd c:\k-trader ^&^& run_k_trader.bat
echo   ※ run_k_trader.bat 는 .venv 를 우선 사용합니다.
echo   ※ pandas 오류 시: c:\k-trader\install_k_trader.bat
echo ============================================================
echo.
pause
exit /b 0

:fail
echo.
echo ============================================================
echo   배포 실패 — 위 메시지를 확인하세요.
echo ============================================================
echo.
pause
exit /b 1
