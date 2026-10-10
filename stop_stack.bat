@echo off
REM ===========================================================================
REM  SparkBot - stop everything start_stack.bat started
REM
REM  Kills, in order:
REM    1. the ASR service   (speech-service server.py  / port 8760)
REM    2. the TTS service   (speech-service moss_tts_server.py / port 8761)
REM    3. the main server   (sparkbot run.py / port 8765)
REM
REM  Matching is done by COMMAND LINE, so only the intended processes die.
REM  Other Python programs on this machine are left alone.
REM
REM  Usage:
REM    stop_stack.bat             default ports 8765 / 8760 / 8761
REM    stop_stack.bat 8800        custom main-server port
REM
REM  NOTE: ASCII-only on purpose - see the comment in start_stack.bat.
REM ===========================================================================

setlocal
cd /d "%~dp0"

set PORT=%~1
if "%PORT%"=="" set PORT=8765
set ASR_PORT=8760
set TTS_PORT=8761

echo.
echo   ============================================
echo     SparkBot stopping the local voice stack
echo   ============================================
echo.

REM PowerShell reaches the command lines that cmd cannot see. wmic is avoided
REM because it is gone from recent Windows builds.
REM
REM The main server is matched on '[\\/]run\.py' rather than
REM 'run\.py --port': the real command line is
REM     python run.py --host 0.0.0.0 --port 8765
REM so a pattern that expects --port right after run.py NEVER matches, and the
REM main server would survive the stop while its port stayed busy.
REM Note: 'tts_server\.py' also matches 'moss_tts_server.py' (substring), the
REM alternative TTS service in speech-service.
set "PS_CMD=$ErrorActionPreference='SilentlyContinue'; $targets=@(Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='py.exe' OR Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match '[\\/]run\.py' -or $_.CommandLine -match 'sparkbot\\\\' -or $_.CommandLine -match 'tts_server\.py' -or $_.CommandLine -match 'server\.py --host' -or $_.CommandLine -match 'mock_device' }); if ($targets.Count -eq 0) { Write-Host '  no SparkBot service process found.' } else { foreach ($p in $targets) { Write-Host ('  [STOP] ' + $p.Name + ' PID=' + $p.ProcessId); Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } }"

powershell -NoProfile -ExecutionPolicy Bypass -Command "%PS_CMD%"

REM Give the OS a moment to release the listening sockets.
ping -n 3 127.0.0.1 >nul

echo.
echo   port status:
for %%P in (%ASR_PORT% %TTS_PORT% %PORT%) do (
    netstat -ano | findstr /r /c:"LISTENING" | findstr /r /c:":%%P " >nul 2>&1
    if errorlevel 1 (
        echo     %%P  released
    ) else (
        echo     %%P  still in use - another program may own it
    )
)
echo.
pause
endlocal
