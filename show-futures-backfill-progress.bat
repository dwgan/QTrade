@echo off
setlocal
title QTrade futures backfill progress
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\show-futures-backfill-progress.ps1"
if errorlevel 1 (
  echo.
  echo Failed to read progress. See the error above.
  pause
)
endlocal
