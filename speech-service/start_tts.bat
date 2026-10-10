@echo off
REM ============================================================
REM  Start the local TTS service (MOSS-TTS-Nano, OpenAI compatible)
REM ============================================================
REM
REM  MOSS-TTS-Nano: 0.1B params, pure CPU, fully offline, 48kHz output.
REM  The voice is defined by a REFERENCE WAV (voice cloning), so changing
REM  the voice needs no retraining - just another sample.
REM
REM  Why its own venv (.venv-moss) instead of .venv:
REM  the model's official code imports torchaudio, and torchaudio stopped
REM  shipping after 2.8 (2.8.0 is the last build that pairs with torch
REM  2.8.x). The ASR side runs torch 2.14.1, which has no torchaudio at
REM  all, so the two cannot live in one environment. Pinned combo here:
REM      torch 2.8.0 + torchaudio 2.8.0 + transformers 4.57.1
REM  This keeps the working ASR environment untouched.
REM
REM  Do NOT set a HOME / USERPROFILE override in this file: the weights
REM  are loaded from a local folder, and a HOME override breaks other
REM  things on this machine (see the story in start_asr.bat).
REM
REM  TMP / TEMP point at drive D because the system temp directory is not
REM  writable here and model loading needs a scratch dir.
REM
REM  Usage:  start_tts.bat              (default 127.0.0.1:8761)
REM          start_tts.bat 9001         (custom port)
REM          start_tts.bat 8761 Xiaoyu  (another voice)
REM
REM  Built-in voices: Junhao (male), Xiaoyu / Yuewen / Lingyu (female).
REM
REM  NOTE: ASCII-ONLY ON PURPOSE - see the comment in start_asr.bat.
REM ============================================================

setlocal
set PORT=%1
if "%PORT%"=="" set PORT=8761
set VOICE=%2
if "%VOICE%"=="" set VOICE=Junhao

cd /d "%~dp0"

set TMP=D:\dsh\.tmp
set TEMP=D:\dsh\.tmp
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist ".venv-moss\Scripts\python.exe" (
    echo   [ERROR] .venv-moss not found - see speech-service\README.md.
    pause
    exit /b 1
)

echo.
echo   Starting MOSS-TTS-Nano TTS service
echo     URL    : http://127.0.0.1:%PORT%/v1
echo     Health : http://127.0.0.1:%PORT%/health
echo     Voice  : %VOICE%
echo.
echo   Offline, CPU only. Model load takes a few seconds.
echo   Press Ctrl+C to stop.
echo.

".venv-moss\Scripts\python.exe" moss_tts_server.py --host 127.0.0.1 --port %PORT% --voice %VOICE%
endlocal
