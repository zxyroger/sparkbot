@echo off
REM ============================================================
REM  Start the local SenseVoice ASR service (OpenAI compatible)
REM ============================================================
REM
REM  Why this .bat exists instead of a one-line command:
REM  the service needs several environment variables, and missing any
REM  one of them fails in a very confusing way:
REM
REM   * HOME / USERPROFILE must point at a WRITABLE directory.
REM     ModelScope wants to create %USERPROFILE%\.modelscope, which is
REM     read-only on this machine, and fails with:
REM       [E1022] Failed to create SDK directories: WinError 5 access denied
REM     which shows up as "model download failed / model not registered".
REM   * MODELSCOPE_CACHE points at drive D (the model is ~900MB and drive C
REM     only has a few GB free).
REM   * TMP / TEMP point at a writable directory (pip and model extraction
REM     use them).
REM
REM  Verified: model loads in ~13s, recognition RTF ~0.12 on CPU
REM  (about 8x faster than real time).
REM
REM  Usage:  start_asr.bat            (default 127.0.0.1:8760)
REM          start_asr.bat 9000       (custom port)
REM
REM  NOTE: ASCII-ONLY ON PURPOSE. cmd.exe parses a .bat using the system
REM  ANSI codepage (GBK here), so UTF-8 Chinese in this file would be read
REM  as garbage bytes and turned into bogus commands. Chinese docs live in
REM  README.md instead.
REM ============================================================

setlocal
set PORT=%1
if "%PORT%"=="" set PORT=8760

cd /d "%~dp0"

set HOME=%~dp0home
set USERPROFILE=%~dp0home
set MODELSCOPE_CACHE=%~dp0modelscope-cache
set TMP=D:\dsh\.tmp
set TEMP=D:\dsh\.tmp

if not exist "%HOME%" mkdir "%HOME%"
if not exist "%MODELSCOPE_CACHE%" mkdir "%MODELSCOPE_CACHE%"

echo.
echo   Starting SenseVoice ASR service
echo     URL    : http://127.0.0.1:%PORT%/v1
echo     Health : http://127.0.0.1:%PORT%/health
echo     Device : cpu
echo     Model  : iic/SenseVoiceSmall
echo.
echo   First run downloads ~900MB; afterwards it loads in ~13s.
echo   Press Ctrl+C to stop.
echo.

".venv\Scripts\python.exe" server.py --host 127.0.0.1 --port %PORT% --device cpu
endlocal
