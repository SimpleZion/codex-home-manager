@echo off
setlocal

set "APP_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%APP_DIR%scripts\start-ui-service.ps1"

if errorlevel 1 (
  echo.
  echo Failed to start Codex Home Manager. See server.err.log for details.
  pause
)
