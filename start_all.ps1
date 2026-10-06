<#
.SYNOPSIS
    Windows 上推荐用 start_all.bat —— 本脚本只是 PowerShell 用户的等价入口。

.DESCRIPTION
    功能与 start_all.bat 相同：把服务端与模拟设备各起在独立窗口里，
    进程脱离当前终端独立存在。

    注意：Windows 默认的 PowerShell 执行策略会拒绝运行未签名的 .ps1 文件
    （报「未对文件进行数字签名」）。如果遇到，用下面任一方式：

        # 方式一：不改系统设置，只对本次调用放行
        powershell -ExecutionPolicy Bypass -File .\start_all.ps1

        # 方式二：直接用 bat 版本（推荐，不受执行策略限制）
        start_all.bat

.PARAMETER Port
    服务端口，默认 8765。

.PARAMETER NoDevice
    只启动服务，不启动模拟设备（接真板子时用）。

.PARAMETER Trigger
    大于 0 时让模拟设备每隔该秒数模拟一次唤醒词。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\start_all.ps1 -Trigger 8
#>
[CmdletBinding()]
param(
    [int]$Port = 8765,
    [switch]$NoDevice,
    [double]$Trigger = 0
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

$logDir = Join-Path $root 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

function Test-PortBusy([int]$p) {
    <# 必须用与 uvicorn 相同的绑定地址判断：Windows 下
       0.0.0.0:8765 与 127.0.0.1:8765 不算冲突。
       用 New-Object 而非 [TcpListener]::new()，后者是 PS7 语法，
       Windows 自带的 PowerShell 5.1 不支持。 #>
    try {
        $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Any, $p)
        $listener.Start()
        $listener.Stop()
        return $false
    } catch {
        return $true
    }
}

function Find-Python {
    <# 优先 py 启动器，其次 python。 #>
    foreach ($cand in @('py', 'python')) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    throw '找不到 Python，请先安装 Python 3.11+ 并加入 PATH。'
}

function Start-Detached {
    <# 脱离当前终端启动一个进程。

       这里刻意用**显式参数数组**而不是拼字符串：PowerShell 的
       -ArgumentList 会按空格拆分，拼字符串会把带空格的参数拆坏
       （踩过一次：'-c' 'import time; time.sleep(300)' 被拆成多个参数，
       结果是 import 语法错误）。 #>
    param([string]$Exe, [string[]]$Arguments, [string]$OutLog)
    return Start-Process -FilePath $Exe -ArgumentList $Arguments `
        -WorkingDirectory $root -PassThru `
        -RedirectStandardOutput $OutLog -RedirectStandardError "$OutLog.err"
}

$python = Find-Python
Write-Host ""
Write-Host "  SparkBot 启动" -ForegroundColor Cyan
Write-Host "  " + ("-" * 46)
Write-Host "  Python : $python"
Write-Host "  端口   : $Port"
Write-Host "  日志   : $logDir"
Write-Host ""

# ---- 服务端 ------------------------------------------------------------- #
if (Test-PortBusy $Port) {
    Write-Host "  [跳过] 端口 $Port 已被占用 —— 服务应该已经在跑了" -ForegroundColor Yellow
    Write-Host "         直接打开 http://127.0.0.1:$Port/"
} else {
    $serverLog = Join-Path $logDir 'server.log'
    $proc = Start-Detached -Exe $python -Arguments @('run.py', '--port', "$Port") -OutLog $serverLog
    Write-Host "  [启动] 服务端 PID=$($proc.Id)" -ForegroundColor Green

    $waited = 0
    while (-not (Test-PortBusy $Port) -and $waited -lt 60) {
        Start-Sleep -Milliseconds 500
        $waited++
    }
    if (Test-PortBusy $Port) {
        Write-Host "         http://127.0.0.1:$Port/ 已就绪（等了 $([math]::Round($waited / 2, 1)) 秒）"
    } else {
        Write-Host "         启动超时，看看 $serverLog" -ForegroundColor Red
    }
}

# ---- 模拟设备 ----------------------------------------------------------- #
if ($NoDevice) {
    Write-Host "  [跳过] 未启动模拟设备（-NoDevice）" -ForegroundColor Yellow
} else {
    $deviceArgs = @('-m', 'sparkbot.mock_device', '--url', "ws://127.0.0.1:$Port/robot")
    if ($Trigger -gt 0) { $deviceArgs += @('--trigger', "$Trigger") }

    $deviceLog = Join-Path $logDir 'device.log'
    $proc = Start-Detached -Exe $python -Arguments $deviceArgs -OutLog $deviceLog
    Write-Host "  [启动] 模拟设备 PID=$($proc.Id)" -ForegroundColor Green
    Start-Sleep -Seconds 2
    if (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) {
        Write-Host "         设备进程立刻退出了，看 $deviceLog" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "  完成。控制台：http://127.0.0.1:$Port/" -ForegroundColor Cyan
Write-Host "  停止服务：  .\stop_all.ps1   或   stop_all.bat"
Write-Host ""
