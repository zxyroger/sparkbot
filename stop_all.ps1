<#
.SYNOPSIS
    停止由 start_all.ps1 启动的 SparkBot 进程。

.DESCRIPTION
    优先读 logs\*.pid 精确结束；pid 文件丢了就退回按命令行匹配
    （只杀命令行里含 sparkbot 的 python 进程，不会误伤别的 Python 程序）。

.PARAMETER Port
    端口，仅在需要按端口兜底查找时使用。

.EXAMPLE
    .\stop_all.ps1
#>
[CmdletBinding()]
param([int]$Port = 8765)

$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$logDir = Join-Path $root 'logs'

$stopped = @()

function Stop-Tree([int]$ProcessId, [string]$Label) {
    <# 结束一个进程及其子进程。 #>
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Stop-Tree -ProcessId $child.ProcessId -Label $Label
    }
    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($proc) {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "  [停止] $Label PID=$ProcessId" -ForegroundColor Green
        $script:stopped += $ProcessId
    }
}

Write-Host ""
Write-Host "  SparkBot 停止" -ForegroundColor Cyan
Write-Host "  " + ("-" * 46)

# ---- 1. 按 pid 文件精确停止 --------------------------------------------- #
foreach ($name in @('device', 'server')) {
    $pidFile = Join-Path $logDir "$name.pid"
    if (-not (Test-Path $pidFile)) { continue }
    $raw = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    $target = 0
    if ([int]::TryParse($raw, [ref]$target) -and $target -gt 0) {
        if (Get-Process -Id $target -ErrorAction SilentlyContinue) {
            Stop-Tree -ProcessId $target -Label $name
        } else {
            Write-Host "  [跳过] $name 的 PID=$target 已不存在" -ForegroundColor DarkGray
        }
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

# ---- 2. 兜底：按命令行匹配 ---------------------------------------------- #
$leftover = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='py.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and ($_.CommandLine -match 'sparkbot|run\.py') }

foreach ($proc in $leftover) {
    if ($stopped -contains $proc.ProcessId) { continue }
    Stop-Tree -ProcessId $proc.ProcessId -Label 'sparkbot(兜底)'
}

if ($script:stopped.Count -eq 0) {
    Write-Host "  没有找到运行中的 SparkBot 进程。" -ForegroundColor Yellow
}

# ---- 3. 报告端口状态 ---------------------------------------------------- #
Write-Host ""
$busy = $false
try {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Any, $Port)
    $listener.Start()
    $listener.Stop()
} catch { $busy = $true }

if ($busy) {
    Write-Host "  注意：端口 $Port 仍被占用，可能有别的程序在用。" -ForegroundColor Yellow
} elseif ($script:stopped.Count -gt 0) {
    Write-Host "  端口 $Port 已释放。" -ForegroundColor Green
}
Write-Host ""
