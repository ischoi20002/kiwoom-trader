@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion

cd /d "%~dp0"
echo.
echo ============================================================
echo   K-Trader 운영 PC 설치 (가상환경 + pip 의존성)
echo   폴더: %CD%
echo ============================================================
echo.

REM --- Python 3 찾기 (64bit 권장) ---
set "PY_LAUNCHER="
where py >nul 2>&1
if not errorlevel 1 set "PY_LAUNCHER=py -3"

set "PY_EXE="
if defined PY_LAUNCHER (
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
  echo        https://www.python.org/downloads/ 에서 Python 3.11+ 설치 후
  echo        「Add python.exe to PATH」 옵션을 켜 주세요.
  goto :fail
)

echo [1/4] Python: !PY_EXE!
"!PY_EXE!" -c "import sys; print('       버전:', sys.version.split()[0], '|', '64bit' if sys.maxsize^>2**32 else '32bit')" 2>nul
if errorlevel 1 (
  echo [오류] Python 실행 확인 실패
  goto :fail
)

set "VENV=%~dp0.venv"
set "VENV_PY=%VENV%\Scripts\python.exe"
set "VENV_PIP=%VENV%\Scripts\pip.exe"

REM --- 가상환경 ---
echo.
echo [2/4] 가상환경 (.venv) 준비...
if exist "%VENV_PY%" (
  echo        기존 .venv 사용
) else (
  "!PY_EXE!" -m venv "%VENV%"
  if errorlevel 1 (
    echo [오류] venv 생성 실패
    goto :fail
  )
  echo        .venv 생성 완료
)

if not exist "%VENV_PY%" (
  echo [오류] .venv\Scripts\python.exe 없음
  goto :fail
)

REM --- pip / requirements ---
echo.
echo [3/4] pip 패키지 설치 (수 분 소요)...
if not exist "requirements.txt" (
  echo [오류] requirements.txt 없음 — K-Trader 폴더 전체를 복사했는지 확인하세요.
  goto :fail
)

"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 goto :fail

"%VENV_PIP%" install -r requirements.txt
if errorlevel 1 goto :fail

echo.
echo [4/4] 설치 확인...
"%VENV_PY%" -c "import pandas, websockets, kiwoom_rest_api; print('       pandas / websockets / kiwoom-rest-api OK')"
if errorlevel 1 (
  echo [오류] 필수 패키지 import 실패
  goto :fail
)

echo.
echo ============================================================
echo   설치 완료
echo   실행: run_k_trader.bat
echo ============================================================
echo.
pause
exit /b 0

:fail
echo.
echo ============================================================
echo   설치 실패 — 위 메시지를 확인하세요.
echo ============================================================
echo.
pause
exit /b 1
