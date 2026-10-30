@echo off
rem ============================================================
rem  Stop AI Property Assistant: kills the gateway and backend
rem  processes started by start.bat (PID files first, then a
rem  command-line match as a fallback).
rem ============================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "RUNTIME=%~dp0.runtime"

call :kill_pid "%RUNTIME%\gateway.pid"
call :kill_pid "%RUNTIME%\backend.pid"

rem Fallback: only match this project's exact artifacts, never
rem kill every java.exe / python.exe on the machine.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'ai-property-gateway.*\.jar' -or $_.CommandLine -match 'ai-property-backend\.exe' -or $_.CommandLine -match 'backend\\serve\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>&1

echo Assistant stopped.
timeout /t 2 /nobreak >nul
exit /b 0

:kill_pid
if not exist "%~1" exit /b 0
set "PID="
set /p PID=<"%~1"
if defined PID taskkill /PID !PID! /T /F >nul 2>&1
del /q "%~1" >nul 2>&1
exit /b 0
