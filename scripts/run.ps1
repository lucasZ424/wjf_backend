param([string]$Config = 'config/development.json', [int]$Port = 8000)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
& '.\.venv\Scripts\python.exe' -m htsg.cli run --config $Config --port $Port
exit $LASTEXITCODE
