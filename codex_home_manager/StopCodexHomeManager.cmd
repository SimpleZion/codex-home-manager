@echo off
setlocal

set "APP_DIR=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%APP_DIR%scripts\stop-ui-service.ps1"

if errorlevel 1 (
  echo.
  echo Failed to stop Codex Home Manager.
  pause
)
