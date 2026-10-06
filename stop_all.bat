@echo off
REM ===========================================================================
REM  SparkBot - stop the processes started by start_all.bat / start.bat
REM
REM  Usage:
REM    stop_all.bat            port 8765 (only used for the final status report)
REM    stop_all.bat 8800
REM
REM  Matches processes by command line, so it only kills python processes whose
REM  command line mentions sparkbot / run.py. Other Python programs are safe.
REM
REM  NOTE: ASCII-only on purpose - see the comment in start_all.bat.
REM
REM  Uses PowerShell's Get-CimInstance to read command lines instead of wmic
REM  (wmic has been removed from recent Windows builds). Calling PowerShell
REM  from cmd like this is NOT affected by the PowerShell execution policy.
REM ===========================================================================

setlocal
cd /d "%~dp0"
set PORT=%~1
if "%PORT%"=="" set PORT=8765

echo.
echo   ============================================
echo     SparkBot stopping
echo   ============================================
echo.

set "PS_CMD=$ErrorActionPreference='SilentlyContinue'; $roots=@(Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='py.exe' OR Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'sparkbot' -or $_.CommandLine -match 'run\.py --port' -or $_.CommandLine -match '[\\/]run\.py' }); if ($roots.Count -eq 0) { Write-Host '  no running SparkBot process found.' } else { $kids=@(Get-CimInstance Win32_Process | Where-Object { $roots.ProcessId -contains $_.ParentProcessId }); $all=@($kids + $roots); foreach ($p in $all) { Write-Host ('  [STOP] ' + $p.Name + ' PID=' + $p.ProcessId); Stop-Process -Id $p.ProcessId -Force } }"

powershell -NoProfile -ExecutionPolicy Bypass -Command "%PS_CMD%"

echo.
echo   port %PORT% status:
netstat -ano | findstr /r /c:"LISTENING" | findstr /r /c:":%PORT% " >nul 2>&1
if errorlevel 1 (
    echo     released
) else (
    echo     still in use - probably another program
)
echo.
pause
endlocal
