param([switch]$PrepareOnly, [switch]$NoBrowser, [switch]$SkipDesktopActions, [switch]$ResumeBackground)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$runtimeDir = Join-Path $projectRoot 'runtime'
$toolsDir = Join-Path $projectRoot 'tools'
$logDir = Join-Path $runtimeDir 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$env:PATH = "$toolsDir;$env:PATH"
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$pythonExe = Join-Path $runtimeDir '.venv\Scripts\python.exe'
$backgroundScript = Join-Path $projectRoot 'background_runtime.py'
$workbenchPort = 18766
if ($env:MOVER_WORKBENCH_PORT) { $workbenchPort = [int]$env:MOVER_WORKBENCH_PORT }
$baseUrl = "http://127.0.0.1:$workbenchPort"
$runId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + $PID + '-' + [guid]::NewGuid().ToString('N').Substring(0,8)
$logPath = Join-Path $logDir "bootstrap-$runId.log"
$keyHasher = [Security.Cryptography.SHA256]::Create()
try { $key = [BitConverter]::ToString($keyHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant()))).Replace('-','') }
finally { $keyHasher.Dispose() }
$mutex = New-Object Threading.Mutex($false, "Local\MoverV2-CmdSetup-$key")
$locked = $false
$transcribing = $false
$serverProcess = $null
$locationPushed = $false
$setupMarker = Join-Path $runtimeDir 'setup-in-progress.json'
$setupMarkerOwned = $false
$startAstrBot = $true
$backgroundEnabled = $true
$pluginInstallReport = Join-Path $logDir "plugin-install-$runId.json"

function Read-WorkbenchStatus {
    try { return Invoke-RestMethod -Uri "$baseUrl/api/status" -TimeoutSec 3 }
    catch { return $null }
}

