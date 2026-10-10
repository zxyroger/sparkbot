@echo off
REM ===========================================================================
REM  SparkBot - start the whole local voice stack in one command
REM
REM  Brings up THREE processes (the full local, no-cloud-cost setup):
REM
REM    1. ASR service   :8760   SenseVoice on CPU  (speech -> text)
REM    2. TTS service   :8761   MOSS-TTS-Nano      (text -> speech, offline CPU)
REM    3. SparkBot      :8765   main server        (this window, foreground)
REM
REM  ASR and TTS run minimized in their own windows. The main server runs HERE
REM  so that closing this window (or Ctrl+C) stops it, and you can see its log
REM  live instead of tailing a file.
REM
REM  Usage:
REM    start_stack.bat                  all three, port 8765
REM    start_stack.bat 8800             custom main-server port
REM    start_stack.bat 8765 noasr       skip the ASR service
REM    start_stack.bat 8765 notts       skip the TTS service
REM    start_stack.bat 8765 noasr notts server only
REM
REM  Point the main server at the local services via settings page or .env:
REM    SPARKBOT_SPEECH_ASR_PROVIDER=openai
REM    SPARKBOT_SPEECH_ASR_BASE_URL=http://127.0.0.1:8760/v1
REM    SPARKBOT_SPEECH_ASR_MODEL=iic/SenseVoiceSmall
REM    SPARKBOT_SPEECH_TTS_PROVIDER=openai
REM    SPARKBOT_SPEECH_TTS_BASE_URL=http://127.0.0.1:8761/v1
REM    SPARKBOT_SPEECH_TTS_MODEL=moss
REM    SPARKBOT_SPEECH_TTS_VOICE=Junhao
REM
REM  NOTE: ASCII-only on purpose. A .bat saved as UTF-8 without BOM gets read
REM  as ANSI/GBK by cmd.exe and turns into garbage commands. Chinese docs live
REM  in README.md instead.
REM
REM  Stop everything with stop_stack.bat.
REM ===========================================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

set PORT=%~1
if "%PORT%"=="" set PORT=8765

REM Accept the skip flags in any order after the port.
set SKIP_ASR=
set SKIP_TTS=
shift
:parse
if "%~1"=="" goto parsed
if /i "%~1"=="noasr" set SKIP_ASR=1
if /i "%~1"=="notts" set SKIP_TTS=1
shift
goto parse
:parsed

set ASR_PORT=8760
set TTS_PORT=8761
set SPEECH_DIR=%~dp0speech-service
if not exist "%SPEECH_DIR%\" set SPEECH_DIR=%~dp0..\speech-service

if not exist "logs" mkdir "logs"

echo.
echo   ============================================
echo     SparkBot local voice stack
echo   ============================================
echo.

REM ---- helper: is a TCP port already listening? ----------------------------
REM netstat + findstr is used instead of PowerShell so this works even in
REM constrained environments, and it is fast.

REM ---- 1. ASR service ------------------------------------------------------
if defined SKIP_ASR (
    echo   [SKIP] ASR service not requested
    goto after_asr
)
set BUSY=
for /f "delims=" %%a in ('netstat -ano ^| findstr /r /c:"LISTENING" ^| findstr /r /c:":%ASR_PORT% "') do set BUSY=1
if defined BUSY (
    echo   [SKIP] ASR already listening on %ASR_PORT%
    goto after_asr
)
if not exist "%SPEECH_DIR%\.venv\Scripts\python.exe" (
    echo   [WARN] ASR venv not found at %SPEECH_DIR%\.venv
    echo          see speech-service\README.md to set it up
    goto after_asr
)
REM Quoting note: do NOT write
REM     start /min cmd /c "cd /d "PATH" && foo"
REM - cmd strips the inner quotes and `start` then sees extra arguments.
REM We simply call the child .bat by its full path; start_asr.bat and
REM start_tts.bat both do `cd /d "%~dp0"` themselves, so the working
REM directory takes care of itself. One level of quoting, no nesting.
start "SparkBot ASR" /min cmd /c ""%SPEECH_DIR%\start_asr.bat" %ASR_PORT%"
echo   [OK]   ASR service starting on %ASR_PORT%  (model load ~15s)
:after_asr

REM ---- 2. TTS service ------------------------------------------------------
if defined SKIP_TTS (
    echo   [SKIP] TTS service not requested
    goto after_tts
)
set BUSY=
for /f "delims=" %%a in ('netstat -ano ^| findstr /r /c:"LISTENING" ^| findstr /r /c:":%TTS_PORT% "') do set BUSY=1
if defined BUSY (
    echo   [SKIP] TTS already listening on %TTS_PORT%
    goto after_tts
)
REM The TTS service has its own venv (.venv-moss): its torchaudio pin cannot
REM coexist with the ASR venv's torch version. See speech-service\start_tts.bat.
if not exist "%SPEECH_DIR%\.venv-moss\Scripts\python.exe" (
    echo   [WARN] TTS venv not found at %SPEECH_DIR%\.venv-moss
    echo          see speech-service\README.md to set it up
    goto after_tts
)
REM NOTE: the TTS service must NOT inherit the ASR HOME override - that would
REM break other things that rely on the real home directory. start_tts.bat
REM handles that by simply not setting HOME.
REM Same quoting rule as the ASR line above: call the child .bat directly.
start "SparkBot TTS" /min cmd /c ""%SPEECH_DIR%\start_tts.bat" %TTS_PORT%"
echo   [OK]   TTS service starting on %TTS_PORT%  (offline CPU, model load ~10s)
:after_tts

REM ---- 3. main server (foreground) ----------------------------------------
set BUSY=
for /f "delims=" %%a in ('netstat -ano ^| findstr /r /c:"LISTENING" ^| findstr /r /c:":%PORT% "') do set BUSY=1
if defined BUSY (
    echo   [SKIP] port %PORT% already in use - server is probably running
    echo.
    echo   Console: http://127.0.0.1:%PORT%/
    echo.
    pause
    goto done
)

REM Locate Python (prefer the launcher "py -3").
set PY=
where py >nul 2>&1 && set PY=py -3
if not defined PY where python >nul 2>&1 && set PY=python
if not defined PY (
    echo   [ERROR] Python not found. Install Python 3.11+ and add it to PATH.
    pause
    exit /b 1
)

echo   [OK]   main server starting on %PORT%
echo.
echo   ------------------------------------------------------------
echo     Console : http://127.0.0.1:%PORT%/
echo     ASR     : http://127.0.0.1:%ASR_PORT%/health
echo     TTS     : http://127.0.0.1:%TTS_PORT%/health
echo.
echo     Give ASR ~15s to load the model before speaking to the robot.
echo     Ctrl+C or close this window stops the main server.
echo     Run stop_stack.bat to stop the background services too.
echo   ------------------------------------------------------------
echo.

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
%PY% run.py --port %PORT%

:done
endlocal
