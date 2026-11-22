param(
    [string]$PackagePath = '',
    [string]$TargetRoot = '',
    [switch]$NoStart,
    [switch]$NoBrowser,
    [switch]$SkipDesktopActions,
    [switch]$PrepareOnly,
    [switch]$StartHere,
    [switch]$ResumeBackground
)

$ErrorActionPreference = 'Stop'
$sourceRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$source = $sourceRoot
if ($PackagePath) { $source = [IO.Path]::GetFullPath($PackagePath) }
$registration = 'HKCU:\Software\InformationMoverV2'

function Test-Installation([string]$Path) {
    if (-not $Path) { return $false }
    $required = @('start-workbench.ps1','workbench_server.py','astrbot_plugin_message_bridge\metadata.yaml')
    foreach ($name in $required) { if (-not (Test-Path -LiteralPath (Join-Path $Path $name) -PathType Leaf)) { return $false } }
    return (Get-Content -LiteralPath (Join-Path $Path 'astrbot_plugin_message_bridge\metadata.yaml') -Raw) -match '(?m)^name:\s*astrbot_plugin_message_bridge\s*$'
}

function Test-WorkbenchPort([int]$Port) {
    $probe = New-Object Net.Sockets.TcpClient
    try {
        $pending = $probe.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(300)) { return $false }
        $probe.EndConnect($pending)
        return $true
    } catch { return $false }
    finally { $probe.Dispose() }
}

