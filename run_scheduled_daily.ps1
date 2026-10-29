param([string]$PythonPath)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'find_python.ps1')
$pythonExecutable = Resolve-RadarPython -Requested $PythonPath
if ([string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)) {
    $env:DEEPSEEK_API_KEY = [Environment]::GetEnvironmentVariable('DEEPSEEK_API_KEY', 'User')
}
Set-Location -LiteralPath $PSScriptRoot
& $pythonExecutable -B -X utf8 (Join-Path $PSScriptRoot 'main.py') --daily-run
exit $LASTEXITCODE
