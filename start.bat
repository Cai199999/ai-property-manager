@echo off
rem ============================================================
rem  AI Property Assistant - Windows one-click launcher
rem  Starts the Python backend (127.0.0.1:8000) and the Java
rem  gateway (127.0.0.1:8080) in the background, then opens the
rem  web UI in the default browser.
rem
rem  Backend options (first match wins):
rem    backend\ai-property-backend.exe        packaged build
rem    .venv\Scripts\python.exe  serve.py     source build
rem    backend\.venv\Scripts\python.exe       source build
rem ============================================================
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "ROOT=%~dp0"
set "RUNTIME=%ROOT%.runtime"
if not exist "%RUNTIME%" mkdir "%RUNTIME%" >nul 2>&1

rem ---------- locate backend ----------
set "BACKEND_FILE="
set "BACKEND_ARGS="
set "BACKEND_DIR=%ROOT%backend"
if exist "%ROOT%backend\ai-property-backend.exe" (
    set "BACKEND_FILE=%ROOT%backend\ai-property-backend.exe"
) else if exist "%ROOT%.venv\Scripts\python.exe" (
    set "BACKEND_FILE=%ROOT%.venv\Scripts\python.exe"
    set "BACKEND_ARGS=serve.py"
) else if exist "%ROOT%backend\.venv\Scripts\python.exe" (
    set "BACKEND_FILE=%ROOT%backend\.venv\Scripts\python.exe"
    set "BACKEND_ARGS=serve.py"
)
if not defined BACKEND_FILE (
    echo [ERROR] Backend not found.
    echo         Expected backend\ai-property-backend.exe or .venv\Scripts\python.exe
    pause
    exit /b 1
)

rem ---------- locate gateway jar ----------
set "GATEWAY_JAR="
if exist "%ROOT%gateway\target\ai-property-gateway.jar" (
    set "GATEWAY_JAR=%ROOT%gateway\target\ai-property-gateway.jar"
)
if not defined GATEWAY_JAR (
    for %%F in ("%ROOT%gateway\target\ai-property-gateway-*.jar") do set "GATEWAY_JAR=%%~fF"
)
if not defined GATEWAY_JAR (
    echo [ERROR] Gateway jar not found.
    echo         Build it first: mvn -f gateway\pom.xml clean package -DskipTests
    pause
    exit /b 1
)

rem ---------- start backend in the background ----------
if defined BACKEND_ARGS (
    for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "(Start-Process -FilePath '%BACKEND_FILE%' -ArgumentList '%BACKEND_ARGS%' -WorkingDirectory '%BACKEND_DIR%' -WindowStyle Hidden -PassThru).Id"`) do set "BACKEND_PID=%%P"
) else (
    for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "(Start-Process -FilePath '%BACKEND_FILE%' -WorkingDirectory '%BACKEND_DIR%' -WindowStyle Hidden -PassThru).Id"`) do set "BACKEND_PID=%%P"
)
if defined BACKEND_PID ( > "%RUNTIME%\backend.pid" echo !BACKEND_PID! )

rem ---------- start gateway in the background ----------
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "(Start-Process -FilePath 'javaw' -ArgumentList '-jar','%GATEWAY_JAR%' -WorkingDirectory '%ROOT%' -WindowStyle Hidden -PassThru).Id"`) do set "GATEWAY_PID=%%P"
if defined GATEWAY_PID ( > "%RUNTIME%\gateway.pid" echo !GATEWAY_PID! )

rem ---------- wait for the gateway to be ready ----------
timeout /t 3 /nobreak >nul
powershell -NoProfile -Command "for($i=0;$i -lt 45;$i++){ try { $r=Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8080/actuator/health' -TimeoutSec 2; if($r.StatusCode -eq 200){ exit 0 } } catch {} ; Start-Sleep -Seconds 1 }; exit 1"
if errorlevel 1 echo [WARN] Gateway is not healthy yet, opening the page anyway.

start "" "http://127.0.0.1:8080"
exit /b 0
