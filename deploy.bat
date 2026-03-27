@echo off
:: [v1.2] Stable Deployment Script for Kiwoom Trader
:: UTF-8 Encoding safe (Batch style)

set SOURCE=c:\바이브
set TARGET=c:\바이브_운영

echo --------------------------------------------------
echo [1/3] Creating Operation Directory...
if not exist "%TARGET%" mkdir "%TARGET%"

echo [2/3] Syncing Main Folders (core, gui, strategy, utils, reporting)...
xcopy "%SOURCE%\core" "%TARGET%\core" /E /C /I /Y /Q /H
xcopy "%SOURCE%\gui" "%TARGET%\gui" /E /C /I /Y /Q /H
xcopy "%SOURCE%\strategy" "%TARGET%\strategy" /E /C /I /Y /Q /H
xcopy "%SOURCE%\utils" "%TARGET%\utils" /E /C /I /Y /Q /H
xcopy "%SOURCE%\reporting" "%TARGET%\reporting" /E /C /I /Y /Q /H

echo [3/3] Syncing Root Resource Files...
:: Loop through py, json, csv, md, bat files
for %%f in ("%SOURCE%\*.py" "%SOURCE%\*.json" "%SOURCE%\*.csv" "%SOURCE%\*.md" "%SOURCE%\*.bat") do (
    set "fname=%%~nxf"
    :: Exclude large DB and sensitive trade data (if they exist in target)
    if /I not "%%~nxf"=="trading_data.db" (
        if /I not "%%~nxf"=="virtual_db.json" (
            copy /Y "%%f" "%TARGET%\" >nul
        ) else (
            if not exist "%TARGET%\virtual_db.json" (
                copy /Y "%%f" "%TARGET%\" >nul
            )
        )
    )
)

echo.
echo ✅ Deployment Complete!
echo 🚀 Operation Path: %TARGET%
echo --------------------------------------------------
pause