function Test-SameInstall($Status) {
    return $Status -and $Status.workbench_root -and ([IO.Path]::GetFullPath([string]$Status.workbench_root).TrimEnd('\') -eq $projectRoot)
}

function Test-LoadedWorkbenchCurrent {
    try {
        $build = Invoke-RestMethod -Uri "$baseUrl/api/diagnostics/build" -TimeoutSec 3
        return $build.schema_version -eq 1 -and $build.workbench.matches -eq $true
    }
    catch { return $false }
}

function Invoke-LocalPost([string]$Path, [object]$Body = @{}, [int]$Timeout = 30) {
    $jsonBody = [Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 12 -Compress))
    return Invoke-RestMethod -Uri "$baseUrl$Path" -Method Post -ContentType 'application/json; charset=utf-8' -Body $jsonBody -TimeoutSec $Timeout
}

function Read-StartPreference {
    if (-not (Test-Path -LiteralPath $backgroundConfig -PathType Leaf)) { return $true }
    $latest = Get-Content -LiteralPath $backgroundConfig -Encoding UTF8 -Raw | ConvertFrom-Json
    return $latest.astrbot_desired -ne $false
}

function Read-PluginRestart([string]$AstrBotRoot) {
    $path = Join-Path $AstrBotRoot 'data\plugin_data\astrbot_plugin_message_bridge\source-restart.pending.json'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    $pending = Get-Content -LiteralPath $path -Encoding UTF8 -Raw | ConvertFrom-Json
    if (-not $pending.generation -or -not $pending.astrbot_root -or
        ([IO.Path]::GetFullPath([string]$pending.astrbot_root).TrimEnd('\') -ne [IO.Path]::GetFullPath($AstrBotRoot).TrimEnd('\'))) {
        throw 'The pending plugin restart does not identify this AstrBot directory. No service was stopped.'
    }
    return $pending
}

try {
    try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) {
        Write-Output 'Setup is already running for this folder. Waiting for it to finish...'
        for ($attempt = 0; $attempt -lt 600 -and -not $locked; $attempt++) {
            try { $locked = $mutex.WaitOne(1000) } catch [Threading.AbandonedMutexException] { $locked = $true }
        }
        if (-not $locked) { throw 'Another setup is still running. Keep its CMD window open and try again after it finishes.' }
    }
    $env:MOVER_INSTALL_ROOT = $projectRoot
    $backgroundConfig = Join-Path $runtimeDir 'background.settings.json'
    if (Test-Path -LiteralPath $backgroundConfig -PathType Leaf) {
        $savedBackground = Get-Content -LiteralPath $backgroundConfig -Encoding UTF8 -Raw | ConvertFrom-Json
        $backgroundEnabled = $savedBackground.enabled -ne $false
        $startAstrBot = $savedBackground.astrbot_desired -ne $false
        if ($savedBackground.port -and -not $env:MOVER_WORKBENCH_PORT) {
            $workbenchPort = [int]$savedBackground.port
            $baseUrl = "http://127.0.0.1:$workbenchPort"
        }
    }
    if ($ResumeBackground -and -not $backgroundEnabled) {
        Write-Output 'Background recovery is disabled in saved preferences; automatic startup was skipped.'
        return
    }
    if ($workbenchPort -lt 1024 -or $workbenchPort -gt 65535) { throw 'Invalid workbench port.' }
    $epoch = [datetime]::SpecifyKind([datetime]'1970-01-01',[DateTimeKind]::Utc)
    $processEpoch = ([Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime() - $epoch).TotalSeconds
    @{pid=$PID; process_started_at=$processEpoch} | ConvertTo-Json -Compress | Set-Content -LiteralPath $setupMarker -Encoding UTF8
    $setupMarkerOwned = $true
    Start-Transcript -LiteralPath $logPath | Out-Null
    $transcribing = $true
    Push-Location $projectRoot
    $locationPushed = $true
    Write-Output '[1/6] Checking this installation and Python runtime...'
    $existing = Read-WorkbenchStatus
    $probe = New-Object Net.Sockets.TcpClient
    $occupied = $false
    try { $probe.Connect('127.0.0.1',$workbenchPort); $occupied = $true } catch {} finally { $probe.Dispose() }
    if ($occupied -and -not (Test-SameInstall $existing)) {
        throw "Port $workbenchPort belongs to another program or installation. Close that installation first."
    }
    & (Join-Path $projectRoot 'install_astrbot.ps1') -ResultPath $pluginInstallReport
    if ($LASTEXITCODE -ne 0) { throw "AstrBot setup failed ($LASTEXITCODE)." }
    $pluginFilesChanged = $false
    $installedBridge = $null
    if (Test-Path -LiteralPath $pluginInstallReport -PathType Leaf) {
        $installedBridge = Get-Content -LiteralPath $pluginInstallReport -Encoding UTF8 -Raw | ConvertFrom-Json
    }
    if (-not $PrepareOnly) {
        # The setup marker pauses service recovery while the remaining setup runs.
        # Start the watchdog now so a later dependency/HTTP startup failure is recoverable.
        $env:MOVER_WORKBENCH_PORT = [string]$workbenchPort
        & $pythonExe -B $backgroundScript ensure $projectRoot --port $workbenchPort
        if ($LASTEXITCODE -ne 0) { throw 'Background supervisor did not start.' }
    }
    Write-Output '[2/6] Checking all Feishu login and media dependencies...'
    $larkxSource = Join-Path $runtimeDir 'LarkAgentX-runtime\LarkAgentX-d288ef4df8a42f6286c5506b7185f64430071490'
    $env:LARKX_SOURCE = $larkxSource
    $env:LARKX_HOME = Join-Path $runtimeDir 'LarkAgentX-data'
    $env:LARKX_AGENT_BACKEND = 'none'
    $env:PYTHONPATH = $larkxSource
    $importCheck = 'import loguru,requests,websockets,google.protobuf,protobuf_to_dict,sqlalchemy,qrcode,PIL,dotenv,questionary,fastapi,uvicorn,imageio_ffmpeg,pysilk; from larkx.auth import LarkAuth,QrLogin; from larkx.client import LarkClient'
    $previousErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $pythonExe -c $importCheck 2> (Join-Path $logDir "dependency-check-$runId.log")
        $dependenciesRepaired = $LASTEXITCODE -ne 0
    }
    finally { $ErrorActionPreference = $previousErrorPreference }
    if ($dependenciesRepaired) {
        & (Join-Path $projectRoot 'install_larkagentx.ps1')
        if ($LASTEXITCODE -ne 0) { throw "Feishu dependency setup failed ($LASTEXITCODE)." }
    }
    Write-Output '[3/6] Preparing the QQ connector and platform settings...'
    $targetRoot = & $pythonExe -c 'import json,workbench_server as w; print(json.dumps(str(w.astrbot_root()),ensure_ascii=True))'
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the local AstrBot directory.' }
    $targetRoot = ($targetRoot | Select-Object -Last 1) | ConvertFrom-Json
    $defaultAstrBotRoot = [IO.Path]::GetFullPath((Join-Path $runtimeDir 'AstrBot')).TrimEnd('\')
    if ([IO.Path]::GetFullPath([string]$targetRoot).TrimEnd('\') -ne $defaultAstrBotRoot) {
        # A configured custom root must also be compared before prepare copies
        # the source. The default install's report belongs to a different bot.
        & (Join-Path $projectRoot 'install.ps1') -AstrBotRoot $targetRoot -Replace -ResultPath $pluginInstallReport
        if ($LASTEXITCODE -ne 0) { throw 'Bridge plugin setup failed for the selected AstrBot directory.' }
        $installedBridge = Get-Content -LiteralPath $pluginInstallReport -Encoding UTF8 -Raw | ConvertFrom-Json
    }
    if ($installedBridge -and $installedBridge.astrbot_root -and
        ([IO.Path]::GetFullPath([string]$installedBridge.astrbot_root).TrimEnd('\') -eq [IO.Path]::GetFullPath([string]$targetRoot).TrimEnd('\'))) {
        $pluginFilesChanged = $installedBridge.changed -eq $true
    }
    $pendingRestart = Read-PluginRestart $targetRoot
    & (Join-Path $projectRoot 'install_snowluma.ps1') -AstrBotRoot $targetRoot
    if ($LASTEXITCODE -ne 0) { throw "QQ connector installation failed ($LASTEXITCODE)." }
    $prepareOutput = & $pythonExe -c 'import json,workbench_server as w; print(json.dumps(w.prepare_local_setup(),ensure_ascii=True))'
    if ($LASTEXITCODE -ne 0) { throw 'Platform configuration failed. Original login data has been preserved.' }
    $prepared = ($prepareOutput | Select-Object -Last 1) | ConvertFrom-Json
    if (($prepared.requires_restart -or $dependenciesRepaired) -and -not $pendingRestart) {
        # Configuration-only upgrades also survive PrepareOnly or a later
        # startup failure; plugin file changes were recorded before the copy.
        $pendingRestart = [pscustomobject]@{
            schema = 1; generation = [guid]::NewGuid().ToString('N')
            astrbot_root = [IO.Path]::GetFullPath([string]$targetRoot); changed_files = @()
        }
        $pendingPath = Join-Path $targetRoot 'data\plugin_data\astrbot_plugin_message_bridge\source-restart.pending.json'
        New-Item -ItemType Directory -Force -Path ([IO.Path]::GetDirectoryName($pendingPath)) | Out-Null
        $pendingTemporary = $pendingPath + '.' + $runId + '.tmp'
        try {
            [IO.File]::WriteAllText($pendingTemporary, ($pendingRestart | ConvertTo-Json -Depth 8 -Compress), (New-Object Text.UTF8Encoding($false)))
            if (Test-Path -LiteralPath $pendingPath -PathType Leaf) { [IO.File]::Replace($pendingTemporary, $pendingPath, [NullString]::Value) }
            else { [IO.File]::Move($pendingTemporary, $pendingPath) }
        }
        finally { if (Test-Path -LiteralPath $pendingTemporary -PathType Leaf) { Remove-Item -LiteralPath $pendingTemporary -Force } }
    }
    if ($PrepareOnly) {
        Write-Output "Preparation complete. Runtime: $($prepared.astrbot_root)"
        return
    }
    Write-Output '[4/6] Starting or reusing the local workbench...'
    # A same-version extraction or a direct start can leave an old workbench
    # process serving newly copied source. Old versions lack the safe build
    # endpoint; restart only the installation already identified above.
    $workbenchCurrent = $false
    if ($existing) { $workbenchCurrent = Test-LoadedWorkbenchCurrent }
    if ($existing -and ($dependenciesRepaired -or -not $workbenchCurrent)) {
        $null = Invoke-LocalPost '/api/workbench/stop' @{preserve_background=$true}
        for ($attempt=0; $attempt -lt 40 -and (Read-WorkbenchStatus); $attempt++) { Start-Sleep -Milliseconds 250 }
        if (Read-WorkbenchStatus) { throw 'The previous workbench did not stop; a duplicate was not started.' }
        $existing = $null
    }
    if (-not $existing) {
        $serverPid = & $pythonExe -B $backgroundScript launch-workbench $projectRoot --port $workbenchPort --run-id $runId
        if ($LASTEXITCODE -ne 0) { throw 'Could not start the workbench server.' }
        $serverProcess = Get-Process -Id ([int]$serverPid) -ErrorAction SilentlyContinue
        if (-not $serverProcess) { throw 'Workbench exited before becoming ready. See the per-run workbench log.' }
    }
    $status = $null
    for ($attempt=0; $attempt -lt 90; $attempt++) {
        $status = Read-WorkbenchStatus
        if (Test-SameInstall $status) { break }
        if ($serverProcess -and $serverProcess.HasExited) { throw 'Workbench exited before becoming ready. See the per-run workbench log.' }
        Start-Sleep -Seconds 1
    }
    if (-not (Test-SameInstall $status)) { throw 'Workbench did not become ready.' }
    $requiresRestart = $prepared.requires_restart -or $dependenciesRepaired -or $pluginFilesChanged -or ($null -ne $pendingRestart)
    if ($status.plugin.build.state -eq 'mismatch') { $requiresRestart = $true }
    if ($status.astrbot.running -and $requiresRestart) {
        $null = Invoke-LocalPost '/api/astrbot/stop' @{preserve_background=$true} 40
        for ($attempt=0; $attempt -lt 40; $attempt++) {
            $status = Read-WorkbenchStatus
            if (-not (Test-SameInstall $status)) { throw 'Workbench identity changed while restarting AstrBot.' }
            if (-not $status.astrbot.running) { break }
            Start-Sleep -Milliseconds 250
        }
        if ($status.astrbot.running) { throw 'The previous AstrBot process did not stop. The pending restart was preserved.' }
    }
    Write-Output '[5/6] Starting AstrBot, QQ listener and saved Feishu login...'
    $startAstrBot = Read-StartPreference
    if ($startAstrBot -and -not $status.astrbot.running) {
        $started = Invoke-LocalPost '/api/astrbot/start' @{reset_password=$false; preserve_background=$true; respect_saved_desired=$true} 40
        if ($started.skipped -and $started.reason -eq 'manually_stopped') { $startAstrBot = $false }
        elseif (-not $started.started) { throw 'AstrBot did not start.' }
    }
    for ($attempt=0; $startAstrBot -and $attempt -lt 60; $attempt++) {
        $startAstrBot = Read-StartPreference
        if (-not $startAstrBot) { break }
        $status = Read-WorkbenchStatus
        if (-not (Test-SameInstall $status)) { throw 'Workbench identity changed while waiting for AstrBot.' }
        if ($status.astrbot.running) { break }
        Start-Sleep -Seconds 1
    }
    if ($startAstrBot -and -not $status.astrbot.running) { throw 'AstrBot listener did not become ready.' }
    if ($startAstrBot -and $status.astrbot.running -and $pendingRestart) {
        $currentPending = Read-PluginRestart $targetRoot
        # An independent installer can record a newer generation while setup
        # is in progress. Never consume that installer's restart obligation.
        if ($currentPending -and $currentPending.generation -eq $pendingRestart.generation) {
            Remove-Item -LiteralPath (Join-Path $targetRoot 'data\plugin_data\astrbot_plugin_message_bridge\source-restart.pending.json') -Force
        }
    }
    if (-not $startAstrBot) { Write-Output 'AstrBot remains manually stopped. Any pending plugin restart was preserved.' }
    Write-Output '[6/6] Opening the setup page and QR login...'
    if ($startAstrBot -and -not $SkipDesktopActions) {
        if (-not $status.runtime.qq.identity_verified) {
            try { $null = Invoke-LocalPost '/api/qq/launch' }
            catch { Write-Output 'QQ is not installed or could not open. Use the QQ step on the setup page.' }
        }
        $needsFeishuLogin = -not $status.larkagentx.has_credentials -or $status.larkagentx.bridge_state -eq 'login_required'
        if ($needsFeishuLogin) { $null = Invoke-LocalPost '/api/larkagentx/qr/start' }
    }
    if (-not $NoBrowser) {
        $setupUrl = "$baseUrl/?page=setup"
        if ($startAstrBot) { $setupUrl += '&autostart=1' }
        Start-Process $setupUrl
    }
    Write-Output "Ready: $baseUrl/"
    if (-not $backgroundEnabled) {
        Write-Output 'Background recovery remains disabled in saved preferences.'
    } else {
        Write-Output 'Background recovery enabled. Display may turn off; keep Windows signed in. Confirm QQ / Feishu QR login on your phone.'
    }
    Write-Output "Setup log: $logPath"
}
catch {
    Write-Output "Setup failed: $($_.Exception.Message)"
    Write-Output "Log: $logPath"
    throw
}
finally {
    if (Test-Path -LiteralPath $pluginInstallReport -PathType Leaf) { Remove-Item -LiteralPath $pluginInstallReport -Force }
    if ($setupMarkerOwned -and (Test-Path -LiteralPath $setupMarker)) {
        $marker = Get-Content -LiteralPath $setupMarker -Raw | ConvertFrom-Json
        if ($marker.pid -eq $PID) { Remove-Item -LiteralPath $setupMarker -Force }
    }
    if ($transcribing) { Stop-Transcript | Out-Null }
    if ($locationPushed) { Pop-Location }
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
