@echo off
REM ===========================================================================
REM  SparkBot 一键启动（Windows）
REM
REM  双击本文件即可。它会：
REM    1. 切到脚本所在目录（避免「双击后工作目录不对」的老问题）
REM    2. 检查依赖是否已安装，没装就自动装到 .vendor
REM    3. 启动 PC 端服务，并在浏览器里打开控制台
REM
REM  关掉这个窗口 = 停止服务。
REM ===========================================================================

setlocal
cd /d "%~dp0"
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo.
echo   ============================================
echo     SparkBot - ESP32-S3 机器人 PC 端 Agent
echo   ============================================
echo.

REM ---- 找一个可用的 Python ------------------------------------------------
set PY=
where py >nul 2>&1 && set PY=py -3
if not defined PY (
    where python >nul 2>&1 && set PY=python
)
if not defined PY (
    echo   [错误] 没有找到 Python。
    echo          请先安装 Python 3.11 或更高版本： https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

REM ---- 检查依赖，缺失则自动安装 -------------------------------------------
%PY% -c "import sys; sys.path.insert(0,'.'); from sparkbot.paths import ensure_path, missing_dependencies; ensure_path(); sys.exit(1 if missing_dependencies() else 0)" >nul 2>&1
if errorlevel 1 (
    echo   首次运行，正在安装依赖到 .vendor （不会改动系统 Python）...
    echo.
    %PY% -m pip install --disable-pip-version-check --target .vendor -r requirements.txt
    if errorlevel 1 (
        echo.
        echo   [错误] 依赖安装失败。请手动执行：
        echo          %PY% -m pip install --target .vendor -r requirements.txt
        echo.
        pause
        exit /b 1
    )
    echo.
    echo   依赖安装完成。
    echo.
)

REM ---- 延迟打开浏览器（等服务真正起来） -----------------------------------
start "" cmd /c "timeout /t 4 /nobreak >nul & start http://127.0.0.1:8765/"

REM ---- 启动服务（前台运行，Ctrl+C 或关窗口即停止） ------------------------
echo   正在启动服务... 浏览器会自动打开 http://127.0.0.1:8765/
echo   停止服务：按 Ctrl+C，或直接关闭本窗口。
echo.
%PY% run.py %*

echo.
echo   服务已停止。
pause
endlocal
