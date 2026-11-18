param([switch]$ForUpdate)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$pythonExe = Join-Path $projectRoot 'runtime\.venv\Scripts\python.exe'
$port = 18766
$backgroundFile = Join-Path $projectRoot 'runtime\background.settings.json'
if (Test-Path -LiteralPath $backgroundFile) {
    $saved = Get-Content -LiteralPath $backgroundFile -Raw | ConvertFrom-Json
    if ($saved.port) { $port = [int]$saved.port }
}
if ($env:MOVER_WORKBENCH_PORT) { $port = [int]$env:MOVER_WORKBENCH_PORT }
if ($port -lt 1024 -or $port -gt 65535) { throw 'Invalid workbench port.' }
if (Test-Path -LiteralPath $pythonExe) {
    $action = if ($ForUpdate) { 'stop-for-update' } else { 'disable' }
    $null = & $pythonExe -B (Join-Path $projectRoot 'background_runtime.py') $action $projectRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not safely stop background recovery.' }
}
$baseUrl = "http://127.0.0.1:$port"
try { $status = Invoke-RestMethod -Uri "$baseUrl/api/status" -TimeoutSec 4 }
catch { Write-Output 'Workbench is not responding; no unrelated processes were stopped.'; return }
if (-not $status.workbench_root -or [IO.Path]::GetFullPath([string]$status.workbench_root).TrimEnd('\') -ne $projectRoot) {
    throw 'This port belongs to another installation. No processes were stopped.'
}
$body = @{preserve_background=[bool]$ForUpdate} | ConvertTo-Json -Compress
$null = Invoke-RestMethod -Uri "$baseUrl/api/astrbot/stop" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 40
$null = Invoke-RestMethod -Uri "$baseUrl/api/workbench/stop" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 10
Write-Output 'This installation has stopped. Saved login data and rules are retained.'
