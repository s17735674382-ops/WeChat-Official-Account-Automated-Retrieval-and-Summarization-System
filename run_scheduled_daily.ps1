param([string]$PythonPath)
# 任务计划程序入口；运行时从当前用户环境读取密钥，不打印或保存密钥。
$ErrorActionPreference = 'Stop'
if ((Get-Date).Hour -lt 10) { exit 0 }
$logDir = Join-Path $PSScriptRoot 'logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$launchLog = Join-Path $logDir ((Get-Date -Format 'yyyy-MM-dd') + '-launcher.log')
Add-Content -LiteralPath $launchLog -Encoding UTF8 -Value ((Get-Date -Format o) + ' 启动每日任务')
. (Join-Path $PSScriptRoot 'find_python.ps1')
$pythonExecutable = Resolve-RadarPython -Requested $PythonPath
if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
    $env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
}
Set-Location -LiteralPath $PSScriptRoot
try {
    # 登录补跑与常规触发共用入口；当天已有正式报告则不重复发起请求。
    $statePath = Join-Path $PSScriptRoot 'reports\status.json'
    if (Test-Path -LiteralPath $statePath) {
        try {
            $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($state.report_date -eq (Get-Date -Format 'yyyy-MM-dd') -and $state.status -in @('complete','partial')) {
                $md = Join-Path $PSScriptRoot ('reports\' + $state.report_date + '.md')
                $html = Join-Path $PSScriptRoot ('reports\' + $state.report_date + '.html')
                if ((Test-Path -LiteralPath $md) -and (Test-Path -LiteralPath $html) -and
                    (Get-FileHash -LiteralPath $md -Algorithm SHA256).Hash -eq $state.markdown_sha256 -and
                    (Get-FileHash -LiteralPath $html -Algorithm SHA256).Hash -eq $state.html_sha256) {
                    Add-Content -LiteralPath $launchLog -Encoding UTF8 -Value '当天报告已生成，跳过自动重复运行'
                    exit 0
                }
            }
        } catch { } # 状态损坏则由主程序可靠读取数据库后重建。
    }
    & $pythonExecutable -B -X utf8 (Join-Path $PSScriptRoot 'main.py') --daily-run
    $result = $LASTEXITCODE
    Add-Content -LiteralPath $launchLog -Encoding UTF8 -Value ((Get-Date -Format o) + ' Python退出码：' + $result)
    exit $result
} catch {
    Add-Content -LiteralPath $launchLog -Encoding UTF8 -Value '启动失败：请检查解释器路径、程序权限及项目文件；原始异常已隐藏'
    exit 1
}
