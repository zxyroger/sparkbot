@echo off
REM ============================================================
REM  Start the local TTS service (edge-tts, OpenAI compatible)
REM ============================================================
REM
REM  Difference from the ASR service: NO HOME / MODELSCOPE_CACHE
REM  override is needed here. edge-tts downloads no model - it uses
REM  Microsoft's online endpoint.
REM
REM  Do NOT set a HOME override in this file: that would break anything
REM  that relies on the real home directory (on this machine it made
REM  System.Speech report "no voice installed").
REM
REM  Verified: 15 characters -> 3.53s of audio in ~1.37s (RTF 0.387).
REM  NOTE: edge-tts is an ONLINE service; it needs network access.
REM
REM  Usage:  start_tts.bat            (default 127.0.0.1:8761)
REM          start_tts.bat 9001       (custom port)
REM          start_tts.bat 8761 zh-CN-YunxiNeural   (male voice)
REM
REM  Chinese voices available:
REM    zh-CN-XiaoxiaoNeural(F)  zh-CN-XiaoyiNeural(F)
REM    zh-CN-YunjianNeural(M)   zh-CN-YunxiNeural(M)
REM    zh-CN-YunxiaNeural(M)    zh-CN-YunyangNeural(M)
REM
REM  NOTE: ASCII-ONLY ON PURPOSE - see the comment in start_asr.bat.
REM ============================================================

setlocal
set PORT=%1
if "%PORT%"=="" set PORT=8761
set VOICE=%2
if "%VOICE%"=="" set VOICE=zh-CN-XiaoxiaoNeural

cd /d "%~dp0"

echo.
echo   Starting edge-tts TTS service
echo     URL    : http://127.0.0.1:%PORT%/v1
echo     Health : http://127.0.0.1:%PORT%/health
echo     Voice  : %VOICE%
echo.
echo   Online service (Microsoft endpoint), no model download.
echo   Press Ctrl+C to stop.
echo.

".venv\Scripts\python.exe" tts_server.py --host 127.0.0.1 --port %PORT% --voice %VOICE%
endlocal
