@echo off
setlocal
cd /d "%~dp0"

echo QTrade futures backfill launcher
echo Workspace: %CD%
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\continue-futures-backfill.ps1"
set "QTRADE_EXIT_CODE=%ERRORLEVEL%"

echo.
if "%QTRADE_EXIT_CODE%"=="0" (
    echo Backfill completed successfully.
) else (
    echo Backfill stopped with exit code %QTRADE_EXIT_CODE%.
    echo Check runtime\continue-futures-backfill.log for details.
)
echo.
pause
exit /b %QTRADE_EXIT_CODE%
