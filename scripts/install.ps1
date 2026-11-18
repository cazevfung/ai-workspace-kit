# AI 工作区套件 · Windows 安装入口
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$py = $null
if (Get-Command py -ErrorAction SilentlyContinue) { $py = "py" }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $py = "python" }
if (-not $py) {
    Write-Error "需要 Python 3.10 或更高版本，但没有找到 py / python。"
    exit 2
}

& $py install.py @args
exit $LASTEXITCODE
