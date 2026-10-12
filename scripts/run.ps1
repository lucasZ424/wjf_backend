param([string]$Config = 'config/development.json', [int]$Port = 8000, [string]$Database = '.local/database.json', [string]$Accounts = '.local/accounts.json')
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
& '.\.venv\Scripts\python.exe' -m htsg.cli run --config $Config --database $Database --accounts $Accounts --port $Port
exit $LASTEXITCODE
