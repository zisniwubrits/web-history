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
    throw "找不到 history_archive.py：$scriptPy"
}

# ---------- 卸载 ----------
if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[OK] 已删除计划任务: $TaskName" -ForegroundColor Green
    } else {
        Write-Host "计划任务不存在: $TaskName"
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
    throw "没有找到 python，请先安装 Python 3.8+ 并加入 PATH。"
}
Write-Host "Python      : $pythonw"

# ---------- 组织参数 ----------
$argument = '"' + $scriptPy + '" sync'
if ($Archive -ne '') {
    $argument += ' --archive "' + $Archive + '"'
    Write-Host "归档目录    : $Archive"
} else {
    Write-Host "归档目录    : $scriptDir\archive （默认）"
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
    -Description '把浏览器历史增量归档到本地 SQLite，永久保存（web-history-archive）' `
    -Action $action -Trigger @($triggerLogon, $triggerRepeat) `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "[OK] 计划任务已注册: $TaskName" -ForegroundColor Green
Write-Host "     运行时机: 登录后 3 分钟，之后每 $IntervalHours 小时一次"

$info = Get-ScheduledTask -TaskName $TaskName
Write-Host "     状态    : $($info.State)"

if ($RunNow) {
    Write-Host "     正在立即运行一次 …"
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    Write-Host "     任务状态: $((Get-ScheduledTask -TaskName $TaskName).State)"
}

Write-Host ''
Write-Host '常用操作:' -ForegroundColor Cyan
Write-Host "  立即运行   Start-ScheduledTask -TaskName $TaskName"
Write-Host "  查看状态   Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
Write-Host "  删除任务   powershell -ExecutionPolicy Bypass -File install-task.ps1 -Uninstall"
