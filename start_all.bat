@echo off
REM ===========================================================================
REM  SparkBot - start server + mock device, each in its own window
REM
REM  Difference from start.bat:
REM    start.bat      first-time use: installs deps, starts server, opens browser
REM    start_all.bat  daily use: brings up server AND mock device together
REM
REM  Usage:
REM    start_all.bat                 port 8765, no wake-word simulation
REM    start_all.bat 8800            custom port
REM    start_all.bat 8765 8          simulate a wake word every 8 seconds
REM    start_all.bat 8765 0 nodev    server only (when using a real board)
REM
REM  NOTE: this file is intentionally ASCII-only. A .bat written as UTF-8
REM  without BOM gets parsed as ANSI/GBK by cmd.exe and its Chinese text
REM  turns into garbage commands. Chinese docs live in README.md instead.
REM
REM  Processes run detached, so you can close this window.
REM  To stop: run stop_all.bat
REM ===========================================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

set PORT=%~1
if "%PORT%"=="" set PORT=8765
set TRIGGER=%~2
if "%TRIGGER%"=="" set TRIGGER=0
set MODE=%~3

if not exist "logs" mkdir "logs"

echo.
echo   ============================================
echo     SparkBot starting   (port %PORT%)
echo   ============================================
echo.

REM ---- locate Python ------------------------------------------------------
set PY=
where py >nul 2>&1 && set PY=py -3
if not defined PY (
    where python >nul 2>&1 && set PY=python
)
if not defined PY (
    echo   [ERROR] Python not found. Install Python 3.11+ and add it to PATH.
    echo.
    pause
    exit /b 1
)

REM ---- server -------------------------------------------------------------
REM If something already listens on the port, the server is already running.
set BUSY=
for /f "delims=" %%a in ('netstat -ano ^| findstr /r /c:"LISTENING" ^| findstr /r /c:":%PORT% "') do set BUSY=1
if defined BUSY (
    echo   [SKIP] port %PORT% already in use - server is probably running
    echo          console: http://127.0.0.1:%PORT%/
) else (
    start "SparkBot server" /min cmd /c "%PY% run.py --port %PORT% > logs\server.log 2>&1"
    echo   [OK]   server started in a new window
)

REM wait until the port is actually listening (max 30s)
REM
REM Delay uses "ping -n 2 127.0.0.1" rather than "timeout /t 1": timeout
REM requires stdin, and when this script is invoked with stdin redirected
REM it fails instantly with "Input redirection is not supported", turning
REM the wait loop into a busy spin. ping has no such requirement.
set READY=
for /l %%i in (1,1,30) do (
    if not defined READY (
        for /f "delims=" %%a in ('netstat -ano ^| findstr /r /c:"LISTENING" ^| findstr /r /c:":%PORT% "') do set READY=1
        if not defined READY ping -n 2 127.0.0.1 >nul
    )
)
if defined READY (
    echo          http://127.0.0.1:%PORT%/ is ready
) else (
    echo          [WARN] not listening after 30s - check logs\server.log
)

REM ---- mock device --------------------------------------------------------
if /i "%MODE%"=="nodev" (
    echo   [SKIP] mock device not started
    goto done
)

set DEVARGS=-m sparkbot.mock_device --url ws://127.0.0.1:%PORT%/robot
if not "%TRIGGER%"=="0" set DEVARGS=%DEVARGS% --trigger %TRIGGER%

start "SparkBot mock device" /min cmd /c "%PY% %DEVARGS% > logs\device.log 2>&1"
echo   [OK]   mock device started in a new window
if not "%TRIGGER%"=="0" echo          simulating a wake word every %TRIGGER%s

:done
echo.
echo   Console: http://127.0.0.1:%PORT%/
echo   Stop:    run stop_all.bat
echo   Logs:    type logs\server.log
echo.
echo   Both processes run detached; you can close this window.
echo.
endlocal