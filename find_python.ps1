function Resolve-RadarPython {
    param([string]$Requested)
    if ($Requested) {
        if (-not (Test-Path -LiteralPath $Requested -PathType Leaf)) { throw '指定的 Python 文件不存在。' }
        return (Resolve-Path -LiteralPath $Requested).Path
    }
    $candidates = @()
    if ($env:LOCALAPPDATA) {
        $candidates += Join-Path $env:LOCALAPPDATA 'Python\pythoncore-3.14-64\python.exe'
    }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notlike '*\WindowsApps\*') { $candidates += $command.Source }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return (Resolve-Path -LiteralPath $candidate).Path }
    }
    throw '找不到 Python；请用 -PythonPath 参数指定自己的 python.exe 完整路径。'
}
