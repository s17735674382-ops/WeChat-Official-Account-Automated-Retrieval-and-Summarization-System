param([string]$PythonPath)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'find_python.ps1')
$pythonExecutable = Resolve-RadarPython -Requested $PythonPath
$pwshExecutable = Join-Path $PSHOME 'pwsh.exe'
if (-not (Test-Path -LiteralPath $pwshExecutable)) { throw '请在 PowerShell 7 中运行本脚本。' }
$taskScript = Join-Path $PSScriptRoot 'run_scheduled_daily.ps1'
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -File "{0}" -PythonPath "{1}"' -f $taskScript, $pythonExecutable
$action = New-ScheduledTaskAction -Execute $pwshExecutable -Argument $arguments -WorkingDirectory $PSScriptRoot
$nextStart = (Get-Date).Date.AddHours(10)
if ($nextStart -le (Get-Date)) { $nextStart = $nextStart.AddDays(1) }
$trigger = New-ScheduledTaskTrigger -Daily -At $nextStart
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 25) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName '人大信息雷达-每日日报' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description '每日10:00采集、分析并生成日报' -Force | Select-Object TaskName,State
Write-Host '已注册，未立即运行。请检查系统时区、用户级密钥和本地配置中的付费开关。'
