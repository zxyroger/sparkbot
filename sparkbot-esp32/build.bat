@echo off
REM ===========================================================================
REM  SparkBot firmware build helper (Windows)
REM
REM  Why not use idf.py directly:
REM    ESP-IDF's export.ps1 / export.bat validates EVERY installed tool.
REM    If any one of them is missing or broken (e.g. riscv32-esp-elf-gdb not
REM    installed, or qemu-xtensa fails to launch), it aborts - which makes it
REM    impossible to even compile for xtensa. A build only needs the xtensa
REM    toolchain plus cmake, ninja and esptool.
REM    This script puts exactly those on PATH and sets IDF_PATH, skipping the
REM    full validation pass.
REM
REM  Usage (from this directory):
REM      build.bat                configure and build
REM      build.bat reconf         reconfigure then build (after CMake changes)
REM      build.bat clean          remove the build directory
REM      build.bat flash COM15    build and flash
REM      build.bat monitor COM15  open the serial monitor
REM      build.bat full COM15     build + flash + monitor
REM
REM  NOTE: this file is ASCII-only on purpose. A .bat saved as UTF-8 without
REM  BOM is parsed as ANSI/GBK by cmd.exe and its non-ASCII text turns into
REM  garbage commands.
REM
REM  If your tools live elsewhere, edit the paths below.
REM ===========================================================================

setlocal
cd /d "%~dp0"

REM ---- configurable paths --------------------------------------------------
if "%IDF_PATH%"=="" set IDF_PATH=D:\esp\v5.5.4\v5.5.4\esp-idf
if "%IDF_TOOLS_PATH%"=="" set IDF_TOOLS_PATH=C:\Espressif
set PY_ENV=%IDF_TOOLS_PATH%\python_env\idf5.5_py3.13_env\Scripts\python.exe
set TOOLCHAIN=%IDF_TOOLS_PATH%\tools\xtensa-esp-elf\esp-14.2.0_20260121\xtensa-esp-elf\bin
set CMAKE_BIN=%IDF_TOOLS_PATH%\tools\cmake\3.30.2\bin
set NINJA_BIN=%IDF_TOOLS_PATH%\tools\ninja\1.12.1

REM ---- sanity checks ------------------------------------------------------
if not exist "%IDF_PATH%\tools\idf.py" (
    echo [ERROR] ESP-IDF not found at: %IDF_PATH%
    echo         Edit IDF_PATH at the top of this script.
    exit /b 1
)
if not exist "%PY_ENV%" (
    echo [ERROR] IDF python env not found at: %PY_ENV%
    echo         Run this once first:
    echo           "%IDF_PATH%\install.bat" esp32s3
    exit /b 1
)
if not exist "%TOOLCHAIN%\xtensa-esp32s3-elf-gcc.exe" (
    echo [ERROR] xtensa toolchain not found at: %TOOLCHAIN%
    echo         Edit TOOLCHAIN at the top of this script.
    exit /b 1
)

set PATH=%TOOLCHAIN%;%CMAKE_BIN%;%NINJA_BIN%;%PATH%
set ESP_IDF_VERSION=5.5
set IDF_TARGET=esp32s3
set PYTHONUTF8=1

REM ESP_ROM_ELF_DIR: needed by esp_rom's gen_gdbinit.py during configure.
REM Normally export.ps1 sets it; since we skip export, set it here or the
REM bootloader configure step fails with "ESP_ROM_ELF_DIR not defined".
set ESP_ROM_ELF_DIR=%IDF_TOOLS_PATH%\tools\esp-rom-elfs\20241011

set IDF_PY="%PY_ENV%" "%IDF_PATH%\tools\idf.py"

REM ---- dispatch -----------------------------------------------------------
set ACTION=%~1
if "%ACTION%"=="" set ACTION=build

if /i "%ACTION%"=="build"      goto :do_build
if /i "%ACTION%"=="reconf"     goto :do_reconf
if /i "%ACTION%"=="clean"      goto :do_clean
if /i "%ACTION%"=="flash"      goto :do_flash
if /i "%ACTION%"=="monitor"    goto :do_monitor
if /i "%ACTION%"=="full"       goto :do_full
if /i "%ACTION%"=="menuconfig" goto :do_menuconfig

echo Unknown action: %ACTION%
echo Available: build / reconf / clean / flash / monitor / full / menuconfig
exit /b 1

:do_build
echo === Building SparkBot firmware ===
%IDF_PY% build
exit /b %errorlevel%

:do_reconf
echo === Reconfigure and build ===
%IDF_PY% reconfigure
%IDF_PY% build
exit /b %errorlevel%

:do_clean
echo === Cleaning build directory ===
if exist build rmdir /s /q build
echo Removed build\
exit /b 0

:do_flash
echo === Build and flash to port %~2 ===
%IDF_PY% -p %~2 flash
exit /b %errorlevel%

:do_monitor
echo === Serial monitor on %~2 (Ctrl+] to exit) ===
%IDF_PY% -p %~2 monitor
exit /b %errorlevel%

:do_full
echo === Build + flash + monitor on port %~2 ===
%IDF_PY% -p %~2 flash monitor
exit /b %errorlevel%

:do_menuconfig
echo === menuconfig ===
%IDF_PY% menuconfig
exit /b %errorlevel%
