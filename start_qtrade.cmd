@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo QTrade virtual environment was not found.
    echo Complete the installation steps in README.md first.
    pause
    exit /b 1
)

echo Starting QTrade local interface...
echo The browser will open automatically. Press Ctrl+C to stop.
echo.
".venv\Scripts\python.exe" -m qtrade ui %*
set "qtrade_exit_code=%errorlevel%"

if not "%qtrade_exit_code%"=="0" (
    echo.
    echo QTrade failed to start. Review the error above.
    pause
)

exit /b %qtrade_exit_code%
