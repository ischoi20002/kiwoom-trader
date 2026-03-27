@echo off
title Kiwoom Trader - Research Mode
echo [INFO] Starting Kiwoom Trader in Research Folder...
echo --------------------------------------------------
python k_trader_gui.py %*
if %ERRORLEVEL% neq 0 (
    echo.
    echo [ERROR] Application crashed with error level %ERRORLEVEL%
    pause
)
