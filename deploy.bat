@echo off
set SOURCE=%~dp0
set TARGET=c:\vibe_op

echo [1/3] Creating Operation Directory: %TARGET%
if not exist "%TARGET%" mkdir "%TARGET%"

echo [2/3] Syncing Main Folders...
xcopy "%SOURCE%core" "%TARGET%\core" /E /I /C /Y /Q
xcopy "%SOURCE%gui" "%TARGET%\gui" /E /I /C /Y /Q
xcopy "%SOURCE%strategy" "%TARGET%\strategy" /E /I /C /Y /Q
xcopy "%SOURCE%utils" "%TARGET%\utils" /E /I /C /Y /Q
xcopy "%SOURCE%reporting" "%TARGET%\reporting" /E /I /C /Y /Q

echo [3/3] Syncing Root Resource Files...
xcopy "%SOURCE%*.py" "%TARGET%\" /Y /Q
xcopy "%SOURCE%*.bat" "%TARGET%\" /Y /Q
xcopy "%SOURCE%*.ps1" "%TARGET%\" /Y /Q

echo Deployment Complete!
timeout /t 3
