@echo off
:: Remote Update Handover Script

title Kiwoom Trader - Remote Updating...
echo --------------------------------------------------
echo [1/4] Waiting for application to release file locks (5s)...
timeout /t 5 /nobreak >nul

echo [2/4] Pulling latest code from Git (Research Folder)...
cd /d "%~dp0"
git pull

echo [3/4] Running Deployment Sync...
call deploy.bat

echo [4/4] Restarting Application in Operation Folder...
cd /d "c:\vibe_op"
start run_app.bat --auto-start

echo.
echo Remote Update Sequence Completed!
echo --------------------------------------------------
exit
