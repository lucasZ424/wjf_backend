# HISTORICAL - not in use since 2026-10-26.  Superseded by `python scripts/deploy_databases.py register` (the backend runs as a
# SYSTEM task at boot and every five minutes, kept alive by scripts/supervise.py; see the fix-round 01 record in dev-docs).
# Kept only to support a rollback.  It expects to sit in scripts/ (its project path is the parent of its own folder), so move it
# back first: git mv scripts/history/register_task.ps1 scripts/
param([string]$TaskName = 'HTSG-Backend', [string]$Config = 'config/development.json', [PSCredential]$StartupCredential)
$ErrorActionPreference = 'Stop'
$projectPath = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectPath '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) { throw 'Missing project .venv' }
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) { throw 'Task already exists; inspect before replacing' }
$configPath = (Resolve-Path -LiteralPath (Join-Path $projectPath $Config)).Path
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument "-m htsg.cli run --config `"$configPath`"" -WorkingDirectory $projectPath
if ($StartupCredential) {
    $trigger = New-ScheduledTaskTrigger -AtStartup
} else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
}
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -Hidden -StartWhenAvailable
if ($StartupCredential) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -User $StartupCredential.UserName -Password $StartupCredential.GetNetworkCredential().Password -Description 'HTSG backend, startup, single instance, restart after failure'
} else {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description 'HTSG backend, logon, single instance, restart after failure'
}
Write-Output 'Task registered. PostgreSQL startup and service-account directory permissions must be configured at deployment.'