function Find-RunningInstallation {
    $port = if ($env:MOVER_WORKBENCH_PORT) { [int]$env:MOVER_WORKBENCH_PORT } else { 18766 }
    if ($port -lt 1024 -or $port -gt 65535 -or -not (Test-WorkbenchPort $port)) { return '' }
    try {
        $status = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/status" -TimeoutSec 2
        $runningRoot = [IO.Path]::GetFullPath([string]$status.workbench_root).TrimEnd('\')
        if (Test-Installation $runningRoot) {
            return $runningRoot
        }
    } catch { }
    return ''
}

function Get-OwnedWorkbench([switch]$AllowForeignPort) {
    $port = 18766
    $backgroundPath = Join-Path $TargetRoot 'runtime\background.settings.json'
    if (Test-Path -LiteralPath $backgroundPath -PathType Leaf) {
        $savedBackground = Get-Content -LiteralPath $backgroundPath -Raw | ConvertFrom-Json
        if ($savedBackground.port) { $port = [int]$savedBackground.port }
    }
    if ($env:MOVER_WORKBENCH_PORT) { $port = [int]$env:MOVER_WORKBENCH_PORT }
    if ($port -lt 1024 -or $port -gt 65535) { throw 'Invalid saved workbench port.' }
    if (-not (Test-WorkbenchPort $port)) { return $null }
    $url = "http://127.0.0.1:$port"
    try {
        $status = Invoke-RestMethod -Uri "$url/api/status" -TimeoutSec 2
        if ($status.workbench_root -and ([IO.Path]::GetFullPath([string]$status.workbench_root).TrimEnd('\') -eq $TargetRoot)) {
            return @{url=$url;port=$port;status=$status}
        }
    } catch { }
    if ($AllowForeignPort) { return $null }
    $message = "Port $port is occupied and does not identify this installation. No application files were changed."
    if ($StartHere) {
        # Match the native install launcher's visible port conflict message.
        [Console]::Error.WriteLine($message)
        exit 20
    }
    throw $message
}

if ($StartHere) {
    if ($TargetRoot -or $PackagePath) { throw 'StartHere cannot be used with a separate target or package.' }
    $TargetRoot = $sourceRoot
}
if (-not $TargetRoot) {
    if ($ResumeBackground) { $TargetRoot = $sourceRoot }
    else {
        $TargetRoot = Find-RunningInstallation
        if (-not $TargetRoot) {
            $saved = Get-ItemProperty -LiteralPath $registration -Name InstallRoot -ErrorAction SilentlyContinue
            if ($saved -and (Test-Installation ([string]$saved.InstallRoot))) { $TargetRoot = [string]$saved.InstallRoot }
            else { $TargetRoot = $sourceRoot }
        }
    }
}
$TargetRoot = [IO.Path]::GetFullPath($TargetRoot).TrimEnd('\')
if (-not (Test-Installation $TargetRoot)) { throw 'Target is not an existing Information Mover V2 installation.' }
if ($StartHere) {
    $backgroundPath = Join-Path $TargetRoot 'runtime\background.settings.json'
    if (-not $env:MOVER_WORKBENCH_PORT -and -not (Test-Path -LiteralPath $backgroundPath) -and
        (Test-WorkbenchPort 18766) -and -not (Get-OwnedWorkbench -AllowForeignPort)) {
        foreach ($candidate in 18767..18786) {
            $listener = New-Object Net.Sockets.TcpListener([Net.IPAddress]::Loopback, $candidate)
            try {
                $listener.ExclusiveAddressUse = $true
                $listener.Start()
                $env:MOVER_WORKBENCH_PORT = [string]$candidate
                Write-Output "Workbench port 18766 is occupied; using $candidate for this installation."
                break
            } catch [Net.Sockets.SocketException] { }
            finally { $listener.Stop() }
        }
    }
    $null = Get-OwnedWorkbench
}

. (Join-Path $sourceRoot 'python-runtime.ps1')
$verificationPython = Get-MoverVerificationPython -SourceRoot $sourceRoot -TargetRoot $TargetRoot
$python = $verificationPython.Executable
$pythonArgs = @($verificationPython.Arguments)
$updater = Join-Path $sourceRoot 'updater.py'
$sameDirectory = $source -eq $TargetRoot
$verified = $null
if (-not ($sameDirectory -and -not (Test-Path -LiteralPath (Join-Path $sourceRoot 'release-manifest.json')))) {
    $verifyOutput = & $python @pythonArgs -I -B $updater verify $source
    if ($LASTEXITCODE -ne 0) { throw 'Package verification failed; the existing installation has not been changed.' }
    $verified = ($verifyOutput | Select-Object -Last 1) | ConvertFrom-Json
    Write-Output ('Verified local release ' + $verified.version)
}

$runtime = Join-Path $TargetRoot 'runtime'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$pause = Join-Path $runtime 'update-in-progress.json'
$hasher = [Security.Cryptography.SHA256]::Create()
try { $key = [BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($TargetRoot.ToLowerInvariant()))).Replace('-','') }
finally { $hasher.Dispose() }
$mutex = New-Object Threading.Mutex($false, "Local\MoverV2-Update-$key")
$locked = $false
$stopped = $false
$supervisorStopped = $false
$failed = $true

function Stop-ForUpdate($Owned) {
    if ($Owned -and $Owned.status.astrbot.running -and -not $Owned.status.astrbot.managed_here) {
        throw 'AstrBot is not owned by this workbench. Stop it before updating.'
    }
    # Use the new release's helper: the installed copy can have an old or broken stop script.
    $result = & $python @pythonArgs -B (Join-Path $sourceRoot 'background_runtime.py') stop-for-update $TargetRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not pause this installation for update.' }
    $script:supervisorStopped = $result -eq 'stopped'
    if (-not $Owned) { return }
    $body = '{"preserve_background":true}'
    if ($Owned.status.astrbot.running -or $Owned.status.astrbot.managed_here) {
        $null = Invoke-RestMethod -Uri ($Owned.url + '/api/astrbot/stop') -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 45
    }
    $null = Invoke-RestMethod -Uri ($Owned.url + '/api/workbench/stop') -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 10
    for ($attempt=0; $attempt -lt 40; $attempt++) {
        if (-not (Test-WorkbenchPort $Owned.port)) { $script:stopped = $true; return }
        Start-Sleep -Milliseconds 250
    }
    throw 'This workbench did not stop; no application files were changed.'
}

function Start-Target([switch]$Quiet) {
    $startArgs = @{}
    if ($NoBrowser -or $Quiet) { $startArgs.NoBrowser = $true }
    if ($SkipDesktopActions -or $Quiet) { $startArgs.SkipDesktopActions = $true }
    if ($PrepareOnly) { $startArgs.PrepareOnly = $true }
    if ($ResumeBackground -or $Quiet) { $startArgs.ResumeBackground = $true }
    & (Join-Path $TargetRoot 'start-workbench.ps1') @startArgs
    if ($LASTEXITCODE -ne 0) { throw 'Updated files are installed, but startup failed. Read runtime\logs.' }
}

function Restore-StoppedSupervisor {
    if (-not $supervisorStopped) { return }
    # Only restore the target watchdog actually stopped by this update. The helper
    # re-reads saved preferences, including a manual opt-out made during startup.
    try {
        # The target interpreter may still be broken after rollback; reuse the one verified above.
        & $python @pythonArgs -I -B (Join-Path $sourceRoot 'background_runtime.py') ensure $TargetRoot --use-current-python
        if ($LASTEXITCODE -ne 0) { throw 'Background recovery did not start.' }
    } catch { Write-Warning ('Could not resume background recovery: ' + $_.Exception.Message) }
}

try {
    try { $locked = $mutex.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'An update is already running for this installation.' }
    $process = Get-Process -Id $PID
    $epoch = [datetime]::SpecifyKind([datetime]'1970-01-01',[DateTimeKind]::Utc)
    $marker = @{pid=$PID;process_started_at=($process.StartTime.ToUniversalTime()-$epoch).TotalSeconds;created_at=([datetime]::UtcNow-$epoch).TotalSeconds}
    [IO.File]::WriteAllText($pause,($marker | ConvertTo-Json -Compress),(New-Object Text.UTF8Encoding($false)))

    # A source directory equal to the target is a fresh extraction / normal launch.
    # Separate source releases and ZIPs are applied transactionally after owned services stop.
    if (-not $sameDirectory) {
        Stop-ForUpdate (Get-OwnedWorkbench)
        $applyOutput = & $python @pythonArgs -I -B $updater apply $source $TargetRoot
        if ($LASTEXITCODE -ne 0) { throw 'Update failed. Original program files were rolled back; login information was preserved.' }
        $result = ($applyOutput | Select-Object -Last 1) | ConvertFrom-Json
        Write-Output ('Updated ' + $result.changed_files + ' application files in ' + $TargetRoot)
        Write-Output ('Previous program backup: ' + $result.backup)
    }
    elseif ($verified) {
        # Extracting onto the active directory changes files on disk, not the
        # Python code already imported by a running workbench.
        $owned = Get-OwnedWorkbench
        if ($owned) {
            $loadedCurrent = $false
            try {
                $build = Invoke-RestMethod -Uri ($owned.url + '/api/diagnostics/build') -TimeoutSec 3
                $loadedCurrent = $build.schema_version -eq 1 -and $build.workbench.matches -eq $true
            } catch { }
            if ([string]$owned.status.version -ne [string]$verified.version -or -not $loadedCurrent) {
                Stop-ForUpdate $owned
            }
        }
    }
    $savedRoot = $null
    if ($StartHere) {
        $saved = Get-ItemProperty -LiteralPath $registration -Name InstallRoot -ErrorAction SilentlyContinue
        $savedRoot = [string]$saved.InstallRoot
    }
    if (-not (Test-Installation $savedRoot)) {
        New-Item -Path $registration -Force | Out-Null
        New-ItemProperty -LiteralPath $registration -Name InstallRoot -Value $TargetRoot -PropertyType String -Force | Out-Null
    }
    $failed = $false
}
finally {
    if ($locked) {
        if (Test-Path -LiteralPath $pause -PathType Leaf) { Remove-Item -LiteralPath $pause -Force }
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
    if ($failed -and $stopped -and -not $NoStart) {
        try { Start-Target -Quiet } catch { Write-Warning ('Could not resume previous services: ' + $_.Exception.Message) }
    }
    if ($failed -and $supervisorStopped) {
        # Rollback can restore a start script that still has the old PowerShell quoting bug.
        Restore-StoppedSupervisor
    }
}
if (-not $NoStart) {
    try { Start-Target }
    catch {
        $startupFailure = $_
        Restore-StoppedSupervisor
        throw $startupFailure
    }
}
