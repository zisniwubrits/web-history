<#
.SYNOPSIS
    为浏览器历史归档器注册 / 卸载 Windows 计划任务。

.DESCRIPTION
    浏览器（尤其是 Chromium 系）默认只保留约 90 天历史，过了就永久删除。
    本脚本注册一个计划任务，定期运行 history_archive.py sync，
    把新产生的历史追加进永久归档库，这样即使浏览器自己删了，归档里还在。

    任务在当前用户登录时运行（不需要管理员权限，也不会弹出黑窗口）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install-task.ps1
    powershell -ExecutionPolicy Bypass -File install-task.ps1 -IntervalHours 4
    powershell -ExecutionPolicy Bypass -File install-task.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [string]$TaskName = 'WebHistoryArchive',
    [int]$IntervalHours = 6,
    [string]$Archive = '',
    [switch]$Uninstall,
    [switch]$RunNow
)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptPy = Join-Path $scriptDir 'history_archive.py'

if (-not (Test-Path $scriptPy)) {
    throw "history_archive.py not found: $scriptPy"
}

# ---------- 卸载 ----------
if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[OK] Scheduled task removed: $TaskName" -ForegroundColor Green
    } else {
        Write-Host "Scheduled task not found: $TaskName"
    }
    return
}

# ---------- 找 pythonw.exe（无窗口运行） ----------
$pythonw = $null
$py = Get-Command python -ErrorAction SilentlyContinue
if ($py) {
    $candidate = Join-Path (Split-Path -Parent $py.Source) 'pythonw.exe'
    if (Test-Path $candidate) { $pythonw = $candidate }
}
if (-not $pythonw) {
    $pyw = Get-Command pythonw -ErrorAction SilentlyContinue
    if ($pyw) { $pythonw = $pyw.Source }
}
if (-not $pythonw) {
    # 退而求其次：用 python.exe（运行时会闪一下窗口）
    if ($py) { $pythonw = $py.Source }
}
if (-not $pythonw) {
    throw "Python not found. Install Python 3.8+ and make sure it is on PATH."
}
Write-Host "Python  : $pythonw"

# ---------- 组织参数 ----------
$argument = '"' + $scriptPy + '" sync'
if ($Archive -ne '') {
    $argument += ' --archive "' + $Archive + '"'
    Write-Host "Archive : $Archive"
} else {
    Write-Host "Archive : $scriptDir\archive (default)"
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument $argument -WorkingDirectory $scriptDir

# 登录时启动一次，然后每隔 N 小时再跑一次
$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$triggerLogon.Delay = 'PT3M'   # 登录后等 3 分钟，避开开机高峰

$triggerRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(10) `
    -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName `
    -Description 'Incrementally archive browser history into a local SQLite database (web-history-archive)' `
    -Action $action -Trigger @($triggerLogon, $triggerRepeat) `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "[OK] Scheduled task registered: $TaskName" -ForegroundColor Green
Write-Host "     Schedule: 3 minutes after logon, then every $IntervalHours hour(s)"

$info = Get-ScheduledTask -TaskName $TaskName
Write-Host "     State   : $($info.State)"

if ($RunNow) {
    Write-Host "     Running once now..."
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    Write-Host "     State   : $((Get-ScheduledTask -TaskName $TaskName).State)"
}

Write-Host ''
Write-Host 'Common commands:' -ForegroundColor Cyan
Write-Host "  Run now      Start-ScheduledTask -TaskName $TaskName"
Write-Host "  Check state  Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "  Remove task  powershell -ExecutionPolicy Bypass -File install-task.ps1 -Uninstall"
