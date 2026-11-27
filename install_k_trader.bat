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

REM --- 포터블 런타임이 이미 있으면 pip 보강만 ---
if exist "%~dp0python\python.exe" (
  echo [안내] 포터블 python\ 런타임이 있습니다 ^(Setup.exe 설치본^).
  echo        시스템 Python / .venv 없이도 run_k_trader.bat 로 실행할 수 있습니다.
  echo.
  set /p FIX="포터블 런타임에 pip 패키지를 다시 설치할까요? [y/N]: "
  if /i "!FIX!"=="y" (
    echo [pip] 포터블 python 에 requirements 설치...
    "%~dp0python\python.exe" -m pip install --upgrade pip
    if errorlevel 1 goto :fail
    "%~dp0python\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
    "%~dp0python\python.exe" -c "import pandas, websockets, kiwoom_rest_api, tkinter; print('OK')"
    if errorlevel 1 goto :fail
    echo.
    echo 완료. 실행: run_k_trader.bat
    pause
    exit /b 0
  )
  echo        건너뜀. 실행: run_k_trader.bat
  pause
  exit /b 0
)

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
  echo        Setup.exe ^(포터블 Python 포함^) 설치를 권장합니다.
  echo        또는 https://www.python.org/downloads/ 에서 Python 3.12 설치 후
  echo        「Add python.exe to PATH」 옵션을 켜 주세요.
  goto :fail
)

echo [1/4] Python: !PY_EXE!
"!PY_EXE!" -c "import sys; print('       version:', sys.version.split()[0])" >nul 2>&1
if errorlevel 1 (
  echo [오류] Python 실행 확인 실패
  goto :fail
)

set "VENV=%~dp0.venv"
set "VENV_PY=%VENV%\Scripts\python.exe"
set "VENV_PIP=%VENV%\Scripts\pip.exe"

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
echo   실행: run_k_trader.bat  ^(첫 실행 시 API 설정 마법사^)
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
