$ErrorActionPreference = 'Stop'
$runtimeDir = Join-Path $PSScriptRoot 'runtime\SnowLuma'
$appDir = Join-Path $runtimeDir 'app'
$node = Join-Path $appDir 'node.exe'
$entry = Join-Path $appDir 'index.mjs'
$logDir = Join-Path $runtimeDir 'logs'

& (Join-Path $PSScriptRoot 'install_snowluma.ps1')

$probe = [System.Net.Sockets.TcpClient]::new()
try {
    $probe.Connect('127.0.0.1', 5099)
    Write-Output 'SnowLuma is already listening at http://127.0.0.1:5099/'
    exit 0
}
catch { }
finally { $probe.Dispose() }

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
Start-Process -FilePath $node -ArgumentList @($entry) -WorkingDirectory $appDir -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logDir 'runtime.stdout.log') -RedirectStandardError (Join-Path $logDir 'runtime.stderr.log') | Out-Null

$ready = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    $probe = [System.Net.Sockets.TcpClient]::new()
    try { $probe.Connect('127.0.0.1', 5099); $ready = $true; break }
    catch { Start-Sleep -Milliseconds 250 }
    finally { $probe.Dispose() }
}
if (-not $ready) { throw "SnowLuma did not start; inspect $logDir" }
Write-Output 'SnowLuma is listening at http://127.0.0.1:5099/'
