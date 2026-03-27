@echo off
:: [v1.0] Remote Update Handover Script
:: This script waits for the main app to close, pulls from git, and redeploys.

title Kiwoom Trader - Remote Updating...
echo --------------------------------------------------
echo [1/4] Waiting for application to release file locks (5s)...
timeout /t 5 /nobreak >nul

echo [2/4] Pulling latest code from Git (Research Folder)...
cd /d "c:\바이브"
git pull

echo [3/4] Running Deployment Sync...
call deploy.bat

echo [4/4] Restarting Application in Operation Folder...
cd /d "c:\바이브_운영"
start run_app.bat

echo.
echo ✅ Remote Update Sequence Completed!
echo --------------------------------------------------
exit
